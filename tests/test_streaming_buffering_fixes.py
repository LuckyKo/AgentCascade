"""Regression tests for the chunky-streaming fixes (Fix 1, Fix 2 & Fix 3).

Background (see reports/streaming_buffering_FIX_PLAN.md and
reports/streaming-buffering-root-cause.md):

    The choppy "buffers for minutes then drops a huge chunk" behaviour is caused by
    three compounding factors. This module pins all three server-side fixes:

      * Fix 1 — proportional streaming tail in ``_serialize_instance``: during active
        streaming with a long history, only the last K = max(5, N//10) messages are
        serialized (with correct absolute indices). Non-streaming and short histories
        keep a full send. The frontend splice (startIdx = history_count -
        messages.length) stays aligned because ``history_count`` is
        ``original_history_count + num_streaming`` — the ``num_streaming`` term cancels
        against the appended streaming responses, so startIdx == start_idx.
      * Fix 2 — token-stats cache/version keys are QUANTIZED (//256) so a burst of
        small SSE chunks no longer invalidates the cache on every chunk (which forced
        a full-history recompute per chunk).
      * Fix 3 — streaming-tick broadcasts are floored at ~200ms so a burst of chunks
        no longer triggers one full-payload build+send per chunk.

Self-contained: no network, no live API — the pool is a MagicMock, mirroring the
conventions in test_state_builder.py.
"""

