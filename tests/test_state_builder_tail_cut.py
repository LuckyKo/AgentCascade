"""Unit tests for additive/delta streaming tail cut (phase 1).

Pins the backend delta logic in ``state_builder``:

* ``_safe_tail_start_index`` — R6 tool-pair integrity rule (the trickiest part;
  written FIRST so it pins the rule before the rest is trusted).
* The tail cut in ``_serialize_instance`` (flag ON -> bounded tail, absolute indices).
* Flag OFF / ``streaming=False`` regression guards (byte-identical full send).
* ``history_count`` invariant and dedup invariant.
* ``prefix_shrank`` detection in ``_serialize_instances_incremental`` (compression/rollback
  shrink -> forced full frame).

Self-contained: no network, no live API, no real AgentPool — lightweight fakes with an
RLock, a conversation list, and ``_streaming_responses``, mirroring the conventions in
test_state_builder.py. The feature flag is toggled by patching the module-level constant
(the env var is read at import time).
"""

import threading
from unittest.mock import MagicMock, patch

from agent_cascade.agent_instance import AgentState
from agent_cascade.api_integration_pkg import state_builder as sb

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _msg(role, content='', **kw):
    """Build a plain-dict message (serialize_message handles dicts natively)."""
    m = {'role': role, 'content': content}
    m.update(kw)
    return m


def _make_inst(conversation, streaming_responses=None, name='Maine'):
    """Lightweight AgentInstance stub with the attrs ``_serialize_instance`` touches."""
    inst = MagicMock()
    inst.instance_name = name
    inst.agent_class = 'coder'
    inst.parent_instance = None
    # Must be a real AgentState enum member: _serialize_instance reads `inst.state.name`.
    inst.state = AgentState.RUNNING
    inst.conversation = list(conversation)
    inst._streaming_responses = list(streaming_responses or [])
    inst._state_lock = threading.RLock()
    inst._compression_lock = threading.RLock()
    return inst


def _make_pool(inst):
    pool = MagicMock()
    pool.is_instance_halted.return_value = False
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)
    return pool


def _serialize(inst, pool, streaming=True, responses=None):
    """Drive ``_serialize_instance`` with a controlled flag value."""
    saved = sb.STREAM_DELTA_ENABLED
    try:
        sb.STREAM_DELTA_ENABLED = True  # default ON for these tests; individual tests flip it
        return sb._serialize_instance(
            inst,
            pool,
            include_messages=True,
            streaming=streaming,
            streaming_responses=responses if responses is not None else inst._streaming_responses,
        )
    finally:
        sb.STREAM_DELTA_ENABLED = saved


# ---------------------------------------------------------------------------
# 1. _safe_tail_start_index — tool-pair integrity table (pins the rule)
# ---------------------------------------------------------------------------


def test_safe_tail_plain_end():
    # [..., user, assistant] plain end -> cut to last TAIL_COMMITTED(1) msg
    msgs = [_msg('user', 'q'), _msg('assistant', 'a')]
    assert sb._safe_tail_start_index(msgs) == 1


def test_safe_tail_pair_at_end():
    # [..., user, assistant(tool_calls), tool] -> widen to whole last chain
    msgs = [
        _msg('user', 'q'),
        _msg('assistant', '', tool_calls=[{
            'id': '1',
            'function': {}
        }]),
        _msg('tool', 'result'),
    ]
    assert sb._safe_tail_start_index(msgs) == 1


def test_safe_tail_chain_of_four():
    # [..., user, asst(tc), tool, asst(tc), tool] (chain of 4 after a user) -> whole chain
    msgs = [
        _msg('user', 'q'),
        _msg('assistant', '', tool_calls=[{
            'id': '1'
        }]),
        _msg('tool', 'r1'),
        _msg('assistant', '', tool_calls=[{
            'id': '2'
        }]),
        _msg('tool', 'r2'),
    ]
    assert sb._safe_tail_start_index(msgs) == 1


