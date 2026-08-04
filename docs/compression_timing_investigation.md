# Compression Timing Investigation: "Lazy" Forced Compression

**Date:** 2026-08-04
**Author:** compression_timing_investigator (research agent)
**Status:** Investigation complete — drives fix planning
**Scope:** `N:\work\WD\AgentCascade\agent_cascade`

---

## 1. Executive Summary

Forced (automatic) compression in AgentCascade is **triggered lazily**: the only automatic
compression decision point runs **immediately before each LLM call** (Phase 2 of the
execution loop). Tool results are appended to the conversation **after** the LLM response is
processed (Phase 4), with **no overflow check in between**. Consequently, when a tool result
(or a batch of sequential tool results) pushes the conversation over the model's context
limit, the system does not notice until the *next* LLM call is about to start. By then the
oversized context may already cause:

- **Silent, irreversible input truncation** inside `llm/base.py::_truncate_input_messages_roughly`
  (the API-level safety net), which strips *oldest non-system messages* from the payload —
  losing tool-call/result pairs and breaking the conversation's internal consistency.
- **`ContextWindowExceeded` API errors** when the truncation guard is bypassed or the backend
  rejects the payload (`exceptions.py::ContextWindowExceeded`, detected in `api_router.py:1165`).
- **Conversation corruption**: truncating only the LLM payload while the pool's full
  `conversation` still contains the oversized messages creates divergence between the working
  set and what the model actually sees.
- **Retry loops**: after context-exceeded errors, the engine advances the endpoint cursor and
  retries (`execution_engine.py:2391`), but without compression the same oversized payload is
  retried against another endpoint, wasting attempts and time.

The fix direction: **insert a proactive overflow check immediately after tool results are
appended** (inside Phase 4), using the existing token-accounting machinery, so compression
runs *before* the conversation ever crosses the limit. This report maps the current flow,
identifies the exact insertion points, and lists the risks, assumptions, and unknowns.

---

## 2. How Context Usage Is Measured

### 2.1 Token counting — `ExecutionEngine._count_history_tokens`
**File:** `agent_cascade/execution_engine.py` lines **4746–4790** (method body starts at 4746; cache update at 4764–4768).

- Counts tokens via `get_message_stats()` (per-message LRU-cached, character-based estimate),
  with a fallback to a rough `len(content) // CHARS_PER_TOKEN_ESTIMATE` estimate if counting fails.
- Uses a per-instance cache:
  - `instance._cached_token_count` — cached cumulative count;
  - `instance._last_token_count_conversation_length` — length of conversation when cached.
- **Critical behavior:** every append/insert/trim/rebuild path in `agent_instance.py`
  (lines 334–543) sets `_last_token_count_conversation_length = -1`, invalidating the cache.

### 2.2 Ground-truth token counts from the LLM API
**File:** `agent_cascade/execution_engine.py` lines **173–182** (`_make_token_count_callback`),
lines **185–200** (`_make_usage_callback`).

- `_on_token_count(all_tokens, available_token, max_tokens)` sets
  `instance._last_actual_token_count = all_tokens` and
  `instance._allocated_max_input_tokens = max_tokens`.
- These are **ground truth** values measured by `llm/base.py` **during** an LLM call
  (see §3.3). They are *not* refreshed between LLM calls unless a call actually happens.

### 2.3 Max-token resolution — `ExecutionEngine._get_max_tokens`
**File:** `agent_cascade/execution_engine.py` lines **4737–4744** → delegates to
`agent_cascade/api_integration.py::_resolve_max_tokens` (lines ~1140–1216).

Priority order (authoritative first):
1. Live API Router per-agent-class limit (`pool.api_router.get_effective_max_tokens(agent_class)`);
2. Template static config (`llm.cfg.generate_cfg.max_input_tokens`);
3. Instance-cached `_allocated_max_input_tokens` (can be **stale**);
4. Runtime-detected `llm.generate_cfg.max_input_tokens` (shared, can be polluted across agents);
5. `DEFAULT_MAX_INPUT_TOKENS` (default **65000**, `settings.py:21–22`).

