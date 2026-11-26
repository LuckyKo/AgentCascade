"""BUG_0425 — tool dry-run / preview-before-commit / preview-verified auto-confirm.

Three-layer proof (see .agent_lessons/bug0425-build-progress.md and the plan's
"Test surface"):

  * behavior   — drive the REAL OperationManager with dry_run=True and assert the
                 returned preview while proving NOTHING is touched on disk and no
                 approval is raised; pair with a dry-run shell case that proves no
                 process is spawned;
  * default-off regression — with all 5 flags OFF (the shipped default) a write still
                 writes and a shell command still spawns + executes, so the feature is
                 provably opt-in and never changes today's behavior;
  * wiring     — drive the REAL WriteFile.call and assert it forwards dry_run to the
                 operation_manager (and defaults it to False when absent);
  * enforcer   — call ApprovalMixin.preview_auto_confirms UNBOUND with a two-method
                 self-double and assert the True/False decisions for each guard;
  * observable — drive request_user_approval(preview=...) and assert the preview lands
                 in list_pending_approvals and the Telegram body when present, and is
                 absent when there is no preview.

All five flags default OFF; the regression cases assert that explicitly.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# All BUG_0425 flags default OFF. The autouse fixture pins them so a test that
# forgets to opt in does not accidentally inherit a globally-on environment, and
# the default-off regression cases assert behavior under a KNOWN-off state.
_DEFAULTS_OFF = {
    'TOOL_DRY_RUN_ENABLED': False,
    'TOOL_PREVIEW_ATTACHED_TO_APPROVAL': False,
    'TOOL_AUTO_CONFIRM_ENABLED': False,
    'TOOL_AUTO_CONFIRM_MAX_DIFF_LINES': 200,
    'TOOL_AUTO_CONFIRM_SCOPE': 'owned-in-workspace',
}


@pytest.fixture(autouse=True)
def _default_off(monkeypatch):
    """Pin all 5 flags OFF unless a test overrides one of them.

    The operation managers read `settings.<FLAG>` at call time (``from agent_cascade
    import settings``), so monkeypatching the module attributes is the supported seam.
    """
    import agent_cascade.settings as ac_settings
    for key, value in _DEFAULTS_OFF.items():
        monkeypatch.setattr(ac_settings, key, value)


def _make_om(tmp_path):
    """A real OperationManager over a temp workspace (no agent_pool)."""
    from agent_cascade.operation_manager import OperationManager
    om = OperationManager(base_dir=str(tmp_path))
    om.enable_timeout = True
    om.approval_timeout_seconds = 10
    return om


def _bak_files(tmp_path):
    """Every *.bak under the OM's backup tree (must stay empty for dry-run)."""
    backups = tmp_path / 'logs' / 'backups'
    return [str(p) for p in backups.rglob('*.bak')] if backups.exists() else []


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — behavior (real OM; dry-run returns a preview, touches nothing)
# ─────────────────────────────────────────────────────────────────────────────
class TestDryRunBehavior:

    def test_write_dry_run_returns_unified_diff_and_touches_nothing(self, tmp_path):
        om = _make_om(tmp_path)
        target = tmp_path / 'a.txt'
        target.write_text('alpha\nbeta\ngamma\n', encoding='utf-8', newline='')
        before_bytes = target.read_bytes()
        st = target.stat()
        mtime0, size0 = st.st_mtime_ns, st.st_size

        preview = om.write_file('a.txt', 'alpha\nBETA\ngamma\nextra\n',
                                agent_name='coder', dry_run=True)

        # Concrete unified-diff content (hunk header + the actual +/- lines), not just
        # a non-empty string.
        assert '@@' in preview, f'expected a unified-diff hunk header, got {preview!r}'
        assert '-beta' in preview and '+BETA' in preview, f'diff body missing, got {preview!r}'

        # No mutation: bytes + mtime/size unchanged, no backup, no approval raised.
        assert target.read_bytes() == before_bytes
        st2 = target.stat()
        assert (st2.st_mtime_ns, st2.st_size) == (mtime0, size0), 'dry_run changed mtime/size'
        assert _bak_files(tmp_path) == [], 'dry_run must not create a .bak'
        assert list(om.pending.values()) == [], 'dry_run must not raise an approval'

    def test_write_dry_run_on_new_file_is_labeled_create(self, tmp_path):
        om = _make_om(tmp_path)
        preview = om.write_file('brand_new.txt', 'hi\n', agent_name='coder', dry_run=True)
        assert 'would create' in preview, f'new-file preview must label a create, got {preview!r}'
        assert not (tmp_path / 'brand_new.txt').exists(), 'dry_run must not create the file'
        assert list(om.pending.values()) == []

    def test_delete_dry_run_lists_targets_without_deleting(self, tmp_path):
        om = _make_om(tmp_path)
        target = tmp_path / 'doomed.txt'
        target.write_text('payload' * 50, encoding='utf-8')

        preview = om.delete_file('doomed.txt', agent_name='coder', dry_run=True)

        assert '[DRY RUN]' in preview and 'would delete' in preview, f'{preview!r}'
        assert 'doomed.txt' in preview
        # No mutation: the file survives, nothing is backed up, no approval raised.
        assert target.exists(), 'dry_run must not delete the target'
        assert _bak_files(tmp_path) == [], 'dry_run must not back anything up'
        assert list(om.pending.values()) == []


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1b — shell transport (real execute path; dry-run spawns NO process)
# ─────────────────────────────────────────────────────────────────────────────
class _StaticPipe:
    def __init__(self, text=''):
        self._text = text

    def read(self, size=-1):
        text, self._text = self._text, ''
        return text


