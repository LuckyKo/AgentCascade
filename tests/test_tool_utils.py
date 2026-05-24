"""Unit tests for agent_cascade.tool_utils.resolve_prev_arg_placeholders().

Covers basic resolution, __GLOBAL__ scope fallback, nested placeholders,
missing-key errors, non-dict passthrough, and thread-safety.
"""

import copy
import threading

from agent_cascade.tool_utils import resolve_prev_arg_placeholders


# ---------------------------------------------------------------------------
# Helper: populate the cache so tests have something to resolve against
# ---------------------------------------------------------------------------

def _seed_cache(pool, scope, tool_name, args):
    """Insert *args* into pool.last_tool_args for the given scope and tool."""
    pool.last_tool_args.setdefault(scope, {})[tool_name] = copy.deepcopy(args)


# ===========================================================================
# Basic __USE_PREV_ARG__ resolution (tool-scoped)
# ===========================================================================

class TestBasicResolution:
    """Test that placeholders resolve from the tool-specific previous args."""

    def test_resolves_single_placeholder(self, agent_pool):
        _seed_cache(agent_pool, "s1", "read_file", {"path": "/tmp/x.txt"})
        tool_args = {"path": "__USE_PREV_ARG__"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "s1", "read_file", agent_pool)
        assert err is None
        assert resolved == {"path": "/tmp/x.txt"}

    def test_resolves_multiple_placeholders(self, agent_pool):
        _seed_cache(agent_pool, "s1", "edit_file", {
            "file_path": "/a/b.py",
            "old_content": "foo",
            "new_content": "bar",
        })
        tool_args = {
            "file_path": "__USE_PREV_ARG__",
            "old_content": "__USE_PREV_ARG__",
        }
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "s1", "edit_file", agent_pool)
        assert err is None
        assert resolved["file_path"] == "/a/b.py"
        assert resolved["old_content"] == "foo"

    def test_mixed_placeholder_and_literal(self, agent_pool):
        _seed_cache(agent_pool, "s1", "write_file", {"content": "prev"})
        tool_args = {
            "file_path": "/new/path.txt",       # literal — not a placeholder
            "content": "__USE_PREV_ARG__",      # resolved
        }
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "s1", "write_file", agent_pool)
        assert err is None
        assert resolved["file_path"] == "/new/path.txt"
        assert resolved["content"] == "prev"

    def test_no_placeholders_returns_original(self, agent_pool):
        tool_args = {"key": "value"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "s1", "any_tool", agent_pool)
        assert err is None
        # Should return the same dict object (no copy without placeholders)
        assert resolved is tool_args

    def test_empty_dict_returns_unchanged(self, agent_pool):
        resolved, err = resolve_prev_arg_placeholders({}, "s1", "t", agent_pool)
        assert err is None
        assert resolved == {}


# ===========================================================================
# __GLOBAL__ scope fallback
# ===========================================================================

class TestGlobalScope:
    """Test that placeholders fall back to the __GLOBAL__ cache entry."""

    def test_fallback_to_global(self, agent_pool):
        # No tool-specific entry for "grep" — only global
        agent_pool.last_tool_args["s1"] = {
            "__GLOBAL__": {"pattern": "error"},
        }
        resolved, err = resolve_prev_arg_placeholders(
            {"pattern": "__USE_PREV_ARG__"}, "s1", "grep", agent_pool)
        assert err is None
        assert resolved["pattern"] == "error"

    def test_tool_specific_takes_precedence_over_global(self, agent_pool):
        _seed_cache(agent_pool, "s1", "read_file", {"path": "/tool/path.txt"})
        agent_pool.last_tool_args["s1"]["__GLOBAL__"] = {
            "path": "/global/path.txt",
        }
        resolved, err = resolve_prev_arg_placeholders(
            {"path": "__USE_PREV_ARG__"}, "s1", "read_file", agent_pool)
        assert err is None
        assert resolved["path"] == "/tool/path.txt"

    def test_global_key_not_in_tool_specific_uses_global(self, agent_pool):
        _seed_cache(agent_pool, "s1", "read_file", {"path": "/tool"})
        agent_pool.last_tool_args["s1"]["__GLOBAL__"] = {
            "extra_key": "from_global",
        }
        resolved, err = resolve_prev_arg_placeholders(
            {"extra_key": "__USE_PREV_ARG__"}, "s1", "read_file", agent_pool)
        assert err is None
        assert resolved["extra_key"] == "from_global"

    def test_only_global_no_tool_specific(self, agent_pool):
        agent_pool.last_tool_args["s1"] = {
            "__GLOBAL__": {"content": "global_content"},
        }
        resolved, err = resolve_prev_arg_placeholders(
            {"content": "__USE_PREV_ARG__"}, "s1", "write_file", agent_pool)
        assert err is None
        assert resolved["content"] == "global_content"


