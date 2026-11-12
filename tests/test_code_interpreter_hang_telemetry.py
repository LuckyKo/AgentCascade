"""Tests for BUG_0289: code-interpreter hang triage enrichment + hung-by-reason counter.

Two surfaces are pinned:

1. The enriched WARNING lines (timeout escalation and the kernel watchdog) must carry the
   triage fields (kernel id, idle seconds, last toucher, code fingerprint, partial-output
   length). The timeout line is emitted from ``_log_timeout_escalation`` (split out of
   ``CodeInterpreter.call`` so it is testable without a live kernel, matching this module's
   pure-helper test convention); the watchdog line is emitted from the real
   ``_kernel_watchdog`` loop, which is driven for exactly one tick.
2. The module-global ``code_interp_hung_by_reason`` counter increments per reason and is
   rendered by the ``system_info`` ``help='telemetry'`` dump — same contract as the
   BUG_0284 autoload counter (rendered from the live dict, exact-parsed, always shown).

No Docker or live kernel is required. The counter is read live (not snapshot-copied), so a
mutation followed by a fresh render must reflect the new value.
"""

import logging
import os
import sys
import time

import pytest

_REPO = os.environ.get('AGENTCASCADE_REPO', r'N:\work\WD\AgentCascade')
sys.path.insert(0, _REPO)

import agent_cascade.tools.code_interpreter as ci  # noqa: E402
from agent_cascade.tools.code_interpreter import (  # noqa: E402
    code_interp_hung_by_reason as _COUNTER,
    _code_fingerprint,
    _kernel_triage,
    _kernel_watchdog,
    _log_timeout_escalation,
    _record_code_interp_hang,
    _record_kernel_touch,
)
from agent_cascade.tools.custom.system_info import SystemInfo  # noqa: E402
from tests.slot_test_helpers import app_logger_names  # noqa: E402

_KEYS = ('idle_reap', 'exec_timeout', 'stuck')
_HEADER = 'Code interpreter hangs by reason:'


# ── helpers ──────────────────────────────────────────────────────────────────────

def _seed_activity(kernel_id, **overrides):
    """Put a minimal ``_KERNEL_ACTIVITY`` entry the watchdog/triage code can read."""
    act = {
        'last_active': time.time(),
        'work_dir': '.',
        'watchdog_timeout': ci.CONTAINER_WATCHDOG_TIMEOUT,
        'stale_ttl': ci.STALE_CONTAINER_TTL,
    }
    act.update(overrides)
    ci._KERNEL_ACTIVITY[kernel_id] = act
    return act


def _cleanup(kernel_id):
    ci._KERNEL_ACTIVITY.pop(kernel_id, None)
    ci._WATCHDOG_KILLED.discard(kernel_id)


def _raise_levels(caplog):
    """Raise every app-logger name to WARNING so the enriched lines are captured.

    The emitting logger is the shared ``agent_cascade_logger`` (possibly suffixed per
    instance) — see [[app-logger-no-basicconfig-slot-logger-blindness]]; targeting the
    importing module path would capture nothing.
    """
    for name in app_logger_names():
        caplog.set_level(logging.WARNING, logger=name)


def _restore_levels(caplog):
    for name in app_logger_names():
        caplog.set_level(logging.NOTSET, logger=name)


def _field(line, name):
    """Extract the value of ``name=<value>`` from a log line (space-delimited)."""
    for tok in line.split():
        if tok.startswith(name + '='):
            return tok.split('=', 1)[1]
    return None


class _OneShotTerminate:
    """Stand-in for ``_WATCHDOG_TERMINATE`` that lets the loop run exactly one tick.

    ``while not is_set(): wait(); <body>`` — first check False (run body once), the body's
    ``wait()`` flips the flag so the next check exits. The background watchdog thread is
    blocked in the *real* event's ``wait(5)`` during the sub-second test window, so it does
    not see this object; monkeypatch restores the real event at teardown.
    """

    def __init__(self):
        self._ran = False

    def is_set(self):
        return self._ran

    def wait(self, timeout=None):
        self._ran = True
        return True


# ── 1. helper units: fingerprint / touch record / triage snapshot ────────────────

