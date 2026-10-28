"""BUG_0168: tree rendering is iterative, so deep trees no longer hit RecursionError.

``render_dir`` was a recursive nested function: one Python frame per directory level.
A tree deeper than the interpreter's recursion limit (~1000 by default) raised
RecursionError, which ``list_directory`` caught and returned as
"Error listing directory: maximum recursion depth exceeded" — the entire listing was
lost, not just the deep tail.

Fix: an explicit stack of (dirpath, depth, entry-iterator). A stack of iterators keeps
the original depth-first rendering order.
"""

import sys
from pathlib import Path

import pytest

from agent_cascade.operation_manager import OperationManager

DEPTH = 600  # > the temporarily lowered recursion limit below


def _fake_deep_walk(depth: int):
    def walk(root, topdown=True, onerror=None, followlinks=False):
        current = Path(root)
        for _ in range(depth):
            yield (str(current), ['d'], [])
            current = current / 'd'
        yield (str(current), [], ['deep.txt'])
    return walk


def _run_with_low_recursion_limit(fn):
    """Run *fn* with a limit low enough that the old recursive renderer would fail."""
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(300)
    try:
        return fn()
    finally:
        sys.setrecursionlimit(old_limit)


def test_deep_tree_renders_without_recursion_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        'agent_cascade.operation_manager.file_operations.os.walk', _fake_deep_walk(DEPTH))
    om = OperationManager(base_dir=str(tmp_path))

    out = _run_with_low_recursion_limit(
        lambda: om.list_directory('.', recursive=True, max_entries=1000, char_limit=-1))

    assert 'deep.txt' in out, out[:500]
    assert 'Error listing directory' not in out, out[:500]
    assert out.count('[DIR]') == DEPTH, out.count('[DIR]')


def test_deep_files_only_tree_renders_without_recursion_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        'agent_cascade.operation_manager.file_operations.os.walk', _fake_deep_walk(DEPTH))
    om = OperationManager(base_dir=str(tmp_path))

    out = _run_with_low_recursion_limit(
        lambda: om.list_directory('.', recursive=True, files_only=True, char_limit=-1))

    assert 'deep.txt' in out, out[:500]
    assert 'Error listing directory' not in out, out[:500]
    assert '[DIR]' not in out, out[:500]


def test_iterative_render_preserves_depth_first_order(tmp_path):
    (tmp_path / 'a_dir').mkdir()
    (tmp_path / 'a_dir' / 'child.txt').write_text('x', encoding='utf-8')
    (tmp_path / 'b.txt').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert '[DIR] a_dir/' in out, out
    assert 'a_dir/child.txt' in out, out
    assert out.index('[DIR] a_dir/') < out.index('a_dir/child.txt') < out.index('b.txt'), out


@pytest.mark.parametrize('depth', [1, 2, 10])
def test_shallow_trees_still_render(tmp_path, monkeypatch, depth):
    monkeypatch.setattr(
        'agent_cascade.operation_manager.file_operations.os.walk', _fake_deep_walk(depth))
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert 'deep.txt' in out, out
    assert out.count('[DIR]') == depth, out
