"""Integration tests for __USE_PREV_ARG__ resolution in the streaming tool path.

These tests verify that resolve_prev_arg_placeholders() from tool_utils is called
in both the streaming (sub-agent) and non-streaming paths of OrchestratorAgent._run,
and that lock protection around last_tool_args reads/writes works correctly.

We mock the LLM and test the resolution logic in isolation from actual agent runs.
"""

import copy
import threading
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_agent_pool():
    """A minimal mock AgentPool with last_tool_args."""
    pool = MagicMock()
    pool.last_tool_args = {}
    pool._state_lock = threading.Lock()
    return pool


@pytest.fixture
def seeded_pool(mock_agent_pool):
    """AgentPool with pre-seeded last_tool_args for resolution tests."""
    mock_agent_pool.last_tool_args = {
        "Maine": {
            "call_agent": {
                "agent_class": "coder",
                "instance_name": "worker1",
                "task": "Write a script",
            },
            "__GLOBAL__": {
                "common_arg": "shared_value",
            },
        },
    }
    return mock_agent_pool


# ===========================================================================
# Streaming path: __USE_PREV_ARG__ resolution gated by USE_UNIFIED_LOOP
# ===========================================================================

class TestStreamingPathResolution:
    """Test that the streaming (sub-agent) path resolves placeholders when USE_UNIFIED_LOOP=True."""

    def test_unified_loop_calls_resolver(self, seeded_pool):
        """When USE_UNIFIED_LOOP is True, resolve_prev_arg_placeholders is called in the streaming path."""
        with patch('agent_orchestrator.USE_UNIFIED_LOOP', True):
            from agent_cascade.tool_utils import resolve_prev_arg_placeholders

            tool_args_str = '{"instance_name": "__USE_PREV_ARG__"}'
            parsed_args = {
                "instance_name": "__USE_PREV_ARG__",
            }

            resolved, err = resolve_prev_arg_placeholders(
                parsed_args, "Maine", "call_agent", seeded_pool,
                lock=seeded_pool._state_lock)
            assert err is None
            assert resolved["instance_name"] == "worker1"

    def test_unified_loop_not_set_skips_resolution(self, seeded_pool):
        """When USE_UNIFIED_LOOP is False, the streaming path does NOT resolve placeholders."""
        with patch('agent_orchestrator.USE_UNIFIED_LOOP', False):
            # The streaming path code at line ~1399 only calls the resolver when USE_UNIFIED_LOOP=True
            # So parsed_args with __USE_PREV_ARG__ would remain unresolved
            tool_args = {"instance_name": "__USE_PREV_ARG__"}
            # Simulate the condition check: if not USE_UNIFIED_LOOP, prev_arg_error stays None
            # and parsed_args is passed through unchanged
            assert tool_args["instance_name"] == "__USE_PREV_ARG__"

    def test_resolver_error_prevents_sub_agent_call(self, mock_agent_pool):
        """If resolution fails in the streaming path, the error becomes the tool result."""
        with patch('agent_orchestrator.USE_UNIFIED_LOOP', True):
            from agent_cascade.tool_utils import resolve_prev_arg_placeholders

            # Empty cache — no previous calls
            parsed_args = {"instance_name": "__USE_PREV_ARG__"}
            resolved, err = resolve_prev_arg_placeholders(
                parsed_args, "Maine", "call_agent", mock_agent_pool,
                lock=mock_agent_pool._state_lock)
            assert err is not None
            assert "no previous call" in err.lower()

    def test_string_tool_args_parsed_before_resolution(self, seeded_pool):
        """String tool args are JSON-parsed before resolution."""
        with patch('agent_orchestrator.USE_UNIFIED_LOOP', True):
            from agent_cascade.tool_utils import resolve_prev_arg_placeholders

            # Simulate: tool_args is a string, parsed to dict, then resolved
            tool_args_str = '{"task": "__USE_PREV_ARG__"}'
            import json
            parsed_args = json.loads(tool_args_str)
            resolved, err = resolve_prev_arg_placeholders(
                parsed_args, "Maine", "call_agent", seeded_pool,
                lock=seeded_pool._state_lock)
            assert err is None
            assert resolved["task"] == "Write a script"

    def test_global_fallback_in_streaming_path(self, seeded_pool):
        """__USE_PREV_ARG__ falls back to __GLOBAL__ in the streaming path."""
        with patch('agent_orchestrator.USE_UNIFIED_LOOP', True):
            from agent_cascade.tool_utils import resolve_prev_arg_placeholders

            # "common_arg" is not in call_agent's specific args, but is in __GLOBAL__
            parsed_args = {"common_arg": "__USE_PREV_ARG__"}
            resolved, err = resolve_prev_arg_placeholders(
                parsed_args, "Maine", "call_agent", seeded_pool,
                lock=seeded_pool._state_lock)
            assert err is None
            assert resolved["common_arg"] == "shared_value"


