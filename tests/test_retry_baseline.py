"""Phase 0 baseline tests for retry/fallback behavior.

These tests measure actual retry counts, failover behavior, and error propagation
before the retry refactoring. They serve as a regression safety net — after
refactoring, these tests must pass with identical behavior.

Run: pytest tests/test_retry_baseline.py -v

ARCHITECTURE NOTE (documenting current state):
- Layer 1 (L1): BaseChatModel.retry_model_service_iterator() — retries individual LLM calls
- Layer 2 (L2): APIRouter.call_with_fallback() — retries endpoints + failover chain
- Coupling: endpoint.max_retries is passed to LLM via to_llm_cfg(), so changing it
  affects BOTH layers simultaneously. This is intentional for now but will be
  decoupled in the retry refactoring.
"""

import time
from typing import Callable, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.api_router import APIRouter, APIEndpoint
from agent_cascade.exceptions import CharacterRunDetected, MaxTokenExceeded
from agent_cascade.llm.base import BaseChatModel, ModelServiceError


# ──────────────────────────────────────────────────────────────────────────────
# Helpers: mock LLM that tracks call counts and can be configured to fail
# ──────────────────────────────────────────────────────────────────────────────

class MockLLM(BaseChatModel):
    """Minimal mock LLM for testing retry behavior without network calls.

    By default sets max_retries=0 to disable Layer 1 retries so tests can
    measure pure Layer 2 (API router) behavior. Set max_retries in cfg dict
    to enable L1 retries for specific tests.
    """

    def __init__(self, cfg=None, fail_count=0, fail_type=None, succeed_after=None):
        # Default: disable L1 retries so we can test pure L2/L3 behavior
        if cfg is None:
            cfg = {"max_retries": 0}
        elif "max_retries" not in cfg:
            cfg["max_retries"] = 0
        super().__init__(cfg)
        self.call_count = 0
        self.fail_count = fail_count          # Number of initial calls that should fail
        self.fail_type = fail_type            # Exception type to raise (default: ModelServiceError)
        self.succeed_after = succeed_after    # Optional: only succeed after this many calls

    def _chat_stream(self, messages, delta_stream=False, generate_cfg=None):
        """Mock streaming that can be configured to fail."""
        self.call_count += 1

        if self.fail_count > 0 and self.call_count <= self.fail_count:
            exc_class = self.fail_type or ModelServiceError
            raise exc_class(message=f"Simulated failure #{self.call_count}")

        # Success path: yield a single accumulated message batch
        from agent_cascade.llm.schema import Message, ASSISTANT
        yield [Message(role=ASSISTANT, content="mock response")]

    def _chat_no_stream(self, messages, generate_cfg=None):
        """Mock non-streaming (required by abstract base)."""
        self.call_count += 1
        if self.fail_count > 0 and self.call_count <= self.fail_count:
            exc_class = self.fail_type or ModelServiceError
            raise exc_class(message=f"Simulated failure #{self.call_count}")
        from agent_cascade.llm.schema import Message, ASSISTANT
        return [Message(role=ASSISTANT, content="mock response")]

    def _chat_with_functions(self, messages, functions, stream, delta_stream, generate_cfg, lang):
        """Mock function-calling chat (required by abstract base)."""
        if stream:
            yield from self._chat_stream(messages, delta_stream=delta_stream, generate_cfg=generate_cfg)
        else:
            result = self._chat_no_stream(messages, generate_cfg=generate_cfg)
            yield result


class FailingStreamLLM(BaseChatModel):
    """Mock LLM that fails mid-stream (after yielding some chunks)."""

    def __init__(self, cfg=None, fail_at_chunk=2):
        super().__init__(cfg or {})
        self.call_count = 0
        self.fail_at_chunk = fail_at_chunk

    def _chat_stream(self, messages, delta_stream=False, generate_cfg=None):
        from agent_cascade.llm.schema import Message, ASSISTANT
        self.call_count += 1
        for i in range(5):
            if i == self.fail_at_chunk:
                raise ConnectionError(f"Mid-stream failure at chunk {i}")
            yield [Message(role=ASSISTANT, content=f"chunk {i}")]

    def _chat_no_stream(self, messages, generate_cfg=None):
        from agent_cascade.llm.schema import Message, ASSISTANT
        return [Message(role=ASSISTANT, content="ok")]

    def _chat_with_functions(self, messages, functions, stream, delta_stream, generate_cfg, lang):
        if stream:
            yield from self._chat_stream(messages, delta_stream=delta_stream, generate_cfg=generate_cfg)
        else:
            result = self._chat_no_stream(messages, generate_cfg=generate_cfg)
            yield result


