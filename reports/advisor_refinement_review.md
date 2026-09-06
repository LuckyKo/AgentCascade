# Skill Advisor Refinement Review

**Files reviewed:**
- `agent_cascade/advisor_runner.py` (280 lines)
- `agent_cascade/skills/advisor.py` (233 lines)
- `agent_cascade/prompts/dna.py` (lines 153-175: SKILL_ADVISOR_PROMPT)
- `tests/test_skill_advisor.py` (431 lines)
- `tests/test_skill_advisor_integration.py` (513 lines)

**Scope discipline:** Cross-file duplication (e.g., tool-filtering block copied from security_handler.py) is noted but not edited. "Fix 6"/incident-number comments are NOT present in these five files — they live exclusively in `security_handler.py` and other non-reviewed files. Verified via grep.

**Verdict:** NEEDS WORK (minor cuts for clarity and maintenance safety)

---

## Comments

### 1. Redundant timer comment
- **File/line:** `advisor_runner.py:159-160`
- **Text to cut:**
  ```python
                  except Exception:
                      pass  # Timer may have already fired
  ```
- **Why:** The comment restates what the `except Exception: pass` inherently implies. Remove the comment and keep just `pass`.

### 2. Overly verbose streaming comment
- **File/line:** `advisor_runner.py:170-171`
- **Text to trim:**
  ```python
                  # Streaming: broadcast per-tick updates to the UI (same pattern as
                  # security_handler.py). Safe from this thread — uses run_coroutine_threadsafe.
  ```
- **Why:** The parenthetical "(same pattern as security_handler.py)" is unnecessary cross-file documentation; the code itself should be self-explanatory. Trim to: `# Streaming: broadcast per-tick updates to the UI. Safe from this thread.`

### 3. Redundant docstring constant
- **File/line:** `skills/advisor.py:180`
- **Text to change:**
  ```python
      - Turn limit ``SECURITY_AGENT_MAX_TURNS`` (20)
  ```
- **Why:** Hardcoding "(20)" is maintenance risk; the constant already defines the value. Change to just `Turn limit ``SECURITY_AGENT_MAX_TURNS```.

---

## Code

### 4. Unnecessary import comment (circular import justification)
- **File/line:** `advisor_runner.py:75-76`
- **Text to cut:**
  ```python
      # Imports resolved lazily to avoid a circular import at module load time and to
      # mirror the defensive-import style used in security_handler.py._execute_check().
  ```
- **Why:** The comment is self-congratulatory and redundant; lazy imports are standard practice. Remove both lines and just leave the imports without comment.

### 5. Section headers with cross-file refs are borderline bloat
- **File/line:** `advisor_runner.py:115`
- **Text to trim:**
  ```python
      # ── 4. Tool-filtering config (SAME as security_handler.py:445-464) ───
  ```
- **Why:** While the duplication note is useful for future refactor, it bloats the code with cross-file references that should not live here. Change to a minimal comment: `# Tool-filtering config` or remove entirely if the code is self-explanatory.

### 6. Over-defensive try/except in runner (load-bearing but can be simplified)
- **File/line:** `advisor_runner.py:190-197`
- **Current:**
  ```python
                  # Keep instance_state fresh for UI (message_count)
                  try:
                      if hasattr(pool, '_execution') and hasattr(pool._execution, '_state_lock'):
                          with pool._execution._state_lock:
                              if instance_name in pool.instance_state:
                                  pool.instance_state[instance_name]['message_count'] = len(instance.conversation)
                  except Exception:
                      pass  # non-critical — never break the advisor over UI bookkeeping
  ```
- **Why:** The `hasattr` checks are defensive but the surrounding code already assumes `pool` has certain attributes. However, per instructions this is KEEP AS-IS because it's load-bearing. I note it here only to confirm it should NOT be cut.

---

## Tests

