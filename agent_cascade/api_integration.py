"""Thin facade for the api_integration package (Phase 3b pure-move refactor).

This module used to contain all pool→UI integration logic. It has been split into
``agent_cascade.api_integration_pkg`` (cache / tokens / streaming / state_builder / runner).
Production importers are unchanged: they import from ``agent_cascade.api_integration`` and
receive the SAME objects now re-exported from the sub-package.

The ``_cache_mgr`` singleton is instantiated exactly once in
``api_integration_pkg.cache``; this facade (and every consumer) imports that same object.
"""

# ── cache.py: CacheManager + the module-level singleton ─────────────────────────
from agent_cascade.api_integration_pkg.cache import (  # noqa: F401
    CacheManager,
    _cache_mgr,
    _TOKEN_STATS_CACHE_MAXSIZE,
    _UI_CACHE_MAXSIZE,
    _STREAM_TOKEN_STATS_CACHE_MAXSIZE,
    _clear_performance_caches,
    _store_ui_cache,
)

# ── tokens.py: max-tokens resolution helpers ────────────────────────────────────
from agent_cascade.api_integration_pkg.tokens import (  # noqa: F401
    _resolve_max_tokens,
    _streaming_content_length,
    _get_max_tokens_for_instance,
)

# ── streaming.py: stream-update broadcasting ────────────────────────────────────
from agent_cascade.api_integration_pkg.streaming import (  # noqa: F401
    _put_stream_update,
    broadcast_stream_update,
    _calc_stream_token_stats,
)

# ── state_builder.py: pool → UI state serialization ─────────────────────────────
from agent_cascade.api_integration_pkg.state_builder import (  # noqa: F401
    _serialize_loop_settings,
    _get_instance_messages,
    _calc_token_stats,
    _serialize_all_instances,
    _get_session_name,
    _get_current_model,
    _safe_get_telemetry,
    _safe_get_api_router_state,
    _get_default_workspace,
    _build_active_stack,
    _get_msg_content,
    _get_msg_reasoning,
    _serialize_instances_incremental,
    _add_pool_runtime_settings,
    build_state_from_pool,
    build_stream_update_from_pool,
    _find_user_message_insertion_point,
    serialize_message,
    _check_is_waiting,
    _serialize_instance,
    _get_approvals,
    _build_agents_list,
    _apply_ui_config,
    get_agent_state_from_pool,
)

# ── runner.py: agent execution entry points (top of DAG) ────────────────────────
from agent_cascade.api_integration_pkg.runner import (  # noqa: F401
    create_main_agent_instance,
    run_agent_in_pool,
    run_agent_in_pool_with_recovery,
    execute_agent_turn,
)

__all__ = [
    # cache
    "CacheManager", "_cache_mgr", "_TOKEN_STATS_CACHE_MAXSIZE", "_UI_CACHE_MAXSIZE",
    "_STREAM_TOKEN_STATS_CACHE_MAXSIZE", "_clear_performance_caches", "_store_ui_cache",
    # tokens
    "_resolve_max_tokens", "_streaming_content_length", "_get_max_tokens_for_instance",
    # streaming
    "_put_stream_update", "broadcast_stream_update", "_calc_stream_token_stats",
    # state_builder
    "_serialize_loop_settings", "_get_instance_messages", "_calc_token_stats",
    "_serialize_all_instances", "_get_session_name", "_get_current_model",
    "_safe_get_telemetry", "_safe_get_api_router_state", "_get_default_workspace",
    "_build_active_stack", "_get_msg_content", "_get_msg_reasoning",
    "_serialize_instances_incremental", "_add_pool_runtime_settings",
    "build_state_from_pool", "build_stream_update_from_pool",
    "_find_user_message_insertion_point", "serialize_message", "_check_is_waiting",
    "_serialize_instance", "_get_approvals", "_build_agents_list", "_apply_ui_config",
    "get_agent_state_from_pool",
    # runner
    "create_main_agent_instance", "run_agent_in_pool",
    "run_agent_in_pool_with_recovery", "execute_agent_turn",
]
