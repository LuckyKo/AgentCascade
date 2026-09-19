---
name: deterministic-delay-recording-tests
description: Make wall-clock timing tests (retry backoff, cooldowns, rate-limit waits) deterministic by patching the injected sleep to RECORD requested delay values and asserting on those instead of measured elapsed time. Use when a test asserts `elapsed >= X` to prove a wait/backoff happened and is flaky under parallel/xdist load.
triggers:
  - "flaky timing test"
  - "assert elapsed backoff"
  - "wall clock test flaky xdist"
  - "deterministic retry backoff test"
  - "patch sleep to record delay"
---

# Deterministic delay-recording tests

## Problem
A test that asserts on REAL elapsed time to prove a wait/backoff happened is fundamentally
flaky under load: a GIL-starved worker thread (pytest-xdist parallel, busy CI) can be
descheduled so `time.time()`-measured elapsed collapses (e.g. measured 0.001s when >=0.05s
expected). The wait may have been REQUESTED correctly — the measurement is just unreliable.

## Fix: assert on the injected delay VALUE, not wall time
Patch the sleep function the code calls so it RECORDS the requested duration and returns
immediately (no actual sleep). Then assert on the recorded values. This is deterministic
(the value comes from the backoff formula, not the scheduler) AND makes the test fast
(no sleeping).

### Step 1 — Find the EXACT patch point
The sleep is usually called as a module-global inside the caller. Patch it in the CALLER's
namespace, not the defining module:
```python
import agent_cascade.api_router_pkg.router as router_mod   # caller
monkeypatch.setattr(router_mod, '_interruptible_sleep', fake)  # NOT helpers._interruptible_sleep
```
Grep for `def _interruptible_sleep` to find the signature (e.g. `(duration, pool, instance_name, interval=0.5)`);
your fake must match it.

### Step 2 — Reusable fixture
```python
@pytest.fixture
def record_delays(monkeypatch):
    delays = []
    def fake(duration, pool, instance_name, interval=0.5):
        delays.append(duration)          # record only — no sleep
    monkeypatch.setattr(router_mod, '_interruptible_sleep', fake)
    return delays
```
`monkeypatch` auto-reverts on teardown (no manual restore).

### Step 3 — Derive expected values from the POLICY, not hardcode
Read the formula. Example `calculate_backoff(attempt, policy) = min(max(base*2^(attempt-1)+jitter, 0.1), max_delay)`
with jitter = `random.uniform(0, jitter_factor)*raw` (ADDED, never subtracted). So attempt n's delay is in
`[base*2^(n-1), base*2^(n-1)*(1+jitter_factor)]`, capped at `max_delay`. Reference the live policy
(`router.policy.base_delay` / `.max_delay`) rather than hardcoding, so the test stays correct if the policy changes.

## Two traps (the reasons naive versions fail)
1. **Other call sites.** The same sleep may be called from rate-limit/cooldown/breaker paths too.
   Confirm those are guarded OFF in your scenario (e.g. `if rate_limit_rpm > 0`, "all endpoints on busy
   breakers") so ONLY the retry path records delays — otherwise scope assertions to AT LEAST the expected backoffs.
2. **Per-endpoint counter reset.** If the code retries per-endpoint with a fresh counter each endpoint,
   a flat delay list across endpoints is NOT globally non-decreasing (a lower-retry fallback appends a small
   delay after a higher-retry endpoint's larger sequence). Do NOT assert whole-list monotonicity. Instead assert:
   - count >= expected-per-primary-endpoint,
   - every value <= max_delay (cap respected),
   - max(values) >= base*2^(max_retries-1) (exponential growth reached the Nth retry; jitter only adds so this is deterministic).

## Verify
Run serially first (`pytest <file> -n 0`), then parallel (`-n auto`) a few times — the whole point is
stability under load. Confirm the converted tests now run in milliseconds (no sleeping). Keep any existing
call-count / `pytest.raises` assertions; only replace the wall-clock asserts.