def test_safe_tail_plain_then_chain():
    # [..., asst(plain), user, asst(tc), tool] (len 4): backward scan stops at the plain
    # assistant (index 0) -> boundary=1; desired cut c=len-1=3 > boundary -> widen to whole
    # last chain. NOTE: the plan's §1.2 table row for this shape is internally inconsistent
    # (it labels len=5 / start_idx=3 but the shape shown has len 4); the code's R6-safe rule
    # yields start_idx=2 here, and no call/response pair is split either way.
    msgs = [
        _msg('assistant', 'a0'),
        _msg('user', 'q'),
        _msg('assistant', '', tool_calls=[{
            'id': '1'
        }]),
        _msg('tool', 'r1'),
    ]
    assert sb._safe_tail_start_index(msgs) == 2


def test_safe_tail_unbroken_chain_full_send():
    # One unbroken 20-msg chain from index 0 -> full send (start_idx 0)
    msgs = []
    for i in range(10):
        msgs.append(_msg('assistant', '', tool_calls=[{'id': str(i)}]))
        msgs.append(_msg('tool', f"r{i}"))
    assert len(msgs) == 20
    assert sb._safe_tail_start_index(msgs) == 0


def test_safe_tail_single_message():
    assert sb._safe_tail_start_index([_msg('user', 'q')]) == 0


def test_safe_tail_legacy_function_call():
    # legacy function_call + function response -> widen to whole chain
    msgs = [
        _msg('user', 'q'),
        _msg('assistant', '', function_call={
            'name': 'f',
            'arguments': '{}'
        }),
        _msg('function', 'result'),
    ]
    assert sb._safe_tail_start_index(msgs) == 1


def test_safe_tail_empty_and_misconfig():
    assert sb._safe_tail_start_index([]) == 0
    # TAIL_COMMITTED <= 0 -> always full send (misconfig guard)
    with patch.object(sb, 'TAIL_COMMITTED', 0):
        assert sb._safe_tail_start_index([_msg('user', 'q'), _msg('assistant', 'a')]) == 0


# ---------------------------------------------------------------------------
# 2. Tail cut on: flag ON -> bounded tail + absolute indices + history_count
# ---------------------------------------------------------------------------


def test_tail_cut_flag_on():
    committed = [_msg('user' if i % 2 == 0 else 'assistant', f"m{i}") for i in range(50)]
    partial = _msg('assistant', 'streaming...')
    inst = _make_inst(committed, streaming_responses=[partial])
    pool = _make_pool(inst)

    result = _serialize(inst, pool, streaming=True)

    # last committed + 1 streaming partial
    assert len(result['messages']) == 2, f"expected tail of 2, got {len(result['messages'])}"
    # TOTAL history count (committed + streaming), unaffected by the cut
    assert result['history_count'] == 51
    # first sent message carries ABSOLUTE index 49 (the last committed message)
    assert result['messages'][0]['index'] == 49
    assert result['messages'][1]['index'] == 50
    assert result['is_partial'] is True


# ---------------------------------------------------------------------------
# 3. Flag OFF -> full send (regression guard, byte-identical to legacy)
# ---------------------------------------------------------------------------


def test_flag_off_full_send():
    committed = [_msg('user' if i % 2 == 0 else 'assistant', f"m{i}") for i in range(50)]
    partial = _msg('assistant', 'streaming...')
    inst = _make_inst(committed, streaming_responses=[partial])
    pool = _make_pool(inst)

    saved = sb.STREAM_DELTA_ENABLED
    try:
        sb.STREAM_DELTA_ENABLED = False
        result = sb._serialize_instance(
            inst,
            pool,
            include_messages=True,
            streaming=True,
            streaming_responses=inst._streaming_responses,
        )
    finally:
        sb.STREAM_DELTA_ENABLED = saved

    # full conversation + 1 partial, indices start at 0 (legacy behavior)
    assert len(result['messages']) == 51
    assert result['history_count'] == 51
    assert result['messages'][0]['index'] == 0


