# Security Agent Endpoint Resolution Investigation

**Issue (todo.md line 90):** Security agent does not use the caller's API endpoint when Security has no endpoints configured. It falls back to slot 0 (main/orchestrator thread). When A (synch slot 0) calls B (async slot 1), and B's `shell_cmd` triggers a Security check while A is still running, Security blocks on slot 0 that A holds → deadlock.

**Proposed solution (from todo):** If no specific endpoints are configured for an agent, it should use the **parent's endpoint** (since the parent is waiting anyway), then fall back to the global setting.

---

## 1. How Agents Resolve Their API Endpoints

**Core resolution function:** `APIRouter.get_endpoint_chain()` — `agent_cascade/api_router.py:930-1128`

The chain (tiers in priority order):
1. **Tier 1 — Agent-specific endpoints** (`api_router.py:981-999`): iterates `self.agent_priorities[normalized_agent_type]`, pulling enabled endpoint configs.
2. **Tier 2 — Caller endpoint inheritance** (`api_router.py:1004-1018`): **only when agent has no priorities (`if not configs and caller_agent_type`)**. If a `caller_agent_type` is passed, it inherits the caller's configured endpoints.
3. **Tier 3 — Last successful endpoint** (`api_router.py:1025-1044`): only for agent types that ever had priorities configured.
4. **Tier 4 — Global default** (`api_router.py:1073-1078`): `default_llm_cfg` always appended last. **This is the "slot 0 / main thread" default.**

**Key entry points that call endpoint resolution:**
- `get_llm_config(agent_type)` → `api_router.py:846` calls `get_endpoint_chain(agent_type)` (no caller context).
- `call_with_fallback(agent_type, ...)` → `api_router.py:1236-1240` pops `caller_agent_type` from kwargs and forwards it into `get_endpoint_chain(..., caller_agent_type=_caller_type)`. **This is the ONLY path that enables Tier 2 caller inheritance.**
- `get_effective_concurrency(agent_type)` → `api_router.py:773-807` (for slot scheduling).
- `_acquire_slot(agent_class, instance_name)` → `agent_pool.py:2453-2478` uses `router.get_llm_config(agent_class)` internally (Note: this calls `get_llm_config` WITHOUT caller context, so it always resolves to Tier 1 or global default — never caller inheritance).
- `agent_factory.py:193` `get_llm_config(agent_name)` (template LLM construction, no caller context).

### Why Security falls back to "slot 0"
- 'Security' has no `agent_priorities` configured (no Tier 1 endpoints).
- The Security instance's `parent_instance` is set to the **orchestrator session** (`Maine`/slot 0), not the actual calling agent (see §3).
- `execution_engine.py:3205-3214` builds `_caller_type` from `instance.parent_instance`.
- So Tier 2 inherits **Maine's** chain, which equals the **default** (`default_llm_cfg`) → slot 0.
- `_acquire_slot` and `call_with_fallback` then contend for slot 0's per-endpoint semaphore.

---

## 2. How the Security Agent Is Invoked (Call Path)

**Trigger path (shell_cmd → approval → security):**
1. `shell_cmd` tool → `operation_manager/shell.py:execute_shell_command()` → for non-safe commands calls `self.request_user_approval(agent_name=<instance>, tool_name='shell_cmd', ...)` (`operation_manager/shell.py:409-414`). Note: `agent_name` = **the actual calling agent instance** (e.g., async child B).
2. `request_user_approval` → `operation_manager/approval.py:84-152` — creates a `PendingApproval` with `agent_name=agent_name` (`approval.py:108`) and **blocks** the calling thread.
3. With Auto-Ask on, frontend `web_ui/app.js:3107` sends `{ type: 'ask_security', request_id, auto_apply: true }`.
4. `ws_handlers.handle_ask_security()` (`ws_handlers.py:979-994`) → instantiates `SecurityAdvisorHandler` → `run_check(data)`.
5. `security_handler.run_check()` (`security_handler.py:99-168`):
   - `instance_name = self.session.get('session_name', 'Maine')` (line 113) — **this is the orchestrator/main session, NOT the caller that requested the tool.**
   - `sec_target = data.get('target_agent') or instance_name` (line 117). Frontend does **NOT** send `target_agent` for `ask_security`, so `sec_target` = Maine.
   - Spawns `_run_check_worker(...)` on a daemon thread (line 161-168).
