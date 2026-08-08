# Investigation Report: Fallback to Lower Context Window API Fails to Trigger Compression

## Meta
- **Investigator:** researcher (investigator_context_fallback)
- **Date:** 2026-08-08
- **TODO item:** todo.md:94 — "fallback to a lower context window limit API fails to properly trigger compression"
- **Codebase:** `N:\work\WD\AgentCascade`
- **Branch state:** HEAD = 1bd3709 (2026-08-08)

---

## Executive Summary

**The gap:** When an agent's conversation exceeds the context window of the endpoint it has fallen back to (a *lower-capacity* endpoint in the router chain), the system silently truncates the input payload (`llm/base.py:331-374`) or retries/fails over (`api_router.py:1437-1451`) — it **never triggers context compression** for the affected instance.

**Why:** The only compression trigger is `_check_and_trigger_compression()` (execution_engine.py:1954), which runs in `_pre_llm_checks()` (execution_engine.py:2121) **before** the LLM call. Its usage percentage is computed against the **first endpoint's** `max_input_tokens` (via `_resolve_max_tokens` → `api_router.get_effective_max_tokens`, api_integration.py:1195 + api_router.py:902), not against the smaller limit of the endpoint that actually serves the request after failover. So a conversation that is fine for the 128k preferred endpoint silently exceeds the 32k fallback endpoint, and the failure path (context-exceeded → cursor advance → retry) never invokes compression.

---

## 1. Where context window limits are checked/enforced

| File | Lines | What it does |
|---|---|---|
| `agent_cascade/llm/base.py` | 312, 331-374 | Reads `max_input_tokens` from merged generate_cfg; if estimated tokens exceed it, invokes `_truncate_input_messages_roughly()` (silent truncation at 345-350); raises `ContextWindowExceeded` only when truncation *fails to reduce* (360-366). |
| `agent_cascade/execution_engine.py` | 3117-3121 | `_store_allocated_max_input_tokens()` caches the effective limit from merged cfg (used by compression threshold checks). |
| `agent_cascade/execution_engine.py` | 4940-4947 | `_get_max_tokens()` → delegates to `_resolve_max_tokens()` (api_integration.py:1140-1237). |
| `agent_cascade/api_integration.py` | 1185-1237 | `_resolve_max_tokens()` priority: (1) per-instance `_generate_cfg_override` → (2) API Router `get_effective_max_tokens(agent_class)` → (3) template static cfg → (4) instance `_allocated_max_input_tokens` → (5) runtime-detected → (6) `DEFAULT_MAX_INPUT_TOKENS` (65000, settings.py:21). |
| `agent_cascade/api_router.py` | 902-918 | `get_effective_max_tokens()` returns the **first enabled endpoint's** `max_input_tokens` per agent type, falling back to general settings — NOT the min/max across the fallback chain. |
| `agent_cascade/api_router.py` | 973-979 | `_adjust_config_for_tokens()` — raises cfg `max_input_tokens` to `allocated_tokens` when the configured limit is smaller (endpoint-selection time). |
| `agent_cascade/llm/oai.py` | 287-386 | `_detect_context_window()` — runtime detection from `/models` (dynamic_models); writes to `generate_cfg['max_input_tokens']`. |

## 2. Where the fallback logic lives

| File | Lines | What it does |
|---|---|---|
| `agent_cascade/api_router.py` | 981-1153 | `get_endpoint_chain()` — builds Tier 1 (agent-specific) → Tier 2 (caller inherited) → Tier 3 (last-successful) → Tier 4 (global default) ordered chain; applies per-instance cursor rotation (1104-1120). |
| `agent_cascade/api_router.py` | 1233-1496 | `call_with_fallback()` — iterates the chain; per-endpoint retries; **on context-exceeded** calls `advance_instance_endpoint()` (1443-1451) so subsequent engine-level retries skip that endpoint. |
| `agent_cascade/api_router.py` | 1186-1215 | `_is_context_exceeded_error()` — recognizes typed `ContextWindowExceeded` or HTTP 400 patterns (`exceed_context_size`, `context length`, `maximum input context`, `context window`, `prompt is too long`, etc.). |
| `agent_cascade/execution_engine.py` | 2561-2575 | `_handle_inner_loop_detection()` — advances cursor for `MaxTokenExceeded`/`ContextWindowExceeded` (added in c51ac35). |
| `agent_cascade/execution_engine.py` | 2962-3037 | Engine-level retry loop — classifies errors, backs off, retries. **Never compresses.** |

