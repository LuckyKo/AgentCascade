# Investigation Report: Image Base64 Propagation in call_agent Task Messages

**Date:** 2026-08-09
**Investigator:** img_researcher
**Codebase:** `N:\work\WD\AgentCascade`
**Status:** Root cause verified; fix delegated to coder by supervisor

---

## Executive Summary

The bug is **confirmed**. `LifecycleManager.build_task_message()` at
`agent_cascade/lifecycle_manager.py:262-372` scans the **caller's entire conversation**,
collects image ContentItems (which are **base64 data URLs**), and embeds them into the
child agent's task message. This happens for every `call_agent` invocation AND for
system agents (Security/Compressor) via `_create_system_agent()`. `max_images_for_llm`
is **never applied during construction** — it only strips base64 later at LLM API call
time, meaning full base64 blobs are already persisted into child conversation history.
The answer to the key question is **YES**: a child CAN receive base64 images even when
its task never references them, via two independent paths (last-user-message path and
identical-basename matching).

---

## Key Findings

### 1. build_task_message() scans caller's conversation and embeds raw image values — CONFIRMED

`agent_cascade/lifecycle_manager.py`:

```python
# Line 310: Get caller's conversation history to scan for images
caller_conv = self.pool.get_conversation(caller)
seen_images = {}
if caller_conv:
    for msg in caller_conv:                                  # line 313
        content = msg_field(msg, 'content')
        if isinstance(content, list):
            for item in content:
                ...
                if item_type == IMAGE:                       # line 319
                    img_url = item_value
                    basename = get_basename_from_url(img_url) # line 323
                    seen_images[basename] = img_url
                    seen_images[f"image_{idx}"] = img_url    # line 326
```

- **Line 319**: matches `IMAGE` type items in ANY content list, in ANY message of the
  caller's entire conversation (not just recent).
- **Line 333-337**: appends `{IMAGE: img_url}` for images whose basename appears in
  task_text.
- **Line 339-357**: appends ALL images found in the **last USER message**
  unconditionally — **no task-text reference check**.

Both paths append the raw stored value (`img_url`), which is the base64 data URL (see
Finding 3).

### 2. max_images_for_llm is NOT read/applied during task message construction — CONFIRMED

- `build_task_message()` (lifecycle_manager.py:262-372) contains **zero references** to
  `max_images_for_llm` (verified by grep — matches only exist in
  `agent_pool.py`, `constants.py`, `config_handlers.py`, `web_ui/app.js`,
  `llm/base.py:528`).
- The only enforcement point is `llm/base.py:526-530`:

```python
# llm/base.py:526
max_images_for_llm = generate_cfg.get('max_images_for_llm', MAX_IMAGES_FOR_LLM_DEFAULT)
if max_images_for_llm != -1:
    messages = self._strip_base64_from_images(messages, max_images_for_llm)
```

- This runs in `_preprocess_messages()` **at API-send time**, AFTER the base64 blobs
  have already been embedded into the child's conversation (task message is the first
  USER message in the child's history). So the limit neither prevents storage bloat nor
  reduces per-agent token cost at construction — and it operates on the child's full
  message list, not on propagation choice.
- The plan file (`plans/fix_image_propagation_in_call_agent.md:21`) independently
  documents this same gap.

### 3. view_image stores BASE64 data URLs in conversations (not file paths) — CONFIRMED

`agent_cascade/tools/custom/file_ops.py:605-619`:

```python
# Line 607: Encode image as inline base64 so logs capture what was actually sent to the model
base64_data_url = encode_image_as_base64(image_path_for_encoding, max_short_side_length=1080)
...
return [
    ContentItem(image=base64_data_url),                       # line 617
    ContentItem(text=f"Viewing image: {path}")                # line 618
]
```

- `ContentItem.image` carries the base64 data URL. Caption/text travels in a separate
  ContentItem (schema: `agent_cascade/llm/schema.py:84-93`).
