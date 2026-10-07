"""
BUG_0062 — parity between the ripgrep fast path and the pure-Python fallback with
respect to VCS ignore files.

Before this fix, `om.grep(...)` returned a different file set depending on whether
`rg` happened to be installed: the fast path let ripgrep apply .gitignore/.ignore/
.rgignore, while the fallback pruned a hardcoded directory-name set and never read
an ignore file. These tests pin the two paths together.

Fixture rules that are load-bearing (see the plan's §2.1 and §5.1):
  * `git init` is REQUIRED. ripgrep honours `.gitignore` only inside a git work
    tree, so a fixture without it silently tests nothing.
  * The DEFAULT_IGNORED_DIRS union members are written into `.gitignore` on purpose.
    The fast path prunes those dirs ONLY via an ignore rule; the fallback prunes
    them via the hardcoded set. Without the rules the two paths genuinely disagree
    and the exact-set parity assertion would be false.
  * The nested `.gitignore` is written with CRLF line endings (CRLF coverage for
    free, since pathspec handles CRLF natively but a BOM would corrupt rule #1).
"""
import os
import re
import shutil
import stat
import subprocess as _sp
import tempfile
import unittest.mock as mock
from pathlib import Path

NEEDLE = 'NEEDLE_TOKEN'

# Directory members of DEFAULT_IGNORED_DIRS that exist as real dirs in the fixture.
_DIR_MEMBERS = ('.venv', 'venv', 'dist', 'build', '.tox', 'node_modules', 'foo.egg-info')

# The exact set both paths must return for ignore_vcs=True.
EXPECTED_IGNORED_VCS = {'tracked.txt', 'dotignored2.txt', 'sub/ok.txt'}


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────


def _rmtree_force(path):
    """Remove a fixture tree. Git object files are read-only on Windows, so a plain
    shutil.rmtree leaves the directory behind — clear the read-only flag via onexc."""
    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)


def _write(path: Path, text: str, *, newline: str = '\n', encoding: str = 'utf-8') -> None:
    """Write with explicit newline handling so the CRLF fixture is genuinely CRLF."""
    with open(path, 'w', encoding=encoding, newline=newline) as fh:
        fh.write(text)


def _make_fixture(root: Path, *, git_init: bool) -> None:
    """Build the parity fixture. `git_init=False` is the negative control fixture for
    the §2.1 three-way rule: `.gitignore` inert, `.ignore` still live."""
    # -- ignore files -------------------------------------------------------------
    # The DEFAULT_IGNORED_DIRS union must appear here or fast/fallback disagree on
    # those directories (fast prunes them only via a rule; fallback via the hardcoded set).
    _write(root / '.gitignore',
           'ignored.log\n'
           + ''.join(f'{d}/\n' for d in _DIR_MEMBERS)
           + '\n')
    _write(root / '.ignore', 'dotignored.txt\n')

    # -- plain files --------------------------------------------------------------
    (root / 'tracked.txt').write_text(f'{NEEDLE}_A\n', encoding='utf-8')
    (root / 'ignored.log').write_text(f'{NEEDLE}_B\n', encoding='utf-8')
    (root / 'dotignored.txt').write_text(f'{NEEDLE}_C\n', encoding='utf-8')
    (root / 'dotignored2.txt').write_text(f'{NEEDLE}_D\n', encoding='utf-8')

    # -- DEFAULT_IGNORED_DIRS members, each holding a match ----------------------
    for d in _DIR_MEMBERS:
        dd = root / d
        dd.mkdir(exist_ok=True)
        (dd / 'x.txt').write_text(f'{NEEDLE}_DIR\n', encoding='utf-8')

    # -- nested .gitignore, deliberately CRLF -----------------------------------
    sub = root / 'sub'
    sub.mkdir()
    _write(sub / '.gitignore', 'nested.txt\n', newline='\r\n')
    (sub / 'nested.txt').write_text(f'{NEEDLE}_E\n', encoding='utf-8')
    (sub / 'ok.txt').write_text(f'{NEEDLE}_F\n', encoding='utf-8')

    if git_init:
        _sp.run(['git', 'init', '-q'], cwd=str(root), check=True, stdin=_sp.DEVNULL)
        _sp.run(['git', 'add', '.gitignore', '.ignore', 'tracked.txt', 'dotignored2.txt'],
                cwd=str(root), check=True, stdin=_sp.DEVNULL)
        _sp.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t',
                 'commit', '-q', '-m', 'init'], cwd=str(root), check=True, stdin=_sp.DEVNULL)


