"""
Propose Skill Tool — Allows agents to propose new reusable skills.

Writes full SKILL.md content (including YAML frontmatter) and registers it
via SkillManager. The skill name is a required argument; if the frontmatter
name disagrees, the argument wins and the frontmatter is patched. Rating-only
mode (name + rating, no content) records a rating without modifying content.
"""

import logging
import re

from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.tools.utils import parse_tool_params

logger = logging.getLogger(__name__)


def _next_patch_version(version: str) -> str:
    """Return the next patch version for a semver string (fallback '1.0.1')."""
    try:
        parts = version.split('.')
        padded = parts + ['0'] * (3 - len(parts))
        return f"{padded[0]}.{padded[1]}.{int(padded[2]) + 1}"
    except (ValueError, IndexError):
        return '1.0.1'


@register_tool('propose_skill', allow_overwrite=True)
class ProposeSkill(BaseTool):
    """Tool to propose a new reusable skill for future tasks."""

    name = 'propose_skill'
    description = ('Propose a new reusable skill for future tasks, or rate an existing one. '
                   '`name` is always required. To CREATE/UPDATE: provide `name` plus the full '
                   'SKILL.md content (YAML frontmatter + body); if the name already exists this is '
                   'an update (patch version auto-incremented). To RATE ONLY (no content change): '
                   'provide just `name` and `rating` — no skill_content needed and no approval is '
                   'requested, because rating is not a content modification.')
    parameters = {
        'type': 'object',
        'properties': {
            'name': {
                'type':
                    'string',
                'description': ('REQUIRED. The skill name (snake_case). For new skills it becomes the '
                                'registered name; for existing names with content it targets an update; '
                                'with `rating` and no content it records a rating for that skill.'),
            },
            'skill_content': {
                'type':
                    'string',
                'description':
                    'Full SKILL.md content including YAML frontmatter (name, description, triggers) and markdown body. Required for creating/updating a skill; omit for rating-only.',
            },
            'justification': {
                'type':
                    'string',
                'description':
                    'Why this skill is needed. Required for creating/updating a skill; optional for rating-only.',
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
        'required': ['name'],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        """Execute propose_skill.

        Args:
            params: JSON string with 'name' (required), 'skill_content' + 'justification'
                    (required to create/update), and optional 'rating'. Rating-only mode is
                    'name' + 'rating' without content.
            kwargs: Additional context (agent_instance_name for logging).

        Returns:
            Result message indicating success or failure.
        """
        parsed = parse_tool_params(params)

        proposed_name = (parsed.get('name') or '').strip()
        skill_content = parsed.get('skill_content', '')
        rating_value = parsed.get('rating')

        if not proposed_name:
            return "The 'name' argument is required for propose_skill."

        # Get SkillManager from pool
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return 'No skills system available. Skills may not have been initialized.'

        # ── Rating-only mode: name + rating, no content → record and confirm ──
        # This is NOT a content modification, so it skips frontmatter validation, version bump,
        # and the user-approval flow. Security note: this bypasses approval by design (a rating is
        # not a content change) but means any agent can adjust a skill's recorded rating at any
        # time; ratings are currently advisory-only and feed no gating/scoring decision.
        if not skill_content and rating_value is not None:
            existing_meta = skill_manager.get_skill_metadata(proposed_name)
            if existing_meta is None:
                return f"Cannot rate unknown skill '{proposed_name}'. It is not in the registry."
            try:
                skill_manager.record_rating(proposed_name, float(rating_value))
            except (ValueError, TypeError) as e:
                return f"Invalid rating {rating_value!r}: must be a number between 0 and 10. ({e})"
            logger.info('[PROPOSE-SKILL] Rating-only: %s -> %s', proposed_name, rating_value)
            return (f"Recorded rating {rating_value}/10 for skill '{proposed_name}' "
                    f"(v{existing_meta.get('version', '1.0.0')}).")

        if not skill_content:
            return ('No skill content provided. To create/update a skill include full SKILL.md with '
                    'YAML frontmatter (description, triggers) and body. To rate an existing skill, '
                    'provide `rating` as well.')

        try:
            justification = parsed.get('justification')
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

        # The `name` argument is authoritative. Parse the frontmatter to (a) patch the name
        # field so the on-disk SKILL.md matches the registered name, and (b) read version/description.
        # NOTE: lightweight line-based parse — sufficient for scalar keys (name/version/description);
        # block-style or multi-line YAML values are not interpreted (only first physical line captured).
        fm = {}
        fm_match = re.match(r'^---\s*\n(.*?)\n---\s*\n?', skill_content, re.DOTALL)
        if fm_match:
            fm_text = fm_match.group(1)
            for line in fm_text.splitlines():
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$', line)
                if m:
                    fm[m.group(1)] = m.group(2).strip().strip('"\'')

            def _patch_field(text, key, value):
                """Replace or insert a top-level scalar field within frontmatter text only."""
                if re.search(r'(?m)^%s\s*:' % key, text):
                    return re.sub(r'(?m)^%s\s*:.*$' % key, f'{key}: {value}', text, count=1)
                return f'{key}: {value}\n' + text

            fm_text = _patch_field(fm_text, 'name', proposed_name)
            skill_content = skill_content[:fm_match.start(1)] + fm_text + skill_content[fm_match.end(1):]

        proposed_version = fm.get('version', '1.0.0')

        agent_name = kwargs.get('agent_instance_name', 'unknown')
        existing_meta = skill_manager.get_skill_metadata(proposed_name)
        is_update = existing_meta is not None

        from agent_cascade.skills.parser import normalize_version

        if is_update:
            existing_version = existing_meta.get('version', '1.0.0')

            # An update always advances the version: if the content carries no explicit
            # (valid semver) version, or one equal to the existing, compute the next patch.
            proposed_norm = normalize_version(proposed_version)
            effective_version = proposed_norm if (proposed_norm != '1.0.0' and proposed_norm != existing_version) \
                else _next_patch_version(existing_version)

            # Patch the version inside the FRONTMATTER BLOCK ONLY (a body line like "version: X"
            # must never be touched). Re-locate the block after the name patch above.
            fm_match2 = re.match(r'^---\s*\n(.*?)\n---\s*\n?', skill_content, re.DOTALL)
            if fm_match2:
                fm_text2 = _patch_field(fm_match2.group(1), 'version', effective_version)
                skill_content = (skill_content[:fm_match2.start(1)] + fm_text2 + skill_content[fm_match2.end(1):])

            # Approval for UPDATE — content for an existing name is always an update;
            # the patch version above makes effective_version differ from existing_version.
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
            verb = 'updated' if is_update else 'registered'
            return f"Skill '{proposed_name}' {verb} successfully (v{effective_version})."
        else:
            error_detail = '; '.join(errors) if errors else 'Unknown error'
            return f"Skill registration failed: {error_detail}"
