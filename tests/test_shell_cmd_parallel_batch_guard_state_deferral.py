"""shell_cmd parallel-batch guard KV state-deferral — ENGINE level.

A turn_output carrying several guard-needing shell_cmd calls (i.e. commands the Security
advisor will review) has an identical caller context across them (no LLM generation between
tool calls). Only the FIRST guard needs to arm the caller save and only the LAST guard needs
to restore the caller's KV. Middle guards re-acquire the slot but DEFER the restore, keeping
_state_label PENDING. A mid-batch early exit (terminal-stop break) must be caught by the
engine guard-net, which forces exactly one /state/load.

These tests assert the OBSERVABLE HTTP-bearing state calls — state_ops.save_state (/state/save)
and state_ops.restore_state (/state/load) — NOT the reacquire_after_slot_yield plumbing call
counts (which pass even when the real restore no-ops; see [[defer-per-item-op-to-last-of-static-batch]]
and tests/test_call_agent_parallel_batch_state_deferral.py:9-12).

The batch is driven through the REAL ExecutionEngine._execute_detected_tools loop so the static
pre-scan (guard_indices, truncation at compress_context), the per-call guard-flag threading, and
the try/finally safety net are all exercised. The advisor thread itself is replaced by a fake
operation_manager.execute_shell_command that mirrors plan §3.2.5 inline (real engine.save_before_slot_yield
+ real engine.reacquire_after_slot_yield), so we test the engine's DECISION and the net's EFFECT.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.execution_engine import ExecutionEngine


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CALLER_NAME = 'caller1'
CALLER_CLASS = 'coder'
_ENDPOINT_CFG = {'state_save_enabled': True, 'api_base': 'http://caller-api', 'model': 'caller-model'}
_HELD_CFG = {'api_base': 'http://caller-api', 'model': 'caller-model'}

GUARD_CMDS = ('rm data.txt', 'curl -X POST http://x', 'del notes.txt')  # not read-only -> guard
SAFE_CMD = 'ls -la'


def _sc_msg(command, cwd='.'):
    return {
        'role': 'assistant',
        'function_call': {'name': 'shell_cmd', 'arguments': {'command': command, 'cwd': cwd}},
        'extra': {'function_id': f'fc_{command}'},
    }


def _compress_msg():
    return {
        'role': 'assistant',
        'function_call': {'name': 'compress_context', 'arguments': {'summary': 'compress'}},
        'extra': {'function_id': 'fc_compress'},
    }


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _SimpleInstance:
    """Plain instance double (NOT MagicMock) for the caller.

    A bare MagicMock auto-creates _safe_mode_kv / _state_label truthy, silently defeating the
    skip-save / defer gates (see [[wrong-shape-double-false-pass]]). These attrs are read by the
    real save/reacquire chain, so they must be real.
    """

    def __init__(self, instance_name, agent_class, *, slot_release=None, last_endpoint_config=None,
                 is_terminated=False):
        self.instance_name = instance_name
        self.agent_class = agent_class
        self._state_lock = threading.RLock()
        self._last_endpoint_config = last_endpoint_config
        self._state_label = None
        self._slot_release = slot_release
        self._slot_key = None
        self._safe_mode_kv = False
        self.is_terminated = is_terminated
        self.state = MagicMock(name=f'{instance_name}_state')


class _FakeOpManager:
    """operation_manager stand-in. The advisor thread is replaced by this: execute_shell_command
    reads the threaded batch guard flags (guard_is_first/guard_is_last), stamps a PendingApproval-style
    record, then runs the REAL engine save/defer primitives exactly as the security guard would
    (plan §3.2.5), and pops the record. evaluate_shell_command_safety drives the static scan.
    """

    def __init__(self, engine, caller, safe_cmds=()):
        self.engine = engine
        self.caller = caller
        self._safe_cmds = set(safe_cmds)
        self.records = {}
        self._rid = 0

    def evaluate_shell_command_safety(self, command, cwd, agent_name, **kwargs):
        return (command in self._safe_cmds), []

    def _get_approval_kv_batch_hints(self, request_id):
        return self.records.get(request_id, (False, False))

    def execute_shell_command(self, **kw):
        command = kw.get('command', '')
        if command in self._safe_cmds:  # auto-approved fast path: no advisor, no KV at all
            return 'ok (auto-approved)'
        guard_is_first = bool(kw.get('guard_is_first', True))
        guard_is_last = bool(kw.get('guard_is_last', True))
        kv_skip_save = not guard_is_first
        kv_defer_restore = not guard_is_last

        self._rid += 1
        rid = f'req_{self._rid}'
        self.records[rid] = (kv_skip_save, kv_defer_restore)

        # Mirror security_handler._execute_check §3.2.5 using the REAL engine primitives.
        safe = bool(getattr(self.caller, '_safe_mode_kv', False))
        batch_skip_save = kv_skip_save and not safe
        if not batch_skip_save:
            self.engine.save_before_slot_yield(self.caller, self.caller.instance_name, 'before_security_check')
        # PLAN §3.2.5 SPEC (verbatim): defer is gated ONLY on the safe-mode flag, never on
        # batch_skip_save — skipping this guard's save PRESERVES the first guard's pending label.
        # (The old `and not batch_skip_save` here mirrored production's CSSSSC bug, so the engine
        # test asserted the test-fake/production self-consistency instead of the spec.)
        batch_defer = kv_defer_restore and not safe
        self.engine.reacquire_after_slot_yield(
            self.caller, self.caller.instance_name, 'after_security_check',
            tolerate_failure=False, skip_kv_restore=safe, defer_kv_restore=batch_defer)

        self.records.pop(rid, None)
        return 'ok'


def _make_pool(caller):
    pool = MagicMock()
    pool.api_router = MagicMock()
    pool.settings = MagicMock()
    pool.settings.cache_threshold_chars = 10**9
    pool.auto_security = True
    pool.instances = {caller.instance_name: caller}
    pool.get_instance.side_effect = lambda name: pool.instances.get(name)

    def _get_template(agent_class):
        return None if agent_class == CALLER_CLASS else MagicMock(name=f'tpl_{agent_class}')
    pool.get_template.side_effect = _get_template
    pool.is_paused.return_value = False
    pool.telemetry = None
    pool.get_conversation.return_value = None
    pool._execution.active_stack = []
    pool._execution._state_lock = threading.RLock()
    return pool


def _make_engine(pool):
    engine = ExecutionEngine(pool)
    engine.compression_handler = None
    engine._append_and_log = lambda *a, **k: None
    engine._cache_tool_output = lambda *a, **k: None
    engine._rebuild_working_set = lambda *a, **k: None
    engine._resolve_placeholders = lambda args, iname, tool: args
    engine._cache_tool_args = lambda *a, **k: None
    engine._release_slot = lambda *a, **k: None
    engine._is_suspended_by_compression = lambda name: False
    engine._is_stopped = lambda name: False
    engine._wait_for_compression_to_clear = lambda name: True
    engine.reacquire_for = lambda instance, holder_name, context='reacquire': True
    engine._is_terminal_stop = lambda name: False
    return engine


def _wire_dispatcher(engine, op_manager, received_flags):
    """Replace tool_dispatcher.execute_tool with a thin shim that forwards the loop's threaded
    guard flags (is_first_guard/is_last_guard) and the command to the fake advisor seam. This is
    the seam where the loop's decision reaches shell_cmd; we assert on it AND on the downstream effect."""

    def _fake_execute_tool(instance, tool_name, tool_args, llm_messages, *,
                           function_id=None, is_first_ca=True, is_last_ca=True,
                           is_first_vram=True, is_last_vram=True,
                           is_first_guard=True, is_last_guard=True):
        received_flags.append((tool_name, is_first_guard, is_last_guard))
        if tool_name == 'compress_context':
            return 'compressed'
        args = tool_args if isinstance(tool_args, dict) else {}
        return op_manager.execute_shell_command(
            command=args.get('command', ''), cwd=args.get('cwd', '.'),
            guard_is_first=is_first_guard, guard_is_last=is_last_guard)

    engine.tool_dispatcher = MagicMock()
    engine.tool_dispatcher.execute_tool.side_effect = _fake_execute_tool
    return engine


