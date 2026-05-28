# Phase 7 — Migration Items Implementation Notes

**Date:** 2026-05-28  
**Items Completed:** 7, 9, 10, 11, 12, 13 (Item 2 deferred to Phase 8)

---

## Item 7: /compress Manual Command Handling

**Location:** `execution_engine.py` — `_handle_compress_command()` method + call in `_pre_llm_checks()`  
**Old code ref:** `agent_orchestrator.py:1017-1078`

Three-step flow: preview (dry_run) → user approval → apply with precomputed_summary.
- Fraction clamped to 0.1–0.9, default 0.5
- Does NOT pop command from history (traceability)
- `precomputed_summary` passed as kwarg to compress_tool.call(), NOT in JSON params

---

## Item 9: Multimodal Image Propagation

**Location:** `_create_and_run_agent()` before building task_msg  
**Old code ref:** `agent_orchestrator.py:1893-1927`

Scans caller's conversation for images (type=='image') in list-type content.
Includes images referenced by basename in task text, plus all images from last user message.

---

## Item 10: Message Pool Validation After Compression

**Location:** Module-level `validate_message_pool()` function  
**Old code ref:** `agent_orchestrator.py:354-402` + calls at 1145, 1198, 1605

Called after forced compression (with recovery), agent-triggered compress_context, and /compress.
Checks: non-empty, first msg is SYSTEM, <30% duplicates, valid roles.

---

## Item 11: Logger Sync After Forced Compression

**Location:** Inside `_force_compression()`, after validation  
**Old code ref:** `agent_orchestrator.py:1168-1176`

Calls `log_inst.update_history(conv)` to sync logger's internal data["history"].
Only needed for forced compression (not agent-triggered — compress_context handles its own sync).

---

## Item 12: Sub-Agent WebUI State Updates

**Location:** Inside `_create_and_run_agent()` execution loop  
**Old code ref:** `agent_orchestrator.py:1956-1968`

Initial state set before execution. Updated each iteration with current_conv + final_resp.
Thread-safe via _state_lock. Wrapped in try/except (must never break execution).

---

## Item 13: Endpoint Scheduling for Parallel Agents

**Location:** `_acquire_slot()` method on ParallelAgentManager + submit_task integration  
**Old code ref:** `agent_orchestrator.py:287-351`

_acquire_slot matches DESIGN_REWRITE.md §3.3. Acquisition before thread pool submit.
endpoint_release in finally block guarantees cleanup.

---

## Reviewer Findings (to be addressed)

### CRITICAL:
1. compress_tool.call() may fail silently if agent_pool not set — need to pass agent_obj=instance or handle error with user-visible feedback
2. Recovery write in _force_compression not under _compression_lock — thread safety gap

### MAJOR:
3. Image propagation now includes ALL last-user-message images (changed from old code) — consider documenting as improvement
4. /compress has no user-visible feedback on failure — need system notifications
5. sub_agent_state update on every turn is expensive — should throttle or only serialize new messages
6. Recovery failure in _force_compression silently continues with corrupted pool

### MINOR:
7. validate_message_pool 30% duplicate threshold may be too lenient
8. Content truncation to 200 chars in duplicate detection loses precision
9. /compress clamps fraction without warning
10. _acquire_slot re-raises exception — inconsistent error handling style
11. Settings propagation swallows all exceptions silently
12. validate_message_pool failure after /compress has no recovery action