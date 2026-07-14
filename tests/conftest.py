"""Shared pytest fixtures for the AgentCascade_unified test suite.

Provides local LLM auto-detection and fixtures so integration tests can run
against LM Studio / Ollama without external API keys when a server is available,
and skip cleanly (with clear messages) when not.
"""

import json
import os
import socket
import threading
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Local LLM Auto-Detection — session-scoped probe at test startup
# ---------------------------------------------------------------------------

# LM Studio runs on the host machine; from inside Docker containers we reach it
# via host.docker.internal.  On bare-metal / WSL hosts, localhost works too.
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "host.docker.internal")

# Endpoints to probe (ordered by preference)
_LOCAL_ENDPOINTS = [
    {
        "name": "LM Studio",
        "port": 1234,
        "path": "/v1/models",
        "model_type": "qwenvl_oai",
    },
    {
        "name": "Ollama",
        "port": 11434,
        "path": "/v1/models",
        "model_type": "qwenvl_oai",
    },
    {
        "name": "vLLM / generic",
        "port": 8000,
        "path": "/v1/models",
        "model_type": "qwenvl_oai",
    },
]

# Default lightweight models for testing (must be loaded on the server)
_DEFAULT_TEST_MODEL = "qwen/qwen3-4b-2507"   # fast general-purpose
_DEFAULT_VL_TEST_MODEL = "qwen/qwen3-vl-4b"  # vision + text


class _LocalLLMDetector:
    """Session-scoped detector that probes once and caches the result.

    Attributes
    ----------
    available : bool
        True when at least one local server responded with models.
    api_base : str | None
        Base URL of the first responsive endpoint (e.g. http://host.docker.internal:1234/v1).
    models : list[str]
        Full model ID list from that endpoint.
    name : str | None
        Human-readable server name ("LM Studio", "Ollama", …).
    """

    def __init__(self):
        self.available = False
        self.api_base: str | None = None
        self.models: list[str] = []
        self.name: str | None = None

    def probe(self, timeout: float = 5.0) -> bool:
        """Try each endpoint on each host; return True if one works."""
        for ep in _LOCAL_ENDPOINTS:
            for host in _LOCAL_HOSTS:
                url = f"http://{host}:{ep['port']}{ep['path']}"
                try:
                    import urllib.request
                    resp = urllib.request.urlopen(url, timeout=timeout)
                    data = json.loads(resp.read())
                    models = [m.get("id", m.get("name", ""))
                              for m in data.get("data", [])]
                    if models:
                        self.available = True
                        self.api_base = f"http://{host}:{ep['port']}/v1"
                        self.models = models
                        self.name = ep["name"]
                        return True
                except Exception:
                    continue
        return False


# Global detector instance — probed once at pytest_configure time
_local_llm_detector = _LocalLLMDetector()


def _find_text_model():
    """Find the best text model from detected local models.
    
    Priority order:
    1. Exact match for _DEFAULT_TEST_MODEL (prefer non-2507 variants if both exist)
    2. Any 'qwen3' or 'qwen2.5' model with 'vl' excluded (text-only models)
    3. First available non-embedding model as fallback
    """
    if not _local_llm_detector.available or not _local_llm_detector.models:
        return _DEFAULT_TEST_MODEL
    
    # Prefer exact match, but skip -2507 variants that are known to crash on LM Studio
    default = _DEFAULT_TEST_MODEL.replace('-2507', '')
    if default in _local_llm_detector.models:
        return default
    if _DEFAULT_TEST_MODEL in _local_llm_detector.models:
        return _DEFAULT_TEST_MODEL
    
    # Fallback: any model with 'qwen3' or 'qwen2.5' in name (text models, not VL)
    for m in _local_llm_detector.models:
        ml = m.lower()
        if 'vl' not in ml and ('qwen3' in ml or 'qwen2.5' in ml):
            return m
    
    # Last resort: first non-embedding model
    for m in _local_llm_detector.models:
        ml = m.lower()
        if 'embed' not in ml:
            return m
    
    return _DEFAULT_TEST_MODEL


