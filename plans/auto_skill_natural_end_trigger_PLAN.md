# Plan: Move auto-skill trigger from budget-exhaustion to natural completion

## Problem
Auto-skill generation "never triggers" in real runs. Root cause: `_try_auto_skill_extension`
(`agent_cascade/engine/core.py`) is called **only** inside the `if turns_available == 1:` branch
(line ~736) — i.e., only when the agent's turn budget is about to run out. Real agents end
**naturally** (assistant message with no tool call, well before their `max_turns=250`), so that
branch is never reached in practice. The existing unit tests all pass only because they use tiny
budgets (`max_turns=3`) that *force* exhaustion — the one path that works in the mock but not live.

## Decision (user-confirmed)
Trigger on **natural completion**: an assistant turn with no tool call, no pending async work, and
non-empty content. In `_post_turn_checks` (core.py:2243) that is specifically the FINAL
`return False` at **line 2295** ("Agent has truly completed"). The other three `return False` paths
must NOT trigger:
- 2264 terminal stop
- 2270 terminal-stop during compression wait
- 2289 pure-thinking stall

One-shot per agent is guaranteed by `_auto_skill_proposed` (never reset) — safe even if an agent
"ends" multiple times across its lifecycle.

## Open decision (needs user answer before implementation)
**`AUTO_SKILL_MIN_TURNS` gate (default 50).** Under budget-exhaustion it was vestigial; under
natural-end it becomes **load-bearing**: `core.py:256` and `manager.py:1450` both require
`_current_turn > AUTO_SKILL_MIN_TURNS`. An agent that finishes a task in 8 turns will NOT reflect.
- Option A (keep): only reflect on "long enough" runs — avoids skill noise from trivial tasks.
- Option B (drop / lower): reflect on every natural completion.
→ **Default to Option A (keep the gate as-is)** unless user says otherwise. It is the conservative,
  behavior-preserving choice and matches the existing setting.

---

## Implementation

### Change 1 — Relocate the trigger in `run()` (core.py)
Today (lines 736-789): the trigger call + budget reset live inside `if turns_available == 1:`.
Move them to the natural-end point at **line 898** (`if not self._post_turn_checks(...): break`).

New structure at ~898:
```python
# ── Phase 5: Post-Turn Checks ───────────────────────────────
completed = self._post_turn_checks(instance, messages, llm_messages, response)
if not completed:
    # Natural end (assistant turn with no tool call / no pending async / non-empty).
    # Try the one-shot auto-skill reflection BEFORE breaking. Only the genuine
    # natural-end path reaches here — _post_turn_checks already excludes terminal
    # stop and pure-thinking-stall exits.
    if self._try_auto_skill_extension(
            instance, messages, llm_messages,
            loaded_skill_names=getattr(instance, '_loaded_skill_names', None)):
        # Trigger fired: grant AUTO_SKILL_EXTRA_TURNS fresh turns for the reflection.
        # Loop-locals live in run()'s frame (NOT the instance), so the reset MUST stay here.
        instance._auto_skill_orig_max_turns = instance.max_turns   # R6 snapshot
        instance.max_turns = instance._current_turn + AUTO_SKILL_EXTRA_TURNS
        max_turns = instance._current_turn + AUTO_SKILL_EXTRA_TURNS
        turns_available = AUTO_SKILL_EXTRA_TURNS + 1               # +1 consumed by _consume_turn
        yield response
        continue                                                   # run the reflection turns
    break
```

Key constraints (verified):
- The budget reset **cannot** move into `_try_auto_skill_extension` — `max_turns`/`turns_available`
  are locals in `run()`'s frame, not instance attributes. Keep the reset in the caller (just relocated).
- Remove the trigger call + reset from the old `if turns_available == 1:` block (736-789). That block
  keeps its **final-turn warning + tool-disable** behavior for non-triggered runs unchanged.

### Change 2 — `_try_auto_skill_extension` body (core.py:232) — minimal change
The method's body (gates @247-257, snapshot @270-296, qualification @299-303, injection @311-318,
flag @317-318) **stays the same**. Only:
- Update the docstring (233-245): it now fires at natural completion, not budget exhaustion.
- The snapshot logic (find last ASSISTANT-with-text) is still correct — at natural end the final
  answer is already committed (per-iteration order: LLM @801 → `_process_response` commits @887 →
  `_post_turn_checks` @898), so it captures the just-completed final answer. (Subtlety vs old design:
  it now captures the natural-end turn's reply, not the previous turn's.)

