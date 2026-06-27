"""
Child Agent Runner — Shared core logic for sync and async child agent execution.

Unifies the duplicated execution paths from tool_dispatcher.py, agent_pool.py,
and api_integration.py (root agent recovery). See DESIGN_REWRITE.md §4.3.
"""

from typing import Callable, Optional

from agent_cascade.log import logger
from agent_cascade.llm.schema import Message, USER
from agent_cascade.compression.helpers import extract_instance_output
from agent_cascade.loop_detection import LoopDetectedError


# ── Shared loop recovery helper (used by both root and child paths) ─────────

def _recover_from_loop(
    pool,                      # AgentPool instance
    exception: LoopDetectedError,
    instance_name: str,        # fallback agent name if e.agent_name is None
    retry_count: int,
    max_auto_retries: int,
    inject_hint: Callable[[object, Message], None],  # callback(instance, msg)
) -> tuple[int, bool]:
    """Perform loop recovery: rollback → get instance → inject hint.

    Shared between run_child_core() (child agents) and
    run_agent_in_pool_with_recovery() (root agent). The only difference
    is how the hint message gets appended — abstracted via inject_hint callback.

    Args:
        pool: The AgentPool managing all instances.
        exception: The LoopDetectedError that was raised.
        instance_name: Fallback name if e.agent_name is None.
        retry_count: Current retry count (0-based).
        max_auto_retries: Maximum retries allowed.
        inject_hint: Callable(instance, message) that atomically appends a Message
            to the agent's conversation and logs it.

    Returns:
        (retry_count, succeeded) tuple. If succeeded is False, no more retries
        should be attempted. The caller should use the returned retry_count.
    """
    looped_agent = exception.agent_name or instance_name
    pop_count = exception.pop_count or 0

    # Surgical rollback on the agent that looped.
    # If pop_count <= 0, we have no messages to roll back — recovery can't proceed safely.
    rollback_success = False
    if pop_count > 0:
        logger.warning(
            f"Loop detected for {looped_agent}: {exception.reason}. "
            f"Surgical rollback (Retry {retry_count + 1}/{max_auto_retries})."
        )
        try:
            pool.surgical_rollback(looped_agent, pop_count, reason=exception.reason)
            rollback_success = True
        except Exception as rb_err:
            logger.error(f"Rollback failed for {looped_agent}: {rb_err}")

    if not rollback_success:
        return (retry_count + 1, False)

    # Get the correct instance for hint injection
    instance = pool.get_instance(looped_agent)
    if not instance:
        logger.error(
            f"Could not find instance '{looped_agent}' for hint injection after rollback. "
            f"Aborting retry — loop may persist."
        )
        return (retry_count + 1, False)

    # Build and inject the loop avoidance hint
    hint_msg = Message(
        role=USER,
        content=(
            f"[SYSTEM]: A repetitive loop was detected ({exception.reason}). "
            f"Please try a different approach."
        ),
    )
    inject_hint(instance, hint_msg)

    return (retry_count + 1, True)


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

    Handles the entire lifecycle: creation, loop detection with rollback and retry,
    status checking, and result formatting. Returns a formatted result string.

    Args:
        max_auto_retries: Maximum number of auto-rollback retries on loop detection.

    Raises:
        Exception: Only truly unexpected errors propagate. LoopDetectedError is handled internally.
    """
    if not force_fresh:
        force_fresh = _determine_force_fresh(agent_class)

    retry_count = 0

    while retry_count <= max_auto_retries:
        # On first attempt, use force_fresh as configured.
        # On retries after rollback, reuse the existing instance to preserve the hint.
        is_retry = retry_count > 0

        # Check if execution was stopped between retries
        if pool.stopped and is_retry:
            return _format_result(
                instance_name=instance_name,
                result="Execution stopped during retry.",
                was_stopped=True,
                prefix=prefix,
            )

        try:
            inst, conv = engine._create_and_run_agent(
                agent_class, instance_name, args, caller_name, child_depth,
                force_fresh=force_fresh and not is_retry,
            )
            # Success — break out of the retry loop and proceed to result formatting
            break

        except LoopDetectedError as e:
            looped_agent = e.agent_name or instance_name

            if retry_count >= max_auto_retries:
                logger.warning(
                    f"Loop detected for {looped_agent}: {e.reason}. "
                    f"Exceeded retries ({retry_count}/{max_auto_retries}). Stopping."
                )
                return f"[{prefix} '{looped_agent}' Loop Detected]: {e.reason}"

            # Shared recovery: rollback → get instance → inject hint
            retry_count, succeeded = _recover_from_loop(
                pool=pool,
                exception=e,
                instance_name=instance_name,
                retry_count=retry_count,
                max_auto_retries=max_auto_retries,
                inject_hint=lambda inst, msg: engine._append_and_log(inst, msg),
            )

            if not succeeded:
                return f"[{prefix} '{looped_agent}' Failed]: Rollback recovery failed."

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