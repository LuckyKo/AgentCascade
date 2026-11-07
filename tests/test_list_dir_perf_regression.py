"""Regression net for the list_dir lazy-stat optimizations (Phases 1-3).

The audit (`plans/list_dir_perf_audit.md`) identified two root causes behind
"sometimes slow":

* **H1/H4** -- ``stat()`` is paid for every candidate before the name filter and
  before truncation, so a large flat directory or a filter-heavy recursive tree
  stat's tens of thousands of entries to render at most ``max_entries``.
* **H2** -- ``Path.resolve()`` runs per walked directory and is O(depth) syscalls
  per call, so deep trees pay a quadratic-ish resolve cost.

Fixes (O1/O4 lazy-stat, O2 ino identity key) turn the syscall cost from
O(entries-in-tree) into O(max_entries + dirs-walked). This file pins that
behavior with an *executable invariant*: wrap ``os.stat`` / ``os.lstat`` /
``os.scandir`` with counters and assert the counts stay bounded. It is
deterministic (syscall counts, not wall-clock) and therefore CI-safe.

Counter wrapping is done at the ``os`` module level so it also catches
``Path.stat()`` (which routes through ``os.stat``) and ``os.walk``'s internal
``os.scandir``. ``DirEntry.stat()`` does NOT route through ``os.stat`` -- that
is why the FLAT-mode lazy win is proven via ``os.scandir`` (the deferred
per-directory empty-scan) rather than via ``os.stat``.
"""

import os
import re
import sys
import tempfile
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cascade.operation_manager import OperationManager

# Tree sizes: far above max_entries so the bounded-count invariant is
# discriminative. Building ~25k inodes once costs ~2s locally.
N_SUBDIRS = 5000
N_FILES = 20000
MAX_ENTRIES = 500


# ─── Tree fixtures (built ONCE per module) ────────────────────────────────

@pytest.fixture(scope='module')
def wide_tree():
    """A directory with N_SUBDIRS empty subdirs (for the flat empty-scan test)."""
    root = tempfile.mkdtemp(prefix='listdir_perf_wide_')
    for i in range(N_SUBDIRS):
        os.mkdir(os.path.join(root, f'd{i}'))
    yield Path(root)
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope='module')
def many_files_tree():
    """A single directory with N_FILES entries (for the recursive stat-count test)."""
    root = tempfile.mkdtemp(prefix='listdir_perf_files_')
    for i in range(N_FILES):
        open(os.path.join(root, f'f{i}'), 'wb').close()
    yield Path(root)
    shutil.rmtree(root, ignore_errors=True)


class _StatCounter:
    """Wrap os.stat / os.lstat / os.scandir at the module level and count calls.

    Wrapping the ``os`` module attributes -- rather than the ``file_operations``
    references -- is required: ``Path.stat()`` calls ``os.stat`` from inside
    ``pathlib`` and ``os.walk`` calls ``os.scandir`` from inside ``os``; both
    resolve the same module attribute, so patching there counts them.
    """

    def __init__(self):
        self.stat = 0
        self.lstat = 0
        self.scandir = 0

    def install(self, monkeypatch):
        real_stat, real_lstat, real_scandir = os.stat, os.lstat, os.scandir

        def _stat(*a, **k):
            self.stat += 1
            return real_stat(*a, **k)

        def _lstat(*a, **k):
            self.lstat += 1
            return real_lstat(*a, **k)

        def _scandir(*a, **k):
            self.scandir += 1
            return real_scandir(*a, **k)

        monkeypatch.setattr(os, 'stat', _stat, raising=False)
        monkeypatch.setattr(os, 'lstat', _lstat, raising=False)
        monkeypatch.setattr(os, 'scandir', _scandir, raising=False)

    def reset(self):
        self.stat = self.lstat = self.scandir = 0


# ─── Phase 1 (O4): flat mode defers per-dir empty-scan to survivors ────────

