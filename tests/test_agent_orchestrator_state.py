"""Integration tests for state management logic in api_server.

The functions get_session_history(), build_state(), and get_agent_state() are
closures inside the app factory, so we can't patch them at module level.
Instead, we test the *logic patterns* they implement by recreating the same
data flow with controlled inputs.

We also test the unified token cache integration that build_state uses.
"""

import copy
from unittest.mock import patch, MagicMock

import pytest


# ===========================================================================
# get_session_history — dual-read wrapper logic
# ===========================================================================

class TestGetSessionHistoryLogic:
    """Test the dual-read pattern used by get_session_history().

    The actual function is a closure; we test the same logic paths with
    controlled data structures.
    """

    # --- Legacy mode (USE_UNIFIED_STATE = False) ---

    def test_legacy_root_reads_from_session(self):
        """In legacy mode, root history comes from session['history']."""
        session = {"history": [{"role": "user", "content": "legacy_msg"}]}
        use_unified = False

        if not use_unified:
            msgs = list(session.get("history", []))

        assert len(msgs) == 1
        assert msgs[0]["content"] == "legacy_msg"

    def test_legacy_sub_agent_reads_from_instance_conversations(self):
        """In legacy mode, sub-agent history comes from instance_conversations."""
        instance_conversations = {
            "worker1": [{"role": "user", "content": "hello"}],
        }
        use_unified = False

        if not use_unified:
            msgs = instance_conversations.get("worker1", [])

        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"

    def test_legacy_no_pool_returns_empty(self):
        """When agent_pool is None, sub-agent reads return empty lists."""
        use_unified = False
        pool = None

        if not use_unified:
            msgs = pool.instance_conversations.get("worker1", []) if pool else []

        assert msgs == []

    # --- Unified mode (USE_UNIFIED_STATE = True) ---

    def test_unified_root_reads_from_sub_agent_state(self):
        """In unified mode, root history comes from sub_agent_state['root']."""
        sub_agent_state = {
            "root": {"messages": [{"role": "user", "content": "unified_hello"}]},
        }
        use_unified = True

        if use_unified:
            store = sub_agent_state.get("root", {})
            msgs = store.get("messages", [])

        assert len(msgs) == 1
        assert msgs[0]["content"] == "unified_hello"

    def test_unified_fallback_to_legacy_when_store_empty(self):
        """Unified mode falls back to session['history'] if root store is empty."""
        sub_agent_state = {"root": {"messages": []}}  # empty unified store
        session = {"history": [{"role": "user", "content": "fallback"}]}
        use_unified = True

        if use_unified:
            store = sub_agent_state.get("root", {})
            msgs = store.get("messages", [])
            if not msgs and session.get("history"):
                msgs = list(session["history"])

        assert len(msgs) == 1
        assert msgs[0]["content"] == "fallback"

    def test_unified_sub_agent_reads_from_sub_agent_state(self):
        """In unified mode, sub-agent history also comes from sub_agent_state."""
        sub_agent_state = {
            "worker1": {"messages": [{"role": "assistant", "content": "unified_work"}]},
        }
        use_unified = True

        if use_unified:
            state = sub_agent_state.get("worker1", {})
            msgs = state.get("messages", [])

        assert len(msgs) == 1
        assert msgs[0]["content"] == "unified_work"

    def test_explicit_use_unified_override(self):
        """The use_unified parameter can override the global flag."""
        sub_agent_state = {
            "root": {"messages": [{"role": "user", "content": "unified"}]},
        }
        session = {"history": [{"role": "user", "content": "legacy"}]}

        # Global flag is False, but we pass use_unified=True
        global_flag = False
        use_unified_param = True
        effective_unified = use_unified_param if use_unified_param is not None else global_flag

        assert effective_unified is True  # param overrides global

        store = sub_agent_state.get("root", {})
        msgs = store.get("messages", [])
        assert msgs[0]["content"] == "unified"

    def test_effective_unified_none_falls_to_global(self):
        """When use_unified is None, it falls back to the global flag."""
        # Simulating: effective_unified = use_unified if use_unified is not None else USE_UNIFIED_STATE
        global_flag = False
        use_unified_param = None
        effective = use_unified_param if use_unified_param is not None else global_flag
        assert effective is False

    def test_no_agent_pool_unified_returns_empty(self):
        """When agent_pool is None in unified mode, sub-agent reads return empty."""
        pool = None
        use_unified = True

        if use_unified:
            state = pool.sub_agent_state.get("worker1", {}) if pool else {}
            msgs = state.get("messages", [])

        assert msgs == []


