# Execution Engine Audit Report

**File:** `agent_cascade/execution_engine.py`  
**Lines:** 3,727  
**Date:** 2026-06-17  
**Auditor:** ExecEngineAuditor  
**Classification:** 🔴 CRITICAL — This is the core execution coordinator; issues here affect all agents.

---

## Executive Summary

This file claims to implement "phase methods (~20-60 lines) that are independently testable" but delivers the opposite: several methods exceed 400 lines with deeply nested state machine logic, duplicated code blocks, and responsibilities that span orchestration, WebSocket management, instance lifecycle, error recovery, and UI streaming. The class is labeled "stateless" but holds `self.pool`, directly manages WebSocket pushes, imports from `api_integration` mid-function, and tightly couples to the pool's execution model.

**Overall Verdict: NEEDS MAJOR REFACTORING**

The file needs to be split into 4-5 focused classes before any other work on it is safe.

---

## Findings (Prioritized)

### 🔴 Critical Issues

#### C1: `run()` Method — ~420 Lines, Unmaintainable State Machine
**Severity:** 🔴 Critical  
**Location:** Lines 382–796

The docstring claims phase methods are "~20-60 lines" but `run()` alone is **~415 lines**. It contains:
- A `while` loop with nested `if/elif/else` chains for SLEEPING state handling (lines 471–637)
- Three separate slot re-acquisition blocks with near-identical code
- State transitions in six different places
- Error handling, logging, and cleanup all intertwined

```python
# Lines 471-637: The SLEEPING state block alone is ~167 lines
while turns_available > 0:
    if instance.state == AgentState.SLEEPING:
        # ... 167 lines of async drain, timeout check, slot re-acquire, retry logic
```

**Impact:** No single developer can hold the full control flow in memory. New bugs are inevitable. The state transitions between IDLE→RUNNING→SLEEPING→COMPLETING→IDLE happen in 8+ scattered locations with no single source of truth.

**Recommendation:** Extract a dedicated `AgentStateMachine` class that handles:
- SLEEPING state transition logic (async drain, timeout, re-acquire)
- State guards and transitions (centralized in one place)
- Turn counting and limit enforcement

---

#### C2: `_create_and_run_agent()` — ~510 Lines, Does Everything
**Severity:** 🔴 Critical  
**Location:** Lines 2675–3184

This method is the single largest method in the file. It performs:
1. Instance reuse/creation logic (lines 2702–2747)
2. System message building (lines 2753–2774)
3. Task message construction with multimodal image propagation (lines 2809–2867)
4. Settings propagation from caller (lines 2899–2962)
5. Active stack tracking (lines 2964–2966)
6. WebUI state initialization AND WebSocket push (lines 2968–3007)
7. Engine loop execution with its own throttled WebSocket push logic (lines 3009–3115)
8. Final state update AND final WebSocket push (lines 3120–3164)
9. Active stack cleanup in finally block (lines 3167–3173)

```python
# Lines 3017-3022: The engine loop runner has its own WebSocket logic INSIDE the method
_final_resp = []
_update_counter = 0
_last_sub_send = 0.0
_sub_send_interval = 0.15
_ws_error_count = 0
_stream_pushing_disabled = False
```

**Impact:** This method violates every principle of single responsibility. It cannot be unit-tested in isolation because it couples instance creation, message building, settings propagation, state tracking, WebSocket pushing, and engine execution all together.

**Recommendation:** Split into:
- `_create_agent_instance()` — handles reuse logic, system message, task message
- `_propagate_settings()` — handles caller→child settings inheritance
- `_run_sub_agent_loop()` — handles the engine iteration with streaming
- WebSocket pushing should be delegated to a dedicated `StreamPublisher` service

---

#### C3: `_process_response()` — ~290 Lines of Scattered Concerns
**Severity:** 🔴 Critical  
**Location:** Lines 1504–1796

