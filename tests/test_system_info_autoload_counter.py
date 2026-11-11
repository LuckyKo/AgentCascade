"""Unit tests for the skill auto-load failure counter in the system_info telemetry dump.

The counter ``skill_autoload_failed_by_reason`` lives as a module-level dict in
``agent_cascade.memory_hint.manager`` (BUG_0284). These tests prove it is read live by
the ``help='telemetry'`` dump without changing the counter or the auto-load path.

The dump renders the counter directly from the global dict (not a frozen copy), so a
mutation followed by a fresh render must reflect the new value — that is the signal the
value "lands", not merely that the code path ran. Values are parsed back out of the dump
and compared exactly (no substring matching, which would mis-match e.g. ``2`` vs ``20``).
"""

from agent_cascade.memory_hint.manager import skill_autoload_failed_by_reason as _COUNTER
from agent_cascade.tools.custom.system_info import SystemInfo

_KEYS = ('skill_not_found', 'disabled', 'exception')
_HEADER = 'Skill autoload failures:'


class _FakeTelemetry:
    """Minimal telemetry collector: enough for _render_telemetry_dump to run.

    get_cache_attribution is intentionally omitted — the dump hasattr-guards it, so a
    collector without it must still render (proving the new section is independent).
    """

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


def _set(monkeypatch, values):
    """Pin every counter key to a deterministic value; restored by monkeypatch."""
    for key in _KEYS:
        monkeypatch.setitem(_COUNTER, key, values.get(key, 0))


def _dump():
    return SystemInfo(agent_pool=_FakePool()).call('{"help": "telemetry"}')


def _autoload_counts(out):
    """Parse the rendered 'reason=N' line back into {reason: int}; fails if absent.

    Exact-parsing the values rather than substring-matching means an extra or renamed
    key surfaces as a dict-shape mismatch (a loud drift signal), and equal digits in
    different fields never produce a false positive.
    """
    for line in out.splitlines():
        if all(f'{key}=' in line for key in _KEYS):
            return {kv.split('=')[0]: int(kv.split('=')[1])
                    for kv in line.split() if '=' in kv}
    raise AssertionError(f'autoload counter line not found in dump:\n{out}')


class TestSystemInfoAutoloadCounter:
    def test_counter_section_present_with_all_keys(self, monkeypatch):
        """The section appears even at all-zero — a flat run of zeros is the healthy signal."""
        _set(monkeypatch, {k: 0 for k in _KEYS})
        out = _dump()
        assert _HEADER in out
        assert _autoload_counts(out) == {k: 0 for k in _KEYS}

    def test_counter_reflects_current_values(self, monkeypatch):
        """Distinct per-reason values render exactly as set (no clobbering, no freeze)."""
        _set(monkeypatch, {'skill_not_found': 5, 'disabled': 2, 'exception': 1})
        out = _dump()
        assert _HEADER in out
        assert _autoload_counts(out) == {
            'skill_not_found': 5, 'disabled': 2, 'exception': 1,
        }

    def test_counter_updates_after_increment(self, monkeypatch):
        """A later render reflects an incremented counter — read live, not snapshot-copied."""
        _set(monkeypatch, {k: 0 for k in _KEYS})
        assert _autoload_counts(_dump()) == {k: 0 for k in _KEYS}

        # Mutate the real module-global dict the dump imports, exactly as the auto-load
        # path does (increment). setitem keeps the restore contract explicit and local,
        # so the test never leaks state even if _KEYS changes.
        monkeypatch.setitem(_COUNTER, 'disabled', _COUNTER['disabled'] + 1)
        monkeypatch.setitem(_COUNTER, 'skill_not_found', _COUNTER['skill_not_found'] + 2)
        assert _autoload_counts(_dump()) == {
            'skill_not_found': 2, 'disabled': 1, 'exception': 0,
        }

    def test_section_omitted_on_render_failure(self, monkeypatch):
        """A memory-hint read failure degrades this section to absent, never blanks the dump."""
        monkeypatch.setattr(
            SystemInfo, '_render_skill_autoload_failures',
            lambda self: '',  # simulate the helper's own except branch returning ''
        )
        out = _dump()
        assert _HEADER not in out
        # The rest of the dump is intact.
        assert 'AC Telemetry Dump (live)' in out
