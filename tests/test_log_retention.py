"""Scoped tests for the log/telemetry retention sweep (agent_cascade.utils.log_retention).

Every test works on a throwaway temp directory (pytest ``tmp_path``) with backdated
mtimes; nothing touches the real logs dir, and the background scheduler thread is never
started. Only ``archive_old_logs`` is exercised directly.
"""

import gzip
import os
import time
from pathlib import Path

from agent_cascade.utils.log_retention import archive_old_logs

_DAY = 24 * 60 * 60
PAYLOAD = b'{"hello": "world"}\n'


def _write_log(path: Path, age_days: float) -> Path:
    """Write a small .jsonl file and backdate its mtime to ``age_days`` ago.

    Creates parent directories so the helper can also place files in subdirectories
    (used to prove the sweep does not recurse).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PAYLOAD)
    old = time.time() - age_days * _DAY
    os.utime(path, (old, old))
    return path


class TestArchiveOldLogs:
    """Tests for archive_old_logs()."""

    def test_stale_archived_fresh_untouched(self, tmp_path):
        """A stale log is gzipped into archive/; a fresh log is left alone."""
        old = _write_log(tmp_path / 'telemetry_old.jsonl', age_days=40)
        fresh = _write_log(tmp_path / 'telemetry_new.jsonl', age_days=0)

        result = archive_old_logs(retention_days=30, archive=True, log_dir=tmp_path)

        assert not old.exists()
        gz = tmp_path / 'archive' / 'telemetry_old.jsonl.gz'
        assert gz.exists()
        assert gzip.open(gz, 'rb').read() == PAYLOAD
        # Fresh file untouched and still readable.
        assert fresh.exists()
        assert fresh.read_bytes() == PAYLOAD
        assert result['archived'] == 1
        assert result['deleted'] == 0
        assert result['bytes_freed'] == len(PAYLOAD)
        assert result['errors'] == []

    def test_locked_file_skipped(self, tmp_path):
        """A stale log with a sibling .lock is treated as active and never archived."""
        locked = _write_log(tmp_path / 'telemetry_locked.jsonl', age_days=40)
        (Path(str(locked) + '.lock')).write_bytes(b'12345')

        archive_old_logs(retention_days=30, archive=True, log_dir=tmp_path)

        assert locked.exists()  # still present: skipped due to lock
        assert locked.read_bytes() == PAYLOAD

    def test_archive_false_deletes(self, tmp_path):
        """archive=False removes a stale log outright and mints no .gz."""
        stale = _write_log(tmp_path / 'telemetry_delete.jsonl', age_days=40)

        result = archive_old_logs(retention_days=30, archive=False, log_dir=tmp_path)

        assert not stale.exists()
        assert not (tmp_path / 'archive').exists()  # delete mode never creates archive/
        assert result['deleted'] == 1
        assert result['archived'] == 0

    def test_retention_days_zero_noop(self, tmp_path):
        """retention_days <= 0 disables the sweep entirely; nothing is moved."""
        stale = _write_log(tmp_path / 'telemetry_old.jsonl', age_days=40)

        result = archive_old_logs(retention_days=0, archive=True, log_dir=tmp_path)

        assert stale.exists()
        assert stale.read_bytes() == PAYLOAD
        assert result['disabled'] is True
        assert result['archived'] == 0
        assert result['deleted'] == 0
        assert result['bytes_freed'] == 0

    def test_subdir_not_recursed(self, tmp_path):
        """Only top-level *.jsonl is targeted; stale files in subdirs are left alone."""
        in_sub = _write_log(tmp_path / 'sub' / 'old.jsonl', age_days=40)
        in_archive = _write_log(tmp_path / 'archive' / 'already.jsonl', age_days=40)

        result = archive_old_logs(retention_days=30, archive=True, log_dir=tmp_path)

        assert in_sub.exists()
        assert in_archive.exists()
        assert result['archived'] == 0
        assert result['deleted'] == 0


def test_default_sweep_roots_cover_logs_and_telemetry():
    """The default sweep must cover BOTH session logs and the telemetry corpus.

    Telemetry_*.jsonl live under <workspace>/telemetry, NOT <workspace>/logs, so a
    logs-only default would leave the bulk of the growth untouched. Prefix match is used
    so the assertion is robust to a per-instance dir suffix (pytest sets a test_<pid> id).
    """
    from agent_cascade.utils.log_retention import _default_sweep_roots

    names = [r.name for r in _default_sweep_roots()]
    assert any(n.startswith('logs') for n in names), names
    assert any(n.startswith('telemetry') for n in names), names


def test_default_roots_aggregate_across_logs_and_telemetry(tmp_path, monkeypatch):
    """log_dir=None sweeps every default root, archives per-root, and aggregates counts."""
    logs_root = tmp_path / 'logs'
    tele_root = tmp_path / 'telemetry'
    _write_log(logs_root / 'coder_old.jsonl', age_days=40)
    _write_log(tele_root / 'telemetry_old.jsonl', age_days=40)
    _write_log(tele_root / 'telemetry_new.jsonl', age_days=0)  # fresh: must survive

    monkeypatch.setattr(
        'agent_cascade.utils.log_retention._default_sweep_roots',
        lambda: [logs_root, tele_root],
    )

    result = archive_old_logs(retention_days=30, archive=True)

    assert not (logs_root / 'coder_old.jsonl').exists()
    assert not (tele_root / 'telemetry_old.jsonl').exists()
    assert (tele_root / 'telemetry_new.jsonl').exists()  # fresh file untouched
    assert (logs_root / 'archive' / 'coder_old.jsonl.gz').exists()
    assert (tele_root / 'archive' / 'telemetry_old.jsonl.gz').exists()
    assert result['archived'] == 2
    assert result['deleted'] == 0
