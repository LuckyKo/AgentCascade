---
name: ptb-22x-inprocess-thread-shutdown
description: Run python-telegram-bot 22.x run_polling() in a daemon thread of an existing asyncio app and drive graceful shutdown cross-thread (stop_running is NOT callable off-loop).
source: auto-generated
version: "1.0.0"
triggers:
  - "run_polling thread"
  - "telegram bridge in-process"
  - "PTB stop_running RuntimeError"
  - "python-telegram-bot daemon thread"
  - "stop_signals=None"
generated_by: ponytail
generated_from_task: "Convert AgentCascade Telegram bridge from supervised child process to in-process daemon thread; needed verified cross-thread shutdown mechanism for PTB 22.8."
---

## Goal
Embed a PTB 22.x `Application.run_polling()` loop inside an app that already runs its own event loop (e.g. uvicorn/FastAPI), with clean external shutdown — no nested-loop errors, no reliance on PTB's signal handling.

## Verified facts (PTB 22.8, `telegram/ext/_application.py`)
- `run_polling()` is synchronous: it creates its own loop and calls `loop.run_forever()`. It must be called from a plain thread, never from inside another running loop.
- `Application.stop_running()` does `asyncio.get_running_loop().stop()` — calling it from ANOTHER thread raises `RuntimeError`. Do NOT use it as the cross-thread stop.
- The correct cross-thread stop: `asyncio.run_coroutine_threadsafe(app.stop(), bridge_loop)`. `__run` awaits `self.stop()`, so resolving that coroutine breaks `run_forever()` and PTB runs its full teardown order (updater.stop → stop → post_stop → shutdown → **post_shutdown**).
- Signal handling: on Windows the default is NO signal handlers (`if stop_signals is DEFAULT_NONE and platform.system() != "Windows"`); on POSIX defaults to SIGINT/SIGTERM/SIGABRT via `loop.add_signal_handler`, which fails off the main thread. Pass `stop_signals=None` explicitly in both cases — shutdown is driven by your code, not signals.
- `ApplicationBuilder().post_shutdown(coro)` (single coro, no `add_post_shutdown_task` in 22.x) is where you cancel fire-and-forget tasks and close loop-bound clients — it runs on the bridge loop during teardown.

## Procedure
### Step 1 — Thread body shape
```python
def _bridge_thread_body(self):
    while True:                      # bounded-restart loop lives HERE, no watcher thread
        try:
            cfg = load_config()
            problems = validate_config(cfg)
            if problems: self._record_error(...); break      # config-class failure: permanent stop
            ac = ACClient(base_url=resolve_base_url())       # sync ctor, safe anywhere
            app = build_application(cfg, ac)                 # registers post_shutdown (cancel waiters + await ac.close())
            self._publish(app)                               # set self._app / self._loop under lock BEFORE run_polling
            app.run_polling(allowed_updates=['message'], stop_signals=None)
            break                                            # clean return = "someone stopped it" -> no restart
        except Exception as e:
            if self._stopping or self._attempts >= MAX: self._record_error(...); break
            time.sleep(min(cap, base * 2 ** self._attempts)); self._attempts += 1
```

### Step 2 — Cross-thread stop()
```python
def stop(self):
    with self._lock:
        self._enabled = False; self._stopping = True
        app, loop, thread = self._app, self._loop, self._thread
    if app is not None and loop is not None and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(app.stop(), loop)   # NOT app.stop_running()!
    if thread is not None:
        thread.join(timeout=self.stop_join_timeout_sec)      # bounded; daemon thread can't block exit
```

### Step 3 — Loop-bound clients (httpx AsyncClient)
Do NOT pre-open the client with a separate `asyncio.run(client.open())` and then run it on PTB's loop — that's a cross-loop fd wart. Instead: skip pre-open entirely if the client auto-opens lazily (`if self._client is None: await self.open()` in its request path), or open it inside a `post_init` hook. Close it in `post_shutdown`. One client per run attempt; fresh client on each restart.

### Step 4 — start/stop race
`start()` under lock: if the previous thread is still alive (slow stop), bounded-wait for its join (≤ stop_join_timeout) before spawning a new one — refusing to start leaves the bot silently off, and a stuck old loop still holding the token is exactly the duplicate-poll condition you're eliminating.

## Tips
- Publish `app`/loop to the manager under lock BEFORE calling `run_polling()`, so `stop()` called during bootstrap can still find them; guard with `loop.is_closed()` for the post-teardown window.
- Threads can't be killed: after join timeout, log + mark error and move on (daemon threads die at interpreter exit). Don't build kill machinery.
- Test seam: wrap `build_application` in a module-level function in the manager and patch it in tests with a fake app that records `run_polling(kwargs)` and blocks on an Event until the stop coroutine is scheduled — this makes the test fail if someone "simplifies" to calling `stop_running()` directly.
- `status()` for UI: drop process-era keys (pid, last_exit_code) — nothing should read them; grep before deleting.