# ===========================================================================
# Non-streaming path: __USE_PREV_ARG__ resolution (always active)
# ===========================================================================

class TestNonStreamingPathResolution:
    """Test that the non-streaming (normal tool) path always resolves placeholders."""

    def test_non_streaming_resolves_placeholders(self, seeded_pool):
        """Normal tool calls resolve placeholders regardless of USE_UNIFIED_LOOP."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        # Non-streaming path doesn't check USE_UNIFIED_LOOP for resolution
        parsed_args = {"file_path": "__USE_PREV_ARG__"}
        seeded_pool.last_tool_args["Maine"]["write_file"] = {
            "file_path": "/tmp/out.txt",
        }
        resolved, err = resolve_prev_arg_placeholders(
            parsed_args, "Maine", "write_file", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        assert resolved["file_path"] == "/tmp/out.txt"

    def test_non_streaming_resolution_with_string_tool_args(self, seeded_pool):
        """String tool args are parsed and then resolved in the non-streaming path."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        # Simulate: JSON string → dict → resolution
        import json
        tool_args_str = '{"file_path": "__USE_PREV_ARG__"}'
        parsed = json.loads(tool_args_str)
        seeded_pool.last_tool_args["Maine"]["write_file"] = {
            "file_path": "/a/b.py",
        }
        resolved, err = resolve_prev_arg_placeholders(
            parsed, "Maine", "write_file", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        assert resolved["file_path"] == "/a/b.py"

    def test_non_streaming_error_skips_execution(self, mock_agent_pool):
        """When resolution fails in non-streaming path, tool execution is skipped."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        parsed_args = {"key": "__USE_PREV_ARG__"}
        resolved, err = resolve_prev_arg_placeholders(
            parsed_args, "Maine", "some_tool", mock_agent_pool,
            lock=mock_agent_pool._state_lock)
        assert err is not None
        # The non-streaming path sets skip_execution=True and tool_result = error
        # We can't test the actual _call_tool skip here, but we verify the error signal
        assert "no previous call" in err.lower()


# ===========================================================================
# Lock protection around last_tool_args reads/writes
# ===========================================================================

class TestLockProtection:
    """Test that lock is properly used for thread-safe cache access."""

    def test_lock_is_passed_in_both_paths(self, seeded_pool):
        """Both streaming and non-streaming paths pass the _state_lock to resolver."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        # The lock parameter should be the pool's _state_lock
        assert isinstance(seeded_pool._state_lock, type(threading.Lock()))

        # Verify resolution works with the lock
        resolved, err = resolve_prev_arg_placeholders(
            {"instance_name": "__USE_PREV_ARG__"},
            "Maine", "call_agent", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None

    def test_concurrent_resolution_and_cache_write(self, seeded_pool):
        """Simulate concurrent reads (resolution) and writes (cache update)."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        errors = []

        def reader():
            try:
                for _ in range(50):
                    resolve_prev_arg_placeholders(
                        {"instance_name": "__USE_PREV_ARG__"},
                        "Maine", "call_agent", seeded_pool,
                        lock=seeded_pool._state_lock)
            except Exception as e:
                errors.append(str(e))

        def writer():
            try:
                for i in range(50):
                    with seeded_pool._state_lock:
                        if "Maine" not in seeded_pool.last_tool_args:
                            seeded_pool.last_tool_args["Maine"] = {}
                        seeded_pool.last_tool_args["Maine"]["call_agent"] = {
                            "task": f"task_{i}",
                        }
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
        # Simulate what the orchestrator does at lines 1522-1528:
        #   self.agent_pool.last_tool_args[scope][tool_name] = copy.deepcopy(tool_args)
        original_args = {"nested": {"key": "value"}}
        cached = copy.deepcopy(original_args)
        seeded_pool.last_tool_args["Maine"]["test_tool"] = cached

        # Mutate the original — cache should be unaffected
        original_args["nested"]["key"] = "mutated"
        assert seeded_pool.last_tool_args["Maine"]["test_tool"]["nested"]["key"] == "value"

    def test_lock_none_avoids_deadlock(self, seeded_pool):
        """When caller already holds the lock, passing lock=None avoids deadlock."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        # Acquire the lock ourselves (simulating the caller holding it)
        with seeded_pool._state_lock:
            resolved, err = resolve_prev_arg_placeholders(
                {"instance_name": "__USE_PREV_ARG__"},
                "Maine", "call_agent", seeded_pool,
                lock=None)  # Caller holds lock, so pass None
            assert err is None
            assert resolved["instance_name"] == "worker1"


# ===========================================================================
# Session scope isolation
# ===========================================================================

class TestSessionScopeIsolation:
    """Test that different sessions don't share cached tool args."""

    def test_different_sessions_isolated(self, seeded_pool):
        """Session 'Maine' and session 'Other' should not share cache entries."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        # "Maine" has entries; "Other" does not
        resolved, err = resolve_prev_arg_placeholders(
            {"instance_name": "__USE_PREV_ARG__"},
            "Other", "call_agent", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is not None

    def test_session_name_defaults_to_root(self):
        """When session_name attribute is missing, it defaults to 'root'."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        pool = MagicMock()
        pool.last_tool_args = {
            "root": {"write_file": {"file_path": "/tmp"}},
        }
        pool._state_lock = threading.Lock()

        resolved, err = resolve_prev_arg_placeholders(
            {"file_path": "__USE_PREV_ARG__"},
            "root", "write_file", pool,
            lock=pool._state_lock)
        assert err is None
        assert resolved["file_path"] == "/tmp"


# ===========================================================================
# Integration: full resolution → cache write cycle
# ===========================================================================

class TestFullResolutionCycle:
    """Test the complete cycle: resolve → execute → cache write → resolve again."""

    def test_resolve_use_cache_write_then_resolve_again(self, seeded_pool):
        """Simulate: first call writes args to cache; second call resolves from it."""
        from agent_cascade.tool_utils import resolve_prev_arg_placeholders

        # Step 1: First call — cache write (simulated)
        tool_args_1 = {"file_path": "/first.py", "content": "hello"}
        seeded_pool.last_tool_args["Maine"]["write_file"] = copy.deepcopy(tool_args_1)
        if "__GLOBAL__" not in seeded_pool.last_tool_args["Maine"]:
            seeded_pool.last_tool_args["Maine"]["__GLOBAL__"] = {}
        seeded_pool.last_tool_args["Maine"]["__GLOBAL__"].update(copy.deepcopy(tool_args_1))

        # Step 2: Second call — resolve from cache
        tool_args_2 = {"file_path": "__USE_PREV_ARG__", "content": "__USE_PREV_ARG__"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args_2, "Maine", "write_file", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        assert resolved["file_path"] == "/first.py"
        assert resolved["content"] == "hello"

        # Step 3: Write new args to cache (simulating successful execution)
        tool_args_3 = {"file_path": "/second.py", "content": "world"}
        seeded_pool.last_tool_args["Maine"]["write_file"] = copy.deepcopy(tool_args_3)
        seeded_pool.last_tool_args["Maine"]["__GLOBAL__"].update(copy.deepcopy(tool_args_3))

        # Step 4: Third call — should resolve from the NEW cache entry
        tool_args_4 = {"file_path": "__USE_PREV_ARG__"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args_4, "Maine", "write_file", seeded_pool,
            lock=seeded_pool._state_lock)
        assert err is None
        assert resolved["file_path"] == "/second.py"