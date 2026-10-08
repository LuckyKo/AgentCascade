"""Shared lightweight advisor runner.

Launches a fresh ExecutionEngine + system agent, runs it synchronously in the
caller's thread with a first-yield timeout guard, and returns structured output.
Intentionally separate from :mod:`agent_cascade.security_handler` (which handles
streaming, slot management, and locks in a daemon thread). Key constraint: no lock
is held during the LLM call — callers must release state locks before invoking.
Primary consumer: the Skill Advisor (:mod:`agent_cascade.skills.advisor`).
"""

from __future__ import annotations

import copy
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

# Process-level claim registry for Security instance reuse (BUG_0029 Phase 3). The claim is
# taken by ExecutionEngine._acquire_reusable_system_agent and released in this runner's finally.
import agent_cascade.security_reuse as security_reuse

# Pre-compiled: matches a [VERDICT] line in the advisor's structured output.
# Used for early-exit so the engine stops as soon as the verdict is produced,
# preventing the LLM from continuing to execute tool calls after its answer.
_VERDICT_RE = re.compile(r'\[VERDICT\]\s*(APPROVE|DENY)', re.IGNORECASE)


@dataclass
class AdvisorResult:
    """Structured result from an advisor agent invocation."""

    output_text: str = ''  # Raw text output from the agent (empty on timeout/error)
    was_timeout: bool = False  # True if first-yield timeout fired before any yield
    was_error: bool = False  # True if engine.run() raised or instance creation failed
    error_msg: str = ''  # Exception message if was_error
    latency_ms: float = 0.0  # Wall-clock time for the advisor call (ms)

    @property
    def ok(self) -> bool:
        """True when a usable output was produced without timeout or error."""
        return not self.was_timeout and not self.was_error


