"""
IdleManager — idle detection and auto-dismissal. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Optional

from agent_cascade.log import logger
from agent_cascade.shared_init import STABLE_SYSTEM_PROMPT_CLASSES

from ..agent_instance import AgentState
from ..slot_queue import _holder_activity_context

# ── Holder-side dead-man's-switch: bounded-blocking-tool bounds (plan t220 §3.4, generalized t292) ──
# A holder carrying an ``_in_blocking_tool`` marker is EXCLUDED from auto-preempt while
# (now - start) < bound. As of t292 the SINGLE producer of that marker is the dispatch
# boundary (ToolDispatcher.execute_tool), which writes a 3-tuple carrying the tool's
# ``BaseTool.dms_bound()`` value (or the settings cap ``holder_dms_max_bound_s`` when the
# tool declares no finite bound). So supporting a new long tool means implementing its
# ``dms_bound()`` — NOT editing this registry and NOT writing the marker by hand.
#
# The entries below are DEFENSIVE FALLBACK bounds only, consulted when a marker lacks a
# carried bound (legacy 2-tuple). The carried bound is authoritative. media_gen/image_gen
# carry their real (config-driven, base.resolve_media_timeout) timeout WITH the marker, so
# these values are not the primary source. There are no per-backend magic numbers — the
# timeout is one UI value.
_BLOCKING_TOOL_BOUNDS: dict[str, float] = {
    'media_gen': 900.0,  # fallback when a marker carries no bound (carried bound is authoritative)
    'image_gen': 900.0,
}


def _blocking_tool_bound(tool_name):
    """Return the bound (seconds) for a long-running tool, or None if it has none.

    ``shell_cmd`` resolves MAX_SHELL_TIMEOUT lazily to avoid a module-import cycle with
    operation_manager (the sync path sets the marker around proc.wait up to this bound).
    """
    if tool_name in _BLOCKING_TOOL_BOUNDS:
        return _BLOCKING_TOOL_BOUNDS[tool_name]
    if tool_name == 'shell_cmd':
        from agent_cascade.operation_manager.shell import MAX_SHELL_TIMEOUT
        return float(MAX_SHELL_TIMEOUT)
    return None

if TYPE_CHECKING:  # pragma: no cover - annotation-only; avoids circular import of core.py
    from .core import AgentPool


class IdleManager:
    """Manages idle detection and auto-dismissal of agents.

    Runs a background daemon thread that periodically checks for agents that have
    been inactive longer than the configured timeout. Auto-dismissed agents have
    their conversations cleared and dismissal callbacks fired for real-time UI tab removal.

    Safety rules:
    - Never dismisses the main orchestrator (parent_instance is None)
    - Never dismisses active agents (in active_stack)
    - Never dismisses halted agents (intentionally paused)
    """

    def __init__(self, pool: AgentPool):
        self.pool = pool
        self._stop_event = threading.Event()
        self._checker_thread: Optional[threading.Thread] = None

    def start(self):
        """Start the background idle checker thread."""
        if self._checker_thread is not None and self._checker_thread.is_alive():
            return  # Already running
        self._stop_event.clear()
        self._checker_thread = threading.Thread(
            target=self._checker_loop,
            name='IdleAgentChecker',
            daemon=True,
        )
        self._checker_thread.start()

    def stop(self):
        """Signal the checker to stop (non-blocking).

        Sets the stop event so the checker loop exits on its next iteration.
        Does NOT join the thread — that would block the calling thread for up to
        (idle_check_interval + 5) seconds when called from an async handler.
        Use stop_and_join() for blocking cleanup during server shutdown.
        """
        self._stop_event.set()

    def stop_and_join(self, timeout: Optional[float] = None):
        """Signal the checker and wait for it to exit (blocking).

        Used during full server shutdown where blocking is acceptable.
        Falls back to idle_check_interval + 5s default if no timeout given.

        Note: After calling this method, _checker_thread is set to None. Calling
        start() again will create a new thread.
        """
        self.stop()
        if self._checker_thread is not None and self._checker_thread.is_alive():
            join_timeout = timeout if timeout is not None else self.pool.settings.idle_check_interval + 5.0
            self._checker_thread.join(timeout=join_timeout)
            if self._checker_thread.is_alive():
                logger.warning('Idle checker thread did not exit in time.')
        self._checker_thread = None

    @staticmethod
    def _is_system_agent(agent_class: str) -> bool:
        """Check if agent class is a system-invoked type (Compressor, Security)."""
        return agent_class.lower() in STABLE_SYSTEM_PROMPT_CLASSES

    def _checker_loop(self):
        """Background loop that periodically checks for and dismisses idle agents."""
        while not self._stop_event.is_set():
            try:
                # Snapshot instance names to avoid holding locks during check
                inst_names = list(self.pool.instances.keys())
                dismissed_this_round = []

                for name in inst_names:
                    if self._stop_event.is_set():
                        break
                    try:
                        if self._is_idle(name):
                            self._auto_dismiss(name)
                            dismissed_this_round.append(name)
                    except Exception as e:
                        logger.error(f"[idle_checker] Error processing '{name}': {e}", exc_info=True)

                if dismissed_this_round:
                    logger.info(f"[idle_checker] Auto-dismissed {len(dismissed_this_round)} idle agent(s): "
                                f"{', '.join(dismissed_this_round)}")
            except Exception as e:
                logger.error(f"[idle_checker] Loop error: {e}", exc_info=True)

            # ── Holder-side dead-man's-switch (plan t220 §3.1) ──────────────────────
            # Separate pass, independent of _is_idle (which is blind to RUNNING holders).
            # Runs on this always-on timer regardless of waiters → fires even with zero
            # waiters (closes the symmetric gap the waiter-side DMS leaves open).
            try:
                self._run_holder_dms()
            except Exception as e:
                logger.error(f"[holder_dms] Pass error: {e}", exc_info=True)

            # Wait for next check interval (or until stop event fires)
            self._stop_event.wait(timeout=self.pool.settings.idle_check_interval)

    def _is_idle(self, instance_name: str) -> bool:
        """Determine whether an agent is idle and eligible for auto-dismissal."""
        inst = self.pool.instances.get(instance_name)
        if not inst:
            return False

        # Never dismiss the main orchestrator (no parent)
        if inst.parent_instance is None:
            return False

        # Read state, last_activity, and agent_class under same lock for consistency (prevents TOCTOU race)
        with inst._state_lock:
            state = inst.state
            last_activity = inst.last_activity
            agent_class = inst.agent_class

        # Must NOT be sleeping (waiting for async results)
        if state == AgentState.SLEEPING:
            return False

        # Must NOT be actively running
        with self.pool._execution._state_lock:
            if any(n == instance_name for n, _depth in self.pool._execution.active_stack):
                return False

        # Must NOT be halted (halted agents are intentionally paused, e.g. during compression)
        if self.pool.is_instance_halted(instance_name):
            return False

        # Must have exceeded the idle timeout threshold
        # System agents (Compressor, Security) use a separate timeout setting
        idle_secs = time.monotonic() - last_activity
        is_system_agent = IdleManager._is_system_agent(agent_class)
        effective_timeout = self.pool.settings.system_agent_idle_timeout_seconds if is_system_agent else self.pool.settings.idle_timeout_seconds

        # 0 means "off" — never auto-dismiss; NaN/inf treated as always idle
        if effective_timeout == 0:
            return False

        if idle_secs < effective_timeout:
            return False

        return True

    def _auto_dismiss(self, instance_name: str):
        """Dismiss a single idle agent and clean up its resources."""
        inst = self.pool.instances.get(instance_name)
        if not inst:
            return

        idle_secs = time.monotonic() - inst.last_activity

        # Determine which timeout threshold applies to this agent
        is_system_agent = IdleManager._is_system_agent(inst.agent_class)
        effective_timeout = self.pool.settings.system_agent_idle_timeout_seconds if is_system_agent else self.pool.settings.idle_timeout_seconds

        agent_type_label = f"system agent ({inst.agent_class})" if is_system_agent else 'agent'
        logger.info(f"[idle_checker] Auto-dismissing idle {agent_type_label} '{instance_name}' "
                    f"(idle for {idle_secs:.0f}s, threshold={effective_timeout:.0f}s)")

        # Remove the instance (fires dismissal callbacks)
        self.pool.dismiss_instance(instance_name)

    # ── Holder-side dead-man's-switch (plan t220_holder_dms_PLAN §3) ──────────────
    def _run_holder_dms(self):
        """Terminate a conc=0 slot holder that has made no turn-progress for the DMS window
        while NOT in a legitimately-bounded operation (retry-loop / non-LLM-stall wedge).

        Lock discipline (plan §3.3 MAJOR): snapshot holders → evaluate the predicate with NO
        pool._cond / inst._state_lock held (lock-free getattr reads via the shared
        _holder_activity_context helper) → call terminate_instance ONLY after all reads complete.
        Never terminate from inside a lock block.
        """
        window = float(getattr(self.pool.settings, 'holder_dms_window_s', 0.0) or 0.0)
        if window <= 0:
            return  # feature disabled

        # Global pause (plan §3.2 guard #4): a paused pool must not
        # preempt a holder — the whole pool is on hold, not a wedge.
        if self._safe_is_paused():
            logger.debug('[holder_dms] pool paused — skipping DMS this tick.')
            return  # pause disables DMS preemption

        api_router = getattr(self.pool, 'api_router', None)
        scheduler = getattr(api_router, 'scheduler', None) if api_router else None
        if scheduler is None:
            return  # no scheduler → no holder registry to inspect

        pools = getattr(scheduler, '_pools', None)
        if not pools:
            return

        now = time.monotonic()

        # ── Approval signal: build the sets ONCE per tick (plan §3.2 MAJOR). ──
        # list_pending_approvals() takes a lock and rebuilds full dicts; calling it per holder
        # would be n lock acquisitions + n×m dict-builds. One call, then O(1) membership.
        pending_names = set()
        om = getattr(self.pool, 'operation_manager', None)
        if om is None:
            # Guard #1: exclusion inactive → fail toward the documented limitation, never silently.
            logger.warning('[holder_dms] operation_manager not injected — approval exclusion is INACTIVE; '
                           'a holder mid-approval could be preempted (plan §3.2 guard #1).')
        else:
            try:
                approvals = om.list_pending_approvals()
            except Exception as e:
                logger.error(f"[holder_dms] list_pending_approvals() failed: {e}", exc_info=True)
                approvals = []
            pending_names = {a.get('agent_name') for a in approvals if a.get('agent_name')}

            # Guard #2: unresolvable approval names ('unknown' or an agent_class produced by the
            # _get_agent_name fallback) cannot be mapped to a specific holder. If ANY pending approval
            # is unresolvable, skip ALL terminations this tick (fail safe toward NOT killing).
            unresolvable = {'unknown'}
            for inst in list(self.pool.instances.values()):
                ac = getattr(inst, 'agent_class', None)
                if ac:
                    unresolvable.add(ac)
            ambiguous = pending_names & unresolvable
            if ambiguous:
                logger.warning(f"[holder_dms] Pending approval(s) with unresolvable agent_name "
                               f"{sorted(ambiguous)} — skipping all terminations this tick (fail-safe).")
                return

        to_terminate = []
        for slot_key, pool in list(pools.items()):
            # Scope discipline (plan §4): holders[0] is exact only for capacity==1 (the conc=0 shared
            # slot). For capacity>1 it is arbitrary → skip and log; never preempt a possibly-wrong holder.
            capacity = getattr(pool, 'capacity', 1)
            if capacity != 1:
                logger.debug(f"[holder_dms] Skipping pool '{slot_key}' capacity={capacity} "
                             '(out of scope: holder-side DMS targets conc=0 / capacity==1 only)')
                continue

            # Reuse the shared holder-context helper (single source of the turn-progress age; no
            # parallel detector). It reads pool._running and inst attrs with NO locks held.
            ctx = _holder_activity_context(pool, now, resolver=self.pool.get_instance)
            if not ctx.get('context_available') or ctx.get('holder_name', '?') == '?':
                continue

            name = self._evaluate_wedged_holder(ctx, now, window, pending_names)
            if name:
                to_terminate.append(name)

        # ── Act only after ALL reads are done and NO locks are held (plan §3.3 MAJOR). ──
        for name in to_terminate:
            logger.warning(f"[holder_dms] Terminating wedged conc=0 holder '{name}': no turn-progress "
                           f"(last_activity) for >= {window:.0f}s while not in a bounded operation.")
            try:
                self.pool.terminate_instance(name)
            except Exception as e:
                logger.error(f"[holder_dms] terminate_instance('{name}') failed: {e}", exc_info=True)

    def _safe_is_paused(self) -> bool:
        try:
            return bool(self.pool.is_paused())
        except Exception:
            return True  # cannot confirm pause state → fail safe toward NOT terminating

    def _evaluate_wedged_holder(self, ctx, now, window, pending_names):
        """Return the holder name if it is a wedged conc=0 holder, else None.

        All holder reads are lock-free getattr results already captured in ``ctx`` by
        _holder_activity_context. The non-marker guards (state, llm_active, pending
        approval, halt-unknown) fail safe toward NOT killing on any ambiguity. The
        in-blocking-tool marker branch is the one deliberate exception: a malformed or
        absent marker means "no bounded operation in flight" → treated as a genuine
        wedge (producers only ever emit well-formed tuples or clear to None, so a
        malformed value cannot occur in practice). See the marker branch below.
        """
        holder_name = ctx['holder_name']

        # No `parent_instance is None` guard here (unlike _is_idle): a parent/orchestrator
        # can never appear as a conc=0 slot holder — it is the root that OWNS instances, not a
        # scheduler slot holder — so the holder registry never yields one.

        # Only a live RUNNING holder can be wedged (IDLE/TERMINATED → terminate would be a no-op).
        if ctx.get('holder_state') != 'RUNNING':
            return None

        # TTFB exclusion (plan §3.2): an in-flight LLM call is owned by the stream watchdog
        # (180s silence / 900s total), NOT the DMS. A holder mid-call is never auto-preempted.
        if ctx.get('llm_active'):
            return None

        # Turn-progress age keyed on inst.last_activity (NOT _last_llm_activity) — the load-bearing
        # signal (plan §2). -1.0 = never stamped → treated as not-aged (fail-safe toward NOT killing).
        age = ctx.get('turn_progress_age_s', -1.0)
        if age < window:
            return None

        # Approval-gated exclusion (plan §3.2): a holder awaiting user approval is not a wedge.
        if holder_name in pending_names:
            return None

        # Compression/halted exclusion (plan §3.2 guard #3): mid-compression/manual-halt is not a wedge.
        try:
            if self.pool.is_instance_halted(holder_name):
                return None
        except Exception:
            return None  # cannot confirm halt state → fail safe toward NOT killing

        # In-bounded-blocking-tool exclusion (Decision D): a holder mid a legitimately-bounded long
        # tool (e.g. an approved sync shell_cmd within MAX_SHELL_TIMEOUT) is not a wedge.
        #
        # Marker shape: (tool_name, start_monotonic[, bound_seconds]). The SINGLE producer is the
        # dispatch boundary (ToolDispatcher.execute_tool), which writes a well-formed 3-tuple for
        # every generic sync tool, carrying the tool's dms_bound() or the settings cap (t292). So no
        # try/except is needed to unpack — a single isinstance+length guard rejects any malformed
        # marker (non-tuple / too short) and fails safe toward termination. The carried bound (3rd
        # element) is authoritative when present; the registry fallback (_blocking_tool_bound) is kept
        # only for defensive/legacy 2-tuple markers and is no longer the primary source.
        inst = self.pool.get_instance(holder_name)
        if inst is not None:
            marker = getattr(inst, '_in_blocking_tool', None)
            if isinstance(marker, tuple) and len(marker) >= 2:
                tool_name, start_mono = marker[0], marker[1]
                carried_bound = marker[2] if len(marker) > 2 else None
                bound = carried_bound if carried_bound is not None else _blocking_tool_bound(tool_name)
                if bound is not None and start_mono is not None and (now - start_mono) < bound:
                    return None

        return holder_name
