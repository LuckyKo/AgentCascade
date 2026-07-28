"""
Auto-skill proposal helpers — shared logic for the auto-skill gating/injection flow.

Extracted to eliminate duplication between execution_engine.py and run_agent_unified.py.
"""

from typing import Callable, List, Optional

from agent_cascade.settings import AUTO_SKILL_ENABLED, AUTO_SKILL_EXTRA_TURNS, LOAD_SKILL_NONE
from agent_cascade.log import logger


def run_auto_skill_proposal(
    pool,
    skill_manager,
    inst,
    task_text: str,
    instance_name: str,
    total_tool_calls: int,
    append_fn: Callable[[str], None],
    rollback_fn: Callable[[int], None],
    is_stopped: Callable[[], bool],
    engine_run_generator: Optional[Callable] = None,
) -> List[str]:
    """Run the auto-skill gating/injection flow and return created_skills list.

    Args:
        pool: AgentPool instance (for settings and rollback).
        skill_manager: SkillManager instance or None.
        inst: AgentInstance being executed.
        task_text: Extracted task text from conversation for proposal context.
        instance_name: Name of the agent instance.
        total_tool_calls: Current tool call count at entry.
        append_fn: Callable to append a user message to the instance's conversation.
        rollback_fn: Callable(pop_count) to roll back instance conversation.
        is_stopped: Callable() returning True if execution should stop.
        engine_run_generator: Optional callable that yields turn outputs when called.
            If None, assumes inst has an .engine attribute with a .run(inst) generator.

    Returns:
        List of created skill names (empty if no skills were created).
    """
    # Unified auto-skill gating: both toggles must be ON, using pool settings as single source of truth
    _auto_skill_enabled = getattr(pool.settings, 'auto_skill_enabled', AUTO_SKILL_ENABLED)
    _load_mode = getattr(pool.settings, 'default_load_skill_mode', 'AUTO')

    if not (_auto_skill_enabled and skill_manager and _load_mode != LOAD_SKILL_NONE):
        return []

    # Snapshot under lock: conversation length + skills registry
    with inst._compression_lock:
        _conv_length = len(inst.conversation)
    _skills_before = set(skill_manager.get_skill_names())

    # Check trigger and inject prompt
    if not skill_manager.check_and_inject_auto_skill_prompt(
        inst=inst,
        total_tool_calls=total_tool_calls,
        task_text=task_text,
        instance_name=instance_name,
        append_fn=append_fn,
    ):
        return []

    # Set turn limit and let the engine loop handle extra turns
    _orig_max_turns = inst.max_turns
    inst.max_turns = AUTO_SKILL_EXTRA_TURNS
    try:
        if engine_run_generator is not None:
            gen = engine_run_generator()
        else:
            gen = inst.engine.run(inst)

        for turn_output_raw in gen:
            if is_stopped():
                break
            # Unpack (turn_output, is_streaming) tuple
            if isinstance(turn_output_raw, tuple) and len(turn_output_raw) == 2:
                turn_output = turn_output_raw[0]
            else:
                turn_output = turn_output_raw
            # Count tool calls from FUNCTION role messages
            from agent_cascade.llm.schema import FUNCTION
            total_tool_calls += sum(1 for m in turn_output if (
                m.get('role', '') == FUNCTION
                if isinstance(m, dict) else getattr(m, 'role', '') == FUNCTION
            ))
    except Exception as e:
        logger.warning("[AUTO-SKILL] Extra turn error for %s: %s", instance_name, e)
    finally:
        inst.max_turns = _orig_max_turns

    created_skills = skill_manager.finalize_auto_skill(
        inst=inst,
        instance_name=instance_name,
        snapshot_length=_conv_length,
        rollback_fn=lambda pop_count: rollback_fn(pop_count),
        check_skill_created_fn=lambda: list(
            set(skill_manager._skills_registry.keys()) - frozenset(_skills_before)),
    )

    # Inject notice into the last message
    from agent_cascade.skills.manager import inject_skill_notice
    inject_skill_notice(inst, created_skills)

    return created_skills