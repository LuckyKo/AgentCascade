# Compressor Endpoint Fallback — Implementation Plan (Corrected)

## Problem (todo.md line 64)
When the compressor's configured endpoint doesn't have enough context window for the history being compressed, it keeps retrying the same endpoint instead of falling back to larger-context endpoints.

## Root Causes Identified

1. **Context-exceeded errors don't advance per-instance cursor**: llama.cpp returns HTTP 400 with `"exceed_context_size_error"` in the response body. This becomes a `ModelServiceError` (code='400') which is caught and retried within `call_with_fallback()`. The fallback chain IS tried, but if all endpoints are too small, execution_engine retries from the FIRST endpoint again because the per-instance cursor was never advanced.

2. **Overfeeding check gives up too easily**: In `compression/core.py` lines 257-271, when target messages exceed `available_for_messages`, the function returns failure instead of reducing the payload to fit within the largest available compressor endpoint (~125k tokens). It should shrink aggressively before giving up.

3. **available_for_messages uses only first compressor endpoint's config**: Lines 131-149 check only the Compressor agent's configured max_input_tokens, not the maximum across ALL endpoints in the fallback chain. This causes premature failure when a larger-context endpoint exists downstream.

## Architecture Notes (Verified)

**Error handling path for LLM calls:**
- `invoke_compression_agent()` → `engine.run(comp_instance)` → `_create_llm_call_iterator()` → `api_router.call_with_fallback('Compressor', _do_call, ...)`
- **`_raise_or_delay()` is DEPRECATED** (base.py line 1081) — bypassed in chat()/raw_chat(). DO NOT modify.
- Context-exceeded errors occur DURING API calls inside `call_with_fallback()`'s `execute_with_sem()`, caught at api_router.py line 1323.
- Streaming errors (CharacterRunDetected/MaxTokenExceeded) occur AFTER call_with_fallback returns, during generator iteration in execution_engine — handled via `_handle_inner_loop_detection()`.

**Existing fallback mechanism:**
- `call_with_fallback()` already tries all endpoints in sequence for ANY error (line 1265-1349).
- Per-instance cursor rotation: `get_endpoint_chain()` rotates the chain based on `_instance_endpoint_position[instance_name]` (api_router.py lines 1037-1051).
- Cursor is advanced by `advance_instance_endpoint()` and reset by `reset_instance_endpoint()`.

**Unique instance names:** Verified — each compression invocation uses unique `comp_state_key = f'Compressor_{_compressor_invocation_counter}'` (agent_invoker.py line 161).

---

## Files to Modify

### 1. `agent_cascade/exceptions.py` — Add ContextWindowExceeded exception

**Change**: Add a new exception class for context window exceeded errors, mirroring `MaxTokenExceeded`.

```python
class ContextWindowExceeded(Exception):
    """Raised when input exceeds the model's context window.

    Indicates the current endpoint cannot handle this payload size — switch to
    an endpoint with larger context window rather than retrying the same one.
    """
    pass
```

**Rationale**: Gives us a typed exception that execution_engine can recognize and handle consistently, just like `CharacterRunDetected` and `MaxTokenExceeded`.

---

### 2. `agent_cascade/api_router.py:call_with_fallback()` — Detect context exceeded and advance cursor

**Location**: Lines ~1323-1350 (the `except Exception as e:` block inside the endpoint retry loop)

**Current code** (relevant portion):
```python
                except Exception as e:
                    err_msg = str(e)

                    # NOTE: CharacterRunDetected/MaxTokenExceeded exceptions are raised during
                    # generator iteration inside execution_engine.py, after this method has returned.
                    # All endpoint advancement for those errors happens via
                    # _handle_inner_loop_detection → advance_instance_endpoint.
                    # This block only handles connection/timeout/etc. errors from execute_with_sem.

                    # All errors (connection, timeout, etc.) retry within the current
                    # endpoint first, then cascade through the fallback chain on exhaustion.
                    tb_str = traceback.format_exc()
                    error_msg = (...)
                    logger.warning(f"[APIRouter] {error_msg}")
                    all_errors.append(error_msg)

                    if attempt < max_retries:
                        delay = calculate_backoff(attempt + 1, self.policy)
                        ...
                        time.sleep(delay)
```

