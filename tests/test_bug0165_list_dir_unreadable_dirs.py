"""BUG_0165: recursive list_dir silently dropped directories it could not read.

``os.walk`` was called without ``onerror``, so a permission-denied (or otherwise
unreadable) subtree was skipped with no trace in the output. The listing then looked
complete, which can seed a false-absence claim ("there is no config under X") — the
same failure class as BUG_0160.

Fix: collect walk errors and append a warning naming each unreadable directory and the
reason. A completely unreadable root no longer claims "(directory contains no entries)".
"""

from agent_cascade.operation_manager import OperationManager
from agent_cascade.operation_manager.file_operations import _walk_with_entries


def _patch_walk(monkeypatch, errors, real=True):
    """Patch file_operations._walk_with_entries to report *errors* (and optionally walk)."""
    real_walk = _walk_with_entries

    def fake_walk(root, onerror=None):
        for err in errors:
            if onerror is not None:
                onerror(err)
        if real:
            yield from real_walk(root, onerror=onerror)

    monkeypatch.setattr(
        'agent_cascade.operation_manager.file_operations._walk_with_entries', fake_walk)


def test_unreadable_subdirectory_is_reported(tmp_path, monkeypatch):
    (tmp_path / 'ok.txt').write_text('x', encoding='utf-8')
    _patch_walk(monkeypatch, [PermissionError(13, 'Permission denied', str(tmp_path / 'secret'))])

    om = OperationManager(base_dir=str(tmp_path))
    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert 'ok.txt' in out, out
    assert '1 directory could not be read' in out, out
    assert 'secret (Permission denied)' in out, out


def test_unreadable_root_is_not_reported_as_empty(tmp_path, monkeypatch):
    _patch_walk(monkeypatch, [PermissionError(13, 'Permission denied', str(tmp_path))], real=False)

    om = OperationManager(base_dir=str(tmp_path))
    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert 'directory contains no entries' not in out, out
    assert 'nothing could be listed' in out, out
    assert 'could not be read' in out, out


def test_many_unreadable_directories_are_capped(tmp_path, monkeypatch):
    errors = [PermissionError(13, 'Permission denied', str(tmp_path / f'd{i}')) for i in range(7)]
    _patch_walk(monkeypatch, errors, real=False)

    om = OperationManager(base_dir=str(tmp_path))
    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert '7 directories could not be read' in out, out
    assert '+2 more' in out, out


def test_clean_listing_has_no_read_warning(tmp_path):
    (tmp_path / 'ok.txt').write_text('x', encoding='utf-8')

    om = OperationManager(base_dir=str(tmp_path))
    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert 'could not be read' not in out, out
