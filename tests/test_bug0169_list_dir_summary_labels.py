"""BUG_0169: show_summary reported truncated counts as "Total".

The summary is built from the entries actually rendered, so under max_entries/max_depth
truncation "Total files: 2" on a 100-file tree was wrong. The labels now say "Listed",
which is exactly what the numbers are.
"""

import re

from agent_cascade.operation_manager import OperationManager


def test_summary_uses_listed_labels(tmp_path):
    (tmp_path / 'a.txt').write_text('x', encoding='utf-8')
    (tmp_path / 'sub').mkdir()
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, show_summary=True, char_limit=-1)

    assert 'Listed directories:' in out, out
    assert 'Listed files:' in out, out
    assert 'Listed size:' in out, out
    assert 'Total directories' not in out, out
    assert 'Total files' not in out, out
    assert re.search(r'Listed directories:\s+1\b', out), out
    assert re.search(r'Listed files:\s+1\b', out), out


def test_summary_counts_only_rendered_entries_under_truncation(tmp_path):
    for i in range(5):
        (tmp_path / f'f{i}.txt').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', files_only=True, max_entries=2, show_summary=True, char_limit=-1)

    assert re.search(r'Listed files:\s+2\b', out), out
    assert re.search(r'Listed files:\s+5\b', out) is None, out
    assert 'output limited to 2 entries' in out, out
