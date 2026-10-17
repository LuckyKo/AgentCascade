"""Tests for the ``safe`` execution_mode on shell_cmd.

A blocking shell call that frees local model RAM/VRAM around the command:
  save KV state BEFORE the security guard → unload the local model immediately
  before Popen → restore after. Degrades to plain sync when the caller's endpoint
  isn't state-save-capable.

The KV save/restore honors the existing "State KV Save/Restore" toggle
(``state_kv_save_enabled``); the model-release (``POST /v1/unload_all``) is ungated.

Mock style mirrors tests/tools/test_media_gen.py::TestMediaGenVRAM: patch
``agent_cascade.state_ops.*`` and drive a MagicMock instance carrying
``_last_endpoint_config``. The ordering test (save before approval, unload after
approval before Popen, restore after execution) is the core acceptance.
"""

import io
import threading
import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.operation_manager import OperationManager
from agent_cascade.operation_manager.shell import ShellMixin
from agent_cascade.tools.custom.shell_cmd import ShellCmd
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.state_ops import restore_held_slot_state
from jsonschema import ValidationError

CAPABLE_EP = {
    'state_save_enabled': True,
    'api_base': 'http://127.0.0.1:1234/v1',
    'model': 'test-model',
}


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _make_instance(*, endpoint=CAPABLE_EP, holds_slot=True):
    """A stand-in AgentInstance carrying the attributes the seam reads."""
    inst = MagicMock()
    inst.instance_name = 'agent-1'
    inst.agent_class = 'assistant'
    # Real AgentInstance uses threading.RLock (agent_instance.py:243); the seam's
    # save path re-acquires it under an outer lock, so a non-reentrant Lock would
    # deadlock in tests that drive the real save_instance_state.
    inst._state_lock = threading.RLock()
    inst._state_label = None
    inst._slot_release = (lambda: None) if holds_slot else None
    inst._last_endpoint_config = endpoint
    return inst


def _make_pool(*, instance, auto_security=False, kv_toggle=True, api_router=None):
    """A pool + OperationManager pair wired for the sync/safe path.

    ``settings`` is a real SimpleNamespace (not a MagicMock) so the kv_toggle gate
    reads a real bool — a bare MagicMock would auto-create a truthy attribute chain.
    """
    pool = MagicMock()
    pool.get_instance = lambda name: instance

    om = OperationManager.__new__(OperationManager)
    om.agent_pool = pool
    om.base_dir = '/tmp'
    # resolve_tool_path delegates to this; stub so cwd resolution succeeds without
    # wiring the real allowed-directory set.
    from pathlib import Path as _Path
    om._resolve_path = lambda path, mode='ro': _Path('.')
    pool.operation_manager = om

    pool.auto_security = auto_security
    pool.settings = SimpleNamespace(state_kv_save_enabled=kv_toggle)
    if api_router is not None:
        pool.api_router = api_router
    return pool, om


def _make_tool(pool):
    tool = ShellCmd()
    tool.agent_pool = pool
    return tool


def _fake_popen(order):
    """A subprocess.Popen stand-in that records 'run' and completes cleanly."""
    def _spawn(*a, **k):
        order.append('run')
        proc = MagicMock()
        proc.returncode = 0
        proc.pid = 0
        proc.wait = lambda timeout=None: 0
        proc.stdout = io.StringIO()
        proc.stderr = io.StringIO()
        return proc
    return _spawn


def _timeout_popen(order):
    """A Popen stand-in whose wait() raises TimeoutExpired (the timeout path)."""
    def _spawn(*a, **k):
        order.append('run')
        proc = MagicMock()
        proc.returncode = 0
        proc.pid = 0
        proc.stdout = io.StringIO()
        proc.stderr = io.StringIO()

        def _raise_timeout(timeout=None):
            import subprocess as _sub
            raise _sub.TimeoutExpired('cmd', timeout)
        proc.wait = _raise_timeout
        return proc
    return _spawn


def _wire_approval(om, order, approved=True):
    """Replace the OperationManager's approval call with an order-recorder."""
    def _approve(**kwargs):
        order.append('approval')
        return (approved, 'ok' if approved else 'no reason needed')
    om.request_user_approval = _approve


def _wire_approval_consume(om, order, inst):
    """Like _wire_approval, but the approval also CONSUMES the in-memory state label.

    Models a system agent (Security/Compressor) that ran inside the approval window and
    did its own save→restore on the same stable label — leaving ``inst._state_label`` None
    by the time the tool's finally runs. This is exactly the condition the P1 fix must
    survive (the old consume-once path would then no-op)."""
    def _approve_and_consume(**kwargs):
        order.append('approval')
        with inst._state_lock:
            inst._state_label = None
        return (True, 'ok')
    om.request_user_approval = _approve_and_consume


