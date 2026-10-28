"""BUG_0160: list_dir's empty-result message must not conflate two opposite states.

The old single message ``(empty directory or all entries filtered out)`` was true both
for a directory with zero entries and for a directory whose entries all failed the
active filters. A negative finding ("no state file for model X") therefore could not be
based on a filtered listing alone.

Fix: the flat/recursive helpers also return the pre-filter entry count. The empty-result
branch now emits either ``(directory contains no entries)`` or
``(no entries matched the active filters: <filters>)`` plus the pre-filter count and a
hint to adjust/remove the filters.
"""

from pathlib import Path

from agent_cascade.operation_manager import OperationManager

OLD_AMBIGUOUS = '(empty directory or all entries filtered out)'


def _write(path: Path, text: str = 'x') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def test_truly_empty_flat_directory_reports_no_entries(tmp_path):
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', char_limit=-1)

    assert 'directory contains no entries' in out, out
    assert 'no entries matched' not in out, out
    assert OLD_AMBIGUOUS not in out, out


def test_truly_empty_recursive_directory_reports_no_entries(tmp_path):
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert 'directory contains no entries' in out, out
    assert 'no entries matched' not in out, out


def test_filtered_flat_listing_reports_count_and_active_filter(tmp_path):
    for name in ('a.bin', 'b.bin', 'c.bin'):
        _write(tmp_path / name)
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', files_only=True, include='*DFlash2*', char_limit=-1)

    assert 'no entries matched the active filters' in out, out
    assert "include='*DFlash2*'" in out, out
    assert '3 entries exist before filtering' in out, out
    assert 'directory contains no entries' not in out, out
    assert OLD_AMBIGUOUS not in out, out


def test_filtered_listing_reports_singular_count(tmp_path):
    _write(tmp_path / 'only.bin')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', include='*.md', char_limit=-1)

    assert '1 entry exists before filtering' in out, out


def test_files_only_names_itself_as_the_hiding_filter(tmp_path):
    """A dirs-only directory under files_only is 'filtered', not 'empty'."""
    (tmp_path / 'sub_a').mkdir()
    (tmp_path / 'sub_b').mkdir()
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', files_only=True, char_limit=-1)

    assert 'no entries matched the active filters' in out, out
    assert 'files_only' in out, out
    assert '2 entries exist before filtering' in out, out


def test_size_filter_is_named_in_the_message(tmp_path):
    _write(tmp_path / 'small.txt')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', min_size='1GB', char_limit=-1)

    assert 'no entries matched the active filters' in out, out
    assert "min_size='1GB'" in out, out
    assert '1 entry exists before filtering' in out, out


def test_filtered_recursive_listing_reports_count(tmp_path):
    _write(tmp_path / 'states' / 'nested' / 'model-other.bin')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('states', recursive=True, files_only=True, include='*DFlash2*', char_limit=-1)

    assert 'no entries matched the active filters' in out, out
    assert "include='*DFlash2*'" in out, out
    assert '2 entries exist before filtering' in out, out


def test_recursive_files_only_on_dirs_only_tree_reports_filtered(tmp_path):
    """Regression: files_only used to count displayable dirs as output, so a tree of
    only directories rendered an empty listing with NO empty-result message at all."""
    (tmp_path / 'states' / 'nested').mkdir(parents=True)
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('states', recursive=True, files_only=True, char_limit=-1, show_summary=True)

    assert 'no entries matched the active filters' in out, out
    assert 'files_only' in out, out
    assert '1 entry exists before filtering' in out, out
    assert 'Listed directories: 0' in out, out


def test_non_empty_listing_has_no_empty_result_message(tmp_path):
    _write(tmp_path / 'keep.md')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', include='*.md', char_limit=-1)

    assert 'keep.md' in out, out
    assert 'no entries matched' not in out, out
    assert 'directory contains no entries' not in out, out