> **Note:** `DEFAULT_MAX_INPUT_TOKENS=65000` may be **smaller** than the model's real context
> window for large-context backends (e.g., 128k/200k+). Conversely, the dynamic detection in
> `llm/oai.py::_detect_context_window` (lines 275–372) *raises* `max_input_tokens` when it
> detects a larger window. Both directions of mismatch affect when compression fires.

### 2.4 Thresholds
**File:** `agent_cascade/settings.py`
- `COMPRESSION_FORCE_THRESHOLD` default **95.0** (%) — force compression (`settings.py:52–53`; docs confirm 95.0 in `docs/SYSTEM_DOCS.md:1068`).
- `COMPRESSION_WARNING_THRESHOLD` default **90.0** (%) — inject inline warning (`settings.py:54–55`; note: `docs/SYSTEM_DOCS.md:1069` says 85.0 — docs are **out of date** vs. code).
- `DEFAULT_COMPRESSION_COOLDOWN_SECONDS` default **2.0** (s) — min gap between forced compressions (`settings.py:48–49`).
- `DEFAULT_COMPRESSION_MAX_ATTEMPTS` default **100** — safety net before termination (`settings.py:50–51`).

---

## 3. Current Order of Operations (Where Checks Happen vs. Where They Should)

### 3.1 The main execution loop — `ExecutionEngine.run`
**File:** `agent_cascade/execution_engine.py`, Phase structure around lines **1215–1400**.

```
run(instance):
  ┌─ loop ─────────────────────────────────────────────────────────────┐
  │  Phase 1: _setup_turn (lines ~1355)                                 │
  │    - conv, llm_messages, response = setup (sys msg sync, slicing)   │
  │                                                                     │
  │  Phase 2: _pre_llm_checks (lines ~1380 → 1896–1955)                 │
  │    ⚠ COMPRESSION CHECK #1 (the ONLY automatic check)                │
  │      _check_and_trigger_compression(...)  [lines 1896–1955]         │
  │        - usage_pct > compression_force_threshold (95%)              │
  │          → _force_compression(...)  [line 1948–1949]                │
  │        - usage_pct > compression_warning_threshold (90%)            │
  │          → _inject_compression_warning(...) [line 1952–1953]        │
  │                                                                     │
  │  Phase 3: _execute_llm_call_with_retry (lines ~2428)                │
  │    - template.chat(...) → llm/base.py::chat()                       │
  │      - base.py truncates payload if > max_input_tokens              │
  │        (⚠ silent truncation — see §3.3)                             │
  │      - token count callback updates _last_actual_token_count         │
  │                                                                     │
  │  Phase 4: _process_response (lines ~1383, impl ~1540+)              │
  │    - if tool calls detected → _execute_detected_tools (3220–3731)   │
  │      - for each tool: execute → build fn_msg →                      │
  │        _append_and_log(instance, fn_msg)  [lines 3496–3506]         │
  │        ⚠ NO OVERFLOW CHECK HERE (the gap)                           │
  │      - response.append(fn_msg)                                      │
  │    - if tools ran → yield response; continue (line 1394–1395)       │
  │                                                                     │
  │  Phase 5: _post_turn_checks (lines 1398, 3890+)                     │
  │    - final answer? wait parallel agents? drain queue                │
  └──────────────────────────────────────────────────────────────────────┘
```

**Key finding:** The check at Phase 2 runs **before** each LLM call — so after tool results
are appended in Phase 4, the *next* check happens only at the start of the *next* iteration
of the loop. Between Phase 4 and Phase 2 (next iteration) the conversation can exceed the
limit **undetected**.

### 3.2 The compression check itself — `_check_and_trigger_compression`
**File:** `agent_cascade/execution_engine.py` lines **1896–1955**.

