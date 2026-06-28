---
name: startup-error-audit
description: Audit Python application entry points for missing error handling, add proper debug/error logging on failure paths, and ensure clear ready confirmation messages at startup completion
source: auto-skill
extracted_at: '2026-06-28T18:46:39.479Z'
---

## Goal

Ensure all application entry points (startup scripts) have comprehensive error handling with meaningful logging, preventing partially-initialized state and making debugging straightforward when startup fails.

## Procedure

### Step 1 — Discover all entry points

Search for files that launch the application:

```bash
# Find files with uvicorn.run, server.run(), Gradio launch, or subprocess.Popen of servers
grep -rn "uvicorn\.run\|server\.run()\|Gradio.*launch" --include="*.py" .

# Also find entry points by convention
find . -name "start*.py" -o -name "main.py" -o -name "run_server.py" | head -20
```

### Step 2 — Audit each entry point for gaps

For every startup file, check these patterns that indicate missing error handling:

| Anti-pattern | What to fix |
|---|---|
| Bare `obj = SomeClass(...)` without try/except | Wrap in try/except with ERROR/FATAL logging and `raise SystemExit(1)` |
| Silent `except Exception: pass` | Replace with explicit debug/warning logging that identifies what failed and why |
| Hardcoded port numbers like `port = 8765` | Use env var fallback: `port = int(os.getenv('QWEN_AGENT_PORT', 8765))` |
| No check for empty results after discovery | Add guard clause: `if not items: logger.error("[FATAL] ..."); raise SystemExit(1)` |
| Bare `uvicorn.run()` or `server.run()` at bottom | Wrap in try/except with OSError errno 98 / "address already in use" detection |

### Step 3 — Apply the standard error handling pattern

For each critical initialization step, use this structure:

```python
try:
    obj = SomeClass(arg1=value1)
    logger.debug("[INIT] %s initialized successfully", type(obj).__name__)
except Exception as e:
    logger.error("[FATAL] %s initialization failed: %s", type(obj).__name__, e)
    raise SystemExit(1)
```

For optional/recoverable components (tools that may not be available):

```python
try:
    obj = OptionalTool(arg=value)
    logger.debug("[INIT] Tool loaded")
except Exception as e:
    logger.debug("[INIT] Tool skipped (not available): %s", e)
    # or WARNING if the failure is unexpected but non-fatal
```

For server launch:

```python
try:
    uvicorn.run(app, host='0.0.0.0', port=port)
except OSError as e:
    if e.errno == 98 or 'address already in use' in str(e).lower():
        logger.error("[FATAL] Port %d is already in use. Change PORT env var or stop the other process.", port)
    else:
        logger.error("[FATAL] Server failed to start: %s", e)
    raise SystemExit(1)
except Exception as e:
    logger.error("[FATAL] Server crashed: %s", e)
    raise SystemExit(1)
```

### Step 4 — Add startup completion confirmation

After all initialization succeeds, emit a clear "ready" message:

```python
logger.info("\n[OK] API Server ready!")
logger.info("    -> Open http://127.0.0.1:%d in your browser", port)
logger.info("    -> WebSocket at ws://127.0.0.1:%d/ws/chat", port)
logger.info("    -> REST API at http://127.0.0.1:%d/api/", port)
logger.info("=" * 50)
```

### Step 5 — Verify with syntax check

```bash
python -m py_compile entry_point.py
```

## Key logging levels to use

| Level | When |
|---|---|
| `[INIT]` (INFO) | High-level milestones: workspace detected, server starting |
| `[OK]` (INFO) | Successful completion of a step or the full startup sequence |
| `[FATAL]` (ERROR) | Unrecoverable failure — application cannot function without this component |
| `WARNING` | Recoverable issue — feature unavailable but app can continue |
| `[INIT]` (DEBUG) | Per-component success/failure for detailed troubleshooting |

## What NOT to do

- Do not use bare `except: pass` anywhere in startup code
- Do not let exceptions propagate without logging the error message
- Do not start the server if critical components (agents, operation manager) failed to initialize
- Do not swallow subprocess spawn failures — clean up already-launched processes before exiting
