# `delete_and_insert` match_mode — Implementation Plan

## 1. Feature Summary

We're adding a new `match_mode` called **`delete_and_insert`** to the `edit_file` tool. Instead of matching text content (like `exact`, `heuristic`, or `heuristic_agnostic`), this mode treats the `old_content` argument as a **line range specification** (`start:end`) that identifies which lines to delete before inserting new content at position `start`.

**Why**: This gives agents a precise, line-number-based editing primitive without needing to copy-paste exact file content. It's faster for bulk operations (delete N lines), surgical insertions (insert at line X), and avoids the character-for-character matching overhead of existing modes.

**Behavior at a glance**:
| `old_content` | `new_content` | Effect |
|---|---|---|
| `"3:7"` | `"hello\nworld\n"` | Delete lines 3–7, insert replacement at line 3 |
| `"5:10"` | `""` | Delete lines 5–10 only |
| `"4"` | `"new line\n"` | Insert "new line" before line 4 (pure insert) |
| `"0"` | `"header\n"` | Append at end of file |
| `"-2"` | `"footer\n"` | Insert before last-1 line |

---

## 2. Scope & Changes

| # | File | Lines Affected | Change Type |
|---|---|---|---|
| 1 | `agent_cascade/tools/custom/file_ops.py` | ~627, ~636, ~686–689 | Add enum value, relax required params, add validation |
| 2 | `agent_cascade/operation_manager/file_operations.py` | ~465–789 (new branch), helper function near top of method | New `delete_and_insert` code path with range parsing and line manipulation |
| 3 | `agent_cascade/prompts/dna.py` | ~139, ~141 | Update TOOL_METADATA descriptions |
| 4 | `tests/tools/test_edit_file_modes.py` | (append) | New test cases for all scenarios |

**Total estimated new lines**: ~80 core logic + ~120 tests = ~200 lines.

---

## 3. Implementation Details Per File

### 3a. `agent_cascade/tools/custom/file_ops.py`

#### Change 1: Add enum value (line 627)
```python
# BEFORE (line 627):
'enum': ['exact', 'heuristic', 'heuristic_agnostic'],

# AFTER:
'enum': ['exact', 'heuristic', 'heuristic_agnostic', 'delete_and_insert'],
```

#### Change 2: Relax required parameters (line 636)
`new_content` must be optional for delete-only operations. Remove it from the required list and handle empty/missing gracefully.

```python
# BEFORE (line 636):
'required': ['path', 'old_content', 'new_content'],

# AFTER:
'required': ['path', 'old_content'],
```

#### Change 3: Validation in `call()` method (lines 686–689)
Add a branch before the general validation to handle `delete_and_insert` mode specifically. The check at line 688 (`if not old_content`) is fine, but we need to allow empty `new_content`:

```python
# AFTER line 688 (before existing checks), insert:
        if match_mode == 'delete_and_insert':
            # Validate range format early for better error messages
            if not old_content or ':' not in str(old_content).replace('-', ''):
                # Allow single number (insert-only) or start:end format
                try:
                    parts = old_content.split(':')
                    if len(parts) > 2:
                        return "ERROR: Invalid range format for delete_and_insert mode. Use 'start:end' or just a line number."
                except Exception:
                    pass
            # new_content can be empty string or None for delete-only — handled below
```

The existing `new_content` check at line 688–689 should be relaxed to allow empty strings in this mode. The simplest fix is to change the check:

```python
# BEFORE (lines 688-689):
        if new_content is None:
            return "ERROR: Missing 'new_content'. Please provide the text you want to replace old_content with."

# AFTER:
        if match_mode == 'delete_and_insert' and (new_content is None or new_content == ''):
            new_content = ''  # Explicit empty string for delete-only operations
```

---

### 3b. `agent_cascade/operation_manager/file_operations.py`

#### New branch location: After line 789 (`else:` return for invalid mode), insert the new `elif` block before it.

The existing structure is:
- Line 485–490: `if match_mode == 'exact':`
- Line 491–788: `elif match_mode in ('heuristic', 'heuristic_agnostic'):`
- Line 789–790: `else:` (catch-all error)

We insert **between lines 789 and 790**:

