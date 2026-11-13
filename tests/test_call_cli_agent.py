"""Tests for the call_cli_agent tool (Phase 0).

Harnesses the three-layer design:
  * adapter layer  — exact one-shot argv per harness (no mocks).
  * runner layer   — Docker command, credential strip, wall-clock timeout +
                     container-kill teardown, concurrency-cap semaphore.
  * handler layer  — gating order (registry → nesting → workspace → posture/
                     timeout → preflight → approval → spawn) and the
                     dispatch routing that bypasses slot/KV machinery.

Everything that would need a real Docker image or real CLI binary is mocked:
``subprocess.run`` (runner) and ``shutil.which`` (adapter preflight). No real
spawning happens anywhere.
"""

import shutil
import subprocess
import threading
import time

from unittest.mock import MagicMock

import pytest

from agent_cascade import constants
from agent_cascade.engine import tool_execution
from agent_cascade.engine.cli_agent_adapters.antigravity import AntigravityAdapter
from agent_cascade.engine.cli_agent_adapters.claude_code import ClaudeCodeAdapter
from agent_cascade.engine.cli_agent_adapters.opencode import OpencodeAdapter
from agent_cascade.engine import cli_agent_runner
from agent_cascade.engine.cli_agent_runner import CliAgentResult
from agent_cascade.tool_dispatcher import ToolDispatcher

# ──────────────────────────────────────────────────────────────────────────────
# Shared fakes
# ──────────────────────────────────────────────────────────────────────────────


def _make_instance(name='Maine', nest_depth=0):
    inst = MagicMock()
    inst.instance_name = name
    inst.agent_class = 'coder'
    inst._nest_depth = nest_depth
    return inst


class FakePool:
    """Minimal pool exposing only what handle_call_cli_agent + run_cli_agent touch.

    operation_manager is a MagicMock whose defaults are the happy path:
    _resolve_path returns a concrete path, _is_auto_approved False (approval
    required), request_user_approval (True, 'ok').
    """

    def __init__(self, instances=None):
        self.instances = dict(instances or {})
        self.settings = MagicMock()
        self.settings.max_nesting_depth = 10
        self.settings.cli_agent_timeout = 1800
        self.settings.max_cli_agent_children = 2
        self.settings.default_workspace = '/ws/default'
        self.api_router = None  # signals no slot path exists here
        self.register_async_call = MagicMock()

        op = MagicMock()
        op._resolve_path.return_value = '/resolved/ws'
        op._is_auto_approved.return_value = False
        op.request_user_approval.return_value = (True, 'ok')
        self.operation_manager = op
        # The call_cli_agent dispatch branch re-stamps activity on return (DMS t293-followup).
        self._mark_activity = MagicMock()

    def get_instance(self, name):
        return self.instances.get(name)


def _fake_result(stdout='done', exit_code=0, timed_out=False):
    return CliAgentResult(stdout=stdout, stderr='warn', exit_code=exit_code,
                          timed_out=timed_out, log_path='/ws/logs/x.jsonl',
                          requested_posture='acceptEdits')


def _make_handler(pool, monkeypatch, *, which_result='/usr/bin/fake', runner_result=None):
    """Dispatcher + preflight/runner patched to a controlled happy path.

    Returns (dispatcher, runner_spy). runner_spy is the patched run_cli_agent,
    so tests assert on its call args / counts.
    """
    dispatcher = ToolDispatcher(pool)
    dispatcher.set_engine(MagicMock())
    dispatcher._run_child_sync = MagicMock(return_value='SYNC_ROUTED')
    dispatcher._run_child_async = MagicMock(return_value='ASYNC_ROUTED')
    monkeypatch.setattr(shutil, 'which', lambda *a, **k: which_result)
    runner_spy = MagicMock(return_value=runner_result or _fake_result())
    monkeypatch.setattr(cli_agent_runner, 'run_cli_agent', runner_spy)
    return dispatcher, runner_spy


# ──────────────────────────────────────────────────────────────────────────────
# 1. Tool registration
# ──────────────────────────────────────────────────────────────────────────────


