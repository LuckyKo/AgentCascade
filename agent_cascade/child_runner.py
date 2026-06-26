"""
Child Agent Runner — Shared core logic for sync and async child agent execution.

Unifies the duplicated execution paths from:
  - ToolDispatcher._run_child_sync() (tool_dispatcher.py)
  - run_child_agent() closure in register_async_call() (agent_pool.py)

Both paths call engine._create_and_run_agent() and extract_instance_output(),
but everything else was duplicated or inconsistent. This module provides a single
source of truth for:
  1. Loop detection handling (rollback + hint injection — was broken in async path)
  2. Result formatting (unified prefix, stopped/terminated semantics)
  3. Stopped/terminated state checking
  4. force_fresh flag logic

See DESIGN_REWRITE.md §4.3 for architecture rationale.
"""

from agent_cascade.log import logger


# ── Helper functions ────────────────────────────────────────────────────────

def _format_result(
    instance_name: str,
    result: str,
    was_terminated: bool = False,
    was_stopped: bool = False,
    prefix: str = "Agent",
) -> str:
    """Format child agent result string with explicit prefix.

    Args:
        instance_name: Name of the child agent instance.
        result: Extracted output from the conversation.
        was_terminated: Whether the agent was terminated by user.
        was_stopped: Whether execution was globally stopped or halted.
        prefix: Display label (e.g. "Agent" for sync, "Parallel Agent" for async).

    Returns:
        Formatted result string like "[Agent 'X' Completed]:\n{result}"
    """
    if was_stopped:
        return f"[{prefix} '{instance_name}' Stopped]: Execution was stopped by user.\n{result}"
    elif was_terminated:
        return f"[{prefix} '{instance_name}' Terminated]:\n{result}"
    else:
        return f"[{prefix} '{instance_name}' Completed]:\n{result}"


def _check_status(pool, instance_name: str) -> tuple[bool, bool]:
    """Check if an agent was stopped/halted or terminated.

    Args:
        pool: AgentPool instance with .stopped and .terminated_instances attrs.
        instance_name: Name of the agent to check.

    Returns:
        (was_stopped_or_halted, was_terminated) tuple.
    """
    # AgentPool always has .stopped and .terminated_instances attributes.
    # Direct access is safe — these are defined in the class __init__.
    was_stopped = pool.stopped
    was_halted = pool.is_instance_halted(instance_name) if hasattr(pool, 'is_instance_halted') else False
    was_terminated = instance_name in pool.terminated_instances
    return (was_stopped or was_halted), was_terminated


def _determine_force_fresh(agent_class: str) -> bool:
    """Determine if the agent should run with force_fresh=True.

    Security and compressor agents always need fresh state to avoid
    inheriting stale context from previous runs.

    Args:
        agent_class: The agent class name (case-insensitive comparison).

    Returns:
        True for 'security' and 'compressor' classes, False otherwise.
    """
    return agent_class.lower() in ('security', 'compressor')


def _handle_loop_detected(
    looped_agent: str,
    pop_count: int,
    reason: str,
    pool,
    prefix: str = "Agent",
) -> tuple[str, dict]:
    """Handle a detected loop: surgical rollback + hint injection.

    Args:
        looped_agent: Name of the agent that entered the loop.
        pop_count: Number of messages to roll back (0 means skip rollback).
        reason: Human-readable description of the loop.
        pool: AgentPool instance with surgical_rollback() method.
        prefix: Display label (e.g. "Agent" for sync, "Parallel Agent" for async).

    Returns:
        (result_string, status_dict) where status_dict has keys:
            'loop_detected', 'agent_name', 'pop_count'
    """
    # Surgical rollback on the agent that looped
    if pop_count > 0:
        logger.warning(
            f"Loop detected for {looped_agent}: {reason}. "
            f"Surgical rollback of {pop_count} messages."
        )
        pool.surgical_rollback(looped_agent, pop_count, reason=reason)

    # Inject loop avoidance hint into the agent's conversation
    from agent_cascade.llm.schema import Message, USER
    instance = pool.get_instance(looped_agent)
    if instance:
        hint_msg = Message(
            role=USER,
            content=(
                f"[SYSTEM]: A repetitive loop was detected ({reason}). "
                f"Please try a different approach."
            ),
        )
        instance.append_message(hint_msg)

    result_string = f"[{prefix} '{looped_agent}' Loop Detected]: {reason}"

    return (result_string, {
        'loop_detected': True,
        'agent_name': looped_agent,
        'pop_count': pop_count,
    })


