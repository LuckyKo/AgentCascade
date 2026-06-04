# Phase 0 Reconciliation Report — Tab Unification Project

**Generated:** 2026-05-23  
**Purpose:** Preparation analysis only — NO codebase changes made. This report identifies all reconciliation work needed to merge main branch features into the unified branch.

---

## Table of Contents
1. [Executive Summary](#executive-summary)
2. [operation_manager.py — Detailed Diff Analysis](#1-operation_managerpy---detailed-diff-analysis)
3. [Branch Divergence: api_server.py](#2-branch-divergence-api_serverpy)
4. [Branch Divergence: agent_orchestrator.py](#3-branch-divergence-agent_orchestratorpy)
5. [Branch Divergence: api_router.py](#4-branch-divergence-api_routerpy)
6. [Uncommitted Changes Assessment](#5-uncommitted-changes-assessment)
7. [Merge Priority Matrix](#6-merge-priority-matrix)

---

## Executive Summary

The main branch has diverged significantly from unified, with **~40KB of additional code** across 4 key files. The most impactful differences are:

| File | Main Size | Unified Size | Gap | Key Missing Features |
|------|-----------|-------------|-----|---------------------|
| operation_manager.py | 1844 lines (91KB) | 988 lines (44KB) | **~856 lines** | Subprocess grep, safe shell auto-approval, path security hardening, indentation preservation |
| api_server.py | 2796 lines (159KB) | 2469 lines (135KB) | **~327 lines** | Security advisor timeout, dismissal callbacks, serve_file endpoint |
| agent_orchestrator.py | 2291 lines (126KB) | 1990 lines (107KB) | **~301 lines** | Grep spill path, tool result truncation, message pool validation |
| api_router.py | 644 lines (30KB) | 461 lines (20KB) | **~183 lines** | Endpoint scheduler with cleanup/diagnostics |

---

## 1. operation_manager.py — Detailed Diff Analysis

### 1A. CRITICAL: Functionality in Main Missing from Unified

#### A) LRU Cache Helpers (Lines 72-105)
| Item | Lines | Category |
|------|-------|----------|
| `_compile_grep_pattern()` | L75-83 | Performance — caches compiled regex patterns |
| `_path_is_contained_cached()` | L87-98 | Security — prevents sibling-directory escape using `os.path.commonpath()` |
| Module-level tool availability cache (`_RIPGREP_AVAILABLE`, `_GREP_AVAILABLE`) | L102-104 | Performance — avoid repeated `shutil.which()` calls |

#### B) Subprocess-Based Grep Fast Path (Lines 439-621)
**`_try_subprocess_grep()`** — Uses ripgrep/system grep via subprocess for dramatically faster search on large codebases. Full feature set:
- Supports `exclude`, `ignore_vcs`, `context` lines, `smart_case` parameters
- Proper output formatting (match lines with `>>>` prefix, context lines with spaces)
- Truncation and spill file handling within the subprocess path
- Falls through to Python fallback on timeout or failure

#### C) Single-File Grep (Lines 623-728)
**`_grep_single_file()`** — Handles when the grep `path` argument is a file instead of a directory. Includes proper include/exclude glob matching for single files.

#### D) Enhanced `grep()` Signature (Lines 730-957)
Main's grep signature includes parameters unified lacks:
```python
# Main:
grep(pattern, path, include, char_limit, agent_name,
     exclude="", ignore_vcs=True, context=0, smart_case=True,
     spill_file_path=None)

# Unified:
grep(pattern, path, include, char_limit, agent_name)
```

Key features in main's grep:
- **`exclude`** — glob pattern to exclude files/directories
- **`ignore_vcs`** — skip `.git`, `node_modules`, `__pycache__`, etc. (L840)
- **`context`** — show N lines before/after each match (H3 feature)
- **`smart_case`** — case-insensitive unless pattern has uppercase (M1 feature)
- **`spill_file_path`** — pre-computed spill file path for truncated output
- **Match counting** — separate count of actual matches vs context/separator lines
- **Inline regex flag detection** — respects `(?-i:)` and `(?i:)` flags (Issue 4)

#### E) Heuristic Edit Indentation Preservation System (Lines 1121-1320)
A complete three-phase system for preserving indentation during heuristic edits:

| Phase | Functions | Purpose |
|-------|-----------|---------|
| Alignment | `get_leading_whitespace()`, `get_indent_width()`, `detect_indent_char()` | Map new_content lines to file block lines via difflib |
| Preservation | Indent adjustment logic (L1224-1269) | Apply file's original indentation to new content lines |
| Validation | `validate_indentation_consistency()` (L1275-1320) | Increment-based anomaly detection for indentation drift |

Additionally:
- **Heuristic edit count tracking** (`_heuristic_edit_counts` at L134) — warns when a file has been edited ≥3 times in heuristic mode
- **Trailing newline preservation** (L1329-1338)

#### F) Safe Read-Only Shell Command Auto-Approval (Lines 1520-1618)
**`_is_safe_readonly_shell_command()`** — Allows `ls`, `dir`, `find`, `tree`, `pwd`, `stat`, etc. to auto-approve without user interaction. Detects dangerous patterns:
- Command chaining (`&&`, `;`, `||`)
- Subshell execution (`$(...)`, backticks)
- File write redirections (`>`, `>>`)
- Background processes (`&`)
- Non-read-only commands

#### G) Enhanced Shell Command Execution (Lines 1620-1838)
**`execute_shell_command()`** — Major improvements over unified:
- Auto-approval for safe read-only commands (via `_is_safe_readonly_shell_command`)
- **Process tree killing on timeout**: Three-pass Windows cleanup (`taskkill /T` → WMIC descendant sweep → fallback)
- Unix process group killing via `os.killpg()`
- Spill files in `logs/spillover/` subdirectory with **size cap** (`MAX_SPILL_SIZE = 50MB`)
- Partial output capture on timeout
- Uses `subprocess.Popen` instead of `subprocess.run` for better timeout control

#### H) Security-Enhanced Path Resolution (Lines 309-349)
Main uses `_path_is_contained()` / `_path_is_contained_cached()` based on `os.path.commonpath()`:
```python
# Main (secure): prevents "foo" matching "foobar"
_path_is_contained(resolved, self.base_dir)

# Unified (vulnerable): str.startswith allows sibling escape
str(resolved).startswith(str(self.base_dir))
```

#### I) Performance-Enhanced `list_directory()` (Lines 354-396)
- Uses `os.scandir()` for cached stat info (vs `Path.iterdir()` in unified)
- Shows file sizes with formatting (unified doesn't show sizes)

### 1B. Optimizations/Refactors in Main Not in Unified

| Item | Lines | Description |
|------|-------|-------------|
| `SECURITY_ADVISOR_TIMEOUT_SECONDS = 180` | L64 | Timeout constant for security advisor checks |
| `SECURITY_ADVISOR_WARNING_SECONDS = 120` | L66 | Warning threshold before timeout |
| `MAX_SPILL_SIZE = 50MB` | L69 | Cap on spill file size to prevent disk exhaustion |
| `_heuristic_edit_counts` tracking | L134 | Per-file heuristic edit count with drift warnings |

### 1C. Conflicting Changes (Overlapping but Different)

#### A) `user_approve()` / `user_reject()` — Race Condition Fix
```python
# Main (atomic): pop inside lock
with self._lock:
    approval = self.pending.pop(request_id, None)

# Unified (race condition): get without removing
with self._lock:
    approval = self.pending.get(request_id)
```
**Impact:** Unified has a race condition where the same request could be approved twice if two clicks happen simultaneously.

#### B) Heuristic Matching Approach — Fundamental Difference
```python
# Main (L1038): Normalize raw content (no comment stripping)
file_lines = file_content.splitlines(keepends=True)

# Unified (L532-626): Strip comments before normalization
clean_file_content = remove_comments_keep_layout(file_content, ext)
```
Main intentionally **removed** comment stripping for more accurate matching. This is a deliberate design change — unified's approach is the older, superseded version.

#### C) `edit_file` Indentation Handling
- Main: Full 3-phase indentation preservation system (L1121-1320)
- Unified: Basic trailing newline preservation only (L713-722)
- These overlap but are completely different implementations

#### D) `execute_shell_command()` Implementation
- Main: Auto-approval + process tree killing + spill file management
- Unified: Always requires approval + simple `subprocess.run` with 120s timeout
- Same method name, entirely different implementation

### 1D. Functionality in Unified Missing from Main (To Preserve)

| Item | Lines | Description |
|------|-------|-------------|
| `CONTEXT_COMPRESSION` operation type | L36 | Added to OperationType enum |
| `apply_context_compression()` method | L978-982 | Internal context compression helper |
| `remove_comments_keep_layout()` in heuristic matching | L532-626 | Comment stripping before heuristic match (SUPERSEDED by main's raw approach) |

---

## 2. Branch Divergence: api_server.py

### 2A. Critical Features Missing from Unified

#### A) Security Advisor Timeout System (Lines 56, 2259-2450)
Main imports and uses security advisor timeout constants:
```python
from operation_manager import SECURITY_ADVISOR_TIMEOUT_SECONDS, SECURITY_ADVISOR_WARNING_SECONDS
```

The `_security_check()` function includes:
- **Timeout tracking** with `time.monotonic()` (L2283)
- **`_sec_warning_injector()` callback** (L2266-2274) — sends warning message to security advisor at 120s mark
- **Termination on timeout** (L2431-2450) — auto-rejects and informs user

Unified's `_security_check()` has NO timeout protection — can hang indefinitely.

#### B) Dismissal Callback System (Lines 1384-1401)
Main registers a callback for real-time UI tab removal when agents are dismissed:
```python
if agent_pool and hasattr(agent_pool, 'on_dismissed'):
    def _on_dismiss_callback(instance_name, log_path):
        # Fires state broadcast for dismissal
    agent_pool.on_dismissed(_on_dismiss_callback)
```

#### C) Dismissal Signal Handling in Sender Loop (Lines 1408-1413)
Main's `_sender_loop()` handles `dismissal` type signals:
```python
if data.get('type') == 'dismissal':
    await broadcast({'type': 'state', **build_state()})
else:
    await broadcast(data)
```

#### D) `api_serve_file()` Endpoint (Lines 1637-1652)
Serves files from the filesystem via `/api/file` endpoint. Handles `file:///` URL scheme and Windows paths.

### 2B. Optimizations/Refactors in Main Not in Unified

| Item | Lines | Description |
|------|-------|-------------|
| `grep_spillover` config key | L925, L2246 | Config option for grep spill file handling |
| Enhanced comment on Layer 1/2 concurrency control | Various | Better documentation of concurrency architecture |

### 2C. Conflicting Changes

#### A) `set_extra_work_folders()` Call at Line 931 vs 905
```python
# Main (correct): passes two args matching the signature
agent_pool.operation_manager.set_extra_work_folders([], work_access_folders)

# Unified (BUG): only passes one arg — will crash with TypeError
agent_pool.operation_manager.set_extra_work_folders(work_access_folders)
```
**This is a bug in unified.** The method signature takes `(folders_ro, folders_rw)` but unified only passes `work_access_folders` as the first argument.

#### B) COMPRESSION_BASELINE_TEMPLATE import
Unified imports `COMPRESSION_BASELINE_TEMPLATE` (L51) while main doesn't use it. This may be dead code in unified or a feature that was removed from main.

---

## 3. Branch Divergence: agent_orchestrator.py

### 3A. Critical Features Missing from Unified

#### A) `_compute_grep_spill_path()` (Lines 537-564)
Pre-computes the spillover file path for grep output before tool execution. This allows `operation_manager.grep()` to write directly to a known location when char_limit is exceeded, and the model receives the exact path in its truncation message.

Called at L1492 and L1538 during grep tool execution.

#### B) `_truncate_tool_result()` (Lines 567-640)
Truncates a tool result if it would push context past 95% capacity. Key features:
- Mirrors `base.py`'s token accounting (`_truncate_input_messages_roughly`)
- Separates system vs non-system message tokens
- Skips truncation for `compress_context` tool results
- Multimodal results pass through without truncation

#### C) `validate_message_pool()` (Lines 352-405)
Sanity-checks a message pool after compression to detect mangling:
- Pool is not empty
- First message is SYSTEM role
- No duplicate consecutive messages (same role + content)
- Message roles are valid strings

#### D) `support_multimodal_input()` (Lines 893-898)
Signals that the orchestrator can handle multimodal messages and should not have them stripped before reaching `_run()`. Returns `True`.

#### E) `count_active_tasks_by_class()` (Lines 280-284)
Counts how many active parallel tasks are running for a given agent class. Used for load balancing decisions.

### 3B. Optimizations in Main Not in Unified

| Item | Lines | Description |
|------|-------|-------------|
| Endpoint-specific concurrency resolution in `submit_task()` | L295-310 | Resolves concurrency limit BEFORE deep-copying history, avoiding wasted work |
| Enhanced `_append_system_notification()` | L848-867 | Supports both Message objects and dict-style messages with ContentItem handling |

### 3C. Conflicting Changes

No major conflicts detected. The unified branch's features (basic `stream_sub_agent_call`, `hooked_call_llm`) are present in main at different line numbers but serve the same purpose.

---

## 4. Branch Divergence: api_router.py

### 4A. Critical Features Missing from Unified

#### A) `EndpointScheduler.acquire()` — Dynamic Semaphore Resizing (Lines 91-165)
Main's `acquire()` includes logic to **safely resize semaphores** when concurrency limits change at runtime:
```python
# If the concurrency limit has changed since this endpoint was last scheduled,
# the semaphore is safely resized — active agents retain their slots, and
# new agents see the updated capacity.
```

#### B) `EndpointScheduler.cleanup_stale()` (Lines 191-202)
Removes schedule entries for endpoints with no activity, preventing memory leaks from temporarily-used endpoints that have gone idle.

#### C) `EndpointScheduler.count_active()` and `get_status()` (Lines 171-188)
Diagnostics methods for monitoring endpoint utilization.

#### D) `ApiRouter.set_agent_priorities()` (Lines 302-311)
Sets the priority-ordered endpoint list for an agent type, with validation that all IDs exist and persistence via `_save()`.

#### E) Enhanced Semaphore Wrapper — Stale-Semaphore Protection (Lines 505-509)
Main uses default-argument capture to freeze semaphore reference:
```python
def sem_generator_wrapper(gen, _sem=sem):
    try:
        yield from gen
    finally:
        _sem.release()
```
This prevents stale-semaphore risk if the endpoint is resized between iteration and generator consumption.

Unified uses a simpler wrapper without this protection:
```python
def generator_wrapper(gen):
    try:
        first = next(gen)
        yield first
        yield from gen
    except Exception:
        raise
```

#### F) `get_effective_concurrency()` (Lines 328-345)
Main has a separate method that also checks default fallback endpoints. Unified's `get_concurrency_limit()` is deprecated and delegates to this method.

### 4B. Conflicting Changes

#### A) Endpoint Resolution in `call_with_fallback()` (Lines 457-462 vs 273-278)
Main resolves endpoint settings for ALL endpoints including the default fallback:
```python
# Main: always try to read from endpoint config, even for default
for ep in self.endpoints.values():
    if ep.api_base == endpoint_base:
        max_retries = ep.max_retries
        concurrency_limit = ep.concurrency_limit
        break

# Unified: skip resolution for default fallback
if not is_default:
    for ep in self.endpoints.values():
```
Main's approach allows the default endpoint to also have its own concurrency setting — this is a Phase 1 fix.

---

## 5. Uncommitted Changes Assessment

### 5A. Modified Files (All Confirmed Different from Main)

| File | Status | Conflict Risk |
|------|--------|--------------|
| operation_manager.py | MODIFIED | **HIGH** — fundamental changes to grep, shell execution, path resolution |
| api_server.py | MODIFIED | **MEDIUM** — security advisor timeout, dismissal callbacks |
| agent_orchestrator.py | MODIFIED | **LOW** — mostly additive features |
| api_router.py | MODIFIED | **MEDIUM** — concurrency control changes |

### 5B. Untracked Files in Unified (Not in Main)

Notable files unique to unified:
- `tab_unification_plan.md` / `tab_unification_plan_v4.md` — planning docs
- `.agent_lessons/parallel_merge_protocol.md` — merge protocol doc
- `scheduling_audit_report.md` — audit report
- `grep_reliability_analysis.md` — analysis doc
- `browser_agent.md` / `browser_agent_cn.md` — browser agent souls
- Various test files (`test_afk_crash_fix.py`, `test_yield_from_finally.py`)

These are documentation/planning artifacts and pose no merge conflict risk.

### 5C. Specific Conflict Risks

1. **operation_manager.py `edit_file()` heuristic matching** — unified uses comment-stripped normalization while main uses raw content. Must choose one approach (recommend main's).

2. **api_server.py line 905** — the `set_extra_work_folders(work_access_folders)` call is a bug that will crash. Must be fixed to pass two args.

3. **api_router.py semaphore wrapper** — unified's `generator_wrapper` needs to be replaced with main's `sem_generator_wrapper` which has stale-semaphore protection.

---

## 6. Merge Priority Matrix

### Phase 1: Critical (Must Be Done First)
| # | File | Change | Effort | Risk |
|---|------|--------|--------|------|
| 1 | operation_manager.py | Add `_path_is_contained_cached()` + update `_resolve_path()` | Low | **HIGH** — security fix |
| 2 | operation_manager.py | Fix `user_approve()`/`user_reject()` race condition (`pop` instead of `get`) | Low | **MEDIUM** — correctness fix |
| 3 | api_server.py | Fix `set_extra_work_folders()` call (pass two args) | Trivial | **HIGH** — crash bug |
| 4 | operation_manager.py | Add security advisor timeout constants and import | Low | MEDIUM |

### Phase 2: High Value (Core Functionality)
| # | File | Change | Effort | Risk |
|---|------|--------|--------|------|
| 5 | operation_manager.py | Add subprocess grep fast path (`_try_subprocess_grep`, `_grep_single_file`) | Medium | Low |
| 6 | operation_manager.py | Update `grep()` signature with new params | Medium | **MEDIUM** — API change |
| 7 | api_server.py | Add security advisor timeout in `_security_check()` | Medium | Low |
| 8 | operation_manager.py | Add safe shell command auto-approval (`_is_safe_readonly_shell_command`) | Medium | Low |
| 9 | agent_orchestrator.py | Add `validate_message_pool()` | Low | Low |

### Phase 3: Important Improvements
| # | File | Change | Effort | Risk |
|---|------|--------|--------|------|
| 10 | operation_manager.py | Add indentation preservation system in `edit_file()` | High | **MEDIUM** — complex logic |
| 11 | api_server.py | Add dismissal callback system | Medium | Low |
| 12 | agent_orchestrator.py | Add `_compute_grep_spill_path()` and `_truncate_tool_result()` | Medium | Low |
| 13 | api_router.py | Add `cleanup_stale()`, `count_active()`, `get_status()` | Low | Low |
| 14 | operation_manager.py | Enhanced shell execution with process tree killing | High | **MEDIUM** — subprocess changes |

### Phase 4: Incremental (Nice to Have)
| # | File | Change | Effort | Risk |
|---|------|--------|--------|------|
| 15 | operation_manager.py | Add LRU caches (`_compile_grep_pattern`, module-level tool availability) | Low | Low |
| 16 | api_server.py | Add `api_serve_file()` endpoint | Low | Low |
| 17 | agent_orchestrator.py | Add `support_multimodal_input()`, `count_active_tasks_by_class()` | Low | Low |
| 18 | api_router.py | Update semaphore wrapper with stale-semaphore protection | Low | **MEDIUM** — concurrent code |

---

## Key Implementation Notes

### Decision Points for the Coder

1. **Heuristic matching approach**: Main's raw content normalization (no comment stripping) is the newer, improved approach. Unified's `remove_comments_keep_layout()` should be removed.

2. **Path containment**: Must use main's `_path_is_contained_cached()` approach. Unified's `str.startswith()` is vulnerable to sibling-directory escape.

3. **set_extra_work_folders API**: Both branches have the same two-argument signature `(folders_ro, folders_rw)`. The unified api_server.py call at L905 passing only one arg is a bug.

4. **grep parameter compatibility**: Main's grep has additional parameters (`exclude`, `ignore_vcs`, `context`, `smart_case`, `spill_file_path`). When merging, the caller code in agent_orchestrator.py needs to be updated to pass these new parameters where appropriate.

5. **Security advisor timeout**: The constants `SECURITY_ADVISOR_TIMEOUT_SECONDS` and `SECURITY_ADVISOR_WARNING_SECONDS` are imported by api_server.py from operation_manager.py. These must exist before the import works.

6. **on_dismissed callback**: Main's dismissal system requires `agent_pool.on_dismissed()` to exist. Check that this method is present in the unified agent_pool before enabling the callback registration.

---

*End of Phase 0 Reconciliation Report*