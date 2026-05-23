# Lessons: edit_file Heuristic Comment Stripping Fix

## Problem
Heuristic match mode in `edit_file` was duplicating comments and corrupting indentation during file edits.

## Root Cause
The heuristic matching pipeline stripped comments before comparing content, but replacement used the raw file block. This created a structural mismatch:

1. **Matching phase**: Comments removed from both `old_content` and file content → normalized comparison
2. **Replacement phase**: `actual_old_content` extracted from raw file lines (with comments intact)
3. **Alignment phase**: difflib alignment built on comment-stripped, blank-line-filtered lines

If `old_content` had different comment text/count than the file:
- Comments could be silently lost (file has more comments → matched block includes them, but old_content didn't account for them)
- Comments could be duplicated (file and old_content both have comments in alignment, but replacement uses raw file block with all its comments)
- Indentation context was corrupted because unmapped lines had no per-line indent reference

## Fix
Removed `remove_comments_keep_layout()` from the heuristic matching pipeline entirely.

**Before**: 
```python
clean_file_content = remove_comments_keep_layout(file_content, ext)
clean_old_content = remove_comments_keep_layout(old_content, ext)
# ... matching on clean content ...
actual_old_content = "".join(file_lines[orig_start_idx : orig_end_idx + 1])  # raw
```

**After**:
```python
file_lines = file_content.splitlines(keepends=True)
# ... matching on raw content with whitespace normalization only ...
actual_old_content = "".join(file_lines[orig_start_idx : orig_end_idx + 1])  # raw
```

Now the matched block IS exactly what gets replaced — no structural gap.

## Key Behavioral Changes
- **old_content comments must match file**: If old_content has different comment text/count, the match will fail (LLM gets error feedback to retry)
- **Whitespace tolerance preserved**: `''.join(line.split())` normalization still tolerates indentation differences
- **Exact mode unchanged**: Only heuristic mode was affected

## Files Modified
- `operation_manager.py` — removed ~100 lines of comment stripping code from heuristic branch (lines 1037-1218 area)
- `todo.md` — marked bug as fixed

## Test Script
- `test_heuristic_comment_fix.py` — 6 tests covering:
  1. Basic comment preservation (no duplication/loss)
  2. Comment count difference causes match rejection
  3. Indentation preservation in nested structures
  4. Whitespace tolerance still works
  5. Comments are structural content (not stripped)
  6. C-style multiline comments treated as structural

## Design Principle
**Don't transform data for matching if the replacement uses untransformed data.** The simpler approach of matching on raw content with whitespace normalization only is more predictable and prevents silent data loss. If there's a mismatch, fail fast with clear feedback rather than silently corrupting the file.