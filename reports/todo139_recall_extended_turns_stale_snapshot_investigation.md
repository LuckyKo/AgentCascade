# Todo 139 — Recalled agent's final message lost after prior extended turns (skill gen)

**Mode:** Investigative (root cause)
**Baseline:** master @ `3b2f6581` (v0.1.103); todo-138 dedup fix = `4548b684`
**Status:** Research only — no code modified
**Confidence:** **Confirmed** (primary hypothesis traced end-to-end with file:line evidence)

---

## Executive Summary

The "final message" a recalled agent sends back to its caller is decided by
`extract_instance_output()` (`compression/helpers.py:580`). When called with the live
`instance=` argument (as the main child path does at `child_runner.py:135`), it returns
`instance._auto_skill_task_output` **if that attribute is a non-empty string**, and only
falls back to `messages[-1]` otherwise (`helpers.py:612-615`).

`_auto_skill_task_output` is a **pre-reflection snapshot** of the agent's real answer,
captured by `_try_auto_skill_extension()` at the moment the in-loop auto-skill trigger fires
(`core.py:323`). It is paired with a **one-shot** flag `_auto_skill_proposed` (`core.py:344`,
"never reset" per `agent_instance.py:257-260`).

**The bug:** neither attribute is ever cleared. On instance **recall/reuse** the SAME
`AgentInstance` object is reused (`lifecycle_manager.py:129-136`) and its conversation is
preserved verbatim, but `initialize_conversation()`'s stale-state reset block
(`lifecycle_manager.py:350-361`) resets six per-run fields — **not** the two auto-skill
fields. So an agent that previously went through extended turns carries a **stale**
`_auto_skill_task_output` (the OLD task's answer) and a **stale** `_auto_skill_proposed=True`.

When the recalled agent completes its NEW task, the trigger does **not** re-fire (one-shot
flag still True → `core.py:288` returns False), so the snapshot is never refreshed. The caller
then extracts via `extract_instance_output(instance=inst)` and gets the **stale OLD answer**
instead of the NEW task's answer. That is exactly "final message does not get transmitted back
properly if a recalled agent previously had a round of extended turns."

---

## Key Findings

### 1. Final-message transmission path (the "result" extraction)

- `child_runner.py:102` — `inst, conv = engine._create_and_run_agent(...)` runs the full loop;
  returns `(inst, list(inst.conversation))` (`core.py:3574`). So `conv` is a copy of the FULL
  conversation, which after extended turns + a new task looks like:
  `[system, user(T1), …, assistant(A1), user(reflection), assistant(S1), user(T2), …, assistant(A2)]`.
- `child_runner.py:135` — `result = extract_instance_output(conv, instance_name, was_terminated=…, pool=pool, instance=inst)`.
  **Passes the live `instance=`** → auto-skill snapshot support is active.
- `helpers.py:612-615` — if `instance._auto_skill_task_output` is a `str`, return it immediately
  (before ever looking at `messages[-1]`). Otherwise fall through to `messages[-1]` (`:636`).

Other call sites do NOT pass `instance=` (so they always use `messages[-1]`):
- `advisor_runner.py:222`, `child_runner.py:121` (rejection path). Not the main result path.

### 2. What "recalled agent" means mechanically

- `_is_recall = is_reuse and not session_was_loaded and conversation[0].role == SYSTEM`
  (`core.py:3182-3183`). Recall = reusing an **active-but-IDLE** instance, NOT a log_file restore.
- `find_or_create_instance()` (`lifecycle_manager.py:97-220`): if the existing instance's state is
  `IDLE`/`TERMINATED`, it sets `inst = existing; is_reuse = True` (`:135-136`) — **the same object**
  is reused. Only `_nest_depth`, `last_activity`, and parent/child tracking are updated here.
- Recall keeps `conversation[0]` byte-for-byte verbatim, no skill refresh (`core.py:3196-3209`).
- A NEW task message IS appended on recall: `initialize_conversation()` reuse branch does
  `instance.append_message(task_msg)` (`lifecycle_manager.py:424`) onto the preserved conversation.
  So a real new run happens and produces a new answer `A2`.

### 3. What extended turns (skill gen) change about agent state

- `_try_auto_skill_extension()` (`core.py:246-351`) fires at **natural completion** (Phase 5,
  gated by `_is_genuine_completion`, `core.py:994-997`). It:
  1. snapshots the last assistant text into `instance._auto_skill_task_output` (`core.py:298-323`);
  2. injects the reflection prompt into conversation + JSONL + LLM view (`core.py:338-341`);
  3. sets one-shot `instance._auto_skill_proposed = True` (`core.py:343-344`).
- The caller then grants extra turns and continues the loop (`core.py:998-1017`), so the
  reflection's skill proposal `S1` is appended AFTER the real answer `A1`.
- **run() exit finally** (`core.py:1097-1106`) restores ONLY `_auto_skill_orig_max_turns →
  max_turns`. It does **not** touch `_auto_skill_task_output` or `_auto_skill_proposed`.

### 4. The corrupted/misread state on recall (the bug)

The stale-state reset on reuse (`lifecycle_manager.py:350-361`) clears:
`compression_summary`, `latest_marker_index`, `_generate_cfg_override`, `max_turns`,
`_current_turn`, `is_terminated`. **It does NOT clear `_auto_skill_task_output` or
`_auto_skill_proposed`.** (These fields were added 2026-09-17 with the in-loop trigger, after the
instance-reuse reset list was written — see `.agent_lessons/lessons_instance_reuse_fix.md`.)

Consequence on recall of an agent that already did skill gen:
- `_auto_skill_task_output` still = `A1` (stale), `_auto_skill_proposed` still = `True`.
- New task T2 runs to completion → `_try_auto_skill_extension` is reached but returns False at
  `core.py:288` (`_auto_skill_proposed` already True) → snapshot NOT refreshed.
- Caller extracts → `helpers.py:613-615` returns stale `A1`. **The NEW answer `A2` is never sent.**

This only happens when extended turns were involved because that is the ONLY thing that sets
`_auto_skill_task_output` to a non-None string. A recalled agent that never triggered skill gen
has `_auto_skill_task_output = None` → falls back to `messages[-1]` = correct new answer.

### 5. Interaction with todo 138 (injected-message dedup)

**Independent — not the cause.** Todo 138's fix (`4548b684`, `_append_and_log_to_llm` at
`core.py:232-244`) corrected double-appending of injected warnings/reflection prompts in the LLM
view (`_cached_llm_messages`). It does not touch `_auto_skill_task_output`, and the snapshot is
captured from `instance.conversation` (`core.py:298`), not the LLM view. Todo 138 made the LLM
request correct; it did nothing to address (and did not cause) the stale-snapshot problem.

