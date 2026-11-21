"""Unit tests for centralized retry policy module (Phase 1 of retry refactoring).

Tests verify:
- classify_error() correctly categorizes fatal, retryable, and unknown errors
- calculate_backoff() produces exponential growth with jitter and proper caps
- RetryPolicy defaults are reasonable
- Policy creation from settings works correctly

Run: pytest tests/test_retry_policy.py -v
"""

import pytest

from agent_cascade.retry_policy import (POLICY_AGGRESSIVE, POLICY_CONSERVATIVE, POLICY_DEFAULT, RetryPolicy,
                                        calculate_backoff, classify_error, deterministic_client_error_patterns,
                                        is_deterministic_client_error)

# ── RetryPolicy defaults ────────────────────────────────────────────────────


class TestRetryPolicyDefaults:
    """Verify default policy values are reasonable."""

    def test_default_retry_max_attempts(self):
        assert RetryPolicy().retry_max_attempts == 3

    def test_default_base_delay(self):
        assert RetryPolicy().base_delay == 1.0

    def test_default_max_delay(self):
        assert RetryPolicy().max_delay == 8.0

    def test_default_jitter_factor(self):
        assert RetryPolicy().jitter_factor == 0.1

    def test_default_endpoint_max_retries(self):
        assert RetryPolicy().endpoint_max_retries == 1

    def test_default_jitter_mode_is_full(self):
        """Default jitter is full (AWS) for thundering-herd de-sync."""
        assert RetryPolicy().jitter_mode == 'full'

    def test_policy_is_frozen(self):
        """RetryPolicy is frozen (immutable) to prevent accidental mutation."""
        policy = RetryPolicy()
        with pytest.raises(Exception):  # FrozenInstanceError in Python 3.10+
            policy.retry_max_attempts = 5  # type: ignore


# ── Predefined policies ─────────────────────────────────────────────────────


class TestPredefinedPolicies:
    """Verify predefined policy constants match plan specifications."""

    def test_policy_default_values(self):
        assert POLICY_DEFAULT.retry_max_attempts == 3
        assert POLICY_DEFAULT.base_delay == 1.0
        assert POLICY_DEFAULT.max_delay == 8.0
        assert POLICY_DEFAULT.endpoint_max_retries == 1

    def test_policy_aggressive_values(self):
        assert POLICY_AGGRESSIVE.retry_max_attempts == 5
        assert POLICY_AGGRESSIVE.base_delay == 0.5
        assert POLICY_AGGRESSIVE.max_delay == 4.0
        assert POLICY_AGGRESSIVE.endpoint_max_retries == 1

    def test_policy_conservative_values(self):
        assert POLICY_CONSERVATIVE.retry_max_attempts == 2
        assert POLICY_CONSERVATIVE.base_delay == 2.0
        assert POLICY_CONSERVATIVE.max_delay == 10.0
        assert POLICY_CONSERVATIVE.endpoint_max_retries == 0


# ── classify_error() ────────────────────────────────────────────────────────


class TestClassifyErrorFatal:
    """Errors that should NOT be retried."""

    def test_auth_invalid_api_key(self):
        assert classify_error(Exception('invalid_api_key')) == 'fatal'

    def test_auth_unauthorized(self):
        assert classify_error(Exception('401 Unauthorized')) == 'fatal'

    def test_auth_forbidden(self):
        assert classify_error(Exception('403 Forbidden')) == 'fatal'

    def test_auth_permission_denied(self):
        assert classify_error(Exception('Permission denied for this model')) == 'fatal'

    def test_quota_insufficient(self):
        assert classify_error(Exception('insufficient_quota')) == 'fatal'

    def test_billing_error(self):
        assert classify_error(Exception('billing_error: account overdue')) == 'fatal'

    def test_account_not_active(self):
        assert classify_error(Exception('account_not_active')) == 'fatal'

    def test_model_not_found(self):
        assert classify_error(Exception('model_not_found: gpt-999')) == 'fatal'

    def test_invalid_model(self):
        assert classify_error(Exception('invalid_model name')) == 'fatal'

    def test_invalid_request(self):
        assert classify_error(Exception('invalid_request: bad parameters')) == 'fatal'

    def test_validation_error(self):
        assert classify_error(Exception('validation failed for field temperature')) == 'fatal'

    def test_case_insensitive(self):
        """Error classification is case-insensitive."""
        assert classify_error(Exception('INVALID_API_KEY')) == 'fatal'
        assert classify_error(Exception('Insufficient_Quota')) == 'fatal'


