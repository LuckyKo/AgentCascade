"""
Per-slot FIFO queue scheduler — Phase 1 of the API scheduler queue refactor.

Replaces semaphore-based blocking with a ticket-based FIFO wait queue per slot pool.
Semaphores are removed; capacity is checked via len(_running) < capacity under a threading.Condition.

Key design decisions:
- One SlotPool per slot_key (e.g., '_shared_sequential_slot_' or api_base).
- OrderedDict[ticket_id → QueueTicket] for _waiters: FIFO by insertion, O(1) removal.
- Single threading.Condition per pool — ALL mutations under this lock.
- Strict FIFO: only head waiter can be granted (must be next(iter(_waiters))).
- Wait loop ticks every 1s for interruptibility (termination checks).
- No semaphores — permits are explicit entries in _running.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent_cascade.agent_instance import AgentInstance


# ──────────────────────────────────────────────────────────────────────────────
# Configuration constants (configurable via environment variables)
# ──────────────────────────────────────────────────────────────────────────────

QUEUE_WAIT_TIMEOUT: int = int(os.getenv('QWEN_AGENT_SLOT_QUEUE_TIMEOUT', 300))
"""Default timeout for waiting in the slot queue. Configurable via QWEN_AGENT_SLOT_QUEUE_TIMEOUT."""


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class SlotQueueTimeout(TimeoutError):
    """Raised when a waiter times out waiting for a slot.

    Carries diagnostic information about the ticket and pool state at time of timeout.
    Subclasses TimeoutError so callers (e.g., EndpointScheduler.acquire) can catch it
    with a plain `except TimeoutError` and wrap it in a holder-aware message.
    """
    def __init__(self, ticket: 'QueueTicket', message: Optional[str] = None):
        self.ticket = ticket
        super().__init__(message or f"Slot queue timeout for ticket {ticket.ticket_id} "
                                     f"(agent={ticket.agent_name}, instance={ticket.instance_name})")


class SlotCancelled(Exception):
    """Raised when a waiter's ticket is cancelled (e.g., agent terminated/dismissed).
    
    This is NOT an error — it's a clean abort signal. Callers should catch this
    and return early without retrying or logging as a failure.
    """
    def __init__(self, ticket: Optional['QueueTicket'] = None, message: Optional[str] = None):
        self.ticket = ticket
        super().__init__(message or f"Slot queue cancelled for ticket {ticket.ticket_id if ticket else 'unknown'}")


# ──────────────────────────────────────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────────────────────────────────────

# Global monotonic counter for unique ticket IDs across all pools.
_ticket_counter = itertools.count()


@dataclass
class QueueTicket:
    """A ticket representing a waiter in the slot queue.
    
    Each agent waiting for a slot gets exactly one ticket. Tickets are ordered by
    insertion into the pool's OrderedDict (FIFO). The ticket carries cancellation
    and grant signaling events.
    """
    ticket_id: int = field(default_factory=lambda: next(_ticket_counter))
    seq: int = 0
    agent_name: str = ""
    instance_name: str = ""
    agent_class: str = ""
    slot_key: str = ""
    created_at: float = field(default_factory=time.monotonic)
    deadline: float = 0.0
    cancelled: threading.Event = field(default_factory=threading.Event)
    granted: threading.Event = field(default_factory=threading.Event)
    holder_ctx: Dict = field(default_factory=dict)


@dataclass
class SlotHolder:
    """Represents a running agent that currently holds a slot permit.
    
    One entry per active instance in _running[instance_name]. The acquisition_id
    enables idempotent release — stale releases with wrong IDs are ignored.
    """
    agent_name: str
    instance_name: str
    acquisition_id: int
    granted_at: float = field(default_factory=time.monotonic)


# ──────────────────────────────────────────────────────────────────────────────
# Core Class
# ──────────────────────────────────────────────────────────────────────────────

class SlotPool:
    """FIFO slot pool.
    
    Manages a queue of waiters for a specific slot key (e.g., '_shared_sequential_slot_'
    or an api_base). Provides strict FIFO ordering and thread-safe acquire/release/cancel.
    
    Thread-safety: ALL mutations to _waiters and _running occur under
    the single threading.Condition (_cond).
    """
    
    __slots__ = ("key", "capacity", "_waiters", "_running", 
                 "_cond", "_seq_counter", "_acquisition_counter")
    
    def __init__(self, key: str, capacity: int):
        self.key = key
        self.capacity = capacity if capacity > 0 else float('inf')
        
        self._waiters: OrderedDict[int, QueueTicket] = OrderedDict()
        self._running: Dict[str, SlotHolder] = {}
        
        self._cond = threading.Condition(threading.RLock())
        self._seq_counter = itertools.count()
        self._acquisition_counter = itertools.count()

    def acquire(self, instance_name: str, agent_class: str,
                timeout: Optional[float] = None, **kwargs) -> Callable[[], None]:
        """Acquire a slot permit from this pool, waiting in FIFO order if necessary.
        
        Algorithm (all under _cond):
        1. Fast path: if capacity free → grant immediately.
        2. Slow path: enqueue ticket, wait with 1s ticks for interruptibility.
           - Wait until: capacity frees and ticket is head of queue.
           - On wakeup, double-check cancelled flag.
           - Only head waiter proceeds; non-head re-waits immediately.
        3. Returns a release callback bound to the granted SlotHolder.
        """
        if self.capacity == float('inf'):
            return lambda: None
        
        if timeout is None:
            timeout = QUEUE_WAIT_TIMEOUT
        
        with self._cond:
            # Fast path: capacity available
            if len(self._running) < self.capacity:
                holder = _grant(self, instance_name, agent_class)
                return _make_release_cb(self, holder)
            
            # Slow path: enqueue as waiter
            ticket = QueueTicket(
                seq=next(self._seq_counter),
                agent_name=instance_name,
                instance_name=instance_name,
                agent_class=agent_class,
                slot_key=self.key,
                created_at=time.monotonic(),
                deadline=time.monotonic() + timeout,
            )
            
            self._waiters[ticket.ticket_id] = ticket
            deadline = ticket.deadline
            
            while not ticket.cancelled.is_set():
                remaining = deadline - time.monotonic()
                
                if remaining <= 0:
                    _remove_ticket(self, ticket)
                    _log_acquire_timeout(self, ticket)
                    raise SlotQueueTimeout(ticket)
                
                # Wait until predicate is true: capacity free + we are head.
                granted = self._cond.wait_for(
                    lambda: (_is_head(self, ticket.ticket_id) and len(self._running) < self.capacity),
                    timeout=min(remaining, 1.0)
                )
                
                if not granted:
                    continue
                
                if ticket.cancelled.is_set():
                    _remove_ticket(self, ticket)
                    raise SlotCancelled(ticket)
                
                if _is_head(self, ticket.ticket_id):
                    self._waiters.pop(ticket.ticket_id)
                    holder = _grant(self, instance_name, agent_class)
                    ticket.granted.set()
                    return _make_release_cb(self, holder)
                
                continue
            
            _remove_ticket(self, ticket)
            raise SlotCancelled(ticket)

    def release(self, holder: SlotHolder) -> None:
        """Release a slot permit held by the given holder."""
        with self._cond:
            existing = self._running.get(holder.instance_name)
            if existing is None or existing.acquisition_id != holder.acquisition_id:
                return
            
            del self._running[holder.instance_name]
            self._cond.notify_all()

    def create_held_slot(self, agent_name: str, instance_name: Optional[str] = None) -> SlotHolder:
        """Create a held slot for testing purposes."""
        inst = instance_name or agent_name
        with self._cond:
            if inst in self._running:
                raise RuntimeError(f"Slot already held by '{inst}'")
            holder = SlotHolder(
                agent_name=agent_name,
                instance_name=inst,
                acquisition_id=next(self._acquisition_counter),
                granted_at=time.monotonic(),
            )
            self._running[inst] = holder
            return holder

    def cancel(self, ticket_id: Optional[int] = None, agent_name: Optional[str] = None) -> bool:
        """Cancel a waiter's ticket, removing it from the queue."""
        with self._cond:
            if ticket_id is not None and ticket_id in self._waiters:
                self._waiters[ticket_id].cancelled.set()
                self._waiters.pop(ticket_id)
                self._cond.notify_all()
                return True
            
            elif agent_name is not None:
                cancelled_ids = [
                    tid for tid, t in self._waiters.items()
                    if t.instance_name == agent_name
                ]
                for tid in cancelled_ids:
                    self._waiters[tid].cancelled.set()
                    self._waiters.pop(tid)
                
                if cancelled_ids:
                    self._cond.notify_all()
                return len(cancelled_ids) > 0
            
            return False

    def terminate_for_agent(self, agent_name: str) -> Tuple[int, int]:
        """Full cleanup for a terminated agent: cancel tickets."""
        with self._cond:
            cancelled_ids = [
                tid for tid, t in self._waiters.items()
                if t.instance_name == agent_name
            ]
            for tid in cancelled_ids:
                self._waiters[tid].cancelled.set()
                self._waiters.pop(tid)
            
            if cancelled_ids:
                self._cond.notify_all()
            
            return len(cancelled_ids), 0

    def get_status(self) -> Dict:
        """Return current pool status for diagnostics."""
        now = time.monotonic()
        with self._cond:
            return {
                "key": self.key,
                "capacity": self.capacity if self.capacity != float('inf') else -1,
                "running_count": len(self._running),
                "waiting_count": len(self._waiters),
                "waiters": [
                    {
                        "ticket_id": t.ticket_id,
                        "seq": t.seq,
                        "instance_name": t.instance_name,
                        "agent_class": t.agent_class,
                        "wait_time": round(now - t.created_at, 2),
                        "remaining_timeout": max(0, round(t.deadline - now, 2)),
                    }
                    for t in self._waiters.values()
                ],
                "holders": [
                    {
                        "instance_name": h.instance_name,
                        "agent_name": h.agent_name,
                        "acquisition_id": h.acquisition_id,
                        "held_duration": round(now - h.granted_at, 2),
                    }
                    for h in self._running.values()
                ],
            }


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers (called only under pool._cond)
# ──────────────────────────────────────────────────────────────────────────────

