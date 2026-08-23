# Plan: Soft "continue" attempts before full retry on reasoning-only end turns

> **Review status: APPROVED (round 5, PASS).** All findings F1–F5 and N1–N3 resolved. Pure-resend default confirmed correct against source (`core.py:_process_response` re-calls LLM on unchanged context when `_check_and_handle_truncation` returns True). Full review history: `reasoning_only_continue_retry_plan_REVIEW.md`. Ready for implementation.

## Todo item (todo.md line 130)
> adjust the malformed retry logic on reasoning only end turns. send a few pure continue attempts before rolling back that last message and doing a full retry.

## Problem / Current behavior

When an agent's turn ends with a **reasoning-only** assistant message (has `reasoning_content`/thinking but no text content and no tool call), the current code treats it as a malformed/incomplete output and immediately does a **full retry**: it rolls back (pops) that last message from the conversation, rebuilds the working set, clears the response buffer, and silently re-calls the LLM.

This is expensive and loses the reasoning the model already produced. The user wants: for reasoning-only end turns specifically, send a few **pure "continue"** attempts first (keep the reasoning message in place, nudge the model to keep going and produce actual output), and only if those keep failing do we roll back that last message and do a full retry.

### Where this happens today
- `agent_cascade/engine/helpers.py::_is_incomplete_state()` — detects 3 cases on the **last assistant** message:
  - `"reasoning-only"` (has reasoning, no content, no tool call)
  - `"broken-json"` (tool call with mismatched brackets/braces)
  - `"empty-output"` (nothing at all)
- `agent_cascade/engine/core.py::_check_and_handle_truncation()` (called from `_process_response`) — for **any** of these cases it does the same thing immediately:
  ```python
  is_incomplete = _is_incomplete_state(turn_output)
  if (is_truncated or is_incomplete) and not self._is_terminal_stop(inst_name) and self.pool.settings.auto_continue:
      instance._auto_continue_count += 1
      if instance._auto_continue_count >= MAX_AUTO_CONTINUE_ATTEMPTS:   # 5
          ... return False   # give up entirely
      pop_count = len(turn_output) (+1 if _continue_fallback_append)
      self.pool._rollback_instance(inst_name, pop_count=pop_count)   # <-- pops the last message
      self._rebuild_working_set(messages, llm_messages, inst_name)
      response.clear()
      instance._auto_continue_triggered = True
      return True   # -> next LLM call (silent re-call, no new prompt text)
  ```
- **No explicit "continue" prompt text is injected** on this path — it's a silent re-call after rollback. (`_make_user_message` exists at core.py:352 but is not used here.)
- Gating constants: `MAX_AUTO_CONTINUE_ATTEMPTS = 5` (settings.py:29). Counter resets to 0 when a turn completes normally or when the cap is hit.

### Separate, related detection (do NOT conflate)
- `_detect_pure_thinking_turn()` (core.py:1845) — checks the **last 3 non-FUNCTION messages** for "has thinking but no real content" and, if so, **breaks the loop** (agent considered stalled/complete). This runs in Phase 5 (`_post_turn_checks`) AFTER `_process_response` returns False. It is a different mechanism (loop-break vs. retry) and should be left as-is unless we decide to coordinate with it.

## Goal
For `"reasoning-only"` end turns:
1. First **N** times: do a *soft continue* — keep the reasoning message in place and re-call the LLM on the SAME history (no rollback of the reasoning message, no new prompt text). This is a **pure resend**: the model already has its own reasoning as context, so we just ask it to go again. No rollback, no nudge on the first pass.
   - **Nudge (deferred, kept as commented logic):** if pure resends prove insufficient, an escalating USER "stop thinking / produce output or tool call" nudge can be injected on the 2nd+ soft attempt. The full mechanism is specified below but shipped **commented out** behind a `SOFT_CONTINUE_NUDGE_ENABLED = False` flag so it can be enabled later without re-architecting.
2. After N soft attempts still yield reasoning-only: fall back to the existing **full retry** behavior (rollback the last message + rebuild + silent re-call), bounded by the existing `MAX_AUTO_CONTINUE_ATTEMPTS` cap.

Other malformed cases (`"broken-json"`, `"empty-output"`) and truncation keep their current immediate full-retry behavior (out of scope for this change — minimal, safe).

