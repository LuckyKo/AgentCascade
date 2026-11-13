#!/usr/bin/env python3
"""Standalone CLI: flag tab-indented lines in the WebUI JavaScript bundle.

Usage:
    python scripts/check_js_indentation.py                # scan web_ui/*.js
    python scripts/check_js_indentation.py <file|dir> ...  # scan explicit paths

Exit codes:
    0 = clean (no tab-indented lines)
    1 = violations found
    2 = usage/IO error

The WebUI bundle is space-indented (2 spaces, see .editorconfig). This reuses the
same analyzer as tests/test_web_ui_indentation.py (single source of truth); it is
also wired as a pre-commit hook so a tab-indented region cannot land in the first
place.
"""

import sys
from pathlib import Path

# Ensure tests/ is on sys.path so we can import the shared analyzer.
_SCRIPT_DIR = Path(__file__).resolve().parent
_TESTS_DIR = _SCRIPT_DIR.parent / 'tests'
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _js_indent_check import WEB_UI_DIR, find_js_files, find_tab_indented_lines  # noqa: E402


def _expand(paths):
    """Expand dirs to their *.js files and keep explicit files as-is."""
    files = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(find_js_files(path))
        elif path.is_file():
            files.append(path)
        else:
            print(f"error: not found: {raw}", file=sys.stderr)
            return None
    return files


def main(argv=None) -> int:
    paths = sys.argv[1:] if argv is None else argv
    files = _expand(paths) if paths else find_js_files(WEB_UI_DIR)
    if files is None:
        return 2
    if not files:
        print('error: no JavaScript files to scan', file=sys.stderr)
        return 2

    violations = {}
    for path in files:
        bad = find_tab_indented_lines(path)
        if bad:
            violations[path] = bad

    if not violations:
        print(f"OK: no tab-indented lines in {len(files)} file(s)")
        return 0

    total = sum(len(bad) for bad in violations.values())
    print(f"FAIL: {total} tab-indented line(s) across {len(violations)} file(s). "
          'web_ui/*.js is space-indented (2 spaces, see .editorconfig); convert the '
          'leading tabs to spaces.\n')
    for path, bad in violations.items():
        print(f"  {path}")
        for line_no, line in bad[:20]:
            print(f"    line {line_no}: {line[:100]}")
        if len(bad) > 20:
            print(f"    ... {len(bad) - 20} more")
    return 1


if __name__ == '__main__':
    sys.exit(main())
