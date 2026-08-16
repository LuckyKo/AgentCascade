"""Pytest configuration for AgentCascade test suite.

Provides test isolation fixtures so tests never touch production config files,
and local LLM auto-detection so integration tests can run against LM Studio /
Ollama without external API keys when a server is available, and skip cleanly
(with clear messages) when not.
"""

import json
import os
from typing import Any, Dict, List, Optional
import pytest


# ---------------------------------------------------------------------------
# Test isolation: unique instance ID + no console windows (set at IMPORT time)
# ---------------------------------------------------------------------------
# AGENT_CASCADE_INSTANCE_ID drives log/telemetry/console.log directory
# isolation (agent_cascade/instance_id.py).  Without it, AgentPools fall back
# to the production workspace and pollute the live zone.  We set a unique
# value per process so parallel xdist workers never collide.  The value must
# match ^[a-zA-Z0-9_]+$ (max 64 chars).
#
# Derivation: prefer the xdist worker id ("gw0", "gw1", ...) when present;
# otherwise fall back to the PID.  pytest-xdist forks workers after collection,
# so PYTEST_XDIST_WORKER is already set by the time this module is imported in
# each worker — giving every worker a distinct value from import time on.
def _derive_test_instance_id() -> str:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    if worker:
        suffix = f"test_{worker}"
    else:
        suffix = f"test_pid{os.getpid()}"
    return suffix[:64]


os.environ["AGENT_CASCADE_INSTANCE_ID"] = _derive_test_instance_id()

# Never pop a visible cmd window for async shell_cmd in tests.  This env var is
# an opt-out override read by agent_cascade/tools/custom/shell_cmd.py; it does
# NOT change production defaults (which still honor the pool toggle).
os.environ["QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW"] = "1"


from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.llm.schema import SYSTEM, USER


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

class MockInstance:
    """Minimal mock of AgentInstance for compression tests."""

    def __init__(self, conversation: List[Any]):
        self.conversation = list(conversation)


class _MockInstanceConversationMapping(Dict[str, List[Any]]):
    """Dict subclass that syncs writes to instances[name].conversation immediately.

    Mirrors production AgentPool.instance_conversations behavior: the mapping is
    a view onto instance.conversation lists, not separate storage.
    """

    def __init__(self, pool: "MockAgentPool"):
        super().__init__()
        self._pool = pool

    def __getitem__(self, key: str) -> List[Any]:
        inst = self._pool.instances.get(key)
        if inst is None:
            return []
        return list(inst.conversation)

    def __setitem__(self, key: str, value: List[Any]) -> None:
        inst = self._pool.instances.get(key)
        if inst is not None:
            inst.conversation[:] = list(value)

    def __contains__(self, key: object) -> bool:
        return key in self._pool.instances


