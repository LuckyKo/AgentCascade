# Investigation Report: Fallback-Compression Misclassification on 503 (BUG A) + Stop-Cascade / L1 Guard Failure (BUG B)

**Date:** 2026-08-21
**Investigator:** compression-stop-investigator (researcher)
**Incident:** 2026-08-21 07:04:21 – 07:05:25 (raw log embedded in `todo.md:133-500`)
**Status:** Both root causes CONFIRMED against live code + incident log. One prior hypothesis REFUTED (documented below).

---

## Executive Summary

Two bugs, one cascade, one external trigger:

- **External trigger:** A concurrent test (`python confirm_reasoning_ab.py A`, todo.md:177) drove the shared LM Studio at `127.0.0.1:1234`, evicting/swapping models mid-flight.
- **BUG A:** Endpoint `Qwen3.8-27B` returned 503 "model failed to load" twice (correctly retried, NOT treated as context-exceeded). On attempt 3 the server — now hosting a *different* model with `n_ctx=16384` — returned HTTP 400 `exceed_context_size_error` for a 40,571-token payload. Router classified it as context-exceeded and raised `FallbackCompressionRequired` → 5 compression rounds fired, even though the payload fit the endpoint's *configured* 90,000-token limit and every other endpoint in Maine's chain. **The hypothesized `DEFAULT_MAX_INPUT_TOKENS` substitution did NOT occur** — but the misclassification is still a real bug: the router compresses the caller's history based on a server-side 400 without checking the payload against the endpoint's configured limit, so a transient wrong-model condition masquerades as "history too large."
- **BUG B:** User pressed Stop at 07:05:05 (Maine + Compressor_1 → IDLE, background services down, todo.md:362-367). The fallback-compression round loop (`llm_call.py:588`) has **no stop-check**, so it spawned Compressor_2…5 *after* the stop. Each was abandoned mid-run on empty output without closing its `run()` generator, leaving state RUNNING; the immediate retry re-entered `run()` and tripped the L1 race guard (`core.py:402-405`). The consumer loop at `core.py:2591-2595` likewise `break`s without `gen.close()`.

---

## BUG A — 503 → False Context-Exceeded → Compression

### Q1: The DEFAULT constant(s) — locations, values, usage

| Constant | Value | Defined | Used as fallback at |
|---|---|---|---|
| `DEFAULT_MAX_INPUT_TOKENS` | **65000** (env `QWEN_AGENT_DEFAULT_MAX_INPUT_TOKENS`) | `agent_cascade/settings.py:22-23` | `llm/base.py:313` — client-side pre-check limit when merged cfg lacks the key |
| `DEFAULT_MAX_INPUT_TOKENS` (local re-import) | 65000 | `api_integration_pkg/tokens.py:49-51` | `_resolve_max_tokens` final fallback (`tokens.py:110`); local copy only if settings import fails |
| `DEFAULT_MAX_INPUT_TOKENS` | 58000 | `test_settings.py:20-21` | Test-only drift, not shipped |
| `FALLBACK_COMPRESSION_MAX_ROUNDS` | 5 | `engine/compression_exec.py:41` | Bounds the fallback-compression loop (`llm_call.py:588`) |
| `FALLBACK_COMPRESSION_MIN_SLICE_FRACTION` | 0.05 | `engine/compression_exec.py:43` | Slice lower bound |

Where the default can influence a context-exceeded decision:

1. **Client-side pre-check** — `llm/base.py:313`: `max_input_tokens = generate_cfg.get('max_input_tokens', DEFAULT_MAX_INPUT_TOKENS)`; `base.py:332` gates on `> 0`; `base.py:348-354` raises `ContextWindowExceeded` when estimate exceeds the limit. The merged cfg is built by `_build_merged_cfg` (`engine/llm_call.py:1037-1071`): Layer 3 = endpoint cfg via `to_llm_cfg()` (`api_router_pkg/endpoints.py:108-125`), which **always includes `max_input_tokens`** (e.g. 90000 for LMS-27B-3.8-MTP, `config/api_endpoints.json:37`). The only cfg that can lack the key is the **Tier-4 default appended as a raw deepcopy at `router.py:510-511`** (not passed through `to_llm_cfg`). If General Settings omits `max_input_tokens`, that cfg reaches base.py:313 keyless → silently capped at 65000. **This is the only real default-substitution path, and it did NOT fire in this incident** (the failing endpoint was a named endpoint with an explicit 90000).
2. **Compression threshold resolution** — `tokens.py:11-110` `_resolve_max_tokens` ends with `return DEFAULT_MAX_INPUT_TOKENS` (`tokens.py:110`); consumed by forced/proactive checks (`engine/compression_exec.py:96`). Pre-checks only; not part of the 503 path.

### Q2: Exact trace of the 503 path

1. **Wrap:** `llm/oai.py:613-615` catches `OpenAIError` → `raise ModelServiceError(exception=ex, code=code)` with `code='503'`. Confirmed verbatim in the incident tracebacks (todo.md:217-219, 259-261, 302-304).
2. **Classification:** `router.py:607-635` `_is_context_exceeded_error`: typed `ContextWindowExceeded` → True (:614); `code == '400'` + context patterns → True (:622-626); generic patterns → True (:629-633). A `code='503'` ModelServiceError matches **none** → **False**. Correct.
3. **Router handling of the 503s** (`router.py:798-857`): not AgentTerminatedError (:801), not context-exceeded (:811) → falls to generic handling: log + append (:841-847), retry with backoff while `attempt < max_retries` (:849-857). `max_retries` comes from the endpoint config (`router.py:687, 699-704`); LMS-27B-3.8-MTP has `max_retries: 2` (`api_endpoints.json:35`) → 3 attempts total, exactly matching log lines "attempt 1/3" (todo.md:178) and "attempt 2/3" (todo.md:263).
4. **Attempt 3 diverged:** no third 503 line exists. The traceback (todo.md:452) shows attempt 3 got `openai.BadRequestError: Error code: 400 … 'request (40571 tokens) exceeds the available context size (16384 tokens)' … 'exceed_context_size_error'` — wrapped by oai.py:615 into `ModelServiceError(code='400')` (todo.md:473-474).
5. **Misfire branch:** this time `_is_context_exceeded_error` → True → `router.py:811` enters the context branch → non-Compressor path `router.py:819-831`: `advance_instance_endpoint` (:821), log "Context window exceeded … Triggering iterative fallback compression" (:822-826, matches todo.md:348), `raise FallbackCompressionRequired` (:828-831).
6. **Compression cascade:** caught at `engine/llm_call.py:559` → round loop `for round_num in range(1, FALLBACK_COMPRESSION_MAX_ROUNDS + 1)` (`llm_call.py:588`, MAX=5 per `compression_exec.py:41`) → all 5 rounds failed (BUG B, below) → `ContextWindowExceeded` raised at `llm_call.py:873-877` → propagates through `core.py:636` (todo.md:492-499).

**Answer to the critical sub-question:** There is **NO code path** where a 503 (or any failure with unknown real limit) substitutes `DEFAULT_MAX_INPUT_TOKENS` and compares `payload > default` to set a context-exceeded flag in the router. The context-exceeded decision at `router.py:811` is made purely from the SERVER's error string/code. The client-side default at `base.py:313` never engaged because the merged cfg carried the endpoint's explicit 90000 and 40,571 < 90,000 anyway.

### Q3: Did the 503 handler fall through correctly?

**Yes — for the 503s themselves.** Two 503s were retried within the endpoint (per its `max_retries: 2`), and had attempt 3 also been a 503, the loop would have moved to the next endpoint (`router.py:859` "Exhausted retries … Moving to next") and recorded a cooldown (`router.py:861-872`). Maine's chain (config `agent_priorities.coder/orchestrator`, api_endpoints.json:340-393) had ample healthy successors: LMS-35B (125k), Agents-A1 (125k), gemma-4-31B (100k), Opencode (165.5k), Opencode2 (150k), OpenRouter (125k), GLM-5.2 (125k).

