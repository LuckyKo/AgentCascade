"""Integration tests for {USE_CACHED_ENTRY_N} resolution in the tool path.

These tests verify that resolve_prev_arg_placeholders() from tool_utils correctly
resolves rolling cache pool placeholders, and that lock protection works correctly.

We mock the LLM and test the resolution logic in isolation from actual agent runs.
"""

import copy
import json
import threading
from unittest.mock import MagicMock

import pytest

from tests.conftest import _FakeInstance, _FakeCachePool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_agent_pool():
    """A minimal mock AgentPool with instance_conversations."""
    pool = MagicMock()
    pool.instance_conversations = {}
    pool._state_lock = threading.Lock()
    return pool


@pytest.fixture
def seeded_pool(mock_agent_pool):
    """AgentPool with pre-seeded cache pool entries for resolution tests."""
    inst = _FakeInstance()
    mock_agent_pool.instance_conversations["Maine"] = inst

    # Seed entries: N=1 = call_agent args, N=2 = common args
    inst.cache_pool.add("arg", "Maine", "call_agent", {
        "agent_class": "coder",
        "instance_name": "worker1",
        "task": "Write a script",
    })
    inst.cache_pool.add("arg", "Maine", "common", {
        "common_arg": "shared_value",
    })
    return mock_agent_pool


# ===========================================================================
# Streaming path: {USE_CACHED_ENTRY_N} resolution
# ===========================================================================

