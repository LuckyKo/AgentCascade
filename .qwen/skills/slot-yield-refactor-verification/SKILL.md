---
name: slot-yield-refactor-verification
description: Verify that a refactoring extraction of three-path slot-yield logic + pool-holder diagnostic into a shared module preserves IDENTICAL behavior and log strings.
triggers:
  - When reviewing refactoring PRs that extract duplicated three-path slot-yield logic
  - When verifying E2E tests grep specific log substrings
---

# Slot Yield Refactoring Verification

## Description
Verify that a refactoring extraction of three-path slot-yield logic + pool-holder diagnostic into a shared module preserves IDENTICAL behavior and log strings. The verification must confirm:
1. All log message strings are byte-for-byte identical to originals for both Security and Compression callers.
2. Control flow returns True/False in exactly the same conditions; caller_inst None → return False with NO logging.
3. Intentional behavior deltas (e.g., adding exc_info=True to a specific error log) are validated as correct/desirable.
4. No circular import risk: shared module depends ONLY on stdlib + logging.
5. _yielded_slot assignment and finally-block reacquire logic in callers remain untouched.

## Workflow

1. **Read all files**:
   - NEW shared module (e.g., `slot_yield_utils.py`)
   - MODIFIED callers (e.g., `security_handler.py`, `compression/agent_invoker.py`)
   - ORIGINAL backups for diffing

2. **Extract log strings from originals**:
   - Path 1: `[{prefix}] Releasing slot for '{name}' before {action}`
   - Path 2 warning: `LEAKED PERMIT DETECTED for '{name}' — force-releasing`
   - Path 2 error (holder not removed): `Force-release did not remove holder for '{name}' — leaving slot as-is`
   - Path 2 error (release exception): `Force-release check failed for '{name}': {e}` with exc_info=True
   - Path 3 skip: `[{prefix}_SKIPPED] No slot to yield for '{name}' — Pool holders: ...`
   - pool-inspect failure debug: `[{prefix}] Failed to inspect pool for '{name}': {e}`

3. **Compare against shared function**:
   - Verify exact string matching including brackets, spacing, punctuation, and special characters (em-dash)
   - Confirm prefix values match: `"SECURITY_SLOT_YIELD"` and `"COMPRESSION_SLOT_YIELD"`

4. **Verify control flow**:
   - `if not caller_inst: return False` (NO logging)
   - Path 1: `_slot_release` exists → log + release + return True
   - Path 2: `_leaked_holder` found → force-release, check removal, return True/False accordingly
   - Path 3: no leaked holder → diagnostic log + return False

5. **Validate intentional deltas**:
   - Check if any log messages changed (e.g., adding `exc_info=True`)
   - Ensure deltas are documented and acceptable for E2E tests

6. **Check circular import safety**:
   - Verify shared module imports ONLY stdlib + logging
   - Confirm no imports from `agent_cascade` package

7. **Confirm caller changes minimal**:
   - `_yielded_slot` assignment unchanged in callers
   - `finally` block reacquire logic untouched

## Output
Return PASS or a numbered list of concrete issues with file:line and severity (🔴 Critical, 🟠 Major, 🟡 Minor, 🔵 Nit).