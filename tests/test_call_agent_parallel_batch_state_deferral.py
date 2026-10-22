"""Parallel call_agent batch state-reload deferral (plan skip-state-reload-parallel-callagent).

A parent that issues several call_agent tool calls in ONE turn_output has an identical
context across them (no LLM generation between tool calls). Only the FIRST child needs to
arm the parent save, and only the LAST child needs to swap the parent model back in. Non-last
children re-acquire their slot but DEFER the KV restore (keeping _state_label pending); a mid-batch
early exit self-heals at the next turn's _setup_turn.

These tests assert on the OBSERVABLE HTTP-bearing state calls — state_ops.save_state (the
/state/save) and state_ops.restore_state (the /state/load) — NOT on reacquire_after_slot_yield
plumbing call counts (which pass even when the real restore no-ops; see
[[wrong-shape-double-false-pass]] and [[defer-per-item-op-to-last-of-static-batch]]).

The batch is driven through the REAL ExecutionEngine._execute_detected_tools loop so the
single-source-of-truth pre-scan, the per-call flag threading, and the try/finally safety net
are all exercised end-to-end. No LLM, no network: the child runner and the state HTTP layer are
patched; the slot machinery is stubbed at reacquire_for.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.execution_engine import ExecutionEngine
from agent_cascade.exceptions import AgentTerminatedError

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _SimpleInstance:
    """Plain instance double (NOT MagicMock) for the parent under test.

    A bare MagicMock would auto-create _state_label as a truthy stub and _last_endpoint_config
    as a non-dict, silently defeating the save/restore gates. The state machinery walks these
    attributes directly, so they are real. See [[wrong-shape-double-false-pass]].
    """

    def __init__(self, instance_name, agent_class, *, slot_release=None,
                 last_endpoint_config=None, is_terminated=False, nest_depth=0):
        self.instance_name = instance_name
        self.agent_class = agent_class
        self._state_lock = threading.RLock()
        self._last_endpoint_config = last_endpoint_config
        self._state_label = None
        self._slot_release = slot_release
        self._nest_depth = nest_depth
        self.parent_instance = None
        self._slot_key = None
        self.is_terminated = is_terminated
        self.state = MagicMock(name=f"{instance_name}_state")


_ENDPOINT_CFG = {'state_save_enabled': True, 'api_base': 'http://parent-api', 'model': 'parent-model'}
# Fixed held-endpoint the patched resolver returns, so the full-restore path reaches restore_state.
_HELD_CFG = {'api_base': 'http://parent-api', 'model': 'parent-model'}

CALLER_NAME = 'parent1'
CALLER_CLASS = 'researcher'
SYNC_CHILD_CLASS = 'coder'
ASYNC_CHILD_CLASS = 'ponytail'


def _ca_msg(child_name, child_class, task='do the thing'):
    """A turn_output assistant message carrying one call_agent tool call."""
    return {
        'role': 'assistant',
        'function_call': {
            'name': 'call_agent',
            'arguments': {'instance_name': child_name, 'agent_class': child_class, 'task': task},
        },
        'extra': {'function_id': f'fc_{child_name}'},
    }


def _make_pool(caller, *, allow_parallel=True, real_router=None):
    """Mock AgentPool configured so handle_call_agent reaches the save + routing path.

    Every attribute the code walks is set EXPLICITLY (a MagicMock truthy default would
    defeat a gate; see [[magicmock-engine-defeats-terminal-stop-gate]]).
    """
    pool = MagicMock()
    pool.api_router = real_router if real_router is not None else MagicMock()

    pool.settings = MagicMock()
    pool.settings.allow_parallel_agents = allow_parallel
    pool.settings.max_nesting_depth = 10
    pool.settings.cache_threshold_chars = 10**9

    pool.instances = {caller.instance_name: caller}
    pool.get_instance.side_effect = lambda name: pool.instances.get(name)

    def _resolve(name, exclude=None):
        name = name.strip()
        for n in pool.instances:
            if n != exclude and n.lower() == name.lower():
                return n
        return name
    pool._resolve_instance_name.side_effect = _resolve

    # get_template: None for the caller (so the loop skips the disabled-tools block),
    # truthy for child classes (so the unknown-class guard does not reject the spawn).
    def _get_template(agent_class):
        if agent_class == CALLER_CLASS:
            return None
        return MagicMock(name=f'template_{agent_class}')
    pool.get_template.side_effect = _get_template
    pool.list_agents.return_value = [CALLER_CLASS, SYNC_CHILD_CLASS, ASYNC_CHILD_CLASS]

    pool.is_paused.return_value = False
    pool.telemetry = None  # _telemetry() -> None so telemetry blocks skip
    pool._mark_activity = MagicMock(return_value=None)
    pool.register_async_call = MagicMock()
    pool.get_conversation.return_value = None

    # P2 stacked-name guard iterates this — must be a real empty list.
    pool._execution.active_stack = []
    pool._execution._state_lock = threading.RLock()
    return pool


def _make_engine(pool, caller):
    """Real ExecutionEngine with the heavy loop methods stubbed; real state machinery kept."""
    engine = ExecutionEngine(pool)
    engine.compression_handler = None  # skip the assemble_tool_result block
    engine._append_and_log = lambda *a, **k: None
    engine._cache_tool_output = lambda *a, **k: None
    engine._rebuild_working_set = lambda *a, **k: None
    engine._resolve_placeholders = lambda args, iname, tool: args
    engine._cache_tool_args = lambda *a, **k: None
    engine._release_slot = lambda *a, **k: None
    engine._is_suspended_by_compression = lambda name: False
    engine._is_stopped = lambda name: False
    engine._wait_for_compression_to_clear = lambda name: True
    # Re-acquire the slot without touching the real FIFO (the deferral/restore logic is what we test).
    engine.reacquire_for = lambda instance, holder_name, context='reacquire': True
    # _is_terminal_stop is the loop's early-exit gate — default False (no early exit).
    engine._is_terminal_stop = lambda name: False
    return engine


def _run_batch(engine, caller, turn_output):
    engine._execute_detected_tools(
        instance=caller, inst_name=CALLER_NAME,
        turn_output=turn_output, messages=[], llm_messages=[], response=[])


def _state_ops_patches():
    """Patch the three state_ops seams: the two HTTP calls + the held-endpoint resolver."""
    save = patch('agent_cascade.state_ops.save_state', side_effect=lambda api_base, model, name: name)
    restore = patch('agent_cascade.state_ops.restore_state', return_value=True)
    resolve = patch('agent_cascade.state_ops.resolve_held_endpoint', return_value=_HELD_CFG)
    return save, restore, resolve


def _child_runner_patch():
    return patch('agent_cascade.child_runner.run_child_core', return_value='child done')


# ---------------------------------------------------------------------------
# Test 1: multi-SYNC batch → save ONCE, /state/load EXACTLY ONCE after the last child
# ---------------------------------------------------------------------------


def test_multi_sync_batch_saves_once_and_loads_once():
    caller = _SimpleInstance(CALLER_NAME, CALLER_CLASS, slot_release=lambda: None,
                             last_endpoint_config=dict(_ENDPOINT_CFG))
    pool = _make_pool(caller, allow_parallel=False)  # force SYNC for both children
    engine = _make_engine(pool, caller)

    turn_output = [
        _ca_msg('childA', SYNC_CHILD_CLASS),
        _ca_msg('childB', SYNC_CHILD_CLASS),
    ]

    save, restore, resolve = _state_ops_patches()
    with save as sv, restore as rs, resolve, _child_runner_patch():
        _run_batch(engine, caller, turn_output)

    assert sv.call_count == 1, f"parent save must fire ONCE (first child only), got {sv.call_count}"
    assert rs.call_count == 1, f"/state/load must fire EXACTLY ONCE (last child), got {rs.call_count}"
    # The last child's full restore consumed the label → it is cleared at end of batch.
    assert caller._state_label is None, "pending label must be consumed by the last child's restore"


# ---------------------------------------------------------------------------
# Test 2: single call_agent control → 1 save + 1 /state/load (must match today)
# ---------------------------------------------------------------------------


def test_single_call_agent_control_one_save_one_load():
    caller = _SimpleInstance(CALLER_NAME, CALLER_CLASS, slot_release=lambda: None,
                             last_endpoint_config=dict(_ENDPOINT_CFG))
    pool = _make_pool(caller, allow_parallel=False)
    engine = _make_engine(pool, caller)

    turn_output = [_ca_msg('childA', SYNC_CHILD_CLASS)]

    save, restore, resolve = _state_ops_patches()
    with save as sv, restore as rs, resolve, _child_runner_patch():
        _run_batch(engine, caller, turn_output)

    assert sv.call_count == 1, f"single call_agent must save ONCE, got {sv.call_count}"
    assert rs.call_count == 1, f"single call_agent must load ONCE, got {rs.call_count}"
    assert caller._state_label is None


# ---------------------------------------------------------------------------
# Test 3: terminal-stop / termination mid-batch → safety net forces exactly one /state/load
# ---------------------------------------------------------------------------


def test_terminal_stop_mid_batch_forces_one_load():
    caller = _SimpleInstance(CALLER_NAME, CALLER_CLASS, slot_release=lambda: None,
                             last_endpoint_config=dict(_ENDPOINT_CFG))
    pool = _make_pool(caller, allow_parallel=False)
    engine = _make_engine(pool, caller)

    # Break path: first child runs (defers), then the loop breaks before the last call_agent.
    calls = {'n': 0}

    def _terminal_stop(name):
        calls['n'] += 1
        return calls['n'] >= 2  # False on iteration 1, True on iteration 2 -> break

    engine._is_terminal_stop = _terminal_stop

    turn_output = [
        _ca_msg('childA', SYNC_CHILD_CLASS),
        _ca_msg('childB', SYNC_CHILD_CLASS),
    ]

    save, restore, resolve = _state_ops_patches()
    with save as sv, restore as rs, resolve, _child_runner_patch():
        _run_batch(engine, caller, turn_output)

    assert sv.call_count == 1, f"first child must save ONCE, got {sv.call_count}"
    assert rs.call_count == 1, (
        f"safety net must force EXACTLY ONE /state/load after an early break, got {rs.call_count}")
    assert caller._state_label is None, 'the forced restore must consume the pending label'


def test_raise_mid_batch_forces_one_load():
    caller = _SimpleInstance(CALLER_NAME, CALLER_CLASS, slot_release=lambda: None,
                             last_endpoint_config=dict(_ENDPOINT_CFG))
    pool = _make_pool(caller, allow_parallel=False)
    engine = _make_engine(pool, caller)

    # Raise path: first child runs (defers); on iteration 2 the instance is seen terminated
    # -> AgentTerminatedError raised at the get_instance/is_terminated check.
    calls = {'n': 0}

    def _terminal_stop(name):
        calls['n'] += 1
        if calls['n'] >= 2:
            caller.is_terminated = True  # trip the :131-133 raise, but do NOT break
        return False

    engine._is_terminal_stop = _terminal_stop

    turn_output = [
        _ca_msg('childA', SYNC_CHILD_CLASS),
        _ca_msg('childB', SYNC_CHILD_CLASS),
    ]

    save, restore, resolve = _state_ops_patches()
    with save as sv, restore as rs, resolve, _child_runner_patch():
        with pytest.raises(AgentTerminatedError):
            _run_batch(engine, caller, turn_output)

    assert sv.call_count == 1, f"first child must save ONCE, got {sv.call_count}"
    assert rs.call_count == 1, (
        f"safety net must force EXACTLY ONE /state/load after the AgentTerminatedError raise, "
        f"got {rs.call_count}")
    assert caller._state_label is None, 'the forced restore must consume the pending label'


# ---------------------------------------------------------------------------
# Test 4: mixed sync/async batch (last is async) → no in-batch load; label self-heals
# ---------------------------------------------------------------------------


def test_mixed_batch_last_async_no_in_batch_load():
    caller = _SimpleInstance(CALLER_NAME, CALLER_CLASS, slot_release=lambda: None,
                             last_endpoint_config=dict(_ENDPOINT_CFG))

    # Real router so the FIRST child routes SYNC (conc=0) and the LAST routes ASYNC (conc=-1).
    router = MagicMock()

    def _slot_info(agent_class):
        if agent_class == SYNC_CHILD_CLASS:
            return {'needs_slot': True, 'is_sequential': True}  # conc=0 -> SYNC
        return {'needs_slot': False}  # conc=-1 -> ASYNC
    router.get_agent_slot_info.side_effect = _slot_info

    pool = _make_pool(caller, allow_parallel=True, real_router=router)
    engine = _make_engine(pool, caller)

    turn_output = [
        _ca_msg('childA', SYNC_CHILD_CLASS),   # first, sync -> save + defer
        _ca_msg('childB', ASYNC_CHILD_CLASS),  # last, async -> no save, no in-batch restore
    ]

    save, restore, resolve = _state_ops_patches()
    with save as sv, restore as rs, resolve, _child_runner_patch():
        _run_batch(engine, caller, turn_output)

    assert sv.call_count == 1, f"only the first child may save, got {sv.call_count}"
    assert rs.call_count == 0, (
        f"no in-batch /state/load when the last call_agent is async, got {rs.call_count}")
    # The label stays pending — it self-heals at the next turn's _setup_turn.
    assert caller._state_label == CALLER_NAME, 'label must remain pending for the next-turn self-heal'
    # The last child genuinely routed async.
    assert pool.register_async_call.called, 'last call_agent must have taken the async path'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