This method handles:
1. Gemma thought tag normalization (lines 1518–1527)
2. Thinking block stripping from content AND function args (lines 1529–1541)
3. Truncation detection (lines 1543–1546)
4. Logger persistence sync (lines 1548–1570)
5. Message appending to all working sets (lines 1572–1579)
6. Token cache invalidation (line 1582)
7. Usage info extraction from LLM response (lines 1584–1597)
8. JSONL logging of turn output (lines 1599–1605)
9. Auto-continue on truncation (lines 1607–1620)
10. Tool detection and execution loop (lines 1622–1744) — ~120 lines
11. Orphaned tool call handling (lines 1746–1784) — ~38 lines
12. Post-tool urgent message injection (lines 1786–1793)

**Impact:** Tool execution logic is embedded deep inside response processing, making it impossible to test tool execution paths in isolation. The orphaned tool call handling duplicates tool detection logic from the main loop.

**Recommendation:** Extract:
- `_execute_tools_from_output()` — dedicated tool execution method
- `_handle_orphaned_tools()` — placeholder generation for halted agents
- `_normalize_response_messages()` — Gemma/strip thinking block normalization

---

#### C4: Slot Re-acquisition Code Duplicated 3 Times Identically
**Severity:** 🔴 Critical  
**Location:** Lines 436–449, 513–525, 608–621

```python
# Line 436-449 (initial acquire) and lines 513-525, 608-621 are nearly identical:
if not skip_slot_acquire and hasattr(self.pool, '_acquire_slot'):
    try:
        logger.debug(f"[SLOT_ACQUIRE] After wakeup ...")
        instance._slot_release = self.pool._acquire_slot(instance.agent_class, instance.instance_name)
        logger.debug(f"[SLOT_ACQUIRED] After wakeup ...")
    except Exception as e:
        logger.error(f"[SLOT_ACQUIRE_FAILED] ... {e}")
        raise
```

**Impact:** Any change to slot acquisition logic (error handling, logging format, retry behavior) must be applied in 3 places. This is a textbook violation of DRY. The `_release_slot` helper was created at line 1884 but no corresponding `_acquire_slot_with_logging` helper exists for the acquire path.

**Recommendation:** Create `_acquire_slot_with_logging(instance)` that encapsulates the check, log, acquire, and error handling pattern. Replace all 3 occurrences.

---

### 🟠 High Issues

#### H1: `_force_compression()` — ~170 Lines, Too Many Responsibilities
**Severity:** 🟠 High  
**Location:** Lines 986–1158

This method handles:
1. Cooldown check (lines 993–1009)
2. Overfeeding detection (lines 1015–1029)
3. Agent halting for other agents (lines 1031–1035)
4. Compression invocation (lines 1037–1050)
5. Working set rebuild (line 1054)
6. Notification dedup guard (lines 1081–1095)
7. Message pool validation + recovery from log (lines 1100–1127) — ~27 lines nested in try/except
8. Logger sync (lines 1132–1139)
9. Cooldown flag setting (line 1141)

**Impact:** The nested try/except for recovery (lines 1104–1127) is deeply indented and hard to follow. If compression ever needs a different recovery strategy, the entire method must be re-read.

**Recommendation:** Extract `_recover_message_pool_from_log()` as a standalone method.

---

#### H2: `_handle_call_agent()` — ~250 Lines, Sync/Async Branching Nightmare
**Severity:** 🟠 High  
**Location:** Lines 2120–2369

This method contains:
- Validation logic (lines 2140–2198)
- Slot collision detection with a nested `_reacquire_slot` function definition (lines 2230–2266)
- SYNC path: slot release, child execution, slot reacquire (~80 lines, lines 2217–2340)
- ASYNC path: registration (~20 lines, lines 2341–2369)

The nested `_reacquire_slot` function (lines 2230–2266) is particularly problematic — it's defined inside a method that's already too deep, and it has its own retry logic with time.sleep.

**Recommendation:** Extract sync/async branching into a dedicated `AgentCallRouter` class or at minimum two separate methods: `_run_child_sync()` and `_run_child_async()`.

---

#### H3: Token Cache Invalidation Called 18 Times — Repetitive Pattern
**Severity:** 🟠 High  
**Location:** Throughout file (lines 366, 841, 1056, 1111, 1210, 1582, 1619, 1734, 1784, 2445, 2626, 2646, 2788, 2889, 3260 + more)