## Design decisions (to confirm with user)
- **N (soft continue attempts)**: default 2. New setting `REASONING_ONLY_CONTINUE_ATTEMPTS` in settings.py (env-overridable), default 2.
- **What the "continue" nudge is** (F4 — DEFERRED, shipped commented out): a short USER message, escalating on the second attempt so an identical repeat doesn't just echo back reasoning. The helper `_reasoning_only_continue_text(attempt: int) -> str` and the `_inject_soft_continue_nudge()` mechanism are **fully specified below but wrapped in `if SOFT_CONTINUE_NUDGE_ENABLED:` (default False)** so they are inert until we decide pure resends aren't enough:
  - `attempt == 1`: `"Your last turn contained only reasoning/thinking and no output or tool call. Please continue — produce your response text or make the next tool call now."`
  - `attempt >= 2`: `"You have produced reasoning-only output again with no visible result. STOP thinking and MUST produce either a final answer or a concrete tool call on this turn — do not emit another reasoning-only message."`
  - When enabled, injected via existing `_make_user_message` + the new `_inject_soft_continue_nudge` helper (same mechanism as urgent-message injection) — makes the retry visible in logs and gives an explicit instruction rather than a silent re-call. Text is deterministic (function of attempt number only), keeping tests stable.
  - **Default behavior (nudge OFF):** soft continue = pure resend, identical to today's silent full-retry re-call EXCEPT it does NOT roll back the reasoning message. This is the minimal change and what we ship first.