class TestHangHelpers:
    def test_fingerprint_keeps_first_line_and_appends_hash(self):
        fp = _code_fingerprint('import pandas as pd\nprint(1)')
        assert fp.startswith('import pandas as pd')
        # 8-char hex hash in brackets at the end
        assert fp.endswith(']')
        digest = fp.rpartition('…[')[2][:-1]
        assert len(digest) == 8 and all(c in '0123456789abcdef' for c in digest)

    def test_fingerprint_truncates_long_first_line(self):
        fp = _code_fingerprint('x' * 200 + '\nsecond line')
        head = fp.split('…')[0]
        assert len(head) == 60

    def test_fingerprint_empty_code_is_marked(self):
        assert _code_fingerprint('').startswith('<empty>')

    def test_record_touch_updates_activity_then_triage_reads_it(self):
        kid = 'ci_T_touch'
        act = _seed_activity(kid, last_active=time.time() - 10.0)
        try:
            _record_kernel_touch(kid, 'Maine', 'x = 1\nprint(x)')
            assert act['last_touched_by'] == 'Maine'
            assert act['last_code_fingerprint'].startswith('x = 1')
            tri = _kernel_triage(kid)
            assert tri['last_touched_by'] == 'Maine'
            assert tri['code_fp'].startswith('x = 1')
            assert 9.0 < tri['idle_for_s'] < 11.0
        finally:
            _cleanup(kid)

    def test_record_touch_is_noop_without_activity_entry(self):
        """No live kernel → must not create a partial record the watchdog would choke on."""
        kid = 'ci_T_noact'
        assert kid not in ci._KERNEL_ACTIVITY
        _record_kernel_touch(kid, 'X', 'code')  # must not raise
        assert kid not in ci._KERNEL_ACTIVITY

    def test_triage_defaults_when_kernel_missing(self):
        tri = _kernel_triage('ci_T_missing')
        assert tri == {'idle_for_s': '?', 'last_touched_by': '?', 'code_fp': '?'}


# ── 2. enriched timeout WARNING line + counter increment ───────────────────────

class TestTimeoutWarning:
    def test_fields_present_and_counter_bumped(self, caplog, monkeypatch):
        for k in _KEYS:
            monkeypatch.setitem(_COUNTER, k, 0)
        kid = 'ci_T_timeout'
        act = _seed_activity(kid)
        act['last_active'] = time.time() - 42.0
        act['last_touched_by'] = 'Coder'
        act['last_code_fingerprint'] = 'import os…deadbeef'
        try:
            _raise_levels(caplog)
            before = _COUNTER['exec_timeout']
            _log_timeout_escalation(kid, 120, 'hello partial')  # len == 13

            line = next(
                r.getMessage() for r in caplog.records if 'execution timed out' in r.getMessage()
            )
            assert f'kernel={kid}' in line
            assert 'last_touched_by=Coder' in line
            assert 'code_fp=import os…deadbeef' in line
            assert 'partial_output_len=13' in line
            idle = _field(line, 'idle_for_s')
            assert idle is not None and 40.0 < float(idle) < 45.0
            assert _COUNTER['exec_timeout'] == before + 1
        finally:
            _restore_levels(caplog)
            _cleanup(kid)

    def test_triage_fields_default_when_kernel_missing(self, caplog, monkeypatch):
        """A timeout on a kernel with no activity entry still logs a well-formed line."""
        kid = 'ci_T_timeout_noact'
        assert kid not in ci._KERNEL_ACTIVITY
        try:
            _raise_levels(caplog)
            _log_timeout_escalation(kid, 30, '')
            line = next(
                r.getMessage() for r in caplog.records if 'execution timed out' in r.getMessage()
            )
            assert f'kernel={kid}' in line
            assert 'idle_for_s=?' in line
            assert 'last_touched_by=?' in line
            assert 'partial_output_len=0' in line
        finally:
            _restore_levels(caplog)


# ── 3. enriched watchdog WARNING line + counter increment ──────────────────────

