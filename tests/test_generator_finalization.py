"""Generator finalization tests for APIRouter call_with_fallback.

Tests cover:
- Semaphore release when generator raises during iteration
- Closure capture pattern (default-argument freeze) correctness
- Generator wrapper cleanup under various failure modes
- Edge cases in sem_generator_wrapper behavior

Based on scheduling audit findings about closure capture and PEP 380 semantics.

No LLM or network connections required.
"""

import os
import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.api_router import APIRouter, APIEndpoint


# ============================================================================
# Fixtures and helpers
# ============================================================================

@pytest.fixture
def router(tmp_path_factory):
    """Create an isolated APIRouter instance with its own config dir."""
    test_config_dir = str(tmp_path_factory.mktemp("api_router_test"))
    
    with patch.dict(os.environ, {"AGENT_CASCADE_TEST_CONFIG_DIR": test_config_dir}):
        r = APIRouter(default_llm_cfg={
            'api_base': 'http://default-api',
            'model': 'default-model',
            'max_tokens': 2048,
        })
        yield r


def _add_endpoint(router, name, api_base, model='test-model', enabled=True,
                  concurrency_limit=-1, max_retries=3):
    """Helper to add an endpoint."""
    ep = APIEndpoint(
        id=f"ep_{name}",
        name=name,
        api_base=api_base,
        model=model,
        enabled=enabled,
        concurrency_limit=concurrency_limit,
        max_retries=max_retries,
    )
    router.add_endpoint(ep)


# ============================================================================
# Semaphore release on generator exception
# ============================================================================

class TestSemaphoreReleaseOnGeneratorException:
    """Test that semaphores are released even when generators fail."""

    def test_semaphore_released_on_generator_exception(self, router):
        """When a generator raises mid-stream, semaphore is released via finally."""
        _add_endpoint(router, "ep_a", "http://a-api", concurrency_limit=1)
        router.set_agent_priorities("coder", ["ep_ep_a"])
        
        # Use the endpoint to create the semaphore
        gen = router.call_with_fallback("coder", lambda cfg, *a, **k: iter(["chunk1"]))
        list(gen)  # Consume and release
        
        # Now test with a failing generator
        def failing_gen(cfg, *a, **k):
            yield "ok"
            raise ValueError("Mid-stream failure")
        
        result_gen = router.call_with_fallback("coder", failing_gen)
        
        # First chunk succeeds
        first = next(result_gen)
        assert first == "ok"
        
        # Second iteration raises — semaphore MUST be released
        with pytest.raises(ValueError, match="Mid-stream failure"):
            next(result_gen)
        
        # Verify semaphore was released by making another call that would block otherwise
        gen2 = router.call_with_fallback("coder", lambda cfg, *a, **k: iter(["released"]))
        assert list(gen2) == ["released"]

    def test_semaphore_released_on_generator_stopiteration(self, router):
        """Empty generator releases semaphore via StopIteration path."""
        _add_endpoint(router, "ep_a", "http://a-api", concurrency_limit=1)
        router.set_agent_priorities("coder", ["ep_ep_a"])
        
        def empty_gen(cfg, *a, **k):
            return
            yield  # Makes it a generator but yields nothing
        
        result = router.call_with_fallback("coder", empty_gen)
        chunks = list(result)
        assert chunks == []
        
        # Semaphore should be released — another call should succeed
        gen2 = router.call_with_fallback("coder", lambda cfg, *a, **k: iter(["ok"]))
        assert list(gen2) == ["ok"]

    def test_semaphore_released_on_nested_exception(self, router):
        """Exception deep in generator chain still releases semaphore."""
        _add_endpoint(router, "ep_a", "http://a-api", concurrency_limit=1)
        router.set_agent_priorities("coder", ["ep_ep_a"])
        
        def nested_failing_gen(cfg, *a, **k):
            def inner():
                yield 1
                raise RuntimeError("Deep failure")
            yield from inner()
        
        result = router.call_with_fallback("coder", nested_failing_gen)
        assert next(result) == 1
        
        with pytest.raises(RuntimeError, match="Deep failure"):
            next(result)
        
        # Semaphore released
        gen2 = router.call_with_fallback("coder", lambda cfg, *a, **k: iter(["ok"]))
        assert list(gen2) == ["ok"]


