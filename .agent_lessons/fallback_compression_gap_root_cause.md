---
tags: [compression, fallback, context-window, api-router, execution-engine]
aliases: [fallback-compression-gap, ctx-fallback-no-compress]
related: [[api_failover_bug_analysis]]
confidence: verified
---

# Root Cause: Fallback to Lower Context Window Doesn't Trigger Compression

**Todo item:** todo.md:94 "fallback to a lower context window limit API fails to properly trigger compression"

## The Gap (Summary)

When an agent's conversation exceeds the context window of the endpoint it falls back to, the system **truncates silently** (`_truncate_input_messages_roughly` in base.py) or **retries/fails over with no context reduction**, instead of triggering `CompressionAgent`. Compression is only triggered by the *pre-LLM* percentage check (`_check_and_trigger_compression`), which is evaluated **before** the endpoint is known/chosen and uses the *first endpoint's* `max_input_tokens` — not the limit of the endpoint that actually fails.

## Key File/Line Evidence

| File | Lines | Role |
|---|---|---|
| `agent_cascade/llm/base.py` | 331-374 | `max_input_tokens` guard: estimates tokens; if > limit → `_truncate_input_messages_roughly()` (SILENT truncation). Only raises `ContextWindowExceeded` when truncation fails to reduce (line 360-366). |
| `agent_cascade/execution_engine.py` | 1954-2078 | `_check_and_trigger_compression()` — the ONLY compression trigger. Uses `_get_max_tokens(instance)` = **first endpoint's** limit (api_integration.py:1195 `get_effective_max_tokens`), NOT the failing endpoint's. |
| `agent_cascade/api_router.py` | 1437-1451 | `call_with_fallback()` catches context-exceeded errors (`_is_context_exceeded_error()` line 1187-1215), advances cursor, but does NOT compress. |
| `agent_cascade/execution_engine.py` | 2943-3037 | Engine-level retry loop. ContextWindowExceeded classifies as retryable; retries with `advance_instance_endpoint` but never invokes compression. |
| `agent_cascade/compression/core.py` | 131-149 | Compressor endpoint selection uses `get_endpoint_chain('Compressor')`; takes max context window there only for overfeeding check. |
| `agent_cascade/api_integration.py` | 1180-1237 | `_resolve_max_tokens`: router limit for agent_class → static → allocated → runtime → 65000 default. |

## The Gap (Step-by-Step Flow)

1. `_pre_llm_checks` → `_check_and_trigger_compression` (execution_engine.py:2121) runs **before** LLM call, checks `usage_pct` vs `force_threshold` (95% default, settings.py:53).
2. That check used `_get_max_tokens()` → **PRE-fallback endpoint's** limit (e.g., 128k).
3. If usage is below threshold → no compression.
4. LLM call → `api_router.call_with_fallback()` (api_router.py:1233) → tries endpoint chain. A lower-context endpoint (e.g., 32k) rejects with HTTP 400 `exceed_context_size`.
5. `call_with_fallback` catches it, calls `advance_instance_endpoint()` (api_router.py:1445-1451) — cursor advances so retries skip the bad endpoint — **but nothing compresses the conversation**.
6. Engine-level `_execute_llm_call_with_retry` (execution_engine.py:2606) classifies ContextWindowExceeded, retries with backoff; never compresses.
7. If all endpoints too small → base.py truncation silently drops conversation (base.py:345) — **lost context, not compressed**.

## Additional Risk: max_input_tokens Wrong After Failover

Commit `9f5d2cf` (2026-08-06, "max_input_tokens not updating on API failover") removed `max_input_tokens` from `_generate_cfg_override` propagation so `_resolve_max_tokens` consults the API Router live. But `_check_and_trigger_compression` still calls `_resolve_max_tokens` → `get_effective_max_tokens(agent_type)` which returns **first configured endpoint's** limit (api_router.py:902-932), not the endpoint actually serving. So the percentage check can be optimistic relative to the active endpoint.

## Proposed Fix Direction

1. After `advance_instance_endpoint()` on context-exceeded (api_router.py:1445), trigger compression of the instance's conversation (via `CompressionHandler.execute_force_compression`) before retrying, OR
2. Detect the *minimum* `max_input_tokens` across the whole fallback chain and use it in `_get_max_tokens` so the pre-LLM compression trigger fires early enough, OR
3. In base.py `chat()`, on `ContextWindowExceeded`/truncation, raise a typed exception that `_execute_llm_call_with_retry` catches and converts into a compression call instead of silent truncation.

## Related
- `[[api_failover_bug_analysis]]` — retry architecture (layers 1-3), error wrapping gap
- `[[compression_boundary_analysis]]` — compression boundary/refinement logic