class _FakeProc:
    """Controllable Popen stand-in; never touches the host."""
    pid = 4242

    def __init__(self, *, stdout='', stderr='', returncode=0):
        self.stdout = _StaticPipe(stdout)
        self.stderr = _StaticPipe(stderr)
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -1


def _make_shell_om(monkeypatch, proc, spawned):
    """Real ShellMixin path with only the OS transport faked (mirrors BUG_0139)."""
    import agent_cascade.operation_manager.shell as shell_mod
    from agent_cascade.operation_manager import OperationManager
    om = OperationManager.__new__(OperationManager)
    om.base_dir = '/tmp'
    om.agent_pool = MagicMock()
    monkeypatch.setattr(shell_mod, 'ON_WINDOWS', False)
    monkeypatch.setattr(shell_mod.ShellMixin, 'evaluate_shell_command_safety',
                        lambda self, *a, **k: (True, []))
    monkeypatch.setattr('agent_cascade.utils.tool_path_resolver.resolve_tool_path',
                        lambda *a, **k: '/tmp')
    monkeypatch.setattr(os, 'getpgid', lambda pid: pid, raising=False)
    monkeypatch.setattr(os, 'killpg', lambda pgid, sig: None, raising=False)

    def _fake_popen(*a, **k):
        spawned.append((a, k))
        return proc

    monkeypatch.setattr(shell_mod.subprocess, 'Popen', _fake_popen)
    return om