### 7. Replicated gate expression in unit test (maintenance risk)
- **File/line:** `test_skill_advisor.py:314-337` (class `TestAdvisorGateCondition`)
- **Issue:** The test hard-codes the exact `should_run_advisor` expression from `engine/core.py`. Any change to the core logic will cause this test to drift, giving false confidence.
- **Recommendation:** Either import the real expression from core.py and assert equality, or mark the test as a contract test of core.py with a comment explaining it must be updated in tandem with core.py. Example:
  ```python
  from agent_cascade.engine.core import should_run_advisor  # or import the exact lambda

  def test_gate_expression_matches_core():
      # This is a contract test — must stay in sync with core.py
      assert should_run_advisor(...) == expected
  ```
- **Severity:** 🟠 Major (brittle test that will cause silent regressions)

### 8. Deny path tests implementation details
- **File/line:** `test_skill_advisor.py:265-298` (class `TestDenyPath`)
- **Issue:** Tests the exact tuple shape `(None, [Message])` used by `engine/core.py`. This is testing implementation details that may change.
- **Recommendation:** Keep but add a note that this is testing observable behavior (no instance allocated, error message present) rather than the internal tuple structure. Consider extracting the deny-message construction to a helper function and test that instead.

### 9. Overly verbose test class separators
- **File/line:** `test_skill_advisor.py` and `test_skill_advisor_integration.py` (multiple lines like `# ===========================================================================`)
- **Text to trim:** Remove excessive separator lines (`# ===========================================================================`). Use single `# ===` or just blank lines.
- **Why:** These are cosmetic bloat that add no value and increase diff noise.

---

## Prompt

### 10. Slight redundancy in SKILL_ADVISOR_PROMPT
- **File/line:** `prompts/dna.py:154-173`
- **Text to trim:**
  ```python
      "You are NOT executing the task. You are NOT the worker. Do not use any tools beyond basic discovery. Respond with text only.\n\n"
  ```
- **Why:** The two "NOT" sentences can be combined: `You are a delegation advisor, not an executor or worker. Do not use tools beyond basic discovery. Respond with text only.` Saves ~40 characters and removes repetition.

- **Also trim:** The phrase "single message, no tool calls, nothing before or after" is already implied by "Respond in exactly this format". Could simplify to: `## RESPOND IN EXACTLY THIS FORMAT (text only, no tools):\n`

---

## KEEP AS-IS

The following look defensive but are **load-bearing** and should NOT be cut:

1. **First-yield timer guard** (`advisor_runner.py:136-139`): Critical protection against LLM that never yields first token.
2. **Non-critical try/except around UI bookkeeping** (`advisor_runner.py:190-197`): As noted, defensive but necessary for stability.
3. **Lazy imports** (`advisor_runner.py:77-80`): Circular-import avoidance is real and required.
4. **Brace-escaping regression tests** (`test_skill_advisor.py:173-189`): These are essential to prevent `.format()` injection bugs.
5. **Early-exit on verdict** (`advisor_runner.py:199-212`): Prevents wasted LLM calls after decision is made.
6. **Parse output error handling** (`skills/advisor.py:104-118`): Graceful degradation on malformed output is required for robustness.
7. **Integration tests for gate ordering** (`test_skill_advisor_integration.py`): These exercise the real gate logic and ensure advisor runs before allocation — not bloated.

---

## Summary of Required Changes

| # | File/Line | Category | Change |
|---|-----------|----------|--------|
| 1 | `advisor_runner.py:159-160` | Comment | Remove redundant comment, keep `pass` |
| 2 | `advisor_runner.py:170-171` | Comment | Trim to `# Streaming: broadcast per-tick updates to the UI.` |
| 3 | `skills/advisor.py:180` | Code | Remove `(20)` from docstring |
| 4 | `advisor_runner.py:75-76` | Comment | Remove lazy-import justification comment |
| 5 | `advisor_runner.py:115` | Comment | Trim cross-file ref or remove |
| 6 | `test_skill_advisor.py:314-337` | Test | Refactor to import real gate expression from core.py |
| 7 | `test_skill_advisor.py:265-298` | Test | Add note about implementation detail testing |
| 8 | All test files | Comment | Remove excessive separator lines |
| 9 | `prompts/dna.py:154-173` | Prompt | Combine "NOT" sentences, simplify format section |

**Final Verdict:** NEEDS WORK (9 targeted cuts/improvements recommended). The code is functionally correct but has accumulated minor bloat that can be safely trimmed for maintainability. No critical bugs found.
