# Implementation Summary — Reasoning-Only Soft "Continue" (Pure-Resend) Retry

Implements the approved design from `reasoning_only_continue_retry_plan.md`, with all review
findings in `reasoning_only_continue_retry_plan_REVIEW.md` honored. **No deviations from the plan.**

## What was built

For a `"reasoning-only"` end turn, the engine now performs up to N **pure-resend** soft continues
(re-call the LLM on the SAME history; the reasoning message stays in place; NO new message, NO
rollback) before falling back to the existing **full-retry** behavior (pop last message + rebuild
working set + clear response + silent re-call). Both share the existing `MAX_AUTO_CONTINUE_ATTEMPTS`
(=5) budget. The nudge is deferred behind `SOFT_CONTINUE_NUDGE_ENABLED` (default `False` = pure resend).

## Files changed (with line references)

### 1. `agent_cascade/settings.py`
- **L30–33** — added `REASONING_ONLY_CONTINUE_ATTEMPTS: int = int(os.getenv('QWEN_AGENT_REASONING_ONLY_CONTINUE_ATTEMPTS', 2))` (default 2), with a comment explaining the shared-budget semantics.
- **L34–40** — added `SOFT_CONTINUE_NUDGE_ENABLED: bool = os.getenv('QWEN_AGENT_SOFT_CONTINUE_NUDGE', '0') == '1'` (default `False`), with a comment describing the deferred nudge escape hatch.
- Placed directly under `MAX_AUTO_CONTINUE_ATTEMPTS` (L29); matches the file's existing `os.getenv` convention.

### 2. `agent_cascade/agent_instance.py`
- **L318–328** — added a new "Reasoning-only soft-continue state" section next to the other auto-continue fields:
  - **L323** — `_reasoning_only_soft_attempts: int = field(default=0)` (episode budget / soft-path guard; never reset mid-episode → N3).
  - **L328** — `_reasoning_only_pending_nudges: int = field(default=0)` (rollback accounting for injected nudges; only non-zero when nudge enabled; zeroed after a rollback pops them → N1).

### 3. `agent_cascade/engine/core.py`
- **L38–39** — imported `REASONING_ONLY_CONTINUE_ATTEMPTS`, `SOFT_CONTINUE_NUDGE_ENABLED` from settings (added to the existing `from agent_cascade.settings import (...)` block).
- **L1649** — `_check_and_handle_truncation()` modified:
  - **L1677–1683** — cap-hit reset (site a): now also zeroes `_reasoning_only_soft_attempts` and `_reasoning_only_pending_nudges` alongside `_auto_continue_count` / `_continue_fallback_append`, then returns `False`.
  - **L1685–1707** — NEW soft-continue branch for reasoning-only, guarded by `instance._reasoning_only_soft_attempts < REASONING_ONLY_CONTINUE_ATTEMPTS`: increments the budget counter, records telemetry + log, and (only when `SOFT_CONTINUE_NUDGE_ENABLED`) increments `_reasoning_only_pending_nudges` + calls `_inject_soft_continue_nudge(...)`. Sets `_auto_continue_triggered = True`, returns `True`. **Default (nudge OFF) = pure resend: appends nothing, no rollback.**
  - **L1709–1735** — existing full-retry path, extended for reasoning-only only: **L1724–1725** `pop_count += instance._reasoning_only_pending_nudges` (guarded to `is_incomplete == "reasoning-only"` → N2); **L1733** zeroes `_reasoning_only_pending_nudges = 0` after rollback (N1). Does NOT reset `_reasoning_only_soft_attempts`.
  - **L1737–1742** — normal-completion reset (site b): now also zeroes both reasoning-only counters alongside `_auto_continue_count` / `_continue_fallback_append`, then returns `False`.
