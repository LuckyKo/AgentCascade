# Fix Plan: Lazy Forced Compression Timing

**Date:** 2026-08-04  
**Status:** READY FOR APPROVAL — investigation complete, code locations verified, review findings incorporated  
**Issue:** todo.md line 71 — forced compression triggers after context overflow instead of before  
**Severity:** High — can cause silent truncation, broken conversations, system errors  

---

## Problem Statement

Forced (lazy) compression currently fires only in Phase 2 (pre-LLM check at ~95% usage). Tool results are appended in Phase 4 with no overflow check. A single large tool result or multiple sequential tool calls can push context past the limit, and nothing detects it until the next LLM call — by which time we either silently truncate history or hit API errors.

---

## Goals

1. Compression triggers **before** context exceeds model limits, not after
2. No silent truncation — overflow is detected loudly and handled explicitly
3. Minimal performance impact — avoid redundant token counts and compression storms
4. All append paths covered (tool results, async child injection, batch appends)
5. Backward compatible — existing thresholds and cooldown behavior preserved

---

## Non-Goals

- Rewriting token accounting system (out of scope)
- Changing compression algorithm or compressor agent behavior
- Adding per-message exact token tracking (nice-to-have for future)

---

## Proposed Fix: Layered Defense-in-Depth

We implement 4 layers, ordered by priority. Each layer is independently safe and adds protection.

### Phase 1: Primary Fix — Proactive Post-Tool Check

**What:** After tool results are appended in `_execute_detected_tools`, check context usage and trigger compression proactively if above a lower threshold (88%), before returning control to the main loop.

**Where:** `execution_engine.py` — end of `_execute_detected_tools` method (ends at line 3632), after all tool results have been appended but before the method returns (`return used_any_tool`). Must be placed AFTER orphan handling completes (lines 3511-3630) so compression doesn't see incomplete tool-call/result pairs.

**Why here instead of per-tool:** Checking after each individual tool append would be more proactive but risks:
- Mid-batch compression interrupting remaining tool calls in the same turn
- Excessive compression attempts on turns with many small tools

Checking at end-of-loop is a safe compromise: catches multi-tool accumulation in one shot, avoids mid-turn disruption. The pre-LLM hard guard (Phase 3) covers extreme single-tool cases.

**Key details:**
- Use existing `_check_and_trigger_compression` logic but with a **proactive threshold** of 88% (configurable via new setting `COMPRESSION_PROACTIVE_THRESHOLD`)
- Always call `self._get_max_tokens(instance)` fresh at each check site — do NOT rely on stale instance cache values (`_allocated_max_input_tokens`)
- Respect existing cooldown and max-attempts guards — do not bypass them in proactive path
- If compression is skipped due to cooldown at >88%, log a warning but continue; the pre-LLM guard will catch it
- Must pass the same `messages`/`instance` context as Phase 2 check
- Acquire `_compression_lock` around token count to prevent race with async drains

**Risks:**
- Additional token count call per tool-using turn (mitigated by existing cache invalidation logic)
- Compression mid-turn could orphan remaining tool calls if we check inside the loop (avoided by checking at end)

---

### Phase 2: Async Drain Check

**What:** After async child agent results are injected into parent context via `_drain_and_inject`, run the same proactive check.

**Where:** `execution_engine.py`:
- After `_drain_and_inject` completes (method defined at line 909, body to ~997)
- Safety drain call site in `_drain_post_generation_messages` (~line 3879)
- Second safety drain in `_post_turn_checks` (~line 4174) — same append-without-check pattern

**Why:** Child agent outputs can be large; they're appended outside Phase 4 with no check. Parent's next Phase 2 check may be too late.

**Key details:**
- Same proactive threshold as Phase 1 (88%)
- Same cooldown/max-attempts guards
- Apply to both synchronous drain and async completion paths
- Acquire `_compression_lock` around token count to prevent race with concurrent appends
- Use fresh `self._get_max_tokens(instance)` at each check site

**Risks:**
- Nested agents could trigger compression on parent while child is still running (unlikely but worth noting)

---

### Phase 3: Pre-LLM Hard Guard Strengthening

**What:** Strengthen the existing Phase 2 check (`_check_and_trigger_compression`) to be a true hard gate: never allow an LLM call with oversized context, even if compression is skipped due to cooldown/overfeeding.

**Where:** `execution_engine.py` — `_check_and_trigger_compression` method (lines 1896–1955)

**Changes:**
1. When delta estimate is unavailable (`_last_token_count_conversation_length < 0`) and we're near/at threshold, force a full recount instead of relying on potentially inaccurate estimates
2. Always use fresh `self._get_max_tokens(instance)` — do NOT rely on stale `_allocated_max_input_tokens` for the comparison
3. If `usage_pct > force_threshold (95%)` AND compression cannot run due to cooldown:
   - **Override cooldown and force compression** — at 95%+, we must compress even if cooldown is active, because silent truncation is worse than a brief compression delay
4. If `usage_pct > force_threshold (95%)` AND max-attempts exceeded (overfeeding guard):
   - Raise `ContextWindowExceeded` exception instead of returning True ("continue") — this prevents silent truncation as the fallback
5. Ensure `_force_compression` return semantics are clear: True = safe to continue, False/Exception = must not proceed
6. Fix stale comment at line 1951: says ">85%" but actual default is 90%
7. Acquire `_compression_lock` around token count to prevent race with async drains

**Why:** Currently, if compression is skipped (cooldown, overfeeding), the engine continues to the LLM call with oversized context → silent truncation in base.py. This makes overflow loud and forces explicit handling. At 95%+, we override cooldown because the risk of truncation outweighs the cost of an extra compression.

**Risks:**
- Could cause more "context window exceeded" errors if cooldowns are too aggressive (tune thresholds)
- Must ensure `ContextWindowExceeded` is properly caught and handled in the retry loop