6. `_run_check_worker` → `_execute_check` (`security_handler.py:171-412`):
   - `sec_instance = engine._create_system_agent(agent_class='Security', instance_name=f'Security_{rid}', task=prompt, caller=self.session.get('session_name', 'Orchestrator'))` (`security_handler.py:260-265`) — **caller is Maine, not the tool-calling agent.**
   - `sec_instance._skip_slot_acquire = True` (`security_handler.py:314`) — skips Layer-1 EndpointScheduler slot in `engine.run()`.
   - Runs `for resp in engine.run(sec_instance)` (`security_handler.py:335`).

**Where the deadlock bites:**
- `_skip_slot_acquire=True` skips the scheduler-level slot, but Security's **LLM API call** still goes through `call_with_fallback` → acquires slot 0's **per-endpoint semaphore** (`api_router.py:1265-1339`).
- Because Security inherited Maine's (slot 0) chain, it'll contend with Maine (A) which holds slot 0. Deadlock.

---

## 3. Parent-Child Agent Relationship Tracking

- `AgentInstance.parent_instance: Optional[str]` field — `agent_instance.py:240` ("Who called this agent; None for root/main").
- Set on creation in `lifecycle_manager.find_or_create_instance()` → `lifecycle_manager.py:151-153, 176` (sets `inst.parent_instance = caller`).
- Also `agent_pool.create_instance()` (`agent_pool.py:818, 841, 850-851`).
- **Who is `caller` for Security?** In `_create_system_agent` (`execution_engine.py:4762-4826`), the `caller` arg is passed straight through to `lifecycle.find_or_create_instance(agent_class, instance_name, caller, ...)`, which sets `parent_instance=caller`. For Security, the caller is `self.session.get('session_name')` = **Maine**. This is the bug — Security's parent is the root orchestrator, not the async child that triggered the tool.
- **Endpoint inheritance reads parent:** `execution_engine.py:3205-3214`:
  ```python
  _caller_type = None
  if getattr(instance, 'parent_instance', None):
      _parent = self.pool.get_instance(instance.parent_instance)
      if _parent and hasattr(_parent, 'agent_class') and not getattr(_parent, 'is_terminated', False):
          _caller_type = _parent.agent_class
  ...call_with_fallback(agent_type, ..., caller_agent_type=_caller_type)
  ```

**Key conclusion:** Because Security's `parent_instance` = Maine (slot 0), Tier 2 inheritance yields Maine's chain = global default = slot 0. If instead `parent_instance` were set to the actual calling agent (B, slot 1), Tier 2 would inherit B's chain (slot 1) — matching the proposed solution.

Note: `ap['agent_name']` in the approval dict (`approval.py:108`) IS the correct calling instance (e.g., B). This field is readily available in `_execute_check` as `ap`.

---

## 4. Exact Code Locations For the Fix

### Primary fix — `agent_cascade/security_handler.py`

**A. Determine the true caller** — replace the session-based default with the approval's `agent_name` (the real tool requester). In `run_check()` (lines 113-121):
```python
instance_name = self.session.get('session_name', 'Maine')
```
should fall back to `ap['agent_name']` (the instance that requested the tool) and resolve `sec_target`/`sec_inst` from it. `ap['agent_name']` is available at line 137 (`ap = ap_list[0]`). Prefer `ap['agent_name']` over `data.get('target_agent') or instance` when available.

