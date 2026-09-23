---
name: agentcascade-engine-loop-phase-test-driver
description: Design correct test drivers for AgentCascade's engine run() loop phase-based control flow — tool-call turns hit Phase 4 continue and never reach Phase 5, so _post_turn_checks mocks must be shaped to match the real phase routing.
source: auto-generated
version: "1.0.0"
triggers:
  - "engine run loop test driver"
  - "_post_turn_checks mock"
  - "phase 4 continue tool call"
  - "auto-skill trigger test"
  - "turn budget test AgentCascade"
generated_by: coder
generated_from_task: "todo149 — fire auto-skill reflection on last-turn tool call; mid-run control test failed because Phase 4 continue skips Phase 5"
---

## Goal

Write test drivers for `AgentEngine.run()` that correctly model the phase-based control flow, so assertions about when triggers fire (or don't) are accurate.

## The Key Non-Obvious Fact

In `core.py`'s `run()` loop:

1. **Phase 4** (`if self._process_response(...):`) — fires on tool-call turns. On success it does `yield response; continue`, which **skips Phase 5 entirely** for that iteration.
2. **Phase 5** (`completed = self._post_turn_checks(...)`) — only reached when `_process_response` returns False (no tool call). The auto-skill natural-completion trigger lives here.

Therefore: **a tool-call turn NEVER reaches `_post_turn_checks`.** Any test driver that assumes `_post_turn_checks` is called after a tool call is wrong.

## Procedure

### Step 1 — Identify which phase your scenario exercises
- Tool call on the last original turn → Phase 4 path (new in todo149).
- Natural completion (no tool call) on any turn → Phase 5 path.
- Tool call on a non-last turn → Phase 4 `continue` with no trigger; loop proceeds to next turn.

### Step 2 — Shape the `_post_turn_checks` mock to match real phase routing
- For **Phase 4 scenarios** (tool-call trigger): the mock is irrelevant for the tool-call turn itself (never called). Design it so a LATER natural-completion turn can fire Phase 5 if your test needs the extension to fire via that path.
- For **mid-run control tests** (tool call on turn N < last): after the tool-call turn `continue`s, the next turns are text turns. Report a genuine natural end (`return False`) on the correct check number so Phase 5 fires on the intended turn.

### Step 3 — Use a counter-based side_effect, not a list
`core.py`'s `except Exception` catches `StopIteration`, so an exhausted `side_effect` list is silently swallowed and the tail dies after one turn. Use a function that keeps returning True (keep looping) until the natural-end check, then False:

```python
def _ptc_driver(*a, **k):
    # Return False only on the natural-end check; True everywhere else.
    return inst._current_turn != natural_end_turn_number
engine._post_turn_checks = MagicMock(side_effect=_ptc_driver)
```

### Step 4 — For tool-call turns, drive `_execute_detected_tools` and `fake_llm` together
- `fake_llm` yields a tool-call assistant message on the chosen LLM call number.
- `_execute_detected_tools` returns True for exactly that call (so `_process_response` → True → Phase 4 `continue`).

```python
if tool_call_at is None:
    engine._execute_detected_tools = MagicMock(return_value=False)
else:
    _tool_exec_calls = {'n': 0}
    def _exec_tool_driver(*a, **k):
        _tool_exec_calls['n'] += 1
        return _tool_exec_calls['n'] == tool_call_at
    engine._execute_detected_tools = MagicMock(side_effect=_exec_tool_driver)
```

## Tips

- **Never assume `_post_turn_checks` is called after a tool-call turn.** Phase 4's `continue` skips it. This caused a real test failure in todo149 where the driver was designed for a scenario that never occurs.
- The `_current_turn` attribute increments per LLM call, so use it (not a separate counter) to identify which turn is "last" — it stays in sync with `turns_available`.
- When testing that a mid-run tool call does NOT fire the trigger, you still need the extension to fire later (via Phase 5 on a natural-completion turn) to prove the run completed correctly. The key assertion is `_auto_skill_dirty_stop` stays False after the mid-run tool call.
- Related: [[engine-loop-test-offbyone-diagnosis]] for diagnosing off-by-one failures in these same loops; [[test_instance_defaults_bypass]] for the `AgentInstance.__new__` field-initialization gotcha that affects every test in this suite.
