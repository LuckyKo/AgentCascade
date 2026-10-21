"""BUG_0108: a relative new-file write must land in base_dir when base_dir can host it.

Root cause: ``_resolve_path`` (path_security.py) guarded the extra-work-folder fallback
with ``if not resolved.exists():``. For a *new* file the full path never exists, so that
guard was always true and the code fell through to the BUG_0031 parent-exists heuristic —
which preferred an extra RW mount whenever its parent dir existed, even when base_dir
itself could host the path. With both trees holding a ``reports/`` dir, a relative
``write_file("reports/x.md")`` landed in the extra mount instead of the workspace.

Fix: gate the fallback on base_dir being *unable* to host the path —
``if not resolved.exists() and not resolved.parent.exists():``. base_dir wins whenever
its parent dir exists; the BUG_0031 "prefer the active extra repo" heuristic is preserved
only for the case where base_dir has no such parent.

Uses real temporary directories so Path.exists() behaves naturally (same approach as
test_bug0031_relative_write_resolution.py).
"""
from pathlib import Path


class FakePathSecurity:
    """Minimal fake that binds the REAL _resolve_path from PathSecurityMixin."""

    def __init__(self, base_dir, extra_rw=None, extra_ro=None):
        from agent_cascade.operation_manager.path_security import PathSecurityMixin
        self._resolve_path = PathSecurityMixin._resolve_path.__get__(self)
        self.base_dir = Path(base_dir)
        self.extra_work_folders_rw = [Path(p) for p in (extra_rw or [])]
        self.extra_work_folders_ro = [Path(p) for p in (extra_ro or [])]
        self.agent_pool = None

    def _path_is_contained(self, path, root):
        try:
            Path(path).resolve().relative_to(Path(root).resolve())
            return True
        except ValueError:
            return False

    def _parse_extra_prefix(self, path, suffix, prefix, folders):
        raise NotImplementedError  # not exercised (no /extra_* virtual prefixes here)


class TestBug0108RelativeWritePrefersBaseDir:
    """Relative new-file resolution when base_dir can host the path."""

    def test_relative_new_file_prefers_base_dir_when_it_can_host(self, tmp_path):
        """The exact BUG_0108 repro: both trees have reports/ -> base_dir must win."""
        base = tmp_path / 'ws'
        (base / 'reports').mkdir(parents=True)
        extra = tmp_path / 'extra'
        (extra / 'reports').mkdir(parents=True)

        fake = FakePathSecurity(base_dir=str(base), extra_rw=[str(extra)])

        resolved = fake._resolve_path('reports/x.md', mode='rw')
        assert resolved == (base / 'reports' / 'x.md').resolve(), \
            f"must land in base_dir, got: {resolved}"
        assert not str(resolved).startswith(str(extra)), f"must NOT land in extra: {resolved}"

    def test_bare_relative_name_prefers_base_dir(self, tmp_path):
        """Latent same-class case: a bare new-file name lands in base_dir, not the
        first extra RW folder root (whose parent always exists)."""
        base = tmp_path / 'ws'
        base.mkdir(parents=True)
        extra = tmp_path / 'extra'
        extra.mkdir(parents=True)

        fake = FakePathSecurity(base_dir=str(base), extra_rw=[str(extra)])

        resolved = fake._resolve_path('notes.md', mode='rw')
        assert resolved == (base / 'notes.md').resolve(), f"bare name must land in base_dir, got: {resolved}"

    def test_parent_only_under_extra_still_resolves_to_extra(self, tmp_path):
        """BUG_0031 preserved: parent exists ONLY under the extra repo -> extra wins."""
        base = tmp_path / 'ws'
        base.mkdir(parents=True)
        extra = tmp_path / 'extra'
        (extra / 'only_here').mkdir(parents=True)

        fake = FakePathSecurity(base_dir=str(base), extra_rw=[str(extra)])

        resolved = fake._resolve_path('only_here/y.md', mode='rw')
        assert resolved == (extra / 'only_here' / 'y.md').resolve(), \
            f"BUG_0031 active-repo preference must be preserved, got: {resolved}"

    def test_relative_read_of_file_only_in_extra_still_resolves_to_extra(self, tmp_path):
        """Reads must be unaffected by the BUG_0108 write-path change.

        base has the parent dir but NOT the file; the file exists ONLY under the extra
        repo. A relative read (mode='ro') must still locate the file under extra — the
        base_dir-host preference is gated on mode='rw' and must NOT apply to reads.
        """
        base = tmp_path / 'ws'
        (base / 'subdir').mkdir(parents=True)  # parent dir present under base, but no file
        extra = tmp_path / 'extra'
        target = extra / 'subdir' / 'existing.txt'
        target.parent.mkdir(parents=True)
        target.write_text('hello', encoding='utf-8')

        fake = FakePathSecurity(base_dir=str(base), extra_rw=[str(extra)])

        resolved = fake._resolve_path('subdir/existing.txt', mode='ro')
        assert resolved == target.resolve(), \
            f"relative read must still find the file under extra, got: {resolved}"
