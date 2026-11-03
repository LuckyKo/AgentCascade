"""Give grep's pure-Python fallback ripgrep's ignore-file semantics (BUG_0062).

Deliberately NOT modelled (do not "fix" these): ``.git/info/exclude`` and the global
``core.excludesFile`` are out of scope; user-supplied ``exclude`` patterns are handled
separately by grep.py and are not VCS ignore rules; and git's "cannot re-include a file
whose parent directory is excluded" rule is left to ``pathspec`` (pure-Python, 0BSD) rather
than hand-rolled globbing, because a bounded matcher that silently disagrees with rg on
re-inclusion would re-introduce exactly this bug.
"""

import fnmatch
import subprocess
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

# F2 (BUG_0202/RC-A): wall-clock cap on a `git ls-files` index query. A hung git (locked
# index, huge repo, slow disk) must not stall the fallback — past this we degrade to
# pattern-only semantics rather than blocking the search.
_TRACKED_QUERY_TIMEOUT_S = 5.0


class IgnoreResolver:
    """Answers "is this path ignored?" the way ripgrep would.

    Args:
        root: Search root. All matching is done relative to this directory.
    """

    def __init__(self, root: Path):
        self._root = root
        # F1 (BUG_0202/RC-C): repo detection is PER DIRECTORY, not a one-shot verdict from
        # the search root. A nested repo's .gitignore is honoured for the directories inside
        # it even when the search root is not a repo — matching ripgrep, which resolves each
        # path against its nearest .git. Memoised per directory so the walk-up runs once.
        self._repo_root_cache: dict = {}
        # F2 (BUG_0202/RC-A): cached `git ls-files` results, keyed by repo_root ONLY — the
        # whole-repo index is queried once per repo (NOT per directory), so the walk stays an
        # O(1) set lookup after the first query. See _tracked_set_for.
        self._tracked_cache: dict = {}
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
        # component so `dist/x.txt` is pruned via its `dist` ancestor. NOTE (pre-existing
        # divergence, accepted): this returns True BEFORE the F2 git-index check, so a tracked
        # file under dist/build/node_modules stays invisible in the fallback while rg (which
        # honours the index) would search it. Kept as-is — those dirs are rarely tracked.
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
        # F2 (BUG_0202/RC-A): git exempts TRACKED files from ignore rules. A pattern-only
        # verdict of "ignored" is overturned when the path is a member of the repo's index —
        # that is git's own semantics (an ignored-but-tracked file is NOT ignored). This is
        # what makes `agents/global/skills/**` (tracked) searchable under a `.gitignore`
        # rule that would otherwise prune it. Degrades to pattern-only when git is absent
        # or the query fails (see is_git_tracked).
        if opinion and self.is_git_tracked(path, is_dir=is_dir):
            return False
        return opinion

    def is_git_tracked(self, path: Path, *, is_dir: bool = False) -> bool:
        """F2 (BUG_0202/RC-A): True if `path` is a member of the nearest repo's git index.

        Git's rule is that ignore patterns do not apply to tracked files, so a path a pattern
        would prune is still searchable when it is tracked. The index is queried ONCE per repo
        root (``git ls-files -z``, whole-repo) and cached; membership is then an O(1) set
        lookup, so the fallback walk stays fast even over a tree with hundreds of ignored
        directories. A missing git binary or a failed query degrades to False (pattern-only).
        """
        matched_dir = path if is_dir else path.parent
        repo_root = self._repo_root_for(matched_dir)
        if repo_root is None:
            return False
        tracked_files, tracked_dirs = self._tracked_set_for(repo_root)
        if tracked_files is None:
            return False  # git absent / query failed → degrade to pattern-only
        # `relative_to` raises ValueError when the path is not under the repo root (a symlink
        # junction or a `..` component that escapes it). That is outside the index's scope, so
        # answer False (pattern-only) rather than let it escape this documented best-effort path.
        target = matched_dir if is_dir else path
        try:
            rel = target.relative_to(repo_root).as_posix()
        except ValueError:
            return False
        # A directory is "tracked" when it holds at least one tracked file (it appears in the
        # ancestor-dir set); pruning it would hide those files, so it must stay searchable.
        return rel in (tracked_dirs if is_dir else tracked_files)

    def _tracked_set_for(self, repo_root: Path) -> tuple:
        """Cached ``(tracked_files, tracked_dirs)`` for the whole repo, or ``(None, None)`` on failure.

        ``tracked_files`` is the set of tracked file paths (relative, forward-slash);
        ``tracked_dirs`` is the set of every ancestor directory of a tracked file, so a directory
        membership test is an O(1) lookup. Runs ONCE per repo root (NOT per directory) — a
        per-directory ``git ls-files`` would spawn one subprocess per ignored dir and stall the
        fallback walk (the BUG_0202 perf regression this whole-repo query avoids). A failure (git
        absent, locked index, timeout) is cached as ``(None, None)`` too, so a missing or hung git
        does not re-spawn a 5 s query on every call for the rest of the search.
        """
        cached = self._tracked_cache.get(repo_root)
        if cached is not None:
            return cached
        cmd = ['git', 'ls-files', '-z']
        try:
            result = subprocess.run(
                cmd, cwd=str(repo_root), capture_output=True, text=True,
                encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL,
                timeout=_TRACKED_QUERY_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            self._tracked_cache[repo_root] = (None, None)
            return None, None
        if result.returncode != 0:
            self._tracked_cache[repo_root] = (None, None)
            return None, None
        # -z is NUL-separated; normalise separators to '/' so Windows paths parse identically.
        files = {p.replace('\\', '/') for p in result.stdout.split('\0') if p}
        dirs: set = set()
        for p in files:
            parts = p.split('/')
            for i in range(1, len(parts)):
                dirs.add('/'.join(parts[:i]))
        pair = (files, dirs)
        self._tracked_cache[repo_root] = pair
        return pair

    def _repo_root_for(self, directory: Path) -> Optional[Path]:
        """F1 (BUG_0202/RC-C): the nearest git repo root for `directory`, or None.

        Memoised per directory (a None verdict is cached too, so a non-repo directory does not
        re-walk its ancestors on every call); reuses the file-or-dir `.git` test (a `git
        worktree` / submodule writes a `.git` *file*, not a directory).
        """
        if directory in self._repo_root_cache:
            return self._repo_root_cache[directory]
        root = _find_repo_root(directory)
        self._repo_root_cache[directory] = root
        return root

    def _ignore_specs_for_dir(self, directory: Path) -> list:
        """Parsed ignore specs for one directory, in increasing precedence order."""
        specs = self._cache.get(directory)
        if specs is not None:
            return specs

        specs = []
        if GitIgnoreSpec is not None:
            # F1 (BUG_0202/RC-C): repo-gating is per-directory (the nearest .git governs), not a
            # one-shot verdict from the search root — so a nested repo's .gitignore is honoured
            # for its directories even under a non-repo search root. .ignore/.rgignore stay
            # unconditional (pinned by test_no_git_repo_gating).
            in_repo = self._repo_root_for(directory) is not None
            for name, repo_gated in _IGNORE_FILES:
                if repo_gated and not in_repo:
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


def _find_repo_root(root: Path) -> Optional[Path]:
    """The nearest git work-tree root for `root` (walking up), or None.

    F1 (BUG_0202/RC-C): returns the repo root so callers can resolve repo-gated rules per
    directory. Looks for a `.git` entry that is either a directory (normal clone) or a file
    (worktree / submodule), since `git worktree add` writes a `.git` *file*.
    """
    try:
        current = root.resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / '.git').exists():
            return candidate
    return None
