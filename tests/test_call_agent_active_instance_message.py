"""Regression tests for todo.md #141 — call_agent on an ACTIVE instance must MESSAGE, not reject/clone.

The fix (ToolDispatcher.handle_call_agent) inserts a branch *before* the P2 stacked-name clone:
when the target resolves to an existing ACTIVE instance (RUNNING/SLEEPING/COMPLETING), the task is
delivered as a message via pool.enqueue_message() — identical to what the send_message tool does —
instead of being rejected (old behaviour told the LLM to retry with '{name}_child') or silently
cloned into a shadow '_child{N}' agent.

Key regression: an ACTIVE instance is by definition on the execution stack, so the old P2 clone ran
FIRST and renamed `instance_name` → '{name}_child{N}', defeating the guard and spawning a shadow
child with no error at all. Placing the new branch before P2 (and keying the lookup off the
canonical name) fixes that half.

All tests are self-contained — no LLM or API server required. Uses a lightweight fake pool modelled
on test_call_agent_self_and_resurrection_guard.py, but with:
  - a REAL enqueue_message that records into a list (plus _pool_lock = threading.RLock()), and
  - instances whose .state is a REAL AgentState enum value (not a MagicMock), because the branch
    reads target_state.name and checks `in ACTIVE_STATES`.
"""

import threading
from unittest.mock import MagicMock

from agent_cascade.agent_instance import AgentState
from agent_cascade.tool_dispatcher import ToolDispatcher

# ──────────────────────────────────────────────
# Test Helpers — lightweight fakes
# ──────────────────────────────────────────────


def _make_mock_instance(instance_name: str, agent_class: str = 'coder', state: AgentState = AgentState.IDLE):
    """Minimal mock AgentInstance with the attributes handle_call_agent touches.

    `state` is a REAL AgentState enum value so `.name` and `in ACTIVE_STATES` behave correctly.
    """
    inst = MagicMock()
    inst.instance_name = instance_name
    inst.agent_class = agent_class
    inst._state_lock = threading.RLock()
    inst.state = state
    inst._slot_release = None
    inst._nest_depth = 0
    return inst


class FakePool:
    """Lightweight fake AgentPool exposing only what handle_call_agent needs.

    Adds a REAL enqueue_message (records into self.enqueued) and _pool_lock, so the new
    message branch is exercised for real rather than absorbed by a MagicMock.
    """

    def __init__(self, instances=None, active_stack=None):
        self.instances = dict(instances or {})
        self.instance_classes = {n: i.agent_class for n, i in self.instances.items()}
        self._execution = MagicMock()
        self._execution._state_lock = threading.RLock()
        self._execution.active_stack = list(active_stack or [])
        self.api_router = None  # → child needs no slot → ASYNC path
        self.settings = MagicMock()
        self.settings.max_nesting_depth = 10
        # Template registry — the unknown-class guard calls pool.get_template()/list_agents().
        # Default to a small set of valid classes so legitimate routing tests pass the guard.
        self.templates = {
            'coder': MagicMock(),
            'orchestrator': MagicMock(),
            'reviewer': MagicMock(),
        }
        # Message queue (real, recording) — used by the new ACTIVE→message branch.
        self._pool_lock = threading.RLock()
        self.enqueued = []

    def get_template(self, name: str):
        """Case-insensitive fallback mirroring pool/config_persist.py."""
        if name in self.templates:
            return self.templates[name]
        for key in self.templates:
            if key.lower() == name.lower():
                return self.templates[key]
        return None

    def list_agents(self):
        return list(self.templates.keys())

    def _resolve_instance_name(self, instance_name: str, exclude=None):
        """Case-insensitive resolution mirroring pool/lifecycle.py."""
        instance_name = instance_name.strip()
        for name in self.instances:
            if name != exclude and name.lower() == instance_name.lower():
                return name
        return instance_name

    def get_instance(self, instance_name: str):
        return self.instances.get(instance_name.strip())

    def enqueue_message(self, instance_name: str, text: str):
        """Record an enqueued message (name, text) — mirrors the real queue append."""
        with self._pool_lock:
            self.enqueued.append((instance_name, text))


def _make_dispatcher(pool: FakePool):
    """Create a dispatcher with routing methods stubbed so tests can assert on them."""
    dispatcher = ToolDispatcher(pool)
    dispatcher.set_engine(MagicMock())
    # Stub the routing endpoints: rejections must never reach these; legitimate
    # calls must. (pool.register_async_call is also stubbed as a backstop in case
    # _run_child_async runs for real.)
    dispatcher._run_child_sync = MagicMock(return_value='SYNC_ROUTED')
    dispatcher._run_child_async = MagicMock(return_value='ASYNC_ROUTED')
    pool.register_async_call = MagicMock()
    return dispatcher


def _run(dispatcher, caller, instance_name, agent_class, task='test'):
    """Drive handle_call_agent and return the result string."""
    return dispatcher.handle_call_agent(
        args={
            'instance_name': instance_name,
            'agent_class': agent_class,
            'task': task
        },
        messages=[],
        instance=caller,
    )


