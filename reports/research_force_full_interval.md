# Force-Full Interval Research: ~10s → 60s + Special Occasions

**Investigated:** 2026-09-06 · **Codebase:** `N:\work\WD\AgentCascade` · **Mode:** Investigative (code-path mapping)
**Confidence:** Confirmed (all claims verified against source with line numbers)

---

## Executive Summary

The current force_full mechanism is a **per-run tick-modulo** (`tick_num % 100 == 0`) inside one shared helper (`broadcast_stream_update`), but `tick_num` is **not** a wall-clock counter — it's a local counter that resets to 0 on every run and increments once per engine yield (variable rate). Converting to "every 60s" therefore requires **replacing the modulo with wall-clock tracking** (per-instance `last_force_full` timestamp), not just changing 100 → 600.

Of the five requested "special occasions", **four already push full state today** (compression/rollback via `prefix_shrank` auto-detection, user edit, retry/rollback, sub-agent spawn). **Agent-tab switch is the only gap** — it is a pure frontend event with no server round-trip and no `request_state` WS message type exists. The critical risk is the frontend `_needsResync` gate, which assumes a full snapshot arrives within ≤10s of a dropped frame; a 60s interval would leave desynced panels frozen up to 60s unless a client-triggered refresh path is added.

---

## 1. The Tick-Based force_full Logic (Verified)

### 1.1 Canonical location
- **`agent_cascade/api_integration_pkg/streaming.py:343`** — `force_full = (tick_num % 100 == 0)` inside `broadcast_stream_update` (L261-383).
  - Passed to `build_stream_update_from_pool(pool, instance_name, turn_output, force_full=force_full)` at **L345-350**.
  - Frame enqueued via `_put_stream_update(ws_queue, {'type': 'stream_update', **stream_update})` at **L367-373**.
- Note: `agent_cascade/api_integration.py` is a thin re-export facade (Phase 3b refactor); `temp_original_streaming.py` / `temp_original_state_builder.py` at repo root are **stale temp copies, not live code** — do not edit them. All production importers (`stream_publisher.py`, `run_agent_unified.py`, `security_handler.py`, `api_server.py`) import through the facade → pkg, so **one edit in the pkg covers all callers**.

### 1.2 What force_full=True does in state_builder
- `build_stream_update_from_pool` (state_builder.py:454) → `_serialize_instances_incremental` (state_builder.py:192-270):
  - **L240**: serialize instance if active OR version changed OR `force_full`
  - **L241-250**: `prefix_shrank` detection (msg count dropped → conversation shrunk by compression/rollback) → `full_this_frame = force_full or prefix_shrank`
  - **L253**: `streaming=(not full_this_frame)` → `streaming=False` disables the delta tail cut in `_serialize_instance` (state_builder.py:897; tail cut at L946-953, `_safe_tail_start_index` L699, `TAIL_COMMITTED=1` gated by env `AGENT_CASCADE_STREAM_DELTA=1`)
  - Result: full conversation history serialized. **`is_partial` is NOT set to False** when streaming responses exist — the frame is a "full snapshot" by *content* (startIdx ≤ 0), not by a `force_full` field. **No `force_full` field is ever sent to the frontend.**

### 1.3 How tick_num works (answering §5)
- **Per-run, not per-connection, not global.** Reset to 0 at each run start and incremented once per `engine.run()` yield:
  - Main agent: `run_agent_unified.py:128` (`tick_num = 0`), passed at L210, `tick_num += 1` at L217
  - Security: `security_handler.py:587` (`_sec_tick_num = 0`), L636
  - Compressor: `compression/agent_invoker.py:324` (`_comp_tick_num = 0`), L366/373
  - Advisor: `advisor_runner.py:144` (`_tick_num = 0`), L183/189
- Consequences: (a) tick 0 always fires force_full on run start (first frame of every run is full — desirable, keep); (b) the modulo is **not** time-based — tick rate = engine yield rate (variable, ~100ms throttle in the helper but yields are event-driven), and it **resets on every new run/reconnection**, so "600 ticks" would give wildly inconsistent wall-clock intervals and a fresh force_full at the start of every run.
- **Recommendation:** drop tick-modulo; use a per-instance wall-clock timestamp (see §6 plan). `now_sec` (monotonic) is already passed into `broadcast_stream_update` (L302, L322).

---

## 2. Special Occasions — Current Mechanisms (Verified)