class TestClassifyErrorRetryable:
    """Transient errors that should be retried."""

    def test_connection_reset(self):
        assert classify_error(Exception('Connection reset by peer')) == 'retryable'

    def test_timeout(self):
        assert classify_error(Exception('Request timed out')) == 'retryable'

    def test_timed_out(self):
        assert classify_error(Exception('Operation timed out after 30s')) == 'retryable'

    def test_ssl_error(self):
        assert classify_error(Exception('SSL handshake failed')) == 'retryable'

    def test_broken_pipe(self):
        assert classify_error(Exception('[Errno 32] Broken pipe')) == 'retryable'

    def test_disconnected(self):
        assert classify_error(Exception('Server disconnected unexpectedly')) == 'retryable'

    def test_eof(self):
        assert classify_error(Exception('EOF when reading from socket')) == 'retryable'

    def test_refused(self):
        assert classify_error(Exception('Connection refused')) == 'retryable'

    def test_terminated(self):
        assert classify_error(Exception('Fetch failed: terminated')) == 'retryable'

    def test_fetch_failed(self):
        assert classify_error(Exception('fetch failed due to network error')) == 'retryable'

    def test_server_error_503(self):
        assert classify_error(Exception('HTTP 503 Service Unavailable')) == 'retryable'

    def test_server_error_502(self):
        assert classify_error(Exception('502 Bad Gateway')) == 'retryable'

    def test_server_error_504(self):
        assert classify_error(Exception('504 Gateway Timeout')) == 'retryable'

    def test_rate_limit_429(self):
        assert classify_error(Exception('429 Too Many Requests')) == 'retryable'

    def test_network_unreachable(self):
        assert classify_error(Exception('Network unreachable')) == 'retryable'

    def test_dns_failure(self):
        assert classify_error(Exception('DNS resolution failed')) == 'retryable'

    def test_overloaded(self):
        assert classify_error(Exception('Server overloaded')) == 'retryable'

    def test_service_unavailable(self):
        assert classify_error(Exception('Service unavailable')) == 'retryable'


class TestClassifyErrorUnknown:
    """Uncategorized errors default to retryable for safety."""

    def test_unknown_error_defaults_to_retryable(self):
        assert classify_error(Exception('Something weird happened')) == 'unknown'

    def test_empty_error_message(self):
        assert classify_error(Exception('')) == 'unknown'

    def test_generic_exception(self):
        assert classify_error(RuntimeError('unexpected state')) == 'unknown'


class TestClassifyErrorPriority:
    """When multiple patterns match, fatal takes priority."""

    def test_fatal_takes_priority_over_retryable(self):
        """If an error message contains both fatal and retryable patterns, fatal wins."""
        # e.g., "Connection timeout with invalid_api_key" — auth issue is more important
        assert classify_error(Exception('Connection failed: invalid_api_key')) == 'fatal'


# ── classify_error() — deterministic client errors (Fix B2) ─────────────────