```python
max_tokens = self._get_max_tokens(instance)                    # line 1920
actual_tokens = instance._last_actual_token_count              # line 1922 (ground truth from last LLM call)
allocated_max = instance._allocated_max_input_tokens           # line 1923
if actual_tokens and allocated_max and allocated_max > 0:      # ~line 1925+
    delta_start = instance._last_token_count_conversation_length   # ⚠ invalidated to -1 by appends
    if delta_start < 0:
        current_tokens = self._count_history_tokens(messages, instance)   # full recount (expensive but correct)
        max_tokens_for_check = allocated_max
    else:
        delta_tokens = self._count_history_tokens(messages[delta_start:], dummy)  # estimate of new msgs
        current_tokens = actual_tokens + delta_tokens          # ground truth + delta estimate
        max_tokens_for_check = allocated_max
else:
    current_tokens = self._count_history_tokens(messages, instance)   # fallback full recount
    max_tokens_for_check = max_tokens

usage_pct = (current_tokens / max_tokens_for_check * 100) ...
if usage_pct > compression_force_threshold:                    # line 1948
    return self._force_compression(...)                        # line 1949
if usage_pct > compression_warning_threshold:                  # line 1952
    self._inject_compression_warning(...)                      # line 1953
```

**Why this is "lazy" — three compounding reasons:**

1. **Timing gap (primary):** the check is only invoked from Phase 2, *before* the LLM call.
   It never runs right after tool results are appended in Phase 4. With one large tool result
   (or several sequential tool calls in one turn), the conversation can cross the limit during
   Phase 4 and stay over it until the next loop iteration — by which time the LLM call is the
   very next step and will consume the oversized context.

2. **Delta estimate blind spot:** when `_last_token_count_conversation_length` is invalidated
   (`-1`) by tool-result appends, the code takes the *full recount* branch (`line 1942`) — which
   is correct but does not make the *timing* better. When the delta branch *is* taken
   (`delta_start >= 0`, i.e., no appends since the last LLM call), the estimate counts only
   `messages[delta_start:]` **using the working-set list passed to Phase 2** — but the tool
   results were appended to `instance.conversation` in the previous turn's Phase 4, and
   `_setup_turn` (line 1771–1776) rebuilds `messages`/`llm_messages` from the conversation each
   turn, so the delta *does* include them (via `_count_history_tokens(messages, instance)` when
   the cache is invalidated). The remaining risk is **estimate accuracy**: the per-message
   char-based estimate can undercount long tool outputs, and `get_message_stats()` fallbacks can
   drift from the real tokenizer count.

3. **Ground truth is stale by definition:** `_last_actual_token_count` reflects the token count
   of the *previous* LLM call's payload. It is refreshed only during an LLM call (callbacks at
   `execution_engine.py:173–200`). After tool results are appended, the true count is
   `ground_truth + new_messages`, where `new_messages` is an *estimate*. The estimate is only as
   good as the char-based fallback (`utils/utils.py::get_message_stats`).

### 3.3 The silent-truncation safety net — `llm/base.py::_truncate_input_messages_roughly`
**File:** `agent_cascade/llm/base.py` lines **851–1050** (invoked from `chat()` at **330–337**).

```python
if max_input_tokens > 0:                                        # line 330
    messages = _truncate_input_messages_roughly(
        messages=messages, max_tokens=max_input_tokens,
        agent_name=agent_name, on_token_count_cb=on_token_count_cb)
```

- Iterates messages from the **oldest** (skipping system) and removes/truncates content until
  under `max_tokens`.
- **This is the LAST line of defense and it is destructive**: it removes messages from the
  payload sent to the model *without* updating `instance.conversation` (the pool's full
  conversation still contains everything). The model then sees a **different history** than the
  one the pool believes it has. Tool-call/result pairs can be split (assistant tool_call kept,
  function result dropped, or vice versa), which OpenAI-compatible backends reject with a 400 —
  or worse, the model sees dangling tool calls.
- The `on_token_count_cb` invoked here is the source of the *next* iteration's ground truth
  (`_last_actual_token_count`), so after a truncation the next check compares against the
  *truncated* count — masking the fact that the pool conversation is still oversized.

### 3.4 Where the tool result append happens — `_execute_detected_tools`
**File:** `agent_cascade/execution_engine.py` lines **3220–3731** (append point **3496–3506**).

```python
fn_msg = Message(role=FUNCTION, name=tool_name, content=tool_result, extra={...})  # 3496–3504
self._append_and_log(instance, fn_msg)      # 3505  ← ⚠ overflow check should run here (or after)
response.append(fn_msg)                     # 3506
```

