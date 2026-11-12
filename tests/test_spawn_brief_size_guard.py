"""Regression tests for the spawn-brief size guard (two-tier: DEBUG routine band,
WARNING pathological).

The guard lives in ``AgentLifecycleManager.build_task_message`` (the single point
where ``context`` and ``task`` are assembled into the sub-agent's first message).
It is PURE OBSERVABILITY: no truncation, no rejection — the returned Message is
built identically.

The guard is two-tier:
  * routine band     (``_SPAWN_BRIEF_SIZE_WARN`` < size <= 2x the floor) -> DEBUG,
  * pathological band (size > 2x ``_SPAWN_BRIEF_SIZE_WARN``)           -> WARNING.
The routine band covers normal Coder briefs (observed 6-7k), so it stays DEBUG to
avoid constant WARNING noise, while a genuinely oversized brief (> 2x the floor)
still escalates to WARNING.

These tests pin both tiers:
  * at/below the floor            -> no record at all,
  * in the routine band           -> DEBUG record, no WARNING,
  * pathological (> 2x floor)     -> WARNING record,
  * the returned message is never altered (no truncation).
"""

import logging

import pytest

from agent_cascade.lifecycle_manager import (
    AgentLifecycleManager,
    _SPAWN_BRIEF_SIZE_WARN,
    _SPAWN_BRIEF_SIZE_WARN_PATHOLOGICAL,
)

# AC routes its own log records through this named logger; caplog must target it
# (same convention as the rest of the suite).
_APP_LOGGER = 'agent_cascade_logger'


def _mgr():
    """Lifecycle manager with no pool wiring.

    ``build_task_message`` reads only ``args`` and ``caller`` — it never touches
    ``self.pool``/``self.engine`` — so ``__new__`` sidesteps all pool setup (and the
    MagicMock-str-leak class of bugs) while still running the real builder.
    """
    return AgentLifecycleManager.__new__(AgentLifecycleManager)


def _records(caplog, levelno):
    return [r for r in caplog.records
            if r.levelno == levelno and 'spawn_brief_oversized' in r.getMessage()]


def _warnings(caplog):
    return _records(caplog, logging.WARNING)


def _debugs(caplog):
    return _records(caplog, logging.DEBUG)


def test_at_or_below_floor_is_silent(caplog):
    # Exactly at the floor (not >) must produce no record at all.
    at_floor = 'c' * _SPAWN_BRIEF_SIZE_WARN
    with caplog.at_level(logging.DEBUG, logger=_APP_LOGGER):
        _mgr().build_task_message({'task': 'do it', 'context': at_floor}, 'caller')
    assert _warnings(caplog) == []
    assert _debugs(caplog) == []


def test_routine_band_logs_debug_not_warning(caplog):
    # The observed Coder-brief band (floor+1 .. 2x) must be DEBUG, never WARNING.
    routine = 'c' * (_SPAWN_BRIEF_SIZE_WARN + 1)
    with caplog.at_level(logging.DEBUG, logger=_APP_LOGGER):
        _mgr().build_task_message({'task': 'do it', 'context': routine}, 'caller')
    assert len(_debugs(caplog)) == 1
    assert _warnings(caplog) == []


def test_routine_band_upper_edge_stays_debug(caplog):
    # Exactly at the pathological line (not >) must remain DEBUG (boundary check).
    at_pathological = 'c' * _SPAWN_BRIEF_SIZE_WARN_PATHOLOGICAL
    with caplog.at_level(logging.DEBUG, logger=_APP_LOGGER):
        _mgr().build_task_message({'task': 'do it', 'context': at_pathological}, 'caller')
    assert len(_debugs(caplog)) == 1
    assert _warnings(caplog) == []


def test_pathological_task_triggers_warning(caplog):
    # A genuinely oversized brief (> 2x the floor) escalates to WARNING.
    pathological = 't' * (_SPAWN_BRIEF_SIZE_WARN_PATHOLOGICAL + 1)
    with caplog.at_level(logging.DEBUG, logger=_APP_LOGGER):
        _mgr().build_task_message({'task': pathological, 'context': 'short'}, 'caller')
    assert len(_warnings(caplog)) == 1
    assert _debugs(caplog) == []


def test_pathological_context_triggers_warning(caplog):
    pathological = 'c' * (_SPAWN_BRIEF_SIZE_WARN_PATHOLOGICAL + 1)
    with caplog.at_level(logging.DEBUG, logger=_APP_LOGGER):
        _mgr().build_task_message({'task': 'short', 'context': pathological}, 'caller')
    assert len(_warnings(caplog)) == 1


def test_guard_is_pure_observability_message_unchanged(caplog):
    """The guard must not truncate or otherwise alter the returned message (any tier)."""
    big_context = 'C' * (_SPAWN_BRIEF_SIZE_WARN_PATHOLOGICAL + 100)
    big_task = 'T' * (_SPAWN_BRIEF_SIZE_WARN_PATHOLOGICAL + 200)
    args = {'task': big_task, 'context': big_context}

    with caplog.at_level(logging.DEBUG, logger=_APP_LOGGER):
        mgr = _mgr()
        first = mgr.build_task_message(dict(args), 'caller')
        second = mgr.build_task_message(dict(args), 'caller')

    # One WARNING per call (both pathological), and none may have suppressed content.
    assert len(_warnings(caplog)) == 2

    content = first.content
    # The documented, unmodified wrapper format.
    assert content.startswith('Context: This is a message from caller.')
    assert content.endswith('\n\nPlease help with this task.')
    # No truncation: the raw context and task are present verbatim.
    assert big_context in content
    assert big_task in content
    # Deterministic: the guard introduces no state/mutation, identical calls match.
    assert first.content == second.content


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
