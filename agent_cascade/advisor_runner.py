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
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AdvisorResult:
    """Structured result from an advisor agent invocation."""

    output_text: str = ""       # Raw text output from the agent (empty on timeout/error)
    was_timeout: bool = False   # True if first-yield timeout fired before any yield
    was_error: bool = False     # True if engine.run() raised or instance creation failed
    error_msg: str = ""         # Exception message if was_error
    latency_ms: float = 0.0     # Wall-clock time for the advisor call (ms)

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
    from agent_cascade.log import logger

    # Imports resolved lazily to avoid a circular import at module load time and to
    # mirror the defensive-import style used in security_handler.py._execute_check().
    from agent_cascade.execution_engine import ExecutionEngine
    from agent_cascade.constants import NON_LLM_KEYS, DEFAULT_SECURITY_DISABLED_TOOLS
    from agent_cascade.utils import merge_disabled_tools_for_auto_agent
    from agent_cascade.settings import SECURITY_AGENT_MAX_TURNS

    result = AdvisorResult()
    start_time = time.perf_counter()

    engine: Optional[ExecutionEngine] = None
    instance = None
    first_yield_timer: Optional[threading.Timer] = None
    first_yield_event = threading.Event()

    def _first_yield_timeout_trigger():
        logger.warning(
            "[ADVISOR] First-yield timeout trigger fired for '%s' after %.0fs — model has not yielded.",
            instance_name, first_yield_timeout,
        )
        first_yield_event.set()

    try:
        # ── 1. Fresh engine per call (NOT shared) ────────────────────────────
        engine = ExecutionEngine(pool)

        # ── 2. Create the advisor instance (always fresh) ────────────────────
        instance = engine._create_system_agent(
            agent_class=agent_class,
            instance_name=instance_name,
            task=task,
            caller=caller,
        )

        # ── 3. Turn budget ───────────────────────────────────────────────────
        if max_turns is None:
            instance.max_turns = SECURITY_AGENT_MAX_TURNS
        else:
            instance.max_turns = int(max_turns)

        # ── 4. Tool-filtering config (SAME as security_handler.py:445-464) ───
        ui_cfg = copy.deepcopy(generate_cfg or {})
        llm_safe_cfg = {k: v for k, v in ui_cfg.items() if k not in NON_LLM_KEYS}
        if 'disabled_tools' in ui_cfg:
            llm_safe_cfg['disabled_tools'] = ui_cfg['disabled_tools']
        existing_disabled = llm_safe_cfg.get('disabled_tools', [])
        llm_safe_cfg['disabled_tools'] = merge_disabled_tools_for_auto_agent(
            existing_disabled, agent_class, DEFAULT_SECURITY_DISABLED_TOOLS
        )

        template = pool.get_template(agent_class) if hasattr(pool, 'get_template') else None
        if template is not None and hasattr(template, 'llm'):
            cfg = (template.llm.generate_cfg or {}).copy()
            cfg.update(llm_safe_cfg)
            instance._generate_cfg_override = cfg
        else:
            logger.warning("[ADVISOR] Template missing for '%s' — using minimal config", agent_class)
            instance._generate_cfg_override = {
                'disabled_tools': llm_safe_cfg.get('disabled_tools', [])
            }

        # ── 5. First-yield timeout guard (threading.Timer + Event) ───────────
        first_yield_timer = threading.Timer(first_yield_timeout, _first_yield_timeout_trigger)
        first_yield_timer.daemon = True
        first_yield_timer.start()

        # ── 6. Run engine with a simplified loop (no streaming ticks) ────────
        got_first_yield = False
        for resp in engine.run(instance):
            if pool.stopped:
                break

            if not got_first_yield:
                got_first_yield = True
                try:
                    first_yield_timer.cancel()
                except Exception:
                    pass  # Timer may have already fired

                if first_yield_event.is_set():
                    result.was_timeout = True
                    logger.warning(
                        "[ADVISOR] First-yield timeout after %.0fs for '%s'. Generator did not yield in time.",
                        time.monotonic() - start_time, instance_name,
                    )
                    break

        # ── 7. Extract output ────────────────────────────────────────────────
        if not result.was_timeout:
            from agent_cascade.compression.helpers import extract_instance_output
            result.output_text = extract_instance_output(instance.conversation, instance_name) or ""

    except Exception as e:  # noqa: BLE001 — advisor must never crash the caller
        result.was_error = True
        result.error_msg = str(e)
        logger.error("[ADVISOR] Execution error for '%s': %s", instance_name, e)

    finally:
        # ── 8. Telemetry (non-blocking, always fires even on timeout/error) ──
        latency_ms = (time.perf_counter() - start_time) * 1000
        result.latency_ms = latency_ms
        if engine is not None:
            tel = engine._telemetry()
            if tel is not None:
                try:
                    tel.record_agent_instance_call(
                        instance_name, agent_class, caller, latency_ms=latency_ms,
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
        _cleanup_advisor_instance(pool, instance_name)

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
