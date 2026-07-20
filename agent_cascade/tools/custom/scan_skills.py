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
    description = (
        'Scan registered skills and return matching skills with relevance scores. '
        'Use this to discover which skills are available before calling call_agent with load_skill. '
        'Returns skill names, descriptions, and match scores for the given query.'
    )
    parameters = {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Search query or task description to match against available skills. Leave empty to list all registered skills.',
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
            return "No skills system available. Skills may not have been initialized."

        # Trigger a fresh discovery (cache-respecting) so new skills appear
        skill_manager._ensure_discovered()

        all_skills = skill_manager.get_all_metadata()
        if not all_skills:
            return "No skills are currently registered in the system."

        # Filter disabled skills when all=False
        if not show_all:
            disabled = getattr(skill_manager, '_disabled_names', set())
            all_skills = [s for s in all_skills if s['name'] not in disabled]
            if not all_skills:
                return "No skills are currently registered in the system."

        # If no query, just list everything
        if not query.strip():
            lines = ["## Available Skills"]
            for skill in all_skills:
                source = skill.get('source', 'system')
                lines.append(f"- **{skill['name']}** [{source}]: {skill.get('description', 'No description')}")
            return '\n'.join(lines)

        # Use public API to score skills against the query
        matches = skill_manager.match_skills(query)
        if not matches:
            return (
                f"No skills matched the query '{query}'.\n\n"
                "Available skills:\n" +
                "\n".join(f"- **{s['name']}** [{s.get('source', 'system')}]: {s.get('description', '')}" for s in all_skills)
            )

        # Build response with scores
        lines = [f"## Skills Matching Query: '{query}'"]
        for name, score in matches:
            meta = skill_manager.get_skill_metadata(name)
            desc = meta.get('description', 'No description') if meta else 'Unknown'
            source = meta.get('source', 'system') if meta else 'unknown'
            lines.append(f"- **{name}** [{source}] (score: {score:.2f}): {desc}")

        return '\n'.join(lines)