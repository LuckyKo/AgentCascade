# ============================================================================
# AgentCascade — Centralized disabled_tools Resolution Module
# ============================================================================
# This is the SINGLE source of truth for determining which tools are disabled
# for any given agent instance. All code paths should use this module instead
# of inline disabled_tools lookup logic.
#
# Design principles:
# - One canonical format internally (set[str])
# - Validates tool names against known registry
# - Defense-in-depth for Security/Compressor agents is built in
# ============================================================================

from __future__ import annotations

from typing import Optional, Union, Dict, List, Set
import logging

logger = logging.getLogger(__name__)

# Import from constants — DO NOT duplicate these defaults here.
# They are the authoritative source and enforced by this module automatically.
from agent_cascade.constants import (
    DEFAULT_SECURITY_DISABLED_TOOLS,
    DEFAULT_COMPRESSOR_DISABLED_TOOLS,
)


def normalize_disabled_tools(raw: Optional[Union[Dict, List, Set, tuple]]) -> Set[str]:
    """Normalize any disabled_tools value to a set of tool names.

    Handles all formats the UI or config might produce:
      - None                → empty set
      - set / frozenset     → converted to mutable set
      - list / tuple        → converted to set
      - dict                → flattened values into a single set (see below)

    For dict format the *values* are treated as per-agent tool lists and all
    values are merged together.  This is useful when you want the union of
    every agent's disabled tools without caring about which agent they belong
    to.  For targeted lookups prefer ``resolve_disabled_tools_for_agent()``.

    Args:
        raw: The disabled_tools value from any source (config, UI, override).

    Returns:
        A mutable set of tool-name strings.
    """
    if raw is None:
        return set()
    if isinstance(raw, (set, frozenset)):
        return set(raw)
    if isinstance(raw, (list, tuple)):
        return set(raw)
    if isinstance(raw, dict):
        # Flatten all per-agent values into one set
        result: Set[str] = set()
        for key, tools in raw.items():
            if not isinstance(tools, (dict, list, tuple, set, frozenset)):
                logger.warning("disabled_tools value for key %r is type %r — ignoring", key, type(tools).__name__)
            result |= normalize_disabled_tools(tools)
        return result
    if raw is not None:
        logger.warning("Unrecognized disabled_tools type %r — ignoring", type(raw).__name__)
    return set()


def resolve_disabled_tools_for_agent(
    instance_override: Optional[Dict] = None,
    template_cfg: Optional[Dict] = None,
    agent_name: str = "",
    agent_type: str = "",
) -> Set[str]:
    """Resolve the complete set of disabled tools for a single agent.

    Resolution order (layers are accumulated top-down):
      1. Instance override  — ``instance._generate_cfg_override['disabled_tools']``
         Always merged into the result set.
      2. Template config   — ``template.llm.generate_cfg['disabled_tools']``
         Always merged with Layer 1 (union of both sources).
      3. Agent-class defaults — Security / Compressor defense-in-depth
         **Always applied** regardless of Layers 1–2.

    For dict-format disabled_tools the function looks up by:
      - ``agent_name`` (exact match, e.g. ``"Coder"``)
      - slugified name  (e.g. ``"coder"`` or ``"main_agent"``)
      - ``agent_type``  (e.g. ``"security"``)

    Args:
        instance_override: The instance ``_generate_cfg_override`` dict, or None.
        template_cfg:     The template ``llm.generate_cfg`` dict, or None.
        agent_name:       Display name of the agent (e.g. ``"Coder"``).
        agent_type:       Agent type string (e.g. ``"coder"``, ``"Security"``).

    Returns:
        A set of tool names that should be disabled for this agent.
    """
    disabled: Set[str] = set()

    # ── Helper to extract per-agent tools from a dict or flat list ──────────
    def _extract(dt, name: str, atype: str) -> Set[str]:
        if isinstance(dt, dict):
            s: Set[str] = normalize_disabled_tools(dt.get(name, []))
            slug = name.lower().replace(' ', '_')
            s |= normalize_disabled_tools(dt.get(slug, []))
            s |= normalize_disabled_tools(dt.get(atype, []))
            # Also try lowercase type — UI often sends {"coder": [...]} but atype may be "Coder"
            if atype:
                s |= normalize_disabled_tools(dt.get(atype.lower(), []))
            return s
        else:
            return normalize_disabled_tools(dt)

    # ── Layer 1: Instance override (highest precedence) ─────────────────────
    if instance_override and 'disabled_tools' in instance_override:
        disabled |= _extract(instance_override['disabled_tools'], agent_name, agent_type)

    # ── Layer 2: Template config (always merged with instance override) ─────
    if template_cfg and 'disabled_tools' in template_cfg:
        disabled |= _extract(template_cfg['disabled_tools'], agent_name, agent_type)

    # ── Layer 3: Agent-class defaults (defense-in-depth, ALWAYS applied) ────
    atype_lower = agent_type.lower() if agent_type else ''
    if atype_lower == 'security':
        disabled |= DEFAULT_SECURITY_DISABLED_TOOLS
    elif atype_lower == 'compressor':
        disabled |= DEFAULT_COMPRESSOR_DISABLED_TOOLS

    return disabled


def validate_tool_names(
    tool_names: Set[str], known_tools: Optional[Set[str]] = None,
) -> Set[str]:
    """Validate tool names against the registry. Warns on unknown names.

    Args:
        tool_names:  Set of tool-name strings to validate (None returns empty set).
        known_tools: Optional set of known tool names (if omitted validation
                     is skipped — no warning emitted).

    Returns:
        A new set of validated tool names (never the same object as input,
        preventing callers from mutating the original). Returns ``set()`` for
        empty or None input.
    """
    if not tool_names:
        return set()
    if known_tools is None:
        return set(tool_names)

    unknown = tool_names - known_tools
    if unknown:
        # Downgraded from WARNING to DEBUG: it's expected that not all tools
        # are available to every agent type, so listing disabled tools that
        # aren't in the registry is normal (e.g. python_compiler for coders).
        logger.debug(
            "Unknown tool names in disabled_tools: %s. "
            "These will be silently ignored.",
            sorted(unknown),
        )

    return set(tool_names)  # Always return a copy to prevent mutation of the input set


def merge_disabled_tools(parent: Set[str], child: Set[str]) -> Set[str]:
    """Merge parent and child disabled_tools sets (union).

    When an agent calls a sub-agent the caller's disabled tools are propagated.
    This is a simple union — if *either* side disables a tool it stays disabled.

    Args:
        parent: Disabled tools from the calling agent.
        child:  Disabled tools for the target agent.

    Returns:
        Merged set of disabled tool names.
    """
    return parent | child