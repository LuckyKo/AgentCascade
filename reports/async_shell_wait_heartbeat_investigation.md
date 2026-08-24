# Investigation: `__wait` heartbeat / message-queue timing bug (async shell_cmd)

**Date:** 2026-08-24 · **Investigator:** research_wait_bug · **Status:** Root cause CONFIRMED (code + production log evidence)
**Source todo:** todo.md:135 — *"check up whats up with the `__wait` cmd of async shell, seems to not properly wait for a heartbeat in msg que entry before responding the first time (not on any new line as it is now)"*

---

## Executive Summary

`__wait` was designed (todo.md:30, launch-message help text, dna.py:325 prompts) as a **"sleep until next heartbeat"** tool: the agent goes quiet and is woken by the next message-queue event (heartbeat / completion). The implementation does something completely different: it **busy-polls the raw task output buffers every 0.5 s and returns immediately on ANY new output line**, ignoring the message queue entirely.

Consequences, all confirmed in code and reproduced in a production session log:

1. **First response fires instantly** whenever *any* output accumulated since the last consumption point (`launch` early-output or the last `__status`) — it never waits out the heartbeat interval. *(todo's main complaint)*
2. Already-queued heartbeats are invisible to `__wait`; the purpose-built queue primitive `MessageQueueMixin.wait_for_message()` (whose docstring claims it is "Used by __wait tool") has **zero callers** — dead code.
3. Output **duplication**: `__wait` reads `task.stdout_lines` without advancing `task.last_heartbeat_sent_pos`, so the later heartbeat/completion message resends the same lines (proven in `coder_fallback-compression-fix-impl` log).
4. A follow-up `__wait` racing task cleanup returns misleading `"No running shell found"` seconds before the completion message arrives.

---

## 1. Exact code path

### 1.1 Entry — `agent_cascade/tools/custom/shell_cmd.py`

- `ShellCmd.call()` routes control commands → `_handle_control_command()` (`shell_cmd.py:367`).
- `__wait` branch: **`shell_cmd.py:399-480`**. Sequence:
  1. `task = tracker._get_task(...)`; None → `"No running shell found"` (:401-404).
  2. Snapshot `completed`/`return_code` under `task._lock`; if completed → early return (:409-418).
  3. Compute timeout (:420-427): `hb>0 → min(hb_interval, WAIT_CMD_MAX_TIMEOUT=180)` else `WAIT_CMD_DEFAULT_TIMEOUT=30`.
  4. Snapshot buffer lengths (:436-438), then **busy-poll loop** (:440-480):
     - sleep `WAIT_CMD_POLL_INTERVAL = 0.5 s` (settings.py:352);
     - completed → return completion string (:456-465);
     - **`new_stdout or new_stderr` → return immediately with the joined lines** (:467-480). ← *This is the premature-return site.*

### 1.2 Settings — `agent_cascade/settings.py:350-352`
```python
WAIT_CMD_MAX_TIMEOUT: float = 180.0
WAIT_CMD_DEFAULT_TIMEOUT: float = 30.0
WAIT_CMD_POLL_INTERVAL: float = 0.5
```
(Raised to 180 s in commit `81fb73c`; correct and not part of the bug.)

### 1.3 Heartbeat producer — `agent_cascade/async_shell_pkg/tracker.py`
- Tracking thread `_monitor_loop` (:449-490): checks every `HEARTBEAT_CHECK_INTERVAL=0.5 s` (settings.py:344); calls `_send_heartbeat` when `heartbeat_interval` elapsed (:478-490).
- `_send_heartbeat` (:915-989): reads `combined[task.last_heartbeat_sent_pos:]` and **advances `last_heartbeat_sent_pos` under `task._lock`** (:930-942), then `self._enqueue(agent_name, msg)`.
- Completion path `_track_task` (:610-657): builds one merged completion message using `_get_remaining_output_text` (which also advances `last_heartbeat_sent_pos`, :995-1009) and enqueues it **before** task cleanup (:652-657).
- `_enqueue` (:1096-1102) → `pool.enqueue_message(agent_name, text)`.

### 1.4 Message queue — `agent_cascade/pool/message_queue.py`
- `enqueue_message` (:62-67): append + **`self._message_condition.notify_all()`** — comment: *"Wake any `__wait` callers"*. **There are none.**
- `wait_for_message` (:135-175): pops one message or condition-waits (≤1.0 s slices, termination-aware). Docstring: *"Used by `__wait` tool to pause execution until the next async message (e.g., heartbeat or completion)"*. **Grep across the repo: zero call sites. Dead code.**

### 1.5 Consumer side (for contrast)
- Sleeping-agent wakeup: `engine/core.py:2320` drains the queue and injects entries as USER messages via `_drain_and_inject` (:245-338) → `_make_user_message` (:355-357). Heartbeats/completions therefore normally arrive as **separate USER messages**, which is why `__wait` returning raw output collides with them (see §3 RC3).

---

## 2. Production evidence (real session)

`N:\work\WD\AgentWorkspace\logs\coder_fallback-compression-fix-impl_20260823_082157.jsonl`, lines 14-46 (Tool ID 3):

| t | Event |
|---|---|
| 08:34:43 | pytest launched async, Tool ID 3, **heartbeat_interval = 30 s** |
| 08:34:48 | `__status` → "No new output since last status check." (consumption pos advanced by `get_status`, tracker.py:1278-1286) |
| 08:34:51 | `__wait` issued → **response in the SAME second**, `(elapsed 10s)` + conftest/"bringing up nodes…" output. Process still running (completes 08:34:54). **Did not wait for the 30 s heartbeat.** |
| 08:34:54 | Second `__wait` → **"No running shell found."** (task already cleaned up) |
| 08:34:54 | USER message `⟨shell_cmd completed⟩ Tool ID 3 …` **repeats the identical conftest/nodes lines** already returned by `__wait` → duplication (RC3) |

Same pattern repeats for Tool IDs 4 and 5 in the same session. This matches the todo wording precisely: the first `__wait` answers on **any new output line** instead of waiting for a heartbeat message-queue entry.

---

## 3. Root cause analysis

### RC1 — Wrong wakeup predicate (primary)
`shell_cmd.py:474-480`: the loop treats **any new stdout/stderr line** as a reason to return. The documented contract (todo.md:30 *"simply wait for next heartbeat"*; launch help text shell_cmd.py:341 *"wait until next heartbeat"*; dna.py:325 guidance) is *wake on next heartbeat/queue event*. Any chatty process (progress spew, build logs) makes `__wait` degenerate into an instant `__status` — exactly "responding the first time on any new line as it is now".

### RC2 — Message-queue blindness + dead primitive
`__wait` never consults `pool.message_queues`. An already-enqueued heartbeat (e.g., produced during a long LLM turn between tool calls) is neither detected nor awaited; the wait loop polls task state instead. The mechanism built for this — `enqueue_message` notifying `_message_condition` (message_queue.py:66) and `wait_for_message` (message_queue.py:135) — is unused; its docstring falsely claims `__wait` uses it. Stale-docstring drift indicates the design changed (commit `0f467ba` "merge async result buffer into user message queue", commit `7b3cbf4` docs) but the tool-side half never landed.

### RC3 — Position-tracking asymmetry → duplicated output
Heartbeats and completions advance `task.last_heartbeat_sent_pos` (tracker.py:936, :1009); `get_status` does too (tracker.py:1286). **`__wait` reads raw buffers without advancing it** (shell_cmd.py:467-472 only tracks its own local lengths). Whatever `__wait` prints will be printed again by the next heartbeat or the merged completion message. Log evidence: §2, 08:34:51 vs 08:34:54.

### RC4 — Cleanup race (minor)
After completion, `_track_task` removes the task (tracker.py:686-689) slightly before/around delivering the USER completion message; a promptly issued second `__wait` hits `_get_task → None` and reports "No running shell found" (log §2, 08:34:54). Cosmetic but confusing to agents.

### Note on "first time"
"Responding the first time" is deterministic, not racy: the first `__wait` after launch/status sees all lines accumulated since the last consumption point and returns on iteration 1 of the poll loop (<0.5 s), regardless of the configured heartbeat interval.

---

## 4. Existing test coverage — `tests/test_async_shell_cmd.py:178-320`

Covered: no-running-shell (:191), already-completed incl. elapsed (:200), **returns-new-output (:209)**, completion-status (:230), timeout-no-output (:252), 180 s cap boundary (:263, :290), heartbeat-below-cap (:276), sequential no-deadlock (:305), lock discipline (:315+). Plus control-command membership (:526).

**Gaps:**
- `test_wait_returns_new_output` (:209-228) **encodes the buggy semantics** (asserts `'new output line' in result`) — it must be rewritten, not preserved.
- No test for "already-queued heartbeat is consumed/awaited".
- No test that `__wait` output does not duplicate the later heartbeat/completion (would have caught RC3).
- Nothing exercises `wait_for_message` (it is untested dead code).

---

## 5. Fix proposal (investigation only — not implemented)

**Recommended: make `__wait` a true queue-driven sleep.**

1. Add a predicate-aware variant in `MessageQueueMixin`, e.g.
   `wait_for_message(instance_name, timeout, predicate=None)` that only pops messages matching the predicate and leaves others queued (reuse existing condition-wait skeleton at message_queue.py:152-175).
2. Rewrite the `__wait` branch (shell_cmd.py:399-480):
   - Keep timeout math (:420-427) unchanged.
   - Replace the poll loop with:
     `msg = pool.wait_for_message(agent_name, remaining, predicate=lambda m: m.startswith('⟨shell_cmd') and f'Tool ID: {tool_id}' in m)`.
   - If a message returns → return it **verbatim** as the tool result (tracker already advanced `last_heartbeat_sent_pos`, so no duplication anywhere; queued-heartbeat case fixed; condition-variable wake = instant reaction).
   - If None (timeout) → keep current `"No new output (timeout after Xs…)"` string.
   - Delete the raw-buffer output-return path (RC1+RC3 disappear structurally). This also honors todo.md:30's original "a no-reply tool call basically".
   - Guard: if the pool lacks the method (unit-test MagicMock pools), fall back to today's polling loop to stay backward-compatible.
3. RC4 cosmetic: when `_get_task` returns None, soften the message: *"No running shell found (may have just completed — watch for the completion message)."*
4. Update docstring at message_queue.py:138 to name the actual caller once wired.

**Alternative considered:** keep polling but (a) return only on completion/timeout and (b) advance `task.last_heartbeat_sent_pos` under lock before returning output. Smaller diff, but keeps CPU polling, keeps the 0.5 s latency floor, and leaves the queue/`notify_all` machinery unused. Rejected as primary.

### Risks / edge cases
- **Non-shell messages must not be swallowed** into a shell_cmd result → predicate is mandatory (supervisor `send_message`, user messages share the queue). Mismatched-tool_id shell messages (two concurrent async shells, one owner) are left queued; decide whether `__wait(X)` may consume `Y`'s heartbeat (recommend: no — predicate includes tool id).
- **Completion-only tasks (`heartbeat_interval=-1`)**: completion message wakes the wait — works; timeout falls back to `WAIT_CMD_DEFAULT_TIMEOUT=30` as today.
- **Dismissed/terminated instance**: `wait_for_message` returns None on termination (message_queue.py:157-158) — `__wait` should return promptly; verify no hang.
- **Test churn:** ≥3 existing `__wait` tests encode current semantics and will fail post-fix; update alongside (see §4).
- Running app must be restarted to pick changes (per todo.md:129 note).

---

## 6. Confidence & unknowns

- RC1, RC2, RC3, RC4: **Confirmed** (direct code reading + production log reproduction; two independent evidence classes).
- Unknown: whether any other code path relies on `__wait` returning raw output (grep shows none outside tests).
- Unknown: exact UI effect of "(not on any new line…)" beyond the log-confirmed instant/duplicated replies; interpreted per log evidence as the any-new-line trigger described above.

## 7. Suggested next actions
1. Implement §5 (coder), incl. predicate variant + test rewrite.
2. Red/green test: enqueue a fake heartbeat, assert `__wait` returns its text; assert completion message no longer contains lines already returned by `__wait`.
3. Optionally delete-or-wire `wait_for_message` docstring drift follow-up.
