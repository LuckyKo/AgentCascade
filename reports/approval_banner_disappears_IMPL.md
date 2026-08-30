# Approval Banner Disappears — Implementation Report (Option A)

**Date:** 2026-08-31
**Implementer:** approval_fix_coder
**Status:** Implemented + regression-tested. **NOT committed** (Reviewer to verify first).
**Related:** [[approval_banner_disappears_INVESTIGATION]] (root cause), `.agent_lessons/approval-modal-reopens-fix.md`

---

## 1. What Was Changed

**Option A — server-side, minimal.** Remove the `approvals` field from **stream-update** payloads only; keep it in **full-state** payloads. The dedicated `{'type':'approvals'}` WS message (broadcast by `_approval_loop`) is now the sole source of approval show/clear during active generation, so a stale stream tick can no longer clobber live approval state on the client.

### File: `agent_cascade/api_integration_pkg/state_builder.py`

**Change 1 — removed the now-unused computation** in `build_stream_update_from_pool()` (was line 487):
```diff
-    # Get pending approvals (only include if non-empty to prevent UI flickering)
-    pending_approvals = _get_approvals(pool)
+    # NOTE: pending approvals are intentionally NOT computed/included in stream updates.
+    # See the return-dict comment below for why the 'approvals' field is omitted here.
```

**Change 2 — removed the `approvals` key from the stream-update return dict** (was line 562; now a comment at lines 562-565):
```diff
     return {
         'instances': all_instances,
         'agent_instances': all_instances,
         'active_stack': active_stack,
-        'approvals': pending_approvals,
+        # Intentionally NO 'approvals' key here. Approvals are delivered exclusively via
+        # the dedicated {'type':'approvals'} WS message broadcast by _approval_loop; a
+        # stream tick built before an approval is registered would carry a stale [] and,
+        # if included, clobber live approval state on the client (banner disappears).
         'generating': True,
         ...
```

**Not changed (as required):**
- `build_state_from_pool()` — still computes `pending_approvals = _get_approvals(pool)` (line 301) and emits `'approvals': pending_approvals` (line 379). Initial load / refresh still needs it.
- `_get_approvals()` (lines 934-941) — untouched.
- `web_ui/app.js` — untouched. The client's guarded overwrite (`if ('approvals' in data && Array.isArray(data.approvals))`) now simply leaves `state.approvals` alone on stream ticks, since the key is absent.
- `approval.py`, `api_server.py`, `security_handler.py` — untouched.

**Net diff:** +6 / -3 lines in one file (verified via `git diff`).

### New file: `tests/test_state_builder.py`

New regression test module pinning the invariant (there was **no** existing test covering this race). Three tests, all using a MagicMock pool fake following the conventions in `test_refactor_name_resolution.py`:
- `test_stream_update_omits_approvals_key` — asserts `'approvals' not in build_stream_update_from_pool(...)`.
- `test_full_state_includes_approvals_key` — asserts `'approvals' in build_state_from_pool(...)` and that the value equals the live snapshot.
- `test_stream_and_full_state_diverge_on_approvals` — same pool, one payload has the key and the other does not (sharpest statement of the contract).

---

## 2. Verification Results

### `_approval_loop` broadcasts current list including empty (makes Option A safe)
`agent_cascade/api_server.py`, `_approval_loop()` **lines 748-766**. Key lines:
```python
755:                 pending = get_approvals()
756:                 current_ids = {a['request_id'] for a in pending}
757:                 new_seen = current_ids - seen_ids
758:                 resolved_ids = known_ids - current_ids  # IDs that were known but now gone
759:                 if new_seen or resolved_ids:
760:                     seen_ids.update(current_ids)
761:                     known_ids = current_ids.copy()
762:                     await broadcast({'type': 'approvals', 'approvals': pending})
```
When an approval is resolved, `resolved_ids` becomes non-empty → the loop broadcasts `{'type':'approvals','approvals': pending}` where `pending` is the **current** list — which is `[]` when all are resolved. So the client still receives clearing signals even though stream ticks no longer carry `approvals`. This matches the cumulative-`seen_ids` logic from the prior fix (`.agent_lessons/approval-modal-reopens-fix.md`), so transient approvals (created+resolved within one 0.3 s poll) are also covered: they appear in `seen_ids`, and their removal triggers a clearing broadcast.

### Client grep confirmation (`web_ui/app.js`)
- `case 'approvals':` handler **exists** at **lines 2230-2236** and calls `renderApprovals()` (line 2235). It is the dedicated approval show/clear path.
- The three overwrite sites all use the guard `if ('approvals' in data && Array.isArray(data.approvals))`:
  - `state` / `done` fall-through: **lines 1839-1842**
  - `stream_update`: **lines 2139-2142**
  - `approvals`: **lines 2232-2235**
- Because of the `'approvals' in data` guard, once stream ticks omit the key they no longer touch `state.approvals`. **No client code path relies on `stream_update` to set/clear approvals beyond the dedicated `approvals` message type.** (The only other local mutations are the defensive `approveRequest`/`rejectRequest` filters at lines 3769 & 3803, which remove the resolved `request_id` immediately for instant feedback — independent of server broadcasts.)

### Test runs
Command: `python -m pytest tests/test_api_endpoints.py tests/test_refactor_name_resolution.py tests/test_state_builder.py -p no:xdist -o addopts="" -v`
- **48 passed, 0 failed** (6 pre-existing warnings unrelated to this change).
  - `tests/test_state_builder.py`: **3/3 passed** (the new regression tests).
  - `tests/test_api_endpoints.py`: covers `build_state()`, WS-connect initial state, and approve/reject endpoints — all pass.
  - `tests/test_refactor_name_resolution.py`: exercises the edited `build_stream_update_from_pool()` path (`_calc_stream_token_stats` resolution) — passes, confirming the removed computation didn't break that path.

---

## 3. Deviations from Instruction

1. **Test file location/name.** The instruction referenced `agent_cascade/tests/test_state_builder.py` (and `test_approval.py`, `test_api_server.py`, `test_ws_handlers.py`). Those paths/files do **not** exist — the suite lives in `N:\work\WD\AgentCascade\tests/`, and there was no `test_state_builder.py`. I created `tests/test_state_builder.py` (new file, as permitted: "or a new test file if that's cleaner"). For the existing-test run I used the real equivalents present in this repo: `test_api_endpoints.py` (state/WS/approval endpoints) and `test_refactor_name_resolution.py` (the stream-update path). No dedicated `test_approval.py` / `test_api_server.py` / `test_ws_handlers.py` exist here to run.
2. **Removed the computation, not just the key.** Per the instruction's explicit allowance ("If so, also remove/avoid the now-unused computation"), I confirmed `pending_approvals` in `build_stream_update_from_pool()` was computed at line 487 and used *only* for the removed key (grep-verified), so I removed the dead `_get_approvals(pool)` call too. `_get_approvals()` itself and its use in `build_state_from_pool()` are untouched.

No other deviations. No client, approval.py, api_server.py, or security_handler.py changes were made.
