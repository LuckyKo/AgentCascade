# Chat Input Cursor Fix - AC Unified Branch

## Problem
The `#chatInput` textarea in the web UI lost cursor position during typing and streaming. The caret would jump around instead of staying where you type.

## Root Causes & Fixes Applied

### 1. `autoResize()` function (line ~3090)
**Problem:** Set `height = 'auto'` then recalculated on every input event, causing layout shifts that moved the cursor position. No selection preservation.

**Fix:** 
- Save/restore `selectionStart`/`selectionEnd` before and after resize
- Skip height assignment if it hasn't changed (prevents unnecessary reflows)

### 2. Operation order in insert functions (lines ~3376, ~3533, ~3455)
**Problem:** In `insertImageMarkdown`, `insertAtCursor`, and `processDocFile`: focus/selection was set BEFORE calling `autoResize()`. Since autoResize resets height to 'auto' first, the layout shifted before the browser rendered the caret at the new position.

**Fix:** Call `autoResize()` FIRST, then set selection, then call `focus()`. Order: resize → select → focus.

### 3. Focus preservation during streaming (line ~2490)
**Problem:** During streaming (`stream_update` events), `renderSubAgents()` rebuilds message panels with DOM manipulation that can steal focus from `#chatInput`, causing the caret to jump mid-typing.

**Fix:** At start of `renderSubAgents()`: save whether chatInput is focused + its selection positions. At end: restore both if user was typing.

### 4. CSS word wrapping (line ~1532)
**Problem:** No explicit text wrapping rules on the textarea, which could cause line-break shifts during resize.

**Fix:** Added `overflow-wrap: break-word` and `word-break: break-word`.

## Files Modified
- `web_ui/app.js`: 6 changes across autoResize, renderSubAgents, insertImageMarkdown, insertAtCursor, processDocFile, input event listener
- `web_ui/styles.css`: 1 change to textarea CSS rule