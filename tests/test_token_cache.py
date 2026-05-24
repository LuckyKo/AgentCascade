"""Unit tests for config.token_cache.AgentTokenCache.

Covers basic set/get, TTL expiration, invalidate/clear_all, cleanup_expired,
and thread-safety under concurrent reads/writes.
"""

import threading
import time

from config.token_cache import AgentTokenCache


# ===========================================================================
# Basic operations
# ===========================================================================

class TestBasicOperations:
    """Test the fundamental set/get/invalidate flow."""

    def test_set_and_get(self, short_ttl_cache):
        short_ttl_cache.set("agent1", count=5, tokens=1000)
        result = short_ttl_cache.get("agent1")
        assert result is not None
        assert result["count"] == 5
        assert result["tokens"] == 1000

    def test_get_nonexistent_returns_none(self, short_ttl_cache):
        assert short_ttl_cache.get("no_such_agent") is None

    def test_set_overwrites_existing(self, short_ttl_cache):
        short_ttl_cache.set("a", count=1, tokens=100)
        short_ttl_cache.set("a", count=2, tokens=200)
        result = short_ttl_cache.get("a")
        assert result["count"] == 2
        assert result["tokens"] == 200

    def test_get_does_not_return_timestamp(self, short_ttl_cache):
        """Timestamp is an internal detail; callers shouldn't see it."""
        short_ttl_cache.set("a", count=1, tokens=50)
        result = short_ttl_cache.get("a")
        assert "timestamp" not in result

    def test_size(self, short_ttl_cache):
        assert short_ttl_cache.size() == 0
        short_ttl_cache.set("a", 1, 10)
        short_ttl_cache.set("b", 2, 20)
        assert short_ttl_cache.size() == 2

    def test_repr(self, short_ttl_cache):
        short_ttl_cache.set("a", 1, 10)
        r = repr(short_ttl_cache)
        assert "AgentTokenCache" in r
        assert "ttl=1" in r


# ===========================================================================
# TTL expiration
# ===========================================================================

class TestTTLExpiration:
    """Test that expired entries are treated as missing."""

    def test_entry_expires_after_ttl(self, short_ttl_cache):
        short_ttl_cache.set("a", count=3, tokens=300)
        assert short_ttl_cache.get("a") is not None
        # Wait for TTL to expire
        time.sleep(1.1)
        assert short_ttl_cache.get("a") is None

    def test_expired_entry_not_counted_in_size(self, short_ttl_cache):
        short_ttl_cache.set("a", 1, 10)
        short_ttl_cache.set("b", 2, 20)
        assert short_ttl_cache.size() == 2
        time.sleep(1.1)
        # size() reads under lock but does NOT expire — it just counts raw entries
        # However get() triggers lazy deletion on access
        assert short_ttl_cache.get("a") is None  # lazily removes "a"
        assert short_ttl_cache.size() == 1       # only "b" left

    def test_fresh_entry_does_not_expire(self, short_ttl_cache):
        short_ttl_cache.set("a", 5, 500)
        time.sleep(0.2)  # well within TTL of 1 second
        result = short_ttl_cache.get("a")
        assert result is not None
        assert result["count"] == 5


# ===========================================================================
# Invalidate and clear_all
# ===========================================================================

class TestInvalidateAndClear:
    """Test explicit cache invalidation."""

    def test_invalidate_single_entry(self, short_ttl_cache):
        short_ttl_cache.set("a", 1, 10)
        short_ttl_cache.set("b", 2, 20)
        short_ttl_cache.invalidate("a")
        assert short_ttl_cache.get("a") is None
        assert short_ttl_cache.get("b") is not None

    def test_invalidate_nonexistent_is_noop(self, short_ttl_cache):
        # Should not raise
        short_ttl_cache.invalidate("ghost")

    def test_clear_all(self, short_ttl_cache):
        short_ttl_cache.set("a", 1, 10)
        short_ttl_cache.set("b", 2, 20)
        short_ttl_cache.clear_all()
        assert short_ttl_cache.size() == 0
        assert short_ttl_cache.get("a") is None
        assert short_ttl_cache.get("b") is None

    def test_clear_all_on_empty(self, short_ttl_cache):
        # Should not raise
        short_ttl_cache.clear_all()


# ===========================================================================
# cleanup_expired
# ===========================================================================

