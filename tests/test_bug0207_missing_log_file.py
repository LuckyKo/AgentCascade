"""BUG_0207 regression: ``call_agent(log_file=...)`` with a MISSING log file must
surface a clear "Log file not found" error instead of silently starting a fresh
(empty) instance.

Root cause (see .agent_lessons / BUG_0207): a missing path was parsed as a JSONL
*string*, yielded zero messages, and the resulting generic
``Error: No valid messages found in log input.`` status was swallowed as a
``logger.warning`` in the resurrection branch — so an empty instance ran as if new.

The fix wires three coordinated pieces:
  1. ``pool/session_io.py`` ``load_session_from_log`` — a missing path returns a
     distinct, path-inclusive ``Error: Log file not found: <resolved_path>``.
  2. ``lifecycle_manager.py`` ``find_or_create_instance`` — that status now raises
     ``LogFileNotFoundError`` (other load errors keep the warn-and-continue path).
  3. ``engine/system_agent.py`` ``_create_and_run_agent`` — converts the exception
     into the existing structured-rejection tuple, which ``run_child_core`` formats
     as ``[Agent 'X' Rejected]: ...``.

All tests are red-at-HEAD / green-after. No LLM required.
"""

import threading
from unittest.mock import MagicMock

import pytest

from agent_cascade.agent_pool import AgentPool
from agent_cascade.exceptions import LogFileNotFoundError
from agent_cascade.lifecycle_manager import AgentLifecycleManager
from agent_cascade.pool.session_io import SessionIOMixin
from agent_cascade.tool_dispatcher import ToolDispatcher

DUMMY_LLM_CFG = {
    'model': 'qwen/qwen3-4b',
    'model_server': 'http://127.0.0.1:1234/v1',
    'api_key': 'EMPTY',
    'model_type': 'qwenvl_oai',
}


# ── load_session_from_log: missing path + raw-JSON guard ─────────────────────

class TestLoadSessionFromLogMissingFile:

    def test_missing_path_returns_path_inclusive_error(self, tmp_path):
        """A missing path must yield the distinct, path-inclusive error — not the
        generic 'No valid messages found in log input.' (RED at HEAD)."""
        pool = AgentPool(DUMMY_LLM_CFG)
        missing = str(tmp_path / 'nope.jsonl')
        status = pool.load_session_from_log(missing)
        assert status.startswith('Error: Log file not found')
        assert missing in status, 'resolved/absolute path must be in the message'

    def test_raw_json_string_not_misdetected_as_path(self, tmp_path):
        """Guard: a raw JSON array with no messages must still yield the generic
        'No valid messages found' error, NOT the missing-file error."""
        pool = AgentPool(DUMMY_LLM_CFG)
        status = pool.load_session_from_log('[]')
        assert status == 'Error: No valid messages found in log input.'

    def test_inline_json_with_slash_in_content_not_misdetected(self, tmp_path):
        """A JSON string whose *content* contains a slash (e.g. a URL in a message
        body) must be parsed as JSON, not misrouted to the missing-file branch.
        This pins the parse-first ordering: the guard only fires when parsing
        yielded nothing."""
        pool = AgentPool(DUMMY_LLM_CFG)
        inline = '[{"role": "user", "content": "see https://example.com/a/b"}]'
        status = pool.load_session_from_log(inline)
        assert status.startswith('Error: Log file not found') is False, \
            f'slash-bearing JSON misdetected as a path: {status!r}'


# ── find_or_create_instance: raises on missing log_file ──────────────────────

class TestFindOrCreateRaisesOnMissingLog:

    def test_fresh_create_missing_log_raises(self, tmp_path):
        """RED at HEAD: a fresh create with a missing log_file returned a fresh
        instance; post-fix it raises LogFileNotFoundError."""
        pool = AgentPool(DUMMY_LLM_CFG)
        manager = AgentLifecycleManager(pool)
        missing = str(tmp_path / 'missing.jsonl')
        with pytest.raises(LogFileNotFoundError) as exc_info:
            manager.find_or_create_instance(
                agent_class='coder', instance_name='fresh_missing', caller=None,
                nest_depth=0, force_fresh=False, log_file=missing)
        assert exc_info.value.log_file == missing

    def test_existing_instance_missing_log_raises(self, tmp_path):
        """Documents the deliberate behavior change: a missing log_file on an
        EXISTING instance is now a hard error (was a no-op-ish warn before)."""
        pool = AgentPool(DUMMY_LLM_CFG)
        manager = AgentLifecycleManager(pool)
        name = 'existing_missing'
        # First create the instance (no log_file → no error).
        manager.find_or_create_instance(
            agent_class='coder', instance_name=name, caller=None,
            nest_depth=0, force_fresh=False, log_file=None)
        assert name in pool.instances, 'precondition: instance now exists'
        missing = str(tmp_path / 'missing.jsonl')
        with pytest.raises(LogFileNotFoundError):
            manager.find_or_create_instance(
                agent_class='coder', instance_name=name, caller=None,
                nest_depth=0, force_fresh=False, log_file=missing)


