"""
Child Agent Runner — Shared core logic for sync and async child agent execution.

Unifies the duplicated execution paths from tool_dispatcher.py, agent_pool.py,
and api_integration.py (root agent recovery). See DESIGN_REWRITE.md §4.3.
"""

from typing import Optional

from agent_cascade.log import logger
from agent_cascade.compression.helpers import extract_instance_output


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
        Exception: Only truly unexpected errors propagate. LoopDetectedError is handled internally.
    """
    if not force_fresh:
        force_fresh = _determine_force_fresh(agent_class)

    try:
        inst, conv = engine._create_and_run_agent(
            agent_class, instance_name, args, caller_name, child_depth,
            force_fresh=force_fresh,
        )
    except (KeyboardInterrupt, SystemExit):
        # Never swallow user interrupts or explicit exits
        raise

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