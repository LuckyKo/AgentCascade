---
name: code-execution
description: Execute and test code snippets in isolated environments (Python, JavaScript, shell commands) with safe execution patterns, output parsing, and error handling
source: manual
version: "1.0.0"
triggers:
  - "execute code"
  - "run snippet"
  - "test code"
  - "code sandbox"
  - "python interpreter"
  - "shell command"
  - "js execution"
---

## Goal

Safely execute and validate code snippets across multiple languages (Python, JavaScript, shell) using isolated environments. Parse output correctly, handle errors gracefully, and verify results before trusting them.

## Procedure

### Step 1 — Choose the right execution environment

| Language | Tool | Notes |
|---|---|---|
| Python | `code_interpreter` | Docker-based sandbox with workspace mounted at `/workspace` |
| JavaScript/Node | `code_interpreter` (run via subprocess) or inline `node -e` | Use `code_interpreter` to spawn node process |
| Shell commands | `shell_cmd` (async for long-running, sync for quick checks) | Requires user approval — use sparingly |
| File-based code | Write file first, then execute via import or direct run | Best for anything over ~20 lines |

### Step 2 — Python execution patterns

**Quick one-liners and small snippets (≤200 chars):**
```python
# Direct inline execution
result = [x**2 for x in range(10) if x % 2 == 0]
print(result)
```

**Larger scripts — write to file first:**
```python
# Write the script, then import or run it
import subprocess
result = subprocess.run(["python", "script.py"], capture_output=True, text=True)
print("stdout:", result.stdout)
print("stderr:", result.stderr)
print("returncode:", result.returncode)
```

**Safe execution checklist:**
- Always check `returncode` for non-zero exits
- Capture both `stdout` and `stderr` separately
- Set timeouts for operations that might hang: `timeout=30`
- Use `fresh=True` in code_interpreter when state from previous runs could interfere

### Step 3 — JavaScript execution patterns

```python
import subprocess, json

code = """
const data = { items: [1,2,3] };
console.log(JSON.stringify(data.items.map(x => x * 2)));
"""
result = subprocess.run(["node", "-e", code], capture_output=True, text=True)
print(result.stdout.strip())
```

### Step 4 — Output parsing strategies

**Structured output (JSON):**
```python
import json
output = '{"status": "ok", "data": [1,2,3]}'
parsed = json.loads(output)
assert parsed["status"] == "ok"
```

**Tabular/CSV-like output:**
```python
import csv, io
lines = """col1,col2\na,1\nb,2"""
reader = csv.DictReader(io.StringIO(lines))
for row in reader:
    print(row)  # {'col1': 'a', 'col2': '1'} ...
```

**Log-style output (grep for patterns):**
```python
import re
log_lines = "INFO: Started\nWARN: Slow query\nERROR: Timeout".split("\n")
errors = [l for l in log_lines if re.search(r"ERROR|FATAL", l)]
warnings = [l for l in log_lines if re.search(r"WARN|WARNING", l)]
```

### Step 5 — Error handling patterns

**Python execution with retries:**
```python
import time

def run_with_retry(code, max_retries=3, delay=1.0):
    for attempt in range(max_retries):
        try:
            result = eval(compile(code, "<string>", "single"))
            return result
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                raise RuntimeError(f"Failed after {max_retries} attempts: {e}")

print(run_with_retry("2 + 2"))
```

**Shell command with error capture:**
```python
import subprocess, shlex
cmd = "ls /nonexistent/path"
result = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=10)
if result.returncode != 0:
    print(f"Command failed (rc={result.returncode}): {result.stderr.strip()}")
```

### Step 6 — Validation and assertion patterns

After execution, always validate the output:
```python
# Numeric checks
assert isinstance(result, (int, float)), f"Expected number, got {type(result)}"
assert result > 0, "Result should be positive"

# Structural checks for data
assert isinstance(data, dict), "Expected dictionary"
assert all(k in data for k in ["id", "name"]), "Missing required keys"

# Type-safe parsing
try:
    count = int(output.strip())
except ValueError:
    raise ValueError(f"Could not parse output as integer: {output!r}")
```

## Key Configuration Values

| Parameter | Recommended | Why |
|---|---|---|
| `timeout` (shell) | 10s for quick, 60s for heavy | Prevents hanging on slow operations |
| `max_retries` | 3 | Balances resilience against infinite loops |
| `delay` (retry backoff) | Exponential: `base * attempt` | Avoids thundering herd on transient failures |

## What NOT to do

- Do not execute code inline without error handling — always wrap in try/except or check return codes
- Do not trust stdout alone — stderr may contain warnings that matter
- Do not run shell commands with unquoted variables — use `shlex.split()` for safety
- Do not skip output validation — a successful exit code doesn't mean correct results
- Do not reuse code_interpreter state without considering side effects from previous runs