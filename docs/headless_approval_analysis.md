# Headless Mode Approval System — Deep Dive Analysis

**Date**: 2026-07-10
**Scope**: How approvals work when the API server runs without a connected WebUI client.

---

## 1. Architecture Overview

The approval system has **three layers**:

```
Tool Call → request_user_approval() [blocking] ← user_approve()/user_reject()
     │                                         ↑
     │                                  WebSocket / REST API
     └── (optional) ─→ Security Advisor ─→ verdict parse ──┘
```

### Key Files and Line Numbers

| File | Role | Lines |
|------|------|-------|
| `agent_cascade/operation_manager/approval.py` | Core approval types, blocking API, timeout logic | 1-194 |
| `agent_cascade/operation_manager/__init__.py` | OperationManager init (default timeout = 300s) | 56-82 |
| `agent_cascade/api_server.py` | WebSocket broadcast loop, REST endpoints, approval polling | 757-934 |
| `agent_cascade/ws_handlers.py` | Approve/reject/dispatch handlers | 690-726 |
| `agent_cascade/security_handler.py` | Security advisor lifecycle with own timeout | 18-674 |
| `agent_cascade/constants.py` | Tool approval sets | 18-50 |

---

## 2. Approval Timeout Chain — Full Flow

### Step 1: Default Values (OperationManager.__init__)
**File**: `operation_manager/__init__.py`, lines 76-78
```python
self.enable_timeout: bool = True
self.approval_timeout_seconds: int = 300  # 5 minutes default
```

### Step 2: Blocking Wait Loop (request_user_approval)
**File**: `operation_manager/approval.py`, lines 118-147

The blocking loop works as follows:
```python
# Line 119: Timeout value depends on enable_timeout flag
timeout_val = self.approval_timeout_seconds if self.enable_timeout else 3600

while time.time() - start_time < timeout_val:   # Line 123
    if self.agent_pool and self.agent_pool.stopped:  # Line 126 — stop check
        break
    if approval.event.wait(timeout=0.1):         # Line 132 — poll every 100ms
        got_response = True
        break

# Lines 137-138: Cleanup pending entry
with self._lock:
    self.pending.pop(request_id, None)
```

**VERDICT**: ✅ The timeout chain is correct. On timeout:
- The approval IS properly removed from `self.pending` (line 138).
- The return value is `(False, "User is AFK...")` (lines 145-147).
- The polling interval was reduced to 0.1s for responsive stop detection (MINOR-3 FIX comment at line 131).

### Step 3: Cleanup Guarantee
**File**: `operation_manager/approval.py`, lines 137-138

The `self.pending.pop(request_id, None)` happens in ALL exit paths:
- After approval/reject (already popped by `user_approve`/`user_reject` at lines 157/170)
- After timeout (popped here at line 138)
- The `.pop()` is idempotent — if already removed, it silently returns None.

---

## 3. Security Advisor Timeout Chain

### Step 4: Security Advisor Has Its Own Timeout
**File**: `security_handler.py`

| Constant | Value | Set At | Purpose |
|----------|-------|--------|---------|
| `SECURITY_ADVISOR_TIMEOUT_SECONDS` | **180s** (3 min) | `approval.py:47` | Kill security advisor if it takes too long |
| `SECURITY_ADVISOR_WARNING_SECONDS` | **120s** (2 min) | `approval.py:48` | Nudge the security agent via message queue |

### Execution Flow:
1. **Engine loop** at lines 326-337 checks elapsed time every iteration
2. On timeout → sets `sec_timeout_reached = True`, breaks from engine
3. **Result handling** at `_handle_result()` (line 493):
   - Timeout path: calls `user_reject()` + broadcasts to UI (lines 511-564)
   - Verdict path: auto-applies via `user_approve()` or `user_reject()` (lines 576-586)

### ⚠️ RACE CONDITION #1: Dual Timeout Overlap

The security advisor timeout (180s) and the approval timeout (300s) can collide:

**Scenario**: 
1. Tool call triggers at T=0
2. Security check starts, takes 190 seconds
3. At T=190 → security times out, calls `user_reject(rid)` at line 538
4. But the approval was only removed from pending at that point
5. Meanwhile, the approval timeout is still counting to 300s

