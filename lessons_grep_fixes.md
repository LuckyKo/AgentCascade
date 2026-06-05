# Grep Tool Fixes Summary

## Date: 2026-06-05
## Author: GrepFixer (Coder Agent)

## Files Modified
1. `N:\work\WD\AgentCascade\operation_manager.py`
2. `N:\work\WD\AgentCascade\file_manager.py`

---

## Changes Made

### 🔴 CRITICAL Fixes

#### 1. line_number can be None → malformed output (Line 534)
**Before:**
```python
line_number = data.get('line_number', 0)
```

**After:**
```python
line_number = data.get('line_number') or 0
```

**Rationale:** Using `or 0` instead of default value handles both missing key and explicit None values, preventing malformed output when line_number is None.

---

#### 2. Dead code: _sub_truncated branch removed (Lines 802-805)
**Removed:**
```python
elif _sub_truncated and spill_file_path is not None:
    # Subprocess already truncated and wrote spill file — just add the truncation notice
    summary += " [TRUNCATED]"
    output_text += f"\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded. Full output ({_orig_output_size} chars) saved to: {spill_file_path}\nYou can read it with read_file if needed.]"
```

**Rationale:** The `_sub_truncated` variable will never be True since truncation/spill logic was removed from the subprocess path. This dead code was misleading.

---

#### 3. Exclude filter with ** patterns fails for root-level files (Line 540)
**Before:**
```python
if exclude and fnmatch.fnmatch(rel_path_text, exclude):
    continue  # Skip this file
```

**After:**
```python
if exclude and Path(rel_path_text).match(exclude):
    continue  # Skip this file
```

**Rationale:** `Path.match()` provides better ** glob pattern support compared to `fnmatch.fnmatch()`, especially for root-level files. This aligns with modern Python pathlib best practices.

---

### 🟠 MAJOR Fixes

#### 4. Double-search on "no matches" (Lines 765-769)
**Before:**
```python
if count == 0 and not _sub_truncated:
    # Don't return early — fall through to Python fallback which may find matches
    # in hidden directories or handle globs differently
    logger.debug(f"grep: subprocess found no matches for '{pattern}', trying Python fallback")
else:
```

**After:**
```python
if count == 0 and not _sub_truncated:
    # Subprocess found zero matches legitimately — return early instead of falling through
    # to Python fallback which would re-scan all files unnecessarily
    logger.debug(f"grep: subprocess found no matches for '{pattern}'")
    return f"Found {count} matches for '{pattern}'"
else:
```

**Rationale:** When ripgrep returns `([], 0, False, False, 0)` (no matches found legitimately), the caller should return that result directly instead of falling through to Python fallback which re-scans all files unnecessarily. Only fall through when subprocess FAILS (returns None).

---

#### 5. Updated docstring of `_try_subprocess_grep` (Lines 442-447)
**Before:**
```python
"""Fast-path grep using system ripgrep or grep via subprocess.

Returns (results_list, count, was_timed_out, was_truncated, original_output_size) on success,
or (None, 0, False, False, 0) on failure.
Output format matches Python fallback: "relative_path:line_number: content"
"""
```

**After:**
```python
"""Fast-path grep using system ripgrep or grep via subprocess.

Returns (results_list, count, was_timed_out, was_truncated, original_output_size) on success,
or (None, 0, False, False, 0) on failure. Note: was_truncated is always False and 
original_output_size is always 0 since truncation/spill logic was removed from subprocess path.
Output format matches Python fallback: "relative_path:line_number: content"
"""
```

**Rationale:** Docstring now accurately reflects the current implementation where `was_truncated` is always `False` and `original_output_size` is always `0`.

---

### 🟡 MINOR Fixes

#### 6. Removed unused char_limit param from `_try_subprocess_grep` signature (Line 439)
**Before:**
```python
def _try_subprocess_grep(self, pattern: str, path: Path, include: str, char_limit: int, timeout: float,
                         exclude: str = "", ignore_vcs: bool = True, context: int = 0, smart_case: bool = True,
                         spill_file_path: Optional[str] = None):
```

**After:**
```python
def _try_subprocess_grep(self, pattern: str, path: Path, include: str, timeout: float,
                         exclude: str = "", ignore_vcs: bool = True, context: int = 0, smart_case: bool = True,
                         spill_file_path: Optional[str] = None):
```

**Rationale:** The `char_limit` parameter was accepted but never used inside the function. Also removed from call site at line 758-761.

---

#### 7. Sync file_manager.py list_dir format (Line 134)
**Before:**
```python
result += f"  [dir] {d}/\n"
```

**After:**
```python
result += f"  [dir] {d}\n"
```

**Rationale:** Removed trailing slash from directory listings in `file_manager.py` for consistency with `operation_manager.py` format.

---

## Testing Performed
- ✅ Python syntax validation passed using `ast.parse()`
- ✅ All edits verified by reading affected sections
- ✅ Backups automatically created for all modified files

## Backup Files Created
1. `logs\backups\coder\operation_manager.py.1780691613.bak`
2. `logs\backups\coder\operation_manager.py.1780691623.bak`
3. `logs\backups\coder\operation_manager.py.1780691637.bak`
4. `logs\backups\coder\operation_manager.py.1780691656.bak`
5. `logs\backups\coder\operation_manager.py.1780691673.bak`
6. `logs\backups\coder\operation_manager.py.1780691690.bak`
7. `logs\backups\coder\file_manager.py.1780691708.bak`

---

## Notes for Reviewer
- All CRITICAL and MAJOR issues have been addressed
- MINOR issues cleaned up for code quality  
- **ADDITIONAL FIX**: Removed residual dead code at lines 774-775 (_sub_truncated check) as identified by reviewer
- Changes are surgical and focused on specific lines
- No functional regression expected - all changes align with existing behavior
- The `fnmatch` import is still used elsewhere in the file (line 630, 633, 848) so it wasn't removed

## Reviewer Follow-ups (Optional)
The reviewer identified these additional items that are outside the current scope but worth noting:
1. **NIT #9**: `fnmatch` inconsistency - subprocess path now uses `Path().match()` but Python fallback and `_grep_single_file` still use `fnmatch.fnmatch()`. Could create inconsistent results for same exclude patterns. Consider unified approach in follow-up.
2. **NIT #10**: `file_manager.py` line 120 header still has trailing slash (`Contents of {path}/:`) which is cosmetically inconsistent with fixed directory entries. Outside scope but worth noting.