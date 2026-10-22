"""Parallel media_gen batch VRAM deferral (plan media_gen_vram_batch_deferral).

A parent that issues several media_gen tool calls in ONE turn_output has an identical context
across them (no LLM generation between tool calls). Each media_gen call does a full save -> unload
-> generate -> restore VRAM cycle, but only the FIRST needs to save/unload (the model is free after
it) and only the LAST needs to swap the parent model back in. Non-last calls DEFER the restore (leaving
_state_label pending); a mid-batch early exit is caught by the try/finally safety net in
tool_execution._execute_detected_tools, which forces the deferred restore.

These tests target the ENGINE-LEVEL loop and its safety net specifically (the media_gen handler's own
save/restore gating is unit-tested in tests/tools/test_media_gen.py). They assert on the OBSERVABLE
HTTP-bearing state calls -- state_ops.save_state (the /state/save) and state_ops.restore_state (the
/state/load) -- NOT on plumbing call counts, mirroring
tests/test_call_agent_parallel_batch_state_deferral.py and the [[defer-per-item-op-to-last-of-static-batch]]
skill (test the effect, cover early-exit break AND raise, and prove the net does not over-fire on a
batch that legitimately reaches its last call).

The batch is driven through the REAL ExecutionEngine._execute_detected_tools loop so the single-source-of-
truth mg_indices pre-scan, the per-call is_first_vram/is_last_vram threading, and the try/finally safety net
are all exercised end-to-end. The dispatcher's execute_tool is replaced with a faithful simulator that
reproduces ONLY the media_gen VRAM seam's observable effect (save on is_first_vram, restore on is_last_vram)
using the REAL state_ops machinery -- so the label lifecycle is real while the generation/backend plumbing
(which the handler tests already cover) stays out of scope.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.execution_engine import ExecutionEngine
from agent_cascade.exceptions import AgentTerminatedError

# ---------------------------------------------------------------------------
# Test doubles (mirrors the call_agent batch harness)
# ---------------------------------------------------------------------------


class _SimpleInstance:
    """Plain instance double (NOT MagicMock) for the parent under test.

    A bare MagicMock auto-creates _state_label as a truthy stub and _last_endpoint_config as a non-dict,
    silently defeating the save/restore gates. The state machinery walks these attributes directly.
    """

    def __init__(self, instance_name, agent_class, *, last_endpoint_config=None,
                 is_terminated=False):
        self.instance_name = instance_name
        self.agent_class = agent_class
        self._state_lock = threading.RLock()
        self._last_endpoint_config = last_endpoint_config
        self._state_label = None
        self.is_terminated = is_terminated


_ENDPOINT_CFG = {'state_save_enabled': True, 'api_base': 'http://parent-api', 'model': 'parent-model'}
# Fixed held-endpoint the patched resolver returns, so the full-restore path reaches restore_state.
# Must MATCH _ENDPOINT_CFG so the explicit-label P5 endpoint-moved guard does not skip the restore.
_HELD_CFG = {'api_base': 'http://parent-api', 'model': 'parent-model'}

PARENT_NAME = 'parent1'
PARENT_CLASS = 'researcher'


def _mg_msg(prompt='a red cube'):
    """A turn_output assistant message carrying one media_gen tool call."""
    return {
        'role': 'assistant',
        'function_call': {
            'name': 'media_gen',
            'arguments': {'prompt': prompt, 'backend': 'sdcpp'},
        },
        'extra': {'function_id': f'fc_{prompt}'},
    }


def _ca_msg(instance_name='child1'):
    """A turn_output assistant message carrying one call_agent tool call (for the mixed-batch test)."""
    return {
        'role': 'assistant',
        'function_call': {
            'name': 'call_agent',
            'arguments': {'agent_class': 'generalist', 'instance_name': instance_name, 'task': 'do a thing'},
        },
        'extra': {'function_id': f'ca_{instance_name}'},
    }


def _make_pool(parent):
    """Mock AgentPool configured so _execute_detected_tools reaches the media_gen dispatch path."""
    pool = MagicMock()
    pool.api_router = MagicMock()

    pool.settings = MagicMock()
    pool.settings.cache_threshold_chars = 10**9

    pool.instances = {parent.instance_name: parent}
    pool.get_instance.side_effect = lambda name: pool.instances.get(name)

    # get_template: None for the parent so the loop skips the disabled-tools / auto-deny block
    # (a media_gen call must NOT be auto-denied here).
    pool.get_template.return_value = None

    pool.is_paused.return_value = False
    pool.telemetry = None  # _telemetry() -> None so telemetry blocks skip
    pool._mark_activity = MagicMock(return_value=None)
    pool._execution.active_stack = []
    pool._execution._state_lock = threading.RLock()
    return pool


def _make_engine(pool, parent):
    """Real ExecutionEngine with the heavy loop methods stubbed; real state machinery kept."""
    engine = ExecutionEngine(pool)
    engine.compression_handler = None  # skip the assemble_tool_result block
    engine._append_and_log = lambda *a, **k: None
    engine._cache_tool_output = lambda *a, **k: None
    engine._rebuild_working_set = lambda *a, **k: None
    engine._resolve_placeholders = lambda args, iname, tool: args
    engine._cache_tool_args = lambda *a, **k: None
    engine._is_suspended_by_compression = lambda name: False
    engine._is_stopped = lambda name: False
    engine._wait_for_compression_to_clear = lambda name: True
    engine._is_terminal_stop = lambda name: False  # no early exit by default
    return engine


def _make_sim_execute_tool(pool, parent, flags_seen):
    """Faithful stand-in for ToolDispatcher.execute_tool that reproduces ONLY the media_gen VRAM seam.

    Records the (is_first_vram, is_last_vram) the loop threaded for each call (so the pre-scan + per-call
    threading is verified), and drives the REAL state_ops so the label lifecycle is real: save arms _state_label
    on is_first_vram, restore consumes it on is_last_vram.
    """
    from agent_cascade.state_ops import save_instance_state, restore_held_slot_state

    def _sim(instance, tool_name, tool_args, llm_messages, *, function_id=None,
             is_first_ca=True, is_last_ca=True, is_first_vram=True, is_last_vram=True):
        assert tool_name == 'media_gen', f'unexpected tool routed through the media_gen sim: {tool_name}'
        flags_seen.append((is_first_vram, is_last_vram))
        if is_first_vram:
            save_instance_state(instance)
        if is_last_vram:
            restore_held_slot_state(pool, instance, instance.instance_name,
                                    label=instance.instance_name)
        return 'ok'
    return _sim


def _run_batch(engine, pool, parent, turn_output, flags_seen):
    engine.tool_dispatcher = MagicMock()
    engine.tool_dispatcher.execute_tool = _make_sim_execute_tool(pool, parent, flags_seen)
    engine._execute_detected_tools(
        instance=parent, inst_name=PARENT_NAME,
        turn_output=turn_output, messages=[], llm_messages=[], response=[])


def _state_ops_patches():
    """Patch the state_ops seams: the two HTTP calls + the held-endpoint resolver."""
    save = patch('agent_cascade.state_ops.save_state', side_effect=lambda api_base, model, name: name)
    restore = patch('agent_cascade.state_ops.restore_state', return_value=True)
    resolve = patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=_HELD_CFG)
    return save, restore, resolve


# ---------------------------------------------------------------------------
# Test 1: multi media_gen batch -> save ONCE, /state/load EXACTLY ONCE after the last call
# ---------------------------------------------------------------------------


def test_multi_batch_saves_once_and_loads_once():
    parent = _SimpleInstance(PARENT_NAME, PARENT_CLASS, last_endpoint_config=dict(_ENDPOINT_CFG))
    pool = _make_pool(parent)
    engine = _make_engine(pool, parent)
    flags_seen = []

    turn_output = [_mg_msg('a'), _mg_msg('b'), _mg_msg('c')]

    save, restore, resolve = _state_ops_patches()
    with save as sv, restore as rs, resolve:
        _run_batch(engine, pool, parent, turn_output, flags_seen)

    # The loop must have threaded first-only / last-only flags across the 3-call batch.
    assert flags_seen == [(True, False), (False, False), (False, True)], flags_seen
    assert sv.call_count == 1, f"media_gen save must fire ONCE (first call only), got {sv.call_count}"
    assert rs.call_count == 1, f"/state/load must fire EXACTLY ONCE (last call), got {rs.call_count}"
    assert parent._state_label is None, "pending label must be consumed by the last call's restore"


# ---------------------------------------------------------------------------
# Test 2: single media_gen control -> 1 save + 1 /state/load (byte-identical to today)
# ---------------------------------------------------------------------------


def test_single_media_gen_control_one_save_one_load():
    parent = _SimpleInstance(PARENT_NAME, PARENT_CLASS, last_endpoint_config=dict(_ENDPOINT_CFG))
    pool = _make_pool(parent)
    engine = _make_engine(pool, parent)
    flags_seen = []

    turn_output = [_mg_msg('a')]

    save, restore, resolve = _state_ops_patches()
    with save as sv, restore as rs, resolve:
        _run_batch(engine, pool, parent, turn_output, flags_seen)

    assert flags_seen == [(True, True)], flags_seen
    assert sv.call_count == 1, f"single media_gen must save ONCE, got {sv.call_count}"
    assert rs.call_count == 1, f"single media_gen must load ONCE, got {rs.call_count}"
    assert parent._state_label is None


# ---------------------------------------------------------------------------
# Test 3: terminal-stop mid-batch -> safety net forces exactly one /state/load
# ---------------------------------------------------------------------------


def test_terminal_stop_mid_batch_forces_one_load():
    parent = _SimpleInstance(PARENT_NAME, PARENT_CLASS, last_endpoint_config=dict(_ENDPOINT_CFG))
    pool = _make_pool(parent)
    engine = _make_engine(pool, parent)
    flags_seen = []

    # Break path: first call runs (defers), then the loop breaks before the last media_gen.
    calls = {'n': 0}

    def _terminal_stop(name):
        calls['n'] += 1
        return calls['n'] >= 2  # False on iteration 1, True on iteration 2 -> break

    engine._is_terminal_stop = _terminal_stop

    turn_output = [_mg_msg('a'), _mg_msg('b')]

    save, restore, resolve = _state_ops_patches()
    with save as sv, restore as rs, resolve:
        _run_batch(engine, pool, parent, turn_output, flags_seen)

    assert flags_seen == [(True, False)], flags_seen  # only the first call ran
    assert sv.call_count == 1, f"first call must save ONCE, got {sv.call_count}"
    assert rs.call_count == 1, (
        f"safety net must force EXACTLY ONE /state/load after an early break, got {rs.call_count}")
    assert parent._state_label is None, 'the forced restore must consume the pending label'


# ---------------------------------------------------------------------------
# Test 4: AgentTerminatedError mid-batch -> safety net forces exactly one /state/load
# ---------------------------------------------------------------------------


def test_raise_mid_batch_forces_one_load():
    parent = _SimpleInstance(PARENT_NAME, PARENT_CLASS, last_endpoint_config=dict(_ENDPOINT_CFG))
    pool = _make_pool(parent)
    engine = _make_engine(pool, parent)
    flags_seen = []

    # Raise path: first call runs (defers); on iteration 2 the instance is seen terminated
    # -> AgentTerminatedError raised at the get_instance/is_terminated check.
    calls = {'n': 0}

    def _terminal_stop(name):
        calls['n'] += 1
        if calls['n'] >= 2:
            parent.is_terminated = True
        return False

    engine._is_terminal_stop = _terminal_stop

    turn_output = [_mg_msg('a'), _mg_msg('b')]

    save, restore, resolve = _state_ops_patches()
    with save as sv, restore as rs, resolve:
        with pytest.raises(AgentTerminatedError):
            _run_batch(engine, pool, parent, turn_output, flags_seen)

    assert flags_seen == [(True, False)], flags_seen
    assert sv.call_count == 1, f"first call must save ONCE, got {sv.call_count}"
    assert rs.call_count == 1, (
        f"safety net must force EXACTLY ONE /state/load after the AgentTerminatedError raise, "
        f"got {rs.call_count}")
    assert parent._state_label is None, 'the forced restore must consume the pending label'


# ---------------------------------------------------------------------------
# Test 5: no-save batch (first call's save fails) -> safety net must NOT over-fire
# ---------------------------------------------------------------------------


def test_no_save_batch_safety_net_does_not_overfire():
    # If the first call's save fails, no label is armed and there is nothing to restore. The net is gated
    # on a pending _state_label, so it must NOT fire (this is the "legitimate no-restore end state" guard).
    parent = _SimpleInstance(PARENT_NAME, PARENT_CLASS, last_endpoint_config=dict(_ENDPOINT_CFG))
    pool = _make_pool(parent)
    engine = _make_engine(pool, parent)
    flags_seen = []

    calls = {'n': 0}

    def _terminal_stop(name):
        calls['n'] += 1
        return calls['n'] >= 2  # early break after the first call, like test 3

    engine._is_terminal_stop = _terminal_stop

    turn_output = [_mg_msg('a'), _mg_msg('b')]

    # save_state returns no label -> save_instance_state returns False, _state_label stays None.
    save = patch('agent_cascade.state_ops.save_state', return_value=None)
    restore = patch('agent_cascade.state_ops.restore_state', return_value=True)
    resolve = patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=_HELD_CFG)
    with save, restore as rs, resolve:
        _run_batch(engine, pool, parent, turn_output, flags_seen)

    assert rs.call_count == 0, (
        f"the net must NOT fire when no save happened (no pending label), got {rs.call_count}")
    assert parent._state_label is None


# ---------------------------------------------------------------------------
# Test 6: mixed batch [media_gen, call_agent], early exit before the call_agent
#         -> exactly ONE /state/load (the two safety nets share one _state_label)
# ---------------------------------------------------------------------------


def test_mixed_batch_early_exit_before_call_agent_loads_once():
    # A batch with a single media_gen followed by a call_agent, where the loop exits BEFORE processing
    # the call_agent. This guards the SHARED finally: both safety nets live there and share one _state_label.
    # The media_gen here is first==last of its OWN batch, so its last call performs the full restore itself
    # and clears _state_label. The media net is therefore suppressed by BOTH of its gates — the label gate
    # (the consumed _state_label is None) AND the flow-marker gate (reached_last_mg was set True before
    # dispatch). For the media net the LABEL gate is the load-bearing one (the last call always restores on
    # every success/skip path); the flow marker is belt-and-suspenders. The call_agent never runs, so ran_ca
    # stays False and the call_agent net is inert. Net effect: EXACTLY ONE /state/load, from the media_gen's
    # own restore — the shared finally must NOT turn that into two.
    parent = _SimpleInstance(PARENT_NAME, PARENT_CLASS, last_endpoint_config=dict(_ENDPOINT_CFG))
    pool = _make_pool(parent)
    engine = _make_engine(pool, parent)
    flags_seen = []

    # Break before index 1 (the call_agent): iteration 1 (media_gen) runs, iteration 2 breaks.
    calls = {'n': 0}

    def _terminal_stop(name):
        calls['n'] += 1
        return calls['n'] >= 2

    engine._is_terminal_stop = _terminal_stop

    turn_output = [_mg_msg('a'), _ca_msg('child1')]

    save, restore, resolve = _state_ops_patches()
    with save as sv, restore as rs, resolve:
        _run_batch(engine, pool, parent, turn_output, flags_seen)

    # Only the media_gen ran; it was first==last of its own batch, so it restored itself.
    assert flags_seen == [(True, True)], flags_seen
    assert sv.call_count == 1, f"the single media_gen must save ONCE, got {sv.call_count}"
    assert rs.call_count == 1, (
        f"exactly ONE /state/load across the mixed batch (media net must NOT double-fire after the "
        f"last media_gen restored itself, and the call_agent net must stay inert), got {rs.call_count}")
    assert parent._state_label is None, 'the media_gen restore consumed the label; no net may re-fire'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