# ---------------------------------------------------------------------------
# 4. streaming=False (force_full / connect-time) -> full send regardless of flag
# ---------------------------------------------------------------------------


def test_streaming_false_full_send_flag_on():
    committed = [_msg('user' if i % 2 == 0 else 'assistant', f"m{i}") for i in range(50)]
    partial = _msg('assistant', 'streaming...')
    inst = _make_inst(committed, streaming_responses=[partial])
    pool = _make_pool(inst)

    result = _serialize(inst, pool, streaming=False)  # flag ON by default here

    # no tail cut even with a live partial in flight
    assert len(result['messages']) == 51
    assert result['history_count'] == 51
    assert result['messages'][0]['index'] == 0
    # is_partial still reflects the live _streaming_responses (independent of `streaming`)
    assert result['is_partial'] is True


# ---------------------------------------------------------------------------
# 5. history_count invariant + absolute indices across all shapes
# ---------------------------------------------------------------------------


def test_history_count_invariant_and_absolute_indices():
    shapes = [
        [_msg('user', 'q'), _msg('assistant', 'a')],
        [_msg('user', 'q'), _msg('assistant', '', tool_calls=[{
            'id': '1'
        }]), _msg('tool', 'r')],
        [
            _msg('assistant', 'a0'),
            _msg('user', 'q'),
            _msg('assistant', '', tool_calls=[{
                'id': '1'
            }]),
            _msg('tool', 'r')
        ],
    ]
    for committed in shapes:
        partial = _msg('assistant', 'streaming...')
        inst = _make_inst(committed, streaming_responses=[partial])
        pool = _make_pool(inst)
        result = _serialize(inst, pool, streaming=True)

        # invariant: history_count == len(committed) + num_streaming (regardless of tail size)
        assert result['history_count'] == len(committed) + 1
        # absolute index check: every sent message's index == its position in the FULL
        # conversation. start_idx = history_count - messages.length; a message at tail
        # position `pos` sits at full position start_idx + pos (the streaming partial is NOT
        # committed, so it does not shift the committed messages' absolute indices).
        start_idx = result['history_count'] - len(result['messages'])
        for pos, m in enumerate(result['messages']):
            assert m['index'] == start_idx + pos, f"index {m['index']} != full position {start_idx + pos}"


# ---------------------------------------------------------------------------
# 6. Dedup invariant: a partial identical to the last committed msg is not double-appended
#    (relies on the last committed message being inside the tail — documented in §1.4)
# ---------------------------------------------------------------------------


def test_dedup_partial_matches_last_committed():
    # last committed assistant message has content "DONE"; the in-flight partial equals it
    committed = [_msg('user', 'q'), _msg('assistant', 'DONE')]
    partial = _msg('assistant', 'DONE')  # same fingerprint as committed[1]
    inst = _make_inst(committed, streaming_responses=[partial])
    pool = _make_pool(inst)

    result = _serialize(inst, pool, streaming=True)

    # tail = [committed assistant "DONE"] ; the identical partial is deduped (num_streaming=0)
    assert len(result['messages']) == 1
    assert result['history_count'] == 2  # committed only; deduped partial not counted


# ---------------------------------------------------------------------------
# 7. prefix_shrank detection in _serialize_instances_incremental
# ---------------------------------------------------------------------------


