"""Unit tests for centralized retry policy module (Phase 1 of retry refactoring).

Tests verify:
- classify_error() correctly categorizes fatal, retryable, and unknown errors
- calculate_backoff() produces exponential growth with jitter and proper caps
- RetryPolicy defaults are reasonable
- Policy creation from settings works correctly

Run: pytest tests/test_retry_policy.py -v
"""

import random
from dataclasses import replace

import pytest

from agent_cascade.retry_policy import (
    RetryPolicy,
    classify_error,
    calculate_backoff,
    POLICY_DEFAULT,
    POLICY_AGGRESSIVE,
    POLICY_CONSERVATIVE,
)


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
        assert classify_error(Exception("invalid_api_key")) == 'fatal'

    def test_auth_unauthorized(self):
        assert classify_error(Exception("401 Unauthorized")) == 'fatal'

    def test_auth_forbidden(self):
        assert classify_error(Exception("403 Forbidden")) == 'fatal'

    def test_auth_permission_denied(self):
        assert classify_error(Exception("Permission denied for this model")) == 'fatal'

    def test_quota_insufficient(self):
        assert classify_error(Exception("insufficient_quota")) == 'fatal'

    def test_billing_error(self):
        assert classify_error(Exception("billing_error: account overdue")) == 'fatal'

    def test_account_not_active(self):
        assert classify_error(Exception("account_not_active")) == 'fatal'

    def test_model_not_found(self):
        assert classify_error(Exception("model_not_found: gpt-999")) == 'fatal'

    def test_invalid_model(self):
        assert classify_error(Exception("invalid_model name")) == 'fatal'

    def test_invalid_request(self):
        assert classify_error(Exception("invalid_request: bad parameters")) == 'fatal'

    def test_validation_error(self):
        assert classify_error(Exception("validation failed for field temperature")) == 'fatal'

    def test_case_insensitive(self):
        """Error classification is case-insensitive."""
        assert classify_error(Exception("INVALID_API_KEY")) == 'fatal'
        assert classify_error(Exception("Insufficient_Quota")) == 'fatal'


class TestClassifyErrorRetryable:
    """Transient errors that should be retried."""

    def test_connection_reset(self):
        assert classify_error(Exception("Connection reset by peer")) == 'retryable'

    def test_timeout(self):
        assert classify_error(Exception("Request timed out")) == 'retryable'

    def test_timed_out(self):
        assert classify_error(Exception("Operation timed out after 30s")) == 'retryable'

    def test_ssl_error(self):
        assert classify_error(Exception("SSL handshake failed")) == 'retryable'

    def test_broken_pipe(self):
        assert classify_error(Exception("[Errno 32] Broken pipe")) == 'retryable'

    def test_disconnected(self):
        assert classify_error(Exception("Server disconnected unexpectedly")) == 'retryable'

    def test_eof(self):
        assert classify_error(Exception("EOF when reading from socket")) == 'retryable'

    def test_refused(self):
        assert classify_error(Exception("Connection refused")) == 'retryable'

    def test_terminated(self):
        assert classify_error(Exception("Fetch failed: terminated")) == 'retryable'

    def test_fetch_failed(self):
        assert classify_error(Exception("fetch failed due to network error")) == 'retryable'

    def test_server_error_503(self):
        assert classify_error(Exception("HTTP 503 Service Unavailable")) == 'retryable'

    def test_server_error_502(self):
        assert classify_error(Exception("502 Bad Gateway")) == 'retryable'

    def test_server_error_504(self):
        assert classify_error(Exception("504 Gateway Timeout")) == 'retryable'

    def test_rate_limit_429(self):
        assert classify_error(Exception("429 Too Many Requests")) == 'retryable'

    def test_network_unreachable(self):
        assert classify_error(Exception("Network unreachable")) == 'retryable'

    def test_dns_failure(self):
        assert classify_error(Exception("DNS resolution failed")) == 'retryable'

    def test_overloaded(self):
        assert classify_error(Exception("Server overloaded")) == 'retryable'

    def test_service_unavailable(self):
        assert classify_error(Exception("Service unavailable")) == 'retryable'


class TestClassifyErrorUnknown:
    """Uncategorized errors default to retryable for safety."""

    def test_unknown_error_defaults_to_retryable(self):
        assert classify_error(Exception("Something weird happened")) == 'unknown'

    def test_empty_error_message(self):
        assert classify_error(Exception("")) == 'unknown'

    def test_generic_exception(self):
        assert classify_error(RuntimeError("unexpected state")) == 'unknown'


class TestClassifyErrorPriority:
    """When multiple patterns match, fatal takes priority."""

    def test_fatal_takes_priority_over_retryable(self):
        """If an error message contains both fatal and retryable patterns, fatal wins."""
        # e.g., "Connection timeout with invalid_api_key" — auth issue is more important
        assert classify_error(Exception("Connection failed: invalid_api_key")) == 'fatal'


