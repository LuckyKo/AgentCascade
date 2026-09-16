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
    description = ('Propose a new reusable skill for future tasks, or rate an existing one. '
                   'To CREATE/UPDATE: provide the full SKILL.md content including YAML frontmatter '
                   '(name, description, triggers). To RATE ONLY (no content change): provide just '
                   '`name` and `rating` — no skill_content needed and no approval is requested, '
                   'because rating is not a content modification.')
    parameters = {
        'type': 'object',
        'properties': {
            'skill_content': {
                'type':
                    'string',
                'description':
                    'Full SKILL.md content including YAML frontmatter (name, description, triggers) and markdown body. Required for creating/updating a skill; omit for rating-only.',
            },
            'test_task': {
                'type':
                    'string',
                'description':
                    'Optional task text for self-match validation. If provided, the skill must match this task to be promoted.',
            },
            'justification': {
                'type':
                    'string',
                'description':
                    'Why this skill is needed. Required for creating/updating a skill; optional for rating-only.',
            },
            'update_existing': {
                'type': 'boolean',
                'default': False,
                'description': 'If True and skill name exists, create a new version instead of rejecting.',
            },
            'name': {
                'type':
                    'string',
                'description': ('Optional. The registered skill name to rate (rating-only mode). '
                                'Must match an existing skill. When provided with `rating` and no '
                                '`skill_content`, records a rating without modifying content.'),
            },
            'rating': {
                'type':
                    'number',
                'minimum':
                    0,
                'maximum':
                    10,
                'description': ('Optional quality rating (0-10, 0.5 steps) for an existing skill. '
                                'Use with `name` for rating-only mode, or alongside `skill_content` to '
                                'record a rating after registration/update.'),
            },
        },
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
        rating_name = (parsed.get('name') or '').strip()
        rating_value = parsed.get('rating')

        # Get SkillManager from pool
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return 'No skills system available. Skills may not have been initialized.'

        # ── Rating-only mode: name + rating, no content → record and confirm ──
        # This is NOT a content modification, so it skips frontmatter validation, version bump,
        # and the user-approval flow. Security note: this bypasses approval by design (a rating is
        # not a content change) but means any agent can adjust a skill's recorded rating at any
        # time; ratings are currently advisory-only and feed no gating/scoring decision.
        if not skill_content and rating_name and rating_value is not None:
            existing_meta = skill_manager.get_skill_metadata(rating_name)
            if existing_meta is None:
                return f"Cannot rate unknown skill '{rating_name}'. It is not in the registry."
            try:
                skill_manager.record_rating(rating_name, float(rating_value))
            except (ValueError, TypeError) as e:
                return f"Invalid rating {rating_value!r}: must be a number between 0 and 10. ({e})"
            logger.info('[PROPOSE-SKILL] Rating-only: %s -> %s', rating_name, rating_value)
            return (f"Recorded rating {rating_value}/10 for skill '{rating_name}' "
                    f"(v{existing_meta.get('version', '1.0.0')}).")

        if not skill_content:
            return ('No skill content provided. To create/update a skill include full SKILL.md with '
                    'YAML frontmatter (name, description, triggers). To rate an existing skill, '
                    'provide `name` and `rating` instead.')

        try:
            justification = parsed.get('justification')
            update_existing = bool(parsed.get('update_existing', False))
        except (AttributeError, TypeError):
            return 'Invalid parameters for propose_skill'

        if not justification:
            return "'justification' is required to create or update a skill"

        # Validate rating early when supplied alongside content.
        content_rating = None
        if rating_value is not None:
            try:
                content_rating = float(rating_value)
            except (TypeError, ValueError):
                return f"Invalid rating value: {rating_value!r} (expected a number 0-10)"
            if not (0.0 <= content_rating <= 10.0):
                return f"Invalid rating {content_rating}: must be between 0 and 10."

        # Parse frontmatter for name and version
        fm, _ = parse_frontmatter(skill_content)
        proposed_name = fm.get('name', '') if fm else ''
        proposed_version = fm.get('version', '1.0.0') if fm else '1.0.0'

        if not proposed_name:
            return 'Skill name is required in YAML frontmatter.'

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
                    effective_version = '1.0.1'

                # Patch frontmatter with computed version
                skill_content = skill_content.replace(f'version: {proposed_version}', f'version: {effective_version}',
                                                      1)

            # Reject only if no explicit update flag AND proposed version equals existing
            if not update_existing and effective_version == existing_version:
                return (f"Skill '{proposed_name}' already exists (v{existing_version}).\n\n"
                        f"To update it, set update_existing=true or provide a higher version in frontmatter.")

            # Approval for UPDATE
            description = (f"📝 **Update Existing Skill**: {proposed_name}\n\n"
                           f"Current version: v{existing_version} → New version: v{effective_version}\n"
                           f"Justification: {justification}")
        else:
            effective_version = normalize_version(proposed_version) or '1.0.0'
            # Approval for NEW skill
            description = (f"📝 **Propose New Skill**: {proposed_name}\n\n"
                           f"Description: {fm.get('description', '') if fm else ''}\n"
                           f"Version: v{effective_version}\n"
                           f"Justification: {justification}\n\n"
                           f"This will be registered and available to all agents via scan_skills/load_skill.")

        # Request user approval (same pattern as shell_cmd)
        approved, reason = self.agent_pool.operation_manager.request_user_approval(
            agent_name=agent_name,
            tool_name='propose_skill',
            tool_args={
                'skill_content': skill_content,
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
                source='auto-generated',
            )
        else:
            success, errors = skill_manager.register_skill_from_content(
                skill_content=skill_content,
                source='auto-generated',
                task_text=test_task,
            )

        if success:
            # If a rating was supplied alongside content, record it after successful
            # registration/update. New skills already got an initial 0.5 in the manager; this
            # records the caller's explicit assessment on top of that.
            if content_rating is not None:
                try:
                    skill_manager.record_rating(proposed_name, content_rating)
                except ValueError as e:
                    logger.warning('[PROPOSE-SKILL] Failed to record rating for %s: %s', proposed_name, e)
            return f"Skill '{proposed_name}' registered successfully (v{effective_version})."
        else:
            error_detail = '; '.join(errors) if errors else 'Unknown error'
            return f"Skill registration failed: {error_detail}"
