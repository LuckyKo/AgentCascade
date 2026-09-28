"""Regression tests for the token-stats cache HIT path (todo.md L135, plan v2).

Background — RC-1: the h_stats fast path in ``build_stream_update_from_pool`` could
NEVER execute. The reader built a 3-tuple version key but compared it against
``_cache_mgr.stream_versions`` (which only ever holds the serializer's 4-tuples),
and the writer (``streaming._calc_stream_token_stats``) never stored any version at
all — the dedicated ``stream_token_stats_versions`` store existed but was dead.
Every stream tick therefore recomputed full history stats: O(N) per tick, O(N^2)
per turn (the ~68% profiled mass in the slow-streaming run).

RC-2: the Message-object assistant tool-call branch in ``get_message_stats``
returned BEFORE the LRU was declared/consulted, so every tool-call message was
re-tokenized on every call.

These tests pin the post-fix behavior:

  1. h_stats is computed ONCE across many ticks of a stable non-empty conversation
     (MUST fail on unpatched code — run it pre-fix to prove).
  2. The version lands in ``stream_token_stats_versions``; ``stream_versions``
     entries stay 4-tuples.
  3. Appending a committed message invalidates the cache (recompute next tick).
  4. ``evict_instance`` removes the token-stats version entry.
  5. Assistant tool-call Message stats are LRU-cached (1 tokenization, not per call).

Fixture traps (verified — repeating them makes tests vacuous):
  * The existing ``_make_fake_pool`` in test_state_builder.py uses
    ``conversation = []`` ("empty -> no cached version match") — every existing
    test exercises the MISS path only. Cache-hit fixtures here MUST use a
    non-empty committed conversation.
  * Stubbing ``_serialize_instances_incremental`` with a MagicMock body throws
    RecursionError — use a plain function stub.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_committed_conversation(n=50):
    """Build n STABLE committed Message objects (non-empty — the cache-hit path).

    Each message gets a distinct ts so ``_msg_fingerprint`` (role, ts) is stable
    and unique per message. Objects are never mutated in place during the tests.
    """
    from agent_cascade.llm.schema import Message

    msgs = []
    for i in range(n):
        role = 'user' if i % 2 == 0 else 'assistant'
        # Distinct, non-trivial content per message (token math must be real).
        content = f'committed turn {i} with a few distinct words to tokenize number {i}'
        msgs.append(Message(role=role, content=content, ts=float(i + 1)))
    return msgs


def _make_fake_pool(instance_name='Maine', conversation=None):
    """Build a MagicMock AgentPool sufficient to drive build_stream_update_from_pool.

    NOTE: conversation MUST be non-empty for cache-hit tests (see module docstring).
    """
    from agent_cascade.llm.schema import Message  # noqa: F401  (ensures schema imported)

    instance = MagicMock()
    if conversation is None:
        conversation = _make_committed_conversation(50)
    instance.conversation = conversation
    instance._streaming_responses = None
    instance._compression_lock = threading.Lock()
    instance.compression_summary = ''
    instance.agent_class = 'orchestrator'

    pool = MagicMock()
    pool.get_instance.return_value = instance
    pool.instances = {instance_name: instance}
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)
    pool.has_messages.return_value = True
    pool.get_queue_messages.return_value = []
    pool.stopped = False
    pool.is_paused.return_value = False
    pool.active_stack = [instance_name]
    # _get_max_tokens_for_instance: MagicMock attr access returns MagicMock, which is
    # truthy, so the resolution chain short-circuits to a MagicMock (never used in
    # assertions here). No real settings/router needed.

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
    body would recurse — the stub returns a dict that the real code would have built).
    """
    from agent_cascade.api_integration_pkg import state_builder

    def _fake_serialize(pool_, name, force_full):
        return {name: {'messages': [], 'streaming': True}}

    with patch.object(state_builder, '_serialize_instances_incremental', _fake_serialize):
        return state_builder.build_stream_update_from_pool(pool, instance_name, responses)


# ---------------------------------------------------------------------------
# 1. h_stats computed once across many stream ticks (MUST FAIL pre-fix)
# ---------------------------------------------------------------------------

