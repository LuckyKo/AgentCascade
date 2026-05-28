# Cleanup Inventory — Post Phase 7 Migration

Generated after Phase 7 migration (6 commits). Maps all remaining cleanup opportunities: old files, feature flag branches, "sub-agent" terminology in new code, and duplicate code paths.

---

## 1. OLD FILES TO REMOVE

### 1.1 `agent_orchestrator.py` (~127 KB) — Confidence: **MEDIUM**

**Location:** `N:\work\WD\AgentCascade_unified\agent_orchestrator.py`

This file is still actively imported by production code paths. It's NOT safe to delete yet. But it CAN be significantly reduced because most of its content is the old execution loop that has been replaced by `ExecutionEngine`.

**Still actively imported (cannot remove):**
| Importing File | Line | What's Imported | Purpose |
|---|---|---|---|
| `agent_factory.py` | 35 | `_SubAgentFunctionProxy, CALL_AGENT_SCHEMA` | Tool registration for call_agent |
| `agent_factory.py` | 185 | `OrchestratorAgent` | Loading orchestrator template |
| `agent_pool.py` | 135 | `ParallelAgentManager` | Parallel agent execution |
| `api_server.py` | 1464 | `validate_message_pool` | Message validation in error handler |
| `start_api_server.py` | 37 | `AgentPool, load_orchestrator_agent` | App startup |
| `start_multi_agent.py` | 42 | `AgentPool, load_orchestrator_agent` | Multi-agent startup |
| `manager_ops.py` (agent_cascade/tools/) | 143,147,158,165 | `detect_loop, LoopDetectedError, extract_sub_agent_feedback` | Loop detection in parallel agent execution |

**Compat shim re-exports at bottom of file (lines 2297-2299):**
```python
from agent_pool import AgentPool  # noqa: F401
from agent_logger import AgentInstanceLogger  # noqa: F401
from agent_factory import load_orchestrator_agent, load_sub_agent_with_tools  # noqa: F401
```

**Recommendation:** Don't delete yet. Instead, migrate the still-needed components (`_SubAgentFunctionProxy`, `CALL_AGENT_SCHEMA`, `ParallelAgentManager`, `validate_message_pool`, `detect_loop`, `LoopDetectedError`, `extract_sub_agent_feedback`) into their own small modules under `agent_cascade/`. Then this file becomes just compat shims and can be removed.

**Test files still importing from it:**
- `test_compression.py` (lines 777, 807) — imports `OrchestratorAgent`
- `test_double_compression.py` (lines 17, 71, 108) — imports `OrchestratorAgent`

### 1.2 `agent_logger.py` (~21 KB) — Confidence: **HIGH** for replacement

**Location:** `N:\work\WD\AgentCascade_unified\agent_logger.py`

The new code has `agent_instance_logger.py` under `agent_cascade/logger/`. The old file is only still referenced by:
- `agent_orchestrator.py:2298` — compat shim re-export: `from agent_logger import AgentInstanceLogger  # noqa: F401`
- `agent_pool.py:24` — `from agent_logger import AgentInstanceLogger`

**Recommendation:** Once `agent_pool.py` is updated to import from `agent_cascade.logger.agent_instance_logger`, this file can be deleted. HIGH confidence that the new logger supersedes it.

### 1.3 `agent_pool.py` (old standalone, ~53 KB at root) — Confidence: **MEDIUM**

**Location:** `N:\work\WD\AgentCascade_unified\agent_pool.py`

This is STILL actively used as the main AgentPool class. The new architecture has its own `agent_cascade/agent_pool.py` (~60 KB) but it's NOT yet a replacement — both exist.

**Imported by:**
- `agent_orchestrator.py:54` — `from agent_pool import AgentPool`
- `agent_orchestrator.py:2297` — compat shim re-export
- `test_agent_pool.py:38` — tests
- `test_compression.py` (lines 657, 668, 684, 701, 713, 724, 730, 743) — tests

**Recommendation:** Don't delete. The new `agent_cascade/agent_pool.py` needs to fully replace it first.

### 1.4 Backup Files at Workspace Root — Confidence: **HIGH** for deletion

| File | Size | Note |
|---|---|---|
| `N:\work\WD\AgentWorkspace\backup_api_server.py` | ~135 KB | Old API server backup, contains all the old sub-agent terminology |
| `N:\work\WD\AgentWorkspace\current_api_server.py` | ~135 KB | Another old API server copy |

**Recommendation:** Delete both. They contain stale code with "sub-agent" terminology and are not imported anywhere in the current codebase.

---

## 2. FEATURE FLAG BRANCHES TO ELIMINATE

### 2.1 `USE_UNIFIED_STATE` — Confidence: **HIGH** to eliminate legacy path

**Defined in:** `config/unified.py:13`
**Imported by:** `api_server.py:59` and `agent_orchestrator.py` (via config)

**Branch locations in `api_server.py`:**

