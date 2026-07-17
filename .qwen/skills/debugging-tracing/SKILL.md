---
name: debugging-tracing
description: Debug issues using systematic logging, trace analysis, breakpoint strategies with log pattern recognition, stack trace interpretation, and root cause analysis methodology
source: manual
version: "1.0.0"
triggers:
  - "debug"
  - "trace"
  - "stack trace"
  - "root cause"
  - "log analysis"
  - "breakpoint"
  - "debugging"
  - "error investigation"
---

## Goal

Systematically diagnose software issues by analyzing logs, interpreting stack traces, setting strategic breakpoints, and applying root cause analysis methodology to identify the underlying problem rather than just symptoms.

## Procedure

### Step 1 — Gather evidence before hypothesizing

**Collect all relevant artifacts:**
- Application logs (with timestamps)
- Stack traces (full, not truncated)
- Environment details (Python version, package versions, OS)
- Recent code changes (git diff of affected files)
- Configuration files that may have changed

```python
# Quick environment snapshot for debugging context
import sys, platform, importlib.metadata as meta

print(f"Python: {sys.version}")
print(f"Platform: {platform.platform()}")
for pkg in ["requests", "httpx", "numpy"]:
    try:
        print(f"{pkg}: {meta.version(pkg)}")
    except Exception:
        print(f"{pkg}: not installed")
```

### Step 2 — Log pattern recognition

**Common log patterns and what they indicate:**

| Pattern | Likely Cause | Action |
|---|---|---|
| Repeated identical messages every N seconds | Polling loop without backoff or stuck retry | Check for missing exit conditions |
| Timestamps jumping forward (e.g., 10s gaps) | Blocking I/O, long GC pause, deadlock | Profile the blocking call |
| `WARNING` then `INFO` in alternating pairs | Retry-after-fallback pattern | Check if fallback is masking a real issue |
| Messages interleaved from different sources | Race condition / concurrent access | Add thread IDs to log format |

**Structured log parsing:**
```python
import re, json
from collections import Counter

# Parse timestamped logs into structured data
LOG_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})\s+(\w+)\s+\[(.*?)\]\s+(.*)"
)

def parse_logs(log_text: str) -> list[dict]:
    entries = []
    for match in LOG_PATTERN.finditer(log_text):
        entries.append({
            "timestamp": match.group(1),
            "level": match.group(2),
            "source": match.group(3),
            "message": match.group(4),
        })
    return entries

# Find error clusters (errors within 5 seconds of each other)
def find_error_clusters(entries, window_seconds=5):
    errors = [e for e in entries if e["level"] in ("ERROR", "FATAL")]
    # Group nearby errors — likely same root cause
    clusters = []
    current_cluster = [errors[0]] if errors else []
    for error in errors[1:]:
        if time_diff(current_cluster[-1]["timestamp"], error["timestamp"]) < window_seconds:
            current_cluster.append(error)
        else:
            clusters.append(current_cluster)
            current_cluster = [error]
    return clusters
```

### Step 3 — Stack trace interpretation

**Read stack traces bottom-to-top for context, top-to-bottom for the fault:**

1. **Top frame**: The immediate error (what failed)
2. **Middle frames**: The call chain (how we got there)
3. **Bottom frame**: The entry point (where it started)

```python
# Python stack trace anatomy example:
"""
Traceback (most recent call last):
  File "app.py", line 45, in main()          <- Entry point / caller context
    result = process_data(input_data)
  File "processor.py", line 128, in process_data()   <- Where logic went wrong
    value = raw_string.strip().split(",")[0]
IndexError: list index out of range           <- Immediate cause
"""

# Use traceback module for programmatic analysis
import traceback
try:
    result = compute_something()
except Exception:
    tb_lines = traceback.format_exc().strip().split("\n")
    # The last line is the exception type + message
    error_type, _, error_msg = tb_lines[-1].partition(": ")
    # The second-to-last meaningful frame shows where it happened
    for frame in reversed(tb_lines):
        if "File" in frame:
            print(f"Fault location: {frame.strip()}")
            break
```

**Key stack trace patterns:**

| Pattern | Meaning |
|---|---|
| Same function called recursively 50+ times | Infinite recursion — missing base case |
| Alternating frames (A→B→A→B) | Circular dependency or mutual recursion |
| Deep call chain (>20 frames) | Consider refactoring to iterative approach |
| Multiple exception types in same trace | Chained exceptions — look at the innermost one first |

### Step 4 — Strategic breakpoint placement

**Where to set breakpoints (in order of priority):**

1. **At the boundary**: Where data enters/leaves a module
2. **After mutations**: Right after variables change state
3. **Before branches**: At conditional logic where paths diverge
4. **At error sites**: One frame above the crash location

```python
# Python breakpoint patterns
import logging

def debug_boundary(func_name, input_data):
    """Log entry/exit of a function for tracing"""
    logger = logging.getLogger(func_name)
    logger.debug(f"[ENTER] {func_name} with data type={type(input_data).__name__}")
    try:
        result = func_name  # actual call here
        logger.debug(f"[EXIT] {func_name} returned type={type(result).__name__}")
        return result
    except Exception as e:
        logger.error(f"[ERROR] {func_name}: {e}", exc_info=True)
        raise

# Conditional breakpoint (break only when condition met)
def trace_with_condition(data, target_id="abc-123"):
    for item in data:
        if item["id"] == target_id:
            import pdb; pdb.set_trace()  # Only breaks for our target
        process(item)
```

### Step 5 — Root cause analysis methodology (5 Whys + Fishbone)

**The 5 Whys technique applied to debugging:**

```
Symptom: API returns 500 error on /users endpoint
  ↓ Why? → Database query timed out
    ↓ Why? → Query scanned full table instead of using index
      ↓ Why? → New field added without updating WHERE clause
        ↓ Why? → Migration script didn't update the query builder
          ↓ Why? → No integration test for query performance after schema changes
Root cause: Missing performance regression tests in CI pipeline
```

**Systematic elimination approach:**

1. **Reproduce reliably**: Can you trigger it consistently? If not, add more logging first.
2. **Isolate the variable**: Change one thing at a time (binary search through code paths)
3. **Verify the fix**: The symptom disappears AND the root cause is addressed
4. **Add prevention**: Test, lint rule, or monitoring alert to catch recurrence

### Step 6 — Debug logging injection patterns

```python
import logging

# Create a debug logger with context
logger = logging.getLogger("debug.trace")
logger.setLevel(logging.DEBUG)

def trace_call(func):
    """Decorator for tracing function calls"""
    import functools, time
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        logger.debug(f"→ {func.__name__}(args={len(args)}, kwargs={list(kwargs.keys())})")
        try:
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start
            logger.debug(f"← {func.__name__} OK ({elapsed:.3f}s)")
            return result
        except Exception as e:
            elapsed = time.perf_counter() - start
            logger.error(f"× {func.__name__}: {e} ({elapsed:.3f}s)")
            raise
    return wrapper
```

## What NOT to do

- Do not add print statements everywhere — use structured logging with levels instead
- Do not fix the symptom without finding the root cause (band-aid fixes create technical debt)
- Do not ignore "working" stack traces — a silent fallback may be masking data loss
- Do not debug in production without cleanup — remove all debug logging before merging
- Do not assume the first error is THE error — look for cascading failures