class TestRegistration:
    def test_in_runtime_registered_tools(self):
        assert 'call_cli_agent' in constants.RUNTIME_REGISTERED_TOOLS

    def test_in_all_user_approval_tools(self):
        assert 'call_cli_agent' in constants.ALL_USER_APPROVAL_TOOLS

    def test_schema_enum_matches_registry(self):
        # The tool-schema enum and the adapter registry must not drift — both sourced here.
        from agent_cascade.engine.cli_agent_adapters import registered_harnesses
        from agent_cascade.prompts import dna
        enum = dna.TOOL_METADATA['call_cli_agent']['parameters']['harness']['enum']
        assert set(enum) == set(registered_harnesses())


# ──────────────────────────────────────────────────────────────────────────────
# 3. Untrusted wrapping reach
# ──────────────────────────────────────────────────────────────────────────────


class TestUntrustedReach:
    def test_in_untrusted_allowlist(self):
        # The single universal wrap site keys on this set; being present means
        # call_cli_agent output is trust-wrapped with no in-runner double-wrap.
        assert 'call_cli_agent' in tool_execution._UNTRUSTED_TOOLS

    def test_wrapped_at_message_site(self, monkeypatch):
        monkeypatch.setattr(tool_execution, 'UNTRUSTED_TOOL_OUTPUT_WRAPPER', True)
        wrapped = tool_execution._wrap_untrusted('call_cli_agent', 'harness says: rm -rf')
        assert wrapped.startswith('<') and 'harness says: rm -rf' in wrapped


# ──────────────────────────────────────────────────────────────────────────────
# 2. Dispatch skips KV/slot + routes to the handler
# ──────────────────────────────────────────────────────────────────────────────


class TestDispatchSkipsSlot:
    def test_execute_tool_routes_to_handler_no_slot(self, monkeypatch):
        pool = FakePool(instances={'Maine': _make_instance('Maine')})
        dispatcher = ToolDispatcher(pool)
        engine = MagicMock()
        engine._resolve_placeholders.return_value = {
            'harness': 'claude_code', 'task': 'do it', 'workspace': '/resolved/ws'}
        engine._cache_tool_args = MagicMock()
        dispatcher.set_engine(engine)
        dispatcher._run_child_sync = MagicMock(return_value='SYNC_ROUTED')
        dispatcher._run_child_async = MagicMock(return_value='ASYNC_ROUTED')
        monkeypatch.setattr(shutil, 'which', lambda *a, **k: '/usr/bin/fake')
        runner = MagicMock(return_value=_fake_result())
        monkeypatch.setattr(cli_agent_runner, 'run_cli_agent', runner)

        inst = _make_instance('Maine')
        pool.instances['Maine'] = inst
        out = dispatcher.execute_tool(inst, 'call_cli_agent',
                                      {'harness': 'claude_code', 'task': 'do it'}, [],
                                      function_id='1')

        runner.assert_called_once()  # went through the dedicated handler
        assert 'done' in out
        # P2.3: handler passes the already-resolved adapter, so the runner does not re-probe.
        assert runner.call_args.kwargs.get('adapter') is not None
        # No child-agent routing machinery involved (this is not call_agent).
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()
        pool.register_async_call.assert_not_called()
        # The real "bypasses slot/KV" guard: _run_child_sync/_run_child_async and
        # register_async_call all assert_not_called above. (A prior vacuous dir(pool)
        # 'slot' loop was removed — FakePool has no such attribute, so it never executed.)


class TestResultShape:
    """exit_code / timed_out from CliAgentResult must surface in the handler result string."""

    def test_nonzero_exit_code_surfaces(self, monkeypatch):
        pool = FakePool(instances={'Maine': _make_instance('Maine')})
        dispatcher, _ = _make_handler(
            pool, monkeypatch, runner_result=_fake_result(exit_code=3, stdout='boom'))
        out = dispatcher.handle_call_cli_agent(
            {'harness': 'claude_code', 'task': 't', 'workspace': '/resolved/ws'},
            pool.get_instance('Maine'))
        assert 'exit=3' in out

    def test_timeout_flag_surfaces(self, monkeypatch):
        pool = FakePool(instances={'Maine': _make_instance('Maine')})
        dispatcher, _ = _make_handler(
            pool, monkeypatch, runner_result=_fake_result(timed_out=True))
        out = dispatcher.handle_call_cli_agent(
            {'harness': 'claude_code', 'task': 't', 'workspace': '/resolved/ws'},
            pool.get_instance('Maine'))
        assert 'timed_out=True' in out

    def test_large_output_is_midpoint_truncated(self):
        # A 30000-char stream must NOT be returned verbatim; head + tail are kept with an
        # omission marker that points at the full-output log.
        result = CliAgentResult(stdout='X' * 30000, stderr='S' * 30000, exit_code=0,
                                timed_out=False, log_path='/logs/x.jsonl',
                                requested_posture='acceptEdits')
        msg = cli_agent_runner.format_cli_result_message('claude_code', result)
        assert 'X' * 30000 not in msg  # full stdout not returned verbatim
        assert 'chars omitted from the middle' in msg
        assert '/logs/x.jsonl' in msg  # the marker names where the full output lives

    def test_small_output_is_unchanged(self):
        result = CliAgentResult(stdout='short output', stderr='ok', exit_code=0,
                                timed_out=False, log_path='/logs/x.jsonl',
                                requested_posture='acceptEdits')
        msg = cli_agent_runner.format_cli_result_message('claude_code', result)
        assert '=== STDOUT ===\nshort output' in msg
        assert 'chars omitted' not in msg