class TestShellTransport:

    def test_shell_dry_run_does_not_spawn_a_process(self, monkeypatch):
        spawned = []
        om = _make_shell_om(monkeypatch, _FakeProc(stdout='SHOULD-NOT-RUN\n'), spawned=spawned)
        result = om.execute_shell_command(command='echo SHOULD-NOT-RUN',
                                          justification='dry run', agent_name='normal',
                                          dry_run=True)
        # Nothing reached the transport, and the reply is the command-echo preview.
        assert spawned == [], 'dry_run must NOT spawn a process'
        assert '[DRY RUN]' in result and 'would execute' in result, f'{result!r}'
        assert 'echo SHOULD-NOT-RUN' in result, 'preview must echo the command'

    def test_shell_default_off_still_spawns_and_executes(self, monkeypatch):
        spawned = []
        om = _make_shell_om(monkeypatch, _FakeProc(stdout='defaultoffcheck\n', returncode=0),
                            spawned=spawned)
        result = om.execute_shell_command(command='echo defaultoffcheck',
                                          justification='prove exec', agent_name='normal')
        # Default (dry_run omitted) → the OS transport IS invoked (executes as before),
        # and its output flows back.
        assert spawned, 'default-off shell must still spawn a process'
        assert 'defaultoffcheck' in result, f'command output must return, got {result!r}'


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — default-off regression (write still writes; flags OFF = today's behavior)
# ─────────────────────────────────────────────────────────────────────────────
class TestDefaultOffRegression:

    def test_default_off_write_still_writes_file(self, tmp_path):
        # autouse fixture guarantees all 5 flags OFF here.
        om = _make_om(tmp_path)
        res = om.write_file('out.txt', 'hello world\n', agent_name='coder')
        assert res.startswith('OK:'), f'write must succeed by default, got {res!r}'
        assert (tmp_path / 'out.txt').read_text(encoding='utf-8') == 'hello world\n', \
            'default-off write must persist bytes exactly'

    def test_dry_run_not_in_default_schema(self):
        """With TOOL_DRY_RUN_ENABLED OFF, the dry_run property is not advertised."""
        from agent_cascade.tools.custom.file_ops import WriteFile, DeleteFile, CopyFile, EditFile
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        for cls in (WriteFile, DeleteFile, CopyFile, EditFile, ShellCmd):
            assert 'dry_run' not in cls.parameters['properties'], \
                f'{cls.__name__} schema must NOT advertise dry_run with the flag OFF'


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — wiring (real WriteFile.call → forwards dry_run kwarg)
# ─────────────────────────────────────────────────────────────────────────────
class _RecordingOpManager:
    def __init__(self):
        self.calls = []

    def write_file(self, **kwargs):
        self.calls.append(kwargs)
        return 'OK: (stub)'


class _StubPool:
    def __init__(self, om):
        self.operation_manager = om


class TestDryRunWiring:

    def test_writefile_call_forwards_dry_run_true(self, monkeypatch):
        from agent_cascade.tools.custom.file_ops import WriteFile
        # BaseTool validates against self.parameters (additionalProperties:False);
        # declare dry_run to simulate TOOL_DRY_RUN_ENABLED ON (auto-undone by monkeypatch).
        monkeypatch.setitem(WriteFile.parameters['properties'], 'dry_run', {'type': 'boolean'})
        om = _RecordingOpManager()
        tool = WriteFile(agent_pool=_StubPool(om), agent_name='coder')
        tool.call({'path': 'x.txt', 'content': 'hi', 'dry_run': True})
        assert om.calls, 'WriteFile.call must reach operation_manager.write_file'
        assert om.calls[-1].get('dry_run') is True, \
            f'dry_run kwarg must be True, got {om.calls[-1]!r}'

    def test_writefile_call_forwards_dry_run_false_when_absent(self, monkeypatch):
        from agent_cascade.tools.custom.file_ops import WriteFile
        monkeypatch.setitem(WriteFile.parameters['properties'], 'dry_run', {'type': 'boolean'})
        om = _RecordingOpManager()
        tool = WriteFile(agent_pool=_StubPool(om), agent_name='coder')
        tool.call({'path': 'x.txt', 'content': 'hi'})
        assert om.calls, 'negative twin must still reach the call site'
        assert 'dry_run' not in om.calls[-1], f"default-off call must not forward dry_run, got {om.calls[-1]!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3b — enforcer (preview_auto_confirms UNBOUND with a 2-method self-double)
# ─────────────────────────────────────────────────────────────────────────────
class _PolicySelf:
    """Minimal self for the unbound preview_auto_confirms: only _resolve_path/_get_owner."""

    def __init__(self, owner='coder', unresolvable=()):
        self.owner = owner
        self.unresolvable = set(unresolvable)

    def _resolve_path(self, p, mode='rw'):
        if p in self.unresolvable:
            raise ValueError('outside the workspace')
        return f'RESOLVED::{p}'

    def _get_owner(self, resolved):
        return self.owner


class TestPreviewAutoConfirmsEnforcer:

    def _pac(self, monkeypatch, **flags):
        import agent_cascade.settings as ac_settings
        import agent_cascade.operation_manager.approval as app
        for key, value in flags.items():
            monkeypatch.setattr(ac_settings, key, value)
        return app.ApprovalMixin.preview_auto_confirms

    def test_true_only_when_enabled_bounded_inworkspace_owned(self, monkeypatch):
        pac = self._pac(monkeypatch, TOOL_AUTO_CONFIRM_ENABLED=True)
        s = _PolicySelf(owner='coder')
        assert pac(s, preview='@@ -1 +1 @@\n-a\n+b', affected_paths=['f.txt'],
                   agent_name='coder') is True

    def test_false_when_disabled(self, monkeypatch):
        pac = self._pac(monkeypatch, TOOL_AUTO_CONFIRM_ENABLED=False)
        assert pac(_PolicySelf(owner='coder'), preview='x',
                   affected_paths=['f.txt'], agent_name='coder') is False

    def test_false_when_scope_not_owned_in_workspace(self, monkeypatch):
        pac = self._pac(monkeypatch, TOOL_AUTO_CONFIRM_ENABLED=True,
                        TOOL_AUTO_CONFIRM_SCOPE='anything-else')
        assert pac(_PolicySelf(owner='coder'), preview='x',
                   affected_paths=['f.txt'], agent_name='coder') is False

    def test_false_when_preview_exceeds_max_lines(self, monkeypatch):
        pac = self._pac(monkeypatch, TOOL_AUTO_CONFIRM_ENABLED=True,
                        TOOL_AUTO_CONFIRM_MAX_DIFF_LINES=2)
        big = '\n'.join(f'line{i}' for i in range(10))
        assert pac(_PolicySelf(owner='coder'), preview=big,
                   affected_paths=['f.txt'], agent_name='coder') is False

    def test_false_on_empty_preview_or_low_risk_false(self, monkeypatch):
        pac = self._pac(monkeypatch, TOOL_AUTO_CONFIRM_ENABLED=True)
        s = _PolicySelf(owner='coder')
        assert pac(s, preview='', affected_paths=['f.txt'], agent_name='coder') is False
        assert pac(s, preview='x', affected_paths=['f.txt'], agent_name='coder',
                   low_risk=False) is False

    def test_false_when_any_path_outside_workspace(self, monkeypatch):
        pac = self._pac(monkeypatch, TOOL_AUTO_CONFIRM_ENABLED=True)
        s = _PolicySelf(owner='coder', unresolvable=['f.txt'])
        assert pac(s, preview='x', affected_paths=['f.txt'], agent_name='coder') is False

    def test_false_when_not_owned_by_agent(self, monkeypatch):
        pac = self._pac(monkeypatch, TOOL_AUTO_CONFIRM_ENABLED=True)
        assert pac(_PolicySelf(owner='someone-else'), preview='x',
                   affected_paths=['f.txt'], agent_name='coder') is False


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4 — observable (preview surfaces in list_pending_approvals + Telegram)
# ─────────────────────────────────────────────────────────────────────────────
class _NotifyPool:
    """Pool double: auto_security OFF so the TG push fires; records notify_user bodies."""

    def __init__(self):
        self.auto_security = False
        self.stopped = False
        self.messages = []
        self.telegram_supervisor = self  # notify_user lives here

    def notify_user(self, message, instance_name=''):
        self.messages.append(message)


class TestPreviewObservable:

    def _drive(self, om, preview):
        pool = _NotifyPool()
        om.agent_pool = pool
        om.afk_enabled = False  # runtime_state-backed; default OFF (no pre-emptive reject)

        t = threading.Thread(
            target=lambda: om.request_user_approval(
                agent_name='coder', tool_name='write_file',
                tool_args={'justification': 'j'}, description='desc', preview=preview))
        t.daemon = True
        t.start()

        captured, rid = {}, None
        deadline = time.time() + 5
        while time.time() < deadline:
            pend = om.list_pending_approvals()
            if pend:
                captured = pend[0]
                rid = pend[0]['request_id']
                break
            time.sleep(0.01)

        if rid:
            om.user_reject(rid, 'test done')
        t.join(timeout=5)

        assert captured, 'a pending approval should have appeared'
        tg = pool.messages[-1] if pool.messages else ''
        return captured.get('preview', '__missing__'), tg

    def test_preview_surfaced_when_present(self, tmp_path):
        om = _make_om(tmp_path)
        body = 'UNIFIED-DIFF-HERE\n@@ x @@'
        p, tg = self._drive(om, body)
        assert p == body, f'list_pending_approvals must carry the preview, got {p!r}'
        assert ('Preview:\n' + body) in tg, f'Telegram body must include the preview, got {tg!r}'

    def test_preview_absent_when_empty(self, tmp_path):
        om = _make_om(tmp_path)
        p, tg = self._drive(om, '')
        assert p == '', f'list_pending preview must be empty when none attached, got {p!r}'
        assert 'Preview:' not in tg, f'Telegram body must NOT include a Preview block, got {tg!r}'


if __name__ == '__main__':
    import sys as _sys
    _sys.exit(pytest.main([__file__, '-v', '-o', 'addopts=', '-m',
                           'not live_api and not skip_if_no_local and not extra_examples '
                           'and not extra_tools and not extra_vl and not stress '
                           'and not fullstack_e2e and not serial']))
