# Inventory: `async_mode` → `execution_mode` (tri-state enum) hard-break rename — shell_cmd tool

Scope: `N:\work\WD\AgentCascade`. Research only, no code written.
Date: 2026-08-29. Investigator: researcher shell_research1 (for Maine).

## Executive Summary

- The `async_mode` **boolean** param of the `shell_cmd` tool is defined in exactly **2 production files**
  (`agent_cascade/tools/custom/shell_cmd.py`, `agent_cascade/prompts/dna.py`) plus 1 comment
  (`agent_cascade/pool/message_queue.py`).
- Runtime dispatch: `ShellCmd.call()` in `shell_cmd.py` (lines 215–276). "Omitted" is detected by
  **key-presence in the parsed params dict** (`'async_mode' not in params`), NOT by schema default.
  This is the crux for the new `auto` default (see §7).
- Tests exercising shell_cmd `async_mode` / sync-vs-async decision: `tests/test_async_shell_cmd.py`
  (decision logic: lines 828–874; ~40 mechanical `async_mode: true` call sites), `tests/test_shell_cmd_cwd_resolution.py`
  (6 sites). `tests/test_async_shell_kill.py` and `tests/test_async_shell_failure_scenarios.py` do **NOT**
  reference `async_mode` (they drive the tracker directly).
- ⚠️ Important disambiguation: `tests/test_e2e_agent_calls.py` has 9 `"async_mode": True` occurrences, but
  all are in **`call_agent`** mock args — a *different tool*. `call_agent` has **no** `async_mode` param
  (verified in `dna.py` 408–463; also documented in `.agent_lessons/e2e-stress-tests-obsolete-conc0-always-sync.md`).
  These are **out of scope** for this rename (and those tests are already marked obsolete/skipped).
- JSON validation: `BaseTool._verify_json_format_args` → `jsonschema.validate` (no `additionalProperties:false`).
  Verified empirically: unknown params are **silently ignored** (old `async_mode` would be a silent no-op, not an
  error); wrong type is rejected; `enum` rejects invalid values; omitted params pass. See §8.

## 1. Schema definition — `agent_cascade/tools/custom/shell_cmd.py`

`ShellCmd.parameters['properties']` block (lines 124–161). The `async_mode` entry:

```python
144:             'async_mode': {
145:                 'type': 'boolean',
146:                 'default': False,
147:                 'description': TOOL_METADATA['shell_cmd']['parameters']['async_mode']
148:             },
```
- type: `boolean`
- default: `False` (note: schema `default` is **never read at runtime** — dispatch uses key presence, §7)
- description: pulled from `TOOL_METADATA['shell_cmd']['parameters']['async_mode']` in `prompts/dna.py`
- Required list (line 160): `['command']` — `async_mode` is optional.

## 2. Runtime dispatch logic — `ShellCmd.call()`, same file

```python
212:         timeout = params.get('timeout')  # None means use default (30s sync / 3600s async)
213:
214:         # ── Parse new async parameters ──────────────────────────────
215:         async_mode = bool(params.get('async_mode', False))
216:         heartbeat_interval = float(params.get('heartbeat_interval', -1))
217:         tool_id = params.get('tool_id')
218:
219:         # Auto-async mode: if timeout exceeds threshold and async_mode not explicitly set
220:         if timeout is not None and timeout > AUTO_ASYNC_TIMEOUT_THRESHOLD and 'async_mode' not in params:
221:             async_mode = True
222:             logger.info(f"Auto-async mode triggered for shell_cmd: timeout={timeout}s")
223:             # Default heartbeat for auto-async unless explicitly set
224:             if 'heartbeat_interval' not in params:
225:                 heartbeat_interval = DEFAULT_AUTO_ASYNC_HEARTBEAT
```
- **(a) auto-async trigger (timeout > 60):** line 220. Threshold = `AUTO_ASYNC_TIMEOUT_THRESHOLD = 60`
  (`agent_cascade/settings.py:400`); companion `DEFAULT_AUTO_ASYNC_HEARTBEAT = 30` (line 401),
  `ASYNC_SHELL_DEFAULT_TIMEOUT = 3600` (line 395).
- **(b) explicit override:** line 215 — `bool(params.get('async_mode', False))`. An explicit `async_mode=false`
  with timeout>60 stays sync because line 220 requires `'async_mode' not in params`; an explicit `true` with
  timeout≤60 goes async (line 215 alone).
- **(c) "omitted" detection:** line 220 — `'async_mode' not in params` (key presence in the **parsed dict**
  returned by `_verify_json_format_args`, base.py:147–179). The schema `default: False` is never consulted.
