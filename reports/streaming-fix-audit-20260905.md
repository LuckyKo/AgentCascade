# Streaming Fix Audit — commit safety classification (AgentCascade)

**Scope:** `030c0f6..5a039e5` (3 commits) + out-of-range tail/revert commits `9abeaa6`, `841292a`, `8abdf3d`, `4908ecd`, `609aacc`, `3fc4b39`.
**Baseline:** current HEAD after full revert `3fc4b39` — verified byte-identical to `030c0f6` for `state_builder.py`, `streaming.py`, `run_agent_unified.py`, `web_ui/app.js`, `api_server.py`.

## Executive summary

- The three in-range commits (`10f04aa`, `d51915a`, `5a039e5`) are **all SAFE** — pure performance/
  correctness fixes that never touch frame structure (`history_count`, message `index`, tail). They
  can each be re-applied independently on the reverted baseline, in any order.
- The actual regressions came from the **out-of-range** tail-serialization work (`9abeaa6` + `841292a`):
  observed frontend duplication/variant-swapping, and it also failed to fix the send-queue backpressure
  it targeted.
- **HEAD is currently red:** two leftover regression tests in `tests/test_state_builder.py` fail
  (verified by running pytest) because their fixes were reverted. Re-applying the SAFE subset fixes the suite.

## Per-commit classification

### SAFE (re-apply on reverted baseline, independently, no frontend contract impact)

| Commit | What it does | Why safe | Dependencies |
|---|---|---|---|
| `10f04aa` (BUG_0005 Fix A) | `state_builder.py` `serialize_message` store gate: also cache Pydantic `Message` objects (was dict-only). 1-line change + 2 regression tests. | Frame content/structure unchanged (same fields, same absolute `index`, full history, `index>0` guard preserved). Only avoids re-running `model_dump()` per tick. Evidence: 29 tests passed at landing; deterministic cache-hit sentinel test. | None |
| `d51915a` (BUG_0005 follow-up) | Separate `stream_token_stats_versions` dict in `cache.py` + read/write in `state_builder.py` — the token-stats cache and `_serialize_instances_incremental` share keys of different arity, causing permanent cache misses. | Pure cache-hit behavior fix; emitted values byte-identical. No frame structure change. Note: the `cache.py` field **already exists at HEAD** (revert left it); only the `state_builder.py` portion needs re-applying. | None |
| `5a039e5` (r_stats O(tail) + BUG_0004) | Cache-hit branch: `r_stats = _calc_stream_r_stats(stream_resp_snapshot)` instead of `responses` (full view); `_calc_stream_token_stats_uncached`: h_stats over committed history only, r_stats over in-flight partial only (disjoint sets → no double-count). + env-gated `AGENT_CASCADE_STREAM_TIMING` instrumentation ("REVERT-AFTER-MEASURE — Do NOT commit"). | Frame FIELDS unchanged; frontend only displays `total_tokens`, whose value is corrected downward by exactly the partial's tokens (intended bug fix, verified: 74 vs correct 53). Measured: r_stats 30.6 ms→5.2 ms at conv_len=43. Instrumentation is zero-overhead when env off; the untracked e2e test degrades gracefully (`try/except` → "unavailable"). | None |

### BROKEN (do NOT re-apply)

| Commit | What it did | Observed regression (evidence) |
|---|---|---|
| `9abeaa6` | Tail-only serialization (last 20 committed msgs, absolute indices) + byte-budget binary-search tail reduction + tail-only **final `done` state** in `run_agent_unified.py`. | (1) **UI breakage (duplicates/variant swapping/flicker):** non-partial frames became tail-only, breaking the frontend invariant array-position==absolute-index → duplicate absolute indices rendered, array-length oscillation forcing re-renders (`.agent_lessons/frontend-tail-splice-duplication-bug.md`, verified in code L2080-2084/L2054-2055). (2) **Failed to fix its target:** send-queue still saturated (depth 127, enqueue→send 9–15 s, `send_text` still scaling with conv_len — `tail-only-serialization-did-not-fix-send-backpressure.md`); payload breakdown showed full-history frames still dominant (`streaming-payload-breakdown-tail-cut-inert.md`). Reverted by `609aacc` ("breaks frontend compatibility"). |
| `841292a` | Review cleanup on top of `9abeaa6` (docstrings, byte-budget heuristic note, `_STREAM_TAIL_SIZE` import). | Inherits BROKEN — depends entirely on `9abeaa6`. |

