# Implementation Summary: Security Agent Endpoint Inheritance Deadlock Fix

**Todo:** #90  
**File changed:** `agent_cascade/security_handler.py` (single file)  
**Date:** 2026-08-07

## Problem
Security agent always inherited slot 0 (orchestrator's endpoint) because its `parent_instance` was set to the session name (`Maine`) instead of the actual calling agent. When an async child agent holding a different slot triggered a Security check, deadlock occurred as Security contended for slot 0 held by the synchronous parent.

## Changes Made

### Change A — Resolve true caller from approval data (lines 140-146)
After `ap = ap_list[0]`, added logic to derive `caller_agent` from `ap['agent_name']`:
- Uses `ap.get('agent_name') or instance_name` as primary source
- Falls back to session name if the resolved caller is not in the agent pool
- Passed `caller_agent` through thread args → `_run_check_worker` → `_execute_check`

### Change B — Security parent set to true caller (line 272)
Changed `_create_system_agent(caller=...)` from:
```python
caller=self.session.get('session_name', 'Orchestrator')
```
to:
```python
caller=caller_agent
```
This makes Security inherit the calling agent's endpoint chain instead of always slot 0.

### Change C — Slot-bypass logging updated (line 319)
Changed `caller_name_sec` from session name to `caller_agent` so debug logs report the actual caller.

### Change D — Fallback guards (lines 143-145)
If `ap['agent_name']` is missing, None, or not in pool → falls back to session name. No crash possible.

## Signature Changes
- `_run_check_worker`: added `caller_agent: str` parameter after `instance_name`
- `_execute_check`: added `caller_agent: str` parameter after `instance_name`
- Both are private methods called only within this file — no external impact

## Verification
- Syntax check passed (685 lines)
- No other call sites of modified methods found outside security_handler.py
- Fallback behavior preserved — orchestrator-as-caller case degenerates to prior behavior