Every place the conversation is mutated, `_invalidate_token_cache(instance)` is called. Sometimes followed by `inst._cached_token_count = 0` (line 1211). The pattern is:

```python
_invalidate_token_cache(instance)  # Clears _last_actual_token_count and _last_token_count_conversation_length
# Sometimes also:
inst._cached_token_count = 0       # Redundant — _invalidate_token_cache already does this
```

**Impact:** While `_invalidate_token_cache()` itself is a small function, the repetition means every mutation point must remember to call it. If a new mutation path is added and this is forgotten, stale token counts cause incorrect compression triggers.

**Recommendation:** Create a context manager `with instance.token_cache_invalidated():` that automatically invalidates on exit. Apply it around all conversation mutations.

---

#### H4: `_call_llm_with_injection()` — ~160 Lines, Retry Logic Buried
**Severity:** 🟠 High  
**Location:** Lines 1258–1417

The retry loop (lines 1294–1403) contains:
- Error classification logic with two tuple lists of keywords (lines 1345–1361)
- Backoff calculation
- UI message yielding for retries

The error classification is particularly fragile — it uses substring matching in lowercased error strings, which can produce false positives and false negatives. For example, `'HTTP'` is deliberately excluded (line 1344) because it matches URLs, but other error patterns may have similar issues.

**Recommendation:** Extract retry logic into a `RetryWithBackoff` utility class that accepts an error classification function as a parameter.

---

### 🟡 Medium Issues

#### M1: 47 `[CALL_AGENT_DEBUG]` Log Lines — Mostly Noise
**Severity:** 🟡 Medium  
**Location:** Throughout file (47 occurrences)

The tag is used extensively in the following patterns:
- **Entry/Exit logging** for every method (~20 occurrences) — useful during debugging but adds I/O overhead on every call
- **Early exit reason logging** (~10 occurrences) — only valuable if something actually goes wrong
- **State transition logging** (~7 occurrences) — partially useful

Most entry/exit logs at DEBUG level will never be seen in production. The pattern:
```python
logger.debug(f"[CALL_AGENT_DEBUG] _handle_call_agent ENTRY — caller={caller_name}, args_type={type(args).__name__}...")
# ... 200+ lines of code ...
logger.debug(f"[CALL_AGENT_DEBUG] EXIT (async) — caller={caller_name}, target={instance_name}, function_id={function_id}")
```

adds measurable I/O overhead to the hot path (every tool call, every LLM call).

**Recommendation:** 
- Remove entry/exit logging from all methods except `run()`
- Keep EXIT logs only for ERROR-level conditions
- Use a `@debug_trace` decorator that's conditionally enabled via environment variable

---

#### M2: `_rebuild_working_set()` — Redundant with `_force_compression()` Path
**Severity:** 🟡 Medium  
**Location:** Lines 1172–1224

This method is called from multiple places (_force_compression, _handle_compress_context, _handle_compress_command, tool execution for compress_context) but contains significant duplication:

```python
# Line 1209-1211: Manual cache invalidation that duplicates _invalidate_token_cache
if inst:
    _invalidate_token_cache(inst)
    inst._cached_token_count = 0  # Redundant — _invalidate_token_cache already sets this to 0
```

Also calls `_clear_preprocess_cache()` (line 1217), which is another side effect not obvious from the method name.

**Recommendation:** Rename to `_rebuild_and_invalidate()` to reflect actual behavior, or extract cache invalidation into a separate step.

---

#### M3: `validate_message_pool()` at Module Level — Tight Coupling
**Severity:** 🟡 Medium  
**Location:** Lines 3663–3727

This function is called from:
- `_force_compression()` (line 1101)
- `_handle_compress_context()` path via tool execution (lines 1713, 2614)
- `_handle_compress_command()` (lines 2614, 2621, 2642)

It does string-based content comparison with a fixed 500-char truncation (line 3693), which is an implementation detail of the compression module. If the message format changes (e.g., new multimodal fields), this validation needs updating too.

