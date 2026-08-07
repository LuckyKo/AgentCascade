# Implementation Plan: Security Agent Endpoint Inheritance Deadlock Fix

**Todo:** #90 — "Security agent does not use the caller's API endpoint; falls back to slot 0 (main/orchestrator thread), causing deadlock when an async child's tool triggers a Security check while the synch parent still holds slot 0."

**Workflow stage:** bugfix (research → implementation_plan → plan_review → implement → code_review → fix_review_comments → testing → final_quality_review)

**Target file:** `agent_cascade/security_handler.py` (primary, single-file fix). No router or engine changes required.

---

## 1. Problem Statement

When an asynchronous child agent (B, holding slot N) triggers a tool that requires a Security advisor check (e.g. `shell_cmd`), the Security agent is created with its `parent_instance` set to the **orchestrator session** (`Maine`), not the actual calling agent (B). Because `Security` has no own endpoints configured, Tier-2 endpoint inheritance reads the parent's chain → resolves to `Maine`'s chain = global default = **slot 0**. The Security LLM call then contends for slot 0's per-endpoint semaphore, which the *synchronous* parent A is holding → **deadlock** ("Timed out after 30s waiting for endpoint slot... Currently held by: phase1_reviewer_worker").

---

## 2. Root Cause

Verified in source (see `investigation_report_security_agent_endpoint.md`):

1. `security_handler.run_check()` derives the caller from `self.session.get('session_name', 'Maine')` (`security_handler.py:113`) — this is the *orchestrator session*, **not** the agent that requested the tool.
2. `_execute_check()` creates the Security instance with `caller=self.session.get('session_name', 'Orchestrator')` (`security_handler.py:264`), so `Security.parent_instance = "Maine"` (slot 0).
3. The caller's instance name is **already available** in the approval dict as `ap['agent_name']` (`approval.py:186`, populated at `approval.py:108`). This field is the real tool requester (e.g. async child B) — but the fix never uses it.
4. Endpoint inheritance (`execution_engine.py:3256-3265`): `_caller_type = parent.agent_class` → forwarded to `call_with_fallback(..., caller_agent_type=_caller_type)`. With parent = Maine, Tier-2 (`api_router.py:1004-1018`) inherits Maine's chain = `default_llm_cfg` = slot 0 → `Security` contends with A for the slot-0 semaphore → deadlock.

