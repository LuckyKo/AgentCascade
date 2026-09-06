# Investigation: Adding Elapsed Time to Async shell_cmd Heartbeats

**Date:** 2026-08-10
**Investigator:** invest_shell_heartbeat
**Objective:** Add wall-clock elapsed duration to async `shell_cmd` heartbeat messages (target format: `"⟨shell_cmd heartbeat⟩ Beat 2 (15s), Tool ID: 8 | No new output (still running)"`).

---

## 1. Summary of Findings

The async `shell_cmd` implementation lives in **`agent_cascade/async_shell.py`** (1469 lines). Heartbeat generation is centralized in one method, `AsyncShellTracker._send_heartbeat()`. The infrastructure for elapsed-time tracking **already exists**:

- `AsyncShellTask.start_time` (epoch float, set via `field(default_factory=time.time)` at task creation) — the "command start" timestamp.
- Elapsed time is *already computed and displayed* in two sibling messages: the completion message (`Completed in {elapsed:.1f} s`) and the `__status` control command (`running ({elapsed:.0f}s elapsed)`).

The **only gap** is that `_send_heartbeat()` does not currently read `task.start_time` / compute `elapsed` when formatting its two heartbeat message variants.

---

## 2. Key Files & Locations

### 2.1 Primary: `agent_cascade/async_shell.py`

| What | Location | Notes |
|---|---|---|
| `AsyncShellTask` dataclass | **lines 119–171** | Holds `start_time: float = field(default_factory=time.time)` at **line 150** |
| `AsyncShellTracker.launch()` | lines ~280–345 | Creates task via `AsyncShellTask(...)` at **lines 309–316**; `start_time` auto-set here |
| `_poll_loop()` | **lines 572–616** | Calls `_send_heartbeat()` at **line 610** when `since_last_hb >= current_hb_interval`; already computes `elapsed = time.time() - task.start_time` at **line 595** for timeout checks |
| `_send_heartbeat()` | **lines 1034–1105** | ⭐ **The method to modify** — see §4 |
| `_send_remaining_output()` | lines 1108+ | Final-output flush; not a heartbeat, uses `task._lock` + `last_heartbeat_sent_pos` pattern; no elapsed display |
| Completion message | **lines 1166–1190** | Already shows `elapsed = time.time() - task.start_time if task and task.start_time else 0` → `Completed in {elapsed:.1f} s` |
| `__status` control command | **lines ~1350–1419** | Already shows `elapsed = time.time() - task.start_time` → `running ({elapsed:.0f}s elapsed)` |
| `_enqueue()` | lines 1196–1203 | Message injection into agent queue via `self._pool.enqueue_message(agent_name, text)` |
| `_get_task()` | lines 208–211 | Thread-safe task lookup under `self._lock` |

### 2.2 Caller/entry: `agent_cascade/tools/custom/shell_cmd.py`

| What | Location | Notes |
|---|---|---|
| `async_mode` param parse | lines 91–102 | `async_mode`, `heartbeat_interval`, auto-async threshold logic |
| `_launch_async()` | lines ~250–267 | Calls `tracker.launch(...)` at **line 259**; has its own local `start_time = time.time()` at **line 257** (used only for the sync wrapper timing) |
| Tracker access | lines 156–160 | `self.agent_pool._async_shell_tracker` |

### 2.3 Wiring: `agent_cascade/agent_pool.py`

| What | Location | Notes |
|---|---|---|
| Tracker instantiation | **lines 321–323** | `self._async_shell_tracker = AsyncShellTracker(pool=self)` |
| Kill-all on agent shutdown | lines 1020–1026 | `self._async_shell_tracker.kill_all(instance_name)` |
| Pending check | lines 2484–2487 | `has_active_tasks(instance_name)` used by wait/pending logic |

---

## 3. Current Heartbeat Behavior

**Trigger path:** `_poll_loop()` (line 607–611): when `task.heartbeat_interval > 0` and `time.time() - last_heartbeat_time >= task.heartbeat_interval`, calls `_send_heartbeat(agent_name, tool_id)`.

**`_send_heartbeat()` current flow (lines 1034–1105):**
1. Lookup task (`_get_task`) → bail if `None`.
2. Under `task._lock`: read `stdout_lines + stderr_lines`, slice `combined[task.last_heartbeat_sent_pos:]` → `new_lines`, advance `last_heartbeat_sent_pos`, increment `task.heartbeat_count`, capture `beat = task.heartbeat_count`.
3. Re-check `task.killed` under lock → bail if killed.
4. **No new output branch (lines 1065–1072):**
   `"⟨shell_cmd heartbeat⟩ Beat {beat}, Tool ID: {tool_id} | No new output (still running)"`
5. **With-output branch (lines 1099–1105):**
   `"⟨shell_cmd heartbeat⟩ Beat {beat}, Tool ID: {tool_id} | {line_count} line(s) since last tick\n{output_text}"` (after truncation via `truncate_with_spillover`, `operation_mode='mid'`).
6. Both branches enqueue via `self._enqueue(agent_name, msg)` → `pool.enqueue_message()`.

**Beat counting:** `task.heartbeat_count` incremented inside `_send_heartbeat` per beat (per task, per agent). Note: only increments on *heartbeats actually enqueued* (the `killed` re-check at line 1061–1063 can return before increment — actually no, the increment happens *before* that re-check at line 1057, so a killed-task race could increment count without sending; minor, pre-existing).

