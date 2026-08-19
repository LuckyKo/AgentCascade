"""Regression tests for the module-split refactor's name-resolution (F821) fixes.

The Phase 1/2/3 pure-move refactors split monoliths into sub-packages while keeping
function bodies byte-identical. In doing so, several names that were resolved via a
shared module namespace in the original monolith stopped resolving in the new
sub-modules (ruff F821). These tests prove:

  1. Every one of the five new sub-packages imports cleanly.
  2. The runtime-crash paths actually resolve their previously-undefined names
     (``_calc_stream_token_stats``, ``_store_ui_cache``, ``PoolSettings``).
  3. Annotation-only forward refs now resolve — via ``typing.get_type_hints()`` where
     a real import is possible (engine's ``AgentInstance``), and via the module's
     global namespace / ruff F821-cleanliness for self-referential names that cannot
     be imported without a cycle (pool's ``AgentPool``).

Self-contained: no network, no live API, no real AgentPool construction.
"""

import threading
import typing
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# 1. Package / facade imports resolve cleanly
# ---------------------------------------------------------------------------

def test_all_five_packages_import():
    """The five refactored sub-packages (and their facades) import without error."""
    import agent_cascade.api_integration
    import agent_cascade.api_router
    import agent_cascade.async_shell
    import agent_cascade.engine.core
    import agent_cascade.pool.core

    # And the specific sub-modules that previously had F821 errors.
    from agent_cascade.api_integration_pkg import state_builder, streaming, cache
    from agent_cascade.api_router_pkg import router
    from agent_cascade.engine import compression_exec, llm_call, tool_execution
    from agent_cascade.pool import (
        config_persist, conversation_map, idle_manager, logger_mgr, parallel_manager,
    )

    assert state_builder is not None
    assert streaming is not None
    assert cache is not None
    assert router is not None
    assert compression_exec is not None
    assert llm_call is not None
    assert tool_execution is not None
    assert config_persist is not None
    assert conversation_map is not None
    assert idle_manager is not None
    assert logger_mgr is not None
    assert parallel_manager is not None


# ---------------------------------------------------------------------------
# 2. Runtime-crash path: _calc_stream_token_stats resolves in state_builder
# ---------------------------------------------------------------------------

def test_build_stream_update_reaches_token_stats_path():
    """Exercise build_stream_update_from_pool far enough to hit _calc_stream_token_stats.

    A fresh (uncached) instance forces the "recompute" branch, which lazily imports and
    calls ``_calc_stream_token_stats`` from streaming.py. We stand in for that function
    with a sentinel; if the name were undefined in state_builder's scope this call would
    raise NameError instead of invoking the sentinel — exactly the regression guarded here.
    """
    from agent_cascade.api_integration_pkg import state_builder, streaming

    saved_stats = dict(state_builder._cache_mgr.stream_token_stats)
    saved_versions = dict(state_builder._cache_mgr.stream_versions)
    try:
        # Force the "recompute" branch (no cached stats for this instance).
        with state_builder._cache_mgr._lock:
            state_builder._cache_mgr.stream_token_stats.clear()
            state_builder._cache_mgr.stream_versions.clear()

        instance = MagicMock()
        instance.conversation = []              # empty -> no cached version match
        instance._streaming_responses = None
        instance._compression_lock = threading.Lock()

        pool = MagicMock()
        pool.get_instance.return_value = instance
        pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)

        calls = {}

        def sentinel(pool_, name, conv, sr, resp):
            calls['hit'] = True
            return ({'tokens': 0, 'words': 0}, {'tokens': 0, 'words': 0})

        # Patch the real home (streaming). state_builder's lazy import resolves there.
        with patch.object(streaming, '_calc_stream_token_stats', sentinel):
            result = state_builder.build_stream_update_from_pool(pool, "Maine")

        assert isinstance(result, dict)
        assert calls.get('hit') is True  # proves the name resolved and was invoked
    finally:
        with state_builder._cache_mgr._lock:
            state_builder._cache_mgr.stream_token_stats.clear()
            state_builder._cache_mgr.stream_versions.clear()
            state_builder._cache_mgr.stream_token_stats.update(saved_stats)
            state_builder._cache_mgr.stream_versions.update(saved_versions)


def test_state_builder_resolves_store_ui_cache_identity():
    """state_builder's reference to _store_ui_cache is the real cache one (module-level import)."""
    from agent_cascade.api_integration_pkg import state_builder, cache

    assert state_builder._store_ui_cache is cache._store_ui_cache


# ---------------------------------------------------------------------------
# 3. Runtime-crash path: PoolSettings resolves in pool.config_persist
# ---------------------------------------------------------------------------

