---
name: engine-loop-test-offbyone-diagnosis
description: Diagnose off-by-one and "got X expected Y" failures in tests that drive a real execution loop (agent engine run, turn budget, generator) by instrumenting the actual code path to capture state at the exact trigger/decision point, then attributing the bug to test fixture vs production. Covers snapshot-captured-wrong-value and conversation-length assertion mismatches after production changes like switching warnings to full message insertions.
source: auto-generated
version: "1.0.0"
triggers:
  - "auto-skill in-loop trigger test"
  - "test fixture bug off-by-one"
  - "snapshot captured wrong value"
  - "got X expected Y assertion failure"
  - "engine.run loop test debugging"
  - "turn budget conversation length assertion"
generated_by: coder
generated_from_task: "Finish auto-skill in-loop trigger; TestInLoopTrigger tests failed with snapshot 'reply 3' vs expected 'reply 2' and conversation-length 15 vs 12 after warnings became full user-message insertions."
---

## Goal
When a test that drives a REAL production loop (e.g. `ExecutionEngine.run()`, a turn-budget while-loop, any generator) fails with an off-by-one or "got X / expected Y" assertion — especially a snapshot value or conversation-length mismatch after a production change like switching warnings to full message insertions — determine in minutes whether the bug is in the TEST FIXTURE or in PRODUCTION.

## Why this is its own skill
`systematic-debugging`'s Phase-2 (data-flow trace) is the general method; this captures the SPECIFIC, high-leverage technique for loop-driven tests: **instrument the real code path to capture state at the exact decision point, then diff a corrected stub against the broken one.** That single comparison proves which side is wrong. Handoff diagnoses are frequently stale — verify before trusting.

## Procedure

### Step 1 — Do NOT trust the handoff's diagnosis; reproduce first
Run the failing test(s) via host shell (`-n 0` on Windows). Note the EXACT symptom (e.g. `assert 'reply 3' == 'reply 2'`). Count how many tests actually fail — often far fewer than reported, and they share one root cause. If the handoff says "12 failures from a discovery bug" but only 3 fail with a value mismatch, the real bug is elsewhere.

### Step 2 — Replicate the test harness in a standalone script
Copy the test's `_make_pool`/setup verbatim into a throwaway `.py` (workspace, NOT repo root): same `MagicMock` pool, same stubbed engine methods (`_setup_turn`, `_pre_llm_checks`, `_post_turn_checks`, ...), same constant patches. This isolates you from conftest/fixture noise and lets you print freely.

### Step 3 — Instrument the REAL decision point
Wrap the method under test (e.g. `engine._try_auto_skill_extension`) with a tracer that, BEFORE delegating to the real impl, prints the state the assertion depends on:
```python
orig = engine._try_auto_skill_extension
def traced(instance, messages, llm_messages, **kw):
    print(f"[TRIGGER] turn={instance._current_turn} conv_len={len(instance.conversation)}")
    for m in instance.conversation:
        c = getattr(m, 'content', '')
        print("   ", getattr(m, 'role', '?'), "|", (c[:40] if isinstance(c, str) else '<list>'))
    return orig(instance, messages, llm_messages, **kw)
engine._try_auto_skill_extension = traced
```
Also instrument the stubbed LLM to log what it yields and with what inputs. Run, read the trace.

### Step 4 — Diff a corrected variant against the broken one (the decisive step)
Identify the ONE thing that differs between your working mental model and the trace. Change ONLY that in the stub and re-run. If the corrected variant produces the expected value and the original reproduces the failure, **production is correct and the fixture is wrong.** Classic case: a stub numbers output by `len(msgs)` while production inserts extra messages (warnings) into that same list — so labels drift from iteration numbers. Fix: number by a per-call counter.

### Step 5 — Recompute EVERY over-specific assertion, not just the failing one
Loop tests often have hand-counted assertions (conversation length, call counts, warning counts). With the corrected stub, recompute each and check it matches the trace. A test that "passes" may be passing by luck of a miscount that happens to cancel out — verify against the actual message layout, not the comment's arithmetic.

### Step 6 — Fix the fixture/assertions (NOT production), then run the whole class + full suite
Apply minimal edits with comments explaining the real layout so the next person doesn't re-derive it. Run the failing class first (`-n 0`), then the full required suite. Confirm green on host shell.

## Tips
- The "got X expected Y" value pair is a CLUE about WHERE in the loop the state diverged — trace backward from that value to the exact iteration/message, not forward through the whole run.
- If a stub's output depends on `len(some_list)` and production appends to that list (warnings, system messages, injected prompts), the numbering WILL drift. Prefer explicit counters or content that doesn't depend on mutable list lengths.
- Warnings/notifications that are "full message insertions" (appended to both the persistent conversation AND the loop-local working set) change BOTH conversation length and any `len()`-based stub logic. Count them in every length assertion.
- A standalone probe that imports the real engine beats a code_interpreter sandbox for this: it runs on host, has the project deps, and you control all prints. (See [[pytest-docker-subprocess-hang]] — in-container pytest hangs on Windows.)
- Delete all probe scripts before delivering; leave none in repo root or workspace root.
- Save a memory of the non-obvious layout/threshold arithmetic (e.g. `turns_50pct = max(3, int(N*0.5))`) so it isn't re-derived next time.
