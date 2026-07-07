# Inner Loop Trace Report: Reviewer Killed at 2048 Tokens

## Executive Summary

The reviewer agent was killed at **~2048 tokens** even though you set Max Tokens to **16000**. The root cause is a **two-step resolution chain** in `execution_engine.py` where the `_max_output_tokens` guard defaults to 2048 and only gets overridden if specific conditions are met. The issue: **the override may not have been propagated correctly to the child (reviewer) instance**, leaving it at the default 2048 cap.

---

## Key Constants

| Constant | Value | Source |
|----------|-------|--------|
| `TOKEN_ESTIMATE_CHAR_DIVISOR` | **5.0** | `settings.py:96-97` (env var fallback) |
| `_max_output_tokens` default | **2048** | `execution_engine.py:1644` hard-coded |
| Inner loop detector min_chars | **4000** | `inner_loop_detect.py:17` → ~800 tokens before detection activates |
| Score threshold | **200** | `inner_loop_detect.py:31` |

---

## Flow Diagram: How a Generation Gets Killed

```
┌─────────────────────────────────────────────────────────────┐
│  LLM Streaming Loop (execution_engine.py ~line 1660)        │
│                                                             │
│  For each streaming chunk:                                  │
│    ┌──────────────────────────────────────────────────┐     │
│    │ 1. Extract delta text from accumulated response   │     │
│    │    _delta_text = _total_text[_prev_text_len:]    │     │
│    └─────────────────────┬────────────────────────────┘     │
│                          │                                  │
│          ┌───────────────┴───────────────┐                  │
│          ▼                               ▼                  │
│  ┌───────────────┐             ┌──────────────────┐        │
│  │ INNER LOOP    │             │ MAX TOKEN GUARD   │        │
│  │ DETECTOR      │             │ (safety net)      │        │
│  │               │             │                    │        │
│  │ _inner_       │             │ if not triggered:  │        │
│  │ detector.feed()│            │ est = chars // 5   │        │
│  │               │             │ if est > limit:    │        │
│  │ Checks (in    │             │   KILL + retry     │        │
│  │ order):       │             └──────────────────┘        │
│  │ 1. Char run  │                                          │
│  │    (>70 same)│                                          │
│  │ 2. Sentence  │                                          │
│  │    repetition│                                          │
│  │    (≥7 reps) │                                          │
│  │ 3. N-gram    │                                          │
│  │    repeat    │                                          │
│  │ 4. Block     │                                          │
│  │    repeat    │                                          │
│  │ 5. Low       │                                          │
│  │    entropy   │                                          │
│  │    (<2.0)    │                                          │
│  └──────┬───────┘                                          │
│         │ If score ≥ 200:                                 │
│         ▼                                                  │
│   KILL + retry (same path as max token guard)              │
└─────────────────────────────────────────────────────────────┘
```

---

## How `_max_output_tokens` Is Resolved (execution_engine.py lines 1644-1655)

### Resolution Chain (3 steps, first match wins):

**Step 1 — Default:**
```python
_max_output_tokens = 2048   # Hard-coded default per single response
```

**Step 2 — Per-instance override (`_generate_cfg_override`):**
```python
_gen_override = getattr(instance, '_generate_cfg_override', None)
if _gen_override and isinstance(_gen_override, dict):
    _mt = _gen_override.get('max_tokens') or _gen_override.get('max_output_tokens')
    if isinstance(_mt, int) and _mt > 0:
        _max_output_tokens = _mt   # ← This is where 16000 should land
```

**Step 3 — Template LLM `generate_cfg` fallback:**
```python
if _max_output_tokens == 2048:  # Only if step 2 didn't override
    _llm_cfg = getattr(getattr(template, 'llm', None), 'generate_cfg', None) or {}
    _mt = _llm_cfg.get('max_tokens') or _llm_cfg.get('max_output_tokens')
    if isinstance(_mt, int) and _mt > 0:
        _max_output_tokens = _mt
```

### ⚠️ Critical Bug in Step 3 Check:
The condition `if _max_output_tokens == 2048` is an **exact equality check**. If the per-instance override has a value of exactly 2048, it won't fall through to the template fallback. But more importantly, if `_generate_cfg_override` is `None` or empty dict for the reviewer instance, the default stays at 2048 AND the template fallback might also not have `max_tokens` set.

---

## How Max Tokens Flows from UI → Reviewer Instance

### Path A: Root Agent (Maine/Orchestrator)
```
UI sends max_tokens=16000
    ↓
api_integration.py:apply_ui_config() [~line 1480]
    ↓ Sanitize as int, filter NON_LLM_KEYS, store in _generate_cfg_override
instance._generate_cfg_override = {'max_tokens': 16000, ...}
```

### Path B: Child Agent (Reviewer called by Coder)
```
lifecycle_manager.py:_propagate_settings() [~line 493]
    ↓ Read caller's _generate_cfg_override or template generate_cfg
llm_cfg = getattr(caller_inst, '_generate_cfg_override', None) 
          or getattr(caller_template.llm, 'generate_cfg', {})
    
    ↓ Query for max_input_tokens (NOT max_tokens!)
propagated_max = llm_cfg.get('max_input_tokens')   ← LINE 513
    
    ↓ Store on child instance
cfg['max_input_tokens'] = propagated_max
instance._generate_cfg_override = cfg               ← Only has 'max_input_tokens'!
```

