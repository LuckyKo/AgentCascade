# AgentCascade Unified Architecture — Design Compliance Audit Report

**Date:** 2026-05-28  
**Auditor:** DesignComplianceAuditor  
**Scope:** Phases 1-6 of the unified architecture rewrite  
**Reference:** `DESIGN_REWRITE.md`  

---

## Executive Summary

The codebase has made **significant progress** on the unified architecture, but it is **NOT yet fully compliant** with the design document. Critical deviations remain in the form of:

1. **Dual-read wrapper classes** (`_InstanceConversationMapping`) that violate the single-source-of-truth principle
2. **Feature flags** that allow fallback to old dual-path behavior (defaulting to legacy mode)
3. **Structural remnants** of the old architecture still present in `api_server.py` and `agent_invoker.py`
4. **Dead/duplicate files** from the pre-rewrite codebase still on disk

The rewrite is approximately **70% complete** architecturally, with the remaining 30% consisting of cleanup and removal tasks that are explicitly marked as Phase 6 scope in internal documentation.

---

## 1. PASS — Requirements Confirmed as Met

### §2.1 AgentInstance Data Model
- ✅ `AgentInstance` dataclass matches design exactly (identity, conversation, execution state, compression state)
- ✅ `_compression_lock: threading.Lock` present on every instance
- ✅ `CompressResult`, `LoopDetectedError`, `PoolSettings` all match design specification
- ✅ No optional fields distinguishing "main" from "sub"

### §2.2 AgentPool Thin Coordinator Pattern
- ✅ `instances: Dict[str, AgentInstance]` — single registry
- ✅ `templates: Dict[str, Assistant]` — template registry
- ✅ `settings = PoolSettings()` — configuration object
- ✅ `_execution = ParallelAgentManager(self)` — delegated parallel execution
- ✅ `_logger = LoggerManager(self, workspace_dir)` — delegated logger lifecycle
- ✅ `_halted_instances: set` — inline halt state (simple data structure)
- ✅ `message_queues: Dict[str, List[str]]` — inline message routing
- ✅ `_stopped_event: threading.Event()` — global stop flag
- ✅ Delegation methods: `send_message()`, `enqueue_message()`, `drain_queue()`, `has_messages()`, `halt_instance()`, `resume_instance()`, `is_instance_halted()`, `submit_parallel()`

### §3.1 Phase-Based Execution Engine
- ✅ `ExecutionEngine` receives `AgentInstance` as parameter (stateless)
- ✅ `run(instance)` — single entry point for all agents
- ✅ Five phases implemented: `_setup_turn()`, `_pre_llm_checks()`, `_call_llm_with_injection()`, `_process_response()`, `_post_turn_checks()`
- ✅ Each phase is a focused method (~20-60 lines)
- ✅ Generator yields `List[Message]` on each phase transition
- ✅ Loop detection raises `LoopDetectedError` (propagates to consumer)
- ✅ Exception handling with error state yield and `is_active = False` in finally

### §3.2 Unified Tool Execution
- ✅ `_execute_tool()` handles all tools through single method
- ✅ `call_agent`, `dismiss_agent`, `compress_context` routed through dedicated handlers
- ✅ Other tools delegated to template's `function_map`
- ✅ `__USE_PREV_ARG__` placeholder resolution in place

### §3.3 Parallel Execution via ThreadPoolExecutor
- ✅ `ParallelAgentManager` with `active_stack`, `active_tasks`, `_state_lock (RLock)`
- ✅ `submit_task()` submits to background thread pool
- ✅ Uses unified `ExecutionEngine.run()` internally
- ✅ Completion notification via `pool.send_message()` to caller's queue

### §4.1 Unified Queue System
- ✅ `message_queues` on pool — all agents use same system
- ✅ Injection at top of while loop (`_pre_llm_checks` drain)
- ✅ Mid-tool urgent injection (`_process_response`)
- ✅ Parallel completion enqueues to caller's queue

### §4.2 State Building from Pool
- ✅ `build_state_from_pool()` reads from `pool.instances`
- ✅ Snapshot before iteration (C3 fix)
- ✅ `build_stream_update_from_pool()` for lightweight deltas
- ✅ `get_agent_state_from_pool()` unified path for any agent

