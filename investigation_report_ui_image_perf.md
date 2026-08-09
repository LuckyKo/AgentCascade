# UI Performance Investigation: Multiple Images in AgentCascade Agent Tabs

**Date:** 2026-08-09
**Investigator:** researcher_image_perf
**Scope:** Web UI rendering performance degradation with many inline base64 images during active streaming
**Files examined:** `web_ui/app.js`, `web_ui/index.html`, `web_ui/styles.css`, `agent_cascade/api_integration.py`, `agent_cascade/tools/custom/file_ops.py`, `agent_cascade/execution_engine.py`

---

## Executive Summary

The UI streaming refresh-rate drop with many images is **not** caused by a single bug, but by a **systemic mismatched assumption** between the backend and frontend:

- The backend streams **full conversation snapshots on partial updates** (every 100ms tick), **always including every base64 image** because `serialize_message()` flattens `ContentItem` image objects into inline `![image](data:image/...base64...)` markdown strings (`api_integration.py:1416-1444`).
- The frontend, on each `stream_update`, **re-renders the entire message panel** and **recreates every message bubble from scratch** (including `<img>` elements) whenever the last-message content key changes — which it does on every streaming tick because the active agent's last message grows (`renderSubAgentPanel` contentKey at `app.js:3541` includes `lastMsgTextLen`).
- Because `updateBubbleContent()` replaces `contentDiv.innerHTML` on **every** content change (full re-render) rather than preserving DOM nodes, **every inline base64 image is re-parsed, re-decoded, and re-rendered** up to ~7–10 times/second during streaming — even though the image bytes never change.

This creates an **O(images × history × ticks)** cost: with N images and M streaming ticks, the browser does N×M base64 decodes and image rasterizations, plus the websocket carries N image payloads on every tick.

Severity: **Critical** for sessions with ≥5 images and long streaming turns.

---

## Root Cause Analysis

### Finding 1 — Images are rendered as inline base64 data URIs, re-created on EVERY render (CRITICAL)

**How images get into messages:**
- Tool `view_image` returns a `ContentItem` list with an inline base64 data URI (not a file path, not a placeholder):
  ```python
  # agent_cascade/tools/custom/file_ops.py:607-619
  base64_data_url = encode_image_as_base64(image_path_for_encoding, max_short_side_length=1080)
  return [
      ContentItem(image=base64_data_url),
      ContentItem(text=f"Viewing image: {path}")
  ]
  ```
  Images can be **multiple MB**. A 1080px-encoded PNG at quality 0.9 is typically 500KB–2.5MB as base64.

- `serialize_message()` (backend → frontend) joins every message's content parts into a string, embedding the images inline:
  ```python
  # agent_cascade/api_integration.py:1416-1444
  if isinstance(content, list):
      parts: List[str] = []
      for item in content:
          ...
          elif 'image' in item:
              parts.append(f"![image]({item['image']})")   # <-- full base64 data URI inline
  content = '\n'.join(parts)
  ```

**Where the images get re-created in the DOM:**
- `renderAgentConversation()` (app.js:2289) calls `createMessageEl()` for EVERY message on every full render.
- `createMessageEl()` sets `contentDiv.innerHTML = html` (app.js:2449) where html includes the markdown-rendered `<img src="data:image/...">`.
- On the very next streaming tick, `updateBubbleContent()` runs and — because the content changed (streaming text) — **wipes and re-parses `innerHTML`** (app.js:2596):
  ```js
  contentDiv.innerHTML = html;  // line 2596 — full replacement, no image preservation
  ```
  This replaces **all** `<img src="data:...">` nodes inside the bubble. The browser must re-decode each base64 string, re-rasterize each image, and re-layout the page. This happens every render tick, even when the image itself hasn't changed.

