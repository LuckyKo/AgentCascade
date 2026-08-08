# Fix Plan: Max Auto-Rollbacks Enforcement (v2 — Reviewer-Revised)

**Author:** planner_rollback_fix  
**Date:** 2026-08-08  
**Version:** 2 (addresses reviewer findings on Change 4, merged Changes 1+3, dropped alias handler)  
**Status:** Draft — awaiting review before implementation  

## Overview

The setting `max_auto_rollbacks` ("Max Auto-Rollbacks (-1=∞)") is wired through UI → config → persistence but never enforced in the inline loop-detection path. This plan fixes five root causes:

1. Hardcoded threshold `3` in execution_engine.py — replace with `pool.settings.max_auto_rollbacks`.
2. Config handler clamps to [0, 10], discarding `-1` (∞) — fix clamp range; only `-1` is special, all other negatives clamp to 0.
3. `auto_rollback_on_loop` toggle ignored — respect it; skip inline rollback when False.
4. Turn counter bypass — loop-rollback cycles don't consume a turn; fix so max_turns acts as backstop.
5. Dead code paths — mark/clean legacy retry loop that catches LoopDetectedError which is never raised.

**Design principle:** Minimal, targeted changes. No refactoring of the inline rollback approach. Avoid changing return types across multiple functions when a simpler local fix exists.

---

## Exhaustive Return Statement Mapping in `_pre_llm_checks`

Before describing the turn-consumption fix, here is the complete mapping of all return statements in `_pre_llm_checks` (lines 2113–2210) for reference and test coverage:

| # | Line | Condition | Current Return | Semantic Meaning | Should Consume Turn? |
|---|------|-----------|----------------|------------------|---------------------|
| R1 | 2128 | `_check_stop_conditions(instance)` is True | `True` | Terminal stop or halt — skip LLM, will break next iteration | **No** — no real work done; termination path |
| R2 | 2132 | `_inject_async_messages(...)` returns True | `True` | New async messages/user input injected into conversation | **Yes** — new material arrived; agent must respond to it (real cycle) |
| R3 | 2139 | `handle_rollback_command(...)` returns True | `True` | User issued `/rollback` command; conversation state mutated | **Yes** — user-initiated state change consumed a turn's worth of work |
| R4 | 2147 | `handle_compress_command(...)` returns True | `True` | User issued `/compress` command; compression may have occurred | **Yes** — user-initiated state change consumed a turn's worth of work |
| R5 | 2152 | `_check_and_trigger_compression(...)` returns True | `True` | Forced compression triggered due to high context usage | **Yes** — compression is a real cycle that transforms conversation |
| R6 | 2198 | Loop detected → inline rollback performed | `True` | Loop detected, messages popped, hint injected | **Yes** — rollback + hint is a real recovery cycle |
| R7 | 2210 | Default fall-through (no pre-check triggered) | `False` | All checks passed; proceed to LLM call | N/A — returns False; turn consumed later at line 1343 |

