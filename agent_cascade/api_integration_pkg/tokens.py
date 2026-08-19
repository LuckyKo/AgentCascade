"""Max-tokens resolution helpers (moved verbatim from api_integration.py).

Phase 3b pure-move refactor.
"""

from agent_cascade.log import logger
from agent_cascade.agent_instance import AgentInstance
from agent_cascade.agent_pool import AgentPool
from agent_cascade.llm.schema import CONTENT, REASONING_CONTENT

def _resolve_max_tokens(pool, instance=None):
    """Resolve effective max_input_tokens using unified priority order.

    Shared helper to eliminate code duplication across execution_engine,
    api_integration, and api_server. Called from all 5 resolution sites.

    Resolution Order (highest → lowest priority):
      1. Per-instance override (_generate_cfg_override) — absolute priority set by supervisor
      2. API Router effective limit — authoritative live source, always checked before caches
      3. Template static config (from settings, via llm.cfg) — original user-set value
      4. Instance's allocated max_input_tokens — fallback from last LLM call (can be stale)
      5. Runtime-detected LLM limit (OAI detection in shared generate_cfg) — last resort
      6. User-configured DEFAULT_MAX_INPUT_TOKENS from settings

    Why this order matters:
      - The API Router is the authoritative source for max_tokens because it reflects
        the CURRENT endpoint configuration. Cached values can become stale when endpoints
        are reconfigured (e.g., agent switched from a 128K model to a 32K model).
      - Per-instance overrides (step 1) short-circuit everything because they represent
        an explicit supervisor decision that should never be overridden by auto-detection.
      - Template static config (step 3) is more reliable than per-instance cache (step 4)
        because it was set at initialization and doesn't change based on which endpoint
        happened to serve the last request.
      - Runtime-detected LLM limit from shared generate_cfg (step 5) is checked last
        because it's a TEMPLATE-level mutable dict shared across ALL instances of that
        agent type — one instance's OAI detection pollutes every other instance.

    Args:
        pool: The AgentPool (or None for safe fallback).
        instance: The agent instance (or None for orchestrator-only lookups).

    Returns:
        Maximum input token count as integer.
    """
    # Import DEFAULT_MAX_INPUT_TOKENS locally to avoid circular import issues
    try:
        from agent_cascade.settings import DEFAULT_MAX_INPUT_TOKENS
    except ImportError:
        DEFAULT_MAX_INPUT_TOKENS = 58000

    # ── Step 1: Per-instance override (from UI config via _apply_ui_config) ──
    # Absolute priority — supervisor-set overrides should never be second-guessed.
    # Note: lifecycle_manager._propagate_settings() does NOT set max_input_tokens here;
    # it is resolved dynamically at call time via the API Router (Step 2 below).
    if instance and hasattr(instance, '_generate_cfg_override') and instance._generate_cfg_override:
        inst_override = instance._generate_cfg_override.get('max_input_tokens')
        if inst_override:
            return int(inst_override)

    # ── Step 2: API Router (per-endpoint priority-based selection) — AUTHORITATIVE SOURCE ──
    # Always checked before any cached value to avoid stale-state bugs when endpoints change
    router_limit = 0
    if pool and hasattr(pool, 'api_router') and pool.api_router:
        try:
            agent_class = instance.agent_class.lower() if instance else 'orchestrator'
            router_limit = pool.api_router.get_effective_max_tokens(agent_class)
        except Exception as e:
            logger.debug(f"API Router lookup failed for {agent_class}: {e}")

    # ── Gather fallback sources (template-level lookups, done once) ──
    static_llm_limit = 0
    allocated = 0
    runtime_max = 0
    try:
        if instance and hasattr(pool, 'templates'):
            template = pool.get_template(instance.agent_class)
            if template and hasattr(template, 'llm'):
                llm = template.llm

                # Step 3: Template static config (from settings, via llm.cfg dict)
                cfg = getattr(llm, 'cfg', {})
                agent_max = (
                    (cfg.get('generate_cfg') or {}).get('max_input_tokens') or
                    cfg.get('max_input_tokens')
                )
                if agent_max:
                    static_llm_limit = int(agent_max)

                # Step 5: Runtime-detected LLM limit (OAI detection writes to shared generate_cfg)
                runtime_max = getattr(llm, 'generate_cfg', {}).get('max_input_tokens', 0)
    except Exception as e:
        logger.debug(f"Template fallback lookup failed for {instance.agent_class if instance else '?'}: {e}")

    # Instance-level allocated max from last LLM call (Feature 006)
    if instance and hasattr(instance, '_allocated_max_input_tokens'):
        allocated = instance._allocated_max_input_tokens

    # ── Priority Resolution — router always wins over cached values ──
    if router_limit > 0:
        return router_limit       # Live API Router limit (authoritative)
    if static_llm_limit > 0:
        return static_llm_limit   # Template's original config from settings
    if allocated > 0:
        return allocated          # Per-instance cache from last LLM call (can be stale)
    if runtime_max:
        return runtime_max        # Shared template generate_cfg (last resort, can be polluted)

    return DEFAULT_MAX_INPUT_TOKENS   # User-configured default from settings (final fallback)

def _streaming_content_length(messages: list) -> int:
    """Calculate total content length of streaming messages for streaming dedup cache.
    
    This helper extracts the pattern used in 3 places to calculate content length
    for cache invalidation during streaming updates. It handles both dict and 
    Message object types.
    
    Args:
        messages: List of message dicts or Message objects from _streaming_responses.
        
    Returns:
        Total character count across content, reasoning_content, and function_call fields.
    """
    if not messages:
        return 0
    
    total_length = 0
    for m in messages:
        # Handle dict, Message object, and unexpected list types
        if isinstance(m, dict):
            total_length += len(m.get(CONTENT, '') or '')
            total_length += len(m.get(REASONING_CONTENT, '') or '')
            total_length += len(str(m.get('function_call') or ''))
        elif isinstance(m, list):
            # Skip unexpected list objects (can occur from streaming/multimodal content)
            continue
        else:
            total_length += len(getattr(m, CONTENT, '') or '')
            total_length += len(getattr(m, REASONING_CONTENT, '') or '')
            total_length += len(str(getattr(m, 'function_call', None) or ''))
    
    return total_length

def _get_max_tokens_for_instance(pool: AgentPool, instance: AgentInstance) -> int:
    """Get the effective max_input_tokens for an agent instance.

    Thin wrapper around _resolve_max_tokens — kept for backward compatibility
    since it's called from build_state_from_pool and build_stream_update_from_pool.
    """
    return _resolve_max_tokens(pool, instance)
