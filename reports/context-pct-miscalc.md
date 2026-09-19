# Context-Window Percentage Miscalculation — Root-Cause Investigation

**Date:** 2026-08-29
**Investigator:** pct-bug-recon (read-only investigation; no code changes made)
**Report path:** `N:\work\WD\AgentCascade\research_reports\context-pct-miscalc.md`

## Executive Summary

The displayed percentage is computed against the **effective limit** (`max_input_tokens − reserve_tokens`, default reserve = 3000), while the token pair printed next to it uses the **raw** `max_input_tokens`. For the reported case (106350 used, 120000 window, reserve 3000): the % is `106350 / 117000 = 90.9%`, but the printed "106350/120000" reads as `88.6%`. The mismatch is exactly the 3000-token reserve. The same inflated percentage also feeds all threshold comparisons, so compression triggers ~2.4 percentage-points earlier (in raw-window terms) than the threshold number suggests — this early trigger is **intentional by design** (reserve = headroom for system prompt / tool schemas / reasoning); only the display is wrong.

**Confidence: Confirmed** (arithmetic reproduces the exact reported values; all code paths read directly).

---

## 1. Root Cause

The denominator of the percentage and the denominator of the printed "(A/B tokens)" string are **two different numbers**:

| Value | Source | Value in reported case |
|---|---|---|
| `current_tokens` (A) | `_count_history_tokens(...)` | 106350 |
| `effective_limit` (pct denominator) | `max_tokens_for_check − reserve_tokens` (reserve default 3000) | 117000 |
| `max_tokens_for_check` (printed B) | raw max input tokens of endpoint | 120000 |

- Percentage = `106350 / 117000 × 100 = 90.897… → "90.9%"` ✓ matches the warning
- Printed ratio = `106350 / 120000 × 100 = 88.625 → 88.6%` ✓ matches the user's observation

So the string `Context window at 90.9% capacity (106350/120000 tokens)` is internally contradictory: the % is of 117000, the fraction is of 120000. Not rounding, not integer division, not an off-by-one — a **denominator mismatch** (raw window vs. window-minus-reserve).

---

## 2. The Two Code Paths

All in `agent_cascade/engine/compression_exec.py` (mixin `CompressionExecMixin`, composed into `ExecutionEngine`).

### Path A — where the percentage is computed (and where thresholds are evaluated)

```python
# compression_exec.py:93-94
force_threshold = self.pool.settings.compression_force_threshold
reserve_tokens = self.pool.settings.compression_context_reserve_tokens
...
# compression_exec.py:152-157
# Reserve tokens for LLM call overhead (system prompt, function schemas, reasoning)
effective_limit = max_tokens_for_check - reserve_tokens
if effective_limit <= 0:
    effective_limit = max_tokens_for_check

usage_pct = (current_tokens / effective_limit * 100) if effective_limit > 0 else 0

# compression_exec.py:159  ← FORCE TRIGGER uses the same pct
if usage_pct > force_threshold:
...
# compression_exec.py:191  ← WARNING trigger uses the same pct
if usage_pct > self.pool.settings.compression_warning_threshold:
    self._inject_compression_warning(llm_messages, usage_pct, current_tokens, max_tokens_for_check)
```

The identical pattern exists in `_proactive_compression_check` at **lines 247–257** (`effective_limit = max_tokens − reserve_tokens`; `usage_pct = current_tokens / effective_limit × 100`).

`reserve_tokens` default = **3000** (`agent_cascade/settings.py:111-112`; confirmed in live config `config/agent-cascade-settings-2026-08-12T04-50-32.json:8`). `max_tokens_for_check` is the raw endpoint max (lines 96–107, 144–150).

### Path B — where "(A/B tokens)" is printed

```python
# compression_exec.py:192  ← passes the RAW window as B
self._inject_compression_warning(llm_messages, usage_pct, current_tokens, max_tokens_for_check)

# compression_exec.py:283-287  ← the warning string
warning = (
    f"[SYSTEM WARNING: Context window at {usage_pct:.1f}% capacity "
    f"({current_tokens}/{max_tokens} tokens). "
    f"Consider using compress_context to free space.]"
)
```

`usage_pct` (from Path A, denominator 117000) is printed next to `current_tokens/max_tokens` (denominator 120000). **That single call at line 192 is the mismatch.**

### Secondary sites with the same mismatch

- `agent_cascade/compression/handler.py:694-696` (`check_cooldown`):
  `max_tokens = self.engine._get_max_tokens(instance)` (raw, no reserve) → `_inject_compression_warning(llm_messages, usage_pct, current_tokens, max_tokens)` where `usage_pct` was computed on the effective limit upstream. Same mismatch.
- `agent_cascade/compression/handler.py:783-785` (`check_overfeeding` backoff gate): same pattern — raw `_get_max_tokens`, effective-limit `usage_pct`.

### Sites that are consistent (no fix needed)

- `compression_exec.py:164-169` — `ContextWindowExceeded` raise prints `(current_tokens/effective_limit tokens)` — matches its own `usage_pct`.
- `compression_exec.py:258-260` — proactive log prints only the %, no A/B pair.
- `web_ui/app.js:4640` — UI context bar computes `tokens / maxContext` for both the bar and the label (raw window, no reserve): self-consistent, but a *different definition* of "usage" than the engine's (no reserve). Not part of this bug; noted for completeness.

---

## 3. Impact: display-only, or does it skew the trigger?

**Both.**

1. **Display (the reported bug):** the warning string is self-contradictory. User sees 90.9% next to a fraction that equals 88.6%.

