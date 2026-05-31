# Merge Notes: origin/master → tab-unification (2026-05-30)

## Summary of Changes

### Step 1: Soul Files (Simple Copy)
- `agents/coder_soul.md` — copied from master (5 insertions, 2 deletions)
- `agents/orchestrator_soul.md` — copied from master (17 insertions, 6 deletions)
- `agents/security_advisor_soul.md` — copied from master (5 insertions, 2 deletions)

### Step 2: shell_cmd.py + operation_manager.py
- `agent_cascade/tools/custom/shell_cmd.py` — copied from master (now has timeout parameter support)
- `agent_cascade/operation_manager.py` — added `timeout: Optional[int] = None` to `execute_shell_command()`, command length validation, and effective_timeout logic (default 30s, max 3600s). Kept our advanced 3-pass process tree killing with WMIC sweep.

### Step 3: code_interpreter.py (Careful Merge)
- Copied master's version entirely — all our unique features (extra_work_folders_ro/rw, _KERNEL_LOCK, _is_path_allowed, _resolve_extra_folders, path_mapping) were present in master too.
- **Gained from master:**
  - Resource limits: CONTAINER_MEMORY_LIMIT, CONTAINER_CPU_LIMIT, CONTAINER_PID_LIMIT
  - 3-tier timeout recovery (Tier 1: Jupyter interrupt → Tier 2: Docker SIGINT → Tier 3: container kill)
  - Security hardening: --cap-drop=ALL, --security-opt=no-new-privileges
  - Port binding to 127.0.0.1 only (Fix C2)
  - Timeout on wait_for_ready() (Fix A2)
  - Activity tracking after successful execution (Fix D3)
  - Leftover container cleanup before start (Fix D2)
  - Raise TimeoutError instead of setting text (Fix A1a/b)

### Step 4: dna.py (Selective Merge)
- Removed "NOTE: All paths are relative to the workspace root." from grep description
- Removed "NOTE: All file tool paths must still be relative to workspace root" from system_info description
- Added `timeout` parameter to shell_cmd metadata
- Updated compress_context description and summary_text parameter with master's more specific wording
- **Did NOT touch** COMPRESSION_BASELINE_TEMPLATE — our version has a better header

## Key Findings
- Our branch's unique features were already present in master (they were merged upstream)
- The main value of this merge was gaining master's 3-tier timeout recovery and security hardening
- operation_manager.py lives at root level on master but inside agent_cascade/ on our branch — the reorganization was done on our side