- Dispatch fork:
```python
256:         if async_mode:
...
260:             return self._launch_async(...)   # lines 256–267
...
270:         return self._execute_sync(...)        # lines 269–276 (sync default)
```
- Also line 361 (inside `_launch_async`): approval-dialog payload hard-codes the param name:
```python
361:                 tool_args={'command': command, 'justification': justification, 'cwd': cwd, 'async_mode': True},
```
- Control-command path (lines 239–253): when `tool_id` is present, routes to `_handle_control_command`
  **before** the async_mode fork — control calls never re-enter sync/async decision.

## 3. TOOL_METADATA — `agent_cascade/prompts/dna.py` (`'shell_cmd'` block, lines 318–337)

```python
323:             '**ASYNC:** Set async_mode=true (or timeout>60 auto-triggers it, unless async_mode=false) for long-running commands. '
332:             'timeout': 'Optional timeout in seconds. Use a higher value for long-running commands. Values over 60s are treated as async unless async_mode=false is set explicitly (which forces sync). Default: 30s.',
333:             'async_mode': 'Run the command in background and return immediately with tool_id + PID. The agent continues working while the command runs. Heartbeat updates are injected as user messages at intervals. Set to false to enforce blocking/synchronous execution regardless of timeout value.',
334:             'heartbeat_interval': 'Seconds between heartbeat output updates (-1 means only notify on completion, 0 or positive = periodic heartbeats). Only effective when async_mode=true. Default: -1.',
335:             'tool_id': 'Reference an existing running shell by its tool_id to send input, update settings, or kill it. Returned in the initial response when launching with async_mode=true.'
```
- Line 323 is inside the tool **`description`** (lines 319–327). Lines 332–335 are inside **`parameters`**
  (block spans 328–336). All 5 lines must be rewritten for `execution_mode` (auto/sync/async).

## 4. Test files

### `tests/test_async_shell_cmd.py` (primary)
Decision-logic tests (semantically coupled to the rename — **must be rewritten**):
- L828 `test_timeout_gt_60_auto_switches_to_async` — timeout=120, no async_mode → expects `tracker.launch`
- L834 `test_timeout_gt_60_with_explicit_async_mode_false_stays_sync` — L838 passes `"async_mode": false` + timeout 120 → expects `_execute_sync`
- L842–849 `test_timeout_at_or_below_60_stays_sync` (parametrized 30/60) → sync
- L851 `test_explicit_async_mode_true_ignores_timeout_threshold` — L853 `"async_mode": true`, timeout 1 → async
- L857 `test_auto_async_defaults_heartbeat_to_30` (auto-async heartbeat default)
- L867 `test_auto_async_respects_explicit_heartbeat`

Mechanical `async_mode: true` call sites (need name swap; semantics unchanged — explicit async):
L275, 286, 305, 317, 329, 343, 357, 371, 379, 393, 411, 431, 449, 482, 526, 546, 562, 581,
606, 614, 620, 627, 639, 653, 666, 725, 740, 758, 778, 925 (all pass `"async_mode": true` in JSON to `shell_cmd_tool.call(...)`).

### `tests/test_shell_cmd_cwd_resolution.py`
L127, 143, 160, 169, 185, 202 — 6 sites, all `"async_mode": true` to force the async path for cwd-resolution assertions (mechanical swap).

### `tests/test_async_shell_kill.py`
**No `async_mode` references.** Drives `AsyncShellTracker.launch()` directly (L169, 222, 320, etc.) — unaffected.

### `tests/test_async_shell_failure_scenarios.py`
**No `async_mode` references.** Drives `tracker.launch()` directly (L69, 103, 133, 173, 206, 245, 293, 336, 388) — unaffected.

### `tests/test_e2e_agent_calls.py` — OUT OF SCOPE (different tool)
L636, 637, 700, 777, 829, 917, 931, 1000–1004 — 9 occurrences of `"async_mode": True`, all inside **`call_agent`**
mock args. `call_agent` has no `async_mode` param (dna.py 416–461: agent_class, instance_name, task, context,
log_file, max_turns, load_skill only) and the engine ignores it (`.agent_lessons/e2e-stress-tests-obsolete-conc0-always-sync.md`,
APPLIED 2026-08-24 → class marked `@pytest.mark.skip`). Do not touch in this rename; optionally clean up separately.

## 5. Other callers / docs / lessons