### RISKY (safe no-ops vs the reverted backend; only meaningful paired with a reworked tail change)

| Commit | What it does | Assessment |
|---|---|---|
| `8abdf3d` | Frontend: index-based merge (`mergeTailMessages`) for tail-only frames; `_lastHistoryCount` high-water mark; null-gap guards in `updateGenStats`. | Against the baseline backend (full-history, dense indices, `len(messages) == history_count`), the tail-merge branch never fires → **no-op**. Re-apply only together with a future tail-only backend change. |
| `4908ecd` | Frontend: non-partial frames always full-replace. | Matches baseline behavior for full-history frames → **no-op** today. Same pairing requirement. |

### Frontend contract coupling points (per request)

- `web_ui/app.js` `case 'stream_update'` (baseline): partial frames splice positionally via `startIdx = history_count - sa.messages.length`; non-partial frames full-replace. This only works because frames are full-history.
- `history_count` = total committed + unique streaming partials (`state_builder.py`); every message carries an absolute `index` (baseline sets `d['index'] = index`).
- **`api_server.py` was NOT modified in any commit in range** (or in the tail/revert series) — no server-side frame-contract change there. The `api_server.py` payload instrumentation mentioned in lessons was never committed (it lives only in the untracked e2e test).
- `web_ui/app.js` was only touched by `8abdf3d` / `4908ecd` / `609aacc` (all after `5a039e5`); the three SAFE commits touch only `state_builder.py`, `cache.py`, `streaming.py` + tests.

## Residuals at HEAD (post-revert) — action items

1. **Failing tests (suite is red):** `tests/test_state_builder.py::test_serialize_message_caches_pydantic_message_object` and `::test_stream_token_stats_no_double_count_of_partial` FAIL against the reverted code (verified: 2 failed, 2 passed). Re-applying `10f04aa` + `5a039e5` fixes both; alternatively delete/guard the two tests.
2. **Dead field:** `cache.py` `stream_token_stats_versions` — harmless; keep it if `d51915a` is re-applied (it's half-applied already), delete if not.
3. **Untracked** `tests/test_streaming_fullstack_e2e.py` references `dump_stream_timings()` etc. — wrapped in `try/except`, degrades to "(unavailable)" after revert. Safe as-is.

## Recommended minimal safe subset (re-apply on the reverted baseline)

1. `10f04aa` — Pydantic Message UI-cache (1 line + tests).
2. `d51915a` — separate token-stats version dict (state_builder portion only).
3. `5a039e5` — BUG_0004 double-count fix + O(tail) r_stats (skip or keep the timing instrumentation; it's env-gated and the untracked e2e test wants it).

All three are order-independent, individually revertible, and leave the frontend contract untouched.
The tail-only work (`9abeaa6`/`841292a`) should not be retried as-is: the evidence shows the
backpressure culprit is the **full-state refresh path** (`build_state_from_pool` sends full 480 KB
frames) interleaved with stream ticks — any future fix must address that path AND ship with the
frontend index-merge (`8abdf3d` + `4908ecd`) in the same change.

## Confidence

- Commit contents & file-level attribution: **confirmed** (direct `git show`/`git diff` inspection).
- SAFE classification: **confirmed** (diffs + test evidence at landing + current-failure verification).
- BROKEN classification: **confirmed** (two independent verified lesson files with code-level analysis + the revert commit message citing "breaks frontend compatibility").
- Open unknown: whether re-applying the SAFE subset restores the pre-regression E2E latency curve end-to-end (per-tick cost fixed; residual full-frame send cost is a separate, still-open problem — see [[streaming-e2e-latency-not-history-driven]]).