def test_flat_default_defers_empty_scan_to_survivors(wide_tree, monkeypatch):
    """Default flat listing must not open every subdir to check `(empty)`.

    With N_SUBDIRS (5000) subdirs but only MAX_ENTRIES (500) rendered, the old
    code ran ``os.scandir`` on every surviving subdir -> ~5000 calls. Lazy mode
    scans only the rendered dirs -> bounded by the render budget.
    """
    counter = _StatCounter()
    counter.install(monkeypatch)
    om = OperationManager(base_dir=str(wide_tree))
    counter.reset()

    out = om.list_directory('.', recursive=False,
                            max_entries=MAX_ENTRIES, char_limit=-1)

    # The old eager path scanned all N_SUBDIRS directories; the lazy path may
    # scan at most the rendered ones (+ the top readdir + a small slack).
    assert counter.scandir <= MAX_ENTRIES + 50, (
        f"flat listing opened {counter.scandir} dirs; expected <= "
        f"{MAX_ENTRIES + 50} (lazy empty-scan regressed toward O(dirs-in-tree))")
    assert counter.scandir < N_SUBDIRS // 2, (
        f"flat listing scanned {counter.scandir} dirs, still ~O(dirs-in-tree)")

    # Rendering is still correct: dirs sort before files, all these subdirs are
    # empty, and the overflow note reflects the truncated remainder.
    assert 'Directories:' in out, out[:400]
    assert out.count(' (empty)') == MAX_ENTRIES, out[:400]
    assert f'and {N_SUBDIRS - MAX_ENTRIES} more entries' in out, out[-400:]


def test_flat_size_sort_still_renders(wide_tree, monkeypatch):
    """sort_by='size' keeps the eager path; the listing must still render + truncate.

    Flat mode's eager path uses ``DirEntry.stat()`` (not routed through
    ``os.stat``), so the lazy-gate syscall bound cannot be asserted here. This
    only pins that disabling the lazy gate still produces correct output; the
    gate-disabled syscall bound is covered by
    test_recursive_size_sort_stats_every_candidate.
    """
    counter = _StatCounter()
    counter.install(monkeypatch)
    om = OperationManager(base_dir=str(wide_tree))
    counter.reset()

    out = om.list_directory('.', recursive=False, sort_by='size',
                            max_entries=MAX_ENTRIES, char_limit=-1)
    # Flat size-sort keeps the eager path (DirEntry.stat does not route through
    # os.stat), so this asserts only that the listing still renders + truncates.
    assert 'Directories:' in out, out[:400]
    assert f'and {N_SUBDIRS - MAX_ENTRIES} more entries' in out, out[-400:]


# ─── Phase 2 (O1): recursive defers file stat to need + render ─────────────

def test_recursive_default_stats_bounded_files(many_files_tree, monkeypatch):
    """Default recursive listing must not stat every file in the tree.

    Single directory with N_FILES (20000) files, no filters. The old code
    stat'd every candidate before truncating; the lazy path only stats entries
    that render.
    """
    counter = _StatCounter()
    counter.install(monkeypatch)
    om = OperationManager(base_dir=str(many_files_tree))
    counter.reset()

    out = om.list_directory('.', recursive=True,
                            max_entries=MAX_ENTRIES, char_limit=-1)

    # Bound is O(max_entries), not O(files-in-tree).
    assert counter.stat <= MAX_ENTRIES * 2, (
        f"recursive listing stat'd {counter.stat} files; expected <= "
        f"{MAX_ENTRIES * 2} (deferred-stat regressed toward O(files-in-tree))")
    assert counter.stat < N_FILES // 10, (
        f"recursive listing stat'd {counter.stat}, still ~O(files-in-tree)")

    assert 'output limited to' in out, out[-400:]


def test_recursive_name_filter_stats_only_survivors(many_files_tree, monkeypatch):
    """A name filter must not pay a stat per rejected file.

    This is H1's "sometimes" signature: scan+stat everything to find the few
    matches. include='f10*' matches 1111 of N_FILES; the lazy path filters on
    name first and stats only the survivors (capped by the render budget),
    never the 18889 rejected names.
    """
    counter = _StatCounter()
    counter.install(monkeypatch)
    om = OperationManager(base_dir=str(many_files_tree))
    counter.reset()

    out = om.list_directory('.', recursive=True, include='f10*',
                            max_entries=MAX_ENTRIES, char_limit=-1)

    # f10, f100..f109, f1000..f1099, f10000..f10999 -> 1111 matches.
    matching = sum(1 for i in range(N_FILES) if str(i).startswith('10'))
    assert matching == 1111, matching  # sanity: the pattern really is a small subset
    # Survivors render up to the budget; stats are bounded by the budget, not N_FILES.
    assert counter.stat <= MAX_ENTRIES * 2, (
        f"filtered recursive stat'd {counter.stat}; expected <= "
        f"{MAX_ENTRIES * 2} (stat-before-name-filter regressed)")
    assert counter.stat < matching, (
        f"filtered recursive stat'd {counter.stat} >= every match ({matching}); "
        'stat-before-filter still present')
    assert 'f10' in out, out[:400]