def _grant(pool: SlotPool, instance_name: str, agent_class: str) -> SlotHolder:
    """Grant a permit to the requesting agent. Must be called under pool._cond."""
    acquisition_id = next(pool._acquisition_counter)
    holder = SlotHolder(
        agent_name=instance_name,
        instance_name=instance_name,
        acquisition_id=acquisition_id,
        granted_at=time.monotonic(),
    )
    pool._running[instance_name] = holder
    return holder


def _make_release_cb(pool: SlotPool, holder: SlotHolder) -> Callable[[], None]:
    """Create a release callback bound to the given holder."""
    def release():
        pool.release(holder)
    return release


def _remove_ticket(pool: SlotPool, ticket: QueueTicket) -> None:
    """Remove a ticket from the waiters queue. O(1) via OrderedDict.pop."""
    pool._waiters.pop(ticket.ticket_id, None)


def _is_head(pool: SlotPool, ticket_id: int) -> bool:
    """Check if the given ticket is at the head of the queue. O(1)."""
    if not pool._waiters:
        return False
    return next(iter(pool._waiters)) == ticket_id


def _log_acquire_timeout(pool: SlotPool, ticket: QueueTicket) -> None:
    """Log diagnostic information when acquire() times out. Must be called under pool._cond."""
    now = time.monotonic()
    wait_time = now - ticket.created_at
    logger.warning(
        f"[SLOTPOOL] Acquire timeout on pool '{pool.key}' for ticket {ticket.ticket_id} "
        f"(agent={ticket.instance_name}, wait_time={wait_time:.1f}s): "
        f"running={len(pool._running)}/{pool.capacity}, waiters={len(pool._waiters)}"
    )