def _router_held(api_base, model):
    """A pool.api_router stand-in resolving a held endpoint (for restore tests)."""
    router = SimpleNamespace()
    router.get_effective_slot_info = lambda *a, **kw: {'api_base': api_base, 'needs_slot': True}
    router.get_endpoint_chain = lambda *a, **kw: [{'api_base': api_base, 'model': model}]
    return router


def _seam_patches(order, *, save=True, unload=True, restore=True, popen=True,
                 save_factory=None, unload_factory=None, restore_factory=None,
                 popen_factory=None):
    """Install the seam's four patches (save/unload/restore/Popen) as order-recorders.

    Each boolean opts a patch out; each ``*_factory`` overrides that patch's
    side_effect (default = append the step name to ``order``). Also zeroes the
    drain-thread join timeout in ShellMixin.execute_shell_command so the plain-sync
    path doesn't burn real sleeps — the tests assert on call ORDER, not output.
    Returns a contextlib.ExitStack for ``with _seam_patches(order):``.
    """
    stack = contextlib.ExitStack()
    if save:
        stack.enter_context(patch(
            'agent_cascade.state_ops.save_instance_state',
            side_effect=save_factory or (lambda i: (order.append('save'), True)[1])))
    if unload:
        stack.enter_context(patch(
            'agent_cascade.state_ops.unload_all_models',
            side_effect=unload_factory or (lambda b: (order.append('unload'), True)[1])))
    if restore:
        stack.enter_context(patch(
            'agent_cascade.state_ops.restore_held_slot_state',
            # Production now passes label=instance.instance_name; accept **kw so the
            # order-recorder stub stays in lockstep with the real signature.
            side_effect=restore_factory or (lambda p, i, n, **kw: order.append('restore'))))
    if popen:
        stack.enter_context(patch(
            'agent_cascade.operation_manager.shell.subprocess.Popen',
            side_effect=popen_factory or _fake_popen(order)))
    # Stub the cleanup/grace path (drain-thread join) so sync tests stay fast.
    stack.enter_context(patch('agent_cascade.operation_manager.shell.DRAIN_THREAD_JOIN_TIMEOUT', 0))
    return stack


# ────────────────────────────────────────────────────────────────────────────
# 1. Dispatch: 'safe' routes to _execute_safe, never to the async tracker.
# ────────────────────────────────────────────────────────────────────────────


class TestSafeDispatch:
    def test_safe_dispatches_to_execute_safe_not_async(self):
        pool, _om = _make_pool(instance=_make_instance(), auto_security=False)
        tool = _make_tool(pool)
        with patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe, \
              patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC'), \
              patch.object(tool, '_launch_async', side_effect=lambda **k: 'ASYNC') as m_async:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it',
                             'execution_mode': 'safe'})
            assert out == 'SAFE'
            m_async.assert_not_called()  # async tracker never touched by an explicit safe call

    def test_control_command_with_tool_id_beats_safe(self):
        # Control-command priority: tool_id + 'safe' → control path, no VRAM calls.
        pool, _om = _make_pool(instance=_make_instance(), auto_security=False)
        tool = _make_tool(pool)
        with patch.object(tool, '_handle_control_command',
                         side_effect=lambda **k: 'CONTROL') as m_ctrl, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': '__kill', 'tool_id': 7, 'execution_mode': 'safe'})
            assert out == 'CONTROL'
            m_safe.assert_not_called()


# ────────────────────────────────────────────────────────────────────────────
# 2. Ordering (the core requirement): save → approval → unload → run → restore.
# ────────────────────────────────────────────────────────────────────────────


class TestSafeOrdering:
    def test_ordering_save_before_guard_unload_before_popen_restore_after(self):
        order = []
        inst = _make_instance()
        pool, om = _make_pool(instance=inst, auto_security=False)  # non-safe cmd, no advisor
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=True)

        with _seam_patches(order):
            # A non-safe command forces the approval gate to run (is_safe=False).
            tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                               justification='run it', cwd='.', timeout=None)

        assert order == ['save', 'approval', 'unload', 'run', 'restore'], \
            f'save must precede the guard; unload after approval, before Popen; restore after: {order}'


