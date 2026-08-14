# UI Agent Termination Path Trace — dismiss_instance Verification

**Date**: 2026-08-10
**Author**: ui_path_checker (researcher)
**Scope**: Verify that ALL UI termination paths reach the fixed `dismiss_instance()` in `agent_cascade/agent_pool.py` (zombie-thread fix, commits `961baff`, `3fef78e`).
**Confidence**: High (static trace of committed code; working tree matches HEAD for the touched file)

---

## Executive Summary

✅ **All UI agent-termination entry points funnel into the same WebSocket handler (`handle_terminate`), which calls `agent_pool.dismiss_instance()`. There is exactly ONE WebSocket endpoint (`/ws/chat`), ONE handler for both `terminate_agent_instance` and `terminate_sub_agent`, and no REST endpoint that terminates agents.**

- Closing an agent tab (×) and clicking the ⚠️ Terminate button **both send the identical message** `{type: 'terminate_agent_instance', instance_name}` (app.js:3427 and app.js:4699).
- The server handler `WsMessageHandler.handle_terminate` calls `self.agent_pool.dismiss_instance(instance_name)` (ws_handlers.py:542).
- **No direct UI-layer manipulation of `terminated_instances`** exists — the only production mutations are inside `agent_pool.py` (in `terminate_instance`, `dismiss_instance`, `reset`, `_clear_all_state_dicts`, `_restore_instance_state`).
- The only pool-removal bypass found (`agent_pool.py:2017`, session-load instance swap) is a **same-name in-place swap during `load_session_from_log`** — not a UI termination path, and it does not touch `terminated_instances` (no zombie-thread risk).
- `stop` (`handle_stop`) does **not** remove instances — it transitions them to IDLE. Not a dismissal path.

---

## 1. Termination Paths (UI → Handler → Pool)

### Path A — Agent tab close button (×)
```
web_ui/app.js:3425-3429 (closeBtn.onclick, non-primary agent tabs only)
  └─ send({ type: 'terminate_agent_instance', instance_name: name })
      └─ api_server.py:1124  @app.websocket("/ws/chat")  ← the ONLY WS endpoint
          └─ api_server.py:1158  await handler.dispatch(data)
              └─ ws_handlers.py:84  'terminate_agent_instance' → self.handle_terminate
                  └─ ws_handlers.py:503-544  handle_terminate()
                      ├─ (root guard: blocks orchestrator, transitions to IDLE, returns)
                      ├─ enqueue feedback to parent if any
                      └─ ws_handlers.py:542  self.agent_pool.dismiss_instance(instance_name)
                          └─ agent_pool.py:1083  dismiss_instance()
                              ├─ cascade-dismiss children (recursive, :1091-1097)
                              ├─ terminate_instance() if active (:1110) → inst.terminate() sets is_terminated=True
                              ├─ wake SLEEPING parent if async child (:1119-1143)
                              ├─ join thread (_instance_threads.pop + join w/ dismiss_thread_join_timeout) (:1149-1165)
                              ├─ discard from terminated_instances ONLY if thread confirmed stopped (:1167-1170)
                              └─ remove_instance() (:1173) → pool removal + callbacks → UI tab removal
```

### Path B — ⚠️ Terminate button (chat toolbar)
```
web_ui/app.js:4688-4702 (terminateBtn click)
  ├─ guard: refuses root orchestrator (isSessionPrimaryAgent → alert, return)
  ├─ confirm() dialog
  └─ send({ type: 'terminate_agent_instance', instance_name: activeInstance })
      → IDENTICAL downstream path as Path A (server-side is the same handler)
```

### Path C — `terminate_sub_agent` alias
```
ws_handlers.py:85  'terminate_sub_agent' → self.handle_terminate  (same handler)
No current UI producer found for 'terminate_sub_agent' — it is an alias retained for API compatibility.
Any client sending it lands on the same dismiss_instance() code path.
```

### Verification: server dispatch is unique
- Only **one** WebSocket route: `@app.websocket("/ws/chat")` (api_server.py:1124).
- Received JSON → `WsMessageHandler(...).dispatch(data)` (api_server.py:1153-1158) → dispatch table lookup (ws_handlers.py:77-108).
- Both `terminate_agent_instance` and `terminate_sub_agent` map to `handle_terminate` (ws_handlers.py:84-85).
- No `@app.post/get/delete` REST endpoint names contain terminate/dismiss (verified: api_server.py REST routes are keys/handshake/message/status/agents/state/reset/approve/reject/resume_all/sessions/file/telemetry/endpoints, api_router.py methods, e2e message inject) — **none remove agents from the pool**.
- The legacy `agent_server/workstation_server.py` contains **zero** terminate/dismiss/remove references (grep: no matches).

