"""Regression tests for BUG_0285: spawn brief size warning.

The guard lives in ``AgentLifecycleManager.build_task_message`` (the single point
where ``context`` and ``task`` are assembled into the sub-agent's first message).
It is PURE OBSERVABILITY: when the raw ``context`` or ``task`` param passed to
``call_agent`` exceeds ``_SPAWN_BRIEF_SIZE_WARN`` chars, it logs a ``warning``.
No truncation, no rejection — the returned Message must be built identically.

These tests pin both directions of that contract:
  * oversized context / oversized task → warning emitted,
  * context at/below threshold → no warning,
  * warning never changes the returned message content.
"""

import logging

import pytest

from agent_cascade.lifecycle_manager import AgentLifecycleManager, _SPAWN_BRIEF_SIZE_WARN

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


def _oversize_warnings(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and 'spawn_brief_oversized' in r.getMessage()]


def test_oversized_context_triggers_warning(caplog):
    oversized = 'c' * (_SPAWN_BRIEF_SIZE_WARN + 1)
    with caplog.at_level(logging.WARNING, logger=_APP_LOGGER):
        _mgr().build_task_message({'task': 'do it', 'context': oversized}, 'caller')
    warnings = _oversize_warnings(caplog)
    assert len(warnings) == 1, f"expected exactly one oversize warning, got {warnings}"


def test_context_at_or_below_threshold_does_not_warn(caplog):
    # Exactly at the threshold (not >) must stay silent — boundary check.
    at_threshold = 'c' * _SPAWN_BRIEF_SIZE_WARN
    with caplog.at_level(logging.WARNING, logger=_APP_LOGGER):
        _mgr().build_task_message({'task': 'do it', 'context': at_threshold}, 'caller')
    assert _oversize_warnings(caplog) == []


def test_oversized_task_triggers_warning(caplog):
    oversized = 't' * (_SPAWN_BRIEF_SIZE_WARN + 1)
    with caplog.at_level(logging.WARNING, logger=_APP_LOGGER):
        _mgr().build_task_message({'task': oversized, 'context': 'short'}, 'caller')
    assert len(_oversize_warnings(caplog)) == 1


def test_warning_is_pure_observability_message_unchanged(caplog):
    """The warning must not truncate or otherwise alter the returned message."""
    big_context = 'C' * (_SPAWN_BRIEF_SIZE_WARN + 100)
    big_task = 'T' * (_SPAWN_BRIEF_SIZE_WARN + 200)
    args = {'task': big_task, 'context': big_context}

    with caplog.at_level(logging.WARNING, logger=_APP_LOGGER):
        mgr = _mgr()
        first = mgr.build_task_message(dict(args), 'caller')
        second = mgr.build_task_message(dict(args), 'caller')

    # Exactly one warning per call, and none of them may have suppressed content.
    assert len(_oversize_warnings(caplog)) == 2

    content = first.content
    # The documented, unmodified wrapper format.
    assert content.startswith('Context: This is a message from caller.')
    assert content.endswith('\n\nPlease help with this task.')
    # No truncation: the raw context and task are present verbatim.
    assert big_context in content
    assert big_task in content
    # Deterministic: warning introduces no state/mutation, identical calls match.
    assert first.content == second.content


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