def _setup(*, safe_cmds=(), terminal_stop_after_first=False):
    caller = _SimpleInstance(CALLER_NAME, CALLER_CLASS, slot_release=lambda: None,
                             last_endpoint_config=dict(_ENDPOINT_CFG))
    pool = _make_pool(caller)
    engine = _make_engine(pool)
    op_manager = _FakeOpManager(engine, caller, safe_cmds=safe_cmds)
    pool.operation_manager = op_manager
    received_flags = []
    _wire_dispatcher(engine, op_manager, received_flags)
    if terminal_stop_after_first:
        c = {'n': 0}

        def _ts(name):
            c['n'] += 1
            return c['n'] >= 2  # False on iteration 1, True on iteration 2 -> break before 2nd guard
        engine._is_terminal_stop = _ts
    return caller, engine, op_manager, received_flags


def _run(engine, caller, turn_output):
    engine._execute_detected_tools(
        instance=caller, inst_name=CALLER_NAME,
        turn_output=turn_output, messages=[], llm_messages=[], response=[])


# ---------------------------------------------------------------------------
# Test 1: multi-guard batch -> save ONCE, /state/load EXACTLY ONCE, label consumed
# ---------------------------------------------------------------------------


def test_multi_guard_batch_saves_once_and_loads_once():
    caller, engine, op_manager, flags = _setup()
    turn_output = [_sc_msg(c) for c in GUARD_CMDS]  # 3 guard-needing shell_cmd

    sv = rs = None
    with patch('agent_cascade.state_ops.save_state', side_effect=lambda api_base, model, name: name) as save, \
         patch('agent_cascade.state_ops.restore_state', return_value=True) as restore, \
         patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=_HELD_CFG), \
         patch('agent_cascade.state_ops.kv_save_effectively_enabled', return_value=True):
        sv, rs = save, restore
        _run(engine, caller, turn_output)

    assert sv.call_count == 1, f"first guard must save ONCE, got {sv.call_count}"
    assert rs.call_count == 1, f"/state/load must fire EXACTLY ONCE (last guard), got {rs.call_count}"
    assert caller._state_label is None, "last guard's full restore must consume the pending label"
    # Batch flags the loop threaded: guard1 first, guard3 last, guard2 middle.
    guard_flags = [f for f in flags if f[0] == 'shell_cmd']
    assert guard_flags == [('shell_cmd', True, False), ('shell_cmd', False, False), ('shell_cmd', False, True)], \
        f"loop must thread first/middle/last guard flags, got {guard_flags}"


