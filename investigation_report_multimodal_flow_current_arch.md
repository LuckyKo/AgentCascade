# Investigation Report: Multimodal Messaging Flow in AgentCascade (current architecture)

**Date:** 2026-08-09
**Investigator:** multimodal_researcher
**Objective:** Document current end-to-end multimodal flow (ContentItem model, view_image, user-inserted images, LLM propagation, tail exclusions, WS serialization) as groundwork for the refactor: *ContentItem carries file paths instead of base64 data, converting to base64 only right before LLM calls; user images saved to /logs/media as jpg with view_image-style captions.*

**Scope:** Documentation only — no change proposals.

---

## 1. ContentItem Model

| Item | Location |
|---|---|
| Definition | `N:\work\WD\AgentCascade\agent_cascade\llm\schema.py:84-143` |
| Class | `ContentItem(BaseModelCompatibleDict)` |

**Fields:** `text`, `image`, `file`, `audio`, `video` — **exactly one required** (enforced by `model_validator check_exclusivity`). A `caption` field may be set **post-init** (bypassing exclusivity) to carry the image description.

**Key members:**
- `type` property → returns the content-type literal (`'image'`, `'text'`, ...) via `get_type_and_value()`
- `value` property → returns the payload (e.g., base64 data URL, path, text string)
- Inherits pydantic extra='allow' — `caption` and arbitrary metadata ride along in `model_dump()`

