# Router circuit-breaker concurrency fix plan (PLAN ONLY — not implemented)

Deferred follow-up to the router-cascade fix (commits 8f4103c + b7e4bbd). The opt-in stress
suite `tests/test_router_cascade_breaker_stress.py` deterministically exposes **two** distinct
bugs in the per-server circuit breaker. This plan scopes a minimal, safe fix for both, with the
design tradeoffs made explicit for review. **No code has been changed.**

Baseline (verified 2026-08-23, after reverting the stopgap edit):
- `tests/test_router_cascade_breaker.py` → **32 passed** (green).
- `tests/test_router_cascade_breaker_stress.py` → **2 failed** (expected; they catch the bugs):
  - Test 1: `expected exactly ONE probe, got 2`.
  - Test 2: `busy base was hammered 100 times (expected bounded)`.

All line refs are to `agent_cascade/api_router_pkg/router.py` unless noted.

---

## Bug 1 — single-probe race in `_breaker_should_skip`

### Root cause (line refs)
`_breaker_should_skip` (L771-810). When the breaker is `open` and the window has elapsed, the
first caller transitions it to `half_open` and returns `False` ("go ahead") **without claiming
the probe** (L790-796):

```python
if state == 'open':
    if time.monotonic() - br['opened_at'] >= br['window']:
        br['state'] = 'half_open'          # L791 — transition ...
        return False                        # L796 — ... but probing stays False!
```

A second caller that then acquires `_lock` sees `state == 'half_open'`, `probing == False`, sets
it, and **also** returns `False` (L802-810). Result: 2+ callers both fire HTTP in one half-open
window. The 32-thread barrier in stress Test 1 makes this deterministic (`got 2`).

### Why Option A is the correct production design
Verified against production: `call_with_fallback` calls `_breaker_should_skip(endpoint_base)` at L1011
and, if it returns `False`, immediately reads `_holds_probe = self._caller_holds_probe(...)` at L1020,
then releases in the `finally` at L1207-1209. **Production never calls `_breaker_claim_probe`.** It relies
on `_breaker_should_skip` returning `False` AND leaving the *current thread* as probe holder, so
`_caller_holds_probe` (L829-834: checks `probing and probe_owner == get_ident()`) is `True` and the
finally releases it. Therefore the open→half_open transition **MUST** set BOTH `probing=True` AND
`probe_owner=get_ident()`; otherwise `_caller_holds_probe` is `False`, the finally never releases, and
the half-open slot **wedges forever**. The two-step protocol in the 5 regressing tests (`should_skip`
then a separate `claim_probe`) does not match production — `_breaker_claim_probe` is test-only.

### Proposed change (exact)
In `_breaker_should_skip`, open→half_open branch, claim the probe inline in the same critical
section (we already hold `self._lock`, a non-reentrant Lock, so we cannot call `_breaker_claim_probe`
— it would re-acquire the same lock and self-deadlock). Set both fields:

```python
if state == 'open':
    if time.monotonic() - br['opened_at'] >= br['window']:
        br['state'] = 'half_open'
        # Claim THE single probe inline in this SAME critical section so no other caller can
        # also fire while we are the designated prober. (self._lock is non-reentrant, so we
        # cannot call _breaker_claim_probe here.) BOTH fields must be set: _caller_holds_probe
        # (used by call_with_fallback's finally to release) checks probe_owner == get_ident().
        br['probing'] = True
        br['probe_owner'] = threading.get_ident()
        logger.info(f"[APIRouter] Server breaker {key}: open -> half_open (claiming single probe)")
        return False  # this caller won the single-probe claim — proceed
    return True  # still inside the open window
```

The existing `half_open` branch (L798-810) already claims correctly and is unchanged.

### Existing tests that MUST be updated (5), and how
All five use the wrong pattern: `_breaker_should_skip(base)` to reach half_open, then assert a *fresh*
`_breaker_claim_probe(base)` returns `True`. After the fix the transition caller already holds the probe,
so a fresh claim returns `False`. Rewrite each to the **real single-step protocol**: after a winning
`_breaker_should_skip`, assert `_caller_holds_probe(base) is True` (not that a new claim succeeds).

1. **`test_probe_success_closes`** (~L205-213): `assert not should_skip(base)` →
   `assert _caller_holds_probe(base)` → `_breaker_on_success(base)` →
   `assert normalize_api_base(base) not in router._server_breakers`. Drop the standalone claim.

