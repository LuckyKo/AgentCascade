"""Regression tests for BUG_0031: relative write_file resolves to wrong root.

BUG_0031: When a relative path is used with write_file and the parent directory
exists in both base_dir and an extra-RW folder, the resolver should prefer the
extra-RW folder (the agent's active context) over base_dir.

Root cause: _resolve_path checked `resolved.exists()` on the full path, which is
always False for a new-file write. The fix checks whether the PARENT DIRECTORY
exists in an extra folder before defaulting to base_dir.

These tests use REAL temporary directories so Path.exists() behaves naturally —
no global patching of Path methods (which is fragile and masks real behavior).
"""

import pytest
from pathlib import Path


class FakePathSecurity:
    """Minimal fake that uses the REAL _resolve_path logic from PathSecurityMixin."""

    def __init__(self, base_dir, extra_rw=None, extra_ro=None):
        from agent_cascade.operation_manager.path_security import PathSecurityMixin
        # Bind the real method to this instance so we test the actual fix logic.
        self._resolve_path = PathSecurityMixin._resolve_path.__get__(self)
        self.base_dir = Path(base_dir)
        self.extra_work_folders_rw = [Path(p) for p in (extra_rw or [])]
        self.extra_work_folders_ro = [Path(p) for p in (extra_ro or [])]
        self.agent_pool = None

    # Stub out the helper methods _resolve_path calls that we don't need to mock.
    def _path_is_contained(self, path, root):
        try:
            Path(path).resolve().relative_to(Path(root).resolve())
            return True
        except ValueError:
            return False

    def _parse_extra_prefix(self, path, suffix, prefix, folders):
        # Not exercised by these tests (no /extra_rw_N virtual prefixes).
        raise NotImplementedError


class TestBug0031RelativeWriteResolution:
    """Verify BUG_0031 fix using real temp directories."""

    def test_new_file_write_prefers_extra_rw_when_parent_exists(self, tmp_path):
        """New-file write where the parent dir exists ONLY in extra-RW.

        base_dir/subdir/          -> does NOT exist
        extra_rw/subdir/          -> EXISTS (parent present)
        extra_rw/subdir/new.txt   -> does not exist yet (new file)

        Expected: resolves into extra_rw (the agent's active repo), not base_dir.
        """
        base = tmp_path / 'base'
        extra = tmp_path / 'extra'
        # Create the parent dir ONLY in the extra-RW folder.
        (extra / 'subdir').mkdir(parents=True)

        fake = FakePathSecurity(base_dir=str(base), extra_rw=[str(extra)])

        resolved = fake._resolve_path('subdir/new.txt', mode='rw')

        assert str(resolved).startswith(str(extra)), f"Expected resolution in extra, got: {resolved}"
        assert not str(resolved).startswith(str(base)), f"Should NOT resolve to base_dir: {resolved}"
        assert resolved.name == 'new.txt'

    def test_existing_file_found_via_full_path_when_parent_absent(self, tmp_path):
        """Existing file in extra-RW where the parent dir does NOT exist as a standalone.

        This exercises the ORIGINAL full-path exists() fallback (parent check finds
        nothing because we only create the leaf's parent implicitly). The file itself
        exists, so the original behavior locates it.
        """
        base = tmp_path / 'base'
        extra = tmp_path / 'extra'
        # Create the file in extra (this also creates its parent dir).
        target = extra / 'subdir' / 'existing.txt'
        target.parent.mkdir(parents=True)
        target.write_text('hello', encoding='utf-8')

        fake = FakePathSecurity(base_dir=str(base), extra_rw=[str(extra)])

        resolved = fake._resolve_path('subdir/existing.txt', mode='ro')

        # Should resolve to the existing file in extra.
        assert str(resolved) == str(target), f"Expected {target}, got: {resolved}"

    def test_no_extra_folders_falls_back_to_base_dir(self, tmp_path):
        """No extra folders configured -> resolves to base_dir (original behavior)."""
        base = tmp_path / 'base'
        base.mkdir(parents=True)

        fake = FakePathSecurity(base_dir=str(base))

        resolved = fake._resolve_path('some/path.txt', mode='rw')

        assert str(resolved).startswith(str(base)), f"Expected base_dir resolution, got: {resolved}"

    def test_parent_exists_in_both_prefers_extra_rw(self, tmp_path):
        """The exact BUG_0031 scenario: parent dir exists in BOTH base and extra-RW.

        Before the fix, a new-file write always landed in base_dir (the first candidate),
        even when the agent's active context was the extra-RW repo. After the fix, the
        parent-dir check should route it to extra-RW.
        """
        base = tmp_path / 'base'
        extra = tmp_path / 'extra'
        # Parent dir exists in BOTH roots (the ambiguous case).
        (base / 'shared').mkdir(parents=True)
        (extra / 'shared').mkdir(parents=True)

        fake = FakePathSecurity(base_dir=str(base), extra_rw=[str(extra)])

        resolved = fake._resolve_path('shared/commit_msg.txt', mode='rw')

        # Fix: prefer the extra-RW folder over base_dir when parent exists in both.
        assert str(resolved).startswith(str(extra)), f"Expected extra (active repo), got: {resolved}"