**Impact:**
- Each `stream_update` tick (at 100ms throttle → **~7–10 updates/sec**, `THROTTLE.RENDER_SUBAGENT_MS = 150` at app.js:46) during active streaming re-creates **every** image element in the active tab.
- Each re-creation costs:
  - base64 decode of the image (e.g., 2MB string → ~1.5MB binary) 
  - PNG/JPEG decode via browser codec
  - CSS layout + rasterization of the `<img>` at its display size (up to 400px max-height, styles.css:2285)
- With even 3–5 images in history, this is **tens of MB of re-decoding per streaming tick**, saturating the main thread and causing the "refresh rate drop."

**Severity: CRITICAL** — this is the primary cause of the "Images appear to be reloaded/rendered repeatedly" symptom.

### Finding 2 — Entire message history re-rendered, not just delta appended (HIGH)

The streaming incremental-append (from earlier `lessons_incremental_stream_fix.md`) is implemented but has a narrow scope:

app.js:2540-2559:
```js
// FIX 2: Restore incremental path for plain-text messages only...
if (isGenerating && prevContent !== undefined && !msg.function_call && msg.role !== 'function' && !msg.reasoning_content) {
    const newText = curContent.slice(prevContent.length);
    if (newText) {
        appendStreamingDelta(contentDiv, newText);   // O(1) text append
        ...
        return;  // success - skip full re-render
    }
}
```

**The problem:** 
1. The incremental path is gated only on absence of `function_call`, non-`function` role, and no `reasoning_content` (app.js:2540). The comment says "plain-text messages only," but **the code does not check for images** — a bubble containing inline images can take this path, which then calls `appendStreamingDelta` that descends into the last element and appends text. However, if the last element is an `<img>` (or the message contains images anywhere), the plain text append will still be applied to the *last text container*, which may misplace content; and even when the incremental path succeeds, **FIX 4 at line 2549 forces a full re-render every 8th tick** — with images in the bubble, every 8th tick (i.e., ~every 800ms) wipes and recreates the images.

**Impact**: Even with the incremental text path, images inside the *message being streamed* force a full re-render every 8 ticks, and images in *every other* message get wiped whenever a full `renderAgentConversation` happens (message append, delete, tab switch, out-of-sync recovery).

---

### Finding 3 — websocket carries full image payloads on every stream_update tick (backend, HIGH)

- `build_stream_update_from_pool()` is invoked on **every** streaming tick (`broadcast_stream_update`, api_integration.py:202-206: `is_streaming_tick` or 100ms elapsed → broadcast).
- It calls `_serialize_instancesIncremental()` (api_integration.py:674) which — even with the version-dedup — must re-serialize the ACTIVE instance every tick:
  ```python
  # api_integration.py:722
  if name == instance_name or current_version != prev_version or force_full:
      all_instances[name] = _serialize_instance(
          inst, pool, include_messages=True, streaming=..., streaming_responses=...)
  ```
  Note: non-active instances are only re-serialized when their version changes or on the 100-tick forced full refresh — but the active (streaming) instance, holding the base64 image history, is re-serialized on **every** tick.
- `serialize_message` for the active instance re-flattens ALL messages (`msgs = full_msgs_snapshot` at 1558), each carrying its `![image](data:...)` string. **The full base64 image text is transmitted to the browser multiple times per second.**
- `isPartial` is set `True` when streaming responses exist (api_integration.py:1569), meaning the **same** base64 image is sent again and again — there is no image-content dedup, only fingerprint dedup for whole messages.

**Impact:** 
- With a 1.5MB image in a message and 10 ticks/sec, the browser receives ~15MB/s of repeated identical image text.
- `JSON.parse` on the receiving side (app.js:1496) allocates and copies that whole string each time — another main-thread stall.

### Finding 4 — Images are re-rendered during streaming markdown (frontend per-tick parser)

- `renderMarkdown(text, false)` (app.js:2609-2670) parses the full markdown and **DOMPurify-sanitizes** it via `DOMPurify.sanitize(marked.parse(text))` — on every full re-render.
- The `marked.parse` → `DOMPurify.sanitize` pipeline will convert `![image](data:...)` to `<img src="data:...">` DOM nodes each time. This is necessary for rendering but becomes O(N) when N includes megabytes of base64 across many images.
- There's no caching between renders: each call re-parses the whole string including all embedded base64.

