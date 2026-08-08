# Investigation Report: "Max Auto-Rollbacks (-1=∞)" Setting

**Investigator:** researcher (investigator_rollback)
**Date:** 2026-08-08
**Repo:** N:\work\WD\AgentCascade
**Task source:** Bug report from Maine (todo.md line 83):
> "the UI setting `Max Auto-Rollbacks (-1=∞)` is supposed to give us the max
> allowed message loops before the agent gets kicked back to caller. doesnt
> seem to work... also probably needs a better name"

---

## Executive Summary

The setting **is fully wired end-to-end for definition, UI, persistence, and
serialization** — but it has **no enforcement consumer**. The value is read
into a local variable in `run_agent_unified.py` and passed to a recovery
wrapper, but the actual loop-handling code inside the execution engine never
reads it. Instead, the engine performs loop rollback **inline** with a
**hardcoded threshold of 3** that only logs a warning — it never kicks the
agent back, terminates it, or escalates to the caller.

The setting was orphaned by commit `44d8b58` ("fix: simplify loop rollback —
inline rollback eliminates retry loop and message duplication", 2026-07-01),
which replaced the old exception-based retry loop with inline rollback. The
old retry-loop code (`run_agent_in_pool_with_recovery`) still exists and still
accepts `max_auto_retries`, but `LoopDetectedError` is **never raised** in the
production code path, so the retry loop is dead code.

Additionally, there is a **wiring bug**: the config handler clamps the value to
`[0, 10]`, so the UI's advertised `-1=∞` is impossible to persist (the UI
itself allows -1 to 100, but the backend silently clamps the stored value).
The enforcement-side mapping (`run_agent_unified.py`) does handle -1 correctly
(999_999), but that code has no effect since nothing reads the value.

---

## 1. Where the setting is DEFINED

| Location | File:Line | Notes |
|---|---|---|
| Env var default | `agent_cascade/settings.py:83-84` | `AGENT_MAX_AUTO_ROLLBACKS = int(os.getenv('QWEN_AGENT_MAX_AUTO_ROLLBACKS', 3))` |
| PoolSettings field | `agent_cascade/agent_instance.py:622` | `max_auto_rollbacks: int = AGENT_MAX_AUTO_ROLLBACKS` — comment "Max loop recovery retries" |
| Related toggle | `agent_cascade/agent_instance.py:634` | `auto_rollback_on_loop: bool = True` — "Auto-rollback on detected loops (loop recovery toggle)" |
| Config key registries | `agent_cascade/constants.py:108-109` | In `NON_LLM_KEYS` (never sent to LLM API) |
| | `agent_cascade/config_handlers.py:31-32` | In `POOL_SETTINGS_KEYS` (persists to pool_settings.json) |

`PoolSettings` is a `@dataclass`; `to_dict`/`from_dict` at
`agent_instance.py:689-716` serialize ALL fields including
`max_auto_rollbacks`.

## 2. Where it is EXPOSED in the UI

| File:Line | Element |
|---|---|
| `web_ui/index.html:641` | Label: `Max Auto-Rollbacks (-1=∞)` |
| `web_ui/index.html:642` | `<input type="number" id="setting-max-rollbacks" min="-1" max="100" value="3" />` |
| `web_ui/index.html:547-548` | Sibling toggle: "Auto-Rollback on Loop" (`#setting-auto-rollback`) |
| `web_ui/app.js:138` | `POOL_SETTINGS_MAP` entry: id → `#setting-max-rollbacks`, key `max_auto_rollbacks`, localKey `max-rollbacks` |
| `web_ui/app.js:1110-1111` | Restore value on load: `if (s['max_auto_rollbacks'] !== undefined) ... .value = s['max_auto_rollbacks']` |
| `web_ui/app.js:4699` | Save: `if ($('#setting-max-rollbacks')) cfg.max_auto_rollbacks = parseInt($('#setting-max-rollbacks').value);` |
| `web_ui/app.js:139` | `POOL_SETTINGS_MAP` for toggle (key `auto_rollback_on_loop`) |
| `web_ui/app.js:4684, 1114-1115` | Toggle save/restore |

## 3. How it is PERSISTED

**UI → server flow** (`ws_handlers.py:709-741`, `handle_update_config`):
1. UI sends `{ type:'update_config', generate_cfg: {...} }`
2. Stored into `session['generate_cfg']` (ws_handlers.py:719-721)
3. Dispatched via `ConfigUpdateRouter.apply()` → `config_handlers.py:752-780`,
   which calls `CONFIG_HANDLERS[key]` for each present key.

