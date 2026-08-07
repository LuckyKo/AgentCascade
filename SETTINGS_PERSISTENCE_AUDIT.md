# AgentCascade Settings Persistence Audit

Date: 2026-08-07
Investigator: investigator_settings_persistence (researcher)
Scope: Every UI-backed setting in `web_ui/app.js` POOL_SETTINGS_MAP + web_ui/index.html settings panel,
cross-checked against localStorage save/load and server `pool_settings.json` save/load.

## Legend
- **Save** = written to localStorage (`agent-cascade-settings`) via saveSettings()
- **Load** = restored from localStorage on page load via loadSettings()
- **Persist** = written to `pool_settings.json` via `_save_pool_settings()`
- **Restore** = read from `pool_settings.json` on server start via `_load_pool_settings()`

## CRITICAL FINDING (root cause of reported reset)

All six **tool char-limit settings are stored in `pool.llm_cfg`, NOT in `PoolSettings`**.
`_save_pool_settings()` builds its payload from `self.settings.to_dict()` (PoolSettings dataclass) plus
a small explicit set of extras — it **never includes `pool.llm_cfg` values**. They are also
**not read back** in `_load_pool_settings()` (they are not PoolSettings fields, so `PoolSettings.from_dict()`
silently drops them).

Net effect: these settings change the live runtime and are present in localStorage (so the UI shows
correct values during the session), but they are **never written to `pool_settings.json`** and
**never restored on server restart**. After an abrupt shutdown / restart they fall back to factory defaults.

Affected (all broken at Persist AND Restore):
- `tool_result_max_chars`
- `grep_char_limit`
- `grep_spillover`  ← the one reported by the user
- `shell_char_limit`
- `code_char_limit`
- `list_dir_char_limit`

The comments in `config_handlers.py` (lines 46-48: *"stored in pool.llm_cfg but persisted via pool_settings.json"*)
state the intended design, but `_save_pool_settings()`/`_load_pool_settings()` never implemented it.

## Full Setting Table

| Setting (UI id) | localStorage key | Backend key | Save | Load | Persist | Restore | Status |
|---|---|---|---|---|---|---|---|
| `#setting-max-turns` | max-turns | `max_turns` (PoolSettings) | Y | Y | Y | Y | OK |
| `#setting-max-rollbacks` | max_auto_rollbacks | `max_auto_rollbacks` (PS) | Y | Y | Y | Y | OK |
| `#setting-auto-rollback` | auto_rollback_on_loop | `auto_rollback_on_loop` (PS) | Y | Y | Y | Y | OK |
| `#setting-agent-budgeting` | enable_agent_budgeting | `enable_agent_budgeting` (PS) | Y | Y | Y | Y | OK |
| `#setting-auto-continue` | auto-continue | `auto_continue` (PS) | Y | Y | Y | Y | OK |
| `#setting-max-parallel` | max_parallel_agents | `max_parallel_agents`→`max_workers` (PS) | Y | Y | Y | Y | OK |
| `#setting-inner-loop-detect` | inner-loop-detect | `inner_loop_detect_enabled` (PS) | Y | Y | Y | Y | OK |
| `#setting-loop-min-chars` | loop-min-chars | `loop_min_chars` (PS) | Y | Y | Y | Y | OK |
| `#setting-loop-max-chars` | loop-max-chars | `loop_max_chars` (PS) | Y | Y | Y | Y | OK |
| `#setting-loop-max-chars-enabled` | loop-max-chars-enabled | `loop_max_chars_enabled` (PS) | Y | Y | Y | Y | OK |
| `#setting-loop-char-run` | loop-char-run-enabled | `loop_char_run_enabled` (PS) | Y | Y | Y | Y | OK |
| `#setting-loop-char-run-limit` | loop-char-run-limit | `loop_char_run_limit` (PS) | **N** | **N** | N | Y | **BROKEN (dead control, front-end never sends it)** |
| `#setting-loop-two-phase` | loop-two-phase-enabled | `loop_two_phase_enabled` (PS) | Y | Y | Y | Y | OK |
| `#setting-loop-suspicion-threshold` | loop-suspicion-threshold | `loop_suspicion_threshold` (PS) | Y | Y | Y | Y | OK |
| `#setting-loop-confirm-required` | loop-confirm-required | `loop_confirm_required` (PS) | Y | Y | Y | Y | OK |
| `#setting-loop-cooldown-feeds` | loop-cooldown-feeds | `loop_cooldown_feeds` (PS) | Y | Y | Y | Y | OK |
| `#setting-enable-skills` | enable-skills | `default_load_skill_mode` (PS) | Y | Y | Y | Y | OK |
| `#setting-auto-skill-gen` | auto-skill-gen | `auto_skill_enabled` (PS) | Y | Y | Y | Y | OK |
| `#setting-retry-max-attempts` | retry-max-attempts | `retry_max_attempts` (PS) | Y | Y | Y | Y | OK |
| `#setting-endpoint-max-retries` | endpoint-max-retries | `endpoint_max_retries` (PS) | Y | Y | Y | Y | OK |
| `#setting-retry-base-delay` | retry-base-delay | `retry_base_delay` (PS) | Y | Y | Y | Y | OK |
| `#setting-retry-max-delay` | retry-max-delay | `retry_max_delay` (PS) | Y | Y | Y | Y | OK |
| `#setting-cache-pool-enabled` | cache-pool-enabled | `cache_pool_enabled` (PS) | Y | Y | Y | Y | OK |
| `#setting-cache-pool-size` | cache-pool-size | `cache_pool_size` (PS) | Y | Y | Y | Y | OK |
| `#setting-cache-threshold-chars` | cache-threshold-chars | `cache_threshold_chars` (PS) | Y | Y | Y | Y | OK |
| `#setting-tool-result-max-chars` | tool-result-max-chars | `tool_result_max_chars` (llm_cfg) | Y | Y | **N** | **N** | **BROKEN — not persisted to disk** |
| `#setting-grep-char-limit` | grep-char-limit | `grep_char_limit` (llm_cfg) | Y | Y | **N** | **N** | **BROKEN — not persisted to disk** |
| `#setting-grep-spillover` | grep-spillover | `grep_spillover` (llm_cfg) | Y | Y | **N** | **N** | **BROKEN — ROOT CAUSE (resetting)** |
| `#setting-shell-char-limit` | shell-char-limit | `shell_char_limit` (llm_cfg) | Y | Y | **N** | **N** | **BROKEN — not persisted to disk** |
| `#setting-code-char-limit` | code-char-limit | `code_char_limit` (llm_cfg) | Y | Y | **N** | **N** | **BROKEN — not persisted to disk** |
| `#setting-list-dir-char-limit` | list-dir-char-limit | `list_dir_char_limit` (llm_cfg) | Y | Y | **N** | **N** | **BROKEN — not persisted to disk** |
| `#settingApprovalTimeoutEnabled` | approval-timeout-enabled | `enable_approval_timeout` | Y | Y | Y | Y | OK |
| `#settingApprovalTimeoutSeconds` | approval-timeout-seconds | `approval_timeout_seconds` | Y | Y | Y | Y | OK |
| `#settingAsyncShellConsoleWindow` | async-shell-console-window | `enable_async_shell_console_window` | Y | Y | Y | Y | OK |
| `#setting-compression-warning-threshold` | compression-warning-threshold | `compression_warning_threshold` (PS) | Y | Y | Y | Y | OK |
| `#setting-compression-force-threshold` | compression-force-threshold | `compression_force_threshold` (PS) | Y | Y | Y | Y | OK |
| `#setting-compression-proactive-threshold` | compression-proactive-threshold | `compression_proactive_threshold` (PS) | Y | Y | Y | Y | OK |
| `#setting-compression-context-reserve-tokens` | compression-context-reserve-tokens | `compression_context_reserve_tokens` (PS) | Y | Y | Y | Y | OK |
| `#setting-compression-fraction` | compression-fraction | `compression_fraction` (module-level) | Y | Y | Y | Y | OK |

