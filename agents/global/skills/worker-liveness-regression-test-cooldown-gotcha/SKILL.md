---
name: worker-liveness-regression-test-cooldown-gotcha
description: When writing a regression test that proves a background worker/thread is alive after a stop→restart cycle, make the post-restart job match something NOT already in a per-item cooldown, or the suppression logic masks liveness and the test falsely passes.
source: auto-generated
version: "1.0.0"
triggers:
  - "worker restart regression test"
  - "stop resume worker alive test"
  - "cooldown suppresses second delivery"
  - "daemon thread respawn test"
  - "memory hint worker liveness"
generated_by: coder
generated_from_task: "todo162 memory-hint worker stop→resume restart fix + revert-proofed regression tests"
---

## Goal
Write a regression test that genuinely proves a background worker/thread is alive after a stop→restart (respawn) cycle — without the system's own suppression/cooldown logic giving a false green.

## The gotcha
Many hinting/caching workers keep **per-item cooldown state** (e.g. `_recently_hinted[item] = timestamp`, `cooldown_seconds`). If your test does:

1. job 1 → delivers hint for item X (stamps the cooldown),
2. stop → start (the cycle under test),
3. job 2 matching **the same item X** → assert delivered,

…then job 2 is **suppressed by the cooldown regardless of whether the worker is alive**. Post-fix the worker IS alive but still suppresses X; pre-fix the worker is dead and also delivers nothing. Both paths deliver zero → your "load-bearing" test passes on BOTH versions → it proves nothing. You only discover this when the test fails for a reason that looks like liveness but is actually cooldown.

## Procedure
### Step 1 — Probe the suppression state before trusting a red/green
When a post-cycle job unexpectedly delivers nothing, dump the per-item state (`inst._recently_hinted`, `_last_*_turn`, etc.) and check whether item X was already stamped by an earlier job. A dead worker AND a cooled-down live worker both yield zero deliveries — they're indistinguishable on that assertion alone.

### Step 2 — Use a DIFFERENT item for the post-cycle job
Plant **two disjoint-topic items** (lessons/skills/keys) in the fixture so each is a clear winner for its own query:
- job 1 → matches item A (stamps A's cooldown).
- stop → start.
- job 2 → matches item B (NOT stamped, NOT in cooldown).

Now delivery of job 2 is purely a function of worker liveness: dead worker → nothing; live worker → delivers B. The test is load-bearing again.

```python
# Two disjoint lessons so each query has a clear, non-cooldown winner.
_write_lesson(vault, 'a.md', 'A', 'topic A description', 'body A')
_write_lesson(vault, 'b.md', 'B', 'topic B description', 'body B')
mgr.start()
mgr.submit('w', QUERY_A, turn=1)   # delivers A, stamps cooldown[A]
assert wait(lambda: len(warnings) >= 1)
mgr.stop(); mgr.start()            # the cycle under test
warnings.clear()
mgr.submit('w', QUERY_B, turn=2)   # matches B — NOT in cooldown
assert wait(lambda: len(warnings) >= 1)  # fails pre-fix (dead worker), passes post-fix
```

### Step 3 — Revert-proof it anyway
Even with the cooldown fix, prove the test is load-bearing: temporarily revert ONLY the production fix, run the test, confirm it FAILS with the liveness signature (e.g. `is_alive()==False`, "never delivered"), restore, confirm PASS. See [[regression-test-revert-proof]].

## Tips
- **Bounded polling, not fixed sleeps** — wait-for-delivery with a deadline; see [[testing-best-practices]] / [[deterministic-delay-recording-tests]].
- **A "logically correct" assertion can still pass pre-fix** if it targets the wrong item/slot. When a supposedly-regression test passes on reverted code, dump the actual state structure and fix the assertion — don't assume. (Same lesson as [[regression-test-revert-proof]] Step 3.)
- **Real harness > invented one.** For pool-level e2e, reuse an existing integration fixture; if you build a minimal instance via `__new__`, set the fields unrelated supervisor threads read (e.g. `parent_instance`) or they log AttributeError noise that drowns the real signal.
- Related: [[hermetic-skill-manager-test-factory]] for the real-SkillManager fixture, [[engine-loop-test-offbyone-diagnosis]] for loop-state assertions.
