"""Permanent lint gate: catch undefined-name (ruff F821-equivalent) regressions.

Scans every .py file under agent_cascade/ with a pure-stdlib, scope-aware AST analyzer
(see tests/_undef_names_check.py) and fails if any name is used in executable code or
non-string annotations without being defined/imported/builtin.

This catches the class of refactor bug where function bodies moved to sibling sub-modules
without carrying their imports (18 such bugs fixed in commit 2acf16e).

Run: python -m pytest tests/test_no_undefined_names.py
Or standalone: python scripts/check_undefined_names.py
"""

import sys
from pathlib import Path

# Ensure the tests/ directory is on sys.path so we can import the shared analyzer.
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _undef_names_check import check_package, default_package_root  # noqa: E402


def test_no_undefined_names():
    """Assert there are NO undefined-name (F821-equivalent) errors in agent_cascade/."""
    root = default_package_root()
    assert root.is_dir(), f"agent_cascade package not found at {root}"

    violations = check_package(root)

    if violations:
        # Build a clear, actionable failure message.
        by_file = {}
        for v in violations:
            by_file.setdefault(v.file, []).append(v)

        lines = [
            f"FAIL: {len(violations)} undefined name(s) across {len(by_file)} file(s):",
            "",
        ]
        for fname in sorted(by_file):
            lines.append(f"  {fname}")
            for v in by_file[fname]:
                lines.append(f"    line {v.line}:{v.col}  undefined name {v.name!r}")
            lines.append("")
        lines.append(
            "Fix: ensure every name used in executable code or non-string annotations is "
            "defined, imported, or builtin in the current scope chain. "
            "String annotations (forward refs) are exempt."
        )
        assert False, "\n".join(lines)
