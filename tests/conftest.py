"""Shared pytest fixtures for the AgentCascade_unified test suite."""

import threading
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures: tool_utils tests
# ---------------------------------------------------------------------------

class _FakeAgentPool:
    """Minimal fake agent pool with a last_tool_args dict for testing."""

    def __init__(self):
        self.last_tool_args = {}


@pytest.fixture
def agent_pool():
    """A fresh fake AgentPool with an empty last_tool_args cache."""
    return _FakeAgentPool()


@pytest.fixture
def pool_with_tool_args(agent_pool):
    """AgentPool pre-populated with tool args for two tools."""
    agent_pool.last_tool_args = {
        "session1": {
            "write_file": {"file_path": "/tmp/out.txt", "content": "hello"},
            "__GLOBAL__": {"common_key": "shared_value"},
        },
    }
    return agent_pool


@pytest.fixture
def lock():
    """A fresh threading.Lock for tests that want thread-safe resolution."""
    return threading.Lock()


# ---------------------------------------------------------------------------
# Fixtures: token_cache tests
# ---------------------------------------------------------------------------

@pytest.fixture
def short_ttl_cache():
    """AgentTokenCache with 1-second TTL so tests don't have to wait."""
    from config.token_cache import AgentTokenCache

    cache = AgentTokenCache(ttl=1)
    # Cancel the background cleanup timer so tests are deterministic
    if cache._cleanup_timer is not None:
        cache._cleanup_timer.cancel()
    return cache


@pytest.fixture
def normal_ttl_cache():
    """AgentTokenCache with default 300-second TTL."""
    from config.token_cache import AgentTokenCache

    cache = AgentTokenCache(ttl=300)
    if cache._cleanup_timer is not None:
        cache._cleanup_timer.cancel()
    return cache


# ---------------------------------------------------------------------------
# Fixtures: feature_flags tests
# ---------------------------------------------------------------------------

@pytest.fixture
def env_patch(monkeypatch):
    """Convenience wrapper to set an env var, yield, then unset it."""
    def _set(name, value):
        monkeypatch.setenv(name, value)
    return _set


@pytest.fixture
def clear_feature_env_vars(monkeypatch):
    """Ensure all feature-flag env vars are unset before the test."""
    for var in ("AC_USE_UNIFIED_STATE", "AC_USE_UNIFIED_LOOP"):
        monkeypatch.delenv(var, raising=False)