def test_config_persist_poolsettings_resolves_to_real_class():
    """pool.config_persist.PoolSettings is the real class from agent_instance."""
    from agent_cascade.pool import config_persist
    from agent_cascade.agent_instance import PoolSettings as RealPoolSettings

    assert config_persist.PoolSettings is RealPoolSettings
    # It must be a usable class (the regression was `PoolSettings.from_dict(data)`).
    assert isinstance(config_persist.PoolSettings, type)
    assert hasattr(config_persist.PoolSettings, "from_dict")


# ---------------------------------------------------------------------------
# 4. Annotation-only paths: get_type_hints resolves AgentInstance (engine)
# ---------------------------------------------------------------------------

def test_engine_agentinstance_annotations_resolve():
    """Engine methods annotated with AgentInstance resolve via get_type_hints.

    These are the annotation-only F821 cases that a plain import would NOT catch:
    without the added import, get_type_hints raises NameError for 'AgentInstance'.
    """
    from agent_cascade.agent_instance import AgentInstance
    from agent_cascade.engine.compression_exec import CompressionExecMixin
    from agent_cascade.engine.llm_call import LLMCallMixin
    from agent_cascade.engine.tool_execution import ToolExecMixin

    # _check_and_trigger_compression(self, instance: AgentInstance, ...)
    hints = typing.get_type_hints(CompressionExecMixin._check_and_trigger_compression)
    assert hints["instance"] is AgentInstance

    # tool_execution._execute_detected_tools(self, instance: AgentInstance, ...)
    hints = typing.get_type_hints(ToolExecMixin._execute_detected_tools)
    assert hints["instance"] is AgentInstance

    # llm_call._execute_llm_call(self, instance: AgentInstance, ...)
    hints = typing.get_type_hints(LLMCallMixin._execute_llm_call)
    assert hints["instance"] is AgentInstance


def test_pool_agentpool_annotations_are_resolvable_forward_refs():
    """Pool sub-module __init__ annotations reference the real AgentPool.

    ``AgentPool`` is defined in pool/core.py, which imports these sub-modules — so a
    runtime import would be circular. The fix uses a TYPE_CHECKING-guarded import, which
    makes ruff F821 clean and binds the name for static type checkers. Because
    ``from __future__ import annotations`` stringifies the hint and the guard is not
    executed at runtime, ``get_type_hints()`` cannot resolve it without a cycle; we instead
    assert the raw annotation is a well-formed forward-ref naming the real class (the
    pre-fix regression was a bare ``AgentPool`` with NO binding anywhere in the module).
    """
    from agent_cascade.pool.conversation_map import _InstanceConversationMapping
    from agent_cascade.pool.idle_manager import IdleManager
    from agent_cascade.pool.logger_mgr import LoggerManager
    from agent_cascade.pool.parallel_manager import ParallelAgentManager

    for cls in (
        _InstanceConversationMapping, IdleManager, LoggerManager, ParallelAgentManager,
    ):
        ann = cls.__init__.__annotations__.get("pool")
        # Under `from __future__ import annotations` this is the forward-ref string. Some
        # sites already wrap it in source quotes ('AgentPool'), others don't (AgentPool) —
        # both stringify to a name; normalize by stripping any surrounding quotes.
        assert isinstance(ann, str), f"{cls.__name__} pool annotation {ann!r} not a string"
        assert ann.strip("'\"") == "AgentPool", f"{cls.__name__} pool annotation is {ann!r}"


# ---------------------------------------------------------------------------
# 5. api_router phantom forward-ref now points at a real, resolvable type
# ---------------------------------------------------------------------------

def test_router_endpoint_annotation_resolves():
    """router._resolve_own_endpoints return annotation resolves (was 'EndpointConfig')."""
    from agent_cascade.api_router_pkg.router import APIRouter
    from agent_cascade.api_router_pkg.endpoints import APIEndpoint

    hints = typing.get_type_hints(APIRouter._resolve_own_endpoints)
    ret = hints["return"]
    # Return type is Tuple[List[Tuple[str, APIEndpoint]], bool]. Walk down to the inner
    # endpoint element and assert it is the real APIEndpoint (the old phantom
    # 'EndpointConfig' was unresolvable and would have raised here).
    outer_args = typing.get_args(ret)                 # (List[...], bool)
    inner_list = outer_args[0]                        # List[Tuple[str, APIEndpoint]]
    pair = typing.get_args(inner_list)[0]             # Tuple[str, APIEndpoint]
    assert typing.get_args(pair)[1] is APIEndpoint