| Location | Line | Branch | Legacy Path | Unified Path |
|---|---|---|---|---|
| `_get_session_history()` | 519-541 | `if effective_unified:` | Lines 536-541 (reads from `_get_main_history` / `instance_conversations`) | Lines 522-534 (reads from `pool.instances`) |
| `get_agent_state()` | 556-572 | `if USE_UNIFIED_STATE:` | Lines 563-571 (root→pool, sub→sub_agent_state) | Lines 558-561 (all from sub_agent_state) |

**Recommendation:** Once the unified path is confirmed stable (Phase 7 is merged), set `USE_UNIFIED_STATE = True` permanently, then remove the else branches. The legacy paths at lines 536-541 and 563-571 become dead code.

### 2.2 `USE_UNIFIED_ARCHITECTURE` — Confidence: **HIGH** to eliminate (never used as condition)

**Defined in:** `config/unified.py:10`
**Imported by:** `api_server.py:59`, `agent_cascade/__init__.py`

**Critical finding:** This flag is imported but NEVER used as an `if` condition anywhere. It's a master toggle that was supposed to gate all unified behavior, but the actual gating is done by `USE_UNIFIED_STATE` and `USE_UNIFIED_LOOP`.

**Recommendation:** Remove this flag entirely. It adds confusion without providing any functionality.

### 2.3 `USE_UNIFIED_LOOP` — Confidence: **MEDIUM** to eliminate legacy path

**Defined in:** `config/unified.py:16`
**Used as condition at:** `agent_orchestrator.py:1399` (gates `__USE_PREV_ARG__` resolution in streaming path)

This flag gates whether the streaming (sub-agent) path resolves `__USE_PREV_ARG__` placeholders. When False, parsed_args pass through unresolved.

**Recommendation:** After verifying the unified loop is stable, set to True permanently and remove the condition at `agent_orchestrator.py:1399`.

### 2.4 Feature Flag Test Files — Confidence: **HIGH** for removal

After flags are eliminated, these test files become obsolete:
- `test_feature_flags.py` — tests the flag module itself
- `test_streaming_tool_resolution.py` — tests resolution gated by `USE_UNIFIED_LOOP`

---

## 3. "SUB-AGENT" TERMINOLOGY TO UPDATE

### 3.1 In New Code (`agent_cascade/` directory)

These are places where the new code still uses "sub-agent" terminology that should use unified agent instance terminology:

| File | Line | Current Term | Suggested Replacement |
|---|---|---|---|
| `agent_pool.py` (root) | 267-270 | `sub_agent_state` attribute | `instance_state` or remove entirely |
| `agent_pool.py` (root) | 377-379 | `sub_agent_state.pop()` | Update to use new terminology |
| `agent_pool.py` (root) | 431 | Comment: "UI-initiated termination path (WebSocket terminate_sub_agent message)" | "terminate_agent_instance" |
| `agent_pool.py` (root) | 456, 467 | `sub_agent_state.clear()` | Update terminology |
| `agent_pool.py` (root) | 755, 764, 1034, 1050 | `load_sub_agent_with_tools()` | `load_agent_template_with_tools()` |
| `agent_pool.py` (root) | 1133, 1140 | `extract_sub_agent_feedback` | `extract_agent_feedback` or `extract_instance_output` |
| `execution_engine.py` | 743 | Comment: "replacing _stream_sub_agent_call" | Update to reference new unified method name |
| `execution_engine.py` | 1141, 1168, 1185, 1189 | `sub_agent_msg_content` variable | `agent_msg_content` or `instance_msg_content` |
| `execution_engine.py` | 1275, 1307, 1330 | `self.pool.sub_agent_state[...]` | Update to new terminology |
| `execution_engine.py` | 1349-1360 | Comment + function referencing `_stream_sub_agent_call` and `extract_sub_agent_feedback` | Update both references |
| `api_integration.py` | 7, 78, 85, 91, 109, 345, 380, 434, 457, 756 | Various "sub-agent" references in comments and dict keys | Update to "agent instance" terminology |
| `run_agent_unified.py` | 6, 109 | Comment: "_stream_sub_agent_call() for sub-agents" | Update reference |
| `agent_invoker.py` (root) | 3, 72, 127-154, 245, 247 | Multiple references to `_stream_sub_agent_call` and "subagent" | Update to unified terminology |
| `agent_cascade/compression/agent_invoker.py` | Same as root version | References to `_stream_sub_agent_call` | Update terminology |
| `core.py` (compression) | 35 | Comment referencing `_stream_sub_agent_call` | Update reference |

### 3.2 In Old Code (backup files — will be deleted anyway)

The following files contain extensive "sub-agent" terminology but are marked for deletion:
- `N:\work\WD\AgentWorkspace\backup_api_server.py` — ~174 occurrences of sub_agent/subagent
- `N:\work\WD\AgentWorkspace\current_api_server.py` — similar count