# ────────────────────────────────────────────────────────────────────────────
# 3. Rejection path: no unload, no Popen, REJECTED result, label cleared.
# ────────────────────────────────────────────────────────────────────────────


class TestSafeRejection:
    def test_rejection_no_unload_no_popen_label_cleared(self):
        order = []
        inst = _make_instance()
        pool, om = _make_pool(instance=inst, auto_security=False)
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=False)  # approval denied

        with _seam_patches(order), \
             patch('agent_cascade.state_ops.clear_orphaned_state_label') as m_clear:
            result = tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                                        justification='run it', cwd='.', timeout=None)

        assert order == ['save', 'approval']  # saved BEFORE the guard; approval ran and denied
        m_clear.assert_called_once()          # label we set is cleared (nothing to restore)
        assert result.startswith('REJECTED:')
        assert not result.startswith('[safe]')  # no note on rejection


# ────────────────────────────────────────────────────────────────────────────
# 4. Unload-failure path: command still runs; note present. Plus hook symmetry:
#    a raise from before_execute is caught (ERROR returned) and restore still runs.
# ────────────────────────────────────────────────────────────────────────────


class TestSafeUnloadFailure:
    def test_unload_returns_false_still_runs_with_note(self):
        order = []
        inst = _make_instance()
        pool, om = _make_pool(instance=inst, auto_security=False)
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=True)

        def _unload_false(base):
            order.append('unload')
            return False

        with _seam_patches(order, unload_factory=_unload_false):
            result = tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                                        justification='run it', cwd='.', timeout=None)

        assert order == ['save', 'approval', 'unload', 'run', 'restore']  # ran despite unload False
        assert result.startswith('[safe]')              # note present (freed=True)

    def test_before_execute_raises_still_restores_returns_error(self):
        # before_execute is the FIRST statement inside the try in
        # ShellMixin.execute_shell_command, so its exception hits that method's except
        # (ERROR string) and this method's own finally still restores.
        order = []
        inst = _make_instance()
        pool, om = _make_pool(instance=inst, auto_security=False)
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=True)

        def _boom(base):
            raise RuntimeError('unload blew up')

        with _seam_patches(order, unload_factory=_boom):
            result = tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                                        justification='run it', cwd='.', timeout=None)

        # freed was set BEFORE the POST → restore still ran; result is the ERROR string.
        assert order == ['save', 'approval', 'restore']
        assert 'ERROR: Approved but execution failed' in result


# ────────────────────────────────────────────────────────────────────────────
# 5. Timeout path: restore still called exactly once (finally semantics).
# ────────────────────────────────────────────────────────────────────────────


class TestSafeTimeout:
    def test_timeout_restore_still_called_once(self):
        order = []
        inst = _make_instance()
        pool, om = _make_pool(instance=inst, auto_security=False)
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=True)

        with _seam_patches(order, popen_factory=_timeout_popen(order)):
            result = tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                                        justification='run it', cwd='.', timeout=1)

        assert 'restore' in order            # finally restored despite the timeout
        assert 'timed out' in result


# ────────────────────────────────────────────────────────────────────────────
# 6. Degradation: not-capable endpoint → plain sync, zero HTTP calls.
# ────────────────────────────────────────────────────────────────────────────


class TestSafeDegradation:
    def test_not_capable_plain_sync_no_http(self):
        order = []
        inst = _make_instance(endpoint={'state_save_enabled': False,
                                        'api_base': 'http://127.0.0.1:1234/v1',
                                        'model': 'test-model'})
        pool, om = _make_pool(instance=inst, auto_security=False)
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=True)

        with _seam_patches(order):
            result = tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                                        justification='run it', cwd='.', timeout=None)

        assert 'save' not in order and 'unload' not in order and 'restore' not in order
        assert not result.startswith('[safe]')  # degraded → no note

    def test_missing_instance_plain_sync_no_http(self):
        # _get_agent_name can yield 'unknown' → get_instance returns None.
        order = []
        pool, om = _make_pool(instance=None, auto_security=False)
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=True)
        with _seam_patches(order):
            result = tool._execute_safe(agent_name='unknown', command='python heavy.py',
                                        justification='run it', cwd='.', timeout=None)
        assert 'save' not in order and 'unload' not in order and 'restore' not in order
        assert not result.startswith('[safe]')


# ────────────────────────────────────────────────────────────────────────────
# 10. Schema: 'safe' accepted; unknown mode still rejected (BUG_0024 enum).
# ────────────────────────────────────────────────────────────────────────────


