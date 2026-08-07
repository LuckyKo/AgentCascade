# Investigation Report — "Server stuck in loading" (WebSocket initial state never received)

**Investigator:** researcher_hang
**Date:** 2026-08-05
**Mode:** Investigative
**Confidence:** Moderate–High (code-level confirmed; runtime failure signature inferred from logs)

---

## Executive Summary

The frontend never receives the initial `{"type": "state", ...}` message and stays on the
loading screen because of how `ws_chat()` in `api_server.py` builds and sends it:

1. **The entire heavy state build runs synchronously on the single asyncio event-loop**
   before the first `send_text`, and
2. **the build+send are wrapped in a single `try/except` that silently discards the socket
   and `return`s on any exception** (only a single WARNING, easily lost in rotated logs).

The evidence shows this is **intermittent**, not a permanent defect — a later server run
(port 12346) serves WebSockets fine and my own session runs through it. The stuck-loading
only reproduces when `build_state()` throws (non-serializable content, empty/bad instance,
lock contention) or the event loop is transiently blocked during connect.

---

## Key Findings

### Finding 1 — The initial-send failure mode is silent and irrecoverable (PRIMARY)

`agent_cascade/api_server.py`, `ws_chat()` (L1022-1059):

```python
@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    ws_connections.add(websocket)
    # Send initial state
    try:
        init = {'type': 'state', **build_state()}
        await websocket.send_text(json.dumps(init, ensure_ascii=False, default=str))
    except Exception as e:
        logger.warning(f"WebSocket initial state send failed: {e}")
        ws_connections.discard(websocket)
        return          # ← client gets NOTHING, no error frame, no retry
```

If `build_state()` raises or `send_text` fails, the socket is discarded and the handler
`return`s. **No error is sent to the client**, so the frontend's `onopen` fires, the WS
stays `OPEN`, but the app never receives a `{type:'state'}` message — the UI stays on
"loading" forever, and `scheduleReconnect()` (which only fires on `onclose`) never triggers.
The frontend has **no open-timeout / no initial-state timeout**.

### Finding 2 — `build_state()` is heavy and fully synchronous on the event loop

`build_state()` (api_server.py L392) → `build_state_from_pool()` (api_integration.py L735).
It synchronously (no `await`, no `to_thread`):
- `_serialize_all_instances` → `_serialize_instance` → `serialize_message` for **all messages
  of every instance** (incl. the loaded 147-message session).
- Computes token stats per message via `get_history_stats`/`get_message_stats`
  (utils.py L1036) which calls **`qwen_count` (tiktoken `qwen.tiktoken`) per message**
  (tokenization_qwen.py L245), acquiring `_cache_mgr._lock`.
- Acquires `inst._compression_lock` (an `RLock`, so re-entrancy is safe, but it can still
  wait on a worker thread holding it during compression).

Because uvicorn runs `ws_chat` on the single event-loop, this CPU-heavy build blocks:
- the initial `send_text` for this client,
- `broadcast()` to **all** clients,
- every `async def` REST handler (`/api/*`),
- the `_sender_loop` and `_approval_loop` background tasks.

### Finding 3 — It's intermittent (confirmed via logs)

- 12:06:50 run: `[OK] API Server ready!`; loaded session `...with 147 messages`;
  the frontend's `update_config` messages were processed (12:07:02) — which only happens
  **after** the initial state send at L1030 — so on that run the initial send succeeded.
- A later run (13:24:51, port 12346) actively serves agents → WS path works.
- Therefore stuck-loading has a transient trigger (an initial-build exception or a blocked
  loop during connect), not a permanent logic bug in the connect path itself.

### Finding 4 — No blocking network call in the state path itself

`build_state_from_pool()` and its helpers (token stats, serialization, api_router, approvals)
contain only CPU work + short RLock/Lock acquisitions and are wrapped in defensive
`try/except`. `qwen_count` uses a **local** `qwen.tiktoken` file (no network). So the hang is
**event-loop blocking + CPU** or an **exception**, not a network deadlock.

### Finding 5 — Likely trigger candidates

1. **A `Message`/dict in the conversation that fails `serialize_message`/tokenize** (non-string
   content, multimodal list edge-case, `function_call` object) → raises inside `build_state()`
   → the L1031 `except` swallows it.
2. **Lock contention**: `_cache_mgr._lock` or `inst._compression_lock` held by a worker during
   compression/tool execution while the loop is trying to build initial state.
3. **Multi-instance blow-up**: `_serialize_all_instances` serializes every instance; many
   sub-agents concurrently → large synchronous payload; if it takes seconds, the frontend
   (socket OPEN, no timeout) shows loading while the loop is stalled.
4. **`handle_set_session_name`/`handle_update_config` on connect** (frontend sends both
   immediately on `onopen`) run AFTER the initial send, so they don't block it — but they do
   each trigger an additional `_broadcast()` = additional synchronous `build_state()`.

### Non-issues examined and cleared
- `agent_pool` bad state during `create_app()`: guarded — if `_load_session_history` fails or
  instance is `None`, it creates a fallback instance (api_server.py L315-329) so
  `build_state_from_pool()` doesn't return `None`. **Not the hang cause.**
- `refresh_agents`/`refresh_souls` recent changes: triggered only by explicit WS tool commands,
  not the connect/initial-state path. Not the cause.
- Frontend rendering timing (prior 2026-06-28 lesson `lessons_api_loading_investigation.md`):
  that lesson found agents/endpoints loading but not displaying — **different symptom**. This
  issue is the initial `state` message not being received at all.

---

## Recommended Fixes (objective, evidence-based)

1. **Log the full traceback and propagate on initial-send failure**:
   `traceback.print_exc()` in the `except` at api_server.py:1031 so the exact failing object
   is identifiable (currently only `logger.warning` with the message string).

2. **Don't silently `return`** — send an error frame or close with a code
   (`await websocket.close(code=1011)`), so the frontend shows a message and reconnects.

3. **Move the initial build off the event loop** (`await asyncio.to_thread(build_state)`) and
   add a timeout, so a heavy/locking build can't stall the loop or all other clients.

4. **Frontend**: add a connect/initial-state timeout in `connect()` (app.js) — if `ws` is OPEN
   but no `{type:'state'}` within N seconds, surface an error instead of an infinite
   loading spinner.

5. **Defensive token-stats**: in `get_message_stats`/`serialize_message`, catch and coerce
   non-string/multimodal content to a string instead of raising mid-build (reduces trigger #1).

---

## Confidence

- **Confirmed (code):** initial-send failure silently discards socket + returns; heavy build is
  synchronous on the loop; no client timeout.
- **High (behavior):** frontend gets no state and stays loading when that path fires.
- **Moderate (trigger):** exact transient trigger not reproduced here — needs the full traceback
  logging (fix #1) to capture the first failure in the field.

## Remaining Unknowns

- Which exact object/exception fires in `build_state()` at connect in the field.
- Whether the trigger is an exception vs. long lock wait (both produce the same user symptom).

## Suggested Next Actions

1. Apply fix #1 (full traceback logging) and reproduce/observe the first failing connect.
2. Add the frontend initial-state timeout (fix #4) as a user-visible diagnostic.
3. Inspect `console.log.*` rotation files before the reported incident for the
   "WebSocket initial state send failed:" WARNING line.

*Report and memory saved:* `N:\work\WD\AgentCascade\.agent_lessons\initial_state_stuck_loading.md`