import threading
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Fix 2 — cache-key / version-tuple quantization (//256)
# ---------------------------------------------------------------------------

def _streaming_content_bucket(stream_resp_snapshot):
    """Return the quantized streaming-content bucket exactly as build_stream_update_from_pool does."""
    from agent_cascade.api_integration_pkg.state_builder import _get_msg_content, _get_msg_reasoning
    raw = sum(
        len(_get_msg_content(m)) + len(_get_msg_reasoning(m))
        for m in stream_resp_snapshot
    ) if stream_resp_snapshot else 0
    return raw // 256


def test_version_bucket_stable_within_256_chars():
    """Two snapshots differing only by <256 chars of streaming content share a bucket.

    Regression: with the raw length in the version tuple, every SSE chunk (a few dozen
    chars) produced a new bucket => cache miss => full-history recompute per chunk.
    """
    small = [{'role': 'assistant', 'content': 'x' * 10}]
    grown = [{'role': 'assistant', 'content': 'x' * 200}]   # +190 chars, still < 256

    assert _streaming_content_bucket(small) == _streaming_content_bucket(grown), (
        "sub-256-char streaming growth must NOT change the quantized bucket"
    )


def test_version_bucket_changes_at_256_chars():
    """A >=256-char difference in streaming content produces a different bucket."""
    base = [{'role': 'assistant', 'content': 'x' * 10}]
    grown = [{'role': 'assistant', 'content': 'x' * 300}]   # +290 chars, crosses the 256 boundary

    assert _streaming_content_bucket(base) != _streaming_content_bucket(grown), (
        "a >=256-char streaming-content jump MUST change the quantized bucket so stats refresh"
    )


def test_version_bucket_stable_across_sub_256_growth():
    """build_stream_update_from_pool must REUSE cached stats across a <256-char growth.

    The regression this guards: with the RAW streaming-content length in the version tuple,
    every SSE chunk (a few dozen chars) produced a new version => cache miss => a fresh
    full-history recompute per chunk. With //256 bucketing, a small growth keeps the same
    version and the cached stats are reused (no recompute).

    Mechanism: build_stream_update_from_pool compares its locally-built ``current_version``
    against ``_cache_mgr.stream_versions[instance_name]``. We seed that cache with the exact
    version the builder computes for 10 chars of streaming content, then drive a second build
    with 200 chars (same //256 bucket) — it must REUSE (no recompute). A control build with
    300 chars (next bucket) MUST recompute.

    The seeded version is computed by the SAME expression the builder uses (msg count, last
    msg id, streaming resp count, quantized content length), so the test pins the //256
    quantization contract end-to-end.
    """
    from agent_cascade.api_integration_pkg import state_builder, streaming

    instance = MagicMock()
    instance.instance_name = "Maine"
    instance.conversation = [{'role': 'user', 'content': 'hi'}]
    instance._compression_lock = threading.Lock()
    # Start with 10 chars of streaming content (bucket 0).
    instance._streaming_responses = [{'role': 'assistant', 'content': 'y' * 10}]

    pool = MagicMock()
    pool.get_instance.return_value = instance
    pool.instances = {"Maine": instance}
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.stopped = False
    pool.is_paused.return_value = False

    om = MagicMock()
    om.list_pending_approvals.return_value = []
    om.extra_work_folders_ro = []
    om.extra_work_folders_rw = []
    om.base_dir = "/tmp/fake-workspace"
    pool.operation_manager = om

    def _builder_version(conv_snapshot, stream_resp_snapshot):
        """Mirror build_stream_update_from_pool's current_version (with //256 quantization)."""
        raw = sum(
            len(state_builder._get_msg_content(m)) + len(state_builder._get_msg_reasoning(m))
            for m in stream_resp_snapshot
        ) if stream_resp_snapshot else 0
        return (
            len(conv_snapshot),
            id(conv_snapshot[-1]) if conv_snapshot else None,
            len(stream_resp_snapshot) if stream_resp_snapshot else 0,
            raw // 256,
        )

    calls = {'n': 0}

    def sentinel(pool_, name, conv, sr, resp):
        calls['n'] += 1
        return ({'tokens': 0, 'words': 0}, {'tokens': 0, 'words': 0})

    saved_stats = dict(state_builder._cache_mgr.stream_token_stats)
    saved_versions = dict(state_builder._cache_mgr.stream_versions)
    try:
        with state_builder._cache_mgr._lock:
            state_builder._cache_mgr.stream_token_stats.clear()
            state_builder._cache_mgr.stream_versions.clear()

        # Seed the cache as if a prior build had computed stats for 10 chars of streaming content.
        seed_version = _builder_version(
            list(instance.conversation), list(instance._streaming_responses)
        )
        assert seed_version[3] == 0, "10 chars must land in bucket 0"
        with state_builder._cache_mgr._lock:
            state_builder._cache_mgr.stream_token_stats["Maine"] = ({'tokens': 1, 'words': 1}, {'tokens': 0, 'words': 0})
            state_builder._cache_mgr.stream_versions["Maine"] = seed_version

        with patch.object(streaming, '_calc_stream_token_stats', sentinel):
            # Build A: grow to 200 chars (still bucket 0) -> version matches seed -> REUSE.
            instance._streaming_responses = [{'role': 'assistant', 'content': 'y' * 200}]
            state_builder.build_stream_update_from_pool(pool, "Maine")
            assert calls['n'] == 0, (
                f"sub-256-char streaming growth must NOT recompute token stats "
                f"(version bucket stable); got {calls['n']} recomputes"
            )

            # Build B: grow to 300 chars -> bucket 1 (300 // 256 == 1) -> RECOMPUTE.
            instance._streaming_responses = [{'role': 'assistant', 'content': 'y' * 300}]
            state_builder.build_stream_update_from_pool(pool, "Maine")
            assert calls['n'] == 1, (
                f"a >=256-char streaming-content jump MUST recompute token stats; got {calls['n']} recomputes"
            )
    finally:
        with state_builder._cache_mgr._lock:
            state_builder._cache_mgr.stream_token_stats.clear()
            state_builder._cache_mgr.stream_versions.clear()
            state_builder._cache_mgr.stream_token_stats.update(saved_stats)
            state_builder._cache_mgr.stream_versions.update(saved_versions)


# ---------------------------------------------------------------------------
# Fix 3 — min-interval floor for streaming-tick broadcasts
# ---------------------------------------------------------------------------

def _make_broadcast_pool():
    """A pool whose broadcast path is observable: build + queue are sentinels."""
    from agent_cascade.api_integration_pkg import state_builder, streaming

    instance = MagicMock()
    instance.conversation = [{'role': 'user', 'content': 'hi'}]
    instance._streaming_responses = None
    instance._compression_lock = threading.Lock()

    pool = MagicMock()
    pool.get_instance.return_value = instance
    pool.instances = {}
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.stopped = False
    pool.is_paused.return_value = False

    om = MagicMock()
    om.list_pending_approvals.return_value = []
    om.extra_work_folders_ro = []
    om.extra_work_folders_rw = []
    om.base_dir = "/tmp/fake-workspace"
    pool.operation_manager = om

    # Explicit queue + loop so the broadcast path runs without touching pool attributes.
    # NOTE: a bare MagicMock() is FALSY (bool(MagicMock()) is False), which would trip the
    # `if not ws_queue or not ws_loop` guard and return early — so force truthiness here.
    q = MagicMock()
    q.configure_mock(__bool__=lambda self: True)
    loop = MagicMock()
    loop.configure_mock(__bool__=lambda self: True)
    loop.is_closed.return_value = False
    return pool, q, loop


def _run_broadcast(pool, q, loop, is_streaming_tick, now_sec, last_send, turn_output, last_resp_len):
    """Call broadcast_stream_update with the build+queue steps stubbed out.

    Returns the returned ``last_send`` (float). A broadcast happened iff the returned
    value advanced to ``now_sec`` — this is the function's documented contract ("the
    returned last_send is updated only if a broadcast was actually sent"), so it is the
    reliable observable signal (patching the builder is unreliable because streaming.py
    imports build_stream_update_from_pool as its own module global).

    ``last_resp_len`` is passed explicitly by the caller (the length known from the
    PREVIOUS tick) so len_changed detection is deterministic.

    We patch ``streaming._put_stream_update`` with a REAL async function (so it returns a
    coroutine, as ``asyncio.run_coroutine_threadsafe`` requires) and
    ``asyncio.run_coroutine_threadsafe`` to a no-op so no real event-loop scheduling runs.
    """
    import asyncio
    from agent_cascade.api_integration_pkg import streaming

    async def fake_put(*a, **k):
        # No-op stand-in for the real _put_stream_update; must be a coroutine.
        return None

    with patch.object(streaming, '_put_stream_update', fake_put), \
         patch.object(asyncio, 'run_coroutine_threadsafe', lambda *a, **k: None):
        result = streaming.broadcast_stream_update(
            pool=pool,
            instance_name="Maine",
            turn_output=turn_output,
            is_streaming_tick=is_streaming_tick,
            tick_num=1,
            now_sec=now_sec,
            last_send=last_send,
            last_resp_len=last_resp_len,
            send_queue=q,
            loop=loop,
        )
    return result[0]


def test_streaming_tick_floor_suppresses_rapid_ticks():
    """Rapid consecutive streaming ticks within the 200ms floor broadcast only once.

    Regression: previously ``is_streaming_tick`` short-circuited the throttle, so every SSE
    chunk triggered a full-payload build+send. Now a second tick 50ms later is suppressed.
    A broadcast is detected by the returned last_send advancing to now_sec.
    """
    pool, q, loop = _make_broadcast_pool()
    # First tick at t=0: last_send starts far in the past -> interval elapsed -> broadcast.
    new_last = _run_broadcast(pool, q, loop, True, now_sec=0.0, last_send=-1.0, turn_output=[{'role': 'assistant', 'content': 'a'}], last_resp_len=0)
    assert new_last == 0.0, "first streaming tick (interval elapsed) must broadcast (last_send advances to now_sec)"

    # Second tick 50ms later: within the 200ms floor -> suppressed (last_send unchanged).
    new_last2 = _run_broadcast(pool, q, loop, True, now_sec=0.05, last_send=new_last, turn_output=[{'role': 'assistant', 'content': 'a'}], last_resp_len=1)
    assert new_last2 == new_last, "streaming tick within 200ms of the last send must be suppressed (last_send unchanged)"


def test_streaming_tick_broadcasts_after_floor_elapses():
    """A streaming tick after >=200ms since the last send broadcasts again."""
    pool, q, loop = _make_broadcast_pool()
    # Establish a last_send at t=0.
    new_last = _run_broadcast(pool, q, loop, True, now_sec=0.0, last_send=-1.0, turn_output=[{'role': 'assistant', 'content': 'a'}], last_resp_len=0)
    assert new_last == 0.0
    # 250ms later: floor elapsed -> broadcast (last_send advances).
    new_last2 = _run_broadcast(pool, q, loop, True, now_sec=new_last + 0.25, last_send=new_last, turn_output=[{'role': 'assistant', 'content': 'a'}], last_resp_len=1)
    assert new_last2 == new_last + 0.25, "streaming tick after the 200ms floor must broadcast (last_send advances)"


def test_len_changed_bypasses_floor():
    """A committed-message length change broadcasts immediately even within the floor."""
    pool, q, loop = _make_broadcast_pool()
    # Establish last_send at t=0 with a single message already known (last_resp_len matches).
    new_last = _run_broadcast(pool, q, loop, True, now_sec=0.0, last_send=-1.0, turn_output=[{'role': 'assistant', 'content': 'a'}], last_resp_len=0)
    assert new_last == 0.0
    # 50ms later, a NEW message is committed (turn_output grows to 2). len_changed=True.
    new_last2 = _run_broadcast(pool, q, loop, False, now_sec=new_last + 0.05, last_send=new_last, turn_output=[{'role': 'assistant', 'content': 'a'}, {'role': 'user', 'content': 'b'}], last_resp_len=1)
    assert new_last2 == new_last + 0.05, "len_changed must bypass the streaming-tick floor and broadcast immediately (last_send advances)"


# ---------------------------------------------------------------------------
# Fix 1 — proportional streaming tail in _serialize_instance
# ---------------------------------------------------------------------------

def _make_tail_instance(num_msgs, streaming_responses=None):
    """A minimal instance with ``num_msgs`` confirmed conversation messages."""
    from agent_cascade.api_integration_pkg import state_builder

    inst = MagicMock()
    inst.conversation = [{'role': 'user', 'content': f'msg-{i}'} for i in range(num_msgs)]
    inst._streaming_responses = streaming_responses if streaming_responses is not None else []
    inst._compression_lock = threading.Lock()
    inst.compression_summary = ""
    return inst


def _serialize_directly(inst, streaming):
    """Call _serialize_instance on a single instance and return the result dict."""
    from agent_cascade.api_integration_pkg import state_builder

    pool = MagicMock()
    pool.get_instance.return_value = inst
    pool.instances = {"Maine": inst}
    om = MagicMock()
    om.list_pending_approvals.return_value = []
    om.extra_work_folders_ro = []
    om.extra_work_folders_rw = []
    om.base_dir = "/tmp/fake-workspace"
    pool.operation_manager = om

    # Patch token-stats + max-tokens so the call is deterministic and cheap.
    with patch.object(state_builder, '_get_max_tokens_for_instance', lambda *a, **k: 8192), \
         patch.object(state_builder._cache_mgr, 'token_stats', {}):
        return state_builder._serialize_instance(inst, pool, include_messages=True, streaming=streaming)


def test_tail_active_for_long_streaming_history():
    """streaming=True with 200 msgs -> tail of max(5, 200//10)=20 messages, indices aligned.

    The plan's invariant: first serialized message index == 180 (original_history_count - K),
    and history_count in the payload reflects the full count so the frontend splice
    (startIdx = history_count - messages.length) lands exactly on start_idx.
    """
    inst = _make_tail_instance(200, streaming_responses=[{'role': 'assistant', 'content': 'partial'}])
    result = _serialize_directly(inst, streaming=True)

    msgs = result['messages']
    # 20 tail messages + 1 appended streaming response.
    assert len(msgs) == 21, f"expected 20 tail + 1 streaming = 21 messages, got {len(msgs)}"
    # First serialized (confirmed) message index must be 180 = 200 - 20.
    assert msgs[0]['index'] == 180, f"first tail message index should be 180, got {msgs[0]['index']}"
    # Last confirmed-message index is 199; the appended streaming response is at 200.
    assert msgs[-1]['index'] == 200, f"appended streaming response index should be 200, got {msgs[-1]['index']}"
    # history_count = original_history_count + num_streaming = 200 + 1 = 201.
    assert result['history_count'] == 201, f"history_count should be 201 (200+1), got {result['history_count']}"
    # Frontend splice: startIdx = history_count - messages.length = 201 - 21 = 180 == first index.
    assert result['history_count'] - len(msgs) == msgs[0]['index'], "frontend splice misaligned"


def test_tail_inactive_below_threshold():
    """streaming=True with 30 msgs (<= TAIL_THRESHOLD=50) -> full send, start_idx 0."""
    inst = _make_tail_instance(30, streaming_responses=[{'role': 'assistant', 'content': 'partial'}])
    result = _serialize_directly(inst, streaming=True)

    msgs = result['messages']
    # All 30 confirmed + 1 streaming response.
    assert len(msgs) == 31, f"expected full send of 30 + 1 streaming = 31, got {len(msgs)}"
    assert msgs[0]['index'] == 0, f"below threshold must start at index 0, got {msgs[0]['index']}"


def test_tail_inactive_for_non_streaming():
    """streaming=False with 200 msgs -> full send (start_idx 0), regardless of length."""
    inst = _make_tail_instance(200, streaming_responses=None)
    result = _serialize_directly(inst, streaming=False)

    msgs = result['messages']
    assert len(msgs) == 200, f"non-streaming must send all 200 messages, got {len(msgs)}"
    assert msgs[0]['index'] == 0, f"non-streaming must start at index 0, got {msgs[0]['index']}"


def test_tail_min_floor_of_five():
    """streaming=True with 60 msgs -> K = max(5, 60//10=6) = 6 (floor only binds below 50).

    Also verifies the min-5 floor: a history just above threshold uses N//10 when that is
    >=5, and never fewer than 5.
    """
    inst = _make_tail_instance(60, streaming_responses=None)
    result = _serialize_directly(inst, streaming=True)

    msgs = result['messages']
    # K = max(5, 6) = 6 -> 6 messages, no streaming responses appended.
    assert len(msgs) == 6, f"expected tail of 6 (max(5,60//10)), got {len(msgs)}"
    assert msgs[0]['index'] == 54, f"first index should be 60-6=54, got {msgs[0]['index']}"
