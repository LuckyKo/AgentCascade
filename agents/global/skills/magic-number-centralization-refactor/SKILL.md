---
name: magic-number-centralization-refactor
description: How to replace hardcoded magic numbers in tests and source with named constants when a POLISH/review round flags them — including circular-import checks, house-style precedent matching, and contract-pin comments for module-level constants that live outside settings.py
source: auto-generated
version: 1.0.1
triggers:
  - "magic number"
  - "hardcoded constant"
  - "POLISH nits"
  - "named constant"
  - "circular import check"
generated_by: coder
generated_from_task: "POLISH rev122 nits — todo 122+123 skill matching"
---

## Goal
Replace hardcoded magic numbers in tests and source with named constants, safely (no circular imports), matching house style, and leaving contract-pin comments where the constant lives outside settings.py.

## Procedure

### Step 1 — Locate every occurrence of the magic number
Grep for the literal value across both source and test files:
```bash
grep -rn '== 20\|range(20)\|> 20' tests/ agent_cascade/
```
Classify each hit: does it represent the constant's semantic meaning (e.g., "max candidates"), or is it an unrelated number that happens to equal the constant? Only replace the former.

### Step 2 — Check circular-import risk before hoisting to module top
Before moving a local import to module level, verify the target module's own imports:
```bash
grep -n '^import\|^from' agent_cascade/settings.py
```
If the target only imports stdlib (os, ast, pathlib, dataclasses, typing), it is safe to import at module top. If it imports from the same package subtree (e.g., `agent_cascade.skills.*`), there IS circular risk — keep one local import and delete duplicates instead.

### Step 3 — Match house style for test constant imports
Check how OTHER tests in the same file (or conftest.py) handle settings/module constants:
```bash
grep -n 'from agent_cascade.settings import\|from agent_cascade.tools' tests/test_*.py
```
- If tests import constants directly (even private ones like `SKILL_SCAN_MAX_RESULTS`), keep the import and add a **contract-pin comment** in the docstring: "If this constant is ever moved, update this import — assert against the live value, never hardcode."
- If house style forbids importing private names, use the constant's VALUE behavior (e.g., create N+5 items and assert exactly N are shown) without naming the constant.

### Step 4 — Replace in tests with the named constant
```python
from agent_cascade.settings import SKILL_ADVISOR_MAX_CANDIDATES

# Before:
assert len(skill_lines) == 20
names = self._names(20)  # boundary test

# After:
assert len(skill_lines) == SKILL_ADVISOR_MAX_CANDIDATES
names = self._names(SKILL_ADVISOR_MAX_CANDIDATES)  # boundary test
```
Update docstrings that reference the number ("total (30) > N=20") to use "N" instead.

### Step 5 — For source: make mutable constants immutable + extract duplicated logic
- Mutable dict constant → `frozenset` of pairs (immutable); derive a `dict()` view once at module level if O(1) lookup is needed in a hot loop.
- Duplicated slice/fallback logic across two code paths → extract a small private helper (`_name_slice_fallback(entries, n)`) and call it from both sites. This guarantees identical semantics and makes the invariant auditable in one place.

### Step 6 — Run affected test files together
```bash
python -m pytest tests/test_skills_system.py tests/test_skill_advisor.py -q
```
All must pass before committing.

## Tips
- **Don't replace numbers that are fixture sizes, not cap references.** `self._names(30)` is a corpus size (deliberately > N); only the assertion `== 20` represents the constant. Replacing the fixture size would break the test's intent.
- **Boundary tests use the constant as the corpus size** (`_names(SKILL_ADVISOR_MAX_CANDIDATES)`) — this is correct and self-documenting: "total == N exactly → NOT filtered."
- **Contract-pin comments matter most for module-level constants** (not in settings.py). They tell future developers WHY the import exists and what to do if the constant moves.
- **frozenset-of-pairs vs frozen dict**: Python has no frozen dict. `frozenset((k, v), ...)` is immutable but O(n) lookup. For ≤5 entries with a derived `dict()` view, this is fine. For larger maps, consider `types.MappingProxyType` (read-only view over a dict).
- **Pre-commit two-pass**: on Windows + AgentCascade, the first commit often aborts after `mixed-line-ending` auto-fixes staged files. Re-stage the same explicit file list and re-commit.