# ===========================================================================
# Missing-key error handling
# ===========================================================================

class TestMissingKeys:
    """Test that missing keys produce errors and return the original args."""

    def test_no_previous_calls_at_all(self, agent_pool):
        # Empty cache for this scope
        resolved, err = resolve_prev_arg_placeholders(
            {"key": "__USE_PREV_ARG__"}, "empty_scope", "any_tool", agent_pool)
        assert err is not None
        assert "no previous call" in err.lower()
        # Returns the UNMODIFIED original args on error
        assert resolved == {"key": "__USE_PREV_ARG__"}

    def test_key_not_in_tool_or_global(self, agent_pool):
        _seed_cache(agent_pool, "s1", "read_file", {"path": "/tmp"})
        resolved, err = resolve_prev_arg_placeholders(
            {"nonexistent": "__USE_PREV_ARG__"}, "s1", "read_file", agent_pool)
        assert err is not None
        assert "nonexistent" in err.lower()
        assert resolved == {"nonexistent": "__USE_PREV_ARG__"}

    def test_error_message_includes_tool_and_scope(self, agent_pool):
        resolved, err = resolve_prev_arg_placeholders(
            {"x": "__USE_PREV_ARG__"}, "myScope", "myTool", agent_pool)
        assert err is not None
        assert "myTool" in err
        assert "myScope" in err

    def test_partial_failure_returns_original_args(self, agent_pool):
        """If any key fails to resolve, the entire dict is returned unchanged."""
        _seed_cache(agent_pool, "s1", "t", {"a": 1})
        # "a" exists, "b" does not → should error and return original
        tool_args = {"a": "__USE_PREV_ARG__", "b": "__USE_PREV_ARG__"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "s1", "t", agent_pool)
        assert err is not None
        # Must return the ORIGINAL (unchanged) dict, not a partially-resolved one
        assert resolved == {"a": "__USE_PREV_ARG__", "b": "__USE_PREV_ARG__"}


# ===========================================================================
# Non-dict passthrough
# ===========================================================================

class TestNonDictPassthrough:
    """Non-dict inputs should pass through unchanged with no error."""

    def test_string_passthrough(self, agent_pool):
        resolved, err = resolve_prev_arg_placeholders("hello", "s", "t", agent_pool)
        assert err is None
        assert resolved == "hello"

    def test_list_passthrough(self, agent_pool):
        resolved, err = resolve_prev_arg_placeholders([1, 2], "s", "t", agent_pool)
        assert err is None
        assert resolved == [1, 2]

    def test_none_passthrough(self, agent_pool):
        resolved, err = resolve_prev_arg_placeholders(None, "s", "t", agent_pool)
        assert err is None
        assert resolved is None

    def test_int_passthrough(self, agent_pool):
        resolved, err = resolve_prev_arg_placeholders(42, "s", "t", agent_pool)
        assert err is None
        assert resolved == 42


# ===========================================================================
# Nested placeholder resolution
# ===========================================================================