# ── handle_call_agent: surfaces the rejection string (FakePool) ───────────────


class _FakeCaller:
    """Minimal caller instance exposing only what handle_call_agent reads."""

    def __init__(self, name):
        self.instance_name = name
        self.agent_class = 'orchestrator'
        self._state_lock = threading.RLock()
        self._slot_release = None
        self._nest_depth = 0
        self.parent_instance = None


class FakePool:
    """Minimal pool exposing only what ``handle_call_agent`` + the real
    ``find_or_create_instance`` / ``load_session_from_log`` touch.

    Binds the REAL ``SessionIOMixin`` session-load methods so the missing-file
    guard is genuinely exercised (not a hand-rolled fallback double). The
    parse-first guard in ``load_session_from_log`` calls ``_parse_json_input``
    (and its static helpers), so those are bound too.
    """
    load_session_from_log = SessionIOMixin.load_session_from_log
    _parse_json_input = SessionIOMixin._parse_json_input
    # _parse_json_line / _extract_last_session are @staticmethod on SessionIOMixin;
    # re-wrap them so instance access does NOT inject `self` (a plain-function class
    # attribute would be re-bound as a method and receive an extra arg).
    _parse_json_line = staticmethod(SessionIOMixin._parse_json_line)
    _extract_last_session = staticmethod(SessionIOMixin._extract_last_session)

    def __init__(self):
        self.instances = {}
        self.instance_classes = {}
        self._execution = MagicMock()
        self._execution._state_lock = threading.RLock()
        self._execution.active_stack = []
        self.api_router = None
        self.settings = MagicMock()
        self.settings.max_nesting_depth = 10
        self.settings.allow_parallel_agents = False  # force the SYNC path
        self.templates = {'coder': MagicMock(), 'orchestrator': MagicMock()}
        self._pool_lock = threading.RLock()
        self._logger = MagicMock()
        self._logger.workspace_dir = None
        self.enqueued = []

    def get_template(self, name):
        if name in self.templates:
            return self.templates[name]
        low = (name or '').lower()
        for k, v in self.templates.items():
            if k.lower() == low:
                return v
        return None

    def list_agents(self):
        return list(self.templates.keys())

    def _resolve_instance_name(self, n, exclude=None):
        n = (n or '').strip()
        low = n.lower()
        for k in self.instances:
            if k.lower() == low:
                return k
        return n

    def get_instance(self, n):
        return self.instances.get((n or '').strip())

    def enqueue_message(self, name, text):
        with self._pool_lock:
            self.enqueued.append((name, text))

    def _update_child_relationship(self, caller, instance_name, add=True):
        pass

    def register_async_call(self, **kwargs):
        pass


def _make_engine(pool, manager):
    """An engine stub whose ``_create_and_run_agent`` is the REAL
    ``SystemAgentMixin`` method, so the ``LogFileNotFoundError`` → structured-
    rejection conversion is the production code path (not a mock)."""
    from agent_cascade.engine.system_agent import SystemAgentMixin
    engine = SystemAgentMixin.__new__(SystemAgentMixin)
    engine.lifecycle = manager
    engine._run_skill_advisor_gate = lambda *a, **k: (None, None, None)
    engine._release_slot = lambda *a, **k: None
    engine.reacquire_after_slot_yield = lambda *a, **k: None
    return engine


def _make_dispatcher(pool):
    d = ToolDispatcher(pool)
    d.set_engine(_make_engine(pool, AgentLifecycleManager(pool)))
    return d


class TestHandleCallAgentMissingLog:

    def test_missing_log_file_surfaces_rejection(self, tmp_path):
        """RED at HEAD: a missing log_file returned a fresh-instance completion;
        post-fix the tool result carries the 'Log file not found' rejection."""
        missing = str(tmp_path / 'missing.jsonl')
        pool = FakePool()
        caller = _FakeCaller('caller')
        d = _make_dispatcher(pool)
        result = d.handle_call_agent(
            args={'instance_name': 'worker1', 'agent_class': 'coder',
                  'task': 'test', 'log_file': missing},
            messages=[], instance=caller)
        assert 'Log file not found' in result
        assert 'Rejected' in result
        assert pool.enqueued == []
