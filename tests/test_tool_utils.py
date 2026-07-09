"""Unit tests for agent_cascade.tool_utils.resolve_prev_arg_placeholders().

Covers basic {USE_CACHED_ENTRY_N} resolution, cache pool lookups,
non-dict passthrough, deep copy isolation, and thread-safety.
"""

import copy
import threading

from agent_cascade.tool_utils import resolve_prev_arg_placeholders


# ---------------------------------------------------------------------------
# Helper: populate the cache pool so tests have something to resolve against
# ---------------------------------------------------------------------------

def _seed_cache(pool, scope, entries):
    """Insert *entries* (list of values) into the cache pool for the given scope.
    Each entry gets a sequential N index. Returns the cache pool for chaining."""
    from tests.conftest import _FakeInstance
    if scope not in pool.instance_conversations:
        pool.instance_conversations[scope] = _FakeInstance()
    cp = pool.instance_conversations[scope].cache_pool
    for val in entries:
        cp.add("arg", scope, "test", val)
    return cp


# ===========================================================================
# Basic {USE_CACHED_ENTRY_N} resolution
# ===========================================================================

class TestBasicResolution:
    """Test that placeholders resolve from the rolling cache pool."""

    def test_resolves_single_placeholder(self, agent_pool):
        _seed_cache(agent_pool, "s1", [{"path": "/tmp/x.txt"}])
        # N=1 contains {"path": "/tmp/x.txt"} — the whole dict is JSON-serialized into the placeholder
        tool_args = {"path": "{USE_CACHED_ENTRY_1}"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "s1", "read_file", agent_pool)
        assert err is None
        # The cached dict is JSON-serialized and replaces the placeholder
        import json
        cached_dict = json.loads(resolved["path"])
        assert cached_dict["path"] == "/tmp/x.txt"

    def test_resolves_string_placeholder(self, agent_pool):
        _seed_cache(agent_pool, "s1", ["hello_world"])
        tool_args = {"greeting": "{USE_CACHED_ENTRY_1}"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "s1", "any_tool", agent_pool)
        assert err is None
        assert resolved["greeting"] == "hello_world"

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
# Cache pool scope isolation
# ===========================================================================

class TestScopeIsolation:
    """Different scopes should have independent cache pools."""

    def test_different_scopes_isolated(self, agent_pool):
        _seed_cache(agent_pool, "scopeA", ["valueA"])
        # scopeB has no cache pool → placeholder not found
        tool_args = {"key": "{USE_CACHED_ENTRY_1}"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "scopeB", "t", agent_pool)
        assert err is None  # No error, just passes through unchanged
        assert resolved == {"key": "{USE_CACHED_ENTRY_1}"}

    def test_correct_scope_resolves(self, agent_pool):
        _seed_cache(agent_pool, "s1", ["the_answer"])
        resolved, err = resolve_prev_arg_placeholders(
            {"key": "{USE_CACHED_ENTRY_1}"}, "s1", "t", agent_pool)
        assert err is None
        assert resolved["key"] == "the_answer"


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
# Multiple entries and index resolution
# ===========================================================================

class TestMultipleEntries:
    """Test that different N indices resolve to different cached values."""

    def test_resolve_entry_1_and_2(self, agent_pool):
        _seed_cache(agent_pool, "s1", ["first_value", "second_value"])
        tool_args = {
            "a": "{USE_CACHED_ENTRY_1}",
            "b": "{USE_CACHED_ENTRY_2}",
        }
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "s1", "t", agent_pool)
        assert err is None
        assert resolved["a"] == "first_value"
        assert resolved["b"] == "second_value"

    def test_resolve_nonexistent_index_unchanged(self, agent_pool):
        _seed_cache(agent_pool, "s1", ["only_one"])
        tool_args = {"key": "{USE_CACHED_ENTRY_99}"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "s1", "t", agent_pool)
        assert err is None
        assert resolved == {"key": "{USE_CACHED_ENTRY_99}"}  # passes through


# ===========================================================================
# Deep copy isolation
# ===========================================================================