def test_h_stats_computed_once_across_many_stream_ticks():
    """M=20 ticks over a STABLE non-empty conversation: get_message_stats must be
    called far fewer than n_msgs * M times.

    Pre-fix this fails hard: the version comparison was always a miss (3-tuple vs
    4-tuple / no version stored), so every tick recomputed full history stats.
    """
    from agent_cascade.api_integration_pkg import state_builder
    from agent_cascade.utils import utils

    M = 20
    n_msgs = 50
    conversation = _make_committed_conversation(n_msgs)
    pool = _make_fake_pool(conversation=conversation)

    real_gms = utils.get_message_stats

    calls = {'n': 0}

    def counting_gms(msg):
        calls['n'] += 1
        return real_gms(msg)

    with patch.object(utils, 'get_message_stats', counting_gms):
        for tick in range(M):
            # Simulate a growing in-flight partial (the committed conversation is
            # UNCHANGED — that is the stable-turn invariant the cache relies on).
            responses = [MagicMock(role='assistant', content=f'partial {tick}',
                                   reasoning_content=None, function_call=None)]
            result = _drive_tick(pool, responses=responses)
            assert isinstance(result, dict)

    # Unpatched code: every tick recomputes h_stats over all n_msgs => >= n_msgs*M.
    # Patched code: first tick computes h_stats (n_msgs calls) + r_stats per tick;
    # ticks 2..M hit the cache (h_stats reused). Target ≈70 vs 1000+.
    assert calls['n'] < n_msgs * M, (
        f'RC-1 regression: get_message_stats called {calls['n']} times over '
        f'{M} ticks x {n_msgs} msgs — expected far below {n_msgs * M} once the '
        f'token-stats version cache hits.'
    )
    # Sharper bound: first tick does n_msgs history calls; each later tick only
    # recomputes the 1-message partial. Allow generous slack for r_stats work.
    assert calls['n'] <= n_msgs + M, (
        f'Expected ~{n_msgs}+{M} get_message_stats calls (one full pass + per-tick '
        f'partial), got {calls['n']} — the h_stats cache is not being reused.'
    )


# ---------------------------------------------------------------------------
# 2. Version store is token-stats specific
# ---------------------------------------------------------------------------

def test_version_store_is_token_stats_specific():
    """After a miss tick, the version must live in stream_token_stats_versions;
    any stream_versions entry (written by the serializer) stays a 4-tuple."""
    from agent_cascade.api_integration_pkg import state_builder

    conversation = _make_committed_conversation(10)
    pool = _make_fake_pool(conversation=conversation)

    result = _drive_tick(pool, responses=[MagicMock(role='assistant', content='p',
                                                    reasoning_content=None, function_call=None)])
    assert isinstance(result, dict)

    cm = state_builder._cache_mgr
    with cm._lock:
        version = cm.stream_token_stats_versions.get('Maine')
        stats = cm.stream_token_stats.get('Maine')
        sv_entry = cm.stream_versions.get('Maine')

    # The dedicated store must now hold the 3-tuple version (the writer stored it).
    assert version is not None, (
        'FIX 1 regression: _calc_stream_token_stats did not store the version in '
        'stream_token_stats_versions — the reader will keep missing forever.'
    )
    assert isinstance(version, tuple) and len(version) == 3, (
        f'Expected a 3-tuple version key (len, fingerprint, stream_count); got {version!r}. '
        'Do NOT add a 4th element — that reintroduces the O(N) invalidation.'
    )
    # Stats must be cached alongside it.
    assert stats is not None and isinstance(stats, tuple) and len(stats) == 2

    # The serializer's store (if populated) keeps its own 4-tuple semantics.
    if sv_entry is not None:
        assert isinstance(sv_entry, tuple) and len(sv_entry) == 4, (
            f'stream_versions entry must stay a 4-tuple; got {sv_entry!r}.'
        )


# ---------------------------------------------------------------------------
# 3. h_stats invalidates when the conversation changes
# ---------------------------------------------------------------------------