class TestNestedValues:
    """Test that deeply nested dicts/lists with placeholders get resolved."""

    def test_nested_dict_value(self, agent_pool):
        _seed_cache(agent_pool, "s1", "t", {
            "config": {"path": "/deep", "flag": True},
        })
        resolved, err = resolve_prev_arg_placeholders(
            {"config": "__USE_PREV_ARG__"}, "s1", "t", agent_pool)
        assert err is None
        assert resolved["config"] == {"path": "/deep", "flag": True}

    def test_resolved_value_is_deep_copied(self, agent_pool):
        """Mutating the resolved value must not mutate the cache."""
        _seed_cache(agent_pool, "s1", "t", {"nested": {"x": 1}})
        resolved, err = resolve_prev_arg_placeholders(
            {"nested": "__USE_PREV_ARG__"}, "s1", "t", agent_pool)
        assert err is None
        # Mutate the resolved value
        resolved["nested"]["x"] = 999
        # Re-resolve — should still be 1 (cache was deep-copied)
        resolved2, _ = resolve_prev_arg_placeholders(
            {"nested": "__USE_PREV_ARG__"}, "s1", "t", agent_pool)
        assert resolved2["nested"]["x"] == 1

    def test_tool_args_deep_copied_before_resolution(self, agent_pool):
        """Original tool_args must not be modified by resolution."""
        original = {"key": "__USE_PREV_ARG__"}
        _seed_cache(agent_pool, "s1", "t", {"key": "resolved_val"})
        resolved, err = resolve_prev_arg_placeholders(original, "s1", "t", agent_pool)
        assert err is None
        # original dict must still contain the placeholder
        assert original["key"] == "__USE_PREV_ARG__"


# ===========================================================================
# Thread-safety: lock=None when caller already holds the lock
# ===========================================================================

class TestThreadSafety:
    """Test that passing lock=None avoids deadlock when caller holds the lock."""

    def test_no_deadlock_with_lock_none(self, agent_pool):
        """Passing lock=None should work without error."""
        _seed_cache(agent_pool, "s1", "t", {"a": 1})
        resolved, err = resolve_prev_arg_placeholders(
            {"a": "__USE_PREV_ARG__"}, "s1", "t", agent_pool, lock=None)
        assert err is None
        assert resolved["a"] == 1

    def test_lock_provided_works(self, agent_pool):
        """Passing an actual lock should acquire it for cache reads."""
        _seed_cache(agent_pool, "s1", "t", {"x": "y"})
        l = threading.Lock()
        resolved, err = resolve_prev_arg_placeholders(
            {"x": "__USE_PREV_ARG__"}, "s1", "t", agent_pool, lock=l)
        assert err is None
        assert resolved["x"] == "y"

    def test_concurrent_resolution_with_lock(self, agent_pool):
        """Multiple threads resolving with the same lock should not corrupt."""
        _seed_cache(agent_pool, "s1", "t", {"val": 0})

        l = threading.Lock()
        errors = []

        def resolve_then_update(i):
            try:
                resolved, err = resolve_prev_arg_placeholders(
                    {"val": "__USE_PREV_ARG__"}, "s1", "t", agent_pool, lock=l)
                if err:
                    errors.append(err)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=resolve_then_update, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent resolution produced errors: {errors}"

    def test_concurrent_resolution_without_lock(self, agent_pool):
        """Without a lock, concurrent reads may be unsafe but should not crash."""
        _seed_cache(agent_pool, "s1", "t", {"val": 42})

        errors = []

        def resolve():
            try:
                resolved, err = resolve_prev_arg_placeholders(
                    {"val": "__USE_PREV_ARG__"}, "s1", "t", agent_pool, lock=None)
                if err:
                    errors.append(err)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=resolve) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No hard assertions on correctness (unsynchronized), but no crashes
        assert not errors

    def test_attribute_error_defensive_fallback(self):
        """AgentPool without last_tool_args should return original args."""
        bare_pool = object()  # no last_tool_args attribute
        tool_args = {"key": "__USE_PREV_ARG__"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "s1", "t", bare_pool)
        assert err is None  # Defensive: returns original without error
        assert resolved == tool_args


# ===========================================================================
# Scoping: different scopes are isolated
# ===========================================================================

class TestScopeIsolation:
    """Different instance_scope values should not share cached args."""

    def test_different_scopes_isolated(self, agent_pool):
        _seed_cache(agent_pool, "scopeA", "t", {"key": "valueA"})
        # scopeB has no entry → error
        resolved, err = resolve_prev_arg_placeholders(
            {"key": "__USE_PREV_ARG__"}, "scopeB", "t", agent_pool)
        assert err is not None

    def test_different_tools_isolated(self, agent_pool):
        _seed_cache(agent_pool, "s1", "toolA", {"path": "/a"})
        # toolB has no entry in s1 → error
        resolved, err = resolve_prev_arg_placeholders(
            {"path": "__USE_PREV_ARG__"}, "s1", "toolB", agent_pool)
        assert err is not None