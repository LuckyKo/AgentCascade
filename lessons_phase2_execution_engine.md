# Lessons Learned — Phase 2 Execution Engine Implementation

## Critical Issues Fixed (3 Review Cycles + This Session)

### Cycle 1 Critical Fixes:
1. **extract_sub_agent_feedback missing from compression module** - Added to helpers.py, re-exported from __init__.py
2. **ParallelAgentManager missing methods** - Added _state_lock (RLock), has_active_tasks(), proper submit_task() with ThreadPoolExecutor
3. **compress_context references agent_pool.instance_loggers** - Made graceful with hasattr() check
4. **_rebuild_working_set should use shared helper** - Now uses rebuild_working_set from helpers module

### Cycle 2 Critical Fixes:
5. **_execute_agent_sync() had empty body** - Added extract_sub_agent_feedback + return statement
6. **instance undefined in _execute_llm_call** - Added instance parameter to signature and caller
7. **Missing get_agent()/load_agent() on AgentPool** - Added for compression agent invoker compatibility
8. **agent_invoker.py wrong active_stack path** - Changed to _execution.active_stack; added sub_agent_state compat shim
9. **helpers.py missing ASSISTANT/FUNCTION/extract_text_from_message imports** - Added all three
10. **LoopDetectedError swallowed by generic handler** - Added `except LoopDetectedError: raise` before generic

### Cycle 3 Critical Fixes:
11. **count_by_class missing _state_lock protection** - Added lock acquisition (C3 fix per DESIGN_REWRITE §4.2)
12. **compression_summary = None after forced compression** - Now extracts from pool state via marker message scan
13. **core.py inline imports** - Moved `copy` import to module level, removed inline Message import from try block
14. **caller_history unused parameter** - Removed from _execute_agent_sync and _create_and_run_agent signatures

### This Session Fixes:
15. **AgentInstance dataclass field ordering bug** - `conversation = field(default_factory=list)` came before `is_active: bool` (no default). Python dataclasses require non-default fields first. Fixed by removing the default — callers always provide explicit list.
16. **LoggerManager raised NotImplementedError on get_logger()** - compression/core.py called `_logger.get_logger()` after pool mutation → raised NotImplementedError → except block rolled back pool → compression always failed. Fixed by implementing NoOpLogger (insert_compression_marker, log_message, update_history) as placeholder.

## File Locations (absolute paths)
- `N:\work\WD\AgentCascade_unified\agent_cascade\execution_engine.py` — Full ExecutionEngine implementation (1032 lines, 22 methods)
- `N:\work\WD\AgentCascade_unified\agent_cascade\agent_pool.py` — Updated with NoOpLogger + LoggerManager fix (564 lines)
- `N:\work\WD\AgentCascade_unified\agent_cascade\agent_instance.py` — Fixed dataclass field ordering (93 lines)
- `N:\work\WD\AgentCascade_unified\agent_cascade\compression\core.py` — Fixed imports (353 lines)
- `N:\work\WD\AgentCascade_unified\agent_cascade\compression\helpers.py` — extract_sub_agent_feedback (129 lines)
- `N:\work\WD\AgentCascade_unified\agent_cascade\compression\__init__.py` — Re-export of extract_sub_agent_feedback

## Key Design Decisions
1. **_create_and_run_agent owns active_stack lifecycle** - Appends and removes in finally block, no duplicate cleanup in callers
2. **is_success check in _force_compression** - Uses `"failed" not in result.lower()` instead of string prefix matching
3. **compression_summary extraction** - Scans pool state for marker message with `<context_summary>` tags after successful compression
4. **Turn budget preserved during forced compression** - turns_available -= 1 happens AFTER _pre_llm_checks, so compression doesn't consume a turn