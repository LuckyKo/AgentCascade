# Plan: Turn off last-turn tool-disabling when skill-reflection extended turns will fire (todo.md:149)

**Mode:** Investigative + Due-diligence (root cause + implementation plan, NO code changes yet)
**Confidence:** Confirmed (mechanism traced end-to-end through source; all claims cite file:line)
**Date:** 2026-09-19 · branch master @ `29bb5e77` (v0.1.114)

---

## 1. Executive Summary

The auto-skill reflection trigger was **restructured from a budget-exhaustion point to the
natural-completion point (Phase 5)** in commit `3ec6d3d`. That restructure moved the trigger
to *after* the LLM call, but the "last turn" tool-disable block still runs *before* the LLM
call in the same iteration. As a result, when an agent ends **naturally on its exact last turn**
(`turns_available == 1`) with the extension gates passing:

1. The final-turn block disables ALL tools → the request's function schema changes → **KV full reprocess #1**.
2. The LLM returns a final answer (no tool call — none available).
3. Phase 5 fires the auto-skill extension → budget reset to `AUTO_SKILL_EXTRA_TURNS`.
4. First reflection turn: tools are re-enabled → function schema changes again → **KV full reprocess #2**.

Two consecutive prefix-divergence events = the "double full reprocess on the next 2 turns."

**Fix:** In the `turns_available == 1` block, check the cheap auto-skill trigger conditions
*before* disabling tools. If they pass, skip tool-disable (and the final-turn warning). These
conditions are fully knowable before the LLM call, so the decision can be made in time.

---

## 2. Root-Cause Explanation

### 2.1 The two mechanisms

**A. Tool-disabling on the last turn** — `agent_cascade/engine/core.py:849-878` (inside
`ExecutionEngine.run()`, the `while turns_available > 0:` loop):

```python
849:  if turns_available == 1:
850:      # Final-turn handling for a run that is genuinely ending on its last
...
862:      if max_turns != 1:
863:          final_msg = self._make_user_message(
864:              f"[SYSTEM WARNING: Final turn. You have 1 turn left to complete your task. "
865:              f"Wrap up and deliver your results now.]")
866:          self._append_and_log_to_llm(instance, final_msg, llm_messages)
867:
868:      # Disable ALL tools on the last turn so agent is forced to
869:      # return a final answer
870:      template = self.pool.get_template(instance.agent_class)
871:      if template and hasattr(template, 'function_map'):
872:          all_tools = list(template.function_map.keys())
873:          if all_tools:
874:              if not hasattr(instance, '_generate_cfg_override') or instance._generate_cfg_override is None:
875:                  instance._generate_cfg_override = {}
876:              instance._generate_cfg_override['disabled_tools'] = all_tools   # <-- tool disable
877:              final_turn_tools_disabled = True
```

The override is cleaned up **after** the LLM call, `core.py:966-971`:

```python
966:  if final_turn_tools_disabled:
967:      if hasattr(instance, '_generate_cfg_override') and isinstance(instance._generate_cfg_override, dict):
968:          instance._generate_cfg_override.pop('disabled_tools', None)
```

**How the disable reaches the request (verified end-to-end):**
- `engine/llm_call.py:1256` — `active_functions = _get_active_functions_from_template(template, instance, pool=self.pool)`
- `engine/helpers.py:37-93` — `_get_active_functions_from_template` reads `instance._generate_cfg_override` (L65) and resolves via `resolve_disabled_tools_for_agent`; with all tools disabled it returns `[]`.
- `engine/llm_call.py:1431 / 1446 / 1482` — `functions=active_functions` is passed to the LLM call.

