# Code Review — reasoning-only soft "continue" (pure-resend) retry

**Reviewer:** reviewer (`reasoning_retry_code_review`) — independent of implementer.
**Verdict: PASS** (1 MINOR documentation finding, now resolved).

## Findings

### M1 (MINOR) — Plan off-by-one in cap arithmetic — **RESOLVED**
- **Issue:** The plan stated "2 soft + 3 full = 5 total" for N=2, cap=5. The actual code uses `if _auto_continue_count >= MAX_AUTO_CONTINUE_ATTEMPTS: return False` (core.py:1676–1683), evaluated *right after* incrementing and *before* performing the retry. So the 5th reasoning-only call returns False **without** a 5th retry → only **4 actual retries fire**: 2 soft + 2 full.
- **Code & tests:** correct and internally consistent. Test `test_full_retries_keep_firing_after_soft_budget_exhausted` asserts `results == [True, True, True, True, False]` and computes `n_full = cap − N − 1`. `test_total_attempts_never_exceeds_cap` asserts `results.count(True) == cap − 1`.
- **Resolution:** Plan corrected (F2 bullet + test section) to state "2 soft + 2 full = 4 actual retries, then the 5th call gives up", and that the `>= MAX_AUTO_CONTINUE_ATTEMPTS` check means at most `cap − 1` retries fire. No code change needed — this is a pre-existing property of the cap logic, not something introduced by this feature.

### BLOCKER: none
### MAJOR: none
### NIT: none

## Invariant confirmation (verified against source + tests)
| Invariant | Status | Evidence |
|-----------|--------|----------|
| **N1** — after full-retry rollback only `_reasoning_only_pending_nudges` is zeroed; subsequent full retries pop exactly `len(turn_output)` | ✅ | core.py:1733 zeroes pending nudges post-rollback; test `test_consecutive_full_retries_pop_exactly_turn_output` asserts `[1, 1]`. |
| **N2** — `pop_count += _reasoning_only_pending_nudges` guarded to reasoning-only | ✅ | core.py:1724–1725; tests for empty-output / broken-json / truncation assert counters stay 0. |
| **N3** — `_reasoning_only_soft_attempts` never resets mid-episode | ✅ | zeroed only at cap-hit (core.py:1681) and normal completion (core.py:1740); tests confirm soft path never reopens. |

## Test results (reviewer ran independently)
```
cd N:\work\WD\AgentCascade && python -m pytest tests/test_reasoning_only_continue_retry.py -p no:xdist -o addopts="" -q
24 passed in 0.13s
```

## Concurrency / locking
`_inject_soft_continue_nudge` holds `instance._compression_lock` while appending to messages/llm_messages/response and calling `_append_and_log`; mirrors the existing urgent-message injection pattern; no double-acquire (deadlock) risk. Inert by default (nudge OFF).

## Conclusion
Implementation faithfully executes the approved design with pure-resend default. All three critical invariants hold. Ready for commit on M1 documentation fix (applied).
