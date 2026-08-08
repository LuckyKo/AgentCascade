# Implementation Plan: Fallback Compression Gap Fix — Iterative Compression with Smart Slicing

**Author:** planner_fallback_compression  
**Date:** 2026-08-08 (rewritten with smart slice-first algorithm)  
**TODO item:** todo.md:94 — "fallback to a lower context window limit API fails to properly trigger compression"  
**Priority:** Critical (context loss in production)  
**Branch:** HEAD = 1bd3709  

---

## Executive Summary

When an agent's conversation exceeds the context window of a fallback endpoint (smaller than the primary), the system silently truncates or retries — it never compresses. This plan fixes that with two complementary changes:

1. **Step 0:** Disable silent truncation in `llm/base.py` — raise `ContextWindowExceeded` immediately when tokens exceed limit, forcing upstream to handle via compression instead of silently dropping content.
2. **Steps 1-3:** Iterative compress-on-fallback with smart slicing — before each compression attempt, test whether the sliced history fits the compressor's context window. If not, halve the slice ratio and retest. Only invoke the Compressor agent when we know it will succeed. After compression, check if result fits the target endpoint; if not, loop again or trigger automatic forced compression.

**Core principle:** The only time we "lose" content is through deliberate compression (which preserves meaning in summary form). Silent truncation is unacceptable. Never waste an LLM call on a slice that won't fit.

---

## Architecture Overview

### Current Flow (BROKEN)

```
Agent conversation grows → _pre_llm_checks() checks usage vs first endpoint limit (128k)
    ↓
Usage at 60k/128k = 47% → no compression triggered
    ↓
_call_llm_with_injection() → _execute_llm_call() → api_router.call_with_fallback()
    ↓
Primary endpoint (128k) unavailable → falls back to secondary (32k)
    ↓
llm/base.py: estimated_tokens > 32k → SILENTLY TRUNCATES via _truncate_input_messages_roughly()
    ↓
OR truncation can't reduce enough → raises ContextWindowExceeded
    ↓
api_router catches error → advance_instance_endpoint() only, no compression
    ↓
RESULT: Silent context loss (truncation) or failure message — NEVER compresses
```

### Desired Flow (FIXED — Smart Slice-First Iterative Compression)

```
Agent conversation grows → _pre_llm_checks() checks usage vs first endpoint limit
    ↓
Usage at 60k/128k = 47% → no compression triggered
    ↓
_call_llm_with_injection() → _execute_llm_call() → api_router.call_with_fallback()
    ↓
Primary endpoint (128k) unavailable → falls back to secondary (32k)
    ↓
llm/base.py: estimated_tokens > 32k → RAISES ContextWindowExceeded (no truncation!)
    ↓
api_router catches error → raises FallbackCompressionRequired
    ↓
Execution engine catches exception → enters ITERATIVE COMPRESSION LOOP with SMART SLICING:
    │
    │   Outer loop: compression rounds (max 5)
    │   Inner loop: find slice ratio that fits compressor's window
    │   │
    │   │   Round 1: Start with fraction=0.70 (discard 70%)
    │   │   → Compute discard_count = len(active_set) * 0.70
    │   │   → TEST BEFORE COMPRESS: count tokens of target_messages slice
    │   │   → Slice too large for compressor window? Halve fraction → 0.35, retest
    │   │   → Still too large? Halve again → 0.175, retest
    │   │   → Test passes! Now invoke Compressor agent with this slice.
    │   │
    │   → Compression complete. Rebuild working set from compressed pool state.
    │   → Check if compressed payload fits next endpoint (32k)?
    │   → Yes! Inject notification, resume agent. DONE.
    │   → No? Outer loop continues with Round 2...
    │
    │   Round N: If after compression still too large, repeat outer loop.
    │   Each round compresses the remaining active history further.
    │
    │   After 5 rounds or if overfeeding detected → raise ContextWindowExceeded.
    ↓
RESULT: Context preserved via iterative compression summaries, no silent data loss,
        no wasted LLM calls on slices that don't fit
```

### Smart Slice-First Algorithm (Key Innovation)

**Problem with naive approach:** Blindly compressing with fraction=0.7 might produce a slice of 50k tokens that exceeds the compressor's own context window (e.g., 32k). The Compressor agent then fails or truncates — wasting an LLM call and losing data.

**Solution:** Before invoking the Compressor agent, test whether the planned slice fits its context window:

1. **Start with desired fraction** (e.g., 0.70 for first round)
2. **Compute discard count**: `discard = int(len(active_set) * fraction)`
3. **Build target_messages** (the slice to compress) — same logic as core.py line 226-236
4. **Count tokens of target_messages + overhead** (system prompt, compression prompt template)
5. **Compare against compressor's available window**: `available_for_messages = max_compressor_tokens * 0.85`
6. **If slice too large**: Halve the fraction (`fraction *= 0.5`) and go to step 2
7. **If slice fits**: Invoke Compressor agent — we know it will succeed