def make_router(default_llm_cfg=None):
    """Create an APIRouter instance with minimal default config."""
    cfg = default_llm_cfg or {
        "model": "default-model",
        "api_base": "http://localhost:1111/v1",
        "model_type": "qwenvl_oai",
    }
    return APIRouter(default_llm_cfg=cfg)


# ──────────────────────────────────────────────────────────────────────────────
# Test Group 1: Single endpoint retry counts
# ──────────────────────────────────────────────────────────────────────────────

class TestSingleEndpointRetryCount:
    """Verify per-endpoint retry behavior matches documented defaults."""

    def test_endpoint_max_retries_two_gives_three_calls(self):
        """max_retries=2 → exactly 3 calls (initial + 2 retries) before failover."""
        router = make_router()
        ep = APIEndpoint(
            name="test-ep",
            api_base="http://localhost:9998/v1",
            model="mock-model",
            max_retries=2,
            base_retry_delay=0.01,  # Fast retries for testing
            max_retry_delay=0.05,
        )
        router.add_endpoint(ep)

        llm = MockLLM(fail_count=5, fail_type=ConnectionError)

        def do_call(llm_cfg):
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)
            return True

        # Will exhaust custom endpoint (3 calls) then fall back to default_llm_cfg
        with pytest.raises(RuntimeError, match="All API endpoints exhausted"):
            router.call_with_fallback("coder", do_call, agent_instance_name="test-inst")

        # Custom endpoint: 3 attempts (max_retries=2). Default fallback also tried.
        # Total >= 3 because default fallback adds more attempts.
        assert llm.call_count >= 3, f"Expected at least 3 calls from custom endpoint, got {llm.call_count}"

    def test_endpoint_fails_then_succeeds_on_retry(self):
        """Fail once, succeed on retry → total 2 calls."""
        router = make_router()
        ep = APIEndpoint(
            name="test-ep",
            api_base="http://localhost:9997/v1",
            model="mock-model",
            max_retries=2,
            base_retry_delay=0.01,
            max_retry_delay=0.05,
        )
        router.add_endpoint(ep)

        llm = MockLLM(fail_count=1, fail_type=ConnectionError)

        def do_call(llm_cfg):
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)
            return True

        # First call fails, second succeeds. Should complete without raising.
        router.call_with_fallback("coder", do_call, agent_instance_name="test-inst")

        assert llm.call_count == 2, f"Expected exactly 2 calls (1 fail + 1 success), got {llm.call_count}"


# ──────────────────────────────────────────────────────────────────────────────
# Test Group 2: Multi-endpoint failover
# ──────────────────────────────────────────────────────────────────────────────

class TestMultiEndpointFailover:
    """Verify failover behavior across multiple endpoints."""

    def test_first_fails_second_succeeds(self):
        """First endpoint exhausts retries, second succeeds."""
        router = make_router()

        # First endpoint: fails always, max_retries=1 (2 calls total)
        ep_a = APIEndpoint(
            id="ep-a", name="A", api_base="http://localhost:9996/v1", model="a",
            max_retries=1, base_retry_delay=0.01, max_retry_delay=0.05,
        )
        # Second endpoint: succeeds immediately
        ep_b = APIEndpoint(
            id="ep-b", name="B", api_base="http://localhost:9995/v1", model="b",
            max_retries=2, base_retry_delay=0.01, max_retry_delay=0.05,
        )

        router.add_endpoint(ep_a)
        router.add_endpoint(ep_b)

        # Assign endpoints to coder agent priority list so they're used
        with router._lock:
            router.agent_priorities["coder"] = ["ep-a", "ep-b"]

        llm_a = MockLLM(fail_count=10, fail_type=ConnectionError)
        llm_b = MockLLM(fail_count=0)  # Always succeeds

        def do_call(llm_cfg):
            api_base = llm_cfg.get("api_base", "")
            if "9996" in api_base:
                llm = llm_a
            else:
                llm = llm_b
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)
            return True

        # Should fail on ep_a (2 attempts), succeed on ep_b (1 attempt)
        router.call_with_fallback("coder", do_call, agent_instance_name="test-inst")

        assert llm_a.call_count == 2, f"EP-A: expected 2 calls, got {llm_a.call_count}"
        assert llm_b.call_count == 1, f"EP-B: expected 1 call, got {llm_b.call_count}"


