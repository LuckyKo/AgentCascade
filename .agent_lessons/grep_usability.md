# Grep Usability Improvements - operation_manager.py

## Date: 2026-05-21

### Changes Made

#### H1: Add .gitignore/exclusion support and exclude parameter
- Added `exclude: str = ""` parameter to grep method signature and _try_subprocess_grep
- Added `ignore_vcs: bool = True` parameter — when True (default), ripgrep respects .gitignore; when False, passes --no-ignore
- In the subprocess path: added --glob '!<pattern>' for exclude, --no-ignore for ignore_vcs=False
- In the Python fallback: skip files in VCS/build directories (.git, node_modules, __pycache__, .venv, venv, dist, build, .tox) and also skip files matching the exclude glob pattern using fnmatch.fnmatch()

#### H2: Fix Python fallback path parsing on Windows
- Added `.replace('\\', '/')` to normalize paths to forward slashes in the Python fallback (both standard mode and context mode)
- Subprocess path already normalized paths; this makes both paths consistent

#### H3: Add context lines support
- Added `context: int = 0` parameter to grep method signature
- In subprocess path: passes -C <N> to ripgrep/standard grep; output parsing handles "---" separators
- In Python fallback: shows context lines with ">>>" prefix on matched line, spaces for context lines, "---" separators between groups
- Match count correctly tracks only actual matches (not context lines or separators)

#### M1: Smart case sensitivity
- Added `smart_case: bool = True` parameter (default True)
- Updated _compile_grep_pattern to accept optional keyword-only flags parameter
- When smart_case=True and pattern has uppercase → case-sensitive; otherwise case-insensitive
- In subprocess path: only passes -i when smart_case=False OR pattern has no uppercase
- Respects inline regex flags like (?-i:) and (?i:)

#### M3: Remove .strip() from matched lines
- Removed .strip() from matched line content in both subprocess path and Python fallback — preserves whitespace (critical for Python/YAML)

#### M4: Clean up list_dir formatting
- Replaced emoji markers with ASCII markers ([dir], [file]) in both operation_manager.py and file_manager.py

### Key Patterns Learned

1. **lru_cache with keyword-only args**: When a cached function needs optional parameters, make them keyword-only (`*, flags=0`) to prevent cache key collisions between positional and keyword calls.

2. **Context line counting**: When context mode is enabled, the output includes context lines and separators that should NOT be counted as matches. Track actual match count separately using `match_count` variable or by checking for ">>>" prefix in formatted lines.

3. **ripgrep vs standard grep for context**: Ripgrep prefixes context lines with a space (` file.py:10:context line`). Standard grep does NOT add this prefix. Only apply the >>>/    distinction when using ripgrep — for standard grep, all lines look the same and can't be distinguished.

4. **fnmatch vs Path.match() for exclude patterns**: `Path.match()` doesn't support `**` recursive wildcards. Use `fnmatch.fnmatch(str(path), pattern)` which handles `**` correctly.

5. **Always normalize Windows paths to forward slashes**: Use `.replace('\\', '/')` before outputting file paths in grep results so LLMs can parse with `split(':', 2)`.

6. **Preserve whitespace in matched lines**: Never use `.strip()` on matched line content — it removes indentation critical for Python/YAML/JSON files.

### Test Results
- 12 tests, all passing (test_grep_usability.py)
- Tests cover: compile flags, smart_case logic, list_dir no emoji, path normalization, no strip, context lines, exclude, VCS skip, backwards compatibility, context match count not inflated, fnmatch exclude, keyword-only flags