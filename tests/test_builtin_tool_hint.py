"""BUG_0180 follow-up: deterministic "prefer built-in tool" hint.

A read-only shell command whose primary maps to a built-in tool (Get-Content→
read_file, dir→list_dir, Select-String→grep, ...) is auto-approved WITHOUT an
LLM guard review, emitting a ``[SECURITY_HINT]`` log line. These tests drive the
REAL ``SecurityAdvisorHandler._execute_check`` with a fake pool + fake engine so
the observable is the guard invocation count (must be 0 on a hint) and the
approval delivered to the operation manager.
"""

import threading
import types
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.security_handler import (
    _BUILTIN_TOOL_HINTS,
    _builtin_tool_hint,
    SecurityAdvisorHandler,
)

PROMPT_TEMPLATE = ('PROMPT tool={tool_name} desc={description} args={arguments} '
                   'os={os_info} ws={workspace_info}')


# ── Fakes (mirror test_security_approval_cache.py) ───────────────────────────


class _FakeOperationManager:
    """Records the verdicts the guard delivers (the observable effect)."""

    def __init__(self):
        self.base_dir = 'N:\\work\\WD\\AgentWorkspace'
        self.extra_work_folders_ro = []
        self.extra_work_folders_rw = []
        self.approvals = []
        self.rejections = []

    def user_approve(self, rid, reason=''):
        self.approvals.append((rid, reason))

    def user_reject(self, rid, message=''):
        self.rejections.append((rid, message))


class _FakePool:
    def __init__(self):
        self.operation_manager = _FakeOperationManager()
        self.instance_state = {}
        self._execution = MagicMock()
        self._execution._state_lock = threading.Lock()
        self.stopped = False
        self.active_stack_removed = []

    def get_instance(self, name):
        return None

    def get_template(self, name):
        return None

    def active_stack_remove(self, name):
        self.active_stack_removed.append(name)


def _make_ap(rid, command, tool_name='shell_cmd'):
    return {
        'request_id': rid,
        'tool_name': tool_name,
        'description': 'probe',
        'tool_args': {'command': command},
    }