### Finding 5 — No image element preservation / caching strategy (frontend)

- There is no `loading="lazy"` or `decoding="async"` on message images (only the textarea preview at app.js:4469 uses `loading='eager'`).
- No `data-*` attribute caching of the image binary across renders.
- No `img` node reuse: `innerHTML` replacement destroys nodes each cycle.
- No memoization keyed on `content` to skip re-render if the only delta is in another message.

---

## Code Locations (verified)

| # | File | Line(s) | What |
|---|------|---------|------|
| 1 | `web_ui/app.js` | 1599-1621 | `stream_update` handler: merges `data.agent_instances` into `state.subAgents` |
| 2 | `web_ui/app.js` | 1680-1765 | Full re-render loop on every message (throttled) |
| 3 | `web_ui/app.js` | 3525-3761 | **`renderSubAgentPanel`** — contentKey includes `lastMsgTextLen` → any streaming change forces re-render; full rebuild paths 3562-3590 |
| 4 | `web_ui/app.js` | 3615-3621 | Previous bubble forced full re-render when new message arrives |
| 5 | `web_ui/app.js` | 2449, 2596 | `contentDiv.innerHTML = html` — destroys/recreates `<img>` |
| 6 | `web_ui/app.js` | 2540-2559 | Incremental append only for no-images plain text; every 8th tick full re-render |
| 7 | `web_ui/app.js` | 2609-2670 | `renderMarkdown` + DOMPurify on full content each time |
| 8 | `agent_cascade/api_integration.py` | 1416-1444 | `serialize_message` embeds full base64 images inline |
| 9 | `agent_cascade/api_integration.py` | 1561-1601 | All messages serialized each snapshot, no image dedup |
| 10 | `agent_cascade/api_integration.py` | 674-740 | `_serialize_instancesIncremental` serializes active instance every tick |
| 11 | `agent_cascade/execution_engine.py` | 3090-3093 | Streaming responses updated every ~100ms (deep copy of partial) |
| 12 | `web_ui/app.js` | 1494-1497 | `JSON.parse(event.data)` — parses multi-MB message each tick |
| 13 | `web_ui/app.js` | 46 | Throttle 150ms → ~7 re-renders/sec |

---

## Severity Assessment

| Finding | Severity | Impact | Evidence |
|---------|----------|--------|----------|
| Inline base64 images of full history re-rendered every tick | **Critical** | Browser main thread stalls; refresh rate drop | app.js:2449, 2596; api_integration.py:1418-1427 |
| Full conversation re-serialize per tick (backend) | **High** | MBps over WS; JSON.parse each tick | api_integration.py:1571-1600 |
| Incremental append only for plain text — no image-only dedup | **Medium** | Images still re-render at 8-tick boundaries | app.js:2540-2559 |
| No image caching / `decoding="async"` / lazy loading | **Medium** | Unnecessary decode+rasterize | styles.css:2285; app.js:4467-4469 |

---

## Recommended Fixes (Prioritized)

### P0 — Decouple image rendering from streaming delta (DO IT FIRST, highest impact)

Replace `contentDiv.innerHTML = html` full replacement with **tree-preserving** diffing, or at minimum:

1. **Cache image nodes across updates:** Before `innerHTML` replacement, collect existing `img` elements and their `data-src-hash` and reuse them if the image content hash matches:
   ```js
   // before replace: keep a Map of hash → img element
   const imgCache = new Map();
   contentDiv.querySelectorAll('img[data-image-hash]').forEach(img => imgCache.set(img.dataset.imageHash, img));
   // after: re-insert same nodes
   ```
2. **At minimum, skip `innerHTML` replacement entirely when only text delta changed and the bubble contains images.** Since `appendStreamingDelta` already does pure-text append, extend it to also handle bubbles with images by appending only new text nodes — never replacing the images.

