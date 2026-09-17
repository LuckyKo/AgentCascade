"""
Propose Skill Tool — Allows agents to propose new reusable skills.

Writes full SKILL.md content (including YAML frontmatter) and registers it
via SkillManager. The skill name is a required argument; if the frontmatter
name disagrees, the argument wins and the frontmatter is patched. Rating-only
mode (name + rating, no content) records a rating without modifying content.
"""

import logging
import re

from agent_cascade.skills.matcher import find_similar_skills
from agent_cascade.skills.parser import normalize_version, parse_frontmatter
from agent_cascade.settings import SKILL_DUP_SIM_THRESHOLD
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


def _coerce_rating(value):
    """Return (float_value or None, error_string or None) for a 0-10 rating."""
    if value is None:
        return None, None
    try:
        float_val = float(value)
    except (TypeError, ValueError):
        return None, f"Invalid rating value: {value!r} (expected a number 0-10)"
    if not (0.0 <= float_val <= 10.0):
        return None, f"Invalid rating {float_val}: must be between 0 and 10."
    return float_val, None


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
            rating_float, err = _coerce_rating(rating_value)
            if err:
                return err
            skill_manager.record_rating(proposed_name, rating_float)
            logger.info('[PROPOSE-SKILL] Rating-only: %s -> %s', proposed_name, rating_value)
            return (f"Recorded rating {rating_value}/10 for skill '{proposed_name}' "
                    f"(v{existing_meta.get('version', '1.0.0')}).")

        if not skill_content:
            return ('No skill content provided. To create/update a skill include full SKILL.md with '
                    'YAML frontmatter (description, triggers) and body. To rate an existing skill, '
                    'provide `rating` as well.')

        justification = parsed.get('justification')

        if not justification:
            return "'justification' is required to create or update a skill"

        # Validate rating early when supplied alongside content.
        content_rating, err = _coerce_rating(rating_value)
        if err:
            return err

        # The `name` argument is authoritative. Match the frontmatter block ONCE, parse its
        # scalar fields, determine whether this is an update and compute the effective version
        # BEFORE reconstruction, then patch name (+version on updates) in a single pass.
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

        proposed_version = fm.get('version', '1.0.0')

        agent_name = kwargs.get('agent_instance_name', 'unknown')
        existing_meta = skill_manager.get_skill_metadata(proposed_name)
        is_update = existing_meta is not None

        # ── Hard-reject similarity gate (NEW + UPDATE) ────────────────────────
        # Reject a proposal whose frontmatter text is too similar to an existing skill,
        # before any approval request or registration. For an UPDATE we exclude the
        # incumbent of that name (an update is expected to resemble its own current
        # version), but still compare against every OTHER skill so an update cannot
        # silently collide with a different one. Frontmatter fields only — cheap.
        try:
            all_metadata = skill_manager.get_all_metadata()
        except Exception as e:  # pragma: no cover - defensive; gate must not break propose
            logger.warning('[PROPOSE-SKILL] similarity gate skipped (get_all_metadata failed): %s', e)
            all_metadata = []

        # Extract the proposal's description/triggers with the robust YAML parser so block-style
        # `triggers:` lists are read correctly (the lightweight line parse above only captures
        # scalar values and would yield an empty string for a list, making the comparison
        # asymmetric against registered metadata that stores real triggers).
        prop_fm, _ = parse_frontmatter(skill_content)
        collisions = find_similar_skills(
            proposed_name, str(prop_fm.get('description', '') or ''), prop_fm.get('triggers'),
            all_metadata, SKILL_DUP_SIM_THRESHOLD,
            exclude_names=[proposed_name] if is_update else None,
        )
        if collisions:
            lines = [f"  - '{name}' (similarity: {sim * 100:.1f}%)" for name, sim in collisions]
            logger.warning('[PROPOSE-SKILL] REJECTED duplicate %s (%d collisions, threshold %.2f)',
                           proposed_name, len(collisions), SKILL_DUP_SIM_THRESHOLD)
            return (f"REJECTED: proposed skill '{proposed_name}' is too similar to existing skills "
                    f"(threshold {SKILL_DUP_SIM_THRESHOLD * 100:.0f}%):\n"
                    + '\n'.join(lines) +
                    "\nIf this is an update, use the existing skill's name. Otherwise differentiate "
                    'the name/description/triggers and re-propose.')

        if is_update:
            existing_version = existing_meta.get('version', '1.0.0')

            # An update always advances the version: if the content carries no explicit
            # (valid semver) version, or one equal to the existing, compute the next patch.
            proposed_norm = normalize_version(proposed_version)
            effective_version = proposed_norm if (proposed_norm != '1.0.0' and proposed_norm != existing_version) \
                else _next_patch_version(existing_version)
        else:
            effective_version = normalize_version(proposed_version) or '1.0.0'

        # Patch the frontmatter block (name always; version only on updates) in one pass, so a
        # body line like "version: X" is never touched.
        if fm_match:

            def _patch_field(text, key, value):
                """Replace or insert a top-level scalar field within frontmatter text only."""
                if re.search(r'(?m)^%s\s*:' % key, text):
                    return re.sub(r'(?m)^%s\s*:.*$' % key, f'{key}: {value}', text, count=1)
                return f'{key}: {value}\n' + text

            fm_text = _patch_field(fm_match.group(1), 'name', proposed_name)
            if is_update:
                fm_text = _patch_field(fm_text, 'version', effective_version)
            skill_content = skill_content[:fm_match.start(1)] + fm_text + skill_content[fm_match.end(1):]

        if is_update:
            # Approval for UPDATE — content for an existing name is always an update;
            # the patch version above makes effective_version differ from existing_version.
            description = (f"📝 **Update Existing Skill**: {proposed_name}\n\n"
                           f"Current version: v{existing_version} → New version: v{effective_version}\n"
                           f"Justification: {justification}")
        else:
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

        # Both NEW skills and UPDATES go through register_skill_from_content. For an existing
        # name it routes to _register_candidate_upgrade (candidate folder + decision gate), so
        # an update creates a candidate that the gate later promotes/discards — it never
        # overwrites the production file directly. A new name registers/auto-promotes as before.
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
            action = 'update' if is_update else 'register'
            return f"Failed to {action} skill '{proposed_name}': {error_detail}"