class TestWatchdogWarning:
    def test_fields_present_and_counter_bumped(self, caplog, monkeypatch):
        for k in _KEYS:
            monkeypatch.setitem(_COUNTER, k, 0)
        kid = 'ci_T_watchdog'
        _seed_activity(
            kid,
            watchdog_timeout=1,
            last_active=time.time() - 305.0,
            last_touched_by='Maine',
            last_code_fingerprint='sleep(1000)…cafebabe',
        )
        # Run the real watchdog loop for exactly one tick, and don't let the (harmless)
        # stale-container sweep touch global state.
        monkeypatch.setattr(ci, '_WATCHDOG_TERMINATE', _OneShotTerminate())
        monkeypatch.setattr(ci, '_cleanup_stale_containers', lambda now: None)
        try:
            _raise_levels(caplog)
            before = _COUNTER['idle_reap']
            _kernel_watchdog()

            # The background watchdog thread may or may not have grabbed the entry first;
            # either way the line is captured process-wide and the counter rose by >= 1.
            line = next(
                r.getMessage() for r in caplog.records
                if f'Kernel {kid} inactive for' in r.getMessage()
            )
            assert 'last_touched_by=Maine' in line
            assert 'code_fp=sleep(1000)…cafebabe' in line
            assert 'threshold 1s' in line  # threshold now reported separately from idle
            idle = _field(line, 'idle_for_s') if 'idle_for_s=' in line else None
            # idle is reported inline (seconds before "(threshold"); just sanity-check the
            # actual idle value shows up in the line, not the stale 300s threshold alone.
            assert '305' in line or idle is not None
            assert _COUNTER['idle_reap'] >= before + 1
        finally:
            _restore_levels(caplog)
            _cleanup(kid)


# ── 4. counter → system_info telemetry (mirrors BUG_0284 autoload counter) ─────

class _FakeTelemetry:
    def get_session_summary(self):
        return {'total_runs': 1, 'total_llm_calls': 0}

    def get_config_comparison(self):
        return []

    def get_agent_class_summary(self):
        return []

    def get_skill_usage_summary(self):
        return []


class _FakePool:
    def __init__(self):
        self.server_info = ('0.0.0.0', 8765)
        self.instance_conversations = {}
        self.operation_manager = None
        self.get_instance = lambda name: None
        self.get_conversation = lambda name: []
        self.telemetry = _FakeTelemetry()


def _dump():
    return SystemInfo(agent_pool=_FakePool()).call('{"help": "telemetry"}')


def _hang_counts(out):
    """Parse the rendered 'reason=N' line back to {reason: int}; fails loudly if absent."""
    for line in out.splitlines():
        if all(f'{key}=' in line for key in _KEYS):
            return {kv.split('=')[0]: int(kv.split('=')[1])
                    for kv in line.split() if '=' in kv}
    raise AssertionError(f'hung-by-reason line not found in dump:\n{out}')


class TestHangCounterTelemetry:
    def test_section_present_with_all_keys_at_zero(self, monkeypatch):
        """All-zero renders: a flat run of zeros is the healthy signal."""
        for k in _KEYS:
            monkeypatch.setitem(_COUNTER, k, 0)
        out = _dump()
        assert _HEADER in out
        assert _hang_counts(out) == {k: 0 for k in _KEYS}

    def test_section_reflects_current_values(self, monkeypatch):
        for k in _KEYS:
            monkeypatch.setitem(_COUNTER, k, 0)
        monkeypatch.setitem(_COUNTER, 'exec_timeout', 7)
        monkeypatch.setitem(_COUNTER, 'idle_reap', 3)
        out = _dump()
        assert _hang_counts(out) == {'exec_timeout': 7, 'idle_reap': 3, 'stuck': 0}

    def test_render_reflects_production_increment(self, monkeypatch):
        """A counter bump via the production helper shows up in a fresh render (read live)."""
        for k in _KEYS:
            monkeypatch.setitem(_COUNTER, k, 0)
        assert _hang_counts(_dump()) == {k: 0 for k in _KEYS}
        _record_code_interp_hang('idle_reap')
        _record_code_interp_hang('idle_reap')
        assert _hang_counts(_dump()) == {'exec_timeout': 0, 'idle_reap': 2, 'stuck': 0}

    def test_section_omitted_on_render_failure(self, monkeypatch):
        """A read/import failure degrades only this section, never blanks the dump."""
        monkeypatch.setattr(SystemInfo, '_render_code_interp_hangs', lambda self: '')
        out = _dump()
        assert _HEADER not in out
        assert 'AC Telemetry Dump (live)' in out


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-n', '0', '-v']))
