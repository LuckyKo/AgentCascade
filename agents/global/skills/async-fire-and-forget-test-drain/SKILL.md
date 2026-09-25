---
name: async-fire-and-forget-test-drain
description: Draining fire-and-forget asyncio tasks spawned by code under test (e.g. Telegram bridge on_message waiter) so pytest event loops close cleanly and mock assertions stay deterministic.
source: auto-generated
version: "1.0.0"
triggers:
  - "fire-and-forget task leak telegram bridge"
  - "asyncio.create_task in test drain"
  - "event loop closed with pending tasks pytest"
  - "on_message waiter task asyncio.wait_for"
generated_by: coder
generated_from_task: "Telegram bridge unknown-slash-command fall-through change; updated tests now inject via on_message which spawns a fire-and-forget waiter task that had to be drained before the per-test event loop closed."
---

## Goal
When production code under test (e.g. the AC Telegram bridge `on_message` handler) spawns background tasks via `asyncio.create_task(...)`, and your pytest helper runs each test on a fresh event loop, those tasks outlive the awaited coroutine — causing "Task was destroyed but it is pending" noise, flaky mock assertions (background task hits the mock AFTER your asserts), or hangs. This skill captures the drain pattern used in `tests/test_telegram_bridge.py`.

## Procedure

### Step 1 — Track spawned tasks in a known location
Production code should register fire-and-forget tasks somewhere the test can reach, e.g. `context.bot_data.setdefault('waiters', set()).add(task)` plus a done-callback that discards it. If you cannot change production code, find where the task is referenced (or fall back to best-effort draining via `asyncio.all_tasks()`).

### Step 2 — Drain inside the SAME coroutine before the loop closes
Wrap "call under test + drain" in one async function so everything runs on the same loop:
```python
async def go():
    await on_message(update, context)
    for t in list(context.bot_data.get('waiters', ()) or ()):
        try:
            await asyncio.wait_for(t, timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            pass

_run(go())   # _run = fresh new_event_loop().run_until_complete(coro)
```
Key details:
- `list(...)` — the set mutates as done-callbacks discard tasks while you iterate.
- `wait_for(t, timeout=...)` with a SHORT timeout (1s): background tasks often block on polling/sleeping against mocks; you only need them to stop touching the mock before assertions run, not to complete.
- Catch `Exception` broadly: a drained task failing is expected in test land; what you must NOT let happen is it running concurrently with your asserts.

### Step 3 — Use >= assertions for counters the background task touches
If the waiter task also calls a mocked method (e.g. `ac.get_status`), use `assert_called_at_least` / `call_count >= n` instead of `assert_called_once()` for that mock. Reserve exact-count asserts (`assert_called_once()`) for methods ONLY the main path calls — e.g. `ac.inject_message.call_args.args[0] == original_text` is safe and non-vacuous because no background task injects.

### Step 4 — Verify determinism
Run the file a few times (or with `-p no:randomly` if installed). A green run where asserts happen while a waiter task is still in flight can pass by luck on timing; the drain removes that race.

## Tips / gotchas
- **Do NOT assert after `_run(...)` returns for things the background task mutates** — the loop is closed, tasks are gone (cancelled/destroyed), and mock state may be partial. Assert inside `go()` after draining, or accept >= semantics outside.
- If you must drain without a tracked set: `for t in asyncio.all_tasks(): if t is not asyncio.current_task(): await wait_for(t, timeout)`.
- A cancelled drained task raises `CancelledError` inside `wait_for` — the broad `except (asyncio.TimeoutError, Exception)` covers it; do not let it propagate and fail the test.
- This pattern is what makes tests like "unregistered slash command forwards to agent" safe: `on_message` injects AND spawns a waiter that would poll the mock AC client after your assertions otherwise.