**Change**: Add context-exceeded detection before the generic retry logic. When detected, advance the per-instance cursor so subsequent engine-level retries skip past this failing endpoint.

First, add import at top of file:
```python
from agent_cascade.exceptions import ContextWindowExceeded
```

Then in the exception handler (after line 1324), add context-exceeded detection:

```python
                except Exception as e:
                    err_msg = str(e)

                    # Detect context window exceeded errors and advance cursor.
                    # Unlike CharacterRunDetected/MaxTokenExceeded (which occur during streaming
                    # in execution_engine), context-exceeded happens here at API call time.
                    # Advance the per-instance cursor so engine-level retries skip past this endpoint.
                    _inst_name_for_cursor = kwargs.get('agent_instance_name')
                    if _inst_name_for_cursor and self._is_context_exceeded_error(e):
                        new_pos = self.advance_instance_endpoint(_inst_name_for_cursor)
                        logger.warning(
                            f"[APIRouter] Context window exceeded for '{_inst_name_for_cursor}' "
                            f"on endpoint '{endpoint_name}'. Cursor advanced to {new_pos}. "
                            f"Next engine-level retry will use a different endpoint."
                        )

                    # NOTE: CharacterRunDetected/MaxTokenExceeded exceptions are raised during
                    # generator iteration inside execution_engine.py, after this method has returned.
                    # All endpoint advancement for those errors happens via
                    # _handle_inner_loop_detection → advance_instance_endpoint.
                    # This block only handles connection/timeout/etc. errors from execute_with_sem.

                    # All errors (connection, timeout, etc.) retry within the current
                    # endpoint first, then cascade through the fallback chain on exhaustion.
                    tb_str = traceback.format_exc()
                    error_msg = (...)
                    logger.warning(f"[APIRouter] {error_msg}")
                    all_errors.append(error_msg)

                    if attempt < max_retries:
                        delay = calculate_backoff(attempt + 1, self.policy)
                        ...
                        time.sleep(delay)
```

Add helper method to APIRouter class (near advance_instance_endpoint around line 1092):

```python
    @staticmethod
    def _is_context_exceeded_error(error: Exception) -> bool:
        """Check if an error indicates the input exceeded the model's context window.

        llama.cpp returns HTTP 400 with "exceed_context_size_error" in body.
        Other servers may use different patterns — catch them too.
        """
        # Already a typed ContextWindowExceeded exception
        if isinstance(error, ContextWindowExceeded):
            return True

        err_str = str(error).lower()
        from agent_cascade.llm.base import ModelServiceError
        code = getattr(error, 'code', None)

        # llama.cpp and similar servers: HTTP 400 with context-size patterns
        if code == '400' and any(
            pattern in err_str
            for pattern in ('exceed_context_size', 'context length', 'maximum input context', 'context window')
        ):
            return True

        # Generic patterns from various servers
        if any(
            pattern in err_str
            for pattern in ('prompt is too long', 'input tokens exceed', 'max_tokens exceeded', 'exceeds the context limit')
        ):
            return True

        return False
```

**Rationale**: This detects context-exceeded errors at the point they occur (during API calls in call_with_fallback), advances the per-instance cursor, and allows automatic fallback to larger-context endpoints on subsequent engine-level retries. The fallback chain is still tried within this call, but if all fail, the cursor ensures next retry doesn't start from the same failing endpoint.

---

### 3. `agent_cascade/execution_engine.py:_handle_inner_loop_detection()` — Handle ContextWindowExceeded

**Location**: Lines ~2288-2350

**Current code** (relevant portion):
```python
    def _handle_inner_loop_detection(
        self,
        instance: AgentInstance,
        e: Exception,
        retry_count: int,
        loop_retry_count: int,
        _max_attempts: int
    ) -> None:
        ...
        # Advance endpoint cursor only on character-run or max-token
        # detection so the next retry starts from a different endpoint
        # in the chain. Other detection types (sentence, ngram, block,
        # entropy, max-chars) should retry the same endpoint — they are
        # weaker signals. This is the "kick to next endpoint" mechanism
        # — without this, retries would try the same (failing) endpoint
        # again because call_with_fallback builds a fresh chain each time.
        _reason = getattr(e, 'detection_reason', '')
        if isinstance(e, MaxTokenExceeded) or _reason.startswith('character run'):
            new_pos = self.pool.api_router.advance_instance_endpoint(inst_name)
            logger.warning(...)
```