`_append_and_log` (lines **867–884**) atomically appends to `instance.conversation`,
`_cached_messages`, `_cached_llm_messages` (via `instance.append_message`, which invalidates
the token cache — `agent_instance.py:346–350`) and logs to the JSONL logger. **No token check
occurs in this path.**

### 3.5 Tool-result size limits (existing mitigations)
**File:** `agent_cascade/settings.py`
- `DEFAULT_TOOL_RESULT_MAX_CHARS` default **10000** (`settings.py:31`).
- `DEFAULT_READ_FILE_MAX_LINES` default **150** (`settings.py:32`).
- Tool outputs are truncated with spillover via `tool_utils.py::truncate_with_spillover`
  (line 178+) and `MAX_SPILL_SIZE`; per-tool char limits (`grep_char_limit`,
  `shell_char_limit`, `code_char_limit`) are configurable.

**Mitigation exists but is per-tool and char-based, not aggregate/token-based.** A single tool
result is capped, but a turn can execute **multiple** tools sequentially (loop at
`execution_engine.py:3277`), and the *sum* of all results plus the pre-existing conversation
can exceed the limit. Also, tool *arguments* from the LLM (function_call JSON) and the
assistant's reasoning content are not bounded by these caps.

---

## 4. Step-by-Step Flow: A Typical Tool-Using Turn

Assumptions: 65k default limit (`DEFAULT_MAX_INPUT_TOKENS`), force threshold 95% (→ ~61.75k
trigger), warning threshold 90%.

```
Step 0 (previous turn)  conversation has ~55,000 tokens (84.6%).
Step 1  Phase 2 check:  usage_pct ≈ 84.6% → below 90% warning? No warning. No compression.
Step 2  Phase 3 LLM:    payload 55k tokens sent. Ground truth recorded (55k).
Step 3  Phase 4:        assistant emits tool_call: read_file(path, limit=150)
                        → tool executes → result ~9,000 chars ≈ ~2,500 tokens (est. 3.5 chars/tok)
                        → fn_msg appended. Conversation now ≈ 57,500 (88.5%).
                        ⚠ NO CHECK HERE. If usage were checked now it would still be < 95%
                          → no compression would fire anyway at 88.5%.
Step 4  Phase 4 (next tool in same turn): assistant called tool again (parallel/sequential):
                        grep(...) → result ~9,000 chars ≈ ~2,500 tokens
                        → fn_msg appended. Conversation now ≈ 60,000 (92.3%).
                        ⚠ NO CHECK. Still under 95% but close.
Step 5  Phase 4 (third tool): list_dir + read_file again → +5,000 tokens.
                        Conversation now ≈ 65,000 ≈ 100% — AT the limit already.
                        ⚠ NO CHECK. Nothing notices.
Step 6  yield response; continue → next loop iteration.
Step 7  Phase 1 _setup_turn: working sets rebuilt from conversation (65k tokens).
Step 8  Phase 2 check:    usage_pct = 100% > 95% → _force_compression triggers NOW.
                        Compression runs (Compressor agent, inline, blocking, up to
                        COMPRESSION_TIMEOUT=120s). If successful, conversation shrinks and
                        the LLM call proceeds.
  ── BUT if compression fails/skips (cooldown, overfeeding, timeout, Compressor down): ──
Step 9  Phase 3 LLM:      payload 65k > max_input_tokens 65,000
                        → llm/base.py truncates silently (drops oldest non-system messages)
                        → model sees corrupted/partial history
                        → or backend returns 400 context-exceeded → endpoint advanced → retry
                          (execution_engine.py:2391–2396, api_router.py:1165–1193)
                        → repeated failures → [SYSTEM ERROR: LLM context window exceeded ...]
                          message injected (execution_engine.py:2743–2750)
                        → conversation/broken session state.
```

**Exact point of failure:** between **Step 5** (last tool result appended, line 3505) and
**Step 8** (next Phase 2 check, line 1948) the conversation is at/over the limit with **no
intervening check**. The window is as long as the *entire remainder of Phase 4* plus the
`yield`/UI-broadcast and the next `_setup_turn`.

---

## 5. Edge Cases and Risks