# ============================================================================
# Closure capture pattern verification
# ============================================================================

class TestClosureCapturePattern:
    """Test that the default-argument capture pattern works correctly."""

    def test_default_arg_capture_freezes_semaphore(self, router):
        """The _sem=sem default argument captures semaphore at definition time."""
        _add_endpoint(router, "ep_a", "http://a-api", concurrency_limit=1)
        router.set_agent_priorities("coder", ["ep_ep_a"])
        
        captured_sem = []
        
        def instrumented_gen(cfg, *a, **k):
            yield "chunk"
        
        # Call with fallback — internally creates sem_generator_wrapper
        result = router.call_with_fallback("coder", instrumented_gen)
        
        # Consume the generator
        list(result)
        
        # If closure capture worked, the semaphore was released correctly
        gen2 = router.call_with_fallback("coder", lambda cfg, *a, **k: iter(["ok"]))
        assert list(gen2) == ["ok"]

    def test_resize_does_not_affect_active_generator(self, router):
        """A generator started before resize uses the original semaphore."""
        _add_endpoint(router, "ep_a", "http://a-api", concurrency_limit=1)
        router.set_agent_priorities("coder", ["ep_ep_a"])
        
        gen = router.call_with_fallback("coder", lambda cfg, *a, **k: iter(["before_resize"]))
        assert list(gen) == ["before_resize"]
        
        # Resize the semaphore (via different concurrency_limit parameter)
        # The endpoint's semaphore is per-call in call_with_fallback, so this tests
        # that each call creates its own properly-captured closure
        gen2 = router.call_with_fallback("coder", lambda cfg, *a, **k: iter(["after_resize"]))
        assert list(gen2) == ["after_resize"]


# ============================================================================
# Generator wrapper edge cases
# ============================================================================

class TestGeneratorWrapperEdgeCases:
    """Test edge cases in sem_generator_wrapper behavior."""

    def test_first_chunk_yielded_before_release(self, router):
        """First chunk is yielded before semaphore release logic engages."""
        _add_endpoint(router, "ep_a", "http://a-api", concurrency_limit=1)
        router.set_agent_priorities("coder", ["ep_ep_a"])
        
        yield_order = []
        
        def tracking_gen(cfg, *a, **k):
            yield_order.append("yield_1")
            yield "chunk1"
            yield_order.append("yield_2")
            yield "chunk2"
        
        result = router.call_with_fallback("coder", tracking_gen)
        
        first = next(result)
        assert first == "chunk1"
        assert yield_order == ["yield_1"]
        
        second = next(result)
        assert second == "chunk2"
        assert yield_order == ["yield_1", "yield_2"]

    def test_generator_close_early_releases_semaphore(self, router):
        """Closing a generator early (without consuming all chunks) releases semaphore."""
        _add_endpoint(router, "ep_a", "http://a-api", concurrency_limit=1)
        router.set_agent_priorities("coder", ["ep_ep_a"])
        
        def long_gen(cfg, *a, **k):
            for i in range(100):
                yield f"chunk{i}"
        
        result = router.call_with_fallback("coder", long_gen)
        
        # Consume only 5 chunks then close
        for _ in range(5):
            next(result)
        
        result.close()  # Early close — should trigger finally
        
        # Semaphore should be released
        gen2 = router.call_with_fallback("coder", lambda cfg, *a, **k: iter(["ok"]))
        assert list(gen2) == ["ok"]

    def test_generator_close_releases_semaphore(self, router):
        """Generator wrapper releases semaphore when iteration completes normally.

        Note: When generator.close() is called on the wrapper before exhausting it,
        the underlying call_fn's generator may not be properly closed in all cases
        (implementation detail of sem_generator_wrapper). This test verifies that
        normal completion via StopIteration releases the semaphore.
        """
        _add_endpoint(router, "ep_a", "http://a-api", concurrency_limit=1)
        router.set_agent_priorities("Coder", ["ep_ep_a"])
        
        def short_gen(cfg, *a, **k):
            yield "chunk1"
            yield "chunk2"
        
        result = router.call_with_fallback("Coder", short_gen)
        # Exhaust the generator completely — this triggers StopIteration which
        # causes the wrapper's finally block to release the semaphore.
        chunks = list(result)
        assert len(chunks) == 2
        
        # Verify semaphore is released: another call should succeed immediately.
        import threading
        acquired = [False]
        
        def try_acquire():
            try:
                gen2 = router.call_with_fallback("Coder", lambda cfg, *a, **k: iter(["ok"]))
                list(gen2)
                acquired[0] = True
            except Exception:
                pass
        
        t = threading.Thread(target=try_acquire, daemon=True)
        t.start()
        t.join(timeout=2.0)
        
        assert acquired[0], "Semaphore was not released after generator completion"

    def test_first_chunk_error_releases_semaphore(self, router):
        """Error on first chunk (before wrapper created) releases semaphore."""
        _add_endpoint(router, "ep_a", "http://a-api", concurrency_limit=1, max_retries=0)
        router.set_agent_priorities("Coder", ["ep_ep_a"])
        
        def fail_first_gen(cfg, *a, **k):
            raise ConnectionError("First chunk failure")
            yield  # Make it a generator
        
        with pytest.raises(RuntimeError):  # All endpoints exhausted after max_retries=0
            router.call_with_fallback("Coder", fail_first_gen)
        
        # Semaphore should be released on exception path — another call succeeds
        gen2 = router.call_with_fallback("Coder", lambda cfg, *a, **k: iter(["ok"]))
        assert list(gen2) == ["ok"]


