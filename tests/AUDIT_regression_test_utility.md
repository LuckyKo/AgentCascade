# Regression Test Utility Audit — AgentCascade/tests

**Date:** 2026-08-12
**Auditor:** researcher (test_audit_analyst)
**Method:** Content review of test bodies (not just filenames), including assertion strength,
mock-vs-real behavior, contention/pressure levels, and duplicate-coverage analysis.

**Reference standard:** `test_e2e_agent_calls.py` — black-box tests against a real HTTP mock LLM
server (`ProgrammableMockLLMHandler`), asserting interleaving-free serialization via request-log
identity extraction and wall-clock timing. Meaningful, would fail on real regressions.

---

## 1. Scheduler / Concurrency

### test_scheduler_integration.py — **KEEP** (as canonical)
- **What:** 30 tests. Real `SlotPool`/`EndpointScheduler` with real threads: FIFO grant order under contention (handshake-gated), reservation blocking (todo.md#93 scenario), cancel-on-termination, mass termination, soak (100 rounds × 5 agents), randomized agent-call-graph brute force with violation tracker (over-capacity, FIFO break, reservation bypass).
- **Why useful:** Exercises real contention with meaningful invariants (`assert_no_over_capacity`, FIFO batch assertions, wait-count/reservation assertions). Would catch a broken FIFO, broken reservation, or leaked permit.
- **Note:** Stress/soak tests carry ~30s+ runtime risk; the brute-force tests use `PROCEDURAL_SEEDS` — verify they don't time out in CI. `test_unlimited_concurrency_no_pool` correctly asserts conc=-1 endpoints bypass pools.

### test_scheduler_integration_refactored.py — **DROP** (duplicate)
- **What:** Same 30 test names as the above; AST comparison shows the *same* test bodies (only docstrings/formatting differ; helper `_stress_agent_task` inlined). Refactored variant is the newer, handshake-hardened revision.
- **Why useless as a separate file:** Running both files doubles scheduler test time with zero new coverage. If a regression breaks scheduling, BOTH files fail identically.
- **Recommendation:** Delete the *other* file, keep whichever is the maintained one (refactored is more deterministic via handshake gates; confirm with maintainer). If `test_scheduler_integration.py` is the one referenced by CI configs, delete `_refactored` instead. **Do not keep both.**

### test_slot_queue.py — **KEEP**
- **What:** 21 tests, unit-level on `SlotPool` internals (`_running`, `_waiters`, `_cond`) but with real multithreading: FIFO no-barging, cancel races (cancel-after-grant guard, no phantom permit), mass-cancel performance (<50ms for 100 waiters), reservation blocking, timeout isolation, cap-1 stress (8×100 iterations max 1 running).
- **Why useful:** The race-condition tests (cancel-after-grant, idempotent release, stale release) target real, hard-to-catch concurrency bugs and would fail on a permit leak. Internal-state access is acceptable for race determinism; it's white-box but not vacuous.

### test_endpoint_scheduler_stress.py — **KEEP**
- **What:** 20 tests on real `EndpointScheduler` with real threads: 50-agent acquire/release, 100-agent peak-active ≤ limit, sequential strict serialization (peak==1), shared sequential slot across endpoints, semaphore resize during active use, double/triple-release idempotence, stale-schedule cleanup, slot-holder tracking, stuck-slot detection.
- **Why useful:** Peak-observed ≤ limit is a real invariant; resize-up/down under active use is genuinely contention-heavy and would catch permit leaks or deadlocks.

### test_concurrency_dispatch.py — **REWRITE**
- **What:** 9 tests on `ToolDispatcher.handle_call_agent` sync/async path selection (sequential child → SYNC, parallel child → ASYNC, mixed, no-deadlock ordering).
- **Why value masked:** `_make_mock_pool` is 100% `MagicMock` — `pool._acquire_slot` returns `lambda: None` (slot acquisition is **mocked out entirely**); `dispatcher._run_child_sync/_run_child_async` are replaced with log-recording fakes. Tests assert only *which fake* was invoked. They prove the decision *logic* is wired, but never exercise the real slot-acquire/release path, so a slot leak or deadlock regression would pass.
- **What to change:** Use a real `SlotPool`/`EndpointScheduler` (as in `test_endpoint_scheduler_stress`) with real acquire/release, and assert that children actually execute + slots released, instead of faking `_run_child_*`. Alternatively fold the path-selection cases into `test_call_agent_sync_async_selection.py` (see below) and drop the duplicated ones.

### test_call_agent_sync_async_selection.py — **REWRITE**
- **What:** 12 tests on sync/async decision logic with real `APIRouter` endpoints (concurrency=0/2/-1) but a `MagicMock` pool and `MagicMock` engine (`_release_slot` mocked).
- **Why value masked:** Assertions are largely `pool.register_async_call.assert_called()/assert_not_called()` plus string checks ("launched asynchronously" in result). These verify the branch taken, not that the chosen path *works* (no real slot contention, no actual child execution, no result correctness).
- **What to change:** Replace MagicMock pool with real pool + real SlotPool; assert observable outcomes (children complete, slots released, no deadlock under contention) rather than mock call counts. Note the conc=-1 (unlimited) cases are covered by assertion that they always take ASYNC — keep those, but add a real-behavior check.

### test_rate_limiting_concurrency.py — **KEEP** (with note)
- **What:** 11 tests on real `APIRouter` rate limiting with real threads: RPM enforced/burst allowed, throttling after RPM, sliding-window no-double-count under 50 threads, window expiry, concurrent cleanup, fallback when rate-limited, per-endpoint isolation, retries counted.
- **Why useful:** Real router + real threading + a callback-based LLM stub. `test_sliding_window_no_duplicate_counting` (50 concurrent, rpm=100, expect exactly 50 completions) and `test_rate_limit_per_endpoint_not_global` assert real behavior.
- **Caveat:** `test_rate_limit_enforced_under_concurrency` (rpm=200) only asserts "some calls succeeded" — weak, but the burst/throttle tests carry the weight. Keep.

### test_nested_agent_calls.py — **REWRITE**
- **What:** 17 tests. **Mostly NOT scheduler tests** — the bulk is defensive-check unit tests for `_get_active_functions_from_template`/`_build_resources_block` with templates missing `llm`/`function_map` (MagicMock-based: "returns [] instead of crashing").
- **Why value masked:** The defensive checks are pure unit tests on helper functions (DROP-tier per criteria), but the *nested agent call* regression value (settings propagation merges `disabled_tools`) is real.
- **What to change:** Keep the settings-propagation and nesting-depth tests (they assert real merge behavior); drop or move the `MagicMock(spec=[])` "doesn't crash" tests into a unit-test module if they must stay — they prove nothing about system behavior.

### test_security_endpoint_inheritance.py — **REWRITE**
- **What:** 10 tests: security check caller resolution from approval, parent-instance assignment, run_check/execute_check signature compatibility, slot-bypass logging.
- **Why value masked:** The primary regression test (`test_primary_regression_async_child_triggers_security`) asserts `_run_check_worker` was *called* with the right args — but `_run_check_worker` is `patch.object`-mocked, so it proves the handler passed args correctly, not that a security check actually runs/approves/denies. Signature-compat tests are vacuous (asserting the mock signature).
- **What to change:** Let one real `run_check` flow execute against a real (or minimally-stubbed) security worker and assert the security instance is created with the child's endpoint config; keep arg-passing assertions only as secondary.

---

## 2. Compression

### test_compression_no_duplication.py — **KEEP**
- **What:** 31 tests. **Best of the compression files**: uses real `AgentPool` (not mock), real JSONL files, real `compress_context` + `reset_history(rewrite=True)` cycle mirroring production handler flow. Asserts no duplicates in pool AND jsonl across 1/3/6 compression cycles, marker count increments, tool-pair integrity, reload cycles (compress → save → reload → compress), orphaned-FUNCTION detection, timestamp consistency, 5-round stress with reloads.
- **Why useful:** Would fail on real duplication regressions, message-loss, marker-position, or tool-pair-split bugs. This is the compression equivalent of E2E.

### test_compression_consistency.py — **REWRITE** *(corrected after reviewer verification)*
- **What:** 31 tests. Uses **conftest's `MockAgentPool`** — a hand-rolled *reimplementation* of the production AgentPool subset ("Matches production logic from agent_pool.py:2093-2123" etc.) — plus locally-simulated JSONL sync (`_write_jsonl`, `simulate_reset_history`, `rebuild_working_set_from_jsonl` are test-local helpers, NOT the production handler/recovery code). `compress_context` itself is the real function, so pool-mutation/marker-structure assertions are meaningful, but the pool, JSONL sync, and crash-recovery paths are all simulated.
- **Why value is masked:** A divergence between the mock pool's `get_compression_target_set`/`find_last_marker` and the real `AgentPool` would go undetected — the mock is a parallel implementation that must be manually kept in sync. Crash-recovery tests validate the *local* rebuild simulation, not production recovery. `test_tail_sync_detects_drift` is valuable but exercises a test-local drift check.
- **What to change:** Port the valuable assertions (exact post-compression pool structure, tail-sync invariants, N-compressions) onto the real `AgentPool` + real logger harness used by `test_compression_no_duplication.py` (load_session_from_log + reset_history); delete the mock-pool variants that no_duplication already covers.

### test_compression.py — **REWRITE**
- **What:** 62 tests. Mostly unit tests on pure helpers (`compute_discard_count`, `build_marker_message`, `_find_last_marker`, fraction validation) with trivial list-based assertions (e.g., `count == 5`, `count == 8`) — these are DROP-tier per criteria. The `compress_context` tests use `MockAgentPool` (mock pool) + `patch("...invoke_compression_agent")` — they verify *invocation plumbing*, not real pool behavior.
- **Why partial value:** Clean-trim-not-cumulative, marker-position, force-mode, dry-run-no-mutation, token-guard, nested-compression-guard, precomputed-summary — these do assert real compress_context behavior, but against a mock pool.
- **What to change:** Port the compress_context-level tests to use the real `AgentPool` harness from `test_compression_no_duplication`; delete the trivial helper-constant tests (covered by no_duplication/consistency integration paths).

### test_compression_tool_pairs.py — **REWRITE**
- **What:** 30 tests. The tool-pair boundary refinement logic (`_refine_tool_call_boundary` Rules 1/2, batched chains) is real and important, and `TestBruteForceRegression` (random conversations, validate no splits) is strong. But the compress_context integration tests use `MockAgentPool` + patched invoke_compression_agent (mock pool), and many unit tests assert helper return values directly.
- **Why partial value:** Pair-boundary correctness is exactly the kind of regression that breaks silently — but the mock-pool tests wouldn't catch a real-pool message-ordering regression.
- **What to change:** Move pair-boundary integration tests onto the real-AgentPool harness (no_duplication already has tool-chain variants — merge); keep the brute-force validator; drop the trivial `_get_message_role` object-vs-dict unit tests or move to a unit module.

### test_compression_boundary_fix.py — **REWRITE**
- **What:** 37 tests. Pure helper-level tests on `compute_discard_count`/`refine` boundary cases with list-of-dicts inputs — no pool, no compression run, no JSONL. Exercises the boundary-refinement algorithm thoroughly (independent pairs, batched chains, landed-on-function, post-refinement guard).
- **Why partial value:** The algorithm itself is the bug-prone core, so these *would* fail if boundary logic regresses. But they're pure unit tests of a helper — no system behavior.
- **What to change:** Keep the algorithmic tests but consolidate with tool_pairs' boundary tests into one helper-level module; ensure the *real-pool* path (no_duplication) covers the same rules end-to-end, which it already does.

### test_fallback_compression.py — **KEEP**
- **What:** 23 tests. Real `ExecutionEngine` with a configured pool; real `_find_compression_slice` halving algorithm; overfeeding/max-rounds detection; fallback chain cursor advanced before raising; smart-slice token counting; full-flow non-compressor agent. Pool is MagicMock but engine internals are real and the slice algorithm is exercised meaningfully.
- **Why useful:** Would catch regressions in the iterative-halving fallback path and the cursor-advance-on-fallback behavior.

---

## 3. Loop Detection

### test_loop_detection.py — **KEEP** (with trimming)
- **What:** 61 tests. Real `detect_loop` on real Message sequences: pattern detection at various lengths, false-positive guards, truncation-marker normalization, multimodal/reasoning content, pop-count accuracy, recovery handler (rollback targeting, retry limits, hint injection), execution-engine integration (cooldown suppression, sub-agent via manager_ops), parametrized pattern sweeps, auto-rollback limits.
- **Why useful:** The recovery-handler and engine-integration tests exercise real behavior; pattern-length parametrization catches real detector regressions. `test_e1-e9` edge cases (None content, dict messages) are genuinely useful for robustness.
- **Caveat:** Some tests assert internal fields (`detector._chars_fed`) — acceptable for determinism. `TestFeatureExtraction`/`test_e2` (all-system, empty list) are trivial — trim.

### test_inner_loop_detect.py — **REWRITE**
- **What:** 35 tests. Unit tests on `InnerLoopDetector.feed()`: char-run detection, max-chars guard, reset, return-format keys, memory-boundedness, and *performance/latency* assertions (feed latency < threshold).
- **Why partial value:** Char-run and memory-boundedness tests are real and would fail on regressions. But return-format tests (`test_return_has_required_keys`, `test_score_is_numeric`, `test_reason_is_string`) are vacuous — they'd pass with any non-None dict. Latency tests (`test_feed_latency_*`) are flaky timing assertions.
- **What to change:** Drop format-key and latency assertions; keep char-run/ngram/memory-boundedness; the real streaming detection coverage lives in `test_inner_loop_live_data.py` and `test_inner_loop_regression.py`.

### test_inner_loop_live_data.py — **KEEP**
- **What:** 14 tests. Feeds *real assistant log texts* (`LOG_DIR`, 500+ samples) through the detector with realistic chunk sizes; FP-rate thresholds (<5%, <1% default params), char-run/sentence/ngram detection on real data, long-reasoning-loop detection, parameter sensitivity.
- **Why useful:** Real-data FP-rate regression tests are exactly the non-trivial kind that catch threshold tuning regressions. Skip-guards when logs absent are sane.

### test_inner_loop_regression.py — **KEEP**
- **What:** 9 tests. Production-settings regression: char runs (a×100, /×80, _×80) fed through `loop_test_utils.feed_streaming` with realistic 20-char chunks; two-phase loop detection with calibrated suspicion/confirmation thresholds.
- **Why useful:** Directly guards against threshold-tuning breaking real loop detection. Concise, meaningful.

### test_inner_loop_fp_simulation.py — **DROP** (duplicate)
- **What:** 2 tests. FP-rate with 20-char chunks over sampled real texts.
- **Why useless:** Duplicates the FP-rate coverage in `test_inner_loop_live_data.py` (which already tests chunk-size sensitivity and FP rates); 30-sample ±6% MOE adds noise, not signal. Sample-size skip guards make it likely skipped anyway.

### test_two_phase_loop_detect.py — **KEEP** (with trimming)
- **What:** 19 tests. Two-phase suspicion/confirmation flow: feature-flag gating, short/medium-interval loops, cooldown, reset, edge cases, loop-vs-nonloop discrimination (identical tool calls = loop; scattered repetition ≠), memory boundedness.
- **Why useful:** Discrimination tests (actual loop vs. technical prose with repeated words) are meaningful — would catch a detector that over-triggers on prose. Feature-flag gating and cooldown are real behavior.
- **Caveat:** `TestTokenization` (tokenize_basic/empty/whitespace/punctuation) are trivial helper-unit tests — move or drop.

### test_loop_regression.py — **KEEP**
- **What:** 1 parametrized test (181KB loop_samples.json, ~N samples): no false positives on real recorded samples.
- **Why useful:** Real-sample FP guard; would catch over-aggressive detection.

### test_loop_chunk_sizes.py — **DROP** (duplicate)
- **What:** 1 test: same loop_samples fed at 4 chunk sizes (10/30/50/100).
- **Why useless:** The same FP samples are already covered by `test_loop_regression.py`; the chunk-size axis duplicates `test_inner_loop_live_data.py`'s chunk-sensitivity coverage. Requires an external JSONL (`samples_2026-07-07.jsonl`) that may not exist → high skip risk.

---

## 4. Async / Shell

### test_async_shell_kill.py — **KEEP**
- **What:** 23 tests. **Best of the shell files**: real-process kill tests (`ping -t` / `sleep 60` launched for real, then `kill_task`, verifying process dead via `tasklist` and heartbeats stop), plus mocked-process kill/poll/kill-race/descendant-PID tests.
- **Why useful:** Real-process kill verification would catch a kill regression that mock-only tests can't. Race-condition tests (concurrent kill, kill-while-tracking) are meaningful.

### test_async_shell_failure_scenarios.py — **REWRITE**
- **What:** 22 tests. Failure paths: kill nonexistent PID/permission-denied/already-gone, SIGKILL escalation, timeout detection in poll loop, heartbeat integrity, zombie/orphan detection, kill-process-tree error handling. **All mocked processes** (MagicMock proc_mock) — no real subprocess anywhere.
- **Why partial value:** The timeout/kill/heartbeat logic is real code, and these tests would catch logic regressions (e.g., timeout not detected). But they never verify against a real process, so a regression in *actual* subprocess handling would pass.
- **What to change:** Port the key failure scenarios (timeout-detect, kill escalation, orphan detection) to real-process variants like `test_async_shell_kill.py` does; drop the pure state-flag assertions (`test_task_completed_property`, `test_task_heartbeat_interval_default`) which test dataclass defaults.

### test_async_shell_cmd.py — **REWRITE**
- **What:** 36 tests. Heartbeat routing (async-result buffer vs enqueue), wait-command behavior, optional-justification rules, control-command validation, auto-async mode (>60s → async), edge cases. **Heavy MagicMock** — tracker.launch is mocked (`return_value=(1,12345,...)`), `_execute_sync` patched, processes are fake.
- **Why partial value:** The justification/control-command validation rules are real security-relevant behavior and these tests do verify them (would catch a rule regression). But most execution assertions are mock-call-count based (`tracker.launch.assert_called_once()`, `send_input.assert_called_once_with(...)`) — a broken real launch path passes.
- **What to change:** Keep the validation-rule tests; add real-process coverage for launch/wait/heartbeat (mirror kill file's approach); drop mock-only path assertions or convert to verifying real outcome (e.g., real `echo` command actually produces output).

### test_generator_finalization.py — **KEEP**
- **What:** 13 tests. Real `APIRouter` semaphore release on generator exception/StopIteration/nested-exception/early-close/non-generator paths (verify by making a *second* call that would block if the semaphore leaked).
- **Why useful:** The "make another call that would block otherwise" pattern is a strong, non-vacuous assertion — would catch a real semaphore leak.

### test_streaming_timeout.py — **REWRITE**
- **What:** 13 tests. `watch_stream` behavior (normal/silence-timeout/total-timeout/first-item-delay) with real generators — those 4 are **KEEP-worthy** and meaningful. The rest are: settings-constant range assertions (`60 <= STREAM_MAX_SILENCE_SECONDS <= 300` — asserts *that a number exists*, vacuous), PoolSettings hasattr checks, one classify_error check, import smoke tests, config-handler registration.
- **Why partial value:** The 4 watch_stream tests would catch real streaming-timeout regressions. The settings/import/hasattr tests prove nothing about behavior.
- **What to change:** Keep the 4 watch_stream tests; drop the constants-range/hasattr/import/config-registration tests (or move import smoke to a single startup test).

---

## 5. Agent Lifecycle

### test_agent_pool.py — **REWRITE**
- **What:** 21 tests on real `AgentPool` + real `AgentInstance`: message queues, dismissal (conversation clear, stop-flag semantics), halt/resume lifecycle, snapshots, thread-safety (concurrent enqueue/drain), instance_conversations.
- **Why partial value:** Dismissal semantics (no global stop flag) and concurrent enqueue/drain are real behavior worth testing. But `test_dismiss_fires_callbacks`/`test_dismiss_callback_error_is_caught` test the private `_fire_on_dismissed` plumbing directly (would pass if callbacks weren't wired into dismiss), and `test_instance_conversations_can_be_written` is trivial.
- **What to change:** Assert dismissal *outcomes* through the public path (e.g., terminate → thread stops → conversation cleared), not private callback helpers; keep the thread-safety and halt/resume tests.

### test_agent_orchestrator_state.py — **DROP**
- **What:** 15 tests. Nearly all are **re-implementations of dict lookups** — e.g., `test_unified_root_reads_from_instance_state` does `store = instance_state.get("Maine", {}); msgs = store.get("messages", []); assert len(msgs) == 1`. These test Python dict semantics, not system code. Token-cache tests are the only real unit tests (cache TTL, set/get, expiry) — but they're trivial (would pass unless cache completely broken).
- **Why useless:** The state-logic tests never call the actual `get_session_history`/`build_state`/`get_agent_state` functions — they inline the same `.get()` expressions and assert on them. Vacuous by construction (a bug in the real function would pass).
- **Action:** DROP the dict-logic tests entirely; optionally move the 3 token-cache tests into a real token-cache test file.

### test_dismiss_real_thread.py — **KEEP**
- **What:** 4 tests. Real thread + real cooperative-termination check: dismiss stops a real running thread within 2s bound, dismiss-before-start keeps signal, no-thread-registered signal not discarded, join-timeout bound.
- **Why useful:** Real-thread termination behavior — would catch the signal-discard bug and unbounded-join regressions. Directly validates a real fix (RC4).

### test_dismiss_termination.py — **REWRITE**
- **What:** 34 tests on `AgentInstance.terminate()`/`is_instance_terminated`/`interruptible_sleep`/`dismiss_instance` state transitions. Uses **real AgentInstance** (good) but many tests are state-transition trivia (`terminate` from RUNNING/SLEEPING/COMPLETING/IDLE/TERMINATED — 6 near-identical variants asserting `state == TERMINATED`).
- **Why partial value:** Idempotency, durable flag, streaming-response clearing, and interruptible-sleep wake are real behavior. But the transition matrix is redundant (one parametrized test would do), and `test_error_message_is_meaningful`/`test_has_instance_name_attribute` are vacuous (attribute presence).
- **What to change:** Collapse the 6 transition variants into one parametrized test; keep idempotency/durable-flag/sleep-wake/streaming-clear; drop attribute-presence assertions.

### test_instance_separation.py — **DROP**
- **What:** 24 tests, all pure unit tests of string helpers: `validate_instance_id` (regex validation), `get_instance_id` (env var), `get_instance_suffix`, `make_instance_dir` (path joins). Pure helper-function tests with zero contention, zero system behavior.
- **Why useless:** Proves only that a regex/path-join helper behaves as written; a regression in instance separation (the actual isolation logic) would not be caught. If the helpers matter, they're better covered implicitly by real instance-separation E2E tests. (Existing E2E suite covers real instance identity.)

---

## 6. Misc

### test_code_interpreter_extra_mounts.py — **REWRITE**
- **What:** 29 tests on docker-cmd construction, path mapping (JSON-serializable), mount ordering, rw/ro flags, nonexistent-path skip, work-dir resolution priority (env/config/default), watchdog type-safety, path-mapping written on docker success.
- **Why partial value:** Mount-ordering and security checks (`is_path_allowed` blocks sibling/outside paths) are real behavior worth testing. But most tests are **string/command inspection** (`docker_cmd` contains `-v ...:...`) — they'd pass if docker invocation were broken in other ways (e.g., wrong container, missing exec). The docker-run failure/success tests are mocked (`patch` on docker command) — mock-level.
- **What to change:** Keep path-security and work-dir priority tests; the docker-command-string tests are borderline — consider one real docker-smoke test (if docker available in CI) or explicitly mark them as unit-level documentation tests. Watchdog type-safety tests are genuinely useful (guards against real corruption bug).

### test_cursor_rotation_fallback_chain.py — **KEEP**
- **What:** 22 tests on real `APIRouter`: 4-tier fallback chain construction (agent-specific → caller-inherited → last-successful → default), simulated per-tier failures, instance cursor persistence/rotation/wrap, cursor reset after success, cooldown filtering/expiry, generator passthrough and error-triggered fallback.
- **Why useful:** Tests real router state machines and would catch cursor-rotation, cooldown, or tier-ordering regressions. Uses real router + real endpoint configs with functional LLM callbacks.

### test_reset_history_rewrite.py — **REWRITE**
- **What:** 16 tests on `reset_history(rewrite=True)`: marker preservation across rewrites, marker position mirroring pool tail, no-marker fallback, internal state (data_history updated, file_history_synced flag), malformed-JSON handling, full message retention across 3 and 5 compressions.
- **Why partial value:** The full-retention tests (all originals + tails preserved, no loss/duplication across compressions) are **strong** — real JSONL file + real reset_history. But `test_marker_position_mirrors_pool_tail`/`test_zero_tail_count`/`test_tail_larger_than_file_clamping` assert internal clamping arithmetic, and `test_data_history_updated`/`test_file_history_synced_flag_set` check private flags.
- **What to change:** Keep retention/lossless tests; drop or de-emphasize private-flag assertions. Note significant overlap with `test_compression_consistency.py` crash-recovery and `test_compression_no_duplication.py` reload tests — consolidate the retention coverage in one file.

### test_retry_policy.py — **KEEP** (as unit test)
- **What:** 55 tests, pure unit tests of `retry_policy`: defaults, predefined policies (default/aggressive/conservative), error classification (fatal/retryable/unknown/priority), exponential backoff growth/jitter/caps, policy-from-settings.
- **Why useful:** Error classification is a security/correctness-critical mapping — a wrongly-classified error (fatal vs retryable) causes infinite retries or premature bailout. Backoff math is deterministic and worth locking down. This is legitimate unit testing of a pure function (not vacuous — asserts exact values).
- **Caveat:** Per criteria these are "unit-level checks on helper functions," but unlike the DROP cases they assert *exact* non-trivial behavior (e.g., auth→fatal, timeout→retryable, priority order) that a regression would break. **KEEP but classify as unit-tier; consolidate with retry_baseline integration.**

### test_retry_baseline.py — **KEEP**
- **What:** 14 tests. Real `APIRouter` + MockLLM/FailingStreamLLM: retry-count verification (max_retries=2 → 3 calls), fail-then-succeed (exactly 2 calls), multi-endpoint failover (per-endpoint call counts), inner-loop exception → endpoint advance, error-type preservation, sub-agent retry budget, performance latency baselines, L1 (LLayer) retry decoupling, backoff timing.
- **Why useful:** Assertions on exact call counts and endpoint failover behavior are meaningful — would catch a retry-loop, wrong-budget, or backoff regression. Performance baselines (`elapsed < 0.5s`, `elapsed >= 0.05`) are slightly flaky but bound real behavior.

### test_async_result_handling.py (9) — **REWRITE** (not in focus list but related)
- *Not deeply reviewed* — listed for completeness. Checked briefly: async result handling with mocked tracker. Likely same mock-heavy pattern as shell tests.

---

## Summary Table

| File | Class | Tests | Key reason |
|---|---|---|---|
| test_scheduler_integration.py | **KEEP** | 30 | Real threads+pool, FIFO/reservation/stress invariants |
| test_scheduler_integration_refactored.py | **DROP** | 30 | Exact duplicate suite (same test bodies) |
| test_slot_queue.py | **KEEP** | 21 | Real race-condition tests (cancel-after-grant, leaks) |
| test_endpoint_scheduler_stress.py | **KEEP** | 20 | Peak-≤limit, resize-under-load, strict serialization |
| test_concurrency_dispatch.py | **REWRITE** | 9 | All-MagicMock; slot acquire + child runners faked |
| test_call_agent_sync_async_selection.py | **REWRITE** | 12 | Asserts branch-taken via mocks, not real behavior |
| test_rate_limiting_concurrency.py | **KEEP** | 11 | Real router+threads; sliding-window race tests |
| test_nested_agent_calls.py | **REWRITE** | 17 | Defensive-helper unit tests + some real merge logic |
| test_security_endpoint_inheritance.py | **REWRITE** | 10 | Worker mocked; asserts arg passing, not security run |
| test_compression_no_duplication.py | **KEEP** | 31 | Real pool+JSONL; dup/loss/pair-integrity stress |
| test_compression_consistency.py | **REWRITE** | 31 | Mock pool (parallel impl); port to real-pool harness |
| test_compression.py | **REWRITE** | 62 | Mock pool; many trivial helper-constant tests |
| test_compression_tool_pairs.py | **REWRITE** | 30 | Real algorithm, mock pool; merge into real harness |
| test_compression_boundary_fix.py | **REWRITE** | 37 | Pure helper tests; consolidate w/ tool_pairs |
| test_fallback_compression.py | **KEEP** | 23 | Real engine; slice algorithm, fallback cursor |
| test_loop_detection.py | **KEEP** | 61 | Real detector+engine integration; trim trivial edge cases |
| test_inner_loop_detect.py | **REWRITE** | 35 | Keep char-run/memory; drop format-key & latency asserts |
| test_inner_loop_live_data.py | **KEEP** | 14 | Real-log FP-rate regression |
| test_inner_loop_regression.py | **KEEP** | 9 | Production-settings loop detection |
| test_inner_loop_fp_simulation.py | **DROP** | 2 | Duplicates live_data FP coverage |
| test_two_phase_loop_detect.py | **KEEP** | 19 | Discrimination + cooldown; drop tokenize unit tests |
| test_loop_regression.py | **KEEP** | 1 | Real-sample FP guard |
| test_loop_chunk_sizes.py | **DROP** | 1 | Duplicates loop_regression + live_data; external file |
| test_async_shell_kill.py | **KEEP** | 23 | Real-process kill verification |
| test_async_shell_failure_scenarios.py | **REWRITE** | 22 | All-mocked processes; port key cases to real |
| test_async_shell_cmd.py | **REWRITE** | 36 | Mock launch; keep validation rules, add real exec |
| test_generator_finalization.py | **KEEP** | 13 | Semaphore-leak verified via blocking second call |
| test_streaming_timeout.py | **REWRITE** | 13 | Keep 4 watch_stream; drop constants/hasattr/import |
| test_agent_pool.py | **REWRITE** | 21 | Keep dismissal/thread-safety; drop private-callback tests |
| test_agent_orchestrator_state.py | **DROP** | 15 | Tests inline dict lookups, not system functions |
| test_dismiss_real_thread.py | **KEEP** | 4 | Real-thread termination, bounded join |
| test_dismiss_termination.py | **REWRITE** | 34 | Real instance; collapse transition matrix |
| test_instance_separation.py | **DROP** | 24 | Pure string/path helper unit tests |
| test_code_interpreter_extra_mounts.py | **REWRITE** | 29 | Keep path-security; docker-cmd string tests borderline |
| test_cursor_rotation_fallback_chain.py | **KEEP** | 22 | Real router state machine, cursor/cooldown |
| test_reset_history_rewrite.py | **REWRITE** | 16 | Keep lossless retention; drop private-flag asserts |
| test_retry_policy.py | **KEEP** | 55 | Exact-value unit tests of critical classification/backoff |
| test_retry_baseline.py | **KEEP** | 14 | Real router failover + call-count assertions |

**Totals: KEEP 15 · REWRITE 16 · DROP 6** (of the 37 audited files).

---

## Key Cross-Cutting Findings

1. **Biggest win — duplicate suites:** `test_scheduler_integration_refactored.py` is the *same 30 tests* as `test_scheduler_integration.py` (verified by AST comparison: 30/30 identical test names, bodies differ only in docstrings/formatting). Running both ≈ doubles scheduler CI time for zero coverage. **Delete one.**

2. **Mock-heavy vacuity pattern:** `test_concurrency_dispatch.py`, `test_call_agent_sync_async_selection.py`, `test_async_shell_cmd.py`, `test_async_shell_failure_scenarios.py` all follow the pattern: MagicMock pool/tracker + patched execution paths → assertions on *which mock was called*, not on *what the system did*. A regression that breaks real slot acquisition, real process launch, or real kill behavior passes all of these. These are the highest-value rewrite targets.

3. **True vacuity:** `test_agent_orchestrator_state.py` doesn't call the functions it claims to test — it re-implements the dict `.get()` expressions inline and asserts on those. This is the clearest DROP.

4. **Compression is over-covered but has one gold core + one near-duplicate:** 5 files / ~191 tests, of which only `no_duplication` (real AgentPool+JSONL, 31 tests) is the true gold core. `consistency` (31 tests) is a *parallel mock implementation* of the pool/recovery logic — valuable assertions but not the real production path; port to the real-pool harness. The other 3 files (~129 tests) are helper-level and/or mock-pool — consolidate rather than keep all five.

5. **Loop detection is well-covered with real data:** live-data FP rates, real-sample regression, and production-threshold tests are the strongest part of the suite. Only the trivial format/tokenize/latency tests and 2 duplicate files should go.

6. **`conc=-1` (unlimited) handling:** covered correctly in `test_scheduler_integration.py` (no pool created) and `test_call_agent_sync_async_selection.py` (always async). Not a vacuity issue, but note the sync/async file's unlimited-endpoint tests are branch-assertion-only (see #2).

## Suggested Next Actions

1. Delete one of the duplicate scheduler integration files (confirm which is CI-referenced first).
2. Rewrite the 4 mock-heavy concurrency/shell files around real pools/processes (mirror `test_endpoint_scheduler_stress.py` and `test_async_shell_kill.py` patterns).
3. Consolidate compression: keep `no_duplication` (real-pool gold core), port `consistency`'s valuable assertions onto the real-pool harness, and drop/merge the helper-level and mock-pool coverage in `compression.py`, `tool_pairs.py`, `boundary_fix.py`.
4. Drop `test_agent_orchestrator_state.py`, `test_instance_separation.py`, `test_inner_loop_fp_simulation.py`, `test_loop_chunk_sizes.py`.
5. Have a Reviewer agent independently verify the AST-based duplicate detection and spot-check 5–8 classification calls before executing deletions.

## Confidence
- **High** for the duplicate-suite finding (verified by AST comparison and independent reviewer).
- **High** for the mock-vacuity classifications (read the actual test bodies; reviewer confirmed).
- **High** for the compression classification after correction: `no_duplication` is the only real-pool compression file (confirmed `MockAgentPool` usage in `consistency`/`compression`/`tool_pairs`, helper-only in `boundary_fix`).
- **Moderate** for a few KEEP-vs-REWRITE boundary calls (`test_code_interpreter_extra_mounts`, `test_retry_policy`) — these are defensible either way depending on team preference for unit-tier tests.
- **Unknowns:** which scheduler file is the canonical one referenced by CI; whether `test_loop_chunk_sizes.py` data file exists; exact test runtime budget in CI (affects soak/stress KEEP decisions).
