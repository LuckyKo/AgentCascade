# Investigation Report — Root Agent Drops Out of SLEEPING During Long Async Calls

**Investigator:** researcher (sleep_bug_investigator)
**Date:** 2026-08-05
**Mode:** Investigative
**Confidence:** High (code + runtime log evidence)

---

## Executive Summary

The reported symptom — "root agent (Maine) wakes from SLEEPING prematurely
during a long async call, possibly triggered by compression in a child agent" —
is **NOT a premature wakeup and NOT caused by child compression**.

Log evidence (2026-08-04 10:06–10:26) shows Maine entered SLEEPING at 10:06:31
waiting for an async researcher child, **never received a wakeup signal**, and
at 10:11:31 (exactly 300.0s = the `AGENT_SLEEPING_TIMEOUT` default) was
transitioned `COMPLETING → IDLE` by the **SLEEPING TIMEOUT handler**. The
child's compression at 10:19:26 happened ~8 minutes **after** Maine was already
IDLE, so it cannot have caused the transition. The child's result later arrived
as a queued USER message and Maine resumed normally.

There is no code path by which a child's `compress_context` can signal the
parent: the Compressor runs synchronously on the caller's thread, never writes
to the parent's `AsyncResultBuffer` (the only SLEEPING wakeup signal), and never
touches `_run_generation`/`_config_version` in a way that alters state.

**Root cause of the observed behavior: the 300-second SLEEPING timeout is
shorter than the child's total runtime** (16m46s in the observed case). The
root "drops out of SLEEPING" because the system intentionally gives up waiting
at 300s.

---

## Key Findings

### Finding 1 — Log timeline proves timeout, not premature wakeup (CONFIRMED)

From `logs/console.log.1` (session 2026-08-04, lines 36573–36844):

| Time | Event | Log line |
|---|---|---|
| 10:05:48 | Maine starts generation (gen_id=2) | 36573 |
| 10:06:28.8 | Maine launches `compression_timing_investigator` (researcher) ASYNC | 36586-36587 |
| 10:06:31.4 | **Maine → SLEEPING** ("Pending async tools for Maine. Transitioning to SLEEPING.") | 36603 |
| 10:06:36 → 10:11:29 | `SLEEPING - Maine waiting Ns` every 5s (222s→297.9s) | 36731-36818 |
| 10:11:31.4 | **SLEEPING TIMEOUT — Maine waited 300.0s (timeout=300s)** → `EXIT - Maine COMPLETING→IDLE` | 36819-36820 |
| 10:17:46 | Child researcher spawns Compressor_1 (its own compression) | 36802 |
| 10:19:26 | Compressor_1 done; researcher's history compressed (pool_len=104) | 36809-36813 |
| 10:23:14 | Child researcher completes | 36838 |
| 10:26:37 | Result delivered to Maine as USER message ("well?" + "[Agent ... Completed]") | Maine session log |

**There is NO `RESUMED from SLEEPING` log for Maine in this window.** Maine
left SLEEPING exactly once, via the timeout path (`execution_engine.py:4264`,
current file numbering), 300.0s after entering.

### Finding 2 — The compression path cannot signal the parent (CONFIRMED by code)

Wakeup of a SLEEPING agent requires a **non-empty async result buffer** for that
agent: `_handle_sleeping_state()` at `execution_engine.py:4200-4204` drains
`pool.drain_async_results(inst_name)`; if empty and `has_pending()`, it stays
asleep (`4247-4296`).

The child's `compress_context` flow:
1. `tools/custom/compression_tools.py:56-135` — thin wrapper → unified `compress_context()`.
2. `compression/core.py:314-320` → `invoke_compression_agent(...)`.
3. `compression/agent_invoker.py:276-289` — Compressor runs **synchronously on
   the caller's thread** via `engine.run(comp_instance)` with
   `comp_instance._skip_slot_acquire = True`. **No `register_async_call`, no
   `_async_results.put` for the parent.**
4. Post-compression rebuild (`_rebuild_working_set`, `handler.py:583/720/986`)
   only mutates the **child's own** working lists; `_sync_logger_after_compression`
   (`handler.py:378-437`) rewrites the **child's** JSONL log only.

Search of all `_async_results.put`/`add_async_result` call sites (async_tools.py,
async_shell.py, shell_cmd.py) confirms the **only producers** of wakeup signals
are: async child completion (`async_tools.py:144`), async shell heartbeats/
completions (`async_shell.py:908/946/1002/1044`), and shell_cmd background mode
(`tools/custom/shell_cmd.py:363`). Compression is not among them.

### Finding 3 — Global state touched by compression is benign to SLEEPING (CONFIRMED)

- `pool._config_version` (`agent_pool.py:331, 2041`) — incremented on config
  changes; consumed only by `_setup_turn` cache-rebuild decisions
  (`execution_engine.py:1598-1642`), **never** by the SLEEPING loop.
- `pool._run_generation` — stop detection (`execution_engine.py:1830-1833`),
  incremented **only** on stop/resume (`ws_handlers.py:330`,
  `run_agent_unified.py:94`). Compression never touches it.
- `halt_instance` (`agent_pool.py:2542`) is called in compression only on
  overfeeding safety-net (`handler.py:528`) or recovery failure (`handler.py:121`)
  — neither occurred in the observed run (no `[idle_checker]`/halt logs for
  Maine, no overfeeding warning).

### Finding 4 — SLEEPING state machine locations (current code)

- `execution_engine.py:3955-3982` — `_transition_to_sleeping_if_pending()` (log
  "Pending async tools for %s. Transitioning to SLEEPING."). (was line ~3842 in
  the 08-04 build)