- These ContentItem lists are stored as FUNCTION-role messages in the agent's
  conversation for vision-capable agents (execution_engine.py:4190-4199 →
  `Message(role=FUNCTION, content=tool_result)`), preserved as lists by
  `compression/handler.py::_assemble_tool_result()` (lines 430-468) when the endpoint
  supports vision.

**Critical detail — `get_basename_from_url()` returns a CONSTANT for base64 URLs:**
`agent_cascade/utils/utils.py:236-245`:

```python
def get_basename_from_url(path_or_url: str) -> str:
    if path_or_url.lower().startswith('data:'):
        ...
        return f"data_image.{ext}"     # e.g. "data_image.png" for ALL base64 PNGs
```

Every base64 image in the caller's history produces the **same** `data_image.<ext>`
basename, and the aliases generated at lifecycle_manager.py:324-326 (`image_0`,
`image_1`, ...) are positional. Consequences:
- The "referenced in task text" check (line 335: `if basename in task_text`) becomes
  trivially true for ANY image once `data_image` or `image_N` appears anywhere in the
  task text — so images not actually relevant to the task get included.
- The `seen_images` dict keys collide (later images overwrite earlier ones for the same
  ext), so the scan is lossy and order-dependent.

### 4. _create_system_agent() also propagates image data — CONFIRMED

`agent_cascade/execution_engine.py:5273-5336`:

```python
# Line 5303: Build args dict for lifecycle manager's build_task_message
args = {'task': task, 'context': context}
...
# Line 5315: Build task message using lifecycle manager
task_msg = self.lifecycle.build_task_message(args, caller, agent_class=inst.agent_class)
```

- `_create_system_agent()` (used for **Security** and **Compressor**) funnels through
  the exact same `build_task_message()`.
- The ONLY exclusion is at lifecycle_manager.py:330: `if agent_class != 'Compressor':`
  — so **Security receives image data**, and the exclusion list does not cover any
  other text-only agent class.
- Note the exclusion wraps the entire image-propagation block, so Compressor is
  correctly skipped today, but Security is not.

### 5. Existing tests — NO coverage for image propagation in call_agent — CONFIRMED

- `tests/test_nested_agent_calls.py:470, 519` — `build_task_message` is **mocked out**
  (`MagicMock`); no propagation logic exercised.
- `tests/test_vision_tool_call_regression.py` — covers ContentItem preservation in tool
  results and `convert_messages_to_dicts`, NOT call_agent propagation.
- `tests/test_screen_capture.py` — view_image directive routing only.
- No test file references `build_task_message` image behavior, `data_image`, or
  propagation limits. Grep of `tests/` for `build_task_message|call_agent|view_image`
  shows no propagation assertions.

---

## Key Question — Direct Answer

> When parent agent has viewed images (base64 stored in conversation) and calls a child
> agent WITHOUT referencing those images, does the child's task message still contain
> base64 image data?

**YES — with high confidence, for the common case.** Two mechanisms:

1. **Last-user-message path (unconditional):** lifecycle_manager.py:349-357 appends
   every image ContentItem in the last USER message regardless of task relevance. When
   the caller's most recent user/turn message embeds images (or when the latest message
   with list content containing images is a USER message), they flow into the child
   task message with zero relevance filtering.

2. **Basename collision path:** because `get_basename_from_url()` maps ALL base64 data
   URLs to a constant `data_image.<ext>`, the line-335 check (`basename in task_text`)
   matches any image whenever the task text contains `data_image` or an alias. Even a
   generic phrase containing an alias can pull in every image.

Both paths embed the **raw base64 data URL** (potentially megabytes) into the child's
first message — before `max_images_for_llm` ever runs at API time.

**Caveat (accuracy note):** whether the last-user-message path fires depends on the
exact shape of the caller's latest messages. `view_image` results land as FUNCTION
messages; the unconditional USER-path fires when a USER-role message with a list content
(containing images) is the latest matching USER message. The basename-collision path
alone is sufficient to demonstrate the bug independent of message ordering.

---

## Supporting Evidence Summary

