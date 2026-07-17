"""
Scan Skills Tool — Read-only tool that queries SkillManager and returns matching skills.

Allows agents to discover available skills and their relevance scores for a given query.
This is the primary way orchestrators decide which skills to load via call_agent(load_skill=[...]).
"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA

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
        },
        'required': [],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    async def _ensure_discovered(self, skill_manager) -> None:
        """Trigger real-time discovery to pick up newly added skills."""
        # Find the .qwen/skills/ directory relative to project root
        _project_root = Path(__file__).resolve().parent.parent.parent.parent
        _skills_dir = _project_root / '.qwen' / 'skills'
        if not _skills_dir.exists():
            logger.debug("[SKILLS] Skills directory not found at %s", _skills_dir)
            return

        await skill_manager.discover([_skills_dir])

    async def call(self, params: str, **kwargs) -> str:
        """Execute the scan_skills tool.

        Args:
            params: JSON string or dict with 'query' field.
            kwargs: Additional context (agent_instance_name for logging).

        Returns:
            Formatted markdown list of matching skills.
        """
        # Parse params
        if isinstance(params, str):
            import json
            try:
                parsed = json.loads(params) if params.strip() else {}
            except json.JSONDecodeError:
                parsed = {}
        elif isinstance(params, dict):
            parsed = params
        else:
            parsed = {}

        query = parsed.get('query', '')

        # Get SkillManager from pool
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return "No skills system available. Skills may not have been initialized."

        # Trigger real-time discovery (picks up newly added skills without restart)
        await self._ensure_discovered(skill_manager)

        all_skills = skill_manager.get_all_metadata()
        if not all_skills:
            return "No skills are currently registered in the system."

        # If no query, just list everything
        if not query.strip():
            lines = ["## Available Skills"]
            for skill in all_skills:
                lines.append(f"- **{skill['name']}**: {skill.get('description', 'No description')}")
            return '\n'.join(lines)

        # Use public API to score skills against the query
        matches = skill_manager.match_skills(query)
        if not matches:
            return (
                f"No skills matched the query '{query}'.\n\n"
                "Available skills:\n" +
                "\n".join(f"- **{s['name']}**: {s.get('description', '')}" for s in all_skills)
            )

        # Build response with scores
        lines = [f"## Skills Matching Query: '{query}'"]
        for name, score in matches:
            meta = skill_manager.get_skill_metadata(name)
            desc = meta.get('description', 'No description') if meta else 'Unknown'
            lines.append(f"- **{name}** (score: {score:.2f}): {desc}")

        return '\n'.join(lines)