# Max Token Limit Bug Fix - 2026-05-31 (Comprehensive Reviewer Fix)

## Problem
OAI endpoint detection (oai.py ~line 206) updates `llm.generate_cfg['max_input_tokens']` at startup with the real detected context window. However, resolution code read from `llm.cfg.get('generate_cfg', {}).get('max_input_tokens')` — a DIFFERENT object. The OAI detection writes to `self.generate_cfg` (an attribute dict), but resolution only checked `self.cfg['generate_cfg']` (a nested dict). These are NOT the same thing.

Additionally, per-instance overrides (`_generate_cfg_override`) were only respected in execution_engine.py but not in UI paths, causing inconsistent context bar display. Code was duplicated across 5 locations with hardcoded `58000` instead of using `DEFAULT_MAX_INPUT_TOKENS`.

## Root Cause
1. **OAI read-path bug**: `llm.generate_cfg` vs `llm.cfg['generate_cfg']` — different objects
2. **Per-instance override missing**: UI paths didn't check `_generate_cfg_override`
3. **Code duplication**: 5 separate resolution blocks, each a maintenance hazard
4. **Hardcoded threshold**: 58000 instead of DEFAULT_MAX_INPUT_TOKENS from settings.py

## Fix Applied

### Created shared helper: `_resolve_max_tokens(pool, instance)` in api_integration.py
Single source of truth with full resolution priority:
1. User-set router limit (≠ DEFAULT_MAX_INPUT_TOKENS) — explicit user config
2. Per-instance override (`_generate_cfg_override`) from execution engine propagation
3. Runtime-detected LLM limit from OAI detection (`llm.generate_cfg` directly)
4. Template LLM config (`llm.cfg['generate_cfg']`) static from settings
5. Default-ish router value as fallback
6. 128000 hard-coded default

### Replaced all 5 resolution sites:
- `api_integration.py:_get_max_tokens_for_instance()` → delegates to `_resolve_max_tokens()`
- `execution_engine.py:_get_max_tokens()` → delegates to `_resolve_max_tokens()`
- `api_server.py:get_agent_max_tokens()` → delegates to `_resolve_max_tokens()` (was dead code)
- `api_server.py:build_state() fallback` → uses `_resolve_max_tokens()`
- `api_server.py:stream handler` → uses `_resolve_max_tokens()`

### Replaced hardcoded 58000
All resolution sites now use `DEFAULT_MAX_INPUT_TOKENS` from settings.py (with try/except ImportError fallback of 58000 at the function level to avoid circular imports).

## Files Modified
- `agent_cascade/api_integration.py` — Added `_resolve_max_tokens()`, updated `_get_max_tokens_for_instance()`
- `agent_cascade/execution_engine.py` — Replaced `_get_max_tokens()` with delegation
- `agent_cascade/api_server.py` — Updated import, `get_agent_max_tokens()`, build_state fallback, stream handler

## Key Constants
- DEFAULT_MAX_INPUT_TOKENS = from settings.py (env: QWEN_AGENT_DEFAULT_MAX_INPUT_TOKENS, default 58000)
- 128000 = hardcoded fallback when nothing else works
- Per-instance override (`_generate_cfg_override`) in execution engine takes absolute precedence

## Dead Code Note
`api_server.py:386` `get_agent_max_tokens()` is still never called — it's dead code. Updated it to use `_resolve_max_tokens()` for consistency, but consider removing it entirely if not needed.