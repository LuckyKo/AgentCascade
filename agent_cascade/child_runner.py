"""
Child Agent Runner — Shared core logic for sync and async child agent execution.

Unifies the duplicated execution paths from tool_dispatcher.py, agent_pool.py,
and api_integration.py (root agent recovery). See DESIGN_REWRITE.md §4.3.
"""

from agent_cascade.compression.helpers import extract_instance_output
from agent_cascade.log import logger
from agent_cascade.shared_init import STABLE_SYSTEM_PROMPT_CLASSES


class ChildAgentFailedError(Exception):
    """Raised when a sub-agent's only meaningful output is a system error
    or termination notice. Propagates through slots.py to async_tools.py
    so the parent receives [Background Tool Error] instead of [Background Tool Result]."""
    pass


# ── Helper functions ────────────────────────────────────────────────────────


def _format_result(
    instance_name: str,
    result: str,
    was_terminated: bool = False,
    was_stopped: bool = False,
    prefix: str = 'Agent',
) -> str:
    """Format child agent result string with explicit prefix."""
    if was_stopped:
        return f"[{prefix} '{instance_name}' Stopped]: Execution was stopped by user.\n{result}"
    elif was_terminated:
        return f"[{prefix} '{instance_name}' Terminated]:\n{result}"
    else:
        return f"[{prefix} '{instance_name}' Completed]:\n{result}"


def _check_status(pool, instance_name: str) -> tuple[bool, bool]:
    """Check if an agent was stopped/halted or terminated.

    Returns:
        (was_stopped_by_user, was_terminated) tuple.

    Note: Compression-halt is NOT treated as user stop — it's a transient
    suspension that clears automatically via resume_all_instances(). Only
    pool.stopped (global user stop) and manual halt count as 'stopped'.
    """
    stop_flag = pool.stopped
    # Only count MANUAL halt (in _halted_instances but NOT in _compression_halted)
    # as "stopped". Compression-halt is transient.
    was_manual_halt = (instance_name in pool._halted_instances and instance_name not in pool._compression_halted)

    # Check both the set (authoritative while in pool) and the instance flag (durable after removal)
    was_terminated = instance_name in pool.terminated_instances

    # Also check the instance object directly if still accessible
    inst = pool.get_instance(instance_name)
    if inst:
        was_terminated = was_terminated or inst.is_terminated

    return (stop_flag or was_manual_halt), was_terminated


def _resolve_child_log_path(pool, instance_name: str) -> str:
    """Best-effort resolve a child's JSONL log file path.

    Uses the same source of truth as list_agents / dismiss (pool.instance_loggers), so the
    parent can reload the child from the exact file dismiss_agent would have listed. Falls
    back to a searchable hint when the logger cannot be resolved.
    """
    try:
        loggers = getattr(pool, 'instance_loggers', None)
        if loggers and hasattr(loggers, 'get'):
            logger_inst = loggers.get(instance_name)
            if logger_inst and getattr(logger_inst, 'log_path', None):
                return str(logger_inst.log_path)
    except Exception:
        pass
    return f"logs/ (search for '{instance_name}' in AgentWorkspace/logs/)"