### 5.1 Very large tool outputs
- Per-tool char caps (10k default) bound *individual* results, but:
  - `read_file`/`grep`/`log` outputs can still be large (150 lines of dense code ≈ thousands of tokens; `grep -C` context lines; spillover files).
  - **Tool arguments** (the assistant's `function_call` payload) and **reasoning_content** are unbounded by tool caps and count toward context.
  - The char→token estimate (`CHARS_PER_TOKEN_ESTIMATE=5.0`, `settings.py:86–87`) is conservative, but `get_message_stats()` may use a different divisor for the LLM payload path, causing the Phase 2 estimate to **undercount** relative to the model's real tokenizer.
- **Impact:** a single tool result can push usage from 94% → 105% between checks.

### 5.2 Multiple sequential tool calls in one turn
- The loop at `execution_engine.py:3277` iterates over *all* tool calls in `turn_output`
  (parallel tool calls from one assistant message, or multi-tool turns). Each iteration appends
  a FUNCTION message (line 3505) with **no intermediate check**.
- A turn with 5–10 tool calls can grow the context by 15k–50k tokens in a single Phase 4 pass.
  This is the **highest-risk scenario** for the lazy-timing bug.

### 5.3 Different models / context limits
- `_resolve_max_tokens` (api_integration.py:1140–1216) can return a *stale*
  `_allocated_max_input_tokens` (priority 3) or a *shared, potentially polluted* runtime value
  (priority 4). If the limit used for the 95% computation is **larger** than the model actually
  supports, compression never fires in time.
- Dynamic detection (`oai.py::_detect_context_window`, lines 275–372) only updates
  `llm.generate_cfg['max_input_tokens']` for defaults (58000/4096), and only at init/config
  change — not per turn.
- Endpoint failover (`api_router`) may route the same agent to endpoints with **different**
  context windows; the compression check uses the *current* effective max, which can change
  between retries (see `api_router.py:1404–1415` — cursor advances on context-exceeded).

### 5.4 Compressor endpoint slow/unavailable
- Forced compression runs **inline on the agent's thread** (agent_invoker.py:276–289 — slot
  bypass), with `COMPRESSION_TIMEOUT=120s` (settings.py:56–57) and a 5-minute poll cap in
  agent_invoker.py:270.
- If the Compressor fails (`execute_force_compression`, handler.py:645–648), a
  `[SYSTEM] ... automatic compression failed.` notification is injected, and the loop
  **continues to the LLM call with the oversized context** → truncation/error path.
- Cooldown (`check_cooldown`, handler.py:464–497, default cooldown from settings) can skip
  compression even at >95% — the warning is injected instead. If tool results keep growing the
  context, the system can cross the limit *during* the cooldown window with no recourse.

### 5.5 Nested agents / sub-agents
- Sub-agents run via `_create_and_run_agent` (execution_engine.py:4369–4536+) → `self.run(inst)`
  — the same lazy loop applies to each child.
- The child's final result is returned as a string (child_runner.py:16–29) and appended to the
  **parent's** context as a FUNCTION message (tool_dispatcher.py::handle_call_agent). The
  parent's Phase 2 check saw the parent's context *before* the child ran; after the child
  returns, the parent's context grows by the entire child transcript summary — again with no
  immediate check until the parent's next Phase 2.
- Async children (tool_dispatcher.py:520–550, agent_pool.py:2425+) inject results via
  `_async_results` → drained in Phase 5 (`_post_turn_checks`, 3890+; `_drain_and_inject` at
  line 909, safety drain call at 3878–3884) — appended to the parent conversation **after** the
  parent's Phase 2 check, worsening the same gap. The drain path is another
  append-without-check site.
- Each instance has its own `_compression_lock`, `_last_force_compress_time`,
  `_force_compress_count` (agent_instance.py:242, 259–261), so cooldown/counters do not leak
  between parent/child, but the **token accounting does not aggregate across the nesting
  stack** — a parent can overflow due to a child's output without any shared visibility.

### 5.6 Streaming and async injection
- `_drain_and_inject` / async result injection appends messages to `instance.conversation`
  outside the Phase 4 loop (via `_append_and_log_batch`, lines 886+; `_drain_and_inject` at
  909; safety drain call at 3878–3884). These appends also invalidate the token cache but
  trigger no check.