def _files_in(output: str) -> set:
    """Extract the searched file set from grep output.

    Reuses the format contract that tests/scripts/grep_compare.py:165-180 parses:
    per-line `path:line: text`, with the summary line skipped. The summary may carry
    the BUG_0061 advisory note as a suffix, so 'Found'/'No matches' lines are dropped
    rather than regex-matched.
    """
    out = set()
    for line in output.split('\n'):
        if line.startswith('Found ') or not line.strip():
            continue
        m = re.match(r'^(.+?):(\d+):\s*(.*)$', line)
        if m:
            out.add(m.group(1).replace('\\', '/'))
    return out


def _run_both(root: Path, *, ignore_vcs: bool):
    """Run the same grep on the fast path and on the Python fallback.

    The fallback is forced by patching _check_tool_availability, which is the single
    mechanism used here (no PATH manipulation — that is process-global and racy under
    pytest-xdist, which this suite runs under).
    """
    from agent_cascade.operation_manager import OperationManager
    from agent_cascade.operation_manager import grep as grep_mod

    om = OperationManager(base_dir=str(root))
    fast = om.grep(pattern=NEEDLE, path='.', ignore_vcs=ignore_vcs, char_limit=-1)

    with mock.patch.object(grep_mod, '_check_tool_availability', return_value=(False, False)):
        fallback = om.grep(pattern=NEEDLE, path='.', ignore_vcs=ignore_vcs, char_limit=-1)

    return fast, fallback


def _run_fallback_only(root: Path, *, ignore_vcs: bool) -> str:
    """Run grep with the subprocess fast path disabled (same single patch mechanism)."""
    from agent_cascade.operation_manager import OperationManager
    from agent_cascade.operation_manager import grep as grep_mod

    om = OperationManager(base_dir=str(root))
    with mock.patch.object(grep_mod, '_check_tool_availability', return_value=(False, False)):
        return om.grep(pattern=NEEDLE, path='.', ignore_vcs=ignore_vcs, char_limit=-1)


# ──────────────────────────────────────────────
#  Tests
# ──────────────────────────────────────────────


def test_parity_ignore_vcs_true():
    """THE BUG: with ignore_vcs=True the fast path and the fallback must return the
    identical file set, and that set must be exactly the non-ignored files.

    Pre-fix this failed: the fast path returned 1 file, the fallback returned 8.
    """
    root = Path(tempfile.mkdtemp(prefix='bug62_a_'))
    try:
        _make_fixture(root, git_init=True)
        fast, fallback = _run_both(root, ignore_vcs=True)

        fast_files = _files_in(fast)
        fallback_files = _files_in(fallback)

        assert fast_files == fallback_files, (
            f'fast/fallback divergence with ignore_vcs=True: '
            f'fast={sorted(fast_files)} fallback={sorted(fallback_files)}')
        assert fast_files == EXPECTED_IGNORED_VCS, (
            f'unexpected file set: {sorted(fast_files)} '
            f'(expected {sorted(EXPECTED_IGNORED_VCS)})')
    finally:
        _rmtree_force(str(root))


def test_parity_ignore_vcs_false():
    """ignore_vcs=False must stay a full escape hatch on BOTH paths — no regression
    guard for the other flag value."""
    root = Path(tempfile.mkdtemp(prefix='bug62_b_'))
    try:
        _make_fixture(root, git_init=True)
        fast, fallback = _run_both(root, ignore_vcs=False)

        fast_files = _files_in(fast)
        fallback_files = _files_in(fallback)

        assert fast_files == fallback_files, (
            f'fast/fallback divergence with ignore_vcs=False: '
            f'fast={sorted(fast_files)} fallback={sorted(fallback_files)}')
        # Every previously-pruned file must come back.
        for name in ('ignored.log', 'dotignored.txt', 'sub/nested.txt',
                     'dist/x.txt', 'foo.egg-info/x.txt'):
            assert name in fast_files, f'{name} must be searchable with ignore_vcs=False'
            assert name in fallback_files, \
                f'{name} must be searchable on the fallback with ignore_vcs=False'
    finally:
        _rmtree_force(str(root))


