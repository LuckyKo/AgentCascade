# Grep Spillover & Settings Persistence Fixes

## Issue 1: Model didn't know where spillover files were written (todo.md line 41)

### Root Cause
The truncation notice in `agent_orchestrator.py` communicated the spillover file path as an **absolute Windows path** like:
```
N:\work\WD\AgentCascade\logs\spillover\Maine_grep_20260523_120000.txt
```

But the `read_file` tool expects paths **relative to the workspace root**. The model would receive an absolute path and might try to use it directly, causing confusion or failures.

### Fix
In `agent_orchestrator.py`, after creating the spillover file, we now convert the absolute path to a relative one:
```python
spill_rel = str(spill_path.relative_to(self.agent_pool.workspace_dir)).replace('\\', '/')
```

The model now receives:
```
logs/spillover/Maine_grep_20260523_120000.txt
```

This is directly usable with the `read_file` tool. The `replace('\\', '/')` ensures forward slashes work cross-platform. A ValueError fallback preserves the absolute path if spill_path somehow ends up outside workspace_dir.

### Note
The shell command spillover in `operation_manager.py` already did this conversion (line 1825), so it was only the orchestrator-level spillover that needed fixing.

## Issue 2: grep-char-limit and grep-spillover settings reset on refresh (todo.md line 42)

### Root Cause
In `web_ui/app.js`, the `saveSettings()` function calls `getGenerateCfg()` which saves settings with **camelCase** keys like `grep_char_limit` and `grep_spillover`. But `loadSettings()` was looking for them with **hyphenated** keys (`grep-char-limit`, `grep-spillover`). Key name mismatch = settings lost on refresh.

### First Fix Attempt (Rejected by reviewer)
Adding explicit save lines in saveSettings():
```js
s['grep-char-limit'] = $('#setting-grep-char-limit').value;
s['grep-spillover'] = $('#setting-grep-spillover').checked;
```
**Problem:** This created dual-key pollution — localStorage contained BOTH `grep_char_limit` (from getGenerateCfg) AND `grep-char-limit` (from explicit line).

### Correct Fix
Changed `loadSettings()` to read the **camelCase keys** that `getGenerateCfg()` already writes:
```js
// Before (wrong): if (s['grep-char-limit'] !== undefined) { ... }
// After (correct): if (s['grep_char_limit'] !== undefined) { ... }
```

This matches the pattern used by other settings in loadSettings that don't have explicit save overrides (like `max_tokens` at line 446).

### Lesson Learned
When adding new settings to the UI, always ensure the save/load keys match. The pattern is:
1. `getGenerateCfg()` writes camelCase keys → these get saved via line 383 (`const s = getGenerateCfg()`)
2. Some settings have explicit save overrides that convert camelCase to hyphenated (e.g., `auto_continue` → `auto-continue`)
3. If a setting has an explicit save override, loadSettings must use the **hyphenated** key
4. If a setting does NOT have an explicit save override, loadSettings must use the **camelCase** key from getGenerateCfg()

The grep settings fall into category 4 — they don't need explicit save overrides since their camelCase keys work fine for localStorage persistence.

## Issue 3: Orchestrator stripped grep spillover notices (agent_orchestrator.py)

### Root Cause
When operation_manager wrote the full output to a spill file and added a truncation notice WITH the path, the orchestrator's `_truncate_tool_result` could apply a SECOND truncation layer that replaced the notice with one WITHOUT the spill path. The model would see "TOOL RESPONSE TRUNCATED" but have no way to find the full content.

### Fix
In `agent_orchestrator.py`, when `is_grep=True`, check if `"[TOOL RESPONSE TRUNCATED"` already exists in `tool_result`. If so, set `notice = ""` — preserving operation_manager's complete truncation notice with spill file path. Only add the orchestrator's own truncated notice when there's no existing one.

## Issue 4: Subprocess grep "Found 0 matches" after all lines truncated (operation_manager.py)

### Root Cause
When subprocess truncation removes ALL match lines (e.g., first line alone exceeds char_limit), `formatted` becomes empty and `count == 0`. The code then fell through to Python fallback via `if count == 0`, completely ignoring the fact that matches WERE found but truncated. No spill file notice was shown.

### Fix
Changed `if count == 0:` to `if count == 0 and not _sub_truncated:` — when `_sub_truncated=True` and `count==0`, we know matches existed. The summary now shows "Matches found for 'pattern' [TRUNCATED]" instead of misleading "Found 0 matches".

## Issue 5: Inconsistent truncation notice format (operation_manager.py)

### Root Cause
Subprocess path truncation notices omitted the character count (`{chars} chars`) that Python fallback included. Model couldn't gauge how much content was in the spill file.

### Fix
Added `_original_output_size` to `_try_subprocess_grep` return tuple (5th value). All 4 truncation points now include `({chars} chars)` consistently:
1. Single-file grep: `({len(full_output)} chars)` ✓
2. Subprocess re-check: `({len(full_output)} chars)` ✓  
3. Subprocess early-trunc: `({_orig_output_size} chars)` ✓
4. Python fallback: `({len(full_output)} chars)` ✓