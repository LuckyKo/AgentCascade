# Investigation: Issue #150 — "Server goes crazy when it tries to view_image tool" (502 "Model server unreachable")

**Date:** 2026-08-31 · **Mode:** Investigative (root-cause) · **Investigator:** view_image_research
**Scope:** `N:\work\WD\AgentCascade` (client) + `N:\work\stuff\Beta\llama-autoloader` (proxy, port 1234). No code modified.

---

## Executive Summary

The `502 {'detail': 'Model server unreachable'}` is **not produced by llama.cpp and is not a payload-size error**. It is emitted by the **`llama-autoloader` FastAPI proxy** (the service actually bound to `127.0.0.1:1234`) when its `httpx` forward to the per-model `llama-server` backend raises `httpx.ConnectError` — i.e. **the backend process was not accepting connections at that moment** while the proxy still believed it was "ready".

`view_image` *does* contribute to the incident by putting a base64 image into the conversation, which (a) inflates the prompt and (b) is **re-encoded and re-sent on every LLM call and every retry**. A large image combined with an already-large context (~71k tokens) can push the backend toward OOM/timeout and make it crash; once a backend dies, every subsequent `llama-server` request (not just image ones) returns 502 — this is the "goes crazy" effect.

**Most likely root cause (inference, high confidence on mechanism):** a large multimodal request stresses the `llama-server` (27B + vision encoder, `--parallel 1`, single slot) until it dies or stalls; the proxy's stale `ready` flag then turns every retry into a `ConnectError`→502, and AgentCascade's retry logic resends the *same oversized image payload* 3×, keeping the server "crazy".

---

## A. `view_image` tool implementation & return value

- **Definition:** `agent_cascade/tools/custom/file_ops.py`, class `ViewImage` (line 403).
- **Return:** a **list** `[ContentItem(image=...), ContentItem(text=caption)]` — not a plain string. The `image` field is a **local file path** (or a base64 data URI in the fallback path), **not** raw bytes and **not** a data URL in the primary path.
- **Image encoding paths:**
  - Primary: `utils/media_utils.py` `save_image_to_media()` (line 78) resizes the **short side to ≤ 1080 px** (`max_short_side=1080`, LANCZOS, line 125–133), saves JPEG at **quality 0.85** (line 82), caps file size at **10 MB** (`max_file_size_mb=10.0`, line 83), and returns an **absolute file path** (e.g. `N:/.../logs/media/images/img_*.jpg`).
  - Fallback: `utils/utils.py` `encode_image_as_base64()` (line ~1057) → `data:image/jpeg;base64,...` data URI (used only if media storage fails).
- **How large can it be?** Worst case ≈ **10 MB JPEG → ~13.3 MB base64 data URI** (base64 ≈ ×1.33). Typical resized 1080px JPEG is ~100–600 KB → ~130–800 KB base64.

## B. How the image payload enters LLM messages (and persistence)

- **History persistence:** `compression/handler.py` `_assemble_tool_result()` (line 438; multimodal branch line 478–518). For **vision-capable agents** (`model_type`/`model_service_type` in `VISION_MODEL_TYPES`, e.g. `qwenvl_oai`), the `ContentItem` list is **preserved intact** (line 488–505) and stored in `Message.content`. The code comment (line 462–464) explicitly states the list "survives into Message.content and gets converted to base64 by qwenvl_oai.py."
  - **Implication:** the image is **NOT transient** — it remains in conversation history and is re-serialized on **every subsequent LLM call** (every turn and every retry), until compression strips it.
- **Transport-time conversion:** `llm/qwenvl_oai.py` `convert_messages_to_dicts()` (line ~140–144) → `conv_multimodel_value()`. When a value is a local file path it calls `encode_image_as_base64(v, max_short_side_length=1080)` and embeds it into the OpenAI-style `image_url` content part. So the file path stored in history becomes an inline base64 `data:` URL at send time.
- **Endpoint is vision-capable:** `config/api_endpoints.json` line 30–34 — endpoint "LMS-27B-3.8-MTP", `model: "Qwen3.8-27B"`, `model_type: "qwenvl_oai"`, `api_base: http://127.0.0.1:1234/v1`. This confirms the base64 image flow is exercised for this endpoint.

## C. Size caps / downscaling / token guards