**Image representation TODAY:**
- **Mixed, but overwhelmingly base64 data URLs** (`data:image/jpeg;base64,...` / `data:image/png;base64,...`). Plain paths are *supported* (file:// URIs / local paths) but only appear as fallback (view_image encode failure) and are subsequently converted to base64 by `qwenvl_oai.convert_messages_to_dicts()`.
- **Dicts are coerced into ContentItem** at `Message` construction: `Message(role=USER, content=[{'image': url, 'caption': cap}, ...])` → Pydantic converts each dict into a `ContentItem`. Verified via runtime test: `model_dump()` returns dicts again, and re-loading those dicts back into a Message re-coerces to ContentItems. **This is why the user-WebUI path (which produces plain dicts) works.**

`Message` (schema.py:146-179): `role`, `content` (`str | List[ContentItem]`), `reasoning_content`, `name`, `function_call`, `extra` (dict). Both inherit `BaseModelCompatibleDict`; `model_dump()` defaults `exclude_none=True`.

---

## 2. view_image Integration (end-to-end)

| Step | Location |
|---|---|
| Tool class | `agent_cascade/tools/custom/file_ops.py:401-631` (`ViewImage`, registered `'view_image'`) |
| Base64 encoder | `agent_cascade/utils/utils.py:1038-1050` (`encode_image_as_base64(path, max_short_side_length=1080)`) |
| Temp/final file | `file_ops.py:607-618` |

**Flow:**
1. **Directives:** `__screen_capture` / `__window_capture:PID` → captures screen/window, saves temp PNG via `screen_capture` module.
2. **SVG:** converted to PNG via `cairosvg`.
3. **Normalize:** open with PIL → resize short side to ≤1080px → RGB → save as JPEG → `encode_image_as_base64` returns `data:image/jpeg;base64,...`.
4. **Return:** `[ContentItem(image=<data_url>), ContentItem(text=f"Viewing image: {path}")]` — the text item acts as the image's inline caption/context.
5. On failure, falls back to a `file://` URI in the text ContentItem.

### Attachment to messages (after tool returns):
- `execution_engine.py:4068` → `tool_dispatcher.execute_tool()` → raw tool result.
- `execution_engine.py:4136-4152` — **vision gate uses `self.pool.api_router.get_llm_config(instance.agent_class)`** (per `.agent_lessons/handler_preserves_multimodal_contentitem_lists_for_vision_agents.md`; previously passed `pool.llm_cfg` missing `model_type`, breaking detection).
- `compression/handler.py:389-535` `_assemble_tool_result()`:
  - If result is a **list** AND `is_multimodal_content()` AND agent model_type ∈ `VISION_MODEL_TYPES` (`{qwenvl_oai, qwenvl_dashscope, qwenaudio_dashscope}`, defined `utils/utils.py:53-57`):
    - **PRESERVES list as-is** (char-limit truncation intentionally bypassed — binary data), calls `_caption_tool_result_images()` (handler.py:85-116), drains cache notifications/tool warnings into text ContentItems, returns list.
  - Else: **stringifies** to markdown `![image](url)` + caption (`![image](base64)` embedded inline) — used for non-vision agents and string results.
- `execution_engine.py:4190-4199`: builds `Message(role=FUNCTION, name=tool_name, content=tool_result, extra={function_id, tool_success})` → `_append_and_log` (910-927: appends to `instance.conversation` + JSONL under `_compression_lock`).

**Caption system:**
- `api_router.py:1561-1672` `APIRouter.caption_images()` — hunts uncaptioned images (`_has_uncaptioned_images`, 1521-1534), selects a vision endpoint (`_get_vision_endpoint_for_agent`, 1549-1559; uses `vision_enabled` flag), sends `CAPTION_PROMPT` + image to vision model via `get_chat_model(vision_cfg)`, truncates caption to `MAX_CAPTION_LENGTH=300` (api_router.py:73), patches `ContentItem.caption` in place. Fallback: `'[Image]'`.
- Also called at send-time: `execution_engine.py:2685-2705` (`_has_images` + `_ensure_image_captions`) before the LLM call (line 2843-2845).

---

## 3. User-inserted images (WebUI paste/upload)

### Frontend path
| Step | File |
|---|---|
| Paste handler | `web_ui/app.js:4503-4514` (clipboard `paste` → `processImageFile`) |
| Upload button + drag/drop | app.js:4427-4435 (file input), 4447+ (dragover/drop → inserts file path text or `/api/find_file`) |
| Resize + encode | `processImageFile` (app.js:4348-4383): canvas-resized to `settingMaxImageSize` (default 1024) → `canvas.toDataURL(mimeType, 0.9)` → **base64 data URL** |
| Insert into chat box | `insertImageMarkdown` (app.js:4334-4346): `![filename](data:image/png;base64,...)` |
| Preview | `IMAGE_MARKDOWN_RE` (app.js:4523) → thumbnails in `imagePreviewContainer` |
| Vision gating | `formatMultimodalContent` (app.js:4320-4328): if vision disabled (checkbox `settingVisionEnabled`), replaces base64 images with `[Image: name]`; else passes data URL through |
| Send | `sendMessage` (app.js:4851+) → WS message `{type:'message', text:'...'}` |

### Server receive path
| Step | File / Function |
|---|---|
| WS endpoint | `api_server.py:1026-1068` `/ws/chat` → `WsMessageHandler.dispatch` |
| Message handler | `ws_handlers.py:225-268` `handle_message()` — if generating → queues; else enqueues + starts gen thread |
| **Markdown → content items** | `_parse_multimodal_content(text)` (`api_server.py:109-131`) |
| Regex | `_IMAGE_DATA_RE` = `!\[([^\]]*)\]\((data:image/[^;]+;base64,[a-zA-Z0-9+/=]+)\)` from `utils/thinking_block.py:40` (also aliased `IMAGE_REGEX` in utils) |
| Output | Returns **original text** if no data-URI images; else a list of plain dicts: `[{'text': ...}, {'image': <data_url>}, {'text': ...}]` |
| Queue entry | `agent_pool.enqueue_message(instance_name, parsed_content)` (`agent_pool.py:2361-2366`) → `message_queues[inst].append()` + condition notify |

### Queue → conversation
| Step | File / Function |
|---|---|
| Early-exit drain (no working set) | `execution_engine.py:1224-1236`: `pool.drain_queue()` → `_make_user_message(item)` → `_append_and_log` |
| SLEEPING wakeup drain | `execution_engine.py:4767-4785` (`SLEEPING` guard): `drain_queue` → `_drain_and_inject(..., factory=self._make_user_message)` |
| Message factory | `execution_engine.py:1061-1063` `_make_user_message(text)` → `Message(role=USER, content=text)` (dict-list coerced to ContentItems via pydantic) |
| Queue read for UI | `agent_pool.get_queue_messages()` (2392-2403) → `[str(msg)]` |

**Key point:** user images **arrive as base64 over the WebSocket, are stored as dicts in memory, and never touch disk.** They become USER `Message.content` lists (dicts → ContentItem coercion at construction) carrying data URLs. `logs/media` **does not exist** in the current codebase (verified: no `media` dir under logs; no MEDIA_DIR constants).

**Note:** the parsed dict items lack captions (regex captures no caption — user-input images are not auto-captioned at ingest; they get captioned only at LLM send time via `_ensure_image_captions` if a vision endpoint exists).

---

## 4. Propagation to LLM (payload building / base64 embedding point)

### The single conversion point for vision
| Step | File / Function |
|---|---|
| Base chat entry | `llm/base.py:chat()` (line ~371) — `messages = self._preprocess_messages(...)` |
| Vision gate | `base.py:380` `effective_vision = self.support_multimodal_input and generate_cfg.get('vision_enabled', True)` → if `not effective_vision` → `format_as_text_message()` converts images to `[Image: caption]`/`[Image]` |
| **max_images_for_llm (only base64 cap)** | `base.py:526-530` `_preprocess_messages` → `strip_base64_from_images(messages, max_images)` (`utils/utils.py:1260-1310`): keeps only last N data-URI images; replaces extras with `ContentItem(text='[Image: caption]' or '[Image]')`. Default `MAX_IMAGES_FOR_LLM_DEFAULT=2` (constants.py:117). **This runs at API-send time, AFTER base64 is already persisted in conversation/logs.** |
| Transport: OpenAI-compatible | `oai.py:`, `TextChatAtOAI._chat_stream` (388-394) → `convert_messages_to_dicts` (764-774): base = `format_as_text_message` + `model_dump` + `_conv_agent_cascade_messages_to_oai` |
| **Vision override** | `qwenvl_oai.py:41-125` `QwenVLChatAtOAI.convert_messages_to_dicts()` — iterates ContentItems; `conv_multimodal_value()` (128-150) **converts `file://` or local paths to base64 via `encode_image_*` (max 1080)** and emits `{'type':'image_url','image_url':{'url': base64}}` (image), `video_url`, `input_audio` |
| Tool-message flatten guard | `base.py:680-748` `_conv_agent_cascade_messages_to_oai` — FUNCTION messages with multimodal items (image_url/video_url/input_audio) **pass through**; else flatten to string |
| DashScope path | `qwenvl_dashscope.py:48-68` (vision) / `qwen_dashscope.py` — Δ model_dump + `_conv_agent_cascade_messages_to_oai`; QwenVL `support_multimodal_input=True` |

**Key architecture fact:** base64 is embedded **at transport time** (in `convert_messages_to_dicts`), not when appending to conversation. The ContentItem.image can theoretically hold a path — the vision conversion is what reads the path and encodes. **This is the seam the refactor can exploit.** The conversation/JSONL/logs keep the original representation (today: already-base64).

### Where the data actually goes (post-conversion)
`_chat_complete_create` (oai.py:278+) posts `messages` dict with `image_url.url = data:...` to `{api_base}/chat/completions` via the OpenAI SDK.

### Tool result data-URL behavior caveat
OpenAI tool-message spec technically requires **string content**; qwenvl_oai passes multimodal lists through inside FUNCTION messages anyway — some endpoints may drop images in that position (documented in prior reports `investigation_report_view_image_image_embedding.md`; **FATAL#1 (handler stringify) and FATAL#2 (base.py FUNCTION flatten) are now fixed**).

---

## 5. Serializations: tail exclusion, logs, summaries, compression

| Path | Function / Behavior | Base64 carried? |
|---|---|---|
| WS state (full) | `api_integration.py:1357-1485` `serialize_message(for_ui=True)`: content list → joined string with `![image]({data_url})\nCaption: {cap}`; also 100K char truncation; strips None / `_tokens`/`_words` / `extra` (pulls `tool_success` into `tool_success`) | **YES — full base64 in WS payload** |
| WS queued messages | `agent_pool.get_queue_messages` → `[str(msg)]` | YES (str of ContentItem includes base64) |
| Instance serialization | `api_integration.py:1512-1560` `_serialize_instance` — **always sends all messages, no tail optimization** (docstring) | YES |
| Working set (LLM input) | `agent_pool.py:1895-1934` `slice_history_for_llm`: `[SYS][U0][COMP markers][tail after last marker]` — tail = post-compression messages; full history stays in instance.conversation | YES (working set passes data URLs to `_setup_turn`; stripped later only by max_images at send time) |
| JSONL persistent logs | `logger/agent_instance_logger.py:143-200` `_format_message()`: `model_dump()` (content list → dicts with base64 inside) | **YES — full base64 blobs written to JSONL** |
| **Compression prompts** | `compression/agent_invoker.py:43-113` `_format_messages_for_summary()` — flattens multimodal lists **without base64**: images → `[Image: caption]` or `[Image]` | **NO (headers only)** |
| Message stats/summaries | `utils.py:860-924` `extract_text_from_message` → `format_as_text_message` | NO |
| Sub-task propagation | `lifecycle_manager.py:287-441` `_collect_images_from_agent` + `_propagate_images_to_agent_task` + `build_task_message` — reads `pool.llm_cfg['max_images_for_llm']` (default 2), appends `{image: <base64 url>}` to task content | **YES — copies raw base64 URLs into child conversation** |

---

## 6. WebSocket messaging serialization

- **Where:** `api_integration.py:1357` `serialize_message()` (used via `build_state_from_pool` → `_serialize_instance` → each message) and `queued_messages` → `get_queue_messages`.
- **Bloat confirmed:** `serialize_message()` embeds the full base64 data URI as `![image](data:image/jpeg;base64,...)` + caption text in **every state broadcast** to the frontend. This is the primary WS payload bloat source (one or more images × every WS 'state' update).
- Related prior report: `investigation_report_ui_image_perf.md` (2026-08-09) — UI perf issues from inline base64.

---

## 7. Key Technical Notes / Gaps for Refactor Planning

1. **Dict→ContentItem coercion** in `Message.__init__` is the linchpin that makes `_parse_multimodal_content` dict-items survive to the LLM. Any refactor switching URL→path must keep the `Message` content type flexible (dicts, ContentItems, str).
2. **Only vision conversion reads paths** — `qwenvl_oai.convert_message_...` is the *only* place that turns path/file into base64; `view_image` currently encodes *eagerly* at tool time. Moving encode to transport time (already partly true for the Qwen VL path) is the seam for the refactor.
3. **max_images_for_llm is the only enforcement point of image volume**, and it operates on *representation* after base64 is already persisted to conversation & JSONL; applying it at produce-time (alongside `/logs/media` saving) is where the refactor targets.
4. **No server-side saving of user images exists today.** There is **no `/logs/media`** directory, no media-storage module, no path-handling for user images anywhere. The `view_image` caption logic (`_caption_tool_result_images` + `caption_images`) is caption-ready; only the storage/save layer is missing.
5. **JPEG normalization exists** in `encode_image_as_base64` (resizes short side ≤1080, RGB→JPEG, quality-per-PIL default). The proposed `/logs/media` jpg saving mirrors this same pipeline logic — a potential shared helper opportunity.
6. The **GPU caching** (`_preprocess_cache`) at `BaseChatModel` should be invalidated when ContentItem representation changes (cache keyed on message list). Refactor must bump cache keys.

---

## Confidence & Unknowns
- **Confirmed (by direct code read):** all file paths, class names, function names, repr behaviors above.
- **Runtime-verified:** Dict content → ContentItem coercion; caption survival; `get_type_and_value` availability.
- **Known conflict:** `.agent_lessons/non-vision-endpoint-image-stripping-broken.md` (2026-08-09) documented that `vision_enabled` was NOT checked during calls, but current `base.py:380` **does** check `generate_cfg.get('vision_enabled')` in `chat()` — the fix appears already applied (memory superseded for this point).
- **Unverified:** (a) OpenAI endpoints accept image_url inside tool messages in practice; (b) whether repeated `Messages re-coercion` (dict↔ContentItem loops through JSONL) preserves `caption` (runtime showed caption survives one direction — confirmed local test).

## Suggested Next Actions (for Maine's consideration; not proposals)
1. Refactor ContentItem to carry file path (e.g., `/logs/media/<uid>.jpg`) instead of data URL at *tool-result assembly* and *queue ingestion*.
2. Add a server-side save step at both: view_image result and `_parse_multimodal_content` (from WS), writing JPEG via the resize/encode helper shared with `encode_image`.
3. Generate captions at save time (reuse `api_router.caption_images`), store caption in ContentItem.caption.
4. Move the ONLY bytes-embedding decision into `qwenvl_oai.convert_messages_to_dicts` (already the one place that reads paths) — i.e., keep conversation in path form up to that point.
5. Strip/placeholder base64 at serialization (WS + JSONL) once paths exist; keep `max_images_for_llm` semantics but at construction time.
6. Introduce the `logs/media` directory with lifecycle/cache eviction policy (untracked images referenced only by basename in calls' recent user messages).

---
*Files analyzed: `schema.py`, `file_ops.py`, `utils.py`, `thinking_block.py`, `api_server.py`, `ws_handlers.py`, `agent_pool.py`, `execution_engine.py`, `api_integration.py`, `compression/handler.py`, `compression/agent_invoker.py`, `llm/base.py`, `llm/oai.py`, `llm/qwenvl_oai.py`, `llm/qwenvl_dashscope.py`, `api_router.py`, `lifecycle_manager.py`, `logger/agent_instance_logger.py`, `web_ui/app.js`, `constants.py`, `plans/fix_image_propagation_in_call_agent.md`, `.agent_lessons/` (6 memories).*