```python
        elif match_mode == 'delete_and_insert':
            file_lines = file_content.splitlines(keepends=True)
            total_lines = len(file_lines)

            # ── Range Parsing ────────────────────────────────────────
            start, end = _parse_range(old_content, total_lines)

            if start < 0 or end < 0 or start > total_lines or end > total_lines:
                return f"ERROR: Range '{old_content}' is out of bounds for file with {total_lines} lines."

            # ── Delete + Insert Operation ────────────────────────────
            before = file_lines[:start]
            after = file_lines[end:]  # end is exclusive (Python slice semantics)

            if new_content:
                inserted = new_content.splitlines(keepends=True)
                # Preserve line ending style from surrounding context
                if after and not inserted[-1].endswith(('\n', '\r')):
                    ref_ending = _detect_line_ending(after[0])
                    if ref_ending:
                        inserted[-1] = inserted[-1].rstrip('\r\n') + ref_ending
                file_lines = before + inserted + after
            else:
                # Delete-only: no insertion
                file_lines = before + after

            new_file_content = ''.join(file_lines)

            # ── Write result directly (skip content matching) ────────
            description = f"Delete-and-insert edit to: {path} (lines {int(old_content.split(':')[0])+1 if ':' in old_content else old_content})"
            tool_args = {'path': path, 'old_content': old_content, 'new_content': new_content or '', 'match_mode': match_mode}

            # Ownership + approval check
            if not self._is_auto_approved(path, agent_name):
                approved, reason = self.request_user_approval(
                    agent_name=agent_name,
                    tool_name='edit_file',
                    tool_args=tool_args,
                    description=description,
                )
                if not approved:
                    return f"REJECTED BY USER: {reason}"
                justification = reason
            else:
                justification = ""

            # Backup + write (reuse existing pattern)
            resolved = self._resolve_path(path, mode="rw")
            safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
            backup_dir = self.base_dir / "logs" / "backups" / safe_agent
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{resolved.name}.{int(time.time())}.bak"
            shutil.copy2(resolved, backup_path)

            try:
                backup_path_str = str(backup_path.relative_to(self.base_dir))
            except ValueError:
                backup_path_str = str(backup_path)

            resolved.write_text(new_file_content, encoding='utf-8')
            self.file_ownership[str(resolved)] = agent_name

            res_msg = f"APPROVED: Edited {path} (delete_and_insert mode)"
            if justification:
                res_msg += f"\nSecurity Justification: {justification}"
            if backup_path_str:
                res_msg += f" (Backup saved to: {backup_path_str})"
            return res_msg
```

#### Helper function: `_parse_range` and `_detect_line_ending`

These should be defined as **local functions inside the `edit_file` method** (same pattern as heuristic helpers at lines 571–623), placed right after reading file content (after line 478):

```python
        def _parse_range(range_str: str, total_lines: int):
            """Parse old_content as a range specification for delete_and_insert mode.
            
            Returns (start_idx, end_idx) as 0-based Python slice indices.
            - start is inclusive, end is exclusive.
            - 1-indexed input: '3:7' means lines 3 through 7.
            - Single number '4' means insert before line 4 (delete range [4:4] = empty).
            - Negative numbers count from end: -1 = last line.
            - 0 means append at end.
            """
            range_str = range_str.strip()
            
            if ':' in range_str:
                parts = range_str.split(':')
                if len(parts) != 2:
                    raise ValueError(f"Range must have exactly one ':'. Got '{range_str}'")
                
                start_part, end_part = parts
                
                # Parse start (1-indexed, 0 means append at end)
                if start_part.strip() == '' or start_part.strip() == '0':
                    start = total_lines + 1  # Append beyond last line
                else:
                    start = int(start_part)
                
                # Parse end (empty means same as start, i.e., delete single line at start)
                if end_part.strip() == '' or end_part.strip() == '0':
                    end = start + 1  # Delete just the start line
                else:
                    end = int(end_part)
                
                # Handle negative indices
                if start < 0:
                    start = total_lines + 1 + start  # -1 means last line
                if end < 0:
                    end = total_lines + 1 + end
                
                # Clamp
                start = max(0, min(start, total_lines + 1))
                end = max(0, min(end, total_lines + 1))
                
                if start > end:
                    raise ValueError(f"Start ({start}) must be <= end ({end})")
                
                # Convert to 0-based slice indices
                return start - 1, end  # [start-1 : end] in 0-based
                
            else:
                # Single number = insert-only (or delete single line if new_content is empty)
                if range_str == '0':
                    return total_lines, total_lines  # Append at end
                start = int(range_str)
                if start < 0:
                    start = total_lines + 1 + start
                start = max(0, min(start, total_lines))
                return start - 1, start  # Empty range at position (insert point)

        def _detect_line_ending(line: str) -> str:
            """Detect the line ending style of a line."""
            if '\r\n' in line:
                return '\r\n'
            elif '\n' in line:
                return '\n'
            elif '\r' in line:
                return '\r'
            return ''
```