class TestClassifyErrorDeterministicClientErrors:
    """Deterministic 4xx client errors are classified as fatal (do not retry).

    These errors will recur on every attempt to the same endpoint, so retrying
    is pointless. Previously they fell through to 'unknown' and consumed the
    whole retry budget (see the gpt-5.6-luna reasoning_effort incident).
    """

    def test_classify_deterministic_400_is_fatal(self):
        """A bare 400 client error is fatal, even with a non-specific message."""
        from agent_cascade.llm.base import ModelServiceError
        err = ModelServiceError(code='400', message='Bad Request')
        assert classify_error(err) == 'fatal'

    def test_classify_reasoning_effort_error_is_fatal(self):
        """The incident's exact error message is classified as fatal."""
        msg = 'Function tools with reasoning_effort are not supported for gpt-5.6-luna'
        assert classify_error(Exception(msg)) == 'fatal'

    def test_classify_not_supported_feature_is_fatal(self):
        assert classify_error(Exception('Feature X is not supported for this model')) == 'fatal'

    def test_classify_invalid_api_key_phrase_is_fatal(self):
        # Space-separated "invalid api key" (the deterministic pattern) vs. the
        # underscored fatal_patterns entry — both must resolve to fatal.
        assert classify_error(Exception('Error: invalid api key provided')) == 'fatal'

    def test_classify_model_not_found_phrase_is_fatal(self):
        assert classify_error(Exception('model not found: gpt-999')) == 'fatal'

    def test_classify_does_not_exist_is_fatal(self):
        assert classify_error(Exception('The requested model does not exist')) == 'fatal'

    def test_classify_4xx_codes_are_fatal(self):
        """ModelServiceError carrying a 4xx status code is fatal."""
        from agent_cascade.llm.base import ModelServiceError
        for code in ('400', '401', '403', '404', '422'):
            err = ModelServiceError(code=code, message='opaque server error')
            assert classify_error(err) == 'fatal', f"code {code} should be fatal"

    def test_classify_deterministic_4xx_beats_retryable_pattern(self):
        """A deterministic 4xx that also mentions a transient keyword is still fatal.

        e.g., a 400 whose body happens to contain "timeout" must not be retried —
        the client error takes priority over the retryable pattern.
        """
        from agent_cascade.llm.base import ModelServiceError
        err = ModelServiceError(code='400', message='request timeout in tool schema')
        assert classify_error(err) == 'fatal'

    def test_classify_transient_errors_unchanged(self):
        """Transient (5xx / network) errors keep their previous classification — no regression."""
        assert classify_error(Exception('HTTP 503 Service Unavailable')) == 'retryable'
        assert classify_error(Exception('502 Bad Gateway')) == 'retryable'
        assert classify_error(Exception('Request timed out')) == 'retryable'
        assert classify_error(Exception('Connection reset by peer')) == 'retryable'
        assert classify_error(Exception('429 Too Many Requests')) == 'retryable'

    def test_classify_unknown_unchanged(self):
        """Uncategorized errors still default to 'unknown' — no regression."""
        assert classify_error(Exception('Something weird happened')) == 'unknown'
        assert classify_error(Exception('')) == 'unknown'


class TestIsDeterministicClientErrorHelper:
    """The shared helper used by both classify_error and the router blacklist (Fix B1)."""

    def test_helper_detects_pattern_in_message(self):
        assert is_deterministic_client_error(Exception('tools not supported')) is True
        assert is_deterministic_client_error(Exception('reasoning_effort rejected')) is True

    def test_helper_detects_4xx_code_on_model_service_error(self):
        from agent_cascade.llm.base import ModelServiceError
        for code in ('400', '401', '403', '404', '422'):
            assert is_deterministic_client_error(ModelServiceError(code=code, message='x')) is True

    def test_helper_false_for_transient(self):
        assert is_deterministic_client_error(Exception('connection reset by peer')) is False
        assert is_deterministic_client_error(Exception('HTTP 503 Service Unavailable')) is False

    def test_helper_false_for_non_4xx_model_service_error(self):
        """A ModelServiceError with a non-4xx code and no matching pattern is not deterministic."""
        from agent_cascade.llm.base import ModelServiceError
        assert is_deterministic_client_error(ModelServiceError(code='503', message='server hiccup')) is False

    def test_helper_false_for_empty(self):
        assert is_deterministic_client_error(Exception('')) is False

    def test_patterns_tuple_is_nonempty_and_lowercase(self):
        """The shared pattern tuple exists and entries are lowercase (matching lowercased input)."""
        assert len(deterministic_client_error_patterns) > 0
        for p in deterministic_client_error_patterns:
            assert p == p.lower()


# ── calculate_backoff() ─────────────────────────────────────────────────────


