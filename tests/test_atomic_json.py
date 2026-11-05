"""Regression tests for atomic_write_json path-type guard (BUG_0218).

Ensures that passing a non-path object (e.g. a MagicMock) raises TypeError
instead of silently stringifying into a junk directory tree.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import json

from agent_cascade.utils.atomic_json import _json_safe, atomic_write_json


class TestAtomicWriteJsonPathGuard:
    """The path argument must be a real path type (str or os.PathLike)."""

    def test_rejects_magicmock(self):
        """A bare MagicMock is neither str nor Path → TypeError."""
        m = MagicMock()
        with pytest.raises(TypeError, match=r'atomic_write_json: path must be str or pathlib.Path'):
            atomic_write_json(m, {'key': 'value'})

    def test_rejects_magicmock_child(self):
        """Simulates the exact BUG_0218 failure: mock._config_dir / 'file.json'."""
        m = MagicMock()
        mock_path = m._config_dir / 'pool_settings.json'
        with pytest.raises(TypeError, match=r'atomic_write_json: path must be str or pathlib.Path'):
            atomic_write_json(mock_path, {'key': 'value'})

    def test_accepts_str(self, tmp_path):
        """A plain string path works normally."""
        p = tmp_path / 'ok.json'
        atomic_write_json(str(p), {'data': 1})
        assert p.exists()

    def test_accepts_path(self, tmp_path):
        """A pathlib.Path works normally."""
        p = tmp_path / 'ok.json'
        atomic_write_json(p, {'data': 2})
        assert p.exists()


class TestJsonSafe:
    """_json_safe normalizes non-JSON containers at the persistence boundary."""

    def test_frozenset_becomes_sorted_list(self):
        assert _json_safe(frozenset({'b', 'a'})) == ['a', 'b']

    def test_set_becomes_sorted_list(self):
        assert _json_safe({'c', 'b', 'a'}) == ['a', 'b', 'c']

    def test_unsortable_set_falls_back_to_list(self):
        """A set of mutually incomparable items must not raise — plain list fallback."""
        result = _json_safe({1, 'a'})  # int vs str: no total ordering
        assert result in ([1, 'a'], ['a', 1])

    def test_tuple_becomes_list(self):
        assert _json_safe((1, 2, 3)) == [1, 2, 3]

    def test_nested_containers_recurse(self):
        payload = {'a': (1, 2), 'b': [{'c': {'d', 'e'}}], 'e': ('x', frozenset({'y', 'z'}))}
        assert _json_safe(payload) == {'a': [1, 2], 'b': [{'c': ['d', 'e']}], 'e': ['x', ['y', 'z']]}

    def test_set_elements_recurse(self):
        """Set elements must be normalized too, not returned raw."""
        # Subset-ordered frozensets: {'a'} ⊂ {'a','b'} → deterministic order.
        result = _json_safe({'s': {frozenset({'a'}), frozenset({'a', 'b'})}})
        assert result == {'s': [['a'], ['a', 'b']]}
        # The result must round-trip through JSON (no raw containers left behind).
        assert json.loads(json.dumps(result)) == result

    def test_json_natives_pass_through_unchanged(self):
        assert _json_safe(1) == 1
        assert _json_safe('x') == 'x'
        assert _json_safe(None) is None
        assert _json_safe(True) is True
        assert _json_safe([1, 'two', 3.0]) == [1, 'two', 3.0]

    def test_atomic_write_json_persists_set_valued_key(self, tmp_path):
        """A frozenset must not veto the file (Todo.md:134 backstop)."""
        p = tmp_path / 'data.json'
        atomic_write_json(p, {'skill_always_protected': frozenset({'a', 'b'}), 'other': 1})
        on_disk = json.loads(p.read_text())
        assert on_disk['skill_always_protected'] == ['a', 'b']
        assert on_disk['other'] == 1