def test_h_stats_invalidates_when_conversation_changes():
    """Append a committed message -> next tick recomputes (no stale totals)."""
    from agent_cascade.api_integration_pkg import state_builder
    from agent_cascade.llm.schema import Message
    from agent_cascade.utils.utils import get_history_stats

    conversation = _make_committed_conversation(10)
    pool = _make_fake_pool(conversation=conversation)
    partial = [MagicMock(role='assistant', content='partial body',
                         reasoning_content=None, function_call=None)]

    # Tick 1: cold compute; h_stats covers the initial conversation.
    result1 = _drive_tick(pool, responses=partial)
    expected_h1 = get_history_stats(list(conversation))['tokens']
    total_after_first = result1['total_tokens']

    # Append a NEW committed message (conversation length changes -> version changes).
    new_msg = Message(role='user', content='a brand new committed turn with fresh words', ts=99.0)
    conversation.append(new_msg)
    expected_h2 = get_history_stats(list(conversation))['tokens']
    assert expected_h2 > expected_h1

    # Tick 2: must NOT reuse the stale h_stats — total_tokens must reflect the new message.
    result2 = _drive_tick(pool, responses=partial)
    assert result2['total_tokens'] >= expected_h2, (
        f'Stale-cache regression: after appending a committed message, total_tokens '
        f'({result2['total_tokens']}) did not grow to include it (h_stats expected >= '
        f'{expected_h2}). The version key must change with the conversation.'
    )
    assert result2['total_tokens'] > total_after_first


# ---------------------------------------------------------------------------
# 4. Pause/resume evicts the token-stats version
# ---------------------------------------------------------------------------

def test_pause_resume_evicts_token_stats_version():
    """evict_instance (pause/dismiss path) must remove the version entry so a
    dismissed+recreated instance cannot hit with stale h_stats."""
    from agent_cascade.api_integration_pkg import state_builder

    conversation = _make_committed_conversation(5)
    pool = _make_fake_pool(conversation=conversation)

    _drive_tick(pool, responses=[MagicMock(role='assistant', content='x',
                                           reasoning_content=None, function_call=None)])
    cm = state_builder._cache_mgr
    with cm._lock:
        assert 'Maine' in cm.stream_token_stats_versions, 'precondition: version was stored'

    # Pause/dismiss path.
    cm.evict_instance('Maine')

    with cm._lock:
        assert 'Maine' not in cm.stream_token_stats_versions, (
            'FIX 1 regression: evict_instance did not pop stream_token_stats_versions — '
            'a dismissed+recreated instance would read a STALE version and hit with '
            'wrong h_stats.'
        )
        assert 'Maine' not in cm.stream_token_stats


# ---------------------------------------------------------------------------
# 5. Tool-call Message stats are LRU-cached (RC-2)
# ---------------------------------------------------------------------------

def test_tool_call_message_stats_are_lru_cached():
    """Two get_message_stats calls on the same assistant tool-call Message must
    tokenize exactly ONCE and return identical results."""
    from agent_cascade.llm.schema import FunctionCall, Message
    from agent_cascade.utils import utils
    from agent_cascade.utils.tokenization_qwen import count_tokens

    msg = Message(
        role='assistant',
        content='',
        function_call=FunctionCall(name='shell_cmd', arguments='{"command": "ls -la"}'),
        ts=1.0,
    )

    # get_message_stats imports count_tokens locally:
    #   from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
    # so patch the source module attribute.
    calls = {'n': 0}
    real_qwen_count = count_tokens

    def counting(text):
        calls['n'] += 1
        return real_qwen_count(text)

    with patch('agent_cascade.utils.tokenization_qwen.count_tokens', side_effect=counting):
        first = utils.get_message_stats(msg)
        second = utils.get_message_stats(msg)

    assert calls['n'] == 1, (
        f'RC-2 regression: assistant tool-call Message was tokenized {calls['n']} times '
        'across two get_message_stats calls — expected exactly 1 (LRU hit on the 2nd).'
    )
    assert first == second, f'Results must be identical; got {first!r} vs {second!r}'
    assert first['tokens'] > 0 and first['words'] >= 0