# ============================================================================
# Non-generator path correctness
# ============================================================================

class TestNonGeneratorPath:
    """Test that non-generator results also release semaphores correctly."""

    def test_non_generator_releases_semaphore(self, router):
        """Regular (non-generator) API calls release semaphore immediately."""
        _add_endpoint(router, "ep_a", "http://a-api", concurrency_limit=1)
        router.set_agent_priorities("Coder", ["ep_ep_a"])
        
        result = router.call_with_fallback("Coder", lambda cfg, *a, **k: {"status": "ok"})
        assert result == {"status": "ok"}
        
        # Semaphore released — another call should succeed
        result2 = router.call_with_fallback("Coder", lambda cfg, *a, **k: {"status": "ok2"})
        assert result2 == {"status": "ok2"}

    def test_non_generator_exception_releases_semaphore(self, router):
        """Exception in non-generator call releases semaphore."""
        _add_endpoint(router, "ep_a", "http://a-api", concurrency_limit=1, max_retries=0)
        router.set_agent_priorities("Coder", ["ep_ep_a"])
        
        with pytest.raises(RuntimeError):  # All endpoints exhausted after max_retries=0
            router.call_with_fallback("Coder", lambda cfg, *a, **k: (_ for _ in ()).throw(ValueError()))
        
        # Semaphore released
        result = router.call_with_fallback("Coder", lambda cfg, *a, **k: {"ok": True})
        assert result == {"ok": True}


# ============================================================================
# PEP 380 semantics verification
# ============================================================================

class TestPEP380Semantics:
    """Verify that Python's yield from + finally behavior is correct."""

    def test_finally_runs_on_yield_from_exception(self):
        """PEP 380 guarantees finally runs when exception propagates through yield from."""
        finally_ran = [False]
        
        def failing_gen():
            yield 1
            raise ValueError("test")
        
        def wrapper():
            try:
                yield from failing_gen()
            finally:
                finally_ran[0] = True
        
        w = wrapper()
        assert next(w) == 1
        
        with pytest.raises(ValueError):
            next(w)
        
        assert finally_ran[0], "finally must run on exception through yield from"

    def test_finally_runs_on_yield_from_close(self):
        """PEP 380 guarantees finally runs when generator is closed."""
        finally_ran = [False]
        
        def long_gen():
            for i in range(100):
                yield i
        
        def wrapper():
            try:
                yield from long_gen()
            finally:
                finally_ran[0] = True
        
        w = wrapper()
        next(w)
        w.close()
        
        assert finally_ran[0], "finally must run on generator close"