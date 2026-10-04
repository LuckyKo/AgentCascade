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
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple


class _AppLoggerProxy:
    """Late-bound delegate to agent_cascade.log.logger.

    The app's handlers live on the top-level 'agent_cascade_logger'
    (log.py:setup_logger), and init_logging() REBINDS the log module's
    `logger` global to it. A module-level `from agent_cascade.log import
    logger` executed at import time binds the pre-init, handler-less
    object; `logging.getLogger(__name__)` never reaches the app tree at
    all. This proxy resolves the current `log.logger` on every attribute
    access, so slot forensics are emitted from whichever logger is live —
    before and after init_logging().
    """
    __slots__ = ()

    def __getattr__(self, name):
        from agent_cascade.log import logger as _live
        return getattr(_live, name)


logger = _AppLoggerProxy()

if TYPE_CHECKING:
    from agent_cascade.agent_instance import AgentInstance  # noqa: F401  (type-checking only)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration constants (configurable via environment variables)
# ──────────────────────────────────────────────────────────────────────────────

QUEUE_WAIT_TIMEOUT: int = int(os.getenv('AGENT_CASCADE_SLOT_QUEUE_TIMEOUT', 300))
"""Default timeout for waiting in the slot queue. Configurable via AGENT_CASCADE_SLOT_QUEUE_TIMEOUT."""