def test_prefix_shrink_forces_full_frame():
    """When the conversation shrinks (compression/rollback), the frame is forced full."""
    # Two committed messages; a streaming partial in flight.
    inst = _make_inst([_msg('user', 'q'), _msg('assistant', 'a')], streaming_responses=[_msg('assistant', 'partial')])
    pool = MagicMock()
    pool.instances = {'Maine': inst}
    pool.is_instance_halted.return_value = False
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)

    saved_versions = dict(sb._cache_mgr.stream_versions)
    saved_flag = sb.STREAM_DELTA_ENABLED
    try:
        with sb._cache_mgr._lock:
            sb._cache_mgr.stream_versions.clear()
            sb.STREAM_DELTA_ENABLED = True

            # Frame 1: active instance, streaming -> delta tail. committed=[user,assistant],
            # TAIL_COMMITTED=1 => start_idx=1 => [committed assistant(idx 1), partial(idx 2)].
            r1 = sb._serialize_instances_incremental(pool, 'Maine', force_full=False)
            assert len(r1['Maine']['messages']) == 2
            assert r1['Maine']['messages'][0]['index'] == 1  # tail (not full)

            # Simulate compression/rollback: shrink the conversation to a single message.
            with inst._compression_lock:
                inst.conversation = [_msg('user', 'q')]

            # Frame 2: current_version[0] (1) < prev_version[0] (2) -> prefix_shrank -> FULL frame
            # (no tail cut): [committed user(idx 0), partial(idx 1)]. Distinguished from the
            # delta tail by the first message index being 0.
            r2 = sb._serialize_instances_incremental(pool, 'Maine', force_full=False)
            assert len(r2['Maine']['messages']) == 2
            assert r2['Maine']['messages'][0]['index'] == 0  # full send (prefix shrank)
    finally:
        with sb._cache_mgr._lock:
            sb.STREAM_DELTA_ENABLED = saved_flag
            sb._cache_mgr.stream_versions.clear()
            sb._cache_mgr.stream_versions.update(saved_versions)


def test_prefix_shrink_noop_when_growing():
    """A growing conversation does NOT trigger prefix_shrank (delta tail still applies)."""
    inst = _make_inst([_msg('user', 'q')], streaming_responses=[_msg('assistant', 'partial')])
    pool = MagicMock()
    pool.instances = {'Maine': inst}
    pool.is_instance_halted.return_value = False
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)

    saved_versions = dict(sb._cache_mgr.stream_versions)
    try:
        with sb._cache_mgr._lock:
            sb._cache_mgr.stream_versions.clear()
            sb.STREAM_DELTA_ENABLED = True

            sb._serialize_instances_incremental(pool, 'Maine', force_full=False)

            # Grow the conversation (new committed message) — not a shrink.
            with inst._compression_lock:
                inst.conversation.append(_msg('assistant', 'a'))

            r2 = sb._serialize_instances_incremental(pool, 'Maine', force_full=False)
            # Delta tail still applies (no shrink): last committed + partial.
            assert len(r2['Maine']['messages']) == 2
    finally:
        with sb._cache_mgr._lock:
            sb._cache_mgr.stream_versions.clear()
            sb._cache_mgr.stream_versions.update(saved_versions)


def test_streaming_true_no_responses_sends_full():
    """CRITICAL: streaming=True but empty stream_responses => is_partial=False.

    The tail cut must NOT apply — a non-partial frame with only a tail would cause
    the frontend to replace the entire message list with just the tail (UI corruption).
    With no active stream, we must send the full history.
    """
    msgs = [_msg('user', 'q'), _msg('assistant', 'a1'), _msg('user', 'q2')]
    inst = _make_inst(msgs, streaming_responses=[])  # NO streaming responses
    pool = MagicMock()
    pool.instances = {'Maine': inst}
    pool.is_instance_halted.return_value = False
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)

    saved_flag = sb.STREAM_DELTA_ENABLED
    try:
        with patch.object(sb, 'STREAM_DELTA_ENABLED', True):
            result = sb._serialize_instance(
                inst,
                pool,
                include_messages=True,
                streaming=True,  # not force_full
                streaming_responses=[],  # but no active stream!
            )
            # Must send ALL messages (full), not just the tail.
            assert len(result['messages']) == 3, \
                f"Expected full send (3 msgs) when no streaming responses, got {len(result['messages'])}"
            assert result['is_partial'] is False
    finally:
        sb.STREAM_DELTA_ENABLED = saved_flag


