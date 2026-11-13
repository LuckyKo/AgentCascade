"""
IdleManager — idle detection and auto-dismissal. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Optional

from agent_cascade.log import logger
from agent_cascade.settings import (
    SECURITY_REUSE_APPROVAL_NAME,
    SECURITY_REUSE_SKILL_ADVISOR_NAME,
)
from agent_cascade.shared_init import STABLE_SYSTEM_PROMPT_CLASSES

from ..agent_instance import AgentState
from ..slot_queue import _holder_activity_context

# ── Holder-side dead-man's-switch: bounded-blocking-tool bounds (plan t220 §3.4, generalized t292) ──
# A holder carrying an ``_in_blocking_tool`` marker is EXCLUDED from auto-preempt while
# (now - start) < bound. The dispatch boundary (ToolDispatcher.execute_tool) is the producer:
# for generic tools it writes a 3-tuple carrying the tool's ``BaseTool.dms_bound()`` value (or
# the settings cap ``holder_dms_max_bound_s`` when the tool declares no finite bound). call_cli_agent
# is a special case that carries a TAIL-INCLUSIVE bound (effective CLI timeout +
# _CLI_AGENT_TEARDOWN_MARGIN_S, deliberately NOT capped at the settings cap) because its container
# is hard-killed at the timeout. So supporting a new long generic tool means implementing its
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


def _marker_shield_state(inst, now) -> str:
    """Return 'none' | 'shielded' | 'past' for the instance's ``_in_blocking_tool`` marker.

    Shared arithmetic for BOTH the holder-side DMS (``_evaluate_wedged_holder``) and the
    root/orchestrator scan (``_is_wedged_root``) so they decide shield/past at the same
    wall-clock point — ONE deadline computation, no drift, no second timer. The *posture* (what
    each caller does with each state) stays local: the holder terminates on 'none' (no bounded op
    in flight → a retry-loop wedge), while the root skips on 'none' (no in-flight tool → not this
    watchdog's target). A three-state enum — not a bool — is load-bearing precisely because those
    no-marker fail-safe postures are opposite (skill: shared-predicate-opposite-postures-multistate).

        'none'     — no well-formed marker (absent / non-tuple / too short / ``inst`` is None).
        'shielded' — well-formed marker and (now - start) < resolved bound.
        'past'     — well-formed marker at or past its bound. A marker whose resolved bound is
                     None (legacy 2-tuple with no registry entry) or whose start stamp is missing
                     maps to 'past' (NO shield) — fail safe toward termination, exactly the old
                     ``bound is not None and start_mono is not None`` short-circuit.
    """
    if inst is None:
        return 'none'
    marker = getattr(inst, '_in_blocking_tool', None)
    if not (isinstance(marker, tuple) and len(marker) >= 2):
        return 'none'
    tool_name, start_mono = marker[0], marker[1]
    carried_bound = marker[2] if len(marker) > 2 else None
    bound = carried_bound if carried_bound is not None else _blocking_tool_bound(tool_name)
    if bound is None or start_mono is None:
        return 'past'  # no resolvable bound → no shield, fail safe toward termination
    return 'shielded' if (now - start_mono) < bound else 'past'


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

        # Warm-reuse instances are reserved for prefix-cache reuse; never reclaim them via
        # idle-dismissal or every gap > SYSTEM_AGENT_IDLE_TIMEOUT forces a cold re-seed (BUG_0265).
        if instance_name in (SECURITY_REUSE_APPROVAL_NAME, SECURITY_REUSE_SKILL_ADVISOR_NAME):
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

        # Snapshot the clock once; the unresolvable-approval scan and BOTH passes below share it.
        # The scheduler-pool registry (api_router → scheduler → _pools) is resolved lazily above the
        # holder pass and is OPTIONAL: the holder pass needs it, but a wedged ROOT owns no scheduler
        # slot, so the root pass must run even without a scheduler (BUG_0245). Hence no early return.
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

        # ── Holder pass: conc=0 / capacity==1 slot holders resolved from the scheduler's pool
        # registry. Needs api_router → scheduler → _pools; a missing scheduler merely SKIPS this
        # pass (a root holds no slot), so it is not an early return of the whole net. ──
        api_router = getattr(self.pool, 'api_router', None)
        scheduler = getattr(api_router, 'scheduler', None) if api_router else None
        pools = getattr(scheduler, '_pools', None) if scheduler else None
        if pools:
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

        # ── Root pass (BUG_0245): a root/orchestrator wedged in an in-flight tool is INVISIBLE to
        # the holder pass — it owns instances and never holds a scheduler slot, so pool._running
        # never yields it — yet it carries the very same _in_blocking_tool marker the holder pass
        # shields on. Scan pool.instances (the one registry _checker_loop already walks: NO new
        # container) and collect wedged roots via the shared shield. Dedup against to_terminate so
        # a fake holder (which, like every test double, lacks parent_instance and so also looks
        # root-like here) is collected exactly once; real holders carry parent_instance, so the two
        # passes are disjoint by _is_wedged_root guard #1. Runs even when scheduler is None. ──
        for name, inst in list(self.pool.instances.items()):
            if name in to_terminate:
                continue
            if self._is_wedged_root(name, inst, now, window, pending_names):
                to_terminate.append(name)

        # ── Act only after ALL reads are done and NO locks are held (plan §3.3 MAJOR). ──
        for name in to_terminate:
            logger.warning(f"[holder_dms] Terminating wedged instance '{name}': no turn-progress "
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
        # tool (e.g. an approved sync shell_cmd within MAX_SHELL_TIMEOUT) is not a wedge. The shield
        # arithmetic lives in the shared _marker_shield_state helper (also used by _is_wedged_root) so
        # holder and root decide shield/past at the same wall-clock point. The holder posture: 'shielded'
        # → skip; 'none' (no/malformed marker → no bounded op in flight, i.e. the retry-loop wedge class)
        # and 'past' (past the carried bound) both fall through to termination. The marker is written by
        # the single dispatch boundary as a well-formed 3-tuple carrying dms_bound() or the settings cap.
        inst = self.pool.get_instance(holder_name)
        if _marker_shield_state(inst, now) == 'shielded':
            return None

        return holder_name

    def _is_wedged_root(self, name, inst, now, window, pending_names):
        """Return True if ``inst`` is a wedged root/orchestrator stuck in an in-flight tool.

        BUG_0245 scan-scope extension: the holder pass never sees the root (it owns instances, never
        holds a scheduler slot, so pool._running never yields it), yet it carries the same
        ``_in_blocking_tool`` marker the holder pass shields on — so the existing deadline/shield
        machinery needs only a second pass over pool.instances to reach it. Mirrors the holder
        predicate's fail-safes (every guard fails safe toward NOT killing; cheap getattr reads first,
        pool method calls last). The one deliberate divergence is guard #7: the root requires a
        PAST-bound marker, because (unlike a holder) 'no marker' on a root does not mean a retry-loop
        wedge — it means nothing in flight, which is not this watchdog's target.
        """
        # 1. Roots only. A child/holder (parent_instance set) is the holder pass's job, so the two
        #    passes are disjoint by construction. The getattr default keeps doubles (no attr) working.
        if getattr(inst, 'parent_instance', None) is not None:
            return False
        # 2. THE idempotency guard. A wedged root's finally at the dispatch boundary never runs, so
        #    its marker LEAKS — but terminate_instance() transitions state to TERMINATED, so every
        #    later tick skips it and the abandoned (never-joined) parked thread never re-fires.
        #    getattr mirrors slot_queue's holder_state read (state.name).
        if getattr(getattr(inst, 'state', None), 'name', None) != 'RUNNING':
            return False
        # 3. An in-flight LLM call is owned by the stream watchdog (180s silence / 900s total), NOT the DMS.
        if getattr(inst, '_llm_call_active', False):
            return False
        # 4. Still making turn-progress. Keyed on inst.last_activity (NOT _last_llm_activity), the
        #    same signal as the holder turn_progress_age_s; <= 0 means never-stamped → not aged.
        last_activity = getattr(inst, 'last_activity', 0.0)
        if last_activity <= 0.0 or (now - last_activity) < window:
            return False
        # 5. Mid manual-halt / compression is not a wedge (fail safe if halt state is unknown).
        try:
            if self.pool.is_instance_halted(name):
                return False
        except Exception:
            return False
        # 6. Awaiting user approval → not a wedge.
        if name in pending_names:
            return False
        # 7. Requires a PAST-bound marker: no in-flight tool ('none') or still within the declared
        #    bound ('shielded') → not disturbed. A legitimately-long root tool (e.g. shell_cmd, carried
        #    bound = MAX_SHELL_TIMEOUT) is therefore never preempted.
        if _marker_shield_state(inst, now) != 'past':
            return False
        return True
