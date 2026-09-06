# Investigation: Context Token Estimator Undercount (todo.md line 46)

Date: 2026-08-29 · Investigator: token-research · Mode: Investigative
Companion memory: `.agent_lessons/token-estimator-current-state.md`

## Executive Summary

**The todo.md item is largely STALE.** Its #1 structural claim — "serialized tool/function
schemas are never counted" — was already fixed in commit **f73856a (2026-08-23)**, before the
todo's last update (2026-08-17) was superseded. The fix is in HEAD, verified by 5 passing tests.
The remaining undercount sources are much smaller: assistant `function_call` counted as
Python-repr (utils.py:1164-1170, 1186-1189), a flat 8-token/message template overhead that is
actually a slight *over*estimate of the real Qwen wrapper, a flat 255-token/image estimate, and
local-vs-server tokenizer mismatch. The client pre-check and the A1/A2 router gate both already
include tool schemas; the A1/A2 gate prefers server-reported `n_prompt_tokens` as ground truth.
No captured request dumps exist on disk (`logs/debug/` is empty), so the exact 94k-vs-103,789
incident split cannot be re-measured without re-capturing.

## Key Findings

### 1. Tool-schema counting: ALREADY IMPLEMENTED (contradicts todo.md)

- `estimate_functions_tokens(functions)` — `agent_cascade/utils/utils.py:1114-1135`:
  builds the exact wire format `[{'type': 'function', 'function': f} for f in functions]`
  (identical to `llm/function_calling.py:181` and `llm/base.py:624-627`) and counts
  `json.dumps(wire, ensure_ascii=False)` with the same Qwen tiktoken as `get_message_stats`.
  Fail-soft: returns 0 on any dump failure.
- Added by commit **f73856a** "fix(router): A1/A2 gate uses server-reported n_prompt_tokens as
  ground truth" (2026-08-23). `git merge-base --is-ancestor f73856a HEAD` → in HEAD. No uncommitted
  changes in `agent_cascade/` or `tests/`.

### 2. Router estimator (current code, router.py:1401-1430)

```python
@staticmethod
def _estimate_payload_tokens(messages, functions=None) -> Optional[int]:
    """Estimate total input tokens of a message list using the same estimator as the
    client-side pre-check in llm/base.py (get_message_stats per message).

    When ``functions`` (tool schemas) is provided, the serialized tools payload is
    counted too — the server tokenizes it as part of the prompt. Schema-counting
    failures are fail-soft (message-only total is still returned).

    Returns None when estimation fails — callers must treat that as "unknown" and must
    NOT make a context-exceeded decision off an unknown estimate.
    """
    if not messages:
        return 0
    try:
        from agent_cascade.utils.utils import get_message_stats, estimate_functions_tokens
        total = 0
        for m in messages:
            # Skip values that can leak via JSON parsing/logger recovery (mirrors base.py chat())
            if m is None or isinstance(m, (list, bool)):
                continue
            total += get_message_stats(m)['tokens']
        try:
            total += estimate_functions_tokens(functions)
        except Exception as fn_err:
            logger.warning(f"[APIRouter] Tool-schema token estimation failed: {fn_err}")
        return total
    except Exception as est_err:
        logger.warning(f"[APIRouter] Payload token estimation failed: {est_err}")
        return None
```

Call site (A1/A2 gate, router.py:2143): `_estimated = self._estimate_payload_tokens(messages, functions=functions)`.
`functions` is a parameter of `call_with_fallback` (router.py:1642) and the engine passes
`functions=active_functions` (llm_call.py:1443) — so schemas reach the estimator.

### 3. `get_message_stats` (utils/utils.py:1138-1247) — full behavior

- dict path with `role==ASSISTANT and function_call` (utils.py:1164-1170):
  `text = f'{function_call}'` → `qwen_count(text) + CHAT_TEMPLATE_TOKEN_OVERHEAD` (caches into `msg['_tokens']`).
- Message path with `function_call` (utils.py:1186-1189): same `f'{function_call}'` repr treatment.
- Generic path (utils.py:1201-1241): `extract_text_from_message(msg, add_upload_info=True)`,
  replaces image URLs (IMAGE_REGEX) with a placeholder and adds `IMAGE_TOKEN_ESTIMATE` (255) per image,
  then `qwen_count(text) + image_tokens + CHAT_TEMPLATE_TOKEN_OVERHEAD` (8).
- LRU cache (512 entries) keyed on role + md5(content); dict results cached back into `msg['_tokens']`.

### 4. Client-side pre-check (llm/base.py:332-362)

```python
if max_input_tokens > 0:
    agent_name = generate_cfg.pop('agent_name', 'Unknown')
    try:
        # Count tool schemas too — the server tokenizes the tools array as part of the
        # prompt. This runs BEFORE _preprocess_messages, so prompt-injected tool
        # descriptions are not yet in messages and nothing is double-counted.
        estimated_tokens = sum(get_message_stats(m)['tokens'] for m in messages) \
            + estimate_functions_tokens(functions)
    except Exception:
        logger.warning(f"[{agent_name}] Token estimation failed, skipping overflow check. ...")
        estimated_tokens = None
    ...
    if estimated_tokens is not None and estimated_tokens > max_input_tokens:
        raise ContextWindowExceeded(...)
```

### 5. A1/A2 gate (router.py:2098-2169)

