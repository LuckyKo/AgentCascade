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

    def call(self, params: str, **kwargs) -> str:
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

        # Check for duplicate name before registration (provide actionable guidance)
        try:
            fm, _ = parse_frontmatter(skill_content)
            proposed_name = fm.get('name', '') if fm else ''

            if proposed_name and proposed_name in skill_manager.get_skill_names():
                existing_meta = skill_manager.get_skill_metadata(proposed_name)
                existing_version = existing_meta.get('version', '1.0.0') if existing_meta else '1.0.0'

                # Suggest next patch version (safely handle malformed versions like "1.0")
                try:
                    parts = existing_version.split('.')
                    if len(parts) >= 3:
                        suggested_version = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
                    else:
                        # Pad with zeros for incomplete semver (e.g., "1.0" -> "1.0.1")
                        padded = parts + ['0'] * (3 - len(parts))
                        suggested_version = f"{padded[0]}.{padded[1]}.{int(padded[2]) + 1}"
                except (ValueError, IndexError):
                    suggested_version = "1.0.1"

                return (
                    f"Skill '{proposed_name}' already exists (current version: v{existing_version}).\n\n"
                    f"To update it, either:\n"
                    f"1. Edit the existing SKILL.md file directly and increment its version field.\n"
                    f"2. Use a different name (e.g., '{proposed_name}-v2' or '{proposed_name}-updated').\n\n"
                    f"Suggested version for update: v{suggested_version}"
                )
        except Exception:
            pass  # Continue with normal registration if pre-check fails

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