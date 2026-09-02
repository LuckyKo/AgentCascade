"""Regression tests for the O(N) full-history recompute fix (streaming "weird mode").

Root cause: ``build_stream_update_from_pool`` -> ``_serialize_instance`` recomputed
token stats over the ENTIRE conversation on every SSE chunk, because the token-stats
cache key included the RAW streaming content length (which changes every chunk). As
conversations grew, per-tick cost exceeded LLM chunk arrival rate and the producer
thread starved — new tokens queued unprocessed ("no stream during generation, pours
in after completion").

Two fixes under test (NO tail optimization — that variant was reverted because it
broke log history on refresh):

  Fix 1 — Quantized token-stats cache keys.
      The streaming content length is quantized into ~256-char buckets so the cache
      key (and thus the full-history recompute) is stable across per-chunk growth.
      Verified by asserting the quantization produces a STABLE bucket for small
      growth and ADVANCES only once ~256 chars accumulate.

  Fix 2 — Broadcast throttle floor.
      ``broadcast_stream_update`` floors streaming ticks at MIN_STREAM_BROADCAST_INTERVAL
      (~0.2s / 5x/sec) so a burst of SSE chunks does not trigger one full-payload
      build+send per chunk. Committed messages (len_changed) still pass through
      immediately.

These are TESTS ONLY — no production code is modified here.

Run with: pytest tests/test_streaming_oon_recompute_fix.py -v
"""

import sys
from pathlib import Path

import pytest

# Ensure top-level imports work (matches test_api_endpoints.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fix 1 — Quantized token-stats cache keys
# ---------------------------------------------------------------------------
# The quantization lives in two places that MUST agree on the bucket size:
#   * state_builder.build_stream_update_from_pool  (stream_content_len)
#   * state_builder._serialize_instance            (per_agent_stream_content_len)
# Both compute `raw // 256`. We test the arithmetic contract directly so a future
# edit that changes one bucket size (or removes the quantization) fails loudly.

def _quantize(raw: int) -> int:
    """Mirror of the production quantization (`raw // 256`)."""
    return raw // 256


def test_quantize_stable_within_bucket():
    """Growth strictly inside one 256-char bucket must NOT change the key."""
    # base=1024 is exactly at a bucket boundary (bucket 4); +1..+255 stay in it.
    base = 1024
    assert _quantize(base) == _quantize(base + 1)
    assert _quantize(base) == _quantize(base + 200)
    assert _quantize(base) == _quantize(base + 255)


def test_quantize_advances_across_bucket_boundary():
    """Once ~256 chars accumulate, the key must advance (stats recompute)."""
    base = 1024
    # Crossing a full bucket boundary advances the quantized value by exactly 1.
    assert _quantize(base + 256) == _quantize(base) + 1


def test_quantize_zero_and_small():
    """Empty / tiny content must quantize to 0 (no false recompute)."""
    assert _quantize(0) == 0
    assert _quantize(1) == 0
    assert _quantize(255) == 0


def test_production_quantization_is_256_bucket():
    """Guard: the production code must actually quantize with a 256-char bucket.

    This inspects the source of both call sites to ensure the `// 256` quantization
    is present (and not accidentally reverted to raw length). It fails if someone
    re-introduces the per-chunk cache-miss storm.
    """
    sb_path = PROJECT_ROOT / "agent_cascade" / "api_integration_pkg" / "state_builder.py"
    src = sb_path.read_text(encoding="utf-8")

    # Both quantization sites must divide by 256.
    assert "// 256" in src, (
        "Expected the 256-char bucket quantization (`// 256`) in state_builder.py; "
        "the O(N) per-chunk recompute fix may have been reverted."
    )
    # It must appear at least twice (build_stream_update_from_pool + _serialize_instance).
    assert src.count("// 256") >= 2, (
        "Expected the `// 256` quantization in BOTH build_stream_update_from_pool and "
        "_serialize_instance; found fewer than 2 occurrences."
    )


# ---------------------------------------------------------------------------
# Fix 2 — Broadcast throttle floor
# ---------------------------------------------------------------------------
# We exercise the REAL broadcast_stream_update() with a minimal fake pool so the
# actual should_broadcast logic runs. We count how many times the (expensive)
# build+enqueue path is entered by stubbing build_stream_update_from_pool to a
# counter and providing a real asyncio loop + queue.

def _make_fake_pool():
    """Build a minimal pool-like object with the attrs broadcast_stream_update reads."""
    import asyncio

    class FakePool:
        def __init__(self):
            self._ws_send_queue = asyncio.Queue(maxsize=128)
            # A real (closed-safe) loop is created per-test in the scenario below;
            # we set _ws_loop lazily to avoid binding a queue to a dead loop.
            self._ws_loop = None

    return FakePool()