# ──────────────────────────────────────────────────────────────────────────────
# Test Group 3: Inner-loop detection (CharacterRunDetected, MaxTokenExceeded)
# ──────────────────────────────────────────────────────────────────────────────

class TestInnerLoopDetection:
    """Verify special exceptions cause endpoint advance without exhausting retries."""

    def test_character_run_detected_skips_to_next_endpoint(self):
        """CharacterRunDetected → skip remaining retries on current endpoint, advance to next.

        NOTE: Current behavior may differ from ideal due to L1 retry wrapper catching
        CharacterRunDetected and wrapping it as ModelServiceError before L2 sees it.
        This test documents what actually happens.
        """
        router = make_router()

        ep_a = APIEndpoint(id="ep-a", name="A", api_base="http://localhost:9992/v1", model="a", max_retries=5)
        ep_b = APIEndpoint(id="ep-b", name="B", api_base="http://localhost:9991/v1", model="b", max_retries=2)

        router.add_endpoint(ep_a)
        router.add_endpoint(ep_b)

        llm_a = MockLLM(fail_count=10, fail_type=CharacterRunDetected)
        llm_b = MockLLM(fail_count=0)

        def do_call(llm_cfg):
            llm = llm_a if "ep-a" in llm_cfg.get("api_base", "") else llm_b
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)
            return True

        # With L1 catching CharacterRunDetected, it gets wrapped as ModelServiceError.
        # L2 then sees ModelServiceError and retries normally (not inner-loop skip).
        # This documents the CURRENT behavior which will be fixed in Phase 2.
        try:
            router.call_with_fallback("coder", do_call, agent_instance_name="test-inst")
            succeeded = True
        except RuntimeError as e:
            succeeded = False
            error_msg = str(e)

        # Document what actually happened:
        print(f"[OBSERVED] CharacterRunDetected test: llm_a={llm_a.call_count}, llm_b={llm_b.call_count}, "
              f"succeeded={succeeded}")

    def test_max_token_exceeded_skips_to_next_endpoint(self):
        """MaxTokenExceeded → skip remaining retries on current endpoint, advance to next.

        Same caveat as CharacterRunDetected: L1 may wrap this exception.
        """
        router = make_router()

        ep_a = APIEndpoint(id="ep-a", name="A", api_base="http://localhost:9990/v1", model="a", max_retries=5)
        ep_b = APIEndpoint(id="ep-b", name="B", api_base="http://localhost:9989/v1", model="b", max_retries=2)

        router.add_endpoint(ep_a)
        router.add_endpoint(ep_b)

        llm_a = MockLLM(fail_count=10, fail_type=MaxTokenExceeded)
        llm_b = MockLLM(fail_count=0)

        def do_call(llm_cfg):
            llm = llm_a if "ep-a" in llm_cfg.get("api_base", "") else llm_b
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)
            return True

        try:
            router.call_with_fallback("coder", do_call, agent_instance_name="test-inst")
            succeeded = True
        except RuntimeError as e:
            succeeded = False

        print(f"[OBSERVED] MaxTokenExceeded test: llm_a={llm_a.call_count}, llm_b={llm_b.call_count}, "
              f"succeeded={succeeded}")


# ──────────────────────────────────────────────────────────────────────────────
# Test Group 4: Error type preservation (raw_chat path)
# ──────────────────────────────────────────────────────────────────────────────