**B. Create the Security instance with the true caller** — `_execute_check()` line 260-264:
```python
sec_instance = engine._create_system_agent(
    agent_class='Security',
    instance_name=sec_state_key,
    task=prompt,
    caller=self.session.get('session_name', 'Orchestrator'),  # <-- WRONG parent
)
```
Change `caller=` to **`ap['agent_name']`** (guarded with a fallback to session name / logout if not found). This makes `instance.parent_instance = <true caller>` → Tier 2 endpoint inheritance resolves to the caller's chain (e.g., B's slot 1).

**C. Update slot-bypass logging** — lines 311-318: `caller_name_sec` and `caller_inst_sec` should use the same resolved caller so the `[SECURITY_SLOT_BYPASS]` log reflects the real caller.

**D. Guard** — If `ap['agent_name']` isn't an instance in the pool (`pool.get_instance(...)` returns None), fall back to session_name to avoid crashes.

### Support points (confirm no change needed)
- Tier 2 inheritance will only kick in when Security has no own endpoints — this is exactly the intended behavior and already implemented in `api_router.py:1004-1018` + `execution_engine.py:3205-3214`. Once `parent_instance` is the correct caller, no router change is required for the primary scenario.
- `_acquire_slot` at `agent_pool.py:2465` calls `get_llm_config()` **without** caller context → will still fall to global default. Since Security bypasses slot acquire (`_skip_slot_acquire=True`), `_acquire_slot` isn't invoked for the Security agent itself, so this is not the deadlock source. But if the same belong inheritance is ever wanted for a slot-acquiring nested agent, `_acquire_slot` would need the caller's api_base too (document for follow-up).

### Secondary/deep consideration
- If the parent endpoint chain is empty/disabled, Tier 2 yields nothing and the Security agent correctly falls to Tier 4 global default — acceptable final safety net.
- Ensure the caller's instance is `is_terminated` check (already in `execution_engine.py:3208`) doesn't drop a legitimately-waiting parent. A sleeping parent that holds a slot is not terminated, so it should pass.

---

## 5. Additional Findings

- **Frontend omits `target_agent`** for `ask_security` (`web_ui/app.js:3107, 3236`). This is why the handler falls back to session 'Maine'. The frontend sends `agent_name` only implicitly via the approval's stored `agent_name`. Thus `ap['agent_name']` is the reliable source.
- **Failure symptom** matches todo line 77: *"Timed out after 30s waiting for endpoint slot... Currently held by: phase1_reviewer_worker"* — an async child blocked waiting for slot 0 held by its parent.
- Prior lessons confirm the architecture: `lessons_slot_fix.md` and `lessons_parent_slot_fix.md` document the `_skip_slot_acquire` / slot-release machinery for nested Security/Compressor execution. See also `.agent_lessons/lessons_deadlock_fix.md`.

---

## Confidence Level

**High Confidence.** Endpoint inheritance mechanism, parent tracking, and slot-0 fallback are all verified in source. The fix location (Security `caller` → true calling agent) is directly evidenced.

## Open Questions / Risks
- Whether `ap['agent_name']` reliably equals the instance that issued the tool across ALL tool types (shell only verified). Requires a code trace of `agent_name` propagation for `async_shell.py` (`AsyncShellTask.agent_name`, `async_shell.py:311`) and grep/file ops — all use the caller's instance name, consistent.
- If the true caller is the orchestrator itself (root Agent A) in a synch-only call, the fix degenerates to current behavior (inherits Maine) — no regression.

## Suggested Next Actions
1. Apply fix in `security_handler.py`: derive caller from `ap['agent_name']` in `run_check()`/`_execute_check()`, pass it as the `caller` to `_create_system_agent`.
2. Update `[SECURITY_SLOT_BYPASS]` logging to use the true caller.
3. Add fallback guard when the caller instance isn't found in the pool.
4. Add a regression test simulating: A(sync slot0) calls B(async slot1), B shell_cmd triggers Security → assert Security's endpoint chain resolves to B's endpoint, not slot 0, and no deadlock/timeout.