class TestCalculateBackoffExponentialGrowth:
    """Verify the full-jitter ceiling grows exponentially (1→2→4→8) per attempt.

    Full jitter randomises within [0.1, min(raw, max_delay)], so we assert the
    deterministic ceiling (raw-driven) rather than a point value.
    """

    def test_attempt_1(self):
        """First retry: ceiling = base_delay * 2^0 = base_delay."""
        policy = RetryPolicy(base_delay=1.0)
        raw = policy.base_delay * 2 ** (1 - 1)  # 1.0
        assert 0.1 <= calculate_backoff(1, policy) <= min(raw, policy.max_delay)

    def test_attempt_2(self):
        """Second retry: ceiling = base_delay * 2^1 = 2 * base_delay."""
        policy = RetryPolicy(base_delay=1.0)
        raw = policy.base_delay * 2 ** (2 - 1)  # 2.0
        assert 0.1 <= calculate_backoff(2, policy) <= min(raw, policy.max_delay)

    def test_attempt_3(self):
        """Third retry: ceiling = base_delay * 2^2 = 4 * base_delay."""
        policy = RetryPolicy(base_delay=1.0)
        raw = policy.base_delay * 2 ** (3 - 1)  # 4.0
        assert 0.1 <= calculate_backoff(3, policy) <= min(raw, policy.max_delay)

    def test_attempt_4(self):
        """Fourth retry: ceiling = base_delay * 2^3 = 8 * base_delay (== max_delay)."""
        policy = RetryPolicy(base_delay=1.0)
        raw = policy.base_delay * 2 ** (4 - 1)  # 8.0
        assert 0.1 <= calculate_backoff(4, policy) <= min(raw, policy.max_delay)

    def test_custom_base_delay(self):
        """Backoff ceiling scales with custom base_delay (0.5 → 0.5, 1.0, 2.0)."""
        policy = RetryPolicy(base_delay=0.5)
        for attempt in (1, 2, 3):
            raw = policy.base_delay * 2 ** (attempt - 1)
            assert 0.1 <= calculate_backoff(attempt, policy) <= min(raw, policy.max_delay)

    def test_ceiling_grows_discriminating(self):
        """A constant-ceiling regression (e.g. always returning base_delay) must fail.

        Over N draws of attempt-3 (raw=4.0), at least one draw must exceed the
        attempt-1 ceiling (raw=1.0). A constant-ceiling bug returning a fixed value
        in [0.1, 1.0] would fail this; real full jitter passes with P≈1.
        """
        policy = RetryPolicy(base_delay=1.0)  # max_delay=8.0
        ceil_3 = min(1.0 * 4, policy.max_delay)  # 4.0
        ceil_1 = min(1.0 * 1, policy.max_delay)  # 1.0
        results = [calculate_backoff(3, policy) for _ in range(100)]
        assert max(results) > ceil_1, \
            f"Ceiling does not grow: max of 100 attempt-3 draws is {max(results):.3f} <= {ceil_1}"


class TestCalculateBackoffJitter:
    """Verify full (AWS) jitter spreads delays across the raw-driven range."""

    def test_jitter_stays_within_bounds(self):
        """Full jitter keeps every draw within [0.1, min(raw, max_delay)]."""
        # max_delay high enough that the cap does not clamp the ceiling
        policy = RetryPolicy(base_delay=1.0, max_delay=100.0)
        raw = 1.0  # attempt 1: 1 * 2^0

        # Run many times to ensure jitter is actually applied and stays in bounds
        results = [calculate_backoff(1, policy) for _ in range(100)]

        lower_bound = 0.1
        upper_bound = min(raw, policy.max_delay)

        assert all(lower_bound <= r <= upper_bound for r in results), \
            f"Some results outside [{lower_bound}, {upper_bound}]"

    def test_jitter_produces_variation(self):
        """Full jitter should produce different values (not always the same)."""
        policy = RetryPolicy(base_delay=1.0)

        # Full jitter draws from uniform[0.1, raw=1.0]; it's statistically unlikely
        # all 50 calls return the same value.
        results = [calculate_backoff(1, policy) for _ in range(50)]
        unique_results = set(results)

        assert len(unique_results) > 1, 'Jitter not producing variation'


class TestCalculateBackoffCaps:
    """Verify backoff respects min and max bounds."""

    def test_max_delay_cap(self):
        """Full-jitter ceiling is capped at max_delay regardless of attempt."""
        policy = RetryPolicy(base_delay=1.0, max_delay=5.0)

        # Attempt 4 raw=8.0 > max_delay, so every draw is capped within [0.1, 5.0]
        results = [calculate_backoff(4, policy) for _ in range(100)]
        assert all(0.1 <= r <= 5.0 for r in results)

        # Even higher attempts stay capped at max_delay
        results = [calculate_backoff(10, policy) for _ in range(100)]
        assert all(0.1 <= r <= 5.0 for r in results)

    def test_min_delay_floor(self):
        """Backoff never goes below 0.1 seconds."""
        # raw = 0.05 < 0.1, so the 0.1 floor clamp dominates the whole draw range.
        policy = RetryPolicy(base_delay=0.05)

        # Every draw must respect the 0.1s floor.
        for _ in range(100):
            result = calculate_backoff(1, policy)
            assert result >= 0.1, f"Backoff {result} below minimum 0.1"