- `agent_cascade/pool/message_queue.py:216` — comment only: `# Also check async shell tasks (async_mode=true shell commands)`.
- `.agent_lessons/async_shell_test.md:12` — doc: `shell_cmd(async_mode=true, heartbeat_interval=3)` returns tool_id.
- `.agent_lessons/e2e-stress-tests-obsolete-conc0-always-sync.md:13,15,32` — about `call_agent` (out of scope); line 15 states "only shell_cmd has one".
- `reports/investigation_async_shell_heartbeat_timer.md:40` — stale line numbers (says lines 91–102; actual is 215–225).
- `reports/e2e_test_scheduling_analysis.md:125` — about `call_agent` (out of scope).
- `regression_e2e_20260829.log:652–659` — historical test-run log (regenerates; no action).
- No references in `docs/`, `README.md`, `examples/`, or any `agent_cascade/` production code beyond §1–§3.

## 6. JSON-schema validation layer (question 6)

`BaseTool._verify_json_format_args` (`agent_cascade/tools/base.py:147–179`):
- parses params (JSON string or dict) then, for dict-typed schemas: `jsonschema.validate(instance=params_json, schema=self.parameters)` (line 176).
- shell_cmd schema has **no `additionalProperties: false`** → **unknown params are silently accepted/ignored**.
- Verified empirically (jsonschema):
  - unknown param (e.g. old `async_mode` on new schema) → **accepted, ignored** (silent no-op, NOT an error)
  - `{"type":"boolean"}` + string value → **rejected** (type error)
  - `{"type":"string","enum":["auto","sync","async"]}` + valid → accepted; + invalid → **rejected**
  - omitted param → accepted
- Consequence for the hard break: because the schema `default` is never read at runtime, the new
  `execution_mode` **must be handled in dispatch** as `params.get('execution_mode', 'auto')` and the
  auto-trigger must test `if 'execution_mode' not in params and timeout > 60: mode = 'async'`
  (mirroring current lines 215/220). Old callers still sending `async_mode` will **silently run sync**
  (unknown param ignored) — that is the intended hard-break behavior, but it fails silently; consider a
  one-time deprecation warning if that risk is acceptable.

## 7. "Omitted vs explicit" detection — the crux for the new `auto` default

Currently, "omitted" is detected by **key presence in the parsed params dict** (`'async_mode' not in params`,
line 220), **not** by the JSON-schema `default` field (which is never read). The three-state semantics map cleanly:
- **omitted** (`'execution_mode' not in params`) → `auto` → async iff `timeout > 60`, else sync (+ heartbeat default 30 when async)
- **`"sync"`** → always `_execute_sync`
- **`"async"`** → always `_launch_async`
- **`"auto"`** (explicit) → identical to omitted
The existing key-presence pattern is exactly what makes `auto` as default behave correctly; keep the same
pattern and the schema `default: 'auto'` is purely documentary for the LLM.

## 8. Complete change inventory (file → lines)

| # | File | Lines | What |
|---|------|-------|------|
| 1 | `agent_cascade/tools/custom/shell_cmd.py` | 144–148 | schema entry: `boolean`/`default False` → `string`/enum `[auto,sync,async]`/`default auto` |
| 2 | `agent_cascade/tools/custom/shell_cmd.py` | 215, 219–225 | parse + auto-trigger + heartbeat default (tri-state logic) |
| 3 | `agent_cascade/tools/custom/shell_cmd.py` | 256, 269–276 | dispatch fork on `mode` |
| 4 | `agent_cascade/tools/custom/shell_cmd.py` | 361 | approval payload param name |
| 5 | `agent_cascade/prompts/dna.py` | 323, 332–335 | description + 4 parameter texts |
| 6 | `agent_cascade/pool/message_queue.py` | 216 | comment |
| 7 | `tests/test_async_shell_cmd.py` | 828–874 (decision), 275…925 (~40 mechanical) | rewrite decision tests; swap param name |
| 8 | `tests/test_shell_cmd_cwd_resolution.py` | 127,143,160,169,185,202 | swap param name |
| 9 | `.agent_lessons/async_shell_test.md` | 12 | doc |
| 10 | `reports/investigation_async_shell_heartbeat_timer.md` | 40 | stale doc (optional) |

**Out of scope (do NOT change):** `tests/test_e2e_agent_calls.py` (call_agent, different tool, already skipped),
`tests/test_async_shell_kill.py`, `tests/test_async_shell_failure_scenarios.py` (tracker-driven, no references).

## Confidence
- **Confirmed** — all line numbers read directly from source; jsonschema behavior verified by execution;
  call_agent disambiguation verified via dna.py schema + .agent_lessons note.
- **Assumption:** `AUTO_ASYNC_TIMEOUT_THRESHOLD` remains 60 (settings.py:400) for the new `auto` behavior.
