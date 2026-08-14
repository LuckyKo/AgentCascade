# E2E Agent Call Test Analysis — Stress Tests vs Happy-Path Checks

**Date:** 2026-08-11
**File analyzed:** `tests/test_e2e_agent_calls.py` (12 tests, 1026 lines)
**Author:** e2e_test_analyzer (researcher)
**Context:** Requested by Maine — identify useful stress tests vs useless happy-path checks, then redesign for proper black-box stress testing of the agent scheduling layer.

---

## 0. Executive Summary

The current E2E suite **does not exercise the scheduling layer at all**. The `ac_server` fixture registers the mock LLM endpoint with `concurrency_limit=-1` (unlimited → no `SlotPool`, `acquire()` returns `None`, children always take the ASYNC path). All 12 tests therefore run with **zero slot contention**, meaning:

- **None of the 12 tests can catch the `SlotQueueTimeout` bug class** (todo.md#93/#99 family) or the reservation fixes (commits `783a3fd`, `6ee94ce`).
- The tests verify LLM-request plumbing and state transitions — valuable as **integration smoke tests**, but they are **not stress tests** and several assertions are trivially satisfiable (weak/tautological).

**Recommendation:** Fix the fixture to route agents through a `concurrency_limit=0` (sequential) endpoint, then keep ~5 tests as black-box regression stress tests, and remove/rewrite the rest.

---

## 1. Critical Fixture Finding (root cause of uselessness)

In `ac_server` (lines 269-351):

```python
mock_ep = APIEndpoint(
    id="mock-endpoint", name="Mock LLM Server",
    api_base=mock_base_url, model="mock-model",
    concurrency_limit=-1,  # ← UNLIMITED — no slot needed, allows async execution
    enabled=True,
)
pool.api_router.add_endpoint(mock_ep)
```

Consequences, verified against `agent_cascade/api_router.py`:

1. `get_effective_concurrency()` (api_router.py:1070-1112): the mock endpoint IS found by api_base → returns `-1` for **every** agent class (orchestrator, researcher, reviewer, coder — none have own priorities, all inherit default).
2. `EndpointScheduler.acquire()` (api_router.py:372-374): `concurrency_limit == -1` → returns `None` immediately. **No `SlotPool` is created, no FIFO queue, no waiting.**
3. `tool_dispatcher.py:307-309` (Rule 1): child needs no slot → **always ASYNC**. The "sync" calls in the test scripts never actually run synchronously — they're launched async.
4. `execution_engine` slot acquisition is a no-op; `_slot_release` stays `None`.

**Result: every test in this file is effectively testing the async-only path with unlimited parallelism.** The `conc=0` FIFO queue, `SlotQueueTimeout`, reservations, self-exemption, and sync-child inline execution — the entire scheduler — is bypassed.

**Concrete failure example:** the `test_scenario_b_fiforder_conc0_no_deadlock` test *claims* to verify FIFO ordering on a conc=0 pool, but the FIFO assertion `log[i].seq > log[i-1].seq` is trivially true (the mock server assigns `seq` under its own lock at arrival time, and with no contention arrivals are naturally ordered). The test would **still pass with a completely broken FIFO scheduler**, because no contention exists to break.

---

## 2. Per-Test Classification (12 tests)

Legend: **USEFUL** = would catch broken scheduling; **WEAK** = pass trivially / don't verify real behavior; **HAPPY-PATH** = checks plumbing only.

| # | Test | Class | Reasoning |
|---|------|-------|-----------|
| 1 | `test_scenario_a_async_sync_parallel_sleeping_resume` (line 533) | **WEAK (keep, rewritten)** | Scenario itself (parent sleeps while async child pending) is the right idea and catches the "parent completes instead of sleeping" bug class. But with `conc=-1` there's no slot pressure; SLEEPING occurs purely from async tool pending, not from slot release. `verify_agent_entered_state(..., "SLEEPING")` is the one genuinely behavioral assertion. Keep, but rebuild on a conc=0 endpoint. |
| 2 | `test_scenario_b_fiforder_conc0_no_deadlock` (line 620) | **USELESS as written** | Claims FIFO on conc=0, but pool is `-1`. `log[i].seq > log[i-1].seq` is guaranteed by mock-server lock, not by scheduling. The "D's response ordering" check is optional-guarded (`if b_calls_d_idx is not None and d_response_idx is not None`) → weak. Rewrite as a real contention test. |
| 3 | `test_scenario_c_nested_depth_gt2_mixed_sync_async` (line 687) | **WEAK** | `assert len(log) >= 7` (line 737) is the "count" anti-pattern — doesn't verify ordering or which agent did what. Bounds check `len(log) <= 12` is arbitrary. State-history "agent created" checks are satisfied by any run that creates the instances. Only deadlock-freedom is meaningful; keep scenario, replace assertions with ordering verification. |
| 4 | `test_scenario_d_different_endpoint_collision` (line 755) | **USELESS** | Only asserts `len(log) >= 2` and completion. The docstring claims to test collision detection / Rule 4 inheritance, but with one endpoint (conc=-1) there is no collision to detect. Pure happy-path smoke. |
| 5 | `test_negative_async_pending_without_deadlock` (line 789) | **USEFUL (rewritten)** | Core regression scenario: slow async child, parent must not complete-early or deadlock. `assert len(log) >= 3` is weak, but `wait_for_completion` timeout is a real behavioral check (deadlock → test times out → fail). Keep scenario, tighten assertions, add contention. |
| 6 | `test_response_queue_ordering` (line 838) | **USELESS (remove)** | Tests the *mock server itself* (FIFO of response queue). Pure unit-test territory — belongs in a unit test of `ProgrammableMockLLMHandler`, not E2E. |
| 7 | `test_tool_call_format` (line 868) | **USELESS (remove)** | Asserts `"tool_calls" in resp.text` — verifies the mock's SSE serialization, not AC. Unit-test territory. |
| 8 | `test_delay_script` (line 893) | **USELESS (remove)** | Tests mock server delay behavior with wall-clock asserts (`< 0.3s`, `>= 0.4s`). Flaky-prone (timing asserts) and tests the mock, not AC. |
| 9 | `test_request_logging` (line 914) | **USELESS (remove)** | Tests mock server log internals (`log[0].seq == 1`). Pure unit test of the mock fixture. |
| 10 | `test_rapid_async_completion` (line 939) | **WEAK (keep, rewritten)** | Fast-completing async child race — good scenario for the safety drain. But assertion is only `completed == True` (45s timeout); no verification the result was actually consumed, no ordering check. On conc=-1 it never races a slot. |
| 11 | `test_multiple_async_calls_same_parent` (line 967) | **WEAK** | Only asserts completion. With conc=-1, B and C never contend. No assertion that *both* results were injected (Maine's final turn exists in script, but log count isn't checked). |
| 12 | `test_sync_chain_no_async` (line 995) | **USELESS as written** | Docstring says "pure sync chain", but Rule 1 makes all children async (conc=-1). There is NO sync chain being tested. Only completion is asserted. Mislabeled test of a path that isn't exercised. |

### Summary counts
- **Remove outright (mock-server unit tests):** 4 (tests 6-9)
- **Useless/misleading as written (rewrite or drop):** 3 (tests 2, 4, 12)
- **Worth keeping but only after rewrite onto real contention:** 5 (tests 1, 3, 5, 10, 11)

---

## 3. Why Current Tests Can't Catch the SlotQueueTimeout Bug Class

Evidence from `todo.md` lines 101-170 (the actual bug incident, 2026-08-11):

```
Maine (SLEEPING, waiting 1500+s for background tools)
  → grep_safety_reviewer (sync child) → SlotPool.acquire() → wait 300s → SlotQueueTimeout
  → [SLOT_ACQUIRE_FAILED] initial for grep_safety_reviewer
  → Sync child 'grep_safety_reviewer' failed: Slot queue timeout
```

The bug only manifests when **a waiter is enqueued behind a holder/reservation on a real `SlotPool` with capacity 1** and the grant never arrives within `QUEUE_WAIT_TIMEOUT`. Requirements to reproduce:

1. A `SlotPool` exists (requires `concurrency_limit != -1`). ← **currently never happens in E2E**
2. A holder occupies the single permit (or a blocking reservation exists).
3. At least one waiter enqueues and times out.

Current suite violates #1, so the E2E layer is **blind** to this entire failure class. The unit tests in `test_scheduler_integration.py` cover the mechanics (FIFO grant order, reservation blocking, cancel-on-termination) but operate directly on `SlotPool` — they cannot catch integration regressions like the removed reservation calls in `agent_pool._run_child_async` (commit `6ee94ce`) or `_transition_to_sleeping` (commit `783a3fd`) being re-introduced.

---

## 4. Proposed New E2E Stress Test Suite

### 4.1 Fixture changes (prerequisite — REQUIRED)

In `ac_server` fixture:

```python
mock_ep = APIEndpoint(
    id="mock-endpoint", name="Mock LLM Server",
    api_base=mock_base_url, model="mock-model",
    concurrency_limit=0,      # ← was -1: real SlotPool, capacity=1, shared sequential key
    enabled=True,
)
```

This single change makes **every** agent (orchestrator + children) route through the shared sequential `SlotPool`. Then:
- LLM calls from any agent must serialize — a second agent's `engine.run()` blocks in `SlotPool.acquire()`.
- Sync children run inline (Rule 4), async children queue on the pool.
- SLEEPING/re-acquire paths actually release and re-acquire the slot.

**Also needed:** raise the per-test `--timeout=60` in `pytest.ini` for this file only (marker or per-test timeout), since contention tests with mock delays can exceed 60s under xdist load; and consider serializing these tests (`-n0` / a `pytest.mark.serial`) because the shared-sequential pool plus xdist process parallelism can interact with the shared mock server (module-scoped fixture per worker is OK, but keep tests in one module).

### 4.2 New test designs (black-box, observable behavior only)

All tests use: `conc=0` endpoint, mock server request log (with per-request **body summary** containing the system-prompt rewrite `You are <instance_name>.`), `/api/status` (completion), `/api/state` (agent states). No internal `SlotPool` introspection, no `scheduler.reserve` knowledge, no `_slot_release` access.

---

**T1. Concurrent agents strictly serialize on conc=0 (contention + FIFO)**

- Script: Maine's first turn calls **two children** (A and B), both `async_mode=True` (so both spawn; on conc=0 they must queue on the shared slot).
- B's script has a delay (e.g., 1.5s); A's is instant.
- **Assertions (observable):**
  1. `wait_for_completion` succeeds within timeout (no deadlock).
  2. From the mock request log, extract the `You are <name>.` identity of each LLM request. Assert the sequence of *agent identities* contains `Maine → A → B → Maine` (or `Maine → B → A → Maine` — order of *first arrival* is not guaranteed) but critically: **no two different agent identities ever have overlapping wall-clock request windows** — i.e., no interleaving. This is checkable from log timestamps: for every consecutive pair of requests from different agents, `req[i+1].timestamp >= req[i].timestamp` and no agent's request started while another was in-flight (mock `delay` + log timestamps give this).
  3. `len(log)` ≥ 4 (Maine turn, A, B, Maine final).
- **Fails if:** FIFO broken (interleaving), deadlock (timeout), slot never released (A completes but B never starts).

**T2. Slow async child while parent continues (SLEEPING reservation regression)**

- Script: Maine turn 1 calls child A async (delay 2.5s) then child B **sync** (delay 0.2s) — the exact `6ee94ce` scenario (parent must not block its own sync child with a spawn-time reservation).
- **Assertions:**
  1. Completion within timeout.
  2. Request log: `Maine(1) → B (completes quickly) → Maine resumes → A completes → Maine final`. Verify **B's request precedes A's completion** (B must not be starved by A's pending async).
  3. Measure wall-clock: `A_delay ≈ 2.5s` must NOT delay B. If B's request timestamp - Maine's turn-1 timestamp < ~2s while A's total span ≈ 2.5s+, the sync child ran inline while parent slept — correct. If B is blocked ~QUEUE_WAIT_TIMEOUT, that's the `SlotQueueTimeout` regression (B never granted).
- **Fails if:** spawn-reservation re-introduced (B starved), FIFO starvation, wake-from-sleep broken.

**T3. Deadlock: parent waits for async child that needs the pool the parent holds (release-on-sleep required)**

- Script: Maine turn 1 calls A async (delay 1.5s). Maine must enter SLEEPING and **release the slot**; otherwise A (needs the same conc=0 pool) times out → `SlotQueueTimeout` → `[Agent 'A' Failed]` → Maine reports failure but completes (the failure is observable!).
- **Assertions:**
  1. Completion within timeout.
  2. `len(log) >= 3` (Maine, A, Maine-resume).
  3. **Final conversation/state check:** the parent's final turn text is the scripted success text (i.e., the async result was actually delivered) — observable via `/api/state` messages or by asserting the request count/sequence: if A failed, Maine would call the LLM again with the failure injected (extra request) or produce the "failed" text. Script Maine's final text to be a sentinel; assert the sentinel appears in the last request's body summary.
- **Fails if:** parent keeps slot while sleeping (A times out), parent completes early without result (original bug), wake/drain broken.

**T4. Reservation must NOT block unrelated waiter indefinitely (783a3fd regression)**

- Script: Maine turn 1 calls A async with **long delay (3.0s)**. While Maine sleeps (reservation active on pool, if re-introduced), a second **independent session/root agent** (same pool via same mock endpoint) sends a message and must complete *before* Maine's async child finishes.
- **Assertions:**
  1. Both sessions complete within timeout.
  2. Request log shows the independent agent's LLM request(s) **before** A's completion request (A's delay is 3s; independent agent must run during that window). If the SLEEPING reservation blocks the unrelated waiter (the pre-`783a3fd` behavior), the independent agent times out or is delayed ~300s → observable as failure.
- **Fails if:** stale SLEEPING reservation blocks unrelated agents (the exact bug removed in `783a3fd`).

**T5. Deep nesting under contention (order preserved, no starvation)**

- Script: `Maine → A(async) → B(sync) → C(async) → D(sync)` with alternating delays; all on conc=0 pool.
- **Assertions:**
  1. Completion within timeout.
  2. Extract agent identity sequence from log; assert the *dependency-respecting* subsequences appear in order: A before B, B before C, C before D, and each parent's resume after its child completes.
  3. No identity interleaving (same overlap check as T1).
- **Fails if:** nested FIFO violation, deadlock at depth, reservation self-exemption broken (parent blocked by its own reservation — `test_self_reserve_reacquire_succeeds` unit analogue, but E2E).

**T6. Mass contention: N children from one parent (scheduling pressure + cancel-on-termination smoke)**

- Script: Maine turn 1 calls 5 children async (`B1..B5`), each with small distinct delays; Maine waits for all.
- **Assertions:**
  1. Completion within timeout.
  2. All 5 child identities appear in request log (observable) in *some* order, and Maine's final sentinel turn appears last.
  3. No identity overlap (serialized).
- **Fails if:** starvation (later children never granted), FIFO barging, async result injection loss.

---

### 4.3 Test helper additions required

1. **`extract_agent_identity(body_summary)`** — parse `You are <name>.` from the last message content in each request's body summary (the engine rewrites system prompt identity at execution_engine.py:1725-1728). Gives black-box attribution of each LLM request to its agent.
2. **`assert_no_overlap(log, agent_names)`** — verify no two different agents' requests have overlapping `[timestamp, timestamp+delay]` windows (serialization proof).
3. **`assert_subsequence(identity_seq, required_order)`** — dependency ordering checks.
4. **`wait_for_agent_state(...)`** (exists) — keep for SLEEPING verification.
5. **Sentinel-text checks** — script final responses with unique sentinels and assert the sentinel appears in the *last* logged request (proves result consumption, not just "something completed").

---

## 5. Recommendations (prioritized)

1. **Change fixture `concurrency_limit=-1` → `0`** in `tests/test_e2e_agent_calls.py`. This is the single highest-impact change; without it, no E2E test can catch scheduling regressions. (api_router.py:372-374, tool_dispatcher.py:307-313 confirm the current bypass.)
2. **Delete the 4 mock-server unit tests** (6-9) — move `test_response_queue_ordering`, `test_tool_call_format`, `test_delay_script`, `test_request_logging` into a small unit test file for `ProgrammableMockLLMHandler` (they need the fixture, not AC).
3. **Rewrite tests 1, 3, 5, 10, 11** as T1-T6 above with identity-based ordering + serialization assertions.
4. **Drop tests 2, 4, 12 as written** (their claimed scenarios — FIFO, endpoint collision, sync chain — don't run under conc=-1; rebuild equivalents per T1/T3/T5).
5. **Add per-file timeout/marker handling** — with real slot waits, `--timeout=60` in `pytest.ini` may be too tight for T4/T5 under xdist; use a module-level `pytestmark = pytest.mark.timeout(...)` or marker-excluded timeout.
6. **Keep the unit-level coverage** in `test_scheduler_integration.py` as-is — it tests `SlotPool` mechanics directly (FIFO, reservations, cancel). The E2E suite should test the *integration* of those mechanics with real agent execution, which is currently the blind spot.

---

## 6. Confidence & Open Questions

- **Confirmed (high confidence):** fixture uses `conc=-1`; all 12 tests run with no slot contention; 4 tests are pure mock-server unit tests; tests 2/4/12 do not exercise the scenarios they claim.
- **High confidence:** the proposed T1-T6 designs would fail under the pre-fix code (`783a3fd`, `6ee94ce` reverted) — T2 directly reproduces the todo.md#99 `SlotQueueTimeout` traceback shape (parent sleeping, sync child queued behind reservation).
- **Open questions:**
  1. Does the orchestrator's own system prompt reliably contain `You are <session_name>.` in the *request body* sent to the mock (verified for instance rename at execution_engine.py:1725-1728, but the exact serialized form in the mock's `body_summary` should be validated by a quick run before relying on identity extraction)?
  2. Can a second root session be started against the same `ac_server` fixture (needed for T4)? `create_app` uses `session_name` per fixture instance — T4 may need a second WS handshake with a different token against the same server; verify `api_sessions` supports multiple.
  3. Wall-clock overlap assertions (T1/T6) depend on mock `delay` being accurate under load; use generous margins (e.g., 3x expected delay) to avoid flakes.

---

## 7. Handoff Notes

- **Key files:** `tests/test_e2e_agent_calls.py` (analyzed), `agent_cascade/api_router.py` (concurrency resolution), `agent_cascade/tool_dispatcher.py` (sync/async decision), `agent_cascade/slot_queue.py` (SlotPool/FIFO), `todo.md` (bug incident evidence), `.agent_lessons/api-scheduling-architecture.md` (architecture).
- **Suggested next action:** apply fixture change + implement T1-T6 (can be delegated to a coder agent with this report as spec), then run with a reverted `783a3fd`/`6ee94ce` to confirm the tests actually fail (mutation check).