# ─── Phase 2 gate DISABLED: size/date sort still stats every candidate ──────

def test_recursive_size_sort_stats_every_candidate(monkeypatch):
    """sort_by='size' forces the eager path: every candidate is stat'd.

    Single dir, 400 files, include='keep_*' matches 150. Under name sort the
    lazy gate would stat ~150 survivors; under size sort the gate is disabled
    and all 400 are stat'd so they can be ordered by size.
    """
    root = Path(tempfile.mkdtemp(prefix='listdir_perf_size_'))
    try:
        for i in range(400):
            prefix = 'keep_' if i < 150 else 'drop_'
            # Distinct sizes: byte count = 1000 - i, so ordering is checkable.
            (root / f'{prefix}{i:03d}.bin').write_bytes(b'x' * (1000 - i))
        counter = _StatCounter()
        counter.install(monkeypatch)
        om = OperationManager(base_dir=str(root))
        counter.reset()

        out = om.list_directory('.', recursive=True, include='keep_*',
                                sort_by='size', char_limit=-1)

        # Gate disabled => all 400 files stat'd (250 rejected by name still paid
        # a stat, because they had to be stat'd to know their size exists).
        assert counter.stat >= 400, (
            f"size-sort recursive stat'd only {counter.stat}; expected >= 400 "
            "(lazy gate wrongly stayed enabled under sort_by='size')")

        # The 150 kept files render, ordered largest-first.
        rendered = _rendered_file_names(out)
        assert len(rendered) == 150, len(rendered)
        assert all(n.startswith('keep_') for n in rendered), rendered[:10]
        # keep_000 is largest (999 bytes), keep_149 smallest (850 bytes).
        assert rendered[0] == 'keep_000.bin', rendered[:5]
        assert rendered[-1] == 'keep_149.bin', rendered[-5:]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_recursive_date_sort_stats_every_candidate(monkeypatch):
    """sort_by='date' forces the eager path: every candidate is stat'd."""
    root = Path(tempfile.mkdtemp(prefix='listdir_perf_date_'))
    try:
        names = []
        # 300 files, 100 keep / 200 drop, mtimes spaced 1 day apart so the
        # expected ordering (most-recent first) is unambiguous.
        for i in range(300):
            prefix = 'keep_' if i < 100 else 'drop_'
            name = f'{prefix}{i:03d}.bin'
            p = root / name
            p.write_bytes(b'x')
            ts = 1_700_000_000 + i * 86_400  # increasing with i
            os.utime(p, (ts, ts))
        counter = _StatCounter()
        counter.install(monkeypatch)
        om = OperationManager(base_dir=str(root))
        counter.reset()

        out = om.list_directory('.', recursive=True, include='keep_*',
                                sort_by='date', char_limit=-1)

        assert counter.stat >= 300, (
            f"date-sort recursive stat'd only {counter.stat}; expected >= 300 "
            "(lazy gate wrongly stayed enabled under sort_by='date')")

        rendered = _rendered_file_names(out)
        assert len(rendered) == 100, len(rendered)
        # keep_099 has the highest mtime => rendered first; keep_000 last.
        assert rendered[0] == 'keep_099.bin', rendered[:5]
        assert rendered[-1] == 'keep_000.bin', rendered[-5:]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_flat_size_filter_disables_lazy_gate(monkeypatch):
    """A size filter forces the eager path in flat mode too (renders/filters right)."""
    root = Path(tempfile.mkdtemp(prefix='listdir_perf_szflt_'))
    try:
        for i in range(200):
            (root / f's{i:03d}.bin').write_bytes(b'x' * (i * 50))  # sizes 0..9950
        om = OperationManager(base_dir=str(root))

        out = om.list_directory('.', recursive=False, min_size='5000',
                                max_entries=MAX_ENTRIES, char_limit=-1)
        # Files with size >= 5000 are those with i*50 >= 5000 => i >= 100 => 100 files.
        rendered = _rendered_file_names(out)
        assert len(rendered) == 100, len(rendered)
        assert all(n.startswith('s') for n in rendered)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ─── Phase 3 (O2): deep tree returns; ino identity key blocks deep junction ──