**The short-circuit into compression was caused by the attempt-3 HTTP 400** from the server now running a 16k-context model. The exact branch that misfires: **`router.py:819-831`** — it treats any server-side `exceed_context_size` 400 as "caller history too large, compress now," without validating the payload against the endpoint's **configured** `max_input_tokens` (available right there in `llm_cfg`). A transient wrong-model/eviction condition is indistinguishable, in this branch, from a genuine overflow.

### Q4: Fix direction (confirmed, refined)

Agreed with the tasking, with one precision: the elegant fall-through for 503 **already works**; what's missing is a sanity gate on the context-exceeded classification:

1. **Gate `FallbackCompressionRequired` on the configured limit** (minimal fix, `router.py:811-831`): before raising, compute `cfg_limit = llm_cfg.get('max_input_tokens', 0)` and estimate payload tokens (machinery exists: `get_message_stats` per `base.py:339`). If `estimated <= cfg_limit` (and `cfg_limit > 0`), the 400 reflects server-side state drift (model swapped/evicted), not caller overflow → treat as a service error: advance cursor and continue to the next endpoint; do NOT compress.
2. **Never compress off an unknown limit:** if `cfg_limit <= 0` (missing/0), skip the context-exceeded interpretation entirely and fall through. `DEFAULT_MAX_INPUT_TOKENS` must never stand in for an endpoint's real limit in this decision (consistent with the existing `> 0` gating at `base.py:332`).
3. **Exhaustion behavior:** if every endpoint is exhausted, raise the existing generic `RuntimeError("All API endpoints exhausted…")` (`router.py:874-877`) — never `ContextWindowExceeded` derived from a 503/service failure.
4. **Optional hardening:** on `exceed_context_size` 400, re-probe `/models` (detection machinery already exists at `oai.py:290-386` `_detect_context_window`) to detect the model swap and refresh the effective limit.
5. **Close the residual default-substitution hole:** make `get_endpoint_chain` guarantee every returned cfg carries `max_input_tokens` (inject `general_limit` or `0`, mirroring `router.py:457-459`) so the Tier-4 raw dict (`router.py:510-511`) can never trigger the silent 65000 cap at `base.py:313`.

**Verdict: BUG A is a real bug** — a 503/model-load failure must end in endpoint fall-through or a generic service error, never in compression. In this incident compression was triggered by the adjacent 400-under-wrong-model condition, which the proposed gate eliminates.

---

## BUG B — Stop-Cascade + L1 Race-Guard Failures

### Timeline (all from todo.md)

- 07:04:45 FCR raised; Round 1 starts; Compressor_1 enters `run()` (todo.md:350-355).
- 07:05:05 **User Stop**: "Stop: Transitioned Maine from RUNNING to IDLE", "Compressor_1 … to IDLE", background services shut down, slots released, generation bumped (todo.md:362-367).
- 07:05:25 Compressor_1 exits "in IDLE state" (todo.md:368 — the exit transition at `core.py:878-889` found state already IDLE, so no RUNNING→IDLE work). Empty-summary retries then re-enter `run()` twice in 4 ms; the second entry finds state RUNNING → `[BUG] … L1 race guard failed!` (todo.md:369-378).
- 07:05:25 Rounds 2–5 spawn **new** instances Compressor_2…5 *after* the stop; each repeats the empty-summary → re-entry → guard-failure pattern (todo.md:380-431).
- 07:05:25,903 "Exhausted 5 compression rounds … Raising ContextWindowExceeded" (todo.md:432); Maine exits IDLE (todo.md:500).

### Root cause 1: no stop-check in the fallback-compression round loop

