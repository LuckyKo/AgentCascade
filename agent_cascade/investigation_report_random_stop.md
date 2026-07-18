# Bug Investigation: Agents Stop Randomly During Long Reasoning Streaming

## Bug Description
Agents get stopped randomly in the middle of streaming long reasoning. Suspected causes:
1. Content output limit triggering silently
2. Inner loop detection logic issue

---

## Findings

### 1. Max Output Token Guard — Default is 2048 Tokens
**File:** `execution_engine.py` lines 2125–2136

```python
_max_output_tokens = 2048  # Default cap per single response
_gen_override = getattr(instance, '_generate_cfg_override', None)
if _gen_override and isinstance(_gen_override, dict):
    _mt = _gen_override.get('max_tokens') or _gen_override.get('max_output_tokens') or _gen_override.get('max_input_tokens')
    if isinstance(_mt, int) and _mt > 0:
        _max_output_tokens = _mt
# Also check template LLM generate_cfg as fallback
if _max_output_tokens == 2048:
    _llm_cfg = getattr(getattr(template, 'llm', None), 'generate_cfg', None) or {}
    _mt = _llm_cfg.get('max_tokens') or _llm_cfg.get('max_output_tokens')
    if isinstance(_mt, int) and _mt > 0:
        _max_output_tokens = _mt
```

**Resolution chain:**
1. Default: **2048 tokens**
2. Check `_generate_cfg_override` for `max_tokens` → `max_output_tokens` → `max_input_tokens`
3. Fallback: check `template.llm.generate_cfg` for `max_tokens` → `max_output_tokens` (NOTE: does NOT check `max_input_tokens` here!)

