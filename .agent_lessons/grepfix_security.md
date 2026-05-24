# GrepFixer Security Fixes - operation_manager.py

## Date: 2026-05-20

### Fix 1: Truncation Bypass When Result Limit Hit (CRITICAL)
- **File:** `operation_manager.py`, line ~437
- **Bug:** `and not hit_result_limit` in the truncation condition meant that when grep hit the 5000-result limit, character truncation was completely skipped → nearly 1GB output possible
- **Fix:** Removed `and not hit_result_limit` so truncation always applies when char_limit is exceeded

### Fix 2: Path Containment Using Flawed String Prefix Matching (HIGH)
- **File:** `operation_manager.py`, lines ~256-304
- **Bug:** `_resolve_path` used `str.startswith()` for directory containment → sibling-directory escape possible (e.g., `/workspace_evil` passes startswith for `/workspace`)
- **Fix:** Added `_path_is_contained()` static method using `os.path.commonpath()`, same pattern as `code_interpreter.py`'s `_is_path_allowed()`. Made case-insensitive comparison unconditional (not just on Windows).

### Fix 3: Spill Filename Sanitization (MEDIUM)
- **File:** `operation_manager.py`, lines ~442 and ~1075
- **Bug:** Only replaced `/` and `\` in agent names → `../evil` could create spill files outside logs dir
- **Fix:** Used `re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)` to allow only safe characters

### Fix 4: Backup Directory Path Traversal (CRITICAL - caught by reviewer)
- **File:** `operation_manager.py`, lines ~108, ~499, ~762
- **Bug:** `agent_name` used unsanitized in backup directory paths for `cleanup_backups()`, `write_file()`, and `edit_file()` → path traversal via `../../../tmp/evil`
- **Fix:** Added same regex sanitization before constructing backup paths

### Fix 5: Unbounded Spill File Size (HIGH - caught by reviewer)
- **File:** `operation_manager.py`, lines ~448 and ~1079
- **Bug:** After Fix 1, when result limit was hit AND char limit exceeded, the FULL untruncated output was written to disk as a spill file with no size cap
- **Fix:** Added `MAX_SPILL_SIZE = 50 * 1024 * 1024` (50MB) constant. Both grep and shell spill writes now check this before writing

## Key Pattern: Always sanitize `agent_name` before ANY filesystem path construction
The regex `re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)` is the standard sanitization pattern used throughout. Apply it whenever `agent_name` is used in a Path expression.