#### Key design decisions embedded in the logic:

1. **Range format**: `start:end` where both are 1-indexed, matching Python slice semantics (start inclusive, end exclusive after conversion).
2. **Single number** (e.g., `"4"`): treated as insert point — empty delete range at that position.
3. **Zero value**: `"0"` means append at the very end of the file.
4. **Negative indices**: `-1` = last line, `-2` = second-to-last, etc. Same semantics as Python list indexing but 1-based offset (`-1 → total_lines`).
5. **Line ending preservation**: When inserting content without trailing newline, we match the surrounding context's line ending style (CRLF vs LF).

---

### 3c. `agent_cascade/prompts/dna.py`

#### Change 1: Update `old_content` description (line 139)
```python
# BEFORE:
'old_content': 'The EXACT literal text to replace. Include at least 3 lines of context with matching whitespace and indentation.',

# AFTER:
'old_content': "For exact/heuristic modes: The EXACT literal text to replace (include at least 3 lines of context). For delete_and_insert mode: A line range 'start:end' (1-indexed) specifying which lines to delete before inserting new_content.",
```

#### Change 2: Update `match_mode` description (line 141)
```python
# BEFORE:
'match_mode': "Optional: Match mode for old_content. Can be 'exact' (default), 'heuristic' (Python-aware structure matching), or 'heuristic_agnostic' (language-agnostic whitespace-only normalization).",

# AFTER:
'match_mode': "Match mode for editing. Options: 'exact' (default, character-for-character match), 'heuristic' (Python-aware structure matching), 'heuristic_agnostic' (whitespace-only normalization), or 'delete_and_insert' (old_content is a line range start:end to delete before inserting new_content).",
```

---

## 4. Range Parsing Specification

### Format: `start:end` (1-indexed)

| Input | File has 10 lines | Resulting slice (0-based) | Meaning |
|-------|------------------|--------------------------|---------|
| `"1:3"` | 10 | `[0:3]` → lines 1,2,3 deleted | Delete first 3 lines |
| `"5:5"` | 10 | `[4:5]` → line 5 only | Delete single line 5 |
| `"3:7"` | 10 | `[2:7]` → lines 3–7 deleted | Delete lines 3 through 7 |
| `"3:"` | 10 | `[2:4]` → line 3 only (end defaults to start+1) | Delete single line at position 3 |
| `":"` | 10 | `[0:1]` → line 1 only | Delete first line |
| `"5"` | 10 | `[4:4]` → empty range at pos 5 | Insert before line 5 |
| `"0"` | 10 | `[10:10]` → end of file | Append after all lines |
| `"-1"` | 10 | `[9:9]` → last line position | Insert before last line (between 9 and 10) |
| `"-2:-1"` | 10 | `[8:9]` → second-to-last line | Delete the second-to-last line |

### Parsing Rules (in order of evaluation):

1. **Split on `:`** — if present, parse as `start:end`. If absent, treat as single number.
2. **Empty parts default**: empty start → 1, empty end → start+1.
3. **Zero handling**: `0` means "beyond the last line" (append point).
4. **Negative conversion**: `val = total_lines + val` (so `-1 → total_lines`, which is the last line in 1-indexed terms).
5. **Clamp to bounds**: `[0, total_lines+1]` for both start and end.
6. **Validation**: start must be ≤ end after conversion.

### Clamping Behavior:
- Out-of-bounds positive values are clamped to `total_lines + 1`.
- Out-of-bounds negative values (e.g., `-100` on a 10-line file) clamp to `0`.
- The range `[start:end]` is always valid after clamping.