# ---------------------------------------------------------------------------
# Test 1b: multi-guard batch -> the single /state/load fires AFTER the LAST guard
# (ORDERING, not count). The count-only test above is position-blind: under the old
# `and not batch_skip_save` formula guard2's forced full restore consumed guard1's label
# and loaded mid-batch, then guard3's restore no-op'd on the cleared label — net 1 save +
# 1 load + cleared label, IDENTICAL counts to the spec. Only the load's POSITION
# distinguishes them (defer-per-item-op-to-last-of-static-batch: "counts are
# position-blind"). The dispatcher appends its per-guard flag BEFORE calling
# execute_shell_command, and restore_state appends a 'load' marker DURING that call, so the
# combined event list orders each guard's approval against the load.
# ---------------------------------------------------------------------------


def test_multi_guard_batch_load_fires_after_last_guard():
    caller, engine, op_manager, flags = _setup()
    turn_output = [_sc_msg(c) for c in GUARD_CMDS]  # 3 guard-needing shell_cmd

    def _load_marker(*a, **k):
        flags.append(('load',))  # recorded inside execute_shell_command → after this guard's dispatch
        return True

    with patch('agent_cascade.state_ops.save_state', side_effect=lambda api_base, model, name: name), \
         patch('agent_cascade.state_ops.restore_state', side_effect=_load_marker), \
         patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=_HELD_CFG), \
         patch('agent_cascade.state_ops.kv_save_effectively_enabled', return_value=True):
        _run(engine, caller, turn_output)

    dispatches = [i for i, f in enumerate(flags) if f[0] == 'shell_cmd']
    loads = [i for i, f in enumerate(flags) if f[0] == 'load']
    assert len(loads) == 1, f"exactly one /state/load expected, got {flags}"
    assert len(dispatches) == 3, f"all three guards must be dispatched, got {flags}"
    # The single load must sit AFTER the LAST (third) guard's approval, i.e. after
    # dispatches[-1]. Under the buggy formula the load fired at the middle guard instead,
    # landing BEFORE the third dispatch → this assertion flips red.
    assert loads[0] > dispatches[-1], (
        f"/state/load must fire after the LAST guard's approval (dispatch index "
        f"{dispatches[-1]}), but fired at index {loads[0]} → event order {flags}")


