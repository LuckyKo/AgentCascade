# Investigation Report: Is `reacquire_for` in security_handler Valid Production Behavior?

**Date:** 2026-08-24
**Investigator:** reacquire_investigator (researcher)
**Supervisor:** Maine
**Repo:** N:\work\WD\AgentCascade

---

## VERDICT: **VALID** — the reacquire logic is required production behavior.

The old unit test (`tests/test_security_handler_deadlock_fixes.py:904-969`) asserts a **false invariant** introduced by commit `0b1ed67`: *"next turn acquires its own slot naturally."* There is **no mechanism** by which a mid-run caller re-acquires a slot "naturally." The E2E tests (`tests/e2e_security_slot_deadlock.py`) encode the correct behavior. The two failing old unit tests must be updated, not the production code.

Confidence: **High** — verified via code-path tracing, git archaeology, and empirical test runs.

---

## 1. Production Flow Analysis

### Call chain for `_execute_check`
- WebSocket message `ask_security` → `SecurityAdvisorHandler.run_check()` (`agent_cascade/security_handler.py:246`)
- `run_check` spawns a **daemon worker thread** → `_run_check_worker` (:324) → `_execute_check` (:340, defined :365)
- The **caller** (e.g., `screen_capture_fix` calling `edit_file`) is simultaneously **blocked inside its own `engine.run()`** in `OperationManager.request_user_approval` (`operation_manager/approval.py:123-134`, polling `approval.event.wait(timeout=0.1)`).

### Why the caller cannot self-heal
Slot acquisition sites in the entire codebase (verified by grep):
| Site | File:Line | When |
|---|---|---|
| `engine._acquire_slot_with_logging` | `engine/core.py:170-181` | **Once** at `engine.run()` entry ("initial", :458); also on SLEEPING wakeups (:2340, :2409) |
| `reacquire_for` | `engine/core.py:2150` | Explicit helper for yield patterns |
| `pool._acquire_slot` | `pool/slots.py:10-38` | Wraps `scheduler.acquire`, called only from the above |

There is **NO per-LLM-call or per-turn slot acquisition**. Slots span the entire `run()` lifetime (by design: `scheduler.py` "agents are strictly serialized — one at a time, from task submission to full completion"). The approval blocks *inside* `run()`'s tool-execution path, so `run()`'s exit-finally release has not happened yet.

Consequence of releasing the caller's permit with no reacquire (the 0b1ed67 state):
1. `caller._slot_release = None` for the rest of the run.
2. Any subsequent `call_agent` from that caller routes **ASYNC even on the same shared sequential pool** — `tool_dispatcher.py:317-321` reads `_slot_release is None` → `caller_holds_slot=False`. On a capacity-1 pool this permits concurrent LLM use / collisions — exactly the class of bug the sync/async router exists to prevent.
3. Sleep-transition KV save paths (`core.py:2244-2258`) expect `_slot_release`; a slotless mid-run agent skips save-before-release hygiene.
4. Nothing re-acquires until `run()` exits — potentially hours later if the caller keeps working.

The comment in 0b1ed67 claimed equivalence with "the sync-child path in tool_dispatcher" — **that comparison is factually wrong**: the sync-child path *does* reacquire in its finally block (`tool_dispatcher.py:544-549`, delegating to `reacquire_for` at :634).

## 2. Established Yield→Reacquire Pattern (security check is NOT unique)

Three sibling flows all release-then-reacquire around a child operation:

| Flow | Release | Reacquire |
|---|---|---|
| Sync child (`call_agent`) | `tool_dispatcher.py:497-501` | `finally` :544-549 → `reacquire_for` |
| Compression-halt suspension | `engine/core.py:1205` (BUG-1 fix, f007b97) | `core.py:1224` `reacquire_for(..., "after_compression_resume")` |
| Security advisor check | `security_handler.py:556-609` | `finally` :755-760 → `reacquire_for` |

`reacquire_for` itself documents this contract (`engine/core.py:2150-2170`): *"Public helper for the Security/Compressor yield/reacquire pattern… this method puts it back"* with graceful degradation (bounded 30s FIFO wait, `[SLOT_REACQUIRE_FAILED]` → async-only fallback, never fatal).

## 3. Timeline / Git Archaeology

