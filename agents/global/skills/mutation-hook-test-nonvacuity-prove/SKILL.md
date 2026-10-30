---
name: mutation-hook-test-nonvacuity-prove
description: Prove a regression test is a true discriminator (not vacuous) by temporarily re-introducing the exact condition the fix removed - an env-var-guarded bypass in dynamic languages, or wrapping the newly-always-taken branch back in its old guard in compiled languages (keeping new scaffolding so it still compiles) - confirming the test FAILS, then fully reverting and verifying git diff is empty.
source: auto-generated
version: 1.1.0
triggers:
  - "non-vacuous"
  - "vacuous test"
  - "mutation test"
  - "bypass the dispatcher"
  - "prove test fails if"
  - "telegram bridge e2e test"
  - "true discriminator"
  - "test fails on unfixed code"
  - "re-introduce the fixed-away condition"
generated_by: coder
generated_from_task: "Phase 4 (a) E2E hermetic integration test for AC Telegram bridge: real on_message -> command dispatcher -> real ACClient HTTP -> fake AC server; must confirm the /stop test actually fails if the dispatcher is bypassed, not just passes trivially."
---

## Goal
Before delivering (or trusting) a test whose whole point is catching a specific regression ("X must never reach Y", "path A must route to endpoint B", "the fix makes the engine reclaim instead of 429"), PROVE it would FAIL if that behavior broke — instead of trusting that the assertions are meaningful. Validated on the AC Telegram bridge E2E suite (Python) and on a C++ admission-path regression test.

## Procedure

### Step 1 — Pick the minimal mutation
Identify the single production line/condition whose removal (or restoration) flips the guarantee under test. The mutation must be the SMALLEST change that simulates the regression class, not a rewrite.

### Step 2 — Apply the mutation in the form that fits the language
**Dynamic language (Python/JS):** gate it behind an env-var so default behavior is byte-identical:
```python
if text.startswith('/') and not os.environ.get('TG_TEST_BYPASS_DISPATCHER'):
    reply = await dispatch_command(text, ac, cfg)
    ...
```
Add a clearly-labeled `# TEMPORARY MUTATION-TEST HOOK (reverted in the same session)` comment. If the guard needs `import os` (or similar), add it in the SAME edit — a missing import is an instant NameError on the production path.

**Compiled language (C++/Go/etc.):** re-introduce the EXACT condition the fix removed. For a fix that deleted an `if (candidates.empty())` gate so a root candidate is now always appended, restore it as a one-token guard:
```cpp
if (root) {                       // fixed
if (root && candidates.empty()) { // TEMP BUG-XXX-DISCRIMINATOR: emulate pre-fix gate
```
Keep every NEW scaffolding the test depends on (added enums, struct fields, params) so the test STILL COMPILES — you are mutating the *behavior*, not removing the interface. Prefer a mutation that touches only the one production TU the test exercises; grep which sources `#include` the changed header to confirm the rebuild blast radius is small (one TU + relink).

### Step 3 — Run the test with the mutation ON; it MUST fail
Dynamic:
```bash
set TG_TEST_BYPASS_DISPATCHER=1 && python -m pytest tests/test_telegram_bridge.py::test_e2e_stop_command_hits_api_stop_and_never_injects -o addopts="" --timeout=60 -q   # Windows
```
Compiled: rebuild ONLY the affected object then run the bundle entry (see the run-ctest-bundle skill for the standalone link / dispatcher):
```
ninja <build>/tests/<bundle>.exe && tests/<bundle>.exe <test_name>
```
A failing ASSERTION (not a crash) at the RIGHT line is the proof. If it still passes, the test is vacuous — fix the assertions before proceeding. If the build fails with a missing-include error like `C1083: Cannot open include file: 'cstddef'`, that is a `vcvars`/env problem, NOT the mutation — `call vcvars64.bat` and retry, and do not misreport it as the test failing.

### Step 4 — Revert completely and verify with git
Revert BOTH the mutation and any helper (guard/import). Then:
- `git diff <file>` must be EMPTY (a whitespace-only CRLF warning on Windows does not count as a diff).
- Re-run the full suite to confirm green (the FIXED code).
If the edit tool refuses the revert, restore from its auto-backup path shown in the edit/reject response.

## Tips / gotchas
- **Edit guards reject unexplained production mutations.** An empty justification + a self-labeled "mutation test" that weakens a documented boundary gets DENIED. State the temporary purpose and that you will revert it in the same session; the env-var-guarded form (default unchanged) is what passes review.
- **Assert on SIDE EFFECTS, not just call counts**, so the mutation produces a visible, meaningful failure (e.g. the user-visible reply text changed, or the request was NOT admitted). A test that only checks "no exception" often survives mutations it should catch.
- **Keep the interface, change only the behavior** in compiled languages: if you delete the enum/field the test asserts on, the test won't compile and you prove nothing about the discriminator — you proved nothing about the *behavior*.
- **Watch for state mutated by background work**: if the code under test spawns fire-and-forget tasks (waiters) that hit the mock, use `>=` assertions on counters those tasks touch, and drain the tasks (`await asyncio.wait_for(t, timeout)` over the tracked task set) so none leak out of the event loop.
- Keep the hook's lifetime to ONE session: inject → verify failure → revert → git-diff-empty, all before reporting. Never leave a bypass or re-introduced gate in production code, even if "harmless".