### §5.1 Single Source of Truth (in new code paths)
- ✅ All new code reads from `pool.instances[name].conversation`
- ✅ No `session['history']` in api_integration.py or execution_engine.py

### §5.3 Halt/Resume Uniform Across Agents
- ✅ `halt_instance()`, `resume_instance()`, `is_instance_halted()` on pool
- ✅ `_compression_halted` set for compression-specific tracking
- ✅ `halt_all_instances()` with except list support
- ✅ `resume_all_instances()` only clears compression halts

### §6.4 Compression Thread Safety via _compression_lock
- ✅ Per-agent `threading.Lock` on `AgentInstance`
- ✅ Used in `_handle_compress_context()` and `_force_compression()`
- ✅ All conversation reads/writes protected by lock in agent_pool.py

### §6.5 Special-Purpose Agent Invocation Pattern
- ✅ Compression and Security Advisor follow the common lifecycle pattern (trigger → register in pool → execute via ExecutionEngine → parse result → apply decision → cleanup)

### §7.1 Unified Loop Detection
- ✅ `_detect_loop()` in ExecutionEngine — shared across all agents
- ✅ Feature extraction from non-system messages
- ✅ Pattern matching with configurable L and K parameters
- ✅ False positive filtering for FUNCTION/USER patterns

### §7.2 Loop Recovery at Consumer Level
- ✅ `LoopDetectedError` raised by engine, caught by wrapper
- ✅ `run_agent_in_pool_with_recovery()` in api_integration.py implements retry logic
- ✅ Surgical rollback + corrective hint injection
- ✅ Bounded by `max_auto_retries` (default 3)

### §7.3 Surgical Rollback Safety Guarantees
- ✅ Never removes SYSTEM message or first USER message
- ✅ Caps rollback at 50% of removable history per operation
- ✅ Refines pop_count to avoid dangling tool calls
- ✅ Syncs logger via `truncate_to()`

### §2.6 Marker Stacking Reload Algorithm
- ✅ `find_last_marker()` implemented on pool
- ✅ `slice_history_for_llm()` extracts system + post-marker tail
- ✅ `load_session_from_log()` supports marker stacking restoration

### Performance Requirements (§6)
- ✅ Token caching via `_count_history_tokens()` with fallback estimation
- ✅ Lazy sync for instance_conversations via version counter (`_instances_version`)
- ✅ Throttled loop detection (window of last 40 messages, pattern lengths 1-20)

---

## 2. DEVIATION — Where Code Differs from Design

### D1: `_InstanceConversationMapping` Dual-Read Wrapper 🔴 CRITICAL

**Design Reference:** §1.2 "The Unified Principle" — "NO session['history']", §5.1 "Single Source of Truth"

**Current State:** `agent_pool.py` contains a 166-line `_InstanceConversationMapping` class that:
- Bridges writes to `instance_conversations[name]` with `instances[name].conversation`
- Implements bidirectional sync (read from instances, write propagates both ways)
- Has fallback dict storage for "session rename patterns"
- Explicitly marked as "compatibility shim — remove in Phase 6"

**Why This Violates Design:** The design explicitly states:
> "The API server NEVER holds its own copy. It always reads from the pool."

This class is a **dual-read wrapper** — it exists precisely because `api_server.py` still writes to `instance_conversations[name] = list(...)` in multiple places (lines 1802, 1863, 2172, 2213, 2227). The design says there should be NO such writes.

**Severity:** 🔴 CRITICAL — This is the exact structural duality the design was meant to eliminate. It creates a code path where `api_server.py` can write to one dict while agents read from another (until sync happens).

**Recommendation:** Remove in Phase 6. All writes to `instance_conversations[name]` must be eliminated from `api_server.py`. The shim exists because Phase 6 cleanup is incomplete.

---

### D2: Feature Flags Default to Legacy Mode 🔴 CRITICAL

**Design Reference:** §9.3 "Clean break approach — rewrite the core modules, then reconnect"

