# Investigation: Fallback Compression Silently Disabled — A1/A2 Gate Misclassification

**Date:** 2026-08-23 · **Investigator:** researcher (fallback-compression-misclass-investigation)
**Mode:** Investigation only — no code changes made.
**Related memories:** [[fallback-compression-false-trigger-and-cursor]], [[fallback-503-misclass-and-stop-cascade]]

---

## Executive Summary

The A1/A2 gate (commit 7df3815) is **working as coded**, but its input is poisoned: every
endpoint cfg in the fallback chain has its `max_input_tokens` **inflated to the chain-head
endpoint's limit** by `APIRouter._adjust_config_for_tokens` before the gate ever runs.
When an agent whose first-priority endpoint was configured at 165,500 tokens failed over
to `LMS-27B-3.8-NVFP4-MTP` (configured 90,000, real server context 100,096), the gate
compared the payload estimate (~94k–107k) against **165500 — not 90000** — concluded
"genuine overflow disproven", classified a genuine llama.cpp `exceed_context_size_error`
as a service error, and never raised `FallbackCompressionRequired`.

**165500 was the configured limit of two other endpoints** (`Opencode`, later demoted;
`LMS-35B`, since demoted to 125000). It reached the gate via the token-allocation path,
not via any runtime re-detection. The oai.py detection path ("could not detect context
length via API") is **not** involved — it never writes `max_input_tokens` back into the
router's endpoint configs.

Secondary defect: the payload estimator undercounts by ~10k tokens (~10%) in this regime,
which also blinds the client-side pre-check in `llm/base.py`.

---

## 1. The Exact Override Path (config → runtime → gate)

### Hop 1 — Config load (correct)
`config/api_endpoints.json:301` — `LMS-27B-3.8-NVFP4-MTP`, model
`Qwen3.8-27B-NVFP4-MTP-ako`, `"max_input_tokens": 90000`. Loaded into
`APIEndpoint.max_input_tokens`; `APIEndpoint.to_llm_cfg()` copies it verbatim into the
cfg dict (`api_router_pkg/endpoints.py:108-125`, specifically line 120).

### Hop 2 — Allocation resolution (where 165500 enters)
`engine/llm_call.py:1122-1135` — before every routed LLM call, the engine resolves
`allocated_tokens`:
1. Per-instance UI override if set (llm_call.py:1123-1127), else
2. `api_router.get_effective_max_tokens(agent_type)` (llm_call.py:1131-1135).

`get_effective_max_tokens` (`api_router_pkg/router.py:352-382`) returns the
**first enabled priority endpoint's** configured `max_input_tokens`
(router.py:371-379). Production logs show that endpoint was one of the 165500-configured
endpoints:

```
logs/console.log:4354   Endpoint allocation updated for researcher: {'endpoint': 'Opencode', ... 'max_input_tokens': 165500, ...}
logs/console.log:6746   (same, again)
logs/console.log:20662  (same, researcher, 2026-08-23)
```

So `allocated_tokens = 165500` for those agents while Opencode/LMS-35B sat at priority 0.
(Both have since been demoted to 125000/125500 in api_endpoints.json — the exact incident
value now exists only in logs.)

### Hop 3 — The inflation (THE BUG)
`call_with_fallback` receives `allocated_tokens=165500` (llm_call.py:1233-1237) and builds
the chain via `get_endpoint_chain` (`router.py:431`). For **every** Tier-1 endpoint —
including the 90000-configured NVFP4 endpoint — it calls:

```python
# router.py:486 (Tier 1) and router.py:553 (Tier 4 default)
self._adjust_config_for_tokens(cfg, allocated_tokens)
```

```python
# router.py:424-429
@staticmethod
def _adjust_config_for_tokens(cfg, allocated_tokens):
    if allocated_tokens is not None:
        effective_limit = cfg.get('max_input_tokens', 0)
        if effective_limit > 0 and effective_limit < allocated_tokens:
            cfg['max_input_tokens'] = allocated_tokens   # 90000 → 165500
```

Every cfg in the returned chain now carries `max_input_tokens >= 165500`. The loop binds
it as `llm_cfg` (`router.py:970`: `for cfg_idx, llm_cfg in enumerate(chain)`).

### Hop 4 — Gate reads the poisoned value
On the llama.cpp 400 (`exceed_context_size_error`, real n_ctx=100096, n_prompt=103789),
the A1/A2 gate reads the *same inflated dict*:

```python
# router.py:1158
_cfg_limit = llm_cfg.get('max_input_tokens') or 0     # → 165500
...
_genuine_overflow = isinstance(e, ContextWindowExceeded) or (
    _has_known_limit and _estimated is not None and _estimated > _cfg_limit)   # 94003 > 165500 = False
```

→ `_genuine_overflow=False` → warning logged (`router.py:1188-1194`) → falls through as
"service error" → retries exhaust → next endpoint → **same comparison repeats on every
endpoint in the chain** (all equally inflated), confirmed in production:

```
logs/console.log:23554  [APIRouter] ... on endpoint 'Qwen3.8-27B-NVFP4-MTP-ako' but payload fits configured limit 165500 (~94003 tokens) — treating as service error ...
logs/console.log:23687  ... on endpoint 'Qwen3.8-27B' but payload fits configured limit 165500 (~94003 tokens) ...
logs/console.log:23832  ... on endpoint 'Qwen3.8-27B-Ablit' but payload fits configured limit 165500 (~94003 tokens) ...
logs/console.log:24116+ (second burst, estimate grew to ~106685 — history kept growing, still "fits")
```

`FallbackCompressionRequired` is never raised; compression never triggers; the session
burns through the whole chain per turn.

### Collateral damage — the client-side pre-check is blinded too
The same inflated value reaches `llm.chat` via the merge
(llm_call.py:1195 → `_build_merged_cfg` Layer 3, llm_call.py:1084-1091):
`llm/base.py:313` reads `generate_cfg['max_input_tokens']` (=165500) and the overflow
guard at base.py:332-354 therefore does **not** fire at ~94k/104k tokens either. Both
defensive layers read the same poisoned number.

### Ruled out: oai.py runtime detection
`agent_cascade/llm/oai.py:_detect_context_window` (lines 312-417) writes detected values
only to `self.generate_cfg['max_input_tokens']` on the LLM *instance* (oai.py:403-409),
and only when detection succeeds; the "could not detect context length via API" branch
(oai.py:412-413) writes nothing. It never touches `APIRouter.endpoints`,
`default_llm_cfg`, or the chain dicts. On detection failure the instance keeps whatever
it had (template default; DEFAULT_MAX_INPUT_TOKENS=65000 is only a last-resort floor in
`_resolve_max_tokens`, `api_integration_pkg/tokens.py:45-51`). **Not part of this bug.**
No stale per-api_base cache exists in oai.py either.

---

## 2. Where 165500 Comes From

- Not a constant, default, or rounding artifact — literal grep across source finds zero
  occurrences. It is a **user-configured endpoint limit**: `Opencode`
  (opencode.ai/zen/v1) was configured `max_input_tokens: 165500` during the incident and
  has since been edited to 125500 (api_endpoints.json:181); `LMS-35B` carried 165500 and
  was demoted to 125000 (api_endpoints.json:13, per operator).
- Propagation into the gate: first-priority endpoint limit → `allocated_tokens`
  (llm_call.py:1131-1135) → `_adjust_config_for_tokens` floors every chain cfg at
  allocated_tokens (router.py:424-429, applied at :486/:553) → `llm_cfg` at the gate
  (router.py:1158).

---

## 3. Why the Estimate Undercounts (~94k est. vs 103,789 actual)

`_estimate_payload_tokens` (router.py:679-698) sums `get_message_stats(m)['tokens']`
per message (`utils/utils.py:1114-1223`): `qwen_count(extracted_text) +
image_tokens(255/img) + CHAT_TEMPLATE_TOKEN_OVERHEAD(8/msg)` (utils.py:1201-1212;
settings.py:193-196). Identified undercount sources, ranked:

1. **Tool/function schemas are never counted.** The request sent to llama.cpp includes
   the rendered tool schema block (functions passed separately from `messages`), which
   for this framework's rich toolset is thousands of tokens. Neither the router estimator
   nor `get_message_stats` sees them. (Structural omission — largest contributor.)
2. **Chat-template overhead modeled at 8 tokens/message** vs real llama.cpp Qwen chat
   template cost (`<|im_start|>role\n … <|im_end|>\n` plus system/tool blocks) — several
   tokens more per message, and per-tool-call round trips multiply message count.
3. **Assistant `function_call` serialization mismatch:** counted as
   `f'{function_call}'` (utils.py:1163-1165) — Python-repr-ish text, not the JSON wire
   format the server re-tokenizes; argument-heavy calls diverge.
4. **Flat 255-token/image estimate** (settings.py:193-194) regardless of actual vision
   tokenization; irrelevant to this text-only incident but part of the systematic gap.
5. Tokenizer vocabulary differences (Qwen tiktoken here vs the NVFP4 GGUF's tokenizer
   on-server) add low-single-digit-% noise.

The exact split cannot be proven without a captured request dump (`log_api_post` dumps
exist under `logs/debug/api_post_*.json` when enabled); the structural omissions above
are code-verified. Note the *same* estimator guards the client-side pre-check
(base.py:339), so the ~10k blind spot applies there too.

---

## 4. Ranked Root-Cause Assessment

**(a) PRIMARY — stale/wrong runtime `max_input_tokens` override. CONFIRMED (High).**
`_adjust_config_for_tokens` (router.py:424-429, applied :486/:553) overwrites each
endpoint's true configured limit with the chain-head allocation. Intent was capacity
*filtering/weighting* (docstring router.py:458-459); effect is destroying the per-endpoint
ground truth that both the A1/A2 gate (router.py:1158) and the client-side pre-check
(base.py:313) rely on. The gate logic itself is correct **given truthful inputs** — its
own design docs assume `_cfg_limit` is the endpoint's configured limit.

**(b) SECONDARY — estimator undercount. CONFIRMED (High that it exists, Moderate on
exact magnitude split.)** ~9.8k-token gap in production; structural causes listed in §3.
Alone it would not have caused this incident (94k < 90k is false anyway — wait: 94k >
90k, so with the *correct* 90000 limit the gate would have classified these errors as
genuine overflow and compressed). It matters as margin erosion: payloads between ~82%–92%
of the real window pass both checks and die on the server.

**(c) The gate logic itself: NOT the root cause.** Verified sound: typed
`ContextWindowExceeded` trusted unconditionally (preserves pre-gate behavior);
unknown/failed estimation treated conservatively; `DEFAULT_MAX_INPUT_TOKENS` never
substituted. Its only flaw is trusting `llm_cfg` as endpoint ground truth.

---

## 5. Recommended Fix Direction (NOT implemented)

Aligned with operator direction ("check the assigned endpoint's window before sending;
let regular forced compression handle it"):

1. **Stop mutating per-endpoint limits in the chain.** Either drop the
   `_adjust_config_for_tokens` calls (router.py:486, :553) or apply allocation to a
   scratch key (e.g. `cfg['_alloc_hint']`) used only for selection ordering — never
   overwrite `max_input_tokens`, which is consumed as ground truth by router.py:1158 and
   base.py:313. *This single change restores both defensive layers.*
2. **Make pre-send checking endpoint-truthful.** The regular forced-compression guard
   (`_check_and_trigger_compression`, engine/compression_exec.py:54; invoked from
   `_pre_llm_checks`, engine/llm_call.py:109) currently sizes against
   `get_effective_max_tokens` = *first* endpoint's limit (tokens.py Step 2,
   api_integration_pkg/tokens.py:19) — so it cannot anticipate failover onto a smaller
   assigned endpoint (known gap, [[fallback-compression-false-trigger-and-cursor]] §"Still
   open"). Fix: evaluate the guard against the limit of the endpoint actually about to be
   called (post-cursor-rotation `chain[0]['max_input_tokens']`, un-inflated). Then a
   103k payload assigned to a 90k endpoint compresses *before* the send, and the
   reactive `FallbackCompressionRequired` path becomes a rare safety net (estimate-drift
   only) rather than the primary mechanism — making it safe to simplify/retire later.
3. **Estimator hardening (follow-up):** include serialized tool schemas + realistic
   per-message template overhead in the shared counter, or adopt server-reported
   `n_prompt_tokens` (available in the 400 body and usage stats) as feedback to calibrate.
4. **Do not touch the slot mechanics.** The compressor-slot deadlock concern is already
   structurally handled: all local endpoints are `concurrency_limit: 0` → one *shared*
   sequential slot (scheduler.py:62-66); both compression paths funnel through
   `invoke_compression_agent` → `_execute_compressor_and_extract_summary`, which releases
   the caller's slot before `engine.run(compressor)`
   (compression/core.py:537 passes `caller_name`;
   compression/agent_invoker.py:217-224 performs the idempotent release). Same pattern in
   security_handler.py:546-555 and tool_dispatcher.py:498-501. Any redesign must preserve
   this yield-before-spawn invariant.

### Risks / unknowns
- Removing the inflation may surface latent assumptions elsewhere (grep shows
  `_adjust_config_for_tokens` used only in router.py; callers of chain cfgs expect int
  limits — preserved by fix option "scratch key").
- Exact estimator error budget per workload remains unquantified without request dumps.
- The demotion of the 165500 endpoints masks the bug today; it will resurface whenever
  any high-limit endpoint holds priority 0 for an agent that fails over to a smaller one.

## Confidence
Root cause (a): **High** — full code path traced hop-by-hop with matching production log
lines (console.log:23554-24395 vs router.py:1158/424-429/486/553 and llm_call.py:1131-1237).
Undercount existence: High; decomposition: Moderate. Gate-logic innocence: High.