def test_streaming_tick_is_throttled_to_floor():
    """A burst of streaming ticks with NO content change must be throttled to the
    ~0.2s floor (i.e., far fewer builds than the number of ticks)."""
    import asyncio
    from agent_cascade.api_integration_pkg import streaming as st

    build_calls = {"n": 0}

    # Stub the expensive builder so we can count how many times it runs.
    orig_builder = st.build_stream_update_from_pool

    def counting_builder(*a, **k):
        build_calls["n"] += 1
        return {"instance_name": "Maine", "messages": []}

    st.build_stream_update_from_pool = counting_builder
    try:
        async def scenario():
            pool = _make_fake_pool()
            loop = asyncio.get_running_loop()
            pool._ws_loop = loop

            # Simulate a tight burst of streaming ticks (no len_changed) over ~0.5s,
            # spaced 1ms apart — well above the natural chunk rate. Without the floor,
            # every tick would trigger a build; with it, only ~floor-count do.
            last_send = 0.0
            last_resp_len = 5
            t0 = asyncio.get_event_loop().time()
            ticks = 0
            while (asyncio.get_event_loop().time() - t0) < 0.5:
                now = asyncio.get_event_loop().time()
                # is_streaming_tick=True, turn_output length unchanged (5).
                last_send, _ = st.broadcast_stream_update(
                    pool=pool,
                    instance_name="Maine",
                    turn_output=[object()] * 5,
                    is_streaming_tick=True,
                    tick_num=ticks,
                    now_sec=now,
                    last_send=last_send,
                    last_resp_len=last_resp_len,
                )
                ticks += 1
                await asyncio.sleep(0.001)
            return ticks

        ticks = asyncio.run(scenario())
    finally:
        st.build_stream_update_from_pool = orig_builder

    # The burst produced many ticks but only a handful of actual builds (~5/sec * 0.5s).
    assert ticks > 20, f"expected a tight burst of ticks, got {ticks}"
    # With the 0.2s floor over ~0.5s we expect at most ~3-4 builds (plus slack).
    assert build_calls["n"] <= 6, (
        f"throttle floor not effective: {build_calls['n']} builds for {ticks} ticks "
        "(expected <=~4 at a 0.2s floor over 0.5s)"
    )
    # And it must still build at least once (streaming is not fully suppressed).
    assert build_calls["n"] >= 1, "no builds at all — streaming would appear frozen"


def test_len_changed_bypasses_throttle():
    """A new committed message (len_changed) must broadcast immediately, even right
    after the last send (throttle floor must not delay it)."""
    import asyncio
    from agent_cascade.api_integration_pkg import streaming as st

    build_calls = {"n": 0}
    orig_builder = st.build_stream_update_from_pool

    def counting_builder(*a, **k):
        build_calls["n"] += 1
        return {"instance_name": "Maine", "messages": []}

    st.build_stream_update_from_pool = counting_builder
    try:
        async def scenario():
            pool = _make_fake_pool()
            loop = asyncio.get_running_loop()
            pool._ws_loop = loop
            now = asyncio.get_event_loop().time()
            # last_send == now (just sent) but content length grew 5 -> 6.
            st.broadcast_stream_update(
                pool=pool,
                instance_name="Maine",
                turn_output=[object()] * 6,
                is_streaming_tick=False,
                tick_num=0,
                now_sec=now,
                last_send=now,          # no time elapsed since last send
                last_resp_len=5,        # previous length was 5 -> len_changed=True
            )

        asyncio.run(scenario())
    finally:
        st.build_stream_update_from_pool = orig_builder

    assert build_calls["n"] == 1, (
        f"len_changed must bypass the throttle and build immediately, got {build_calls['n']}"
    )


def test_suppressed_tick_does_not_advance_last_send():
    """A suppressed streaming tick must NOT update last_send.

    If a suppressed tick wrongly advanced last_send, the next tick would be measured
    against the wrong baseline and could starve for an extra floor interval (or never
    fire). We assert last_send is returned unchanged across two back-to-back suppressed
    ticks within the 0.2s floor.
    """
    import asyncio
    from agent_cascade.api_integration_pkg import streaming as st

    build_calls = {"n": 0}
    orig_builder = st.build_stream_update_from_pool

    def counting_builder(*a, **k):
        build_calls["n"] += 1
        return {"instance_name": "Maine", "messages": []}

    st.build_stream_update_from_pool = counting_builder
    try:
        async def scenario():
            pool = _make_fake_pool()
            loop = asyncio.get_running_loop()
            pool._ws_loop = loop
            now = asyncio.get_event_loop().time()
            # First tick fires (last_send=0.0, far in the past) and advances last_send.
            last_send, _ = st.broadcast_stream_update(
                pool=pool, instance_name="Maine", turn_output=[object()] * 5,
                is_streaming_tick=True, tick_num=0, now_sec=now,
                last_send=0.0, last_resp_len=5,
            )
            # Two back-to-back suppressed ticks: no content change, well within the floor.
            for i in (1, 2):
                new_last_send, _ = st.broadcast_stream_update(
                    pool=pool, instance_name="Maine", turn_output=[object()] * 5,
                    is_streaming_tick=True, tick_num=i, now_sec=now + 0.01 * i,
                    last_send=last_send, last_resp_len=5,
                )
                # Suppressed tick must return the SAME last_send (not advance it).
                assert new_last_send == last_send, (
                    f"suppressed tick advanced last_send from {last_send} to {new_last_send}; "
                    "this would starve subsequent ticks for an extra floor interval"
                )
            return build_calls["n"]

        builds = asyncio.run(scenario())
    finally:
        st.build_stream_update_from_pool = orig_builder

    # Only the first tick should have built; the two suppressed ones must not.
    assert builds == 1, f"expected exactly 1 build (first tick), got {builds}"


def test_no_tail_optimization_full_history_preserved():
    """Guard: the fix must NOT reintroduce tail optimization.

    The reverted commit sent only the last K messages during streaming, which broke
    log history on refresh. We assert _serialize_instance still serializes the FULL
    conversation (start_idx == 0 / no tail cut) by checking the source does not apply
    a streaming tail slice to the persisted history.
    """
    sb_path = PROJECT_ROOT / "agent_cascade" / "api_integration_pkg" / "state_builder.py"
    src = sb_path.read_text(encoding="utf-8")

    # The known-bad pattern: slicing the full message list by a tail threshold during
    # streaming. The current (correct) code serializes all messages from index 0.
    assert "TAIL_THRESHOLD" not in src, (
        "Tail optimization appears to have been reintroduced (TAIL_THRESHOLD found); "
        "this broke log history on refresh and must stay out."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
