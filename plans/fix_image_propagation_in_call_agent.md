# Fix: Image Base64 Propagation in call_agent Task Messages

## Problem Statement

When `call_agent` is invoked, image base64 data and caption texts from the parent's conversation are being appended to ALL child agent task messages based on `Max Images for LLM` settings. This causes:

1. Raw base64 image data (potentially megabytes) passed to agents that don't need it
2. Image data is per-agent, not global — each agent should only receive images relevant to its task
3. Unnecessary token consumption and potential LLM context issues

## Root Cause Analysis

Location: `agent_cascade/lifecycle_manager.py::build_task_message()` (lines 262-372)

Current behavior:
1. Scans caller's entire conversation for ALL images → stores as base64 data URLs from view_image tool results
2. For images whose basename appears in task text → adds `{IMAGE: img_url}` with raw base64 to child task message
3. For last user message → adds ALL images found there regardless of task relevance
4. Only skips image embedding for Compressor (line 330), but NOT for Security or other agents

The `max_images_for_llm` setting only controls stripping at LLM API call time (`llm/base.py::generate()`), not during task message construction. So base64 data flows into child task messages first, then gets stripped later — wasteful and incorrect.

## Design Goals

1. **No raw base64 in task messages**: Child agents should receive file paths or captions, not megabytes of base64
2. **Relevance-based propagation**: Only propagate images actually needed by the child's task
3. **Respect max_images_for_llm**: Apply image limits during task message construction, not just at API call time
4. **Backward compatible**: Don't break existing workflows that legitimately need image context

## Proposed Solution

### Change 1: Store file paths instead of base64 in conversations

**File**: `agent_cascade/tools/custom/file_ops.py::ViewImage.call()` (line 610)

Current behavior:
```python
base64_data_url = encode_image_as_base64(image_path_for_encoding, max_short_side_length=1080)
return [ContentItem(image=base64_data_url), ContentItem(text=f"Viewing image: {path}")]
```

**Option A (Preferred)**: Store file paths in conversations, encode to base64 only when sending to LLM API.
- Pros: Clean separation, no bloat in conversation history
- Cons: Requires changes to how images are referenced throughout the codebase

**Option B (Minimal Change)**: Keep base64 in conversations but fix propagation logic in build_task_message()
- Pros: Minimal disruption
- Cons: Conversations still contain large base64 blobs

### Change 2: Fix image propagation in build_task_message()

**File**: `agent_cascade/lifecycle_manager.py::build_task_message()`

Fixes needed:
1. Apply `max_images_for_llm` during task message construction (read from pool.llm_cfg)
2. Only propagate images that are explicitly referenced in the task text by basename/alias
3. For last user message images: only add if also referenced in task OR if max_images allows it
4. Consider using file paths instead of base64 when possible

### Change 3: Ensure Security agent doesn't receive image data

**File**: `agent_cascade/lifecycle_manager.py::build_task_message()` (line 330)

Currently only skips Compressor. Add Security to the exclusion list since it's text-only.

## Implementation Plan

### Phase 1: Immediate Fix (Minimal Risk)

Fix `build_task_message()` to:
1. Read `max_images_for_llm` from pool.llm_cfg and apply during construction
2. Exclude Security agent from image propagation (like Compressor)
3. Only add images that are explicitly referenced in task text by basename/alias
4. Limit total images added based on max_images_for_llm

### Phase 2: Architecture Fix (Medium Risk, Higher Impact)

Change conversation storage to use file paths instead of base64:
1. ViewImage tool stores file path + caption as ContentItem(image=file_path)
2. LLM adapters encode to base64 when building API payloads
3. build_task_message() propagates file paths, not base64

### Phase 3: Validation

Test scenarios:
- call_agent with image in parent's conversation → child should only get relevant images
- call_agent to Security/Compressor → no image data
- max_images_for_llm=0 → no base64 propagated
- view_image followed by call_agent referencing that image → works correctly

## Files Affected

- `agent_cascade/lifecycle_manager.py` - Primary fix location
- `agent_cascade/tools/custom/file_ops.py` - ViewImage tool (Phase 2)
- `agent_cascade/llm/qwenvl_oai.py` - Base64 encoding at API call time (Phase 2)