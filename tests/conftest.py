"""Shared pytest fixtures for the AgentCascade_unified test suite."""

import threading
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures: tool_utils tests
# ---------------------------------------------------------------------------

class _FakeCachePool:
    """Minimal fake cache pool for testing (mimics ArgumentCachePool)."""

    def __init__(self):
        self.enabled = True
        self._entries = {}  # index -> value

    def get(self, n):
        return self._entries.get(n)

    def add(self, kind, name, label, value):
        idx = len(self._entries) + 1
        entry = type('Entry', (), {'value': value})()
        self._entries[idx] = entry
        return idx


class _FakeInstance:
    """Minimal fake instance with a cache_pool."""

    def __init__(self):
        self.cache_pool = _FakeCachePool()


class _FakeAgentPool:
    """Minimal fake agent pool with an instance_conversations map for testing."""

    def __init__(self):
        self.instance_conversations = {}


@pytest.fixture
def agent_pool():
    """A fresh fake AgentPool with an empty instance_conversations map."""
    return _FakeAgentPool()


def _seed_cache_pool(pool, scope, values):
    """Helper: seed the cache pool for a scope with key-value pairs.
    Each value gets a sequential index. Returns the pool."""
    if scope not in pool.instance_conversations:
        pool.instance_conversations[scope] = _FakeInstance()
    cp = pool.instance_conversations[scope].cache_pool
    for v in values:
        cp.add("arg", scope, "test", v)
    return pool


@pytest.fixture
def pool_with_tool_args(agent_pool):
    """AgentPool pre-populated with cached entries for resolution tests."""
    _seed_cache_pool(agent_pool, "session1", [
        {"file_path": "/tmp/out.txt", "content": "hello"},
        {"common_key": "shared_value"},
    ])
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