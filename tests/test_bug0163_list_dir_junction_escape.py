"""BUG_0163: recursive list_dir must not follow junctions out of the workspace.

``os.walk(followlinks=False)`` refuses to follow symlinks, but a Windows junction is not
reported as a symlink (``os.path.islink`` is False), so the walk descended into it. A
junction created inside the workspace could therefore expose any directory on the machine
in a recursive listing (names, sizes, mtimes), bypassing ``_resolve_path`` containment,
which only validates the path the caller passed.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cascade.operation_manager import OperationManager

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='NTFS junctions are Windows-only')


def _mklink_junction(link: Path, target: Path) -> bool:
    result = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@pytest.fixture
def junction(tmp_path):
    """Create junctions and remove them (os.rmdir) before pytest cleans the tree.

    Removing the reparse point explicitly matters: rmtree on a junction can be
    ambiguous, and the target may live outside the directory being cleaned.
    """
    created = []

    def _make(link: Path, target: Path) -> Path:
        target.mkdir(parents=True, exist_ok=True)
        if not _mklink_junction(link, target):
            pytest.skip('mklink /J is unavailable in this environment')
        created.append(link)
        return link

    yield _make

    for link in created:
        try:
            os.rmdir(link)
        except OSError:
            pass


def test_recursive_listing_does_not_follow_outside_junction(tmp_path, junction):
    base = tmp_path / 'ws'
    base.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'SECRET_OUTSIDE_FILE.txt').write_text('secret', encoding='utf-8')
    junction(base / 'junction_out', outside)

    om = OperationManager(base_dir=str(base))
    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert 'SECRET_OUTSIDE_FILE' not in out, out
    assert 'junction_out' in out, 'the junction itself should still be visible'
    assert 'not traversed' in out, out


def test_recursive_files_only_does_not_follow_outside_junction(tmp_path, junction):
    base = tmp_path / 'ws'
    base.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'SECRET_OUTSIDE_FILE.txt').write_text('secret', encoding='utf-8')
    junction(base / 'junction_out', outside)

    om = OperationManager(base_dir=str(base))
    out = om.list_directory('.', recursive=True, files_only=True, char_limit=-1)

    assert 'SECRET_OUTSIDE_FILE' not in out, out
    # files_only hides [DIR] headers, so the omission must be reported explicitly.
    assert 'not traversed' in out, out
    assert 'junction_out' in out, out


def test_nested_outside_junction_is_blocked(tmp_path, junction):
    base = tmp_path / 'ws'
    (base / 'sub').mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'SECRET_OUTSIDE_FILE.txt').write_text('secret', encoding='utf-8')
    junction(base / 'sub' / 'junction_out', outside)

    om = OperationManager(base_dir=str(base))
    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert 'SECRET_OUTSIDE_FILE' not in out, out
    assert 'sub/junction_out' in out, out
    assert 'not traversed' in out, out


def test_junction_within_workspace_is_still_traversed(tmp_path, junction):
    base = tmp_path / 'ws'
    (base / 'real').mkdir(parents=True)
    (base / 'real' / 'inside.txt').write_text('ok', encoding='utf-8')
    junction(base / 'junction_in', base / 'real')

    om = OperationManager(base_dir=str(base))
    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert 'junction_in/inside.txt' in out, out
    assert 'not traversed' not in out, out


def test_junction_into_allowed_extra_folder_is_traversed(tmp_path, junction):
    base = tmp_path / 'ws'
    base.mkdir()
    shared = tmp_path / 'shared'
    shared.mkdir()
    (shared / 'allowed.txt').write_text('ok', encoding='utf-8')
    junction(base / 'shared_link', shared)

    om = OperationManager(base_dir=str(base))
    om.set_extra_work_folders([], [str(shared)])

    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert 'shared_link/allowed.txt' in out, out
    assert 'not traversed' not in out, out


def test_flat_listing_still_shows_the_junction_without_its_contents(tmp_path, junction):
    base = tmp_path / 'ws'
    base.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'SECRET_OUTSIDE_FILE.txt').write_text('secret', encoding='utf-8')
    junction(base / 'junction_out', outside)

    om = OperationManager(base_dir=str(base))
    out = om.list_directory('.', char_limit=-1)
    assert 'junction_out/' in out, out
    assert 'SECRET_OUTSIDE_FILE' not in out, out