**Handler** — `config_handlers.py:459-464`:
```python
@register_config_handler('max_auto_rollbacks')
def _handle_max_auto_rollbacks(ui_cfg, agent_pool, agents):
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('max_auto_rollbacks', 3))
        agent_pool.settings.max_auto_rollbacks = max(0, min(val, 10))   # ⚠️ clamps [0,10]
```
**BUG #1**: `max(0, min(val, 10))` discards `-1` (∞) and any value > 10. The
UI declares `min="-1"` but the backend clamps -1 → 0.

**Persistence** (`agent_pool.py`):
- `_save_pool_settings()` (lines 362-421) writes `self.settings.to_dict()` to
  `config/pool_settings.json`.
- `_load_pool_settings()` (lines 423-502) reads it back via `PoolSettings.from_dict`.
- Startup persistence hook: `api_server.py:1236-1237`, `shared_init.py:164-165`.
- Persist trigger: `ws_handlers.py:728-731` — key present in
  `POOL_SETTINGS_KEYS`/`EXTRA_PERSIST_KEYS`.

**Back to UI** (`api_integration.py`):
- `pool_settings` dict built for the frontend sync includes
  `'max_auto_rollbacks': getattr(ps, 'max_auto_rollbacks', 3)` (lines 833, 1012)
  and `'auto_rollback_on_loop'` (lines 834, 1013). Two nearly identical
  serialization blocks (state builders).

## 4. Where it is SUPPOSED to be CONSUMED / ENFORCED

### 4a. The dead wrapper path
- `run_agent_unified.py:116-118` reads the setting:
  ```python
  max_auto_retries = ui_cfg.get('max_auto_rollbacks', 3)
  if max_auto_retries == -1:
      max_auto_retries = 999_999
  ```
  → passes to `run_agent_in_pool_with_recovery(...)` at `run_agent_unified.py:139-144`.
  Note: this uses `ui_cfg` (session `generate_cfg`), not `pool.settings` — so
  even if the pool value is persisted, the *session* UI config is what's used,
  and it's clamped by the handler above before it's saved to the session. The
  session value comes from the UI directly (before clamping), so this path
  could see -1 — but it never matters because the wrapper is dead code.

- `run_agent_in_pool_with_recovery` (`api_integration.py:414-483`):
  - Docstring (line 429): "max_auto_retries: Max retry attempts (default 3). -1 for unlimited."
  - `retry_limit = sys.maxsize if max_auto_retries == -1 else max_auto_retries` (line 437)
  - `for attempt in range(retry_limit + 1):` (line 439)
  - Catches `LoopDetectedError` (line 443) → surgical rollback + hint → retry/error.
  - **BUT `LoopDetectedError` is NEVER raised in production code.** Verified via
    repo-wide grep: `raise LoopDetectedError` appears only in tests
    (`tests/test_loop_detection.py`) and audit plan docs. The class docstring in
    `loop_detection.py:32-45` says: *"Kept for backward compatibility
    with existing tests. No longer raised by the main codebase — loop
    detection is now handled inline inside engine.run()."*

- Child agents: `child_runner.py:58-68` keeps `max_auto_retries: int = 3`
  but its docstring (lines 77-78) explicitly says "Kept for backward
  compatibility (no longer used; rollback happens inline inside engine.run()
  up to 3 times)". Both `agent_pool.py:2559` and `tool_dispatcher.py:560`
  call `run_child_core` WITHOUT passing any retry value → default 3, unused.

### 4b. The REAL enforcement path (inline, hardcoded)
`ExecutionEngine._pre_llm_checks` (`execution_engine.py:2163-2198`), called
from `engine.run()` at line 2052 **before** each LLM call:

```python
if not getattr(instance, '_suppress_loop_detection_next_turn', False):
    loop_info = _canonical_detect_loop(messages)
    if loop_info:
        reason, pop_count = loop_info
        ...
        rollbacks = getattr(instance, '_loop_rollback_count', 0) + 1
        instance._loop_rollback_count = rollbacks
        self._inline_rollback_and_hint(...)
        if rollbacks >= 3:                       # ⚠️ HARDCODED 3
            logger.warning(f"Loop recovery for {inst_name}: rolled back {rollbacks} times without success. Continuing.")
        ...
        return True   # continue loop with fresh state
```

Key facts:
- `instance._loop_rollback_count` is reset to 0 at the start of each
  `engine.run()` execution (line 1130) and after compression cooldown
  (lines 2207-2208).
