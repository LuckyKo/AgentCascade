# UI Streaming Sync Fixes — `web_ui/app.js`

Two frontend fixes for the AgentCascade streaming message-sync bugs. File: `N:\work\WD\AgentCascade\web_ui\app.js`.

## Fix 1 (PRIMARY): Allow full-snapshot frames to clear the resync gate

**Location:** `stream_update` merge logic, lines **2051–2065** (inside the `if (sa.is_partial)` branch).

### Before
```javascript
if (existing._needsResync) {
  continue; // skip delta splices until full frame arrives
}
```

### After
```javascript
if (existing._needsResync) {
  // Widen the gate: also let FULL-SNAPSHOT partial frames through. A frame is a
  // full snapshot when startIdx <= 0 (its messages array starts at index 0 = complete
  // history, not a tail). During active streaming even force_full recovery frames are
  // still is_partial=True (force_full only disables the tail cut, not the partial flag),
  // so gating on "non-partial" alone would freeze the panel for the whole turn after a
  // single dropped frame. A full snapshot safely replaces the entire list (no prefix
  // truncation risk), so it's safe to clear the gate and splice below. True tail frames
  // (startIdx > 0) remain gated — they must still wait for a real baseline resync.
  const _resyncStartIdx = hCount - sa.messages.length;
  if (_resyncStartIdx > 0) {
    continue; // skip true tail deltas until a full snapshot / force_full arrives
  }
  existing._needsResync = false; // full snapshot re-establishes the baseline
}
```

### How it addresses the bug
- The gate previously skipped **all** partial frames while `_needsResync` was set. But during active streaming, even `force_full` recovery frames arrive with `is_partial=True` (force_full only disables the tail cut, not the partial flag), so a single dropped frame froze the agent panel for the whole turn.
- The fix computes `startIdx = history_count - messages.length`. If `startIdx <= 0`, the frame is a **full snapshot** (complete history from index 0) and is allowed through; it clears `_needsResync` and falls into the existing merge path, which does a full replacement (`existing.messages.length = startIdx; push(...sa.messages)`).
- True tail frames (`startIdx > 0`) are still gated — they continue to wait for a real baseline resync. The `_needsResync` mechanism itself is preserved.

### Safety / edge cases
- A full snapshot always replaces the entire list, so there is no prefix-truncation risk (the concern that motivates gating tail deltas does not apply).
- When `startIdx === 0`, `existing.messages.length = 0; push(...sa.messages)` is a clean full replacement — correct.
- The stale-frame guard (`hCount < _lastHistoryCount`) still runs after the gate, so an old full snapshot can't downgrade newer state (it only syncs metadata).

## Fix 2: Optimistic local echo of user messages

**Location:** `sendMessage()` function, lines **5584–5598** (immediately after the final `send({...})` call for a normal send).

### Added
```javascript
// Optimistic echo: show the user's message in the active instance's panel immediately.
// Otherwise it only appears once the first stream_update frame from the backend arrives —
// which can be delayed (or temporarily gated by _needsResync), making the message look
// like it "disappeared". The server frame that carries this same message at the same index
// reconciles/replaces it via the positional splice in the merge logic, so no duplication.
const echoInst = state.subAgents[targetAgent];
if (echoInst && Array.isArray(echoInst.messages)) {
  const lastMsg = echoInst.messages.length > 0 ? echoInst.messages[echoInst.messages.length - 1] : null;
  // Dedup: skip if the server already echoed this exact user message (e.g. fast re-render).
  const lastText = lastMsg && typeof lastMsg.content === 'string' ? lastMsg.content : '';
  if (!(lastMsg && lastMsg.role === 'user' && lastText === messageText)) {
    const nextIndex = lastMsg && typeof lastMsg.index === 'number' ? lastMsg.index + 1 : (echoInst.messages.length);
    echoInst.messages.push({ role: 'user', content: messageText, index: nextIndex });
  }
}
```

### How it addresses the bug
- Previously the user's message only appeared once a backend `stream_update` frame carried it. If that frame was delayed or gated by `_needsResync`, the message looked like it "disappeared."
- The echo pushes `{ role: 'user', content: messageText, index }` into the active instance's `state.subAgents[targetAgent].messages` array — the same array `renderSubAgentPanel()` renders. `renderSubAgents()` is called on every stream_update tick and after tab switches, so the bubble appears promptly.
- Reconciliation: when the server frame carrying the same message at the same index arrives, the positional splice in the merge logic replaces it (no duplicate). An extra content-dedup guard skips the push if the last message is already an identical user message.

### Safety / edge cases
- Uses `role: 'user'` and the exact text the user typed (`messageText`, after mention-cleaning), routed to `targetAgent` — the instance actually being messaged (active tab's agent, or @mention target / session primary).
- Placed **only** in the normal-send path, NOT in the `if (state.generating)` async-injection early-return path (that path intentionally returns before this code), matching the existing behavior where injected messages are not locally echoed.
- The existing Agent-Messages tab echo (`addAgentMessage`/`appendAgentMessageToVisible`, ~L5552–5562) is untouched — that's a separate UI list, not the conversation panel.
- `nextIndex` uses the last message's `.index + 1` when present (matches backend absolute indexing), falling back to `messages.length`. If the echo's index is slightly off, the server frame's positional splice corrects it on arrival.

## Verification
- `node --check` equivalent: `syntax_check` on `app.js` → **valid JavaScript (6597 lines)**.
- `python -m pytest tests/test_state_builder.py -x -q --timeout=60` → backend unaffected (frontend-only change); see run output.

## Risks / notes
- Fix 2's echo is a transient optimistic entry; if the server never echoes it back for some reason, it would remain as an extra user bubble. In practice every accepted user message round-trips through `state_builder`, so reconciliation is expected.
- Both changes are frontend-only; no backend behavior changed.