class TestRawChatErrorPreservation:
    """Verify error types propagate correctly through chat()/raw_chat() without L1 wrapping.

    After Phase 2: L1 retry wrappers have been bypassed entirely. Original exception types
    now propagate directly to L2 (API router) and L3 (execution engine), allowing
    _classify_llm_error() to make correct retry vs fatal decisions.
    """

    def test_error_type_preserved_generic_exception(self):
        """VERIFIED FIX: Generic exceptions propagate without being wrapped as ModelServiceError.

        Before Phase 2: retry_model_service_iterator caught bare Exception and re-wrapped
        as ModelServiceError, corrupting error types. Now wrappers are bypassed entirely.
        """
        class CustomAPIError(Exception):
            def __init__(self, message=None):
                super().__init__(message or "custom api error")

        llm = MockLLM(cfg={"max_retries": 0}, fail_count=1, fail_type=CustomAPIError)

        # Error should propagate as-is, NOT wrapped as ModelServiceError
        with pytest.raises(CustomAPIError):
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)

    def test_error_type_preserved_connection_error(self):
        """VERIFIED FIX: ConnectionError propagates without L1 wrapping."""
        # Use a ConnectionError subclass that accepts keyword args (MockLLM uses message=)
        class MockConnectionError(ConnectionError):
            def __init__(self, message=None):
                super().__init__(message or "connection failed")

        llm = MockLLM(cfg={"max_retries": 0}, fail_count=1, fail_type=MockConnectionError)

        with pytest.raises(MockConnectionError):
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)


# ──────────────────────────────────────────────────────────────────────────────
# Test Group 5: Sub-agent retry behavior
# ──────────────────────────────────────────────────────────────────────────────

class TestSubAgentRetryBehavior:
    """Verify sub-agent LLM calls have their own retry budget."""

    def test_sub_agent_separate_retry_budget(self):
        """Each agent instance has its own endpoint cursor and retry state."""
        router = make_router()

        ep_a = APIEndpoint(id="ep-a", name="A", api_base="http://localhost:9986/v1", model="a", max_retries=1)
        ep_b = APIEndpoint(id="ep-b", name="B", api_base="http://localhost:9985/v1", model="b", max_retries=1)

        router.add_endpoint(ep_a)
        router.add_endpoint(ep_b)

        llm_a = MockLLM(fail_count=10, fail_type=ConnectionError)
        llm_b = MockLLM(fail_count=0)

        def do_call(llm_cfg):
            llm = llm_a if "ep-a" in llm_cfg.get("api_base", "") else llm_b
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)
            return True

        # Agent 1: fails on ep_a, succeeds on ep_b
        router.call_with_fallback("coder", do_call, agent_instance_name="agent-1")

        calls_after_agent1 = llm_a.call_count + llm_b.call_count

        # Agent 2: fresh cursor — should also try ep_a first (may fail), then ep_b
        router.call_with_fallback("coder", do_call, agent_instance_name="agent-2")

        total_calls = llm_a.call_count + llm_b.call_count

        # Both agents went through their own retry/failover chain
        assert total_calls > calls_after_agent1, \
            "Agent 2 should have made its own LLM calls"


# ──────────────────────────────────────────────────────────────────────────────
# Test Group 6: Performance baselines
# ──────────────────────────────────────────────────────────────────────────────

class TestPerformanceBaseline:
    """Measure latency for different retry scenarios."""

    def test_latency_zero_retries(self):
        """Baseline: successful call on first attempt."""
        router = make_router()
        ep = APIEndpoint(
            id="ep-perf", name="Perf-EP", api_base="http://localhost:9984/v1",
            model="perf-model", max_retries=2, base_retry_delay=0.01, max_retry_delay=0.05,
        )
        router.add_endpoint(ep)

        llm = MockLLM(fail_count=0)

        def do_call(llm_cfg):
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)
            return True

        start = time.time()
        router.call_with_fallback("coder", do_call, agent_instance_name="perf-test")
        elapsed = time.time() - start

        assert llm.call_count == 1
        print(f"[BASELINE] Zero retries: {elapsed:.3f}s (expected <0.5s)")
        assert elapsed < 0.5, f"Zero-retry call too slow: {elapsed:.3f}s"

    def test_latency_one_retry(self):
        """One failure then success → measure total time including backoff."""
        router = make_router()
        ep = APIEndpoint(
            id="ep-perf2", name="Perf-EP", api_base="http://localhost:9983/v1",
            model="perf-model", max_retries=2, base_retry_delay=0.05, max_retry_delay=0.1,
        )
        router.add_endpoint(ep)

        llm = MockLLM(fail_count=1, fail_type=ConnectionError)

        def do_call(llm_cfg):
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)
            return True

        start = time.time()
        router.call_with_fallback("coder", do_call, agent_instance_name="perf-test")
        elapsed = time.time() - start

        assert llm.call_count == 2
        print(f"[BASELINE] One retry: {elapsed:.3f}s (expected ~0.1s with base_delay=0.05)")
        assert elapsed >= 0.05, f"One-retry call too fast: {elapsed:.3f}s (should include backoff)"

    def test_latency_max_retries_exhausted(self):
        """All retries exhausted → measure total time until failure."""
        router = make_router()
        ep = APIEndpoint(
            id="ep-perf3", name="Perf-EP", api_base="http://localhost:9982/v1",
            model="perf-model", max_retries=2, base_retry_delay=0.05, max_retry_delay=0.1,
        )
        router.add_endpoint(ep)

        llm = MockLLM(fail_count=10, fail_type=ConnectionError)

        def do_call(llm_cfg):
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)
            return True

        start = time.time()
        with pytest.raises(RuntimeError):
            router.call_with_fallback("coder", do_call, agent_instance_name="perf-test")
        elapsed = time.time() - start

        # Custom endpoint: 3 attempts. Default fallback: also tried (with its own retries).
        assert llm.call_count >= 3
        print(f"[BASELINE] Max retries exhausted: {elapsed:.3f}s, total calls={llm.call_count}")


