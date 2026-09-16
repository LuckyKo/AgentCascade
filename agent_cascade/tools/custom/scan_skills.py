"""
Scan Skills Tool — Read-only tool that queries SkillManager and returns matching skills.

Allows agents to discover available skills and their relevance scores for a given query.
This is the primary way orchestrators decide which skills to load via call_agent(load_skill=[...]).
"""

import logging

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
            'all': {
                'type': 'boolean',
                'description': 'If true, include disabled skills in results. Default: false.',
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
        show_all = parsed.get('all', False)

        # Get SkillManager from pool
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return 'No skills system available. Skills may not have been initialized.'

        # Trigger a fresh discovery (cache-respecting) so new skills appear
        skill_manager._ensure_discovered()

        all_skills = skill_manager.get_all_metadata()
        if not all_skills:
            return 'No skills are currently registered in the system.'

        # Filter disabled skills when all=False
        if not show_all:
            disabled = getattr(skill_manager, '_disabled_names', set())
            all_skills = [s for s in all_skills if s['name'] not in disabled]
            if not all_skills:
                return 'No skills are currently registered in the system.'

        # If no query, list everything sorted by average rating (desc); unrated last, name asc tiebreak.
        if not query.strip():

            def _sort_key(skill):
                avg = skill_manager.get_rating_average(skill['name'])
                rated = avg is not None
                return (not rated, -(avg or 0.0), skill['name'].lower())

            lines = ['## Available Skills']
            for skill in sorted(all_skills, key=_sort_key):
                source = skill.get('source', 'system')
                version = skill.get('version', '1.0.0')
                rating = skill_manager.get_rating_average(skill['name'])
                if rating is None:
                    rating_str = 'n/a'
                else:
                    count = (skill_manager.get_metrics(skill['name']).get('ratings') or {}).get('count', 0)
                    rating_str = f'{rating}×{count}'
                lines.append(f"- **{skill['name']}** [{source}] v{version} (rating: {rating_str}): "
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
            if rating is None:
                rating_str = 'n/a'
            else:
                count = (metrics.get('ratings') or {}).get('count', 0)
                rating_str = f'{rating}×{count}'
            lines.append(f"- **{name}** [{source}] v{version} (score: {score:.2f}, loads: {loads}, "
                         f"rating: {rating_str}): {desc}")

        return '\n'.join(lines)