| Occasion | Already pushes full state? | Location | Mechanism |
|---|---|---|---|
| **Compression** | ✅ Yes (indirect) | `state_builder.py:241-250` | `prefix_shrank`: compression shrinks the conversation → msg count in version drops → next frame for that instance is automatically full (no tail cut). No explicit "compression complete" broadcast exists; the compressor agent's own tab streams via `broadcast_stream_update` (`agent_invoker.py:361-371`). |
| **User edits a message** | ✅ Yes | `ws_handlers.py:1004` `handle_edit_message` → `await self._broadcast()` at **L1107** | `_broadcast` (L117-129) builds **full** `state` via `build_state_from_pool` and broadcasts to all clients. (WS type `edit_message`, sent by frontend `app.js:3605`.) `handle_delete_messages` (L1109) does the same. |
| **Rollback** | ✅ Yes | `ws_handlers.py:542` `handle_retry` → `await self._broadcast(generating=True)` at **L591** | Full `state` broadcast after `rollback_to_snapshots` (L552) + trailing-trim rollback (L572). (`state_builder.py:241` `prefix_shrank` also covers in-loop auto-rollbacks.) |
| **UI agent tab switch** | ❌ **No — gap** | `app.js:4349` `switchMainTab` (pure DOM), tab click handlers at L3887/L3997 | No WS message is sent on tab switch; frontend renders from locally-merged `state.subAgents` (populated by earlier frames; comment at app.js:127 confirms "early-return removed to avoid render delays when switching back"). **No `request_state` / `request_full` WS message type exists** (full dispatch table: `ws_handlers.py:65-97`). Closest existing type: `refresh_souls` (L77) → full state broadcast, but with server-side template-reload side effects — unsuitable for tab switches. |
| **Agent call (sub-agent spawned)** | ✅ Yes | `stream_publisher.py:82` `push_initial_state` — `force_full=True` at **L115** ("BUG C: new sub-agent's first frame must be full") | Called from `engine/core.py:3098` (normal sub-agent) and `core.py:3330` (system agent) right after `AgentInstance` creation. Plus `push_periodic_update` (L130, throttled partials) and `push_final_state` (L176) on completion. |

Also relevant (no change needed): **reconnect** — server auto-sends initial full `state` on WS connect (`api_server.py:1258`); frontend reconnects after 2s (`app.js:1686-1694`, `scheduleReconnect` L1712). **Session load** — `handle_load_session` (L1204) → `_broadcast()` L1264.

---

## 3. Frontend Handling of force_full Frames (Verified)

`app.js` `case 'stream_update'` (L2014-2295):
- **No `force_full` field exists.** Detection is by `is_partial` + content:
  - `is_partial` (L2044): delta merge — `startIdx = history_count - messages.length` (L2078); splice in (L2095-2096), index-mismatch → `_needsResync = true` (L2089-2093).
  - **Full snapshot** = a partial frame with `startIdx <= 0` (L2052-2064): safely replaces the entire list and clears `_needsResync`.
  - Non-partial (L2128-2131): whole-agent replacement `state.subAgents[name] = {...sa}`.
- `case 'state'` (L1975-2012): full re-render path (`invalidateAllPanelCaches`, `renderSubAgents`) — used by edit/retry/rollback/reset/load_session broadcasts.
- **Critical coupling:** the `_needsResync` gate (L2051-2064) and the fallback comment at **L2119** ("drop such frames… self-heal at the next force_full (≤10s)") explicitly rely on a full snapshot arriving **within ~10s**. At a 60s interval, a single dropped frame would leave the panel frozen (skipping all tail deltas) for up to 60s.
- **No client-triggered refresh mechanism exists** (verified all `send({type:...})` call sites: message, continue, stop, pause/resume, edit_message, delete_messages, approve/reject/ask_security, terminate, refresh_souls, reset, select_agent, set_session_name, load_session, inject, dismiss_queue, export/import_settings, update_config, restart_server).

**Answer:** the frontend already handles force_full (full-snapshot) frames correctly — it replaces/resyncs on them. It does **not** have a "request refresh" mechanism; one must be added (new WS type, e.g. `request_state` → server replies with a `stream_update` built with `force_full=True` or a full `state` broadcast).

---

## 4. Queue Debug Warning (Verified)

- `streaming.py:243-259` `_put_stream_update`: `queue.put_nowait(event)` with `except asyncio.QueueFull: pass` (L258-259) — **silent drop, no log**.
- Queue: `_send_queue` created lazily per event loop, **maxsize=128** (`api_server.py:405-427`); drained by `_sender_loop` (`api_server.py:769+`) → `broadcast` (L616, per-client 5s timeout, L96).
- Change needed: in `_put_stream_update`'s `QueueFull` handler, log a **rate-limited warning** (e.g., module-level `_last_drop_warn` timestamp; warn at most once per N seconds, include `queue.qsize()`). Note the `dismissal` callback path uses blocking `put` (`api_server.py:761`) — unaffected.

---

## 5. Recommended Implementation Plan

