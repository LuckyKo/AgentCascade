---
name: shell-window-suppression
description: Ensures test harnesses don't pop cmd windows by applying QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW at launch boundary.
triggers:
  - test files that call AsyncShellTracker.launch() directly
  - debugging regression tests on Windows
  - verifying window suppression mechanisms
---

# Shell Command Window Suppression

## WHEN TO ACT (Concrete Triggers)
- When writing tests for async shell commands that use `AsyncShellTracker.launch()` directly
- When debugging regression tests on Windows that show unexpected cmd windows
- When verifying that environment-based window suppression is applied at the right boundary

## CONTEXT
The test suite uses an env-var opt-out `QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW` to suppress visible cmd windows. However, this env var is only checked inside the `shell_cmd` tool (`agent_cascade/tools/custom/shell_cmd.py:261-262`). Tests that call `AsyncShellTracker.launch()` directly (bypassing the tool) are **not** protected, causing them to pop visible windows on Windows.

## PROCEDURE
1. **Identify direct `launch()` calls** in test files: search for `.launch(` or `tracker.launch`.
2. **Check if they pass `console_window` explicitly** — if not, they use the `launch()` default (`True`).
3. **Apply the opt-out pattern inside `launch()`** before task construction (around line 327 in `async_shell.py`):
   ```python
   # Respect the test-harness opt-out so direct launch() calls never pop a visible window.
   if console_window and os.getenv("QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW", "").strip() not in ("", "0", "false", "False"):
       console_window = False
   ```
4. **Verify** by running affected test files on Windows and confirming no cmd window appears.

## PRODUCTION SAFETY
- The `shell_cmd` tool (production caller) passes `console_window` explicitly, so the `launch()` default is irrelevant in production.
- The env var is set only in `tests/conftest.py`, so it has zero effect on production behavior.
- This change aligns the opt-out with the test harness without altering production defaults.

## CROSS-CHECKS
- Confirm that no other code path spawns a visible window via `CREATE_NEW_CONSOLE` without checking this flag.
- Ensure the `shell_cmd` tool retains its own env-var check for backward compatibility (belt-and-suspenders).

## RELATED FILES
- `agent_cascade/async_shell.py` - where the fix is applied
- `tests/conftest.py` - where the env var is set
- `agent_cascade/tools/custom/shell_cmd.py` - tool-level check (should remain)
