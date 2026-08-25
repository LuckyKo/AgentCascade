# Tool-Call Loop Detection — Implementation Notes

Implements the approved design from `reports/loop_detector_research.md` (todo.md line 137).
Validated against both real failure samples in `loop_failure_samples/`.

## Per-file changes

### 1. `agent_cascade/tool_loop_detect.py` (NEW, ~390 lines)
- `detect_tool_loop(messages) -> Optional[Tuple[str, int]]` — same input contract as `detect_loop` (list of dicts or Message objects; both handled via `_as_dict`).
- **Pair extraction** (`_extract_pairs`): scans the last 40 messages; each ASSISTANT message with a function_call is paired with the next FUNCTION-role message. Intervening assistant prose / user / system messages are ignored — this defeats Sample 1's "prose shield". FCs without a following output are dropped.
- **Layer 1** (`_layer1_trailing_run`): trailing run of ≥5 same normalized-action pairs where each output is byte-identical to the previous OR both match a terminal-error signature. Normalization per report §5: shell directives (`__status`, `__wait`, `__kill`, `__ctrl_c`) → `(directive, tool_id)`; volatile arg keys (`justification`) dropped; otherwise canonical sorted-keys JSON of args.
- **Layer 2** (`_layer2_trailing_run`): trailing run of ≥6 same-tool failing pairs with pipe-stripped core-command similarity ≥0.85 (difflib `SequenceMatcher`), identical semantic target set (quoted filters ≥3 chars, `*.py` paths, `nodeid::` targets) and identical failure class (`EXIT:n` n≠0 / no-output exit / pytest `FAILED` banner). Any intervening pair from a different tool (or unclassifiable output) breaks the run.
- **pop_count convention** matches `detect_loop`: number of messages to remove from the end so that ONE occurrence of the trailing run remains (keep the first pair, drop the rest).
- Reason strings are prefixed `tool-call loop:` so callers can tell it's a tool-call loop.
- Robustness: malformed JSON args → pair skipped by Layer 1 / unclassifiable by Layer 2; missing outputs dropped; multimodal content lists handled in `_text_of`.

### 2. `agent_cascade/engine/llm_call.py` (integration)
- In `_pre_llm_checks`, inside the existing `_suppress_loop_detection_next_turn` guard:
  - `loop_info = _canonical_detect_loop(messages)` first; if None and `tool_loop_detection_enabled`, run `_detect_tool_loop(messages)` and set `loop_type = "tool"`.
  - Telemetry records `loop_type="tool"` for the new detector, `"outer"` otherwise (both paths verified by tests).
  - New `_tool_log_only` branch: while `tool_loop_rollback_enabled` is False, a tool-loop hit behaves exactly like the existing `auto_rollback_on_loop=False` branch — logs `[LOOP_DETECTED_NO_ROLLBACK]`, records telemetry with `auto_rolled_back=False`, and returns False (continues to the LLM call).
  - Everything else (auto_rollback toggle, max_auto_rollbacks enforcement, inline rollback + hint via `_inline_rollback_and_hint`) is reused unchanged.

### 3. `agent_cascade/settings.py`
- `TOOL_LOOP_DETECTION_ENABLED: bool = True` (env `QWEN_AGENT_TOOL_LOOP_DETECTION`, default `'1'`).
- `TOOL_LOOP_ROLLBACK_ENABLED: bool = False` (env `QWEN_AGENT_TOOL_LOOP_ROLLBACK`, default `'0'`) — staged rollout: log-only until a zero-FP burn-in.

### 4. `agent_cascade/agent_instance.py`
- `PoolSettings` fields `tool_loop_detection_enabled` / `tool_loop_rollback_enabled` defaulting to the settings constants (defaults consistent with settings.py).

### 5. `agent_cascade/api_integration_pkg/state_builder.py`
- Both flags serialized at both PoolSettings serialization sites (~lines 316-320 and ~498-502), following the existing `getattr(ps, ..., default)` pattern so they survive UI round-trips with defaults consistent with settings.py.

### 6. `agent_cascade/config_handlers.py`
- Registered both flags in the config-handler key list and added `_handle_tool_loop_detection_enabled` / `_handle_tool_loop_rollback_enabled` handlers (set on `agent_pool.settings`, same pattern as `auto_rollback_on_loop`).

### 7. Fixtures (NEW, under `tests/fixtures/`)
- `tool_loop_sample1_tail.jsonl` — trimmed from `async_shell_polling_loop_20260821_kv-restore-confirm.jsonl`; triggers Layer 1 (repeated `__status` polls with identical terminal error "No running shell found…").
- `tool_loop_sample2_tail.jsonl` — trimmed from `coder_impl_phase1_D_fixup_20260824_124318.jsonl`; triggers Layer 2 (near-duplicate failing pytest commands, EXIT:1).
- Both <50KB; verified by running the detector on them.