class MockAgentPool:
    """Mock AgentPool for compression tests.

    ⚠️ WARNING: This is a SIMULATION, not the real AgentPool. It implements only
    the subset of methods used by compress_context and helpers. If production code
    changes its target-set calculation or marker detection logic, this mock must be
    updated in parallel — divergence will go undetected because tests validate against
    the mock's behavior, not the real pool's.

    For higher-fidelity compression testing that uses the actual AgentPool, see
    test_compression_no_duplication.py which runs against production code.

    Method correspondence with production (agent_pool.py):
    - get_compression_target_set_from_conversation() → lines 2125-2142
    - find_last_marker() → lines 2514-2527
    """

    def __init__(self, history: Optional[List[Any]] = None):
        self.instance_name: str = "TestAgent"
        self.instances: Dict[str, MockInstance] = {}
        self.instance_conversations = _MockInstanceConversationMapping(self)

        # Create TestAgent instance with the provided history
        if history is not None:
            self.instances["TestAgent"] = MockInstance(history)

    @staticmethod
    def _msg_field(msg: Any, field: str, default: Any = "") -> Any:
        """Extract a field from a message (dict or Message object)."""
        return msg.get(field, default) if isinstance(msg, dict) else getattr(msg, field, default)

    def get_conversation(self, agent_name: str) -> List[Any]:
        """Get the conversation list for an agent. Returns empty list if not found."""
        inst = self.instances.get(agent_name)
        if inst is None:
            return []
        return list(inst.conversation)

    def get_compression_target_set(self, instance_name: str):
        """Returns (active_start_idx, active_set, latest_summary_idx) for compression.

        Delegates to get_compression_target_set_from_conversation after fetching conv.
        Matches production logic from agent_pool.py:2093-2123.
        """
        conv = self.get_conversation(instance_name)
        if not conv:
            return 0, [], -1

        return self.get_compression_target_set_from_conversation(instance_name, conv)

    def get_compression_target_set_from_conversation(
        self, instance_name: str, conv: List[Any]
    ):
        """Like get_compression_target_set but accepts a pre-fetched conversation snapshot.

        Matches production logic from agent_pool.py:2125-2142:
        - Skip SYSTEM+U0 unless a compression marker exists.
        - When marker exists, active starts right after it.
        """
        if not conv:
            return 0, [], -1

        latest_marker = self.find_last_marker(conv)

        if latest_marker >= 0:
            active_start_idx = latest_marker + 1
        else:
            # Skip system message at index 0 AND first user message (U0)
            first_role = self._msg_field(conv[0], "role")
            active_start_idx = 2 if first_role == SYSTEM else 1

        active_set = conv[active_start_idx:]
        return active_start_idx, active_set, latest_marker

    @staticmethod
    def find_last_marker(history: List[Any]) -> int:
        """Find the index of the last COMPRESSION_MARKER message in a conversation.

        Only considers messages with role=USER (compression markers are user messages).
        Returns -1 if no marker is found.
        Matches production logic from agent_pool.py:2514-2527.
        """
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            role = MockAgentPool._msg_field(msg, "role")
            content = MockAgentPool._msg_field(msg, "content")
            # Only consider USER messages (compression markers are always user role)
            if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                return i
        return -1

    def get_agent(self, name: str):
        """Return None — tests mock invoke_compression_agent directly.

        Returning a MagicMock for Compressor causes issues because core.py
        inspects comp_agent.llm.generate_cfg.get('max_input_tokens'), and
        MagicMock returns another MagicMock which breaks numeric comparisons.
        """
        return None

    def load_agent(self, name: str) -> None:
        """No-op — tests mock invoke_compression_agent directly."""
        pass


# ---------------------------------------------------------------------------
# Fixtures: isolated config directory
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def isolated_config_dir(tmp_path_factory):
    """Set AGENT_CASCADE_TEST_CONFIG_DIR to a temp directory for the entire test session.

    This ensures all APIRouter instances created during tests use an isolated config
    directory instead of the production project-root/config directory.
    """
    test_config = tmp_path_factory.mktemp("test_config")
    os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = str(test_config)
    yield test_config
    # Cleanup not strictly necessary (tmp_path handles it), but explicit is clear
    os.environ.pop("AGENT_CASCADE_TEST_CONFIG_DIR", None)


@pytest.fixture(scope="session", autouse=True)
def isolated_instance_id():
    """Set a UNIQUE AGENT_CASCADE_INSTANCE_ID per parallel worker for the test session.

    WHY: agent_cascade/instance_id.py uses this env var to suffix log/telemetry/console.log
    directories.  When empty, AgentPools fall back to the production workspace and pollute
    the live zone; when shared across xdist workers, parallel runs collide on the same files.
    Setting a per-worker value here (before any pool/logger is constructed) keeps each worker's
    logs isolated in its own instance directory and leaves the production zone untouched.

    The value is also set at module import time (see top of this file); this fixture re-derives
    it so it stays correct even if the env var was unset/cleared by the time fixtures run.
    """
    os.environ["AGENT_CASCADE_INSTANCE_ID"] = _derive_test_instance_id()
    yield os.environ["AGENT_CASCADE_INSTANCE_ID"]


# ---------------------------------------------------------------------------
# Fixtures: token_cache tests
# ---------------------------------------------------------------------------

@pytest.fixture
def short_ttl_cache():
    """AgentTokenCache with a 1-second TTL for fast expiration tests."""
    from agent_cascade.utils.token_cache import AgentTokenCache
    return AgentTokenCache(ttl=1)


@pytest.fixture
def normal_ttl_cache():
    """AgentTokenCache with the default 300-second TTL (used by thread-safety tests)."""
    from agent_cascade.utils.token_cache import AgentTokenCache
    return AgentTokenCache()


# ---------------------------------------------------------------------------
# Fixtures: tool_utils and streaming_tool_resolution tests
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_pool():
    """Minimal fake AgentPool with instance_conversations for cache-pool tests."""
    return _FakeAgentPool()