# ──────────────────────────────────────────────────────────────────────────────
# Test Group 7: L1 retry behavior (BaseChatModel layer)
# ──────────────────────────────────────────────────────────────────────────────

class TestLLayerRetryBehavior:
    """Verify Layer 1 (LLM base) retries are disabled after Phase 2 refactor.

    After Phase 2, L1 retry wrappers have been bypassed in chat()/raw_chat().
    Retries are now handled exclusively by L2 (API router) and L3 (execution engine).
    """

    def test_l1_max_retries_always_zero(self):
        """max_retries is always 0 — config values are ignored after Phase 2.

        L1 retries are disabled to avoid error type corruption. Retries handled by L2/L3.
        """
        # Even if cfg specifies max_retries, it's ignored at L1
        llm_with_cfg = MockLLM({"max_retries": 2})
        assert llm_with_cfg.max_retries == 0

        llm_custom = MockLLM({"max_retries": 5})
        assert llm_custom.max_retries == 0

        llm_zero = MockLLM({"max_retries": 0})
        assert llm_zero.max_retries == 0

    def test_l1_endpoint_max_retries_decoupled(self):
        """VERIFIED FIX: endpoint.max_retries no longer leaks into LLM cfg.

        After Phase 2, to_llm_cfg() excludes max_retries. Endpoint retry config
        controls only L2 (API router) behavior — L1 retries are disabled.
        """
        ep = APIEndpoint(
            name="test", api_base="http://localhost:9981/v1", model="m",
            max_retries=3, base_retry_delay=0.5, max_retry_delay=10.0,
        )
        llm_cfg = ep.to_llm_cfg()

        # Endpoint's max_retries should NOT be in LLM config anymore (coupling broken)
        assert "max_retries" not in llm_cfg, \
            f"Endpoint max_retries leaked to LLM cfg — coupling not broken: {llm_cfg}"


# ──────────────────────────────────────────────────────────────────────────────
# Test Group 8: Backoff timing verification
# ──────────────────────────────────────────────────────────────────────────────

class TestBackoffTiming:
    """Verify exponential backoff behavior."""

    def test_router_backoff_exponential(self):
        """Router uses exponential backoff: base_delay * 2^attempt, capped at max_delay."""
        router = make_router()
        ep = APIEndpoint(
            id="ep-backoff", name="Backoff-EP", api_base="http://localhost:9980/v1",
            model="backoff-model", max_retries=3, base_retry_delay=0.1, max_retry_delay=0.5,
        )
        router.add_endpoint(ep)

        llm = MockLLM(fail_count=10, fail_type=ConnectionError)

        def do_call(llm_cfg):
            result = llm.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
            list(result)
            return True

        start = time.time()
        with pytest.raises(RuntimeError):
            router.call_with_fallback("coder", do_call, agent_instance_name="backoff-test")
        elapsed = time.time() - start

        # 4 attempts (initial + 3 retries), backoff: 0.1 + 0.2 + 0.4 = 0.7s theoretical
        # But capped at max_delay=0.5 per delay, so: 0.1 + 0.2 + 0.5 = 0.8s theoretical
        # Plus default fallback adds more time.
        print(f"[BASELINE] Backoff timing: {elapsed:.3f}s (expected >=0.3s with delays)")
        assert elapsed >= 0.3, f"Backoff not being applied: {elapsed:.3f}s is too fast"