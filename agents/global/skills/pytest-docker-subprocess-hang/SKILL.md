---
name: pytest-docker-subprocess-hang
description: Run and verify pytest for the AgentCascade skill/agent test suites reliably — host shell_cmd completes fast, code_interpreter subprocesses hang.
source: auto-generated
version: "1.0.0"
triggers:
  - "skill rating flush fix"
  - "run the three skill test suites"
  - "pytest hangs no output"
  - "code_interpreter timeout 120s"
  - "verify test results AgentCascade"
generated_by: coder
generated_from_task: "skill rating flush fix — verify the three skill test suites pass after the change"
---

## Goal
Reliably execute and read pytest results for the AgentCascade project (e.g. verifying the three skill test suites after a fix) without burning turns on a hung in-container subprocess.

## Procedure

### Step 1 — Prefer host `shell_cmd` over `code_interpreter` for test runs
In this environment, `python -m pytest ...` launched via `shell_cmd` (cwd = repo root) completes in seconds and returns full output. The SAME command run through `code_interpreter`'s Python sandbox can hang past the interpreter's 120s limit with zero usable output.
```bash
# shell_cmd — works, ~7s:
python -m pytest tests/test_skill_generation.py -n 0 -q --no-header -p no:cacheprovider
```

### Step 2 — If you MUST use code_interpreter, never block on subprocess.run
Blocking `subprocess.run(..., timeout=N)` where N approaches the interpreter's own limit produces a hard 120s timeout with no result. Instead launch non-blocking and poll a log file:
```python
import subprocess, os, time
with open('/tmp/run.log', 'w') as lf:
    proc = subprocess.Popen(['python','-m','pytest','tests/test_x.py','-n','0','-q'],
                            cwd='/extra_rw_0', stdout=lf, stderr=subprocess.STDOUT)
# poll for the final summary line; keep total wait well under 120s
```
Poll for a line containing `passed`/`failed` AND `in X.XXs`.

### Step 3 — Recognize the hang signature (don't loop re-running)
A hung in-container run shows: ~35 progress dots, then silence, then on timeout a `KeyboardInterrupt` at `agent_cascade/utils/utils.py:159`, and NO final "N passed in Xs" line. The cause is typically a non-daemon thread (e.g. conftest local-LLM discovery) that never joins inside the container. **This is environmental, not your code.** Do NOT keep re-running the same command — switch to host `shell_cmd` (Step 1).

### Step 4 — Verify results are real
Confirm a per-file count and the final `N passed / M failed in Xs` line. A bare "passed" with no item count is suspect (see [[feedback_pytest_windows_verification]]).

## Tips
- The host shell has no `tail`/`head` pipes — run the base command; output auto-truncates to a spillover file you can `read_file`.
- If a pre-existing unrelated test fails, confirm it fails on pre-fix code too before attributing it to your change.
- Repeated identical hung runs are a loop, not progress — stop and change angle (container → host).