- Streaming LLM responses (`_execute_llm_call_with_retry`, 2428+) feed the inner-loop/max-token
  guards (`MaxTokenExceeded`, 2650) but those are **output**-side guards — they do not consider
  input context growth from tool results.

### 5.7 Concurrent access
- `_count_history_tokens` reads `instance.conversation` and `_cached_*` under
  `_compression_lock` via `instance.append_message`; but the Phase 2 check itself is not
  explicitly lock-wrapped against concurrent appends from async drains — a race could compute
  usage on a snapshot that is stale by the time compression runs (low likelihood, single-thread
  per agent, but worth noting).

---

## 6. Exact Places Where Proactive Checks Should Be Inserted

All insertion points share one pattern: **after messages are appended and token caches are
invalidated, before control returns to the loop / before any further growth**, run a lightweight
usage check and, if `usage_pct > compression_force_threshold` (or a *new, lower proactive
threshold*), call `_force_compression` — but with care not to interrupt mid-batch (see §7).

| # | Insertion point | File / Lines | Why |
|---|-----------------|--------------|-----|
| **A (primary)** | End of `_execute_detected_tools`, after the tool loop (after line ~3632, before returning) — or inside the loop after each `_append_and_log(instance, fn_msg)` at line 3505 | `execution_engine.py` | Catches single large results and multi-tool turns **before** the next LLM call. Checking inside the loop is the *proactive* ideal; checking at loop end is a cheaper approximation. |
| **B (secondary)** | After async result injection — in `_post_turn_checks` (line 3890+) after the drain/safety-drain path (`_drain_and_inject` defined at line 909, safety drain call at 3878–3884) | `execution_engine.py` | Catches parent-context growth from async children before the next Phase 2. |
| **C (guard)** | In `_pre_llm_checks` / `_check_and_trigger_compression` — add a **pre-LLM hard guard** that re-checks with a *full recount* (not delta) when the delta estimate is suspect (e.g., `_last_token_count_conversation_length < 0` or large recent growth) | `execution_engine.py` 1896–1955 | Converts the lazy check into a last-moment safety net with accurate numbers. |
| **D (defense)** | Inside `llm/base.py::chat()` — before `_truncate_input_messages_roughly` (line 330–337), add a *non-destructive* guard that raises `ContextWindowExceeded` (or triggers a compression request via the callback) instead of silently truncating when the payload is over `max_input_tokens` by a large margin | `llm/base.py` | Eliminates silent truncation as the default outcome; makes overflow *loud*. |
| **E (accounting)** | After any batch append: `_append_and_log_batch` (lines 886–900+) and `instance.append_messages` (agent_instance.py:352–366) | `execution_engine.py`, `agent_instance.py` | Optionally centralize a cheap `check_after_append` hook so *all* append paths (tool results, async drains, user messages) get the same proactive check. |

**Note on cooldown:** any new proactive call site must respect `_last_force_compress_time`
cooldown (`check_cooldown`, handler.py:464–497) and `_force_compress_count` overfeeding guard
(handler.py:512+, core.py:238–282) to avoid compression storms — see §7.2.

---

## 7. Recommendations for a Safe Fix Strategy

### 7.1 Layered approach (defense in depth)
1. **Proactive post-tool check (primary fix):** after each `_append_and_log(instance, fn_msg)`
   in `_execute_detected_tools` (line 3505) — or at minimum after the tool loop — compute
   `usage_pct` with the same `_check_and_trigger_compression` logic but at a **lower proactive
   threshold** (e.g., 88–92%) so compression completes while there is still headroom for the
   LLM call's own overhead (system prompt, function schemas, reasoning). Keep the existing 95%
   threshold as the *pre-LLM* final gate.
2. **Pre-LLM hard guard:** strengthen `_check_and_trigger_compression` to do a full recount
   whenever the delta path is unavailable (`_last_token_count_conversation_length < 0`) and to
   **never proceed to the LLM call** if `usage_pct > force_threshold` — currently
   `_force_compression` returns `True` ("continue") even on cooldown/overfeeding/skip paths
   (handler.py:464–543), which is correct for loop flow but must be paired with the guard in
   `llm/base.py` (next item) to avoid silent truncation when compression is skipped.
