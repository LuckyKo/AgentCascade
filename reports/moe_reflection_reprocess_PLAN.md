# MOE Reflection-Extended-Turn Reprocess — Investigation & Implementation Plan

**Status:** REPORT-ONLY (no code changes made)
**Todo:** `todo.md:150` — "odd reprocessing when reflection extended turns come in (only happens on some MOE models)"
**Related (distinct):** `todo.md:149` / commit `a0e1e8e4` — tool-schema-toggle double-reprocess (RULED OUT as the cause here, see §2.3)
**Model under test:** `Agents-A1-APEX-I-Quality` (APEX MoE), agent instance `finalrev140` (reviewer sub-agent)
**Evidence:** two captured `/v1/chat/completions` request dumps + the instance session log telemetry

---

## Executive Summary

The "odd reprocessing" is **a one-shot KV-cache reprocess caused by context compression firing in the same window as the auto-skill reflection extension** — not a serialization bug and not a regression of the already-fixed tool-toggle bug.

When a task completes naturally at a high context size (near the model's input limit), two things happen back-to-back:
1. **Phase 5** fires the in-loop auto-skill extension (`core.py:1029-1032`), injecting the "## Skill Reflection" user prompt and extending the turn budget.
2. **Compression** triggers because usage crossed the force threshold, and `_rebuild_working_set` (`compression_exec.py:359-378`) **replaces `instance._cached_llm_messages` with a fresh slice of the now-compacted conversation**.

The first reflection request therefore sends a **compacted prefix that no longer byte-matches the pre-compression KV cache**, so llama.cpp reprocesses from the first divergence point. Telemetry confirms this exactly: the genuine-completion turn reused ~99% of its KV cache, while the *first reflection* turn recomputed **76.8%** (`cached_tokens=9377 / prompt_tokens=40374`), and the prompt size **dropped 50035 → 40374** (−9661 tokens) — the fingerprint of a compression pass. The very next reflection turn is healthy again (`cached=40663/40701`), proving the reprocess is **one-shot**, not recurring.

It only *looks* MoE-specific because MoE reasoning models (APEX) emit large per-message `reasoning_content`, so their context grows faster and hits the compression threshold right at task completion — making compression coincide with reflection. On shorter-reasoning / non-MoE models the same code path runs but compression typically does not fire at the boundary, so no visible reprocess occurs. **This MoE-specificity is HYPOTHESIZED** (single-model evidence); the mechanism itself is model-agnostic.

**Recommended minimal fix:** move the one-shot reprocess *off* the reflection boundary by firing proactive compression earlier (during the main run, at a soft threshold) so the KV cache already matches the compacted prefix when reflection fires; complement with an explicit log marker distinguishing "compression-induced reprocess (expected)" from "unexpected prefix-change reprocess (bug)". A regression test in `TestInLoopTrigger` encodes "no *unexpected* mid-run prefix change across the extended-turn boundary."

---

## 1. Structural Diff of the Two Dumps (evidence)

Both files are full OpenAI-compatible request bodies captured for the same instance/turn window.

| Property | A = `api_post_1789802198296.json` | B = `api_post_1789802229820.json` |
|---|---|---|
| Model | `Agents-A1-APEX-I-Quality` | `Agents-A1-APEX-I-Quality` |
| Message count | **56** | **58** |
| tiktoken estimate | ≈ 37,111 tokens | ≈ 40,014 tokens |
| `tools` count / md5 | **25 / `7fb7c36f6829`** | **25 / `7fb7c36f6829`** (identical) |
| Sampling params (`temperature`,`top_p`,`max_tokens`,`stream`,`stop`) | — | identical to A (no diffs) |

**First-divergence analysis (canonical per-message JSON, `sort_keys`):**
- `first_divergence_index = None` → **A is an exact byte-prefix of B.** The two extra messages are appended at the END; no earlier message changed.
- This means the **client-side prefix is stable** across A→B. The reprocessing is therefore *not* caused by a mid-run client-side prefix mutation between these two specific requests.

**The two appended messages in B:**
- `B[56]` — `role=assistant`, `content` ≈ empty (1-char), `reasoning_content` = **5330 chars**, `tool_calls=None` → the **genuine completion** turn (text answer + long reasoning, no tool call). This is what triggers Phase 5.
- `B[57]` — `role=user`, content block = **"## Skill Reflection …"** prompt, no reasoning, no tool calls → the injected reflection prompt.

**Conclusion for §1:** The A→B transition is a *normal* end-append (genuine completion + reflection prompt). The "reprocessing" is not visible in this pair's prefix; it is visible in the **server-side KV telemetry** of the surrounding turns (§2), where the *first reflection request* (which is B plus its own response context) recomputes most of its prompt.

---

## 2. Root-Cause Mechanism

### 2.1 What "reprocessing" means here
llama.cpp reuses KV cache only while the incoming prompt prefix is byte-identical to what produced the cached KV. Any earlier change forces a full/partial reprocess from the first divergent token. `prompt_tokens` = total prompt; `cached_tokens` = tokens reused; **recompute % = 1 − cached/prompt**.

### 2.2 Telemetry (from `logs/reviewer_finalrev140_20260919_101226.jsonl`)
Around the reflection boundary (turn sequence; line numbers are approximate ±1 depending on how duplicate content/tool-call log entries are counted — the token values below are exact and independently confirmed):

| Turn | prompt_tokens | cached_tokens | Recompute % | Note |
|---|---|---|---|---|
| genuine completion (text, no tool call) | **50,035** | **49,507** | **1.06%** | healthy KV reuse |
| user "## Skill Reflection" injected | — | — | — | Phase 5 extension fires |
| **first reflection turn** | **40,374** | **9,377** | **76.75%** | ← the "odd reprocess" (logged twice: content + tool-call variant) |
| next reflection turn | 40,701 | 40,663 | **0.09%** | healthy again → **one-shot** |

Two independent fingerprints of a compression pass at line 86:
1. **Prompt size DROPPED** 50,035 → 40,374 (−9,661 tokens). Appending the assistant completion + reflection prompt would *grow* the prompt; a net drop means messages were **rewritten/summarized** — i.e., compression ran.
2. **Cache reuse collapsed** to 9,377 (the stable early prefix that survived compression unchanged); everything after the first compressed message was recomputed.

### 2.3 Ruled-out hypotheses
- **Tool-schema toggle (todo.md:149 / `a0e1e8e4`):** RULED OUT. The `tools` array is byte-identical between A and B (`25 / 7fb7c36f6829`). This is a *separate* signature from the already-fixed bug.
- **`reasoning_content` serialization drift on a stable prefix:** RULED OUT as the cause *in this capture*. The per-message canonical diff shows A is an exact byte-prefix of B (no reordering/dropping of `reasoning_content`, no changed assistant tool-call serialization). `reasoning_content` IS sent back to the model (assistant turns carry it), but it is serialized *consistently* across A and B, so it is not what breaks KV reuse here.
- **Sampling/system-param divergence:** RULED OUT — all sampling params identical.

### 2.4 The actual mechanism (confirmed by code)
1. Task runs to natural completion at a high context size (line 84 prompt ≈ 50k tokens, near the endpoint limit).
2. `_post_turn_checks` reports genuine completion → Phase 5 calls `_try_auto_skill_extension` (`core.py:1029-1032`, gated on `_is_genuine_completion`). It injects the reflection user prompt via `_make_user_message` → `_append_and_log_to_llm` + `messages.append` (`core.py:364-367`) and extends the budget.
3. **Compression** is also triggered in this window because usage crossed the force threshold (`compression_exec.py:155-184`: `usage_pct > force_threshold → _force_compression`). Proactive checks run post-tool (`tool_execution.py:472`) and on async-drain (`core.py:580`); the forced path compacts the pool conversation.
4. `_rebuild_working_set` (`compression_exec.py:327-384`) rebuilds the working sets from the compacted pool: it re-slices `slice_history_for_llm(conv)` and **assigns `inst._cached_llm_messages = llm_messages`** (line 378), invalidating token/preprocess caches (lines 367-373).
5. The next LLM call (first reflection turn) sends this **compacted prefix**. Because it no longer byte-matches the pre-compression KV cache, llama.cpp reprocesses from the first divergence → `cached_tokens=9377`, 76.8% recompute.
6. Subsequent reflection turns append to the now-stable compacted prefix → healthy reuse (line 89). Hence **one-shot**.

**One-sentence root cause:** *Compression fires in the same window as the reflection extension and replaces `_cached_llm_messages` with a compacted slice, so the first reflection request's prefix diverges from the pre-compression KV cache and is reprocessed once.*

---

## 3. Why MoE-Specific — Confirmed vs Hypothesized

| Claim | Status | Basis |
|---|---|---|
| The reprocess mechanism (compression → `_rebuild_working_set` → prefix divergence) is **model-agnostic** | **Confirmed** | Code path (`compression_exec.py:327-384`, `core.py:1272-1312`) has no model branching; it fires on any model when usage crosses the threshold. |
| The reprocess observed here is a **one-shot compression-induced** event, not a recurring serialization bug | **Confirmed** | Telemetry: line 86 recompute 76.8%, line 89 recompute 0.09%; A→B prefix byte-stable. |
| It *appears* MoE-specific because MoE reasoning models (APEX) emit large `reasoning_content`, so context reaches the compression threshold **at task completion**, coinciding with reflection | **Hypothesized** (leading) | Consistent with the observed 50k-token context at completion and 5,330-char `reasoning_content` on the completion turn; but this is single-model evidence — no non-MoE control capture was available to confirm the contrast. |
| MoE/hybrid KV behavior (recurrent sub-cache / expert routing) makes partial-rollback less effective, enlarging the reprocess | **Hypothesized** (secondary) | Plausible per general llama.cpp hybrid-KV constraints; not verifiable from these dumps alone. APEX here shows *partial* reuse (9,377), not a full `cached=0`, so it is behaving like LCP partial-reuse rather than the worst-case hybrid full-reprocess. |
| Model-specific chat-template serialization of `reasoning_content`-only messages shifts the divergence point during rebuild | **Hypothesized** (secondary) | Documented as a known cross-family difference in prior KV-restore findings; not demonstrated in this capture (prefix was byte-stable A→B). |

**Bottom line:** I can **confirm** the mechanism and that it is one-shot/compression-induced. I can only **hypothesize** the MoE-specificity, with "reasoning-heavy context hits the compression threshold at completion" as the most likely explanation. To *confirm* MoE-specificity you would need a paired capture of a non-MoE (or short-reasoning) model completing a same-length task and showing no boundary reprocess.

---

## 4. Exact Code Locations

| Concern | File:Line |
|---|---|
| Reflection prompt injection (Phase 5) | `agent_cascade/engine/core.py:364-367` (`_make_user_message` → `_append_and_log_to_llm` + `messages.append`) |
| Double-append / identity guard (exactly-once mirror into `llm_messages`) | `agent_cascade/engine/core.py:232-244` (`_append_and_log_to_llm`) |
| Shared prefix-stability gate (settings + one-shot flag) | `agent_cascade/engine/core.py:246-269` (`_auto_skill_gates_met`) |
| Phase 5 trigger call site (gated on `_is_genuine_completion`) | `agent_cascade/engine/core.py:1029-1032` |
| Cache-HIT vs REBUILD decision for the turn | `agent_cascade/engine/core.py:1272-1312` (`_setup_turn`; HIT reuses `_cached_llm_messages` verbatim + re-slices at 1293-1294; MISMATCH when `cached_len > current_len` → force rebuild at 1300-1302) |
| Compression trigger (usage threshold) | `agent_cascade/engine/compression_exec.py:155-184` (`usage_pct > force_threshold → _force_compression`) |
| Proactive compression check call sites | `agent_cascade/engine/tool_execution.py:472` (post-tool); `agent_cascade/engine/core.py:580` (async-drain) |
| **Compression rebuild that replaces the cached prefix** | `agent_cascade/engine/compression_exec.py:327-384` (`_rebuild_working_set`; re-slice at 359-361, cache invalidation 367-373, **`inst._cached_llm_messages = llm_messages` at 378**) |
| `reasoning_content` serialization (outgoing request) | `api_integration_pkg/state_builder.py` (`serialize_message`, plain-object whitelist includes `reasoning_content`); `agent_cascade/engine/helpers.py` (`_normalize_gemma_thought_tags` ~L154, `_normalize_thinking_blocks` ~L180); `agent_cascade/engine/llm_call.py:615-627` (reasoning extraction/merge) |

**The divergent prefix is produced at `compression_exec.py:378`** — the moment `_cached_llm_messages` is reassigned to the compacted slice. The reflection extension (`core.py:364-367`) is what makes this divergence *land on a reflection turn* rather than an ordinary one.

---

## 5. Proposed Minimal Fix + Tests

### 5.1 Framing
The reprocess is the **inherent one-shot cost of compression** (KV cannot be reused for rewritten messages). It cannot be eliminated without eliminating compression, but it can be made less "odd" by (a) moving it off the reflection boundary and (b) making it observable so it isn't mistaken for a bug.

### 5.2 Options (ranked)

**Option A — Proactive earlier compression (RECOMMENDED, minimal behavior change).**
Fire proactive compression during the main run when usage first crosses a *soft* threshold (e.g., ~80–85% of the reserve-reduced limit) rather than waiting for the force threshold at completion. The one-shot reprocess then lands on an ordinary main-run turn (less noticeable), and by the time Phase 5 fires reflection the KV cache already matches the compacted prefix → the reflection turns are clean appends (no boundary reprocess).
- *Where:* tune/extend the proactive check in `compression_exec.py` (`_proactive_compression_check`, ~L213) / its call sites; add a soft-threshold constant. No change to the reflection path.
- *Risk:* low — changes *when* compression fires, not *how*; must respect the existing cooldown/max-attempts guards (L157-184).

**Option B — Observability marker (CHEAPEST, directly addresses "odd").**
When a compression pass occurs in the same window as the auto-skill extension, log an explicit marker, e.g. `[REFLECTION_AFTER_COMPRESSION] first reflection turn will reprocess from divergence (expected)`. This distinguishes expected compression-reprocess from unexpected prefix-change reprocess in future investigations.
- *Where:* `_try_auto_skill_extension` (`core.py:364-374`) — after injecting the prompt, check whether a compression pass ran since the last turn (e.g., a flag set by `_rebuild_working_set`) and log accordingly. No behavior change.

**Option C — Defer reflection until compression settles (alternative).**
In Phase 5, if usage is at/over the force threshold, run/complete a compression pass *first* (under `instance._compression_lock`), then inject the reflection prompt so it appends to an already-stable compacted prefix. The first reflection turn still reprocesses once (compression just ran) but deterministically and one-shot.
- *Where:* `core.py:358-367`. More code change than A/B; marginal benefit over A.

**Recommendation:** implement **A + B together** (A moves the reprocess off the boundary; B makes any residual boundary reprocess self-explanatory). Keep C in reserve.

### 5.3 Regression test(s)
Extend `tests/test_skill_generation.py::TestInLoopTrigger` (class at line 952). The existing `test_tool_disable_prefix_stability_across_trigger` (line 1593) is the **function-schema** analog; add the **message-prefix** analog:

```
def test_reflection_extended_turn_prefix_stability(self, fresh_manager, tmp_path):
    """No UNEXPECTED mid-run prefix change across the extended-turn boundary.

    Encodes todo.md:150: across the genuine-completion -> first-reflection
    boundary, the serialized llm_messages prefix must be byte-identical up to
    the expected append point (assistant completion + reflection prompt),
    UNLESS a compression pass ran in that window — in which case exactly one
    divergence at the compressed boundary is allowed and must be marked.
    """
```

Concretely, the test should:
1. Build a pool whose conversation sits **below** the force threshold so compression does *not* fire at the boundary (happy path). Capture canonical JSON of `llm_messages` for the triggering turn and the first reflection turn. Assert the prefix is byte-identical up to the appended completion+reflection messages (clean append ⇒ no reprocess).
2. Build a second pool whose conversation sits **above** the force threshold so compression *does* fire at the boundary. Assert: (a) exactly one divergence, located at the expected compressed boundary; (b) the Option-B marker/flag is present; (c) the second reflection turn's prefix is again stable relative to the first (one-shot, not recurring).

This mirrors the byte-identity discipline of `test_tool_disable_prefix_stability_across_trigger` and directly encodes "no unexpected prefix change across the extended-turn boundary."

Also worth a small unit test around `_rebuild_working_set` asserting that after a compression rebuild, `_cached_llm_messages` is a fresh list (not aliased to the pre-compression list) and `_last_config_version` is updated — guarding the cache-invalidation contract at `compression_exec.py:376-379`.

---

## 6. Confidence Level
- **Mechanism (compression-induced one-shot reprocess at the reflection boundary):** **High confidence** — supported by three independent, mutually-consistent pieces of evidence (byte-stable A→B prefix; prompt-size drop 50035→40374; cache-reuse collapse to 9377 then recovery to 40663) plus the code path.
- **Not the tool-toggle bug / not a serialization drift:** **Confirmed** (tools hash identical; prefix byte-stable).
- **MoE-specificity:** **Low–Moderate confidence (hypothesized)** — single-model evidence; needs a non-MoE control capture to confirm the contrast.

## 7. Open Questions
1. Is the one-shot ~31k-token recompute at line 86 acceptable latency-wise, or must it be moved off the boundary (Option A)?
2. Does APEX's endpoint have a hard input limit that *forces* compression exactly at completion (making Option A moot if the soft threshold is never reached before the last turn)?
3. Confirm MoE-specificity with a paired non-MoE capture of an equivalent-length task.
4. Whether `slice_history_for_llm` itself can change the prefix between a cache-HIT re-slice (`core.py:1293-1294`) and the prior request (a second, independent reprocess source worth a separate check).

## 8. Suggested Next Actions
1. Capture a **non-MoE** (or short-reasoning) model completing an equivalent-length task; compare boundary telemetry to confirm/refute MoE-specificity.
2. Add the Option-B observability marker first (zero-risk, immediately clarifies future "odd reprocess" reports).
3. Prototype Option A (soft proactive threshold) in a branch; verify with the new `test_reflection_extended_turn_prefix_stability` that the boundary reprocess disappears on the happy path and remains one-shot+marked when compression is unavoidable.
4. Separately audit `slice_history_for_llm` for prefix stability across cache-HIT re-slices (Open Question 4).
