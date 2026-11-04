"""Regression tests for atomic_write_json path-type guard (BUG_0218).

Ensures that passing a non-path object (e.g. a MagicMock) raises TypeError
instead of silently stringifying into a junk directory tree.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_cascade.utils.atomic_json import atomic_write_json


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
