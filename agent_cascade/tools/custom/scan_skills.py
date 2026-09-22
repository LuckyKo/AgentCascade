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

            # Inactive marker data (no-query mode only): skills persisted as status=inactive.
            # The default listing includes them (marked " (inactive)"); active=True hides them.
            inactive = skill_manager.get_inactive_names()

            lines = ['## Available Skills']
            for skill in sorted(all_skills, key=_sort_key):
                source = skill.get('source', 'system')
                version = skill.get('version', '1.0.0')
                chars = skill.get('chars', 0)
                rating = skill_manager.get_rating_average(skill['name'])
                rating_str = f'{rating}' if rating is not None else 'n/a'
                candidate_note = (f" (candidate, pending decision vs v{_incumbent_version(skill['name'])})"
                                  if skill['name'] in candidate_names else '')
                inactive_note = ' (inactive)' if skill['name'].lower() in inactive else ''
                lines.append(f"- **{skill['name']}** [{source}] v{version} "
                             f"(rating: {rating_str}, ~{chars // 1000}.{'0' if chars % 1000 < 500 else '5'}k chars)"
                             f"{candidate_note}{inactive_note}: "
                             f"{skill.get('description', 'No description')}")
            return '\n'.join(lines)

        # Use public API to score skills against the query
        matches = skill_manager.match_skills(query)
        if not matches:
            return (f"No skills matched the query '{query}'.\n\n"
                    'Available skills:\n' +
                    '\n'.join(f"- **{s['name']}** [{s.get('source', 'system')}]: {s.get('description', '')}"
                              for s in all_skills))

        # Build response with scores
        lines = [f"## Skills Matching Query: '{query}'"]
        for name, score in matches:
            meta = skill_manager.get_skill_metadata(name)
            desc = meta.get('description', 'No description') if meta else 'Unknown'
            source = meta.get('source', 'system') if meta else 'unknown'
            version = meta.get('version', '1.0.0') if meta else '1.0.0'
            metrics = skill_manager.get_metrics(name)
            loads = metrics.get('total_loads', 0)
            rating = skill_manager.get_rating_average(name)
            rating_str = f'{rating}' if rating is not None else 'n/a'
            chars = skill_manager.get_skill_chars(name)
            lines.append(f"- **{name}** [{source}] v{version} (score: {score:.2f}, loads: {loads}, "
                         f"rating: {rating_str}, ~{chars // 1000}.{'0' if chars % 1000 < 500 else '5'}k chars): {desc}")

        return '\n'.join(lines)