### Change 3 — `extract_instance_output` behavior (compression/helpers.py:580-615)
No code change needed. When `instance=` is passed and `_auto_skill_task_output` is set, it returns the
pre-reflection snapshot. The reflection tail (last assistant message of the extended turns) is returned
when `instance=` is absent. Behavior is unchanged; only the *content* of the tail differs (see tests).

### Change 4 — Remove now-dead code / stale comments
- Any comment in the old `turns_available == 1` block referencing the auto-skill trigger (736-743)
  must be updated/removed.
- Verify no other call site of `_try_auto_skill_extension` remains (grep confirms only core.py:744).

---

## Test plan (tests/test_skill_generation.py — `TestInLoopTrigger`, line ~942)

The harness `_make_pool` (986-1086) currently forces budget exhaustion via
`engine._post_turn_checks = MagicMock(return_value=True)` (line 1071). To drive a **natural end**,
give `_post_turn_checks` a counter-based `side_effect`: return `True` for the first N-1 calls, `False`
on call N. This decouples the natural-end decision from LLM output and is simpler than making
`fake_llm` emit tool calls.

Tests to **rewrite** (all currently assume budget-exhaustion triggering):
| Test | Line | Change |
|------|------|--------|
| `test_budget_reset_off_by_one` | 1096 | Drive natural end at turn N; assert `call_count == N + EXTRA`. |
| `test_snapshot_captures_last_assistant_text` | 1108 | Snapshot now = the natural-end reply (not previous). Update expected value. |
| `test_snapshot_fallback_when_no_assistant_text` | 1123 | Re-examine: at natural end there IS assistant text; fallback path may need a different setup or removal. |
| `test_no_rollback_and_conversation_grows` | 1142 | Recompute the exact message count for natural-end layout (no "turn-limit-approaching" on original budget if it ends early). |
| `test_one_shot_flag` | 1166 | Drive natural end; assert flag set + second qualification None. |
| `test_tools_enabled_on_triggering_turn` | 1180 | At natural end the final-turn block is skipped (we `continue`, not break) — assert no disabled_tools, no original final-turn warning. |
| `test_output_return_path` | 1200 | **Biggest risk:** `without_snap.startswith('reply N') and 'Turn limit reached' in ...` will NOT hold if the reflection tail ends naturally (no turn-limit notice). Update to assert the natural tail without the notice, or force the extended tail to exhaust its extra budget. |
| gate tests (3) | 1241/1245/1249 | Drive natural end; assert no trigger + normal break (no extension). |
| `test_final_extended_turn_behaves_like_normal_last_turn` | 1257 | The extended tail's last turn: if it ends naturally there is NO final-turn warning. Decide: force the extra budget to exhaust (so a real final-turn warning appears) OR assert natural-end behavior. |
| `test_max_turns_restored_after_run` | 1275 | Should still pass (R6 restore in `finally`). Verify. |

**New tests to add:**
- `test_natural_end_triggers_extension`: agent ends at turn N (< max_turns) with no tool call →
  extension fires, `call_count == N + EXTRA`.
- `test_stop_exit_does_not_trigger`: terminal stop → no trigger (guards the 2264/2270 exclusion).
- `test_pure_thinking_stall_does_not_trigger`: pure-thinking turn → no trigger (guards 2289).
- `test_min_turns_gate_blocks_short_run`: agent ends in N ≤ AUTO_SKILL_MIN_TURNS → no trigger.

**Regression:** run the full `tests/test_skill_generation.py` serially (`-o addopts=""`) after changes.

---

## Verification & process
1. Coder implements Changes 1-4 + test rewrites, runs targeted tests.
2. Independent reviewer reviews for correctness (does it fire on natural end only? is the budget reset
   in the right frame? are stop/stall excluded?) — iterate to explicit PASS.
3. Refinement pass for code quality / bloat.
4. Commit each stage.

## Risks / notes
- **Natural end on the last turn** (turns_available==1): the `if turns_available == 1` final-turn block
  (736-775) fires *before* the natural-end trigger (898), so that turn gets a final-turn warning +
  disabled tools, then the trigger extends. Document as intended nuance; ensure tests don't assert
  otherwise.
- The `test_output_return_path` and `test_final_extended_turn...` assertions are the highest-breakage
  risk — they encode the old "extended tail ends by budget" assumption.
