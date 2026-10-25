"""Regression tests for call_agent resume without agent_class (BUG_0106, extended by BUG_0143).

Root cause (BUG_0106): ``_validate_call_agent_args`` gated on presence of ``agent_class``
with no pool lookup, so continuing an existing instance with only ``instance_name`` +
``task`` was rejected with ``Error: call_agent requires agent_class.`` even though the
downstream "continue on resume" path ignores the requested class.

BUG_0106 fix: the validator became POOL-AWARE — it required ``agent_class`` only when the
pool found no existing instance. That first cut accepted an omitted class ONLY for ACTIVE
targets, so an IDLE instance created earlier in the same session (the BUG_0143 report) was
still rejected.

BUG_0143 fix: any existing instance (ACTIVE, IDLE, or TERMINATED) accepts an omitted
``agent_class``; the validator derives the instance's stored class. New names still require
it, and the schema's ``required`` list no longer advertises ``agent_class`` unconditionally.

Self-contained: no LLM/API server. Uses a lightweight fake pool modelled on
test_call_agent_active_instance_message.py.
"""

import threading
from unittest.mock import MagicMock

from agent_cascade.agent_instance import AgentState
from agent_cascade.tool_dispatcher import ToolDispatcher


def _make_mock_instance(instance_name: str, agent_class: str = 'coder', state: AgentState = AgentState.IDLE):
    """Minimal mock AgentInstance. ``state`` is a REAL enum so ``.name`` and ACTIVE checks work."""
    inst = MagicMock()
    inst.instance_name = instance_name
    inst.agent_class = agent_class
    inst._state_lock = threading.RLock()
    inst.state = state
    inst._slot_release = None
    inst._nest_depth = 0
    return inst


class FakePool:
    """Lightweight fake AgentPool exposing only what the validator + handle_call_agent touch."""

    def __init__(self, instances=None, active_stack=None):
        self.instances = dict(instances or {})
        self._execution = MagicMock()
        self._execution._state_lock = threading.RLock()
        self._execution.active_stack = list(active_stack or [])
        self.api_router = None
        self.settings = MagicMock()
        self.settings.max_nesting_depth = 10
        self.templates = {'coder': MagicMock(), 'reviewer': MagicMock()}
        self._pool_lock = threading.RLock()
        self.enqueued = []

    def get_template(self, name: str):
        if name in self.templates:
            return self.templates[name]
        for key in self.templates:
            if key.lower() == name.lower():
                return self.templates[key]
        return None

    def list_agents(self):
        return list(self.templates.keys())

    def _resolve_instance_name(self, instance_name: str, exclude=None):
        instance_name = instance_name.strip()
        for name in self.instances:
            if name != exclude and name.lower() == instance_name.lower():
                return name
        return instance_name

    def get_instance(self, instance_name: str):
        return self.instances.get(instance_name.strip())

    def enqueue_message(self, instance_name: str, text: str):
        with self._pool_lock:
            self.enqueued.append((instance_name, text))


def _make_dispatcher(pool: FakePool):
    dispatcher = ToolDispatcher(pool)
    dispatcher.set_engine(MagicMock())
    dispatcher._run_child_sync = MagicMock(return_value='SYNC_ROUTED')
    dispatcher._run_child_async = MagicMock(return_value='ASYNC_ROUTED')
    pool.register_async_call = MagicMock()
    return dispatcher


def _run(dispatcher, caller, instance_name, agent_class, task='do the thing'):
    args = {'instance_name': instance_name, 'task': task}
    if agent_class is not None:
        args['agent_class'] = agent_class
    return dispatcher.handle_call_agent(args=args, messages=[], instance=caller)


# ── Validator-level: the actual defect site ──────────────────────────────────

def test_validator_accepts_resume_of_active_instance_without_class():
    """An ACTIVE existing instance with an omitted/empty agent_class must pass validation."""
    active = _make_mock_instance('reviewer_x', 'reviewer', state=AgentState.RUNNING)
    pool = FakePool(instances={'reviewer_x': active})
    td = _make_dispatcher(pool)

    inst, cls, err = td._validate_call_agent_args({'instance_name': 'reviewer_x'}, 'caller')
    assert err is None, f'active resume must be accepted, got: {err}'
    assert inst == 'reviewer_x'


def test_validator_derives_class_for_non_active_existing_instance():
    """BUG_0143: an existing IDLE instance accepts an omitted agent_class, derived from the
    instance itself. Its reuse path (lifecycle_manager) keeps the conversation and needs a
    class, but the instance already carries the only coherent one.

    This intentionally replaces the BUG_0106 pin that a non-ACTIVE target was rejected:
    that narrow rule contradicted the documented "continued regardless of agent_class"
    contract and reproduced the original friction for any finished sub-agent.
    """
    idle = _make_mock_instance('reviewer_x', 'reviewer', state=AgentState.IDLE)
    pool = FakePool(instances={'reviewer_x': idle})
    td = _make_dispatcher(pool)

    inst, cls, err = td._validate_call_agent_args({'instance_name': 'reviewer_x'}, 'caller')
    assert err is None, f'IDLE resume must be accepted, got: {err}'
    assert inst == 'reviewer_x'
    assert cls == 'reviewer', f'agent_class must be derived from the instance, got: {cls!r}'


def test_validator_derives_class_for_terminated_instance():
    """BUG_0143: TERMINATED is the other reuse state (lifecycle_manager reuses IDLE and
    TERMINATED), so an omitted class must be accepted there too."""
    dead = _make_mock_instance('reviewer_x', 'reviewer', state=AgentState.TERMINATED)
    pool = FakePool(instances={'reviewer_x': dead})
    td = _make_dispatcher(pool)

    _, cls, err = td._validate_call_agent_args({'instance_name': 'reviewer_x'}, 'caller')
    assert err is None, f'TERMINATED resume must be accepted, got: {err}'
    assert cls == 'reviewer'


