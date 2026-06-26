# Max Input Tokens Fallback Fix - 2026-06-25

## Problem
When API fallback switches to a different endpoint during `_do_call()`, the new endpoint's `max_input_tokens` value was being overwritten by the stale value from the original template initialization. This happened because config merge order put template defaults *after* endpoint config, so template values clobbered the correct endpoint-specific values.

Additionally, the non-fallback path had an `elif` that skipped template defaults entirely when no fallback occurred and no per-instance override existed.

## Root Cause
In `execution_engine.py::_do_call()`, the config merge order was:

```python
# BEFORE (broken):
cfg = dict(llm.generate_cfg)          # 1. Template defaults
if fallback_chain:
    cfg.update(endpoint_config)        # 2. Endpoint overrides
if override_cfg:
    cfg.update(override_cfg)          # 3. Per-instance overrides

# BUT for non-fallback path:
cfg = {}
if override_cfg:
    cfg = dict(override_cfg)          # Only per-instance, NO template defaults!
elif fallback_chain:
    ...
else:
    pass  # cfg stays empty -> relies on defaults elsewhere
```

Issues:
1. **Fallback path**: Template `generate_cfg` was the base, but when multiple endpoints were tried, the wrong endpoint's config could persist.
2. **Non-fallback path**: Template defaults (`llm.generate_cfg`) were completely skipped due to the `elif` chain — only per-instance overrides or fallback configs contributed values.

## Fix Applied

### Reversed merge priority in `_do_call()`:

```python
# AFTER (correct):
cfg = dict(llm.generate_cfg)          # 1. Template LLM generate_cfg (base defaults)
if fallback_chain:
    cfg.update(endpoint_config)        # 2. Endpoint config from fallback chain (correct max_input_tokens)
if override_cfg:
    cfg.update(override_cfg)          # 3. Per-instance override (user-specified values via UI)

# Non-fallback path also starts with template defaults:
cfg = dict(llm.generate_cfg)          # Template defaults always included
if override_cfg:
    cfg.update(override_cfg)          # Then per-instance overrides on top
```

### Merge Priority Order (correct):
1. **Template LLM generate_cfg** — base defaults from the agent template definition
2. **Endpoint config from fallback chain** — correct `max_input_tokens` for whichever endpoint is actually being used
3. **Per-instance override** — user-specified values via UI (highest priority)

### Non-fallback path fix:
Changed `elif` to always include template defaults as the base, then layer per-instance overrides on top. This ensures consistent behavior whether fallback occurs or not.

## Files Modified
- `agent_cascade/execution_engine.py` — `_do_call()` method config merge logic

## Key Insight
The config merge order matters when endpoints have different context window sizes. If you start with a GPT-4o template (128K context) and fallback to a smaller model (32K context), the wrong `max_input_tokens` value causes either:
- **Truncation errors**: Input exceeds the actual model's context window
- **Wasted tokens**: Not using the full capacity of the fallback endpoint

## Related Lessons
- `lessons_maxtokens_flow.md` — Full trace of how max_tokens flows through the codebase
- `lessons_max_tokens_fix.md` — Original fix for OAI read-path bug (different issue, same subsystem)
- `lessons_max_input_tokens_propagation_fix.md` — Propagation fix from template to execution engine

---
*Document created: 2026-06-25*  
*Fix by: CommitAgent*