---

## 2. The Fixed `dismiss_instance()` (agent_pool.py:1083-1173)

The zombie-thread fix (commits `961baff`, `3fef78e`) is present in the committed HEAD:

| Step | Lines | Behavior |
|---|---|---|
| Cascade children | 1091-1097 | recursive `dismiss_instance(child)` first |
| Active? → terminate | 1108-1116 | `terminate_instance(set_global_stopped=False)` → adds to `terminated_instances`, `inst.terminate()` sets durable `is_terminated=True`; else terminate non-active directly |
| Sleeping-parent wakeup | 1119-1143 | async child dismissal wakes SLEEPING parent |
| Clear state label | 1145-1147 | |
| Thread join | 1149-1165 | pops `_instance_threads[instance_name]` and `join(timeout=dismiss_thread_join_timeout)` (default 2.0s, configurable via settings) |
| **Signal discard guard** | 1167-1170 | `terminated_instances.discard()` **only if `thread and not thread.is_alive()`** — fixes the previous unconditional-discard bug |
| Pool removal | 1173 | `remove_instance()` → UI tab goes away |

Key fix details confirmed:
- `remove_instance()` **no longer discards** from `terminated_instances` (comment at agent_pool.py:895-897: "keep the signal alive until the thread confirms it stopped via join in dismiss_instance()").
- The `is_instance_terminated()` check reads **both** `terminated_instances` set AND `inst.is_terminated` flag (agent_pool.py:2718-2730), so the durable signal survives pool removal.
- `inst.terminate()` sets `self.is_terminated = True` (agent_instance.py:628) — durable per the dataclass field.
- Thread registration happens in `run_agent_thread_unified` **before instance creation** (run_agent_unified.py:103-107) — closes the registration race documented in `.agent_lessons/dismiss-thread-join-gaps.md` Gap 3.
- Working tree matches committed HEAD for `agent_pool.py` (only `.qwen/skills-metrics.json` is dirty), so the code I traced is what will run.

---

## 3. All Other Code Paths That Remove/Modify Agents (audit results)

### Pool removal without `dismiss_instance`
| Location | What it does | Bypasses fix? | Assessment |
|---|---|---|---|
| `agent_pool.py:2017` (`load_session_from_log`) | `self.instances.pop(instance_name, None)` then replaces with fresh instance under `_execution._state_lock` | Removal bypasses `remove_instance()` (no callback/logger cleanup for old inst) | **Not a UI termination path.** Same-name in-place swap during session load. Does NOT touch `terminated_instances` or `_instance_threads`, so no zombie-thread risk from the UI-termination perspective. Documented caveat: old thread refs are superseded via `_run_generation` guard in `run_agent_unified.py:94`. |

### Other `dismiss_instance()` callers (all legitimately go through the fixed method)
| Caller | Trigger |
|---|---|
| `ws_handlers.py:542` | **UI terminate** (this investigation) |
| `agent_pool.py:1259` `reset()` | Session reset — dismisses all sub-agents via `dismiss_instance` |
| `agent_pool.py:1373` `clear_sub_agents()` | Pre-session-load cleanup |
| `agent_pool.py:1497` `_dismiss_all_instances()` | Full wipe before loading another session |
| `agent_pool.py:2653` async child error cleanup | Zombie instance cleanup on slot timeout/failure |
| `agent_pool.py:3156` `IdleManager._auto_dismiss()` | Idle auto-dismissal (background checker) |
| `tool_dispatcher.py:421,462` `dismiss_agent` tool | LLM-initiated dismissal (root can dismiss any; agents only their own children) |

### Other `terminate_instance()` callers (do NOT remove from pool)
| Caller | Trigger | Notes |
|---|---|---|
| `execution_engine.py:2238` | max_auto_rollbacks exceeded | Marks terminated only (no pool removal) |
| `agent_pool.py:1013,1110` | internal (cascade + dismiss) | |

### `terminated_instances` set — all production mutations (no UI bypass)
- `add`: `agent_pool.py:1020` (in `terminate_instance`)
- `discard`: `agent_pool.py:1170` (in `dismiss_instance`, guarded by thread-stopped check)
- `clear`: `agent_pool.py:1282` (`reset()`), `agent_pool.py:1390` (`_clear_all_state_dicts`)
- `update`: `agent_pool.py:1426` (`_restore_instance_state`, restore after load)
- **No direct manipulation from ws_handlers, api_server, app.js, or any UI-layer code.**