## 3. Where compression is triggered

All compression funnels through `compress_context()` (`agent_cascade/compression/core.py:23`) via `CompressionHandler` (`agent_cascade/compression/handler.py`). Activation points:

| Trigger | Location | Condition |
|---|---|---|
| Pre-LLM force | `execution_engine.py:2121` → `_check_and_trigger_compression` (1954-2078) | `usage_pct > force_threshold` (95% default, settings.py:53), computed against **first endpoint** max tokens, minus reserve tokens (settings.py:60 `compression_context_reserve_tokens`=3000). |
| Post-tool | `execution_engine.py:3867` → `_proactive_compression_check` (2201-2254) | `usage_pct > proactive_threshold` (88% default, settings.py:59). Also uses `_get_max_tokens()` → first endpoint. |
| Async-drain | `execution_engine.py:1013` → `_proactive_compression_check` | same as post-tool. |
| Agent tool | `compression/handler.py:647-686` (`handle_compress_tool`) → `compress_context()` | explicit `compress_context` tool call. |
| `/compress` cmd | `compression/handler.py` `handle_compress_command` | user command. |
| Compressor agent invocation | `agent_cascade/compression/agent_invoker.py:116-389` `invoke_compression_agent()` | creates `Compressor_N` instance, runs via `engine.run()`; uses `get_endpoint_chain('Compressor')` in core.py:136-148 to size overfeeding checks. |

## 4. How the pieces connect (or fail to connect) — the exact gap

Step-by-step for the failing scenario:

1. **Before LLM call:** `_pre_llm_checks` → `_check_and_trigger_compression` runs against the **first endpoint's** `max_input_tokens` (~128k). Conversation is at, say, 60k tokens → 47% → **no compression triggered**.
2. **LLM call:** `_execute_llm_call` (execution_engine.py:3123) → `call_with_fallback()` (api_router.py:1233) starts with endpoint A (128k) but it's unavailable/fails → falls to **endpoint B (32k)**.
3. **32k endpoint rejects:** token estimate in `base.py:343` exceeds 32k → `_truncate_input_messages_roughly()` **silently drops frames** (base.py:345) OR, if truncation can't reduce, raises `ContextWindowExceeded` (base.py:362).
4. **api_router catches**: `_is_context_exceeded_error()` → `advance_instance_endpoint()` (api_router.py:1445-1451). **Cursor advances only — nothing calls compression.**
5. **Engine retry:** `_execute_llm_call_with_retry` (execution_engine.py:2606) sees retryable error → `_handle_inner_loop_detection` (execution_engine.py:2997-2998) → cursor already advanced; retries the same-failing endpoint or next; backoff; **never invokes `CompressionHandler`/`compress_context`**. Optionally yields `[SYSTEM ERROR: LLM context window exceeded (tried N times)]` (execution_engine.py:2973-2974) and gives up.
6. **Net effect:** either silent truncation (lost context) or a failure message — never a compressed summary. Exactly what the TODO describes.

**Second-order gap (the one that makes this even worse):** `_resolve_max_tokens` (api_integration.py:1195) takes `get_effective_max_tokens()` which returns the **first endpoint's** limit. Both the pre-LLM force check and the proactive post-tool check therefore use the *optimistic* limit — meaning compression never fires early enough to avoid the failure even though the conversation would have triggered compression had the *smaller* endpoint been used from the start.

**Context of the recent fix:** commit `c51ac35` (2026-08-02) added `ContextWindowExceeded` handling + fallback for the **Compressor agent** only (see docs/compressor_fallback_plan.md). It advances the cursor and retries a different endpoint, but still no compression of the *target agent's own* conversation when a normal agent fallback occurs.

## Primary File:Line Quick-Reference