`engine/llm_call.py:588` `for round_num in range(1, FALLBACK_COMPRESSION_MAX_ROUNDS + 1):` — the loop body (llm_call.py:589-866) contains overfeeding checks, slice tests, and compressor invocation, but **no check of `pool.stopped` / generation / `_is_terminal_stop`**. Grep confirms the only terminal-stop checks in this module are at `llm_call.py:499` and `:528` (streaming section), not in the FCR handler. So a Stop during round N does not prevent rounds N+1…5 from spawning fresh Compressor instances — exactly the "multiple compression spawned on stop" symptom (todo.md:133).

### Root cause 2: abandoned generators leave instances RUNNING

The PRIMARY offender in this incident is the compressor-invoker retry loop:

- `compression/agent_invoker.py:228`: `for resp in engine.run(comp_instance):` with an early `break` at :230-231 (`if agent_pool.stopped: break`) — **no `gen.close()` anywhere in the function**. Every empty-summary retry and timeout abandons a suspended generator whose `finally` (the RUNNING→IDLE transition at core.py:878-889) never runs deterministically.
- The retry loop at `agent_invoker.py:448` then calls `_execute_compressor_and_extract_summary` again (:450), which re-enters `engine.run()` (:228). Entry requires IDLE (`core.py:393-394`); finding RUNNING raises the L1 race-guard `RuntimeError` (`core.py:402-405`). The incident's log format "Compression attempt N/3 failed … retrying on same compressor instance" (todo.md:369-373) matches `agent_invoker.py:477-480` verbatim — confirming this exact loop produced the cascade.
- Secondary site: `engine/core.py:2591-2595`: `for resp in self.run(inst):` … `if self._is_terminal_stop(instance_name): break` — same break-without-close pattern for the parent-agent consumer.

**Independent state-machine bug (found during review verification):** `engine/core.py:441-442` and `:449-451` contain two early `return`s executed AFTER the RUNNING transition (:393-395) but BEFORE the `try:` at :453 — so the exit `finally` at :878-889 never runs and the instance stays RUNNING indefinitely. Any terminal stop landing in that window wedges the instance in RUNNING; a subsequent re-entry trips the L1 guard. Fix: move both guards inside the try block or explicitly transition to IDLE before returning.

The codebase already knows the correct pattern: `core.py:691-692` closes a generator in a `finally` on early break (see `.agent_lessons/fallback-compression-cursor-reset-fix-b1.md`), and `router.py:717-727` `_gen_wrapper` closes the underlying generator. The `run()` consumers in the compression paths just don't do it.

### Fix direction

1. **Stop-check per round** (minimal, `llm_call.py:588` loop head): `if self._is_terminal_stop(inst_name) or self.pool.stopped:` → log and exit the loop cleanly (do not spawn another compressor; surface a clean termination instead of `ContextWindowExceeded`). Mirror the same guard inside the empty-summary retry loop (`agent_invoker.py:448`) before each retry.
2. **Deterministic generator cleanup (PRIORITY: `agent_invoker.py:228`)**: bind the generator and close it in a `finally` on break — the proven GeneratorExit-safe pattern (core.py:691-692, router.py:717-727). This is the loop that actually caused the incident's L1-guard cascade; apply the same to the parent consumer at `core.py:2591-2595`.
3. **Fix the pre-try early returns** at `core.py:441-442` / `:449-451`: move them inside the try block so they pass through the exit finally's IDLE transition (:878-889), or explicitly transition RUNNING→IDLE before returning. Otherwise terminal stops in that window wedge instances in RUNNING regardless of fixes 1–2.
4. **Defense-in-depth (optional):** before raising the L1 race guard at `core.py:402-405`, treat "RUNNING but no live thread/generation match" as stale and reclaim — only if (2) is considered insufficient; explicit close is preferable.
5. **Hardening for `_is_context_exceeded_error` (BUG A adjacent):** the generic-pattern branch (`router.py:629-633`) matches free-text phrases on ANY status code; a 5xx whose message contains e.g. "max_tokens exceeded" would trigger the same compression cascade. Consider requiring `code == '400'` (or excluding 5xx) before trusting these patterns.

---

## Evidence Index