class TestSafeSchema:
    def test_unknown_mode_still_rejected(self):
        pool, _om = _make_pool(instance=_make_instance(), auto_security=False)
        tool = _make_tool(pool)
        with pytest.raises(ValidationError):
            # 'bogus' is not in the enum → BaseTool's jsonschema validation rejects it.
            tool.call({'command': 'ls', 'justification': 'x', 'execution_mode': 'bogus'})


# ────────────────────────────────────────────────────────────────────────────
# 11. Advisor-suppression wiring: save gated, unload ungated.
# ────────────────────────────────────────────────────────────────────────────


class TestAdvisorSuppression:
    def test_advisor_on_suppresses_save_but_unload_still_runs(self):
        order = []
        inst = _make_instance()
        pool, om = _make_pool(instance=inst, auto_security=True)  # advisor will run
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=True)

        with _seam_patches(order):
            # Non-safe command + auto_security=True → advisor_will_run True.
            tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                               justification='run it', cwd='.', timeout=None)

        assert 'save' not in order     # save suppressed (advisor owns the KV)
        assert order == ['approval', 'unload', 'run', 'restore']  # unload ungated

    def test_advisor_off_saves_normally(self):
        order = []
        inst = _make_instance()
        pool, om = _make_pool(instance=inst, auto_security=False)
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=True)
        with _seam_patches(order):
            tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                               justification='run it', cwd='.', timeout=None)
        assert order == ['save', 'approval', 'unload', 'run', 'restore']


# ────────────────────────────────────────────────────────────────────────────
# 12. Toggle-off path: no save/restore, but unload IS called (model release ungated).
# ────────────────────────────────────────────────────────────────────────────


class TestToggleOff:
    def test_toggle_off_no_save_and_restore_only_unload_ungated(self):
        order = []
        inst = _make_instance()
        pool, om = _make_pool(instance=inst, auto_security=False, kv_toggle=False)
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=True)

        with _seam_patches(order):
            result = tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                                        justification='run it', cwd='.', timeout=None)

        assert 'save' not in order  # KV save gated by the toggle
        # With the toggle OFF the finally must NOT attempt a restore (cold reload is what
        # the user asked for) — only the model release (unload) stays ungated. The old
        # "restore-attempt ungated" assumption predates the stable-label fix: an explicit
        # label would have bypassed the consume-once no-op, so the restore is now gated on
        # kv_toggle too. See TestSafeStableLabelRestore.test_toggle_off_no_state_load_issued
        # for the EFFECT-level assertion (this order test patches restore_held_slot_state).
        assert order == ['approval', 'unload', 'run']  # model release ungated; restore gated off
        assert 'State KV Save/Restore' in result               # notification explains the toggle


# ────────────────────────────────────────────────────────────────────────────
# 14. Restore eviction-safety: no slot held / unresolvable endpoint → skip + clear.
# ────────────────────────────────────────────────────────────────────────────


class TestRestoreEvictionSafety:
    def test_no_held_endpoint_skips_restore_and_clears_label(self):
        # Real restore_held_slot_state runs; resolution fails → no state/load, label cleared.
        inst = _make_instance(holds_slot=True)
        with inst._state_lock:
            inst._state_label = 'agent-1'
        pool, _om = _make_pool(instance=inst, auto_security=False)

        def _clear_label(i, n):
            with i._state_lock:
                i._state_label = None

        with patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=None), \
             patch('agent_cascade.state_ops.restore_instance_state') as m_restore, \
             patch('agent_cascade.state_ops.clear_orphaned_state_label', side_effect=_clear_label) as m_clear:
            ok = restore_held_slot_state(pool, inst, 'agent-1')

        assert ok is False
        m_restore.assert_not_called()   # never load onto an unowned endpoint
        m_clear.assert_called_once()    # orphaned label cleared
        with inst._state_lock:
            assert inst._state_label is None  # the clear actually ran (not a no-op mock)


# ────────────────────────────────────────────────────────────────────────────
# 16. Capability gate, no fixed ports: a non-1234/9123 port must NOT block.
# ────────────────────────────────────────────────────────────────────────────


class TestCapabilityGateNoPorts:
    def test_non_legacy_port_still_saves_unloads_restores(self):
        order = []
        ep = {'state_save_enabled': True, 'api_base': 'http://127.0.0.1:9124/v1', 'model': 'x'}
        inst = _make_instance(endpoint=ep)
        pool, om = _make_pool(instance=inst, auto_security=False,
                              api_router=_router_held('http://127.0.0.1:9124/v1', 'x'))
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=True)

        with _seam_patches(order):
            tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                               justification='run it', cwd='.', timeout=None)

        assert order == ['save', 'approval', 'unload', 'run', 'restore']


