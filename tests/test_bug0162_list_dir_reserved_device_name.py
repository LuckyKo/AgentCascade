"""BUG_0162: list_dir must not abort on entries named like Windows devices.

Root cause: the recursive listing computed each entry's path relative to the listed
root with ``os.path.relpath``. On Windows ``ntpath.abspath`` rewrites a path whose
final component is a reserved device name (``nul``, ``con``, ``aux``, ``prn``,
``com1``…, case-insensitive) into the device form ``\\\\.\\nul``, so relpath's drive
comparison fails and raises::

    ValueError: path is on mount '\\\\.\\nul', start on mount 'N:'

A real file named ``nul`` can exist on a network share (or be created through a
``\\\\?\\`` path), and it made every recursive ``list_dir`` of its parent directory
fail with that message — including plain recursive listings of a workspace root and
the BUG_0052 repro (root + ``recursive=True`` + ``include``).

Fix: ``_safe_relpath`` uses ``PurePath.relative_to`` (pure string arithmetic, no
Win32 abspath call) with a basename fallback, and all three relpath call sites use
it. Non-recursive listings were unaffected (they never call relpath).
"""

import os
from pathlib import Path

import pytest

from agent_cascade.operation_manager import OperationManager
from agent_cascade.operation_manager.file_operations import _safe_relpath


def test_safe_relpath_handles_reserved_device_names():
    """Reserved-name entries get a normal relative path instead of a ValueError."""
    root = os.path.join('work', 'repo')
    for name in ('nul', 'NUL', 'con', 'aux', 'prn', 'com1', 'lpt9'):
        entry = os.path.join(root, name)
        if os.name == 'nt':
            # Guard the regression itself: os.path.relpath is what used to blow up.
            with pytest.raises(ValueError, match='is on mount'):
                os.path.relpath(entry, root)
        assert _safe_relpath(entry, root) == name


def test_safe_relpath_root_returns_empty_string():
    root = os.path.join('work', 'repo')
    assert _safe_relpath(root, root) == ''


def test_safe_relpath_non_descendant_falls_back_to_basename():
    assert _safe_relpath(os.path.join('other', 'file.txt'), os.path.join('work', 'repo')) == 'file.txt'


def _create_file_named_nul(directory: Path) -> str:
    """Create a real file literally named ``nul`` via the ``\\\\?\\`` prefix.

    Normal Win32 paths resolve ``nul`` to the null device, so the extended-length
    prefix is required. Returns the prefixed path for later cleanup.
    """
    extended = '\\\\?\\' + str(directory / 'nul')
    with open(extended, 'w', encoding='utf-8') as f:
        f.write('x')
    return extended


@pytest.fixture
def workspace_with_nul_file(tmp_path):
    """Temp workspace containing a normal file tree plus an entry named ``nul``."""
    if os.name != 'nt':
        pytest.skip('Windows reserved device name; cannot be created on this platform')
    extended_nul = _create_file_named_nul(tmp_path)
    (tmp_path / 'root.md').write_text('root', encoding='utf-8')
    sub = tmp_path / 'sub'
    sub.mkdir()
    (sub / 'nested.md').write_text('nested', encoding='utf-8')
    (tmp_path / 'notes.txt').write_text('notes', encoding='utf-8')
    try:
        yield tmp_path
    finally:
        # The parent rmtree cannot unlink a reserved-name child through Win32
        # paths, so remove it explicitly before pytest cleans tmp_path.
        try:
            os.unlink(extended_nul)
        except OSError:
            pass


def test_recursive_listing_on_root_survives_nul_entry(workspace_with_nul_file):
    """Recursive listing of the root lists everything instead of erroring out."""
    om = OperationManager(base_dir=str(workspace_with_nul_file))

    out = om.list_directory('.', recursive=True, char_limit=-1, max_entries=1000)

    assert 'Error listing directory' not in out, out
    assert 'root.md' in out, out
    assert 'nested.md' in out, out
    assert 'nul' in out, out


def test_recursive_include_on_root_survives_nul_entry(workspace_with_nul_file):
    """The BUG_0052 repro (root + recursive + include) now returns the matches."""
    om = OperationManager(base_dir=str(workspace_with_nul_file))

    out = om.list_directory('.', recursive=True, max_depth=2, include='*.md', char_limit=-1)

    assert 'Error listing directory' not in out, out
    assert 'root.md' in out, out
    assert 'nested.md' in out, out


def test_recursive_exclude_on_root_survives_nul_entry(workspace_with_nul_file):
    """Exclude pruning calls relpath on every subdir; it must stay robust too."""
    om = OperationManager(base_dir=str(workspace_with_nul_file))

    out = om.list_directory('.', recursive=True, exclude='*.txt', char_limit=-1, max_entries=1000)

    assert 'Error listing directory' not in out, out
    assert 'root.md' in out, out
    assert 'notes.txt' not in out, out