Notes:
- R6 is the primary bug target: currently does NOT consume a turn.
- R2–R5 also do NOT consume turns currently (same class of bug), but fixing all of them increases blast radius. We fix **at minimum R6** and optionally R2–R5 using the same mechanism.
- R1 must NOT consume a turn (it's on the termination path).

---

## Change 1: Fix config handler clamp to accept -1 (∞)

**File:** `agent_cascade/config_handlers.py`  
**Location:** `_handle_max_auto_rollbacks`, lines ~459–464  

### Current behavior
```python
@register_config_handler('max_auto_rollbacks')
def _handle_max_auto_rollbacks(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update maximum automatic rollback attempts on detected loops."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('max_auto_rollbacks', 3))
        agent_pool.settings.max_auto_rollbacks = max(0, min(val, 10))
```

Clamp `[0, 10]` discards `-1`. UI label says "-1=∞" but it's silently converted to `0`.

### Required change

- Only `-1` is special (unlimited).
- All other negative values clamp to `0`.
- Positive values clamp to `[0, 10]`.

### Proposed replacement

```python
@register_config_handler('max_auto_rollbacks')
def _handle_max_auto_rollbacks(ui_cfg: dict, agent_pool: Optional[Any], agents: list) -> None:
    """Update maximum automatic rollback attempts on detected loops.

    -1 means unlimited (∞); all other negatives clamp to 0; positives clamp to [0, 10].
    """
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = int(ui_cfg.get('max_auto_rollbacks', 3))
        if val == -1:
            agent_pool.settings.max_auto_rollbacks = -1
        else:
            # Clamp all other values to [0, 10]
            agent_pool.settings.max_auto_rollbacks = max(0, min(val, 10))
```

### Rationale

- Preserves `-1` semantics exactly.
- Prevents accidental negative configs (e.g., -5) from being treated as unlimited.
- Keeps positive values bounded.

---

## Change 2: Fix turn counter bypass using a local mutable wrapper (safer than tuple return)

**File:** `agent_cascade/execution_engine.py`  
**Location:** Main execution loop ~1292–1343; `_pre_llm_checks` callsites  

### Problem recap

```python
if self._pre_llm_checks(instance, messages, llm_messages, response, turns_available):
    # ...
    continue  # ← bypasses turns_available -= 1 on line 1343
# ...
turns_available -= 1  # ← never reached when _pre_llm_checks returns True
```

### Why NOT the tuple-return approach (reviewer finding)

The original plan proposed changing `_pre_llm_checks` return type from `bool` to `Tuple[bool, bool]`. Reviewer correctly flagged:
- At least 7 return statements in `_pre_llm_checks` itself need updating.
- `_check_and_trigger_compression`, `_inject_async_messages`, `handle_rollback_command`, `handle_compress_command` are called from `_pre_llm_checks`; their semantics would need to propagate turn-consumption info upward, or we'd misclassify.
- Higher risk of inconsistency and missed updates.

### Chosen approach: Local mutable wrapper at the call site

Use a one-element list `[turns_available]` passed into `_pre_llm_checks`. When a "real cycle" occurs inside `_pre_llm_checks`, it decrements the wrapped value directly. This:
- Keeps all existing return types unchanged (still `bool`).
- Localizes the change to `_pre_llm_checks` and its caller.
- Doesn't require changes to helper functions' signatures.

### Step 1: Update caller at line ~1292

Current:
```python
if self._pre_llm_checks(instance, messages, llm_messages, response, turns_available):
    logger.debug(f"[PRE_LLM_CHECK] Condition met, continuing loop")
    yield response
    if self._check_stop_conditions(instance):
        break
    continue
```

Replace with:
```python
# Wrap turns_available in a mutable container so _pre_llm_checks can decrement it
# when a "real cycle" occurs (rollback, compression, async injection).
_turns = [turns_available]
if self._pre_llm_checks(instance, messages, llm_messages, response, _turns):
    logger.debug(f"[PRE_LLM_CHECK] Condition met, continuing loop")
    yield response
    if self._check_stop_conditions(instance):
        break
    turns_available = _turns[0]  # Sync back after possible decrement
    continue
```

### Step 2: Update `_pre_llm_checks` signature

Current (line 2113–2116):
```python
def _pre_llm_checks(
    self, instance: AgentInstance, messages: List[Message],
    llm_messages: List[Message], response: List[Message], turns_available: int
) -> bool:
```

Change to:
```python
def _pre_llm_checks(
    self, instance: AgentInstance, messages: List[Message],
    llm_messages: List[Message], response: List[Message],
    turns_wrapper: List[int],  # mutable wrapper around turns_available
) -> bool:
    """Phase 2: Stop/halt checks, async injection, compression check, loop detection.

    Returns True if processing should continue to next iteration (yield + continue).
    When a "real cycle" occurs (rollback, compression, async message injection),
    decrements turns_wrapper[0] so max_turns acts as a backstop.
    """
```

### Step 3: Add turn decrement at each "real cycle" return site in `_pre_llm_checks`

Per the mapping above, decrement `turns_wrapper[0]` before returning `True` for R2–R6:

**R2 — Async injection (line ~2131–2132):**
```python
# Before:
if self._inject_async_messages(instance, messages, llm_messages, response):
    return True  # Yield and continue loop to process new messages

# After:
if self._inject_async_messages(instance, messages, llm_messages, response):
    turns_wrapper[0] -= 1  # R2: async injection is a real cycle
    return True
```

**R3 — Rollback command (line ~2137–2139):**
```python
# Before:
if self.compression_handler.handle_rollback_command(instance, messages, llm_messages, response):
    logger.debug(f"[PRE_LLM] Rollback command handled for {inst_name}")
    return True

# After:
if self.compression_handler.handle_rollback_command(instance, messages, llm_messages, response):
    logger.debug(f"[PRE_LLM] Rollback command handled for {inst_name}")
    turns_wrapper[0] -= 1  # R3: user rollback command is a real cycle
    return True
```

**R4 — Compress command (line ~2145–2147):**
```python
# Before:
if self.compression_handler.handle_compress_command(instance, messages, llm_messages, response):
    logger.debug(f"[PRE_LLM] Compress command handled for {inst_name}")
    return True

# After:
if self.compression_handler.handle_compress_command(instance, messages, llm_messages, response):
    logger.debug(f"[PRE_LLM] Compress command handled for {inst_name}")
    turns_wrapper[0] -= 1  # R4: user compress command is a real cycle
    return True
```

**R5 — Compression trigger (line ~2150–2152):**
```python
# Before:
if self._check_and_trigger_compression(instance, messages, llm_messages, response):
    logger.debug(f"[PRE_LLM] Compression triggered for {inst_name}")
    return True

# After:
if self._check_and_trigger_compression(instance, messages, llm_messages, response):
    logger.debug(f"[PRE_LLM] Compression triggered for {inst_name}")
    turns_wrapper[0] -= 1  # R5: forced compression is a real cycle
    return True
```

**R6 — Loop detection rollback (line ~2198):**
```python
# Before (inside the loop_info block):
return True  # Continue loop with fresh state

# After:
turns_wrapper[0] -= 1  # R6: loop rollback is a real cycle
return True
```

**R1 — Stop/halt check (line ~2128):** NO CHANGE — do NOT decrement (termination path).

**R7 — Default fall-through (line ~2210):** NO CHANGE — returns False; turn consumed at line 1343.

### Step 4: Update the post-check turn decrement guard

Current line ~1343:
```python
turns_available -= 1
```

This is now only reached when `_pre_llm_checks` returned `False` (R7), so it's correct as-is. No change needed.

### Rationale for this approach

- **Zero signature changes** to helper functions (`_inject_async_messages`, `_check_and_trigger_compression`, etc.).
- **Single new parameter type** on `_pre_llm_checks` (int → List[int]).
- **Explicit, auditable**: each decrement is at the exact return site with a comment referencing the mapping table.
- Easy to test: verify turns_wrapper[0] after each path.

### Edge cases

| Case | Behavior |
|------|----------|
| Loop detected, rolled back (R6) | Consumes 1 turn; max_turns countdown proceeds. |
| Forced compression (R5) | Consumes 1 turn. |
| Async messages injected (R2) | Consumes 1 turn (agent must respond to new input). |
| User /rollback or /compress command (R3, R4) | Consumes 1 turn. |
| Stop/halt check triggers (R1) | Does NOT consume a turn; terminates immediately. |
| Normal path through (R7) | Turn consumed at line 1343 as before. |

---

## Change 3: Wire `max_auto_rollbacks` + respect `auto_rollback_on_loop` (merged)

**File:** `agent_cascade/execution_engine.py`  
**Location:** `_pre_llm_checks`, loop detection block, lines ~2163–2198  

### Current behavior (simplified)

```python
if not getattr(instance, '_suppress_loop_detection_next_turn', False):
    loop_info = _canonical_detect_loop(messages)
    if loop_info:
        reason, pop_count = loop_info
        # ... logging ...

        rollbacks = getattr(instance, '_loop_rollback_count', 0) + 1
        instance._loop_rollback_count = rollbacks

        self._inline_rollback_and_hint(...)

        if rollbacks >= 3:
            logger.warning(f"Loop recovery for {inst_name}: rolled back {rollbacks} times...")

        # Telemetry...

        return True  # Continue loop with fresh state
```

Issues:
- Ignores `auto_rollback_on_loop` toggle.
- Uses hardcoded threshold `3`, never reads `pool.settings.max_auto_rollbacks`.
- Never terminates when limit exceeded; only warns and continues.

### Proposed replacement (lines ~2163–2205)

```python
if not getattr(instance, '_suppress_loop_detection_next_turn', False):
    loop_info = _canonical_detect_loop(messages)
    if loop_info:
        reason, pop_count = loop_info
        logger.debug(
            f"[LOOP_DETECTED] {inst_name}: pattern={reason}, "
            f"pop_count={pop_count}, messages={len(messages)}"
        )

        # ── Respect auto_rollback_on_loop toggle ──────────────────────
        if not self.pool.settings.auto_rollback_on_loop:
            logger.info(
                f"[LOOP_DETECTED_NO_ROLLBACK] {inst_name}: loop detected "
                f"(pattern={reason}) but auto_rollback_on_loop=False. "
                f"Continuing to LLM call."
            )
            # Telemetry for observability
            if (tel := self._telemetry()) is not None:
                try:
                    tel.record_loop_detected(
                        inst_name, reason=reason, auto_rolled_back=False, pop_count=pop_count,
                    )
                except Exception:
                    pass
            # Return False → proceed to LLM call with current context.
            # No turn consumed here; normal turn decrement at line 1343 applies.
            return False

        # ── Inline rollback + hint (only when toggle is True) ─────────
        rollbacks = getattr(instance, '_loop_rollback_count', 0) + 1
        instance._loop_rollback_count = rollbacks

        self._inline_rollback_and_hint(
            instance, inst_name, pop_count, reason,
            messages, llm_messages, response,
        )

        # ── Enforce configured max_auto_rollbacks limit ───────────────
        max_rb = self.pool.settings.max_auto_rollbacks
        if max_rb == -1:
            effective_limit = sys.maxsize  # unlimited; max_turns still backstops
        else:
            effective_limit = max_rb

        if rollbacks > effective_limit:
            logger.warning(
                f"Loop recovery for {inst_name}: exceeded configured limit "
                f"(rolled back {rollbacks} times, max={max_rb}). Terminating."
            )
            # Append clear failure message for caller visibility
            fail_msg = Message(
                role=USER,
                content=(
                    f"[SYSTEM]: Loop recovery failed — the agent exceeded the maximum "
                    f"allowed loop recoveries ({max_rb if max_rb != -1 else 'unlimited'}). "
                    f"The detected pattern was: {reason}. Please adjust your prompt or task."
                ),
            )
            self._append_and_log(instance, fail_msg)
            # Terminate this instance (and its children). Use set_global_stopped=False
            # so other agents are unaffected.
            self.pool.terminate_instance(inst_name, set_global_stopped=False)
            # Turn consumed for this rollback cycle
            turns_wrapper[0] -= 1
            return True  # Caller will break on _check_stop_conditions next iteration
        elif rollbacks >= min(3, effective_limit):
            # Keep existing warning at ≥3rd rollback if we haven't hit the limit yet
            logger.warning(
                f"Loop recovery for {inst_name}: rolled back "
                f"{rollbacks} times without success. Continuing."
            )

        # Telemetry: record loop detection (non-blocking)
        if (tel := self._telemetry()) is not None:
            try:
                tel.record_loop_detected(
                    inst_name, reason=reason, auto_rolled_back=True, pop_count=pop_count,
                )
            except Exception:
                pass

        # Turn consumed for this rollback cycle (Change 2 integration)
        turns_wrapper[0] -= 1
        return True  # Continue loop with fresh state
```

### Termination propagation verification

When `pool.terminate_instance(inst_name, set_global_stopped=False)` is called:
1. Instance added to `pool.terminated_instances`.
2. Instance state set to TERMINATED.
3. `_check_stop_conditions(instance)` calls `_is_terminal_stop(inst_name)`, which checks `pool.is_instance_terminated(inst_name)` → returns True.
4. Caller at line ~1296: `if self._check_stop_conditions(instance): break` → loop breaks immediately on next iteration.

**Result:** Agent is kicked back to caller within one iteration of termination. The failure message is already in the conversation for visibility.

### Edge cases

| Case | Behavior |
|------|----------|
| `auto_rollback_on_loop=False`, loop detected | No rollback, no hint, telemetry with `auto_rolled_back=False`, returns False → LLM call proceeds normally. Does NOT terminate regardless of `max_auto_rollbacks`. |
| `auto_rollback_on_loop=True` (default), `max_auto_rollbacks=2`, 3rd loop detected | Rollback performed on 1st, 2nd detections; on 3rd: rollback done then terminated because `rollbacks (3) > effective_limit (2)`. Failure message appended. |
| `auto_rollback_on_loop=True`, `max_auto_rollbacks=-1` | No hard cap; rollbacks continue until max_turns backstops (via Change 2). |
| `auto_rollback_on_loop=True`, `max_auto_rollbacks=0` | First loop detection: rollback performed (`rollbacks=1`), then terminated immediately because `1 > 0`. This is correct semantics: "max 0 rollbacks" = "not allowed to rollback." We perform the rollback (already committed) then terminate. |
| `auto_rollback_on_loop=False` + `max_auto_rollbacks=0` | Neither matters — no rollbacks ever occur, so limit is never checked. Agent continues until max_turns or other mechanism stops it. |

**Note on `max_auto_rollbacks=0`:** This means "zero rollbacks allowed." On first loop detection, the rollback executes (we've already detected and committed), then we check `rollbacks (1) > effective_limit (0)` → terminate. So the agent gets exactly one recovery attempt before being kicked back.

---

## Change 4: Clean up dead code paths (minimal)

### 4a: Mark `run_agent_in_pool_with_recovery` as deprecated

**File:** `agent_cascade/api_integration.py`  
**Location:** lines ~414–483  

This function wraps `run_agent_in_pool` with a retry loop that catches `LoopDetectedError`, which is never raised. Orphaned by commit 44d8b58 (inline rollback).

### Proposed action

Add prominent deprecation docstring:

```python
def run_agent_in_pool_with_recovery(...):
    """DEPRECATED (2026-08): Inline loop detection in ExecutionEngine._pre_llm_checks
    handles rollback directly. LoopDetectedError is never raised; this retry wrapper
    is dead code. Kept only for backward compatibility."""
```

### 4b: `max_auto_retries` param in child_runner.py

**File:** `agent_cascade/child_runner.py`, line ~68  

Already documented as "kept for backward compatibility (no longer used)." No change needed.

### 4c: `LoopDetectedError` in loop_detection.py

**File:** `agent_cascade/loop_detection.py`  

Never raised anywhere. Add deprecation comment; do not remove (external code might import it).

---

## Change 5 (Optional): Update UI label only, no new config keys

**Current label:** "Max Auto-Rollbacks (-1=∞)"  
**Suggested label:** "Max Loop Recoveries (-1=∞)"  

### Approach

- Pure HTML/JS change in web_ui; no backend changes.
- Keep `max_auto_rollbacks` as the config key for backward compatibility.
- **No alias handler added** (dropped per reviewer feedback — unnecessary complexity).

---

## Test Plan

All new tests go in `tests/test_loop_detection.py` in a new class `TestMaxAutoRollbacksEnforcement`, unless noted.

### Core enforcement tests

1. **test_rollback_limit_enforced**  
   - Mock pool with `max_auto_rollbacks=2`, `auto_rollback_on_loop=True`.  
   - Simulate 3 consecutive loop detections via `_pre_llm_checks`.  
   - Assert instance terminated after 3rd (exceeds limit).  
   - Assert failure message appended to conversation.

2. **test_rollback_limit_unlimited**  
   - Mock pool with `max_auto_rollbacks=-1`, `auto_rollback_on_loop=True`.  
   - Simulate many loop detections.  
   - Assert instance NOT terminated by rollback limit.

3. **test_rollback_limit_zero**  
   - Mock pool with `max_auto_rollbacks=0`, `auto_rollback_on_loop=True`.  
   - First loop detection → rollback performed (`rollbacks=1`), then terminated immediately because `1 > 0`.  
   - Assert instance terminated after exactly one rollback attempt.

4. **test_rollback_limit_exact_boundary**  
   - Mock pool with `max_auto_rollbacks=3`.  
   - Verify rollbacks 1, 2, 3 succeed; rollback 4 triggers termination.

### auto_rollback_on_loop toggle tests

5. **test_auto_rollback_disabled_no_rollback**  
   - Mock pool with `auto_rollback_on_loop=False`.  
   - Simulate loop detection.  
   - Assert no rollback occurs, no hint message appended.  
   - Assert `_pre_llm_checks` returns False (proceed to LLM).

6. **test_auto_rollback_disabled_no_termination**  
   - Mock pool with `auto_rollback_on_loop=False`, `max_auto_rollbacks=0`.  
   - Simulate multiple loop detections.  
   - Assert instance is NOT terminated (limit never checked when rollback disabled).

7. **test_auto_rollback_disabled_interaction_with_zero_limit**  
   - Same as #6 but explicitly verifies both settings coexist without conflict: toggle takes precedence (no rollbacks → limit irrelevant).

### Turn consumption tests (Change 2)

8. **test_turn_consumed_on_loop_rollback**  
   - Mock pool with `max_auto_rollbacks=5`, `auto_rollback_on_loop=True`.  
   - Initial turns_wrapper = [10].  
   - Simulate loop detection → rollback.  
   - Assert `_pre_llm_checks` returns True and turns_wrapper[0] == 9.

9. **test_turn_consumed_on_async_injection**  
   - Mock `_inject_async_messages` to return True.  
   - Initial turns_wrapper = [10].  
   - Call `_pre_llm_checks`.  
   - Assert returns True and turns_wrapper[0] == 9 (R2).

10. **test_turn_consumed_on_force_compression**  
    - Mock `_check_and_trigger_compression` to return True.  
    - Initial turns_wrapper = [10].  
    - Assert returns True and turns_wrapper[0] == 9 (R5).

11. **test_turn_NOT_consumed_on_stop_condition**  
    - Mock `_check_stop_conditions` to return True.  
    - Initial turns_wrapper = [10].  
    - Assert returns True and turns_wrapper[0] == 10 (R1, no decrement).

12. **test_turn_consumed_on_normal_path**  
    - All pre-checks return False/None → fall through to R7.  
    - `_pre_llm_checks` returns False.  
    - Verify caller decrements turns_available at line 1343 (existing behavior).

13. **test_all_early_return_paths_correct_values**  
    - Parameterized test covering R1–R7: for each path, verify (return_value, turn_consumed) matches the mapping table.

### max_turns as backstop tests

14. **test_max_turns_backstops_infinite_loops**  
    - Mock pool with `max_auto_rollbacks=-1`, `auto_rollback_on_loop=True`.  
    - Set max_turns=5, simulate loop detection on every pre-check.  
    - Assert execution terminates when turns_available reaches 0 (not by rollback limit).

15. **test_turn_warnings_fire_at_correct_thresholds_after_fix**  
    - Verify 50% warning fires at `turns_available == max_turns/2`.  
    - Verify 90% warning fires at `turns_available == max_turns * 0.1` (remaining).  
    - Verify final-turn warning fires at `turns_available == 1`.  
    - Ensure these still work correctly when turns are consumed by pre-check cycles.

### Config handler tests

16. **test_config_handler_accepts_minus_one**  
    - Call `_handle_max_auto_rollbacks` with `max_auto_rollbacks=-1`.  
    - Assert setting is `-1`, not clamped to 0.

17. **test_config_handler_clamps_positive**  
    - Value 15 → clamped to 10.  
    - Value 3 → stays 3.

18. **test_config_handler_clamps_other_negatives_to_zero**  
    - Value -5 → clamped to 0 (only -1 is special).  
    - Value -2 → clamped to 0.

### Integration-style tests

19. **test_end_to_end_rollback_limit_via_engine_run**  
    - Create a mock agent that always produces loop-detectable output.  
    - Run via ExecutionEngine with `max_auto_rollbacks=2`.  
    - Assert execution terminates after 3rd loop detection with clear error message.

20. **test_termination_detected_by_check_stop_conditions**  
    - After calling `pool.terminate_instance`, verify `_check_stop_conditions` returns True immediately.  
    - Ensures propagation chain works as documented in Change 3.

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Mutable wrapper approach causes confusion or bugs | Low | Medium | Well-commented; single new pattern; tests cover all paths. |
| Turn consumption on async injection/compression changes behavior unexpectedly | Low | Medium | These are real cycles that already "wasted" a turn's worth of work; making it explicit is correct. Tests verify. |
| `max_auto_rollbacks=0` behavior confusing to users | Low | Low | Document clearly: "0 = terminate after first rollback attempt (one recovery before kicking back)." |
| Termination via `pool.terminate_instance` causes unexpected child cascade | Low | Medium | Use `set_global_stopped=False`; only this instance and its children affected, which is correct behavior. |
| Config clamp change breaks existing behavior for edge values | Very Low | Low | Only affects `-1` (now preserved) and other negatives (clamped to 0). Existing valid configs unchanged. |

### Rollback considerations

- All changes are localized; easy to revert individually.
- If turn-consumption logic causes issues, can revert Change 2 while keeping Changes 1 and 3 (core enforcement still works, just without max_turns backstop improvement).
- Config handler change is isolated; reverting restores old clamp behavior.

---

## Implementation Order (Recommended)

1. **Change 1** — Fix config clamp (isolated, low risk, unblocks correct -1 semantics).
2. **Change 2** — Turn consumption fix with mutable wrapper (moderate risk, but safer than tuple approach; tests critical here).
3. **Change 3** — Wire `max_auto_rollbacks` + respect `auto_rollback_on_loop` (core fix; depends on Change 2 for turn decrement integration).
4. **Change 4** — Mark dead code as deprecated (cosmetic, no risk).
5. **Change 5** — Update UI label if desired (HTML-only, independent).
6. Add all tests from Test Plan.

---

## Notes for Implementer

- `execution_engine.py` is large (270KB+); use targeted edits only.
- Always verify line numbers before editing; the file may have shifted slightly.
- Use the return statement mapping table to guide turn-decrement placement — each decrement must have a comment referencing its R# from the table.
- After implementation, run existing loop detection tests to ensure no regressions:  
  `pytest tests/test_loop_detection.py -v`
- Pay special attention to test #13 (all early return paths) — it's the key regression guard for Change 2.