**Recommendation:** Move to `agent_cascade/compression/` where it logically belongs. Make content comparison configurable rather than hardcoded to 500 chars.

---

#### M4: `_handle_compress_command()` — ~205 Lines, Command Processing Mixed with Tool Execution
**Severity:** 🟡 Medium  
**Location:** Lines 2468–2673

This method handles:
1. /compress command detection (lines 2483–2498)
2. Command clearing with Unicode replacement (lines 2500–2505)
3. Fraction parsing and clamping (lines 2507–2517)
4. Tool availability check (lines 2519–2528)
5. Preview generation via tool.call() (lines 2532–2555)
6. User approval request (lines 2557–2577)
7. Compression application (lines 2587–2664) — ~77 lines
8. Message pool validation + recovery (~25 lines nested)
9. Working set rebuild
10. Logger sync

The preview→approval→apply pattern is a multi-step workflow that should be its own class or at least have each step as a separate method.

**Recommendation:** Extract into a `CompressionCommandHandler` class with methods: `_detect_command()`, `_generate_preview()`, `_request_approval()`, `_apply_compression()`.

---

#### M5: Dead/Stale Comments — "Bug3 fix" Appears 4 Times
**Severity:** 🟡 Medium  
**Location:** Lines 1116, 1140, 2446, 2648

```python
# Line 1116: Bug3 fix: Set cooldown flag after successful recovery (compression occurred)
# Line 1140: Bug3 fix: Set cooldown flag to suppress loop detection on next turn after compression
# Line 2446: Bug3 fix: Set cooldown flag to suppress loop detection on next turn after compression
# Line 2648: Bug3 fix: Set cooldown flag to suppress loop detection on next turn after compression
```

The exact same comment appears 4 times (lines 1140 and 2648 are identical). "Bug3" references a bug tracker that's no longer accessible in this codebase. The comments don't explain what Bug3 was or why it required this fix.

**Recommendation:** Replace with inline explanation of the actual behavior:
```python
# Suppress loop detection for one turn after compression — compressed conversations have
# concentrated patterns that can falsely trigger the repeated-sequence detector.
instance._suppress_loop_detection_next_turn = True
```

---

#### M6: Feature Tags Without Documentation
**Severity:** 🟡 Medium  
**Location:** Lines 923, 939, 993, 1015, 1177, 1207, 1433, 1457, 1464, 1486, 1493, 1584

Comments like `# Feature 006`, `# Feature 018`, `# Feature 019`, `# Feature 022` reference feature numbers that don't exist in any accessible DESIGN_REWRITE.md or feature tracking document. They provide no actionable information.

**Recommendation:** Remove these tags or replace with brief descriptions of what each feature does.

---

#### M7: `import` Statements Inside Functions
**Severity:** 🟡 Medium  
**Location:** Multiple locations

```python
# Line 1043: from agent_cascade.compression.core import compress_context as _compress
# Line 1195: from agent_cascade.compression.helpers import rebuild_working_set as _rws
# Line 2228: from agent_cascade.compression.helpers import extract_instance_output
# Line 2433: from agent_cascade.compression.core import compress_context as _compress
# Line 2994: from agent_cascade.api_integration import build_stream_update_from_pool, _put_stream_update
# Line 3081: from agent_cascade.api_integration import build_stream_update_from_pool, _put_stream_update
# Line 3151: from agent_cascade.api_integration import build_stream_update_from_pool, _put_stream_update
# Line 3297: from agent_cascade.api_integration import build_stream_update_from_pool, _put_stream_update
```

The `api_integration` import at lines 2994, 3081, 3151, 3297 is duplicated in the same method. The module-level comment on line 34-37 acknowledges this pattern but doesn't solve it. These imports also create circular import risk if `api_integration` imports from `execution_engine`.

**Recommendation:** Move all cross-module imports to module level with lazy evaluation (e.g., import inside function body only for truly optional dependencies). Consolidate the repeated `api_integration` imports into a single module-level conditional import or a dedicated constants/helper module.

---

### 🔵 Low Issues

