"""BUG_0164: list_dir accepted nonsensical max_entries / max_depth values.

``max_entries=-1`` rendered all but the last entry and printed
``output limited to -1``; ``max_entries=0`` truncated everything and (recursively)
blamed ``skip_vcs`` for the empty result. ``max_depth=-5`` pruned every subdir at
the root with no warning because the "unlimited" sentinel was compared as ``!= -1``.

Fix: the schema now declares ``minimum: 1`` for ``max_entries`` and ``minimum: -1``
for ``max_depth`` (tool boundary), and ``list_directory`` defensively clamps invalid
values for direct API callers, emitting a visible warning instead of corrupt output.
"""

import json
from types import SimpleNamespace

from agent_cascade.operation_manager import OperationManager
from agent_cascade.tools.custom.file_ops import ListDir


def _make_tree(tmp_path):
    for i in range(5):
        (tmp_path / f'f{i}.txt').write_text('x', encoding='utf-8')
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'sub' / 'nested.txt').write_text('x', encoding='utf-8')


def test_zero_max_entries_is_clamped_and_warned(tmp_path):
    _make_tree(tmp_path)
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', files_only=True, max_entries=0, char_limit=-1)

    assert 'max_entries=0 is invalid' in out, out
    assert 'output limited to 0' not in out, out
    assert 'f0.txt' in out, out


def test_negative_max_entries_does_not_drop_entries(tmp_path):
    _make_tree(tmp_path)
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', files_only=True, max_entries=-1, char_limit=-1)

    assert 'max_entries=-1 is invalid' in out, out
    assert 'output limited to -1' not in out, out
    assert 'f4.txt' in out, f'the last entry used to be sliced off by a negative cap: {out}'


def test_negative_max_entries_recursive_does_not_blame_filters(tmp_path):
    _make_tree(tmp_path)
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, max_entries=-5, char_limit=-1)

    assert 'max_entries=-5 is invalid' in out, out
    assert 'output limited to -5' not in out, out
    assert 'f0.txt' in out, out


def test_negative_max_depth_is_treated_as_unlimited(tmp_path):
    _make_tree(tmp_path)
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, max_depth=-5, char_limit=-1)

    assert 'max_depth=-5 is invalid' in out, out
    assert 'sub/nested.txt' in out, out


def test_valid_limits_emit_no_warning(tmp_path):
    _make_tree(tmp_path)
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, max_depth=2, max_entries=10, char_limit=-1)

    assert 'is invalid' not in out, out


def test_schema_rejects_zero_max_entries_before_calling_list_directory():
    captured = {}

    def fake_list_directory(*args, **kwargs):
        captured.update(kwargs)
        return 'listing'

    pool = SimpleNamespace(operation_manager=SimpleNamespace(list_directory=fake_list_directory))
    tool = ListDir(agent_pool=pool)

    result = tool.call(json.dumps({'path': '.', 'max_entries': 0}))

    assert str(result).startswith('ERROR:'), result
    assert 'minimum' in str(result), result
    assert captured == {}, captured


def test_schema_rejects_negative_max_depth_before_calling_list_directory():
    captured = {}

    def fake_list_directory(*args, **kwargs):
        captured.update(kwargs)
        return 'listing'

    pool = SimpleNamespace(operation_manager=SimpleNamespace(list_directory=fake_list_directory))
    tool = ListDir(agent_pool=pool)

    result = tool.call(json.dumps({'path': '.', 'max_depth': -2}))

    assert str(result).startswith('ERROR:'), result
    assert 'minimum' in str(result), result
    assert captured == {}, captured
