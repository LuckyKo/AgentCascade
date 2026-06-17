# Phase 3.2 Review: `_create_and_run_agent()` Extraction into 6 Sub-Methods

**File:** `agent_cascade/execution_engine.py` (3777 lines)  
**Plan Reference:** `audit_reports/execution_engine_refactor_plan.md` §3.2 (lines 556–726)  
**Reviewer:** Phase3Reviewer  
**Date:** 2026-06-17  

---

## Verdict: ✅ PASS

All 6 methods were extracted correctly. The coordinator method `_create_and_run_agent()` calls all sub-methods in the expected order. Behavioral preservation is solid — logging, error handling, and WebSocket push locations are intact. The file compiles cleanly.

---

## Findings

### 🔴 Critical: None

No critical issues found.

---

### 🟠 Major: None

No major issues found.

---

### 🟡 Minor: 2

#### 1. `_initialize_instance_conversation` — Task message appended outside lock scope (L2950)

**Location:** Line 2950 (`conv.append(task_msg)`)  

**Issue:** In the reuse path, `instance.conversation` is modified under `instance._compression_lock` (lines 2921–2943), but the task message append at line 2950 occurs **outside** that lock:

```python
# Lines 2920-2946 — inside lock
with token_cache_invalidated(instance):
    with instance._compression_lock:
        # ... reset state, update conversation[0] ...
        conv = instance.conversation   # L2946

# Line 2950 — OUTSIDE the lock!
conv.append(task_msg)
```

Since `conv` is a reference to `instance.conversation`, this append modifies the shared list without holding the lock. A concurrent reader (e.g., another thread accessing `instance.conversation` through `get_conversation()`) could see a partially-constructed list during the append.

**Impact:** Low practical risk — `_create_and_run_agent()` is called in a single-threaded execution path and no other code reads `instance.conversation` concurrently during this call sequence. However, it's technically a lock scope violation that could bite if future changes introduce concurrent access.

**Fix:** Move the append inside the lock:

```python
with token_cache_invalidated(instance):
    with instance._compression_lock:
        # ... reset state, update conversation[0] ...
        conv = instance.conversation
        conv.append(task_msg)  # Move here, inside lock
```

---

### 🔵 Nit: 1

#### 2. `_find_or_create_instance` — Redundant condition at L2746

**Location:** Line 2746 (`if inst is None or not is_reuse:`)  

**Issue:** The condition `inst is None or not is_reuse` is logically equivalent to just `not is_reuse`. When `is_reuse` is `True`, we know `inst` was assigned at line 2732 (so `inst is None` is always `False`). The `or inst is None` clause can never change the outcome.

```python
# Line 2746: Redundant check
if inst is None or not is_reuse:
```

**Impact:** Zero functional impact — code behaves identically. Purely a readability concern.

**Fix:** Simplify to `if not is_reuse:`.

---

## Completeness Check

| # | Extracted Method | Present? | Line |
|---|-----------------|----------|------|
| 1 | `_find_or_create_instance` | ✅ | L2698 |
| 2 | `_build_system_message` | ✅ | L2778 |
| 3 | `_build_task_message` | ✅ | L2813 |
| 4 | `_initialize_instance_conversation` | ✅ | L2890 |
| 5 | `_propagate_settings` | ✅ | L2977 |
| 6 | `_push_subagent_stream_update` | ✅ | L3066 |

Coordinator calls: All 6 methods are called from `_create_and_run_agent()` (L3127–L3169) in the correct order.

---

## Correctness Verification

### 1. `_find_or_create_instance` — Returns `(instance, is_reuse)` ✅
- **Reuse path:** Lines 2727–2741 — Sets `inst = existing`, `is_reuse = True`, returns `(inst, True)`. ✓
- **Fresh creation path:** Lines 2746–2770 — Creates new `AgentInstance`, returns `(inst, False)`. ✓
- **force_fresh=True:** Bypasses reuse check entirely at line 2727. ✓
- **Active instance replacement warning:** Lines 2761–2765 fire when an active instance exists but can't be reused. ✓

### 2. `_build_system_message` — Injects instance name ✅
- Line 2809: `lines[0] = f"You are {instance_name}."` — Replaces first line with identity statement. ✓
- Error handling: Raises `ValueError` if template not found (line 2801). ✓

