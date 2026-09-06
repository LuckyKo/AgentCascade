# Issue #150 — "Server goes crazy on view_image" — Root Cause & Fix Plan

## TL;DR
The 502 "Model server unreachable" is **not** a payload-size rejection and **not** coming from llama.cpp directly. It comes from the **`llama-autoloader` FastAPI proxy** (`N:\work\stuff\Beta\llama-autoloader\server.py`). The image is the **trigger/amplifier**; the proxy's **stale `ready` flag** is what turns a one-time backend crash into a *persistent* 502 on every retry.

## Verified Root Cause (evidence, file:line)

### 1. The 502 source + the "goes crazy" mechanism — CONFIRMED
`llama-autoloader/server.py`:
- L1023–1031: proxy only JIT-reloads a model when `lm is None or not lm.ready`.
- L829: `lm.ready` is set **once** at load time (`ready = await self._wait_until_ready(...)`).
- **No code path clears `lm.ready` when the backend `llama-server` process dies mid-session.** `_status_cache_updater` (L1301) refreshes a *dashboard* status cache only; it does not re-check `proc.poll()` against `lm.ready` on the request path.
- L1086–1087 / 1098–1099: if the forward hits `httpx.ConnectError` (dead port), the proxy raises `502 "Model server unreachable"`.

**Net effect:** backend crashes once (e.g., OOM under a big vision prompt) → `ready` stays `True` (stale) → every subsequent request skips reload, connects to the dead port, and gets 502 — **persistently, on all retries**. That is the "server goes crazy" behavior.

### 2. The image is the trigger/amplifier — CONFIRMED
- `view_image` primary path returns a **file path** (≤1080px short side, JPEG q0.85, 10MB cap) — `web_ui`/`file_ops.py:676-683`; base64 data URL is only the fallback (`file_ops.py:684-698`).
- For the vision endpoint (`Qwen3.8-27B`, `model_type: qwenvl_oai`), the image path **persists in `Message.content` history** and is **re-encoded to base64 at send time on every LLM call AND every retry** — `llm/qwenvl_oai.py:139-141` (`encode_image_as_base64(v, max_short_side_length=1080)`).
- So a single screenshot is re-sent (full 1080px) on every turn and every retry of an already-stressed backend.

### 3. No effective size/token guard — CONFIRMED
- Client counts each image as a **flat `IMAGE_TOKEN_ESTIMATE = 255` tokens** regardless of resolution (`utils/utils.py:1342-1351`, constant in `settings.py:261`). A 1080px vision image really costs ~1k–3k+ tokens.
- The context guard at `llm/base.py:343-362` therefore sees "71,263 / 120,000" and **never trips**, letting the oversized multimodal prompt reach the backend.
- 502 is classified retryable (`retry_policy.py:113`) → the same oversized payload is re-sent 3×, hammering a dying backend.

## Ranked Fix Options

| # | Fix | Where | Impact | Risk | Notes |
|---|-----|-------|--------|------|-------|
| **A** | **Proxy: detect dead backend & reset `ready` → JIT-reload instead of 502** | `llama-autoloader/server.py` (request path ~L1023-1031 + a liveness check) | **ROOT CAUSE.** Stops the *persistent* 502; one crash becomes a transparent reload. | Medium (separate codebase, async, lock-sensitive) | Highest value. On `ConnectError`, mark `lm.ready=False` (and/or `proc.poll()` check) so the next request reloads. Optionally proactively liveness-check in `_status_cache_updater`. |
| **B** | **Strip images from history after first successful use** | AgentCascade (`compression/handler.py` / message lifecycle) | Removes repeated re-encoding/re-sending of stale screenshots; shrinks every subsequent prompt. | Low-Med (must not break agents that legitimately need the image later) | Big win for long tasks with many screenshots. |
| **C** | **Realistic image token accounting + pre-send size guard** | AgentCascade (`utils/utils.py`, `llm/base.py`) | Makes the context guard actually protect the backend; can downscale/reject oversized images before send. | Low (pure accounting + a guard) | Prevents the trigger in the first place. |
| **D** | **Longer/backoff 502 retry** | AgentCascade (`retry_policy.py`) | Reduces hammering a dying backend during reload window. | Low | Symptomatic; pairs with A. |

## Recommended Scope (my recommendation)
- **Fix A is the true root cause** and lives in `llama-autoloader` (a separate, read-write-allowed codebase). It's the only fix that stops the "goes crazy" persistent-502 behavior.
- **Fix C** (accurate image token accounting + a pre-send guard) is the safest in-repo hardening and directly reduces the trigger.
- **Fix B** is high-value for long tasks but needs care around agents that need images across turns.
- **Fix D** is cheap insurance.

**Suggested order:** A (root cause) → C (prevent trigger) → B (reduce repeat cost) → D (insurance). Each independently reviewable.

## Open Item
The exact backend death trigger (OOM vs. model eviction under `--parallel 1`) was not fully pinned — the backend's own stderr/event log at 06:55 wasn't readable. Fix A makes this moot for the 502 symptom, but confirming OOM would validate whether we also want a smaller default image size (part of C).