def _last_step_hint(conv, instance_name: str) -> str:
    """One-line "last completed step" hint derived from the child's conversation tail.

    Prefers the last tool-call the child attempted, else the last assistant text (truncated).
    Reads the live conv, which is the same content the JSONL log tail holds — no extra file IO.
    """
    if not conv:
        return ''

    def _role(m):
        v = m.get('role', '') if isinstance(m, dict) else getattr(m, 'role', '')
        return str(v).lower()

    def _tool_names(m):
        is_dict = isinstance(m, dict)
        names = []
        tc = m.get('tool_calls') if is_dict else getattr(m, 'tool_calls', None)
        if isinstance(tc, list):
            for call in tc:
                nm = None
                fn = call.get('function') if isinstance(call, dict) else getattr(call, 'function', None)
                if isinstance(fn, dict):
                    nm = fn.get('name')
                elif fn is not None:
                    nm = getattr(fn, 'name', None)
                if not nm:
                    nm = call.get('name') if isinstance(call, dict) else getattr(call, 'name', None)
                if nm:
                    names.append(str(nm))
        fc = m.get('function_call') if is_dict else getattr(m, 'function_call', None)
        if fc is not None:
            nm = fc.get('name') if isinstance(fc, dict) else getattr(fc, 'name', None)
            if nm:
                names.append(str(nm))
        return names

    def _text(m):
        is_dict = isinstance(m, dict)
        c = m.get('content') if is_dict else getattr(m, 'content', '')
        if isinstance(c, str):
            return c.strip()
        if isinstance(c, list):
            parts = []
            for it in c:
                if isinstance(it, dict):
                    t = it.get('text', '')
                    if isinstance(t, str):
                        parts.append(t)
            return ' '.join(parts).strip()
        return ''

    for m in reversed(conv):
        if _role(m) == 'assistant':
            names = _tool_names(m)
            if names:
                uniq = sorted(set(names))
                return f"tool call(s): {', '.join(uniq)}"
            txt = _text(m)
            if txt:
                snippet = txt.replace('\n', ' ').strip()
                return f"last assistant note: {snippet[:120]!r}"
    return ''


def _termination_reason(pool, instance_name: str, inst, was_stopped: bool, result: str) -> str:
    """Best-effort human description of why the child stopped, from state observable at failure time.

    Deliberately conservative: it never claims a cause it cannot see. The most reliable signal
    is the turn-budget marker the engine injects on budget exhaustion; otherwise it infers from
    the instance turn counters, pool stop/halt/compression sets, and defaults to an honest
    "no explicit reason" note that still points the parent to the log.
    """
    from agent_cascade.settings import DEFAULT_MAX_TURNS

    markers = result or ''
    hit_turns_marker = '[Turn limit reached' in markers

    # Defensive numeric reads: a bare MagicMock instance auto-creates attributes that are
    # not ints, so only accept real int values and otherwise fall back to defaults
    # (test-mock-interface-parity gotcha).
    cur = 0
    eff = DEFAULT_MAX_TURNS
    if inst is not None:
        cur_val = getattr(inst, '_current_turn', 0)
        if isinstance(cur_val, int) and not isinstance(cur_val, bool):
            cur = cur_val
        mt = getattr(inst, 'max_turns', None)
        if isinstance(mt, int) and not isinstance(mt, bool) and mt > 0:
            eff = mt
    hit_turn_budget = bool(cur) and cur >= eff

    comp = False
    try:
        comp = instance_name in getattr(pool, '_compression_halted', set())
    except Exception:
        comp = False

    if hit_turns_marker or hit_turn_budget:
        return f'hit its turn budget (reached turn {cur} of {eff}).'
    if was_stopped:
        return 'stopped by user (global stop or manual halt).'
    if comp:
        return 'suspended by context/compression handling.'
    return ('terminated with no explicit reason recorded '
            '(likely user cancellation or infrastructure teardown).')


def _failure_detail(pool, instance_name: str, inst, conv, reason: str) -> str:
    """Compose the enriched detail block appended to a ChildAgentFailedError message.

    The base message stays an exact prefix (so callers and `match=` assertions keep working);
    this is only additional recoverable context the parent can act on.
    """
    log_path = _resolve_child_log_path(pool, instance_name)
    step = _last_step_hint(conv, instance_name)
    lines = [f'Reason: {reason}']
    if step:
        lines.append(f'Last completed step: {step}')
    lines.append(f'Recover the child from its log: {log_path}')
    return '\n'.join(lines)


def _determine_force_fresh(agent_class: str) -> bool:
    """Return True for 'security' and 'compressor' classes that need fresh state."""
    return agent_class.lower() in STABLE_SYSTEM_PROMPT_CLASSES


