# Compression Fix #020 - Implementation Notes

## Changes Required

### 1. settings.py
Add `compression_cooldown_seconds` setting to prevent excessive compression triggers in quick succession.

### 2. execution_engine.py  
Add cooldown check in `_pre_llm_checks()` before calling `_force_compression()`.

### 3. api_server.py
Wrap session state modifications in `api_reset()` with `session_lock` (lines 1017-1018).

## Current State Analysis

After reviewing the codebase:
- `compression_force_cooldown` already exists in PoolSettings (agent_instance.py)
- Cooldown check exists in `_force_compression()` method
- Missing: `compression_cooldown_seconds` setting for general cooldown
- Missing: session_lock wrapper in api_reset() for lines 1017-1018

## Implementation Plan

1. Add `compression_cooldown_seconds` to settings.py or PoolSettings
2. Verify _pre_llm_checks has proper cooldown logic
3. Fix api_reset() to use session_lock for session['generating'] and session['generation_id']