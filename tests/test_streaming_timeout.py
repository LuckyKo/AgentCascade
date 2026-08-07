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


# ── Settings constants tests ──────────────────────────────────────────────────


def test_streaming_timeout_settings_defaults():
    """Test that our new settings constants are defined with reasonable defaults."""
    from agent_cascade.settings import (
        STREAM_MAX_SILENCE_SECONDS,
        STREAM_MAX_TOTAL_SECONDS,
        HTTP_READ_TIMEOUT,
        HTTP_CONNECT_TIMEOUT,
    )
    assert 60 <= STREAM_MAX_SILENCE_SECONDS <= 300   # reasonable range
    assert 120 <= STREAM_MAX_TOTAL_SECONDS <= 1800
    assert HTTP_READ_TIMEOUT > 5.0                    # must differ from httpx default
    assert HTTP_CONNECT_TIMEOUT >= 5.0


# ── PoolSettings tests ────────────────────────────────────────────────────────


def test_pool_settings_streaming_timeout_fields():
    """Test that PoolSettings has the streaming timeout fields with correct defaults."""
    from agent_cascade.agent_instance import PoolSettings
    from agent_cascade.settings import STREAM_MAX_SILENCE_SECONDS, STREAM_MAX_TOTAL_SECONDS

    ps = PoolSettings()
    assert hasattr(ps, 'stream_max_silence_seconds')
    assert hasattr(ps, 'stream_max_total_seconds')
    assert ps.stream_max_silence_seconds == STREAM_MAX_SILENCE_SECONDS
    assert ps.stream_max_total_seconds == STREAM_MAX_TOTAL_SECONDS


# ── Retry policy tests ────────────────────────────────────────────────────────


def test_retry_policy_stream_stalled():
    """Test that our new error pattern is classified as retryable."""
    from agent_cascade.retry_policy import classify_error

    assert classify_error(Exception("stream_stalled: no data")) == "retryable"
    assert classify_error(Exception("stream_silence_timeout")) == "retryable"  # matches 'timeout'


# ── Server startup smoke test ─────────────────────────────────────────────────


def test_server_startup_smoke():
    """Smoke test: verify create_app doesn't crash on startup with minimal config."""
    from agent_cascade.api_server import create_app
    from agent_cascade.agent_pool import AgentPool
    from agent_cascade.agent_factory import load_orchestrator_agent

    llm_cfg = {
        "model": "test_model",
        "model_server": "http://localhost:1234/v1",
        "api_key": "EMPTY",
        "model_type": "qwenvl_oai",
        "max_input_tokens": 8192,
    }

    # Just creating the pool and app should not raise
    pool = AgentPool(llm_cfg)
    orchestrator = load_orchestrator_agent(pool, llm_cfg)
    agents = [orchestrator]
    app = create_app(agents=agents, agent_pool=pool, config={"session_name": "TestStartup"})

    assert app is not None
    # PoolSettings should have our new fields accessible
    assert hasattr(pool.settings, 'stream_max_silence_seconds')


# ── Module import smoke tests ─────────────────────────────────────────────────


def test_oai_module_imports_cleanly():
    """Smoke test that the OpenAI module can be imported without errors (verifies no circular imports)."""
    from agent_cascade.llm import oai
    assert hasattr(oai, '_get_cached_client')
    assert hasattr(oai, 'flush_client_cache')


def test_dashscope_modules_import_cleanly():
    """Smoke tests for DashScope backends."""
    from agent_cascade.llm import qwen_dashscope
    from agent_cascade.llm import qwenvl_dashscope
    # Just verifying clean imports — our changes added time imports and watch_stream usage


# ── Config handler registration tests ─────────────────────────────────────────


def test_config_handlers_registered():
    """Test that the config handler system recognizes our new settings."""
    from agent_cascade.config_handlers import POOL_SETTINGS_KEYS
    assert 'stream_max_silence_seconds' in POOL_SETTINGS_KEYS
    assert 'stream_max_total_seconds' in POOL_SETTINGS_KEYS