| # | Claim | Evidence |
|---|-------|----------|
| 1 | build_task_message scans caller conv & embeds images | lifecycle_manager.py:310-357 |
| 2 | max_images_for_llm not applied at construction | grep: no refs in lifecycle_manager.py; only llm/base.py:526-530 |
| 3 | view_image stores base64 data URLs | file_ops.py:605-619 (`encode_image_as_base64`) |
| 4 | base64 URLs → constant basename `data_image.*` | utils/utils.py:236-245 |
| 5 | Security (system) agents also get images | execution_engine.py:5315 + lifecycle_manager.py:330 (only Compressor excluded) |
| 6 | No tests cover propagation | test_nested_agent_calls.py:470,519 (mocked); vision tests cover tool-result path only |
| 7 | Plan doc independently confirms | plans/fix_image_propagation_in_call_agent.md:13-21 |

---

## Confidence Level

- **Confirmed (static analysis + code trace):** propagation logic, base64 storage
  format, missing max_images_for_llm at construction, system-agent exposure, no test
  coverage.
- **High Confidence (behavioral inference):** child task messages actually receive
  base64 data in the common case (basename collision path is deterministic given the
  code; last-user-message path depends on conversation shape). No live reproduction was
  run in this investigation; logs from the current session contain no base64 images
  because Maine had not viewed images before dispatching.

## Open Questions / Remaining Unknowns

1. Does the last-user-message path fire in production with view_image results? Depends
   on whether the newest USER-role message with list-content contains images; worth a
   live repro or log inspection of a real parent that called view_image then call_agent.
2. Is there a separate path where caption texts are embedded as text ContentItems in
   task messages? The plan mentions "image base64 data and caption texts" — captions
   ride on the same ContentItem (schema.py:91-93) but `build_task_message()` only
   copies `{IMAGE: value}` items, so captions are not copied today; verify against
   desired behavior.
3. Exact token/byte impact of the leak (base64 expands ~4/3× binary size) — not
   measured.

## Suggested Next Actions (for the coder)

1. **Apply `max_images_for_llm` in build_task_message()** — read from
   `self.pool.llm_cfg.get('max_images_for_llm')` (default
   `MAX_IMAGES_FOR_LLM_DEFAULT = 2`, constants.py:117) and cap propagation count.
2. **Require real task relevance** — match on actual basename/path tokens; stop using
   the lossy `seen_images[basename]` dict keyed by constant `data_image.*`; consider
   matching on the `image_N` alias only when unique.
3. **Remove unconditional last-user-message propagation** or gate it behind
   `max_images_for_llm` and explicit references.
4. **Extend the exclusion list** — Security (and any text-only system agent) should be
   excluded like Compressor (lifecycle_manager.py:330).
5. **Prefer file paths over base64 in conversations** (Phase 2 per plan): store
   path/caption in ContentItem, encode base64 only at API payload build time
   (`qwenvl_oai.py convert_messages_to_dicts`).
6. **Add regression tests** covering: (a) parent with base64 images calls child without
   referencing them → child gets no image data; (b) `max_images_for_llm=0` → no base64
   propagated; (c) Security/Compressor → no image data; (d) explicit reference by
   basename → still propagates.
7. Also fix the known adjacent bug documented in
   `.agent_lessons/handler_preserves_multimodal_contentitem_lists_for_vision_agents.md`:
   execution_engine.py:4134 passes pool-level `llm_cfg` (no `model_type`) to
   `_assemble_tool_result`, so vision detection may fail in production.

## Files Referenced

- `agent_cascade/lifecycle_manager.py` (262-372, 330)
- `agent_cascade/tools/custom/file_ops.py` (505-619)
- `agent_cascade/execution_engine.py` (5030-5110, 5273-5336, 4134, 4190-4199)
- `agent_cascade/llm/base.py` (505-531)
- `agent_cascade/llm/schema.py` (84-139)
- `agent_cascade/utils/utils.py` (236-246, 1260+ `strip_base64_from_images`)
- `agent_cascade/compression/handler.py` (389-535)
- `agent_cascade/constants.py` (112-117)
- `plans/fix_image_propagation_in_call_agent.md`
- `.agent_lessons/handler_preserves_multimodal_contentitem_lists_for_vision_agents.md`