# ────────────────────────────────────────────────────────────────────────────
# 17. Shared-helper extraction: engine methods delegate to the state_ops helpers.
# ────────────────────────────────────────────────────────────────────────────


class TestEngineDelegates:
    def test_engine_resolve_delegates_to_state_ops(self):
        engine = ExecutionEngine.__new__(ExecutionEngine)
        engine.pool = SimpleNamespace(api_router=_router_held('http://127.0.0.1:1234/v1', 'model-A'))
        inst = _make_instance()

        with patch('agent_cascade.state_ops.resolve_held_endpoint',
                   return_value={'api_base': 'http://127.0.0.1:1234/v1', 'model': 'model-A'}) as m:
            out = engine._resolve_held_endpoint(inst)
        m.assert_called_once()
        assert out == {'api_base': 'http://127.0.0.1:1234/v1', 'model': 'model-A'}

    def test_engine_restore_delegates_to_state_ops(self):
        engine = ExecutionEngine.__new__(ExecutionEngine)
        engine.pool = SimpleNamespace(api_router=_router_held('http://127.0.0.1:1234/v1', 'model-A'))
        inst = _make_instance()
        with patch('agent_cascade.state_ops.restore_held_slot_state', return_value=True) as m:
            out = engine._restore_held_slot_state(inst, 'agent-1')
        m.assert_called_once()
        assert out is True

    def test_engine_clear_delegates_to_state_ops(self):
        # _clear_orphaned_state_label is a @staticmethod — bound/called with no self.
        inst = _make_instance()
        with patch('agent_cascade.state_ops.clear_orphaned_state_label') as m:
            ExecutionEngine._clear_orphaned_state_label(inst, 'agent-1')
        m.assert_called_once()


# ────────────────────────────────────────────────────────────────────────────
# 18. Stable-label restore (the P1 fix). The suite above patches
#     restore_instance_state / save_instance_state / restore_held_slot_state, so it
#     asserts call ORDER, never EFFECT — a patched restore "runs" even when the real
#     one returns False with no request. These tests patch ONLY the HTTP layer
#     (state_ops.save_state / state_ops.restore_state) and drive the real save/restore,
#     asserting a state/load is actually issued for the stable label after the unload.
# ────────────────────────────────────────────────────────────────────────────


def _http_seam(order, *, restore_factory=None, save_factory=None):
    """Patch ONLY the HTTP layer (save_state / restore_state / unload_all_models / Popen)
    for a real-label-lifecycle test, so the real save/restore bookkeeping runs.

    ``restore_factory(api_base, model, label) -> bool`` records each state/load; when
    omitted it defaults to a recorder that appends the label and returns True. Returns an
    ExitStack — use as ``with _http_seam(order, ...) as stack: tool._execute_safe(...)``.
    """
    restore_calls = []

    def _record_restore(api_base, model, lbl):
        restore_calls.append(lbl)  # always record the label that was requested
        if restore_factory is not None:
            return restore_factory(api_base, model, lbl)
        return True

    stack = contextlib.ExitStack()
    stack.enter_context(patch('agent_cascade.state_ops.restore_state', side_effect=_record_restore))
    if save_factory is not None:
        stack.enter_context(patch('agent_cascade.state_ops.save_state', side_effect=save_factory))
    else:
        stack.enter_context(patch('agent_cascade.state_ops.save_state', return_value='agent-1'))
    stack.enter_context(patch('agent_cascade.state_ops.unload_all_models',
                              side_effect=lambda b: (order.append('unload'), True)[1]))
    stack.enter_context(patch('agent_cascade.operation_manager.shell.subprocess.Popen',
                              side_effect=_fake_popen(order)))
    stack.enter_context(patch('agent_cascade.operation_manager.shell.DRAIN_THREAD_JOIN_TIMEOUT', 0))
    return stack, restore_calls


