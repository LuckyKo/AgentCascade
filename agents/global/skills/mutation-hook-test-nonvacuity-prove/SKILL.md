---
name: mutation-hook-test-nonvacuity-prove
description: For hermetic E2E integration tests of the AC Telegram bridge (real on_message -> command dispatcher -> real ACClient HTTP -> fake _MockACServer), prove the test is non-vacuous by temporarily injecting an env-var-guarded mutation into production code, confirming the test FAILS with the mutation on, then fully reverting and verifying git diff is empty.
source: auto-generated
version: "1.0.0"
triggers:
  - "non-vacuous"
  - "vacuous test"
  - "mutation test"
  - "bypass the dispatcher"
  - "prove test fails if"
  - "telegram bridge e2e test"
generated_by: coder
generated_from_task: "Phase 4 (a) E2E hermetic integration test for AC Telegram bridge: real on_message -> command dispatcher -> real ACClient HTTP -> fake AC server; must confirm the /stop test actually fails if the dispatcher is bypassed, not just passes trivially."
---

## Goal
Before delivering a test whose whole point is catching a specific regression ("X must never reach Y", "path A must route to endpoint B"), PROVE it would fail if that behavior broke — instead of trusting that the assertions are meaningful. Validated on the AC Telegram bridge E2E suite (tests/test_telegram_bridge.py section 9).

## Procedure

### Step 1 — Pick the minimal mutation
Identify the single production line whose removal/bypass would break the guarantee under test (e.g., bot.py's `if text.startswith('/'): dispatch_command(...)` early-return). The mutation should be the SMALLEST change that simulates the regression class, not a rewrite.

### Step 2 — Inject it behind an env-var guard
Do NOT hard-disable production code (edit guards may reject "disabling security boundaries", and it's harder to audit). Gate it so default behavior is byte-identical:
```python
if text.startswith('/') and not os.environ.get('TG_TEST_BYPASS_DISPATCHER'):
    reply = await dispatch_command(text, ac, cfg)
    ...
```
Add a clearly-labeled `# TEMPORARY MUTATION-TEST HOOK (reverted in the same session)` comment. If the guard needs `import os` (or similar), add it in the SAME edit — a missing import is an instant NameError on the production path.

### Step 3 — Run the test with the mutation ON; it MUST fail
```bash
set TG_TEST_BYPASS_DISPATCHER=1 && python -m pytest tests/test_telegram_bridge.py::test_e2e_stop_command_hits_api_stop_and_never_injects -o addopts="" --timeout=60 -q   # Windows
TG_TEST_BYPASS_DISPATCHER=1 python -m pytest ...   # POSIX
```
A failing assertion (not a crash) at the RIGHT line is the proof. If it still passes, the test is vacuous — fix the assertions before proceeding.

### Step 4 — Revert completely and verify with git
Revert BOTH edits (the guard AND any added import). Then:
- `git diff <file>` must be EMPTY (a whitespace-only CRLF warning on Windows does not count as a diff).
- Re-run the full suite to confirm green.
If the edit tool refuses the revert, restore from its auto-backup path shown in the rejection/edit response.

## Tips / gotchas
- **Edit guards reject unexplained production mutations.** An empty justification + self-labeled "mutation test" that weakens a documented security boundary gets DENIED. The env-var-guarded form (default behavior unchanged) is what passes review; explain the temporary purpose in the justification.
- **Assert on SIDE EFFECTS, not just call counts**, so the mutation produces a visible, meaningful failure: e.g., `mock.command_calls == []` AND the user-visible reply text changed to the inject-path ack ("🏃 Started → Maine"). A test that only checks "no exception" often survives mutations it should catch.
- **Watch for state mutated by background work**: if the code under test spawns fire-and-forget tasks (waiters) that hit the mock, use `>=` assertions on counters those tasks touch, and drain the tasks (`await asyncio.wait_for(t, timeout)` over the tracked task set) so none leak out of the event loop.
- Keep the hook's lifetime to ONE session: inject → verify failure → revert → git-diff-empty, all before reporting. Never leave an env-var bypass in production code, even if "harmless".