def run_lightweight_advisor(
    pool,
    agent_class: str,
    instance_name: str,
    task: str,
    caller: str,
    max_turns: Optional[int] = None,
    first_yield_timeout: float = 30.0,
    generate_cfg: Optional[dict] = None,
) -> AdvisorResult:
    """Run a lightweight advisor agent synchronously and return a structured result.

    Args:
        pool: The AgentPool (provides templates, settings, instance state, telemetry).
        agent_class: e.g. ``'Security'`` — determines turn limit + tool restrictions.
        instance_name: Unique name, e.g. ``f'Security_op_{uuid4().hex[:8]}'``.
        task: The formatted prompt to give the advisor.
        caller: Parent/caller agent instance name (for telemetry + attribution).
        max_turns: Turn budget. If None, defaults to ``SECURITY_AGENT_MAX_TURNS``
            for the Security class; otherwise the provided value is used verbatim.
        first_yield_timeout: Seconds before the first-yield guard fires (last-resort
            protection against an LLM generator that never yields its first token).
        generate_cfg: Optional base UI generation config to merge with the tool
            restrictions (mirrors ``session['generate_cfg']`` in security_handler).
            When None, only the merged disabled-tools restriction is applied.

    Returns:
        An :class:`AdvisorResult`. On any failure the result carries ``was_error`` /
        ``was_timeout`` so the caller can fall back gracefully. This function never
        raises — all exceptions are captured into the result.
    """
    from agent_cascade.constants import NON_LLM_KEYS
    from agent_cascade.execution_engine import ExecutionEngine
    from agent_cascade.log import logger
    from agent_cascade.settings import (SECURITY_AGENT_MAX_TURNS, SECURITY_REUSE_ENABLED,
                                        SECURITY_REUSE_SKILL_ADVISOR_NAME)

    result = AdvisorResult()
    start_time = time.perf_counter()

    engine: Optional[ExecutionEngine] = None
    first_yield_timer: Optional[threading.Timer] = None
    first_yield_event = threading.Event()

    # BUG_0029 Phase 3 (Security instance reuse): a local rid token identifies this call's claim
    # (the per-rid instance_name is only known on the fresh-spawn fallback path). The runner always
    # acquires via acquire_security_agent (warm-reuse or fresh-spawn) before any use of `instance` /
    # `reuse_name`, and the finally releases the claim — release_claim no-ops when reuse_name is None.
    # NOTE: reuse_name MUST be initialized here (pre-try), NOT only inside the try below — if
    # ExecutionEngine(pool) raises, the finally still runs and dereferences reuse_name; an unbound
    # local would raise UnboundLocalError and MASK the real error.
    adv_rid = f'adv_{time.monotonic_ns()}'
    reuse_name: Optional[str] = None
    # BUG_0029 Phase 3 bootstrap fix: the ACTUAL instance name (fixed reuse name on a reuse/seed hit,
    # per-call fallback name on a miss). MUST be pre-bound like reuse_name — the finally below
    # dereferences it for _cleanup_advisor_instance; an unbound local would raise UnboundLocalError
    # and mask the real error if ExecutionEngine(pool) raises first.
    actual_name: Optional[str] = None
    # Pre-bound so the except/finally handlers can reference it even if ExecutionEngine(pool)
    # raises before the assignment inside the try block.  Falls back to instance_name (the
    # per-call name) which is always available as a function parameter.
    effective_name: Optional[str] = None
    # Pre-bound so the finally can reference it even if engine.run() raises before the
    # assignment inside the try; release_permit_on_abandon skips the close when it is None.
    _engine_gen = None

    def _first_yield_timeout_trigger():
        # actual_name is pre-bound to None (above) and assigned after acquire; the timer thread may
        # fire before that assignment completes, in which case it falls back to instance_name — safe.
        logger.warning(
            "[ADVISOR] First-yield timeout trigger fired for '%s' after %.0fs — model has not yielded.",
            actual_name or instance_name,
            first_yield_timeout,
        )
        first_yield_event.set()

    try:
        # ── 1. Fresh engine per call (NOT shared) ────────────────────────────
        engine = ExecutionEngine(pool)

        # ── 2. Acquire the advisor instance: warm-reuse first, fresh-spawn fallback ──────
        # Shared two-branch logic (see security_reuse.acquire_security_agent). The fallback name is
        # the legacy per-call instance_name, byte-for-byte as before.
        reuse_name = None if not SECURITY_REUSE_ENABLED else SECURITY_REUSE_SKILL_ADVISOR_NAME
        # BUG_0029 Phase 3: acquire_security_agent now returns a 3-TUPLE
        # (instance, is_reuse, actual_name). `actual_name` is the REAL instance name — the
        # fixed reuse name on a reuse/seed hit, the per-call fallback name on a miss. The
        # `finally` cleanup below must target it, or it cleans up a name that was never used.
        instance, _was_reused, actual_name = security_reuse.acquire_security_agent(
            engine=engine,
            agent_class=agent_class,
            reuse_name=reuse_name,
            fallback_name=instance_name,
            task=task,
            caller=caller,
            rid=adv_rid,
        )
        if _was_reused:
            logger.info('[SECURITY_REUSE] advisor reusing warm %s (rid=%s)', reuse_name, adv_rid)

        # The REAL instance name in pool.instances — the fixed reuse name on a
        # warm-reuse hit, the per-call fallback name on a fresh spawn.  Every
        # downstream reference (broadcast, state bookkeeping, logs, cleanup)
        # must use this, NOT the bare `instance_name` parameter.
        effective_name = actual_name or instance_name

        # ── 3. Turn budget ───────────────────────────────────────────────────
        if max_turns is None:
            instance.max_turns = SECURITY_AGENT_MAX_TURNS
        else:
            instance.max_turns = int(max_turns)

        # ── 4. Tool-filtering config ─────────────────────────────────────────
        ui_cfg = copy.deepcopy(generate_cfg or {})
        llm_safe_cfg = {k: v for k, v in ui_cfg.items() if k not in NON_LLM_KEYS}
        if 'disabled_tools' in ui_cfg:
            llm_safe_cfg['disabled_tools'] = ui_cfg['disabled_tools']
        # No hardcoded merge — UI config is authoritative (seeded by the one-time migration).

        template = pool.get_template(agent_class) if hasattr(pool, 'get_template') else None
        if template is not None and hasattr(template, 'llm'):
            cfg = (template.llm.generate_cfg or {}).copy()
            cfg.update(llm_safe_cfg)
            instance._generate_cfg_override = cfg
        else:
            logger.warning("[ADVISOR] Template missing for '%s' — using minimal config", agent_class)
            instance._generate_cfg_override = {'disabled_tools': llm_safe_cfg.get('disabled_tools', [])}

        # ── 5. First-yield timeout guard (threading.Timer + Event) ───────────
        first_yield_timer = threading.Timer(first_yield_timeout, _first_yield_timeout_trigger)
        first_yield_timer.daemon = True
        first_yield_timer.start()

        # ── 6. Run engine with streaming + early-exit on verdict ─────────────
        from agent_cascade.api_integration import broadcast_stream_update

        got_first_yield = False
        _last_send = 0.0
        _tick_num = 0
        _last_resp_len = 0

        _engine_gen = engine.run(instance)
        try:
            for resp in _engine_gen:
                if pool.stopped:
                    break

                if not got_first_yield:
                    got_first_yield = True
                    try:
                        first_yield_timer.cancel()
                    except Exception:
                        pass

                    if first_yield_event.is_set():
                        result.was_timeout = True
                        logger.warning(
                            "[ADVISOR] First-yield timeout after %.0fs for '%s'. Generator did not yield in time.",
                            time.monotonic() - start_time,
                            effective_name,
                        )
                        break

                # Streaming: broadcast per-tick updates to the UI.
                now_sec = time.monotonic()
                if isinstance(resp, tuple) and len(resp) == 2:
                    turn_output, is_streaming_tick = resp
                else:
                    turn_output, is_streaming_tick = resp, False

                # PROBE: capture the moment the engine yielded this tick (yield→enqueue timing).
                _t_yield = time.monotonic()

                _last_send, _last_resp_len = broadcast_stream_update(
                    pool=pool,
                    instance_name=effective_name,
                    turn_output=turn_output,
                    is_streaming_tick=is_streaming_tick,
                    tick_num=_tick_num,
                    now_sec=now_sec,
                    last_send=_last_send,
                    last_resp_len=_last_resp_len,
                    yield_time=_t_yield,  # PROBE: ignored when STREAM_BACKEND_DEBUG is False
                )
                _tick_num += 1

                # Keep instance_state fresh for UI (message_count)
                try:
                    if hasattr(pool, '_execution') and hasattr(pool._execution, '_state_lock'):
                        with pool._execution._state_lock:
                            if effective_name in pool.instance_state:
                                pool.instance_state[effective_name]['message_count'] = len(instance.conversation)
                except Exception:
                    pass  # non-critical — never break the advisor over UI bookkeeping

                # Early-exit: stop as soon as the verdict is present in the last
                # assistant message. Prevents the LLM from continuing to execute
                # tool calls after it has already produced its structured answer.
                _conv = instance.conversation
                if _conv:
                    _last = _conv[-1]
                    if getattr(_last, 'role', '') == 'assistant' and _VERDICT_RE.search(
                            getattr(_last, 'content', '') or ''):
                        logger.debug(
                            "[ADVISOR] Verdict detected in output — stopping early for '%s'",
                            effective_name,
                        )
                        break
        finally:
            # Ensure the generator is properly closed on any exit path (break, exception,
            # timeout) AND deterministically release any slot permit the abandoned generator
            # left behind. When the generator is abandoned while blocked in acquire(), the run()
            # exit-finally that would normally release the permit never runs, so
            # _slot_release/_slot_key stay set and pin the shared conc=0 pool forever.
            # Centralized in security_reuse.release_permit_on_abandon (idempotent; the None
            # guards for _engine_gen/instance are handled internally).
            security_reuse.release_permit_on_abandon(
                _engine_gen, instance, effective_name or instance_name,
                context='advisor timeout/abandon')

        # ── 7. Extract output ────────────────────────────────────────────────
        if not result.was_timeout:
            from agent_cascade.compression.helpers import extract_instance_output
            result.output_text = extract_instance_output(instance.conversation, actual_name) or ''

    except Exception as e:  # noqa: BLE001 — advisor must never crash the caller
        result.was_error = True
        result.error_msg = str(e)
        logger.error("[ADVISOR] Execution error for '%s': %s", effective_name or instance_name, e)

    finally:
        # ── 8. Telemetry (non-blocking, always fires even on timeout/error) ──
        latency_ms = (time.perf_counter() - start_time) * 1000
        result.latency_ms = latency_ms
        if engine is not None:
            tel = engine._telemetry()
            if tel is not None:
                try:
                    tel.record_agent_instance_call(
                        actual_name,
                        agent_class,
                        caller,
                        latency_ms=latency_ms,
                    )
                except Exception:
                    pass

        # ── 9. Timer cleanup (CRITICAL — must always run) ───────────────────
        if first_yield_timer is not None:
            try:
                first_yield_timer.cancel()
            except Exception:
                pass

        # ── 10. Cleanup: mark inactive + remove from active stack ────────────
        # BUG_0029 Phase 3 bootstrap fix: clean up the ACTUAL instance name (the fixed reuse name on
        # a reuse/seed hit), not the per-call fallback name — otherwise the real warm instance is left
        # active and on the active stack, wedging Gate 11 so advisor reuse stays dead.
        _cleanup_advisor_instance(pool, actual_name)

        # BUG_0029 Phase 3 (Security instance reuse): release the claim so the next advisor call
        # can take it. release_claim no-ops on a None/empty name AND on a mismatched rid, so this is
        # safe on every path — including fresh-spawn fallback where reuse_name is None.
        security_reuse.release_claim(reuse_name, adv_rid)

        # ── 11. Remove the fresh-spawn fallback from the pool (BUG: stale Security_op_* tab) ──
        # A warm hit / seed uses the FIXED reuse name and must persist for the next check; only a
        # fresh-spawn fallback (effective_name != reuse_name) is a one-off that _cleanup_advisor_instance
        # marked inactive but never popped from pool.instances, so its UI tab lingered. Remove it here;
        # the fixed-name warm instance is left intact. The `effective_name` truthiness check guards the
        # pre-acquire-exception path where effective_name is still None (acquire raised before assignment).
        if effective_name and effective_name != reuse_name:
            try:
                pool.remove_instance(effective_name)
            except Exception as e:  # noqa: BLE001 — cleanup must never crash the caller
                logger.debug("[ADVISOR] remove_instance failed for '%s' (non-critical): %s",
                             effective_name, e)

    return result


def _cleanup_advisor_instance(pool, instance_name: str) -> None:
    """Mark the advisor instance inactive and remove it from the active stack.

    Mirrors ``security_handler.SecurityAdvisorHandler._cleanup()`` minus the
    active-checks tracking (which is Security-specific). Never raises.
    """
    from agent_cascade.log import logger

    if not instance_name or pool is None:
        return

    # Mark instance as inactive in instance_state (thread-safe)
    try:
        with pool._execution._state_lock:
            if instance_name in pool.instance_state:
                pool.instance_state[instance_name]['active'] = False
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADVISOR] Failed to mark '%s' inactive (non-critical): %s", instance_name, e)

    try:
        pool.active_stack_remove(instance_name)
    except Exception as e:  # noqa: BLE001
        logger.debug("[ADVISOR] Active stack removal failed for '%s' (non-critical): %s", instance_name, e)