### 3. `_build_task_message` — Propagates multimodal images ✅
- Scans caller's conversation via `self.pool.get_conversation(caller)` (line 2847). ✓
- Handles both dict-style and object-style messages (via `_safe_get_role`/`_safe_get_content` helpers, lines 2840–2844). ✓
- Includes images referenced in task text (lines 2861–2865). ✓
- Also includes images from last user message even if not referenced (lines 2867–2882). ✓
- Returns multimodal `Message` when images present, plain text otherwise (lines 2885–2888). ✓

### 4. `_initialize_instance_conversation` — Handles both paths ✅
- **Reuse path:** Resets stale state (`compression_summary`, `latest_marker_index`, `_generate_cfg_override`, `max_turns`, `is_terminated`, `_slot_release`) under lock (lines 2922–2934). Updates system message in-place. Appends task message. Syncs logger via `update_history()` (line 2957). ✓
- **Fresh path:** Builds `[sys_msg, task_msg]` list. Assigns to `instance.conversation`. Logs both messages individually. ✓

### 5. `_propagate_settings` — All settings propagated ✅
- `max_turns`: Read from caller instance directly (line 3017), fallback to 50. ✓
- `max_input_tokens`: Read from caller's `_generate_cfg_override`, with router fallback (lines 3032–3043). ✓
- `disabled_tools`: Merged with existing disabled tools, deduplicated (lines 3053–3062). ✓

### 6. `_push_subagent_stream_update` — Pool from self.pool ✅
- Signature is `_push_subagent_stream_update(self, caller: str)` — **no pool parameter**. ✓
- Internally reads `self.pool._ws_send_queue`, `self.pool._ws_loop` (lines 3080–3081). ✓
- Passes `pool=self.pool` to `build_stream_update_from_pool()` (line 3087). ✓

---

## Behavioral Preservation

### Logging Statements ✅
All logging preserved:
- `[CALL_AGENT_DEBUG] ENTRY/EXIT` in coordinator (lines 3120–3124, 3303–3308)
- `[INSTANCE REUSE]` / `[NEW INSTANCE]` in `_find_or_create_instance` (lines 2738–2741, 2763–2766, 2771–2774)
- `NO TEMPLATE` error in `_build_system_message` (line 2800)
- Logger sync failures wrapped in try/except with debug fallbacks (lines 2958–2959, 2968–2973)
- Settings propagation failure logged at debug level (line 3064)
- WebSocket push failures logged at debug level (line 3098)

### Error Handling ✅
All extracted methods have appropriate error handling:
- `_build_system_message`: Raises `ValueError` for missing template. ✓
- `_initialize_instance_conversation`: Logger operations wrapped in try/except. ✓
- `_propagate_settings`: Entire body wrapped in try/except (line 3063). ✓
- `_push_subagent_stream_update`: All exceptions caught, logged at debug (line 3097–3098). ✓

### WebSocket Push Locations ✅
Three push points preserved:
1. **Initial** (L3169): After state init, before `run()` starts. ✓
2. **Periodic** (L3234): Every `_sub_send_interval` (0.15s) during execution loop. ✓
3. **Final** (L3288): After loop completes, with `_stream_pushing_disabled` guard. ✓

---

## Quality Checks

| Check | Result |
|-------|--------|
| Python syntax compile | ✅ Clean |
| Docstrings on all extracted methods | ✅ All 6 have comprehensive docstrings |
| Method signature matches plan | ✅ All match |
| Return types annotated | ✅ `Tuple[AgentInstance, bool]`, `Message`, `List[Message]`, `None` |

---

## Summary

**Phase 3.2 is a clean extraction.** The refactoring successfully breaks the ~510-line `_create_and_run_agent()` into 6 focused sub-methods, each with clear responsibilities and comprehensive docstrings. Behavioral preservation is excellent — all logging, error handling, and WebSocket push locations are intact. The coordinator method reads like a well-structured pipeline.

### Required Changes (Optional Improvements)

| # | Severity | Change |
|---|----------|--------|
| 1 | 🟡 Minor | Move `conv.append(task_msg)` inside lock scope in `_initialize_instance_conversation` (L2950) |
| 2 | 🔵 Nit | Simplify `inst is None or not is_reuse` to `not is_reuse` in `_find_or_create_instance` (L2746) |

**No blocking issues.** The two noted items are cleanup-style improvements that do not affect correctness.