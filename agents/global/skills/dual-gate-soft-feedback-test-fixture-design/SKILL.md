---
name: dual-gate-soft-feedback-test-fixture-design
description: Designing test fixtures for a soft keyword-overlap feedback gate that runs after a hard similarity reject gate, so the fixture is not rejected before reaching the overlap notice.
source: auto-generated
version: "1.0.0"
triggers:
  - "soft overlap gate test"
  - "hard similarity reject gate"
  - "propose_skill overlap"
  - "difflib threshold fixture"
  - "keyword match score floor"
generated_by: coder
generated_from_task: "todo.md:158 propose_skill match-overlap soft gate + auto-activate inactive skills; test fixtures failing because the hard similarity reject gate fires before the soft keyword-overlap feedback gate."
---

## Goal
Write a passing test for a SOFT/advisory gate (e.g. a keyword-overlap notice) that sits AFTER a hard-reject gate (e.g. difflib similarity), without the fixture being killed by the hard gate before it ever reaches the behavior under test.

## The trap
When feature B (soft feedback) only runs after feature A (hard reject) passes, a fixture that "triggers" B is usually ALSO too similar to an incumbent and gets rejected by A first. Symptom: your test fails with A's rejection message ("REJECTED: … too similar") even though you never asserted on A — so it looks like B is broken when the real problem is the fixture.

## Procedure
### Step 1 — Identify both gates' pass conditions
- Hard gate (A): e.g. difflib `SequenceMatcher.ratio() > 0.95` → reject. You need ratio **< threshold**.
- Soft gate (B): e.g. keyword match-score `= len(query_tokens & indexed_kws) / len(query_tokens)`; notice fires when score **>= floor** (e.g. 0.15).

### Step 2 — Compute BOTH scores for your candidate fixture BEFORE writing it
Do not eyeball it. Replicate the exact scoring logic (read the matcher source for the token regex and normalization) and print both numbers:
```python
from difflib import SequenceMatcher
import re
TOKEN = re.compile(r'[a-zA-Z0-9_]+(?:-[a-zA-Z0-9_]+)*')
def sft(name, desc, trig):  # mirror the real text builder (name + desc + triggers)
    tt = ' '.join(trig) if isinstance(trig, list) else str(trig or '')
    return re.sub(r'\s+', ' ', f"{name} {desc} {tt}").strip()
def hard(a, b):  return SequenceMatcher(None, a, b).ratio()          # must be < 0.95
def soft(indexed, query):
    qt = set(TOKEN.findall(query.lower())); ik = set(TOKEN.findall(indexed.lower()))
    return min(len(qt & ik) / max(len(qt), 1), 1.0)                  # must be >= floor
```

### Step 3 — Tune the fixture into the window
The sweet spot is: **shared keyword cluster** (drives soft score up) + **a few DISTINCT extra tokens** in the proposal (drive difflib down). Add words like "and deployment pipelines" to the description. Re-run Step 2 until you land in the window, e.g. hard ~0.86 (< 0.95) AND soft ~0.67 (>= 0.15). Document the verified numbers in a comment so the next agent knows why the fixture looks odd.

### Step 4 — Scope assertions to the behavior under test
- For a "notice appears" case, assert on the approval description / returned structure that carries the soft feedback, and that the result is NOT A's rejection.
- For an exclusion case (e.g. self-exclusion), scope the negative assertion to the relevant sub-region (the list lines), not the whole string — the surrounding text legitimately contains the name.

## Tips
- If your "soft gate" test fails with the HARD gate's message, that is the fixture problem, not a feature bug — go back to Step 2.
- The window can be narrow when A and B share vocabulary; if you cannot find it, make the incumbents share fewer/longer tokens so soft stays high while hard drops.
- Keep the shared cluster semantically coherent (real words) so the fixture also reads sensibly in logs.
- Verify each case is revert-proof (fails without the fix) per [[regression-test-revert-proof]].
