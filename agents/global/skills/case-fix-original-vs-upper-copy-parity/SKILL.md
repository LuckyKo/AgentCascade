---
name: case-fix-original-vs-upper-copy-parity
description: Fix a security approval/rejection reason (or any user-facing text) that arrives in all caps sometimes — root cause is slicing the output from an .upper()'d copy; keep an original-case copy for output, match on upper, and remove tokens case-insensitively or a lowercase token leaks into the result.
source: auto-generated
version: 1.0.1
triggers:
  - "all caps sometimes"
  - "reason text in all caps"
  - "approval rejected reason"
  - "security approval feedback"
  - "upper() copy slicing"
  - "original case preservation"
  - "case-insensitive token removal"
  - "justification uppercased"
  - "mixed case output bug"
generated_by: orchestrator
generated_from_task: "todo.md:155 — the security approval/rejected reason text arrives in feedback in all caps sometimes (AgentCascade _parse_verdict Fallback-2 fix)"
---

## Goal
Fix a "sometimes renders in ALL CAPS / wrong case" output bug (e.g. a security approval/rejection reason shown to the user) at its root cause, without introducing a new token-leak regression that the original `.upper()` had been silently masking.

## Why this is subtle
A common pattern upper-cases a line for a **case-insensitive membership test** (`'[NO]' in lc.upper()`) but then slices the *displayed* value from that same upper-cased copy — so the user sees ALL CAPS. The obvious fix is "keep an original-case copy and slice from it." But if you now remove a marker/token from the original-case copy with a **case-sensitive** `str.replace('[NO]', '', 1)`, a lowercase `[no]` (or `[Yes]`) in the source no longer matches and **leaks into the output**. The old code never had this problem because it sliced from the upper-cased copy, where `.replace('[NO]')` always matched. So the "obvious" fix trades one bug for another.

## Procedure
### Step 1 — Confirm which code path produces the bad case (don't assume)
The bug is often **intermittent** ("sometimes") because only ONE of several parsing/fallback strategies upper-cases the value. Read every branch that builds the output string and check whether it slices from an `.upper()`'d copy or the original. Fix only the offending branch(es); leave already-correct branches untouched (a regression guard test for them is cheap).

### Step 2 — Keep a parallel original-case copy; match on upper, slice on original
```python
line_orig  = clean(line)          # original case → used to build the OUTPUT value
line_upper = line_orig.upper()    # upper case   → used ONLY for the membership test
if token in line_upper:           # case-insensitive match (still finds [no]/[No])
    ...
```

### Step 3 — Remove tokens from the ORIGINAL copy CASE-INSENSITIVELY
Use `re.sub` with `flags=re.IGNORECASE`, not a case-sensitive `.replace()`:
```python
import re
out = re.sub(r'\[YES\]', '', line_orig, count=1, flags=re.IGNORECASE)
out = re.sub(r'\[NO\]',  '', out,       count=1, flags=re.IGNORECASE).strip()
```
This preserves the old code's "always matches" behavior (lowercase `[no]` is still stripped) while keeping the rest of the string in its original case. If the original used two sequential `.replace(..., 1)` calls (one per token), keep **two** sequential `re.sub` calls for exact parity — a single combined pattern or dropping one sub can leak a token when a line contains both markers.

### Step 4 — Write revert-proof tests that cover the LEAK, not just the case
A test that only asserts "output is mixed-case" passes even if you ship the new leak bug. Add a case where the **source token is lowercase** (e.g. `[no] some reason`) and assert it is detected AND stripped from the output. That single test is what catches the regression the naive fix introduces. Also keep: a correct-path guard, a prefix-strip guard (e.g. `Reason:`), and a case-insensitive-match guard.

## Tips
- The "sometimes" in a case bug is your clue that multiple code paths exist — enumerate them all before editing.
- `.upper()` for matching + original-case for output is the right shape; the trap is only in how you *remove* markers from the original copy.
- `str.replace(x, '', 1)` is case-sensitive and removes only the first occurrence — mirror the exact count/parity of the code you're replacing.
- If a method under test uses no instance state (only module-level regexes), `Cls.__new__(Cls)` is a clean, explicit way to avoid a heavy constructor in unit tests — document why in the docstring; it's clearer than a MagicMock here.