# ── calculate_backoff() ─────────────────────────────────────────────────────

class TestCalculateBackoffExponentialGrowth:
    """Verify exponential backoff formula without jitter interference."""

    def test_attempt_1(self):
        """First retry: base_delay * 2^0 = base_delay."""
        policy = RetryPolicy(base_delay=1.0, jitter_factor=0.0)
        result = calculate_backoff(1, policy)
        assert result == pytest.approx(1.0)

    def test_attempt_2(self):
        """Second retry: base_delay * 2^1 = 2 * base_delay."""
        policy = RetryPolicy(base_delay=1.0, jitter_factor=0.0)
        result = calculate_backoff(2, policy)
        assert result == pytest.approx(2.0)

    def test_attempt_3(self):
        """Third retry: base_delay * 2^2 = 4 * base_delay."""
        policy = RetryPolicy(base_delay=1.0, jitter_factor=0.0)
        result = calculate_backoff(3, policy)
        assert result == pytest.approx(4.0)

    def test_attempt_4(self):
        """Fourth retry: base_delay * 2^3 = 8 * base_delay."""
        policy = RetryPolicy(base_delay=1.0, jitter_factor=0.0)
        result = calculate_backoff(4, policy)
        assert result == pytest.approx(8.0)

    def test_custom_base_delay(self):
        """Backoff scales with custom base_delay."""
        policy = RetryPolicy(base_delay=0.5, jitter_factor=0.0)
        assert calculate_backoff(1, policy) == pytest.approx(0.5)
        assert calculate_backoff(2, policy) == pytest.approx(1.0)
        assert calculate_backoff(3, policy) == pytest.approx(2.0)


class TestCalculateBackoffJitter:
    """Verify jitter adds positive-only randomness within expected bounds."""

    def test_jitter_stays_within_bounds(self):
        """Jitter should keep result between raw and raw*(1+jitter_factor) — never reduces delay."""
        # Use max_delay high enough that jitter bounds are not affected by capping
        policy = RetryPolicy(base_delay=1.0, max_delay=100.0, jitter_factor=0.1)  # +10% max
        raw = 1.0  # attempt 1: 1 * 2^0

        # Run many times to ensure jitter is actually applied and stays in bounds
        results = [calculate_backoff(1, policy) for _ in range(100)]

        lower_bound = raw  # Positive-only jitter never reduces delay
        upper_bound = raw * (1 + policy.jitter_factor)

        # All results should be within jitter bounds (max_delay is high enough not to interfere)
        assert all(lower_bound <= r <= upper_bound for r in results), \
            f"Some results outside [{lower_bound}, {upper_bound}]"

    def test_jitter_produces_variation(self):
        """Jitter should produce different values (not always the same)."""
        policy = RetryPolicy(base_delay=1.0, jitter_factor=0.5)

        # With +50% jitter, it's statistically unlikely all 50 calls return the same value
        results = [calculate_backoff(1, policy) for _ in range(50)]
        unique_results = set(results)

        assert len(unique_results) > 1, "Jitter not producing variation"


class TestCalculateBackoffCaps:
    """Verify backoff respects min and max bounds."""

    def test_max_delay_cap(self):
        """Backoff never exceeds max_delay."""
        policy = RetryPolicy(base_delay=1.0, max_delay=5.0, jitter_factor=0.0)

        # Attempt 4 would be 8.0, but should be capped at 5.0
        result = calculate_backoff(4, policy)
        assert result == pytest.approx(5.0)

        # Even higher attempts stay capped
        result = calculate_backoff(10, policy)
        assert result == pytest.approx(5.0)

    def test_min_delay_floor(self):
        """Backoff never goes below 0.1 seconds."""
        # Very small base_delay with negative jitter could push below 0.1
        policy = RetryPolicy(base_delay=0.05, jitter_factor=0.9)

        # Run many times; even with worst-case jitter, result should be >= 0.1
        for _ in range(100):
            result = calculate_backoff(1, policy)
            assert result >= 0.1, f"Backoff {result} below minimum 0.1"


class TestCalculateBackoffIntegration:
    """End-to-end backoff behavior with realistic policy."""

    def test_default_policy_sequence(self):
        """Default policy should produce reasonable delays across attempts."""
        policy = POLICY_DEFAULT  # base_delay=1.0, max_delay=8.0, jitter_factor=0.1

        # With ±10% jitter, attempt 1 should be around 1.0s (range: ~0.9-1.1)
        delay_1 = calculate_backoff(1, policy)
        assert 0.8 <= delay_1 <= 1.2

        # Attempt 2 should be around 2.0s (range: ~1.8-2.2)
        delay_2 = calculate_backoff(2, policy)
        assert 1.6 <= delay_2 <= 2.4

        # Attempt 3 should be around 4.0s (range: ~3.6-4.4)
        delay_3 = calculate_backoff(3, policy)
        assert 3.4 <= delay_3 <= 5.0


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