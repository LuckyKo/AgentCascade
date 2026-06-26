"""
Child Agent Runner — Shared core logic for sync and async child agent execution.

Unifies the duplicated execution paths from tool_dispatcher.py and agent_pool.py.
See DESIGN_REWRITE.md §4.3 for architecture rationale.
"""

from agent_cascade.log import logger
from agent_cascade.llm.schema import Message, USER
from agent_cascade.compression.helpers import extract_instance_output
from agent_cascade.loop_detection import LoopDetectedError


# ── Helper functions ────────────────────────────────────────────────────────

def _format_result(
    instance_name: str,
    result: str,
    was_terminated: bool = False,
    was_stopped: bool = False,
    prefix: str = "Agent",
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
        (was_stopped_or_halted, was_terminated) tuple.
    """
    stop_flag = pool.stopped
    halted_flag = pool.is_instance_halted(instance_name)
    was_terminated = instance_name in pool.terminated_instances
    return (stop_flag or halted_flag), was_terminated


def _determine_force_fresh(agent_class: str) -> bool:
    """Return True for 'security' and 'compressor' classes that need fresh state."""
    return agent_class.lower() in ('security', 'compressor')


# ── Core runner function ────────────────────────────────────────────────────

def run_child_core(
    engine,           # ExecutionEngine instance
    pool,             # AgentPool instance
    agent_class: str,
    instance_name: str,
    args: dict,
    caller_name: str,
    child_depth: int,
    force_fresh: bool = False,
    prefix: str = "Agent",
) -> str:
    """Core child agent execution logic shared by sync and async paths.

    Handles the entire lifecycle: creation, loop detection with rollback,
    status checking, and result formatting. Returns a formatted result string.

    Raises:
        Exception: Only truly unexpected errors propagate. LoopDetectedError is handled internally.
    """
    if not force_fresh:
        force_fresh = _determine_force_fresh(agent_class)

    try:
        inst, conv = engine._create_and_run_agent(
            agent_class, instance_name, args, caller_name, child_depth,
            force_fresh=force_fresh,
        )
    except LoopDetectedError as e:
        looped_agent = e.agent_name or instance_name
        pop_count = e.pop_count or 0

        # Surgical rollback on the agent that looped
        if pop_count > 0:
            logger.warning(
                f"Loop detected for {looped_agent}: {e.reason}. "
                f"Surgical rollback of {pop_count} messages."
            )
            try:
                pool.surgical_rollback(looped_agent, pop_count, reason=e.reason)
            except Exception as rb_err:
                logger.error(f"Rollback failed for {looped_agent}: {rb_err}")

        # Inject loop avoidance hint into the agent's conversation
        instance = pool.get_instance(looped_agent)
        if instance:
            hint_msg = Message(
                role=USER,
                content=(
                    f"[SYSTEM]: A repetitive loop was detected ({e.reason}). "
                    f"Please try a different approach."
                ),
            )
            instance.append_message(hint_msg)

        return f"[{prefix} '{looped_agent}' Loop Detected]: {e.reason}"

    # Check for null results before doing anything else
    if inst is None or not conv:
        logger.warning(
            f"{prefix} path FAILED - {instance_name} "
            f"creation returned inst={inst}, conv={bool(conv)}"
        )
        return f"[{prefix} '{instance_name}' Failed]: Internal error — agent creation returned no output."

    # Check stopped/terminated/halted status
    was_stopped, was_terminated = _check_status(pool, instance_name)

    # Extract and format result
    result = extract_instance_output(conv, instance_name, was_terminated=was_terminated)
    return _format_result(
        instance_name=instance_name,
        result=result,
        was_terminated=was_terminated,
        was_stopped=was_stopped,
        prefix=prefix,
    )