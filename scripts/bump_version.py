#!/usr/bin/env python
"""Bump the AgentCascade patch version in agent_cascade/__init__.py.

Driven by a git POST-COMMIT hook (installed via scripts/install_version_hook.py).
After each successful commit it increments the PATCH component of `__version__`
(X.Y.Z -> X.Y.(Z+1)) and auto-commits the change, so every real commit is followed
by exactly one version bump. Post-commit (not pre-commit) means lint failures or
aborted commits never waste a version number. The value is surfaced to agents via
the system_info tool (AgentCascade Version line) and read by setup.py.

Usage:
    python scripts/bump_version.py            # bump patch +1 in the working tree only
    python scripts/bump_version.py --minor    # bump minor +1, reset patch to 0
    python scripts/bump_version.py --major    # bump major +1, reset minor+patch to 0
    python scripts/bump_version.py --show     # print current version, change nothing
    python scripts/bump_version.py --post-commit   # hook mode: bump + auto-commit (guarded)

The script is idempotent-safe: it always reads the CURRENT file content (not a cached
value) and rewrites only if the version actually changed. It exits 0 on success and
non-zero with a clear message on failure. In --post-commit mode failures are non-fatal
(the hook wraps them) so they never disturb the user's original commit.
"""
import argparse
import os
import re
import subprocess
import sys

# Resolve the repo root relative to this script (scripts/ -> repo root) so the hook
# works regardless of the CWD pre-commit invokes it from.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INIT_PATH = os.path.join(_REPO_ROOT, 'agent_cascade', '__init__.py')

_VERSION_RE = re.compile(
    r"^(__version__\s*=\s*)(['\"])([^'\"]+)(['\"])",
    re.MULTILINE,
)

# Marker used in the auto-bump commit message so the post-commit hook can tell its
# own commits apart from user commits and avoid recursing.
_AUTO_COMMIT_MARKER = 'chore(version): bump to'


def _last_commit_message():
    """Return the subject line of HEAD, or '' on any failure."""
    try:
        out = subprocess.check_output(
            ['git', 'log', '-1', '--format=%s'],
            cwd=_REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:
        return ''


def _auto_commit(new_version):
    """Stage __init__.py and commit the bump. Returns True on success."""
    try:
        subprocess.check_call(
            ['git', 'add', os.path.relpath(_INIT_PATH, _REPO_ROOT)],
            cwd=_REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # -q quiet; --no-verify skips pre-commit (the bump is trivial and must not
        # be blocked/re-formatted by linters, which would churn __init__.py).
        subprocess.check_call(
            ['git', 'commit', '-q', '--no-verify', '-m', f"{_AUTO_COMMIT_MARKER} {new_version}"],
            cwd=_REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _read_version(path):
    """Return the current version string from __init__.py, or None if not found."""
    with open(path, encoding='utf-8') as f:
        content = f.read()
    m = _VERSION_RE.search(content)
    if not m:
        return None
    return m.group(3)


def _bump(version, level):
    """Return a new version string with `level` (major|minor|patch) incremented."""
    parts = version.split('.')
    # Tolerate versions that don't have 3 components by padding.
    while len(parts) < 3:
        parts.append('0')
    nums = []
    for p in parts[:3]:
        # Strip any non-numeric suffix (e.g. "1-beta"); keep the leading digits only.
        m = re.match(r'(\d+)', p.strip())
        nums.append(int(m.group(1)) if m else 0)
    major, minor, patch = nums
    if level == 'major':
        major += 1
        minor = 0
        patch = 0
    elif level == 'minor':
        minor += 1
        patch = 0
    else:  # patch
        patch += 1
    return f"{major}.{minor}.{patch}"


def main(argv=None):
    parser = argparse.ArgumentParser(description='Bump AgentCascade __version__.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--show', action='store_true', help='print current version only')
    group.add_argument('--minor', action='store_true', help='bump minor, reset patch')
    group.add_argument('--major', action='store_true', help='bump major, reset minor+patch')
    parser.add_argument(
        '--post-commit',
        action='store_true',
        help='hook mode: bump patch and auto-commit (skips if HEAD is our own bump)',
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(_INIT_PATH):
        print(f"ERROR: {os.path.relpath(_INIT_PATH, _REPO_ROOT)} not found", file=sys.stderr)
        return 1

    current = _read_version(_INIT_PATH)
    if current is None:
        print('ERROR: could not find __version__ in agent_cascade/__init__.py', file=sys.stderr)
        return 1

    if args.show:
        print(current)
        return 0

    # Recursion guard for post-commit mode: if HEAD is already one of our own bump
    # commits, do nothing (the auto-commit would otherwise re-trigger this hook).
    if args.post_commit and _AUTO_COMMIT_MARKER in _last_commit_message():
        print('post-commit: HEAD is a version-bump commit; skipping')
        return 0

    level = 'major' if args.major else ('minor' if args.minor else 'patch')
    new_version = _bump(current, level)

    if new_version == current:
        # Nothing to do (shouldn't happen for a bump, but guard against it).
        return 0

    with open(_INIT_PATH, encoding='utf-8') as f:
        content = f.read()
    updated = _VERSION_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{new_version}{m.group(4)}",
        content,
        count=1,
    )
    with open(_INIT_PATH, 'w', encoding='utf-8') as f:
        f.write(updated)

    print(f"version bumped: {current} -> {new_version} ({level})")

    if args.post_commit and not _auto_commit(new_version):
        # Non-fatal: the user's original commit already landed; only the bump failed.
        print('post-commit: version file updated but auto-commit failed', file=sys.stderr)
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
