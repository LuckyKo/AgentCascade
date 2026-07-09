# Snapshot Comparison & DOM Diff Analysis — app.js (lines 2453–2629)

## Overview of the Mechanism

The approval rendering system uses a two-phase optimization:
1. **Snapshot key** (`_approvalSnapshotKey`) — produces a JSON string from request IDs, active security checks, and security response keys to detect if anything changed.
2. **DOM diff** (Fix 2) — maps existing DOM cards by `data-request-id`, reuses them for unchanged approvals, removes stale ones, creates new ones.

---

## Finding 1: Snapshot Key Ignores Approval Content Changes

### Code (line 2456)
```js
const ids = (state.approvals || []).map(a => a.request_id);
```

The snapshot key only captures `request_id` values. It does NOT capture:
- `tool_args`, `tool_name`, `agent_name`, `justification` — any of these can change for the same request_id without triggering a re-render.
- The **order** of approvals (though this is minor since array order maps to DOM order).

### Impact
If the backend sends an updated approval object with the same `request_id` but different content (e.g., enriched tool_args after additional processing), the snapshot key will match and the card won't re-render. The user sees stale data.

### Likelihood
**Medium** — In practice, approvals are typically created once and stay static until approved/rejected. But if the backend ever enriches an approval in-place (e.g., adds a justification after tool execution starts), this becomes invisible to the user.

### Recommendation
Include a content hash or at least `tool_name` + `justification` summary:
```js
const ids = (state.approvals || []).map(a =>
  `${a.request_id}|${a.tool_name}|${(a.justification||'').slice(0,50)}`
);
```

---

## Finding 2: Security Response Handler Skips Re-Render

### Code (lines 1533–1556)
```js
case 'security_response': {
  state.activeSecurityChecks.delete(request_id);
  state.securityResponses[request_id] = { response, verdict, reason };
  // ... QoL auto-fill logic ...
  break;
}
```

The `security_response` case mutates state but does NOT call `renderApprovals()`. It relies on a subsequent `state_change` or `approvals` broadcast to trigger the render.

### Race Condition Scenario
1. User clicks "Ask Security" → `activeSecurityChecks.add()` happens, snapshot key changes next render.
2. Backend sends `security_response` → state updated.
3. If no `approvals` update follows immediately (backend processes auto-apply asynchronously), the security response HTML won't appear until the next periodic heartbeat or unrelated state change triggers a re-render.

### Evidence
The comment at line 1538–1539 acknowledges this:
```js
// (rendered via renderApprovals() triggered by state change broadcast)
```

But there's no guarantee that broadcast arrives promptly. The user sees "⏳ Checking..." indefinitely if the heartbeat interval is long (~2-5s).

### Recommendation
Add an explicit `renderApprovals()` call after updating security response state:
```js
case 'security_response': {
  const { request_id, response, verdict, reason } = data;
  state.activeSecurityChecks.delete(request_id);
  state.securityResponses[request_id] = { response, verdict, reason };
  
  renderApprovals(); // ← Force immediate re-render to show security response
  // ... rest of QoL logic ...
}
```

---

## Finding 3: Early Returns Skip Cleanup of Stale State

### Code (lines 2467–2505)
The cleanup at lines 2467–2478 runs BEFORE early returns. Good. But the early return paths have their own issues:

#### 3a. Auto-Security Path (line 2488–2504)
```js
if (state.autoSecurity) {
  const pending = (state.approvals || []).filter(ap => !state.activeSecurityChecks.has(ap.request_id));
  if (pending.length > 0) {
    state.activeSecurityChecks.add(ap.request_id);
    send({ type: 'ask_security', request_id: ap.request_id, auto_apply: true });
  }
  bar.style.display = 'none';
  return;
}
```

**Issue:** When Auto-Ask is active, the bar is hidden (`display: none`). But `state.activeSecurityChecks` and `state.securityResponses` are populated. If the user toggles Auto-Ask OFF mid-processing, the cleanup at lines 2467–2478 runs correctly. However, if there are multiple pending approvals and the first one's security check is in-flight, toggling off will keep that check active (line 985 comment: "Don't clear activeSecurityChecks — let in-flight checks complete normally"). This is intentional but means a security response could appear for an approval the user didn't explicitly ask about.

#### 3b. AFK Path (lines 2507–2521)
```js
const pending = [...state.approvals];
state.approvals = [];
bar.style.display = 'none';
pending.forEach(ap => { ... });
return;
```

**Issue:** `state.approvals` is cleared BEFORE sends complete. If a new approval arrives between the clear and the send completing, it's fine (no overlap). But if the AFK toggle fires twice rapidly (e.g., user clicks fast), the second call will see an empty array — no bug here, just a no-op. However, `_lastApprovalSnapshotKey` is NOT reset after clearing approvals. The next time new approvals arrive, the snapshot key WILL change and render correctly. So this is fine.

### Recommendation for 3a
No code fix needed, but add a comment explaining the behavior:
```js
// Keep bar hidden during auto-ask; security responses will be shown
// if user toggles Auto-Ask off before backend clears the approvals.
```