**Resolution issue (Bug #1):** The template fallback at line 2134 only checks `max_tokens` and `max_output_tokens`, but the template LLM config typically stores `max_input_tokens` (set by `DEFAULT_MAX_INPUT_TOKENS = 65000` in `settings.py`). This means the template fallback **never matches** and the guard stays at 2048.

**Impact:** With `TOKEN_ESTIMATE_CHAR_DIVISOR = 5.0`, 2048 tokens = ~10,240 characters ≈ ~2,048 words of reasoning content. Long reasoning blocks easily exceed this.

### 2. UI Default is Also 2048
**File:** `web_ui/index.html` line 299
```html
<input type="number" id="setting-max-tokens" min="1" max="32768" value="2048" />
```

**File:** `web_ui/app.js` line 4075
```js
if ($('#setting-max-tokens')) cfg.max_tokens = parseInt($('#setting-max-tokens').value) || 2048;
```

The UI default is 2048, matching the execution engine default. If the user never touches the Max Tokens setting, agents are limited to ~2048 output tokens per response.

### 3. Silent Trigger — No UI Feedback
**File:** `execution_engine.py` lines 2228–2244

When the guard triggers:
```python
if not _token_guard_triggered:
    _est_tokens = len(_total_text) // TOKEN_ESTIMATE_CHAR_DIVISOR
    if _est_tokens > _max_output_tokens:
        _sample_path = save_loop_sample(...)
        yield from _abort_stream(
            f"Output token budget exceeded: ~{_est_tokens} tokens (limit {_max_output_tokens})"
        )
```

The `_abort_stream` function:
- Clears streaming responses (line 2151)
- Closes the generator (line 2165)
- Logs at DEBUG level (line 2168)
- Yields `None` to signal UI (line 2175)
- Then raises `Exception(f"max_tokens: ~{_est_tokens} tokens")` (line 2244)

**No visible message is shown to the user** — the agent just silently retries. After exhausting retries (default `_loop_max = 2`), it shows `[SYSTEM ERROR: LLM exceeded token limit (tried 2 times)]` but this is only at the end.

### 4. Shared Retry Budget
**File:** `execution_engine.py` lines 2171–2174

Both inner-loop detection and max-token guard share the same retry counters:
```python
nonlocal last_output, retry_count, loop_retry_count
last_output = None
retry_count += 1
loop_retry_count += 1
```

With `_loop_max = 2` (default from UI setting `loop_max_retries`), the agent gets only 2 retries total. If the max-token guard fires twice (because the limit is still too low), the agent exhausts retries and shows an error.

### 5. Inner-Loop Detection Settings
**File:** `inner_loop_detect.py` — Default settings from `settings.py` lines 134–172:

| Setting | Default | Description |
|---------|---------|-------------|
| `default_min_chars` | 4000 | Min chars before full detection |
| `score_threshold` | 350 | Cumulative score to trigger |
| `char_run_limit` | 70 | Max consecutive identical chars |
| `sentence_repetition_threshold` | 9 | Sentence count to flag |
| `ngram_repetition_threshold` | 5 | N-gram count to flag |
| `block_repetition_threshold` | 4 | Block count to flag |
| `entropy_threshold` | 2.0 | Shannon entropy below which loop suspected |
| `score_decay_rate` | 0.97 | Multiplicative decay per cycle |
| `max_score` | 500 | Hard cap |

These are reasonable but can accumulate quickly during long reasoning with repeated patterns (e.g., "Let me think about this...", "First...", "Second...").

### 6. Token Estimation is Rough
**File:** `execution_engine.py` line 2231
```python
_est_tokens = len(_total_text) // TOKEN_ESTIMATE_CHAR_DIVISOR
```

With `TOKEN_ESTIMATE_CHAR_DIVISOR = 5.0`, this is a rough character-based estimate. For reasoning-heavy models that produce dense text, this can underestimate actual tokens.

---

## Root Cause Hypothesis

**Primary cause: The max-output-token guard default of 2048 is too low for long reasoning.**

The resolution chain has a bug: the template fallback (line 2134) checks `max_tokens` and `max_output_tokens` but NOT `max_input_tokens`. Since the template LLM config stores `max_input_tokens` (not `max_tokens`), the fallback never matches and the guard stays at 2048.

**Flow for a long reasoning response:**
1. Agent starts streaming reasoning
2. After ~10,240 chars (~2048 words), the max-token guard triggers
3. Stream is aborted, generator closed, retry count incremented
4. Agent retries with same 2048 limit (no change)
5. After 2 retries, agent shows `[SYSTEM ERROR: LLM exceeded token limit]`
6. User sees the agent "stop randomly" with no clear indication why

**Secondary cause: Inner-loop detection may contribute false positives.**

With `default_batch_interval = 1` (heavy checks run every feed call), long reasoning with repeated structural patterns can accumulate score quickly. The activation factor ramps up linearly, so detection becomes fully active after just 4000 chars (~800 words).

---

## Recommended Fix Approach

### Fix 1: Increase Default Max Output Tokens (Quick Win)
Change the default from 2048 to a more reasonable value like 8192 or 16384:
- `execution_engine.py` line 2125: `_max_output_tokens = 8192`
- `web_ui/index.html` line 299: `value="8192"`
- `web_ui/app.js` line 4075: fallback to 8192

### Fix 2: Fix Template Fallback Resolution (Bug Fix)
Add `max_input_tokens` to the template fallback check:
```python
# Line 2134: Also check max_input_tokens in template config
_mt = _llm_cfg.get('max_tokens') or _llm_cfg.get('max_output_tokens') or _llm_cfg.get('max_input_tokens')
```

### Fix 3: Make Max Output Token Proportional to Max Input Tokens
Instead of a fixed default, derive the output limit from the input limit:
```python
# Use a fraction of max_input_tokens as output budget
_max_input = _llm_cfg.get('max_input_tokens', 65000)
_max_output_tokens = max(_max_input // 4, 4096)  # At least 4096, up to 25% of input budget
```

### Fix 4: Add Debug Logging for Silent Triggers
Change the debug log to info level so users can see when the guard fires:
```python
logger.info(f"[STREAM_GUARD] {reason_msg} for {inst_name}. Retrying…")
```

### Fix 5: Show Better UI Feedback
When the guard triggers, show a brief status message instead of silently retrying:
```python
yield Message(role=ASSISTANT, content="[STREAMING: Reasoning response too long, retrying with adjusted limits…]")
```

---

## Files Involved

| File | Lines | Purpose |
|------|-------|---------|
| `execution_engine.py` | 2125-2136 | Max output token resolution |
| `execution_engine.py` | 2228-2244 | Max output token guard trigger |
| `execution_engine.py` | 2143-2175 | Abort stream helper |
| `execution_engine.py` | 2411-2460 | Retry loop and error handling |
| `inner_loop_detect.py` | 27-435 | Inner-loop detection logic |
| `settings.py` | 134-172 | Default detection settings |
| `web_ui/index.html` | 299 | UI Max Tokens default |
| `web_ui/app.js` | 4075 | UI Max Tokens send to backend |

---

## Investigation Date
2026-07-18