### 🔥 ROOT CAUSE FOUND:

**The lifecycle manager propagates `max_input_tokens` (context window limit) but NOT `max_tokens` (output token limit).**

In `lifecycle_manager.py:513`:
```python
propagated_max = llm_cfg.get('max_input_tokens')   # ← reads max_input_tokens
```

Then at line 530-532, it stores ONLY `max_input_tokens` into the child's override:
```python
cfg['max_input_tokens'] = propagated_max
instance._generate_cfg_override = cfg
```

But in `execution_engine.py:1647`, the guard looks for:
```python
_gen_override.get('max_tokens') or _gen_override.get('max_output_tokens')
```

**It does NOT check `max_input_tokens`!** So even though 16000 was set, it's stored as `max_input_tokens` on the child instance but read as `max_tokens`. Key mismatch!

---

## Inner Loop Detector vs Max Token Guard Interaction

### Timing:
Both guards run **every streaming chunk** in the same loop iteration. The inner loop detector runs FIRST (line 1700), then the max token guard runs SECOND (line 1717).

### Priority Order:
1. **Inner loop detector fires first** — if it detects a pattern, it aborts and retries immediately
2. **Max token guard is the safety net** — catches anything the inner loop detector missed

### How They Can Confuse Each Other:
- Both call `save_loop_sample()` with similar signatures
- Both use `_abort_stream()` → same retry mechanism
- The exceptions raised are distinguishable: `"inner_loop: ..."` vs `"max_tokens: ~N tokens"`
- But in logs, both look like "stream interrupted and retried"

### Detection Thresholds (InnerLoopDetector defaults):
| Check | Trigger Condition | Score Added |
|-------|-------------------|-------------|
| Char run | >70 identical chars | Immediate return (+100) |
| Sentence repetition | Same sentence ≥7 times | +80 per hit |
| N-gram repeat (128 tokens) | Same n-gram ≥5 times | +60 per hit |
| Block repeat (128 tokens) | Same block ≥4 times | +70 per hit |
| Low entropy (<2.0 bits) | Token distribution collapse | +30 per hit |

**Score threshold = 200**, with 3% decay per feed call. So roughly:
- 3 sentence repetitions (80×3=240) → triggers at ~1600 tokens if sentences repeat
- 4 block repeats (70×4=280) → triggers
- Mixed signals accumulate faster

### Min Activation Gate:
Detection only activates after **4000 characters** (~800 estimated tokens). Before that, it just accumulates state and decays.

---

## Token Estimation Math

```python
_est_tokens = len(_total_text) // TOKEN_ESTIMATE_CHAR_DIVISOR  # divisor = 5.0

# To trigger max token guard at 2048:
# Need _est_tokens > 2048 → chars > 2048 × 5 = ~10,240 characters of output text
```

For the inner loop detector to activate meaningfully:
- **min_chars = 4000** → detection starts at ~800 estimated tokens
- Between 800 and 2048 tokens, the detector has a **1248-token window** to catch loops before the max token guard fires

---

## Likely Scenario for This Incident

Given that Max Tokens was set to 16000 but the reviewer was killed at ~2048:

### Most Probable Cause:
The `max_tokens` value (16000) was stored in `_generate_cfg_override` as `'max_tokens'`, but when the lifecycle manager propagated settings to the child reviewer instance, it only copied `max_input_tokens`. The child's `_generate_cfg_override` ended up with `{'max_input_tokens': 16000}` but NOT `{'max_tokens': 16000}`.

### Alternative Cause:
If the reviewer was called directly (not via lifecycle propagation) and its template's LLM config didn't have `max_tokens` set, both resolution steps would fail and the default 2048 would stick.

---

## Recommended Fixes

### Fix 1: Propagate `max_tokens` alongside `max_input_tokens`
In `lifecycle_manager.py` ~line 513, also propagate output token limits:
```python
propagated_max = llm_cfg.get('max_input_tokens')
propagated_output_max = llm_cfg.get('max_tokens') or llm_cfg.get('max_output_tokens')

# Then in the cfg assignment:
if propagated_max:
    cfg['max_input_tokens'] = propagated_max
if propagated_output_max:
    cfg['max_tokens'] = propagated_output_max  # ← Add this
```

### Fix 2: Also check `max_input_tokens` in the guard resolution
In `execution_engine.py` ~line 1647, add `max_input_tokens` as a fallback key:
```python
_mt = (_gen_override.get('max_tokens') or 
       _gen_override.get('max_output_tokens') or
       _gen_override.get('max_input_tokens'))  # ← Add this fallback
```

### Fix 3 (Defense in Depth): Make the default match user expectations
Change `execution_engine.py:1644` from:
```python
_max_output_tokens = 2048
```
To use a configurable default that matches the UI setting, or at minimum log when the default is used so it's visible in debugging.

---

## Files Involved

| File | Lines | Role |
|------|-------|------|
| `agent_cascade/execution_engine.py` | 1644-1731 | Core guard logic, token estimation |
| `agent_cascade/settings.py` | 96-97 | TOKEN_ESTIMATE_CHAR_DIVISOR = 5.0 |
| `agent_cascade/lifecycle_manager.py` | 491-532 | Settings propagation to child agents |
| `agent_cascade/api_integration.py` | 1480-1581 | UI config → _generate_cfg_override |
| `agent_cascade/inner_loop_detect.py` | 24-272 | InnerLoopDetector class and detection logic |