- The hardcoded `rollbacks >= 3` at line 2183 **only logs a warning** and then
  continues (`return True` line 2198). There is **no termination**, no
  "kick back to caller", no error raise. It matches the exact bug report:
  "doesn't seem to work".
- `pool.settings.max_auto_rollbacks` and `pool.settings.auto_rollback_on_loop`
  are **never referenced in execution_engine.py** (grep = 0 matches).
- The rollback performed is `_inline_rollback_and_hint()`
  (execution_engine.py:2302-2341) which calls `pool._rollback_instance` and
  appends a hint message — this is *not* `surgical_rollback` alias but
  `_rollback_instance`; the alias wrapper `surgical_rollback`
  (agent_pool.py:2787-2795) calls the same.

## 5. Actual enforcement: NO

**Verdict: The setting is defined, saved, and displayed but never enforced.**
The only "limit" that exists is a hardcoded `_loop_rollback_count >= 3` inside
execution_engine.py:2183, and even that only logs a warning — it never kicks
the agent back to the caller, never raises, and never terminates. The agent
continues looping with rollback+hint until `max_turns` (or another mechanism)
stops it — but see §7 for why `max_turns` may not even catch loop cycles.

Timeline evidence: commit `44d8b58` (2026-07-01) "fix: simplify loop rollback —
inline rollback eliminates retry loop and message duplication" removed the
raising of `LoopDetectedError` and with it the consumer of `max_auto_retries`
in `run_agent_in_pool_with_recovery`. The wrapper now simply
`yield from run_agent_in_pool(...)` (line 121 of that diff context) with no
retry. The parameter was kept as "backward compatibility," but nothing reads
it.

## 6. What "message loop" means / How detection works