**Current State:** `config/unified.py`:
```python
USE_UNIFIED_ARCHITECTURE = os.environ.get('AC_USE_UNIFIED_ARCHITECTURE', '0') == '1'
USE_UNIFIED_STATE = os.environ.get('AC_USE_UNIFIED_STATE', '0') == '1'
USE_UNIFIED_LOOP = os.environ.get('AC_USE_UNIFIED_LOOP', '0') == '1'
```
All default to `False` (legacy mode).

**Why This Violates Design:** The design calls for a "clean break" — not a feature-flagged migration where legacy code runs by default. Phase 4 documentation explicitly states:
> "The old code used `USE_UNIFIED_STATE` and `USE_UNIFIED_ARCHITECTURE` flags to toggle between old and new paths. The new code has no such flags — it IS the unified path."

Yet `api_server.py` still contains `if USE_UNIFIED_STATE:` branches with dual-path logic (lines 519-573). When flags are False, the old dual-path code executes.

**Severity:** 🔴 CRITICAL — The system runs legacy code by default, directly contradicting the clean-break design principle.

**Recommendation:** Either:
1. Flip defaults to `True` and remove all `if USE_UNIFIED_STATE:` branches, or
2. Delete the feature flags entirely since Phases 1-6 are supposedly complete

---

### D3: `sub_agent_state` Still Heavily Used 🟠 MAJOR

**Design Reference:** §2.2 — Pool should NOT hold `sub_agent_state`; state lives in `instances[name].conversation`

**Current State:** `api_server.py` writes to and reads from `pool.sub_agent_state` at **30+ locations**:
- Line 85-91: `create_main_agent_instance()` populates `sub_agent_state['root']` and instance name
- Line 532-534: `get_session_history()` falls back to `sub_agent_state.get(instance_name)`
- Line 560: `get_agent_state()` reads from `sub_agent_state.get(instance_name)`
- Lines 1797-1801: Security Advisor registered in `sub_agent_state`
- Lines 1862-1863: Security advisor messages updated in `sub_agent_state` during streaming
- Line 624-625: `get_sub_agent_state()` iterates over `pool.sub_agent_state`

**Why This Violates Design:** The design explicitly says there is ONE source of truth — `instances[name].conversation`. Reading from `sub_agent_state` creates a second source that can diverge.

**Severity:** 🟠 MAJOR — Dual state sources that can diverge, exactly the problem the redesign was meant to solve.

**Recommendation:** Remove all `sub_agent_state` reads/writes in Phase 6. Replace with `pool.instances[name].conversation` reads and `pool.instances` iteration for sub-agent enumeration.

---

### D4: agent_invoker.py Still References Old Architecture 🟠 MAJOR

**Design Reference:** §3.1 — "All agents go through ExecutionEngine.run()"

**Current State:** `agent_cascade/compression/agent_invoker.py`:
- Line 127: Checks `hasattr(orchestrator, '_stream_sub_agent_call')` — the old method being replaced
- Lines 148-150: Calls `orchestrator._stream_sub_agent_call()` when orchestrator is available
- Line 201: Falls back to `comp_agent.run()` — the old direct execution pattern
- Lines 170-173, 215-219: Updates `sub_agent_state` during streaming (dual state)

**Why This Violates Design:** Compression agent invocation should go through the same `ExecutionEngine.run()` path as all other agents. The dual-path (call_agent pattern vs direct run()) contradicts "one loop, one path."

**Severity:** 🟠 MAJOR — Special-casing for compression agent creates a second execution path.

**Recommendation:** Refactor to always use `ExecutionEngine.run()` via the pool's unified `_create_and_run_agent()` helper. Remove the `_stream_sub_agent_call` check and direct `comp_agent.run()` fallback.

---

### D5: LoggerManager Returns NoOpLogger Placeholder 🟡 MINOR

**Design Reference:** §2.4 "Two-Layer Model" — JSONL file (append-only) + in-memory working set

