# Grep Tool Reliability Test Results

## Date: 2026-05-21

### Test Suite: test_greptool.py

Comprehensive reliability tests comparing `operation_manager.grep()` against equivalent shell commands (grep via subprocess). Tests cover core functionality and edge cases.

### Results Summary

**11/12 passed, 1/12 failed**

| # | Test | Status | Shell Comparison |
|---|------|--------|-----------------|
| 1 | Simple pattern 'hello' | ✓ PASS | Shell found 3 lines — consistent |
| 2 | Case-sensitive 'HELLO' with smart_case=False | ✗ FAIL (KNOWN BUG) | Shell correctly found 0 lines; tool found 5 (wrong) |
| 3 | Hidden directory SECRET_KEY | ✓ PASS | N/A |
| 4 | Include filter '*.py' | ✓ PASS | Shell found 3 lines — consistent |
| 5 | Exclude filter 'test*' | ✓ PASS | Shell found 2 lines — consistent |
| 6 | Special regex chars `def hello_world():` | ✓ PASS | N/A |
| 7 | Empty directory search | ✓ PASS | N/A |
| 8 | Non-existent path | ✓ PASS | N/A |
| 9 | Invalid regex pattern | ✓ PASS | N/A |
| 10 | Context lines mode | ✓ PASS | N/A |
| 11 | Smart case (smart_case=True) | ✓ PASS | N/A |
| 12 | ignore_vcs=False | ✓ PASS | N/A |

### Known Bug: smart_case=False Treated as Case-Insensitive

**Location:** `operation_manager.py`, `_try_subprocess_grep()` method, lines ~463 and ~485

**Bug Description:** When `smart_case=False` is passed to `grep()`, the search becomes case-insensitive instead of case-sensitive. This affects both the ripgrep subprocess path and the standard grep subprocess path.

**Root Cause:** The condition at line 485:
```python
if not smart_case or (not re.search(r'[A-Z]', pattern) and not has_inline_case_flag):
    cmd.append('-i')
```

When `smart_case=False`:
- `not smart_case` → `True`
- The entire OR condition is `True`, so `-i` (case-insensitive flag) is always added
- This makes the search case-insensitive, which is the **opposite** of expected behavior

**Expected Behavior:** `smart_case=False` should mean "no smart case" = plain case-sensitive search (no `-i` flag).

**Python Fallback:** The same logic exists in the Python fallback path at line ~645:
```python
if smart_case and re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
    flags = 0  # Case-sensitive
else:
    flags = re.IGNORECASE  # Case-insensitive
```

When `smart_case=False` with pattern "HELLO":
- The condition `smart_case and ...` evaluates to `False` (because smart_case is False)
- Falls through to `else: flags = re.IGNORECASE` — case-insensitive!

**Fix Required:** Both the subprocess path and Python fallback need their logic corrected. The intended behavior should be:
- `smart_case=True`: Smart case (lowercase pattern → case-insensitive, uppercase pattern → case-sensitive)
- `smart_case=False`: Plain case-sensitive search (no `-i`, no IGNORECASE)

**Suggested Fix for Subprocess Path:** Change the condition from:
```python
if not smart_case or (not re.search(r'[A-Z]', pattern) and not has_inline_case_flag):
    cmd.append('-i')
```
To:
```python
if smart_case and not re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
    cmd.append('-i')
elif not smart_case:
    pass  # Case-sensitive, don't add -i
else:
    cmd.append('-i')  # smart_case=True with no uppercase → case-insensitive
```

**Suggested Fix for Python Fallback:** Change the logic from:
```python
if smart_case and re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
    flags = 0
else:
    flags = re.IGNORECASE
```
To:
```python
if not smart_case:
    flags = 0  # Case-sensitive (no smart case)
elif smart_case and re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
    flags = 0  # Case-sensitive (smart case with uppercase pattern)
else:
    flags = re.IGNORECASE  # Case-insensitive (smart case with lowercase pattern)
```

### Test Infrastructure Notes

- The test creates a temporary directory structure with known content and compares grep tool output against shell grep commands
- Shell comparison confirms that the standard `grep -rI` command correctly handles case-sensitive searches without `-i`
- Tests verify both the subprocess fast path (standard grep, since ripgrep is not installed in the test container) and edge cases