# ---------------------------------------------------------------------------
# Test 2: single guard control -> 1 save + 1 load (byte-identical to today)
# ---------------------------------------------------------------------------


def test_single_guard_control_one_save_one_load():
    caller, engine, op_manager, flags = _setup()
    turn_output = [_sc_msg('rm only.txt')]

    with patch('agent_cascade.state_ops.save_state', side_effect=lambda api_base, model, name: name) as sv, \
         patch('agent_cascade.state_ops.restore_state', return_value=True) as rs, \
         patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=_HELD_CFG), \
         patch('agent_cascade.state_ops.kv_save_effectively_enabled', return_value=True):
        _run(engine, caller, turn_output)

    assert sv.call_count == 1, f"single guard must save ONCE, got {sv.call_count}"
    assert rs.call_count == 1, f"single guard must load ONCE, got {rs.call_count}"
    assert caller._state_label is None
    # A single guard is both first and last -> flags (True, True) -> unchanged behavior.
    assert flags == [('shell_cmd', True, True)], f"single guard must be first==last==True, got {flags}"


# ---------------------------------------------------------------------------
# Test 3: auto-approved shell_cmd -> 0 guard ops (no advisor run)
# ---------------------------------------------------------------------------


def test_auto_approved_shell_cmd_zero_guard_ops():
    caller, engine, op_manager, flags = _setup(safe_cmds=(SAFE_CMD,))
    turn_output = [_sc_msg(SAFE_CMD)]

    with patch('agent_cascade.state_ops.save_state', side_effect=lambda api_base, model, name: name) as sv, \
         patch('agent_cascade.state_ops.restore_state', return_value=True) as rs, \
         patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=_HELD_CFG), \
         patch('agent_cascade.state_ops.kv_save_effectively_enabled', return_value=True):
        _run(engine, caller, turn_output)

    # auto-approved -> the seam returns before any save/reacquire, so NO guard KV at all.
    assert sv.call_count == 0, f"auto-approved shell_cmd must NOT save, got {sv.call_count}"
    assert rs.call_count == 0, f"auto-approved shell_cmd must NOT load, got {rs.call_count}"


# ---------------------------------------------------------------------------
# Test 4: compress truncation — window split at compress_context (conservative, no sharing)
# ---------------------------------------------------------------------------


def test_compress_truncation_two_windows():
    caller, engine, op_manager, flags = _setup()
    turn_output = [_sc_msg('rm a.txt'), _compress_msg(), _sc_msg('rm b.txt')]

    with patch('agent_cascade.state_ops.save_state', side_effect=lambda api_base, model, name: name) as sv, \
         patch('agent_cascade.state_ops.restore_state', return_value=True) as rs, \
         patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=_HELD_CFG), \
         patch('agent_cascade.state_ops.kv_save_effectively_enabled', return_value=True):
        _run(engine, caller, turn_output)

    # Truncation is CONSERVATIVE: only the FIRST guard is in guard_indices (and is first+last of the
    # truncated window -> full save/restore). The second guard is AFTER compress, so it is NOT
    # classified and gets the full save/restore default. => 2 saves + 2 loads, no sharing across compress.
    assert sv.call_count == 2, f"each window owns its own save (truncation), got {sv.call_count}"
    assert rs.call_count == 2, f"each window owns its own load, got {rs.call_count}"
    assert caller._state_label is None
    # The first guard's window is truncated -> first==last==True; the post-compress guard is NOT in
    # guard_indices so it falls to the else branch (also True,True).
    guard_flags = [f for f in flags if f[0] == 'shell_cmd']
    assert guard_flags == [('shell_cmd', True, True), ('shell_cmd', True, True)], \
        f"both guards must be full save/restore due to truncation, got {guard_flags}"


# ---------------------------------------------------------------------------
# Test 5: mis-prediction conservatism — scan says guard, runtime auto-approves
# ---------------------------------------------------------------------------