# ---------------------------------------------------------------------------
# 8. Committed-prefix cache (delta mode): per-frame O(n) -> O(tail)
# ---------------------------------------------------------------------------


def _clear_prefix_cache():
    """Drop the committed-prefix cache so a test starts from a cold prefix."""
    with sb._cache_mgr._lock:
        sb._cache_mgr.prefix_cache.clear()


def _make_toolchain_inst(name='Maine'):
    """An unbroken tool-chain conversation -> _safe_tail_start_index returns 0 (full send).

    The last committed message is a plain assistant 'DONE' (so the current turn's answer is in
    the tail), followed by the in-flight streaming partial. This is the normal agent case where
    the prefix cache must kick in to avoid re-serializing the whole history every frame.
    """
    msgs = []
    for i in range(5):
        msgs.append(_msg('assistant', '', function_call={
            'name': f'tool{i}',
            'arguments': f'{{"x": {i}}}'
        }, extra={'function_id': f'call_{i}'}))
        msgs.append(_msg('tool', f'result {i}', name=f'tool{i}'))
    msgs.append(_msg('assistant', 'DONE'))  # last committed (current turn's answer) -> tail
    return _make_inst(msgs, streaming_responses=[_msg('assistant', 'partial')], name=name)


def _no_cache_baseline(inst, pool, streaming):
    """Serialize with the prefix cache DISABLED — the ground-truth 'no-cache path' output."""
    saved_lookup = sb._prefix_cache_lookup
    try:
        sb._prefix_cache_lookup = lambda *a, **k: None  # force every frame to miss
        return _serialize(inst, pool, streaming=streaming)
    finally:
        sb._prefix_cache_lookup = saved_lookup


class _SerializeSpy:
    """Count committed-range serializations AND disable the per-message UI cache.

    ``serialize_message`` short-circuits on an instance-attached ``_ui_cache`` (via
    ``sb._get_ui_cache``) BEFORE doing real work, so a warm-up call would make later frames hit
    that cache and under-count the spy. Disabling ``_get_ui_cache``/``_store_ui_cache`` forces every
    serialization to do real work, so the count reflects exactly which committed messages were
    (re-)serialized per frame — i.e. whether the prefix was reused from the prefix cache.
    """

    def __init__(self):
        self.count = 0

    def __enter__(self):
        # Capture the ORIGINAL function object BEFORE reassigning the module attribute — the
        # wrapper must close over the bound object (not the name `sb.serialize_message`, which
        # would resolve to the wrapper itself and recurse).
        real_serialize = sb.serialize_message
        self._saved = (real_serialize, sb._get_ui_cache, sb._store_ui_cache)

        def counting(msg, *a, **k):
            self.count += 1
            return real_serialize(msg, *a, **k)

        sb.serialize_message = counting
        sb._get_ui_cache = lambda msg: None
        sb._store_ui_cache = lambda msg, data: None
        return self

    def __exit__(self, *exc):
        sb.serialize_message, sb._get_ui_cache, sb._store_ui_cache = self._saved
        return False