#### L1: `_detect_loop()` — O(n²) Pattern Matching on Window
**Severity:** 🔵 Low  
**Location:** Lines 3371–3439

The nested loop at lines 3415-3427 checks pattern lengths 1-20 against windows of up to 40 messages. For each (L, i) pair it compares K subsequences. Worst case: ~20 × 40 × 2 comparisons, which is fine for now but scales poorly with larger windows or longer patterns.

**Recommendation:** Use rolling hash (Rabin-Karp style) for O(n) pattern matching if the window grows beyond 100 messages.

---

#### L2: `_count_history_tokens()` — Full Recalculation on Every Call
**Severity:** 🔵 Low  
**Location:** Lines 3326–3369

The cache check (lines 3333-3334) only works if `len(messages)` hasn't changed. But messages can be mutated in-place (appended to) without changing the list length, causing stale cache hits. The conversation is appended to at lines 1573-1577 but the cache is invalidated separately at line 1582 — if these execute out of order (e.g., due to threading), the cache returns stale data.

**Recommendation:** Cache invalidation should be atomic with mutation. Use a version counter on the conversation list that increments on every mutation.

---

#### L3: `_truncate_tool_result()` — Triple Token Counting
**Severity:** 🔵 Low  
**Location:** Lines 3494–3607

This method calls `qwen_count()` once per message in the full loop (lines 3527-3540), then again for the tool result estimation (line 3548 uses `len // 3` as a rough estimate). The token counting here duplicates work already done by `_count_history_tokens()`.

**Recommendation:** Pass pre-counted tokens from the caller instead of recounting.

---

#### L4: Redundant `isinstance` Checks Throughout
**Severity:** 🔵 Low  
**Location:** Lines 832-833, 846, 1520, 1749, etc.

The pattern `msg.get('role') if isinstance(msg, dict) else getattr(m, 'role', '')` appears **dozens of times**. There's no shared helper for this.

**Recommendation:** Create a `get_msg_role(msg)` and `get_msg_content(msg)` utility function at module level to eliminate repetition.

---

#### L5: `_strip_thinking_blocks()` — Import Inside Method
**Severity:** 🔵 Low  
**Location:** Line 3458

```python
def _strip_thinking_blocks(self, text: str) -> str:
    import re  # ← Unnecessary — re is imported at module level on line 18
```

**Recommendation:** Remove the redundant `import re`.

---

#### L6: `_append_system_notification()` — Modifies Messages In-Place
**Severity:** 🔵 Low  
**Location:** Lines 3467–3492

This method modifies the last message's content directly. If called on a shared list (e.g., `messages` that's also referenced by other code), it creates unexpected side effects. The comment says "preventing duplicates" but the guard prefix check only works for string content, not multimodal content arrays in all cases.

**Recommendation:** Document that this mutates in-place. Consider returning a new message instead.

---

## Architecture Assessment

### Claim: "Stateless Execution Engine"
**Status: FALSE**

The engine holds `self.pool` (line 304), which gives it access to ALL agent instances, the API router, WebSocket queues, telemetry, settings, and more. It directly:
- Pushes WebSocket messages (`api_integration.build_stream_update_from_pool`)
- Tracks active stacks via `self.pool._execution.active_stack`
- Manages instance state via `self.pool.instance_state`
- Calls pool methods for slot acquisition

A truly stateless engine would receive only the data it needs per-call and produce outputs without side effects. This engine is a **God Object** with responsibilities spanning 6 distinct domains.

### Responsibility Breakdown (Current)
| Domain | Methods | Lines |
|--------|---------|-------|
| Engine orchestration (`run`, phase methods) | run, _setup_turn, _pre_llm_checks, _call_llm_with_injection, _process_response, _post_turn_checks | ~1,600 |
| Compression (force, command, tool handling) | _force_compression, _inject_compression_warning, _rebuild_working_set, _handle_compress_context, _handle_compress_command | ~720 |
| Agent lifecycle (creation, system agents) | _create_and_run_agent, _create_system_agent | ~1,140 |
| Tool execution & dispatch | _execute_tool, _handle_call_agent, _handle_dismiss_agent, _resolve_placeholders, _cache_tool_args | ~580 |
| Utilities (token counting, loop detection, truncation) | _get_max_tokens, _count_history_tokens, _detect_loop, _detect_tool, _strip_thinking_blocks, _truncate_tool_result, _write_spillover_file | ~400 |

