---
name: debug-ac-supervisor-child-not-starting
description: Diagnose why an AgentCascade supervisor-spawned child process (e.g. Telegram bridge) never stays up after a UI toggle — isolate config-handler-fired vs spawn-failed, and find missing env/config in the spawn path.
source: auto-generated
version: "1.0.0"
triggers:
  - "toggle did nothing"
  - "supervisor child not starting"
  - "bridge won't start via UI"
  - "spawned process exits immediately"
  - "config handler fired but no process"
generated_by: orchestrator
generated_from_task: "User toggled AC Telegram bridge on in UI; no bridge process running. Diagnosed and fixed the supervisor spawn path."
---

## Goal
Systematically find why an AC-owned child process (spawned by a `*Supervisor` class, e.g. `TelegramBridgeSupervisor`) never starts/stays up after the user flips a UI toggle — even when the code looks correct and unit tests pass.

## The core diagnostic split
The single most important distinction: **did the config handler fire, or not?**
- If it fired, the bug is in the *supervisor/spawn path* (child spawns then dies, or `agent_pool.<x>_supervisor` is None).
- If it didn't fire, the bug is upstream (UI payload, WS update_config routing, POOL_SETTINGS_KEYS registration).

**How to tell the handler fired (decisive, cheap check):** the handler sets a persisted `PoolSettings` field. Grep the settings file for that key:
```
grep -n "<setting_key>" config/pool_settings.json
```
If it shows the toggled value (e.g. `"telegram_bridge_enabled": true`), the handler ran and persisted — so the problem is downstream in the supervisor, NOT the toggle plumbing. This immediately rules out half the search space.

## Procedure
### Step 1 — Confirm no child process exists
List python processes with command lines (`wmic` is gone on modern Windows; use PowerShell):
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select-Object ProcessId,CommandLine | Format-List
```
Look for the supervisor's spawn argv (e.g. `python -m agent_cascade.telegram_bridge`). Absent = child never stayed up.

### Step 2 — Check the dedicated child log + the supervisor's own log tag
- The supervisor usually tees child stdout/stderr to `<workspace>/logs/<name>.log`. `list_dir` that path; **no file = no spawn happened at all** (points to handler/supervisor-object, not a dying child).
- Grep the main AC log for the supervisor's distinctive log tag (e.g. `[TelegramBridge]`) — NOT generic words like "bridge"/"telegram" which match tons of skill-matching DEBUG noise. A tag like `Spawned bridge pid=` proves a spawn attempt; its absence means `_spawn()` never ran.

### Step 3 — Rule out stale process vs stale bytecode
- Compare the running server PID's start time to the module's mtime (`Get-Process -Id <pid> | Select StartTime`) — if the process is newer than the code, it's not a stale-process issue.
- Per the supervisor gotcha: clear `__pycache__` for the package before re-testing (stale `.pyc` can hide new methods → `hasattr()` False).

### Step 4 — Reproduce in ISOLATION (the decisive step)
Write a tiny script that constructs the supervisor EXACTLY like `api_server.main()` does and drives the public entry point (e.g. `set_enabled(True)`), then prints `status()`:
```python
from agent_cascade.telegram_bridge.supervisor import TelegramBridgeSupervisor
sup = TelegramBridgeSupervisor(ac_base_url='http://127.0.0.1:8126',
                               project_root=r'<repo>', workspace_dir=r'<ws>')
sup.set_enabled(True); time.sleep(2)
print(sup.status())   # look at last_exit_code + error
sup.stop()            # ALWAYS clean up the child you started
```
`status()` gives you `last_exit_code` and a human `error` string — this usually names the exact failure (e.g. exit 2 = config problem) in one shot, instead of grepping logs.

### Step 5 — Read `_build_env()` / spawn kwargs to find the missing value
The most common root cause: **the supervisor passes only a minimal env allowlist to the child** (deliberately, to avoid leaking parent secrets), so any config value the child needs that ISN'T in that allowlist and ISN'T in a file the child reads itself → empty → exit code for "config problem". Check what the child's `config.py` actually reads (env? secrets.json?) vs. what `_build_env()` provides.

## The fix pattern (root cause, not symptom)
If the child needs a value (e.g. an allowlist of user IDs) that the supervisor doesn't pass via env: make the child resolve it from the **same persistent store it already uses for its token** (`config/secrets.json` via `get_secret('<key>')`) with env fallback — mirroring `_load_bot_token()`. This fixes the supervisor path WITHOUT changing `api_server.py`'s construction, because the child reads the file itself (CWD = repo root). Then add a hermetic regression test that mocks `config.secrets_loader.get_secret` and asserts `load_config()` resolves the value when env is absent.

## Tips
- **The config-handler-fired check is the highest-leverage move** — do it before reading supervisor internals; it halves the search space.
- **Always clean up the child** your isolation script starts (`sup.stop()`) or you'll leave a stray process that confuses later checks.
- **Exit codes are documented in the supervisor** (e.g. 0=clean, 2=config/permanent-no-restart, 3=transient-backoff). A config exit (2) means "don't restart" — so a missing value looks like "toggle does nothing," not a crash loop.
- **Don't grep generic words** in the AC console log; it's dominated by skill-matching DEBUG lines. Use the supervisor's exact log tag.
- After fixing, the user re-triggers the toggle (off→on) — NO server restart needed, because the supervisor spawns a fresh child that imports current disk code each time.