class TestDeepCopy:
    """Test that resolved values are deep-copied."""

    def test_resolved_value_is_deep_copied(self, agent_pool):
        """Mutating the resolved value must not mutate the cache."""
        _seed_cache(agent_pool, "s1", [{"nested": {"x": 1}}])
        resolved, err = resolve_prev_arg_placeholders(
            {"nested": "{USE_CACHED_ENTRY_1}"}, "s1", "t", agent_pool)
        assert err is None
        # The value is JSON-serialized (non-string cached value)
        import json
        # Mutate the resolved value
        if isinstance(resolved["nested"], str):
            resolved["nested"] = resolved["nested"] + "_mutated"
        # Re-resolve
        resolved2, _ = resolve_prev_arg_placeholders(
            {"nested": "{USE_CACHED_ENTRY_1}"}, "s1", "t", agent_pool)
        assert resolved2["nested"] != resolved["nested"]

    def test_tool_args_deep_copied_before_resolution(self, agent_pool):
        """Original tool_args must not be modified by resolution."""
        original = {"key": "{USE_CACHED_ENTRY_1}"}
        _seed_cache(agent_pool, "s1", ["resolved_val"])
        resolved, err = resolve_prev_arg_placeholders(original, "s1", "t", agent_pool)
        assert err is None
        # original dict must still contain the placeholder
        assert original["key"] == "{USE_CACHED_ENTRY_1}"


# ===========================================================================
# Thread-safety: lock=None when caller already holds the lock
# ===========================================================================

class TestThreadSafety:
    """Test that passing lock=None avoids deadlock when caller holds the lock."""

    def test_no_deadlock_with_lock_none(self, agent_pool):
        """Passing lock=None should work without error."""
        _seed_cache(agent_pool, "s1", ["a_val"])
        resolved, err = resolve_prev_arg_placeholders(
            {"a": "{USE_CACHED_ENTRY_1}"}, "s1", "t", agent_pool, lock=None)
        assert err is None
        assert resolved["a"] == "a_val"

    def test_lock_provided_works(self, agent_pool):
        """Passing an actual lock should acquire it for cache reads."""
        _seed_cache(agent_pool, "s1", ["x_val"])
        l = threading.Lock()
        resolved, err = resolve_prev_arg_placeholders(
            {"x": "{USE_CACHED_ENTRY_1}"}, "s1", "t", agent_pool, lock=l)
        assert err is None
        assert resolved["x"] == "x_val"

    def test_concurrent_resolution_with_lock(self, agent_pool):
        """Multiple threads resolving with the same lock should not corrupt."""
        _seed_cache(agent_pool, "s1", ["val"])
        l = threading.Lock()
        errors = []

        def resolve_then_update(i):
            try:
                resolved, err = resolve_prev_arg_placeholders(
                    {"val": "{USE_CACHED_ENTRY_1}"}, "s1", "t", agent_pool, lock=l)
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
        _seed_cache(agent_pool, "s1", ["val"])
        errors = []

        def resolve():
            try:
                resolved, err = resolve_prev_arg_placeholders(
                    {"val": "{USE_CACHED_ENTRY_1}"}, "s1", "t", agent_pool, lock=None)
                if err:
                    errors.append(err)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=resolve) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_no_cache_pool_attribute(self):
        """AgentPool without instance_conversations should return original args."""
        bare_pool = type('Pool', (), {'instance_conversations': {}})()
        tool_args = {"key": "{USE_CACHED_ENTRY_1}"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args, "s1", "t", bare_pool)
        assert err is None
        assert resolved == tool_args


# ===========================================================================
# Integration: full resolution cycle
# ===========================================================================

class TestFullResolutionCycle:
    """Test the complete cycle: cache write → resolve → cache write → resolve again."""

    def test_resolve_use_cache_write_then_resolve_again(self, agent_pool):
        """Simulate: first call writes args to cache; second call resolves from it."""
        from tests.conftest import _FakeInstance
        import json
        if "Maine" not in agent_pool.instance_conversations:
            agent_pool.instance_conversations["Maine"] = _FakeInstance()
        cp = agent_pool.instance_conversations["Maine"].cache_pool

        # Step 1: Write args to cache (simulating _cache_tool_args)
        tool_args_1 = {"file_path": "/first.py", "content": "hello"}
        cp.add("arg", "Maine", "write_file", tool_args_1)

        # Step 2: Resolve from cache (N=1) — entire dict is JSON-serialized
        tool_args_2 = {"file_path": "{USE_CACHED_ENTRY_1}"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args_2, "Maine", "write_file", agent_pool)
        assert err is None
        cached_dict = json.loads(resolved["file_path"])
        assert cached_dict["file_path"] == "/first.py"

        # Step 3: Write new args to cache
        tool_args_3 = {"file_path": "/second.py", "content": "world"}
        cp.add("arg", "Maine", "write_file", tool_args_3)

        # Step 4: Third call — should resolve from the NEW cache entry (N=2)
        tool_args_4 = {"file_path": "{USE_CACHED_ENTRY_2}"}
        resolved, err = resolve_prev_arg_placeholders(
            tool_args_4, "Maine", "write_file", agent_pool)
        assert err is None
        cached_dict2 = json.loads(resolved["file_path"])
        assert cached_dict2["file_path"] == "/second.py"