2. **`test_probe_failure_grows_window`** (~L215-226): `assert not should_skip(base)` →
   `assert _caller_holds_probe(base)` → `_record_server_busy(base, _busy_error())` (probe's own 503) →
   assert `br['state']=='open'` and window grew to `base*2`. **Caveat:** also touched by Bug 2; under the
   recommended policy its *growth* assertion still passes unchanged (see Bug 2 §test impact).

3. **`test_probe_guard_released_on_failure`** (~L228-239, verified against current body):
   - Current body (L233-238): `assert not should_skip(base)` → `assert _breaker_claim_probe(base)` →
     `_breaker_release_probe(base)` → `assert _breaker_claim_probe(base)` (claim again) → release.
   - After: `assert not should_skip(base)` → `assert _caller_holds_probe(base)` →
     `_breaker_release_probe(base)` → `assert not _caller_holds_probe(base)` (slot freed). The "another
     caller can claim again" idea is now a *second* `_breaker_should_skip(base)`: the first consult left
     state `half_open` and release cleared `probing`, so this second consult re-claims inline → **assert
     it returns `False` AND `_caller_holds_probe(base)` is `True` again** (a fresh caller on an already
     half-open, un-probed base wins the single probe). Then `_breaker_release_probe(base)`.

4. **`test_exactly_one_probe_two_pools`** (~L510-553): worker becomes `if should_skip(base): return  # loser`;
   else winner **holds** the probe → `probes_fired.append(name)`; sleep (simulated HTTP);
   `_breaker_on_success(base)`; `_breaker_release_probe(base)`. Keep `assert len(probes_fired)==1` — now mirrors production.

5. **`test_no_hammering_failover_single_probe_recovery`** (~L639-706): drives the real `call_with_fallback`
   path (already single-step), so it does **not** call `_breaker_claim_probe`. It regressed only because the
   stopgap changed recovery-phase hit accounting. After fix + Bug 2 policy, re-verify `busy_hits <= 4`
   (first phase) and `busy_hits2 == 1`; no structural rewrite expected — **re-run** to confirm; if first-phase
   hit counts shift, adjust the bound with a comment.

### New/updated assertions
- Stress Test 1 (`test_single_probe_under_heavy_concurrency_many_cycles`) already asserts exactly one probe
  per cycle and no hang — the acceptance test for Bug 1; must turn green.
- Each rewritten state-machine test: add `assert router._caller_holds_probe(base)` right after a winning `_breaker_should_skip`.

### Verification (Bug 1)
- `python -m pytest tests/test_router_cascade_breaker.py -o addopts= -q --no-header` → all pass.
- `python -m pytest "tests/test_router_cascade_breaker_stress.py::TestBreakerStress::test_single_probe_under_heavy_concurrency_many_cycles" -o addopts= -q --no-header` → PASS.

---

## Bug 2 — re-trip oscillation (the bigger hammering source)

### Root cause (line refs)
Even with Bug 1 fixed, stress Test 2 still shows **~100 busy-base hits** and the busy base is never
closed: instrumenting showed **100 TRIP / zero CLOSE** on the busy base (healthy base got 24 CLOSE —
failover works, all 24 calls return 'ok', no deadlock). `call_with_fallback`'s handler calls
`_record_server_busy(endpoint_base, e)` on **every** 503 (L1101-1102) → `_breaker_trip` (L760-769),
which early-returns only when `br['state'] == 'open'` (L743-745):

```python
br = self._server_breakers.get(base_key)
if br and br['state'] == 'open':
    return  # already open — keep original opened_at/window
prev_window = br['window'] if br else BREAKER_BASE_WINDOW_SECONDS
window = min(prev_window * BREAKER_WINDOW_GROWTH, BREAKER_MAX_WINDOW_SECONDS) if br else BREAKER_BASE_WINDOW_SECONDS
self._server_breakers[base_key] = {'state': 'open', 'opened_at': now, 'window': window, 'probing': False}
```

When the single probe fires and gets a 503, state is `half_open` (not `open`), so the early-return does
**not** trigger → it re-trips to `open` with a **grown** window. With ~24 threads racing, the breaker
oscillates open→half_open→probe(503)→open(grown)→…, each cycle burning real HTTP. A correct single-probe
guard alone does **not** achieve "bounded HTTP".

### The tension to reconcile
`test_probe_failure_grows_window` (L215-226) asserts a failed probe GROWS the window to `base*2` — an
*intended* feature. The fix must (a) still back off when genuinely down, but (b) NOT let N racing threads
each open a fresh half-open window and fire their own probe.

### Options (concrete, with pros/cons)

**Option A — per-episode probe budget via a generation counter (RECOMMENDED).**
Add `br['probe_gen']` (int, reset to 0 on every real trip from closed/open) and a constant
`BREAKER_MAX_PROBES_PER_EPISODE` (e.g. 2). In `_breaker_should_skip`, when transitioning open→half_open,
only allow the transition if `br['probe_gen'] < BREAKER_MAX_PROBES_PER_EPISODE`; otherwise keep state
`open` and return `True` (skip) even after the window elapses. Increment `probe_gen` on each
open→half_open transition. A failed probe re-trips to open **without** resetting `probe_gen` (it is
the same episode), so at most `BREAKER_MAX_PROBES_PER_EPISODE` probes fire per down-episode; after the
budget is exhausted the base stays open until a *new* episode (a success closes it, or a fresh trip
from closed). Window growth on failed probe is preserved.
- Pros: directly bounds total HTTP per episode (the actual hammering metric); minimal state change;
  preserves intended window-growth feature; deterministic and easy to test.
- Cons: needs one new field + one guard; "episode" boundary must be defined (reset `probe_gen` only
  when tripping from *closed*, not from half_open).

> **⚠️ Liveness hole in the naive version of Option A (corrected below).** The first draft made a
> budget-exhausted breaker (`probe_gen >= BREAKER_MAX_PROBES_PER_EPISODE`) return `True` FOREVER. But the
> ONLY way a `_server_breakers` entry is removed is `_breaker_on_success` (router.py L848, `.pop`), and
> `probe_gen` has no reset path except on a fresh trip-from-closed. A budget-exhausted breaker never probes
> again → can never succeed → can never be popped → **the base is wedged open permanently and NEVER recovers
> even when the server comes back up.** The corrected version below adds a recovery re-arm tied to the window
> cap so HTTP stays bounded per burst but the base can still recover.

**Option B — `_breaker_trip` does not re-open from `half_open` without a full window elapsing.**
Treat `half_open` like `open` in the early-return (if `br['state'] in ('open','half_open')` and within
window, return). A failed probe then just re-arms the open window instead of growing + re-opening.
- Pros: smallest diff (one condition). Cons: does **not** bound *total* probes — still one probe per
  window indefinitely under concurrency. Weaker guarantee than A.

**Option C — cap half-open transitions per open episode.** Count `br['half_open_count']` since the last
real trip; stop transitioning once a cap is hit until a success or fresh trip from closed. Functionally
close to A but less clean (duplicates window-growth's role). Not recommended.

**Recommendation: Option A (with the recovery re-arm below).** Bounds *total* HTTP per burst (the metric
Test 2 asserts), preserves window-growth-on-failed-probe, and — with the re-arm — guarantees eventual
recovery. Small, testable state change.

### Proposed change (Option A, exact — CORRECTED for liveness)
1. New module constant near the other breaker constants: `BREAKER_MAX_PROBES_PER_EPISODE = 2`
   (tunable; 2 = initial trip + one recovery probe).
2. `_breaker_trip` (L749-754): when creating a fresh breaker **from closed** (`br is None`), set
   `'probe_gen': 0`. When re-tripping from `half_open` (failed probe), **preserve** the existing
   `probe_gen` (do not reset) so the episode budget carries over:
   ```python
   self._server_breakers[base_key] = {
       'state': 'open', 'opened_at': now, 'window': window, 'probing': False,
       'probe_gen': 0 if br is None else br.get('probe_gen', 0),
   }
   ```
3. `_breaker_should_skip` open branch (L790): transition to half_open only while budget remains; when
   the budget is exhausted, **re-arm ONE recovery probe once the window has fully backed off to its
   cap** (`window >= BREAKER_MAX_WINDOW_SECONDS`) and the full capped window has elapsed. This keeps
   HTTP bounded during the burst but guarantees liveness (a successful probe pops the entry → new
   episode; a failed one re-trips with `probe_gen` preserved, so it waits another full capped window):
   ```python
   if state == 'open':
       if time.monotonic() - br['opened_at'] >= br['window']:
           if br.get('probe_gen', 0) >= BREAKER_MAX_PROBES_PER_EPISODE:
               # Budget exhausted. Only re-arm for a recovery probe once we've fully backed off
               # (window at cap) AND the full capped window has elapsed since opened_at. This keeps
               # HTTP bounded during the burst but guarantees eventual recovery when the server returns.
               if br['window'] >= BREAKER_MAX_WINDOW_SECONDS:
                   # Allow this single recovery attempt; reset the budget so a SUCCESS can close it.
                   # (A failure re-trips with probe_gen preserved — see _breaker_trip — and, still at
                   # cap, will wait another full capped window before the next recovery attempt.)
                   br['probe_gen'] = 0
               else:
                   return True  # still backing off — stay open
           br['state'] = 'half_open'
           br['probe_gen'] = br.get('probe_gen', 0) + 1
           br['probing'] = True            # Bug 1 fix (inline claim)
           br['probe_owner'] = threading.get_ident()
           return False
       return True
   ```
4. `_breaker_on_success` (L852-858) already pops the entry → new episode starts fresh next trip.

**Why bounded AND live:** during a burst the window grows below cap, so at most `BREAKER_MAX_PROBES_PER_EPISODE`
probes fire then the base stays open (no hammering). Once the window reaches `BREAKER_MAX_WINDOW_SECONDS`, exactly
ONE recovery probe is permitted per full capped window (O(1) HTTP/capped window); a success closes it. No permanent wedge.

### Test impact (Bug 2)
- `test_probe_failure_grows_window`: still passes — a single failed probe is within budget (gen 0→1), so it
  re-trips to open with grown window exactly as asserted. No change needed.
- Stress Test 2: with budget=2, busy-base hits per burst ≈ (initial trip attempts ≤3) + (≤2 probes × their own
  503) → comfortably under `busy_hits <= 20` and far below the naive 72+. Should turn green.
- `test_no_hammering_failover_single_probe_recovery`: re-run; first-phase `busy_hits <= 4` and recovery
  `busy_hits2 == 1` should still hold (single probe within budget).
- **NEW unit test — `test_budget_exhausted_recovers_after_window`** (add to `TestBreakerStateMachine`,
  proves the liveness fix; no HTTP server needed):
    ```python
    def test_budget_exhausted_recovers_after_window(self, router):
        import agent_cascade.api_router_pkg.router as router_mod
        base = 'http://127.0.0.1:1234/v1'
        key = normalize_api_base(base)
        # Trip once; then exhaust the per-episode budget with 2 failed probes (gen 0->1->2).
        router._record_server_busy(base, _busy_error())
        for _ in range(BREAKER_MAX_PROBES_PER_EPISODE):
            with router._lock:
                br = router._server_breakers[key]
                br['opened_at'] -= (br['window'] + 1)   # force window elapsed
            assert not router._breaker_should_skip(base)   # probe allowed (budget remaining), holds probe
            router._record_server_busy(base, _busy_error())  # probe FAILED -> re-trip to open (gen preserved)
        # Budget now exhausted. While the window is still BELOW cap, the base must stay open.
        with router._lock:
            br = router._server_breakers[key]
            assert br['window'] < BREAKER_MAX_WINDOW_SECONDS  # only grew x2, below the (large) cap
            br['opened_at'] -= (br['window'] + 1)             # force window elapsed
        assert router._breaker_should_skip(base), "budget-exhausted & window<cap must stay open"
        # Now grow the window to its cap and force a full capped-window elapse -> ONE recovery probe allowed.
        with router._lock:
            br = router._server_breakers[key]
            br['window'] = BREAKER_MAX_WINDOW_SECONDS
            br['opened_at'] -= (br['window'] + 1)
        assert not router._breaker_should_skip(base), "at cap + elapsed, a recovery probe must be allowed"
        assert router._caller_holds_probe(base)
        # A successful recovery probe closes the breaker (pops the entry) -> liveness preserved.
        router._breaker_on_success(base)
        assert key not in router._server_breakers
    ```
  - Notes: `BREAKER_MAX_WINDOW_SECONDS` is large by default, so a single x2 growth stays below cap and the
    "still backing off" branch is exercised for real; the test forces the window to cap directly (no sleep).

### Verification (Bug 2)
- `python -m pytest "tests/test_router_cascade_breaker.py::TestBreakerStateMachine::test_budget_exhausted_recovers_after_window" -o addopts= -q --no-header` → PASS (liveness).
- `python -m pytest "tests/test_router_cascade_breaker_stress.py::TestBreakerStress::test_bounded_http_no_deadlock_under_concurrency" -o addopts= -q --no-header` → PASS.
- `python -m pytest tests/test_router_cascade_breaker.py -o addopts= -q --no-header` → all pass (esp. `test_probe_failure_grows_window`, `test_recovery_after_window`, new liveness test).

---

## Combined verification checklist
1. `python -m pytest tests/test_router_cascade_breaker.py -o addopts= -q --no-header` → **all pass**.
2. `python -m pytest tests/test_router_cascade_breaker_stress.py -o addopts= -q --no-header` → **both PASS** (Test 1 single-probe; Test 2 bounded HTTP + failover + no deadlock).
3. `python -m pytest tests/test_router_cascade_breaker_stress.py -o addopts= -q --no-header -m "not stress"` → **deselected 2** (still excluded from default run).
4. Default run sanity: `python -m pytest -q` (or the suite's normal invocation) must remain green — stress tests are opt-in only.

## OUT OF SCOPE
- Do NOT touch FIFO/slot/re-acquire logic in `scheduler.py` or the slot-pool sharing code.
- Do NOT change the streaming path (`execute_api_call` generator handling, first-chunk pull).
- Do NOT change rate-limiting, context-window detection, or termination handling in `call_with_fallback`.
- Do NOT alter `breaker_gate.py` (bypass-path gate) — it is non-mutating and unaffected.
- Do NOT change the public breaker API names; `_breaker_claim_probe` stays as a test-only convenience.
- No architectural rewrite of the circuit breaker; only the two targeted state-machine fixes above.
