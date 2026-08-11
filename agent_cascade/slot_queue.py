"""
Per-slot FIFO queue scheduler — Phase 1 of the API scheduler queue refactor.

Replaces semaphore-based blocking with a ticket-based FIFO wait queue per slot pool,
plus a reservation registry for ancestor-chain protection. Semaphores are removed;
capacity is checked via len(_running) < capacity under a threading.Condition.

Key design decisions:
- One SlotPool per slot_key (e.g., '_shared_sequential_slot_' or api_base).
- OrderedDict[ticket_id → QueueTicket] for _waiters: FIFO by insertion, O(1) removal.
- Single threading.Condition per pool — ALL mutations under this lock.
- Strict FIFO: only head waiter can be granted (must be next(iter(_waiters))).
- Wait loop ticks every 1s for interruptibility (termination checks).
- No semaphores — permits are explicit entries in _running.

Plan reference: plans/api_scheduler_queue_refactor_plan.md
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from agent_cascade.agent_instance import AgentInstance


# ──────────────────────────────────────────────────────────────────────────────
# Configuration constants (configurable via environment variables)
# ──────────────────────────────────────────────────────────────────────────────

QUEUE_WAIT_TIMEOUT: int = int(os.getenv('QWEN_AGENT_SLOT_QUEUE_TIMEOUT', 300))
"""Default timeout for waiting in the slot queue. Configurable via QWEN_AGENT_SLOT_QUEUE_TIMEOUT."""

RESERVATION_TIMEOUT: int = int(os.getenv('QWEN_AGENT_RESERVATION_TIMEOUT', 300))
"""Timeout for reservations before they are considered stale. Configurable via QWEN_AGENT_RESERVATION_TIMEOUT."""


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class SlotQueueTimeout(Exception):
    """Raised when a waiter times out waiting for a slot.
    
    Carries diagnostic information about the ticket and pool state at time of timeout.
    Distinct from generic TimeoutError so callers can handle queue-specific timeouts.
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
    and grant signaling events, plus context for reservation checks.
    
    Attributes:
        ticket_id: Globally unique monotonic ID (from itertools.count).
        seq: Pool-local sequence number for FIFO ordering diagnostics.
        agent_name: Display name of the requesting agent (for logs/diagnostics).
        instance_name: Unique instance identifier (e.g., 'worker1').
        agent_class: Agent class type (e.g., 'coder', 'researcher').
        slot_key: The pool key this ticket is waiting on.
        created_at: Monotonic timestamp when ticket was created.
        deadline: Absolute monotonic time when this ticket should timeout.
        cancelled: Event set when ticket is cancelled externally (terminate/dismiss).
        granted: Event set when ticket is granted a permit.
        holder_ctx: Context dict carrying parent_chain and reservation_token.
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
    
    Attributes:
        agent_name: Display name of the holder agent.
        instance_name: Unique instance identifier (key in _running dict).
        acquisition_id: Unique per grant; prevents stale/double releases.
        granted_at: Monotonic timestamp when permit was granted.
        reservation_token: Token if this grant was via ancestor reservation, else None.
    """
    agent_name: str
    instance_name: str
    acquisition_id: int
    granted_at: float = field(default_factory=time.monotonic)
    reservation_token: Optional[str] = None


@dataclass
class Reservation:
    """A reservation on a slot pool, protecting it from unrelated grants.
    
    When an agent goes SLEEPING or spawns an async child, it reserves its pool so
    only its descendants (in the ancestor chain) can be granted while it waits.
    
    Attributes:
        token: Unique identifier for this reservation (e.g., "res-agentX-42").
        agent_name: The reserving agent's display name.
        ancestor_chain: Instance names in the parent chain including self.
        slot_key: The pool key this reservation applies to.
        created_at: Monotonic timestamp when reservation was created.
        reason: Why the reservation exists ("sleeping", "async_child", "sync_yield").
    """
    token: str
    agent_name: str
    ancestor_chain: Tuple[str, ...]
    slot_key: str
    created_at: float = field(default_factory=time.monotonic)
    reason: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Core Class
# ──────────────────────────────────────────────────────────────────────────────

class SlotPool:
    """FIFO slot pool with reservation support.
    
    Manages a queue of waiters for a specific slot key (e.g., '_shared_sequential_slot_'
    or an api_base). Provides strict FIFO ordering, thread-safe acquire/release/cancel,
    and ancestor-chain-aware reservations.
    
    Thread-safety: ALL mutations to _waiters, _running, _reservations occur under
    the single threading.Condition (_cond). No other locks are held while acquiring
    _cond (lock ordering protocol: instance → scheduler façade → pool condition).
    
    FIFO guarantee: Only the head waiter (next(iter(_waiters))) can be granted. When
    notified via notify_all(), each waiter re-checks under _cond whether it is head
    AND capacity is free AND no blocking reservation applies. Non-head waiters
    immediately re-wait. This yields strict FIFO with plain condition variables.
    
    O(1) removal: _waiters is an OrderedDict[ticket_id → QueueTicket]. Cancel and
    timeout remove via pop(ticket_id), which is O(1). Head access via next(iter())
    is also O(1). This ensures mass cancellation of 100+ waiters completes quickly.
    
    Attributes:
        key: The slot_key this pool manages.
        capacity: Max concurrent holders (1 for sequential, N for parallel, inf for unlimited).
        _waiters: OrderedDict mapping ticket_id → QueueTicket, FIFO by insertion order.
        _running: Dict mapping instance_name → SlotHolder for current permit owners.
        _reservations: Dict mapping reservation_token → Reservation for active reservations.
        _cond: threading.Condition guarding all pool mutations.
        _seq_counter: Pool-local counter for ticket sequence numbers.
        _acquisition_counter: Global counter for unique acquisition IDs.
    """
    
    __slots__ = ("key", "capacity", "_waiters", "_running", "_reservations", 
                 "_cond", "_seq_counter", "_acquisition_counter")
    
    def __init__(self, key: str, capacity: int):
        self.key = key
        self.capacity = capacity if capacity > 0 else float('inf')
        
        # OrderedDict: ticket_id → QueueTicket. Insertion order = FIFO queue order.
        # Python 3.7+ guarantees insertion order. Head = next(iter(_waiters)).
        self._waiters: OrderedDict[int, QueueTicket] = OrderedDict()
        
        # Dict: instance_name → SlotHolder. Tracks currently running permit owners.
        self._running: Dict[str, SlotHolder] = {}
        
        # Dict: reservation_token → Reservation. Multiple reservations per agent allowed.
        self._reservations: Dict[str, Reservation] = {}
        
        # Single condition variable for ALL mutations. Using RLock to allow reentrant waits.
        self._cond = threading.Condition(threading.RLock())
        
        # Pool-local sequence counter for ticket ordering diagnostics.
        self._seq_counter = itertools.count()
        
        # Shared acquisition ID counter across all pools (module-level would work too,
        # but keeping it here for encapsulation). Ensures unique IDs per grant.
        self._acquisition_counter = itertools.count()

    def acquire(self, instance_name: str, agent_class: str, ancestor_chain: Tuple[str, ...],
                timeout: Optional[float] = None) -> Callable[[], None]:
        """Acquire a slot permit from this pool, waiting in FIFO order if necessary.
        
        Algorithm (all under _cond):
        1. Fast path: if capacity free AND no blocking reservation → grant immediately.
        2. Slow path: enqueue ticket, wait with 1s ticks for interruptibility.
           - Wait until: capacity frees, reservation clears, ticket is head of queue.
           - On wakeup, double-check cancelled flag (cancel-after-grant guard).
           - Only head waiter proceeds; non-head re-waits immediately.
        3. Returns a release callback bound to the granted SlotHolder.
        
        Args:
            instance_name: The requesting agent's instance name.
            agent_class: The requesting agent's class type.
            ancestor_chain: Instance names in parent chain (includes self).
                Used for reservation self-exemption checks.
            timeout: Max seconds to wait. Defaults to QUEUE_WAIT_TIMEOUT.
        
        Returns:
            A callable release() that frees the permit when called.
        
        Raises:
            SlotQueueTimeout: If timeout expires before permit is granted.
            SlotCancelled: If ticket is cancelled while waiting (e.g., agent terminated).
        """
        if self.capacity == float('inf'):
            # Unlimited capacity — no queueing needed, return a no-op release.
            return lambda: None
        
        if timeout is None:
            timeout = QUEUE_WAIT_TIMEOUT
        
        with self._cond:
            # Fast path: capacity available AND no blocking reservation
            if (len(self._running) < self.capacity 
                and not _blocked_by_reservation(self, ancestor_chain)):
                holder = _grant(self, instance_name, agent_class, ancestor_chain)
                return _make_release_cb(self, holder)
            
            # Slow path: enqueue as waiter
            ticket = QueueTicket(
                seq=next(self._seq_counter),
                agent_name=instance_name,  # Phase 2 will pass separate display name; instance_name used as placeholder
                instance_name=instance_name,
                agent_class=agent_class,
                slot_key=self.key,
                created_at=time.monotonic(),
                deadline=time.monotonic() + timeout,
                holder_ctx={"ancestor_chain": ancestor_chain},
            )
            
            # Insert at tail of OrderedDict — FIFO order by insertion.
            self._waiters[ticket.ticket_id] = ticket
            deadline = ticket.deadline
            
            while not ticket.cancelled.is_set():
                remaining = deadline - time.monotonic()
                
                if remaining <= 0:
                    # Timeout: remove ticket from queue (O(1) via OrderedDict.pop)
                    _remove_ticket(self, ticket)
                    raise SlotQueueTimeout(ticket)
                
                # Wait until predicate is true: capacity free + no blocking reservation + we are head.
                # Use wait_for with 1s tick for interruptibility (termination checks).
                granted = self._cond.wait_for(
                    lambda: (_is_head(self, ticket.ticket_id)
                             and len(self._running) < self.capacity
                             and not _blocked_by_reservation(self, ancestor_chain)),
                    timeout=min(remaining, 1.0)
                )
                
                if not granted:
                    # wait_for returned False — predicate not met within tick.
                    # Loop back to check timeout/cancelled and retry.
                    continue
                
                # Predicate was true — double-check cancelled (cancel-after-grant guard).
                # A cancellation could have arrived at the exact moment of wakeup.
                if ticket.cancelled.is_set():
                    _remove_ticket(self, ticket)
                    raise SlotCancelled(ticket)
                
                # Verify we are still head (another waiter might have barged on notify_all race).
                if _is_head(self, ticket.ticket_id):
                    # Dequeue head (O(1)) and grant permit atomically under lock.
                    self._waiters.pop(ticket.ticket_id)
                    holder = _grant(self, instance_name, agent_class, ancestor_chain)
                    ticket.granted.set()
                    return _make_release_cb(self, holder)
                
                # Not head anymore — re-loop and wait again (do NOT raise SlotCancelled).
                continue
            
            # Loop exited because ticket.cancelled.is_set() — remove from queue.
            _remove_ticket(self, ticket)
            raise SlotCancelled(ticket)

    def release(self, holder: SlotHolder) -> None:
        """Release a slot permit held by the given holder.
        
        Idempotent via acquisition_id match — stale releases with wrong IDs are ignored.
        After successful release, notify_all wakes waiters so head can proceed.
        
        Args:
            holder: The SlotHolder to release (captured in release callback).
        """
        with self._cond:
            # Idempotency check: match acquisition_id; ignore stale releases.
            existing = self._running.get(holder.instance_name)
            if existing is None or existing.acquisition_id != holder.acquisition_id:
                return  # Already released or wrong ID — nothing to do
            
            # Remove from running (O(1)).
            del self._running[holder.instance_name]
            
            # Wake all waiters; only head will proceed (FIFO guarantee).
            self._cond.notify_all()

    def create_held_slot(self, agent_name: str, instance_name: Optional[str] = None) -> SlotHolder:
        """Create a held slot for testing purposes.
        
        Properly initializes a SlotHolder under the pool lock without bypassing the API.
        This is the test-friendly replacement for direct _running manipulation.
        
        Args:
            agent_name: The agent name for this holder.
            instance_name: Optional override; defaults to agent_name.
            
        Returns:
            A SlotHolder that can be released with pool.release().
            
        Raises:
            RuntimeError: If the slot is already held by this instance.
        """
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
        """Cancel a waiter's ticket, removing it from the queue.
        
        Can be called from any thread (pool stop, dismiss, terminate-instance).
        Sets cancelled event so waiting thread exits promptly within 1s tick.
        
        Args:
            ticket_id: Cancel this specific ticket (O(1) removal).
            agent_name: Cancel ALL tickets for this instance (mass cancel / terminate path).
        
        Returns:
            True if any ticket was cancelled, False otherwise.
        """
        with self._cond:
            if ticket_id is not None and ticket_id in self._waiters:
                # Single ticket cancel: signal + remove atomically.
                self._waiters[ticket_id].cancelled.set()
                self._waiters.pop(ticket_id)
                self._cond.notify_all()
                return True
            
            elif agent_name is not None:
                # Mass cancel: find all tickets for this instance, cancel and remove.
                cancelled_ids = [
                    tid for tid, t in self._waiters.items()
                    if t.instance_name == agent_name
                ]
                for tid in cancelled_ids:
                    self._waiters[tid].cancelled.set()
                    self._waiters.pop(tid)  # O(1) each
                
                if cancelled_ids:
                    self._cond.notify_all()
                return len(cancelled_ids) > 0
            
            return False

    def reserve(self, agent_name: str, ancestor_chain: Tuple[str, ...], reason: str,
                acquisition_id: int) -> str:
        """Create a reservation on this pool to block unrelated grants.
        
        Called when an agent goes SLEEPING or spawns async child. The reservation
        blocks grants to any waiter NOT in the reserving agent's ancestor chain.
        Self-exemption is implicit: A's instance_name is always in its own chain.
        
        Args:
            agent_name: The reserving agent's display name.
            ancestor_chain: Instance names in parent chain (includes self).
            reason: Why reservation exists ("sleeping", "async_child", "sync_yield").
            acquisition_id: Used to construct unique token.
        
        Returns:
            The reservation token, used later for unreserve().
        """
        with self._cond:
            token = f"res-{agent_name}-{acquisition_id}"
            res = Reservation(
                token=token,
                agent_name=agent_name,
                ancestor_chain=ancestor_chain,
                slot_key=self.key,
                created_at=time.monotonic(),
                reason=reason,
            )
            self._reservations[token] = res
            # Notify waiters — a reservation may block them (or clear if they're in chain).
            self._cond.notify_all()
            return token

    def unreserve(self, token: str) -> bool:
        """Remove a specific reservation.
        
        Args:
            token: The reservation token to remove.
        
        Returns:
            True if reservation was found and removed, False otherwise.
        """
        with self._cond:
            if token in self._reservations:
                del self._reservations[token]
                # Notify waiters — clearing a reservation may unblock them.
                self._cond.notify_all()
                return True
            return False

    def unreserve_for_agent(self, agent_name: str) -> int:
        """Remove ALL reservations for an agent atomically under _cond.
        
        Used by wake-from-sleep (belt-and-braces), terminate cleanup, and stale sweeper.
        An agent may hold multiple reservations on the same pool.
        
        Args:
            agent_name: The agent whose reservations to clear.
        
        Returns:
            Number of reservations removed.
        """
        with self._cond:
            tokens_to_remove = [
                tok for tok, res in self._reservations.items()
                if res.agent_name == agent_name
            ]
            for tok in tokens_to_remove:
                del self._reservations[tok]
            
            if tokens_to_remove:
                self._cond.notify_all()
            return len(tokens_to_remove)

    def terminate_for_agent(self, agent_name: str) -> Tuple[int, int]:
        """Full cleanup for a terminated agent: cancel tickets + unreserve.
        
        Called from AgentInstance.terminate() or stop_session(). Removes all pending
        tickets and all reservations held by this agent in one atomic operation.
        
        Args:
            agent_name: The terminated agent's instance name.
        
        Returns:
            Tuple of (tickets_cancelled, reservations_removed).
        """
        with self._cond:
            # Cancel all tickets for this agent.
            cancelled_ids = [
                tid for tid, t in self._waiters.items()
                if t.instance_name == agent_name
            ]
            for tid in cancelled_ids:
                self._waiters[tid].cancelled.set()
                self._waiters.pop(tid)
            
            # Remove all reservations for this agent.
            tokens_to_remove = [
                tok for tok, res in self._reservations.items()
                if res.agent_name == agent_name
            ]
            for tok in tokens_to_remove:
                del self._reservations[tok]
            
            if cancelled_ids or tokens_to_remove:
                self._cond.notify_all()
            
            return len(cancelled_ids), len(tokens_to_remove)

    # ──────────────────────────────────────────────────────────────────────────
    # Diagnostic methods (for monitoring/debugging)
    # ──────────────────────────────────────────────────────────────────────────

    def get_status(self) -> Dict:
        """Return current pool status for diagnostics.
        
        Returns:
            Dict with key, capacity, running count, waiting count, waiters list,
            holders list, and reservations list.
        """
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
                "reservations": [
                    {
                        "token": r.token,
                        "agent_name": r.agent_name,
                        "reason": r.reason,
                        "age": round(now - r.created_at, 2),
                        "ancestor_chain": r.ancestor_chain,
                    }
                    for r in self._reservations.values()
                ],
            }

    def detect_stale_reservations(self, threshold: float = RESERVATION_TIMEOUT) -> List[Reservation]:
        """Return reservations older than the given threshold.
        
        Used by stale reservation sweeper and monitoring. Does NOT remove them —
        caller decides whether to forcibly unreserve.
        
        Args:
            threshold: Age in seconds above which a reservation is considered stale.
        
        Returns:
            List of stale Reservation objects (read-only view).
        """
        now = time.monotonic()
        with self._cond:
            return [
                r for r in self._reservations.values()
                if (now - r.created_at) > threshold
            ]


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers (called only under pool._cond)
# ──────────────────────────────────────────────────────────────────────────────

def _grant(pool: SlotPool, instance_name: str, agent_class: str,
           ancestor_chain: Tuple[str, ...]) -> SlotHolder:
    """Grant a permit to the requesting agent. Must be called under pool._cond.
    
    Creates a SlotHolder and adds it to _running. The caller must have already
    verified capacity and reservation constraints before calling this.
    """
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
    """Create a release callback bound to the given holder.
    
    The callback captures pool and holder by closure; calling it releases the
    permit idempotently via acquisition_id match.
    """
    def release():
        pool.release(holder)
    return release


def _remove_ticket(pool: SlotPool, ticket: QueueTicket) -> None:
    """Remove a ticket from the waiters queue. O(1) via OrderedDict.pop.
    
    Must be called under pool._cond. Safe to call even if ticket not found.
    """
    pool._waiters.pop(ticket.ticket_id, None)


def _is_head(pool: SlotPool, ticket_id: int) -> bool:
    """Check if the given ticket is at the head of the queue. O(1).
    
    Must be called under pool._cond. Returns False if queue is empty.
    """
    if not pool._waiters:
        return False
    return next(iter(pool._waiters)) == ticket_id


def build_ancestor_chain(
    instance: 'AgentInstance',
    get_instance_fn: Callable[[str], Optional['AgentInstance']],
) -> Tuple[str, ...]:
    """Build ancestor chain from root to this instance for reservation self-exemption.

    Shared utility used by agent_pool, execution_engine, and tool_dispatcher.
    Walks up the parent_instance references until reaching a root (no parent) or
    detecting a cycle. Returns names ordered root-first, self-last.

    Args:
        instance: The agent instance whose ancestors to collect.
        get_instance_fn: Callable that looks up an instance by name (e.g., pool.get_instance).

    Returns:
        Tuple of instance names from root to this instance, e.g., ("main", "A", "B").

    Example:
        For instance "worker1" with parent "orchestrator" which has no parent:
            build_ancestor_chain(worker1, pool.get_instance) → ("orchestrator", "worker1")
    """
    chain: List[str] = []
    current = instance
    visited: set = set()

    while current is not None and current.instance_name not in visited:
        visited.add(current.instance_name)
        chain.append(current.instance_name)

        parent_name = getattr(current, 'parent_instance', None)
        if parent_name:
            current = get_instance_fn(parent_name)
        else:
            break

    return tuple(reversed(chain))  # Root first, this instance last.


def _blocked_by_reservation(pool: SlotPool, ancestor_chain: Tuple[str, ...]) -> bool:
    """Return True if ANY active reservation blocks this grantee.

    A reservation blocks unless the grantee is a descendant of the reserving agent
    (i.e., at least one name in the grantee's ancestor_chain appears in the
    reservation's ancestor_chain).

    Self-exemption: if A reserves and then A re-acquires, A's instance_name IS in
    A's own ancestor_chain → not blocked. This is mathematically guaranteed.

    Example:
        - Reservation R has ancestor_chain=("main", "A").
        - Grantee G with chain=("main", "A", "B") → NOT blocked (shares "A").
        - Grantee H with chain=("other", "H") → BLOCKED (no overlap).

    Must be called under pool._cond.
    """
    for res in pool._reservations.values():
        # Grantee is allowed if any of its chain names appear in the reservation's chain.
        # Includes self-exemption: A's instance_name is always in its own chain.
        if any(name in res.ancestor_chain for name in ancestor_chain):
            continue  # This reservation does NOT block us; check next one.
        return True  # Blocked by this reservation until it clears.
    return False  # No active reservations block us.
