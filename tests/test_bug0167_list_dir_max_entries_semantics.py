"""BUG_0167: max_entries now counts entries (directory headers + file lines) in both modes.

Flat mode already capped dirs+files, but recursive mode counted only files, so directory
headers were unbounded: a tree with 10k subdirs and max_entries=500 rendered all 10k
headers. The metadata says "entries", so both modes now share one budget.

Directory headers consume the budget only when they will actually be rendered (a dir that
fails the name filter has no header and must not consume a slot). files_only/dirs_only
consume only what they render.
"""

from agent_cascade.operation_manager import OperationManager


def _make_wide_tree(tmp_path):
    for i in range(5):
        (tmp_path / f'd{i}').mkdir()
        (tmp_path / f'f{i}.txt').write_text('x', encoding='utf-8')


def test_recursive_dir_headers_consume_entry_budget(tmp_path):
    _make_wide_tree(tmp_path)
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, max_entries=3, char_limit=-1)

    assert out.count('[DIR]') == 3, out
    assert '.txt' not in out, out
    assert 'output limited to 3 entries' in out, out


def test_flat_and_recursive_cap_the_same_entries(tmp_path):
    _make_wide_tree(tmp_path)
    om = OperationManager(base_dir=str(tmp_path))

    flat = om.list_directory('.', max_entries=3, char_limit=-1)
    recursive = om.list_directory('.', recursive=True, max_entries=3, char_limit=-1)

    for label, out in (('flat', flat), ('recursive', recursive)):
        assert 'd0/' in out, (label, out)
        assert 'd2/' in out, (label, out)
        assert 'd3/' not in out, (label, out)
        assert 'f0.txt' not in out, (label, out)
        assert 'output limited to 3' in out, (label, out)


def test_files_only_budget_ignores_dir_headers(tmp_path):
    _make_wide_tree(tmp_path)
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, files_only=True, max_entries=2, char_limit=-1)

    assert out.count('.txt') == 2, out
    assert '[DIR]' not in out, out
    assert 'output limited to 2 entries' in out, out


def test_dirs_only_budget_counts_dir_headers(tmp_path):
    _make_wide_tree(tmp_path)
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, dirs_only=True, max_entries=2, char_limit=-1)

    assert out.count('[DIR]') == 2, out
    assert 'output limited to 2 entries' in out, out


def test_no_truncation_notice_when_entries_fit(tmp_path):
    (tmp_path / 'a.txt').write_text('x', encoding='utf-8')
    (tmp_path / 'b.txt').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, max_entries=2, char_limit=-1)

    assert 'a.txt' in out and 'b.txt' in out, out
    assert 'output limited to' not in out, out


def test_filtered_out_entries_do_not_trigger_truncation_notice(tmp_path):
    for i in range(3):
        (tmp_path / f'f{i}.txt').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, include='*.md', max_entries=1, char_limit=-1)

    assert 'no entries matched the active filters' in out, out
    assert 'output limited to' not in out, out


def test_truncation_notice_survives_later_empty_directory(tmp_path):
    """Regression: a later empty dir must not clear the truncation flag
    that was set by budget exhaustion in an earlier subtree.

    Budget of 4 is consumed by: dirA + dirB (root level) + s0 + s1 (inside dirA).
    s2/s3 are dropped (truncation set).  os.walk then yields dirB (empty, no files,
    no subdirs) — the old code would clear ``truncated`` via
    ``truncated = bool(filenames)`` → False.
    """
    (tmp_path / 'dirA').mkdir()
    for i in range(4):
        (tmp_path / 'dirA' / f's{i}').mkdir()
    (tmp_path / 'dirB').mkdir()  # empty leaf walked after budget exhaustion
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, max_entries=4, char_limit=-1)

    assert 'output limited to 4 entries' in out, out
