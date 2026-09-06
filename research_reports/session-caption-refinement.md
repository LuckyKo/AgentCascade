# Session Caption Feature Refinement Review

**Verdict (final): CLEAN PASS — all actionable findings resolved, remainder justified.**

## Disposition of each finding (orchestrator)
- 🔴 **Prompt duplication** → **FIXED.** Extracted shared `CAPTION_INSTRUCTION` constant in `dna.py`; both `COMPRESSION_PROMPT` and `CONSOLIDATION_PROMPT` now reference it (distinct intro lines preserved). Verified: both prompts still carry the caption instruction; 89 tests pass.
- 🟠 **Magic number 120** → **FIXED.** Added rationale comment (UI display heuristic, intentionally not env-configurable) + tightened `_truncate_caption` docstring.
- 🟡 **Duplicate CSS rule** → **FIXED.** Confirmed the file has a single theme scope (one `:root`, no `[data-theme]` blocks), so the two identical top-level `.session-item-caption` rules were a real copy-paste artifact. Removed the second; exactly one rule remains.
- 🟠 **Over-defensive regex fallback** → **JUSTIFIED (kept).** The first-marker re-anchor guards a real rare case: summarized content that itself contains `--- END SUMMARY ---` (or a marker leaking into the caption). It's 6 lines, tested (`test_session_caption.py`), and prevents a stray marker in the body being mis-detected later. Comment sharpened to name the concrete trigger. Not over-engineering given cost≈0.
- 🟡 **set_caption vs update_supervisor** → **JUSTIFIED (intentional).** Supervisor is re-assignable (ownership transfers) so it overwrites; a caption is set once by first compression so first-wins is correct. Added a docstring note making the deliberate contrast explicit rather than changing either method.
- 🟡 **`_LineSpy` test complexity** → **SKIPPED.** Tests are valid and green; not worth churning passing tests for aesthetics (YAGNI on the cleanup itself).
- 🔵 **Nits** → Took the cheap docstring clarity one; skipped speculative `_SUMMARY_PREFIXES` expansion (no evidence of missed cases).

Re-verified after fixes: `tests/test_session_caption.py tests/compression/` → **89 passed**; all 4 edited Python files parse; exactly one `.session-item-caption` CSS rule.

---
## Original review findings (below)

## Findings

### 🔴 BLOCKER

**dna.py:90-134 - Duplicate prompt text across COMPRESSION_PROMPT and CONSOLIDATION_PROMPT**

The caption instruction block (lines 100-104 in COMPRESSION_PROMPT and lines 129-133 in CONSOLIDATION_PROMPT) is nearly identical, with only the introductory context differing. This creates unnecessary duplication that will be hard to maintain if caption requirements change.

**Suggested fix:** Extract the common caption instruction into a shared constant or function parameter. For example:
```python
CAPTION_INSTRUCTION = (
    "The VERY LAST line of your entire output MUST be exactly this single line: "
    f"`{COMPRESSION_END_MARKER} CAPTION: <one short sentence>` — "
    "i.e. the marker `{COMPRESSION_END_MARKER}` followed on the SAME line by `CAPTION:` and a one-line caption. "
    "Do NOT emit the marker anywhere else. The caption must be ≤ ~120 chars, single line only."
)

COMPRESSION_PROMPT = (
    "Summarize the following conversation history.\n...\n\n"
    f"Present the summary below.\n\n{CAPTION_INSTRUCTION}"
)

CONSOLIDATION_PROMPT = (
    "You are consolidating multiple existing conversation summaries...\n...\n\n"
    f"Present the consolidated summary below.\n\n{CAPTION_INSTRUCTION}"
)
```

### 🟠 MAJOR

**agent_cascade/compression/agent_invoker.py:52 - Over-defensive regex pattern and logic**

The `_CAPTION_TAIL_RE` comment and the fallback logic in `_parse_compression_output` (lines 94-100) seem to handle an edge case (marker appearing in caption) that is extremely unlikely. The comment mentions "spurious second marker, e.g. inside a caption" — but why would a model insert the exact end marker string inside a caption? This feels like over-engineering.

**Suggested fix:** Simplify to basic regex match without the complex fallback unless there's evidence this edge case occurs in practice. If it is needed, add a comment explaining the real-world scenario that triggered it.

**agent_cascade/api_server.py:157 - Magic number `_SESSION_CAPTION_MAX_LEN = 120`**