### A. Backend — 60s wall-clock interval (replaces tick modulo)
1. **`api_integration_pkg/streaming.py`**
   - Add module-level `_force_full_state: dict[str, float]` + lock (or reuse `_cache_mgr`) mapping `instance_name → last_force_full monotonic ts`.
   - In `broadcast_stream_update` (L339-343): replace `force_full = (tick_num % 100 == 0)` with:
     - `force_full = (now_sec - _force_full_state.get(instance_name, -INF) >= 60.0)`; record `now_sec` when fired.
     - Keep an initial-run full frame: first broadcast for an unseen instance → force_full (preserves today's "first frame full" behavior that the frontend R1 guard depends on, app.js:2114-2127).
   - `tick_num` param can be **removed** (cleaner) or kept for backward compat; callers at `run_agent_unified.py:210`, `security_handler.py:636`, `compression/agent_invoker.py:366`, `advisor_runner.py:183` and local counters (L128/L587/L324/L144) can be deleted.
   - **`_put_stream_update` (L258)**: add rate-limited QueueFull warning.
2. **`state_builder.py`**
   - Update docstring/comment at L202-204 and L244-245 ("100-tick force_full self-heals within ~10s" → 60s) — behavior unchanged.
   - `prefix_shrank` (L241-250) already covers compression/rollback shrinks — **no change needed** there.
3. **No changes needed** for edit / retry / load_session (they use the full `state` path, independent of stream_update).

### B. New WS type for client-triggered full refresh (closes the 60s resync gap + tab switch)
4. **`ws_handlers.py`**: add `'request_state': self.handle_request_state` to the dispatch table (L65-97). Handler: `build_stream_update_from_pool(pool, session_name, responses=None, force_full=True)` → push `{'type': 'stream_update', **su}` via `_put_stream_update` (or simpler: `await self._broadcast()` full `state`).
5. **`app.js`**:
   - `switchMainTab` (L4349): on manual tab click (L3887, L3997) send `{type: 'request_state'}` — server responds with a full snapshot; existing `stream_update` full-snapshot merge (L2052-2064) clears `_needsResync` and replaces the list.
   - Update the L2119 comment ("≤10s" → "≤60s or on request_state").
   - (Optional) also send `request_state` when `_needsResync` is set (L2084) to shrink the stuck window from 60s to ~1 tick.

### C. Special-occasion audit outcome
- Compression ✅ (prefix_shrank) — no change
- User edit ✅ (`_broadcast` full state) — no change
- Rollback/retry ✅ (`_broadcast` full state) — no change
- Agent call ✅ (`push_initial_state` force_full) — no change
- Tab switch ❌ → new `request_state` WS type (items 4-5)

### D. Risks / open questions
- **Resync gap**: with 60s fulls, a dropped frame stalls a panel up to 60s without item B (HIGH risk — implement B alongside A).
- **Memory growth of full frames**: 1.7MB @ turn 187 observed; 6x less frequent mitigates bandwidth, single frame size unchanged.
- **`refresh_souls` conflation**: do NOT reuse it for tab switches (template-reload side effects).
- **Multi-client**: `_broadcast` reaches all clients; per-client `request_state` should consider whether to broadcast or send unicast (current `broadcast_fn` is broadcast-only; unicast would need per-connection send — decide during implementation).
- **tick_num removal** touches 4 files + tests; if risk-averse, keep the param and ignore it.

---

## Evidence Index (all paths relative to `N:\work\WD\AgentCascade`)

| Fact | Location |
|---|---|
| `force_full = (tick_num % 100 == 0)` | `agent_cascade/api_integration_pkg/streaming.py:343` |
| force_full → build call | streaming.py:345-350 |
| `_put_stream_update` silent QueueFull drop | streaming.py:256-259 |
| `build_stream_update_from_pool` | `agent_cascade/api_integration_pkg/state_builder.py:454` |
| `_serialize_instances_incremental` / `prefix_shrank` | state_builder.py:192-270 (shrink at 241-250) |
| Delta tail cut (`STREAM_DELTA_ENABLED`, `_safe_tail_start_index`) | state_builder.py:23-29, 699-737, 946-953 |
| `push_initial_state` (force_full=True) | `agent_cascade/stream_publisher.py:82-128` (L115) |
| Spawn call sites | `agent_cascade/engine/core.py:3098, 3330` |
| tick_num per-run counters | run_agent_unified.py:128/210/217; security_handler.py:587; compression/agent_invoker.py:324/366/373; advisor_runner.py:144/183/189 |
| `handle_edit_message` → `_broadcast()` | `agent_cascade/ws_handlers.py:1004` (broadcast L1107) |
| `handle_retry` → `_broadcast(generating=True)` | ws_handlers.py:542 (broadcast L591) |
| `handle_load_session` → `_broadcast()` | ws_handlers.py:1204 (broadcast L1264) |
| Dispatch table (no request_state type) | ws_handlers.py:65-97 |
| Queue maxsize=128, `_sender_loop` | `agent_cascade/api_server.py:394-427, 769+` |
| Initial `state` on connect | api_server.py:1251-1268 |
| `pool._ws_send_queue`/`_ws_loop` wiring | run_agent_unified.py:100-101 |
| stream_update merge / `_needsResync` / "≤10s" comment | `web_ui/app.js:2014-2132` (L2051-2064, L2119) |
| `switchMainTab` (no WS round-trip) | app.js:4349; click handlers L3887, L3997 |
| Reconnect (2s) + onopen sync | app.js:1686-1718 |
| Facade (no duplicate live code) | `agent_cascade/api_integration.py` (re-exports pkg) |