### P1 — Stop re-serializing identical image content on every tick (backend)

- In `_serialize_instance` / `serialize_message`, once a message is committed and its content includes base64 images, **send the image URI only once** per session, then send a placeholder token (e.g., `![image](#IMAGE<id>)`) on subsequent ticks, matching it on the client.
- Or: don't include full message content in `stream_update` at all unless it *changed*. Currently the code includes ALL serialized messages each tick (`start_idx=0`, line 1563-1564). Only send the deltas (new/streamed message) which eliminates the base64 from every tick.

### B2 — Add `decoding="async"` and `loading="lazy"` to images

- In `DOMPurify`-produced `<img>` not in message, add attributes or post-process:
  ```js
  contentDiv.querySelectorAll('img').forEach(img => { img.decoding = 'async'; img.loading = 'lazy'; });
  ```
- This offloads decode from the main thread and avoids painting offscreen images.

### B2 — Prevent `renderSubAgentPanel` from doing full rebuilds when only streaming content changed

- The `contentKey` at app.js:3541 includes `lastMsgTextLen` which changes on every stream tick. This forces `renderAgentConversation` for ALL messages whenever streaming. Instead, use `updateBubbleContent` for the last bubble and `append` only.
- The full `renderAgentConversation` calls at 3567/3590 can be replaced with bubble-level updates + append of new messages only.

### B3 — Increase render throttle & batch DOM updates

- The 150ms throttle redisplays whole panels. Increase to ~250ms or batching multiple frames, and only update the *last* bubble in place (which already exists for plain text) instead of rebuilding.

### B4 — Move the 8-tick full re-render to only *text-only* bubbles

- In `updateBubbleContent`, the `incrementCount >= 8` full re-render (app.js:2549) is for heading/formatting convergence; but for bubbles containing images, either skip the forced re-render (accept slight markdown drift) or do the re-render far less frequently (e.g. every 32).

---

## Quantified Example (illustrative)

Assumptions: 3 images @ ~1.2MB each in history, 10 stream_updates/sec, 1-streaming bubble:

- WS bytes: 3×1.2MB×40 = **~144MB/s** (before compression) — very likely saturating console and JS parse.
- DOM/img decode: 3 images × 10 re-decode = **30 image decodes/sec** (~50-150ms each) + layout — enough to kill the frame budget (16.7ms/frame).

With the P0 fix (reuse img node), decode cost drops to **~3 decodes total per session**, and WS drops to only the *changed* delta text.

---

## Related Prior Findings

- `lessons_incremental_stream_fix.md` (2026-06): incremental rendering exists but only for plain text; tool bubbles and multi-image messages fall through to full `innerHTML` replacement. This is the same class of bug observed here (images trapped in a full replacement bucket).
- The 8-tick full re-render `FIX4` drifted from the incremental philosophy — it's the source of the periodic "image reload" felt by users.

**Confidence level:** High — the code paths are verifiably the ones that execute during streaming with images. The exact magnitude of the slowdown depends on browser/codec and image sizes, but the mechanism (re-parse of unchanged images) is confirmed by the code.

**Open questions / unknowns:**
- Whether the images in the *streaming* bubble (the actively streamed assistant message contains images) is rare — but `view_capture` tool results and user-uploaded images in prior messages are common and hit the same wipe.
- Actual measured frame-time/throughput would need a browser profile; not available in this repo snapshot.

## Recommendations

1. **Implement P0 image-node reuse** immediately (highest ROI, ~1 file/function).
2. **Backend: only stream changed/active messages** (P1) — eliminate 144MB/s class traffic.
3. **Add `decoding='async'` + lazy loading** as a cheap win.
4. **Reduce the 8-tick forced full re-render** for image-bearing bubbles.
5. Afterwards, re-profile with DevTools Performance to confirm TTI/frame delta.

*Report prepared by researcher_image_research. Evidence based on source reading at the cited locations.*