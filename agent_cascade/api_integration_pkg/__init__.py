"""api_integration package — pure-move split of api_integration.py (Phase 3b).

Dependency DAG (bottom-up): cache → tokens → streaming → state_builder → runner.
``cache`` is self-contained; ``streaming`` and ``state_builder`` both import the SAME
``_cache_mgr`` singleton from ``cache``. ``runner`` sits at the top of the DAG.
"""

from agent_cascade.api_integration_pkg import cache
from agent_cascade.api_integration_pkg import tokens
from agent_cascade.api_integration_pkg import streaming
from agent_cascade.api_integration_pkg import state_builder
from agent_cascade.api_integration_pkg import runner

__all__ = [
    "CacheManager", "_cache_mgr", "_clear_performance_caches",
    "broadcast_stream_update", "_put_stream_update", "_calc_stream_token_stats",
    "build_state_from_pool", "build_stream_update_from_pool", "get_agent_state_from_pool",
    "create_main_agent_instance", "run_agent_in_pool", "run_agent_in_pool_with_recovery",
    "execute_agent_turn", "_resolve_max_tokens", "serialize_message",
]
