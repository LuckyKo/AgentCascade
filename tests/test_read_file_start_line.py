"""BUG_0022 regression tests — read_file unknown-parameter handling.

Root cause (see .bug_tracker/BUG_0022_*.md): the model sent the line offset as
``offset`` instead of ``start_line``; the schema had no additionalProperties:false,
so jsonschema accepted and silently discarded it and the read started at line 1.

Fix under test:
- Layer 1: ReadFile.parameters carries additionalProperties: False.
- Layer 2: ReadFile.call converts the rejection into a returned ERROR string and
  coerces the narrow ``offset`` alias to ``start_line`` with a nudge log.

All tests drive the public ReadFile.call() so they exercise the real argument
parsing path — the layer that actually broke. Fixtures live under DEFAULT_WORKSPACE
(tmp_path) because the standalone path resolver (agent_pool=None) only allows
paths inside DEFAULT_WORKSPACE.
"""
import jsonschema
from pathlib import Path

import pytest

from agent_cascade.settings import DEFAULT_WORKSPACE
from agent_cascade.tools.custom.file_ops import ReadFile


def _ws_file(name: str, content: str):
    """Write *content* under DEFAULT_WORKSPACE (the only root the standalone resolver allows)."""
    p = Path(DEFAULT_WORKSPACE) / name
    p.write_text(content, encoding='utf-8')
    return p


@pytest.fixture(scope='module')
def big_file():
    """A >200KB, 4051-line file mirroring the BUG_0022 target shape.

    Module-scoped: written ONCE (the per-test write was racy under xdist — a worker's
    unlink could delete another worker's fixture mid-read). Left in place after the
    module; it is a tiny, name-unique scratch file inside DEFAULT_WORKSPACE.
    """
    lines = [f"LINE_{i:05d} " + 'x' * 60 for i in range(1, 4052)]
    return _ws_file('bug22_big.py', '\n'.join(lines) + '\n')


def test_offset_alias_reads_requested_window(big_file):
    """T1 (revert-proof): the literal incident repro — offset=478 must read line 478."""
    out = ReadFile().call({'path': str(big_file), 'offset': 478, 'limit': 44})
    assert not out.startswith('ERROR'), out
    assert 'LINE_00478' in out
    assert 'LINE_00479' in out
    assert 'LINE_00477' not in out


def test_start_line_window_on_large_file(big_file):
    """T2 (control): canonical start_line still correct on a large file."""
    out = ReadFile().call({'path': str(big_file), 'start_line': 478, 'limit': 44})
    assert not out.startswith('ERROR'), out
    assert 'LINE_00478' in out and 'LINE_00521' in out
    assert 'LINE_00477' not in out and 'LINE_00522' not in out


def test_header_reports_requested_range(big_file):
    """T3: header brackets the requested range — it must never lie about the region."""
    out = ReadFile().call({'path': str(big_file), 'start_line': 478, 'limit': 44})
    assert 'lines 478-521/4051' in out.split('\n')[0]


def test_unknown_param_errors_loudly(big_file):
    """T4 (revert-proof): an unknown key fails loudly instead of being dropped."""
    out = ReadFile().call({'path': str(big_file), 'start_line_from': 10, 'limit': 5})
    assert out.startswith('ERROR'), out
    assert 'unknown parameter' in out
    assert 'start_line_from' in out


def test_small_file_unchanged(tmp_path):
    """T5 (control): the common small-file case stays green.

    Uses tmp_path here (not DEFAULT_WORKSPACE) to avoid any cross-worker name collision;
    ReadFile is given a fake agent_pool whose operation_manager resolves via the real
    PathResolutionMixin, so no resolver monkeypatching is needed.
    """
    p = tmp_path / 'bug22_small.py'
    p.write_text('\n'.join(f"line {i}" for i in range(1, 21)) + '\n', encoding='utf-8')

    class _FakeOM:
        def _resolve_path(self, path, mode='ro'):
            return Path(path).resolve()

    class _FakePool:
        operation_manager = _FakeOM()

    out = ReadFile(agent_pool=_FakePool()).call({'path': str(p), 'start_line': 11, 'limit': 5})
    assert not out.startswith('ERROR'), out
    assert 'lines 11-15/20' in out.split('\n')[0]
    assert 'line 11' in out and 'line 15' in out and 'line 16' not in out


def test_schema_rejects_additional_properties():
    """T6 (revert-proof): the schema itself rejects unknown keys."""
    assert ReadFile.parameters.get('additionalProperties') is False
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'path': 'x.py', 'offset': 5}, ReadFile.parameters)