This is already partially implemented in `compression/core.py` lines 131-284 (the overfeeding check), but that code only runs ONCE with a fixed fraction. We need to make it ITERATIVE: if the test fails, reduce fraction and retest before giving up.

**Minimum fraction guard:** Don't go below fraction=0.05 (discard at least 5% or give up). If even 5% of history won't fit the compressor's window, something is fundamentally wrong (e.g., a single massive message).

---

## Step 0: Disable Silent Truncation in llm/base.py

### Problem

At `llm/base.py` lines 343-374, when estimated tokens exceed `max_input_tokens`, `_truncate_input_messages_roughly()` silently drops content BEFORE raising `ContextWindowExceeded`. This loses context that compression could have preserved.

### Solution

Change the behavior to raise `ContextWindowExceeded` immediately when tokens exceed the limit. Remove truncation as a "first defense" — it's a band-aid that hides the real problem (conversation too large for endpoint).

### Implementation Details

File: `agent_cascade/llm/base.py`, lines 331-374

Current code:
```python
if max_input_tokens > 0:
    agent_name = generate_cfg.pop('agent_name', 'Unknown')

    # Overflow guard: token estimates are imperfect, so allow a small tolerance
    # margin. Within tolerance: truncate as safety net. Beyond tolerance: raise
    # ContextWindowExceeded instead of silently dropping messages (prevents
    # divergence between LLM payload and instance.conversation).
    try:
        estimated_tokens = sum(get_message_stats(m)['tokens'] for m in messages)
    except Exception:
        estimated_tokens = None  # If counting fails, fall through to truncation as backstop

    if estimated_tokens is not None and estimated_tokens > max_input_tokens:
        # Truncate first as the primary defense against context overflow.
        messages = _truncate_input_messages_roughly(
            messages=messages,
            max_tokens=max_input_tokens,
            agent_name=agent_name,
            on_token_count_cb=on_token_count_cb,
        )

        # Re-check after truncation: only raise if truncation failed to reduce tokens.
        try:
            truncated_tokens = sum(get_message_stats(m)['tokens'] for m in messages)
        except Exception:
            truncated_tokens = None

        if truncated_tokens is not None and truncated_tokens >= estimated_tokens:
            # Truncation didn't help at all — something is wrong, raise.
            raise ContextWindowExceeded(
                f"Context window exceeded after truncation [{agent_name}]: "
                f"~{truncated_tokens} tokens vs {max_input_tokens} limit. "
                f"Truncation failed to reduce payload. Compression should have prevented this."
            )
    else:
        # No overflow detected, still run truncation as a safety net for edge cases.
        messages = _truncate_input_messages_roughly(
            messages=messages,
            max_tokens=max_input_tokens,
            agent_name=agent_name,
            on_token_count_cb=on_token_count_cb,
        )
```

Replace with:
```python
if max_input_tokens > 0:
    agent_name = generate_cfg.pop('agent_name', 'Unknown')

    # Overflow guard: if estimated tokens exceed limit, raise ContextWindowExceeded
    # immediately so upstream can compress. No silent truncation — compression preserves
    # meaning; truncation just drops content.
    try:
        estimated_tokens = sum(get_message_stats(m)['tokens'] for m in messages)
    except Exception:
        # If counting fails, log warning and continue — let the API handle it.
        logger.warning(
            f"[{agent_name}] Token estimation failed, skipping overflow check. "
            f"API may reject with context-exceeded error."
        )
        estimated_tokens = None

    if estimated_tokens is not None and estimated_tokens > max_input_tokens:
        # Raise immediately — no truncation. Upstream (execution engine) will compress.
        raise ContextWindowExceeded(
            f"Context window exceeded [{agent_name}]: "
            f"~{estimated_tokens} tokens vs {max_input_tokens} limit. "
            f"Compression required before retry."
        )
```

**Key changes:**
1. Removed all calls to `_truncate_input_messages_roughly()` — no more silent truncation
2. Raise `ContextWindowExceeded` immediately when estimated > limit
3. If token counting fails, log warning and let API handle it (graceful degradation)
4. Simplified logic significantly

**Note:** The function `_truncate_input_messages_roughly()` can be kept in the codebase but will no longer be called from this path. It may still be useful for other purposes or as a future last-resort option with explicit opt-in.

### Risk Assessment for Removing Silent Truncation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Edge cases where compression doesn't trigger but truncation would have saved the call | Low | Medium | Iterative compression in Steps 1-3 handles this; pre-LLM checks still run at >95% |
| Token estimation is inaccurate, causes false positives | Low | Low | Token estimates are rough but directionally correct; small overestimates just trigger compression earlier (harmless) |
| API-level truncation still happens for some backends | Very Low | Low | Those errors will surface as context-exceeded and be handled by fallback compression flow |

---

## Step 1: Add FallbackCompressionRequired Exception

### Implementation Details

File: `agent_cascade/exceptions.py`

Add after `ContextWindowExceeded`:

