#!/usr/bin/env python3
"""Standalone CLI: detect undefined names (ruff F821 equivalent) in agent_cascade/.

Usage:
    python scripts/check_undefined_names.py            # scan the default package
    python scripts/check_undefined_names.py <dir>      # scan a specific directory

Exit codes:
    0 = clean (no violations)
    1 = violations found
    2 = usage/IO error

This reuses the same analyzer as tests/test_no_undefined_names.py (single source of truth).
"""

import sys
from pathlib import Path

# Ensure tests/ is on sys.path so we can import the shared analyzer.
_SCRIPT_DIR = Path(__file__).resolve().parent
_TESTS_DIR = _SCRIPT_DIR.parent / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _undef_names_check import check_package, default_package_root, find_python_files  # noqa: E402


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Detect undefined names (ruff F821 equivalent) in agent_cascade."
    )
    parser.add_argument(
        "path", nargs="?", default=None,
        help="Directory to scan (default: the agent_cascade package).",
    )
    args = parser.parse_args(argv)

    root = Path(args.path).resolve() if args.path else default_package_root()
    if not root.is_dir():
        print(f"error: directory not found: {root}", file=sys.stderr)
        return 2

    violations = check_package(root)
    files_scanned = len(find_python_files(root))

    if not violations:
        print(f"OK: no undefined names in {files_scanned} files under {root}")
        return 0

    by_file = {}
    for v in violations:
        by_file.setdefault(v.file, []).append(v)
    print(f"FAIL: {len(violations)} undefined name(s) across {len(by_file)} file(s):\n")
    for fname in sorted(by_file):
        print(f"  {fname}")
        for v in by_file[fname]:
            print(f"    line {v.line}:{v.col}  undefined name {v.name!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