# ── Core runner function ────────────────────────────────────────────────────


def run_child_core(
    engine,  # ExecutionEngine instance
    pool,  # AgentPool instance
    agent_class: str,
    instance_name: str,
    args: dict,
    caller_name: str,
    child_depth: int,
    force_fresh: bool = False,
    prefix: str = 'Agent',
    max_auto_retries: int = 3,
) -> str:
    """Core child agent execution logic shared by sync and async paths.

    Handles the entire lifecycle: creation, loop detection with inline rollback
    (handled inside engine.run()), status checking, and result formatting.
    Returns a formatted result string.

    Args:
        max_auto_retries: Kept for backward compatibility (no longer used; rollback
            happens inline inside engine.run() up to 3 times).

    Raises:
        Exception: Only truly unexpected errors propagate. Loop detection is
        handled inline inside engine.run().
    """
    if not force_fresh:
        force_fresh = _determine_force_fresh(agent_class)

    try:
        inst, conv = engine._create_and_run_agent(
            agent_class,
            instance_name,
            args,
            caller_name,
            child_depth,
            force_fresh=force_fresh,
        )
    except (KeyboardInterrupt, SystemExit):
        # Never swallow user interrupts or explicit exits
        raise

    # Check for null results before doing anything else.
    # When inst is None but conv has content, it's a structured rejection
    # (e.g., Skill Advisor DENY) — surface the message instead of a generic error.
    if inst is None or not conv:
        if inst is None and conv:
            # Structured rejection: the engine returned an explanatory message
            # without creating an instance (e.g., advisor denied the delegation).
            rejection_msg = extract_instance_output(conv, instance_name) or ''
            if rejection_msg:
                logger.warning(f"{prefix} path REJECTED - {instance_name}: {rejection_msg[:200]}")
                return f"[{prefix} '{instance_name}' Rejected]: {rejection_msg}"
        logger.warning(f"{prefix} path FAILED - {instance_name} "
                       f"creation returned inst={inst}, conv={bool(conv)}")
        return f"[{prefix} '{instance_name}' Failed]: Internal error — agent creation returned no output."

    # Check stopped/terminated/halted status
    was_stopped, was_terminated = _check_status(pool, instance_name)

    # Extract and format result. Passing the live instance lets an auto-skill
    # extended run return its pre-reflection task output snapshot instead of the
    # reflection-tail message (plan §3.3); non-auto-skill runs are unaffected.
    result = extract_instance_output(conv, instance_name, was_terminated=was_terminated, pool=pool, instance=inst)

    # Detect if the child's only output is a system error or termination notice.
    # Check the RAW output BEFORE _format_result wraps it in "[Agent 'name' Completed]:".
    # Verified: llm_call.py produces "[SYSTEM ERROR: <msg>]" for every fatal/timeout path.
    if result and result.strip().startswith('[SYSTEM ERROR'):
        # The system-error text IS the cause; still append the log path + last step so the
        # parent can recover partial work without guessing (BUG_0083).
        detail = _failure_detail(pool, instance_name, inst, conv,
                                reason=result.strip())
        raise ChildAgentFailedError(f"Sub-agent '{instance_name}' failed: {result.strip()}\n{detail}")
    # Termination is also a failure for the parent (child didn't produce useful output)
    if was_terminated and result:
        # Base message kept an exact prefix so callers/match assertions still pass; the
        # detail adds reason + log path + last-step hint (BUG_0083).
        reason = _termination_reason(pool, instance_name, inst, was_stopped, result)
        detail = _failure_detail(pool, instance_name, inst, conv, reason=reason)
        raise ChildAgentFailedError(f"Sub-agent '{instance_name}' was terminated.\n{detail}")

    return _format_result(
        instance_name=instance_name,
        result=result,
        was_terminated=was_terminated,
        was_stopped=was_stopped,
        prefix=prefix,
    )
