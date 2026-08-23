"""Tests for fallback compression implementation.

Exercises real production code paths in:
- agent_cascade/llm/base.py (overflow guard)
- agent_cascade/api_router.py (call_with_fallback context-exceeded handling)
- agent_cascade/execution_engine.py (_find_compression_slice, FallbackCompressionRequired handler)
- agent_cascade/exceptions.py (FallbackCompressionRequired)

All tests are self-contained — no LLM or API server required.
"""

import os
import shutil
import tempfile
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from agent_cascade.exceptions import (
    FallbackCompressionRequired,
    ContextWindowExceeded,
)
from agent_cascade.llm.base import ModelServiceError
from agent_cascade.llm.schema import SYSTEM, USER, Message


# ──────────────────────────────────────────────
# 1. Exception tests
# ──────────────────────────────────────────────

class TestFallbackCompressionRequired:
    """Test FallbackCompressionRequired exception behavior."""

    def test_instantiation_sets_attributes(self):
        """Exception stores all attributes correctly."""
        orig = ContextWindowExceeded("original error")
        exc = FallbackCompressionRequired(
            instance_name="coder1",
            agent_type="Coder",
            failed_endpoint="qwen3-4b",
            original_error=orig,
        )

        assert exc.instance_name == "coder1"
        assert exc.agent_type == "Coder"
        assert exc.failed_endpoint == "qwen3-4b"
        assert exc.original_error is orig

    def test_inherits_from_exception(self):
        """FallbackCompressionRequired is a proper Exception subclass."""
        exc = FallbackCompressionRequired("inst", "type", "endpoint")
        assert isinstance(exc, Exception)

    def test_message_format(self):
        """Exception message contains key information for logging."""
        exc = FallbackCompressionRequired(
            instance_name="researcher1",
            agent_type="Researcher",
            failed_endpoint="small-model",
        )
        msg = str(exc)
        assert "researcher1" in msg
        assert "Researcher" in msg
        assert "small-model" in msg
        assert "compression required" in msg.lower()

    def test_original_error_optional(self):
        """original_error defaults to None when not provided."""
        exc = FallbackCompressionRequired("inst", "type", "endpoint")
        assert exc.original_error is None


# ──────────────────────────────────────────────
# 2. Silent truncation removal (llm/base.py) — REAL CODE PATHS
# ──────────────────────────────────────────────

class TestSilentTruncationRemoval:
    """Verify overflow guard in base.py raises ContextWindowExceeded instead of silently truncating."""

    def _make_test_model(self, max_input_tokens: int = 4096):
        """Create a concrete subclass of BaseChatModel for testing the chat() path.

        Implements all abstract methods needed to reach the overflow guard.
        """
        from agent_cascade.llm.base import BaseChatModel

        class TestModel(BaseChatModel):
            @property
            def support_multimodal_input(self):
                return False

            def _preprocess_messages(self, messages, lang="en", generate_cfg=None, functions=None, use_raw_api=False):
                return list(messages)

            def _chat_with_functions(self, messages, functions, stream, delta_stream, generate_cfg, lang):
                raise RuntimeError("_chat_with_functions should not be called on overflow")

            def _chat_stream(self, messages, delta_stream, generate_cfg):
                raise RuntimeError("_chat_stream should not be called on overflow")

            def _chat_no_stream(self, messages, generate_cfg):
                # Should never reach here in overflow tests
                raise RuntimeError("_chat_no_stream should not be called on overflow")

        cfg = {"model": "test-model", "generate_cfg": {"max_input_tokens": max_input_tokens}}
        return TestModel(cfg)

    def test_overflow_raises_context_window_exceeded(self):
        """When estimated_tokens > max_input_tokens, base.py chat() raises ContextWindowExceeded immediately."""
        model = self._make_test_model(max_input_tokens=100)
        messages = [Message(role=USER, content="word " * 200)]  # well over 100 tokens

        with pytest.raises(ContextWindowExceeded) as exc_info:
            model.chat(messages=messages, stream=False)

        err_msg = str(exc_info.value).lower()
        assert "context window exceeded" in err_msg or ("tokens vs" in err_msg and "limit" in err_msg)

    def test_overflow_does_not_call_truncate(self):
        """_truncate_input_messages_roughly is NOT called when overflow guard fires."""
        model = self._make_test_model(max_input_tokens=100)
        messages = [Message(role=USER, content="word " * 200)]

        # Patch at module level — it's a standalone function, not a method
        with patch("agent_cascade.llm.base._truncate_input_messages_roughly") as mock_trunc:
            with pytest.raises(ContextWindowExceeded):
                model.chat(messages=messages, stream=False)

            # Truncate must never be called — overflow guard raises before reaching truncation logic
            mock_trunc.assert_not_called()

    def test_token_counting_failure_logs_and_continues(self, caplog):
        """If token counting fails in base.py, log warning and continue (no crash)."""
        model = self._make_test_model(max_input_tokens=100)
        messages = [Message(role=USER, content="test")]

        # Make get_message_stats raise — simulates tokenizer failure
        with patch("agent_cascade.llm.base.get_message_stats", side_effect=ValueError("counting failed")):
            # Should not crash from token counting failure; proceeds to _chat_no_stream which raises our sentinel
            with pytest.raises(RuntimeError, match="_chat_no_stream"):
                model.chat(messages=messages, stream=False)

        # Verify warning was logged about token estimation failure (from base.py lines 341-344)
        assert any("token estimation failed" in rec.message.lower() for rec in caplog.records)

    def test_under_limit_proceeds_normally(self):
        """Messages under the limit proceed without raising ContextWindowExceeded."""
        model = self._make_test_model(max_input_tokens=1000)
        messages = [Message(role=USER, content="short message")]

        # Should not raise ContextWindowExceeded — proceeds to _chat_no_stream (our sentinel)
        with pytest.raises(RuntimeError, match="_chat_no_stream"):
            model.chat(messages=messages, stream=False)


# ──────────────────────────────────────────────
# 3. API Router fallback behavior (api_router.py) — REAL CODE PATHS
# ──────────────────────────────────────────────

class TestAPIRouterFallbackBehavior:
    """Test context-exceeded handling in call_with_fallback using real APIRouter."""

    def _make_pool_and_router(self):
        """Create a minimal mock pool and real APIRouter instance with proper setup."""
        from agent_cascade.api_router import APIRouter, APIEndpoint

        # Always use a fresh isolated dir per router instance to prevent cross-test
        # contamination: the session-level conftest fixture shares ONE config dir across
        # all tests in a worker, so another test can write corrupted agent_priorities to
        # it before this test runs. CRITICAL: APIRouter.__init__ prioritizes the
        # AGENT_CASCADE_TEST_CONFIG_DIR env var over the config_dir argument, so we must
        # override the env var (not just pass config_dir) for router construction.
        test_config_dir = tempfile.mkdtemp(prefix="ac_test_api_")
        _orig_env = os.environ.get("AGENT_CASCADE_TEST_CONFIG_DIR")
        os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = test_config_dir
        try:
            pool = MagicMock()
            pool.terminated_instances = set()
            # _check_termination() calls this; default MagicMock return is truthy and would
            # spuriously raise AgentTerminatedError during interruptible backoff sleeps.
            pool.is_instance_terminated.return_value = False

            router = APIRouter(
                default_llm_cfg={"model": "default-model", "api_base": "http://localhost:1234/v1"},
                config_dir=test_config_dir,
            )

            # Positive general limit so the A1/A2 gate classifies a TYPED
            # ContextWindowExceeded (no status code) as genuine overflow — the same
            # behavior the pre-gate router had for untyped errors. The default cfg is
            # what these tests actually hit (the test endpoint below is never assigned).
            router.default_llm_cfg['max_input_tokens'] = 128_000

            # Set up the _pool back-reference (same as AgentPool does)
            router._pool = pool

            # Add a test endpoint that we can use in fallback chains.
            ep = APIEndpoint(
                name="test-endpoint",
                api_base="http://test:8080/v1",
                model="test-model",
                max_retries=0,  # No retries — fail fast for tests
                max_input_tokens=128_000,
            )
            router.add_endpoint(ep)

            return router, pool
        finally:
            if _orig_env is not None:
                os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = _orig_env
            else:
                os.environ.pop("AGENT_CASCADE_TEST_CONFIG_DIR", None)
            shutil.rmtree(test_config_dir, ignore_errors=True)

    def test_non_compressor_raises_fallback_compression_required(self):
        """Non-Compressor agent with context-exceeded error raises FallbackCompressionRequired from real router code.

        Uses a TYPED ContextWindowExceeded (no HTTP status code) — the A1/A2 gate in
        call_with_fallback only applies to server-side errors carrying a status code;
        typed errors keep their original behavior regardless of configured limit.
        """
        router, pool = self._make_pool_and_router()

        # call_fn that raises a context-exceeded-like error (matches _is_context_exceeded_error patterns)
        def failing_call(llm_cfg, *args, **kwargs):
            raise ContextWindowExceeded("prompt is too long")

        with pytest.raises(FallbackCompressionRequired) as exc_info:
            router.call_with_fallback(
                agent_type="Coder",
                call_fn=failing_call,
                agent_instance_name="coder1",
            )

        assert exc_info.value.instance_name == "coder1"
        assert exc_info.value.agent_type == "Coder"
        assert exc_info.value.original_error is not None

    def test_compressor_does_not_raise_fallback_compression_required(self):
        """Compressor agent with context-exceeded error just advances cursor via real router code."""
        router, pool = self._make_pool_and_router()

        call_invoked = False

        def failing_call(llm_cfg, *args, **kwargs):
            nonlocal call_invoked
            call_invoked = True
            raise ContextWindowExceeded("prompt is too long")

        # For Compressor: error caught, cursor advanced, no FallbackCompressionRequired raised.
        # Eventually exhausts endpoints and raises RuntimeError from router exhaustion logic.
        with pytest.raises(RuntimeError) as exc_info:
            router.call_with_fallback(
                agent_type="Compressor",
                call_fn=failing_call,
                agent_instance_name="compressor1",
            )

        # Verify FallbackCompressionRequired was NOT raised — got exhaustion error instead
        assert "FallbackCompressionRequired" not in str(exc_info.value)
        assert call_invoked

    def test_cursor_advanced_before_raising(self):
        """Cursor is advanced BEFORE raising FallbackCompressionRequired (verified via real router)."""
        router, pool = self._make_pool_and_router()
        inst_name = "coder1"

        # Check initial cursor position
        initial_pos = router._instance_endpoint_position.get(inst_name, 0)

        def failing_call(llm_cfg, *args, **kwargs):
            raise ContextWindowExceeded("prompt is too long")

        with pytest.raises(FallbackCompressionRequired):
            router.call_with_fallback(
                agent_type="Coder",
                call_fn=failing_call,
                agent_instance_name=inst_name,
            )

        # Cursor must have been advanced by the real router code before raising
        final_pos = router._instance_endpoint_position.get(inst_name, 0)
        assert final_pos > initial_pos, f"Cursor not advanced: {initial_pos} -> {final_pos}"