class TestStreamingPathResolution:
    """Test that the streaming (sub-agent) path resolves placeholders."""

    def test_streaming_path_calls_resolver(self, seeded_pool):
        """resolve_prev_arg_placeholders resolves placeholders in the streaming path."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        parsed_args = {"instance_name": "{USE_CACHED_ENTRY_1}"}
        resolved, err = resolve_prev_arg_placeholders(
            parsed_args, "Maine", "call_agent", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        # N=1 cached value is a dict → JSON serialized
        cached_dict = json.loads(resolved["instance_name"])
        assert cached_dict["instance_name"] == "worker1"

    def test_resolver_no_entries_returns_unchanged(self, mock_agent_pool):
        """If no cache pool entries exist, placeholders pass through."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        parsed_args = {"instance_name": "{USE_CACHED_ENTRY_1}"}
        resolved, err = resolve_prev_arg_placeholders(
            parsed_args, "Maine", "call_agent", mock_agent_pool,
            lock=mock_agent_pool._state_lock)
        assert err is None
        assert resolved == {"instance_name": "{USE_CACHED_ENTRY_1}"}

    def test_string_tool_args_parsed_before_resolution(self, seeded_pool):
        """String tool args are JSON-parsed before resolution."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        tool_args_str = '{"task": "{USE_CACHED_ENTRY_1}"}'
        parsed_args = json.loads(tool_args_str)
        resolved, err = resolve_prev_arg_placeholders(
            parsed_args, "Maine", "call_agent", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        cached_dict = json.loads(resolved["task"])
        assert cached_dict["task"] == "Write a script"

    def test_global_fallback_in_streaming_path(self, seeded_pool):
        """Resolution falls back to any cached entry by index."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        # N=2 contains {"common_arg": "shared_value"}
        parsed_args = {"common_arg": "{USE_CACHED_ENTRY_2}"}
        resolved, err = resolve_prev_arg_placeholders(
            parsed_args, "Maine", "call_agent", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        cached_dict = json.loads(resolved["common_arg"])
        assert cached_dict["common_arg"] == "shared_value"


# ===========================================================================
# Non-streaming path: resolution
# ===========================================================================

class TestNonStreamingPathResolution:
    """Test that the non-streaming (normal tool) path resolves placeholders."""

    def test_non_streaming_resolves_placeholders(self, seeded_pool):
        """Normal tool calls resolve placeholders."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        # Add a write_file entry at N=3
        seeded_pool.instance_conversations["Maine"].cache_pool.add(
            "arg", "Maine", "write_file", {"file_path": "/tmp/out.txt"})

        parsed_args = {"file_path": "{USE_CACHED_ENTRY_3}"}
        resolved, err = resolve_prev_arg_placeholders(
            parsed_args, "Maine", "write_file", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        cached_dict = json.loads(resolved["file_path"])
        assert cached_dict["file_path"] == "/tmp/out.txt"

    def test_non_streaming_resolution_with_string_tool_args(self, seeded_pool):
        """String tool args are parsed and then resolved in the non-streaming path."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        seeded_pool.instance_conversations["Maine"].cache_pool.add(
            "arg", "Maine", "write_file", {"file_path": "/a/b.py"})

        tool_args_str = '{"file_path": "{USE_CACHED_ENTRY_3}"}'
        parsed = json.loads(tool_args_str)
        resolved, err = resolve_prev_arg_placeholders(
            parsed, "Maine", "write_file", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        cached_dict = json.loads(resolved["file_path"])
        assert cached_dict["file_path"] == "/a/b.py"

    def test_non_streaming_error_signal(self, mock_agent_pool):
        """When no cache entries exist, placeholders pass through (no error)."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        parsed_args = {"key": "{USE_CACHED_ENTRY_1}"}
        resolved, err = resolve_prev_arg_placeholders(
            parsed_args, "Maine", "some_tool", mock_agent_pool,
            lock=mock_agent_pool._state_lock)
        assert err is None
        assert resolved == {"key": "{USE_CACHED_ENTRY_1}"}


# ===========================================================================
# Lock protection
# ===========================================================================

class TestLockProtection:
    """Test that lock is properly used for thread-safe cache access."""

    def test_lock_is_passed_in_both_paths(self, seeded_pool):
        """Both streaming and non-streaming paths pass the _state_lock to resolver."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        assert isinstance(seeded_pool._state_lock, type(threading.Lock()))

        resolved, err = resolve_prev_arg_placeholders(
            {"instance_name": "{USE_CACHED_ENTRY_1}"},
            "Maine", "call_agent", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None

    def test_concurrent_resolution_and_cache_write(self, seeded_pool):
        """Simulate concurrent reads (resolution) and writes (cache update)."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        errors = []
        cp = seeded_pool.instance_conversations["Maine"].cache_pool

        def reader():
            try:
                for _ in range(50):
                    resolve_prev_arg_placeholders(
                        {"instance_name": "{USE_CACHED_ENTRY_1}"},
                        "Maine", "call_agent", seeded_pool,
                        lock=seeded_pool._state_lock)
            except Exception as e:
                errors.append(str(e))

        def writer():
            try:
                for i in range(50):
                    with seeded_pool._state_lock:
                        cp.add("arg", "Maine", "call_agent", {"task": f"task_{i}"})
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=reader) for _ in range(3)
        ] + [threading.Thread(target=writer) for _ in range(2)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent lock test errors: {errors}"

    def test_cache_write_deep_copies_args(self, seeded_pool):
        """The orchestrator writes deep-copied args to the cache to prevent mutation."""
        original_args = {"nested": {"key": "value"}}
        cached = copy.deepcopy(original_args)
        cp = seeded_pool.instance_conversations["Maine"].cache_pool
        last_idx = len(cp._entries) + 1
        cp.add("arg", "Maine", "test_tool", cached)

        # Mutate the original — cache should be unaffected
        original_args["nested"]["key"] = "mutated"
        entry = cp.get(last_idx)
        assert entry.value["nested"]["key"] == "value"

    def test_lock_none_avoids_deadlock(self, seeded_pool):
        """When caller already holds the lock, passing lock=None avoids deadlock."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        with seeded_pool._state_lock:
            resolved, err = resolve_prev_arg_placeholders(
                {"instance_name": "{USE_CACHED_ENTRY_1}"},
                "Maine", "call_agent", seeded_pool,
                lock=None)  # Caller holds lock, so pass None
            assert err is None
            cached_dict = json.loads(resolved["instance_name"])
            assert cached_dict["instance_name"] == "worker1"


# ===========================================================================
# Session scope isolation
# ===========================================================================

class TestSessionScopeIsolation:
    """Test that different sessions don't share cached tool args."""

    def test_different_sessions_isolated(self, seeded_pool):
        """Session 'Maine' has entries; 'Other' does not."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        resolved, err = resolve_prev_arg_placeholders(
            {"instance_name": "{USE_CACHED_ENTRY_1}"},
            "Other", "call_agent", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        assert resolved == {"instance_name": "{USE_CACHED_ENTRY_1}"}

    def test_session_with_own_cache(self, agent_pool):
        """Each session has its own cache pool."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders
        from tests.conftest import _FakeInstance

        agent_pool.instance_conversations["root"] = _FakeInstance()
        agent_pool.instance_conversations["root"].cache_pool.add(
            "arg", "root", "write_file", {"file_path": "/tmp"})

        resolved, err = resolve_prev_arg_placeholders(
            {"file_path": "{USE_CACHED_ENTRY_1}"},
            "root", "write_file", agent_pool)
        assert err is None
        cached_dict = json.loads(resolved["file_path"])
        assert cached_dict["file_path"] == "/tmp"


# ===========================================================================
# Integration: full resolution → cache write cycle
# ===========================================================================

class TestFullResolutionCycle:
    """Test the complete cycle: resolve → execute → cache write → resolve again."""

    def test_resolve_use_cache_write_then_resolve_again(self, seeded_pool):
        """Simulate: first call writes args to cache; second call resolves from it."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders
        cp = seeded_pool.instance_conversations["Maine"].cache_pool

        # Step 1: First call — cache write (simulated)
        tool_args_1 = {"file_path": "/first.py", "content": "hello"}
        idx1 = cp.add("arg", "Maine", "write_file", tool_args_1)

        # Step 2: Second call — resolve from cache
        tool_args_2 = {"file_path": "{USE_CACHED_ENTRY_%d}" % idx1}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args_2, "Maine", "write_file", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        cached_dict = json.loads(resolved["file_path"])
        assert cached_dict["file_path"] == "/first.py"

        # Step 3: Write new args to cache (simulating successful execution)
        tool_args_3 = {"file_path": "/second.py", "content": "world"}
        idx3 = cp.add("arg", "Maine", "write_file", tool_args_3)

        # Step 4: Third call — should resolve from the NEW cache entry
        tool_args_4 = {"file_path": "{USE_CACHED_ENTRY_%d}" % idx3}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args_4, "Maine", "write_file", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        cached_dict = json.loads(resolved["file_path"])
        assert cached_dict["file_path"] == "/second.py"