---

## 5. Pseudocode for Core Logic

```
FUNCTION edit_file_delete_and_insert(path, old_content_range, new_content, file_lines):
    total = LENGTH(file_lines)
    
    // ── Step 1: Parse range ───────────────────────────────
    IF ':' IN old_content_range:
        SPLIT on ':' → [start_str, end_str]
        start = PARSE_INT(start_str) OR (total + 1)   // empty or 0 → append point
        end   = PARSE_INT(end_str)   OR (start + 1)    // empty → delete single line
        
        IF start < 0: start ← total + 1 + start
        IF end < 0:   end ← total + 1 + end
    ELSE:
        start = PARSE_INT(old_content_range) OR (total + 1)
        IF start < 0: start ← total + 1 + start
        end = start
    
    // Clamp to valid range
    start ← MAX(0, MIN(start, total + 1)) - 1   // convert to 0-based
    end   ← MAX(0, MIN(end, total + 1))
    
    IF start > end:
        RETURN ERROR "Start exceeds end"
    
    // ── Step 2: Split file into before/deleted/after ───────
    before_lines = file_lines[0 : start]
    deleted_lines = file_lines[start : end]       // these are removed
    after_lines = file_lines[end : total]
    
    // ── Step 3: Prepare insertion content ─────────────────
    IF new_content IS NOT EMPTY:
        insert_lines = SPLIT(new_content, keepends=True)
        
        // Preserve line ending style from context
        IF after_lines IS NOT EMPTY AND insert_lines[-1] HAS_NO_LINE_ENDING:
            ref_ending = DETECT_LINE_ENDING(after_lines[0])
            APPEND ref_ending to insert_lines[-1]
    
    ELSE:
        insert_lines = []
    
    // ── Step 4: Reassemble file ───────────────────────────
    new_file_lines = before_lines + insert_lines + after_lines
    new_content = JOIN(new_file_lines)
    
    // ── Step 5: Backup, write, update ownership ───────────
    CREATE_BACKUP(path)
    WRITE_FILE(path, new_content)
    UPDATE_OWNERSHIP(path, agent_name)
    
    RETURN SUCCESS_MESSAGE
```

---

## 6. Test Plan

All tests go in `tests/tools/test_edit_file_modes.py` as a new test function:

### Function: `test_delete_and_insert_mode()`

| # | Test Name | `old_content` | `new_content` | File Setup | Expected Result |
|---|-----------|---------------|---------------|------------|-----------------|
| 1 | Normal delete+insert | `"3:5"` | `"X\nY\n"` | 8 lines of text | Lines 3–5 replaced with X,Y; file has 8 lines total (3 deleted, 2 inserted = -1 net) |
| 2 | Delete only (empty new_content) | `"2:4"` | `""` | 6 lines | Lines 2–4 removed; file has 4 lines |
| 3 | Insert only (single number) | `"3"` | `"inserted\n"` | 5 lines | New line inserted before original line 3; file has 6 lines |
| 4 | Append at end (start=0) | `"0"` | `"footer\n"` | 5 lines | Line appended; file has 6 lines |
| 5 | Insert at start (start=1) | `"1"` | `"header\n"` | 5 lines | Line inserted at beginning; file has 6 lines |
| 6 | Negative index insert | `"-1"` | `"near_end\n"` | 8 lines | Inserted before last line (between 7 and 8); file has 9 lines |
| 7 | Negative range delete+insert | `"-3:-1"` | `"replaced\n"` | 10 lines | Lines 8–9 deleted, replaced; file has 9 lines |
| 8 | Delete entire file content | `"1:10"` | `""` | 10 lines | File is empty (or just whitespace) |
| 9 | Out-of-bounds clamping | `"5:20"` | `"end\n"` | 10 lines | Lines 5–10 deleted, "end" inserted at position 5; no error |
| 10 | Single line delete+replace | `"4:4"` | `"new_line\n"` | 8 lines | Line 4 replaced with new content |
| 11 | CRLF preservation | `"3:3"` | `"mid\r\n"` | 6 lines (CRLF) | Inserted line has CRLF ending matching file |
| 12 | Empty range at middle (pure insert) | `"5"` | `"a\nb\nc\n"` | 4 lines | 3 lines inserted before original line 5 (which is past end, so appended) |
| 13 | Invalid format error | `"abc:xyz"` | `"test\n"` | 5 lines | Returns ERROR with helpful message |
| 14 | Negative out-of-bounds | `"-100"` | `"start\n"` | 5 lines | Clamped to beginning; inserts at start |