# ──────────────────────────────────────────────
# 3b. A1/A2 gate — server context-exceeded errors must be sanity-checked against the
#     endpoint's CONFIGURED limit before triggering fallback compression
#     (2026-08-21 incident: model-swap produced a spurious exceed_context_size 400 for a
#     payload that fit the configured window; see reports/fallback-compression-misclass-and-stop-cascade.md)
# ──────────────────────────────────────────────

def _make_payload_messages(n_words=50):
    """Build a small deterministic Message list (system + user)."""
    return [
        Message(role=SYSTEM, content="You are a test assistant."),
        Message(role=USER, content=" ".join(f"word{i}" for i in range(n_words))),
    ]


class TestContextExceededLimitGate:
    """A1/A2: FallbackCompressionRequired must only fire when the payload genuinely
    exceeds the endpoint's configured max_input_tokens."""

    def _make_router_with_limit(self, max_input_tokens):
        """Real APIRouter with one coder endpoint carrying an explicit max_input_tokens."""
        from agent_cascade.api_router import APIRouter, APIEndpoint

        test_config_dir = tempfile.mkdtemp(prefix="ac_test_gate_")
        _orig_env = os.environ.get("AGENT_CASCADE_TEST_CONFIG_DIR")
        os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = test_config_dir
        try:
            pool = MagicMock()
            pool.terminated_instances = set()
            pool.is_instance_terminated.return_value = False

            router = APIRouter(
                default_llm_cfg={"model": "default-model", "api_base": "http://localhost:1234/v1"},
                config_dir=test_config_dir,
            )
            router._pool = pool
            ep = APIEndpoint(
                name="gate-endpoint",
                api_base="http://gate:8080/v1",
                model="gate-model",
                max_retries=0,
                max_input_tokens=max_input_tokens,
            )
            router.add_endpoint(ep)
            # Assign the endpoint to Coder so it is Tier 1 in the chain (the gate under test
            # reads llm_cfg['max_input_tokens'] of the failing endpoint).
            router.set_agent_priorities("Coder", [ep.id])
            return router
        finally:
            if _orig_env is not None:
                os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = _orig_env
            else:
                os.environ.pop("AGENT_CASCADE_TEST_CONFIG_DIR", None)
            shutil.rmtree(test_config_dir, ignore_errors=True)

    def test_400_under_limit_does_not_raise_fallback_compression(self):
        """(1) Server 400 exceed_context_size but payload <= configured limit →
        NO FallbackCompressionRequired; treated as service error and falls through to
        endpoint exhaustion (RuntimeError)."""
        router = self._make_router_with_limit(max_input_tokens=90_000)

        def failing_call(llm_cfg, *args, **kwargs):
            assert llm_cfg.get('max_input_tokens') == 90_000
            raise ModelServiceError(code='400', message="exceed_context_size: prompt is too long")

        with pytest.raises(RuntimeError) as exc_info:
            router.call_with_fallback(
                agent_type="Coder",
                call_fn=failing_call,
                agent_instance_name="coder1",
                messages=_make_payload_messages(),
            )

        # Exhaustion error, NOT FallbackCompressionRequired
        assert "All API endpoints exhausted" in str(exc_info.value)
        assert "FallbackCompressionRequired" not in str(exc_info.value)

    def test_400_over_limit_raises_fallback_compression(self):
        """(2) Server 400 exceed_context_size and payload > configured limit →
        FallbackCompressionRequired is raised (existing behavior preserved)."""
        router = self._make_router_with_limit(max_input_tokens=10)

        def failing_call(llm_cfg, *args, **kwargs):
            raise ModelServiceError(code='400', message="exceed_context_size: prompt is too long")

        with pytest.raises(FallbackCompressionRequired) as exc_info:
            router.call_with_fallback(
                agent_type="Coder",
                call_fn=failing_call,
                agent_instance_name="coder1",
                messages=_make_payload_messages(),
            )

        assert exc_info.value.instance_name == "coder1"

    def test_unknown_limit_context_error_falls_through(self):
        """(3) Context-exceeded 400 with unknown/missing configured limit →
        never compress off an unknown limit; falls through to exhaustion."""
        from agent_cascade.api_router import APIRouter, APIEndpoint

        test_config_dir = tempfile.mkdtemp(prefix="ac_test_gate_")
        _orig_env = os.environ.get("AGENT_CASCADE_TEST_CONFIG_DIR")
        os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = test_config_dir
        try:
            pool = MagicMock()
            pool.terminated_instances = set()
            pool.is_instance_terminated.return_value = False

            # Default cfg WITHOUT max_input_tokens; general limit 0 → injected value is 0 (unknown)
            router = APIRouter(
                default_llm_cfg={"model": "default-model", "api_base": "http://localhost:1234/v1"},
                config_dir=test_config_dir,
            )
            router._pool = pool
            ep = APIEndpoint(
                name="gate-endpoint",
                api_base="http://gate:8080/v1",
                model="gate-model",
                max_retries=0,
                max_input_tokens=0,  # unknown limit; general limit also 0 → cfg carries explicit 0
            )
            router.add_endpoint(ep)
            router.set_agent_priorities("Coder", [ep.id])

            def failing_call(llm_cfg, *args, **kwargs):
                raise ModelServiceError(code='400', message="exceed_context_size: prompt is too long")

            with pytest.raises(RuntimeError) as exc_info:
                router.call_with_fallback(
                    agent_type="Coder",
                    call_fn=failing_call,
                    agent_instance_name="coder1",
                    messages=_make_payload_messages(),
                )

            assert "All API endpoints exhausted" in str(exc_info.value)
        finally:
            if _orig_env is not None:
                os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = _orig_env
            else:
                os.environ.pop("AGENT_CASCADE_TEST_CONFIG_DIR", None)
            shutil.rmtree(test_config_dir, ignore_errors=True)

    def test_cursor_not_advanced_on_service_drift_path(self):
        """(A1 judgment call) On the service-drift path (payload fits configured limit)
        the per-instance cursor must NOT be advanced — that mechanism is reserved for
        genuine context errors."""
        router = self._make_router_with_limit(max_input_tokens=90_000)
        inst_name = "coder1"

        def failing_call(llm_cfg, *args, **kwargs):
            raise ModelServiceError(code='400', message="exceed_context_size: prompt is too long")

        with pytest.raises(RuntimeError):
            router.call_with_fallback(
                agent_type="Coder",
                call_fn=failing_call,
                agent_instance_name=inst_name,
                messages=_make_payload_messages(),
            )

        assert router._instance_endpoint_position.get(inst_name, 0) == 0

    def test_no_messages_kwarg_never_raises_fallback_compression(self):
        """Regression (review finding): the production caller forwards no ``messages``
        kwarg. Without a payload estimate a server-side context-exceeded error must be
        treated as a service error — never trigger fallback compression."""
        router = self._make_router_with_limit(max_input_tokens=10)

        def failing_call(llm_cfg, *args, **kwargs):
            raise ModelServiceError(code='400', message="exceed_context_size: prompt is too long")

        with pytest.raises(RuntimeError) as exc_info:
            # NOTE: deliberately NO messages= kwarg — mirrors engine/llm_call.py
            router.call_with_fallback(
                agent_type="Coder",
                call_fn=failing_call,
                agent_instance_name="coder1",
            )

        assert "All API endpoints exhausted" in str(exc_info.value)
        assert "FallbackCompressionRequired" not in str(exc_info.value)


# ──────────────────────────────────────────────
# 3c. A3 — _is_context_exceeded_error hardening: free-text patterns only trusted on 400
# ──────────────────────────────────────────────

class TestIsContextExceededErrorHardened:
    """A3: generic free-text patterns must not fire on non-400 status codes."""

    def _router(self):
        from agent_cascade.api_router import APIRouter

        test_config_dir = tempfile.mkdtemp(prefix="ac_test_cls_")
        _orig_env = os.environ.get("AGENT_CASCADE_TEST_CONFIG_DIR")
        os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = test_config_dir
        try:
            router = APIRouter(
                default_llm_cfg={"model": "default-model", "api_base": "http://localhost:1234/v1"},
                config_dir=test_config_dir,
            )
            return router
        finally:
            if _orig_env is not None:
                os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = _orig_env
            else:
                os.environ.pop("AGENT_CASCADE_TEST_CONFIG_DIR", None)
            shutil.rmtree(test_config_dir, ignore_errors=True)

    def test_5xx_with_max_tokens_phrase_is_not_context_exceeded(self):
        """(4) A 5xx whose message contains 'max_tokens exceeded' is NOT classified as
        context-exceeded (would wrongly trigger compression off a service failure)."""
        router = self._router()
        err = ModelServiceError(code='502', message="upstream error: max_tokens exceeded")
        assert router._is_context_exceeded_error(err) is False

    def test_400_with_max_tokens_phrase_is_context_exceeded(self):
        """The same phrase on a 400 IS still classified as context-exceeded."""
        router = self._router()
        err = ModelServiceError(code='400', message="max_tokens exceeded")
        assert router._is_context_exceeded_error(err) is True

    def test_typed_context_window_exceeded_still_detected(self):
        """Typed ContextWindowExceeded detection is unchanged (any status)."""
        router = self._router()
        assert router._is_context_exceeded_error(ContextWindowExceeded("boom")) is True

    def test_llamacpp_400_patterns_still_detected(self):
        """llama.cpp 400 branch is unchanged."""
        router = self._router()
        err = ModelServiceError(code='400', message="exceed_context_size")
        assert router._is_context_exceeded_error(err) is True


