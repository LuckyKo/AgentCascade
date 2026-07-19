"""
Propose Skill Tool — Allows agents to propose new reusable skills.

Writes full SKILL.md content (including YAML frontmatter) and registers it
via SkillManager. Supports optional self-match validation against a test task.
"""

import logging

from agent_cascade.skills.parser import parse_frontmatter
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.tools.utils import parse_tool_params

logger = logging.getLogger(__name__)


@register_tool('propose_skill', allow_overwrite=True)
class ProposeSkill(BaseTool):
    """Tool to propose a new reusable skill for future tasks."""

    name = 'propose_skill'
    description = (
        'Propose a new reusable skill for future tasks. '
        'Provide the full SKILL.md content including YAML frontmatter '
        'with name, description, and triggers fields.'
    )
    parameters = {
        'type': 'object',
        'properties': {
            'skill_content': {
                'type': 'string',
                'description': 'Full SKILL.md content including YAML frontmatter (name, description, triggers) and markdown body.',
            },
            'test_task': {
                'type': 'string',
                'description': 'Optional task text for self-match validation. If provided, the skill must match this task to be promoted.',
            },
        },
        'required': ['skill_content'],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    async def call(self, params: str, **kwargs) -> str:
        """Execute propose_skill.

        Args:
            params: JSON string with 'skill_content' (required) and
                    'test_task' (optional, for self-match validation).
            kwargs: Additional context (agent_instance_name for logging).

        Returns:
            Result message indicating success or failure.
        """
        parsed = parse_tool_params(params)

        skill_content = parsed.get('skill_content', '')
        test_task = parsed.get('test_task', '')

        if not skill_content:
            return "No skill content provided. Include YAML frontmatter with name, description, and triggers fields."

        # Get SkillManager from pool
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return "No skills system available. Skills may not have been initialized."

        # Register the skill
        success, errors = skill_manager.register_skill_from_content(
            skill_content=skill_content,
            source="auto-generated",
            task_text=test_task,
        )

        if success:
            try:
                fm, _ = parse_frontmatter(skill_content)
                name = fm.get('name', 'unknown') if fm else 'unknown'
            except Exception:
                name = 'unknown'
            return f"Skill '{name}' registered and validated successfully."
        else:
            error_detail = '; '.join(errors) if errors else 'Unknown error'
            return f"Skill registration failed: {error_detail}"