def _find_vl_model():
    """Find the best VL model from detected local models.
    
    Priority order:
    1. Exact match for _DEFAULT_VL_TEST_MODEL
    2. Any model with 'vl' in its name (case-insensitive)
    3. Fall back to _DEFAULT_VL_TEST_MODEL if nothing found
    """
    if not _local_llm_detector.available or not _local_llm_detector.models:
        return _DEFAULT_VL_TEST_MODEL
    # Exact match first
    if _DEFAULT_VL_TEST_MODEL in _local_llm_detector.models:
        return _DEFAULT_VL_TEST_MODEL
    # Fallback: any model with 'vl' in name
    for m in _local_llm_detector.models:
        if 'vl' in m.lower():
            return m
    return _DEFAULT_VL_TEST_MODEL


def pytest_configure(config):
    """Auto-probe for local LLM servers when the test session starts."""
    if _local_llm_detector.probe():
        config.addinivalue_line(
            "markers",
            "skip_if_no_local: skip when no local LLM server is available",
        )
        print(f"\n[conftest] Local LLM found: {_local_llm_detector.name} "
              f"({_local_llm_detector.api_base}) — {len(_local_llm_detector.models)} models")
    else:
        config.addinivalue_line(
            "markers",
            "skip_if_no_local: skip when no local LLM server is available",
        )
        print("\n[conftest] No local LLM server detected — integration tests will be skipped")


def pytest_collection_modifyitems(config, items):
    """Skip tests marked 'skip_if_no_local' when no local server was found."""
    if not _local_llm_detector.available:
        skip_marker = pytest.mark.skip(reason="No local LLM server available (LM Studio / Ollama on localhost)")
        for item in items:
            if "skip_if_no_local" in item.keywords:
                item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Fixtures: local LLM configuration dicts
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def local_llm_available():
    """Return True if a local LLM server was detected at session start."""
    return _local_llm_detector.available


@pytest.fixture(scope="session")
def local_llm_api_base():
    """Base URL of the detected local LLM endpoint (e.g. http://host.docker.internal:1234/v1).

    Raises pytest.skip if no server was found.
    """
    if not _local_llm_detector.available:
        pytest.skip("No local LLM server available")
    return _local_llm_detector.api_base


@pytest.fixture(scope="session")
def local_llm_models():
    """List of model IDs available on the detected local endpoint."""
    if not _local_llm_detector.available:
        pytest.skip("No local LLM server available")
    return _local_llm_detector.models


@pytest.fixture
def local_llm_cfg(local_llm_api_base):
    """LLM config dict pointing to a lightweight text model on the local server.

    Use this fixture in integration tests instead of hardcoding DashScope / OpenAI keys.
    Example::

        def test_chat(local_llm_cfg):
            llm = get_chat_model(local_llm_cfg)
            response = llm.chat(messages=[Message('user', 'hello')])
    """
    return {
        "model": _find_text_model(),
        "model_server": local_llm_api_base,
        "api_key": "EMPTY",
        "model_type": "qwenvl_oai",
    }


@pytest.fixture
def local_vl_llm_cfg(local_llm_api_base):
    """LLM config dict pointing to a vision+text model on the local server.

    Use this for tests that need multimodal capabilities (image understanding).
    """
    return {
        "model": _find_vl_model(),
        "model_server": local_llm_api_base,
        "api_key": "EMPTY",
        "model_type": "qwenvl_oai",
    }


@pytest.fixture
def local_llm_cfg_with_retry(local_llm_cfg):
    """Like local_llm_cfg but with relaxed retry settings for CI environments."""
    cfg = dict(local_llm_cfg)
    cfg.setdefault("generate_cfg", {})["max_retries"] = 2
    return cfg


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