def _make_handler(pool):
    app = type('App', (), {})()
    return SecurityAdvisorHandler(pool, {'session_name': 'Maine'}, app,
                                  MagicMock(), lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    """Each test starts from an empty verdict cache (process singleton)."""
    from agent_cascade.security_handler import _get_security_approval_cache
    cache = _get_security_approval_cache(None)
    cache.clear()
    yield cache
    cache.clear()


@pytest.fixture
def guard(monkeypatch):
    """Install a fake engine + stub LLM-adjacent helpers; count LLM runs."""
    counters = {'run': 0, 'engine_constructed': 0}
    verdict_text = '[YES] safe [END]'

    class _FakeEngine:
        def __init__(self, pool):
            counters['engine_constructed'] += 1

        def run(self, instance):
            counters['run'] += 1

            def _gen():
                yield ({'role': 'assistant', 'content': verdict_text}, False)
            return _gen()

        def _telemetry(self):
            return None

        def save_before_slot_yield(self, *args, **kwargs):
            pass

        @staticmethod
        def _discard_stale_permit(*args, **kwargs):
            pass

    monkeypatch.setattr('agent_cascade.execution_engine.ExecutionEngine', _FakeEngine)
    # No slot to yield in the fake pool → the fresh path skips the yield/reacquire.
    monkeypatch.setattr('agent_cascade.security_handler.yield_caller_slot',
                        lambda *a, **k: False)
    monkeypatch.setattr(
        'agent_cascade.security_handler.security_reuse.acquire_security_agent',
        lambda engine, **kw: (types.SimpleNamespace(conversation=[]),
                              False, kw.get('fallback_name', 'Security_x')))
    # Verdict text extraction + streaming broadcast: pure plumbing, not under test.
    monkeypatch.setattr('agent_cascade.compression.helpers.extract_instance_output',
                        lambda conv, key: verdict_text)
    monkeypatch.setattr('agent_cascade.api_integration.broadcast_stream_update',
                        lambda **kw: (0.0, 0))
    return counters


def _run(pool, guard, rid, ap, auto_apply=True):
    handler = _make_handler(pool)
    handler._execute_check(
        ap, None, rid, auto_apply, 'Maine', 'coder',
        PROMPT_TEMPLATE, 60.0, 3600.0,
    )
    return handler


# ── Mapping unit tests (the deterministic core) ───────────────────────────────


def test_hint_mapping_primary_to_tool():
    assert _builtin_tool_hint('Get-Content foo.txt') == 'read_file'
    assert _builtin_tool_hint('type foo.txt') == 'read_file'
    assert _builtin_tool_hint('dir /b x') == 'list_dir'
    assert _builtin_tool_hint('Get-ChildItem x') == 'list_dir'
    assert _builtin_tool_hint('ls x') == 'list_dir'
    assert _builtin_tool_hint('Select-String -Pattern p f') == 'grep'
    assert _builtin_tool_hint('findstr p f') == 'grep'
    assert _builtin_tool_hint('grep p f') == 'grep'


def test_hint_is_case_insensitive():
    assert _builtin_tool_hint('GET-CONTENT foo.txt') == 'read_file'
    assert _builtin_tool_hint('DIR /b x') == 'list_dir'
    assert _builtin_tool_hint('SELECT-STRING -Pattern p f') == 'grep'


def test_hint_none_for_unmapped_primary():
    # rm is not in the map (and not read-only) → no hint.
    assert _builtin_tool_hint('rm -rf /') is None
    # curl is arg-scoped, not hint-mapped → no hint.
    assert _builtin_tool_hint('curl https://x') is None


def test_hint_none_for_non_readonly():
    # find -exec is not strictly read-only → primary 'find' is mapped but no hint.
    assert _builtin_tool_hint("find . -name '*.tmp' -exec rm {} \\;") is None
    # A mapped primary with a redirect to a real file is a write → no hint (the
    # strict predicate rejects redirect targets, so it falls to the guard).
    assert _builtin_tool_hint('dir > out.txt') is None
    assert _builtin_tool_hint('Get-Content foo.txt > /etc/out') is None
    # ... but a redirect to an allowed sink (/dev/null) stays read-only → hint.
    assert _builtin_tool_hint('dir > /dev/null') == 'list_dir'
    # Empty / None command → no hint.
    assert _builtin_tool_hint('') is None
    assert _builtin_tool_hint(None) is None


def test_mapping_is_single_source_of_truth():
    # Every mapped key resolves to its tool through the hint function (which
    # also confirms each bare primary is structurally read-only).
    for primary, tool in _BUILTIN_TOOL_HINTS.items():
        assert _builtin_tool_hint(primary) == tool


# ── Integration: the hint path skips the LLM guard ────────────────────────────


def test_hint_path_skips_llm_and_auto_approves(guard):
    pool = _FakePool()
    ap = _make_ap('op_hint_gc', 'Get-Content foo.txt')
    _run(pool, guard, 'op_hint_gc', ap)
    assert guard['run'] == 0              # no LLM review
    assert guard['engine_constructed'] == 0  # fast path never builds an engine
    assert len(pool.operation_manager.approvals) == 1
    rid, reason = pool.operation_manager.approvals[0]
    assert rid == 'op_hint_gc'
    assert 'read_file' in reason          # the hint names the preferred tool
    assert '[SECURITY_HINT]' in reason


def test_hint_path_for_dir_and_select_string(guard):
    pool = _FakePool()
    _run(pool, guard, 'op_hint_dir', _make_ap('op_hint_dir', 'dir /b x'))
    _run(pool, guard, 'op_hint_ss', _make_ap('op_hint_ss', 'Select-String -Pattern p f'))
    assert guard['run'] == 0
    assert [r for r, _ in pool.operation_manager.approvals] == ['op_hint_dir', 'op_hint_ss']
    for _rid, reason in pool.operation_manager.approvals:
        assert 'list_dir' in reason or 'grep' in reason


def test_non_shell_tool_falls_through_to_llm(guard):
    # A non-shell tool is not hint-eligible → the LLM guard runs.
    pool = _FakePool()
    ap = _make_ap('op_hint_edit', 'x', tool_name='edit_file')
    _run(pool, guard, 'op_hint_edit', ap)
    assert guard['run'] == 1  # LLM guard ran (no hint for edit_file)


def test_non_readonly_shell_falls_through_to_llm(guard):
    # A mapped primary that is NOT structurally read-only → no hint → LLM runs.
    pool = _FakePool()
    ap = _make_ap('op_hint_findexec', "find . -name '*.tmp' -exec rm {} \\;")
    _run(pool, guard, 'op_hint_findexec', ap)
    assert guard['run'] == 1  # no hint → the LLM guard ran
    # The fake LLM yields [YES] → approved via the normal LLM path (not the hint).
    assert pool.operation_manager.approvals == [('op_hint_findexec', 'safe')]


def test_redirect_to_file_falls_through_to_llm(guard):
    # A mapped primary with a redirect to a real file is a write (the strict
    # predicate rejects it) → no hint → the LLM guard runs.
    pool = _FakePool()
    ap = _make_ap('op_hint_redir', 'dir > out.txt')
    _run(pool, guard, 'op_hint_redir', ap)
    assert guard['run'] == 1  # no hint → the LLM guard ran
    assert pool.operation_manager.approvals == [('op_hint_redir', 'safe')]