- `execution_engine.py:4110-4159` — `_transition_to_sleeping()` (timestamps,
  slot release).
- `execution_engine.py:4165-4349` — `_handle_sleeping_state()`:
  - 4200-4245: async results → wake (RESUMED).
  - 4247-4296: still pending → wait, log every `sleeping_wakeup_interval` (5s).
  - **4264-4278: `sleeping_duration >= sleeping_timeout` → `COMPLETING` + `BREAK_LOOP`** ← the observed exit.
  - 4298-4349: no pending → stable drain → `COMPLETING`.
- `execution_engine.py:1236-1252` — SLEEPING guard in `run()` loop.
- `agent_pool.py:2393-2412` — `has_pending()` (AsyncToolRegistry +
  AsyncShellTracker).
- `async_tools.py:215-298` — `AsyncResultBuffer` (put/drain/wait_for_next).
- `settings.py:89-92` — `AGENT_SLEEPING_TIMEOUT=300.0`,
  `AGENT_SLEEPING_WAKEUP_INTERVAL=5.0`.
- `config_handlers.py:636-647` — UI handlers for both settings.

### Finding 5 — Nested async chains make 300s easy to exceed (HIGH confidence)

Root → child → (child's own async children / compressor) routinely exceeds 300s.
The observed child ran 16m46s; its own compression took 1m40s. Each level's
SLEEPING timeout applies independently, so a root waiting on a deep chain will
time out long before the leaf finishes. When the root times out it becomes
IDLE/COMPLETING; the eventual result is delivered as a USER message (observed
working at 10:26:37 — the "well?" user prompt plus the result arrived together).

---

## Alternative hypotheses examined and rejected

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Child compression writes to parent's async buffer | Rejected | No `_async_results.put(parent)` anywhere in compression/agent_invoker/handler/core; compressor runs synchronously on caller thread |
| `_config_version` bump re-triggers parent loop | Rejected | Only drives cache rebuild in `_setup_turn`; SLEEPING guard doesn't consult it |
| `_run_generation` bump during compression | Rejected | Only incremented on stop/resume |
| Idle-checker dismissed the sleeping root | Rejected | `IdleManager._is_idle` explicitly returns False for SLEEPING (`agent_pool.py:2948-2950`); no dismiss log for Maine |
| WebSocket/stream pushes woke the parent | Rejected | `stream_publisher` only enqueues WS messages to `_ws_send_queue`; the SLEEPING loop drains only `_async_results` + user queue; no "RESUMED" log |

---

## Confidence

- **Confirmed (code):** SLEEPING wake requires async results; compression path
  produces none for the parent; timeout transition at 4264-4278.
- **Confirmed (logs):** Maine entered SLEEPING 10:06:31, exited via
  "SLEEPING TIMEOUT ... waited 300.0s" at 10:11:31, compression at 10:19:26 was
  after the exit.
- **High (behavior):** the "premature wakeup" symptom == timeout transition to
  IDLE; child result delivered later as USER message.

## Remaining Unknowns

- Whether the *user-visible* complaint is about Maine going IDLE (so the UI
  shows it stopped working) rather than about a state-machine bug. The logs
  show no abnormal wakeup; the only abnormal-looking event is the 300s timeout.
- Whether `sleeping_timeout` was customized in the running session (default 300
  matches exactly, so it was default).

## Recommended Fixes (objective, evidence-based)

1. **Raise or remove the timeout for root/nested waits** — e.g. make
   `AGENT_SLEEPING_TIMEOUT` configurable per depth or default higher (e.g. 1800s),
   or make timeout derive from `sleeping_wakeup_interval * N` so it scales.
2. **Non-destructive timeout** — on timeout, keep the agent SLEEPING but stop
   logging/polling every 5s (switch to a slow poll, e.g. every 30s) instead of
   transitioning to COMPLETING/IDLE. This preserves the wake-on-result flow.
3. **Re-deliver late results to IDLE parents** (already partially works) — when
   an async child completes for a parent that is IDLE, ensure the result is
   queued as a USER message (observed behavior at 10:26:37) and optionally
   broadcast a UI notification so the user knows the result arrived.
4. **Document the 300s semantics** in the UI (sleeping_timeout field already
   exposed via config_handlers.py:636-647).

## Suggested Next Actions

1. Reproduce with a child that sleeps >300s (e.g. `time.sleep` via shell_cmd
   async) and confirm the timeout transition — quickest validation.
2. Decide desired UX: wait indefinitely vs. timeout-with-result-redelivery.
3. Implement fix 1 or 2 above; add a regression test asserting
   `_handle_sleeping_state` does not transition to COMPLETING before
   `sleeping_timeout` unless stopped.

## Files Referenced

- `agent_cascade/execution_engine.py` (L1236-1252, 3955-3982, 4110-4159, 4165-4349)
- `agent_cascade/agent_pool.py` (L2393-2412, 2542-2548, 2948-2950)
- `agent_cascade/async_tools.py` (L50-213 registry, L215-298 buffer)
- `agent_cascade/compression/agent_invoker.py` (L276-289 slot bypass)
- `agent_cascade/compression/handler.py` (L378-437 logger sync)
- `agent_cascade/tools/custom/compression_tools.py` (L56-135)
- `agent_cascade/settings.py` (L89-92)
- `agent_cascade/config_handlers.py` (L636-647)
- Logs: `logs/console.log.1` (L36573-36844),
  `N:\work\WD\AgentWorkspace\logs\orchestrator_Maine_20260804_144254.jsonl`
- Memory saved: `.agent_lessons/sleeping-timeout-not-premature-wakeup.md`