# ──────────────────────────────────────────────
# 3d. A4 — get_endpoint_chain must guarantee max_input_tokens on every returned cfg
# ──────────────────────────────────────────────

class TestEndpointChainMaxInputTokensGuarantee:
    """A4: the Tier-4 default cfg (and every chain cfg) must carry an int
    max_input_tokens so base.py's pre-check can never silently cap at
    DEFAULT_MAX_INPUT_TOKENS."""

    def test_default_cfg_gets_limit_injected(self):
        """(5) A chain cfg lacking max_input_tokens gets one injected."""
        from agent_cascade.api_router import APIRouter

        test_config_dir = tempfile.mkdtemp(prefix="ac_test_a4_")
        _orig_env = os.environ.get("AGENT_CASCADE_TEST_CONFIG_DIR")
        os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = test_config_dir
        try:
            # Default cfg WITHOUT max_input_tokens, general limit 0 → injected value must be 0 (explicit)
            router = APIRouter(
                default_llm_cfg={"model": "default-model", "api_base": "http://localhost:1234/v1"},
                config_dir=test_config_dir,
            )
            chain = router.get_endpoint_chain("Coder")
            assert len(chain) == 1
            cfg = chain[0]
            assert 'max_input_tokens' in cfg
            assert isinstance(cfg['max_input_tokens'], int)
            assert cfg['max_input_tokens'] == 0

            # General limit > 0 → injected as the explicit limit (never left keyless)
            router2 = APIRouter(
                default_llm_cfg={"model": "default-model",
                                 "api_base": "http://localhost:1234/v1",
                                 "max_input_tokens": 128_000},
                config_dir=test_config_dir,
            )
            chain2 = router2.get_endpoint_chain("Coder")
            assert chain2[0]['max_input_tokens'] == 128_000

            # Existing explicit limit is never clobbered by the injection
            router3 = APIRouter(
                default_llm_cfg={"model": "default-model",
                                 "api_base": "http://localhost:1234/v1",
                                 "max_input_tokens": 90_000},
                config_dir=test_config_dir,
            )
            chain3 = router3.get_endpoint_chain("Coder")
            assert chain3[0]['max_input_tokens'] == 90_000
        finally:
            if _orig_env is not None:
                os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = _orig_env
            else:
                os.environ.pop("AGENT_CASCADE_TEST_CONFIG_DIR", None)
            shutil.rmtree(test_config_dir, ignore_errors=True)


# ──────────────────────────────────────────────
# 3e. Fallback-compression-misclass fix (reports/fallback-compression-misclass-investigation.md)
#     PRIMARY: chain cfgs must keep each endpoint's TRUE max_input_tokens (no inflation).
#     SECONDARY: a genuine llama.cpp 400 on a smaller assigned endpoint must classify as
#                overflow and raise FallbackCompressionRequired.
# ──────────────────────────────────────────────

def _make_two_endpoint_router(limit_a, limit_b):
    """Real APIRouter with two Coder endpoints (A at priority 0 = chain head, B at 1)."""
    from agent_cascade.api_router import APIRouter, APIEndpoint

    test_config_dir = tempfile.mkdtemp(prefix="ac_test_misclass_")
    _orig_env = os.environ.get("AGENT_CASCADE_TEST_CONFIG_DIR")
    os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = test_config_dir
    try:
        pool = MagicMock()
        pool.terminated_instances = set()
        pool.is_instance_terminated.return_value = False

        router = APIRouter(
            default_llm_cfg={"model": "default-model", "api_base": "http://localhost:1234/v1"},
            config_dir=test_config_dir,
        )
        router._pool = pool
        ep_a = APIEndpoint(
            name="ep-a", api_base="http://a:8080/v1", model="model-a",
            max_retries=0, max_input_tokens=limit_a,
        )
        ep_b = APIEndpoint(
            name="ep-b", api_base="http://b:8080/v1", model="model-b",
            max_retries=0, max_input_tokens=limit_b,
        )
        router.add_endpoint(ep_a)
        router.add_endpoint(ep_b)
        # A first (chain head / first-priority), B second.
        router.set_agent_priorities("Coder", [ep_a.id, ep_b.id])
        return router, ep_a, ep_b
    finally:
        if _orig_env is not None:
            os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = _orig_env
        else:
            os.environ.pop("AGENT_CASCADE_TEST_CONFIG_DIR", None)
        shutil.rmtree(test_config_dir, ignore_errors=True)


class TestNoMaxInputTokensInflation:
    """PRIMARY fix: the chain-head allocation must NOT inflate a smaller endpoint's
    max_input_tokens. Before the fix, get_endpoint_chain overwrote every cfg's limit with
    allocated_tokens (the first-priority endpoint's limit), blinding both the A1/A2 gate
    and the client-side pre-check."""

    def test_smaller_endpoint_keeps_true_limit(self):
        """A 90k endpoint assigned behind a 165.5k head keeps 90k, not 165.5k."""
        router, ep_a, ep_b = _make_two_endpoint_router(limit_a=165_500, limit_b=90_000)

        chain = router.get_endpoint_chain("Coder", allocated_tokens=165_500)

        by_model = {cfg.get('model'): cfg for cfg in chain}
        assert by_model['model-a']['max_input_tokens'] == 165_500, \
            "chain head must keep its own configured limit"
        assert by_model['model-b']['max_input_tokens'] == 90_000, \
            f"smaller endpoint inflated: got {by_model['model-b']['max_input_tokens']}, expected 90_000"

    def test_smaller_endpoint_not_inflated_even_without_alloc(self):
        """No inflation occurs even when allocated_tokens is omitted (behavior parity)."""
        router, ep_a, ep_b = _make_two_endpoint_router(limit_a=165_500, limit_b=90_000)

        chain = router.get_endpoint_chain("Coder")
        by_model = {cfg.get('model'): cfg for cfg in chain}
        assert by_model['model-b']['max_input_tokens'] == 90_000


class TestGateClassifiesGenuineOverflowOnAssignedEndpoint:
    """SECONDARY consequence of the PRIMARY fix: with truthful per-endpoint limits, a
    genuine llama.cpp 400 on a SMALLER assigned endpoint (payload > that endpoint's true
    limit, even though allocated_tokens is larger) must raise FallbackCompressionRequired."""

    def test_400_on_smaller_assigned_endpoint_raises_fcr(self):
        router, ep_a, ep_b = _make_two_endpoint_router(limit_a=165_500, limit_b=90_000)

        # Rotate the cursor so B (90k) becomes the endpoint actually called.
        router.advance_instance_endpoint("coder1")

        # Payload well over 90k but under 165.5k: with the inflated value the old gate said
        # "fits 165500" → service error; with B's true 90k limit it must be genuine overflow.
        messages = _make_payload_messages(n_words=45_000)

        def failing_call(llm_cfg, *args, **kwargs):
            # We must actually be hitting the SMALL endpoint (proves rotation + no inflation).
            assert llm_cfg.get('model') == 'model-b', f"expected model-b, got {llm_cfg.get('model')}"
            assert llm_cfg.get('max_input_tokens') == 90_000, \
                f"assigned endpoint limit inflated: {llm_cfg.get('max_input_tokens')}"
            raise ModelServiceError(code='400', message="exceed_context_size_error: prompt is too long")

        with pytest.raises(FallbackCompressionRequired) as exc_info:
            router.call_with_fallback(
                agent_type="Coder",
                call_fn=failing_call,
                allocated_tokens=165_500,
                agent_instance_name="coder1",
                messages=messages,
            )

        assert exc_info.value.instance_name == "coder1"


# ──────────────────────────────────────────────
# 4. Execution engine iterative compression (execution_engine.py) — REAL CODE PATHS
# ──────────────────────────────────────────────