def test_prefix_reuse_identical_output_and_single_serialize():
    """Across a turn's streaming frames (same committed conv, growing partial):

    * output is byte-identical to the no-cache path (correctness: same msgs/indices/dedup);
    * the committed prefix [0:cut_point] is serialized exactly ONCE across all frames.
    """
    inst = _make_toolchain_inst()
    pool = _make_pool(inst)

    # Ground truth: for each frame's partial, capture the no-cache output under the SAME conditions
    # as the cached frames (UI cache disabled via the spy) so the ONLY variable is the prefix cache.
    baselines = []
    with _SerializeSpy():  # disables UI cache; count ignored here
        for k in range(4):
            inst._streaming_responses = [_msg('assistant', f'partial {k}')]
            baselines.append(_no_cache_baseline(inst, pool, streaming=True))

    # Now drive the SAME frames WITH the cache (spy counts committed-range serializations).
    _clear_prefix_cache()
    spy = _SerializeSpy()
    with spy:
        for k in range(4):
            inst._streaming_responses = [_msg('assistant', f'partial {k}')]
            cached = _serialize(inst, pool, streaming=True)

            # Correctness: byte-identical to the no-cache path for this exact frame.
            assert cached['messages'] == baselines[k]['messages'], \
                f"frame {k}: cached output differs from no-cache path"
            assert cached['history_count'] == baselines[k]['history_count']
            assert cached['is_partial'] is True

    # Per-frame cost is O(streaming), not O(n). Frame 0 (MISS) serializes the committed range
    # (1 msg 'DONE') + the partial = 2 calls; frames 1-3 (HIT) reuse the cached committed range and
    # serialize ONLY the growing partial = 1 call each. Total = 2 + 1 + 1 + 1 = 5. Crucially this is
    # CONSTANT regardless of history length: with 5k committed msgs frame 0 would be 5k+1 but frames
    # 1-3 still 1 each (the win), whereas the no-cache path re-serializes all 5k+1 EVERY frame.
    assert spy.count == 5, f"expected MISS(2)+3xHIT(1)=5 serialize calls for 4 frames, got {spy.count}"


def test_prefix_reuse_nonzero_cut_point():
    """A plain conversation (cut_point > 0): prefix is serialized once, tail per frame.

    Proves the general O(tail) path: with a 20-msg history and TAIL_COMMITTED=1, cut_point=19,
    so only the last committed message + streaming partial are re-serialized per frame while the
    19-message prefix is cached. The UI cache is disabled (see _SerializeSpy) so the counts reflect
    real serialization work, not instance-attached UI-cache hits.
    """
    committed = [_msg('user' if i % 2 == 0 else 'assistant', f'm{i}') for i in range(20)]
    inst = _make_inst(committed, streaming_responses=[_msg('assistant', 'p')])
    pool = _make_pool(inst)

    _clear_prefix_cache()
    spy = _SerializeSpy()
    with spy:
        r1 = _serialize(inst, pool, streaming=True)  # MISS -> builds + caches prefix (20 serializes)
        first_calls = spy.count
        inst._streaming_responses = [_msg('assistant', 'p2')]
        r2 = _serialize(inst, pool, streaming=True)  # HIT -> tail only (1 committed + partial deduped)

    # Frame 1 (MISS) serialized the committed range [19:] = 1 msg + the streaming partial = 2 calls,
    # and cached that committed range. Frame 2 (HIT) reused the cached committed range verbatim and
    # re-serialized ONLY the growing partial = 1 call. The committed range was NOT re-serialized on
    # frame 2 — that's the O(streaming) win (a no-cache frame would serialize all 20 committed + partial).
    assert first_calls == 2, f"frame 1 should serialize committed(1)+partial(1)=2, got {first_calls}"
    assert spy.count - first_calls == 1, \
        f"frame 2 should serialize only the partial (committed reused), got {spy.count - first_calls}"

    # Correctness: identical to a fresh no-cache serialization of the same frame.
    baseline = _no_cache_baseline(inst, pool, streaming=True)
    assert r2['messages'] == baseline['messages']


def test_prefix_rebuild_on_commit():
    """After Phase 4 commits a new message (conversation grows), the next frame rebuilds the
    prefix and still produces correct output (the identity key changes -> cache miss)."""
    committed = [_msg('user' if i % 2 == 0 else 'assistant', f'm{i}') for i in range(10)]
    inst = _make_inst(committed, streaming_responses=[_msg('assistant', 'p')])
    pool = _make_pool(inst)

    _clear_prefix_cache()
    spy = _SerializeSpy()
    with spy:
        r1 = _serialize(inst, pool, streaming=True)  # builds prefix for the 10-msg conv
        calls_after_first = spy.count

        # Commit a new message (turn boundary): conversation grows.
        with inst._compression_lock:
            inst.conversation.append(_msg('assistant', 'new-commit'))

        r2 = _serialize(inst, pool, streaming=True)  # key changed -> rebuild prefix

    # Frame 2 must NOT reuse the stale cache (its identity key changed), so it is a MISS that
    # re-serializes the new committed range [10:] = 1 msg + partial = 2 calls and rebuilds the cache.
    assert spy.count - calls_after_first == 2, \
        f"rebuild should serialize committed(1)+partial(1)=2, got {spy.count - calls_after_first}"

    # Correctness: output matches a fresh no-cache serialization of the grown conversation.
    baseline = _no_cache_baseline(inst, pool, streaming=True)
    assert r2['messages'] == baseline['messages']
    assert r2['history_count'] == 11 + 1


