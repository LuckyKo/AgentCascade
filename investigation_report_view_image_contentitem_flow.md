# view_image / screen capture — ContentItem tool result trace (Debugging)

**Investigation:** researchers.2026-08-08 · **Status:** root cause confirmed

## Code path (tool return → conversation message)

| Step | Location | Behavior |
|---|---|---|
| Return | `tools/custom/file_ops.py:607-610` | `ViewImage.call()` → `[ContentItem(image=uri), ContentItem(text=...)]` (captures + files identical) |
| Pass-through | `agent.py:263-268` | `_call_tool()`: ContentItem list → `return tool_result` (OK) |
| Dispatch | `tool_dispatcher.py:180-188` | returns list unchanged |
| **FATAL stringify** | `compression/handler.py:331-335` `_assemble_tool_result()` | `str(raw_tool_result)` on non-str → `[ContentItem(text=None, image='file:///...')]` (pydantic repr) |
| Wrap | `execution_engine.py:4172-4180` | `Message(role=FUNCTION, name=..., content=tool_result)` — content is mangled string |
| Append | `execution_engine.py:4181-4182` + `_append_and_log` (910-927) | pool conv + JSONL logger |

## Why regular view_image has the same issue
Identical list → identical `str()` at handler.py:335. There is no engine branch that keeps
ContentItem lists alive. The only list-preserving path is direct `fncall_agent.py:102-113`
(skips execution engine), and even there `llm/base.py:691-704` flattens lists and **drops
image content** (joins only `.text`), so no real image bytes reach a vision model.
Caption fallback (`api_router.caption_images` + `_ensure_image_captions`) can only rescue
images while `msg.content` is still a list — it never sees them post-stringification.

## Fix points (observations, unapplied)
- `compression/handler.py:331-335`: bypass assembly for ContentItem lists (`all(isinstance(i, ContentItem))`).
- `execution_engine.py:4172-4181`: branch FUNCTION wrap on list content; keep list, run caption ensure first.
- `llm/base.py:691-704`: for OpenAI spec, emit `image_url`/base64 parts for `image` items, not just `.text`.

## Files/refs (exact)
- `agent_cascade/tools/custom/file_ops.py:607-610` (return), `398-399` (class)
- `agent_cascade/agent.py:229-268` (`_call_tool` ContentItem branch at 265-266)
- `agent_cascade/tool_dispatcher.py:127-188` (execute_tool generic branch 170-188)
- `agent_cascade/compression/handler.py:294-374` (`_assemble_tool_result`; stringify 331-335)
- `agent_cascade/execution_engine.py:4055-4182` (tool exec → FUNCTION message 4172-4181; caption ensure 2695-2705, use site 2843-2845; `_append_and_log` 910-927)
- `agent_cascade/llm/base.py:687-704` (OpenAI FUNCTION conversion drops image parts)
- `agent_cascade/llm/fncall_prompts/qwen_fncall_prompt.py:57-70` (interleaves ContentItems in direct path)
- Tests: `tests/test_screen_capture.py:38-89` (only validate tool return, not engine path)

Confidence: Confirmed for handler.py:335 as the visible stringification; High for the rest.
Unknowns: whether deployment endpoints are vision-capable; UI rendering layer not inspected.
Next: add regression test for `_assemble_tool_result` receiving ContentItem lists; patch handlers;
live verify with `__screen_capture` + a regular file image.