# ──────────────────────────────────────────────
# 1. ACTIVE target → message (not rejection / not clone)
# ──────────────────────────────────────────────


class TestActiveTargetMessaged:
    """An existing ACTIVE instance receives the task as a message, not a rejection or clone."""

    def test_active_target_receives_message_not_rejection(self):
        """#1 Target RUNNING → non-Error result; enqueue_message called with tagged text; no spawn."""
        target = _make_mock_instance('worker1', 'coder', state=AgentState.RUNNING)
        caller = _make_mock_instance('Maine', 'orchestrator')
        pool = FakePool(instances={'worker1': target, 'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, 'worker1', 'coder', task='do the thing')

        assert not result.startswith('Error:')
        assert pool.enqueued == [('worker1', '[MESSAGE from Maine]: do the thing')]
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()

    def test_stacked_active_target_is_messaged_not_cloned(self):
        """#2 KEY REGRESSION: target RUNNING AND on the execution stack → message, NOT a _child1 clone.

        This is the exact silent-clone bug from todo #141: P2 used to rename 'worker1' →
        'worker1_child1' before the guard could see it. The new branch must run first and key off
        the canonical name so the live instance is found and messaged.
        """
        target = _make_mock_instance('worker1', 'coder', state=AgentState.RUNNING)
        caller = _make_mock_instance('Maine', 'orchestrator')
        pool = FakePool(
            instances={'worker1': target, 'Maine': caller},
            active_stack=[('worker1', 1)],
        )
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, 'worker1', 'coder', task='continue')

        assert not result.startswith('Error:')
        # No shadow child was spawned under any *_child* name.
        assert not any(n.startswith('worker1_child') for n in pool.instances), \
            f"Shadow child created: {list(pool.instances)}"
        # enqueue_message was called with the ORIGINAL canonical name, not a clone.
        assert ('worker1', '[MESSAGE from Maine]: continue') in pool.enqueued
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()

    def test_sleeping_target_wakes_with_message(self):
        """#3 Target SLEEPING → same enqueue; message delivered (no spawn)."""
        target = _make_mock_instance('worker1', 'coder', state=AgentState.SLEEPING)
        caller = _make_mock_instance('Maine', 'orchestrator')
        pool = FakePool(instances={'worker1': target, 'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, 'worker1', 'coder', task='wake up and do X')

        assert not result.startswith('Error:')
        assert ('worker1', '[MESSAGE from Maine]: wake up and do X') in pool.enqueued
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()

    def test_completing_target_enqueued_with_state_noted(self):
        """#4 Target COMPLETING → enqueued (no special reject); message delivered (no spawn)."""
        target = _make_mock_instance('worker1', 'coder', state=AgentState.COMPLETING)
        caller = _make_mock_instance('Maine', 'orchestrator')
        pool = FakePool(instances={'worker1': target, 'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, 'worker1', 'coder', task='one more thing')

        assert not result.startswith('Error:')
        assert ('worker1', '[MESSAGE from Maine]: one more thing') in pool.enqueued
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()


# ──────────────────────────────────────────────
# 2. Self-call still rejected (never messaged)
# ──────────────────────────────────────────────


class TestSelfCallStillRejected:
    """The self-call guard runs before the new branch, so self-calls are never turned into messages."""

    def test_self_call_still_rejected_not_messaged(self):
        """#5 Caller 'Maine' targets 'Maine' (RUNNING) → Error; enqueue_message NOT called."""
        caller = _make_mock_instance('Maine', 'orchestrator', state=AgentState.RUNNING)
        pool = FakePool(instances={'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, 'Maine', 'orchestrator')

        assert result.startswith('Error:')
        assert pool.enqueued == []
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()

    def test_self_call_case_variant_still_rejected(self):
        """#6 Requesting 'maine' when canonical is 'Maine' (RUNNING) → rejected, no enqueue."""
        caller = _make_mock_instance('Maine', 'orchestrator', state=AgentState.RUNNING)
        pool = FakePool(instances={'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, 'maine', 'orchestrator')

        assert result.startswith('Error:')
        assert pool.enqueued == []
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()


# ──────────────────────────────────────────────
# 3. Class mismatch / unknown class on ACTIVE target → still delivered (E2)
# ──────────────────────────────────────────────


class TestClassHandlingOnActiveTarget:
    """When messaging an ACTIVE instance, the requested agent_class is moot — no spawn happens."""

    def test_class_mismatch_on_active_target_still_delivers(self):
        """#7 Active 'worker1' (coder), requested class 'reviewer' → delivered, not rejected.

        Pins the E2 decision: P5's class-mismatch rejection is bypassed on the ACTIVE path because
        we are delivering a message to a live agent whose real class is already fixed.
        """
        target = _make_mock_instance('worker1', 'coder', state=AgentState.RUNNING)
        caller = _make_mock_instance('Maine', 'orchestrator')
        pool = FakePool(instances={'worker1': target, 'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, 'worker1', 'reviewer', task='please review')

        assert not result.startswith('Error:')
        assert ('worker1', '[MESSAGE from Maine]: please review') in pool.enqueued
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()

    def test_unknown_class_on_active_target_still_delivers(self):
        """#8 Active 'worker1', agent_class='bogus' (no template) → delivered, not rejected.

        Pins the "skip unknown-class guard" decision: a bogus class cannot break anything because no
        template lookup / spawn happens on the message path.
        """
        target = _make_mock_instance('worker1', 'coder', state=AgentState.RUNNING)
        caller = _make_mock_instance('Maine', 'orchestrator')
        pool = FakePool(instances={'worker1': target, 'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, 'worker1', 'bogus', task='do it')

        assert not result.startswith('Error:')
        assert ('worker1', '[MESSAGE from Maine]: do it') in pool.enqueued
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()


# ──────────────────────────────────────────────
# 4. Non-ACTIVE targets → unchanged routing (fall-through preserved)
# ──────────────────────────────────────────────


class TestNonActiveTargetsUnchanged:
    """IDLE / TERMINATED / unknown targets fall through to the existing spawn flow, untouched."""

    def test_idle_target_still_routes_async(self):
        """#9 IDLE target → unchanged async routing; no enqueue."""
        target = _make_mock_instance('worker1', 'coder', state=AgentState.IDLE)
        caller = _make_mock_instance('Maine', 'orchestrator')
        pool = FakePool(instances={'worker1': target, 'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, 'worker1', 'coder')

        assert not result.startswith('Error:')
        dispatcher._run_child_async.assert_called_once()
        dispatcher._run_child_sync.assert_not_called()
        assert pool.enqueued == []

    def test_terminated_target_still_routes(self):
        """#10 TERMINATED target → unchanged routing (async, since no slot info)."""
        target = _make_mock_instance('worker1', 'coder', state=AgentState.TERMINATED)
        caller = _make_mock_instance('Maine', 'orchestrator')
        pool = FakePool(instances={'worker1': target, 'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, 'worker1', 'coder')

        assert not result.startswith('Error:')
        dispatcher._run_child_async.assert_called_once()
        dispatcher._run_child_sync.assert_not_called()
        assert pool.enqueued == []


# ──────────────────────────────────────────────
# 5. Concurrency / edge cases
# ──────────────────────────────────────────────


class TestConcurrencyAndEdgeCases:

    def test_concurrent_message_to_same_active_target(self):
        """#11 8 threads call handle_call_agent on the same RUNNING target → 8 intact messages, no spawn."""
        target = _make_mock_instance('worker1', 'coder', state=AgentState.RUNNING)
        caller = _make_mock_instance('Maine', 'orchestrator')
        pool = FakePool(instances={'worker1': target, 'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        n_threads = 8
        errors = []

        def worker(i):
            try:
                _run(dispatcher, caller, 'worker1', 'coder', task=f'task-{i}')
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Exceptions during concurrent messaging: {errors}"
        assert len(pool.enqueued) == n_threads
        # Every message is intact and correctly tagged.
        names = {name for name, _ in pool.enqueued}
        assert names == {'worker1'}
        tasks = {text.split(': ', 1)[1] for _, text in pool.enqueued}
        assert tasks == {f'task-{i}' for i in range(n_threads)}
        # No shadow child was spawned.
        assert not any(n.startswith('worker1_child') for n in pool.instances)
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()

    def test_empty_task_does_not_enqueue(self):
        """#12 Empty/whitespace task → falls through to the normal routing path (no blank message)."""
        target = _make_mock_instance('worker1', 'coder', state=AgentState.RUNNING)
        caller = _make_mock_instance('Maine', 'orchestrator')
        pool = FakePool(instances={'worker1': target, 'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        # Whitespace-only task: the message branch must be skipped (task_text is empty), so the
        # call falls through to the normal spawn flow. No blank "[MESSAGE from Maine]: " is enqueued.
        result = _run(dispatcher, caller, 'worker1', 'coder', task='   ')

        assert pool.enqueued == []
        # It fell through to routing (async, since no slot info) — proving the branch was skipped.
        dispatcher._run_child_async.assert_called_once()
        dispatcher._run_child_sync.assert_not_called()
        _ = result  # non-Error either way; the contract is "no blank message enqueued"

    def test_return_string_says_message_sent(self):
        """#13 Return string confirms the message was sent (matches send_message style)."""
        target = _make_mock_instance('worker1', 'coder', state=AgentState.RUNNING)
        caller = _make_mock_instance('Maine', 'orchestrator')
        pool = FakePool(instances={'worker1': target, 'Maine': caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, 'worker1', 'coder', task='do the thing')

        assert 'Message sent successfully' in result
        # Also confirm it is a clear non-error signal.
        assert not result.startswith('Error:')