- `agent_cascade/execution_engine.py:1954-2078` — `_check_and_trigger_compression` (usage = first endpoint limit)
- `agent_cascade/execution_engine.py:2120-2123` — `_pre_llm_checks` calls it before LLM
- `agent_cascade/execution_engine.py:2201-2254` — `_proactive_compression_check` (same first-endpoint limit)
- `agent_cascade/execution_engine.py:2561-2590` — `_handle_inner_loop_detection` (advances cursor)
- `agent_cascade/execution_engine.py:2943-3066` — retry/backoff (never compresses)
- `agent_cascade/api_router.py:902-918` — `get_effective_max_tokens` (first endpoint)
- `agent_cascade/api_router.py:1186-1215` — `_is_context_exceeded_error`
- `agent_cascade/api_router.py:1443-1457` — cursor advance on context excess (no compression)
- `agent_cascade/llm/base.py:331-374` — silent truncation → `ContextWindowExceeded`
- `agent_cascade/compression/handler.py:533-643` — `execute_force_compression`
- `agent_cascade/compression/agent_invoker.py:116-389` — `invoke_compression_agent`

## Recommendations (priority order)

1. **Compress-on-fallback (best):** When `api_router.call_with_fallback()` detects `_is_context_exceeded_error()` on a non-Compressor agent, invoke `CompressionHandler.execute_force_compression()` on that instance (via a callback/engine reference) *before* advancing the cursor and retrying. This matches the intent "compress first, then try a different endpoint."
2. **Use min window across chain in proactive check:** Make `_get_max_tokens` / `_resolve_max_tokens` return the **minimum** `max_input_tokens` among the endpoints in the agent's active fallback chain (or the *current* cursor-selected endpoint), so `_check_and_trigger_compression` and `_proactive_compression_check` trigger sooner and prevent the fallback overflow from ever reaching the server.
3. **Stop silent truncation as a first defense:** In `base.py:343-366`, prefer raising `ContextWindowExceeded` (typed) rather than silently `_truncate_input_messages_roughly()` so upstream layers can react (compress), reserving truncation as last resort with logging. (Note: this partially reverts behavior of commit `558349f` — deliberate decision needed.)

## Confidence & unknowns

- **Confidence: High** — code paths verified by reading; commit history (`c51ac35`, `9f5d2cf`) corroborates; log evidence in `logs/console.log` shows `max_input_tokens` changing live on endpoint changes (execution_engine.py:3128 logs show 90000→125000→90000 across endpoint switches).
- **Unknowns:** (a) exact production trigger frequency (no explicit `exceed_context_size_error` in the searched logs — most recent logs show connection/503 failures instead); (b) whether `_force_compress_count`/cooldown logic would interact poorly with an on-fallback compression (compression_max_attempts=100, cooldown=2s defaults give headroom); (c) `base.py` truncation still handles some overflows before `api_router` sees them (truncation happens inside `llm.chat()` which is invoked through `_do_call` — errors surface at the first-chunk pull inside `execute_with_sem`).

## Suggested Next Actions

1. Implement fix 1 (compress-on-fallback) in `api_router.call_with_fallback` — needs access to the CompressionHandler/engine; the router holds `self._pool`, so it can `self._pool._execution.compression_handler.execute_force_compression(...)`, or better, raise a special exception and handle it in `_execute_llm_call_with_retry`.
2. Alternatively/also implement fix 2 in `_resolve_max_tokens` (min across chain).
3. Add regression tests: mock pool + router chain where first endpoint 128k, second 32k; assert that after a context-exceeded fallback the instance's conversation has a compression marker, and that the second call uses the compressed payload.
4. Update todo.md:94 once fixed.

**Files read (all in `N:\work\WD\AgentCascade`):** `agent_cascade/api_router.py`, `agent_cascade/execution_engine.py`, `agent_cascade/llm/base.py`, `agent_cascade/llm/oai.py`, `agent_cascade/api_integration.py`, `agent_cascade/compression/core.py`, `agent_cascade/compression/handler.py`, `agent_cascade/compression/agent_invoker.py`, `agent_cascade/settings.py`, `agent_cascade/exceptions.py`, `agent_cascade/retry_policy.py`, `docs/compressor_fallback_plan.md`, `todo.md`, `.agent_lessons/*.md` (api_failover_bug_analysis, compression_boundary_analysis, compression_flow_trace).