### 8. `tests/test_tool_loop_detect.py` (NEW)
- **Fixture tests**: sample1 tail → Layer 1 fires (reason mentions `__status` + "stable/terminal output", pop_count > 0); sample2 tail → Layer 2 fires (reason mentions "near-duplicate failing command" + `EXIT:1`). Pop-count convention test.
- **Legacy detector pin**: full raw sample logs through `detect_loop` still return None (skipped when raw samples absent).
- **FP battery** (parametrized): retry-until-success ×4; live progress polling with changing outputs ×10; exploratory grep/read streaks; multi-file failing survey (different targets); failing test interleaved with edit_file ×7; identical successful read_file streaks; boundary cases (4 vs 5 stable pairs, 5 vs 6 fuzzy pairs, similarity just below/above threshold).
- **Robustness**: empty list, <6 messages, malformed JSON args, missing outputs, Message objects vs dicts, multimodal content lists, system messages ignored.
- **Integration** (fake-engine pattern from `TestMaxAutoRollbacksEnforcement`): rollback invoked with expected pop_count when `tool_loop_rollback_enabled=True`; log-only behavior (no rollback, continues) when False; flag-off → no-op; telemetry `loop_type="tool"`; outer detector takes priority.

## Deviations from the research report

1. **`_is_failing_output` guard in Layer 1** (added beyond the report's sketch): a pair whose output matches none of the failure patterns (non-zero exit, terminal error, FAILED banner, traceback, leading `Error:`/`Exception:` line) is considered a *success* and can never form a Layer 1 run. Rationale: identical successful streaks (e.g., repeated identical `read_file` calls returning the same file content) are stable but not loops; without this guard they were false positives. This makes Layer 1 strictly "stable/terminal **failing** output runs", which is cleaner semantics and matches the intent of both failure samples.

2. **Layer 2 boundary test uses distinct outputs**: 5 byte-identical failing pytest pairs are caught by Layer 1 (correct — a byte-identical failing streak *is* a stable-output run). To isolate Layer 2's fuzzy matching at its threshold, the 5/6-pair boundary fixture varies per-call output text (elapsed time churn, like real sample 2) so only Layer 2 can see them. Decision: keep detector semantics clean — Layer 1 = stable/terminal outputs; Layer 2 = near-duplicate failing commands with varying outputs.

3. **Similarity "just below threshold" fixture**: verified by computing difflib ratios before finalizing. Suffixes `--filter-token={a..f}{'z' * (i*6)}` on a ~70-char base command give min pairwise similarity ≈ 0.8447 (< 0.85, no detection); the "just above" fixture (`-t{a..f}` suffixes) gives ≈ 0.9867 (fires). The earlier `i*4` variant measured 0.8878 and incorrectly fired.

## Test results

| Suite | Result |
|---|---|
| `tests/test_tool_loop_detect.py` (new) | **31 passed, 2 skipped** (skips = raw sample files not present in test env) |
| `tests/test_loop_detection.py` + `test_inner_loop_detect.py` + `test_two_phase_loop_detect.py` + `test_loop_regression.py` | **160 passed** (combined run), 0 failed, 0 skipped |

Zero regressions. Not committed.

## Reviewer attention list

- **`_is_failing_output` guard** (`tool_loop_detect.py`): confirm the failure-pattern set is not too narrow (could miss exotic failure outputs that are byte-identical across retries — those would then be invisible to Layer 1; Layer 2 only covers shell_cmd). Consider whether a repeated identical `Error: ...` line from a non-shell tool should fire.
- **Layer 1 terminal-error signatures** (`TERMINAL_ERROR_RES`): currently "No running shell found" and "'...' is not recognized as an internal or external command". These were derived from the two real samples; new terminal-error phrasings won't be caught by Layer 1's non-identical branch (they'd need byte-identity instead).
- **`_tool_log_only` interaction in `llm_call.py`**: when `auto_rollback_on_loop=False` AND `tool_loop_rollback_enabled=True`, tool-loop hits still roll back (the tool flag overrides the global toggle off). Verify this is the intended precedence — it mirrors how the report describes the staged rollout, but it's a deliberate choice worth confirming.
- **Window = last 40 messages** in `_extract_pairs`: matches the legacy detector's window; for very long conversations only the tail is scanned (by design).
- **pop_count when the run starts before the window**: if the trailing run extends past the 40-message window, `run_start` is clamped to the window start and pop_count reflects only in-window messages. Acceptable per report §5, but worth a glance.
- **State round-trip**: both new flags are serialized at both `state_builder.py` sites with `getattr(..., default)` matching settings.py defaults; verify UI round-trip once the API integration is exercised end-to-end.

---

## Refinement: output normalization (2026-08-25)

**Directive.** The detector must NOT depend on raw tool-reply text. Tool replies carry
system-injected noise (security-approval verdict banners, elapsed markers, auto-generated
justification prose, truncation notices, timestamps) that varies per call and hinders
detection. Detection identity comes from the LLM-generated `function_calls`; the FUNCTION
content is a **weak signal** used only for stability byte-identity and failure-class gating.

### What was added — `_normalize_output(content: str) -> str`

Applied at pair-extraction time in `_extract_pairs` (the stored pair output IS the
normalized text), so BOTH layers operate on normalized content only. Verified consumers:
Layer 1 byte-identity, `_fail_class`, `_is_failing_output`, `_is_generic_error_output`,
`_is_noisy_output`, `_is_terminal_output` — all read the normalized text.

Stripped (grounded in the real wrapper formats from `tools/custom/shell_cmd.py`,
`operation_manager/shell.py`, `tool_utils.py`):

| Pattern | Example | Notes |
|---|---|---|
| Security verdict banner | `APPROVED: Command exited with return code 1.` → `Command exited with return code 1.` | Only the verdict word is stripped; **the exit-code sentence survives** — this is what keeps `EXIT:n` extraction working post-normalization. Same for `AUTO-APPROVED:` / `REJECTED:`. |
| Elapsed markers, anywhere | `(elapsed 12.3s)` | Stripped mid-line too (surrounding genuine text kept). |
| Async completion lines | `Completed in 13.8 s (exit code 1).` → `Completed in (exit code 1).` | Only the duration is stripped; the exit-code parenthetical survives. |
| Security Justification blocks | `Security Justification: <auto prose>` + continuation lines | Continuation lines are consumed only while they don't start with a section marker (`STDOUT:`/`STDERR:`/`Output:`) or a pytest `FAILED ` banner — so a banner immediately after the justification is NOT swallowed. |
| Spillover/truncation notices | `[TRUNCATED — Character limit exceeded. Full output (4996 chars) saved to: <path>]` | Char count + path vary per run; whole line dropped → different paths normalize identically. |
| ISO timestamps | `2026-08-24T13:00:48.976877` | Anywhere in the output. |

Whitespace runs are collapsed, then a single post-pass restores a newline before
line-start `FAILED ` markers (a dropped block must not glue two lines and hide the
line-anchored banner regex).

### What deliberately SURVIVES normalization

- Exit-code sentences (`Command exited with return code 1.`) — failure-class identity.
- `No output produced.` — NOOUT/terminal classification.
- Pytest `FAILED tests/...::test_y` banners — TESTFAIL class + target extraction.
- Genuine error text, stdout/stderr content, differing line numbers / counts / messages.
- Anything not matching a KNOWN system-injected format (conservative by design).

### Behavior change on the sample fixtures

- **Sample 1** (`__status` polling): unchanged — Layer 1 fires, pop_count 26.
- **Sample 2** (pytest fixup churn): **now fires EARLIER via Layer 1 instead of Layer 2.**
  Pre-normalization the per-call wrapper noise made every output unique, so only Layer 2's
  fuzzy matching could chain them. Post-normalization the trailing run of 8 pairs is
  byte-identical (`Command exited with return code 1. No output produced.`), which Layer 1
  catches at its own (lower) threshold. **Trigger point (pop_count = 15) is unchanged**; only
  the layer attribution shifted. The `TestFixtures` test was updated accordingly and now also
  pins the exact pop_count.

### Tests added (`tests/test_tool_loop_detect.py`, new `TestOutputNormalization` class)

- **Regression (the user's case)**: polling loop where every error reply embeds a varying
  timestamp + elapsed marker + varying Security Justification block → Layer 1 MUST fire.
- No over-normalization: 6 identical failing polls + 1 poll with genuinely different
  substantive output AND a different exit code → no detection (both layers' chaining broken).
- Failure-class extraction on wrapped outputs: `EXIT:1`, banner-only `TESTFAIL`, and
  banner-survival-with-exit-code all verified post-normalization.
- Spillover lines with different paths/char counts normalize to identical text.
- Elapsed/timestamp stripped everywhere; genuine differences (error text, exit codes) survive.

### Test results (post-refinement)

| Suite | Result |
|---|---|
| `tests/test_tool_loop_detect.py` | **43 passed, 2 skipped** (skips = raw sample files not present in test env) |
| `tests/test_loop_detection.py` + `test_inner_loop_detect.py` + `test_two_phase_loop_detect.py` + `test_loop_regression.py` | **160 passed**, 0 failed, 0 skipped |

Zero regressions. Not committed. Protected files untouched (`loop_detection.py`,
`inner_loop_detect.py`, `two_phase_loop_detect.py`, `compression_exec.py`).

---

## Refinement Fix Round (2026-08-25, post commit 995fe0d)

Pure documentation/refactor pass addressing the quality/bloat review
(`reports/tool_loop_detect_REFINEMENT_REVIEW.md` in the AgentWorkspace).
**Zero behavior change**: `detect_tool_loop` output is byte-identical for all
inputs — the module diff contains no executable-code changes (verified by
inspecting `git diff`: only docstrings/comments touched), and the full test
matrix passes with identical counts.

### Per-finding disposition

| # | Finding | Disposition | What was done |
|---|---------|-------------|---------------|
| 1 | Verbose module docstring (BLOCKER) | **FIXED** | Trimmed 35 → ~20 lines: purpose, one line per layer, compressed FUNCTION-normalization note (the key mental model), explicit `See reports/...` pointer, and the self-containment contract ("does not modify or import from loop_detection.py") preserved verbatim. |
| 2 | Redundant comments restating code | **FIXED** | Pruned `_extract_pairs` docstring (shed parameter/return restatement + duplicate normalization narration; **kept** the CRITICAL index-semantics paragraph — it documents a previously-found critical bug) and one inline comment in its body; pruned `_normalize_output` docstring (weak-signal rationale now lives once in the module docstring). All WHY comments kept (regex `#:` blocks, pop_count warning, conservative-design notes). |
| 3 | `_as_dict` complexity | **FIXED-DIFFERENTLY** | Verified against `llm/schema.py`: `Message.model_dump()` forces `exclude_none=True` and does NOT raise for valid messages, so the try/except fallback is not dead code — it guards non-schema duck-typed message objects that lack a working `model_dump`. Kept the fallback; added a 3-line docstring naming that case instead of removing it. |
| 4 | Test fixture duplication | **FIXED** | Added shared factories in `tests/test_tool_loop_detect.py`: `failing_poll_pairs(count, user)`, `churned_pytest_pairs(count, user)`, `identical_pytest_pairs(count, output, user)`, `read_error_pairs(count, path, user)`, plus `POLL_OUTPUT` / `PYTEST_CMD` constants. Applied to 10 test methods + the integration `_tool_loop_msgs` helper. Every factory reproduces the old inline construction byte-for-byte (verified against pre-refactor code). |
| 5 | Verbose test docstrings | **FIXED** | Compressed to 1-2 line purpose statements throughout (`test_sample2_tail_fires`, `test_layer2_still_fires_on_synthetic_churn`, `TestOutputNormalization` class + methods, boundary-test NOTE → 2-line comment, etc.). Assertions and intent unchanged. |
| 6 | Overlap with `loop_detection.py` | **DECLINED-WITH-JUSTIFICATION** (code sharing) + cross-ref added | Separate algorithms (exact contiguous matching vs. normalization/trailing-run scanning), separate evolution — sharing would couple two independent detectors. Added ONE comment at the constants block noting patterns are intentionally not shared. |
| 7 | Flag naming convention | **DECLINED-WITH-JUSTIFICATION** | SCREAMING_SNAKE_CASE module constants + lowercase snake_case `PoolSettings` attributes is standard Python; changing either side would be churn with no benefit. No change. |
| 8 | Missing cross-module refs | **FIXED** (folded into #1) | The rewritten module docstring now carries an explicit `See reports/loop_detector_research.md ... and reports/tool_loop_detect_IMPL.md` line. |
| 9 | Test names (NIT) | **FIXED** | Renamed the two boundary tests to `test_layer1_fires_at_threshold_of_5_pairs` / `test_layer2_fires_at_threshold_of_6_pairs`. No external references to the old names exist (grep-verified). |

### Verification

| Suite | Result |
|---|---|
| `tests/test_tool_loop_detect.py` | **43 passed, 2 skipped** (same counts as pre-refactor; skips = raw sample files absent) |
| `tests/test_loop_detection.py` + `test_inner_loop_detect.py` + `test_two_phase_loop_detect.py` + `test_loop_regression.py` | **160 passed**, 0 failed, 0 skipped |

Combined: **203 passed, 2 skipped** — identical to the pre-refactor baseline.
Protected files untouched (`loop_detection.py`, `inner_loop_detect.py`,
`two_phase_loop_detect.py`, `compression_exec.py`). Not committed.
