# XSS Sanitization - DOMPurify Integration

## Date
2026-05-29

## Problem
LLM-generated content was rendered via `marked.parse()` and injected into the DOM via `innerHTML` without any sanitization. This creates an XSS vulnerability — a malicious LLM response could inject `<script>` tags or other HTML that executes in the user's browser.

## Solution
Added DOMPurify v3.1.7 to sanitize all HTML generated from markdown rendering, and escaped server-provided data that goes into innerHTML via template literals.

## Changes Made

### 1. Added DOMPurify CDN (`index.html`)
- Added `<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.7/dist/purify.min.js"></script>` right after the marked.js script tag
- This ensures DOMPurify loads before app.js

### 2. Added DOMPurify hardened configuration (`app.js`, lines ~20-36)
Explicit ALLOWED_TAGS and ALLOWED_ATTR whitelist for defense-in-depth:
```javascript
DOMPurify.setConfig({
  ALLOWED_TAGS: ['b','i','u','em','strong','a','code','pre','blockquote',
                 'p','br','hr','ul','ol','li','table','thead','tbody',
                 'tr','th','td','img','details','summary','span','h1'...'h6',
                 'div','section','article','mark','del'],
  ALLOWED_ATTR: ['href','src','title','class','open','style','alt'],
  ALLOW_DATA_ATTR: true,
});
```
Note: `'data-*'` should NOT be in ALLOWED_ATTR — DOMPurify treats it as a literal attribute name. `ALLOW_DATA_ATTR: true` handles data attributes.

### 3. Sanitized `renderMarkdown()` (`app.js`)
Both code paths in `renderMarkdown()` now wrap `marked.parse()` output with `DOMPurify.sanitize()`:
- Line ~1720: fast path (no thinking block parsing)
- Line ~1768: full path (with thinking block parsing)

This is the **most important fix** — it covers all content rendered through the main pipeline:
- Agent messages via `createMessageEl()` → `renderMarkdown()`
- Security advisor responses (line 1247)
- Approval descriptions (line 2246)
- Tool results (via `renderToolResult` → `renderMarkdown()`)
- Thinking blocks (via `renderThinkingBlock` → `renderMarkdown()`)
- System bubbles (via `appendSystemBubble` → `renderMarkdown()`)

### 4. Escaped session data in `renderSessions()` (`app.js`)
Session name, agent name, and path from server response are now escaped with `escapeHtml()`:
```javascript
// Before:
${s.name} / ${s.agent} / ${s.path.replace(...)}
// After:
${escapeHtml(s.name)} / ${escapeHtml(s.agent)} / ${escapeHtml(s.path.replace(...))}
```

### 5. Fixed onclick handler injection in approval cards (`app.js`)
Changed from inline string interpolation of `request_id` to using data attributes:
```javascript
// Before (vulnerable to quote injection):
onclick="approveRequest('${ap.request_id}')"
// After (safe via data attribute):
data-request-id="${escapeHtml(ap.request_id)}" onclick="approveRequest(this.dataset.requestId)"
```

Same pattern applied to:
- `askSecurity()` calls
- `showRejectInput()` calls
- Reject button in `showRejectInput()` function body

### 6. Escaped `state.sessionName` in tab innerHTML (`app.js`, lines ~2754 and ~2762)
```javascript
// Before:
rootTabEl.innerHTML = '...💬 ' + (state.sessionName || 'Maine') + '_root';
// After:
rootTabEl.innerHTML = '...💬 ' + escapeHtml(state.sessionName || 'Maine') + '_root';
```

### 7. Escaped telemetry data (`app.js`)
- Tool names in `updateTelemetryPanel()`: both text content and title attribute escaped
- Model names and config fingerprints in `updateTelemetryConfigTable()`: all properly escaped

## Key Design Decisions

1. **Sanitize at render time, not at parse time**: DOMPurify runs on the final HTML output of `marked.parse()`, not on the raw markdown text. This is more efficient and catches any XSS that could arise from marked's HTML generation.

2. **Streaming deltas are safe by design**: `appendStreamingDelta()` uses `insertAdjacentText()` / `createTextNode()` which create text nodes, not HTML elements. These are inherently safe from XSS since browsers don't parse them as HTML.

3. **escapeHtml() for non-markdown server data**: Session names, agent names, request IDs etc. that go directly into innerHTML via template literals use the existing `escapeHtml()` function rather than DOMPurify (which is overkill for simple string escaping).

4. **data attributes instead of inline onclick strings**: Using `data-request-id` + `this.dataset.requestId` pattern prevents quote injection attacks in onclick handlers.

## Attack Vectors Now Mitigated
- LLM generating `<script>alert(1)</script>` in markdown content
- LLM generating `<img src=x onerror=alert(1)>` in tool results
- Malicious session names containing HTML from server (tab labels, session list)
- Request IDs containing quotes that break out of onclick strings
- Telemetry data (tool names, model names, config fingerprints) injected into innerHTML/title attributes