"""BUG_0166: empty-result message noise and the flat-mode slash-pattern trap.

Two message-quality defects left over from BUG_0160:

1. ``skip_vcs`` was named in every recursive filtered-empty result, even when no VCS
   directory existed (nothing was pruned), implying the caller had hidden data.
2. Flat mode matches include/exclude against the basename only, so
   ``include='.hidden/*'`` silently matched nothing while the same call with
   ``recursive=true`` worked. The empty result now points at ``recursive=true``.
"""

from agent_cascade.operation_manager import OperationManager


def test_vcs_filter_not_named_when_nothing_was_pruned(tmp_path):
    (tmp_path / 'a.txt').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, include='*.md', char_limit=-1)

    assert 'no entries matched the active filters' in out, out
    assert 'skip_vcs' not in out, out


def test_skip_vcs_named_when_a_vcs_directory_was_pruned(tmp_path):
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'a.txt').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, include='*.md', char_limit=-1)

    assert 'no entries matched the active filters' in out, out
    assert 'skip_vcs' in out, out


def test_flat_slash_include_hints_at_recursive_mode(tmp_path):
    (tmp_path / '.hidden').mkdir()
    (tmp_path / '.hidden' / 'h.md').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', include='.hidden/*', char_limit=-1)

    assert 'no entries matched the active filters' in out, out
    assert 'recursive=true' in out, out


def test_recursive_slash_include_still_works_without_hint(tmp_path):
    (tmp_path / '.hidden').mkdir()
    (tmp_path / '.hidden' / 'h.md').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, include='.hidden/*', char_limit=-1)

    assert '.hidden/h.md' in out, out
    assert 'recursive=true' not in out, out