---

## Finding 4: Stale DOM Data from Reused Cards

### Code (lines 2610–2620)
```js
const existing = existingCards.get(ap.request_id);
if (existing) {
  existing.innerHTML = cardContent; // Full innerHTML replacement on reused cards
} else {
  const card = document.createElement('div');
  // ...
  bar.appendChild(card);
}
```

**Issue:** Reused cards get their `innerHTML` fully replaced, which is correct. BUT the stale-card removal loop (lines 2624–2628) only removes cards whose IDs aren't in `currentIds`. If an approval's content changed but kept the same ID, the card IS reused and updated correctly via `innerHTML = cardContent`.

**However**, there's a subtle issue: the `existingCards` map is built BEFORE any processing. If two approvals share the same `request_id` (backend bug or duplicate), only the LAST one wins in both `currentIds` and the DOM loop. The first approval's card content gets overwritten by the second, then the stale removal doesn't catch it because the ID exists in `currentIds`.

### Recommendation
Add a deduplication guard:
```js
const currentIds = new Set();
for (const ap of state.approvals) {
  if (currentIds.has(ap.request_id)) {
    logger.warn(`Duplicate approval request_id: ${ap.request_id}`);
    continue; // Skip duplicates, keep first occurrence's card
  }
  currentIds.add(ap.request_id);
  // ... rest of rendering logic
}
```

---

## Finding 5: Race Condition Between Snapshot Check and Content Rendering

### Code Flow
```
1. WebSocket message arrives → state.approvals = data.approvals (line 1161/1434)
2. renderApprovals() called immediately
3. Inside renderApprovals():
   a. Cleanup stale security checks/responses (lines 2467-2478)
   b. Snapshot comparison (line 2524-2528) — EARLY RETURN if same
   c. DOM diff and update (lines 2534-2628)
```

**Race scenario:** Two WebSocket messages arrive in quick succession:
1. Message A: `state.approvals = [approval_1]` → render starts, snapshot key computed as "A"
2. Before render finishes DOM updates, Message B arrives: `state.approvals = [approval_1, approval_2]`
3. Since both messages update `state.approvals` synchronously (JS is single-threaded), the second assignment happens before step 2b runs.

**Actually**, this can't happen because JS event loop processes one message at a time. The real race is:

1. Message A updates state, calls render → snapshot "A", DOM updated
2. User clicks "Ask Security" on approval_1 → `activeSecurityChecks.add()` (line 2637)
3. Before the next WebSocket heartbeat, the button shows "⏳ Checking..." via inline DOM manipulation (line 2640)
4. The snapshot key NOW differs from `_lastApprovalSnapshotKey`, but no render is triggered until the next state_change broadcast

This is actually handled by the inline DOM mutation at line 2639-2641, which directly modifies the button. So this is a deliberate optimization, not a bug. But it creates a **divergence**: the DOM state and snapshot key are out of sync until the next render cycle.

### Recommendation
The `askSecurity` function could call `renderApprovals()` to keep things in sync:
```js
window.askSecurity = function (requestId, btn) {
  state.activeSecurityChecks.add(requestId);
  delete state.securityResponses[requestId];
  send({ type: 'ask_security', request_id: requestId, auto_apply: false });
  renderApprovals(); // ← Keep DOM in sync with state immediately
};
```

---

## Finding 6: `_lastApprovalSnapshotKey` Never Reset on Session Switch

The variable is declared at module scope (line 2462):
```js
let _lastApprovalSnapshotKey = '';
```

It's never reset when switching sessions or agents. If a previous session had approvals with IDs `[1,2,3]`, and the new session has different approvals that happen to produce the same snapshot key string (unlikely but possible with overlapping request_id patterns), the render would be skipped.

### Recommendation
Reset `_lastApprovalSnapshotKey` in session initialization/switch code:
```js
// In your session switch handler:
_lastApprovalSnapshotKey = '';
state.activeSecurityChecks.clear();
state.securityResponses = {};
```

---

## Summary Table

| # | Issue | Severity | Location | Fix Difficulty |
|---|-------|----------|----------|---------------|
| 1 | Snapshot ignores approval content changes | Low-Medium | L2456 | Easy |
| 2 | `security_response` skips explicit re-render | Medium | L1533-1556 | Trivial |
| 3a | Auto-ask hidden bar, in-flight checks linger | Low (by design) | L2488-2504 | Comment only |
| 4 | Duplicate request_id handling missing | Low | L2547-2621 | Easy |
| 5 | DOM/state divergence after `askSecurity` click | Medium | L2636-2643 | Trivial |
| 6 | `_lastApprovalSnapshotKey` not reset on session switch | Low | L2462 | Easy |

## Priority Fixes (Recommended Order)
1. **Fix #2** — Add `renderApprovals()` in `security_response` handler (highest impact, trivial change)
2. **Fix #5** — Call `renderApprovals()` after `askSecurity` click for state consistency
3. **Fix #1** — Enrich snapshot key with content hash for robustness
4. **Fix #6** — Reset snapshot key on session switch