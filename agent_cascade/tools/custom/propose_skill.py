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
            'justification': {
                'type': 'string',
                'description': 'Why this skill is needed. Required for both new skills and updates.',
            },
            'update_existing': {
                'type': 'boolean',
                'default': False,
                'description': 'If True and skill name exists, create a new version instead of rejecting.',
            },
        },
        'required': ['skill_content', 'justification'],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        """Execute propose_skill.

        Args:
            params: JSON string with 'skill_content' (required), 'justification' (required),
                    'test_task' (optional), and 'update_existing' (optional).
            kwargs: Additional context (agent_instance_name for logging).

        Returns:
            Result message indicating success or failure.
        """
        parsed = parse_tool_params(params)

        skill_content = parsed.get('skill_content', '')
        test_task = parsed.get('test_task', '')

        if not skill_content:
            return "No skill content provided. Include YAML frontmatter with name, description, and triggers fields."

        try:
            justification = parsed.get('justification')
            update_existing = bool(parsed.get('update_existing', False))
        except (AttributeError, TypeError):
            return "Invalid parameters for propose_skill"

        if not justification:
            return "'justification' is required for propose_skill"

        # Get SkillManager from pool
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return "No skills system available. Skills may not have been initialized."

        # Parse frontmatter for name and version
        fm, _ = parse_frontmatter(skill_content)
        proposed_name = fm.get('name', '') if fm else ''
        proposed_version = fm.get('version', '1.0.0') if fm else '1.0.0'

        if not proposed_name:
            return "Skill name is required in YAML frontmatter."

        agent_name = kwargs.get('agent_instance_name', 'unknown')
        existing_meta = skill_manager.get_skill_metadata(proposed_name)
        is_update = existing_meta is not None

        from agent_cascade.skills.parser import normalize_version

        if is_update:
            existing_version = existing_meta.get('version', '1.0.0')

            # Auto-increment version if not provided or same as existing
            effective_version = normalize_version(proposed_version)

            if effective_version == existing_version:
                # Compute next patch version
                try:
                    parts = existing_version.split('.')
                    padded = parts + ['0'] * (3 - len(parts))
                    effective_version = f"{padded[0]}.{padded[1]}.{int(padded[2]) + 1}"
                except (ValueError, IndexError):
                    effective_version = "1.0.1"

                # Patch frontmatter with computed version
                skill_content = skill_content.replace(
                    f'version: {proposed_version}',
                    f'version: {effective_version}',
                    1
                )

            # Reject only if no explicit update flag AND proposed version equals existing
            if not update_existing and effective_version == existing_version:
                return (
                    f"Skill '{proposed_name}' already exists (v{existing_version}).\n\n"
                    f"To update it, set update_existing=true or provide a higher version in frontmatter."
                )

            # Approval for UPDATE
            description = (
                f"📝 **Update Existing Skill**: {proposed_name}\n\n"
                f"Current version: v{existing_version} → New version: v{effective_version}\n"
                f"Justification: {justification}"
            )
        else:
            effective_version = normalize_version(proposed_version) or '1.0.0'
            # Approval for NEW skill
            description = (
                f"📝 **Propose New Skill**: {proposed_name}\n\n"
                f"Description: {fm.get('description', '') if fm else ''}\n"
                f"Version: v{effective_version}\n"
                f"Justification: {justification}\n\n"
                f"This will be registered and available to all agents via scan_skills/load_skill."
            )

        # Request user approval (same pattern as shell_cmd)
        approved, reason = self.agent_pool.operation_manager.request_user_approval(
            agent_name=agent_name,
            tool_name='propose_skill',
            tool_args={
                'skill_content': skill_content[:500] + '...' if len(skill_content) > 500 else skill_content,
                'justification': justification,
                'update_existing': is_update,
            },
            description=description,
        )

        if not approved:
            return f"REJECTED: {reason}"

        # Proceed with registration or update
        if is_update:
            success, errors = skill_manager.update_skill_in_place(
                name=proposed_name,
                skill_content=skill_content,
                source="auto-generated",
            )
        else:
            success, errors = skill_manager.register_skill_from_content(
                skill_content=skill_content,
                source="auto-generated",
                task_text=test_task,
            )

        if success:
            return f"Skill '{proposed_name}' registered successfully (v{effective_version})."
        else:
            error_detail = '; '.join(errors) if errors else 'Unknown error'
            return f"Skill registration failed: {error_detail}"