def test_nested_gitignore_honoured():
    """A .gitignore in a SUBDIRECTORY must prune on both paths.

    This is the only test that catches a broken per-directory cache: a resolver that
    cached only the root's rules (or cached per-file instead of per-dir) would still
    pass every other test here.
    """
    root = Path(tempfile.mkdtemp(prefix='bug62_c_'))
    try:
        _make_fixture(root, git_init=True)
        fast, fallback = _run_both(root, ignore_vcs=True)

        for label, out in (('fast', fast), ('fallback', fallback)):
            files = _files_in(out)
            assert 'sub/nested.txt' not in files, \
                f'{label}: nested .gitignore (CRLF) not honoured: {sorted(files)}'
            assert 'sub/ok.txt' in files, \
                f'{label}: nested .gitignore over-pruned: {sorted(files)}'
    finally:
        _rmtree_force(str(root))


def test_no_git_repo_gating():
    """§2.1 three-way rule, with a negative control.

    WITHOUT `git init`: `.gitignore` (root AND nested) must be INERT, but `.ignore`
    must still be honoured. A resolver that gates all three ignore files on "is a
    repo" fails this; so does one that ignores `.gitignore` entirely (the negative
    control is what makes this test able to tell those two apart).
    """
    root = Path(tempfile.mkdtemp(prefix='bug62_d_'))
    try:
        _make_fixture(root, git_init=False)
        assert not (root / '.git').exists(), 'negative control: fixture must not be a repo'

        fast, fallback = _run_both(root, ignore_vcs=True)

        for label, out in (('fast', fast), ('fallback', fallback)):
            files = _files_in(out)
            # .ignore is honoured unconditionally -> dotignored.txt is pruned.
            assert 'dotignored.txt' not in files, \
                f'{label}: .ignore must be honoured outside a git repo: {sorted(files)}'
            # .gitignore is repo-gated -> ignored.log comes back.
            assert 'ignored.log' in files, \
                f'{label}: .gitignore must be inert outside a git repo: {sorted(files)}'
            # ...and so must a NESTED .gitignore.
            assert 'sub/nested.txt' in files, \
                f'{label}: nested .gitignore must be inert outside a repo: {sorted(files)}'

        # The two paths must still agree on the IGNORE-FILE layer.
        # KNOWN RESIDUAL (pre-existing, out of BUG_0062 scope): in a non-repo tree rg prunes
        # ONLY .git/, while the fallback has always pruned its hardcoded DEFAULT_IGNORED_DIRS
        # set unconditionally — the old `skip_dirs` prune was equally unconditional on repo
        # state. So blanket fast==fallback equality is NOT achievable here without deleting
        # the fallback's long-standing node_modules protection, which §7A explicitly keeps.
        # Compare the two sets with those directory members factored out.
        def _without_dir_members(files):
            return {f for f in files if f.split('/')[0] not in _DIR_MEMBERS}

        assert _without_dir_members(_files_in(fast)) == _without_dir_members(_files_in(fallback)), \
            f'no-repo ignore-layer divergence: fast={sorted(_files_in(fast))} ' \
            f'fallback={sorted(_files_in(fallback))}'
    finally:
        _rmtree_force(str(root))


def test_fallback_without_pathspec_degrades():
    """A missing optional dependency must degrade to DEFAULT_IGNORED_DIRS-only, never
    raise. The test environment itself must have pathspec, or this test is vacuous."""
    import pathspec  # noqa: F401  — assertion: the dep must be installed to test

    from agent_cascade.operation_manager import grep as grep_mod
    from agent_cascade.operation_manager import grep_ignore as gi

    root = Path(tempfile.mkdtemp(prefix='bug62_e_'))
    try:
        _make_fixture(root, git_init=True)

        assert gi.GitIgnoreSpec is not None, 'pathspec must be importable in the test env'

        # Simulate the guarded-import degradation: GitIgnoreSpec is None.
        with mock.patch.object(gi, 'GitIgnoreSpec', None):
            out = _run_fallback_only(root, ignore_vcs=True)

        files = _files_in(out)
        assert 'ERROR' not in out, f'fallback must not raise without pathspec: {out[:300]}'
        # Hardcoded dir set still prunes the DEFAULT_IGNORED_DIRS members ...
        for d in _DIR_MEMBERS:
            assert f'{d}/x.txt' not in files, \
                f'degraded fallback must still prune {d}/ via DEFAULT_IGNORED_DIRS'
        # ...but ignore-file rules are no longer applied, so previously-pruned files return.
        assert 'ignored.log' in files, \
            f'degraded fallback should not honour .gitignore: {sorted(files)}'
        assert 'dotignored.txt' in files, \
            f'degraded fallback should not honour .ignore: {sorted(files)}'
    finally:
        _rmtree_force(str(root))