### 3.3 Function/Attribute Name Changes Needed

| Current Name | Suggested Name | Used In |
|---|---|---|
| `_stream_sub_agent_call()` | `_execute_agent_instance()` or `execute_instance()` | agent_orchestrator.py:1735, agent_invoker.py |
| `load_sub_agent_with_tools()` | `load_agent_template_with_tools()` | agent_pool.py:755, 1036 |
| `extract_sub_agent_feedback()` | `extract_agent_feedback()` or `extract_instance_output()` | agent_orchestrator.py:2253 |
| `sub_agent_state` (dict attribute) | `instance_state` | agent_pool.py, api_integration.py, execution_engine.py |
| `terminate_sub_agent` (WS message type) | `terminate_agent_instance` | backup_api_server.py:1790 |

---

## 4. DUPLICATE CODE PATHS TO MERGE

### 4.1 `api_server.py` — Dual-Path State Reading — Confidence: **HIGH**

| Location | Lines | Old Path | New Path |
|---|---|---|---|
| `_get_session_history()` | 519-541 | `effective_unified=False`: reads from `_get_main_history()` + `instance_conversations` | `effective_unified=True`: reads from `pool.instances[name].conversation` |
| `get_agent_state()` | 556-572 | Legacy: root→pool, sub→sub_agent_state | Unified: all from sub_agent_state |

**Resolution:** Remove the else branches (legacy paths) after confirming unified path is stable.

### 4.2 `_stream_sub_agent_call()` Still Called — Confidence: **HIGH** that this can be replaced

The old method `OrchestratorAgent._stream_sub_agent_call()` at `agent_orchestrator.py:1735` is still called from:

| Caller | Line | Context |
|---|---|---|
| `agent_orchestrator.py` itself | 315, 1444, 1449 | Inside OrchestratorAgent's own _run loop |
| `agent_invoker.py` (root) | 127, 149 | Compression agent invocation via orchestrator |

**New replacement exists:** `ExecutionEngine._handle_call_agent()` at `execution_engine.py:743` and `_execute_agent_sync()` at `execution_engine.py:1348`

**Resolution:** The new path is fully implemented. The old `_stream_sub_agent_call` can be removed once all callers are migrated to use `ExecutionEngine`.

### 4.3 Broken Import Paths — Confidence: **HIGH** for fixing (not removing)

These imports reference functions that DON'T exist in the target module:

| File | Line | Broken Import | Problem |
|---|---|---|---|
| `agent_pool.py` (root) | 1133 | `from agent_cascade.compression.helpers import extract_sub_agent_feedback` | Function not in helpers.py — only exists in old agent_orchestrator.py |
| `execution_engine.py` | 1350 | `from agent_cascade.compression.helpers import extract_sub_agent_feedback` | Same issue |

**Resolution:** Either (a) move `extract_sub_agent_feedback` from `agent_orchestrator.py:2253` into `agent_cascade/compression/helpers.py`, or (b) change the imports to reference the old module. Option (a) is preferred since it decouples from the old file.

### 4.4 `run_agent_thread()` Wrapper — Confidence: **MEDIUM** for cleanup

The function `api_server.py:851` (`run_agent_thread`) is now just a thin wrapper that delegates to `run_agent_thread_unified()`. The wrapper could be eliminated by having the callers call `run_agent_thread_unified` directly.

### 4.5 Old Test Files — Confidence: **MEDIUM** for updating

| File | Issue |
|---|---|
| `test_compression.py` | Imports from old modules (agent_orchestrator, agent_pool) instead of new paths |
| `test_double_compression.py` | Same — imports from old agent_orchestrator |
| `test_agent_pool.py` | Tests old root-level agent_pool module |

**Resolution:** Update imports to use new `agent_cascade.*` paths. Some test logic may need adjustment since the new architecture works differently.

---

## 5. PRIORITY CLEANUP ORDER

1. **Fix broken imports** (Section 4.3) — These will cause runtime failures if that code path is hit
2. **Remove backup files** (Section 1.4) — No risk, immediate space savings
3. **Eliminate `USE_UNIFIED_ARCHITECTURE` flag** (Section 2.2) — Never used, dead code
4. **Migrate functions out of agent_orchestrator.py** — `_SubAgentFunctionProxy`, `CALL_AGENT_SCHEMA`, `ParallelAgentManager`, `validate_message_pool`, `detect_loop`, `LoopDetectedError`, `extract_sub_agent_feedback` → own modules
5. **Set feature flags to True permanently**, remove else branches (Sections 2.1, 2.3)
6. **Replace `_stream_sub_agent_call` with ExecutionEngine** paths (Section 4.2)
7. **Update terminology** from "sub-agent" to "agent instance" (Section 3)
8. **Remove old test files or update imports** (Section 4.5)
9. **Delete agent_orchestrator.py** once all functions are migrated out (Section 1.1)

---

*Generated by CleanupMapper — May 28, 2026*