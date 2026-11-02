#!/usr/bin/env python3
"""Run all Node.js JS tests in a single command.

Usage:
    python tests/run_js_tests.py

Discovers every ``tests/test_*.js`` file and runs it with ``node``.
Exits 0 if all pass, 1 if any fail. Does NOT wire into pytest collection.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def main() -> int:
    js_tests = sorted(TESTS_DIR.glob('test_*.js'))
    if not js_tests:
        print('No JS test files found in', TESTS_DIR)
        return 0

    print(f"Running {len(js_tests)} JS test file(s) with node...")
    failures = 0
    for test_file in js_tests:
        print(f"\n{'='*60}")
        print(f"  {test_file.name}")
        print(f"{'='*60}")
        result = subprocess.run(
            ['node', str(test_file)],
            capture_output=True,
            text=True,
            cwd=str(TESTS_DIR.parent),
        )
        if result.stdout:
            print(result.stdout, end='')
        if result.stderr:
            print(result.stderr, end='', file=sys.stderr)
        if result.returncode == 0:
            print(f"  ✅ PASS")
        else:
            print(f"  ❌ FAIL (exit {result.returncode})")
            failures += 1

    print(f"\n{'='*60}")
    total = len(js_tests)
    print(f"  {total - failures}/{total} passed, {failures} failed")
    print(f"{'='*60}")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