- **L1744–1757** — NEW `@staticmethod _reasoning_only_continue_text(attempt: int) -> str`: the two deterministic escalating strings from plan F4.
- **L1759–1789** — NEW `_inject_soft_continue_nudge(self, instance, inst_name, messages, llm_messages, response)`: mirrors the urgent-message injection pattern; under `instance._compression_lock` appends to `messages` + `llm_messages` + `response`, then `_append_and_log(..., lock_held=True)`; no rollback / no response clear. Only called when nudge enabled.

### 4. `tests/test_reasoning_only_continue_retry.py` (new)
24 tests across the plan's Section 6 cases:
- **Unit regression** for `_is_incomplete_state` (reasoning-only / empty-output / broken-json / complete). Includes a documented guard for a pre-existing quirk: a pydantic `FunctionCall` object's arguments are read as `''` by `helpers.py:338`, so such a message is NOT flagged broken-json (out of scope; left unchanged).
- **Default pure-resend path** — no message appended, no rollback, counters advance.
- **Soft→full transition** and **cap enforcement** (exactly N soft + remaining full within the cap).
- **N3** — soft path stays closed after a full retry (budget never reset mid-episode).
- **N1 / N2** — correct pop counts, no over-pop across consecutive full retries; reasoning-only guard leaves broken-json/empty-output/truncation unaffected.
- **`auto_continue=False` regression** — nothing fires.
- **Nudge-ON path** (monkeypatched flag) — nudge appended with escalating text, cleanup on soft→full transition, no over-pop after cleanup, and the default-N cap-hit edge documented.

## Test commands run + results

All run from `N:\work\WD\AgentCascade` (serial; `-p no:xdist -o addopts=""` to bypass the xdist/timeout defaults in `pytest.ini`).

1. **New suite:**
   ```
   python -m pytest tests/test_reasoning_only_continue_retry.py -p no:xdist -o addopts=""
   ```
   → **24 passed** (0 failed).

2. **Telemetry + retry + loop suites (regression):**
   ```
   python -m pytest tests/test_telemetry.py tests/test_retry_policy.py tests/test_retry_baseline.py tests/test_inner_loop_regression.py tests/test_loop_detection.py tests/test_reasoning_only_continue_retry.py -p no:xdist -o addopts=""
   ```
   → **207 passed** (0 failed).

3. **Broader engine-adjacent suites (regression):**
   ```
   python -m pytest tests/test_agent_pool.py tests/test_phase5_polish.py tests/test_json_robustness.py tests/test_extract_text_function_call.py tests/test_loop_regression.py -p no:xdist -o addopts=""
   ```
   → **108 passed** (0 failed).

**Total: 339 passed, 0 failed** across the three runs (the new suite is counted in run #2 as well; unique coverage = all of the above). No regressions.

## Invariants honored (from review)
- **N3:** `_reasoning_only_soft_attempts` never resets mid-episode — only at cap-hit (L1681) and normal completion (L1740). Once it reaches N the soft path is permanently closed for that episode.
- **N1:** after a full-retry rollback pops nudges, ONLY `_reasoning_only_pending_nudges` is zeroed (L1733); subsequent full retries pop exactly `len(turn_output)` (no over-pop). Verified by `test_no_over_pop_after_nudge_cleanup` (`[3, 1]`) and `TestNoOverPop`.
- **N2:** the `pop_count += _reasoning_only_pending_nudges` adjustment is guarded to `is_incomplete == "reasoning-only"` (L1724), so broken-json / empty-output / truncation retries are unaffected.

## Deviations from the plan
**None.** Implementation matches the plan exactly, including the two deterministic nudge strings (F4) and the helper signatures. The only non-plan item is a *documented* pre-existing quirk in `_is_incomplete_state` (broken-json detection does not fire for pydantic `FunctionCall` objects because `helpers.py:338` only reads arguments when `func_call` is a dict); this was left untouched per scope and covered by a regression-guard test so any future fix is deliberate.

## Status
Implementation complete, all tests green. **Not committed** — awaiting independent code review / reviewer PASS before commit (per task instructions).