# ── FIFO head-stall alarm thresholds (plan §3.3) ──────────────────────────────
# Read by NAME at use time inside SlotPool.acquire / _holder_activity_context —
# do NOT hoist into function defaults or copy elsewhere: that would freeze the
# value at import time and silently defeat test patching (plan §5.1, M5).
SLOT_HEAD_STALL_WARN_S: float = float(os.getenv('AGENT_CASCADE_SLOT_HEAD_STALL_WARN_S', 30.0))
"""Head-only WARNING escalation threshold (existing per-waiter warn is 15s)."""
SLOT_HEAD_STALL_ALARM_S: float = float(os.getenv('AGENT_CASCADE_SLOT_HEAD_STALL_ALARM_S', 90.0))
"""ERROR alarm threshold — only meaningful when the holder is NOT actively streaming."""
SLOT_HEAD_STALL_REPEAT_S: float = float(os.getenv('AGENT_CASCADE_SLOT_HEAD_STALL_REPEAT_S', 300.0))
"""Re-fire throttle for the ERROR alarm (one per interval while the stall persists)."""
SLOT_HEAD_STALL_ACTIVE_S: float = float(os.getenv('AGENT_CASCADE_SLOT_HEAD_STALL_ACTIVE_S', 45.0))
"""'Holder is quiet for this long' threshold — the ONLY suppression predicate (§3.2.1):
a holder that stamped an LLM call start or a stream chunk within this window suppresses
the ERROR alarm indefinitely; the 30s WARN is never suppressed."""

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
    agent_name: str = ''
    instance_name: str = ''
    agent_class: str = ''
    slot_key: str = ''
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

    __slots__ = ('key', 'capacity', '_waiters', '_running', '_cond', '_seq_counter',
                 '_acquisition_counter', '_orphan_overwrites', '_inflight')

    def __init__(self, key: str, capacity: int):
        self.key = key
        self.capacity = capacity if capacity > 0 else float('inf')

        self._waiters: OrderedDict[int, QueueTicket] = OrderedDict()
        self._running: Dict[str, SlotHolder] = {}
        # D-5 single-flight gate: instance_name → Event (set when that
        # instance's in-flight acquire() returns or raises). Only spans the
        # blocking window; sequential acquires are unaffected.
        self._inflight: Dict[str, threading.Event] = {}

        self._cond = threading.Condition(threading.RLock())
        self._seq_counter = itertools.count()
        self._acquisition_counter = itertools.count()
        # LEAK #4: count of permits orphaned by a same-instance double-acquire
        # (grant overwriting an existing holder) or the matching stale release.
        # Machine-checkable signal for the regression tests and status dumps.
        self._orphan_overwrites = 0

    def acquire(self,
                instance_name: str,
                agent_class: str,
                timeout: Optional[float] = None,
                pool=None,
                **kwargs) -> Callable[[], None]:
        """Acquire a slot permit from this pool, waiting in FIFO order if necessary.

        Algorithm (all under _cond):
        1. Fast path: if capacity free → grant immediately.
        2. Slow path: enqueue ticket, wait with 1s ticks for interruptibility.
           - Wait until: capacity frees and ticket is head of queue.
           - On wakeup, double-check cancelled flag.
           - Only head waiter proceeds; non-head re-waits immediately.
        3. Returns a release callback bound to the granted SlotHolder.

        D-5 single-flight gate: at most ONE in-flight blocking acquire per
        (pool, instance). A concurrent duplicate does NOT enqueue a second
        ticket; it waits on the primary's Event and re-checks capacity when the
        primary exits. The probe, registration, capacity check and waiter
        registration all happen under ONE acquisition of _cond — no code that
        can raise sits between the inflight read and the write (a failure in
        that window would leave an entry nothing clears, wedging the instance).

        A duplicate blocked on the primary's Event is interruptible like a queued
        waiter: each 1s tick checks whether every ticket for the instance has been
        removed/cancelled (terminate_for_agent / cancel_all), and raises
        SlotCancelled so the caller's lifecycle cleanup completes promptly.

        Args:
            pool: Optional AgentPool reference (plan §3.3 option a). The FIFO
                head-stall alarm uses ``pool.get_instance`` as its holder-context
                resolver; when omitted, it falls back to an ``instance_resolver``
                kwarg if one was passed, and finally degrades to a context-free
                line (the alarm must NEVER be gated on the resolver).
        """
        if self.capacity == float('inf'):
            return lambda: None

        if timeout is None:
            timeout = QUEUE_WAIT_TIMEOUT

        # Holder-context resolver for the head-stall alarm (plan §3.3 option a):
        # an explicit `instance_resolver` kwarg always wins; only fall back to the
        # owning AgentPool when none was passed. (Clobbering an explicit resolver
        # with pool.get_instance made the V22 spy — and any caller-supplied resolver
        # on a scheduler-routed acquire — silently invisible.)
        instance_resolver = kwargs.pop('instance_resolver', None)
        if instance_resolver is None and pool is not None and hasattr(pool, 'get_instance'):
            instance_resolver = pool.get_instance

        # ── D-5 Single-flight gate: one in-flight blocking acquire per (pool, instance).
        # Probe → mark → capacity check → waiter registration all happen under ONE
        # acquisition of _cond so no failure window exists between the inflight read
        # and the write. The entry is cleared in `finally` on every exit path (grant,
        # timeout, cancellation, raise), so sequential acquires by the same instance
        # are unaffected. A concurrent duplicate does NOT enqueue a second ticket —
        # it waits for the primary to finish, then re-checks capacity; if still busy
        # it loops back and enqueues normally as a fresh primary.
        while True:
            owned = False
            try:
                # ── Probe → mark → fast-path capacity check: ONE _cond acquisition.
                # Nothing that can raise sits between the inflight read and the write.
                with self._cond:
                    my_event = self._inflight.get(instance_name)
                    first = my_event is None
                    if first:
                        my_event = threading.Event()
                        self._inflight[instance_name] = my_event
                        owned = True

                        # Fast path: capacity available (same lock scope as the mark).
                        if len(self._running) < self.capacity:
                            holder = _grant(self, instance_name, agent_class)
                            return _make_release_cb(self, holder)
                    else:
                        # Another thread is already blocking on this pool for this
                        # instance. Do NOT enqueue a second ticket — wait for the
                        # primary to finish (outside the lock; see below).
                        logger.warning(
                            f"[SLOTPOOL] DUPLICATE-ACQUIRE suppressed on '{self.key}': "
                            f"agent={instance_name} — an acquire is already in flight; not enqueueing a 2nd ticket")

                if not first:
                    dup_deadline = time.monotonic() + timeout
                    while not my_event.is_set():
                        # Interruptible on the same signal as a queued waiter:
                        # terminate_for_agent / cancel_all remove our ticket from
                        # _waiters, which wakes us within one 1s tick.
                        if _ticket_cancelled(self, instance_name):
                            raise SlotCancelled(QueueTicket(
                                seq=-1, agent_name=instance_name, instance_name=instance_name,
                                agent_class=agent_class, slot_key=self.key,
                                created_at=time.monotonic(), deadline=dup_deadline))
                        if time.monotonic() >= dup_deadline:
                            raise SlotQueueTimeout(QueueTicket(
                                seq=-1, agent_name=instance_name, instance_name=instance_name,
                                agent_class=agent_class, slot_key=self.key,
                                created_at=time.monotonic(), deadline=dup_deadline))
                        my_event.wait(timeout=1.0)        # 1s tick, same cadence as the wait loop
                    # Primary finished — re-check whether the pool is now free for us.
                    with self._cond:
                        if len(self._running) < self.capacity:
                            holder = _grant(self, instance_name, agent_class)
                            return _make_release_cb(self, holder)
                    # Pool still busy: loop back and queue normally (we no longer
                    # hold the in-flight slot, so a new primary may claim it).
                    first = True
                    with self._cond:
                        if instance_name not in self._inflight:
                            my_event = threading.Event()
                            self._inflight[instance_name] = my_event
                            owned = True
                            continue
                    # Someone re-registered while we were re-checking: wait for them.
                    continue

                # Slow path: enqueue as waiter. The pool was full at probe time, so
                # the ticket is registered under the SAME _cond scope that will hold
                # it across the wait loop (wait_for requires the lock).
                with self._cond:
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

                    # BUG-11: once-per-enqueue lifecycle trace (never per-tick — logged
                    # BEFORE the poll loop starts).
                    logger.debug(f"[SLOTPOOL] Queued on '{self.key}': agent={instance_name} ({agent_class}) "
                                 f"ticket={ticket.ticket_id} position={len(self._waiters)} "
                                 f"waiters={len(self._waiters)} holders={[h.instance_name for h in self._running.values()]} "
                                 f"timeout={timeout:.0f}s")
                    logger.warning(f"[SLOTPOOL] Slot contention on '{self.key}': agent='{instance_name}' ({agent_class}) "
                                   f"queued (position={len(self._waiters)}, waiters={len(self._waiters)}, "
                                   f"running={len(self._running)}/{self.capacity}, "
                                   f"holders={[h.instance_name for h in self._running.values()]}, timeout={timeout:.0f}s)")

                    deadline = ticket.deadline
                    last_wait_warn = ticket.created_at
                    # FIFO head-stall escalation state — plain locals, NOT pool state (§3.3.1):
                    # the lock-free resolution phase writes them safely, and this keeps the
                    # change confined to one method (no QueueTicket migration).
                    head_warned = False
                    # The REPEAT_S throttle must only gate *subsequent* alarms; the first
                    # ERROR is allowed as soon as age >= ALARM_S. Initializing to created_at
                    # made (now - last_head_alarm) start at 0, so due_alarm stayed False for
                    # the whole ALARM_S..REPEAT_S window — the first alarm was unreachable
                    # unless the waiter outlived REPEAT_S (plan §3.2: fire at ALARM_S).
                    last_head_alarm = ticket.created_at - SLOT_HEAD_STALL_REPEAT_S

                    while not ticket.cancelled.is_set():
                        now_mono = time.monotonic()

                        # ── FIFO HEAD-STALL escalation (diagnostic only) ────────────────
                        # Two-phase by design: cheap threshold detection under _cond,
                        # expensive instance resolution with _cond RELEASED. The explicit
                        # release()/acquire() pair below is LOAD-BEARING — do not refactor
                        # it into a try/finally around the loop or a helper called from
                        # inside the `with` block (both silently reintroduce the lock-order
                        # inversion). Legal because _cond wraps an RLock and this loop body
                        # holds it at recursion depth exactly 1 (the `with` below plus the
                        # reacquisition inside wait_for). See plan §3.3.1.
                        if _is_head(self, ticket.ticket_id):
                            age = now_mono - ticket.created_at
                            due_warn = (not head_warned) and (age >= SLOT_HEAD_STALL_WARN_S)
                            due_alarm = ((age >= SLOT_HEAD_STALL_ALARM_S) and
                                         (now_mono - last_head_alarm) >= SLOT_HEAD_STALL_REPEAT_S)
                            if due_warn or due_alarm:
                                self._cond.release()  # legal: _cond is RLock-backed
                                try:
                                    ctx = _holder_activity_context(self, now_mono, resolver=instance_resolver)
                                    if due_warn:
                                        logger.warning(_head_stall_msg(self, ticket, age, ctx, level='WARN'))
                                        head_warned = True
                                    # §3.2.1: streaming suppresses the ERROR for ANY waiter age.
                                    # No `age < WARN_S*3` clause — that was unreachable.
                                    if due_alarm and not ctx['streaming']:
                                        logger.error(_head_stall_msg(self, ticket, age, ctx, level='ALARM'))
                                        last_head_alarm = now_mono
                                except Exception:
                                    # A diagnostic must never break the wait loop or leak the
                                    # ticket. Degrade to a context-free alarm.
                                    logger.error(f"[SLOT_HEAD_STALL] context resolution failed on "
                                                 f"'{self.key}' head='{ticket.instance_name}' "
                                                 f"age={age:.0f}s — emitting without holder context",
                                                 exc_info=True)
                                finally:
                                    self._cond.acquire()  # MUST run on every path

                        if now_mono - last_wait_warn >= 15.0:
                            elapsed = now_mono - ticket.created_at
                            logger.warning(f"[SLOTPOOL] Agent '{instance_name}' still waiting for slot on '{self.key}' "
                                           f"after {elapsed:.0f}s (waiters={len(self._waiters)}, "
                                           f"running={len(self._running)}/{self.capacity}, "
                                           f"holders={[h.instance_name for h in self._running.values()]})")
                            last_wait_warn = now_mono

                        remaining = deadline - now_mono

                        if remaining <= 0:
                            _remove_ticket(self, ticket)
                            _log_acquire_timeout(self, ticket)
                            raise SlotQueueTimeout(ticket)

                        # Wait until predicate is true: capacity free + we are head.
                        granted = self._cond.wait_for(lambda:
                                                      (_is_head(self, ticket.ticket_id) and len(self._running) < self.capacity),
                                                      timeout=min(remaining, 1.0))

                        if not granted:
                            continue

                        if ticket.cancelled.is_set():
                            # BUG-11: lifecycle trace for the silent SlotCancelled abort.
                            logger.debug(f"[SLOTPOOL] Cancelled while waiting on '{self.key}': "
                                         f"agent={ticket.instance_name} ticket={ticket.ticket_id}")
                            _remove_ticket(self, ticket)
                            raise SlotCancelled(ticket)

                        if _is_head(self, ticket.ticket_id):
                            self._waiters.pop(ticket.ticket_id)
                            holder = _grant(self, instance_name, agent_class, ticket=ticket)
                            ticket.granted.set()
                            wait_dur = time.monotonic() - ticket.created_at
                            if wait_dur >= 1.0:
                                logger.info(f"[SLOTPOOL] Agent '{instance_name}' acquired slot on '{self.key}' "
                                            f"after {wait_dur:.1f}s wait in queue.")
                            return _make_release_cb(self, holder)

                        continue

                    # BUG-11: lifecycle trace for cancellation detected outside the
                    # wait loop (ticket.cancelled set between iterations).
                    logger.debug(f"[SLOTPOOL] Cancelled while waiting on '{self.key}': "
                                 f"agent={ticket.instance_name} ticket={ticket.ticket_id}")
                    _remove_ticket(self, ticket)
                    raise SlotCancelled(ticket)
            finally:
                # D-5: clear the in-flight entry on EVERY exit path (grant, timeout,
                # cancellation, raise). Only the OWNER of the entry may clear it and
                # set its Event — a duplicate that loops back via `continue` must
                # neither evict the primary's entry nor wake its waiters early.
                if owned:
                    with self._cond:
                        ev = self._inflight.get(instance_name)
                        if ev is my_event:
                            del self._inflight[instance_name]
                    my_event.set()

    def release(self, holder: SlotHolder) -> None:
        """Release a slot permit held by the given holder."""
        with self._cond:
            existing = self._running.get(holder.instance_name)
            if existing is None:
                logger.debug(f"[SLOTPOOL] Stale release on '{self.key}': agent={holder.instance_name} "
                             f"acquisition={holder.acquisition_id} — no current holder "
                             f"(already released; idempotent no-op).")
                return
            if existing.acquisition_id != holder.acquisition_id:
                logger.warning(f"[SLOTPOOL] STALE RELEASE on '{self.key}': agent={holder.instance_name} "
                               f"presented acquisition={holder.acquisition_id} but "
                               f"acquisition={existing.acquisition_id} is current "
                               f"(held {time.monotonic() - existing.granted_at:.1f}s). "
                               f"Ignoring — this is the DOUBLE-ACQUIRE signature (LEAK #4).")
                self._orphan_overwrites += 1
                return

            del self._running[holder.instance_name]

            # BUG-11: once-per-release lifecycle trace with held duration
            # (held_duration is only computable here — the scheduler layer never
            # sees the SlotHolder).
            held = time.monotonic() - holder.granted_at
            logger.debug(f"[SLOT] {holder.instance_name}: released pool='{self.key}' "
                         f"held={held:.1f}s remaining={len(self._running)}/{self.capacity}")
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
                ticket = self._waiters[ticket_id]
                # BUG-11: lifecycle trace for single-ticket cancel.
                logger.debug(f"[SLOTPOOL] Cancelled on '{self.key}': agent={ticket.instance_name} "
                             f"ticket={ticket.ticket_id}")
                ticket.cancelled.set()
                self._waiters.pop(ticket_id)
                self._cond.notify_all()
                return True

            elif agent_name is not None:
                cancelled_ids = [tid for tid, t in self._waiters.items() if t.instance_name == agent_name]
                for tid in cancelled_ids:
                    self._waiters[tid].cancelled.set()
                    self._waiters.pop(tid)

                if cancelled_ids:
                    # BUG-11: lifecycle trace for agent-scoped cancel.
                    logger.debug(f"[SLOTPOOL] Cancelled on '{self.key}': agent={agent_name} "
                                 f"tickets={cancelled_ids}")
                    self._cond.notify_all()
                return len(cancelled_ids) > 0

            return False

    def terminate_for_agent(self, agent_name: str) -> Tuple[int, int]:
        """Full cleanup for a terminated agent: cancel waiters AND drop any held permit."""
        with self._cond:
            cancelled_ids = [tid for tid, t in self._waiters.items() if t.instance_name == agent_name]
            for tid in cancelled_ids:
                self._waiters[tid].cancelled.set()
                self._waiters.pop(tid)

            if cancelled_ids:
                # BUG-11: lifecycle trace for termination cleanup.
                logger.debug(f"[SLOTPOOL] Terminated on '{self.key}': agent={agent_name} "
                             f"tickets={cancelled_ids}")
                self._cond.notify_all()

            # BUG_0034: was a hardcoded 0 — the released count was never computed.
            # The holder entry is removed by identity so we can report it truthfully
            # without firing the caller's callback (terminate_for_agent must not
            # invoke a release closure it does not own).
            released = 0
            holder = self._running.get(agent_name)
            if holder is not None:
                del self._running[agent_name]
                released = 1
                held = time.monotonic() - holder.granted_at
                logger.debug(f"[SLOTPOOL] Terminated holder on '{self.key}': agent={agent_name} "
                             f"acquisition={holder.acquisition_id} held={held:.1f}s")
                self._cond.notify_all()

            return len(cancelled_ids), released

    def get_status(self) -> Dict:
        """Return current pool status for diagnostics."""
        now = time.monotonic()
        with self._cond:
            return {
                'key':
                    self.key,
                'capacity':
                    self.capacity if self.capacity != float('inf') else -1,
                'running_count':
                    len(self._running),
                'waiting_count':
                    len(self._waiters),
                'orphan_overwrites':
                    self._orphan_overwrites,
                'waiters': [{
                    'ticket_id': t.ticket_id,
                    'seq': t.seq,
                    'instance_name': t.instance_name,
                    'agent_class': t.agent_class,
                    'wait_time': round(now - t.created_at, 2),
                    'remaining_timeout': max(0, round(t.deadline - now, 2)),
                } for t in self._waiters.values()],
                'holders': [{
                    'instance_name': h.instance_name,
                    'agent_name': h.agent_name,
                    'acquisition_id': h.acquisition_id,
                    'held_duration': round(now - h.granted_at, 2),
                } for h in self._running.values()],
            }