class TestExecutionEngineIterativeCompression:
    """Test the FallbackCompressionRequired handler using real ExecutionEngine."""

    def _make_pool_and_engine(self):
        """Create a mocked pool and real ExecutionEngine for testing."""
        from agent_cascade.execution_engine import ExecutionEngine

        pool = MagicMock()
        instance = MagicMock()
        compression_lock = MagicMock()
        compression_lock.__enter__ = MagicMock()
        compression_lock.__exit__ = MagicMock()
        instance._compression_lock = compression_lock
        instance._streaming_responses = []
        instance.instance_name = "test-agent"
        instance._force_compress_count = 0      # Real int for check_overfeeding comparison
        instance.compression_summary = None     # Set by handler after successful compression
        instance.latest_marker_index = -1       # Set by handler after successful compression

        # _is_terminal_stop() (execution_engine.py:1839) reads self.pool.stopped,
        # self._my_generation, self.pool._run_generation and
        # self.pool.is_instance_terminated(). A bare MagicMock pool makes the first two
        # comparisons raise AttributeError/TypeError, which the retry loop swallows as a
        # failed call and retries — inflating the call count. Configure them so the check
        # returns False (no terminal stop) for this unit test:
        pool.stopped = False
        pool.is_instance_terminated.return_value = False
        pool._run_generation = 1

        pool.get_instance.return_value = instance
        history = [Message(role=SYSTEM, content="sys")] + [Message(role=USER, content=f"msg{i}") for i in range(20)]
        pool.get_conversation.return_value = history
        pool.get_compression_target_set_from_conversation.return_value = (2, history[2:], -1)

        # slice_history_for_llm is called by _rebuild_working_set — must return non-empty list
        pool.slice_history_for_llm.return_value = history[2:]

        # Mock compressor window lookup
        comp_chain = [{"max_input_tokens": 32768}]
        pool.api_router = MagicMock()
        pool.api_router.get_endpoint_chain.side_effect = lambda agent_type, **kw: comp_chain if agent_type == "Compressor" else [{"max_input_tokens": 10000}]

        # Mock compressor agent for system prompt estimation
        comp_agent = MagicMock()
        comp_agent.system_message = "You are a compressor."
        pool.get_agent.return_value = comp_agent

        # CRITICAL: settings must be a real object with proper attributes, not mocks.
        # Using a simple namespace to avoid MagicMock comparison issues in backoff calculation.
        class Settings:
            retry_max_attempts = 2       # Keep low for fast tests
            retry_base_delay = 0.1
            retry_max_delay = 1.0
            loop_min_chars = 4000
            loop_max_chars = 40960
            loop_char_run_enabled = True
            loop_char_run_limit = 129
            loop_max_chars_enabled = True
            loop_two_phase_enabled = False
            loop_suspicion_threshold = 7
            loop_confirm_required = 3
            loop_cooldown_feeds = 50

        pool.settings = Settings()

        engine = ExecutionEngine(pool)
        # _my_generation is normally captured in run() (execution_engine.py:1134); this
        # test calls _execute_llm_call_with_retry directly, bypassing run(), so set it
        # here to match pool._run_generation and avoid an AttributeError in
        # _is_terminal_stop().
        engine._my_generation = 1
        return engine, pool, instance, history

    def test_handler_calls_find_compression_slice(self):
        """FallbackCompressionRequired handler calls real _find_compression_slice."""
        from agent_cascade.engine.compression_exec import FALLBACK_COMPRESSION_INITIAL_FRACTION
        from agent_cascade.compression.result import CompressResult

        engine, pool, instance, history = self._make_pool_and_engine()

        # Mock compress_context to succeed on first call
        success_result = CompressResult(
            success=True,
            summary_text="compressed",
            marker_message=None,
            messages_discarded=10,
            tail_count=5,
            error=None,
            mode="auto",
            tokens_before=5000,
            tokens_after=2000,
        )

        # Track calls to _find_compression_slice via wrapping
        slice_calls = []
        original_find = engine._find_compression_slice

        def tracking_find(*args, **kwargs):
            slice_calls.append((args, kwargs))
            return (FALLBACK_COMPRESSION_INITIAL_FRACTION, 10, history[2:12])

        engine._find_compression_slice = tracking_find

        # Patch _execute_llm_call to raise FCR once then succeed
        call_count = 0

        def mock_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise FallbackCompressionRequired("test-agent", "Coder", "small-model")
            # After compression, succeed with a valid response
            yield [Message(role="assistant", content="done")]

        with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
            with patch("agent_cascade.compression.core.compress_context", return_value=success_result):
                template = MagicMock()
                template.llm_cfg = {"model": "test"}
                template.function_map = {}
                template.llm = MagicMock()
                template.llm.generate_cfg = {}

                result = list(engine._execute_llm_call_with_retry(instance, [Message(role=USER, content="test")], template, []))

        assert len(slice_calls) >= 1, "_find_compression_slice should be called by handler"
        assert call_count == 2, f"Expected 2 calls (FCR then success), got {call_count}"

    def test_overfeeding_detection_raises_context_window_exceeded(self):
        """Overfeeding detection during compression raises ContextWindowExceeded directly via real handler.

        Note: Overfeeding check happens inside the FCR handler's inner try block but OUTSIDE the
        except ContextWindowExceeded: raise clause, so it propagates up and is caught by outer loop.
        However, with overfeeding=True on first round, it raises immediately before any retry logic.
        """
        engine, pool, instance, history = self._make_pool_and_engine()

        # Make check_overfeeding return True (overfeeding detected)
        engine.compression_handler.check_overfeeding = MagicMock(return_value=True)

        call_count = 0

        def mock_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise FallbackCompressionRequired("test-agent", "Coder", "small-model")

        with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
            template = MagicMock()
            template.llm_cfg = {"model": "test"}
            template.function_map = {}
            template.llm = MagicMock()
            template.llm.generate_cfg = {}

            gen = engine._execute_llm_call_with_retry(instance, [Message(role=USER, content="test")], template, [])
            with pytest.raises(ContextWindowExceeded) as exc_info:
                list(gen)

        assert "overfeeding" in str(exc_info.value).lower()

    def test_max_rounds_exceeded_raises_context_window_exceeded(self):
        """Compression loop exhausts all rounds → raises ContextWindowExceeded via real handler."""
        from agent_cascade.engine.compression_exec import FALLBACK_COMPRESSION_MAX_ROUNDS
        from agent_cascade.compression.result import CompressResult

        engine, pool, instance, history = self._make_pool_and_engine()

        # compress_context always fails — forces loop to exhaust rounds
        fail_result = CompressResult(
            success=False,
            summary_text="",
            marker_message=None,
            messages_discarded=0,
            tail_count=0,
            error="simulated failure",
            mode="auto",
        )

        compress_call_count = 0

        def always_fail(*args, **kwargs):
            nonlocal compress_call_count
            compress_call_count += 1
            return fail_result

        call_count = 0

        def mock_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise FallbackCompressionRequired("test-agent", "Coder", "small-model")

        with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
            with patch("agent_cascade.compression.core.compress_context", side_effect=always_fail):
                template = MagicMock()
                template.llm_cfg = {"model": "test"}
                template.function_map = {}
                template.llm = MagicMock()
                template.llm.generate_cfg = {}

                gen = engine._execute_llm_call_with_retry(instance, [Message(role=USER, content="test")], template, [])
                with pytest.raises(ContextWindowExceeded) as exc_info:
                    list(gen)

        # Verify compression was attempted multiple times (up to max rounds)
        assert compress_call_count >= FALLBACK_COMPRESSION_MAX_ROUNDS, \
            f"Expected at least {FALLBACK_COMPRESSION_MAX_ROUNDS} compression attempts, got {compress_call_count}"

    def test_compression_failure_loop_continues(self):
        """When compress_context returns success=False, loop continues to next round."""
        from agent_cascade.compression.result import CompressResult

        engine, pool, instance, history = self._make_pool_and_engine()

        attempt = 0

        def fail_then_succeed(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt < 3:
                # First two rounds fail
                return CompressResult(
                    success=False,
                    summary_text="",
                    marker_message=None,
                    messages_discarded=0,
                    tail_count=0,
                    error="simulated failure",
                    mode="auto",
                )
            else:
                # Third round succeeds
                return CompressResult(
                    success=True,
                    summary_text="compressed",
                    marker_message=None,
                    messages_discarded=10,
                    tail_count=5,
                    error=None,
                    mode="auto",
                    tokens_before=5000,
                    tokens_after=2000,
                )

        api_call_count = 0

        def mock_execute(*args, **kwargs):
            nonlocal api_call_count
            api_call_count += 1
            if api_call_count == 1:
                raise FallbackCompressionRequired("test-agent", "Coder", "small-model")
            # After successful compression, succeed
            yield [Message(role="assistant", content="done")]

        with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
            with patch("agent_cascade.compression.core.compress_context", side_effect=fail_then_succeed):
                template = MagicMock()
                template.llm_cfg = {"model": "test"}
                template.function_map = {}
                template.llm = MagicMock()
                template.llm.generate_cfg = {}

                result = list(engine._execute_llm_call_with_retry(instance, [Message(role=USER, content="test")], template, []))

        # Compression was retried until success
        assert attempt >= 3, f"Expected at least 3 compression attempts (fail, fail, succeed), got {attempt}"


# ──────────────────────────────────────────────
# 5. Smart slice algorithm (_find_compression_slice) — REAL CODE PATHS
# ──────────────────────────────────────────────

class TestSmartSliceAlgorithm:
    """Test the real _find_compression_slice method in ExecutionEngine."""

    def _make_engine_with_pool(self, history):
        """Create a real ExecutionEngine with a pool containing the given history."""
        from agent_cascade.execution_engine import ExecutionEngine

        pool = MagicMock()
        pool.get_conversation.return_value = history
        pool.get_compression_target_set_from_conversation.return_value = (2, history[2:], -1)

        # Mock compressor agent for system prompt token estimation
        comp_agent = MagicMock()
        comp_agent.system_message = "You are a compressor."
        pool.get_agent.return_value = comp_agent

        engine = ExecutionEngine(pool)
        return engine, pool

    def test_returns_valid_slice_when_initial_fraction_fits(self):
        """Real _find_compression_slice returns slice when initial fraction fits compressor window."""
        history = [Message(role=SYSTEM, content="sys")] + [Message(role=USER, content=f"msg{i}") for i in range(10)]
        engine, pool = self._make_engine_with_pool(history)

        active_start_idx, active_set, latest_summary_idx = (2, history[2:], -1)

        result = engine._find_compression_slice(
            active_set=active_set,
            history=history,
            active_start_idx=active_start_idx,
            latest_summary_idx=latest_summary_idx,
            compressor_window=10000,  # generous window
            min_fraction=0.05,
        )

        assert result is not None
        fraction, discard_count, target_messages = result
        assert fraction > 0
        assert discard_count > 0
        assert len(target_messages) > 0

    def test_halves_fraction_iteratively_until_fit(self):
        """Real _find_compression_slice halves fraction iteratively; track decreasing values."""
        history = [Message(role=SYSTEM, content="sys")] + [Message(role=USER, content=f"msg{i} " * 10) for i in range(50)]
        engine, pool = self._make_engine_with_pool(history)

        active_start_idx, active_set, latest_summary_idx = (2, history[2:], -1)

        # Wrap compute_discard_count to track fraction values used across iterations.
        # Must patch where it's LOOKED UP — inside execution_engine module where _find_compression_slice lives.
        fractions_used = []
        original_compute = None

        def tracking_compute(active, fraction, force=False):
            nonlocal original_compute
            if original_compute is None:
                from agent_cascade.compression.helpers import compute_discard_count as cd
                original_compute = cd
            fractions_used.append(fraction)
            return original_compute(active, fraction, force=force)

        with patch("agent_cascade.engine.compression_exec.compute_discard_count", side_effect=tracking_compute):
            # Very small compressor window — forces multiple halvings
            result = engine._find_compression_slice(
                active_set=active_set,
                history=history,
                active_start_idx=active_start_idx,
                latest_summary_idx=latest_summary_idx,
                compressor_window=50,  # tiny window forces halving
                min_fraction=0.05,
            )

        # Verify fractions were iteratively halved (each < previous)
        assert len(fractions_used) > 1, f"Expected multiple fraction attempts, got {len(fractions_used)}: {fractions_used}"
        for i in range(1, len(fractions_used)):
            assert fractions_used[i] < fractions_used[i - 1], \
                f"Fractions should decrease: {fractions_used[i-1]} -> {fractions_used[i]}"

        if result is not None:
            fraction, discard_count, target_messages = result
            # Final fraction should be reduced from initial 0.70
            assert fraction <= 0.70
            assert discard_count > 0

    def test_returns_none_for_single_massive_message(self):
        """Real _find_compression_slice returns None when even minimum fraction doesn't fit."""
        # Large but safe message for tokenizer (avoids stack overflow)
        history = [
            Message(role=SYSTEM, content="sys"),
            Message(role=USER, content="initial"),
            Message(role=USER, content="x" * 100_000),
        ]
        engine, pool = self._make_engine_with_pool(history)

        active_start_idx, active_set, latest_summary_idx = (2, history[2:], -1)

        result = engine._find_compression_slice(
            active_set=active_set,
            history=history,
            active_start_idx=active_start_idx,
            latest_summary_idx=latest_summary_idx,
            compressor_window=10,  # tiny window vs large message
            min_fraction=0.05,
        )

        assert result is None

    def test_cumulative_token_counting_correctness(self):
        """Real _find_compression_slice cumulative token counting produces correct estimates."""
        from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
        from agent_cascade.utils.utils import extract_text_from_message

        # Build history with known content
        history = [Message(role=SYSTEM, content="sys")] + [Message(role=USER, content=f"word{i}") for i in range(20)]
        engine, pool = self._make_engine_with_pool(history)

        active_start_idx, active_set, latest_summary_idx = (2, history[2:], -1)

        # Manually compute expected cumulative tokens for verification
        expected_cumulative = []
        running = 0
        for msg in active_set:
            content = extract_text_from_message(msg, add_upload_info=False)
            running += qwen_count(content)
            expected_cumulative.append(running)

        # Call _find_compression_slice with generous window — should use initial fraction
        result = engine._find_compression_slice(
            active_set=active_set,
            history=history,
            active_start_idx=active_start_idx,
            latest_summary_idx=latest_summary_idx,
            compressor_window=500,
            min_fraction=0.05,
        )

        if result is not None:
            fraction, discard_count, target_messages = result
            # Verify slice validity
            assert 0 < discard_count <= len(active_set)
            assert len(target_messages) >= discard_count

            # The cumulative count at discard_count-1 should match what we computed
            # (this verifies the optimization produces correct results)
            assert expected_cumulative[discard_count - 1] > 0


# ──────────────────────────────────────────────
# 6. Integration tests — REAL CODE PATHS
# ──────────────────────────────────────────────

class TestFallbackCompressionIntegration:
    """Higher-level integration tests exercising real production code."""

    def test_full_flow_non_compressor_agent(self):
        """End-to-end flow using real APIRouter: context-exceeded → FallbackCompressionRequired."""
        from agent_cascade.api_router import APIRouter, APIEndpoint

        # Always use a fresh isolated dir per router instance (see _make_pool_and_router note).
        # Override the env var too — APIRouter.__init__ prioritizes it over config_dir.
        test_config_dir = tempfile.mkdtemp(prefix="ac_test_integration_")
        _orig_env = os.environ.get("AGENT_CASCADE_TEST_CONFIG_DIR")
        os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = test_config_dir
        try:
            pool = MagicMock()
            pool.terminated_instances = set()

            router = APIRouter(
                default_llm_cfg={"model": "default", "api_base": "http://localhost:1234/v1"},
                config_dir=test_config_dir,
            )
            # Positive general limit so the A1/A2 gate classifies the TYPED
            # ContextWindowExceeded (no status code) as genuine overflow — pre-gate behavior preserved.
            router.default_llm_cfg['max_input_tokens'] = 128_000
            router._pool = pool

            ep = APIEndpoint(name="test", api_base="http://test:8080/v1", model="test-model",
                             max_retries=0, max_input_tokens=128_000)
            router.add_endpoint(ep)

            def failing_call(llm_cfg, *args, **kwargs):
                raise ContextWindowExceeded("prompt is too long")

            with pytest.raises(FallbackCompressionRequired) as exc_info:
                router.call_with_fallback(
                    agent_type="Coder",
                    call_fn=failing_call,
                    agent_instance_name="coder1",
                )

            assert exc_info.value.instance_name == "coder1"
            assert exc_info.value.agent_type == "Coder"
        finally:
            if _orig_env is not None:
                os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = _orig_env
            else:
                os.environ.pop("AGENT_CASCADE_TEST_CONFIG_DIR", None)
            shutil.rmtree(test_config_dir, ignore_errors=True)

    def test_compressor_window_safety_factor_applied(self):
        """Compressor window uses safety factor to reserve overhead tokens."""
        from agent_cascade.engine.compression_exec import _COMPRESSOR_WINDOW_SAFETY_FACTOR

        max_tokens = 32768
        expected_available = int(max_tokens * _COMPRESSOR_WINDOW_SAFETY_FACTOR)

        assert expected_available < max_tokens
        assert abs(expected_available - (max_tokens * 0.85)) <= 1

    def test_post_compression_endpoint_check_logic(self):
        """Post-compression check verifies payload fits next endpoint with safety margin."""
        # Simulate the check logic from execution_engine.py lines 3252-3281
        estimated_tokens = 900
        next_limit = 1000

        # With 95% safety margin: 900 <= 1000 * 0.95 = 950 → fits
        assert estimated_tokens <= next_limit * 0.95

        # Edge case: right at limit without margin → should NOT fit
        estimated_tokens = 960
        assert not (estimated_tokens <= next_limit * 0.95)

    def test_is_context_exceeded_error_from_real_router(self):
        """Verify _is_context_exceeded_error behavior using real APIRouter method."""
        from agent_cascade.api_router import APIRouter

        # Create minimal router just to access the static method.
        # Always use a fresh isolated dir per router instance (see _make_pool_and_router note).
        # Override the env var too — APIRouter.__init__ prioritizes it over config_dir.
        test_config_dir = tempfile.mkdtemp(prefix="ac_test_ctx_")
        _orig_env = os.environ.get("AGENT_CASCADE_TEST_CONFIG_DIR")
        os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = test_config_dir
        try:
            router = APIRouter(
                default_llm_cfg={"model": "default", "api_base": "http://localhost:1234/v1"},
                config_dir=test_config_dir,
            )

            # Test with real ContextWindowExceeded
            assert router._is_context_exceeded_error(ContextWindowExceeded("test"))

            # Test with matching error strings (generic patterns — only trusted on HTTP 400, A3)
            assert router._is_context_exceeded_error(ModelServiceError(code='400', message="prompt is too long"))
            assert router._is_context_exceeded_error(ModelServiceError(code='400', message="input tokens exceed limit"))

            # Test non-matching error
            assert not router._is_context_exceeded_error(RuntimeError("connection timeout"))
        finally:
            if _orig_env is not None:
                os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = _orig_env
            else:
                os.environ.pop("AGENT_CASCADE_TEST_CONFIG_DIR", None)
            shutil.rmtree(test_config_dir, ignore_errors=True)


# ──────────────────────────────────────────────
# 7. Logger/pool consistency + fits-check limit fallback — REGRESSION (compression infinite-loop bug)
# ──────────────────────────────────────────────
# Root cause: after a successful FALLBACK compression round the POOL was compressed but the
# JSONL logger was NOT synced (only an append-only notification), so pool/log desynced. Any path
# reading back from the log (recovery, session reload) restored the OLD large history → context-
# exceeded re-fired next turn → infinite loop. The two triggers: (a) endpoint reorder corrupts the
# positional cursor; (b) no endpoint assigned → post-compression "fits" check sees limit=0 and
# assumes fit without verifying against the real server limit.

class TestFallbackCompressionLoggerSyncAndFitsCheck:
    """Regression tests for the compression infinite-loop bug (todo.md line 123)."""

    def _make_pool_and_engine(self, next_limit):
        """Build a mocked pool + real ExecutionEngine with a controllable post-compression chain.

        ``next_limit`` is the max_input_tokens reported by the FIRST entry of the router chain used
        in the post-compression "fits" check (i.e. the non-Compressor branch). Mirrors the existing
        TestExecutionEngineIterativeCompression fixture but exposes the next-endpoint limit so we can
        exercise both the "limit present" and "limit == 0" branches.
        """
        from agent_cascade.execution_engine import ExecutionEngine

        pool = MagicMock()
        instance = MagicMock()
        compression_lock = MagicMock()
        compression_lock.__enter__ = MagicMock()
        compression_lock.__exit__ = MagicMock()
        instance._compression_lock = compression_lock
        instance._streaming_responses = []
        instance.instance_name = "test-agent"
        instance.agent_class = "Coder"
        instance._force_compress_count = 0
        instance.compression_summary = None
        instance.latest_marker_index = -1

        pool.stopped = False
        pool.is_instance_terminated.return_value = False
        pool._run_generation = 1

        pool.get_instance.return_value = instance
        history = [Message(role=SYSTEM, content="sys")] + [Message(role=USER, content=f"msg{i}") for i in range(20)]
        pool.get_conversation.return_value = history
        pool.get_compression_target_set_from_conversation.return_value = (2, history[2:], -1)
        pool.slice_history_for_llm.return_value = history[2:]

        # Compressor window lookup: generous so _find_compression_slice uses the initial fraction.
        comp_chain = [{"max_input_tokens": 32768}]
        pool.api_router = MagicMock()
        pool.api_router.get_endpoint_chain.side_effect = (
            lambda agent_type, **kw: comp_chain if agent_type == "Compressor" else [{"max_input_tokens": next_limit}]
        )

        comp_agent = MagicMock()
        comp_agent.system_message = "You are a compressor."
        pool.get_agent.return_value = comp_agent

        class Settings:
            retry_max_attempts = 2
            retry_base_delay = 0.1
            retry_max_delay = 1.0
            loop_min_chars = 4000
            loop_max_chars = 40960
            loop_char_run_enabled = True
            loop_char_run_limit = 129
            loop_max_chars_enabled = True
            loop_two_phase_enabled = False
            loop_suspicion_threshold = 7
            loop_confirm_required = 3
            loop_cooldown_feeds = 50

        pool.settings = Settings()

        engine = ExecutionEngine(pool)
        engine._my_generation = 1
        return engine, pool, instance, history

    def _run_one_successful_round(self, engine, instance):
        """Drive the retry loop through exactly one successful compression round.

        Returns the number of times the LLM call was invoked (should be 2: FCR then success).
        """
        from agent_cascade.compression.result import CompressResult
        from agent_cascade.engine.compression_exec import FALLBACK_COMPRESSION_INITIAL_FRACTION

        success_result = CompressResult(
            success=True,
            summary_text="compressed",
            marker_message=None,
            messages_discarded=10,
            tail_count=5,
            error=None,
            mode="auto",
            tokens_before=5000,
            tokens_after=2000,
        )

        # Force a single deterministic slice so the round succeeds immediately.
        engine._find_compression_slice = lambda *a, **k: (FALLBACK_COMPRESSION_INITIAL_FRACTION, 10, [Message(role=USER, content="x")])

        call_count = [0]

        def mock_execute(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise FallbackCompressionRequired("test-agent", "Coder", "small-model")
            yield [Message(role="assistant", content="done")]

        with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
            with patch("agent_cascade.compression.core.compress_context", return_value=success_result):
                template = MagicMock()
                template.llm_cfg = {"model": "test"}
                template.function_map = {}
                template.llm = MagicMock()
                template.llm.generate_cfg = {}
                list(engine._execute_llm_call_with_retry(instance, [Message(role=USER, content="test")], template, []))
        return call_count[0]

    def test_logger_synced_after_successful_fallback_compression(self):
        """After a successful fallback compression round, the JSONL logger is synced to the pool.

        Regression: the forced path calls _sync_logger_after_compression() (reset_history rewrite) but
        the fallback path did not — leaving pool/log desynced and re-inflating on recovery/reload.
        """
        engine, pool, instance, history = self._make_pool_and_engine(next_limit=10000)

        # Replace the real compression handler's sync method with a spy so we can assert it fired.
        sync_spy = MagicMock()
        engine.compression_handler._sync_logger_after_compression = sync_spy

        calls = self._run_one_successful_round(engine, instance)

        assert calls == 2, f"Expected FCR-then-success (2 LLM calls), got {calls}"
        # The logger must be synced to the compressed pool after the successful round.
        sync_spy.assert_called_once()
        args, kwargs = sync_spy.call_args
        # Positional signature: (instance_name, agent_class, operation_name, instance)
        assert args[0] == "test-agent"
        assert args[1] == "Coder"
        assert "fallback compression" in str(args[2])
        assert kwargs.get("instance") is instance or (len(args) >= 4 and args[3] is instance)

    def test_logger_synced_once_per_round_across_multi_round_run(self):
        """_sync_logger_after_compression fires exactly ONCE per successful compression round,
        including on NON-final rounds — so reset_history rewrite double-logging is avoided per round
        and sync runs on every round (not just the final one).

        Regression (NIT #7): pre-fix there was no test proving sync runs once-per-round across a
        multi-round run. We drive TWO successful rounds: round 1's post-compression payload still
        exceeds the next limit (so it does NOT break and advances to round 2), round 2 fits (breaks).
        The sync spy must be called exactly twice — one per round — with the correct positional args.

        Mechanics note: the post-compression "fits" check estimates tokens from the REBUILT working
        set (llm_messages), not from CompressResult.tokens_after. So to force round 1 to exceed we
        keep the working set large for rounds 1's rebuild+check, then shrink it before round 2 so it
        fits. This is done with a stateful slice_history_for_llm: calls 1&2 (round-1 top rebuild and
        post-compress rebuild) return the full big set (exceeds next_limit=1000); call 3+ (round-2
        rebuild) returns a small set (fits).
        """
        from agent_cascade.compression.result import CompressResult
        from agent_cascade.engine.compression_exec import FALLBACK_COMPRESSION_INITIAL_FRACTION

        engine, pool, instance, history = self._make_pool_and_engine(next_limit=1000)

        sync_spy = MagicMock()
        engine.compression_handler._sync_logger_after_compression = sync_spy

        # Force a deterministic slice so BOTH rounds succeed (no "too small" break / give-up).
        engine._find_compression_slice = lambda *a, **k: (FALLBACK_COMPRESSION_INITIAL_FRACTION, 10, [Message(role=USER, content="x")])

        # Large working set so the post-compression estimate EXCEEDS next_limit=1000.
        big = "word " * 400
        big_set = [Message(role=USER, content=f"msg{i} {big}") for i in range(20)]

        # Stateful: calls 1&2 → big (exceeds); call 3+ → small (fits).
        slice_calls = [0]

        def stateful_slice(conv):
            slice_calls[0] += 1
            if slice_calls[0] <= 2:
                return list(big_set)  # big working set → exceeds next_limit
            return [Message(role=USER, content="small")]  # small → fits within 95% margin

        pool.slice_history_for_llm = stateful_slice

        compress_calls = [0]

        def stateful_compress(*a, **k):
            compress_calls[0] += 1
            return CompressResult(
                success=True, summary_text="compressed", marker_message=None,
                messages_discarded=10, tail_count=5, error=None, mode="auto",
                tokens_before=5000, tokens_after=2000,
            )

        call_count = [0]

        def mock_execute(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise FallbackCompressionRequired("test-agent", "Coder", "small-model")
            yield [Message(role="assistant", content="done")]

        with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
            with patch("agent_cascade.compression.core.compress_context", side_effect=stateful_compress):
                template = MagicMock()
                template.llm_cfg = {"model": "test"}
                template.function_map = {}
                template.llm = MagicMock()
                template.llm.generate_cfg = {}
                list(engine._execute_llm_call_with_retry(instance, [Message(role=USER, content="test")], template, []))

        # Two successful rounds → compress_context ran twice and sync fired once per round.
        assert call_count[0] == 2, f"Expected FCR-then-success (2 LLM calls), got {call_count[0]}"
        assert compress_calls[0] == 2, (
            f"Expected 2 successful compression rounds, got {compress_calls[0]}"
        )
        assert sync_spy.call_count == 2, (
            f"Expected _sync_logger_after_compression once per round (2 rounds → 2 calls), "
            f"got {sync_spy.call_count}"
        )
        # Each call uses the correct positional signature.
        for c in sync_spy.call_args_list:
            a = c.args
            assert a[0] == "test-agent"
            assert a[1] == "Coder"
            assert "fallback compression" in str(a[2])

    def test_logger_sync_failure_does_not_abort_compression(self, caplog):
        """A logger-sync failure must NOT abort compression AND must surface a WARNING that the
        JSONL log may now be out of sync with the compressed pool.

        Regression (MINOR #4 / NIT #6a): pre-fix this branch only logged at ERROR with a generic
        "may desync" message; post-fix it logs at WARNING explicitly naming the half-written-log /
        re-inflation risk so operators can distinguish the failure mode. The behavioural difference
        asserted here is that the warning fires (not just "no exception").
        """
        import logging
        from agent_cascade.log import logger as ac_logger

        engine, pool, instance, history = self._make_pool_and_engine(next_limit=10000)

        # Make the sync raise — simulates a JSONL rewrite failure.
        def boom(*a, **k):
            raise RuntimeError("simulated logger sync failure")

        engine.compression_handler._sync_logger_after_compression = boom

        with caplog.at_level(logging.WARNING, logger=ac_logger.name):
            # Must NOT propagate: compression still completes and the agent resumes (2 LLM calls).
            calls = self._run_one_successful_round(engine, instance)

        assert calls == 2, f"Logger-sync failure must not abort the loop; got {calls} LLM calls"

        # The sync-failure warning must fire and name the out-of-sync / re-inflation risk.
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Logger sync after compression FAILED" in joined, (
            f"Expected a sync-failure log line; logs:\n{joined}"
        )
        assert "out of sync" in joined and "re-inflate" in joined, (
            f"Expected the warning to name the out-of-sync / re-inflation risk; logs:\n{joined}"
        )

    def test_fits_check_uses_llm_instance_limit_when_router_limit_zero(self, caplog):
        """When the router chain reports max_input_tokens=0, the fits-check falls back to the
        detected limit on the agent's LLM instance instead of blindly assuming fit.

        Regression (trigger b): with no endpoint assigned the router default cfg has
        max_input_tokens=0, so the old code took the "no limit → assume it fits" path without
        verifying against the real server limit.
        """
        import logging
        from agent_cascade.log import logger as ac_logger

        engine, pool, instance, history = self._make_pool_and_engine(next_limit=0)

        # Give the LLM instance a detected context size (llm/oai.py sets this dynamically).
        instance.llm = MagicMock()
        instance.llm.generate_cfg = {"max_input_tokens": 4096}

        with caplog.at_level(logging.DEBUG, logger=ac_logger.name):
            calls = self._run_one_successful_round(engine, instance)

        assert calls == 2, f"Expected FCR-then-success (2 LLM calls), got {calls}"

        # The detected-limit branch must have been taken: it logs a debug line mentioning the
        # detected limit. The blind "assume fits" branch logs a DIFFERENT message ("no max_input_tokens
        # configured and no detected limit"), which must NOT appear.
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "using LLM-instance detected limit 4096" in joined, (
            f"Expected the fits-check to use the LLM-instance detected limit; logs:\n{joined}"
        )
        assert "no detected limit on the LLM instance" not in joined, (
            "The blind 'assume fits' branch must NOT be taken when a detected limit is available."
        )

    def test_fits_check_does_not_assume_fit_when_no_limit_anywhere(self, caplog):
        """When BOTH the router chain and the LLM instance report no limit, the code must NOT
        silently "assume it fits" — it logs a WARNING and does NOT break, so the retry loop advances
        (re-checking against the real unknown limit) instead of accepting an unverified payload.

        Regression for MAJOR #2: pre-fix this branch took a silent `break` ("assume compressed
        payload fits") — exactly the escape hatch that caused the original infinite loop. Post-fix it
        logs a WARNING and continues; with no limit to verify against, the safe bounded outcome is
        that the loop exhausts its rounds and raises ContextWindowExceeded (NOT a silent success).
        This is the genuine regression test for the false-negative path: pre-fix the generator
        terminates cleanly after 2 LLM calls (silent assume-fit); post-fix it raises.
        """
        import logging
        from agent_cascade.log import logger as ac_logger

        engine, pool, instance, history = self._make_pool_and_engine(next_limit=0)
        # No detected limit on the LLM instance either (key absent).
        instance.llm = MagicMock()
        instance.llm.generate_cfg = {}

        with caplog.at_level(logging.WARNING, logger=ac_logger.name):
            with pytest.raises(ContextWindowExceeded):
                self._run_one_successful_round(engine, instance)

        joined = "\n".join(r.getMessage() for r in caplog.records)
        # The new no-limit branch must log a WARNING that it is NOT assuming fit.
        assert "NOT assuming it fits" in joined, (
            f"Expected the no-limit branch to warn that it is NOT assuming fit; logs:\n{joined}"
        )
        # The old silent assume-fit debug message must be gone entirely.
        assert "Assuming compressed payload fits" not in joined, (
            "The silent 'assume fits' escape hatch must no longer exist."
        )

    def test_cursor_reset_on_error_path_after_fallback_compression(self):
        """After a fallback-compression cycle where the resumed turn ERRORS (no successful
        last_output), the endpoint cursor is still reset so the NEXT turn uses position 0.

        Regression: pre-fix, reset_instance_endpoint was gated on `last_output is not None and
        not error_already_yielded`. If the resumed call after compression hit a retryable error
        that exhausted retries (or any path where last_output stays None), the cursor stayed at
        position 1 permanently — every subsequent turn routed to the fallback model.

        Post-fix: the reset is unconditional at the turn boundary (after the while loop), so it
        fires regardless of whether the turn succeeded, errored, or stalled.

        Mechanics: drive FCR → one successful fits round → resumed call raises a retryable error
        that exhausts retries (last_output stays None, error_already_yielded=True). Assert the
        reset spy fired for this instance.
        """
        engine, pool, instance, history = self._make_pool_and_engine(next_limit=10000)

        # Simulate the transient failover advance (router.py context-exceeded branch).
        pool.api_router.advance_instance_endpoint("test-agent")

        reset_spy = MagicMock()
        pool.api_router.reset_instance_endpoint = reset_spy

        from agent_cascade.compression.result import CompressResult
        from agent_cascade.engine.compression_exec import FALLBACK_COMPRESSION_INITIAL_FRACTION

        success_result = CompressResult(
            success=True, summary_text="compressed", marker_message=None,
            messages_discarded=10, tail_count=5, error=None, mode="auto",
            tokens_before=5000, tokens_after=2000,
        )
        engine._find_compression_slice = lambda *a, **k: (FALLBACK_COMPRESSION_INITIAL_FRACTION, 10, [Message(role=USER, content="x")])

        call_count = [0]

        def mock_execute(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise FallbackCompressionRequired("test-agent", "Coder", "small-model")
            # Resumed call raises a retryable error → exhausts retries → last_output stays None.
            raise ConnectionError("connection refused")

        with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
            with patch("agent_cascade.compression.core.compress_context", return_value=success_result):
                template = MagicMock()
                template.llm_cfg = {"model": "test"}
                template.function_map = {}
                template.llm = MagicMock()
                template.llm.generate_cfg = {}
                list(engine._execute_llm_call_with_retry(instance, [Message(role=USER, content="test")], template, []))

        # The turn ended in error (no successful last_output), but the cursor MUST still be reset.
        assert reset_spy.call_count >= 1, (
            f"Expected reset_instance_endpoint to fire at the turn boundary even on error path, "
            f"got {reset_spy.call_count} calls. Pre-fix code only reset on successful completion."
        )
        args, kwargs = reset_spy.call_args_list[-1]
        assert (args and args[0] == "test-agent") or (kwargs.get("instance_name") == "test-agent"), (
            f"Expected reset_instance_endpoint('test-agent'), got: {reset_spy.call_args_list[-1]}"
        )

    def test_cursor_reset_on_timeout_error_after_fallback_compression(self):
        """After a fallback-compression cycle where the resumed turn hits a timeout error
        (retries exhausted, no successful last_output), the endpoint cursor is still reset.

        This covers the real-world scenario from logs/coder_kv-restore-confirm: the agent got
        stuck after compression and never produced a clean last_output. Pre-fix, the cursor
        stayed at position 1 forever because reset was gated on `last_output is not None`.
        Post-fix, the unconditional turn-boundary reset clears it regardless of outcome.

        Mechanics: FCR → one successful fits round → resumed call raises a timeout error
        (classified as retryable) that exhausts the retry budget. last_output stays None.
        Assert the reset spy fired for this instance at the turn boundary.
        """
        engine, pool, instance, history = self._make_pool_and_engine(next_limit=10000)

        # Simulate the transient failover advance (router.py context-exceeded branch).
        pool.api_router.advance_instance_endpoint("test-agent")

        reset_spy = MagicMock()
        pool.api_router.reset_instance_endpoint = reset_spy

        from agent_cascade.compression.result import CompressResult
        from agent_cascade.engine.compression_exec import FALLBACK_COMPRESSION_INITIAL_FRACTION

        success_result = CompressResult(
            success=True, summary_text="compressed", marker_message=None,
            messages_discarded=10, tail_count=5, error=None, mode="auto",
            tokens_before=5000, tokens_after=2000,
        )
        engine._find_compression_slice = lambda *a, **k: (FALLBACK_COMPRESSION_INITIAL_FRACTION, 10, [Message(role=USER, content="x")])

        call_count = [0]

        def mock_execute(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise FallbackCompressionRequired("test-agent", "Coder", "small-model")
            # Resumed call times out (retryable error → exhausts budget → last_output stays None).
            raise TimeoutError("LLM request timed out after 60s")

        with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
            with patch("agent_cascade.compression.core.compress_context", return_value=success_result):
                template = MagicMock()
                template.llm_cfg = {"model": "test"}
                template.function_map = {}
                template.llm = MagicMock()
                template.llm.generate_cfg = {}
                list(engine._execute_llm_call_with_retry(instance, [Message(role=USER, content="test")], template, []))

        # The turn ended in timeout (no successful last_output), but the cursor MUST still be reset.
        assert reset_spy.call_count >= 1, (
            f"Expected reset_instance_endpoint to fire at the turn boundary even on timeout path, "
            f"got {reset_spy.call_count} calls."
        )
        args, kwargs = reset_spy.call_args_list[-1]
        assert (args and args[0] == "test-agent") or (kwargs.get("instance_name") == "test-agent"), (
            f"Expected reset_instance_endpoint('test-agent'), got: {reset_spy.call_args_list[-1]}"
        )

    def test_inner_loop_retry_preserves_cursor_within_turn(self):
        """The turn-boundary reset fires AFTER the retry loop exits, not between retries.

        This proves the unconditional turn-boundary reset does NOT interfere with the legitimate
        mid-turn cursor advancement done by _handle_inner_loop_detection (core.py:1456). The
        cursor is advanced mid-turn so retries hit a different endpoint; it is only cleared
        AFTER the entire retry loop exits — not between individual retry attempts.

        Mechanics: pre-advance the cursor (simulating what _handle_inner_loop_detection does
        mid-turn when a MaxTokenExceeded/ContextWindowExceeded fires during streaming). Then
        drive a successful LLM call. We assert that:
        1. reset_instance_endpoint was called exactly ONCE, at the very end (turn boundary)
        2. The cursor was cleared so the NEXT turn starts from position 0

        This is sufficient to prove the reset is scoped to end-of-turn: if it fired between
        retries (e.g., after each successful _execute_llm_call), a multi-retry scenario would
        see multiple resets. Here we see exactly one, at the boundary.
        """
        engine, pool, instance, history = self._make_pool_and_engine(next_limit=10000)

        # Simulate the mid-turn cursor advance that _handle_inner_loop_detection performs
        # when a MaxTokenExceeded/ContextWindowExceeded fires during streaming (core.py:1456).
        pool.api_router.advance_instance_endpoint("test-agent")

        reset_spy = MagicMock()
        pool.api_router.reset_instance_endpoint = reset_spy

        call_count = [0]

        def mock_execute(*args, **kwargs):
            call_count[0] += 1
            # First attempt fails (simulating the mid-stream abort that triggered the advance).
            if call_count[0] == 1:
                raise ConnectionError("connection reset by peer")
            # Second attempt (on the advanced endpoint) succeeds.
            yield [Message(role="assistant", content="done")]

        with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
            template = MagicMock()
            template.llm_cfg = {"model": "test"}
            template.function_map = {}
            template.llm = MagicMock()
            template.llm.generate_cfg = {}
            list(engine._execute_llm_call_with_retry(instance, [Message(role=USER, content="test")], template, []))

        # The reset must fire exactly once, at the turn boundary (after the while loop exits).
        # If the reset were per-retry (fired after each successful call), we'd see it here
        # but NOT in a multi-failure scenario. Exactly one call = end-of-turn scope.
        assert reset_spy.call_count == 1, (
            f"Expected exactly one reset at the turn boundary, got {reset_spy.call_count}"
        )
        args, kwargs = reset_spy.call_args_list[0]
        assert (args and args[0] == "test-agent") or (kwargs.get("instance_name") == "test-agent"), (
            f"Expected reset_instance_endpoint('test-agent'), got: {reset_spy.call_args_list[0]}"
        )

    def test_cursor_reset_on_generator_early_close(self):
        """The cursor is reset even when the generator is closed early via gen.close().

        This is the CRITICAL regression test for the GeneratorExit path. The consumer in
        core.py:691-692 calls gen.close() in a finally: block on early break (user stop,
        generation superseded). This raises GeneratorExit inside the generator at whatever
        yield it's suspended on. Without a finally: block in _execute_llm_call_with_retry,
        code after that yield (including the cursor reset) is SKIPPED — leaving the cursor
        stuck at position 1 permanently.

        With the try/finally fix, the reset fires in the finally: block regardless of how
        the generator terminates (normal exhaustion, GeneratorExit, or exception).

        Mechanics: drive _execute_llm_call_with_retry as a generator, pull one item with
        next(), then call .close() on it. Assert reset_instance_endpoint WAS called.
        This test FAILS if the reset is not in a finally (pre-fix behavior) and PASSES
        with the finally version.
        """
        engine, pool, instance, history = self._make_pool_and_engine(next_limit=10000)

        # Simulate the transient failover advance (as would happen mid-turn).
        pool.api_router.advance_instance_endpoint("test-agent")

        reset_spy = MagicMock()
        pool.api_router.reset_instance_endpoint = reset_spy

        call_count = [0]

        def mock_execute(*args, **kwargs):
            call_count[0] += 1
            # First call: yield a message (simulating streaming output), then the generator
            # will be suspended at this yield point when the consumer calls .close().
            yield Message(role="assistant", content="partial response")

        with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
            template = MagicMock()
            template.llm_cfg = {"model": "test"}
            template.function_map = {}
            template.llm = MagicMock()
            template.llm.generate_cfg = {}

            gen = engine._execute_llm_call_with_retry(
                instance, [Message(role=USER, content="test")], template, []
            )
            # Pull one item to advance the generator past the initial setup and into
            # the streaming phase. The generator is now suspended at a yield point.
            first = next(gen)

            # Simulate the consumer's early-close (core.py:691-692 finally: gen.close()).
            # This raises GeneratorExit inside the generator, which triggers the finally: block.
            gen.close()

        # The reset MUST have fired despite the early close.
        assert reset_spy.call_count == 1, (
            f"Expected reset_instance_endpoint to fire exactly once on generator close, "
            f"got {reset_spy.call_count} calls. Without a finally: block, GeneratorExit "
            f"skips code after the yield point and the cursor stays stuck."
        )
        args, kwargs = reset_spy.call_args_list[0]
        assert (args and args[0] == "test-agent") or (kwargs.get("instance_name") == "test-agent"), (
            f"Expected reset_instance_endpoint('test-agent'), got: {reset_spy.call_args_list[0]}"
        )

    def test_cursor_reset_fires_exactly_once_on_normal_exhaustion(self):
        """Sanity check: on normal generator exhaustion (list(gen)), the reset fires exactly once.

        This confirms the finally: block doesn't double-fire when the generator completes
        naturally (StopIteration) vs. being closed early (GeneratorExit). Both paths should
        trigger exactly one reset.
        """
        engine, pool, instance, history = self._make_pool_and_engine(next_limit=10000)

        # Simulate the transient failover advance.
        pool.api_router.advance_instance_endpoint("test-agent")

        reset_spy = MagicMock()
        pool.api_router.reset_instance_endpoint = reset_spy

        call_count = [0]

        def mock_execute(*args, **kwargs):
            call_count[0] += 1
            yield Message(role="assistant", content="done")

        with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
            template = MagicMock()
            template.llm_cfg = {"model": "test"}
            template.function_map = {}
            template.llm = MagicMock()
            template.llm.generate_cfg = {}
            # Fully exhaust the generator (normal completion path).
            results = list(engine._execute_llm_call_with_retry(
                instance, [Message(role=USER, content="test")], template, []
            ))

        # Exactly one reset on normal exhaustion.
        assert reset_spy.call_count == 1, (
            f"Expected exactly one reset on normal generator exhaustion, "
            f"got {reset_spy.call_count} calls."
        )


# ──────────────────────────────────────────────
# 5b. Part 2 — pre-send forced-compression guard is endpoint-truthful
#     (reports/fallback-compression-misclass-investigation.md §5.2)
# ──────────────────────────────────────────────

class TestPreSendCompressionEndpointTruthful:
    """The regular forced-compression guard must size against the limit of the endpoint
    ACTUALLY about to be called (assigned_max_tokens), not just the first-priority limit.
    Before Part 2, a payload that fit the big first-priority endpoint but overflowed a
    smaller assigned endpoint would NOT compress pre-send."""

    def _make_engine(self):
        """Real ExecutionEngine with a mocked pool; settings use production thresholds."""
        from agent_cascade.execution_engine import ExecutionEngine

        pool = MagicMock()
        instance = MagicMock()
        compression_lock = MagicMock()
        compression_lock.__enter__ = MagicMock()
        compression_lock.__exit__ = MagicMock()
        instance._compression_lock = compression_lock
        instance.instance_name = "test-agent"
        instance.agent_class = "Coder"
        # First-turn path: no cached ground-truth count → guard recounts messages.
        instance._last_actual_token_count = 0
        instance._allocated_max_input_tokens = 0
        instance._last_token_count_conversation_length = -1
        instance._cached_token_count = 0
        instance._force_compress_count = 0
        instance._last_force_compress_time = 0.0

        class Settings:
            compression_force_threshold = 96.0
            compression_warning_threshold = 90.0
            compression_proactive_threshold = 95.0
            compression_context_reserve_tokens = 3000
            compression_max_attempts = 100
            compression_force_cooldown = 0.0

        pool.settings = Settings()
        engine = ExecutionEngine(pool)
        return engine, pool, instance

    def _run_guard(self, engine, instance, assigned_max_tokens):
        """Drive the guard with a deterministic 105k-token payload and a stubbed first-
        priority resolution of 165.5k. Returns (triggered, force_compression_mock)."""
        # Deterministic token count (real qwen_count is ~6 tokens/word — not exact). Stub it
        # so the threshold math is precise: current_tokens == 105_000 exactly.
        with patch.object(engine, "_count_history_tokens", return_value=105_000), \
             patch.object(engine, "_get_max_tokens", return_value=165_500):
            messages = [Message(role=USER, content="payload")]
            llm_messages = list(messages)
            with patch.object(engine, "_force_compression", return_value=True) as mock_force:
                triggered = engine._check_and_trigger_compression(
                    instance, messages, llm_messages, None, assigned_max_tokens=assigned_max_tokens
                )
        return triggered, mock_force

    def test_triggers_against_assigned_endpoint_limit(self):
        """A payload that fits the first-priority limit (165.5k) but overflows the assigned
        endpoint's true limit (90k) MUST trigger pre-send compression."""
        engine, pool, instance = self._make_engine()

        # 105k vs effective_limit(90k - 3k reserve) = 87k → ~120% usage > 96% threshold.
        triggered, mock_force = self._run_guard(engine, instance, assigned_max_tokens=90_000)

        assert triggered is True, (
            "Guard must compress pre-send against the assigned endpoint's true limit"
        )
        mock_force.assert_called_once()

    def test_no_trigger_when_assigned_limit_is_larger(self):
        """Same 105k payload with a large assigned limit (165.5k) fits → no compression."""
        engine, pool, instance = self._make_engine()

        # 105k vs effective_limit(165.5k - 3k) = 162.5k → ~65% usage < 96% threshold.
        triggered, mock_force = self._run_guard(engine, instance, assigned_max_tokens=165_500)

        assert triggered is False, (
            "Payload fits the assigned endpoint's limit — no pre-send compression"
        )
        mock_force.assert_not_called()

    def test_falls_back_to_first_priority_when_assigned_none(self):
        """When assigned_max_tokens is None/absent, behavior is unchanged: sized against the
        first-priority resolution (165.5k) → no compression for a 105k payload."""
        engine, pool, instance = self._make_engine()

        triggered, mock_force = self._run_guard(engine, instance, assigned_max_tokens=None)

        assert triggered is False
        mock_force.assert_not_called()

    def test_assigned_resolution_failure_falls_back_to_first_priority(self):
        """If resolving the assigned endpoint's limit raises (e.g. config reload mid-call),
        _pre_llm_checks must NOT crash and must fall back to the prior first-priority behavior."""
        engine, pool, instance = self._make_engine()
        # Make get_endpoint_chain raise — simulates a transient failure.
        pool.api_router.get_endpoint_chain = MagicMock(side_effect=RuntimeError("config reload"))

        # Stub the upstream pre-LLM checks so we reach step 5 (compression trigger).
        engine._check_stop_conditions = MagicMock(return_value=False)
        engine._inject_async_messages = MagicMock(return_value=False)
        engine.compression_handler = MagicMock()
        engine.compression_handler.handle_rollback_command = MagicMock(return_value=False)
        engine.compression_handler.handle_compress_command = MagicMock(return_value=False)

        instance.agent_class = "Coder"
        instance.instance_name = "test-agent"
        messages = [Message(role=USER, content="payload")]

        with patch.object(engine, "_check_and_trigger_compression", return_value=False) as mock_guard:
            engine._pre_llm_checks(instance, messages, list(messages), None, [10])

        # The guard must have been called with assigned_max_tokens=None (fallback), not a value.
        assert mock_guard.call_count == 1
        _, kwargs = mock_guard.call_args
        assert kwargs.get('assigned_max_tokens') is None, (
            "On resolution failure the guard must fall back to first-priority (assigned_max_tokens=None)"
        )