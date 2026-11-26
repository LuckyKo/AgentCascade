"""BUG_0424 — closed-enum tool-call failure taxonomy (`failure_class`).

Pins that a FAILED tool_call telemetry record carries a `failure_class` drawn from
the closed `TOOL_FAILURE_CLASSES` enum — derived from the error string by the pure
classifier, unless a caller overrides it with a valid enum member. Additive contract:
success-path records must NOT carry the field, and the pre-existing `success`/`error`
fields are unchanged.

See the plan: N:\\work\\WD\\AgentWorkspace\\plans\\BUG_0424_tool_failure_class_plan.md
"""

import pytest

from agent_cascade.telemetry import (
    TOOL_FAILURE_CLASSES,
    TelemetryCollector,
    _classify_tool_failure,
)


@pytest.fixture
def collector(tmp_path):
    """A fresh TelemetryCollector writing into an isolated temp dir."""
    return TelemetryCollector(log_dir=str(tmp_path), instance_id='bug0424')


def _drive_tool_call(collector, *, success=True, error='', failure_class=None):
    """Record one tool call (start then end) and return the single captured event.

    ``record_tool_call_start`` is required so ``record_tool_call_end`` does not
    early-return on its ``call is None and latency_ms is None`` guard. ``_write_event``
    is the single write chokepoint, so spying on it captures exactly the emitted event.
    """
    events = []
    collector._write_event = lambda ev: events.append(ev)

    collector.record_tool_call_start('bug0424', 'the_tool')
    if failure_class is not None:
        collector.record_tool_call_end('bug0424', 'the_tool', success=success,
                                       error=error, failure_class=failure_class)
    else:
        collector.record_tool_call_end('bug0424', 'the_tool', success=success,
                                       error=error)

    assert len(events) == 1
    return events[0]


@pytest.mark.parametrize('error,expected', [
    ('Request timed out after 30s', 'timeout'),
    ("Invalid argument: 'foo' is not a valid int", 'bad-args'),
    ('File not found: /x/y', 'not-found'),
    ('Permission denied', 'permission'),
    ('HTTP 503 service unavailable', 'transient-5xx'),
    ('Something exploded', 'other'),
])
def test_failure_class_per_error_class(collector, error, expected):
    ev = _drive_tool_call(collector, success=False, error=error)
    assert ev['failure_class'] == expected


def test_failure_class_is_always_a_closed_enum_member(collector):
    ev = _drive_tool_call(collector, success=False, error='Something exploded')
    assert ev['failure_class'] in TOOL_FAILURE_CLASSES


def test_transient_5xx_checked_before_timeout():
    # "504 Gateway Timeout" must be transient-5xx, not timeout — the ordering the
    # plan calls out, exercised directly against the pure classifier.
    assert _classify_tool_failure('504 Gateway Timeout') == 'transient-5xx'


def test_explicit_override_wins(collector):
    ev = _drive_tool_call(collector, success=False, error='boom', failure_class='timeout')
    assert ev['failure_class'] == 'timeout'


def test_invalid_override_falls_back_to_classifier(collector):
    ev = _drive_tool_call(collector, success=False, error='Request timed out',
                          failure_class='bogus')
    assert ev['failure_class'] == 'timeout'


def test_success_path_has_no_failure_class(collector):
    ev = _drive_tool_call(collector, success=True, error='')
    assert 'failure_class' not in ev


def test_additive_safety_existing_fields_unchanged(collector):
    # The new field rides alongside the pre-existing success/error signal.
    ev = _drive_tool_call(collector, success=False, error='Request timed out after 30s')
    assert ev['success'] is False
    assert ev['error'] == 'Request timed out after 30s'
    assert ev['failure_class'] == 'timeout'