- **Downscaling:** `save_image_to_media()` caps short side at 1080 px + JPEG 0.85 + 10 MB cap (media_utils.py line 78–156). This is the *only* real size control on the image itself.
- **Image-count cap:** `max_images_for_llm` defaults to **2** (`constants.py:178`), enforced by `llm/base.py` `_strip_base64_from_images()` (line ~513+). **However** this strip happens *after* base64 is already embedded into the conversation, and only limits the *number* of images — it does not cap the *byte size* of a single image.
- **Data-URL size guard:** `utils/utils.py` `MAX_DATA_URL_SIZE = 50 MB` (line 49) — a very loose ceiling, far above the 10 MB image cap; effectively non-binding here.
- **Token guard (context):** `llm/base.py` (line 343–362) estimates total tokens and raises `ContextWindowExceeded` if `estimated_tokens > max_input_tokens`. The log shows `ALL tokens: 71263, Available tokens: 120000` — **under** the limit, so no guard tripped and the oversized request was allowed to proceed.
- **Image token counting is heavily under-estimated:** `utils/utils.py` `get_message_stats()` (line 1342–1351) replaces each image with `[Image: ...]` and adds a **flat `IMAGE_TOKEN_ESTIMATE = 255` tokens per image** (`settings.py:261–262`). A 1080px image actually consumes **thousands** of vision tokens at the model (llama.cpp `--image-min-tokens 1024`+; typically 1k–3k+ per 1080px image). So the client's 71k "token" figure **massively undercounts** the true multimodal prompt size — there is **no guard** that reflects real image token cost. **This is the key gap.**

## D. The 502 "Model server unreachable" — exact source & mechanism

- **Port 1234 is the `llama-autoloader` proxy**, not raw llama.cpp. `N:\work\stuff\Beta\llama-autoloader\server.py`:
  - `proxy()` (line 1012) resolves the model, and **only forwards if `lm is not None and lm.ready`** (line 1025–1031); otherwise it JIT-loads (503 on load failure).
  - Forwards to `http://127.0.0.1:{lm.port}` (line 1035).
  - Streaming path: `try: ... except httpx.ConnectError: raise HTTPException(status_code=502, detail="Model server unreachable")` (**line 1086–1087**). Non-streaming path: `except httpx.ConnectError: ... 502 "Model server unreachable"` (**line 1098–1099**). Read errors/timeout → separate 502 "Model server read error" (line 1096–1097).
  - The proxy's forward client is `httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))` (**line 290**). A `ConnectError` = the TCP connect to the backend failed within 10 s.
- **Interpretation (CONFIRMED by code):** a `ConnectError` means the **backend `llama-server` process was not listening** (crashed / OOM-killed / evicted) while the proxy's `lm.ready` flag was **stale** (still true). The proxy therefore returns 502 even though the *proxy* itself is up. This is a **backend liveness** problem, not a request-size rejection.
- **What makes a large image request kill/stall the backend:** `llama-server` is launched with `--parallel 1 --jinja` and a single slot (config.yaml line 10; observed argv line in log `--parallel 1 ... --image-min-tokens 1024`). A 27B model + vision encoder processing a huge prompt (71k text tokens + a 1080px image worth of vision tokens) consumes large KV/vision memory. If total prompt memory exceeds what the slot can hold, the backend can **crash (OOM) or stall**. Log evidence shows the backend processing very large prompts (e.g. `prompt eval ... 54427 tokens`, `n_tokens = 57205` at 06:40; `n_tokens` climbing to ~50k by 06:45–06:46) — consistent with a memory-heavy workload that can tip into OOM.
- **Client retry behavior:** `agent_cascade/retry_policy.py` — 502 is in `retryable_patterns` (line 113), so `classify_error()` → `'retryable'` (line 108–133). `RetryPolicy` default: `retry_max_attempts=3, base_delay=1.0, max_delay=8.0, endpoint_max_retries=1` (line 29–33). `engine/llm_call.py` (line 1180–1199) retries with exponential backoff, **resending the same messages (including the same base64 image) each time**. The log's "attempt 1/3, 2/3, 3/3" matches this.

## E. "Goes crazy" — retry / re-encoding amplification

- **Amplification is real but bounded:** each of the 3 engine retries re-runs `convert_messages_to_dicts()` → **re-encodes the image to base64** (CPU + memory churn) and re-sends an oversized request to a backend that is already dead/overloaded. No unbounded infinite loop was found; the loop is capped at 3 attempts. The "crazy" perception comes from: (1) repeated 502s, (2) repeated heavy re-encoding, (3) the backend being stuck/crashed so *all* model calls fail, not just image ones.
- **No evidence** of a tool-level tight retry loop in `tool_dispatcher.py` for `view_image` specifically; the retry is at the LLM-call layer (llm_call.py), not the tool layer.

## F. Token accounting of images

- Client counts an image as a **flat 255 tokens** (`settings.py:261`) regardless of resolution/byte size (utils.py:1342–1351). Real multimodal token cost for a 1080px image is an order of magnitude higher. Consequence: the 71k-token guard (base.py:356) never triggers for image-heavy prompts, so the client happily ships a request the backend cannot handle. **This undercounting is the principal reason no pre-send guard protects the backend.**

---

## Root-Cause Assessment

