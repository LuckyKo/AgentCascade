"""
Load Skill Tool — Allows agents to inject skill instructions at runtime.

Loads registered skills and appends their full instructions as USER messages
to the current agent instance's conversation, enabling self-augmentation during
task execution. Uses the same skill infrastructure as init-time auto-loading.
"""

import json
import logging
import re

from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.tools.utils import parse_tool_params

logger = logging.getLogger(__name__)


@register_tool('load_skill', allow_overwrite=True)
class LoadSkill(BaseTool):
    """Tool to load skill instructions into the current agent's context at runtime."""
    name = 'load_skill'
    description = TOOL_METADATA['load_skill']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'skill_names': TOOL_METADATA['load_skill']['parameters']['skill_names'],
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
            return 'Invalid skill_names parameter. Provide a string or list of strings.'

        if not skill_names:
            return 'No skill names provided.'

        # Get SkillManager from pool
        if self.agent_pool is None:
            return 'No agent pool available.'
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return 'No skills system available. Skills may not have been initialized.'

        # Trigger discovery so new/recent skills are visible (matches scan_skills behavior)
        skill_manager._ensure_discovered()

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

        # Load each skill via the shared AgentPool.inject_skill (single source of truth,
        # BUG_0263). Name validation stays above; injection/dedup/record are the helper's job.
        loaded = []
        failed = []
        already_loaded = []
        re_enabled = []  # NEW: previously-disabled skills re-activated by this load

        # The dedup guard now lives inside AgentPool.inject_skill (BUG_0263): it seeds from
        # inst._loaded_skill_names and updates that list after each successful enqueue, so
        # skills already active (init-time AUTO-matched / prior runtime loads) and names
        # repeated within this call all dedup there. Name validation stays here so invalid
        # inputs never reach the helper.
        for raw_name in skill_names:
            # Validate skill name (Fix #3)
            name = raw_name.strip() if isinstance(raw_name, str) else ''
            if not name:
                logger.warning('[SKILLS] Runtime load: empty skill name skipped')
                failed.append(raw_name)
                continue
            if not VALID_SKILL_NAME_RE.match(name):
                logger.warning(
                    "[SKILLS] Runtime load: invalid skill name '%s' (must be alphanumeric with hyphens/underscores)",
                    raw_name)
                failed.append(raw_name)
                continue

            # Single source of truth (shared with the memory-hint auto-loader, BUG_0263):
            # inject_skill performs load_full_instructions -> re-enable -> dedup -> enqueue
            # -> record _loaded_skill_names -> telemetry. It returns (status, was_re_enabled);
            # re-enable is reported independently of the loaded/already_loaded outcome, so a
            # re-enabled-but-already-loaded skill still shows in the Re-enabled summary line.
            status, was_re_enabled = self.agent_pool.inject_skill(agent_name, name)
            if was_re_enabled:
                re_enabled.append(name)
            if status == 'loaded':
                loaded.append(name)
            elif status == 'already_loaded':
                already_loaded.append(name)
            else:  # 'failed' — inject_skill already logged the specific reason
                failed.append(name)

        # Build summary
        lines = []
        if loaded:
            lines.append(f"Successfully loaded {len(loaded)} skill(s): {', '.join(loaded)}")
        if already_loaded:
            # Dedupe (a skill may be listed multiple times in one call) while keeping order.
            lines.append(f"Already loaded (skipped): {', '.join(dict.fromkeys(already_loaded))}")
        if re_enabled:
            # A re-enabled skill is also reported in `loaded`/`already_loaded`; this line is the
            # distinct signal that its disabled state was corrected as a side effect of loading.
            lines.append(f"Re-enabled (was disabled): {', '.join(dict.fromkeys(re_enabled))}")
        if failed:
            lines.append(f"Failed to load {len(failed)} skill(s) (not found): {', '.join(failed)}")

        return '\n'.join(lines)