# ──────────────────────────────────────────────────────────────────────────────
# Shared slot-release helper (capture-nullify-release-log)
# ──────────────────────────────────────────────────────────────────────────────


def _get_router_from_holder(holder: Any):
    """Best-effort recovery of the APIRouter from a slot holder (may be None).

    The only reachable source is the per-instance back-ref ``holder._pool_ref.api_router``: this
    function receives ONLY the holder, with no pool/scheduler/engine in scope. The router's own
    back-ref to its pool (``api_router._pool``) is useless as a second source — it lives on the
    object being sought, and ``release_slot_permit(pool=...)`` passes a SlotPool, not the AgentPool.

    RESIDUAL LIMITATION: no safe second source exists here without new coupling (a global registry
    was deliberately NOT added), so a holder whose ``_pool_ref`` was never set still returns None
    and skips the marker write + committed/held cleanup. The known construction gap is now closed at
    every site; if a future unguarded AgentInstance( appears, recovery silently degrades — the
    release_slot_permit WARNING is what makes that visible.

    Logging-only: the DEBUG line names which source succeeded (or that none was available). No lock,
    no network I/O; return value is byte-identical to the pre-change implementation.
    """
    pool_ref = getattr(holder, '_pool_ref', None)
    _router = getattr(pool_ref, 'api_router', None) if pool_ref is not None else None
    try:
        if _router is not None:
            logger.debug(
                f"[SLOT] {getattr(holder, 'instance_name', '?')}: router recovery source=pool_ref")
        else:
            logger.debug(
                f"[SLOT] {getattr(holder, 'instance_name', '?')}: "
                f"router recovery source=none (_pool_ref missing or api_router absent)")
    except Exception:
        pass  # observability must never alter the release path
    return _router