**Inter-turn (this setting's domain):** `loop_detection.detect_loop()`
(`loop_detection.py:48-187`):
- Threshold: needs `len(messages) >= 6`.
- Feature extraction per message: role + content (truncated to 3000 chars),
  function calls use `role:name:args`, function results use `role:toolname:
  snippet+hash` (lines 73-124).
- Window: last 40 non-system messages (line 127).
- Pattern search: lengths L=1..20, requiring K repeats (K=3 for L<5, K=2 for
  L≥5), searching backwards (lines 141-165).
- False-positive guards: single FUNCTION/USER patterns, all-FUNCTION blocks.
- Returns `(reason, pop_count)`; pop_count = # messages from end belonging to
  the loop (lines 178-180).

- **"Message loop" ≈ repeated sequence of conversation messages** (assistant
  + tool responses), detected before an LLM call. When found, the engine
  rolls back `pop_count` messages and injects a SYSTEM hint.

- `_canonical_detect_loop` is imported in execution_engine.py:92 and invoked
  at line 2164.

- Note: there is a *separate* inner-loop (streaming) detector
  (`inner_loop_detect.py`, `_handle_inner_loop_detection` at
  execution_engine.py:2648-2702) that retries on degenerate LLM streams. Its
  budget is **from `pool.settings.retry_max_attempts`** (the retry policy
  setting, default 6 per config_handlers.py:373-391; execution_engine.py:2682
  uses `_max_attempts` from `pool.settings.retry_max_attempts`) — not
  `max_auto_rollbacks`. It does not remove/replay messages, it just retries
  the LLM endpoint. So it is a *different* mechanism that is correctly
  wired (but wrongly named in some places).

## 7. Overlapping/conflicting related settings

| Setting | Location | What it does | Overlap w/ max_auto_rollbacks |
|---|---|---|---|
| `max_turns` (default 50) | `agent_instance.py:633`, engine run loop at `:1257-1263` | Turn budget for one execution | **PROBLEM**: loop-rollback path `return True`s in `_pre_llm_checks` (execution_engine.py:1298 continues BEFORE `turns_available -= 1` at line 1343). So each loop-detected cycle does NOT decrement the turn counter — a perpetually looping agent may never deplete `max_turns`. |
| `auto_continue` (+`MAX_AUTO_CONTINUE_ATTEMPTS=5`, settings.py:27) | engine :3809-3827 | resets turn counter on truncation | Different trigger (truncation), hardcoded cap 5. Also interacts with `max_turns` reset at :1441-1446. |
| `auto_rollback_on_loop` | `agent_instance.py:634`; config handler :467-471; UI toggle | intended to disable rollback | **IGNORED**: execution_engine.py has 0 references. Inline rollback always fires. |
| `inner_loop_detect_enabled` + sub-toggles | `agent_instance.py:637-657` | streaming-level detector | separate mechanism, correctly wired to `retry_max_attempts` via _handle_inner_loop_detection (execution_engine.py:2648-2702) |
| `retry_max_attempts`/`endpoint_max_retries` | (`config_handlers.py:373-411`) | endpoint retry budget | not loop-rollback-related |
| `compression_*` | siptings.py, config_handlers | context management | loop suppression cooldown after compression: `_suppress_loop_detection_next_turn` (execution_engine.py:2163, 2199-2208) |

Other notes:
- `MAX_AUTO_CONTINUE_ATTEMPTS = 5` is also hardcoded (settings.py:27) — same
  "magic number" anti-pattern.
- `compression/handler.py:1118` uses `getattr(pool.settings,'rollback_max_count', 50)` —
  a related but distinct cap on `/rollback`/retry message count.

## 8. Naming problem (from the bug report)

- The name "Max Auto-Rollbacks" is misleading; the setting is intended to cap
  **loop recovery attempts** for a run/turn, i.e., "max loop recovery attempts"
  or "loop retry limit". It currently sounds like it limits how many messages
  can be rolled back in one go (which is actually governed by
  `rollback_max_count`/`_rollback_instance` and the 50% cap in docs).
- The `-1=∞` semantics are broken by the backend clamp (see §3), so even the
  name is not honored.
- Suggested: `max_loop_recoveries` / "Max Loop Recoveries (-1=∞)" or
  "Loop Recovery Limit (per run)". Whatever name is chosen, the local
  variables (`max_auto_retries`) and wrapper param
  (`max_auto_retries`) should be renamed consistently — or the whole legacy
  wrapper removed if the inline path becomes the sole authority.

---

## Root Cause Summary

1. **Setting is orphaned**. Commit 44d8b58 removed the only enforcement
   (`LoopDetectedError` raising + retry loop) in favor of inline recovery,
   but never moved the `max_auto_rollbacks` consumption into the inline path.
2. **Hardcoded 3** in `execution_engine.py:2183` is the only *de facto*
   cap, and it doesn't enforce anything (warning only).
3. **`auto_rollback_on_loop` toggle is also dead** — the inline path always
   rolls back regardless of the checkbox.
4. **Backend clamp breaks -1** — `config_handlers.py:464` clamps to [0,10].
5. **max_turns doesn't backstop** — loop-detect continues don't decrement the
   turn counter.

## Recommended Fix Direction (for planning; not implemented)

1. In `execution_engine.py:2163-2198`, replace the hardcoded `rollbacks >= 3`
   with `pool.settings.max_auto_rollbacks`:
   - If limit >= 0 and `rollbacks >= limit` → raise `LoopDetectedError`
     (reconnecting the existing `run_agent_in_pool_with_recovery` retry loop,
     which then yields the "[SYSTEM]: Loop detected ... loop recovery failed"
     message to the caller) — OR terminate the run with a clear system message.
   - Respect `auto_rollback_on_loop` (skip inline rollback when disabled).
2. Fix clamp in `config_handlers.py:464` to accept -1 (e.g.,
   `max(-1, min(val, 10))`) and update UI/backend docs.
3. Consider whether `run_agent_unified.py:116-118` should read
   `pool.settings.max_auto_rollbacks` (single source of truth) instead of the
   raw `ui_cfg` copy.
4. Rename the setting (docs + UI + key or keep key and only rename label).
5. Add a regression test that sets `max_auto_rollbacks=1` and verifies the
   agent is kicked back to the caller after one loop.

## Confidence

- **Verified** for: definition, UI wiring, persistence, serialization, and
  the absence of any consumer in `execution_engine.py` (grep = 0), the
  hardcoded `3`, the never-raised `LoopDetectedError`, the clamp bug, and
  commit 44d8b58 as the regression point.
- **High confidence** for: `max_turns` non-decrement interaction (code path
  analysis), dead wrapper param (docstring + grep).
- **Low/Open**: whether any *runtime* log/telemetry shows real looping agents
  being stopped by other mechanisms in practice (would require repro), and
  whether the user wants restore-old semantics or new inline semantics.

## Files touched / created

- Report: `investigation_report_max_auto_rollbacks.md` (this file)
- Memory: `.agent_lessons/max-auto-rollbacks-not-enforced.md`

## Handoff / Next Actions

1. (Recommended) Fix execution_engine inline path to consume
   `pool.settings.max_auto_rollbacks` and `auto_rollback_on_loop`.
2. Fix `config_handlers.py:464` clamp.
3. Rename setting (UI label + maybe key).
4. Add tests in `tests/test_loop_detection.py` for the new inline enforcement.
5. Run `tests/test_loop_detection.py` after changes.