| Claim | Evidence |
|---|---|
| 503 wrapped as ModelServiceError(code='503') | oai.py:613-615; todo.md:217-219 |
| 503 not classified as context-exceeded | router.py:607-635 (code=='400' gate at :622) |
| Retry-within-endpoint then fall-through design | router.py:841-857, :859, cooldown :861-872 |
| max_retries=2 → "attempt N/3" | api_endpoints.json:35; router.py:687,699-704; todo.md:178,263 |
| Attempt-3 real error = 400, n_ctx=16384 | todo.md:452, 473-474 |
| Misfiring branch (cursor advance + FCR) | router.py:811, :819-831 |
| Log line "Context window exceeded for 'Maine'…" | router.py:822-826 → todo.md:348 |
| FCR consumed → 5-round loop | llm_call.py:559, :588; compression_exec.py:41 |
| Exhaustion → ContextWindowExceeded | llm_call.py:869-877; todo.md:432, 497-499 |
| DEFAULT_MAX_INPUT_TOKENS=65000 canonical | settings.py:22-23 |
| Client-side pre-check + default | base.py:313, :332, :348-354 |
| Endpoint cfg always carries limit | endpoints.py:108-125; api_endpoints.json:37 |
| Tier-4 raw dict (residual hole) | router.py:510-511; pool/core.py:75 |
| No stop-check in round loop | llm_call.py:588-877 (checks only at :499,:528) |
| Compressor retry loop re-entry (primary L1-guard trigger) | agent_invoker.py:228, :230-231, :448, :477-480; todo.md:369-378 |
| Pre-try early returns skip exit finally (state wedge) | core.py:441-442, :449-451 vs try at :453, finally at :878-889 |
| Stop transitions + shutdown | todo.md:362-367 |
| Post-stop compressor spawns | todo.md:380-431 (timestamps 07:05:25 > 07:05:05) |
| L1 race guard raise | core.py:391-405 |
| Exit transition lives in finally | core.py:878-889 |
| Break-without-close consumer | core.py:2591-2595 |
| Known-good close pattern | core.py:691-692; router.py:717-727 |

## Confidence

- BUG A mechanics & refutation of default-substitution: **Confirmed** (code + incident traceback).
- BUG A "real bug" verdict (should fall through, not compress): **Confirmed** as behavior defect; the precise minimal fix (configured-limit gate) is **High Confidence**, pending implementer validation.
- BUG B root causes: **Confirmed** (log timeline + code); fix direction **High Confidence**.

## Open Questions / Unknowns

- Which model LM Studio actually served at attempt 3 (16k ctx implies a small model, e.g. qwen3-vl-4b @32000 trimmed, or a test-specific load) — server-side logs needed; immaterial to the fix.
- Residual: proactive/forced thresholds use the FIRST endpoint's limit only ([[fallback-compression-false-trigger-and-cursor]] Finding 3) — separate enhancement, unchanged by this report.

## Review Verification Record

Independent reviewer verification (2026-08-21): **PASS** on all 5 claim groups. Two required amendments were made to this report post-review:
1. BUG B fix priority re-aimed at `agent_invoker.py:228` retry loop (the actual re-entry source), with `core.py:2591` demoted to secondary site.
2. Added newly-discovered state-machine bug: pre-try early returns at `core.py:441-451` skip the exit finally → instance wedged RUNNING.
Also added optional hardening note for the free-text patterns in `_is_context_exceeded_error` (`router.py:629-633`).

## Suggested Next Actions

1. Implement BUG A gate at router.py:811-831 (+ chain-wide `max_input_tokens` injection at :510-511 path).
2. Implement BUG B stop-check at llm_call.py:588 + generator-close wrappers (**priority: agent_invoker.py:228**; also core.py:2591-2595) + fix pre-try early returns (core.py:441-451).
3. Regression tests: (a) 400-exceed_context_size with payload < configured limit → falls through, no FCR; (b) stop mid-fallback-compression → zero post-stop compressor spawns; (c) consumer break → generator closed → state IDLE before any re-entry; (d) terminal stop during run() pre-try window → instance ends IDLE, not RUNNING.
