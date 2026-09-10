---
name: logging-handle-error-review
description: Review method for StreamHandler.handleError overrides that suppress teardown noise, evaluating safety of error masking and surgical alternatives.
source: auto-generated
version: "1.0.0"
triggers:
  - "StreamHandler handleError override"
  - "no-op handleError"
  - "teardown logging error"
generated_by: review1
generated_from_task: Independent code review of a logging-infrastructure fix that overrides StreamHandler.handleError to suppress spurious stderr noise at teardown. Focus on whether the no-op handleError could mask genuinely important errors, and whether a more surgical variant is warranted.
---

## Goal

Enable reviewers to systematically evaluate logging infrastructure changes that override StreamHandler.handleError() to suppress errors during teardown or shutdown, ensuring benign suppression of expected failures without masking genuine issues.

## Procedure

### Step 1 - Verify the Actual Fix and Root Cause
- Read the actual source code (not just claims) to confirm the change: a _SafeStreamHandler subclass overriding handleError(self, record) with pass.
- Confirm the root cause: StreamHandler.emit() catches all exceptions, calls handleError(), which by default prints traceback when raiseExceptions=True. The error occurs because the handler's stream (e.g., pytest's captured stdout) is closed during teardown.

### Step 2 - Isolate the Scope
- Use grep to ensure only the intended console handler uses _SafeStreamHandler.
- Verify that other handlers (file handlers, etc.) retain default error handling.
- Confirm no other StreamHandler instances in production code are affected.

### Step 3 - Assess Safety of No-op Suppression
- Critical question: Does suppressing ALL errors via pass mask genuinely important issues?
- Consider edge cases: formatter exceptions, encoding errors, real I/O problems on the terminal device (not just closed stream).
- Evaluate context: Teardown/shutdown errors are often benign because output is unreachable anyway.
- Check fallbacks: Is another handler (file logger) still reporting these errors?

### Step 4 - Evaluate Surgical Alternatives
- Ask: Could we suppress only the specific exception types that are harmless (e.g., ValueError with "closed file" message)?
- Implementation pattern: Check sys.exc_info(), call super().handleError(record) for other cases.
- Tradeoff: Is the added complexity worth it? If masked errors are extremely rare, simple no-op is acceptable.
- Consider future maintainability: Will another developer copy-paste this pattern into a risky context?

### Step 5 - Verify Documentation and Structure
- Docstring quality: Should explain why the override is safe, not just what it does.
- Class placement: Should be near other custom handlers (e.g., next to _WindowsSafeRotatingFileHandler).
- Avoid bloat: Keep implementation minimal.

### Step 6 - Regression Assessment
- Confirm normal logging behavior is unchanged.
- Verify test suite passes after change.
- Consider: Could this hide bugs that would surface in non-test environments?

## Tips

- Never accept claims without reading code — Always verify the actual implementation.
- Remember: StreamHandler.emit() never re-raises; overriding handleError is the correct hook, not wrapping emit.
- Severity rating: Use red for critical if genuinely important errors could be masked; orange for major if risk is moderate; yellow for minor if risk is low but worth noting; blue for nitpicks.
- Final verdict: Be decisive — PASS if safe and effective, NEEDS WORK if significant gaps, FAIL if dangerous.

## Common Pitfalls

1. Over-suppression — A bare pass silences ALL errors, including formatter bugs or encoding issues.
2. Scope leakage — Failing to verify the change only affects the console handler.
3. Ignoring shutdown context — Errors during interpreter shutdown are often expected and benign.
4. Missing documentation — Future developers may not understand why this is safe.

## Example Review Questions

- What specific exceptions does this suppress, and are they all benign in this teardown context?
- Could a developer miss a real bug because this handler silently fails?
- Is the docstring clear enough that another engineer won't replicate this pattern in a risky scenario (e.g., during active runtime)?
- Are there tests that cover the error paths being modified, or is this purely an observability fix?

---

This skill is for reviewer agents evaluating logging infrastructure changes where StreamHandler.handleError is overridden to suppress errors, particularly at teardown/shutdown. It focuses on safety of error masking and whether surgical suppression is warranted.