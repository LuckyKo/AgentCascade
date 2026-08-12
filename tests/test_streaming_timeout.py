"""Regression tests for the streaming timeout fix (todo.md #86).

Tests cover:
- watch_stream() utility behavior (silence/total timeouts, first-item delay)
- Settings constants and defaults
- PoolSettings fields
- Retry policy classification
- Server startup smoke test
- Module import smoke tests
- Config handler registration
"""

import time

import pytest

# ── watch_stream utility tests ────────────────────────────────────────────────


def test_watch_stream_normal():
    """Test that watch_stream yields items correctly when they arrive within timeout limits."""
    from agent_cascade.utils.streaming import watch_stream

    def fast_gen():
        for i in range(5):
            yield i

    result = list(watch_stream(fast_gen(), max_silence_seconds=10.0, max_total_seconds=60.0))
    assert result == [0, 1, 2, 3, 4]


def test_watch_stream_silence_timeout():
    """Test that RuntimeError is raised when silence exceeds max_silence_seconds (after first item)."""
    from agent_cascade.utils.streaming import watch_stream

    def slow_gen():
        yield "first"          # first item — no silence check yet
        time.sleep(0.3)        # gap exceeds max_silence
        yield "second"

    with pytest.raises(RuntimeError, match="stream_stalled"):
        list(watch_stream(slow_gen(), max_silence_seconds=0.1, max_total_seconds=60.0))


def test_watch_stream_total_timeout():
    """Test that RuntimeError is raised when total duration exceeds max_total_seconds."""
    from agent_cascade.utils.streaming import watch_stream

    def slow_gen():
        yield "first"
        time.sleep(0.3)
        yield "second"

    with pytest.raises(RuntimeError, match="stream_stalled.*total"):
        list(watch_stream(slow_gen(), max_silence_seconds=60.0, max_total_seconds=0.1))


def test_watch_stream_first_item_delay_allowed():
    """Test that a long delay before the first item does NOT trigger silence timeout (slow reasoning models)."""
    from agent_cascade.utils.streaming import watch_stream

    def slow_first_gen():
        time.sleep(0.3)        # long delay before first item
        yield "first"
        yield "second"         # quick follow-up

    # Should NOT raise: first-item delay is allowed even if > max_silence
    result = list(watch_stream(slow_first_gen(), max_silence_seconds=0.1, max_total_seconds=60.0))
    assert result == ["first", "second"]