def test_mis_prediction_no_crash_and_no_worse_state():
    caller, engine, op_manager, flags = _setup()
    # Force the scan to classify 'ls' as guard-needing (evaluate returns not-safe) while the tool
    # runtime auto-approves it: emulate by making it SAFE in the seam's fast path.
    op_manager._safe_cmds.add(SAFE_CMD)

    # The scan uses evaluate_shell_command_safety -> not in _safe_cmds at SCAN time would make it a
    # guard, but we added it to safe_cmds, so evaluate returns SAFE -> it is NOT in guard_indices.
    # To force a mis-prediction (scan says guard, runtime auto-approves), temporarily drop it from
    # the scan-safe set while keeping it runtime-auto-approve.
    saved = set(op_manager._safe_cmds)

    def _eval(command, cwd, agent_name, **kwargs):
        # scan sees 'ls' as GUARD (not safe) so it lands in guard_indices with the batch flags;
        # the seam below still fast-paths it as auto-approved.
        return False, []
    op_manager.evaluate_shell_command_safety = _eval
    # seam: ls is auto-approved -> returns early, no save/reacquire.
    try:
        with patch('agent_cascade.state_ops.save_state', side_effect=lambda api_base, model, name: name) as sv, \
             patch('agent_cascade.state_ops.restore_state', return_value=True) as rs, \
             patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=_HELD_CFG), \
             patch('agent_cascade.state_ops.kv_save_effectively_enabled', return_value=True):
            _run(engine, caller, [_sc_msg(SAFE_CMD)])
        # Survived without crash; a mis-predicted-guard that auto-approves at runtime issues NO KV
        # (it returns before the seam), and the label stays clean.
        assert sv.call_count == 0, f"auto-approved-at-runtime must not save, got {sv.call_count}"
        assert rs.call_count == 0, f"auto-approved-at-runtime must not load, got {rs.call_count}"
        assert caller._state_label is None
    finally:
        op_manager._safe_cmds = saved


# ---------------------------------------------------------------------------
# Test 6: terminal-stop before last guard -> engine net forces EXACTLY ONE load;
#         a completed batch -> net contributes zero.
# ---------------------------------------------------------------------------


def test_terminal_stop_before_last_guard_net_forces_one_load():
    caller, engine, op_manager, flags = _setup(terminal_stop_after_first=True)
    turn_output = [_sc_msg(c) for c in GUARD_CMDS]  # 3 guards; loop breaks before the 2nd

    with patch('agent_cascade.state_ops.save_state', side_effect=lambda api_base, model, name: name) as sv, \
         patch('agent_cascade.state_ops.restore_state', return_value=True) as rs, \
         patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=_HELD_CFG), \
         patch('agent_cascade.state_ops.kv_save_effectively_enabled', return_value=True):
        _run(engine, caller, turn_output)

    # guard1 saved + deferred (label pending); loop broke before guard3; the guard-net forces the one load.
    assert sv.call_count == 1, f"first guard must save ONCE, got {sv.call_count}"
    assert rs.call_count == 1, \
        f"guard safety net must force EXACTLY ONE /state/load after an early break, got {rs.call_count}"
    assert caller._state_label is None, 'the forced restore must consume the pending label'


def test_completed_batch_net_contributes_zero_loads():
    # Same 3-guard batch WITHOUT the terminal stop: guard3 does the full restore and clears the label,
    # so the net sees a None label and does NOT force an extra load (total loads == 1, not 2).
    caller, engine, op_manager, flags = _setup()
    turn_output = [_sc_msg(c) for c in GUARD_CMDS]

    with patch('agent_cascade.state_ops.save_state', side_effect=lambda api_base, model, name: name) as sv, \
         patch('agent_cascade.state_ops.restore_state', return_value=True) as rs, \
         patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=_HELD_CFG), \
         patch('agent_cascade.state_ops.kv_save_effectively_enabled', return_value=True):
        _run(engine, caller, turn_output)

    assert sv.call_count == 1
    # One load total => the last guard's restore only; the safety net contributed zero.
    assert rs.call_count == 1, \
        f"a completed batch must not double-fire the net (1 load total), got {rs.call_count}"
    assert caller._state_label is None


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
