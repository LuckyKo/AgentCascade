"""Regression tests for BUG_0088 — skill-metrics flush must tolerate a transient Windows
file lock and must NOT surface it as a scary [SKILLS] WARNING.

Root cause: on Windows ``os.replace`` of the shared global ``agents/global/skills-metrics.json``
raises PermissionError / OSError winerror 32 (sharing violation) when a concurrent process holds
the target open. ``fcntl.flock`` is a no-op on Windows, so there is no cross-process protection.
Metrics are best-effort and every flush rewrites the FULL snapshot, so a lost flush self-heals.

Fix: a SMALL BOUNDED retry (3 attempts, 50ms→100ms→200ms backoff) around ``os.replace`` for
transient locks only, and the final failure log downgraded from WARNING to DEBUG.

Uses the hermetic_skill_manager fixture (tests/conftest.py) so no production artifact is touched.
"""

import logging
import os
from unittest.mock import patch

from agent_cascade.log import logger as app_logger


def _winerror32():
    """An OSError carrying the Windows sharing-violation code (portable across platforms)."""
    exc = OSError(32, 'The process cannot access the file because it is held by another process')
    exc.winerror = 32
    return exc


def test_transient_lock_recovers_without_warning(hermetic_skill_manager, caplog):
    """A single transient lock on os.replace is retried and the flush succeeds; no WARNING."""
    m = hermetic_skill_manager
    m._metrics = {'demo-skill': {'status': 'active'}}

    real_replace = os.replace
    calls = {'n': 0}

    def flaky_replace(src, dst):
        calls['n'] += 1
        if calls['n'] == 1:
            raise _winerror32()
        real_replace(src, dst)

    caplog.set_level(logging.DEBUG, logger=app_logger.name)
    try:
        with patch('agent_cascade.skills.manager._os.replace', side_effect=flaky_replace):
            m._flush_metrics_to_disk()
    finally:
        caplog.set_level(logging.NOTSET, logger=app_logger.name)

    # Recovered: file written with the full snapshot.
    assert m._metrics_file.exists(), 'expected the metrics file to be written after retry'
    assert calls['n'] >= 2, 'expected the transient failure to be retried'
    import json
    assert json.loads(m._metrics_file.read_text(encoding='utf-8'))['skills']['demo-skill']['status'] == 'active'

    # No scary WARNING about the flush.
    warnings = [r for r in caplog.records if r.levelname == 'WARNING' and 'Failed to flush metrics' in r.getMessage()]
    assert not warnings, f'expected no flush WARNING, got: {[r.getMessage() for r in warnings]}'


def test_persistent_lock_degrades_to_debug(hermetic_skill_manager, caplog):
    """A persistent lock never raises out of the flush; it logs at DEBUG (not WARNING) and
    cleans up the orphaned temp file."""
    m = hermetic_skill_manager
    m._metrics = {'demo-skill': {'status': 'active'}}

    attempts = {'n': 0}

    def always_locked(src, dst):
        attempts['n'] += 1
        raise _winerror32()

    caplog.set_level(logging.DEBUG, logger=app_logger.name)
    try:
        with patch('agent_cascade.skills.manager._os.replace', side_effect=always_locked):
            m._flush_metrics_to_disk()  # must not raise
    finally:
        caplog.set_level(logging.NOTSET, logger=app_logger.name)

    # Bounded: exactly 3 attempts, not an unbounded loop.
    assert attempts['n'] == 3, f'expected exactly 3 attempts, got {attempts['n']}'

    # Degrades at DEBUG, never WARNING.
    debug_lines = [r for r in caplog.records if 'Failed to flush metrics' in r.getMessage()]
    assert debug_lines, 'expected a debug-level note about the failed flush'
    assert all(r.levelname == 'DEBUG' for r in debug_lines), \
        f'expected DEBUG only, got: {[(r.levelname, r.getMessage()) for r in debug_lines]}'

    # Orphaned temp file removed; no final metrics file (replace never succeeded).
    tmp = m._metrics_file.with_suffix('.tmp')
    assert not tmp.exists(), 'orphaned .tmp must be cleaned up'