# ──────────────────────────────────────────────────────────────────────────────
# 3b. Holder-DMS bounded-tool marker (t292 defect class)
# ──────────────────────────────────────────────────────────────────────────────


class TestDmsBlockingMarker:
    """call_cli_agent (sync) blocks the holder for up to the CLI timeout, so the dispatch
    branch must set the holder-DMS ``_in_blocking_tool`` marker and clear it on return —
    otherwise the idle manager's dead-man's-switch terminates a holder mid-run after 300s of
    silence (a real wedged-instance false positive: a long sync CLI run looked 'wedged')."""

    def _dispatch_with_marker_spy(self, pool, monkeypatch, resolved, tool_args):
        dispatcher = ToolDispatcher(pool)
        engine = MagicMock()
        engine._resolve_placeholders.return_value = resolved
        engine._cache_tool_args = MagicMock()
        dispatcher.set_engine(engine)
        dispatcher._run_child_sync = MagicMock(return_value='SYNC_ROUTED')
        dispatcher._run_child_async = MagicMock(return_value='ASYNC_ROUTED')
        monkeypatch.setattr(shutil, 'which', lambda *a, **k: '/usr/bin/fake')
        monkeypatch.setattr(cli_agent_runner, 'run_cli_agent',
                            MagicMock(return_value=_fake_result()))
        inst = _make_instance('Maine')
        pool.instances['Maine'] = inst

        seen = {}
        real_handler = dispatcher.handle_call_cli_agent

        def spy(res, instance):
            seen['during'] = instance._in_blocking_tool
            return real_handler(res, instance)

        monkeypatch.setattr(dispatcher, 'handle_call_cli_agent', spy)
        dispatcher.execute_tool(inst, 'call_cli_agent', tool_args, [], function_id='1')
        return seen['during'], inst

    def test_sync_sets_and_clears_marker_with_arg_timeout(self, monkeypatch):
        pool = FakePool(instances={'Maine': _make_instance('Maine')})
        during, inst = self._dispatch_with_marker_spy(
            pool, monkeypatch,
            {'harness': 'claude_code', 'task': 'do it', 'workspace': '/resolved/ws', 'timeout': 300},
            {'harness': 'claude_code', 'task': 'do it', 'timeout': 300})
        name, _mono, bound = during
        assert name == 'call_cli_agent'
        assert bound == 300.0  # the arg timeout, deliberately NOT capped at holder_dms_max_bound_s
        assert inst._in_blocking_tool is None  # cleared on return

    def test_marker_bound_falls_back_to_settings_default(self, monkeypatch):
        pool = FakePool(instances={'Maine': _make_instance('Maine')})
        pool.settings.cli_agent_timeout = 1800
        during, inst = self._dispatch_with_marker_spy(
            pool, monkeypatch,
            {'harness': 'claude_code', 'task': 'do it', 'workspace': '/resolved/ws'},
            {'harness': 'claude_code', 'task': 'do it'})
        name, _mono, bound = during
        assert name == 'call_cli_agent'
        assert bound == 1800.0  # settings default, no timeout arg
        assert inst._in_blocking_tool is None


# ──────────────────────────────────────────────────────────────────────────────
# 4. Nesting-depth rejection
# ──────────────────────────────────────────────────────────────────────────────