---

### Phase 4: Non-Destructive Overflow Detection in LLM Layer

**What:** In `llm/base.py::chat()`, before silently truncating via `_truncate_input_messages_roughly`, add a loud, non-destructive guard that raises `ContextWindowExceeded` when the payload exceeds `max_input_tokens` by more than a small margin (e.g., 2-5%).

**Where:** `llm/base.py` — `chat()` method before calling `_truncate_input_messages_roughly` (lines 330–336; truncation function defined at line 851)

**Behavior:**
- If payload tokens <= max_input_tokens: proceed normally
- If payload tokens > max_input_tokens but within small tolerance (e.g., +2%): allow truncation as a safety net (token estimates are imperfect)
- If payload tokens >> max_input_tokens (beyond tolerance): raise `ContextWindowExceeded` instead of truncating

**Why:** Silent truncation is the root cause of conversation corruption. It removes messages from the LLM payload but not from `instance.conversation`, causing divergence. Making this loud forces the engine to handle it properly (compress and retry, or fail explicitly).

**Risks:**
- Token estimates in base.py may differ from Phase 2 estimates → false positives (mitigated by tolerance margin)
- Must preserve truncation as a last-resort backstop for edge cases

---

## Configuration Changes

Add two new settings:

- `COMPRESSION_PROACTIVE_THRESHOLD` (default: **88.0**%) — threshold for proactive checks in Phase 1 and Phase 2. Lower than `COMPRESSION_FORCE_THRESHOLD` (95%) to allow compression to complete before we hit the hard limit, with headroom for system prompt + function schemas overhead.
- `COMPRESSION_CONTEXT_RESERVE_TOKENS` (default: **3000**) — tokens reserved for LLM call overhead (system prompt, function schemas, reasoning). Used when computing effective threshold: `effective_limit = max_input_tokens - COMPRESSION_CONTEXT_RESERVE_TOKENS`.

Rationale: Compression is not instant. We need headroom so that by the time it completes, we're safely below the force threshold and the LLM call can proceed with room for system prompt + function schemas. The reserve ensures we don't compress too late when overhead would push us over.

---

## Implementation Order

1. **Phase 1 first** (Post-tool check) — primary fix, covers the main gap; lowest risk since it adds a new check without changing existing behavior
2. **Phase 2** (Async drain check) — secondary fix, covers nested agent gap; same pattern as Phase 1
3. **Phase 4** (LLM layer guard) — makes truncation loud instead of silent; provides backstop before we change exception semantics in Phase 3
4. **Phase 3 last** (Pre-LLM hard guard strengthening) — most invasive, changes exception/cooldown behavior at the core check site; do this after proactive checks are in place so we know they reduce overflow events

Rationale: By implementing proactive checks first, we significantly reduce the number of times Phase 3's stricter behavior will be triggered. This lets us validate that overflows are being caught early before making the final gate more aggressive.

## Testing Requirements

For each phase, verify:

1. **Single large tool result:** Tool output pushes context from 85% → 98%; compression triggers proactively before LLM call
2. **Multiple sequential tools:** 5-10 tool calls in one turn accumulate past threshold; compression triggers after loop
3. **Async child injection:** Large child result appended to parent; proactive check fires at all drain sites
4. **Cooldown override at 95%+:** At >95% with cooldown active, Phase 3 overrides cooldown and forces compression (no silent truncation)
5. **Max-attempts exceeded:** When overfeeding guard triggers at >95%, raise `ContextWindowExceeded` instead of silently truncating
6. **Compressor unavailable:** Compression fails/times out; system fails loudly via `ContextWindowExceeded`, no silent truncation
7. **No regression:** Normal turns without tool calls still use existing Phase 2 check only (no extra overhead)
8. **Threshold headroom:** After proactive compression completes, context is safely below force threshold with room for LLM overhead (reserve tokens respected)
9. **Stale max_tokens:** With stale `_allocated_max_input_tokens`, fresh `_get_max_tokens` call still computes correct threshold
10. **Concurrency safety:** Async drain concurrent with tool append — no race condition in token count under lock

**Performance tests:**
- Measure overhead of extra token counts per tool-using turn (should be minimal due to caching)
- Verify no latency spikes on turns with many small tools

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Extra token counts add latency | Use existing cache logic; only count when near thresholds; acquire lock briefly around count only |
| Compression storms from too-aggressive checks | Respect cooldown/max-attempts guards; proactive threshold (88%) is lower but not zero; cooldown only overridden at 95%+ |
| Mid-batch tool orphaning | Check at end of tool loop after orphan handling completes, not inside it |
| Token estimate inaccuracies cause false positives/negatives | Add tolerance margin in Phase 4; use full recount when delta unavailable; reserve tokens provide buffer |
| Breaking existing behavior | Phased rollout (1→2→4→3); each layer independently safe; preserve all existing thresholds |
| Endpoint cycling on ContextWindowExceeded | Max-attempts exceeded raises exception that's handled by engine-level error injection, not endpoint advance |

---

## Open Questions

1. **RESOLVED:** Threshold approach — using percentage (88%) plus reserve tokens (3000) for headroom calculation
2. Do we need different thresholds for different agent classes (e.g., compressor vs. coder)? Likely not initially; can add per-class config later if needed
3. Logging verbosity — log at debug level with rate limiter, always log warnings for skipped compressions and overflows

---

## Next Steps

1. ✅ Verify code locations from investigation (completed)
2. ✅ Initial review of plan (completed — findings incorporated)
3. Final review pass on updated plan (quick check on critical changes only)
4. On approval: implement Phase 1 first, then Phase 2, Phase 4, Phase 3; review after each phase
5. Write tests as we go; run regression suite after all phases complete