class TestCalculateBackoffIntegration:
    """End-to-end backoff behavior with realistic policy."""

    def test_default_policy_sequence(self):
        """Default full-jitter policy keeps delays within [0.1, min(raw, max_delay)]."""
        policy = POLICY_DEFAULT  # base_delay=1.0, max_delay=8.0

        # attempt 1 raw=1.0 → [0.1, 1.0]
        assert 0.1 <= calculate_backoff(1, policy) <= 1.0
        # attempt 2 raw=2.0 → [0.1, 2.0]
        assert 0.1 <= calculate_backoff(2, policy) <= 2.0
        # attempt 3 raw=4.0 → [0.1, 4.0]
        assert 0.1 <= calculate_backoff(3, policy) <= 4.0


class TestCalculateBackoffPlusMode:
    """Legacy additive (plus) jitter: raw + uniform(0, jitter_factor) * raw.

    Retained so the old behaviour stays covered and reversible via jitter_mode="plus".
    """

    def test_calculate_backoff_plus_mode(self):
        """Plus mode never reduces delay below raw and stays within raw*(1+jitter_factor)."""
        policy = RetryPolicy(base_delay=1.0, max_delay=100.0,
                             jitter_factor=0.1, jitter_mode='plus')
        raw = 1.0  # attempt 1: 1 * 2^0

        results = [calculate_backoff(1, policy) for _ in range(100)]
        lower_bound = raw  # positive-only jitter never reduces delay
        upper_bound = raw * (1 + policy.jitter_factor)

        assert all(lower_bound <= r <= upper_bound for r in results), \
            f"Plus-mode results outside [{lower_bound}, {upper_bound}]"
        # Jitter still produces variation
        assert len(set(results)) > 1


# ── Policy creation from settings ───────────────────────────────────────────


class TestPolicyFromSettings:
    """Verify RetryPolicy can be constructed from PoolSettings fields."""

    def test_create_policy_from_settings(self):
        """A RetryPolicy should be constructible from settings values."""
        # Simulating what execution_engine.py will do in Phase 4
        retry_max_attempts = 3
        retry_base_delay = 1.0
        retry_max_delay = 8.0

        policy = RetryPolicy(
            retry_max_attempts=retry_max_attempts,
            base_delay=retry_base_delay,
            max_delay=retry_max_delay,
        )

        assert policy.retry_max_attempts == 3
        assert policy.base_delay == 1.0
        assert policy.max_delay == 8.0
        # Defaults should apply for unspecified fields
        assert policy.jitter_factor == 0.1
        assert policy.endpoint_max_retries == 1

    def test_create_policy_with_custom_values(self):
        """Custom settings values should be respected."""
        policy = RetryPolicy(
            retry_max_attempts=5,
            base_delay=0.5,
            max_delay=4.0,
            endpoint_max_retries=2,
        )

        assert policy.retry_max_attempts == 5
        assert policy.base_delay == 0.5
        assert policy.max_delay == 4.0
        assert policy.endpoint_max_retries == 2


# ── D-7: Terminal exceptions are always fatal (typed short-circuit) ───────────


class TestTerminalExceptionClassification:
    """SlotCancelled and AgentTerminatedError must classify as 'fatal'
    regardless of their message text (proves typed check precedes string match)."""

    def test_classify_slot_cancelled_is_fatal(self):
        from agent_cascade.slot_queue import SlotCancelled
        # Message contains 'timeout' which would normally be retryable
        err = SlotCancelled(message='Slot queue timeout for ticket 1 (agent=Maine)')
        assert classify_error(err) == 'fatal'

    def test_classify_slot_cancelled_with_terminated_text_is_fatal(self):
        from agent_cascade.slot_queue import SlotCancelled
        # Message contains 'terminated' which could match a fatal pattern,
        # but the typed check must fire first
        err = SlotCancelled(message='agent terminated during slot wait')
        assert classify_error(err) == 'fatal'

    def test_classify_agent_terminated_is_fatal(self):
        from agent_cascade.exceptions import AgentTerminatedError
        err = AgentTerminatedError('Instance dismissed by user')
        assert classify_error(err) == 'fatal'