def release_slot_permit(
    holder: Any,
    holder_name: str,
    action: Optional[str] = None,
    context: Optional[str] = None,
    pool: Optional['SlotPool'] = None,
    strict: bool = False,
) -> bool:
    """Atomically capture and release a slot permit held by ``holder``.

    The canonical implementation of the capture-nullify-release-log pattern used
    at every sticky-slot lifecycle point (run exit, sleep transition, dismiss,
    stop_session, reuse, side-call cross-pool swap). All sites share these exact
    semantics:

      1. Under ``holder._state_lock``: if a live ``_slot_release`` callback is
         held, capture it and nullify ``_slot_release``/``_slot_key`` (so a
         concurrent release from the execution thread becomes a no-op).
      2. Invoke the captured callback OUTSIDE the state lock — the pool's own
         condition may block on waiters, so holding the state lock across it
         would deadlock a dismiss racing a queued yield.
      3. If ``action`` is given, emit exactly one structured line:
         ``[SLOTPOOL] instance=<name> pool=<key> action=<action> waiters=<n>``
         where the waiter count is snapshotted from ``pool`` when passed, else
         best-effort recovered from the release callback's closure (see below),
         else -1.

    Thread-safe and idempotent: a second call after a successful release finds
    no live callback and returns False without logging.

    Args:
        holder: Object exposing ``_state_lock``, ``_slot_release`` and
            ``_slot_key`` (an AgentInstance or similar slot holder).
        holder_name: Instance name used in log lines.
        action: Structured event label ('drop-exit', 'drop-sleep', ...). When
            None, no [SLOTPOOL] line is emitted (plain cleanup release).
        context: Human-readable context appended to the failure log message
            (e.g., "sleep transition", "sync child").
        pool: The SlotPool this permit belongs to. Pass it when available —
            the waiter count is then an exact snapshot under ``pool._cond``.
            When None, the pool is recovered from the release callback's
            closure cells (SlotPool._make_release_cb closes over the pool) as a
            best-effort fallback; any failure yields waiters=-1.
        strict: When True, a failing release callback re-raises instead of being
            absorbed. The [SLOT_RELEASE_ERROR] ERROR is still logged first. Do NOT
            use this on lifecycle paths whose finally block must guarantee cleanup —
            there an absorbed (logged) failure is strictly safer than a hang.

    Returns:
        True if a live permit was captured and released, False if nothing was
        held (no state change, no [SLOTPOOL] line).
    """
    if not hasattr(holder, '_slot_release'):
        return False

    context_suffix = f" during {context}" if context else ''
    slot_key: Optional[str] = None
    release_callback: Optional[Callable[[], None]] = None

    # Acquire state lock for atomic check-nullify-capture.
    if hasattr(holder, '_state_lock'):
        with holder._state_lock:
            if getattr(holder, '_slot_release', None) is not None:
                release_callback = holder._slot_release
                slot_key = getattr(holder, '_slot_key', None)
                holder._slot_release = None
                if hasattr(holder, '_slot_key'):
                    holder._slot_key = None
    elif getattr(holder, '_slot_release', None) is not None:
        # Fallback for holders without a state lock (defensive; AgentInstance
        # always has one). No atomicity guarantee on this path.
        release_callback = holder._slot_release
        slot_key = getattr(holder, '_slot_key', None)
        holder._slot_release = None
        if hasattr(holder, '_slot_key'):
            holder._slot_key = None

    if release_callback is None:
        return False

    # Record the endpoint this holder was using, so an unassigned agent resolving next
    # (e.g. a spawned Security check) can prefer it over the racy global marker.
    # BEST-EFFORT scope: this is a GLOBAL "last released by anyone" record, not per-caller.
    # It guarantees capability matching in the common single-caller case (one shell command
    # -> one security check). Under heavy parallel fan-out, a mismatched allocation is possible
    # if another agent releases between this holder's release and the check's resolution; that
    # is an accepted, rare edge case (user sign-off 2026-10-02). Never worse than the racy global.
    # CRITICAL ordering: this read MUST run BEFORE the committed-map pop in the block below —
    # once popped, _instance_committed_endpoint[holder_name] is gone and we'd record nothing.
    # Guarded by its own try/except so a failure here never breaks the release path.
    try:
        _router = _get_router_from_holder(holder)
        if _router is not None and hasattr(_router, '_instance_committed_endpoint'):
            with _router._lock:
                _committed = _router._instance_committed_endpoint.get(holder_name)
                if _committed is None:
                    # Committed marker was already consumed by a prior release (yield/reacquire
                    # cycle whose LLM call never re-ran). Fall back to the acquire-time held
                    # endpoint so _last_released_endpoint still tracks this holder's endpoint.
                    _held = getattr(_router, '_instance_held_endpoint', {}).get(holder_name)
                    if _held is not None:
                        _committed = _held
                if _committed is not None:
                    # Convert (base, model) tuple to endpoint ID for O(1) Tier 1.5 lookup.
                    # Already under _router._lock — safe to call the helper directly.
                    _ep_id = _router._endpoint_id_for_key(_committed[0], _committed[1])
                    if _ep_id is not None:
                        _router._last_released_endpoint = _ep_id
                        logger.debug(f"[SLOT] {holder_name}: last_released_endpoint → {_ep_id}")
        elif _router is None:
            # BUG_0052 Change C: make the silent skip of the _last_released_endpoint write visible.
            # In this branch _router is None, so the retained global marker can't be read safely
            # (no handle — would need a lock/new coupling); log 'unrecoverable'. The holder's own
            # endpoint id is best-effort, read lock-free below. Prefix preserved verbatim for greps.
            _retained = 'unrecoverable'  # no router handle in this branch — see comment above
            try:
                # Holder's own committed/held endpoint id, if safely readable. Prefer the per-instance
                # markers on holder._pool_ref.api_router (the exact source release_slot_permit would
                # have written); fall back to the holder's cached endpoint config / slot key. Read
                # lock-free — this is observability only and must never break the release path.
                _own_id = None
                _pr = getattr(holder, '_pool_ref', None)
                if _pr is not None:
                    _r2 = getattr(_pr, 'api_router', None)
                    if _r2 is not None:
                        _key = (_r2._instance_committed_endpoint.get(holder_name)
                                or _r2._instance_held_endpoint.get(holder_name))
                        if _key is not None:
                            _own_id = _r2._endpoint_id_for_key(_key[0], _key[1])
                if _own_id is None:
                    _own_id = (getattr(holder, '_last_endpoint_config', None) or
                               getattr(holder, '_slot_key', None))
                if _own_id is not None:
                    _retained += f"; holder_endpoint={_own_id}"
            except Exception:
                pass  # observability must never alter the release path
            logger.warning(
                f"[SLOT] {holder_name}: router recovery failed on release "
                f"(_pool_ref or api_router missing) — _last_released_endpoint NOT updated "
                f"(retained marker={_retained})")
    except Exception:
        pass

    # Clear the committed-endpoint probe marker so the next acquisition re-probes
    # instead of skipping against a dead connection — same contract as
    # APIRouter._drop_held_permit; recovered best-effort via holder._pool_ref. The acquire-time
    # held-endpoint marker is popped alongside it (last-released bookkeeping, see above).
    try:
        _router = _get_router_from_holder(holder)
        if _router is not None and hasattr(_router, '_instance_committed_endpoint'):
            with _router._lock:
                _router._instance_committed_endpoint.pop(holder_name, None)
                _router._instance_held_endpoint.pop(holder_name, None)
        elif _router is None:
            # BUG_0052 (Change C): router recovery failed — the committed/held markers for this
            # holder will NOT be cleared. Surface it (single WARNING per release) instead of a
            # silent skip; does not change any other behavior.
            logger.warning(
                f"[SLOT] {holder_name}: router recovery failed on release "
                f"(_pool_ref or api_router missing) — committed/held markers NOT cleared")
    except Exception:
        pass

    try:
        release_callback()
    except Exception as e:
        # The [SLOT_RELEASE_ERROR] ERROR is logged unconditionally — it is the
        # discoverability mechanism and must not become conditional on `strict`.
        logger.error(
            f"[SLOT_RELEASE_ERROR] Failed to release slot for {holder_name}{context_suffix}: {e}",
            exc_info=True,
        )
        if strict:
            raise

    if action:
        _waiters = -1
        try:
            _pool = pool
            if _pool is None:
                # Last-resort recovery: SlotPool._make_release_cb closes over the
                # pool, so scan the callback's closure cells for it. Best-effort —
                # any failure just yields waiters=-1.
                for _cell in getattr(release_callback, '__closure__', None) or ():
                    _obj = _cell.cell_contents
                    if hasattr(_obj, '_waiters') and hasattr(_obj, '_cond'):
                        _pool = _obj
                        break
            if _pool is not None:
                with _pool._cond:
                    _waiters = len(_pool._waiters)
        except Exception:
            pass
        logger.debug(f"[SLOT] {holder_name}: release ({action}) pool={slot_key} waiters={_waiters}")
    return True


