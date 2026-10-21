"""BUG_0111: list_dir must honour an explicit max_entries, not be cut by a hidden char limit.

Root cause: ``list_directory`` renders the entry list (correctly capped by ``max_entries``)
and then unconditionally truncates the whole result by ``char_limit``. The effective ``char_limit``
came from the tool wrapper (file_ops.py), NOT a per-call parameter, so for any non-trivial
directory the 3000/8000-char budget cut the listing long before the 500-entry cap could
apply — making ``max_entries`` appear to have no effect.

Fix (wrapper only): when the caller explicitly passes ``max_entries``, pass ``char_limit=-1``
so the entry cap governs and the caller gets up to ``max_entries`` entries. Default calls
(no ``max_entries`` param) keep the existing char-limit safety unchanged. A schema
``maximum`` on ``max_entries`` bounds explicit requests.

Part A exercises the real list_directory entry-cap behaviour; Part B asserts the wrapper
routes an explicit request to char_limit=-1 and leaves default calls untouched.
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_cascade.operation_manager import OperationManager
from agent_cascade.tools.custom.file_ops import ListDir, DEFAULT_LIST_DIR_CHAR_LIMIT, MAX_LIST_DIR_ENTRIES


class TestBug0111ListDirMaxEntriesNotUndercutByCharLimit:

    def test_explicit_max_entries_returns_all_entries_no_char_truncation(self, tmp_path):
        """max_entries >= N returns all N entries with no char-truncation footer."""
        for i in range(60):
            (tmp_path / f"f_{i:03d}.txt").write_text('x', encoding='utf-8')
        om = OperationManager(base_dir=str(tmp_path))

        out = om.list_directory('.', files_only=True, max_entries=100, char_limit=-1)
        assert 'f_059.txt' in out, f"last entry must be listed: {out}"
        assert 'TRUNCATED' not in out, f"no char-truncation footer expected: {out}"

    def test_entry_cap_still_binds_when_exceeded(self, tmp_path):
        """max_entries < N returns exactly max_entries + the entry-cap footer naming the knob."""
        for i in range(60):
            (tmp_path / f"f_{i:03d}.txt").write_text('x', encoding='utf-8')
        om = OperationManager(base_dir=str(tmp_path))

        out = om.list_directory('.', files_only=True, max_entries=10, char_limit=-1)
        assert 'output limited to 10' in out, f"entry-cap footer must name max_entries: {out}"
        assert out.count('.txt') <= 10, f"must cap rendered entries at 10, got {out.count('.txt')}: {out}"

    def _capture_wrapper_kwargs(self, params: dict) -> dict:
        """Call the list_dir wrapper with a capturing operation_manager; return its kwargs."""
        captured: dict = {}

        def fake_list_directory(*args, **kwargs):
            captured.update(kwargs)
            captured['_positional'] = args
            return 'listing'

        pool = SimpleNamespace(operation_manager=SimpleNamespace(list_directory=fake_list_directory))
        tool = ListDir(agent_pool=pool)
        result = tool.call(json.dumps(params))
        assert not str(result).startswith('ERROR:'), f"wrapper call failed: {result!r}"
        return captured

    def test_wrapper_passes_char_limit_minus_one_when_max_entries_given(self):
        """Explicit max_entries -> wrapper relaxes the char budget so the entry cap governs."""
        captured = self._capture_wrapper_kwargs({'path': '.', 'max_entries': 100})
        assert captured['char_limit'] == -1, f"char_limit must be -1 for explicit request, got {captured['char_limit']}"
        assert captured['max_entries'] == 100, f"max_entries must be forwarded, got {captured.get('max_entries')}"

    def test_wrapper_keeps_default_char_limit_when_max_entries_absent(self):
        """Default call (no max_entries) keeps the documented char-limit safety."""
        captured = self._capture_wrapper_kwargs({'path': '.'})
        assert captured['char_limit'] == DEFAULT_LIST_DIR_CHAR_LIMIT, \
            f"default char limit must be preserved, got {captured['char_limit']}"

    def test_schema_bounds_max_entries(self):
        """The schema 'maximum' rejects an out-of-range explicit max_entries."""
        captured: dict = {}

        def fake_list_directory(*args, **kwargs):
            captured.update(kwargs)
            return 'listing'

        pool = SimpleNamespace(operation_manager=SimpleNamespace(list_directory=fake_list_directory))
        tool = ListDir(agent_pool=pool)
        result = tool.call(json.dumps({'path': '.', 'max_entries': MAX_LIST_DIR_ENTRIES + 1}))
        assert str(result).startswith('ERROR:'), f"request over the ceiling must be rejected: {result!r}"
        assert 'maximum' in str(result), f"rejection must cite the bound: {result!r}"
        assert captured == {}, f"list_directory must not be called for a rejected request: {captured}"