**Output capture:** stdout/stderr drained continuously by background threads (`_spawn_process`, lines 449–569) into `task.stdout_lines`/`task.stderr_lines`; heartbeat reads a *slice since last send* under `task._lock`.

---

## 4. Where to Add Elapsed-Time Tracking (Recommended Change)

**Single edit point:** `_send_heartbeat()` in `agent_cascade/async_shell.py` (lines 1034–1105). No other file needs changes.

1. **Record start:** Already done — `AsyncShellTask.start_time` (line 150) is set at task creation (line 309–316 in `launch()`). No new field needed.
2. **Compute duration:** Inside `_send_heartbeat`, read under the existing lock (e.g., right after `beat = task.heartbeat_count` at line 1058):
   ```python
   elapsed = time.time() - task.start_time
   ```
   Safe to compute under `task._lock` since `start_time` is set once at construction and never mutated; alternatively compute outside the lock. Consistency with siblings: `_poll_loop` line 595, completion line 1166, status line 1373 all use the same `time.time() - task.start_time` expression.
3. **Format:** Modify the two `msg` strings:
   - No-output branch (line 1070) →
     `f"⟨shell_cmd heartbeat⟩ Beat {beat} ({elapsed:.0f}s), Tool ID: {tool_id} | No new output (still running)"`
   - With-output branch (line 1100) →
     `f"⟨shell_cmd heartbeat⟩ Beat {beat} ({elapsed:.0f}s), Tool ID: {tool_id} | {line_count} line{'s' if line_count != 1 else ''} since last tick\n{output_text}"`
   Use `{elapsed:.0f}` (integer seconds) to match the `__status` format (`running ({elapsed:.0f}s elapsed)`) and the target example `(15s)`.

**Thread-safety note:** read `start_time` under `task._lock` (it's immutable; simplest is to add `elapsed = time.time() - task.start_time` inside the existing `with task._lock:` block at lines 1049–1058 and reference it in both branches). Alternatively read it just before formatting — no locking risk either way.

---

## 5. Existing Timer/Elapsed Logic (inventory)

| Location | Expression | Where shown |
|---|---|---|
| `async_shell.py:595` | `elapsed = time.time() - task.start_time` | `_poll_loop` timeout check vs `task.timeout` |
| `async_shell.py:1166` | `elapsed = time.time() - task.start_time if task and task.start_time else 0` | Completion msg: `Completed in {elapsed:.1f} s` |
| `async_shell.py:1373` | `elapsed = time.time() - task.start_time` | `__status` msg: `running ({elapsed:.0f}s elapsed)` |
| `shell_cmd.py:257` | `start_time = time.time()` | `_launch_async` wrapper (measures tracker.launch round-trip, unrelated to heartbeat) |
| `agent_pool.py` / `security_handler.py:303` | `sec_start_time = time.monotonic()` | Unrelated (session timeout warning) |

No existing timer-based elapsed display in `_send_heartbeat` — beats are counted by `heartbeat_count`, interval enforced by `last_heartbeat_time` in `_poll_loop` (line 587).

---

## 6. Tests Impacted

- `tests/test_async_shell_cmd.py` heartbeat assertions (lines ~98–160) only check `'⟨shell_cmd heartbeat⟩' in msg` and `'Tool ID: 1' in msg` — **substring checks, not exact strings**, so appending `({elapsed:.0f}s)` to the header is non-breaking for these.
- `tests/test_async_shell_cmd.py:256/268` and `test_async_shell_kill.py:192–197` check `'No new output'` / `'heartbeat'` substrings — unaffected by adding the elapsed segment.
- ⚠️ **Caveat:** if any test asserts the *exact* string `"Beat 1, Tool ID"`, it would break. Current grep found no such exact-match tests; recommend re-running `tests/test_async_shell_cmd.py` and `tests/test_async_shell_kill.py` after the change.

---

## 7. Implementation Recommendation

- **Minimal, single-file change** in `agent_cascade/async_shell.py::_send_heartbeat` (lines 1034–1105).
- Add `elapsed = time.time() - task.start_time` inside the existing `with task._lock:` block (after line 1058).
- Insert `({elapsed:.0f}s)` after `Beat {beat}` in **both** heartbeat message branches (lines 1070 and 1100), matching the requested target format and the existing `__status` convention.
- Reuse `task.start_time`; do **not** introduce a new timestamp field — it already exists and is used consistently elsewhere.

## 8. Confidence Level

- **Confirmed:** heartbeat generation location, message formats, beat counting, output slicing, and existing elapsed computation patterns (all read directly from source).
- **High Confidence:** `start_time` exists on `AsyncShellTask` (line 150) and is the right anchor; `_send_heartbeat` is the only heartbeat formatting site.

## 9. Open Questions / Notes

- Whether elapsed should appear in the with-output branch too (recommended: yes, for consistency) — design decision for Maine.
- `{elapsed:.0f}` vs `{elapsed:.1f}` formatting: `.0f` matches the target example `(15s)` and the `__status` format.
- Minor pre-existing quirk: `task.heartbeat_count` is incremented (line 1057) *before* the killed re-check (lines 1061–1063), so a killed task could theoretically increment count without a send — not related to this change, flagging only.