- Typed `ContextWindowExceeded` (from the pre-check) trusted unconditionally.
- Server errors: `_extract_server_token_counts(e)` (router.py:1355-1396) parses
  `n_prompt_tokens` / `n_ctx` from the 400 body (structured `.body` walk + quote-tolerant regex fallback).
  If `n_prompt_tokens` present and a verified bound exists (configured limit and/or 0.95×n_ctx),
  the server count is **authoritative** — the estimator never runs (router.py:2129-2131).
- Estimator is the last resort: only when the server reports no counts AND a configured limit is known (router.py:2142-2144).

### 6. Request dumps

- Mechanism exists: `log_api_post` (oai.py:456-468, 698+) dumps `{"model", "messages", **generate_cfg}`
  (i.e. includes `tools`) to `logs/debug/api_post_<ms>.json`, toggled from the UI (`web_ui/app.js:5328`).
- **`logs/debug/` is empty** — no captured dumps exist; the exact incident split is not re-measurable.
- `reports/fallback-compression-misclass-investigation.md` §3 (lines 142-167) is from **pre-f73856a**:
  its line refs (router.py:679-698, utils.py:1114-1223) and claim #1 ("schemas never counted") are
  outdated; items 2-4 (template overhead, function_call repr, flat image estimate) still describe
  current code.

### 7. Empirical probe (temp/estimate_gap_probe.py)

- Realistic Qwen template wrapper ≈ 4-5 tokens/message (measured) vs the flat 8 assumed →
  the template term is a slight *over*estimate, not a meaningful undercount source.
- `function_call` repr vs JSON: ~1 token difference for a typical `shell_cmd` call (repr=32, json=33);
  grows with argument payload size — minor, directionally neutral for simple calls.
- Tool schemas: 1 realistic schema ≈ 193 tokens; 20 tools ≈ 3,841 tokens — now counted.
- 200-round tool-heavy session (601 msgs): estimator 20,098 vs content-only 6,220 — dominated by
  per-message overhead, confirming message accounting is the bulk of long sessions.

## What the todo claims vs. reality (todo.md:46)

| # | todo claim | Status in HEAD |
|---|-----------|----------------|
| 1 | tool schemas never counted | **FIXED** (f73856a; router + base.py + utils + 5 tests) |
| 2 | flat 8 tokens/message template overhead | Still flat 8, but measured ~4-5 tokens real → slight over-count, low impact |
| 3 | function_call as Python-repr not JSON | **STILL TRUE** (utils.py:1164-1170, 1186-1189); minor magnitude |
| 4 | flat 255 tokens/image | **STILL TRUE** (settings.py:257-258); text-only sessions unaffected |
| 5 | no server-reported calibration | Gate uses `n_prompt_tokens` when present (fixed in f73856a); no *calibration feedback* (learning a ratio over time) exists — grep "calibrat" → 0 hits |

## Assessment: what would close the remaining gap

Confidence: High (code-verified; empirical probe for magnitudes).

1. **Update todo.md line 46** — mark item 1 as done (f73856a) and narrow the item to the
   residual margin issues below. This is the most important action; the bug report as written
   would send an implementer down a path already taken.
2. **Count tool schemas in `_count_history_tokens`** (engine/core.py:3209) — the compression
   trigger currently sizes only messages, so it can fire a few thousand tokens late in
   tool-heavy sessions. Small, safe change: add `estimate_functions_tokens(active_functions)`
   (the engine already has the active toolset at the call site; llm_call.py:1443).
3. **Serialize `function_call` as JSON in `get_message_stats`** (utils.py:1164-1170, 1186-1189) —
   replace `f'{function_call}'` with `json.dumps(function_call, ensure_ascii=False)` (and count
   `reasoning_content` if present). Low risk; cache key already uses `str(fc)`, update accordingly.
4. **Optional: calibrate from server truth** — when `n_prompt_tokens` is available (success usage
   or 400 body), log the estimate-vs-actual ratio per endpoint; persist a per-endpoint correction
   factor (bounded, e.g. 0.9–1.15) applied to the estimator. This converts the remaining
   tokenizer-mismatch noise from a blind margin into a measured one. Keep `n_prompt_tokens`
   authoritative in the A1/A2 gate (already the case).
5. **Re-capture evidence**: enable `log_api_post` for one representative long session and compare
   the dumped payload (messages + tools) against llama.cpp's reported `n_prompt_tokens` to
   quantify the residual split before/after items 2-3. `logs/debug/` is currently empty.

## Risks / caveats

- The incident's ~9.8k gap is attributed to tool schemas by the (pre-fix) report; with schemas
  now counted, the *remaining* gap is expected to be small (single-digit % at most) — but this is
  **inference**, not measurement, since no request dump exists.
- `estimate_functions_tokens` counts schemas with the Qwen tiktoken; for non-Qwen endpoints the
  server tokenizer differs — same class of noise as messages, acceptable for a margin estimate.
- The A1/A2 gate is conservative: when the limit is unknown and the server reports no counts,
  context-exceeded is treated as a service error (no compression) — by design (2026-08-21 invariant).

## Open questions

- Does the operator's current toolset make item 3 (function_call JSON) worth doing at all?
  (Probe: ~1 token for typical calls; larger for argument-heavy tools like `write_file`.)
- Should `_count_history_tokens` also count reasoning_content? It currently doesn't (only the
  exception fallback at core.py:3238-3253 does) — reasoning-heavy sessions could trigger compression late.

## Suggested next actions

1. Update todo.md:46 (mark schemas-fixed; narrow scope).
2. Implement items 2-3 above (small, testable; existing `TestToolSchemaTokenAccounting` pattern
   shows the test style to follow).
3. Capture one `log_api_post` dump + `n_prompt_tokens` for a long session to validate.