```python
class FallbackCompressionRequired(Exception):
    """Raised by APIRouter when a context-exceeded error occurs during fallback.
    
    Signals to the ExecutionEngine that it should iteratively compress the agent's
    conversation until it fits an available endpoint, before retrying.
    
    Attributes:
        instance_name: The agent instance name that needs compression
        agent_type: The agent type (e.g., 'coder', 'researcher')
        failed_endpoint: Name/model of the endpoint that rejected due to context size
        original_error: The underlying ContextWindowExceeded or API error
    """
    def __init__(self, instance_name: str, agent_type: str, failed_endpoint: str, original_error: Exception = None):
        self.instance_name = instance_name
        self.agent_type = agent_type
        self.failed_endpoint = failed_endpoint
        self.original_error = original_error
        super().__init__(
            f"Context window exceeded on fallback endpoint '{failed_endpoint}' for "
            f"'{instance_name}' ({agent_type}). Iterative compression required before retry."
        )
```

---

## Step 2: Modify call_with_fallback() to Raise FallbackCompressionRequired

### Implementation Details

File: `agent_cascade/api_router.py`, lines 1445-1451 (inside the except block at line 1437)

Current code:
```python
_inst_name_for_cursor = kwargs.get('agent_instance_name')
if _inst_name_for_cursor and self._is_context_exceeded_error(e):
    new_pos = self.advance_instance_endpoint(_inst_name_for_cursor)
    logger.warning(
        f"[APIRouter] Context window exceeded for '{_inst_name_for_cursor}' "
        f"on endpoint '{endpoint_name}'. Cursor advanced to {new_pos}. "
        f"Next engine-level retry will use a different endpoint."
    )
```

Replace with:
```python
_inst_name_for_cursor = kwargs.get('agent_instance_name')
if _inst_name_for_cursor and self._is_context_exceeded_error(e):
    # For Compressor agents: just advance cursor (they handle their own compression)
    if agent_type.lower().startswith('compressor'):
        new_pos = self.advance_instance_endpoint(_inst_name_for_cursor)
        logger.warning(
            f"[APIRouter] Context window exceeded for Compressor '{_inst_name_for_cursor}' "
            f"on endpoint '{endpoint_name}'. Cursor advanced to {new_pos}."
        )
    else:
        # Advance cursor NOW so retry uses a different (hopefully larger) endpoint after compression.
        new_pos = self.advance_instance_endpoint(_inst_name_for_cursor)
        logger.warning(
            f"[APIRouter] Context window exceeded for '{_inst_name_for_cursor}' "
            f"on endpoint '{endpoint_name}'. Triggering iterative fallback compression. "
            f"Cursor advanced to {new_pos}."
        )
        # Lazy import to avoid potential circular imports
        from agent_cascade.exceptions import FallbackCompressionRequired
        raise FallbackCompressionRequired(
            _inst_name_for_cursor, agent_type, endpoint_name, original_error=e
        ) from e
```

**Key design decision:** Cursor advances BEFORE raising. After compression, `get_endpoint_chain()` returns a rotated chain starting from the next endpoint. If we didn't advance, compression would run but the same failing endpoint would be tried again.

---

## Step 3: Smart Slice-First Iterative Compression in Execution Engine

### Implementation Details

File: `agent_cascade/execution_engine.py`, inside `_execute_llm_call_with_retry()`.

**EXACT PLACEMENT:** The new handler must be placed BEFORE the generic `except Exception as e:` block at line 2943. This is critical because `FallbackCompressionRequired` inherits from `Exception`.

Current structure (lines 2943-2960):
```python
            except Exception as e:
                with instance._compression_lock:
                    instance._streaming_responses = []

                # Check if this is a termination-abort error from api_router — exit cleanly without retrying.
                _is_termination_abort = (
                    isinstance(e, RuntimeError) and 
                    len(e.args) >= 1 and 
                    e.args[0] and 
                    "has been terminated" in str(e.args[0])
                )
                
                if _is_termination_abort:
```