### `stop` is NOT a termination path
`handle_stop` (ws_handlers.py:290-370) transitions active agents to IDLE and calls `stop_session()` — instances stay in the pool, tabs stay. It also cleans the active_stack of `terminated_instances` entries (ws_handlers.py:352-359) — read-only w.r.t. the set.

---

## 4. Answers to the Specific Questions

1. **Where is `terminate_agent_instance` handled?** → `ws_handlers.py:84` dispatch-table entry → `WsMessageHandler.handle_terminate` (ws_handlers.py:503). Wired from the single WS endpoint at `api_server.py:1124-1158`.
2. **Does it call `pool.dismiss_instance()`?** → **Yes** (ws_handlers.py:542). One line, no intermediate wrapper.
3. **Tab close vs. Terminate button — same path?** → **Yes.** Both send the identical `terminate_agent_instance` message (app.js:3427 and app.js:4699). Two entry points, one server path. The only UI-side difference is the root guard (Terminate button blocks root; tab close button is simply never rendered on the primary tab).
4. **Does every entry point reach fixed `dismiss_instance()`?** → **Yes.** All three (tab-close, button, `terminate_sub_agent` alias) reach `dismiss_instance()` at ws_handlers.py:542.

### Extra checks
- **Direct `terminated_instances` manipulation from UI layer?** → **None found.** All mutations are inside `agent_pool.py`.
- **Other code paths removing agents without `dismiss_instance`?** → Exactly one: `load_session_from_log()` in-place instance swap (agent_pool.py:2017) — not UI-triggerable as a "terminate", no zombie-thread implications (no `terminated_instances`/thread interplay), documented above.

---

## 5. Remaining Caveats / Open Questions

1. **Join timeout is cooperative, not forceful** (already known): `thread.join(timeout=2.0)` only *waits*; if the agent thread is stuck in a non-interruptible blocking call (e.g., `time.sleep(120)`, plain network read), the 2s join times out and logs a warning (agent_pool.py:1157-1161). The termination signal stays active, so the agent stops at the next cooperative check — but the dismissal returns promptly rather than guaranteeing dead-thread. This is by design (configurable `dismiss_thread_join_timeout`) but is a known limitation documented in `.agent_lessons/dismiss-thread-join-gaps.md`.
2. **Async child executor workers** are still not registered in `_instance_threads` (they run via `ThreadPoolExecutor` in `run_child_agent`). For those, `dismiss_instance` sees no thread → skip join → signal discarded only if thread entry missing AND per the guard (`thread and not thread.is_alive()`) — actually since `thread` is None the discard is skipped, keeping the terminated signal. The `inst.is_terminated=True` durable flag plus `is_instance_terminated()` (which checks the flag) covers the worker's stop-checks. The `_instance_threads` registration gap for async children remains (Gap 2 in the lesson), but the *signal-discard* part of that gap is fixed by the current guard logic.
3. **Not verified at runtime** — this trace is static code-path analysis against committed HEAD. A live UI test (open tab → close tab → confirm thread terminates, e.g., via `tests/test_dismiss_real_thread.py`) would close the loop. Recommend running `tests/test_dismiss_real_thread.py` which exercises real threads.

---

## 6. Recommendation

- **Accept**: All UI termination paths demonstrably reach the fixed `dismiss_instance()`.
- **Optional hardening**: Register async child executor workers in `_instance_threads` (or ensure `run_child_core` also joins/discards), to close the remaining Gap 2 from `.agent_lessons/dismiss-thread-join-gaps.md`, so async-children dismissal also gets thread-join visibility.
- **Suggested test**: Run `pytest tests/test_dismiss_real_thread.py` plus a manual UI smoke test (spawn a sub-agent, click ×, verify no zombie thread via logs `"Waiting for '<name>' thread to stop..."`).

---

## Confidence & Method

- **Confidence: High** for the path trace (single WS endpoint verified, single handler verified, all UI producers enumerated by grep, committed code matches working tree).
- **Method**: grep-based call-graph tracing across `web_ui/app.js`, `api_server.py`, `ws_handlers.py`, `agent_pool.py`, `agent_instance.py`, `run_agent_unified.py`, `tool_dispatcher.py`, `execution_engine.py`, `async_tools.py`, `child_runner.py`, `api_router.py`, `agent_server/`; git log/show inspection of the two fix commits; verification that `agent_pool.py` working tree == HEAD.
- **Limitations**: static analysis only; no runtime/UI interaction performed in this session.