**Current State:** `agent_pool.py` lines 1091-1150:
```python
class LoggerManager:
    def get_logger(self, instance_name, agent_class):
        with self._lock:
            if instance_name not in self._loggers:
                self._loggers[instance_name] = NoOpLogger(instance_name)
        return self._loggers[instance_name]

class NoOpLogger:
    """All methods are no-ops that emit a one-time warning."""
```

**Why This is a Deviation:** The design specifies `AgentInstanceLogger` with `log_message()` (append-only), `log_compression_marker()`, and session recovery from JSONL. Instead, we get a NoOp placeholder that emits warnings but does nothing.

**Severity:** 🟡 MINOR — Not a functional violation (the pool owns state regardless of logging), but means JSONL persistence is completely non-functional. Compression markers are not written to disk.

**Recommendation:** Implement the full `AgentInstanceLogger` in Phase 6 as specified in design §2.4. The NoOpLogger is a known placeholder.

---

### D6: IdleManager Not Implemented 🟡 MINOR

**Design Reference:** §5.4 — "IdleManager runs a background thread that periodically checks for idle agents"

**Current State:** `agent_pool.py` line 247-248:
```python
self._idle = IdleManager(self)  # TODO: Implement IdleManager for idle detection and auto-dismissal (Phase 2)
```
And line 1153-1154:
```python
# TODO: Implement IdleManager for idle detection and auto-dismissal (Phase 2)
# Placeholder removed to avoid consuming memory with an empty class.
```

**Why This is a Deviation:** The design specifies a full `IdleManager` class with `_check_loop()`, `_is_idle()`, `_auto_dismiss()` methods. None of this exists.

**Severity:** 🟡 MINOR — Auto-dismissal is missing, meaning abandoned agents accumulate in the pool indefinitely. Not a data integrity issue but a resource management gap.

**Recommendation:** Implement `IdleManager` as specified in design §5.4. Use the provided code template from the design doc.

---

### D7: `_handle_compress_context()` Does NOT Use _compression_lock 🟠 MAJOR

**Design Reference:** §6.4 — "Acquire per-agent threading lock before modifying conversation"

```python
# Design specification:
with inst._compression_lock:
    compress_context(self.pool, target_agent_name, fraction=0.5, force=True)
```

**Current State:** `execution_engine.py` lines 668-691:
```python
# NOTE: Do NOT wrap compress_context in _compression_lock — it internally
# calls agent_pool.get_conversation() which acquires the same lock.
# Holding the outer lock + inner lock = deadlock (non-reentrant Lock).
from agent_cascade.compression.core import compress_context as _compress
result = _compress(...)  # Called WITHOUT lock
```

**Why This Violates Design:** The design explicitly specifies wrapping `compress_context()` in `_compression_lock`. The implementation deliberately omits it due to a deadlock concern. However, this means the conversation is NOT protected during compression — other threads can read/write concurrently.

**Note:** The developer recognized a real technical issue (non-reentrant Lock would deadlock). But the fix should be either:
1. Change `_compression_lock` from `threading.Lock()` to `threading.RLock()`, or
2. Have `compress_context()` accept a pre-acquired lock, or
3. Document this as an explicit design deviation with justification

**Severity:** 🟠 MAJOR — Thread safety gap during compression operations. The lock exists but isn't used where the design says it should be.

**Recommendation:** Change `_compression_lock` from `threading.Lock()` to `threading.RLock()` in `AgentInstance`. This is the minimal fix that preserves the design intent while avoiding deadlock.

---

### D8: Tool Execution Passes Extra `messages` Parameter 🟡 MINOR

**Design Reference:** §3.2 — `_execute_tool(self, instance, tool_name, tool_args, messages)`

**Current State:** `execution_engine.py` line 558-563:
```python
return template._call_tool(
    tool_name, tool_args,
    agent_instance_name=instance.instance_name,
    agent_obj=self,
    messages=messages,  # Extra parameter not in design spec
)
```

**Why This is a Deviation:** The design shows `_call_tool(tool_name, tool_args, agent_instance_name=..., agent_obj=...)` — no `messages` parameter. However, this appears to be a justified addition for tool execution context.

**Severity:** 🟡 MINOR — Justified addition. Tools need message context for some operations. No design violation in spirit.