Replace with:
```python
            except FallbackCompressionRequired as fcr:
                # Context window exceeded during fallback to smaller endpoint.
                # Use SMART SLICE-FIRST iterative compression: before each compression,
                # test whether the slice fits the compressor's window. If not, halve
                # the fraction and retest. Only compress when we know it will succeed.
                
                inst_name = fcr.instance_name
                
                # Get instance from pool
                instance = self.pool.get_instance(inst_name)
                if not instance:
                    logger.error(
                        f"[FALLBACK_COMPRESSION] Instance {inst_name} not found in pool. "
                        f"Cannot compress after context-exceeded on '{fcr.failed_endpoint}'."
                    )
                    retry_count += 1
                    continue
                
                # Clear streaming responses under lock (matching existing pattern)
                with instance._compression_lock:
                    instance._streaming_responses = []
                
                # ── Configuration ──
                MAX_COMPRESSION_ROUNDS = 5          # Max outer loop iterations
                INITIAL_FRACTION = 0.70             # Start with 70% discard
                MIN_SLICE_FRACTION = 0.05           # Don't go below 5% (single massive message guard)
                
                logger.info(
                    f"[FALLBACK_COMPRESSION] Starting smart slice-first iterative compression "
                    f"for {inst_name} after context-exceeded on '{fcr.failed_endpoint}'. "
                    f"Max rounds: {MAX_COMPRESSION_ROUNDS}, initial fraction: {INITIAL_FRACTION}"
                )
                
                agent_type = fcr.agent_type
                
                for round_num in range(1, MAX_COMPRESSION_ROUNDS + 1):
                    logger.info(
                        f"[FALLBACK_COMPRESSION] === Round {round_num}/{MAX_COMPRESSION_ROUNDS} "
                        f"for {inst_name} ==="
                    )
                    
                    # Check overfeeding before each round
                    conv = self.pool.get_conversation(inst_name)
                    if not conv:
                        logger.error(f"[FALLBACK_COMPRESSION] No conversation found for {inst_name}")
                        break
                    
                    messages = []
                    llm_messages = []
                    self._rebuild_working_set(messages, llm_messages, inst_name)
                    
                    if not llm_messages:
                        logger.error(f"[FALLBACK_COMPRESSION] Empty working set for {inst_name}")
                        break
                    
                    if self.compression_handler.check_overfeeding(instance, llm_messages):
                        logger.warning(
                            f"[FALLBACK_COMPRESSION] Overfeeding detected for {inst_name} "
                            f"at round {round_num}. Raising ContextWindowExceeded."
                        )
                        raise ContextWindowExceeded(
                            f"Overfeeding detected during fallback compression for {inst_name} "
                            f"(context exceeded on '{fcr.failed_endpoint}')"
                        ) from fcr
                    
                    # ── SMART SLICE-FIRST: Find a fraction whose slice fits compressor window ──
                    # Start with INITIAL_FRACTION, halve iteratively until test passes or min reached.
                    target_fraction = INITIAL_FRACTION
                    
                    from agent_cascade.compression.helpers import compute_discard_count
                    from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
                    from agent_cascade.utils.utils import extract_text_from_message
                    from agent_cascade.llm.schema import Message as SchemaMessage
                    from agent_cascade.settings import CHARS_PER_TOKEN_ESTIMATE
                    
                    # Get compressor's available window (same logic as core.py lines 131-163)
                    available_for_messages = None
                    try:
                        comp_chain = self.pool.api_router.get_endpoint_chain('Compressor')
                        max_compressor_tokens = 0
                        for cfg in comp_chain:
                            ep_limit = cfg.get('max_input_tokens', 0)
                            if ep_limit and ep_limit > max_compressor_tokens:
                                max_compressor_tokens = ep_limit
                        
                        # Fallback: check compressor agent config directly if endpoint chain lookup fails
                        # (matches core.py lines 147-156)
                        if not max_compressor_tokens:
                            comp_agent = self.pool.get_agent('Compressor')
                            if comp_agent:
                                max_tokens = None
                                if hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'generate_cfg'):
                                    max_tokens = comp_agent.llm.generate_cfg.get('max_input_tokens')
                                elif hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'cfg'):
                                    max_tokens = comp_agent.llm.cfg.get('max_input_tokens')
                                if max_tokens:
                                    max_compressor_tokens = max_tokens
                        
                        if max_compressor_tokens:
                            available_for_messages = int(max_compressor_tokens * 0.85)
                    except Exception as e:
                        logger.debug(f"[FALLBACK_COMPRESSION] Could not determine compressor window: {e}")
                    
                    # Get active set for slicing
                    history = self.pool.get_conversation(inst_name)
                    active_start_idx, active_set, latest_summary_idx = (
                        self.pool.get_compression_target_set_from_conversation(inst_name, history)
                    )
                    
                    if not active_set or len(active_set) < 3:
                        logger.warning(
                            f"[FALLBACK_COMPRESSION] Active set too small ({len(active_set) if active_set else 0}) "
                            f"for safe compression at round {round_num}."
                        )
                        break
                    
                    # ── Inner loop: halve fraction until slice fits ──
                    slice_found = False
                    final_fraction = None
                    final_target_messages = None
                    
                    for slice_attempt in range(10):  # Max 10 halvings (0.7 → 0.0007 is more than enough)
                        if target_fraction < MIN_SLICE_FRACTION:
                            logger.warning(
                                f"[FALLBACK_COMPRESSION] Fraction {target_fraction:.4f} below minimum "
                                f"{MIN_SLICE_FRACTION}. Cannot find slice that fits compressor window."
                            )
                            break
                        
                        discard_count = compute_discard_count(active_set, target_fraction, force=True)
                        if discard_count <= 0:
                            logger.debug(
                                f"[FALLBACK_COMPRESSION] discard_count=0 at fraction={target_fraction:.4f}, halving..."
                            )
                            target_fraction *= 0.5
                            continue
                        
                        # Build target_messages (same logic as core.py lines 226-236)
                        if latest_summary_idx != -1:
                            test_target_messages = active_set[:discard_count]
                        else:
                            u0_index = active_start_idx - 1
                            test_target_messages = [history[u0_index]] + list(active_set[:discard_count])
                        
                        # Count tokens of target_messages (same logic as core.py lines 240-250)
                        target_token_count = 0
                        for msg in test_target_messages:
                            if isinstance(msg, dict):
                                wrapped = SchemaMessage(**msg)
                            else:
                                wrapped = msg
                            content = extract_text_from_message(wrapped, add_upload_info=False)
                            tokens = qwen_count(content)
                            target_token_count += tokens
                        
                        # Estimate overhead (same logic as core.py lines 252-264)
                        comp_agent = self.pool.get_agent('Compressor')
                        sys_prompt_tokens = 50
                        if comp_agent and hasattr(comp_agent, 'system_message'):
                            sys_prompt_tokens = len(str(comp_agent.system_message)) // CHARS_PER_TOKEN_ESTIMATE
                        
                        from agent_cascade.prompts.dna import COMPRESSION_PROMPT
                        prompt_template_chars = len(COMPRESSION_PROMPT.format(history_text=""))
                        prompt_overhead_tokens = sys_prompt_tokens + (prompt_template_chars // CHARS_PER_TOKEN_ESTIMATE)
                        
                        total_estimated = target_token_count + prompt_overhead_tokens
                        
                        # Test against compressor window
                        if available_for_messages is not None and total_estimated > available_for_messages:
                            logger.info(
                                f"[FALLBACK_COMPRESSION] Slice test FAILED at fraction={target_fraction:.4f}: "
                                f"~{total_estimated} tokens vs ~{available_for_messages} available. Halving..."
                            )
                            target_fraction *= 0.5
                            continue
                        
                        # Test passed! This slice will fit the compressor's window.
                        logger.info(
                            f"[FALLBACK_COMPRESSION] Slice test PASSED at fraction={target_fraction:.4f}: "
                            f"~{total_estimated} tokens (discard {discard_count} messages). "
                            f"Proceeding with compression."
                        )
                        final_fraction = target_fraction
                        final_target_messages = test_target_messages
                        slice_found = True
                        break
                    
                    if not slice_found:
                        logger.error(
                            f"[FALLBACK_COMPRESSION] Could not find a slice that fits compressor window "
                            f"for {inst_name} at round {round_num}. Giving up."
                        )
                        raise ContextWindowExceeded(
                            f"Smart slicing failed for {inst_name}: no slice of active history "
                            f"fits the compressor's context window. Cannot compress further."
                        ) from fcr
                    
                    # ── Invoke compression with the validated fraction ──
                    try:
                        from agent_cascade.compression.core import compress_context as _compress
                        
                        result = _compress(
                            agent_pool=self.pool,
                            target_agent_name=inst_name,
                            fraction=final_fraction,
                            mode='auto',
                            force=True,
                        )
                        
                        if not result.success:
                            logger.warning(
                                f"[FALLBACK_COMPRESSION] Round {round_num} compression failed for "
                                f"{inst_name}: {result.error}. Trying next round."
                            )
                            continue
                        
                        # Compression succeeded — rebuild working set from compressed pool state
                        self._rebuild_working_set(messages, llm_messages, inst_name)
                        
                        # Update instance metadata (matching execute_force_compression pattern)
                        instance.compression_summary = result.summary_text
                        conv = self.pool.get_conversation(inst_name)
                        if conv:
                            for idx, msg in enumerate(conv):
                                c = msg_field(msg, 'content', '')
                                if isinstance(c, str) and '<context_summary>' in c:
                                    instance.latest_marker_index = idx
                        
                        logger.info(
                            f"[FALLBACK_COMPRESSION] Round {round_num} succeeded for {inst_name}: "
                            f"fraction={final_fraction:.4f}, discarded {result.messages_discarded} messages, "
                            f"tokens {result.tokens_before} → {result.tokens_after}"
                        )
                        
                        # ── Post-compression check: Does compressed payload fit next endpoint? ──
                        try:
                            chain = self.pool.api_router.get_endpoint_chain(
                                agent_type, instance_name=inst_name
                            )
                            if chain:
                                next_limit = chain[0].get('max_input_tokens', 0)
                                if next_limit > 0:
# Estimate tokens of compressed payload using actual token counting
                                     estimated = 0
                                     for msg in llm_messages:
                                         content = extract_text_from_message(msg, add_upload_info=False)
                                         estimated += qwen_count(content)
                            
                                    logger.info(
                                        f"[FALLBACK_COMPRESSION] Post-compression check for {inst_name}: "
                                        f"estimated ~{estimated} tokens vs next endpoint limit {next_limit}"
                                    )
                                    
                                    if estimated <= next_limit * 0.95:  # 5% safety margin
                                        # Payload fits — inject notification and resume agent
                                        notif_msg = Message(
                                            role=USER,
                                            content=(
                                                f"[SYSTEM] Context exceeded on endpoint '{fcr.failed_endpoint}'. "
                                                f"Compression applied ({round_num} round(s)), full context preserved in summary. Continue."
                                            )
                                        )
                                        self._append_and_log(instance, notif_msg)
                                        
                                        # Resume all instances (compression may have halted them)
                                        try:
                                            self.pool.resume_all_instances()
                                        except Exception:
                                            pass
                                        
                                        logger.info(
                                            f"[FALLBACK_COMPRESSION] Payload fits next endpoint after "
                                            f"{round_num} compression round(s). Resuming {inst_name}."
                                        )
                                        
                                        # Continue outer retry loop with compressed messages
                                        break
                                    else:
                                        logger.warning(
                                            f"[FALLBACK_COMPRESSION] Compressed payload (~{estimated} tokens) "
                                            f"still exceeds next endpoint limit ({next_limit}). "
                                            f"Continuing to round {round_num + 1}..."
                                        )
                                else:
                                    # No clear limit — assume it fits and continue
                                    logger.debug(
                                        f"[FALLBACK_COMPRESSION] Next endpoint has no max_input_tokens configured. "
                                        f"Assuming compressed payload fits."
                                    )
                                    break
                        except Exception as chain_err:
                            # Non-fatal — continue retry anyway
                            logger.debug(
                                f"[FALLBACK_COMPRESSION] Could not verify next endpoint limit for {inst_name}: "
                                f"{chain_err}. Continuing retry."
                            )
                            break
                    
                    except ContextWindowExceeded:
                        raise
                    except Exception as comp_err:
                        logger.error(
                            f"[FALLBACK_COMPRESSION] Round {round_num} raised exception for {inst_name}: "
                            f"{comp_err}", exc_info=True
                        )
                        # Continue to next round
                    
                    # After each round, check if we should trigger automatic forced compression
                    # via the normal pre-LLM checks (usage_pct > 95%). The retry loop will
                    # naturally hit _pre_llm_checks on the next iteration if needed.
                
                else:
                    # Exhausted all compression rounds without success
                    logger.error(
                        f"[FALLBACK_COMPRESSION] Exhausted {MAX_COMPRESSION_ROUNDS} compression rounds "
                        f"for {inst_name}. Raising ContextWindowExceeded."
                    )
                    raise ContextWindowExceeded(
                        f"Iterative compression exhausted ({MAX_COMPRESSION_ROUNDS} rounds) for {inst_name}. "
                        f"Context still exceeds available endpoint limits after aggressive compression. "
                        f"Original error: context exceeded on '{fcr.failed_endpoint}'."
                    ) from fcr
                
                # If we got here, compression succeeded and payload fits — continue retry loop
                # llm_messages has been updated in-place by _rebuild_working_set
                continue
                
            except Exception as e:
                with instance._compression_lock:
                    instance._streaming_responses = []

                # Check if this is a termination-abort error from api_router — exit cleanly without retrying.
                _is_termination_abort = (
                    isinstance(e, RuntimeError) and 
                    len(e.args) >= 1 and 
                    e.args[0] and 
                    "has been terminated" in str(e.args[0])
                )
                
                if _is_termination_abort:
```