### Proposed Refactoring Target
| Class | Responsibility | Estimated Lines |
|-------|---------------|-----------------|
| `ExecutionEngine` | Core turn loop orchestration, phase dispatch | ~600 |
| `AgentLifecycleManager` | Instance creation, reuse, settings propagation | ~400 |
| `CompressionHandler` | All compression logic (force, command, tool) | ~500 |
| `ToolDispatcher` | Tool execution, call_agent routing, argument resolution | ~450 |
| `StreamPublisher` | WebSocket push, UI state updates, throttling | ~200 |

---

## Testing Assessment

### Currently Testable Methods (in isolation)
- `_make_user_message()` — trivial, but testable
- `_make_async_result_message()` — trivial, but testable
- `_replace_section()` — pure function, testable
- `_detect_tool()` — pure-ish function, testable
- `_strip_thinking_blocks()` — pure function, testable

### NOT Testable (tightly coupled)
- `run()` — requires full AgentPool, multiple locks, WebSocket infrastructure
- `_process_response()` — calls pool methods, logger, tool execution
- `_handle_call_agent()` — accesses pool instances, active_stack, WebSocket queues
- `_create_and_run_agent()` — creates real instances, touches pool, pushes WebSockets
- `_force_compression()` — imports compression module, halts other agents

**Recommendation:** All methods that call `self.pool.*`, access `self.pool._execution.*`, or push WebSocket events need either:
1. A mockable Pool interface (protocol/ABC)
2. Extraction into testable helper functions with dependency injection

---

## Required Changes Summary

### Must Fix (Critical)
1. **Split `run()`** — Extract state machine logic into a separate class
2. **Split `_create_and_run_agent()`** — Separate instance creation from engine loop execution
3. **Extract slot acquisition helper** — Replace 3 duplicate blocks with one method
4. **Split `_process_response()`** — Extract tool execution into its own method

### Should Fix (High)
5. **Split `_force_compression()`** — Extract recovery logic
6. **Split `_handle_call_agent()`** — Separate sync/async routing
7. **Create token cache invalidation context manager** — Eliminate 18 call sites
8. **Extract LLM retry logic** — Move to utility class

### Should Fix (Medium)
9. **Reduce debug logging** — Remove entry/exit logs from hot path
10. **Fix stale "Bug3" comments** — Replace with behavioral descriptions
11. **Remove feature number tags** — Replace with descriptions or remove
12. **Consolidate `api_integration` imports** — Single module-level import
13. **Split `_handle_compress_command()`** — Extract each step

### Nice to Have (Low)
14. **Add shared `get_msg_role()` / `get_msg_content()` helpers**
15. **Remove redundant `import re` in `_strip_thinking_blocks()`**
16. **Optimize `_detect_loop()` with rolling hash if window grows**
17. **Fix token cache atomicity — version counter on conversation**
18. **Move `validate_message_pool()` to compression module**

---

## Final Verdict

**FAIL — NEEDS MAJOR REFACTORING**

This file is the single most important file in the codebase and currently exhibits:
- **3,727 lines** where a well-factored design would be ~1,500 across 4-5 focused classes
- **47 debug log statements** tagged `[CALL_AGENT_DEBUG]` creating noise in production
- **3 exact code duplicates** for slot acquisition with no shared helper
- **Zero testable methods** beyond trivial utilities (everything depends on `self.pool`)
- **False "stateless" claim** — the engine directly manages WebSocket pushes and instance state

The file was clearly grown organically through iterative bug fixes ("Fix #1", "Bug3 fix", "Feature 006") without periodic architectural review. Each fix added code to the nearest method rather than restructuring to accommodate the new concern.

**Priority:** This refactoring should be done before any new features are added to the execution engine, as adding more complexity to this structure will make future maintenance progressively harder.