def test_prefix_rebuild_on_shrink():
    """After compression shrinks the conversation, the next PARTIAL frame rebuilds the prefix
    (count dropped -> key change) and produces correct output."""
    committed = [_msg('user' if i % 2 == 0 else 'assistant', f'm{i}') for i in range(10)]
    inst = _make_inst(committed, streaming_responses=[_msg('assistant', 'p')])
    pool = _make_pool(inst)

    _clear_prefix_cache()
    spy = _SerializeSpy()
    with spy:
        _serialize(inst, pool, streaming=True)  # builds prefix for the 10-msg conv
        calls_after_first = spy.count

        # Compression shrinks the conversation.
        with inst._compression_lock:
            inst.conversation = committed[:5]

        r2 = _serialize(inst, pool, streaming=True)  # key changed (count 5) -> rebuild prefix

    # Shrink -> key change -> MISS: re-serializes the new committed range [4:] = 1 msg + partial
    # = 2 calls and rebuilds the cache for the shrunk conversation.
    assert spy.count - calls_after_first == 2, \
        f"rebuild should serialize committed(1)+partial(1)=2, got {spy.count - calls_after_first}"

    baseline = _no_cache_baseline(inst, pool, streaming=True)
    assert r2['messages'] == baseline['messages']


def test_force_full_bypasses_prefix_cache():
    """A force_full / connect-time frame (streaming=False -> use_delta False) sends the full
    history and does NOT consult or populate the prefix cache."""
    committed = [_msg('user' if i % 2 == 0 else 'assistant', f'm{i}') for i in range(10)]
    inst = _make_inst(committed, streaming_responses=[_msg('assistant', 'p')])
    pool = _make_pool(inst)

    # Prime the cache with a delta frame first.
    _clear_prefix_cache()
    _serialize(inst, pool, streaming=True)
    with sb._cache_mgr._lock:
        assert 'Maine' in sb._cache_mgr.prefix_cache  # cache was populated by the delta frame

    # Now a force_full frame (streaming=False). It must send the full history.
    result = _serialize(inst, pool, streaming=False)
    assert len(result['messages']) == 11  # full committed + partial
    assert result['messages'][0]['index'] == 0  # full send, not a tail

    # The force_full frame must NOT have replaced the cached prefix with a cut_point=0 entry.
    with sb._cache_mgr._lock:
        entry = sb._cache_mgr.prefix_cache.get('Maine')
        assert entry is not None and entry['cut_point'] == 9, \
            'force_full frame must not clobber the delta-mode prefix cache'


