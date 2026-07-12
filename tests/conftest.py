"""Shared pytest fixtures for the AgentCascade_unified test suite."""

import threading
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures: tool_utils tests (lightweight cache-pool fakes)
# ---------------------------------------------------------------------------

class _FakeCachePool:
    """Minimal fake cache pool for testing (mimics ArgumentCachePool)."""

    def __init__(self):
        self.enabled = True
        self._entries = {}  # index -> value

    def get(self, n):
        return self._entries.get(n)

    def add(self, kind, label, value, threshold=0):
        idx = len(self._entries) + 1
        entry = type('Entry', (), {'value': value})()
        self._entries[idx] = entry
        return idx


class _FakeInstance:
    """Minimal fake instance with a cache_pool.

    Used by tool_utils fixtures (pool_with_tool_args). For compression tests,
    prefer the richer `MockAgentPool` / `MockInstance` classes below.
    """

    def __init__(self):
        self.cache_pool = _FakeCachePool()


class _FakeAgentPool:
    """Minimal fake agent pool with an instance_conversations map for testing."""

    def __init__(self):
        self.instance_conversations = {}


# ---------------------------------------------------------------------------
# Fixtures: compression tests (MockInstance, MockAgentPool)
# ---------------------------------------------------------------------------

class _CompressionLock:
    """No-op context manager mimicking threading.RLock for compression tests.

    NOTE: Provides NO actual thread safety – a placeholder to satisfy the RLock-like
    interface in single-threaded unit tests. It simply passes through __enter__/__exit__.
    """

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class MockInstance:
    """Mock AgentInstance with compression lock and conversation sync.

    All state attributes are per-instance (not shared across mocks) to match
    production AgentInstance behavior and ensure test isolation.

    Attributes:
        conversation              – Current message list (list of Message).
        _compression_lock         – RLock-like guard for concurrent compressions.
        _cached_token_count       – Token count cache (reset on rebuild).
        _last_token_count_conversation_length  – Length at last token-count call.
        _streaming_responses      – Accumulated streaming responses.
        _pending_notifications    – Pending notification queue.
        _tool_warnings            – Tool warning list.
        agent_class               – Agent class name (default "coder").
    """

    def __init__(self, history):
        self.conversation = list(history)
        # Per-instance lock (not shared across MockInstance objects)
        self._compression_lock = _CompressionLock()
        # Token-count cache state – per instance like production AgentInstance
        self._cached_token_count = 0
        self._last_token_count_conversation_length = -1
        self._streaming_responses: list = []
        self._pending_notifications: list = []
        self._tool_warnings: list = []
        self.agent_class = "coder"

    def rebuild_conversation(self, new_history: list) -> None:
        """Replace conversation with new history (mimics AgentInstance.rebuild_conversation).

        Resets both token-count cache fields to match production behavior.
        """
        self.conversation = list(new_history)
        self._cached_token_count = 0
        self._last_token_count_conversation_length = -1


class _MockInstanceConversationMapping(dict):
    """Custom dict that syncs writes to instance.conversation.

    Mirrors agent_cascade.agent_pool._InstanceConversationMapping behavior:
    reads come from instances[name].conversation, writes propagate back via
    rebuild_conversation().
    """

    def __init__(self, pool):
        super().__init__()
        self._pool = pool

    def __getitem__(self, key: str) -> list:
        inst = self._pool.instances.get(key)
        if inst is not None:
            return list(inst.conversation)
        try:
            return super().__getitem__(key)
        except KeyError:
            raise KeyError(key)

    def __setitem__(self, key: str, value: list) -> None:
        inst = self._pool.instances.get(key)
        if inst is not None:
            inst.rebuild_conversation(list(value))
        super().__setitem__(key, value)


class MockAgentPool:
    """Lightweight AgentPool mock for compression tests.

    Avoids the heavy real AgentPool (DB/file deps) while providing correct
    behavior for get_compression_target_set, find_last_marker, pool mutation,
    and Compressor agent retrieval.

    Args:
        history:         Initial conversation messages (list of Message). Defaults to [].
        instance_name:   Instance key in the pool (default "TestAgent").
    """

    def __init__(self, history=None, instance_name="TestAgent"):
        self.instance_name = instance_name
        initial_history = list(history) if history else []
        mock_inst = MockInstance(initial_history)
        self.instances = {instance_name: mock_inst}
        self.instance_conversations = _MockInstanceConversationMapping(self)
        self.instance_conversations[instance_name] = mock_inst.conversation
        self.instance_loggers: dict = {}
        self.instance_summaries: dict = {}

    def get_conversation(self, agent_name):
        """Read from instance.conversation (Phase 3 pattern)."""
        inst = self.instances.get(agent_name)
        if inst is not None:
            return list(inst.conversation)
        return []

    def get_instance(self, agent_name):
        """Return a mock instance if it exists."""
        return self.instances.get(agent_name)

    @staticmethod
    def find_last_marker(history):
        """Same logic as AgentPool.find_last_marker."""
        from agent_cascade.prompts.dna import COMPRESSION_MARKER
        from agent_cascade.llm.schema import USER

        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                return i
        return -1

    def get_compression_target_set(self, agent_name):
        """Same logic as AgentPool.get_compression_target_set."""
        history = self.get_conversation(agent_name)
        if not history:
            return 0, [], -1
        first_role = (history[0].get('role') if isinstance(history[0], dict)
                      else getattr(history[0], 'role', ''))
        start_idx = 2 if first_role == SYSTEM else 1
        latest_marker = self.find_last_marker(history)
        active_start = latest_marker + 1 if latest_marker >= 0 else start_idx
        return active_start, history[active_start:], latest_marker

    def get_compression_target_set_from_conversation(self, instance_name: str, conv):
        """Same logic as AgentPool.get_compression_target_set_from_conversation.

        Uses module-level SYSTEM constant (imported at top of conftest).
        """
        if not conv:
            return 0, [], -1
        latest_marker = self.find_last_marker(conv)
        if latest_marker >= 0:
            active_start_idx = latest_marker + 1
        else:
            first_role = (conv[0].get('role') if isinstance(conv[0], dict)
                          else getattr(conv[0], 'role', ''))
            active_start_idx = 2 if first_role == SYSTEM else 1
        active_set = conv[active_start_idx:]
        return active_start_idx, active_set, latest_marker

    def get_agent(self, name: str):
        """Return a mock Compressor agent (needed by compress_context)."""
        if name == 'Compressor':
            fake = type('FakeAgent', (), {})()
            fake.llm = type('LLM', (), {'generate_cfg': {}})()
            return fake
        return None


# Re-export SYSTEM constant for use in get_compression_target_set
from agent_cascade.llm.schema import SYSTEM  # noqa: E402 (used by MockAgentPool above)


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
        cp.add("arg", "test", v)
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