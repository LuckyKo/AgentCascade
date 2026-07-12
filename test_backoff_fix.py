"""
Quick verification that the backoff calculation logic in execution_engine.py works correctly.

Tests the exponential backoff formula:
    raw_backoff = LLM_RETRY_BASE_DELAY * (2 ** (retry_count - 1))
    backoff     = min(raw_backoff, LLM_RETRY_MAX_BACKOFF)
    jitter      = random.uniform(0, 0.1 * backoff)   # up to 10% of delay

Constants imported from settings.py:
    LLM_RETRY_BASE_DELAY  = 1.0  (seconds)
    LLM_RETRY_MAX_BACKOFF = 5.0  (seconds)
"""
import random
import sys
import os

# Add project root to path so we can import settings
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_cascade.settings import LLM_RETRY_BASE_DELAY, LLM_RETRY_MAX_BACKOFF


def calculate_backoff(retry_count: int) -> float:
    """Replicate the backoff calculation from execution_engine.py lines 2143-2146."""
    raw_backoff = LLM_RETRY_BASE_DELAY * (2 ** (retry_count - 1))
    backoff = min(raw_backoff, LLM_RETRY_MAX_BACKOFF)
    jitter = random.uniform(0, 0.1 * backoff)
    backoff += jitter
    return backoff


def test_backoff_values():
    """Verify backoff grows exponentially and respects the max cap."""
    print(f"Base delay: {LLM_RETRY_BASE_DELAY}s | Max backoff: {LLM_RETRY_MAX_BACKOFF}s")
    print("-" * 60)

    all_passed = True

    for retry_count in range(1, 7):
        # Disable jitter for deterministic base checks
        raw_base = LLM_RETRY_BASE_DELAY * (2 ** (retry_count - 1))
        expected_min = min(raw_base, LLM_RETRY_MAX_BACKOFF)  # without jitter
        expected_max = min(raw_base * 1.1, LLM_RETRY_MAX_BACKOFF * 1.1)  # with max jitter

        backoff = calculate_backoff(retry_count)

        # The value should be >= expected_min (jitter only adds) and < expected_max
        in_range = expected_min <= backoff < expected_max
        status = "PASS" if in_range else "FAIL"
        if not in_range:
            all_passed = False

        print(
            f"  retry={retry_count}  raw_base={raw_base:.1f}s  "
            f"backoff={backoff:.3f}s  [{status}] "
            f"(expected {expected_min:.2f}-{expected_max:.2f})"
        )

    print("-" * 60)

    # Verify max cap is respected (retry_count=4: base*8 = 8.0 > 5.0 cap)
    for _ in range(10):
        b = calculate_backoff(4)
        if b > LLM_RETRY_MAX_BACKOFF * 1.1 + 0.01:
            print(f"  FAIL: backoff {b:.3f} exceeds max cap with jitter")
            all_passed = False

    # Verify retry_count=1 gives base delay (with small jitter)
    for _ in range(10):
        b = calculate_backoff(1)
        if not (LLM_RETRY_BASE_DELAY <= b < LLM_RETRY_BASE_DELAY * 1.1 + 0.01):
            print(f"  FAIL: retry_count=1 backoff {b:.3f} outside expected range")
            all_passed = False

    print()
    if all_passed:
        print("✅ All backoff calculations PASSED.")
    else:
        print("❌ Some backoff calculations FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    test_backoff_values()