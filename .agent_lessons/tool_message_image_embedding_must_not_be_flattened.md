---
title: Tool Message Image Embedding Must Not Be Flattened
tags: [vision, images, tool-calling, regression, critical]
created: 2026-08-08
status: active
---

## Problem

Vision models must receive actual image data (base64-encoded) from `view_image` tool results. They cannot see the image from text captions or markdown links alone.

## Root Cause History

**Commit eaa78f5** ("Enhance LLM compatibility, add soft rollback and agent soul refresh") broke image embedding by changing FUNCTION message handling in `_conv_agent_cascade_messages_to_oai()` to flatten ContentItem lists to text-only strings:

```python
# BROKEN — strips all images from tool messages
if isinstance(content, list):
    parts = []
    for item in content:
        if hasattr(item, 'text') and item.text:
            parts.append(item.text)
    content = ''.join(parts)  # Images silently dropped here
```

Before this commit, FUNCTION messages were passed through intact (`copy.deepcopy(msg)`), so images survived.

## How Images Flow (Correct Path)

1. `view_image.call()` returns `[ContentItem(image=file://...), ContentItem(text="Viewing image: ...")]`
2. For vision models: `QwenVLChatAtOAI.convert_messages_to_dicts()` converts images to base64:
   ```python
   {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,....'}}
   ```
3. Then `_conv_agent_cascade_messages_to_oai()` is called — **MUST PRESERVE** these image_url items for FUNCTION/tool messages
4. Final API request contains actual base64 image data in tool message content list

## The Fix (base.py lines ~687-715)

In `_conv_agent_cascade_messages_to_oai`, FUNCTION message handling must detect multimodal items and preserve the list:

```python
elif msg['role'] == FUNCTION:
    tool_call_id = msg.get('extra', {}).get('function_id', '1')
    content = msg.get('content', '')
    if isinstance(content, list):
        has_multimodal = any(
            (hasattr(item, 'type') and item.type in ('image_url', 'video_url', 'input_audio'))
            or (isinstance(item, dict) and item.get('type') in ('image_url', 'video_url', 'input_audio'))
            for item in content
        )
        if has_multimodal:
            pass  # Preserve list with images — DO NOT FLATTEN
        else:
            # Text-only: flatten to string (backward compatible)
            parts = [...]
            content = ''.join(parts)
    new_messages.append({
        'role': 'tool',
        'tool_call_id': tool_call_id,
        'content': content or '',  # Can be list with images or plain string
    })
```

## Critical Rules

- **NEVER flatten a FUNCTION/tool message's list content to string without checking for multimodal items first**
- **NEVER assume tool messages only contain text** — `view_image`, screen capture, and window capture all return images
- OpenAI-compatible vision APIs accept list content in tool messages with image_url items
- Text-only tool results can still be flattened to strings (safe fallback)

## Related Files

- `agent_cascade/llm/base.py` — `_conv_agent_cascade_messages_to_oai()` (PRIMARY — this is where images get dropped)
- `agent_cascade/llm/qwenvl_oai.py` — `convert_messages_to_dicts()` (converts file:// to base64)
- `agent_cascade/compression/handler.py` — `_assemble_tool_result()` (captioning fallback)
- `agent_cascade/tools/custom/file_ops.py` — `ViewImage.call()` (returns ContentItem lists with images)

## How to Verify Fix Works

1. Call `view_image` on any image file
2. Check the debug log for LLM input — tool message should contain:
   ```json
   {"role": "tool", "content": [{"type": "image_url", "image_url": {"url": "data:image/..."}}, {"type": "text", "text": "..."}]}
   ```
3. Vision model should correctly describe image contents (not hallucinate or give generic descriptions)

## See Also

- [[view_image_image_embedding_two_drop_points]] — original investigation report