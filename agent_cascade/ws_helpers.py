"""WebSocket handler helper utilities."""

def _clear_caches_safely() -> None:
    """Clear performance caches with error suppression."""
    try:
        from agent_cascade.api_integration import _clear_performance_caches
        _clear_performance_caches()
    except Exception as e:
        from agent_cascade.log import logger
        logger.debug(f"Cache clearing failed (non-critical): {e}")


def _validate_disabled_tools(ui_cfg: dict) -> None:
    """Validate disabled_tools in a generate_cfg dict against the tool registry."""
    from agent_cascade.utils.disabled_tools import normalize_disabled_tools, validate_tool_names
    from agent_cascade.tools.base import TOOL_REGISTRY
    from agent_cascade.constants import RUNTIME_REGISTERED_TOOLS

    if 'disabled_tools' in ui_cfg and ui_cfg['disabled_tools']:
        dt = ui_cfg['disabled_tools']
        known = set(TOOL_REGISTRY.keys()) | RUNTIME_REGISTERED_TOOLS
        if isinstance(dt, dict):
            for tools in dt.values():
                validate_tool_names(normalize_disabled_tools(tools), known_tools=known)
        else:
            validate_tool_names(normalize_disabled_tools(dt), known_tools=known)
