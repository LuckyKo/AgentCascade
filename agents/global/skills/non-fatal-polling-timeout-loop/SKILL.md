---
name: non-fatal-polling-timeout-loop
description: Convert a fatal poll-waiter timeout into a recurring "still working" checkpoint loop bounded by an outer ceiling, with deterministic hermetic tests.
source: auto-generated
version: "1.0.0"
triggers:
  - "non-fatal timeout"
  - "still working notice"
  - "waiter keeps polling"
  - "outer ceiling"
  - "timeout abandons delivery"
generated_by: coder
generated_from_task: "Make Telegram bridge task timeout non-fatal: send 'still working' notice and keep waiting bounded by a new outer ceiling (TG_TASK_WAIT_CEILING_SEC) instead of abandoning delivery."
---

## Goal
Turn a polling waiter whose timeout currently abandons the result into one that treats each timeout as a recurring user-visible checkpoint, keeps polling until the work actually finishes, and is bounded by an outer hard ceiling so a genuinely stuck backend can't wait forever.

## Procedure

### Step 1 — Keep the existing poller untouched; loop it from the caller
The well-tested `wait_for_completion(...)` (FINISHED/TIMEOUT/OFFLINE) stays as-is. The caller becomes:
```python
start = loop.time()
next_notify_at = start + cfg.task_timeout_sec
offline_after = min(TG_OFFLINE_AFTER_SEC, cfg.task_timeout_sec)  # re-arms per round by design
while True:
    result = await wait_for_completion(ac, poll_interval=..., timeout=max(0.1, next_notify_at - loop.time()), offline_after=offline_after)
    if FINISHED: deliver; return
    if OFFLINE: offline notice; return
    # TIMEOUT -> checkpoint:
    send(f"⏳ Still working ({_fmt_elapsed(loop.time() - start)}) ...")
    if loop.time() - start >= cfg.task_wait_ceiling_sec: ceiling message; return
    next_notify_at += cfg.task_timeout_sec
```
Key invariants: exactly ONE notice per `task_timeout_sec`; a run finishing between notices gets its reply immediately (the poller returns FINISHED the moment it sees idle — no extra ping); `offline_after` is relative to each call, so transient blips survive while sustained unreachability still yields OFFLINE.

### Step 2 — Make the ceiling injectable via the config dataclass
Add `task_wait_ceiling_sec: float = TG_TASK_WAIT_CEILING_SEC` to the BridgeConfig-style dataclass and wire it in `load_config()` with the same env-override pattern as sibling knobs (`float(os.environ.get('ENV') or CONST)`). This beats monkeypatching settings in tests — no env mutation, consistent with how poll_interval_sec/task_timeout_sec are already handled.

### Step 3 — Deterministic tests (the hard part)
- **Ceiling-exit test**: mock status script `[True]*N` (never finishes), tiny `task_timeout_sec=0.1`, tiny `task_wait_ceiling_sec=0.2`. Assert "Still working" sent AND ceiling message sent AND old fatal text is GONE (`assert not any('Timed out' in t ...)` — regression guard against reverting).
- **Late-completion test (the core regression)**: status script `[True]*N + [False]` where N × poll_interval > first round timeout but well under the ceiling. Assert BOTH the "Still working" notice AND the final answer text appear in captured sends. This proves delivery is not abandoned.
- Sizing pitfall: with a mock server, `poll_interval` dominates real elapsed time — e.g. 200 True entries × ~100ms = ~20s of wall clock, which is fine but shows up as the slowest test; keep N minimal (just past the first timeout) and ceiling generous in that test.
- Unit-test any pure helper (`_fmt_elapsed`) separately: s/m/h boundaries (59→"59s", 60→"1m", 3600→"1h 00m") plus negative clamp.

### Step 4 — Update ALL docs in the same pass
Module docstring ("receiving path"), config env-var list, README table. Stale "Timed out / giving up" wording is the most common miss — grep for the old notice string across .py and .md.

## Tips
- **Don't trust your own test math**: I wrote `assert _fmt_elapsed(90) == '90s'` while the function (correctly, per its <60s rule) returns "1m". Boundary assertions must be derived from the implemented rule, not the docstring example — and fix the docstring if it's wrong.
- **xdist flakes**: a supervisor test that fails once in a full parallel run but passes in isolation is likely a pre-existing xdist flake; verify with `git stash` → run suite on clean tree → `git stash pop`, then re-run the full suite once to confirm green before reporting.
- Preserve the existing exception path ("Could not reach AC") INSIDE the loop so a raise mid-wait still surfaces gracefully rather than leaking out of the fire-and-forget task.
- Do NOT commit if the task says "I will review and commit".