**Change**: Add `ContextWindowExceeded` to the condition that advances the endpoint cursor.

First, add import:
```python
from agent_cascade.exceptions import CharacterRunDetected, MaxTokenExceeded, ContextWindowExceeded
```

Then update the condition (around line 2338):
```python
        _reason = getattr(e, 'detection_reason', '')
        if isinstance(e, (MaxTokenExceeded, ContextWindowExceeded)) or _reason.startswith('character run'):
            new_pos = self.pool.api_router.advance_instance_endpoint(inst_name)
            logger.warning(
                f"[INNER_LOOP] Endpoint cursor advanced for '{inst_name}' "
                f"to position {new_pos} (detection: {_reason}). "
                f"Next retry will use a different endpoint."
            )
```

**Rationale**: When context window is exceeded at the execution_engine level (e.g., if re-raised from call_with_fallback or detected elsewhere), advancing the cursor ensures the next retry uses a different endpoint. This mirrors exactly how character-run and max-token detection work.

---

### 4. `agent_cascade/execution_engine.py` — Wire ContextWindowExceeded into error handling paths

**Location A**: Line ~2586 — inner loop exception re-raise check (during streaming detection)
```python
# Current:
if isinstance(e, (CharacterRunDetected, MaxTokenExceeded)):
    raise

# Change to:
if isinstance(e, (CharacterRunDetected, MaxTokenExceeded, ContextWindowExceeded)):
    raise
```

**Location B**: Line ~2757 — retry count increment guard
```python
# Current:
if not isinstance(e, (CharacterRunDetected, MaxTokenExceeded)):
    retry_count += 1

# Change to:
if not isinstance(e, (CharacterRunDetected, MaxTokenExceeded, ContextWindowExceeded)):
    retry_count += 1
```

**Location C**: Line ~2761 — inner loop handler invocation
```python
# Current:
if isinstance(e, (CharacterRunDetected, MaxTokenExceeded)):
    self._handle_inner_loop_detection(instance, e, retry_count, loop_retry_count, _max_attempts)

# Change to:
if isinstance(e, (CharacterRunDetected, MaxTokenExceeded, ContextWindowExceeded)):
    self._handle_inner_loop_detection(instance, e, retry_count, loop_retry_count, _max_attempts)
```

**Location D**: Line ~2789 — endpoint advancement detection for logging
```python
# Current:
advancing_endpoint = isinstance(e, MaxTokenExceeded) or (
    isinstance(e, CharacterRunDetected) and _det_reason.startswith('character run')
)

# Change to:
advancing_endpoint = isinstance(e, (MaxTokenExceeded, ContextWindowExceeded)) or (
    isinstance(e, CharacterRunDetected) and _det_reason.startswith('character run')
)
```

**Location E**: Line ~2737 — display message selection (add before generic else):
```python
# Add:
elif isinstance(e, ContextWindowExceeded):
    display_msg = f"LLM context window exceeded (tried {_max_attempts} times)"
```

**Rationale**: These are all the places where `CharacterRunDetected`/`MaxTokenExceeded` are checked — we add `ContextWindowExceeded` to each one so it's handled identically. This is pure wiring, no new logic.

---

### 5. `agent_cascade/compression/core.py` — Use max endpoint context + aggressive payload reduction

**Location**: Lines ~131-149 and ~257-271

**Problem A**: Current code determines `available_for_messages` from the *first* compressor endpoint's config (lines 136-144), then gives up if target messages exceed that. It should check against the *largest available* compressor endpoint (~125k) and reduce payload aggressively before failing.

**Change A** (lines ~131-151): Get max context from ALL compressor endpoints, not just first:

```python
    # ── 3b. Determine compressor context window limit (for overfeeding check later) ──
    available_for_messages = None
    max_compressor_tokens = None
    try:
        # Find the largest context window among all compressor endpoints in the fallback chain
        if agent_pool.api_router:
            comp_chain = agent_pool.api_router.get_endpoint_chain('Compressor')
            for cfg in comp_chain:
                ep_limit = cfg.get('max_input_tokens', 0)
                if ep_limit and (max_compressor_tokens is None or ep_limit > max_compressor_tokens):
                    max_compressor_tokens = ep_limit

        # Use the largest available endpoint's context window
        if max_compressor_tokens:
            available_for_messages = int(max_compressor_tokens * 0.85)  # Reserve ~85% for input messages
        else:
            # Fallback: check compressor agent config directly (old behavior)
            comp_agent = agent_pool.get_agent('Compressor')
            if comp_agent:
                max_tokens = None
                if hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'generate_cfg'):
                    max_tokens = comp_agent.llm.generate_cfg.get('max_input_tokens')
                elif hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'cfg'):
                    max_tokens = comp_agent.llm.cfg.get('max_input_tokens')
                if max_tokens:
                    available_for_messages = int(max_tokens * 0.85)

        # Cap discard count so compressor can handle the messages (~500 tokens/msg estimate)
        if available_for_messages is not None:
            max_discardable = available_for_messages // 500
            target_discard_count = min(target_discard_count, max_discardable)
    except Exception:
        pass  # If we can't determine the limit, proceed with original count
```

**Problem B**: When overfeeding detected (lines 257-271), the function returns failure instead of reducing payload. The critical bug is that `target_messages` includes U0 at index 0 (`[history[u0_index]] + list(active_set[:target_discard_count])`), so we must NEVER remove U0 during reduction — only reduce from active_set messages.

**Change B** (lines ~257-271): When overfeeding detected, reduce payload instead of failing immediately:

```python
            if total_estimated > available_for_messages:
                # Calculate how many messages we need to drop
                excess_tokens = total_estimated - available_for_messages
                logger.warning(
                    f"Compression payload ({total_estimated} tokens) exceeds compressor context "
                    f"({available_for_messages} tokens). Reducing discard count by ~{excess_tokens} tokens."
                )

                # Greedily remove oldest messages until we fit.
                # IMPORTANT: target_messages[0] is U0 (first user message) on first compression — NEVER remove it.
                # Only reduce from active_set portion (target_messages[1:] or all if not first compression).
                reduced_count = target_discard_count
                running_tokens = target_token_count

                # Determine where active_set messages start in target_messages
                # If latest_summary_idx == -1 (first compression), U0 is at index 0, active_set starts at index 1
                # Otherwise, all of target_messages are from active_set
                active_start_in_target = 0 if latest_summary_idx != -1 else 1

                for i in range(active_start_in_target, len(target_messages)):
                    if running_tokens + prompt_overhead_tokens <= available_for_messages:
                        break
                    # Remove message i from target_messages (from the active_set portion)
                    msg = target_messages[i]
                    if isinstance(msg, dict):
                        wrapped = Message(**msg)
                    else:
                        wrapped = msg
                    content = extract_text_from_message(wrapped, add_upload_info=False)
                    running_tokens -= qwen_count(content)
                    reduced_count -= 1

                # Apply the reduced count — rebuild target_messages preserving U0 if present
                if reduced_count < target_discard_count:
                    active_portion = list(active_set[:reduced_count]) if reduced_count > 0 else []
                    if latest_summary_idx == -1:
                        # First compression: preserve U0 at front
                        u0_index = active_start_idx - 1
                        target_messages = [history[u0_index]] + active_portion
                    else:
                        target_messages = active_portion
                    target_token_count = running_tokens
                    total_estimated = target_token_count + prompt_overhead_tokens
                    target_discard_count = reduced_count

                # If we still can't fit after reduction, give up with clear error
                if total_estimated > available_for_messages or target_discard_count < 1:
                    return CompressResult(
                        success=False,
                        summary_text=None,
                        marker_message=None,
                        messages_discarded=0,
                        tail_count=len(active_set),
                        error=(
                            f"True overfeeding detected: even after reducing to {target_discard_count} "
                            f"messages ({target_token_count} tokens + ~{prompt_overhead_tokens} overhead = "
                            f"~{total_estimated} total), still exceeds compressor context window of "
                            f"~{available_for_messages} tokens. Agent context is filling faster than "
                            f"compression can reduce it."
                        ),
                        mode=mode,
                    )
```