2. **Trigger (real but intentional):** every threshold comparison uses the effective-limit-based `usage_pct`:
   - force: `usage_pct > 96` (line 159) → fires at `current_tokens > 0.96 × 117000 = 112320` = **93.6% of the raw 120000 window**
   - warning: `usage_pct > 90` (line 191) → at 105300 tokens = **87.75% of raw**
   - proactive: `usage_pct > 95` (line 257) → at 111150 tokens = **92.6% of raw**
   - cooldown override (line 178) and the `ContextWindowExceeded` max-attempts raise (line 164) use the same pct.

   So with the default 3000-token reserve on a 120k window, force compression fires ~2.4 raw-window-points earlier than the "96" threshold suggests. Per the design docs this early trigger is **deliberate**: the reserve is headroom for system prompt / tool schemas / reasoning (`docs/compression_fix_plan.md:136`; `settings.py:112` comment; `tests/test_fallback_compression.py:1921` encodes exactly this arithmetic: "105k vs effective_limit(90k − 3k reserve) = 87k"). It is *not* a defect — but it is invisible to the user because the printed numbers don't reflect it.

**Net:** the trigger behavior is by design; the defect is purely that the feedback string advertises one denominator (raw window) while the percentage uses another (window − reserve).

---

## 4. Suggested Fix (recommendation only — NOT implemented)

**Goal (per task direction): cut the reserve from the max in the feedback, so the displayed % matches the printed ratio.**

### Minimal change (2 call sites + 1 optional doc fix)

1. **`agent_cascade/engine/compression_exec.py:192`** — pass the effective limit as the printed denominator:
   ```python
   self._inject_compression_warning(llm_messages, usage_pct, current_tokens, effective_limit)
   ```
   Warning then reads `Context window at 90.9% capacity (106350/117000 tokens)` — internally consistent.

2. **`agent_cascade/compression/handler.py:695` and `:784`** — apply the same definition in the two secondary sites:
   ```python
   max_tokens = self.engine._get_max_tokens(instance)
   reserve = self.pool.settings.compression_context_reserve_tokens
   if max_tokens - reserve > 0:
       max_tokens -= reserve
   ```
   (or thread `effective_limit` through instead of recomputing).

3. **Optional:** update the `_inject_compression_warning` docstring (`compression_exec.py:273-282`) to state that the printed denominator is the *effective* limit (window minus `compression_context_reserve_tokens`), so the reserve stays discoverable.

### Alternatives considered and rejected

- **Display a raw-window percentage** (`current_tokens / max_tokens × 100`) while keeping the trigger on the effective limit: rejected — introduces two different "usage" definitions in one message and hides the reserve entirely; the trigger would then look "late" to a user reading the printed % against the threshold.
- **Remove the reserve from the trigger** (compare against raw window): rejected — changes compression timing (later triggers, higher truncation risk); the reserve is a documented, intentional safety margin.

### Risks / notes

- Any log scrapers or tests asserting the old `A/B` format should be updated (search for `capacity ` / `% capacity` in `tests/`).
- The UI context bar (`web_ui/app.js:4640`) still uses the raw window; after the fix the engine warning and the UI bar will use different denominators (117000 vs 120000). Acceptable — different surfaces — but worth a one-line comment in the UI code if that divergence bothers the team.
- No behavior change to compression timing; display-only.

---

## 5. Open Questions

- None blocking. (Minor: whether the UI bar should also annotate the reserved headroom — a product decision, out of scope.)

---

## 6. Fix Applied (orchestrator)

Implemented per §4 with one improvement: instead of inlining `max − reserve` at two
handler.py call sites (drift risk), added a single-source-of-truth helper and used it
everywhere the warning is emitted.

**Changes:**
1. **`agent_cascade/engine/core.py`** — new `ExecutionEngine._get_effective_limit(instance)`:
   returns `_get_max_tokens(instance) − compression_context_reserve_tokens`, falling back to
   raw max if that would be ≤ 0. Single source of truth for the "usable window."
2. **`agent_cascade/engine/compression_exec.py:194`** — warning now passes `effective_limit`
   (already in scope) instead of the raw `max_tokens_for_check`.
3. **`agent_cascade/compression/handler.py:696, 786`** — both `_inject_compression_warning`
   call sites now pass `self.engine._get_effective_limit(instance)` instead of raw
   `_get_max_tokens(instance)`.

**Result:** every "NN.N% capacity (A/B tokens)" warning now uses the SAME denominator for the
% and the B, so they agree. E.g. `90.9% capacity (106350/117000 tokens)` instead of the old
contradictory `90.9% ... (106350/120000)`. The already-consistent sites (`ContextWindowExceeded`
raise, post-compression message, UI bar) are untouched. **No change to compression trigger timing**
(the reserve-based early trigger is intentional and unchanged).

**Verification:** all 4 edited files parse; `ExecutionEngine._get_effective_limit` present;
all 3 `_inject_compression_warning` call sites confirmed passing the effective limit;
`tests/compression/ tests/test_session_caption.py` → **89 passed**.

## 7. Evidence Citations

- `agent_cascade/engine/compression_exec.py:93-94, 152-159, 164-169, 190-192, 247-257, 283-287`
- `agent_cascade/compression/handler.py:694-696, 783-785`
- `agent_cascade/settings.py:103-112` (thresholds + reserve defaults)
- `config/agent-cascade-settings-2026-08-12T04-50-32.json:5-8` (live values: 96 / 90 / 95 / 3000)
- `docs/compression_fix_plan.md:136` (reserve is intentional design)
- `tests/test_fallback_compression.py:1921` (test encodes effective-limit arithmetic)
- `web_ui/app.js:4640` (UI context bar, separate consistent definition)