- **State tracking** (N3 — two counters, because one counter can't serve both jobs): add per-instance fields on `AgentInstance` (agent_instance.py), next to the other auto-continue state fields (~line 314):
  - `_reasoning_only_soft_attempts: int = 0` — **episode budget**: total soft continues attempted since the last normal completion / cap-hit. Used ONLY for the soft-path guard (`< REASONING_ONLY_CONTINUE_ATTEMPTS`). Reset to 0 at (a) normal completion and (b) cap-hit — i.e., in the SAME places `_auto_continue_count` is reset to 0 (core.py:1676 / core.py:1696). **Never** reset mid-episode, so the soft path can't be re-entered after we've already fallen through to full retry.
  - `_reasoning_only_pending_nudges: int = 0` — **rollback accounting**: number of soft-continue nudges currently sitting in context that a rollback must pop. **Only incremented when `SOFT_CONTINUE_NUDGE_ENABLED` is True** (a nudge was actually injected); with the default pure-resend behavior it stays 0 because nothing new is added to context. Zeroed immediately after any rollback that pops them. This drives the `pop_count += _reasoning_only_pending_nudges` adjustment (guarded to reasoning-only). With nudge OFF, full retries pop exactly `len(turn_output)` — same as today.
  - Reuse existing `_auto_continue_count` for the overall cap / full-retry budget (soft and full both increment it; it is never reset mid-episode, which is what actually bounds the total at `MAX_AUTO_CONTINUE_ATTEMPTS`).
  - Why two counters: a single counter forced a trade-off — keep it non-zero and subsequent full retries over-pop valid history (N1); zero it and the soft path re-opens, breaking the N budget (N3). Splitting "attempts so far" from "nudges still in context" removes the conflict.
- **Interaction with the global cap** (F2 — budget bounded at 5; stated explicitly): soft continues ALSO increment `_auto_continue_count`, so they consume the same `MAX_AUTO_CONTINUE_ATTEMPTS` (=5) budget as full retries. The new N only controls *how many of those are soft vs. full*.
  - **CRITICAL — what keeps the total bounded is `_auto_continue_count`.** It is incremented once per attempt (soft OR full) and is only ever reset to 0 at (a) normal completion, or (b) when the cap is hit — it is NEVER reset by a full retry. That is what bounds the episode at `MAX_AUTO_CONTINUE_ATTEMPTS`. The two reasoning-only counters are separate: `_reasoning_only_soft_attempts` (budget guard, never resets mid-episode so the soft path can't re-open after falling through to full retry) and `_reasoning_only_pending_nudges` (zeroed after each rollback that pops them). Because `_auto_continue_count` never resets mid-episode AND `_reasoning_only_soft_attempts` only ever advances toward N, the sequence of attempts within one "stuck" episode is strictly:
    - attempts 1..N → soft continue = **pure resend** (nudge OFF): `_reasoning_only_soft_attempts` advances 1, 2, … N; `_reasoning_only_pending_nudges` stays 0 (nothing added to context). If nudge is ON, `_reasoning_only_pending_nudges` also advances 1, 2, … N.
    - attempt (N+1): first full-retry rollback — pops `len(turn_output) + _reasoning_only_pending_nudges` (i.e. just the assistant msg when nudge OFF; assistant + N nudges when nudge ON), then zeros ONLY `_reasoning_only_pending_nudges` (NOT `_reasoning_only_soft_attempts`)
    - attempts (N+2)..(cap−1) → further full retries (soft path is now permanently closed because `_reasoning_only_soft_attempts == N`; pending nudges already 0, so pop_count is just len(turn_output))
    - the attempt that would make `_auto_continue_count >= cap` → give up (return False) **before performing that retry** (the check is `>= MAX_AUTO_CONTINUE_ATTEMPTS`, evaluated right after incrementing). So the cap-th call does NOT produce a retry.
  - **Result: exactly `min(N, cap−1)` soft continues followed by `(cap − 1 − min(N, cap−1))` full retries, then the next call hits the cap and gives up.** With defaults N=2, cap=5 → **2 soft + 2 full = 4 actual retries**, then the 5th reasoning-only turn returns False (give up). There is NO re-cycling of soft continues (`_reasoning_only_soft_attempts` never resets mid-episode) — this is what the single-counter version got wrong (N3). NOTE: the cap check `>= MAX_AUTO_CONTINUE_ATTEMPTS` means at most `cap − 1` retries actually fire; the "5" is a bound, not a count of successful retries.
  - Edge: if `N >= cap`, soft continues alone exhaust the budget (no full retry happens). Acceptable and self-consistent; document it.
  - **Known MINOR edge (review round 4, acceptable):** when `N >= cap` the cap is hit *on a soft continue* (not on a full retry), so the last injected nudge is NOT popped before we return False — one pending nudge can remain in context at give-up. This is harmless: the episode is over (we're giving up / breaking out), no further LLM call consumes that turn, and the counters are zeroed. We do NOT add extra rollback-on-cap-hit logic for this; it's documented here so implementers don't "fix" it into a behavior change. In the default config (N=2 < cap=5) this edge never occurs because the cap is always hit after all nudges have already been popped by full retries.
- **Nudge lifecycle / cleanup** (F1 — applies ONLY when nudge is enabled): with the default pure-resend behavior nothing new is added to context, so there is nothing to clean up and full retries pop exactly `len(turn_output)` (identical to today). When `SOFT_CONTINUE_NUDGE_ENABLED` is True, a soft continue appends a USER nudge to `conversation` + `llm_messages` + `response`. On a reasoning-only turn, `turn_output` is fresh each iteration (core.py:638) and holds ONLY the assistant message — the nudges live OUTSIDE it. So a full-retry rollback that pops only `len(turn_output)` (=1) would NOT remove prior nudges; they'd linger in context and pollute the retry. **Fix:** on a *reasoning-only* full retry, pop `len(turn_output) + _reasoning_only_pending_nudges` messages so the rollback removes the current assistant message AND all prior soft-continue nudges, restoring the conversation to the state just before the first soft continue — then **immediately set `_reasoning_only_pending_nudges = 0`** (the nudges no longer exist; leaving it non-zero makes the NEXT full retry over-pop valid history — N1). The adjustment is guarded to `is_incomplete == "reasoning-only"` so a broken-json/empty-output/truncation retry never pops unrelated nudges (N2). `_reasoning_only_pending_nudges` is the single source of truth for how many nudges are currently in context. The full-retry path also rebuilds the working set and clears `response` exactly like today, so the popped nudges are gone from all lists.

## Implementation plan (files & changes)

### 1. `agent_cascade/settings.py`
- Add `REASONING_ONLY_CONTINUE_ATTEMPTS: int = int(os.getenv('QWEN_AGENT_REASONING_ONLY_CONTINUE_ATTEMPTS', 2))`.
- Add `SOFT_CONTINUE_NUDGE_ENABLED: bool = os.getenv('QWEN_AGENT_SOFT_CONTINUE_NUDGE', '0') == '1'` — **default False**. When False, soft continues are pure resends (no nudge message). When True, the escalating USER nudge is injected on each soft continue. This lets us enable the nudge later without code changes.

### 2. `agent_cascade/agent_instance.py`
- Add TWO dataclass fields next to the other auto-continue state fields (~line 314):
  - `_reasoning_only_soft_attempts: int = field(default=0)` — episode budget (soft-path guard).
  - `_reasoning_only_pending_nudges: int = field(default=0)` — rollback accounting (nudges currently in context).
- Reset both to 0 wherever `_auto_continue_count` is reset to 0 on normal completion / cap-hit (core.py:1676 and core.py:1696). Do NOT reset them on a full retry except for the specific zeroing of `_reasoning_only_pending_nudges` after popping nudges (see section 3).

### 3. `agent_cascade/engine/core.py` — `_check_and_handle_truncation()`
Split behavior by case. Three reset sites for the reasoning-only counters: (a) cap-hit, (b) normal completion, and (c) zeroing ONLY `_reasoning_only_pending_nudges` after a rollback pops them. `_reasoning_only_soft_attempts` is never reset mid-episode (N3):
```python
is_incomplete = _is_incomplete_state(turn_output)
if (is_truncated or is_incomplete) and not self._is_terminal_stop(inst_name) and self.pool.settings.auto_continue:
    instance._auto_continue_count += 1
    if instance._auto_continue_count >= MAX_AUTO_CONTINUE_ATTEMPTS:
        # cap-hit reset (site a): clear ALL counters, give up
        instance._auto_continue_count = 0
        instance._continue_fallback_append = False
        instance._reasoning_only_soft_attempts = 0
        instance._reasoning_only_pending_nudges = 0
        return False

    # NEW: reasoning-only gets soft-continue first (up to N times). Guarded by the BUDGET counter,
    # which never resets mid-episode, so once we fall through to full retry this path is closed (N3).
    if is_incomplete == "reasoning-only" and instance._reasoning_only_soft_attempts < REASONING_ONLY_CONTINUE_ATTEMPTS:
        instance._reasoning_only_soft_attempts += 1     # budget: how many soft tries this episode
        reason = f"incomplete state (reasoning-only, soft continue {instance._reasoning_only_soft_attempts}/{REASONING_ONLY_CONTINUE_ATTEMPTS})"
        # telemetry + log as before (record_auto_continue with the reason above)
        # --- Default: PURE RESEND. No rollback, no new message — just re-call the LLM on the same
        #     history (the reasoning message stays in context). Identical to today's silent retry
        #     except we do NOT pop anything. ---
        if self.pool.settings.SOFT_CONTINUE_NUDGE_ENABLED:
            # (Deferred feature — OFF by default.) Inject an escalating USER nudge so the model gets
            # an explicit instruction instead of a bare resend. When enabled, a nudge is now in
            # context, so track it for rollback accounting on the later full retry.
            instance._reasoning_only_pending_nudges += 1
            self._inject_soft_continue_nudge(instance, inst_name, messages, llm_messages, response)   # NEW helper (see section 4)
        instance._auto_continue_triggered = True
        return True

    # existing full-retry path (truncation / broken-json / empty-output / reasoning-only after N soft tries)
    reason = "truncation" if is_truncated else f"incomplete state ({is_incomplete})"
    ... telemetry, log ...
    pop_count = len(turn_output)
    if getattr(instance, '_continue_fallback_append', False):
        pop_count += 1
    # F1 fix: for a reasoning-only full retry, also remove the soft-continue nudges injected this
    # episode (they live in conversation/llm_messages/response, NOT in turn_output). Guarded to
    # reasoning-only so a broken-json/empty-output/truncation retry never pops unrelated nudges (N2).
    if is_incomplete == "reasoning-only":
        pop_count += instance._reasoning_only_pending_nudges
    if pop_count > 0:
        self.pool._rollback_instance(inst_name, pop_count=pop_count)
        self._rebuild_working_set(messages, llm_messages, inst_name)
        response.clear()
    # N1 fix: the nudges were just popped above, so they no longer exist — zero ONLY the pending-nudge
    # counter. Do NOT touch _reasoning_only_soft_attempts (it must stay at N so the soft path stays closed).
    instance._reasoning_only_pending_nudges = 0
    instance._auto_continue_triggered = True
    return True

# normal completion -> reset both reasoning-only counters (site b)
instance._auto_continue_count = 0
instance._continue_fallback_append = False
instance._reasoning_only_soft_attempts = 0
instance._reasoning_only_pending_nudges = 0
return False
```

### 4. `agent_cascade/engine/core.py` — new helper `_inject_soft_continue_nudge()` (F3 — concrete spec)
**Only called when `SOFT_CONTINUE_NUDGE_ENABLED` is True** (see section 3). With the default pure-resend behavior this helper is never invoked, so it is inert shipped code. A small private method on the engine, mirroring the existing injection path so it is thread-safe and keeps all lists in sync:
```python
def _inject_soft_continue_nudge(self, instance, inst_name, messages, llm_messages, response):
    n = instance._reasoning_only_soft_attempts           # 1-based attempt number (for escalating text)
    text = self._reasoning_only_continue_text(n)         # see F4 below
    msg = self._make_user_message(text)
    with instance._compression_lock:
        messages.append(msg)          # full working set
        llm_messages.append(msg)      # LLM-formatted set
        response.append(msg)          # local accumulator (UI visibility)
        self._append_and_log(instance, msg, lock_held=True)   # conversation + JSONL log atomically
    try:
        self.pool._mark_activity(inst_name)
        if getattr(self.pool.settings, 'tail_sync_check_enabled', True):
            from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
            with instance._compression_lock:
                conv = instance.conversation
            log_inst = self.pool.get_logger(inst_name, instance.agent_class)
            _check_tail(inst_name, conv, log_inst.log_path, context="reasoning_soft_continue")
    except Exception as e:
        logger.debug(f"Soft-continue logging failed for {inst_name} (non-critical): {e}")
```
- This is the same append-to-all-lists-under-lock pattern `_drain_and_inject` uses (core.py:309–313), so ordering and cache-invalidation semantics match urgent-message injection. We do NOT reuse `_drain_and_inject` directly because it expects a drain_fn/items queue; a dedicated helper is clearer and avoids the compression-notification side effects of that path.
- Do NOT roll back / clear response (the reasoning message stays). The next LLM call will see: ...reasoning-only assistant msg, then the USER nudge.
- Careful: appending a USER message right after an ASSISTANT message is valid ordering. Verify no tool_call_id dangling (there is none — reasoning-only has no tool call).

### 5. Telemetry / logging
- Reuse `tel.record_auto_continue(inst_name, reason=reason)` with the new reason string so it's distinguishable in session summaries.
- Log at INFO: "Detected incomplete state (reasoning-only) for {inst}. Soft continue attempt {n}/{N}."

### 6. Tests (`tests/`)
New test file e.g. `tests/test_reasoning_only_continue_retry.py`:
- Unit: `_is_incomplete_state` still returns `"reasoning-only"` for the right shapes (regression).
- Behavior — **default (nudge OFF, pure resend)** (mock pool + instance):
  - reasoning-only turn #1 -> soft continue = PURE RESEND: NO message appended to conversation/llm_messages/response, NO `_rollback_instance` call. `_reasoning_only_soft_attempts`=1, `_reasoning_only_pending_nudges`=0. Returns True (re-call).
  - reasoning-only turn #2 -> pure resend again, `_reasoning_only_soft_attempts`=2, `_reasoning_only_pending_nudges`=0.
  - **soft→full transition (nudge OFF):** reasoning-only turn #3 (N=2) -> full retry: assert `_rollback_instance` is called with `pop_count == len(turn_output)` ONLY (nothing extra, since pending_nudges=0). After this call: `_reasoning_only_soft_attempts` STILL 2, `_reasoning_only_pending_nudges`=0.
  - **N3 — soft path stays closed after full retry:** reasoning-only turn #4 -> MUST take the full-retry path (NOT soft continue), because `_reasoning_only_soft_attempts`(=2) is not < N(=2). Assert `pop_count == len(turn_output)` ONLY. Turn #5 likewise. Then the attempt that hits the cap -> returns False (give up), all counters reset to 0.
  - **N1 — no over-pop:** turn #4/#5 pop only `len(turn_output)` (pending_nudges=0 throughout when nudge OFF).
  - After a normal (content/tool) turn -> both reasoning-only counters reset to 0.
  - Cap: `MAX_AUTO_CONTINUE_ATTEMPTS` still stops everything. Because the check is `>= MAX_AUTO_CONTINUE_ATTEMPTS`, at most `cap − 1` retries actually fire before give-up. Verify: results are `[True]*(cap−1) + [False]`, exactly N of the Trues are soft continues and `(cap−1−N)` are full retries (default N=2, cap=5 → 2 soft + 2 full = 4 True then False).
  - **auto_continue=False:** reasoning-only turn is left completely untouched — NO resend, NO rollback, both counters stay 0 (Phase 5 pure-thinking detector governs instead). Regression guard for the gate.
  - Truncation / broken-json / empty-output -> unchanged immediate full retry with `pop_count == len(turn_output)` (+1 if fallback append), and BOTH reasoning-only counters unaffected (regression guard, N2).
- Behavior — **nudge ON** (`SOFT_CONTINUE_NUDGE_ENABLED=True`):
  - reasoning-only turn #1 -> USER nudge appended to conversation+llm_messages+response (attempt-1 text), NO rollback, `_reasoning_only_soft_attempts`=1 AND `_reasoning_only_pending_nudges`=1.
  - reasoning-only turn #2 -> escalated attempt-2 nudge appended, both counters=2.
  - **F1 — nudge cleanup on soft→full transition:** reasoning-only turn #3 (N=2) -> full retry: assert `_rollback_instance` is called with `pop_count == len(turn_output) + 2` (assistant msg + 2 prior nudges), and that after rollback+rebuild the conversation/llm_messages/response contain NO soft-continue nudge messages (they were popped). After this call: `_reasoning_only_pending_nudges`=0 but `_reasoning_only_soft_attempts` STILL 2.
- Ensure no new cmd-shell windows, deterministic (no flaky), follow existing test conventions.

## Risks / edge cases to verify in review
- **Pure-resend semantics (default, nudge OFF)**: a soft continue re-calls the LLM on the *same* history with the reasoning message still present and NO new prompt text. Verify this actually produces a different (non-reasoning-only) result in practice — if the model deterministically repeats reasoning, pure resends will burn all N attempts before falling to full retry. That is acceptable (bounded) but is the main reason the nudge toggle exists as an escape hatch.
- **Ordering** (nudge ON only): USER-after-ASSISTANT with no tool call — confirm backends accept it (they already do for urgent-message injection). Not applicable when nudge is OFF (no message appended).
- **Double counting / bounded budget** (RESOLVED by design): soft continues and full retries share the same `MAX_AUTO_CONTINUE_ATTEMPTS` cap via `_auto_continue_count`, which is never reset mid-episode — so total attempts are exactly `min(N,cap)` soft + `(cap−min(N,cap))` full. Verify in test that no episode exceeds the cap.
- **Nudge cleanup on full retry** (RESOLVED by design — was F1): a *reasoning-only* full retry pops `len(turn_output) + _reasoning_only_pending_nudges`, removing prior nudges with the assistant message, then zeros `_reasoning_only_pending_nudges` (NOT `_reasoning_only_soft_attempts`). Verify no nudge lingers after rollback AND that a subsequent full retry does not over-pop (N1).
- **Interaction with `_detect_pure_thinking_turn`** (Phase 5): a soft continue makes `_process_response` return True, so Phase 5 is skipped and the next LLM call happens BEFORE the pure-thinking detector runs — the soft path wins by construction. The only way Phase 5's break fires is if we've exhausted the cap (returned False) or produced real output; confirm there's no path where a soft continue returns False while nudges are still pending.
- **`turns_available` reset side-effect** (documented, not a bug): setting `_auto_continue_triggered=True` on a soft continue causes the main loop (core.py:731–736) to reset `turns_available = max_turns`, so soft continues do NOT consume from `max_turns`. This matches today's full-retry behavior. Do NOT "fix" this — it is intentional and consistent.
- **`_continue_fallback_append` / pop_count accounting**: on the full-retry path, pop_count = `len(turn_output)` (+1 if fallback append) **+ `_reasoning_only_pending_nudges` ONLY when `is_incomplete == "reasoning-only"`** (N2 guard). The reasoning msg was never popped during soft continues, so this is correct; `_reasoning_only_pending_nudges` is zeroed immediately after the rollback (N1), so verify no double-pop across consecutive full retries.
- **Streaming/UI** (nudge ON only): soft continue appends a visible USER nudge — confirm it doesn't create duplicate UI bubbles or break the streaming merge logic (`_continue_saved_msg`). Not applicable when nudge is OFF.
- **Persistence/log sync** (nudge ON only): appending via `_append_and_log` must keep JSONL log in sync (tail-sync check runs inside `_inject_soft_continue_nudge`; ensure append path logs correctly). With nudge OFF, the soft continue appends nothing, so no log-sync concern beyond the existing telemetry line.

## Out of scope (keep minimal)
- Changing behavior for `"broken-json"`, `"empty-output"`, or truncation.
- Modifying `_detect_pure_thinking_turn`.
- UI changes.

## Definition of done
- Implementation approved by Reviewer.
- New tests pass; existing engine/telemetry tests still pass (regression).
- No flaky / no shell-window side effects.
- todo.md line 130 marked [x] with a short note.
