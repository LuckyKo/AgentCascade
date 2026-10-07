"""Give grep's pure-Python fallback ripgrep's ignore-file semantics (BUG_0062).

Deliberately NOT modelled (do not "fix" these): ``.git/info/exclude`` and the global
``core.excludesFile`` are out of scope; user-supplied ``exclude`` patterns are handled
separately by grep.py and are not VCS ignore rules; and git's "cannot re-include a file
whose parent directory is excluded" rule is left to ``pathspec`` (pure-Python, 0BSD) rather
than hand-rolled globbing, because a bounded matcher that silently disagrees with rg on
re-inclusion would re-introduce exactly this bug.
"""

import fnmatch
from pathlib import Path
from typing import Optional

try:
    from pathspec import GitIgnoreSpec
except ImportError:  # pragma: no cover - optional dependency, degrades silently
    GitIgnoreSpec = None

# Directory names pruned unconditionally by the Python fallback and approximated on
# the GNU-grep path. This is the UNION of the two sets that used to live as separate
# literals in grep.py (the fallback's 8-entry skip_dirs and GNU grep's 4-entry
# _GREP_DEFAULT_EXCLUDE_DIRS) — stated ONCE here and imported by both. Entries are
# matched with fnmatch, so '*.egg-info' behaves as a glob.
DEFAULT_IGNORED_DIRS: frozenset = frozenset({
    '.git',
    'node_modules',
    '__pycache__',
    '*.egg-info',
    '.venv',
    'venv',
    'dist',
    'build',
    '.tox',
})

# Ignore files honoured per directory, in increasing order of precedence.
# `.gitignore` is repo-gated (ripgrep only honours it inside a git work tree);
# `.ignore` and `.rgignore` are unconditional. See BUG_0062 §2.1 — gating all
# three on "is a repo" is wrong and is pinned by test_no_git_repo_gating.
_IGNORE_FILES = (('.gitignore', True), ('.ignore', False), ('.rgignore', False))

_BOM = '\ufeff'


class IgnoreResolver:
    """Answers "is this path ignored?" the way ripgrep would.

    Args:
        root: Search root. All matching is done relative to this directory.
    """

    def __init__(self, root: Path):
        self._root = root
        self._in_repo = _is_inside_git_repo(root)
        # WHY a cache: is_ignored() is called once per file *and* once per directory
        # during the walk. Without it, every ancestor ignore file is re-read and
        # re-parsed for every candidate file — O(files × rules) I/O on the already
        # slowest grep path. A plain dict is enough: the resolver is built per
        # grep() call and is never shared across threads.
        self._cache: dict = {}

    def is_ignored(self, path: Path, *, is_dir: bool) -> bool:
        """Return True if `path` should be pruned.

        Args:
            path: Absolute path to test.
            is_dir: True when testing a directory. Directory rules (`logs/`) only
                match when the tested path carries directory semantics.
        """
        try:
            rel_parts = path.relative_to(self._root).parts
        except ValueError:
            # Outside the search root, so no ignore file's scope can be computed for it.
            # Answer "no" rather than guessing from the bare name: evaluating ROOT-level
            # rules against a basename would produce a verdict for a path they do not
            # govern. Callers (grep.py) skip out-of-root paths themselves; the os.walk
            # prune site cannot reach here.
            return False

        # Lowest precedence: the hardcoded directory set. Checked against every
        # component so `dist/x.txt` is pruned via its `dist` ancestor.
        for part in rel_parts:
            if any(fnmatch.fnmatch(part, pat) for pat in DEFAULT_IGNORED_DIRS):
                return True

        if GitIgnoreSpec is None:
            # Degrade to DEFAULT_IGNORED_DIRS-only rather than failing the search.
            return False

        # Walk ancestor directories from the root down to the path's immediate parent;
        # deeper wins, matching git's "last match wins" for nested ignore files.
        # The trailing-slash candidate makes a directory rule (`logs/`) match when the
        # tested path is a directory — GitWildMatchPattern distinguishes `dist` from `dist/`.
        opinion = False  # default; overridden by any later check_file() verdict that is not None
        for depth in range(len(rel_parts)):
            directory = self._root.joinpath(*rel_parts[:depth])
            # Rules in any directory's ignore file are relative to that directory.
            rel_from_base = '/'.join(rel_parts[depth:])
            candidates = ([rel_from_base, rel_from_base + '/'] if is_dir
                          else [rel_from_base])
            for spec in self._ignore_specs_for_dir(directory):
                for cand in candidates:
                    result = spec.check_file(cand)
                    if result.include is not None:
                        opinion = result.include
        return opinion

    def _ignore_specs_for_dir(self, directory: Path) -> list:
        """Parsed ignore specs for one directory, in increasing precedence order."""
        specs = self._cache.get(directory)
        if specs is not None:
            return specs

        specs = []
        if GitIgnoreSpec is not None:
            for name, repo_gated in _IGNORE_FILES:
                if repo_gated and not self._in_repo:
                    continue
                spec = self._load_spec(directory / name)
                if spec is not None:
                    specs.append(spec)
        # setdefault rather than plain assignment: the miss path must not clobber a
        # value another call already stored for the same directory.
        return self._cache.setdefault(directory, specs)

    @staticmethod
    def _load_spec(path: Path) -> Optional['GitIgnoreSpec']:
        """Parse one ignore file into a spec, or None if absent/unreadable/empty."""
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return None
        # A UTF-8 BOM would otherwise corrupt the first rule; pathspec already
        # handles CRLF line endings natively.
        lines = text.lstrip(_BOM).splitlines()
        if not any(line.strip() for line in lines):
            return None
        return GitIgnoreSpec.from_lines(lines)


def _is_inside_git_repo(root: Path) -> bool:
    """True if `root` sits inside a git work tree (walking up, one time).

    Looks for a `.git` entry that is either a directory (normal clone) or a file
    (worktree / submodule), since `git worktree add` writes a `.git` *file*.
    """
    try:
        current = root.resolve()
    except OSError:
        return False
    for candidate in (current, *current.parents):
        if (candidate / '.git').exists():
            return True
    return False
