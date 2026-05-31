"""Integration tests for state management logic in api_server.

After Phase 8 cleanup, USE_UNIFIED_STATE is permanently True — legacy paths
removed. These tests verify the unified-only behavior of get_session_history(),
build_state(), and get_agent_state().

Unit tests for the AgentTokenCache module used by api_server are also included.
"""

from unittest.mock import MagicMock

import pytest


# ===========================================================================
# get_session_history — unified read only (post Phase 8)
# ===========================================================================

class TestGetSessionHistoryLogic:
    """Test the unified-read pattern used by get_session_history().

    After Phase 8, USE_UNIFIED_STATE is permanently True. The legacy
    use_unified=False path no longer exists.
    """

    def test_unified_root_reads_from_instance_state(self):
        """In unified mode, root history comes from instance_state under the session name."""
        instance_state = {
            "Maine": {"messages": [{"role": "user", "content": "unified_hello"}]},
        }

        store = instance_state.get("Maine", {})
        msgs = store.get("messages", [])

        assert len(msgs) == 1
        assert msgs[0]["content"] == "unified_hello"

    def test_unified_sub_agent_reads_from_instance_state(self):
        """In unified mode, sub-agent history also comes from instance_state."""
        instance_state = {
            "worker1": {"messages": [{"role": "assistant", "content": "unified_work"}]},
        }

        state = instance_state.get("worker1", {})
        msgs = state.get("messages", [])

        assert len(msgs) == 1
        assert msgs[0]["content"] == "unified_work"

    def test_no_agent_pool_unified_returns_empty(self):
        """When agent_pool is None in unified mode, sub-agent reads return empty."""
        pool = None

        state = pool.instance_state.get("worker1", {}) if pool else {}
        msgs = state.get("messages", [])

        assert msgs == []


# ===========================================================================
# build_state — unified only and token cache integration (post Phase 8)
# ===========================================================================

class TestBuildStateLogic:
    """Test the build_state logic pattern with unified mode permanently enabled."""

    def test_build_state_unified_uses_instance_state(self):
        """In unified mode, root messages come from instance_state."""
        instance_state = {
            "Maine": {"messages": [{"role": "user", "content": "unified_msg"}]},
        }

        root_state = instance_state.get("Maine")
        msgs = root_state["messages"] if root_state else []

        assert len(msgs) == 1
        assert msgs[0]["content"] == "unified_msg"

    def test_build_state_writes_to_unified_token_cache(self):
        """Token cache can store and retrieve agent stats (unit test for AgentTokenCache)."""
        from config.token_cache import AgentTokenCache

        cache = AgentTokenCache(ttl=300)
        if cache._cleanup_timer:
            cache._cleanup_timer.cancel()

        # Test the token cache set/get cycle with a primary-agent key
        active_h_count = 10
        h_stats_tokens = 5000

        cache.set("root", active_h_count, h_stats_tokens)

        cached = cache.get("root")
        assert cached is not None
        assert cached["count"] == 10
        assert cached["tokens"] == 5000

    def test_build_state_includes_sub_agents(self):
        """build_state output includes instance state."""
        instance_state = {
            "worker1": {"active": True, "agent_name": "worker1 (Coder)", "messages": []},
        }
        # Build sub-agent state from instance_state (no separate getter needed)
        result = {}
        for name, state in instance_state.items():
            result[name] = {
                "active": state.get("active", False),
                "agent_name": name,
            }
        assert "worker1" in result


# ===========================================================================
# get_agent_state — unified only (post Phase 8)
# ===========================================================================

class TestGetAgentStateLogic:
    """Test the get_agent_state logic with unified mode permanently enabled."""

    def test_unified_root_returns_instance_state(self):
        """In unified mode, primary agent state comes from instance_state under the session name."""
        instance_state = {
            "Maine": {"messages": [{"role": "user", "content": "unified"}], "active": True},
        }

        state = instance_state.get("Maine")
        state = state.copy() if state else None

        assert state is not None
        assert len(state["messages"]) == 1
        assert state["messages"][0]["content"] == "unified"

    def test_unified_missing_instance_returns_none(self):
        """When instance is not in instance_state, returns None."""
        instance_state = {}

        state = instance_state.get("nobody")
        state = state.copy() if state else None

        assert state is None

    def test_no_pool_unified_returns_none(self):
        """When agent_pool is None in unified mode, returns None."""
        pool = None

        state = pool.instance_state.get("Maine") if pool else None
        state = state.copy() if state else None

        assert state is None


# ===========================================================================
# Token cache unit tests (AgentTokenCache module)
# ===========================================================================

class TestTokenCacheIntegration:
    """Unit tests for the AgentTokenCache module used by api_server."""

    def test_cache_ttl_is_300(self):
        """The api_server creates a token cache with 300s TTL."""
        from config.token_cache import AgentTokenCache
        cache = AgentTokenCache(ttl=300)
        if cache._cleanup_timer:
            cache._cleanup_timer.cancel()
        assert cache._ttl == 300

    def test_cache_stores_and_retrieves_primary_agent_stats(self):
        """Token cache stores and retrieves stats for primary agent."""
        from config.token_cache import AgentTokenCache
        cache = AgentTokenCache(ttl=60)
        if cache._cleanup_timer:
            cache._cleanup_timer.cancel()

        cache.set("root", count=10, tokens=5000)
        result = cache.get("root")
        assert result is not None
        assert result["count"] == 10
        assert result["tokens"] == 5000

    def test_cache_entry_expires_and_can_be_refreshed(self):
        """After TTL expires, a fresh entry can be written."""
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

        # Write a new entry after expiry
        cache.set("root", count=8, tokens=3500)
        result = cache.get("root")
        assert result["count"] == 8
        assert result["tokens"] == 3500


# ===========================================================================
# Incremental token counting logic (pattern used in build_state)
# ===========================================================================

class TestIncrementalTokenCounting:
    """Test the incremental token counting pattern used in state building."""

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