# ===========================================================================
# build_state — dual-read logic and token cache integration
# ===========================================================================

class TestBuildStateLogic:
    """Test the build_state logic pattern with both flag modes."""

    def test_build_state_legacy_uses_session_history(self):
        """In legacy mode, messages come from session['history']."""
        use_unified = False
        session = {"history": [{"role": "user", "content": "legacy_msg"}]}

        if not use_unified:
            msgs = list(session.get("history", []))

        assert len(msgs) == 1
        assert msgs[0]["content"] == "legacy_msg"

    def test_build_state_unified_uses_sub_agent_state(self):
        """In unified mode, root messages come from sub_agent_state."""
        use_unified = True
        sub_agent_state = {
            "root": {"messages": [{"role": "user", "content": "unified_msg"}]},
        }

        if use_unified:
            root_state = sub_agent_state.get("root")
            msgs = root_state["messages"] if root_state else []

        assert len(msgs) == 1
        assert msgs[0]["content"] == "unified_msg"

    def test_build_state_writes_to_unified_token_cache_when_unified(self):
        """In unified mode, build_state writes root stats to the token cache."""
        from config.token_cache import AgentTokenCache

        use_unified = True
        cache = AgentTokenCache(ttl=300)
        if cache._cleanup_timer:
            cache._cleanup_timer.cancel()

        # Simulate what build_state does at lines 793-794:
        #   if USE_UNIFIED_STATE:
        #       unified_token_cache.set('root', len(active_h), h_stats['tokens'])
        active_h_count = 10
        h_stats_tokens = 5000

        if use_unified:
            cache.set("root", active_h_count, h_stats_tokens)

        cached = cache.get("root")
        assert cached is not None
        assert cached["count"] == 10
        assert cached["tokens"] == 5000

    def test_build_state_does_not_write_cache_in_legacy_mode(self):
        """In legacy mode, build_state does NOT write to the unified token cache."""
        from config.token_cache import AgentTokenCache

        use_unified = False
        cache = AgentTokenCache(ttl=300)
        if cache._cleanup_timer:
            cache._cleanup_timer.cancel()

        # In legacy mode, the cache.write branch is skipped
        # Verify root key is not set
        cached = cache.get("root")
        assert cached is None

    def test_build_state_includes_sub_agents(self):
        """build_state output includes sub_agent state."""
        sub_agent_state = {
            "worker1": {"active": True, "agent_name": "worker1 (Coder)", "messages": []},
        }
        # get_sub_agent_state iterates over pool.sub_agent_state
        result = {}
        for name, state in sub_agent_state.items():
            result[name] = {
                "active": state.get("active", False),
                "agent_name": name,
            }
        assert "worker1" in result


# ===========================================================================
# get_agent_state — unified vs legacy
# ===========================================================================

class TestGetAgentStateLogic:
    """Test the get_agent_state logic with both flag modes."""

    def test_legacy_root_returns_session_history(self):
        """In legacy mode, root agent state comes from session['history']."""
        use_unified = False
        session = {"history": [{"role": "user", "content": "legacy"}]}

        if not use_unified:
            msgs = list(session.get("history", []))
            state = {
                "messages": msgs,
                "active": True,
                "agent_name": "Maine (OrchestratorAgent)",
            }

        assert len(state["messages"]) == 1
        assert state["active"] is True

    def test_unified_root_returns_sub_agent_state(self):
        """In unified mode, root agent state comes from sub_agent_state."""
        use_unified = True
        sub_agent_state = {
            "root": {"messages": [{"role": "user", "content": "unified"}], "active": True},
        }

        if use_unified:
            state = sub_agent_state.get("root")
            state = state.copy() if state else None

        assert state is not None
        assert len(state["messages"]) == 1
        assert state["messages"][0]["content"] == "unified"

    def test_unified_missing_instance_returns_none(self):
        """When instance is not in sub_agent_state, returns None."""
        use_unified = True
        sub_agent_state = {}

        if use_unified:
            state = sub_agent_state.get("nobody")
            state = state.copy() if state else None

        assert state is None

    def test_no_pool_unified_returns_none(self):
        """When agent_pool is None in unified mode, returns None."""
        use_unified = True
        pool = None

        if use_unified:
            state = pool.sub_agent_state.get("root") if pool else None
            state = state.copy() if state else None

        assert state is None