**This is NOT a problem because**: The security check and the approval wait share the SAME blocking thread. The `request_user_approval()` loop blocks until either:
- The event is set (by `user_approve`/`user_reject`)
- The timeout expires at 300s
- The pool stops

The security advisor's `user_reject()` call sets the event, so the approval unblocks. No race here — the security reject IS the response.

### ⚠️ RACE CONDITION #2: Security Timeout + Approval Timeout Collision

If the security advisor takes 180s AND then the user is AFK for another 120s:
- The security check at T=190 calls `user_reject(rid)` 
- But if `user_reject` misses (approval already timed out at line 138), it returns "not found" at line 578

**File**: `security_handler.py`, lines 528-538:
```python
reject_msg = "SECURITY ADVISOR TIMEOUT: ..."
self.agent_pool.operation_manager.user_reject(rid, reject_msg)
```

The `user_reject` at line 167 silently handles the miss (returns an error string but doesn't crash). So this is handled gracefully.

---

## 4. Approval Broadcast Mechanism

### The _approval_loop() Polling Loop
**File**: `api_server.py`, lines 757-771

```python
async def _approval_loop():
    known_ids: Set[str] = set()
    while True:
        await asyncio.sleep(0.3)              # Poll every 300ms
        pending = get_approvals()
        current_ids = {a['request_id'] for a in pending}
        if current_ids != known_ids:          # Diff-based broadcast
            known_ids = current_ids.copy()
            await broadcast({'type': 'approvals', 'approvals': pending})
```

**Key insight**: This loop polls the `pending` dict and broadcasts changes. In headless mode with no WebSocket clients, `broadcast()` sends to an empty set — harmless but wasteful CPU.

### REST API Endpoints (Alternative Path)
**File**: `api_server.py`, lines 922-934:
```python
POST /api/approve/{request_id}   → user_approve(request_id)
POST /api/reject/{request_id}?reason=... → user_reject(request_id, reason)
```

These work independently of WebSocket — they're usable by any HTTP client.

---

## 5. Auto-Approval Mechanisms (3 Types Found)

### Type A: Agent File Ownership Auto-Approve
**File**: `operation_manager/approval.py`, lines 66-80

```python
def _is_auto_approved(self, path, agent_name, creating_new=False):
    if creating_new:
        resolved = self._resolve_path(path, mode="rw")
        if not resolved.exists():
            return True   # New file — auto-approved
    
    resolved = self._resolve_path(path, mode="rw")
    owner = self.file_ownership.get(str(resolved))
    return owner == agent_name  # Owned files — auto-approved
```

**Applies to**: `write_file`, `edit_file`, `re_indent` (checked before calling `request_user_approval`).

### Type B: Safe Shell Command Auto-Approve  
**File**: `operation_manager/shell.py`, lines 253-257

Read-only filesystem commands (`ls`, `cat`, `grep`, etc.) are auto-approved without user intervention.

### Type C: Security Advisor Auto-Apply Mode
**File**: `security_handler.py`, lines 566-596

When the WebUI sends `ask_security` with `"auto_apply": true`:
1. Security advisor runs and parses [YES]/[NO] verdict
2. Verdict is automatically applied via `user_approve()` or `user_reject()`
3. No human confirmation needed for the final step

---

## 6. WebSocket Disconnection Handling

### Connection Tracking
**File**: `api_server.py`, lines 427, 1112, 1140-1146:

```python
ws_connections: Set[WebSocket] = set()   # Line 427
# ...
@app.websocket("/ws/chat")
async def ws_chat(websocket):
    await websocket.accept()
    ws_connections.add(websocket)         # Line 1112
    
    try:
        while True:
            raw = await websocket.receive_text()
            # ... process messages
    except WebSocketDisconnect:           # Line 1140
        pass
    finally:
        ws_connections.discard(websocket)  # Line 1146
```

### What Happens on Disconnect:
1. The WebSocket connection is removed from `ws_connections` (line 1146).
2. **No approval state is reset** — pending approvals remain in the `self.pending` dict.
3. The `_approval_loop()` continues polling and broadcasting to an empty set.
4. Approvals sit until they timeout at 300s.

### ⚠️ RACE CONDITION #3: Disconnection Mid-Approval

If a WebSocket client disconnects while an approval is pending:
- No new approvals are broadcast (no one listening).
- The agent thread blocks on `approval.event.wait()` for up to 300s.
- **No mechanism exists to detect "headless" state and auto-approve.**

**This is the core headless-mode problem**: There's no feedback loop that says "no clients connected → auto-approve all pending."

---

## 7. Tools That Require Approval

**File**: `constants.py`, lines 18-25:
```python
ALL_USER_APPROVAL_TOOLS = frozenset({
    'shell_cmd',      # Shell commands (except safe read-only ones)
    'code_interpreter',  # Python code execution
    'write_file',     # File creation/overwrite
    'edit_file',      # File editing
    'delete_file',    # File deletion
    'copy_file',      # File/directory copying
})
```

---

## 8. Summary of Findings

### ✅ What Works Well
1. **Timeout chain is clean**: Approvals properly reject AND clear themselves on timeout (approval.py:137-147).
2. **Security advisor has its own timeout** (180s) to prevent cascading AFK waits.
3. **File ownership auto-approval** reduces unnecessary approval prompts for agent-owned files.
4. **Safe shell commands** are auto-approved without any blocking.
5. **Thread-safe**: Lock-based pending dict access, threading.Event for blocking/unblocking.

### ⚠️ Issues Found

| # | Issue | Severity | Location |
|---|-------|----------|----------|
| 1 | No "headless mode" detection — approvals sit until timeout (300s) with no clients | Medium | `api_server.py:757-771` |
| 2 | `_approval_loop()` polls every 300ms even when no WebSocket clients exist (wasted CPU) | Low | `api_server.py:762` |
| 3 | No "auto-approve-all" flag exists — would need to be added for true headless operation | Medium | N/A |
| 4 | Security advisor timeout (180s) + approval timeout (300s) = up to 5 minutes of total wait per tool call in worst case | Low | Combined: `approval.py:78` + `security_handler.py:284-338` |
| 5 | Disconnection doesn't trigger pending approval cleanup — they just sit there until timeout | Low | `api_server.py:1140-1146` |

### 💡 Recommendations for True Headless Mode

1. **Add a headless flag**: A simple `auto_approve_all` boolean on the OperationManager that skips `request_user_approval()` entirely.
2. **Client-count aware timeout**: Reduce timeout when no WebSocket clients are connected (e.g., 30s instead of 300s).
3. **Disconnect-triggered auto-approve**: When all WS clients disconnect, auto-approve pending approvals with a "headless" reason.
4. **REST-based approval polling**: The REST endpoints already exist (`/api/approve/{id}`, `/api/reject/{id}`) — these could be used by an external headless controller.

---

## 9. Call Chain Diagram

```
Tool Invocation (e.g., write_file)
    │
    ├─→ _is_auto_approved()? ──Yes──→ Execute directly
    │       │
    │      No
    │       ↓
    └─→ request_user_approval()          [approval.py:84]
            │
            ├─→ Creates PendingApproval, stores in self.pending  [line 106-116]
            │
            ├─→ Blocks on approval.event.wait(0.1)              [line 132]
            │       │
            │       ├── Path A: user_approve(request_id)        [ws_handlers.py:697]
            │       │         → Sets event, pops from pending   [approval.py:154-165]
            │       │         → Returns (True, reason)
            │       │
            │       ├── Path B: user_reject(request_id)         [ws_handlers.py:708]
            │       │         → Sets event, pops from pending   [approval.py:167-178]
            │       │         → Returns (False, reason)
            │       │
            │       ├── Path C: Security advisor auto-apply     [security_handler.py:579]
            │       │         → Calls user_approve/reject internally
            │       │
            │       └── Path D: Timeout after 300s              [approval.py:145-147]
            │                 → Pops from pending               [line 138]
            │                 → Returns (False, "User is AFK")
            │
            └─→ Returns (approved, reason) to caller
```