**Recommendation:** Acceptable deviation. Document in code comment as "added during implementation for tool context."

---

## 3. MISSING — Design Requirements Not Yet Implemented

### M1: `agent_cascade/agent_instance.py` as Separate File (Design §12 Checklist)
- **Design says:** `agent_instance.py` — AgentInstance dataclass (~100 lines)
- **Current state:** ✅ Exists at `agent_cascade/agent_instance.py`

### M2: `execution_manager.py` as Separate File (Design §12)
- **Design says:** `execution_manager.py` — ParallelAgentManager (~150 lines)
- **Current state:** ❌ `ParallelAgentManager` lives in `agent_pool.py` instead of its own file

### M3: `logger_manager.py` as Separate File (Design §12)
- **Design says:** `logger_manager.py` — Logger creation, session recovery (~200 lines)
- **Current state:** ❌ `LoggerManager` lives in `agent_pool.py` instead of its own file

### M4: `idle_manager.py` as Separate File (Design §12)
- **Design says:** `idle_manager.py` — Idle detection, auto-dismissal (~150 lines)
- **Current state:** ❌ Not implemented at all

### M5: `loop_detection.py` as Standalone Module (Design §3.1)
- **Design says:** "Loop detection is a standalone module (`agent_cascade/loop_detection.py`)"
- **Current state:** ❌ Loop detection is embedded in `execution_engine.py` as `_detect_loop()`

### M6: `compression_checker.py` as Separate Module (Design §12)
- **Design says:** `compression_checker.py` — Token accounting, compression triggers (~120 lines)
- **Current state:** ❌ Token counting is embedded in `execution_engine.py` (`_count_history_tokens()`, `_get_max_tokens()`)

### M7: Full Logger Implementation (Design §2.4)
- **Design says:** `AgentInstanceLogger` with `log_message()` (append-only), `log_compression_marker()`, session recovery
- **Current state:** ❌ NoOpLogger placeholder only

### M8: Clean Break — Remove Old Files (Design §9.2)
- **Design says:** Replace old files, remove dual-path code
- **Current state:** ❌ Old files still present: `agent_orchestrator.py` (126KB), `agent_pool.py` (52KB), `agent_logger.py` (21KB)

---

## 4. EXCESS — Things in Code Not in Design

### E1: `_InstanceConversationMapping` Compatibility Shim (371 lines of shim code)
- **Not in design:** The design shows a clean pool with direct attribute access
- **Actual code:** 166-line custom dict class with `__getitem__`, `__setitem__`, `pop()`, `items()`, `values()`, `keys()`, `__contains__`, `clear()` — all bridging to `instances[name].conversation`
- **Justification:** Phase 5 bridge for gradual migration (documented as such)
- **Status:** Should be removed in Phase 6

### E2: Backward Compatibility Shims on AgentPool (lines 409-456, 693-713)
- `is_halted()` alias for `is_instance_halted()`
- `instance_classes` property derived from instances
- `instance_loggers` property
- `agents` property alias for templates
- `_state_lock` property delegating to `_execution._state_lock`
- **Not in design:** Design shows clean pool with canonical method names

### E3: Feature Flags Module (`config/unified.py`)
- Three feature flags that gate behavior between old and new paths
- **Not in design:** Design calls for "clean break" — no toggling

### E4: `last_tool_args` Dict on Pool (line 257)
- Tool argument cache for `__USE_PREV_ARG__` placeholder resolution
- **Not explicitly in design:** But a justified addition from implementation

### E5: `terminated_instances` Set on Pool (line 253)
- Instances marked for immediate termination
- **Not in design:** Design doesn't mention this as separate from halt state
- **Status:** Likely needed for the dismiss_instance flow; acceptable deviation

### E6: `_compression_halted` Set on Pool (line 251)
- Tracks instances halted by forced compression vs manual halts
- **Not in design:** Design only mentions `_halted_instances` set
- **Status:** Justified — distinguishes compression halts from manual halts for selective resume