def test_stale_prefix_guard_with_cached_prefix():
    """The stale-prefix guard still works when the committed prefix is served from cache.

    The guard compares an in-flight partial against the LAST serialized assistant message and
    must receive the FULL serialized_msgs (prefix + tail) so that scan is correct. We prove the
    cached path preserves this by asserting frame 2 (cache HIT) produces output identical to a
    no-cache baseline for a growing partial, and that a stale prefix of the last committed
    assistant is suppressed identically on both frames.
    """
    # Last committed message is a plain assistant 'FINAL ANSWER'; the in-flight partial grows as a
    # genuine prefix of it (the commit-race window). Exact-fingerprint dedup misses each growing
    # step, so only the stale-prefix guard can suppress them.
    committed = [_msg('user' if i % 2 == 0 else 'assistant', f'm{i}') for i in range(10)]
    committed[-1] = _msg('assistant', 'FINAL ANSWER')
    inst = _make_inst(committed, streaming_responses=[_msg('assistant', 'F')])
    pool = _make_pool(inst)

    # Frame 1 (cache MISS) and frame 2 (cache HIT), each with a DISTINCT growing partial so the
    # frames are not trivially identical. Both must behave exactly like the no-cache path.
    _clear_prefix_cache()
    inst._streaming_responses = [_msg('assistant', 'F')]
    r1 = _serialize(inst, pool, streaming=True)
    baseline1 = _no_cache_baseline(inst, pool, streaming=True)
    assert r1['messages'] == baseline1['messages'], 'frame 1 (MISS) differs from no-cache path'

    inst._streaming_responses = [_msg('assistant', 'FIN')]  # longer stale prefix -> frame 2 (HIT)
    r2 = _serialize(inst, pool, streaming=True)
    baseline2 = _no_cache_baseline(inst, pool, streaming=True)
    assert r2['messages'] == baseline2['messages'], 'frame 2 (HIT) differs from no-cache path'

    # The stale partials are suppressed in BOTH frames: only the tail (last committed 'FINAL
    # ANSWER') is sent, never a second assistant message.
    for r in (r1, r2):
        assert len(r['messages']) == 1, f"stale partial should be suppressed, got {len(r['messages'])} msgs"
        assert r['history_count'] == 10  # committed only; stale partial not counted


def test_dedup_partial_matches_committed_range_message():
    """A streaming partial whose fingerprint matches a message in the SENT committed range is
    deduped on BOTH the MISS and the HIT frame. This is the invariant that would break if the
    cached path seeded existing_fingerprints from anything other than the full committed range:
    the HIT reuses `committed_fps` (built from msgs[start_idx:]) so a partial matching any sent
    committed message must be suppressed exactly as on the no-cache path.

    We use an UNBROKEN tool chain (start_idx==0) so the ENTIRE history is the sent range — the
    worst case where the dedup seed must cover all n messages. A partial duplicating a MIDDLE
    committed message (not the last) is only caught by full-range seeding."""
    msgs = []
    for i in range(5):
        msgs.append(_msg('assistant', '', function_call={'name': f'tool{i}', 'arguments': f'{{"x": {i}}}'},
                         extra={'function_id': f'call_{i}'}))
        msgs.append(_msg('tool', f'result {i}', name=f'tool{i}'))
    # A middle committed assistant with distinctive content (index 8, in the sent range [0:11]).
    msgs.append(_msg('assistant', 'MIDDLE_ANSWER'))
    inst = _make_inst(msgs, streaming_responses=[_msg('assistant', 'MIDDLE_ANSWER')])
    pool = _make_pool(inst)

    _clear_prefix_cache()
    r1 = _serialize(inst, pool, streaming=True)  # MISS (builds committed cache incl. committed_fps)
    baseline1 = _no_cache_baseline(inst, pool, streaming=True)
    assert r1['messages'] == baseline1['messages'], 'frame 1 (MISS) differs from no-cache path'

    inst._streaming_responses = [_msg('assistant', 'MIDDLE_ANSWER')]  # same range-matching partial
    r2 = _serialize(inst, pool, streaming=True)  # HIT (reuses cached committed_fps)
    baseline2 = _no_cache_baseline(inst, pool, streaming=True)
    assert r2['messages'] == baseline2['messages'], 'frame 2 (HIT) differs from no-cache path'

    for r in (r1, r2):
        # The partial 'MIDDLE_ANSWER' matches committed[8] (in the sent range) -> deduped on both
        # frames. It is NOT appended, so history_count stays at the committed length.
        assert r['history_count'] == 11, f"range-matching partial should be deduped, got hc={r['history_count']}"