def test_validator_derives_class_case_insensitively():
    """BUG_0143: the derived class is normalized like a caller-supplied one, and canonical
    name resolution is used (the instance stores the class as given at creation)."""
    idle = _make_mock_instance('Reviewer_X', 'Reviewer', state=AgentState.IDLE)
    pool = FakePool(instances={'Reviewer_X': idle})
    td = _make_dispatcher(pool)

    inst, cls, err = td._validate_call_agent_args({'instance_name': 'reviewer_x'}, 'caller')
    assert err is None, f'case-insensitive canonical resume must pass validation, got: {err}'
    assert inst == 'reviewer_x'  # validator returns the requested spelling; handler resolves canonical
    assert cls == 'reviewer', f'derived class must be lowercased like an explicit one, got: {cls!r}'


def test_validator_rejects_new_instance_without_class():
    """A brand-new instance_name (not in the pool) with no agent_class is still rejected."""
    pool = FakePool()  # empty pool
    td = _make_dispatcher(pool)

    _, _, err = td._validate_call_agent_args({'instance_name': 'brand_new_child'}, 'caller')
    assert err == 'Error: call_agent requires agent_class.'


def test_validator_rejects_missing_instance_name_regardless():
    """instance_name is always required — unchanged behaviour (guards the split branches)."""
    pool = FakePool()
    td = _make_dispatcher(pool)

    _, _, err_both = td._validate_call_agent_args({}, 'caller')
    assert err_both == 'Error: call_agent requires instance_name and agent_class.'

    _, _, err_no_inst = td._validate_call_agent_args({'agent_class': 'coder'}, 'caller')
    assert err_no_inst == 'Error: call_agent requires instance_name.'


def test_validator_with_no_pool_still_requires_class():
    """pool=None (validation-only context) must still reject a missing class."""
    td = ToolDispatcher(pool=None)
    _, _, err = td._validate_call_agent_args({'instance_name': 'child_1'}, 'caller')
    assert err == 'Error: call_agent requires agent_class.'


# ── End-to-end: the real scenario from the bug (active resume → message) ─────

def test_active_resume_without_class_delivers_message():
    """call_agent on an ACTIVE existing instance, omitting agent_class, messages it (not reject)."""
    active = _make_mock_instance('reviewer_x', 'reviewer', state=AgentState.RUNNING)
    pool = FakePool(instances={'reviewer_x': active}, active_stack=[('reviewer_x', 1)])
    dispatcher = _make_dispatcher(pool)
    caller = _make_mock_instance('main', 'orchestrator')

    result = _run(dispatcher, caller, instance_name='reviewer_x', agent_class=None)

    assert result.startswith('Message sent successfully'), f'expected message, got: {result}'
    assert pool.enqueued, 'expected the task to be enqueued to the active instance'


def test_control_new_call_without_class_still_errors():
    """Control in the same flow: a fresh name with no class errors (old guard intact)."""
    pool = FakePool()
    dispatcher = _make_dispatcher(pool)
    caller = _make_mock_instance('main', 'orchestrator')

    result = _run(dispatcher, caller, instance_name='brand_new_child', agent_class=None)
    assert result == 'Error: call_agent requires agent_class.'


# ── BUG_0143: IDLE resume must actually route with the derived class ─────────

def test_idle_resume_without_class_routes_with_derived_class():
    """The BUG_0143 report scenario: an instance created earlier in the session is IDLE.
    Calling it with only instance_name + task must pass validation AND dispatch with the
    instance's own class — not an empty string, which would fail the template/slot lookups.

    ``_run_child_async`` is NOT stubbed here: the assertion is on the real dispatch call, so
    the derived class is proven to reach ``register_async_call``.
    """
    idle = _make_mock_instance('reviewer_x', 'reviewer', state=AgentState.IDLE)
    pool = FakePool(instances={'reviewer_x': idle})
    dispatcher = ToolDispatcher(pool)
    dispatcher.set_engine(MagicMock())
    dispatcher._run_child_sync = MagicMock(return_value='SYNC_ROUTED')
    pool.register_async_call = MagicMock()
    caller = _make_mock_instance('main', 'orchestrator')

    result = _run(dispatcher, caller, instance_name='reviewer_x', agent_class=None)

    assert result.startswith("Agent 'reviewer_x' launched asynchronously"), f'expected async dispatch, got: {result}'
    assert not dispatcher._run_child_sync.called
    kwargs = pool.register_async_call.call_args.kwargs
    assert kwargs['agent_class'] == 'reviewer', f"derived class must be dispatched, got: {kwargs!r}"
    assert kwargs['child_instance_name'] == 'reviewer_x'


# ── BUG_0143: schema must not advertise agent_class as unconditionally required ──

def test_schema_does_not_require_agent_class():
    """The schema's ``required`` list contradicted the description ("Only required when
    starting a NEW instance") and the runtime contract. It must require instance_name/task
    only, while still exposing agent_class as an optional property."""
    from agent_cascade.prompts.dna import TOOL_METADATA
    from agent_cascade.tools._agent_instance_proxy import _AgentInstanceFunctionProxy

    required = TOOL_METADATA['call_agent']['required']
    assert 'agent_class' not in required, f'agent_class must not be unconditionally required: {required!r}'
    assert set(required) == {'instance_name', 'task'}

    schema = _AgentInstanceFunctionProxy('call_agent').parameters
    schema_required = schema['required']
    assert 'agent_class' not in schema_required, f'built schema still requires it: {schema_required!r}'
    assert 'agent_class' in schema['properties'], 'agent_class must remain an accepted property'