class TestNestingRejection:
    def test_rejects_when_too_deep(self, monkeypatch):
        pool = FakePool(instances={'Maine': _make_instance('Maine', nest_depth=10)})
        pool.settings.max_nesting_depth = 5
        dispatcher, runner = _make_handler(pool, monkeypatch)
        out = dispatcher.handle_call_cli_agent(
            {'harness': 'claude_code', 'task': 't', 'workspace': '/resolved/ws'},
            pool.get_instance('Maine'))
        assert out.startswith('Error:') or 'nest' in out.lower()
        runner.assert_not_called()  # rejected before spawn


# ──────────────────────────────────────────────────────────────────────────────
# 6. Harness-not-installed (preflight None)
# ──────────────────────────────────────────────────────────────────────────────


class TestHarnessNotInstalled:
    def test_clean_error_no_spawn(self, monkeypatch):
        pool = FakePool(instances={'Maine': _make_instance('Maine')})
        dispatcher, runner = _make_handler(pool, monkeypatch, which_result=None)
        # Approval must not be requested for an uninstalled harness (preflight gates it).
        out = dispatcher.handle_call_cli_agent(
            {'harness': 'claude_code', 'task': 't', 'workspace': '/resolved/ws'},
            pool.get_instance('Maine'))
        assert 'not installed' in out.lower()
        runner.assert_not_called()
        pool.operation_manager.request_user_approval.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# 8. Approval rejected → DENY before spawn
# ──────────────────────────────────────────────────────────────────────────────


class TestApprovalRejected:
    def test_denied_before_spawn(self, monkeypatch):
        pool = FakePool(instances={'Maine': _make_instance('Maine')})
        pool.operation_manager.request_user_approval.return_value = (False, 'no thanks')
        dispatcher, runner = _make_handler(pool, monkeypatch)
        out = dispatcher.handle_call_cli_agent(
            {'harness': 'claude_code', 'task': 't', 'workspace': '/resolved/ws'},
            pool.get_instance('Maine'))
        assert 'denied' in out.lower()
        runner.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# 7. No credential leak + 5. timeout + teardown  (runner layer)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_runner_globals(monkeypatch, tmp_path):
    """Reset the module-level semaphore and redirect log writes to tmp (no repo noise)."""
    monkeypatch.setattr(cli_agent_runner, '_CLI_SEMAPHORE', None, raising=False)
    monkeypatch.setattr(cli_agent_runner, '_CLI_SEMAPHORE_SIZE', None, raising=False)
    monkeypatch.setattr(cli_agent_runner, '_LOGS_DIR', tmp_path)
    monkeypatch.setenv('M6_CLI_AGENT_DOCKER_IMAGE', 'testimg:tag')  # skip image tag computation


def _pool_for_runner(cap=2):
    pool = MagicMock()
    pool.settings.max_cli_agent_children = cap
    pool.settings.cli_agent_timeout = 1800
    return pool


class TestCredentialStrip:
    def test_openai_env_stripped_from_child(self, monkeypatch, tmp_path):
        monkeypatch.setattr(shutil, 'which', lambda *a, **k: '/usr/bin/fake')
        captured = {}

        def fake_run(cmd, env=None, **kw):
            captured['env'] = env
            return MagicMock(stdout='hi', stderr='', returncode=0)

        monkeypatch.setattr(cli_agent_runner.subprocess, 'run', fake_run)
        monkeypatch.setenv('OPENAI_API_KEY', 'SECRET_AC_KEY')
        monkeypatch.setenv('OPENAI_BASE_URL', 'http://ac-endpoint')
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'harness_native')  # must survive
        cli_agent_runner.run_cli_agent(harness='claude_code', task='t',
                                       workspace=str(tmp_path), permission_posture='acceptEdits',
                                       timeout=30, pool=_pool_for_runner(), caller_name='caller')
        child_env = captured['env']
        # Asserts the docker-CLIENT env boundary — the strongest thing we control here;
        # the spawned container's isolation is the defense-in-depth that contains the child.
        assert 'OPENAI_API_KEY' not in child_env
        assert 'OPENAI_BASE_URL' not in child_env
        assert child_env.get('ANTHROPIC_API_KEY') == 'harness_native'


class TestTimeoutTeardown:
    def test_timeout_kills_container(self, monkeypatch, tmp_path):
        monkeypatch.setattr(shutil, 'which', lambda *a, **k: '/usr/bin/fake')
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd, timeout=1)
            return MagicMock(stdout='', stderr='', returncode=0)

        monkeypatch.setattr(cli_agent_runner.subprocess, 'run', fake_run)
        res = cli_agent_runner.run_cli_agent(harness='claude_code', task='t',
                                             workspace=str(tmp_path), permission_posture='acceptEdits',
                                             timeout=1, pool=_pool_for_runner(), caller_name='caller')
        assert res.timed_out is True
        assert len(calls) >= 2  # the docker run, then the docker kill teardown
        assert 'kill' in calls[1]  # teardown targets the container


