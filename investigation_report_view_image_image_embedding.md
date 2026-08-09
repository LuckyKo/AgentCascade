# view_image image embedding — why the vision model never receives image pixels

**Investigation:** investigator_image_embeds · 2026-08-08 · **Status:** root cause confirmed (two fatal drop points)

## Symptom
`view_image` returns success and the web UI renders the image, but the LLM describes images incorrectly. The conversation log stores the tool result as plain markdown text: `![image](file:///N:/work/WD/AgentWorkspace/No_thx.jpg)\nViewing image: No_thx.jpg` (see `orchestrator_Maine_20260808_142805.jsonl:6`). No image bytes ever reach the model.

## Code path (tool return → conversation message)
| Step | Location | Behavior |
|---|---|---|
| Return | `agent_cascade/tools/custom/file_ops.py:607-610` | `ViewImage.call()` → `[ContentItem(image=file_uri), ContentItem(text='Viewing image: ...')]` — CORRECT |
| Pass-through | `agent_cascade/agent.py:263-268` | `_call_tool()` returns ContentItem list unchanged — CORRECT |
| Dispatch | `agent_cascade/tool_dispatcher.py:180-188` | returns list unchanged — CORRECT |
| **FATAL #1** | `agent_cascade/compression/handler.py:334-353` `_assemble_tool_result()` | converts ContentItem list to markdown STRING `![image](file:///...)` before the FUNCTION message is created. Image data never enters the conversation. |
| Wrap | `agent_cascade/execution_engine.py:4172-4181` | `Message(role=FUNCTION, content=tool_result)` — content is the markdown string |
| Append | `agent_cascade/execution_engine.py:4181` + `_append_and_log` (910-927) | pool conversation + JSONL logger — model receives markdown text only |
| **FATAL #2** | `agent_cascade/llm/base.py:687-704` `_conv_agent_cascade_messages_to_oai()` | for FUNCTION/tool messages with list content, joins ONLY `.text` parts — `image_url` dicts are silently dropped. Would kill images even if the list survived. |
| UI (human only) | `web_ui/app.js:2752-2756` `renderToolResult()` | rewrites `![image](file:///...)` → `/api/file?path=...` proxy → renders for the HUMAN. Model never sees this. |

## Why the caption fallback doesn't rescue images
- `execution_engine.py:2685-2693` `_has_images()` only detects images when `msg.content` is a list.
- After handler.py stringification, FUNCTION msg content is a `str` → `_has_images()` returns False → `_ensure_image_captions()` / `api_router.caption_images()` (api_router.py:1561-1672) never run for view_image results.
- Caption pipeline can only see ContentItem lists; it never sees post-stringification text.

## Why the vision model class can't help (as wired)
- `agent_cascade/llm/qwenvl_oai.py:41-115` `QwenVLChatAtOAI.convert_messages_to_dicts()` correctly turns `ContentItem(image=file:///...)` → base64 `image_url` parts via `conv_multimodal_value()` → `encode_image_as_base64()` (utils/utils.py:960-972).
- But it only runs on messages whose content is still a ContentItem list. In the engine flow the view_image result is already a markdown string.
- And after it produces image_url parts, it calls `_conv_agent_cascade_messages_to_oai()` (qwenvl_oai.py:93) which drops image parts for FUNCTION messages (base.py:692-699).
- Note: OpenAI tool-message spec requires string `content`; images must be attached via a following user message, not inside tool messages.

## History / timeline
- Pre-14:18: handler.py did `str(raw_tool_result)` → agents saw pydantic repr `[ContentItem(text=None, image='file:///...')]` (documented in `.agent_lessons/view_image_contentitem_stringification.md`).
- 14:18-14:20 (`coder_view_image_fixer_20260808_141839.jsonl`): partial fix applied — handler.py now converts ContentItem lists to markdown `![image](...)`. This fixed the raw repr symptom and made the UI render the image, but the model STILL only receives text. The image is never embedded for the LLM.

## Fix recommendations (unapplied)
1. **Keep the list alive to the LLM layer**: in `execution_engine.py:4172-4181`, when `tool_result` is a ContentItem list, keep it as FUNCTION message content (skip markdown conversion); make handler.py return ContentItem lists unchanged (markdown conversion is a UI concern, belongs in api_integration.py).
2. **Emit image parts for FUNCTION messages in base.py:687-704**: convert `ContentItem.image` → `image_url` with base64 data URI. BUT per OpenAI tool-message spec, attach images to a FOLLOWING USER message instead (tool text stays in the tool message; image_url parts go in the next user content array) — this is the documented pattern for vision + function calling.
3. **Or use the caption pipeline as the pragmatic fix**: run `_ensure_image_captions()` on the ContentItem list BEFORE stringification (engine-level), so the model at least receives an accurate text description of the image. The vision endpoint is configured (qwenvl_oai, vision_enabled=true in config/api_endpoints.json).
4. Add regression test: `_assemble_tool_result` receiving a ContentItem list must preserve it (or emit image parts) end-to-end through the LLM conversion; existing tests/test_screen_capture.py only validates `ViewImage.call()` output shape.

## Files/refs (exact)
- `agent_cascade/tools/custom/file_ops.py:607-610` (return), `398-399` (class)
- `agent_cascade/agent.py:229-268` (`_call_tool` ContentItem branch 265-266)
- `agent_cascade/tool_dispatcher.py:170-188` (generic tool branch)
- `agent_cascade/compression/handler.py:294-355` (`_assemble_tool_result`; list→markdown 334-353)
- `agent_cascade/execution_engine.py:4055-4182` (tool exec → FUNCTION msg 4172-4181; caption ensure 2695-2705 + use site 2843-2845; `_append_and_log` 910-927; `_has_images` 2685-2693)
- `agent_cascade/llm/base.py:687-704` (`_conv_agent_cascade_messages_to_oai` drops image parts)
- `agent_cascade/llm/qwenvl_oai.py:41-140` (vision conversion, `conv_multimodal_value`, base64)
- `agent_cascade/utils/utils.py:960-972` (`encode_image_as_base64`), `616-641` (`format_as_text_message` image→[Image] placeholder)
- `agent_cascade/api_router.py:1561-1672` (`caption_images`), `73-117` (vision_enabled flag)
- `config/api_endpoints.json` (endpoints model_type=qwenvl_oai, vision_enabled=true)
- `web_ui/app.js:2740-2780` (`renderToolResult` — human-side rendering only)
- `web_ui/app.js:2755` + `api_server.py:901-914` (`/api/file` proxy)
- Live evidence: `N:\work\WD\AgentWorkspace\logs\orchestrator_Maine_20260808_142805.jsonl:6`
- Prior fix session: `N:\work\WD\AgentWorkspace\logs\coder_view_image_fixer_20260808_141839.jsonl`
- Prior lesson: `.agent_lessons/view_image_contentitem_stringification.md`
- Tests: `tests/test_screen_capture.py:38-89` (only validate tool return, not engine path)

Confidence: Confirmed for both drop points (code + live log evidence). Unknowns: whether any deployment endpoint actually accepts base64 image_url in tool messages (irrelevant once images are moved to user messages); UI rendering layer only verified at app.js level.