def _fake_deep_walk(depth: int):
    """Synthetic os.walk yielding a `d/d/d/...` chain of the given depth."""
    def walk(root, topdown=True, onerror=None, followlinks=False):
        current = Path(root)
        for _ in range(depth):
            yield (str(current), ['d'], [])
            current = current / 'd'
        yield (str(current), [], ['deep.txt'])
    return walk


class _FakeStat:
    """Minimal stat_result stand-in for a synthetic (non-existent) walked dir.

    Carries every attribute the code reads from the patched ``os.stat``: the
    cycle guard wants ``st_dev``/``st_ino`` (to exercise the fast identity-key
    branch), and ``Path.stat()`` on a synthetic child dir also flows through the
    patched ``os.stat`` into ``_make_entry``, which reads ``st_size``/``st_mtime``.
    """

    def __init__(self, ino):
        self.st_dev = 0
        self.st_ino = ino
        self.st_file_attributes = 0  # not a junction/reparse point
        self.st_reparse_tag = 0      # ntpath.isjunction reads this
        self.st_mode = 0o40777
        self.st_size = 0
        self.st_mtime = 0.0


def test_deep_chain_returns_under_ino_key(tmp_path, monkeypatch):
    """1000-deep chain renders with the (st_dev, st_ino) identity key, no resolve().

    Synthetic walk (no real deep filesystem, which would exceed MAX_PATH). Real
    paths (base_dir, '.') keep real stat semantics; the non-existent synthetic
    walked dirs get a cheap fabricated identity so Phase 3 uses the fast
    (dev, ino) key instead of falling back to the O(depth) resolve(). The guard
    now keys off os.stat, so the fake patches os.stat (os.lstat stays real for
    junction detection). This is exactly the deep-tree win H2 pins, and it also
    proves no RecursionError. Pairs with BUG_0168.
    """
    monkeypatch.setattr(
        'agent_cascade.operation_manager.file_operations.os.walk',
        _fake_deep_walk(1000))

    real_stat = os.stat
    inos = {}

    def fake_stat(path, **kw):
        try:
            return real_stat(path, **kw)  # real dirs resolve normally
        except OSError:
            key = str(path)
            if key not in inos:
                inos[key] = len(inos) + 1
            return _FakeStat(inos[key])

    monkeypatch.setattr(
        'agent_cascade.operation_manager.file_operations.os.stat', fake_stat)

    om = OperationManager(base_dir=str(tmp_path))
    out = om.list_directory('.', recursive=True, max_entries=100000, char_limit=-1)

    assert 'Error listing directory' not in out, out[:500]
    assert out.count('[DIR]') >= 990, out.count('[DIR]')
    assert 'deep.txt' in out, out[-400:]


def _mklink_junction(link: Path, target: Path) -> bool:
    result = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(link), str(target)],
        capture_output=True, text=True)
    return result.returncode == 0


@pytest.mark.skipif(sys.platform != 'win32',
                    reason='NTFS junctions are Windows-only')
def test_deep_nested_outside_junction_blocked(tmp_path):
    """An out-of-workspace junction nested several levels deep is still blocked."""
    base = tmp_path / 'ws'
    (base / 'a' / 'b' / 'c').mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'SECRET_OUTSIDE.txt').write_text('secret', encoding='utf-8')
    link = base / 'a' / 'b' / 'c' / 'junction_out'
    if not _mklink_junction(link, outside):
        pytest.skip('mklink /J is unavailable in this environment')
    try:
        om = OperationManager(base_dir=str(base))
        out = om.list_directory('.', recursive=True, char_limit=-1)
        assert 'SECRET_OUTSIDE' not in out, out[:500]
        assert 'a/b/c/junction_out' in out, 'junction itself should still be visible'
        assert 'not traversed' in out, out[:500]
    finally:
        try:
            os.rmdir(link)
        except OSError:
            pass


# ─── helpers ──────────────────────────────────────────────────────────────

# A file line renders as "<indent><name> (<size>, modified: <date>)". Root-level
# recursive entries render with no indent; flat entries use two spaces. The
# distinguishing feature vs a dir line ("(empty)" or "(modified: ...)") is the
# size token followed by ", modified:" -- dir lines have no comma before it.
# \s* consumes the optional indent so group(1) is always the clean basename.
_FILE_LINE = re.compile(r'^\s*(.*?) \((?:[0-9.]+ [KMGT]?B), modified: .*\)$')


def _rendered_file_names(out: str):
    """Return the displayed names of rendered file lines, in output order."""
    return [_FILE_LINE.match(line).group(1)
            for line in out.splitlines() if _FILE_LINE.match(line)]
