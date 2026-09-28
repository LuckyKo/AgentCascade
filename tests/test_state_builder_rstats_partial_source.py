"""Regression tests for the r_stats PARTIAL-SOURCE fix (todo:135, plan v2).

Background — the cache-HIT branch of ``build_stream_update_from_pool`` used to
compute r_stats over the ``responses`` argument. That argument is NOT the
in-flight partial: it is the RUN-SCOPED accumulator
(``engine/core.py:1692``, extended at ``:2412`` every turn, cleared only on the
force-compression paths). Tokenizing it re-counts the whole run on EVERY tick
and double-counts messages that are already in the committed conversation. The
fix points r_stats at ``stream_resp_snapshot`` (the true partial, snapshotted
under the same lock as ``conv_snapshot``) at BOTH call sites:

  * state_builder.py cache-HIT branch  — per-tick hot path
  * streaming._calc_stream_token_stats_uncached — once-per-version-change path

The oracle throughout is the UNMODIFIED ``get_history_stats`` from utils.py;
token counting is never reimplemented here.

Fixture trap (mandatory, .agent_lessons/todo135-token-stats-cache-hit-fix.md):
any test reaching the cache-HIT branch must use a NON-EMPTY conversation with a
VARYING accumulator, or it passes vacuously (empty conv -> version None ->
always miss; constant accumulator -> LRU absorbs the cost and the call-count
bound cannot fail).

House style: plain functions for stubs, not MagicMock bodies (RecursionError
risk — same lesson file).
"""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest


# Fixture scale knobs: how many committed messages the "run" holds, and how large
# each committed tool-call argument blob is. Both are deliberately large so that a
# pre-fix r_stats computed over the whole run accumulator is measurably expensive
# (and double-counts ~2x) while the true partial stays tiny.
COMMITTED_MSG_COUNT = 300
TOOL_CALL_ARG_CHARS = 400


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _committed_tool_call_msg(i, nchars=TOOL_CALL_ARG_CHARS):
    """One committed assistant tool-call Message (distinct per index)."""
    from agent_cascade.llm.schema import Message

    return Message(
        role='assistant',
        content=None,
        function_call={'name': f'tool_{i}',
                       'arguments': json.dumps({'x': ('abcdefgh ' * (nchars // 9))[:nchars]})},
        ts=float(i + 1),
    )


def _make_committed_conversation(n=COMMITTED_MSG_COUNT):
    """Build n committed assistant tool-call Messages (the run's history)."""
    return [_committed_tool_call_msg(i) for i in range(n)]


def _make_partial_msg():
    """One in-flight partial Message (distinct from every committed message)."""
    from agent_cascade.llm.schema import Message

    return Message(role='assistant', content=None,
                   function_call={'name': 'tool_partial',
                                  'arguments': json.dumps({'x': 'partial body ' * 50})},
                   ts=999.0)


def _make_fake_pool(instance_name='Maine', conversation=None):
    """Build a MagicMock AgentPool sufficient to drive build_stream_update_from_pool.

    NOTE: conversation MUST be non-empty for cache-hit tests (fixture trap).
    ``_streaming_responses`` starts empty; tests set it per scenario.
    """
    instance = MagicMock()
    if conversation is None:
        conversation = _make_committed_conversation(50)
    instance.conversation = conversation
    instance._streaming_responses = []
    instance._compression_lock = threading.Lock()
    instance.compression_summary = ''
    instance.agent_class = 'orchestrator'

    pool = MagicMock()
    pool.get_instance.return_value = instance
    pool.instances = {instance_name: instance}
    # Identity slice: h_stats is computed over conv_snapshot as-is, so the test
    # oracle can be get_history_stats(conv) directly.
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)
    pool.has_messages.return_value = True
    pool.get_queue_messages.return_value = []
    pool.stopped = False
    pool.is_paused.return_value = False
    pool.active_stack = [instance_name]

    om = MagicMock()
    om.list_pending_approvals.return_value = []
    om.extra_work_folders_ro = []
    om.extra_work_folders_rw = []
    om.base_dir = '/tmp/fake-workspace'
    pool.operation_manager = om

    return pool


def _reset_caches():
    """Clear the shared CacheManager singleton so tests start cold."""
    from agent_cascade.api_integration_pkg import state_builder
    with state_builder._cache_mgr._lock:
        state_builder._cache_mgr.stream_token_stats.clear()
        state_builder._cache_mgr.stream_token_stats_versions.clear()
        state_builder._cache_mgr.stream_versions.clear()
        state_builder._cache_mgr.cached_instances.clear()


def _reset_msg_lru():
    """Clear the get_message_stats LRU so tokenization counts start at zero."""
    from agent_cascade.utils import utils
    if hasattr(utils.get_message_stats, '_msg_stats'):
        utils.get_message_stats._msg_stats.clear()


@pytest.fixture(autouse=True)
def _clean_caches():
    """Isolate every test from shared module-level cache state (and restore after)."""
    from agent_cascade.api_integration_pkg import state_builder
    from agent_cascade.utils import utils

    cm = state_builder._cache_mgr
    with cm._lock:
        saved_stats = dict(cm.stream_token_stats)
        saved_versions = dict(cm.stream_token_stats_versions)
        saved_stream_versions = dict(cm.stream_versions)
        saved_cached = dict(cm.cached_instances)
    saved_lru = dict(utils.get_message_stats._msg_stats) if hasattr(utils.get_message_stats, '_msg_stats') else {}

    _reset_caches()
    _reset_msg_lru()
    try:
        yield
    finally:
        with cm._lock:
            cm.stream_token_stats.clear()
            cm.stream_token_stats_versions.clear()
            cm.stream_versions.clear()
            cm.cached_instances.clear()
            cm.stream_token_stats.update(saved_stats)
            cm.stream_token_stats_versions.update(saved_versions)
            cm.stream_versions.update(saved_stream_versions)
            cm.cached_instances.update(saved_cached)
        if hasattr(utils.get_message_stats, '_msg_stats'):
            utils.get_message_stats._msg_stats.clear()
            utils.get_message_stats._msg_stats.update(saved_lru)


def _drive_tick(pool, instance_name='Maine', responses=None):
    """Run one build_stream_update_from_pool tick with serialization stubbed.

    ``_serialize_instances_incremental`` is replaced by a PLAIN FUNCTION (a MagicMock
    body would recurse — the stub returns the dict the real code would have built).
    """
    from agent_cascade.api_integration_pkg import state_builder

    def _fake_serialize(pool_, name, force_full):
        return {name: {'messages': [], 'streaming': True}}

    with patch.object(state_builder, '_serialize_instances_incremental', _fake_serialize):
        return state_builder.build_stream_update_from_pool(pool, instance_name, responses)


def _counted_get_message_stats(real_gms):
    """Wrap get_message_stats in a counting closure (plain function, not MagicMock)."""
    calls = {'n': 0}

    def counting_gms(msg):
        calls['n'] += 1
        return real_gms(msg)

    return counting_gms, calls


# ---------------------------------------------------------------------------
# T1. Cost is independent of run-accumulator size (the regression test for the fix)
# ---------------------------------------------------------------------------

def test_r_stats_cost_independent_of_run_accumulator_size():
    """300 committed tool-call msgs + 1-msg partial: r_stats must equal stats over
    the partial ONLY, and a cache-HIT tick must issue O(1) get_message_stats calls
    (<= 3), versus O(300) when tokenizing the whole run accumulator.

    Pre-fix this fails on BOTH counts: r_stats is computed over ``responses``
    (the 300-message run accumulator + partial) and every cache-HIT tick
    re-tokenizes all of it — >300 get_message_stats calls per tick once the LRU
    is cold.
    """
    from agent_cascade.api_integration_pkg import state_builder
    from agent_cascade.utils import utils
    from agent_cascade.utils.utils import get_history_stats

    conversation = _make_committed_conversation(COMMITTED_MSG_COUNT)
    pool = _make_fake_pool(conversation=conversation)
    instance = pool.get_instance('Maine')
    partial_msg = _make_partial_msg()
    instance._streaming_responses = [partial_msg]
    # The VARYING accumulator: the whole run (committed + current turn output),
    # as engine/core.py yields it. Pre-fix r_stats tokenized this every tick.
    responses = list(conversation) + [partial_msg]

    expected_r = get_history_stats([partial_msg])

    real_gms = utils.get_message_stats
    counting_gms, calls = _counted_get_message_stats(real_gms)

    with patch.object(utils, 'get_message_stats', counting_gms):
        # Tick 1: cache MISS (cold compute over the full conversation).
        result1 = _drive_tick(pool, responses=responses)
        assert isinstance(result1, dict)
        first_call_count = calls['n']

        # Tick 2: same version -> cache HIT. This is where pre-fix code re-tokenized
        # the whole run accumulator; post-fix it only touches the 1-msg partial.
        calls['n'] = 0
        result2 = _drive_tick(pool, responses=responses)
        assert isinstance(result2, dict)
        second_call_count = calls['n']

    # r_stats must equal stats over the TRUE PARTIAL ONLY (oracle: unmodified
    # get_history_stats). Post-fix total_tokens == h + partial; pre-fix it was
    # ~2x that. This is the non-vacuity check: with an empty conversation the
    # cache-HIT branch never runs and this assertion would pass vacuously.
    assert result2['total_tokens'] == get_history_stats(list(conversation))['tokens'] + expected_r['tokens'], (
        f'r_stats must reflect the 1-message partial only; total_tokens='
        f'{result2['total_tokens']} — the run accumulator is leaking into r_stats.'
    )

    # The cache-HIT tick issued O(1) get_message_stats calls (<= 3), not O(300).
    assert second_call_count <= 3, (
        f'Cache-HIT tick called get_message_stats {second_call_count} times — '
        f'r_stats is still tokenizing the run accumulator (pre-fix: ~300+ calls/tick). '
        f'(First/miss tick for reference: {first_call_count} calls.)'
    )


# ---------------------------------------------------------------------------
# T2. No double count
# ---------------------------------------------------------------------------

def test_total_tokens_no_double_count():
    """total_tokens must equal stats(conv) + stats(partial) EXACTLY, and stay below
    1.05x the committed-history tokens alone (the pre-fix value was ~2x)."""
    from agent_cascade.utils.utils import get_history_stats

    conversation = _make_committed_conversation(COMMITTED_MSG_COUNT)
    pool = _make_fake_pool(conversation=conversation)
    instance = pool.get_instance('Maine')
    partial_msg = _make_partial_msg()
    instance._streaming_responses = [partial_msg]
    responses = list(conversation) + [partial_msg]

    result = _drive_tick(pool, responses=responses)
    assert isinstance(result, dict)

    h_tokens = get_history_stats(list(conversation))['tokens']
    p_tokens = get_history_stats([partial_msg])['tokens']

    # Exact composition: committed history + in-flight partial, nothing else.
    assert result['total_tokens'] == h_tokens + p_tokens, (
        f'total_tokens ({result['total_tokens']}) must equal stats(conv) '
        f'({h_tokens}) + stats(partial) ({p_tokens}) exactly — any gap means '
        'committed run messages are being counted twice.'
    )
    # The old value was ~2x the true total; it must not be close to that.
    assert result['total_tokens'] < h_tokens * 1.05, (
        f'total_tokens ({result['total_tokens']}) is >= 1.05x the committed-history '
        f'tokens ({h_tokens}) — double-counting of the run accumulator.'
    )


# ---------------------------------------------------------------------------
# T3. Idempotence across ticks
# ---------------------------------------------------------------------------

def test_total_tokens_idempotent_across_ticks():
    """Two consecutive cache-HIT ticks with the same partial must return
    byte-identical totals (no upward drift)."""
    from agent_cascade.utils.utils import get_history_stats

    conversation = _make_committed_conversation(COMMITTED_MSG_COUNT)
    pool = _make_fake_pool(conversation=conversation)
    instance = pool.get_instance('Maine')
    partial_msg = _make_partial_msg()
    instance._streaming_responses = [partial_msg]
    responses = list(conversation) + [partial_msg]

    # Tick 1 (miss, populates the cache), then two HIT ticks.
    _drive_tick(pool, responses=responses)
    r2 = _drive_tick(pool, responses=responses)
    r3 = _drive_tick(pool, responses=responses)

    assert r2['total_tokens'] == r3['total_tokens'], (
        f'Idempotence violated: tick 2 total {r2['total_tokens']} != tick 3 total '
        f'{r3['total_tokens']} — totals drift across ticks.'
    )
    assert r2['total_words'] == r3['total_words'], (
        'Idempotence violated: total_words drifted between two identical ticks.'
    )
    # And the stable value is exactly history + partial.
    expected = get_history_stats(list(conversation))['tokens'] + get_history_stats([partial_msg])['tokens']
    assert r2['total_tokens'] == expected, (
        f'total_tokens ({r2['total_tokens']}) != committed + partial ({expected}).'
    )


# ---------------------------------------------------------------------------
# T4. Empty partial
# ---------------------------------------------------------------------------

def test_empty_partial_gives_zero_r_stats():
    """No in-flight partial -> r_stats is {0, 0} and total_tokens == h_stats.
    Guards the None snapshot at state_builder.py:559."""
    from agent_cascade.utils.utils import get_history_stats

    conversation = _make_committed_conversation(COMMITTED_MSG_COUNT)
    pool = _make_fake_pool(conversation=conversation)
    instance = pool.get_instance('Maine')
    instance._streaming_responses = []  # falsy -> stream_resp_snapshot is None
    responses = list(conversation)

    result = _drive_tick(pool, responses=responses)
    assert isinstance(result, dict)

    h_tokens = get_history_stats(list(conversation))['tokens']
    assert result['total_tokens'] == h_tokens, (
        f'With an empty partial, total_tokens ({result['total_tokens']}) must equal '
        f'the committed-history tokens ({h_tokens}) — r_stats must be zero.'
    )


# ---------------------------------------------------------------------------
# T5. Second call site: _calc_stream_token_stats_uncached
# ---------------------------------------------------------------------------

def test_uncached_r_stats_ignores_responses_accumulator():
    """Direct unit test of the once-per-version-change path: a LARGE ``responses``
    (run accumulator) plus a SMALL stream_resp_snapshot -> r_stats must reflect
    the snapshot only."""
    from agent_cascade.api_integration_pkg import streaming
    from agent_cascade.utils.utils import get_history_stats

    large_responses = _make_committed_conversation(COMMITTED_MSG_COUNT)  # ~52k tokens pre-fix r_stats source
    small_snapshot = [_make_partial_msg()]               # the true partial

    pool = MagicMock()
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)

    h_stats, r_stats = streaming._calc_stream_token_stats_uncached(
        pool,
        _make_committed_conversation(10),  # committed history (h_stats source)
        small_snapshot,
        large_responses,
    )

    expected_r = get_history_stats(small_snapshot)
    assert r_stats == expected_r, (
        f'r_stats ({r_stats}) must equal stats over the stream_resp_snapshot '
        f'({expected_r}) — the `responses` run accumulator leaked into r_stats.'
    )
    # h_stats is untouched by this fix: still over the committed history.
    assert h_stats['tokens'] == get_history_stats(_make_committed_conversation(10))['tokens']


# ---------------------------------------------------------------------------
# T6. Compression invariance
# ---------------------------------------------------------------------------

def test_total_tokens_tracks_history_after_compression():
    """Mutate conv between two calls (simulate _rebuild_working_set): total_tokens
    must track the NEW history while r_stats stays unaffected by the accumulator."""
    from agent_cascade.utils.utils import get_history_stats

    conversation = _make_committed_conversation(COMMITTED_MSG_COUNT)
    pool = _make_fake_pool(conversation=conversation)
    instance = pool.get_instance('Maine')
    partial_msg = _make_partial_msg()
    instance._streaming_responses = [partial_msg]
    # The run accumulator SURVIVES compression (response.clear() is not called on
    # the _rebuild_working_set path) — it still holds all 300 committed messages.
    responses = list(conversation) + [partial_msg]

    # Tick 1 over the pre-compression history.
    r1 = _drive_tick(pool, responses=responses)
    expected_total_1 = (get_history_stats(list(conversation))['tokens']
                        + get_history_stats([partial_msg])['tokens'])
    assert r1['total_tokens'] == expected_total_1

    # Simulate compression: the committed conversation SHRINKS.
    del conversation[200:]
    expected_total_2 = (get_history_stats(list(conversation))['tokens']
                        + get_history_stats([partial_msg])['tokens'])
    assert expected_total_2 < expected_total_1

    # Tick 2: version changed -> recompute. total_tokens must track the NEW history;
    # r_stats is still exactly the partial (the stale accumulator in `responses`
    # must not inflate it).
    r2 = _drive_tick(pool, responses=responses)
    assert r2['total_tokens'] == expected_total_2, (
        f'After compression total_tokens ({r2['total_tokens']}) must track the new '
        f'history + partial ({expected_total_2}); got a value that suggests stale '
        'h_stats or an accumulator-inflated r_stats.'
    )
