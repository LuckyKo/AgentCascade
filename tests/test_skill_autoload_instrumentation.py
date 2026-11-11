"""BUG_0284 regression: observability for the BUG_0263 skill auto-load safety-net.

Before BUG_0284, ``MemoryHintManager._maybe_autoload_skills`` logged EVERY
``inject_skill`` outcome — including a force-load that did NOT land — at INFO, so
a silently-failing auto-load was indistinguishable from "the safety-net never
fired" in production telemetry. This suite pins the fix:

  * a failed auto-load is logged at WARNING and counted in
    ``skill_autoload_failed_by_reason`` under exactly one of
    ``skill_not_found`` / ``disabled`` / ``exception``;
  * the success path ('loaded' / 'already_loaded') stays INFO and increments
    nothing.

The counter is module-level (per-process) shared state, so every assertion uses
a before/after DELTA rather than an absolute value — the tests stay
order-independent no matter what else touches the dict.

Logging target: the shared emitting logger is ``agent_cascade_logger`` (see
``agent_cascade/log.py``); we capture via ``caplog.at_level(..., logger=name)``
using the live object's ``.name`` (not a hardcoded string), per the caplog
shared-module-logger gotcha.
"""

import logging
from types import SimpleNamespace

import pytest

from agent_cascade.log import logger as app_logger
from agent_cascade.memory_hint.manager import (
    MemoryHintManager,
    skill_autoload_failed_by_reason,
)

_SKILL = 'demo-skill'


def _make_mgr(inject_result=None, inject_raises=None, is_disabled=False):
    """A ``MemoryHintManager`` driven by a pool double that mirrors the REAL
    ``AgentPool.inject_skill`` contract: it returns the ``(status, re_enabled)``
    2-tuple and reads ``skill_manager.is_skill_disabled`` for the classification
    added by BUG_0284.

    Args:
        inject_result: the ``(status, re_enabled)`` tuple inject_skill returns.
        inject_raises: an exception instance inject_skill raises instead.
        is_disabled: what ``skill_manager.is_skill_disabled`` returns.

    Returns:
        ``(mgr, calls)`` where ``calls`` records every ``(agent_name, skill_name)``
        inject_skill saw (empty when the auto-load was skipped or short-circuited).
    """
    calls = []

    def inject_skill(agent_name, skill_name):
        calls.append((agent_name, skill_name))
        if inject_raises is not None:
            raise inject_raises
        return inject_result

    pool = SimpleNamespace(
        inject_skill=inject_skill,
        skill_manager=SimpleNamespace(is_skill_disabled=lambda _name: is_disabled),
    )
    return MemoryHintManager(pool), calls


def _autoload(mgr, skill=_SKILL, settings=None):
    """Invoke the safety-net directly (inst is unused by the method). The skill
    is at-threshold and not loaded, so the gate always reaches inject_skill."""
    mgr._maybe_autoload_skills(
        inst=None,
        name='agentX',
        skill_names=[skill],
        skill_counts={skill: 3},
        loaded=set(),
        settings=settings or {'skill_autoload_enabled': True, 'skill_autoload_threshold': 3},
    )


def _failed_records(caplog):
    """The new WARNING line(s): match on the 'auto-load FAILED' marker."""
    return [r for r in caplog.records if 'auto-load FAILED' in r.getMessage()]


def _counters():
    return dict(skill_autoload_failed_by_reason)


def test_failed_autoload_logs_warning_and_counts_skill_not_found(caplog):
    """A 'failed' status with a not-disabled skill → WARNING + skill_not_found +1."""
    with caplog.at_level(logging.INFO, logger=app_logger.name):
        mgr, calls = _make_mgr(inject_result=('failed', False), is_disabled=False)
        before = _counters()
        _autoload(mgr)

    assert calls == [('agentX', _SKILL)], 'inject_skill must be attempted exactly once'
    failed = _failed_records(caplog)
    assert failed, 'BUG_0284: a failed auto-load must emit a WARNING'
    assert all(r.levelno == logging.WARNING for r in failed), \
        f'failure must be WARNING, not INFO; got {[(r.levelname, r.getMessage()) for r in failed]}'
    assert 'reason=skill_not_found' in failed[-1].getMessage(), failed[-1].getMessage()
    assert skill_autoload_failed_by_reason['skill_not_found'] == before['skill_not_found'] + 1
    # Exhaustive: exactly one category moved; disabled/exception untouched.
    assert skill_autoload_failed_by_reason['disabled'] == before['disabled']
    assert skill_autoload_failed_by_reason['exception'] == before['exception']