# ──────────────────────────────────────────────────────────────────────────────
# FIFO head-stall alarm context helpers (plan §3.3)
# ──────────────────────────────────────────────────────────────────────────────


def _holder_activity_context(pool: 'SlotPool', now_mono: float,
                             resolver: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Resolve holder state/activity context for the head-stall alarm.

    MUST be called with pool._cond RELEASED — it reaches out to AgentInstance
    objects whose _state_lock is contended by the very run-loops that are
    supposed to be releasing permits (lock-order inversion, plan §3.3.1).

    Returns a dict with: holder_name, holder_class, holder_state, held_s,
    last_activity_age_s, llm_active, streaming, context_available.

    ``streaming`` is the ONLY suppression predicate (§3.2.1): the holder stamped
    an LLM call start or a stream chunk within SLOT_HEAD_STALL_ACTIVE_S. Read by
    name at use time so tests can patch it (plan §5.1, M5).
    """
    ctx: Dict[str, Any] = {
        'holder_name': '?',
        'holder_class': '?',
        'holder_state': '?',
        'held_s': -1.0,
        'last_activity_age_s': -1.0,
        'llm_active': False,
        'streaming': False,
        'context_available': resolver is not None,
    }
    holders = list(pool._running.values())
    if not holders:
        return ctx

    holder = holders[0]  # conc=1 pools have exactly one; first holder otherwise
    ctx['holder_name'] = holder.instance_name
    ctx['held_s'] = now_mono - holder.granted_at

    inst = None
    if resolver is not None:
        try:
            inst = resolver(holder.instance_name)
        except Exception:
            inst = None  # a broken resolver degrades, it never breaks the wait loop

    if inst is not None:
        ctx['holder_state'] = getattr(getattr(inst, 'state', None), 'name', '?') or '?'
        last_activity = getattr(inst, '_last_llm_activity', 0.0)
        if last_activity > 0:
            ctx['last_activity_age_s'] = now_mono - last_activity
        ctx['llm_active'] = bool(getattr(inst, '_llm_call_active', False))
        # §3.2.1: streaming = an LLM call is in flight AND it stamped activity
        # (call start or chunk) within the ACTIVE_S window. A long prefill with
        # zero output ages out of this on purpose — see plan §3.2.1.
        ctx['streaming'] = (ctx['llm_active'] and last_activity > 0 and
                            (now_mono - last_activity) <= SLOT_HEAD_STALL_ACTIVE_S)
    return ctx


def _head_stall_msg(pool: 'SlotPool', ticket: 'QueueTicket', age: float,
                    ctx: Dict[str, Any], level: str) -> str:
    """Format the single-line key=value head-stall log message (plan §3.3)."""
    holders = list(pool._running.values())
    prefix = '[SLOT_HEAD_STALL_WARN]' if level == 'WARN' else '[SLOT_HEAD_STALL]'
    tail = '' if level == 'WARN' else (' ACTION: manual — diagnostic only, no auto-preemption')
    context_tag = '' if ctx['context_available'] else ' context=unavailable'
    return (f"{prefix} pool={pool.key} head={ticket.instance_name} ({ticket.agent_class}) "
            f"age={age:.0f}s position={len(pool._waiters)} "
            f"holder={ctx['holder_name']} state={ctx['holder_state']} "
            f"held={ctx['held_s']:.0f}s last_activity={ctx['last_activity_age_s']:.0f}s "
            f"llm_active={str(ctx['llm_active']).lower()} streaming={str(ctx['streaming']).lower()}"
            f"{context_tag} running={len(pool._running)}/{pool.capacity} "
            f"holders={[h.instance_name for h in holders]}{tail}")


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers (called only under pool._cond)
# ──────────────────────────────────────────────────────────────────────────────


def _grant(pool: SlotPool, instance_name: str, agent_class: str, ticket: Optional[QueueTicket] = None) -> SlotHolder:
    """Grant a permit to the requesting agent. Must be called under pool._cond.

    BUG-11: single DEBUG choke point for BOTH grant paths — the uncontended
    fast path (acquire) and the queued grant. Pass ``ticket`` when granting a
    queued waiter so the log carries the ticket id + waited duration.
    """
    acquisition_id = next(pool._acquisition_counter)
    holder = SlotHolder(
        agent_name=instance_name,
        instance_name=instance_name,
        acquisition_id=acquisition_id,
        granted_at=time.monotonic(),
    )
    # BUG-11 / LEAK #4: pool._running is keyed by instance_name ONLY. A second
    # acquire by the same instance silently overwrites the first SlotHolder,
    # orphaning the first acquisition_id so its later release hits the stale
    # branch in release() and becomes a no-op. That destroys the very
    # holders=[...] diagnostic used to find these bugs, and across two pools it
    # is a real capacity leak. Detect and shout; do NOT raise here (a raise
    # inside acquire() would abort a legitimate flow and leave the caller with
    # no permit at all). Fix the upstream overwrite instead.
    existing = pool._running.get(instance_name)
    if existing is not None:
        logger.error(f"[SLOTPOOL] DOUBLE-ACQUIRE on '{pool.key}': agent={instance_name} "
                     f"already holds acquisition={existing.acquisition_id} "
                     f"(granted {time.monotonic() - existing.granted_at:.1f}s ago); "
                     f"overwriting with acquisition={acquisition_id}. The prior permit is "
                     f"ORPHANED — find the upstream nullify-without-release.")
        pool._orphan_overwrites += 1
    pool._running[instance_name] = holder

    # BUG-11: once-per-grant lifecycle trace.
    waited = (time.monotonic() - ticket.created_at) if ticket else 0.0
    logger.debug(f"[SLOT] {instance_name} ({agent_class}): granted pool='{pool.key}' "
                 f"acquisition={holder.acquisition_id}" +
                 (f" waited={waited:.1f}s" if ticket else ' (fast-path)'))
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


def _ticket_cancelled(pool: SlotPool, instance_name: str) -> bool:
    """True if this instance has NO live ticket on this pool.

    Used by the D-5 duplicate waiter (which holds no ticket of its own while
    blocked on the primary's Event): once terminate_for_agent / cancel_all removes
    every ticket for the instance, the duplicate must abort instead of sitting
    until its timeout. Called with _cond RELEASED — it snapshots under the lock
    and checks the flags outside it (same pattern as the wait loop's 1s tick).
    """
    with pool._cond:
        # A live ticket is one that is still queued AND not cancelled. The primary's
        # wait loop removes its own ticket on cancel/timeout, so "no live ticket"
        # means the whole acquire chain for this instance has been torn down.
        tickets = [t for t in pool._waiters.values() if t.instance_name == instance_name]
    return not any(not t.cancelled.is_set() for t in tickets)


def _log_acquire_timeout(pool: SlotPool, ticket: QueueTicket) -> None:
    """Log diagnostic information when acquire() times out. Must be called under pool._cond."""
    now = time.monotonic()
    wait_time = now - ticket.created_at
    logger.warning(f"[SLOTPOOL] Acquire timeout on pool '{pool.key}' for ticket {ticket.ticket_id} "
                   f"(agent={ticket.instance_name}, wait_time={wait_time:.1f}s): "
                   f"running={len(pool._running)}/{pool.capacity}, waiters={len(pool._waiters)}")
