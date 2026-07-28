"""Centralized retry policy for LLM calls.

Single source of truth for error classification, backoff calculation, and
retry configuration used by both the execution engine (outer loop) and
API router (inner loop).
"""

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """Centralized retry configuration for LLM calls.

    Applied at the execution engine level (outer loop). The API router
    uses per-endpoint settings that are constrained by this policy.

    Attributes:
        retry_max_attempts: Total retry budget across all layers and endpoints.
            Range [1, 6] — total outer attempts.
        base_delay: Initial backoff delay in seconds.
        max_delay: Cap for exponential backoff. With 3 engine attempts and
            exponential growth (1→2→4→8), total retry wait maxes at ~15s.
        jitter_factor: ±percentage randomization to avoid thundering herd.
        endpoint_max_retries: Retries per endpoint before failover to next.
    """

    retry_max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    jitter_factor: float = 0.1
    endpoint_max_retries: int = 1


# ── Predefined policies ───────────────────────────────────────────────────────

POLICY_DEFAULT = RetryPolicy(
    retry_max_attempts=3,
    base_delay=1.0,
    max_delay=8.0,
    endpoint_max_retries=1,
)

POLICY_AGGRESSIVE = RetryPolicy(
    retry_max_attempts=5,
    base_delay=0.5,
    max_delay=4.0,
    endpoint_max_retries=1,
)

POLICY_CONSERVATIVE = RetryPolicy(
    retry_max_attempts=2,
    base_delay=2.0,
    max_delay=10.0,
    endpoint_max_retries=0,  # No per-endpoint retries; failover immediately
)


# ── Error classification (single source of truth) ────────────────────────────

def classify_error(error: Exception) -> str:
    """Classify an error as 'fatal', 'retryable', or 'unknown'.

    This is the single source of truth for retry decisions. Errors are
    classified based on their string representation, matching patterns
    extracted from production LLM API error responses.

    Args:
        error: The exception that occurred.

    Returns:
        'fatal' — do not retry (auth, quota, config errors)
        'retryable' — transient, safe to retry (network, timeout, 5xx)
        'unknown' — default to retryable for safety on uncategorized errors
    """
    error_str = str(error).lower()

    # Explicitly non-retryable patterns (billing, auth, config)
    fatal_patterns = (
        'insufficient_quota', 'billing_error', 'account_not_active',
        'invalid_api_key', 'authentication', 'unauthorized',
        'forbidden', 'permission denied',
        'model_not_found', 'invalid_model',
        'invalid_request', 'validation',
    )

    # Retryable errors (transient)
    retryable_patterns = (
        'connection', 'timeout', 'timed out', 'ssl',
        'broken pipe', 'disconnected', 'eof',
        'reset by peer', 'refused',
        'terminated', 'fetch failed',  # Connection termination patterns from logs
        '503', '502', '504', '429',  # Server errors + rate limiting
        'network unreachable', 'dns', 'resolution failed',  # Network/DNS issues
        'temporary', 'overloaded', 'service unavailable',  # Transient server states
    )

    is_fatal = any(pattern in error_str for pattern in fatal_patterns)
    has_retryable_pattern = any(pattern in error_str for pattern in retryable_patterns)

    if is_fatal:
        return 'fatal'
    elif has_retryable_pattern:
        return 'retryable'
    else:
        # Unknown error — default to retryable for transient issues we
        # haven't categorized yet. Better to retry once than fail permanently.
        return 'unknown'


# ── Backoff calculation (reusable utility) ────────────────────────────────────

def calculate_backoff(attempt: int, policy: RetryPolicy) -> float:
    """Calculate backoff delay with exponential growth and jitter.

    Formula: min(max(base_delay * 2^(attempt-1) + jitter, 0.1), max_delay)

    Args:
        attempt: 1-based attempt number (first retry = 1).
        policy: The RetryPolicy containing backoff parameters.

    Returns:
        Delay in seconds, bounded between 0.1 and policy.max_delay.
    """
    raw = policy.base_delay * (2 ** (attempt - 1))
    jitter = random.uniform(-policy.jitter_factor, policy.jitter_factor) * raw
    return min(max(raw + jitter, 0.1), policy.max_delay)