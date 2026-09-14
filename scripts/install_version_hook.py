#!/usr/bin/env python
"""Install the AgentCascade post-commit version-bump hook.

The git post-commit hook lives in .git/hooks (not versioned), so this script
installs it idempotently. Run once after cloning / on a new machine:

    python scripts/install_version_hook.py

Behavior: after every SUCCESSFUL commit, the patch component of __version__ in
agent_cascade/__init__.py is incremented by 1 and committed as an automatic
"chore(version): bump to X.Y.Z" commit. Using post-commit (not pre-commit) means
the version only moves when a commit actually lands — lint failures / aborted
commits never waste a version number.

The hook detects its own auto-commits (via the marker message) so it does not
recurse. It is best-effort: any failure is logged to stderr but never blocks
or undoes the user's original commit (post-commit runs after the commit exists).
"""
import os
import stat
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOOK_PATH = os.path.join(_REPO_ROOT, '.git', 'hooks', 'post-commit')
_MARKER = 'chore(version): bump to'

# Bash hook: locate the repo root (works from any CWD), then call the Python
# bumper. The bumper handles the actual version read/write + auto-commit.
_HOOK_SCRIPT = """#!/usr/bin/env bash
# Auto-bump AgentCascade patch version after each successful commit.
# Installed by scripts/install_version_hook.py — do not edit by hand.
set -u
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
BUMP="$REPO_ROOT/scripts/bump_version.py"
[ -f "$BUMP" ] || exit 0
# Resolve a Python 3 interpreter. Prefer python3; only fall back to `python` if it
# actually reports version 3 (guards against systems where `python` is Python 2).
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
    echo "bump_version: no python interpreter found (non-fatal)" >&2
    exit 0
fi
case "$("$PY" --version 2>&1)" in
    *"Python 3"*) : ;;   # Python 3 — OK (quoted portion is a literal substring match)
    *)
        echo "bump_version: no Python 3 available (found: $("$PY" --version 2>&1)) (non-fatal)" >&2
        exit 0
        ;;
esac
"$PY" "$BUMP" --post-commit || echo "bump_version: post-commit bump failed (non-fatal)" >&2
exit 0
"""


def main():
    """Install the post-commit version-bump hook (idempotent)."""
    if not os.path.isfile(os.path.join(_REPO_ROOT, '.git', 'HEAD')):
        print('ERROR: not a git repository (no .git/HEAD)', file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(_HOOK_PATH), exist_ok=True)
    with open(_HOOK_PATH, 'w', encoding='utf-8', newline='\n') as f:
        f.write(_HOOK_SCRIPT)
    # Make executable (matters on Linux/macOS; harmless on Windows).
    st = os.stat(_HOOK_PATH)
    os.chmod(_HOOK_PATH, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    print(f"post-commit version hook installed at: {os.path.relpath(_HOOK_PATH, _REPO_ROOT)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