def _extract_and_format_result(
    conv,
    instance_name: str,
    was_terminated: bool = False,
    was_stopped: bool = False,
    prefix: str = "Agent",
) -> str:
    """Extract result from conversation and format it.

    Args:
        conv: Conversation list returned by _create_and_run_agent.
        instance_name: Name of the child agent instance.
        was_terminated: Whether the agent was terminated.
        was_stopped: Whether execution was stopped or halted.
        prefix: Display label (e.g. "Agent" for sync, "Parallel Agent" for async).

    Returns:
        Formatted result string.
    """
    from agent_cascade.compression.helpers import extract_instance_output
    result = extract_instance_output(conv, instance_name, was_terminated=was_terminated)
    return _format_result(instance_name, result, was_terminated, was_stopped, prefix)


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
) -> tuple[str, dict]:
    """Core child agent execution logic shared by sync and async paths.

    This function handles the entire lifecycle of running a child agent:
      1. Create and run via engine._create_and_run_agent()
      2. Loop detection with surgical rollback + hint injection
      3. Stopped/terminated/halted state checking
      4. Result extraction and formatting

    Args:
        engine: ExecutionEngine instance for running the agent.
        pool: AgentPool instance (for rollback, status checks).
        agent_class: Class of child agent to run.
        instance_name: Name for the child agent instance.
        args: Tool arguments dict passed to the child agent.
        caller_name: Name of the calling (parent) agent.
        child_depth: Nesting depth for max_nesting enforcement.
        force_fresh: Force fresh state (no arg inheritance). Auto-set for
                     security/compressor agents unless explicitly overridden.
        prefix: Display label in result string (e.g. "Agent" or "Parallel Agent").

    Returns:
        (result_string, status_dict) where status_dict has keys:
            - 'loop_detected': bool — whether a loop was detected
            - 'was_stopped': bool — whether execution was globally stopped/halted
            - 'was_terminated': bool — whether the agent was terminated
            - 'agent_name': str — name of agent that looped (if applicable)
            - 'pop_count': int — messages rolled back (if loop detected)

    Raises:
        Exception: Only truly unexpected errors propagate. LoopDetectedError is handled internally.
    """
    # Determine force_fresh if not explicitly set by caller
    # Security and compressor agents always need fresh state
    if not force_fresh:
        force_fresh = _determine_force_fresh(agent_class)

    base_status = {
        'loop_detected': False,
        'agent_name': None,
        'pop_count': 0,
    }

    try:
        inst, conv = engine._create_and_run_agent(
            agent_class, instance_name, args, caller_name, child_depth,
            force_fresh=force_fresh,
        )
    except Exception as e:
        # Catch LoopDetectedError specifically to handle it internally
        from agent_cascade.loop_detection import LoopDetectedError
        if isinstance(e, LoopDetectedError):
            looped_agent = e.agent_name or instance_name
            result_string, status = _handle_loop_detected(
                looped_agent, e.pop_count or 0, e.reason, pool, prefix
            )
            return result_string, status
        raise

    # Check for null results before doing anything else
    if inst is None or not conv:
        logger.warning(
            f"{prefix} path FAILED - {instance_name} "
            f"creation returned inst={inst}, conv={bool(conv)}"
        )
        return (
            f"[{prefix} '{instance_name}' Failed]: Internal error — agent creation returned no output.",
            base_status,
        )

    # Check stopped/terminated/halted status
    was_stopped, was_terminated = _check_status(pool, instance_name)

    # Extract and format result
    result_string = _extract_and_format_result(
        conv, instance_name, was_terminated, was_stopped, prefix
    )

    return result_string, {
        **base_status,
        'was_stopped': was_stopped,
        'was_terminated': was_terminated,
    }