3. **Non-destructive overflow in llm/base.py:** replace/augment the silent
   `_truncate_input_messages_roughly` behavior with a loud, typed failure
   (`ContextWindowExceeded`) or a *request to compress* when the payload exceeds
   `max_input_tokens` — so the engine can compress and retry instead of silently corrupting
   history. If silent truncation must remain for resilience, it should be **last-resort-only**
   and logged loudly.
4. **Async-drain check:** add the same proactive check after `_drain_and_inject`/safety-drain
   (execution_engine.py 909, call site 3878–3884) to cover nested/async agent results.
5. **Better accounting:** after tool-result appends, refresh `_last_actual_token_count` and
   `_last_token_count_conversation_length` in the same lock scope as the append, so the next
   check uses accurate ground truth instead of an estimate. Consider storing per-message token
   counts at append time (cheap, cached) to make the delta exact.

### 7.2 Safety rails for the new proactive path
- **Cooldown handling:** a proactive trigger should not bypass cooldown, but the cooldown
  window should be short (seconds, not minutes) when usage is above the *hard* threshold; and
  when compression is skipped due to cooldown at >95%, the engine must not proceed to the LLM
  call with an oversized payload — it should fail loudly (raise `ContextWindowExceeded` or
  yield a system-error message) rather than silently truncate.
- **Batch atomicity:** if checking inside the multi-tool loop, decide whether to compress
  mid-batch (which would orphan the remaining tool calls in `turn_output` — orphan handling
  exists at 3511–3517 but is for stop/halt, not compression) or to finish the current batch then
  compress before the next LLM call. **Recommendation: check at the end of `_execute_detected_tools`
  (line 3632, after orphan handling) to avoid mid-batch interruption**, and rely on the pre-LLM
  hard guard for the extreme single-result case.
- **Threshold headroom:** because the LLM call payload includes system prompt + function
  schemas + tool-call overhead beyond the raw conversation, the proactive trigger must fire at a
  level that leaves room for that overhead (i.e., use `max_input_tokens` minus a reserve, or
  lower the proactive threshold to ~88–90%).
- **Tests:** simulate (a) one huge tool result, (b) 10 sequential tool calls, (c) async child
  result injection, (d) Compressor down/timeout, (e) cooldown-active-at-threshold, (f) stale
  `_allocated_max_input_tokens` — asserting the conversation never exceeds `max_input_tokens`
  between appends and the LLM call, and that truncation is never silent.

### 7.3 Do-not-do list
- Do **not** remove `_truncate_input_messages_roughly` without a replacement — it is the only
  current backstop.
- Do **not** add a check that mutates the conversation mid-stream in `_execute_llm_call_with_retry`
  (output-side guard) — input-side checks belong in Phase 2/4 paths.
- Do **not** compress every append unconditionally — respect cooldown/count thresholds to avoid
  compression storms and the overfeeding guard (`core.py:238–282`).

---

## 8. Assumptions and Unknowns

### Confirmed facts (verified in code)
- The only automatic compression decision point is `_check_and_trigger_compression` in Phase 2
  (execution_engine.py:1896–1955, called from `_pre_llm_checks`).
- Tool results are appended with **no overflow check** at execution_engine.py:3505.
- `_truncate_input_messages_roughly` silently drops/truncates old messages at llm/base.py:330–337
  when `max_input_tokens` is exceeded — destructive, pool-divergent.
- `_last_actual_token_count`/`_allocated_max_input_tokens` are only updated during LLM calls
  (callbacks at execution_engine.py:173–200, and `_store_allocated_max_input_tokens` at 2889).
- Append paths invalidate the token cache (`agent_instance.py:346–350, 362–366`).
- Compressor runs inline, slot-bypassed, blocking (agent_invoker.py:276–289); failures inject
  a notification and continue (handler.py:645–648).

### Assumptions (not directly verified)
- The exact failure scenario (truncation vs. API error) depends on the backend: llama.cpp-style
  servers return 400 (`exceed_context_size_error`) which is *detected* (api_router.py:1165–1193),
  but the engine-level retry then re-sends the same oversized payload unless compression
  intervenes — this retry-without-compress behavior is inferred from the code path, not observed
  in a live run.