class TestCleanupExpired:
    """Test the periodic cleanup method."""

    def test_cleanup_removes_expired_entries(self, short_ttl_cache):
        short_ttl_cache.set("old", 1, 10)
        time.sleep(1.1)
        short_ttl_cache.set("new", 2, 20)
        # Before cleanup: "old" still in the raw dict (lazy delete on get)
        assert short_ttl_cache.size() == 2
        short_ttl_cache.cleanup_expired()
        assert short_ttl_cache.size() == 1
        assert short_ttl_cache.get("new") is not None

    def test_cleanup_does_not_remove_fresh_entries(self, short_ttl_cache):
        short_ttl_cache.set("fresh", 5, 500)
        short_ttl_cache.cleanup_expired()
        assert short_ttl_cache.get("fresh") is not None

    def test_cleanup_on_empty(self, short_ttl_cache):
        # Should not raise
        short_ttl_cache.cleanup_expired()


# ===========================================================================
# Thread-safety: concurrent reads and writes
# ===========================================================================

class TestThreadSafety:
    """Test that concurrent access does not corrupt the cache."""

    def test_concurrent_set_get(self, normal_ttl_cache):
        """Multiple threads writing different keys; readers should see valid data."""
        errors = []

        def writer(agent_id):
            try:
                for i in range(100):
                    normal_ttl_cache.set(f"agent-{agent_id}-{i}", count=i, tokens=i * 10)
            except Exception as e:
                errors.append(f"writer-{agent_id}: {e}")

        def reader():
            try:
                for _ in range(200):
                    # Read random keys — some may be None (not yet written), that's fine
                    normal_ttl_cache.get("random-key")
            except Exception as e:
                errors.append(f"reader: {e}")

        threads = []
        for agent_id in range(5):
            t = threading.Thread(target=writer, args=(agent_id,))
            threads.append(t)
        for _ in range(3):
            t = threading.Thread(target=reader)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent test errors: {errors}"
        # All 5 writers × 100 entries = 500 entries should exist
        assert normal_ttl_cache.size() == 500

    def test_concurrent_invalidate_and_get(self, normal_ttl_cache):
        """Readers and invalidators running concurrently should not crash."""
        errors = []

        for i in range(50):
            normal_ttl_cache.set(f"k{i}", count=i, tokens=i)

        def invalidate_one(idx):
            try:
                for _ in range(20):
                    normal_ttl_cache.invalidate(f"k{idx % 50}")
            except Exception as e:
                errors.append(str(e))

        def read_one(idx):
            try:
                for _ in range(20):
                    normal_ttl_cache.get(f"k{idx % 50}")
            except Exception as e:
                errors.append(str(e))

        threads = []
        for i in range(10):
            threads.append(threading.Thread(target=invalidate_one, args=(i,)))
            threads.append(threading.Thread(target=read_one, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent invalidate/get errors: {errors}"

    def test_concurrent_clear_and_set(self, normal_ttl_cache):
        """clear_all and set running concurrently should not crash."""
        errors = []

        def setter():
            try:
                for i in range(100):
                    normal_ttl_cache.set(f"key-{i}", count=i, tokens=i)
            except Exception as e:
                errors.append(str(e))

        def clearer():
            try:
                for _ in range(50):
                    normal_ttl_cache.clear_all()
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=setter),
            threading.Thread(target=clearer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent clear/set errors: {errors}"

    def test_concurrent_cleanup_and_set(self, short_ttl_cache):
        """cleanup_expired and set running concurrently should not crash."""
        errors = []

        def setter():
            try:
                for i in range(50):
                    short_ttl_cache.set(f"key-{i}", count=i, tokens=i)
            except Exception as e:
                errors.append(str(e))

        def cleaner():
            try:
                for _ in range(30):
                    short_ttl_cache.cleanup_expired()
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=setter),
            threading.Thread(target=cleaner),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent cleanup/set errors: {errors}"


# ===========================================================================
# Cleanup timer lifecycle
# ===========================================================================

class TestCleanupTimer:
    """Test that the background cleanup timer starts and can be cancelled."""

    def test_timer_started_on_init(self):
        cache = AgentTokenCache(ttl=300)
        assert cache._cleanup_timer is not None
        assert cache._cleanup_timer.is_alive()
        cache._cleanup_timer.cancel()

    def test_timer_is_daemon(self):
        """Timer should be a daemon thread so it doesn't block program exit."""
        cache = AgentTokenCache(ttl=300)
        assert cache._cleanup_timer.daemon is True
        cache._cleanup_timer.cancel()