# ──────────────────────────────────────────────────────────────────────────────
# 9. Concurrency cap (semaphore)
# ──────────────────────────────────────────────────────────────────────────────


class TestConcurrencyCap:
    """Drive run_cli_agent's `with sem:` path (plan §6 #9): with cap=1 a second concurrent
    call must serialize, proving the cap is read from pool.settings.max_cli_agent_children."""

    def test_run_cli_agent_serializes_at_cap_one(self, monkeypatch, tmp_path):
        monkeypatch.setattr(shutil, 'which', lambda *a, **k: '/usr/bin/fake')
        state = {'active': 0, 'peak': 0}
        lock = threading.Lock()

        def fake_run(cmd, **kw):
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
            time.sleep(0.05)
            with lock:
                state['active'] -= 1
            return MagicMock(stdout='', stderr='', returncode=0)

        monkeypatch.setattr(cli_agent_runner.subprocess, 'run', fake_run)
        pool = _pool_for_runner(cap=1)  # cap read from pool.settings.max_cli_agent_children

        def call():
            cli_agent_runner.run_cli_agent(
                harness='claude_code', task='t', workspace=str(tmp_path),
                permission_posture='acceptEdits', timeout=5, pool=pool, caller_name='caller')

        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # peak is measured INSIDE the semaphore-held section, so it can never exceed cap=1;
        # three calls still all ran, proving they serialized rather than ran in parallel.
        assert state['peak'] == 1


# ──────────────────────────────────────────────────────────────────────────────
# 10-12. Per-adapter launch argv (exact)
# ──────────────────────────────────────────────────────────────────────────────


class TestAdapterArgv:
    def test_claude_code_argv(self):
        cmd = ClaudeCodeAdapter().build_launch_cmd(task='add tests', workspace='/ws', posture='acceptEdits')
        assert cmd == ['claude', '-p', '--permission-mode', 'acceptEdits',
                       '--output-format', 'text', 'add tests']

    def test_opencode_argv(self):
        cmd = OpencodeAdapter().build_launch_cmd(task='add tests', workspace='/ws', posture='acceptEdits')
        assert cmd == ['opencode', 'run', '--auto', 'add tests']

    def test_opencode_auth_var(self):
        # Zen/Go free-tier auth reads OPENCODE_API_KEY (alias OPENCODE_ZEN_API_KEY), not
        # OPENAI_API_KEY. ``opencode run`` is inherently a one-shot (ephemeral local server),
        # so no standalone flag is emitted.
        assert OpencodeAdapter.auth_env_var == 'OPENCODE_API_KEY'

    def test_opencode_model_autoprefix(self):
        # A bare model ID is auto-prefixed with 'opencode/' (opencode requires provider/model);
        # an ID that already carries a provider is passed through untouched.
        a = OpencodeAdapter().build_launch_cmd(task='x', workspace='/ws', posture='acceptEdits',
                                              model='deepseek-v4.1-flash-free')
        assert '--model' in a and 'opencode/deepseek-v4.1-flash-free' in a
        b = OpencodeAdapter().build_launch_cmd(task='x', workspace='/ws', posture='acceptEdits',
                                              model='anthropic/claude-sonnet-4')
        assert 'anthropic/claude-sonnet-4' in b and 'opencode/anthropic/' not in ' '.join(b)

    def test_antigravity_argv(self):
        cmd = AntigravityAdapter().build_launch_cmd(task='add tests', workspace='/ws', posture='acceptEdits')
        assert cmd == ['agy', '-p', '--mode', 'accept-edits', '--output-format', 'text',
                       '--print-timeout', '600', 'add tests']

    def test_leading_dash_task_guarded(self):
        # The flag-injection guard is shared by all adapters — assert it for every one.
        for adapter in (ClaudeCodeAdapter(), OpencodeAdapter(), AntigravityAdapter()):
            cmd = adapter.build_launch_cmd(task='-r', workspace='/ws', posture='acceptEdits')
            assert '--' in cmd and cmd[-1] == '-r', f'{adapter.harness}: guard missing'