### 6. JSONL log structure after extended turns

The on-disk JSONL is clean (single copy of each message, per todo-138 evidence). After a skill-gen
run it contains: `[system, user(T1), …, assistant(A1), user(reflection prompt), assistant(S1)]`.
There is **no marker** distinguishing "real final response" (`A1`) from the reflection output
(`S1`) — the disambiguation lives entirely in the in-memory `_auto_skill_task_output` snapshot,
which is exactly what gets lost/stale across recall.

---

## Supporting Evidence (file:line)

| # | Claim | Location |
|---|-------|----------|
| E1 | Extraction returns snapshot if set, else `messages[-1]` | `compression/helpers.py:612-615`, `:636` |
| E2 | Main child path passes live `instance=` | `child_runner.py:135` |
| E3 | Snapshot captured at trigger from conversation | `engine/core.py:298-323` |
| E4 | One-shot flag set at trigger | `engine/core.py:343-344` |
| E5 | Trigger gated by one-shot flag (returns False if already True) | `engine/core.py:288-289` |
| E6 | Trigger fires only at genuine natural completion (Phase 5) | `engine/core.py:994-1017` |
| E7 | run() finally restores ONLY max_turns | `engine/core.py:1097-1106` |
| E8 | Recall reuses SAME instance object (IDLE/TERMINATED) | `lifecycle_manager.py:129-136` |
| E9 | `_is_recall` definition | `engine/core.py:3182-3183` |
| E10 | Reuse resets 6 stale fields, NOT the auto-skill pair | `lifecycle_manager.py:350-361` |
| E11 | New task appended on recall (real new run) | `lifecycle_manager.py:424` |
| E12 | `_create_and_run_agent` returns full conversation copy | `engine/core.py:3574` |
| E13 | Field defs: `_auto_skill_proposed` "never reset", `_auto_skill_task_output` snapshot | `agent_instance.py:257-260` |
| E14 | No existing test covers stale-snapshot-on-recall | `tests/test_recall_skill_preservation.py` (recall skill only); grep of tests shows no such case |

