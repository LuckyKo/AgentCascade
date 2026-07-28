#!/usr/bin/env python3
"""
Scans all Python files in the agent_cascade directory, finds functions with no
docstrings, and prints a report of the top 20 most frequently called undocumented
functions based on import/usage analysis (heuristic call-count).

Usage:
    python test_auto_skill_check.py [--base PATH]

Defaults base path to N:\work\WD\AgentCascade if not provided.
"""

import ast
import collections
import os
import re
import sys


def find_python_files(root: str) -> list[str]:
    """Recursively find all .py files under root, skipping common non-source dirs."""
    out: list[str] = []
    skip_parts = {"__pycache__", ".git", "node_modules", ".pytest_cache"}

    for dirpath, _, filenames in os.walk(root):
        if any(part in skip_parts for part in dirpath.split(os.sep)):
            continue
        for fname in filenames:
            if fname.endswith(".py"):
                out.append(os.path.join(dirpath, fname))
    return out


def get_undocumented_functions(file_path: str) -> list[tuple[str, int]]:
    """
    Parse a Python file and return (function_name, line_number) for functions
    that have no docstring.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source, filename=file_path)
    except Exception:
        return []

    results: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            has_docstring = ast.get_docstring(node) is not None
            if not has_docstring:
                results.append((node.name, node.lineno))
    return results


def estimate_usage_counts(file_path: str) -> dict[str, int]:
    """
    Heuristic usage/import analysis for a single file.

    Counts occurrences of identifiers used in call-like positions (name(...))
    as a rough proxy for how frequently a function/symbol is used. This is not
    perfect but works well enough for prioritization.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
    except Exception:
        return {}

    counts: collections.Counter[str] = collections.Counter()

    # Match identifier followed by '(' as potential call site
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", source):
        name = m.group(1)
        # Skip obvious non-call keywords/patterns
        if name in {
            "if",
            "for",
            "while",
            "with",
            "return",
            "import",
            "from",
            "class",
            "def ",
            "lambda",
            "try",
            "except",
            "finally",
            "raise",
            "yield",
        }:
            continue
        counts[name] += 1

    return dict(counts)


def scan_project(root: str):
    """
    Scan all Python files under root, collect undocumented functions, estimate
    their usage/import frequency across the codebase, and report the top 20.
    """
    py_files = find_python_files(root)

    # Map each undocumented function occurrence to its location
    func_locations: list[tuple[str, str, int]] = []  # (name, file_path, line_no)

    # Aggregate usage counts across all files
    global_usage: collections.Counter[str] = collections.Counter()

    for p in py_files:
        undoc = get_undocumented_functions(p)
        for fname, lineno in undoc:
            func_locations.append((fname, p, lineno))

        usage = estimate_usage_counts(p)
        for name, cnt in usage.items():
            global_usage[name] += cnt

    # Score each undocumented function occurrence by how often it appears as a call
    scored: list[tuple[int, str, str, int]] = []
    for name, file_path, lineno in func_locations:
        score = global_usage.get(name, 0)
        if score > 0:
            scored.append((score, name, file_path, lineno))

    # Sort by score descending, then by name for stability
    scored.sort(key=lambda x: (-x[0], x[1]))

    return scored[:20]


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else r"N:\work\WD\AgentCascade"
    base = os.path.abspath(base)

    if not os.path.isdir(base):
        print(f"Error: directory not found: {base}", file=sys.stderr)
        sys.exit(1)

    top20 = scan_project(base)

    print("Top 20 most frequently called undocumented functions (by heuristic usage count)")
    print("=" * 80)
    for rank, (score, name, file_path, lineno) in enumerate(top20, start=1):
        rel = os.path.relpath(file_path, base)
        print(f"{rank:2d}. {name:<30s} (score={score:>5d}) -> {rel}:{lineno}")


if __name__ == "__main__":
    main()