**Confirmed (code + log):**
1. `view_image` returns a file path; the vision endpoint base64-inlines it at send time and **persists it in history**, re-sending it every call/retry (file_ops.py:403, media_utils.py:78, handler.py:438, qwenvl_oai.py:~140).
2. The 502 "Model server unreachable" is emitted by the `llama-autoloader` proxy on `httpx.ConnectError` to the backend (server.py:1086–1087, 1098–1099) — i.e. the backend `llama-server` was down while the proxy thought it was `ready` (stale flag).
3. AgentCascade treats 502 as retryable and resends the same oversized payload 3× (retry_policy.py:113, llm_call.py:1180).

**Inference (high confidence on mechanism, medium on the exact death trigger):**
4. A large image on top of a ~71k-token context pushes the single-slot 27B+vision `llama-server` past its memory headroom → backend OOM/crash → subsequent requests (image or not) get 502. The image is a *trigger/amplifier*, while the **stale `ready` flag** in the proxy is what converts a one-time crash into a persistent "unreachable" for all retries.

**Uncertainty / open items:**
- The exact reason the backend died at 06:55 (OOM vs. eviction vs. external kill) is not pinned to a single log line I could read; the log shows heavy large-prompt processing just prior, consistent with OOM but not conclusive. Recommend confirming against the backend's own stderr / Windows event log at 06:55.
- Whether `lm.ready` is ever reset on backend death (heartbeat) — if not, the stale-flag behavior is guaranteed.

---

## Ranked Candidate Fixes (no code changes made; impact & risk each)

| # | Fix | File(s) | Expected impact | Risk |
|---|-----|---------|-----------------|------|
| 1 | **Correct image token accounting** — replace flat 255 with an estimate derived from resolution/bytes (or add a real per-image token budget). | `settings.py:261`, `utils/utils.py:1342–1351` | Makes the existing 71k guard actually reflect multimodal size, so oversized requests are compressed/blocked *before* hitting the backend. High value, addresses the root guard gap. | Slight risk of over-estimating and triggering unnecessary compression; must calibrate against model's real `--image-min-tokens`. |
| 2 | **Strip image `ContentItem`s from history after first successful LLM use** (replace with a caption text item). | `compression/handler.py` | Eliminates repeated re-encoding + re-sending on every retry/turn; big payload reduction. | Loses the model's ability to refer back to earlier images in multi-turn; must preserve captions. |
| 3 | **Add a pre-send byte/size guard** — cap total base64 payload (e.g. > ~2–4 MB) and downscale/compress further (lower `max_short_side`, lower JPEG quality) or fall back to path-only. | `media_utils.py:78`, `qwenvl_oai.py` | Directly reduces worst-case request size; prevents OOM-prone requests. | May degrade visual fidelity for fine-detail tasks. |
| 4 | **Proxy: detect backend liveness & reset `ready` on death** (health check / process-exit monitor), so a dead backend triggers JIT-reload (503) instead of a misleading 502. | `llama-autoloader/server.py:1025–1031` | Converts persistent "unreachable" into a one-time reload; stops the retry storm from hammering a corpse. | Needs a reliable liveness signal; reload latency. |
| 5 | **Retry backoff for 502** — longer/exponential backoff and/or cap retries on repeated 502 to avoid re-sending oversized payloads at a down backend. | `retry_policy.py:29–33`, `llm_call.py:1180` | Reduces load during a backend outage. | Longer waits; does not fix the oversized-payload root cause. |
| 6 | **Cache base64 server-side / send file URLs instead of inline base64** (path-only transport). | `qwenvl_oai.py` | Eliminates repeated encoding + repeated large bodies. | Larger infrastructure change; backend must fetch the file. |

**Recommended immediate combination:** #1 (fix the accounting gap) + #3 (shrink worst-case payload) to stop the trigger, and #4 (backend liveness) to stop the "goes crazy" persistence. #2 is the strongest long-term reducer of cumulative payload.

---

## Confidence

- 502 originates from the llama-autoloader proxy on `ConnectError` (backend down, stale `ready`): **Confirmed** (server.py:1086–1099, 1025–1031).
- `view_image` → base64 inline → persisted in history → re-sent each call/retry: **Confirmed** (file_ops.py:403, media_utils.py:78, handler.py:438–518, qwenvl_oai.py:~140).
- Image token undercount (255 flat vs. real k-level cost): **Confirmed** (settings.py:261, utils.py:1342–1351).
- Large image + 71k context → backend OOM/crash as the death trigger: **Inference (high on mechanism, medium on exact trigger)** — needs backend stderr/event-log at 06:55 to fully confirm.

## Suggested Next Actions
1. Pull the backend `llama-server` stderr / Windows Application event log for 06:55 to confirm OOM vs. eviction.
2. Verify whether the proxy ever resets `lm.ready` on backend death (search server.py for heartbeat/exit handling).
3. Prototype fix #1 (accurate image token estimate) and #3 (payload size cap) and re-run a `view_image` at ~70k tokens to confirm the 502 no longer reproduces.