---

## Confidence Level

**Confirmed.** The full causal chain is traced with direct code reads (no inference gaps):
snapshot set (E3) → never cleared on reuse (E10, E7) → trigger can't re-fire (E5, E4) → extraction
returns stale snapshot (E1, E2). Each link is a literal code path, not an assumption.

Residual uncertainty (low impact): I have not executed a live repro; the chain is verified by
reading. A regression test (below) would convert "confirmed by reading" to "confirmed by execution."

---

## Open Questions

1. **Design intent — per-lifetime vs per-task one-shot.** Should `_auto_skill_proposed` also be
   reset on recall so a *qualifying* new task can get its own reflection? Current comment says
   "one-shot, never reset" (per instance lifetime). Resetting it changes behavior (allows repeated
   skill proposals across recalls); NOT resetting it is the conservative choice. Needs maintainer
   decision.
2. **`_loaded_skill_names`** (`agent_instance.py:263`) is also per-run state set on the init path
   and left stale on recall (comment at `core.py:3332` notes "Recall path leaves it unset"). It does
   not affect result extraction, but is the same class of stale-state issue.

---

## Suggested Fix Direction (research recommendation — NOT applied)

**Primary (recommended, verified by independent reviewer `todo139_review`):** In
`initialize_conversation()`'s reuse branch (`lifecycle_manager.py:350-361`), add to the existing
stale-state reset block (already under `instance._compression_lock`):

```python
# Reset auto-skill state for a fresh task (per-run, not per-lifetime)
instance._auto_skill_task_output = None   # clear prior run's pre-reflection snapshot
instance._auto_skill_proposed = False     # allow this new task to qualify for reflection again
```

Rationale:
- `_auto_skill_task_output = None` directly fixes the reported bug — with the snapshot cleared,
  `extract_instance_output` falls back to `messages[-1]` = the NEW answer (or re-captures a fresh
  snapshot if the trigger fires on the new task).
- `_auto_skill_proposed = False` restores the intended **per-run** one-shot semantics. Leaving it
  True would permanently disable skill reflection for that instance across ALL future recalls — a
  behavioral regression beyond the reported symptom (a complex new task on a recalled agent would
  be stuck at the base turn budget with no reflection). The "one-shot, never reset" comment
  (`agent_instance.py:257`) was written before recall-reuse of already-triggered instances and means
  "one per run," not "once for the object's lifetime."

This mirrors the surrounding reset block's intent (clear per-run state for a new task) and is safe:
the only reader of `_auto_skill_task_output` is `helpers.py:613`; the only writer besides the trigger
is this reset. No consumer expects these to persist across runs.

**Optional hardening (not required):** also reset `instance._loaded_skill_names = None` in the same
block — it is per-run state set on the init path and left stale on recall (`core.py:3332` notes the
recall path leaves it unset). Same class of stale-state issue; does not affect result extraction.

**Regression test:** drive `_create_and_run_agent` twice on the same instance with `is_reuse=True`:
first run sets `inst._auto_skill_task_output = 'OLD'` + `_auto_skill_proposed=True`; second (recall)
run appends a new assistant answer; assert `extract_instance_output(conv, name, instance=inst)`
returns the NEW answer, not `'OLD'`. Asserts directly on the reset: after `initialize_conversation`
reuse, `inst._auto_skill_task_output is None`.

---

## Suggested Next Actions

1. Confirm design intent (Open Question 1) with maintainer: per-lifetime vs per-task one-shot.
2. Apply the primary fix (reset `_auto_skill_task_output = None` on reuse) + regression test.
3. Re-verify no other consumer reads `_auto_skill_task_output` expecting persistence across runs
   (grep shows only `helpers.py:613` reads it — safe).