The value 120 appears without context. Why 120? Is it UI pixel width? Character limit for display? The docstring says "Display length cap" but doesn't explain the choice.

**Suggested fix:** Rename to `SESSION_CAPTION_DISPLAY_LIMIT` and add a comment explaining why 120 was chosen (e.g., "fits in UI column", "matches design spec").

### 🟡 MINOR

**agent_cascade/logger/agent_instance_logger.py:118-134 - set_caption vs update_supervisor inconsistency**

`update_supervisor()` (lines 109-116) has no guard against overwriting existing values, while `set_caption()` (lines 118-134) implements first-wins semantics. This is inconsistent design for two similar metadata fields. Either both should be first-wins, or neither.

**Suggested fix:** Align the two methods. Since captions are meant to be set once by compression, first-wins makes sense. Update `update_supervisor()` to also respect first-wins if that's the intended behavior, or remove the guard from `set_caption()` if both should allow updates.

**web_ui/styles.css:2067-2073 AND 2292-2298 - Duplicate CSS rule**

The `.session-item-caption` class is defined twice in styles.css (lines 2067-2073 and 2292-2298). This appears to be a copy-paste error from the git diff where both the old and new versions exist.

**Suggested fix:** Remove one of the duplicates. Check if they're identical (they appear to be) and keep only one.

### 🟡 MINOR

**tests/test_session_caption.py:167-252 - Overly complex test structure for simple assertions**

The `_LineSpy` class (lines 242-252) is a bit elaborate for testing early file reading. A simpler approach would be to mock `open` and count `readline` calls directly without wrapping the file handle.

**Suggested fix:** Simplify by patching `open` to return a generator or counting calls on the real file object. However, this is not critical as the tests are still valid.

### 🔵 NIT

**agent_cascade/compression/agent_invoker.py:42-45 - `_SUMMARY_PREFIXES` list could be more explicit**

The prefixes list includes `"here's a summary"` with an apostrophe, but `"here is a summary"` without. Could normalize to consistent patterns or add more variations like `"summary: "`.

**Suggested fix:** Add common variations and perhaps make it case-insensitive (though the code already lowercases). Not urgent.

**api_server.py:160-171 - `_truncate_caption` returns empty string for `None` but docstring says "Returns '' for empty/None input"**

The function checks `if not text:` which handles both `None` and empty strings, so this is fine. But the docstring could be clearer: "Returns '' for falsy input."

---

## Bloat/Complexity Assessment

The feature implementation is appropriately sized overall. The core logic in `_parse_compression_output` and `set_caption` is clear and focused. However, the duplicated prompt text is a maintenance burden, and the defensive regex fallback feels like over-engineering without clear evidence of need. The UI additions are minimal and well-scoped.

## What's Good

- Clean separation: caption parsing happens in `_parse_compression_output`, keeping summary body clean for model context
- First-wins semantics for captions prevent later overwrites, which is correct for this use case
- API helpers are small and do one thing well
- Tests cover the critical invariants (caption not leaking into marker body)
- CSS/JS changes are minimal and non-invasive

---

## Re-review (commit 4bc4787)

**Final Verdict:** PASS

- Finding #1 (Prompt duplication): ✅ **Fixed correctly.** CAPTION_INSTRUCTION constant extracts shared text; both prompts reference it; prompt text identical to original. Verified by diff and tests pass.
- Finding #2 (Magic number 120): ✅ **Fixed correctly.** Added explanatory comment and improved docstring. Rationale is clear.
- Finding #3 (Duplicate CSS): ✅ **Fixed correctly.** Removed second duplicate `.session-item-caption` rule; only one remains.
- Finding #4 (Over-defensive regex fallback): ✅ **Justified accepted.** The guard handles real edge cases (marker in summarized content); well-commented and tested; no change needed.
- Finding #5 (set_caption vs update_supervisor): ✅ **Justified accepted.** Intentional design difference clearly documented; no change needed.
- Finding #6 (Test complexity/nits): ✅ **No action needed.** Tests are valid and green.

**NEW ISSUES:** None identified. CAPTION_INSTRUCTION extraction produces identical prompt text. No f-string or formatting regressions. All 89 tests pass.

**TEST RESULT:** `python -m pytest tests/test_session_caption.py tests/compression/ -q` → 89 passed.

---

**Files reviewed:** dna.py, agent_invoker.py, core.py, agent_instance_logger.py, api_server.py, app.js, styles.css, test_session_caption.py, plus updated tests.