### Test structure pattern (reusing existing test infrastructure):
```python
def test_delete_and_insert_mode():
    with tempfile.TemporaryDirectory() as tmpdir:
        op_mgr = OperationManager(base_dir=tmpdir)
        op_mgr.file_ownership = {}
        
        file_path = Path(tmpdir) / "test_file.txt"
        
        # Test 1: Normal delete+insert
        file_path.write_text("line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\n", encoding='utf-8')
        op_mgr.file_ownership[str(file_path.resolve())] = "test_agent"
        
        res = op_mgr.edit_file(
            path="test_file.txt", agent_name="test_agent",
            old_content="3:5", new_content="X\nY\n", match_mode="delete_and_insert"
        )
        assert "APPROVED" in res
        content = file_path.read_text(encoding='utf-8')
        assert content == "line1\nline2\nX\nY\nline6\nline7\nline8\n"
        
        # ... (continue for all test cases)
```

---

## 7. Risk Assessment

### Medium Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Line ending mismatch** on Windows vs Linux | Inserted lines may have wrong line endings, causing visual artifacts or tool confusion | `_detect_line_ending` helper normalizes based on surrounding context; tests cover CRLF case (#11) |
| **Off-by-one errors** in range conversion (1-indexed → 0-based) | Wrong lines deleted/inserted | Thorough test coverage of boundary cases: start=1, start=0, negative indices, single-line ranges |
| **Empty file edge case** | File with no content or just a newline | Range parsing handles `total_lines = 0` by clamping; insert at `"0"` on empty file should work |
| **Backup consistency** | If write fails after backup, orphaned backup files | Reuse existing backup pattern from lines 808–822; same try/except structure |

### Low Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Breaking existing tests** | Existing `edit_file` modes might regress | New branch is isolated (`elif match_mode == 'delete_and_insert'`); no changes to existing branches |
| **Model confusion** about when to use which mode | Agents might pick wrong mode | Clear TOOL_METADATA description distinguishes the 4 modes; `exact` remains default |
| **Range parsing performance on huge files** | Files with 100K+ lines | Range parsing is O(1); file split is O(n) but only done for this mode. Same complexity as heuristic mode which already handles large files (test at line 91–120 covers 50K lines) |

### No Known Conflicts
- The `re_indent` tool has its own range parsing (lines 865–891) but uses a slightly different convention (no negative index support, no zero-as-append). Since they operate independently, there's no conflict. However, we could DRY up the code by extracting `_parse_range` into a shared utility if future features need similar parsing — this is noted as a **future improvement**, not part of this PR.

---

## Implementation Order (Recommended)

1. **First**: Update `agent_cascade/prompts/dna.py` — metadata changes are safe and isolated
2. **Second**: Add enum value in `file_ops.py` — minimal change
3. **Third**: Implement core logic in `file_operations.py` — the meat of the feature
4. **Fourth**: Relax validation in `file_ops.py` call method
5. **Fifth**: Write tests and verify all 14 test cases pass
6. **Finally**: Mark todo.md line 25 as complete

---

## File Reference Quick Lookup

| What | File | Lines |
|------|------|-------|
| Tool schema (enum, required) | `agent_cascade/tools/custom/file_ops.py` | 604–637 |
| Call method + validation | `agent_cascade/tools/custom/file_ops.py` | 647–697 |
| edit_file core logic | `agent_cascade/operation_manager/file_operations.py` | 465–843 |
| Heuristic branch (pattern to follow) | `agent_cascade/operation_manager/file_operations.py` | 491–788 |
| Backup + write pattern | `agent_cascade/operation_manager/file_operations.py` | 808–842 |
| re_indent range parsing (reference) | `agent_cascade/operation_manager/file_operations.py` | 865–891 |
| TOOL_METADATA dict | `agent_cascade/prompts/dna.py` | 93–157 |
| Existing tests | `tests/tools/test_edit_file_modes.py` | 1–309 |