**Rationale**: 
- Change A ensures we use the largest available compressor endpoint's context window for planning, not just the first one.
- Change B aggressively reduces payload before giving up, while preserving U0 (first user message) which must never be removed during reduction. Only active_set messages are reduced. If it still can't fit after aggressive reduction, then we truly have an overfeeding situation and should fail with a clear message.

---

## Error Flow (How It Works — Corrected)

1. Compression is triggered → `core.py` reduces payload to fit largest compressor endpoint
2. `invoke_compression_agent()` creates Compressor instance via `_create_system_agent()` with unique name `Compressor_N`
3. `engine.run(comp_instance)` → `_create_llm_call_iterator()` → `api_router.call_with_fallback('Compressor', ..., agent_instance_name='Compressor_N')`
4. First endpoint in chain is tried (e.g., llama.cpp with 32k context)
5. If payload exceeds that endpoint's context:
   - llama.cpp returns HTTP 400 with `"exceed_context_size_error"`
   - `oai.py` wraps it as `ModelServiceError(code='400')`
   - `call_with_fallback()` catches at line 1323, `_is_context_exceeded_error()` detects pattern → advances cursor via `advance_instance_endpoint('Compressor_N')` to position 1
   - Continues to next endpoint in chain (automatic fallback)
6. If second endpoint (e.g., 128k context) succeeds: compression completes normally
7. If ALL endpoints fail with context-exceeded:
   - `call_with_fallback()` raises RuntimeError("All API endpoints exhausted")
   - execution_engine catches at line 2724, classifies error
   - Engine-level retry occurs but cursor is already advanced past small endpoints
   - Next call to `get_endpoint_chain('Compressor', instance_name='Compressor_N')` rotates chain starting from cursor position → tries larger-context endpoints first

---

## Edge Cases Handled

1. **All endpoints too small**: If even the largest compressor endpoint can't handle the payload, overfeeding check fails with clear error message after aggressive reduction. No infinite retry loop.

2. **Compressor has no dedicated endpoints configured**: Falls back to default/global endpoint via `get_endpoint_chain()` Tier 4. Same fallback mechanism applies.

3. **llama.cpp not used**: The pattern matching in `_is_context_exceeded_error()` catches generic context-exceeded patterns (`"context length"`, `"maximum input context"`, `"context window"`, etc.) so other servers' errors are handled too.

4. **Per-instance cursor cleanup**: Cursor is automatically reset on success (existing behavior in `api_router.reset_instance_endpoint()` called when agent completes). Each compression invocation uses a unique instance name (`Compressor_N`), so cursors don't bleed between compressions.

5. **No compressor endpoints at all**: If `get_endpoint_chain('Compressor')` fails, the existing code path continues without the check (wrapped in try/except), maintaining backward compatibility.

6. **U0 preservation during reduction**: On first compression, U0 is prepended to target_messages. The reduction logic only removes from active_set portion (target_messages[1:]), never touching U0 at index 0.

---

## Summary of Changes

| File | Lines | Change Type |
|------|-------|-------------|
| `exceptions.py` | +5 lines | New ContextWindowExceeded exception class |
| `api_router.py:call_with_fallback()` | ~line 1323, +15 lines | Detect context-exceeded errors, advance cursor |
| `api_router.py:_is_context_exceeded_error()` | ~line 1092, +25 lines | New helper method for error detection |
| `execution_engine.py` | 5 locations, +1 import | Wire ContextWindowExceeded into existing handlers |
| `compression/core.py` | Lines 131-271, ~+40 lines | Use max endpoint context + aggressive reduction preserving U0 |

Total: ~85 lines of new/modified code across 5 files. All changes are additive — no existing logic is removed or refactored. `_raise_or_delay()` is NOT modified (deprecated).