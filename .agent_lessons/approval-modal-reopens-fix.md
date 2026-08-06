# Approval Modal Reopens After Approve/Reject — Fix Applied

**Date:** 2026-08-06  
**Related:** [[approval-window-not-closing-fix]] (prior manifestation), [[auto-ask-flash-bug-investigation]]

## Problem
After user clicks Approve or Reject on an approval card, the same card reappears and becomes impossible to dismiss.

## Root Causes (3 interrelated issues)

### 1. Backend omitted `approvals` key when empty
Both `build_state_from_pool()` and `build_stream_update_from_pool()` in api_integration.py used:
```python
{'approvals': pending_approvals} if pending_approvals else {}
```
This meant after approve/reject cleared the pending list, broadcasts had NO `approvals` key. Frontend's `Array.isArray(data.approvals)` check failed → stale state never cleared.

### 2. `_approval_loop` race with transient approvals
The loop only broadcast when `current_ids != known_ids`. If an approval was created and resolved within one 0.3s poll window, the loop never detected it → no clearing broadcast ever sent.

### 3. No defensive frontend cleanup
Frontend waited for backend response before removing approval from state — no instant feedback.

## Fix Applied

### api_integration.py (lines ~875, ~1051)
Always emit approvals key:
```python
'approvals': pending_approvals,  # always present, [] when empty
```

### api_server.py (_approval_loop, line ~670)
Track cumulative `seen_ids`, broadcast on new OR resolved IDs:
```python
new_seen = current_ids - seen_ids
resolved_ids = known_ids - current_ids
if new_seen or resolved_ids:
    seen_ids.update(current_ids)
    known_ids = current_ids.copy()
    await broadcast({'type': 'approvals', 'approvals': pending})
```

### web_ui/app.js
- `approveRequest()` / `rejectRequest()`: immediately filter out resolved request_id from state.approvals
- All approval handlers: explicit `'approvals' in data && Array.isArray(data.approvals)` check

## Why Flickering Wasn't an Issue
The "omit when empty" optimization was meant to prevent UI flicker, but the existing snapshot-based render skip (`bar._approvalSnapshotKey`) already handles this efficiently. When approvals go from non-empty to empty, the snapshot changes and renders once (correctly hiding). Subsequent empty broadcasts match the empty snapshot and skip DOM work.

## Key Takeaway
When frontend and backend share state via WS messages: always emit explicit values for shared keys, even when empty. Omitting keys breaks Array.isArray guards and creates contract mismatches. Let frontend snapshot logic handle flicker prevention instead of backend key-omission tricks.