def test_failed_autoload_on_disabled_skill_counts_disabled(caplog):
    """A 'failed' status where the skill IS disabled → reason=disabled, disabled +1."""
    with caplog.at_level(logging.INFO, logger=app_logger.name):
        mgr, calls = _make_mgr(inject_result=('failed', False), is_disabled=True)
        before = _counters()
        _autoload(mgr)

    assert calls == [('agentX', _SKILL)]
    failed = _failed_records(caplog)
    assert failed, 'disabled-not-loadable must still emit a WARNING'
    assert all(r.levelno == logging.WARNING for r in failed), \
        'a disabled auto-load failure must be WARNING, not INFO'
    assert 'reason=disabled' in failed[-1].getMessage(), failed[-1].getMessage()
    assert 'status=failed' in failed[-1].getMessage()
    assert skill_autoload_failed_by_reason['disabled'] == before['disabled'] + 1
    assert skill_autoload_failed_by_reason['skill_not_found'] == before['skill_not_found']
    assert skill_autoload_failed_by_reason['exception'] == before['exception']


def test_inject_skill_exception_counts_exception(caplog):
    """inject_skill raising → WARNING + exception +1 (skill_manager not consulted)."""
    with caplog.at_level(logging.INFO, logger=app_logger.name):
        boom = RuntimeError('skill body read exploded')
        mgr, calls = _make_mgr(inject_raises=boom)
        before = _counters()
        _autoload(mgr)

    assert calls == [('agentX', _SKILL)]
    failed = _failed_records(caplog)
    assert failed, 'BUG_0284: an inject_skill exception must be surfaced as a WARNING'
    msg = failed[-1].getMessage()
    assert 'reason=exception' in msg and 'skill body read exploded' in msg, msg
    assert skill_autoload_failed_by_reason['exception'] == before['exception'] + 1
    assert skill_autoload_failed_by_reason['skill_not_found'] == before['skill_not_found']
    assert skill_autoload_failed_by_reason['disabled'] == before['disabled']


@pytest.mark.parametrize('ok_status', ['loaded', 'already_loaded'])
def test_successful_autoload_is_info_only_no_counter(caplog, ok_status):
    """The success path is unchanged: INFO 'auto-loaded', no FAILED WARNING, no counter.

    'already_loaded' matters especially: it is the BUG_0263 sub-hypothesis-3 signal
    (the skill actually loaded but the gate's `loaded` snapshot was stale), and it
    must NOT be counted as a failure — leaving the counter at 0 while hint counts
    rise is what localises the failure to upstream of inject_skill.
    """
    with caplog.at_level(logging.INFO, logger=app_logger.name):
        mgr, calls = _make_mgr(inject_result=(ok_status, False))
        before = _counters()
        _autoload(mgr)

    assert calls == [('agentX', _SKILL)]
    assert not _failed_records(caplog), 'a successful auto-load must never be reported as FAILED'
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any('auto-loaded repeatedly-hinted skill' in m for m in info_lines), \
        f'expected the unchanged INFO auto-loaded line; got {info_lines}'
    assert all(r.levelno < logging.WARNING for r in caplog.records
               if 'auto-load' in r.getMessage()), 'success path must not warn'
    assert _counters() == before, 'a successful auto-load must increment nothing'


def test_classification_error_degrades_to_skill_not_found(caplog):
    """A broken is_skill_disabled query must not crash the hint path and must still
    land on exactly one category (skill_not_found)."""
    with caplog.at_level(logging.INFO, logger=app_logger.name):
        def _raise(_name):
            raise ValueError('skill manager on fire')
        pool = SimpleNamespace(
            inject_skill=lambda agent_name, skill_name: ('failed', False),
            skill_manager=SimpleNamespace(is_skill_disabled=_raise),
        )
        mgr = MemoryHintManager(pool)
        before = _counters()
        _autoload(mgr)  # must NOT raise

    failed = _failed_records(caplog)
    assert failed, 'a classified failure must still warn'
    assert 'reason=skill_not_found' in failed[-1].getMessage()
    assert skill_autoload_failed_by_reason['skill_not_found'] == before['skill_not_found'] + 1


def test_autoload_disabled_setting_does_nothing(caplog):
    """skill_autoload_enabled=False → early return: no inject call, no counter, no warn."""
    with caplog.at_level(logging.INFO, logger=app_logger.name):
        mgr, calls = _make_mgr(inject_result=('failed', False))
        before = _counters()
        _autoload(mgr, settings={'skill_autoload_enabled': False, 'skill_autoload_threshold': 3})

    assert calls == [], 'the whole safety-net must be gated off by the enabled flag'
    assert not _failed_records(caplog)
    assert _counters() == before
