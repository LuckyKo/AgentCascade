# Scheduling Code Audit Report
## Date: 2026-05-21 | Auditor: SchedulerAuditor

---

## Issue A: Closure Capture Bug in `sem_generator_wrapper` (api_router.py, lines 502-507)

**Verdict: SAFE but with latent risk — defensive fix recommended**

### Analysis
The `sem` variable captured by `sem_generator_wrapper` at line 506 refers to whatever value `sem` has when the generator is iterated. Control flow analysis:

1. `execute_with_sem` (line 481) is called inside the `for cfg_idx, llm_cfg in enumerate(chain)` loop (line 447)
2. If it returns a generator, that generator is immediately processed at lines 537-561 — specifically `next(it)` at line 552 triggers the first iteration of `sem_generator_wrapper`
3. If `next(it)` succeeds, `reconstruct(first_chunk, it)` is returned at line 558, which **exits** the retry loop AND the outer chain loop
4. The reconstructed generator now holds a closure over `it`, which holds a closure over `sem_generator_wrapper`, which captures `sem`

### Why Currently Safe
Once `reconstruct(...)` is returned (line 558), control exits the entire `call_with_fallback` method. The `self._semaphores` dict can only be modified by new invocations of `call_with_fallback` on other threads, and those use `self._sem_lock` to protect mutations.

### Latent Risk
If the endpoint is resized between the time `next(it)` succeeds and the reconstructed generator is fully consumed, `sem` could refer to a stale semaphore. The old semaphore object would still exist in memory — `sem.release()` on it would work but release the wrong semaphore.

### Recommended Fix
```python
# Line 502-507, change from:
def sem_generator_wrapper(gen):
    try:
        yield from gen
    finally:
        sem.release()

# To:
def sem_generator_wrapper(gen, _sem=sem):
    try:
        yield from gen
    finally:
        _sem.release()
```
This freezes `sem` at definition time via default argument capture. Zero runtime cost.

---

## Issue B: Semaphore Leak on Generator Failure During Retry (api_router.py, lines 521-573)

**Verdict: SAFE — No leak confirmed by testing**

### Analysis
Concern: When `next(it)` at line 552 raises (e.g., ConnectionError), the exception propagates to the outer `except Exception as e:` at line 565, which sleeps and retries. The semaphore was acquired inside `execute_with_sem` but would it be released?

### Why It's Safe
- `next(it)` enters `sem_generator_wrapper`, which does `yield from gen`
- When the underlying generator raises, Python's `yield from` semantics (PEP 380) guarantee that the enclosing `finally` block executes before the exception propagates
- The `finally` at line 505-506 calls `sem.release()`

### Empirical Verification
Tested with simulated generators that raise on first iteration — semaphore was properly released on every retry attempt, with no leak.

---

## Issue C: Double-Semaphore Interaction for concurrency=0 (api_router.py + agent_orchestrator.py)

**Verdict: SAFE — No deadlock possible**

### Analysis
For a concurrency=0 endpoint:
1. **Layer 1 (EndpointScheduler)**: `submit_task` calls `router.scheduler.acquire(api_base, 0)` → acquires S1, holds it for the agent's entire lifecycle
2. **Layer 2 (per-call semaphore)**: Each LLM call inside `call_with_fallback` acquires/releases S2

### Why It's Safe
Could S2 ever block an agent that already holds S1? Only if another thread holds S2. But S2 is per-endpoint (`self._semaphores[endpoint_base]`), and only one agent at a time can use a concurrency=0 endpoint (enforced by S1). Therefore:
- While Agent A holds S1, no other agent can make LLM calls to the same endpoint
- S2 can only be held by Agent A's own sequential LLM calls
- Since Agent A's LLM calls are sequential within a single thread, S2 never blocks

### Note
The double-semaphore is redundant for concurrency=0 (as acknowledged in the comment at lines 467-469), but it's harmless. It becomes meaningful only for concurrency=N>0 endpoints where multiple agents share an endpoint and each agent could make parallel LLM calls.

---

## Summary

| Issue | Severity | Recommendation |
|-------|----------|----------------|
| A | Low (latent risk) | **APPLIED**: `def sem_generator_wrapper(gen, _sem=sem):` at line 502 |
| B | None | No change needed — code is correct |
| C | None | No change needed — double semaphore is safe |

### Additional Findings & Actions Taken
- **Dead code removed**: `generator_wrapper` function (previously lines 543-549 of api_router.py) was defined but never called — copy-paste artifact from refactoring. Removed by the auditor.