So disabling all tools changes the request's function schema from `[tool_a, tool_b, …]` to `[]`.
That changes the serialized prompt prefix → llama.cpp KV cache can no longer be reused → **full reprocess**.
This is corroborated by prior work: `todo.md:131` ("tools are disabled on that so any recall would break
reprocessing skip") and `.agent_lessons/auto-skill-inloop-trigger-design.md` ("tool deactivation = full LLM
reprocess, extension = another — prefix stays stable").

**B. Skill-reflection extended turns (the in-loop trigger)** — `core.py:246-355`
(`ExecutionEngine._try_auto_skill_extension`), called from **Phase 5** at `core.py:998-1021`:

```python
992:  # ── Phase 5: Post-Turn Checks ───────────────────────────────
993:  completed = self._post_turn_checks(instance, messages, llm_messages, response)
994:  if not completed:
998:      if self._is_genuine_completion(instance, response) and \
999:              self._try_auto_skill_extension(
1000:                 instance, messages, llm_messages,
1001:                 loaded_skill_names=getattr(instance, '_loaded_skill_names', None)):
1002:          # Trigger fired: grant AUTO_SKILL_EXTRA_TURNS fresh turns for the reflection.
1004:          instance._auto_skill_orig_max_turns = instance.max_turns   # R6 snapshot
1005:          instance.max_turns = instance._current_turn + AUTO_SKILL_EXTRA_TURNS
1006:          max_turns = instance._current_turn + AUTO_SKILL_EXTRA_TURNS
1013:          _suppress_budget_warnings = True
1019:          turns_available = AUTO_SKILL_EXTRA_TURNS
1020:          yield response
1021:          continue                                                   # run the reflection turns
1022:  break
```

### 2.2 The exact trigger conditions (task item 2)

`_try_auto_skill_extension` (`core.py:273-334`) gates on, in order:

| # | Condition | Code (core.py) | Maps to todo.md:149 wording |
|---|-----------|----------------|------------------------------|
| a | `skill_manager` present | L275-277 `skill_manager = getattr(self.pool,'skill_manager',None); if None: return False` | "skills enabled" |
| b | `auto_skill_enabled` | L279-280 `if not getattr(settings,'auto_skill_enabled',AUTO_SKILL_ENABLED): return False` | "auto skill gen is on" |
| c | load mode ≠ NONE | L281-282 `if getattr(settings,'default_load_skill_mode',DEFAULT_LOAD_SKILL_MODE)==LOAD_SKILL_NONE: return False` | "skills enabled" |
| d | above min turns | L285-287 `min_turns = getattr(settings,'auto_skill_min_turns',AUTO_SKILL_MIN_TURNS); if instance._current_turn <= min_turns: return False` | "above the min nr of turns for skill gen" |
| e | one-shot not used | L290-292 `with instance._compression_lock: if getattr(instance,'_auto_skill_proposed',False): return False` | (precondition) |
| f | qualification + prompt | L329-334 `prompt = skill_manager.auto_skill_qualifies(...); if not prompt: return False` | (deeper check) |

`auto_skill_qualifies` (`skills/manager.py:1408-1461`) re-checks (e) the one-shot flag, (d)
`turns_effectuated > min_turns`, and additionally that **skill-creator is loadable**
(`manager.py:1456-1458`). It is pure (no side effects).

Settings defaults (`agent_cascade/settings.py`):
- `AUTO_SKILL_ENABLED = False` (L526)
- `AUTO_SKILL_EXTRA_TURNS = 25` (L527-529)
- `AUTO_SKILL_MIN_TURNS = 20` (L531-532) — lowered from 50 in commit `af51e94d`
- `DEFAULT_LOAD_SKILL_MODE = 'AUTO'` (L497-498); `LOAD_SKILL_NONE = 'NONE'` (L496)

### 2.3 Why the restructure created the bug

The **old** budget-exhaustion design fired the trigger at `turns_available == 1` *before* the
tool-disable decision, and explicitly **skipped both the final-turn warning and the tool-disable**
on trigger (see `.agent_lessons/auto-skill-inloop-trigger-design.md`). The **current** natural-end
design fires at Phase 5 — *after* the LLM call — so by then the `turns_available == 1` block has
already disabled tools and inserted the warning. The "skip tool-disable on trigger" protection was
dropped in the restructure. That is exactly what todo.md:149 asks to restore.

---

## 3. Turn-by-turn trace of the double reprocess (task item 3)

Let `max_turns = N`. Agent works normally on turns `1..N-1` with tools enabled (KV prefix stable,
incremental). Natural completion happens **on turn N** (`turns_available == 1`) and all extension
gates pass.

| Turn | `turns_available` | Tools in request | Prefix vs previous | KV result |
|------|-------------------|------------------|--------------------|-----------|
| N-1 | 2 | full `[a,b,…]` | stable | incremental ✓ |
| **N** (last) | 1 → 0 after `_consume_turn` (L880) | **∅** (disabled L876) | tool schema removed near start | **FULL REPROCESS #1** |
| N+1 (reflection 1) | 25 | full `[a,b,…]` (re-enabled; pop already ran L966-968) | tool schema restored | **FULL REPROCESS #2** |
| N+2 (reflection 2) | 24 | full `[a,b,…]` | stable extension | incremental ✓ |

Key facts that make this deterministic:
- `_current_turn` is set from locals each iteration (`core.py:792` `instance._current_turn = max_turns - turns_available + 1`), so on turn N it equals `N`; gate (d) `N > min_turns` passes for any reasonable budget.
- The tool-disable override is popped **after** the triggering LLM call (`core.py:966-968`), so the *first reflection turn* runs with tools re-enabled — that's what causes reprocess #2.
- `_suppress_budget_warnings = True` (L1013) already handles the 50%/90% warnings during the reflection (todo.md:141, commits `35eae95`+`ff187f5`) — so budget warnings are NOT part of this bug; only the tool-schema toggle is.

**Net effect:** two back-to-back full prompt reprocesses spanning the triggering turn and the first
reflection turn — "double full reprocess on the next 2 turns." If tools had stayed enabled across
both turns, the prefix would have remained stable and both would have been incremental.

> Note (related but separate): todo.md:150 ("odd reprocessing … only on some MOE models") is a
> distinct symptom with its own debug dumps; this plan addresses todo.md:149 specifically. The two
> likely share the "prefix changed mid-run" root theme but should be verified independently.

---

## 4. Proposed fix (task item 4) — cleanest minimal location

**Can the extended-turn decision be known before the last-turn tool-disable?** **Yes.** The cheap
gates (a)-(e) depend only on `pool` settings, `instance._current_turn`, and the one-shot flag — none
of which require the LLM output. They are fully evaluable at the `turns_available == 1` point, i.e.
*before* the LLM call. Only gate (f) `auto_skill_qualifies` (skill-creator loadable + prompt build)
is a deeper check that runs later; it is not needed to make the tool-disable decision safely.

### Recommended change — shared cheap-gate helper (single source of truth)

**Step 1.** Add a small method to `ExecutionEngine` (near `_try_auto_skill_extension`, ~`core.py:246`):

```python
def _auto_skill_gates_met(self, instance) -> bool:
    """Cheap, pre-LLM auto-skill trigger gates (settings + one-shot flag only).

    Returns True iff the in-loop reflection extension COULD fire this run. Used both to
    decide whether to skip last-turn tool-disable (prefix stability) and as the fast-path
    gate at the top of _try_auto_skill_extension, so the two can never drift.
    Deliberately excludes auto_skill_qualifies (skill-creator/prompt build) — that is a
    deeper qualification checked later and must not be re-run on every final turn.
    """
    skill_manager = getattr(self.pool, 'skill_manager', None)
    if skill_manager is None:
        return False
    settings = getattr(self.pool, 'settings', None)
    if not getattr(settings, 'auto_skill_enabled', AUTO_SKILL_ENABLED):
        return False
    if getattr(settings, 'default_load_skill_mode', DEFAULT_LOAD_SKILL_MODE) == LOAD_SKILL_NONE:
        return False
    min_turns = getattr(settings, 'auto_skill_min_turns', AUTO_SKILL_MIN_TURNS)
    if instance._current_turn <= min_turns:
        return False
    with instance._compression_lock:
        if getattr(instance, '_auto_skill_proposed', False):
            return False
    return True
```

**Step 2.** Guard the final-turn block (`core.py:849-878`). Compute the gate once and skip both the
warning and the tool-disable when it passes:

```python
if turns_available == 1:
    # If the auto-skill reflection extension is going to fire, keep tools enabled:
    # disabling them changes the request function schema -> KV full reprocess, and the
    # extension would force a second one on the first reflection turn. Keeping tools on
    # keeps the prefix stable so this "last turn" behaves like a normal mid-run turn.
    _auto_skill_will_extend = self._auto_skill_gates_met(instance)
    if max_turns != 1 and not _auto_skill_will_extend:
        final_msg = self._make_user_message(
            f"[SYSTEM WARNING: Final turn. You have 1 turn left to complete your task. "
            f"Wrap up and deliver your results now.]")
        self._append_and_log_to_llm(instance, final_msg, llm_messages)

    if not _auto_skill_will_extend:
        template = self.pool.get_template(instance.agent_class)
        if template and hasattr(template, 'function_map'):
            all_tools = list(template.function_map.keys())
            if all_tools:
                if not hasattr(instance, '_generate_cfg_override') or instance._generate_cfg_override is None:
                    instance._generate_cfg_override = {}
                instance._generate_cfg_override['disabled_tools'] = all_tools
                final_turn_tools_disabled = True
```

**Step 3 (optional but recommended).** Refactor the top of `_try_auto_skill_extension`
(`core.py:274-292`) to call `self._auto_skill_gates_met(instance)` instead of the inline
gate checks, keeping the method's body otherwise identical. This guarantees the final-turn
decision and the actual trigger use the *same* conditions (no drift). The `auto_skill_qualifies`
call (L329-334) stays as-is.

### Why this is correct / safe
- If any cheap gate fails, `_try_auto_skill_extension` would also return False → extension does NOT
  fire → tools are correctly disabled (unchanged behavior for non-extending runs).
- If all cheap gates pass, the extension *may* fire. Skipping tool-disable keeps the prefix stable;
  if it fires, the reflection runs with tools enabled (no reprocess).
- **Edge case:** all cheap gates pass but `auto_skill_qualifies` returns None (only realistic cause:
  skill-creator not loadable — rare, it's a core skill). Then tools were left enabled on the last turn
  and no extension fires; if the agent calls a tool, `_process_response` continues but `turns_available`
  is already 0 so the loop exits. Bounded and unlikely. Acceptable; can be tightened later by adding
  the skill-creator check to the helper if it ever becomes a problem.
- **Reflection's own final turn:** when the reflection reaches *its* last turn (`turns_available == 1`
  again), `_auto_skill_gates_met` returns False (one-shot flag now set) → tools are disabled there,
  forcing a clean final answer. That is a single reprocess at the very end of the run (no subsequent
  turn to reprocess against) and is correct/desired behavior. The bug's *double* reprocess spanning
  the trigger boundary is what this fix removes.

### Minimal alternative (if you want zero refactor)
Skip Step 3 and just inline the 5-condition check in the `turns_available == 1` block (Step 2 only).
Slightly duplicates the gate logic but carries no risk to the existing trigger path. The shared helper
(Steps 1-3) is preferred for maintainability.

---

## 5. Tests to add / extend (task item 5)

**Primary suite:** `tests/test_skill_generation.py` → `class TestInLoopTrigger` (L952). It drives the
REAL `ExecutionEngine.run()` with a stubbed LLM. Its `_make_pool(...)` already exposes the exact knobs:
`max_turns, min_turns, extra_turns, auto_skill_enabled, load_mode, with_creator, natural_end_at, exhaust_at`.
The template has `function_map = {'tool_a': None, 'tool_b': None}` (L1027) and `_make_inst` sets
`inst._generate_cfg_override = None` (L982) — so tool-disable is observable on the instance.

**New regression tests to add to `TestInLoopTrigger`:**

1. **`test_last_turn_natural_end_extension_keeps_tools_enabled`** — the direct regression for todo.md:149.
   Drive with `natural_end_at = max_turns` (agent completes on its exact last turn), all gates passing
   (`auto_skill_enabled=True`, `load_mode='AUTO'`, `min_turns < max_turns`, `with_creator=True`).
   Assert: after the run, `instance._generate_cfg_override` is `None` OR does **not** contain a
   `'disabled_tools'` key covering all tools on the triggering turn. (Capture the override at the
   triggering iteration via a tracer/wrapper around `_call_llm_with_injection` or by asserting no
   full-disable was ever set.) This test must FAIL on current code (tools get disabled) and PASS after fix.

2. **`test_last_turn_no_extension_disables_tools`** — control case: same setup but `auto_skill_enabled=False`
   (or `load_mode='NONE'`, or `min_turns >= max_turns`). Assert tools ARE disabled on the last turn
   (`_generate_cfg_override['disabled_tools'] == ['tool_a','tool_b']`) and the final-turn warning is injected.
   Guards against over-suppression.

3. **`test_last_turn_extension_suppresses_final_warning`** — with gates passing + natural end on last turn,
   assert the "[SYSTEM WARNING: Final turn...]" message is NOT appended to `instance.conversation` /
   `llm_messages` (it would be misleading when 25 more turns are about to be granted).

4. **`test_reflection_final_turn_still_disables_tools`** — verify that on the *reflection's* own last turn
   the one-shot flag makes `_auto_skill_gates_met` False, so tools are disabled there (clean final answer).
   Confirms we only skip the disable at the trigger boundary, not throughout the reflection.

5. **`test_tool_disable_prefix_stability_across_trigger`** (optional, higher-value): assert that across
   the triggering turn → first reflection turn, the set of active functions (`_get_active_functions_from_template`)
   is unchanged (tools stay enabled), i.e. no function-schema toggle. This directly encodes "no double reprocess."

**Existing tests to keep green (regression guard):**
- `TestInLoopTrigger` existing 12+ tests (budget-exhaustion + natural-end layouts) — the fix must not change
  the trigger/budget-reset behavior, only the tool-disable/warning decision.
- `tests/test_recall_auto_skill_reset.py` (cf90f88) — one-shot flag / `_auto_skill_task_output` reset on recall.
- `tests/test_loaded_skill_names.py` — reflection prompt skill list.
- `tests/test_injected_message_dedup.py` — injected-message dedup (final-turn warning is one of the 6 sites).

**Harness note:** to assert tool-disable at the triggering iteration, wrap `engine._call_llm_with_injection`
(or `_get_active_functions_from_template`) with a tracer that records `instance._generate_cfg_override.get('disabled_tools')`
per call — mirroring the instrumentation approach in `.agent_lessons/engine-loop-test-offbyone-diagnosis`.

---

## 6. Exact code locations (consolidated, task item 1 & 2)

| Concern | File:Line |
|---------|-----------|
| Last-turn tool-disable block | `agent_cascade/engine/core.py:849-878` (disable at L876) |
| Tool-disable cleanup (pop) | `agent_cascade/engine/core.py:966-968` |
| `_consume_turn` (decrement) | `agent_cascade/engine/core.py:880` (method L162) |
| `_current_turn` recompute | `agent_cascade/engine/core.py:792` |
| Auto-skill trigger method | `agent_cascade/engine/core.py:246-355` (`_try_auto_skill_extension`) |
| Trigger call site (Phase 5) | `agent_cascade/engine/core.py:998-1021` |
| Budget reset on trigger | `agent_cascade/engine/core.py:1004-1019` |
| `_suppress_budget_warnings` flag | `agent_cascade/engine/core.py:788` (init), `1013` (set) |
| Trigger gates (cheap) | `agent_cascade/engine/core.py:275-292` |
| `auto_skill_qualifies` (deeper) | `agent_cascade/skills/manager.py:1408-1461` (skill-creator check L1456-1458) |
| `_is_genuine_completion` gate | `agent_cascade/engine/core.py:2301-2325` |
| `disabled_tools` → request path | `engine/llm_call.py:1256` → `engine/helpers.py:37-93` → `llm_call.py:1431/1446/1482` |
| Settings constants | `agent_cascade/settings.py:496,497-498,526,527-529,531-532` |

---

## 7. Open questions / remaining unknowns

1. **skill-creator-not-loadable edge case** (Section 4): accept the bounded behavior or add the
   skill-creator check to `_auto_skill_gates_met`? Recommend: accept for now, revisit if observed.
2. **Reflection's own final-turn reprocess:** one residual full reprocess at the very end of the
   reflection (tools disabled to force a final answer). Not part of the "double" bug; confirm with
   the user whether they also want that avoided (would require NOT disabling tools on the reflection's
   last turn — tradeoff: agent might not deliver a clean final answer).
3. **todo.md:150 (MOE odd reprocessing):** separate symptom, needs its own investigation against the
   two debug dumps; likely related theme (mid-run prefix change) but unverified.
4. Whether `_current_turn` is guaranteed `> 0` and correctly set at the final-turn block for all entry
   paths (it is set at L792 each iteration, so yes for the normal loop).

## 8. Suggested next actions
1. Implement Steps 1-3 of Section 4 (shared helper + guarded final-turn block + refactor trigger top).
2. Add the 5 tests in Section 5; confirm test #1 fails pre-fix and passes post-fix.
3. Run `TestInLoopTrigger` + the 3 related suites (recall reset, loaded skill names, injected dedup) serially.
4. Independent review of the diff (drift risk between gate helper and trigger is the main thing to check).