**Key design decisions:**

1. **Smart slice-first (inner loop):** Before each compression, iteratively halve the fraction until `target_token_count + overhead <= available_for_messages`. This ensures we never invoke the Compressor agent with a payload it can't handle. No wasted LLM calls.

2. **Outer loop (compression rounds):** Up to 5 rounds of compression. Each round compresses the remaining active history further. After each successful compression, check if the result fits the next endpoint. If not, continue to next round.

3. **Uses existing infrastructure:** Reuses `compute_discard_count()`, token counting from `qwen_count()`, overhead estimation from `core.py` lines 252-264, and `compress_context()` itself. The smart slicing logic is essentially the overfeeding check from core.py made iterative.

4. **Post-compression endpoint check:** After each successful compression, verifies the compressed payload fits the next endpoint in the rotated chain (cursor was advanced by api_router). If not, continues outer loop for another round.

5. **Automatic forced compression integration:** After each round, the retry loop naturally continues to the next LLM call attempt, which triggers `_pre_llm_checks()`. If usage is still above 95%, that will trigger the normal forced compression flow — no special wiring needed.

6. **Locking and safety:** Clears streaming responses under `instance._compression_lock` matching existing patterns. Uses lazy imports to avoid circular dependencies.

---

## Step 4: Import FallbackCompressionRequired at Module Level