class TestSafeStableLabelRestore:
    def test_advisor_consumes_label_restore_still_issued(self):
        """THE regression for this exact bug. With auto_security=True the Security
        advisor owns the KV on the SAME label and consumes _state_label inside the
        approval window before the unload. The old finally then no-ops (no state/load).
        With the P1 fix the finally restores by the stable label, so a state/load IS
        issued after the unload. We model the advisor's net effect: _state_label set
        then consumed (None) during the approval window."""
        order = []
        inst = _make_instance()
        # Model the advisor having saved the caller's KV under the stable label.
        with inst._state_lock:
            inst._state_label = 'agent-1'
        pool, om = _make_pool(instance=inst, auto_security=True,
                              api_router=_router_held(CAPABLE_EP['api_base'], CAPABLE_EP['model']))
        tool = _make_tool(pool)

        # Approval recorder also simulates the advisor's restore consuming the label.
        _wire_approval_consume(om, order, inst)

        stack, restore_calls = _http_seam(order)
        with stack:
            tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                               justification='run it', cwd='.', timeout=None)

        # Advisor path suppresses the tool's own save; unload still runs.
        assert 'save' not in order
        assert order == ['approval', 'unload', 'run']
        # THE FIX: a state/load IS issued after the unload, for the stable label,
        # even though _state_label was None at the finally.
        assert restore_calls, 'restore_state must be issued after the unload (P1 fix)'
        assert all(lbl == 'agent-1' for lbl in restore_calls), \
            f'restore must target the stable label: {restore_calls}'
        # The label was consumed (None) at the finally — proving the old path would no-op.
        with inst._state_lock:
            assert inst._state_label is None

    def test_stable_label_restore_when_label_consumed_mid_window(self):
        """auto_security=False → the tool's own save runs (real save_instance_state,
        only save_state HTTP patched). The approval recorder consumes the label mid-window.
        The finally must still restore by the stable label, not the cleared _state_label."""
        order = []
        inst = _make_instance()
        pool, om = _make_pool(instance=inst, auto_security=False,
                              api_router=_router_held(CAPABLE_EP['api_base'], CAPABLE_EP['model']))
        tool = _make_tool(pool)

        _wire_approval_consume(om, order, inst)

        def _save_http(api_base, model, name):
            order.append('save')
            return name  # real label == instance_name

        stack, restore_calls = _http_seam(order, save_factory=_save_http)
        with stack:
            tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                               justification='run it', cwd='.', timeout=None)

        assert order == ['save', 'approval', 'unload', 'run']
        assert restore_calls == ['agent-1'], \
            f'restore_state must use the stable label: {restore_calls}'

    def test_stale_or_absent_file_no_exception_escapes_finally(self):
        """restore_state returns False (file absent/stale) → the finally must not raise;
        _execute_safe still returns a string; the label is cleared (no retry storm)."""
        order = []
        inst = _make_instance()
        with inst._state_lock:
            inst._state_label = 'agent-1'
        pool, om = _make_pool(instance=inst, auto_security=True,
                              api_router=_router_held(CAPABLE_EP['api_base'], CAPABLE_EP['model']))
        tool = _make_tool(pool)

        _wire_approval_consume(om, order, inst)

        stack, restore_calls = _http_seam(order, restore_factory=lambda a, m, l: False)  # file absent/stale
        with stack:
            result = tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                                        justification='run it', cwd='.', timeout=None)

        assert restore_calls == ['agent-1']  # restore attempted with the stable label
        assert isinstance(result, str)        # no exception escaped the finally
        with inst._state_lock:
            assert inst._state_label is None  # cleared on failure (no retry storm)

    def test_toggle_off_no_state_load_issued(self):
        """EFFECT guard for the kv_toggle gate. With "State KV Save/Restore" OFF the user
        asked for a cold reload, so the finally must NOT issue a state/load — even though
        P1 passes an explicit stable label (which would bypass the consume-once no-op).
        The order-based TestToggleOff cannot catch this because it patches
        restore_held_slot_state; here we patch ONLY the HTTP layer and assert that
        restore_state is never called. Unload stays ungated."""
        order = []
        inst = _make_instance()
        pool, om = _make_pool(instance=inst, auto_security=False, kv_toggle=False,
                              api_router=_router_held(CAPABLE_EP['api_base'], CAPABLE_EP['model']))
        tool = _make_tool(pool)
        _wire_approval(om, order, approved=True)

        stack, restore_calls = _http_seam(order)
        with stack:
            result = tool._execute_safe(agent_name='agent-1', command='python heavy.py',
                                        justification='run it', cwd='.', timeout=None)

        # No save (toggle off), unload still runs (model release ungated).
        assert 'save' not in order
        assert order == ['approval', 'unload', 'run']
        # THE GUARD: no state/load is issued when the KV toggle is off.
        assert restore_calls == [], f'toggle-off must NOT issue a state/load: {restore_calls}'
        assert 'State KV Save/Restore' in result  # notification still explains the toggle