| Commit | Time (Aug 16) | Change |
|---|---|---|
| `a1aaea1` | 10:07 | Fixed persistent deadlock: added yield + **reacquire in finally** |
| `0b1ed67` | 12:56 | Removed reacquire ("next turn acquires naturally") + added old unit test asserting NO reacquire |
| `68de47c` | 14:28 | Added E2E tests asserting reacquire IS called (tests pass only WITH reacquire) |
| HEAD (2457406) | — | Committed code still matches 0b1ed67 (no reacquire) |
| Working tree | — | **Uncommitted change** restores the three-path yield + finally-reacquire (`git diff agent_cascade/security_handler.py`: +69/-8) |

Empirical proof the tests conflict with committed code:
- `tests/e2e_security_slot_deadlock.py` vs current tree: **3/3 PASS**
- `tests/test_security_handler_deadlock_fixes.py` vs current tree: **2 FAILED / 28 passed** — precisely the two assertions `assert not engine_instance.reacquire_for.called` (lines 933-936, 968+)
- At commit `68de47c`, the handler had **no reacquire code** (`findstr` on `git show 68de47c:agent_cascade/security_handler.py` finds zero `reacquire_for` calls) yet the E2E test asserted `reacquire_for.called` — i.e., **the E2E tests failed as-committed** and were red until the working-tree fix restored the reacquire. The E2E file is byte-identical to its committed version except an import path rename (api_router → api_router_pkg.scheduler).

## 4. Deadlock Scenario Validation

Production incident documented in `investigation_security_slot_deadlock.md` (2026-08-16): Security check timed out 300s because caller `screen_capture_fix` held the `_shared_sequential_slot_` (capacity 1) and the yield silently failed when `_slot_release` was `None` while the pool still listed the holder (leak path via lifecycle reuse clearing the callback without releasing — later hardened at `lifecycle_manager.py:516-528`). Root causes fixed by the current three-path structure:
- **Path 1** (normal yield, live callback) — primary case.
- **Path 2** (force-release leaked permit) — real, observed failure mode of Path 1 (callback/pool mismatch); guarded so `_yielded_slot=True` only if the holder actually left the pool (:592-593).
- **Path 3** (skip + diagnostics) — legitimate state (no slot held, e.g. unlimited endpoint or caller already idle); logging-only, prevents silent failures.

Residual risk of the reacquire (documented for completeness): if the caller's `run()` exits between yield and finally (e.g., global stop), `reacquire_for` may hand a permit to an exiting instance. Mitigations exist: `_release_slot` idempotency, SLOT_LEAK hardening on instance reuse (`lifecycle_manager.py:516`), and bounded 30s timeout. Risk is smaller than the certain slot-loss without reacquire.

## 5. Consistency Gap Found (side finding)

`compression/agent_invoker.py:217-224` still contains the same 0b1ed67-era comment and **does NOT reacquire** after yielding the caller's slot `"before_compression"` for inline compression/consolidation. By the same analysis, a caller that survives inline compression mid-turn is left slotless (async-routing degradation) until `run()` exits. Note the *suspension* path (forced compression halt) DOES reacquire (`core.py:1224`). Recommend evaluating the same finally-reacquire for `agent_invoker` — separate task, same root rationale.

## 6. Recommendations

1. **Keep the working-tree reacquire implementation**; commit it.
2. **Update the two stale unit tests** (`test_release_slot_called_before_security_runs`, `test_release_slot_noop_when_no_callback` in `tests/test_security_handler_deadlock_fixes.py`) to assert the correct invariant: `_release_slot` called AND `reacquire_for` called with `(caller_inst, caller_agent)` when a slot was yielded; NOT called when nothing was yielded (skip path).
3. Consider back-fitting the finally-reacquire idiom to `agent_invoker.py` (see §5).
4. Optional hardening: in the reacquire finally, skip if `self.agent_pool.stopped` or caller terminated (shrinks the residual-race window).

---
**Evidence files:** investigation_security_slot_deadlock.md · reports/fix_plan_endpoint_slot_deadlock_IMPLEMENTATION.md · reports/deadlock_investigation_security_compression_20260821.md · git commits a1aaea1/0b1ed67/68de47c/f007b97 · test runs (3/3 E2E pass; 2 fail old unit)