### E7: Session Dictionary Remnants in api_server.py (~2393 lines)
- `session['session_name']`, `session['generating']`, `session['stop_requested']`, `session['generation_id']`, `session['generate_cfg']`, `session['last_turn_snapshots']`
- **Not in design:** Design says api_server is a "state broadcaster, not an execution engine" and should not hold session state
- **Status:** These are lightweight control flags (not conversation data). Acceptable as transient coordination state, but the design envisions them being on the pool or removed.

### E8: `rebuild_working_set` in compression.helpers (imported by execution_engine.py)
- Used to rebuild working sets after compression
- **Not explicitly in design:** But a necessary implementation detail for the marker stacking model
- **Status:** Justified addition

---

## 5. Overall Assessment

### Compliance Scorecard

| Category | Count | Details |
|----------|-------|---------|
| **PASS** | 20+ | Core architecture, execution engine, pool model, thread safety basics, loop detection, rollback |
| **CRITICAL Deviations** | 2 | `_InstanceConversationMapping` dual-read wrapper, feature flags defaulting to legacy |
| **Major Deviations** | 3 | `sub_agent_state` dual state, agent_invoker old references, missing compression lock |
| **Minor Deviations** | 3 | NoOpLogger placeholder, IdleManager missing, extra parameters |
| **Missing from Design** | 8 | Separate manager files, loop_detection module, compression_checker, full logger, old file cleanup |
| **Excess in Code** | 8 | Compatibility shims, feature flags, backward compat aliases, session dict remnants |

### Verdict: 🟠 NEEDS WORK — NOT YET COMPLIANT

The unified architecture is **structurally sound** where implemented. The core execution engine, pool model, and phase-based design are all correct. However, the codebase is in a **hybrid state**:

1. **New code paths** (via `api_integration.py`) are fully compliant
2. **Old code paths** (still active via feature flags) remain functional and used by default
3. **Compatibility shims** bridge the gap but violate the design principle of single source of truth

### Required Changes Before PASS

#### 🔴 Must Fix (Blockers)
1. **Remove `_InstanceConversationMapping` shim** — Eliminate all `instance_conversations[name] = ...` writes from `api_server.py`. Direct readers are fine; writers must go through `pool.instances[name].conversation`.
2. **Flip feature flag defaults to True or remove flags entirely** — The system should run unified mode by default, not legacy mode.

#### 🟠 Should Fix (High Priority)
3. **Eliminate `sub_agent_state` reads/writes** — Replace with `pool.instances` iteration and `instances[name].conversation` reads.
4. **Refactor agent_invoker.py** — Remove `_stream_sub_agent_call()` path; use unified `ExecutionEngine.run()`.
5. **Change `_compression_lock` to `RLock`** — Restore the design-specified lock usage in `_handle_compress_context()`.

#### 🟡 Nice to Have (Lower Priority)
6. Extract `ParallelAgentManager`, `LoggerManager` into separate files as specified in design §12 checklist
7. Create `loop_detection.py` and `compression_checker.py` modules
8. Implement full `IdleManager` and `AgentInstanceLogger`
9. Remove old standalone files (`agent_orchestrator.py`, old `agent_pool.py`, `agent_logger.py`)

---

## Appendix: File-by-File Compliance Summary

| File | Status | Key Issues |
|------|--------|------------|
| `agent_cascade/agent_instance.py` | ✅ COMPLIANT | Matches design exactly |
| `agent_cascade/agent_pool.py` | 🟠 NEEDS WORK | Contains `_InstanceConversationMapping`, NoOpLogger, missing managers |
| `agent_cascade/execution_engine.py` | 🟢 MOSTLY OK | Missing lock usage for compression, extra `messages` param |
| `agent_cascade/api_integration.py` | ✅ COMPLIANT | All functions read from pool only |
| `agent_cascade/compression/agent_invoker.py` | 🟠 NEEDS WORK | References `_stream_sub_agent_call`, dual execution paths |
| `api_server.py` | 🔴 NON-COMPLIANT | Feature flag branches, sub_agent_state writes, _InstanceConversationMapping writes |
| `config/unified.py` | 🔴 NON-COMPLIANT | Feature flags default to legacy mode |