- The char→token divisor used by `get_message_stats()` matches the divisor used by
  `llm/base.py` for the payload-truncation estimate; any mismatch biases the Phase 2 check.
- `DEFAULT_MAX_INPUT_TOKENS=65000` matches the actual configured backend context in the
  deployed environment.

### Unknowns requiring runtime verification
1. **Observed end-user symptom:** whether the current deployments actually hit silent truncation
   or context-exceeded API errors (check logs for `truncated`, `exceed_context`,
   `ContextWindowExceeded`, `[SYSTEM ERROR: LLM context window exceeded`).
2. **Real per-message token accounting:** `_count_history_tokens` uses `get_message_stats()`
   (LRU-cached estimate) — the accuracy vs. the real tokenizer under heavy tool output is not
   quantified.
3. **Async-drain append timing:** whether `_drain_and_inject` appends happen under
   `_compression_lock` consistently (they go through `_append_and_log_batch`, which locks —
   confirmed — but the *check* sites are absent).
4. **Concurrency:** whether two threads (root agent thread + async child completion) can append
   concurrently to the same instance's conversation outside the Phase 4 window.
5. **Cooldown default value:** the exact `compression_cooldown_seconds` default in the current
   settings (settings.py has the field; value should be confirmed at runtime).
6. **Behavior when Compressor's own context is too small** for the payload: `core.py:238–282`
   returns an error → `execute_force_compression` logs failure and continues — the conversation
   then proceeds to the LLM call over the limit (needs the §7.1 item-2 hard guard).

---

## 9. Summary Table — Check Sites: Now vs. Should Be

| Moment in turn | Current check? | Should check? | Location |
|---|---|---|---|
| Before LLM call (Phase 2) | ✅ Yes (lazy, 95%/90%) | ✅ Yes (hard gate, full recount) | `execution_engine.py:1948` |
| After each tool result append (Phase 4) | ❌ No | ✅ Yes (primary fix) | `execution_engine.py:3505` |
| After all tools in a turn (Phase 4 end) | ❌ No | ✅ Yes (batch-safe) | `execution_engine.py:3632` |
| After async child result injection (Phase 5) | ❌ No | ✅ Yes | `execution_engine.py:909` (drain), `3878–3884` (call site) |
| At LLM payload build (base.py) | ⚠️ Silent truncation only | ✅ Loud fail / compress request | `llm/base.py:330–337` |
| After user message append | ❌ No (user turns re-enter Phase 2 anyway) | Optional (covered by Phase 2) | `api_server.py` / engine |

---

## 10. Suggested Next Actions (for the fix plan)

1. **Instrument first:** add temporary logging of `usage_pct` at the Phase 4 end + Phase 2 start
   to quantify the gap in production-like runs (large greps/read_files, multi-tool turns).
2. **Implement fix A (primary):** post-tool-loop proactive check in `_execute_detected_tools`
   with a lower threshold, respecting cooldown/count guards.
3. **Implement fix D (guard):** non-destructive overflow detection in `llm/base.py::chat()`
   before truncation, raising `ContextWindowExceeded` (already handled at
   execution_engine.py:2745–2750 and api_router.py:1404–1415).
4. **Implement fix B (async drains):** proactive check after `_drain_and_inject`/safety drain.
5. **Implement fix E (accounting):** refresh ground-truth counters after appends under the same
   lock.
6. **Write regression tests** for §7.2 scenarios; assert no append-to-LLM window exceeds
   `max_input_tokens`.
7. **Re-run this investigation** after the fix to confirm the check sites are correct and no
   new gaps were introduced (e.g., compression mid-batch orphaning tool calls).

---

### Confidence Levels
- **Confirmed:** lazy timing (single Phase 2 check); append-without-check at 3505; silent
  truncation in base.py; ground-truth only refreshed during LLM calls; append-path cache
  invalidation.
- **High Confidence:** the failure mechanics (truncation / context-exceeded / retry loops) given
  the code paths; the recommendation set.
- **Moderate Confidence:** which specific symptom dominates in the current deployment (depends on
  backend + logs); the accuracy of the char-based token estimates under heavy tool output.
- **Unknown:** cooldown default value in the running config; concurrency edge cases; exact
  behavior of `_drain_and_inject` under all nested-agent scenarios.