## Other settings panel controls (non-POOL_SETTINGS_MAP)

| UI control | localStorage | Backend persist | Status |
|---|---|---|---|
| `#setting-max-tokens` + LLM params (model, api_base, api_key, temperature, top_p, top_k, min_p, repeat/presence/frequency penalty) | Y | Y (via `api_endpoints.json`, APIRouter) | OK (separate persistence path) |
| `#setting-max-context` (max_input_tokens) | Y | via api config | OK |
| `#setting-idle-timeout` / `#setting-system-idle-timeout` | Y | Y (idle_timeout_seconds / system_agent_idle_timeout_seconds, PS) | OK |
| `#setting-tool-result-max-chars` … | (see table) | **N** | BROKEN (llm_cfg) |
| `#setting-log-api-post` | Y | N (frontend-only, intentional) | OK by design |
| `#setting-mcp-enabled` / `#setting-mcp-servers` | Y | N (per-session apply) | OK by design |
| auto-security toggle | Y | Y (`auto_security` EXTRA key) | OK |
| work-access folders | Y | Y (EXTRA keys) | OK |
| default-workspace | Y | Y (EXTRA key) | OK |
| colors, font-size, sounds, lines, truncate, auto-tab-focus, vision, afk | Y | N (cosmetic/frontend) | OK by design |

## Additional Runtime Defect (lower priority)
`api_integration.py` `_apply_ui_config` apply-to-instances loop (lines 1825-1828) handles
`tool_result_max_chars, grep_char_limit, grep_spillover, shell_char_limit, code_char_limit`
but **omits `list_dir_char_limit`** — a live-runtime propagation gap even within a session
(its config handler writes to llm_cfg, so the backend value updates, but instance application loop misses it).

## Confidence
**High Confidence.** All four code paths inspected directly; grep_spillover breakage is definitively
confirmed by code (handler writes llm_cfg + key in POOL_SETTINGS_KEYS, but save/load never materializes
llm_cfg into pool_settings.json).

## Recommendations (fix once, apply to all six llm_cfg limits)
1. In `_save_pool_settings()`, after `data = self.settings.to_dict()`, add copying of the six keys from
   `self.llm_cfg` into `data` (same 6-key tuple used everywhere).
2. In `_load_pool_settings()`, after PoolSettings load, copy those six keys back into `self.llm_cfg`
   (with clamp logic mirroring the handlers), so restart restores them.
3. Fix `loop_char_run_limit`: add it to `saveSettings()`, `loadSettings()`, and `getGenerateCfg()`
   (it has a valid backend handler + PoolSettings field + POOL_SETTINGS_KEYS entry, so the front-end is the only gap).
4. Add `list_dir_char_limit` to the `_apply_ui_config` live-application loop.

## Confidence
High Confidence.
## Open questions
- None blocking. (Verify clamps for the six llm_cfg keys match handler bounds before persisting raw.)