File: `agent_cascade/execution_engine.py`, near top imports (line 48):

Current:
```python
from agent_cascade.exceptions import CharacterRunDetected, MaxTokenExceeded, ContextWindowExceeded
```

Update to:
```python
from agent_cascade.exceptions import (
    CharacterRunDetected,
    MaxTokenExceeded,
    ContextWindowExceeded,
    FallbackCompressionRequired,
)
```

---

## Testing Plan

### Unit Tests

File: `tests/test_fallback_compression.py` (new)

#### Test 1: Silent truncation disabled — ContextWindowExceeded raised immediately

```python
def test_no_silent_truncation_raises_context_exceeded():
    """When tokens exceed max_input_tokens, raise ContextWindowExceeded instead of truncating."""
    messages = [Message(role=USER, content="x" * 100000)]
    llm = MockLLM(generate_cfg={'max_input_tokens': 1000})
    
    with pytest.raises(ContextWindowExceeded) as exc_info:
        llm.chat(messages=messages, stream=False)
    
    assert "Compression required" in str(exc_info.value)
```

#### Test 2: FallbackCompressionRequired raised for non-Compressor agents

```python
def test_fallback_compression_required_raised():
    router = APIRouter(default_llm_cfg=TEST_CFG)
    router.add_endpoint("large", TEST_CFG_128K)
    router.add_endpoint("small", TEST_CFG_32K)
    router.set_agent_priorities("coder", ["large", "small"])
    
    def mock_call(llm_cfg):
        if llm_cfg['model'] == 'small-model':
            raise RuntimeError("exceed_context_size_error")
        return "ok"
    
    router._pool = MockPool()
    
    with pytest.raises(FallbackCompressionRequired) as exc_info:
        router.call_with_fallback("coder", mock_call, agent_instance_name="Coder_1")
    
    assert exc_info.value.instance_name == "Coder_1"
```