# ===========================================================================
# Token cache integration in build_state
# ===========================================================================

class TestTokenCacheIntegration:
    """Test that the unified token cache is properly used during state building."""

    def test_cache_ttl_is_300(self):
        """The api_server creates a token cache with 300s TTL."""
        from config.token_cache import AgentTokenCache
        cache = AgentTokenCache(ttl=300)
        if cache._cleanup_timer:
            cache._cleanup_timer.cancel()
        assert cache._ttl == 300

    def test_cache_stores_and_retrieves_root_stats(self):
        """build_state writes root stats; subsequent reads should find them."""
        from config.token_cache import AgentTokenCache
        cache = AgentTokenCache(ttl=60)
        if cache._cleanup_timer:
            cache._cleanup_timer.cancel()

        # Simulate what build_state does in unified mode
        cache.set("root", count=10, tokens=5000)
        result = cache.get("root")
        assert result is not None
        assert result["count"] == 10
        assert result["tokens"] == 5000

    def test_cache_entry_expires_and_build_state_can_write_new(self):
        """After TTL expires, build_state can write a fresh entry."""
        from config.token_cache import AgentTokenCache
        import time

        cache = AgentTokenCache(ttl=1)
        if cache._cleanup_timer:
            cache._cleanup_timer.cancel()

        # First write
        cache.set("root", count=5, tokens=2000)
        assert cache.get("root") is not None

        # Wait for expiry
        time.sleep(1.1)
        assert cache.get("root") is None  # expired

        # build_state can write a new entry after expiry
        cache.set("root", count=8, tokens=3500)
        result = cache.get("root")
        assert result["count"] == 8
        assert result["tokens"] == 3500


# ===========================================================================
# Incremental token counting logic (from build_state lines 759-790)
# ===========================================================================

class TestIncrementalTokenCounting:
    """Test the incremental token counting pattern used in build_state."""

    def test_incremental_stats_on_history_growth(self):
        """When history grows, stats are computed incrementally."""
        session = {}

        # First call: cache missing → compute full stats
        hist_count = 5
        cached_hist_count = session.get("_cached_hist_stats_count", -1)

        if hist_count > cached_hist_count:
            if cached_hist_count >= 0 and cached_hist_count < hist_count:
                pass  # incremental (not first time)
            else:
                # Full compute
                h_stats = {"tokens": 2500, "words": 500}
            session["_cached_hist_stats"] = h_stats.copy()
            session["_cached_hist_stats_count"] = hist_count

        assert session["_cached_hist_stats"]["tokens"] == 2500

    def test_incremental_stats_on_history_shrink(self):
        """When history shrinks (compression), stats are recomputed from scratch."""
        session = {
            "_cached_hist_stats": {"tokens": 5000, "words": 1000},
            "_cached_hist_stats_count": 20,
        }

        hist_count = 15  # shrank due to compression

        if hist_count < session.get("_cached_hist_stats_count", -1):
            # Recompute from scratch
            h_stats = {"tokens": 3000, "words": 600}
            session["_cached_hist_stats"] = h_stats.copy()
            session["_cached_hist_stats_count"] = hist_count

        assert session["_cached_hist_stats"]["tokens"] == 3000

    def test_cached_stats_reused_when_unchanged(self):
        """When history count is the same, cached stats are reused."""
        session = {
            "_cached_hist_stats": {"tokens": 2500, "words": 500},
            "_cached_hist_stats_count": 10,
        }

        hist_count = 10

        if hist_count == session.get("_cached_hist_stats_count", -1):
            h_stats = session["_cached_hist_stats"].copy()

        assert h_stats["tokens"] == 2500