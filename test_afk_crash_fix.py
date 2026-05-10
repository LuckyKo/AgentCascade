"""
Regression test for the AFK streaming crash (exception chain corruption bug).

The bug was in _raise_or_delay() which did bare `raise e` inside an active
exception handler context, causing Python to set __context__ = self,
creating a circular reference that crashed the process during generator cleanup.

Fix: Changed all raises to use `from None` to break implicit exception chaining.
"""
import sys
sys.path.insert(0, r'N:\work\WD\AgentCascade')

from agent_cascade.llm.base import (retry_model_service_iterator, retry_model_service, 
                                 _raise_or_delay, ModelServiceError)


def check_clean_chain(exc):
    """Check that an exception has no circular reference in its chain."""
    seen_ids = set()
    ctx = exc
    while ctx is not None:
        if id(ctx) in seen_ids:
            return False, f"Circular reference detected at depth {len(seen_ids)}!"
        seen_ids.add(id(ctx))
        ctx = getattr(ctx, '__context__', None)
    
    if exc.__context__ is None or type(exc.__context__) == ValueError:
        return True, "Clean chain"
    elif len(seen_ids) <= 3:
        return True, f"Acceptable chain (depth={len(seen_ids)})"
    else:
        return False, f"Chain too deep (depth={len(seen_ids)})"


def test_raise_or_delay_max_retries_zero():
    """Test the exact AFK auto-reply crash scenario."""
    orig_err = ValueError("openai.APIError simulation")
    wrapped = ModelServiceError(exception=orig_err)
    try:
        _raise_or_delay(wrapped, num_retries=0, delay=1.0, max_retries=0)
    except ModelServiceError as e:
        ok, msg = check_clean_chain(e)
        assert ok, f"Test 1 failed: {msg}"


def test_raise_or_delay_code_400():
    """Test bad request path."""
    err = ModelServiceError(code='400', message="Bad Request")
    try:
        _raise_or_delay(err, num_retries=0, delay=1.0, max_retries=5)
    except ModelServiceError as e:
        ok, msg = check_clean_chain(e)
        assert ok, f"Test 2 failed: {msg}"


def test_retry_iterator_instant_fail():
    """Test streaming path with immediate failure."""
    def failing_gen():
        raise ModelServiceError(exception=ValueError("streaming error"))
    try:
        gen = retry_model_service_iterator(failing_gen, max_retries=0)
        next(gen)
    except ModelServiceError as e:
        ok, msg = check_clean_chain(e)
        assert ok, f"Test 3 failed: {msg}"


def test_retry_non_streaming():
    """Test non-streaming path."""
    def failing_fn():
        raise ModelServiceError(exception=ValueError("sync error"))
    try:
        retry_model_service(failing_fn, max_retries=0)
    except ModelServiceError as e:
        ok, msg = check_clean_chain(e)
        assert ok, f"Test 4 failed: {msg}"


def test_retry_exhaustion():
    """Test retry exhaustion."""
    call_count = [0]
    def flaky_gen():
        call_count[0] += 1
        raise ModelServiceError(exception=ValueError(f"error #{call_count[0]}"))
    try:
        gen = retry_model_service_iterator(flaky_gen, max_retries=2)
        for item in gen:
            pass
    except ModelServiceError as e:
        ok, msg = check_clean_chain(e)
        assert ok, f"Test 5 failed: {msg}"


def test_retry_with_data_inspection_failed():
    """Test DataInspectionFailed code path."""
    err = ModelServiceError(code='DataInspectionFailed', message="Content blocked")
    try:
        _raise_or_delay(err, num_retries=0, delay=1.0, max_retries=5)
    except ModelServiceError as e:
        ok, msg = check_clean_chain(e)
        assert ok, f"Test 6 failed: {msg}"


def test_retry_with_inappropriate_content():
    """Test inappropriate content message path."""
    err = ModelServiceError(exception=ValueError("inappropriate content detected"))
    try:
        _raise_or_delay(err, num_retries=0, delay=1.0, max_retries=5)
    except ModelServiceError as e:
        ok, msg = check_clean_chain(e)
        assert ok, f"Test 7 failed: {msg}"


def test_retry_with_max_context_length():
    """Test maximum context length message path."""
    err = ModelServiceError(exception=ValueError("exceeds maximum context length"))
    try:
        _raise_or_delay(err, num_retries=0, delay=1.0, max_retries=5)
    except ModelServiceError as e:
        ok, msg = check_clean_chain(e)
        assert ok, f"Test 8 failed: {msg}"


def main():
    tests = [
        test_raise_or_delay_max_retries_zero,
        test_raise_or_delay_code_400,
        test_retry_iterator_instant_fail,
        test_retry_non_streaming,
        test_retry_exhaustion,
        test_retry_with_data_inspection_failed,
        test_retry_with_inappropriate_content,
        test_retry_with_max_context_length,
    ]
    
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  [PASS] {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [FAIL] {test.__name__}: {type(e).__name__}: {e}")
            failed += 1
    
    print(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == '__main__':
    import sys as _sys
    success = main()
    if not success:
        _sys.exit(1)