**Fix principle:** make Security's `parent_instance` = the true calling agent (via `ap['agent_name']`). Tier-2 inheritance then resolves to the caller's chain (e.g. B's slot N), matching the slot the parent is already holding — eliminating contention. When the caller is the orchestrator itself, behavior degenerates correctly to current behavior (no regression).

---

## 3. Proposed Changes (single file: `agent_cascade/security_handler.py`)

### Change A — Resolve the true caller from `ap['agent_name']` (in `run_check`, near lines 113-137)

The current session-based `instance_name` is still needed as the *fallback and UI context*, but the caller used for the Security check must be derived from the approval.

**Before (`security_handler.py:113-121`):**
```python
instance_name = self.session.get('session_name', 'Maine')
inst = self.agent_pool.get_instance(instance_name) if self.agent_pool else None

sec_target = data.get('target_agent') or instance_name
sec_inst = (... )
```

**Plan:** keep `instance_name` as the session fallback. After `ap` is resolved (line 137), compute the true caller. Restructure so the worker receives the resolved caller name.

**Pseudo-code after `ap = ap_list[0]` (line 137):**
```python
# True caller = the agent that requested the tool (from the approval).
# Fall back to the session name if not present or not in the pool.
caller_agent = ap.get('agent_name') or instance_name
if self.agent_pool and self.agent_pool.get_instance(caller_agent) is None:
    caller_agent = instance_name  # not in pool → fall back to session/main

# sec_target should reflect the true caller, not just the session.
sec_target = data.get('target_agent') or caller_agent
sec_inst = (
    self.agent_pool.get_instance(sec_target)
    if (self.agent_pool and sec_target != caller_agent) else
    self.agent_pool.get_instance(caller_agent) if self.agent_pool else None
)
```

Then pass `caller_agent` (the **resolved true caller**) down into the worker thread — add it to the `threading.Thread(...)` args and to `_run_check_worker` / `_execute_check` signatures (replacing/augmenting the current `instance_name` argument).

> Dispatch note: `_run_check_worker(..., instance_name, ...)` at `security_handler.py:163` and `_execute_check(..., instance_name, ...)` at `:183` currently receive the session name. Change the argument meaning to carry the **resolved caller** (`caller_agent`), and use it for the Security parent. Keep `instance_name` (session) separately only where pure UI/session context is needed.

### Change B — Create the Security instance with the true caller (`_execute_check`, line 260-264)

**Current (`security_handler.py:260-265`):**
```python
sec_instance = engine._create_system_agent(
    agent_class='Security',
    instance_name=sec_state_key,
    task=prompt,
    caller=self.session.get('session_name', 'Orchestrator'),   # WRONG parent
)
```

**Planned:**
```python
sec_instance = engine._create_system_agent(
    agent_class='Security',
    instance_name=sec_state_key,
    task=prompt,
    caller=caller_agent,   # resolved true caller (ap['agent_name'] or fallback)
)
```

This makes `inst.parent_instance = <true caller>` → `execution_engine.py:3256-3264` builds `_caller_type` from the true caller → Tier-2 inheritance (`api_router.py:1004-1018`) resolves to the caller's chain. No router change needed.

### Change C — Update slot-bypass logging to reflect the true caller (lines 311-318)

**Current (`security_handler.py:311-312`):**
```python
caller_name_sec = self.session.get('session_name', 'Orchestrator')
caller_inst_sec = self.agent_pool.get_instance(caller_name_sec) if caller_name_sec else None
```

**Planned:** use the same `caller_agent`:
```python
caller_name_sec = caller_agent        # true caller, not the session
caller_inst_sec = self.agent_pool.get_instance(caller_name_sec) if caller_name_sec else None
```
The `[SECURITY_SLOT_BYPASS]` debug log (lines 315-318) then reports the real caller.

### Change D — Guard (implicit in Change A)

If `ap['agent_name']` is missing (empty/None) or not an instance in the pool, fall back to `session_name`. This prevents crashes for approval records that lack `agent_name` (edge cases — see §4).

---

## 4. Edge Cases to Handle

1. **`ap['agent_name']` not in the pool** (stale approval, terminated instance, or name mismatch). → Fall back to `session_name`. Covered by Change A's `get_instance(caller_agent) is None` guard.
2. **`ap['agent_name']` missing/None** (approval has no agent_name). → `ap.get('agent_name') or instance_name` yields the session fallback.
3. **Caller is the orchestrator itself** (root Agent A, synchronized-only path). The true caller *is* `Maine` → inheritance resolves to current behavior. No regression (confirmed by investigation §5).
4. **Caller instance terminated** (`is_terminated=True`). `execution_engine.py:3259` already guards `not getattr(_parent, 'is_terminated', False)` — if terminated, `_caller_type` stays `None` and Security falls through to Tier-4 global default. Acceptable safety net; no hang (a terminated parent holds no slot).
5. **Parent's configured endpoint chain empty/disabled** → Tier-2 yields nothing → correctly falls to Tier-4 global default. Acceptable final safety net (investigation §4, secondary).
6. **Non-shell tools** (file ops, async_shell). All tool types pass the caller's `instance_name` into `request_user_approval` consistently (verified in investigation §5 — `AsyncShellTask.agent_name` etc.). The `ap['agent_name']` source is uniform.
7. **`_acquire_slot` (agent_pool.py:2453-2478)** calls `get_llm_config()` *without* caller context. Not a concern here: Security sets `_skip_slot_acquire=True` (`security_handler.py:314`), so `_acquire_slot` is never invoked for the Security instance itself. Document as a separate follow-up if inherited endpoints are ever wanted for slot-acquiring nested agents.

---

## 5. Testing Strategy

**Primary regression test (deadlock/isolation):**
- Simulate A (sync, holds slot 0) → calls async child B (slot 1) → B's `shell_cmd` triggers a Security advisor check while A is still running.
- Assert: (1) Security's `parent_instance` == B (not Maine); (2) Security's resolved endpoint chain == B's endpoint (slot 1), **not** slot 0; (3) the check completes without "Timed out waiting for endpoint slot" and without blocking on slot 0.

**Secondary tests:**
1. **Sync-only call** (root agent A directly invokes a security-requiring tool): verify no behavior regression — caller resolves to Maine, Security still runs on slot 0 as before, no hang.
2. **Unresolvable caller:** force `ap['agent_name']` to a non-existent/terminated instance name → verify fallback to `session_name` and graceful degradation (no crash, no `KeyError`).
3. **Approval without `agent_name`:** construct an approval dict with `agent_name` missing/None → verify fallback and no exception.
4. **Logging:** verify `[SECURITY_SLOT_BYPASS]` and `[SECURITY] ...` logs report the **true caller** (B), not Maine.
5. **Concurrency/duplicate guard:** confirm the active-checks `request_id` dedup (lines 141-146) and `security_check_semaphore` (line 125-325) still function unchanged.

**Note re: existing callers** — `_run_check_worker` and `_execute_check` signature changes must keep backward-calls consistent (thread args + internal call). Any existing call sites/tests that invoke `_execute_check` directly (if any) must be updated to pass the resolved caller.

---

## 6. Regression Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Change to `run_check`/`_execute_check` signatures breaks a direct caller or unit test | Low | Grep for `_run_check_worker(`/`_execute_check(` call sites before implementing; update any test fixtures. |
| `ap` may not contain `agent_name` in some non-shell approval path (regression to poorer inheritance) | Low-Medium | Guarded `ap.get(...) or session`; verification of other tool paths (async_shell, file ops) confirms consistency. |
| Security inheriting a *narrower*/different endpoint set than before changes behavior of the security LLM call | Low | Intended fix; Tier-1 Security-specific config (if ever added) still takes precedence; Tier-4 global default is a hard safety net. |
| Parallel Security checks race over the caller lookup | Low | Lookup/replace happen within the existing `sec_lock`/active-checks-sectional guard; keep the caller-arg computation deterministic per `request_id`. |
| Slot-0 workers that legitimately trigger Security while holding slot 0 still fall back to slot 0 | Medium | Documented; only applicable when the true caller IS slot-0 (degenerates to current, no new deadlock). Out of scope. |

---

## 7. Rollback Plan

- The fix is **single-file** (`security_handler.py`) and additive: it derives `caller_agent` from `ap['agent_name']` and passes it as the `caller` to `_create_system_agent`, with a fallback to the prior session-name behavior.
- **Rollback:** revert `security_handler.py` to the pre-change state (restore `caller=session.get('session_name', 'Orchestrator')` at line 264 and the session-based `instance_name` derivation). Because the fallback path is preserved, removing the fix returns exactly the prior (buggy but non-crashing) behavior.
- No schema/DB/migration/config changes; no frontend change; no route change. No data migration needed.
- After rollback, verify with the primary regression test that the truly-deadlocking scenario re-occurs as expected (confirming the fix was the active variable), then plan a follow-up.

---

## Confidence Level

**High** — root cause and fix location are directly evidenced in the running source (inheritance read at `execution_engine.py:3256-3264`, wrong `caller` at `security_handler.py:264`, correct caller available at `approval.py:108/186`, and the existing `_skip_slot_acquire` bypass at `security_handler.py:314`).

## Open Questions (for coder/implementer)

- Are there any direct callers of `_execute_check` / `_run_check_worker` besides `run_check` (unit tests, other handlers)? Grep before changing signatures.
- Confirm `ap['agent_name']` case/format exactly matches `pool.get_instance(...)` indexing (curated); a mismatch triggers the fallback guard, which is safe but should be tested via §5.2.