#### Test 3: Compressor agents skip compression-on-fallback

```python
def test_compressor_skips_fallback_compression():
    """Compressor agents should just advance cursor, not raise FallbackCompressionRequired."""
```

#### Test 4: Smart slicing — slice fits on first attempt

```python
def test_smart_slicing_slice_fits_first_attempt():
    """When initial fraction=0.7 produces a slice that fits compressor window, compress immediately."""
    # Setup: active_set of 100 messages, compressor window 32k
    # fraction=0.7 → 70 messages → ~25k tokens → fits!
    # Verify: no halving needed, compression runs with fraction=0.7
```

#### Test 5: Smart slicing — requires halving to fit

```python
def test_smart_slicing_halves_fraction_to_fit():
    """When initial slice too large, halve fraction iteratively until it fits."""
    # Setup: active_set of 200 messages, compressor window 32k
    # fraction=0.7 → 140 messages → ~60k tokens → TOO LARGE
    # fraction=0.35 → 70 messages → ~30k tokens → FITS!
    # Verify: halving happened once, compression runs with fraction=0.35
```

#### Test 6: Smart slicing — hits minimum fraction guard

```python
def test_smart_slicing_minimum_fraction_guard():
    """When even 5% slice is too large (single massive message), give up gracefully."""
    # Setup: active_set with one 100k-token message, compressor window 32k
    # fraction=0.7 → still too large → halve → ... → fraction < 0.05 → stop
    # Verify: raises ContextWindowExceeded with clear message about no valid slice
```

#### Test 7: Iterative compression — succeeds after one round

```python
def test_iterative_compression_succeeds_first_round():
    """When compressed payload fits next endpoint after first round, resume agent."""
```

#### Test 8: Iterative compression — requires multiple rounds

```python
def test_iterative_compression_multiple_rounds():
    """When first round doesn't reduce enough, outer loop continues with more rounds."""
    # Setup: compressor reduces to 25k but next endpoint is 16k
    # Round 1: compress → 25k → still too large for 16k endpoint
    # Round 2: compress remaining active history → 10k → fits!
    # Verify: two rounds executed, agent resumes
```

#### Test 9: Iterative compression — exhausts all rounds

```python
def test_iterative_compression_exhausted_raises():
    """When all 5 rounds fail to produce a fitting payload, raise ContextWindowExceeded."""
```

#### Test 10: Post-compression endpoint-aware check

```python
def test_post_compression_size_check_uses_next_endpoint():
    """After compression, verify payload fits the next endpoint in rotated chain."""
```

#### Test 11: Overfeeding detection during iterative compression

```python
def test_overfeeding_stops_iterative_compression():
    """If overfeeding detected during iterative compression, raise ContextWindowExceeded."""
```

#### Test 12: Slice token counting accuracy

```python
def test_slice_token_counting_matches_core_py_logic():
    """Verify slice token counting uses same logic as core.py lines 240-264."""
    # Ensure qwen_count(), extract_text_from_message(), overhead estimation match exactly
```

### Integration Test Scenario

**Scenario:** Agent with 60k conversation falls back from 128k to 32k endpoint.

Setup:
1. Create agent pool with two endpoints: Endpoint A (128k, disabled), Endpoint B (32k)
2. Start a Coder agent and build up its conversation to ~50k tokens
3. Force Endpoint A to fail on LLM call
4. Observe iterative compression behavior

Expected:
- `llm/base.py` raises `ContextWindowExceeded` instead of truncating (Step 0)
- `call_with_fallback()` catches it, raises `FallbackCompressionRequired` (Step 2)
- Execution engine enters iterative loop (Step 3):
  - Attempt 1: fraction=0.70 → check size vs next endpoint → if fits, done; if not, continue
  - Attempt N: progressively more aggressive until fit or exhausted
- Agent conversation has compression marker(s) after success
- No silent truncation occurs

