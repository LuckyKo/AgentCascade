"""
Load Skill Tool — Allows agents to inject skill instructions at runtime.

Loads registered skills and appends their full instructions as USER messages
to the current agent instance's conversation, enabling self-augmentation during
task execution. Uses the same skill infrastructure as init-time auto-loading.
"""

import logging

from agent_cascade.llm.schema import Message, USER
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.tools.utils import parse_tool_params

logger = logging.getLogger(__name__)


@register_tool('load_skill', allow_overwrite=True)
class LoadSkill(BaseTool):
    """Tool to load skill instructions into the current agent's context at runtime."""

    name = 'load_skill'
    description = (
        'Load registered skill instructions into your current context at runtime. '
        'Use this when you need specialized expertise for your task. '
        'Takes one or more skill names and injects their full instructions as guidelines.'
    )
    parameters = {
        'type': 'object',
        'properties': {
            'skill_names': {
                'oneOf': [
                    {
                        'type': 'string',
                        'description': 'A single skill name to load.',
                    },
                    {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'List of skill names to load (e.g., ["code-review", "docker-best-practices"]).',
                    }
                ],
                'description': 'Skill name(s) to load into your context.',
            },
        },
        'required': ['skill_names'],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        """Execute load_skill.

        Args:
            params: JSON string or dict with 'skill_names' (string or list).
            kwargs: Additional context (agent_instance_name, agent_obj for resolving target).

        Returns:
            Summary of which skills loaded successfully and which failed.
        """
        parsed = parse_tool_params(params)
        skill_names_raw = parsed.get('skill_names', [])

        # Normalize to list
        if isinstance(skill_names_raw, str):
            skill_names = [skill_names_raw]
        elif isinstance(skill_names_raw, list):
            skill_names = skill_names_raw
        else:
            return "Invalid skill_names parameter. Provide a string or list of strings."

        if not skill_names:
            return "No skill names provided."

        # Get SkillManager from pool
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return "No skills system available. Skills may not have been initialized."

        # Resolve the target agent instance to append messages to
        agent_obj = kwargs.get('agent_obj')
        agent_name = (
            kwargs.get('agent_instance_name') or
            getattr(agent_obj, 'instance_name', None) if agent_obj else None or
            self.agent_name or
            'Orchestrator'
        )

        inst = None
        if self.agent_pool:
            try:
                inst = self.agent_pool.get_instance(agent_name)
            except Exception as e:
                logger.debug(f"Failed to get instance '{agent_name}': {e}")

        if inst is None:
            return f"Could not find agent instance '{agent_name}' to load skills into."

        # Load each skill and inject as user message
        loaded = []
        failed = []

        for name in skill_names:
            body = skill_manager.load_full_instructions(name)
            if body is None:
                failed.append(name)
                continue

            # Format the injected message
            content = f"## Loaded Skill: {name}\n\n{body}\n\nApply the above guidelines to your current task."

            # Append as USER message
            msg = Message(role=USER, content=content)
            inst.append_message(msg)
            loaded.append(name)

            logger.info("[SKILLS] Runtime load: injected skill '%s' into instance '%s'", name, agent_name)

        # Build summary
        lines = []
        if loaded:
            lines.append(f"Successfully loaded {len(loaded)} skill(s): {', '.join(loaded)}")
        if failed:
            lines.append(f"Failed to load {len(failed)} skill(s) (not found): {', '.join(failed)}")

        return '\n'.join(lines)