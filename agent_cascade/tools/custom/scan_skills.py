"""
Scan Skills Tool — Read-only tool that queries SkillManager and returns matching skills.

Allows agents to discover available skills and their relevance scores for a given query.
This is the primary way orchestrators decide which skills to load via call_agent(load_skill=[...]).
"""

import logging

from agent_cascade.skills.manager import rating_sort_key
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.tools.utils import parse_tool_params

logger = logging.getLogger(__name__)

# Max match bullets rendered in query mode. The matcher returns every skill with a
# non-zero score (hundreds on a broad query — ~45k chars / ~11k tokens per call),
# which is the cost driver of perceived scan_skills latency. Capping at 10 keeps the
# ranking contract (top-10 is what any consumer acts on) and cuts output ~92%.
SKILL_SCAN_MAX_RESULTS = 10


def _format_chars(chars: int) -> str:
    """Format a character count as a rounded '~N.Nk chars' display string."""
    return f"~{chars // 1000}.{'0' if chars % 1000 < 500 else '5'}k chars"


@register_tool('scan_skills', allow_overwrite=True)
class ScanSkills(BaseTool):
    """Read-only tool to query available skills and their relevance scores."""

    name = 'scan_skills'
    description = ('Scan registered skills and return matching skills with relevance scores. '
                   'Use this to discover which skills are available before calling call_agent with load_skill. '
                   'Returns skill names, descriptions, match scores, and quality ratings for the given query.')
    parameters = {
        'type': 'object',
        'properties': {
            'query': {
                'type':
                    'string',
                'description':
                    'Search query or task description to match against available skills. Leave empty to list all registered skills.',
            },
            'active': {
                'type': 'boolean',
                'description': (
                    'If true, restrict the no-query listing to ACTIVE skills only (exclude disabled/'
                    'inactive ones). Default: false — the default listing includes disabled/inactive '
                    'skills, each marked with an " (inactive)" suffix.'
                ),
            },
        },
        'required': [],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        """Execute the scan_skills tool.

        Args:
            params: JSON string or dict with 'query' field.
            kwargs: Additional context (agent_instance_name for logging).

        Returns:
            Formatted markdown list of matching skills.
        """
        parsed = parse_tool_params(params)
        query = parsed.get('query', '')
        active_only = parsed.get('active', False)

        # Get SkillManager from pool
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return 'No skills system available. Skills may not have been initialized.'

        # Trigger a fresh discovery (cache-respecting) so new skills appear
        skill_manager._ensure_discovered()

        # Data path: the DEFAULT listing includes disabled/inactive skills (each marked " (inactive)"
        # below); active=True restricts to the active registry only. In production disabled skills are
        # absent from the registry (discover drops them), so get_all_metadata re-surfaces them on the
        # default path via a servable-disk walk.
        # get_all_metadata(include_active_only=...) already enforces the range: active=True returns
        # registry-only skills (discover() guarantees disabled ones are absent from the registry),
        # active=False additionally re-surfaces disabled-but-servable skills. No manual filter needed.
        all_skills = skill_manager.get_all_metadata(include_active_only=active_only)
        if not all_skills:
            return 'No skills are currently registered in the system.'

        # If no query, list everything sorted by average rating (desc); unrated last, name asc tiebreak.
        if not query.strip():

            def _sort_key(skill):
                return rating_sort_key(skill['name'], skill_manager.get_rating_average(skill['name']))

            # Candidate marker data (no-query mode only): names whose registry winner is a
            # candidate file, plus the incumbent version parsed from its production file.
            candidate_names = set(skill_manager.get_candidate_names())
            _, _production_root = skill_manager._candidate_dirs()

            def _incumbent_version(name: str) -> str:
                prod_file = _production_root / name / 'SKILL.md'
                if not prod_file.exists():
                    return '?'
                try:
                    from agent_cascade.skills.parser import parse_skill_file
                    return parse_skill_file(prod_file).get('version', '?')
                except (OSError, FileNotFoundError):
                    return '?'

            # Inactive marker (no-query mode): mark every disabled/inactive skill. Driven by
            # _disabled_names via is_skill_disabled() — the SAME source the match filter uses,
            # so a SKILLS_DISABLED-style disable (in _disabled_names but with no metrics entry)
            # is marked here too, keeping marker and filter in agreement. active=True hides them.

            lines = ['## Available Skills']
            for skill in sorted(all_skills, key=_sort_key):
                source = skill.get('source', 'system')
                version = skill.get('version', '1.0.0')
                chars = skill.get('chars', 0)
                rating = skill_manager.get_rating_average(skill['name'])
                rating_str = f'{rating}' if rating is not None else 'n/a'
                candidate_note = (f" (candidate, pending decision vs v{_incumbent_version(skill['name'])})"
                                  if skill['name'] in candidate_names else '')
                inactive_note = ' (inactive)' if skill_manager.is_skill_disabled(skill['name']) else ''
                lines.append(f"- **{skill['name']}** [{source}] v{version} "
                             f"(rating: {rating_str}, {_format_chars(chars)})"
                             f"{candidate_note}{inactive_note}: "
                             f"{skill.get('description', 'No description')}")
            return '\n'.join(lines)

        # Use public API to score skills against the query. When the caller wants the
        # DEFAULT (active=False) view, match over the SAME full set the listing uses
        # (include_inactive=True) so an exact match on a retired skill surfaces here
        # instead of "No skills matched". active=True keeps matching active-only.
        matches = skill_manager.match_skills(query, include_inactive=not active_only)
        if not matches:
            # No full-catalog dump here: an out-of-domain query used to print every
            # registered skill (~45k chars), which is exactly the noise this tool
            # exists to avoid. Point at the no-query listing instead — one deliberate
            # call, capped output.
            return (f"No skills matched the query '{query}'.\n"
                    'Call scan_skills with an empty query to list all registered skills.')

        # Build response with scores. Cap the rendered bullets: the matcher returns
        # every skill with a non-zero score, and printing hundreds of bullets per call
        # is the output-volume defect (todo 123). Top-10 preserves the ranking contract.
        total_matches = len(matches)
        truncated = total_matches > SKILL_SCAN_MAX_RESULTS
        matches = matches[:SKILL_SCAN_MAX_RESULTS]

        # Resolve display fields from the already-fetched full listing (``all_skills``)
        # rather than the registry, so registry-absent (inactive) matches still show a
        # real description/source/version/chars. ``all_skills`` carries name/description/
        # source/version/chars for both active and inactive skills.
        meta_by_name = {s['name'].lower(): s for s in all_skills}
        lines = [f"## Skills Matching Query: '{query}'"]
        for name, score in matches:
            meta = meta_by_name.get(name.lower())
            desc = meta.get('description', 'No description') if meta else 'Unknown'
            source = meta.get('source', 'system') if meta else 'unknown'
            version = meta.get('version', '1.0.0') if meta else '1.0.0'
            chars = meta.get('chars', 0) if meta else 0
            metrics = skill_manager.get_metrics(name)
            loads = metrics.get('total_loads', 0)
            rating = skill_manager.get_rating_average(name)
            rating_str = f'{rating}' if rating is not None else 'n/a'
            inactive_note = ' (inactive)' if skill_manager.is_skill_disabled(name) else ''
            lines.append(f"- **{name}** [{source}] v{version} (score: {score:.2f}, loads: {loads}, "
                         f"rating: {rating_str}, {_format_chars(chars)}){inactive_note}: {desc}")

        if truncated:
            lines.append(f"(showing top {SKILL_SCAN_MAX_RESULTS} of {total_matches} matches)")
        return '\n'.join(lines)