**Verification checklist:**
- [ ] No calls to `_truncate_input_messages_roughly()` in the error path
- [ ] Compression markers `<context_summary>` appear in conversation log
- [ ] Token count after compression is below next endpoint's limit
- [ ] Logs show progressive fraction values if multiple attempts needed
- [ ] Agent continues working with compressed context

---

## Risk Assessment

### Potential Regressions

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Removing truncation causes more ContextWindowExceeded errors that compression can't fix | Low | Medium | Smart slicing + iterative compression handles most cases; max-rounds guard terminates overfeeding agents cleanly |
| Token estimation inaccuracy causes false positives (raise when API would have accepted) | Low | Low | Small overestimates just trigger earlier compression (harmless); compression preserves meaning unlike truncation |
| Smart slicing token counting is slow for large active sets | Medium | Medium | qwen_count() on each slice test could be expensive; mitigated by halving quickly (logarithmic convergence: 0.7 → 0.35 → 0.175 → done in ~3 tests max) |
| Iterative compression is slow (multiple rounds × multiple LLM calls) | Medium | Medium | Each round takes ~2-5 seconds; worst case 25 seconds vs silent truncation. Acceptable trade-off for correctness. Cooldown limits apply. |
| Compression can't reduce enough for very small endpoints (<8k tokens) | Low | Medium | After 5 rounds at aggressive fractions, conversation is just summaries; if still too large, raise ContextWindowExceeded — clear failure message |
| Circular import from new exception | Very Low | High | Exception is simple (no imports); lazy import in api_router.py avoids any risk |

### Edge Cases to Verify

1. **Agent with only one endpoint:** If that endpoint rejects with context-exceeded, iterative compression still runs; after compression, same endpoint tried again (cursor wraps). If still too large after 5 rounds, raises ContextWindowExceeded.

2. **All endpoints same size:** Smart slicing ensures slice fits compressor; iterative compression reduces payload until it fits; if it didn't fit before compression, smaller result should fit.

3. **Compression during fallback fails:** Logged, continues to next round with fresh slice calculation. After 5 failures, raises ContextWindowExceeded.

4. **Concurrent fallback compressions on different agents:** Each agent has its own instance lock; `compress_context()` creates Compressor instances which run via engine.run(); serialized by existing concurrency controls.

5. **Nested compression (compression triggers another compression):** Existing cooldown and `compression_max_attempts` guards prevent infinite loops.

6. **Exception handler placement:** Must be BEFORE `except Exception as e:` to avoid being swallowed. Plan shows exact before/after structure.

7. **Single massive message in active set:** If one message is larger than the compressor's window, smart slicing will halve fraction down to MIN_SLICE_FRACTION=0.05 and still fail. Raises ContextWindowExceeded with clear message — no wasted LLM calls attempting to compress an uncompressible payload.

---

## Implementation Order

1. **Step 0:** Disable silent truncation in llm/base.py — raise ContextWindowExceeded immediately
2. **Step 1:** Add FallbackCompressionRequired exception to exceptions.py
3. **Step 2:** Modify call_with_fallback() to raise it on context-exceeded for non-Compressor agents
4. **Step 3:** Handle in execution engine with iterative compression loop (5 attempts, progressive fractions)
5. **Step 4:** Import FallbackCompressionRequired at module level in execution_engine.py

Test after each step. Integration test after all steps complete.

---

## Success Criteria

- [ ] No silent context truncation anywhere in the codebase
- [ ] ContextWindowExceeded raised immediately when tokens exceed limit (llm/base.py)
- [ ] Iterative compression runs on fallback to smaller endpoint, up to 5 attempts with progressive aggressiveness
- [ ] Agent conversation contains compression marker(s) after successful fallback compression
- [ ] Existing behavior unchanged for agents that never fall back or exceed limits
- [ ] Compressor agents unaffected (still just advance cursor)
- [ ] Unit tests pass for all new code paths including iterative scenarios
- [ ] Integration test scenario completes without context loss

---

## Second Review Corrections Applied

| # | Issue | Fix | Location |
|---|-------|-----|----------|
| 1 | Step 0 else clause still called `_truncate_input_messages_roughly()` when no overflow | Verified replacement code has no else block — if tokens under limit, do nothing. No change needed (already correct). | Step 0 pseudocode |
| 2 | Compressor window detection had no fallback when `get_endpoint_chain('Compressor')` fails/returns empty | Added fallback logic matching core.py lines 147-156: check compressor agent config directly via `comp_agent.llm.generate_cfg.get('max_input_tokens')` or `.cfg`. | Step 3, lines ~404-417 |
| 3 | Post-compression estimate used inaccurate char-based formula (`// TOKEN_ESTIMATE_CHAR_DIVISOR`) | Replaced with actual token counting using `qwen_count(extract_text_from_message(msg))` for each message in llm_messages. | Step 3, lines ~561-565 |
| 4 | Redundant `goto_success = True` flag set but never checked | Removed all three occurrences; replaced with direct `break` statements. Cleaner control flow. | Step 3, lines ~595, ~609, ~617 |

All fixes applied. Plan is now implementation-ready.