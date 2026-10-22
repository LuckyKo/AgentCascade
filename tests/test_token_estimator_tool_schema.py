"""Regression tests for BUG_0136 — context usage estimators omitted tool schemas.

The API prompt contains both the message list and the serialized ``tools=`` payload.
``get_history_stats()`` can only see messages, so every user-facing context-usage
readout needs the separate schema term. The compression trigger already counted it
(``_count_history_tokens(functions=...)``), which made the UI bar look low and the
trigger look late.

Pins:
  1. ``estimate_instance_tool_schema_tokens`` counts the active schemas, caches by
     toolset identity, and fails soft to 0.
  2. ``_count_history_tokens`` caches message-only tokens: with/without-``functions``
     calls on the SAME instance cannot poison each other (the old cache key was just
     the message count).
  3. ``_serialize_instance`` (per-agent context bar) adds the schema term.
  4. Both root state builders (initial state + streaming tick) add the schema term.
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.llm.schema import Message, USER
from agent_cascade.utils.utils import estimate_functions_tokens, get_history_stats, get_message_stats


SCHEMA = {
    'name': 'grep',
    'description': 'Search a file for a pattern',
    'parameters': {
        'type': 'object',
        'properties': {'pattern': {'type': 'string'}},
        'required': ['pattern'],
    },
}


def _make_template(schemas=(SCHEMA,), name='T'):
    function_map = {s['name']: SimpleNamespace(function=s) for s in schemas}
    return SimpleNamespace(
        name=name,
        function_map=function_map,
        llm=SimpleNamespace(generate_cfg={}),
        agent_type='orchestrator',
    )


class _TemplatePool:
    def __init__(self, template):
        self._template = template

    def get_template(self, agent_class):
        return self._template


def _make_instance(name='Maine', agent_class='orchestrator'):
    return SimpleNamespace(
        instance_name=name,
        agent_class=agent_class,
        _generate_cfg_override=None,
        disabled_tools=set(),
    )


# ── 1. Shared helper ─────────────────────────────────────────────────────────

def test_schema_token_helper_counts_and_caches_by_toolset():
    from agent_cascade.engine.helpers import estimate_instance_tool_schema_tokens

    inst = _make_instance()
    pool = _TemplatePool(_make_template())
    expected = estimate_functions_tokens([SCHEMA])
    assert expected > 0

    assert estimate_instance_tool_schema_tokens(inst, pool) == expected
    assert inst._cached_tool_schema_tokens == expected
    first_key = inst._cached_tool_schema_key
    assert first_key is not None

    # A second call must hit the instance cache (no re-serialization/tokenization).
    with patch('agent_cascade.engine.helpers.estimate_functions_tokens',
               side_effect=AssertionError('schema tokens recomputed despite cache')):
        assert estimate_instance_tool_schema_tokens(inst, pool) == expected

    # Changing the active toolset changes the key -> recompute (empty schema list = 0).
    inst.disabled_tools = {SCHEMA['name']}
    # The centralized resolver reads the override first; use the explicit override path.
    inst._generate_cfg_override = {'disabled_tools': [SCHEMA['name']]}
    assert estimate_instance_tool_schema_tokens(inst, pool) == 0
    assert inst._cached_tool_schema_key != first_key


def test_schema_token_helper_is_fail_soft():
    from agent_cascade.engine.helpers import estimate_instance_tool_schema_tokens

    inst = _make_instance()
    assert estimate_instance_tool_schema_tokens(None, _TemplatePool(_make_template())) == 0
    assert estimate_instance_tool_schema_tokens(inst, None) == 0
    assert estimate_instance_tool_schema_tokens(inst, _TemplatePool(None)) == 0

    class _BoomPool:
        def get_template(self, agent_class):
            raise RuntimeError('boom')

    assert estimate_instance_tool_schema_tokens(inst, _BoomPool()) == 0


# ── 2. _count_history_tokens cache is message-only ───────────────────────────

def test_count_history_tokens_cache_not_poisoned_by_message_only_call():
    from agent_cascade.engine.core import ExecutionEngine

    engine = object.__new__(ExecutionEngine)
    messages = [Message(role=USER, content='hello world this is a test message')]
    inst = SimpleNamespace(
        _last_token_count_conversation_length=-1,
        _cached_token_count=0,
        _cached_conversation_tokens=0,
        _cached_tool_schema_tokens=0,
        _cached_tool_schema_key=None,
    )
    msg_only = get_message_stats(messages[0])['tokens']
    schema_only = estimate_functions_tokens([SCHEMA])
    assert schema_only > 0

    # Message-only call primes the cache...
    assert ExecutionEngine._count_history_tokens(engine, messages, inst, functions=None) == msg_only
    # ...then a schema-aware call must still add the schema term...
    assert ExecutionEngine._count_history_tokens(engine, messages, inst, functions=[SCHEMA]) == msg_only + schema_only
    # ...and a later message-only call must not inherit the schema term.
    assert ExecutionEngine._count_history_tokens(engine, messages, inst, functions=None) == msg_only
    assert ExecutionEngine._count_history_tokens(engine, messages, inst, functions=[SCHEMA]) == msg_only + schema_only
    # The message cache holds the message-only total, not the last full total.
    assert inst._cached_conversation_tokens == msg_only


# ── State-builder fixtures ───────────────────────────────────────────────────

def _reset_caches():
    from agent_cascade.api_integration_pkg import state_builder
    with state_builder._cache_mgr._lock:
        state_builder._cache_mgr.stream_token_stats.clear()
        state_builder._cache_mgr.stream_token_stats_versions.clear()
        state_builder._cache_mgr.stream_versions.clear()
        state_builder._cache_mgr.cached_instances.clear()
        state_builder._cache_mgr.token_stats.clear()


@pytest.fixture(autouse=True)
def _clean_caches():
    from agent_cascade.api_integration_pkg import state_builder
    cm = state_builder._cache_mgr
    with cm._lock:
        saved = (dict(cm.stream_token_stats), dict(cm.stream_token_stats_versions),
                 dict(cm.stream_versions), dict(cm.cached_instances), dict(cm.token_stats))
    _reset_caches()
    try:
        yield
    finally:
        with cm._lock:
            cm.stream_token_stats.clear()
            cm.stream_token_stats_versions.clear()
            cm.stream_versions.clear()
            cm.cached_instances.clear()
            cm.token_stats.clear()
            cm.stream_token_stats.update(saved[0])
            cm.stream_token_stats_versions.update(saved[1])
            cm.stream_versions.update(saved[2])
            cm.cached_instances.update(saved[3])
            cm.token_stats.update(saved[4])


def _make_state_pool(name='Maine', conversation=None):
    from agent_cascade.agent_instance import AgentState

    if conversation is None:
        conversation = [Message(role=USER, content='hello world this is a test message')]

    inst = MagicMock()
    inst.instance_name = name
    inst.agent_class = 'orchestrator'
    inst.conversation = conversation
    inst._streaming_responses = []
    inst._compression_lock = threading.Lock()
    inst._state_lock = threading.Lock()
    inst.state = AgentState.IDLE
    inst.compression_summary = ''
    inst.parent_instance = None
    inst._last_endpoint_config = None

    pool = MagicMock()
    pool.get_instance.return_value = inst
    pool.instances = {name: inst}
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.is_instance_halted.return_value = False
    pool.is_paused.return_value = False
    pool.stopped = False
    pool.active_stack = [name]
    pool.templates = {}
    pool.llm_cfg = {}
    pool.api_router = None

    om = MagicMock()
    om.list_pending_approvals.return_value = []
    om.extra_work_folders_ro = []
    om.extra_work_folders_rw = []
    om.base_dir = '/tmp/fake-workspace'
    pool.operation_manager = om

    return pool, inst


# ── 3. Per-agent context bar total ──────────────────────────────────────────

def test_serialize_instance_total_includes_schema_tokens():
    from agent_cascade.api_integration_pkg import state_builder

    pool, inst = _make_state_pool(name='schema-per-agent')
    expected_msg = get_history_stats(inst.conversation)['tokens']

    with patch.object(state_builder, '_instance_tool_schema_tokens', return_value=123):
        result = state_builder._serialize_instance(inst, pool, include_messages=True)

    assert result['total_tokens'] == expected_msg + 123
    assert result['total_words'] == get_history_stats(inst.conversation)['words']


# ── 4. Root totals (initial state + streaming tick) ─────────────────────────

def test_build_state_from_pool_total_includes_schema_tokens():
    from agent_cascade.api_integration_pkg import state_builder

    pool, inst = _make_state_pool(name='schema-root-full')
    with patch.object(state_builder, '_calc_token_stats',
                      return_value=({'tokens': 10, 'words': 1}, {'tokens': 2, 'words': 0})), \
         patch.object(state_builder, '_serialize_all_instances', return_value={}), \
         patch.object(state_builder, '_instance_tool_schema_tokens', return_value=77):
        result = state_builder.build_state_from_pool(pool, 'schema-root-full', generating=True)

    assert result is not None
    assert result['total_tokens'] == 10 + 2 + 77
    assert result['total_words'] == 1


def test_build_stream_update_total_includes_schema_tokens():
    from agent_cascade.api_integration_pkg import state_builder, streaming

    pool, inst = _make_state_pool(name='schema-root-stream')

    def _fake_serialize(pool_, name, force_full):
        return {name: {'messages': [], 'streaming': True}}

    def _fake_stream_stats(pool_, name, conv, sr, resp, *args):
        return ({'tokens': 10, 'words': 1}, {'tokens': 2, 'words': 0})

    with patch.object(state_builder, '_serialize_instances_incremental', _fake_serialize), \
         patch.object(streaming, '_calc_stream_token_stats', _fake_stream_stats), \
         patch.object(state_builder, '_instance_tool_schema_tokens', return_value=77):
        result = state_builder.build_stream_update_from_pool(pool, 'schema-root-stream')

    assert result is not None
    assert result['total_tokens'] == 10 + 2 + 77
    assert result['total_words'] == 1
