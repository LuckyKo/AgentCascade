"""
Load Skill Tool — Allows agents to inject skill instructions at runtime.

Loads registered skills and appends their full instructions as USER messages
to the current agent instance's conversation, enabling self-augmentation during
task execution. Uses the same skill infrastructure as init-time auto-loading.
"""

import json
import logging
import re

from agent_cascade.llm.schema import Message, USER
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.tools.utils import parse_tool_params

logger = logging.getLogger(__name__)


@register_tool('load_skill', allow_overwrite=True)
class LoadSkill(BaseTool):
    """Tool to load skill instructions into the current agent's context at runtime."""

    name = 'load_skill'
    description = (
        'Load registered skill instructions into your current context at runtime. '
        'Use this when you need specialized expertise for your task. '
        'Takes one or more skill names and injects their full instructions as guidelines.'
    )
    parameters = {
        'type': 'object',
        'properties': {
            'skill_names': {
                'oneOf': [
                    {
                        'type': 'string',
                        'description': 'A single skill name to load.',
                    },
                    {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'List of skill names to load (e.g., ["code-review", "docker-best-practices"]).',
                    }
                ],
                'description': 'Skill name(s) to load into your context.',
            },
        },
        'required': ['skill_names'],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        """Execute load_skill.

        Args:
            params: JSON string or dict with 'skill_names' (string or list).
            kwargs: Additional context (agent_instance_name, agent_obj for resolving target).

        Returns:
            Summary of which skills loaded successfully and which failed.
        """
        parsed = parse_tool_params(params)
        skill_names_raw = parsed.get('skill_names', [])

        # Normalize to list — handle JSON-encoded arrays passed as strings by LLMs
        if isinstance(skill_names_raw, str):
            # Try parsing as JSON array first (LLM may pass '["skill-a", "skill-b"]' as string)
            try:
                decoded = json.loads(skill_names_raw)
                if isinstance(decoded, list):
                    skill_names = decoded
                else:
                    skill_names = [skill_names_raw]
            except (json.JSONDecodeError, TypeError):
                skill_names = [skill_names_raw]
        elif isinstance(skill_names_raw, list):
            skill_names = skill_names_raw
        else:
            return "Invalid skill_names parameter. Provide a string or list of strings."

        if not skill_names:
            return "No skill names provided."

        # Get SkillManager from pool
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return "No skills system available. Skills may not have been initialized."

        # DEBUG logging
        logger.warning(f"[SKILLS-DEBUG] load_skill: agent_pool id={id(self.agent_pool)}, skill_manager id={id(skill_manager)}")
        logger.warning(f"[SKILLS-DEBUG] load_skill: registry keys={list(skill_manager._skills_registry.keys())}")
        logger.warning(f"[SKILLS-DEBUG] load_skill: skill_paths={skill_manager._skill_paths}, cache_timestamp={skill_manager._cache_timestamp}")

        # Trigger discovery so new/recent skills are visible (matches scan_skills behavior)
        skill_manager._ensure_discovered()

        logger.warning(f"[SKILLS-DEBUG] load_skill after _ensure_discovered: registry keys={list(skill_manager._skills_registry.keys())}")

        # Resolve the target agent instance to append messages to.
        # Priority: explicit kwarg > agent_obj.instance_name > self.agent_name > default.
        agent_obj = kwargs.get('agent_obj')
        agent_name = kwargs.get('agent_instance_name')
        if not agent_name and agent_obj is not None:
            agent_name = getattr(agent_obj, 'instance_name', None)
        if not agent_name:
            agent_name = self.agent_name or 'Orchestrator'

        inst = None
        if self.agent_pool:
            try:
                inst = self.agent_pool.get_instance(agent_name)
            except Exception as e:
                logger.debug(f"Failed to get instance '{agent_name}': {e}")

        if inst is None:
            return f"Could not find agent instance '{agent_name}' to load skills into."

        # Validate and normalize skill names
        VALID_SKILL_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')

        def _sanitize_for_text(raw: str) -> str:
            """Strip newlines and control chars to prevent message injection."""
            return ''.join(c for c in raw if c.isprintable() and c not in '\x00\x1b')

        # Load each skill and inject as user message
        loaded = []
        failed = []

        for raw_name in skill_names:
            # Validate skill name (Fix #3)
            name = raw_name.strip() if isinstance(raw_name, str) else ''
            if not name:
                logger.warning("[SKILLS] Runtime load: empty skill name skipped")
                failed.append(raw_name)
                continue
            if not VALID_SKILL_NAME_RE.match(name):
                logger.warning("[SKILLS] Runtime load: invalid skill name '%s' (must be alphanumeric with hyphens/underscores)", raw_name)
                failed.append(raw_name)
                continue

            # Load instructions with error handling (Fix #2)
            try:
                body = skill_manager.load_full_instructions(name)
            except Exception as e:
                logger.warning("[SKILLS] Runtime load: error loading skill '%s': %s", name, e)
                failed.append(name)
                continue

            if body is None:
                logger.warning("[SKILLS] Runtime load: skill '%s' not found", name)  # Fix #6
                failed.append(name)
                continue

            # Sanitize skill name in message content (Fix #5)
            safe_name = _sanitize_for_text(name)

            # Format the injected message
            content = f"## Loaded Skill: {safe_name}\n\n{body}\n\nApply the above guidelines to your current task."

            # Check instance is still valid before appending (Fix #4)
            if getattr(inst, 'is_terminated', False):
                logger.warning("[SKILLS] Runtime load: instance '%s' terminated during skill loading", agent_name)
                break

            # Append as USER message
            msg = Message(role=USER, content=content)
            inst.append_message(msg)
            loaded.append(name)

            logger.info("[SKILLS] Runtime load: injected skill '%s' into instance '%s'", name, agent_name)

        # Build summary
        lines = []
        if loaded:
            lines.append(f"Successfully loaded {len(loaded)} skill(s): {', '.join(loaded)}")
        if failed:
            lines.append(f"Failed to load {len(failed)} skill(s) (not found): {', '.join(failed)}")

        return '\n'.join(lines)