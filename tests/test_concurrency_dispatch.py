"""Tests for concurrency-based SYNC/ASYNC dispatch in ToolDispatcher.

Verifies the fix where tool_dispatcher checks only the *child's* concurrency
to decide SYNC vs ASYNC path. If the child uses concurrency_limit=0 (sequential),
it forces SYNC. Otherwise ASYNC.

All tests are self-contained — no LLM or API server required.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.tool_dispatcher import ToolDispatcher


# ──────────────────────────────────────────────
# Test Helpers — lightweight mock objects
# ──────────────────────────────────────────────

def _make_mock_template(
    name="TestAgent",
    llm=None,
    function_map=None,
    agent_class="test_agent",
):
    """Create a minimal template with configurable attributes."""
    tmpl = MagicMock()
    tmpl.name = name
    tmpl.agent_class = agent_class
    tmpl.llm = llm
    tmpl.function_map = function_map
    tmpl.base_system_message = f"You are {name}."
    return tmpl


def _make_mock_instance(
    instance_name="worker1",
    agent_class="test_agent",
):
    """Create a minimal AgentInstance mock with concurrency slot support."""
    inst = MagicMock()
    inst.instance_name = instance_name
    inst.agent_class = agent_class
    inst._state_lock = threading.RLock()
    inst._slot_release = None
    inst._nest_depth = 0
    inst.conversation = []
    return inst


def _make_mock_pool(
    templates=None,
    instances=None,
    instance_classes=None,
    agent_concurrency=None,
):
    """Create a minimal AgentPool mock with api_router for concurrency lookup.

    Args:
        templates: Dict of agent_class -> template
        instances: Dict of instance_name -> instance
        instance_classes: Dict of instance_name -> agent_class
        agent_concurrency: Dict of agent_class -> concurrency_limit (0=sequential, >0=parallel)
    """
    pool = MagicMock()
    pool.templates = templates or {}
    pool.instances = instances or {}
    pool.instance_classes = instance_classes or {}
    pool.stopped = False
    pool.is_instance_terminated = MagicMock(return_value=False)

    # Set up api_router for concurrency lookup
    api_router = MagicMock()

    def get_effective_concurrency(agent_class):
        return agent_concurrency.get(agent_class, -1) if agent_concurrency else -1

    api_router.get_effective_concurrency = get_effective_concurrency
    pool.api_router = api_router

    # Set up settings for nesting depth check
    pool.settings = MagicMock()
    pool.settings.max_nesting_depth = 10

    # Set up execution tracker
    mock_execution = MagicMock()
    mock_execution.active_stack = []
    mock_execution._state_lock = threading.RLock()
    mock_execution.count_by_class = MagicMock(return_value=0)
    pool._execution = mock_execution

    # pool.get_instance returns from instances dict
    def get_instance(name):
        return instances.get(name) if instances else None
    pool.get_instance = get_instance

    # pool._acquire_slot returns a release callback
    pool._acquire_slot = MagicMock(return_value=lambda: None)

    return pool


# ──────────────────────────────────────────────
# Test 1: Sequential child → SYNC path
# ──────────────────────────────────────────────
# Logic: child concurrency==0 forces caller_holds_slot=True → SYNC
# This works even when caller does NOT hold a slot.

class TestSequentialChildSyncPath:
    """When child agent has concurrency=0, the call takes SYNC path."""

    def test_sequential_child_forces_sync_with_caller_slot(self):
        """Child with concurrency_limit=0 should take SYNC path when caller holds slot."""
        pool = _make_mock_pool(
            templates={"coder": _make_mock_template(agent_class="coder")},
            instances={"main": _make_mock_instance("main", "orchestrator")},
            instance_classes={"main": "orchestrator"},
            agent_concurrency={"coder": 0},  # sequential
        )

        dispatcher = ToolDispatcher(pool)
        mock_engine = MagicMock()
        dispatcher.set_engine(mock_engine)

        # Caller holds a slot
        caller = pool.get_instance("main")
        caller._slot_release = lambda: None

        sync_called = []
        async_called = []

        def fake_run_sync(agent_class, instance_name, args, slot_holder, caller_name, child_depth):
            sync_called.append((agent_class, instance_name))
            return "sync result"

        def fake_run_async(caller_name, function_id, agent_class, instance_name, args, child_depth):
            async_called.append((agent_class, instance_name))
            return "async result"

        dispatcher._run_child_sync = fake_run_sync
        dispatcher._run_child_async = fake_run_async

        result = dispatcher.handle_call_agent(
            args={"instance_name": "worker1", "agent_class": "coder", "task": "test"},
            messages=[],
            instance=_make_mock_instance("main", "orchestrator"),
        )

        assert sync_called, "Sequential child should take SYNC path"
        assert not async_called, "Sequential child should NOT take ASYNC path"
        assert sync_called[0] == ("coder", "worker1")

    def test_sequential_child_forces_sync_without_caller_slot(self):
        """Child with concurrency_limit=0 should force SYNC even without caller slot.

        This is the key fix: concurrency==0 forces SYNC regardless of caller's slot state.
        """
        pool = _make_mock_pool(
            templates={"coder": _make_mock_template(agent_class="coder")},
            instances={"main": _make_mock_instance("main", "orchestrator")},
            instance_classes={"main": "orchestrator"},
            agent_concurrency={"coder": 0},  # sequential
        )

        dispatcher = ToolDispatcher(pool)
        mock_engine = MagicMock()
        dispatcher.set_engine(mock_engine)

        # Caller does NOT hold a slot
        caller = pool.get_instance("main")
        caller._slot_release = None

        sync_called = []
        async_called = []

        def fake_run_sync(agent_class, instance_name, args, slot_holder, caller_name, child_depth):
            sync_called.append((agent_class, instance_name))
            return "sync result"

        def fake_run_async(caller_name, function_id, agent_class, instance_name, args, child_depth):
            async_called.append((agent_class, instance_name))
            return "async result"

        dispatcher._run_child_sync = fake_run_sync
        dispatcher._run_child_async = fake_run_async

        result = dispatcher.handle_call_agent(
            args={"instance_name": "worker1", "agent_class": "coder", "task": "test"},
            messages=[],
            instance=_make_mock_instance("main", "orchestrator"),
        )

        # With concurrency=0, the code forces caller_holds_slot=True → SYNC
        assert sync_called, "Sequential child should force SYNC even without caller slot"
        assert not async_called


# ──────────────────────────────────────────────
# Test 2: Parallel child → ASYNC path
# ──────────────────────────────────────────────
# Logic: parallel child (concurrency>0) takes ASYNC when caller doesn't hold slot.
# When caller holds slot, it takes SYNC (caller_holds_slot is True).

class TestParallelChildAsyncPath:
    """When child agent has concurrency>0, the call takes ASYNC path."""

    def test_parallel_child_takes_async_no_caller_slot(self):
        """Child with concurrency>0 should take ASYNC path when caller doesn't hold slot."""
        pool = _make_mock_pool(
            templates={"coder": _make_mock_template(agent_class="coder")},
            instances={"main": _make_mock_instance("main", "orchestrator")},
            instance_classes={"main": "orchestrator"},
            agent_concurrency={"coder": 3},  # parallel
        )

        dispatcher = ToolDispatcher(pool)
        mock_engine = MagicMock()
        dispatcher.set_engine(mock_engine)

        # Caller does NOT hold a slot
        caller = pool.get_instance("main")
        caller._slot_release = None

        sync_called = []
        async_called = []

        def fake_run_sync(agent_class, instance_name, args, slot_holder, caller_name, child_depth):
            sync_called.append((agent_class, instance_name))
            return "sync result"

        def fake_run_async(caller_name, function_id, agent_class, instance_name, args, child_depth):
            async_called.append((agent_class, instance_name))
            return "async result"

        dispatcher._run_child_sync = fake_run_sync
        dispatcher._run_child_async = fake_run_async

        result = dispatcher.handle_call_agent(
            args={"instance_name": "worker1", "agent_class": "coder", "task": "test"},
            messages=[],
            instance=_make_mock_instance("main", "orchestrator"),
        )

        assert not sync_called, "Parallel child should NOT take SYNC path"
        assert async_called, "Parallel child should take ASYNC path"
        assert async_called[0] == ("coder", "worker1")

    def test_parallel_child_takes_sync_with_caller_slot(self):
        """Child with concurrency>0 takes SYNC path when caller holds slot.

        This is expected: caller holds slot → SYNC path.
        The fix ensures sequential children also take SYNC, but parallel
        children still follow the normal caller-slot logic.
        """
        pool = _make_mock_pool(
            templates={"coder": _make_mock_template(agent_class="coder")},
            instances={"main": _make_mock_instance("main", "orchestrator")},
            instance_classes={"main": "orchestrator"},
            agent_concurrency={"coder": 3},  # parallel
        )

        dispatcher = ToolDispatcher(pool)
        mock_engine = MagicMock()
        dispatcher.set_engine(mock_engine)

        # Caller holds a slot
        caller = pool.get_instance("main")
        caller._slot_release = lambda: None

        sync_called = []
        async_called = []

        def fake_run_sync(agent_class, instance_name, args, slot_holder, caller_name, child_depth):
            sync_called.append((agent_class, instance_name))
            return "sync result"

        def fake_run_async(caller_name, function_id, agent_class, instance_name, args, child_depth):
            async_called.append((agent_class, instance_name))
            return "async result"

        dispatcher._run_child_sync = fake_run_sync
        dispatcher._run_child_async = fake_run_async

        result = dispatcher.handle_call_agent(
            args={"instance_name": "worker1", "agent_class": "coder", "task": "test"},
            messages=[],
            instance=_make_mock_instance("main", "orchestrator"),
        )

        # Caller holds slot → SYNC
        assert sync_called, "Parallel child with caller slot should take SYNC"
        assert not async_called


# ──────────────────────────────────────────────
# Test 3: Mixed parallel calls
# ──────────────────────────────────────────────
# When caller launches 3 children (2 sequential + 1 parallel),
# the 2 sequential ones take SYNC and the parallel one takes ASYNC.
# Caller doesn't hold a slot so parallel child goes ASYNC.

class TestMixedParallelCalls:
    """When caller launches 3 children (2 sequential + 1 parallel),
    the 2 sequential ones take SYNC and the parallel one takes ASYNC."""

    def test_mixed_sync_and_async_dispatch(self):
        """2 sequential children + 1 parallel child should dispatch correctly.

        Caller has no slot held. Sequential children (concurrency=0) force SYNC.
        Parallel child (concurrency>0) goes ASYNC.
        """
        pool = _make_mock_pool(
            templates={
                "coder": _make_mock_template(agent_class="coder"),
                "reviewer": _make_mock_template(agent_class="reviewer"),
                "security": _make_mock_template(agent_class="security"),
            },
            instances={"main": _make_mock_instance("main", "orchestrator")},
            instance_classes={"main": "orchestrator"},
            agent_concurrency={
                "coder": 0,      # sequential → SYNC
                "reviewer": 0,   # sequential → SYNC
                "security": 2,   # parallel → ASYNC
            },
        )

        dispatcher = ToolDispatcher(pool)
        mock_engine = MagicMock()
        dispatcher.set_engine(mock_engine)

        # Caller does NOT hold a slot — parallel child will go ASYNC
        caller = pool.get_instance("main")
        caller._slot_release = None

        dispatch_log = []

        def fake_run_sync(agent_class, instance_name, args, slot_holder, caller_name, child_depth):
            dispatch_log.append(("SYNC", agent_class, instance_name))
            return f"sync result from {instance_name}"

        def fake_run_async(caller_name, function_id, agent_class, instance_name, args, child_depth):
            dispatch_log.append(("ASYNC", agent_class, instance_name))
            return f"async result from {instance_name}"

        dispatcher._run_child_sync = fake_run_sync
        dispatcher._run_child_async = fake_run_async

        # Launch 3 children
        calls = [
            {"instance_name": "coder1", "agent_class": "coder", "task": "code"},
            {"instance_name": "reviewer1", "agent_class": "reviewer", "task": "review"},
            {"instance_name": "security1", "agent_class": "security", "task": "scan"},
        ]

        mock_instance = _make_mock_instance("main", "orchestrator")

        for call_args in calls:
            dispatcher.handle_call_agent(
                args=call_args,
                messages=[],
                instance=mock_instance,
            )

        # Verify dispatch paths
        assert len(dispatch_log) == 3
        # Coder (sequential) → SYNC
        assert dispatch_log[0] == ("SYNC", "coder", "coder1")
        # Reviewer (sequential) → SYNC
        assert dispatch_log[1] == ("SYNC", "reviewer", "reviewer1")
        # Security (parallel) → ASYNC
        assert dispatch_log[2] == ("ASYNC", "security", "security1")

    def test_mixed_all_with_caller_slot(self):
        """All children take SYNC when caller holds slot, regardless of concurrency.

        With caller holding a slot, both sequential and parallel children
        take the SYNC path (caller_holds_slot is True for all).
        """
        pool = _make_mock_pool(
            templates={
                "coder": _make_mock_template(agent_class="coder"),
                "reviewer": _make_mock_template(agent_class="reviewer"),
                "security": _make_mock_template(agent_class="security"),
            },
            instances={"main": _make_mock_instance("main", "orchestrator")},
            instance_classes={"main": "orchestrator"},
            agent_concurrency={
                "coder": 0,
                "reviewer": 0,
                "security": 2,
            },
        )

        dispatcher = ToolDispatcher(pool)
        mock_engine = MagicMock()
        dispatcher.set_engine(mock_engine)

        # Caller holds a slot
        caller = pool.get_instance("main")
        caller._slot_release = lambda: None

        dispatch_log = []

        def fake_run_sync(agent_class, instance_name, args, slot_holder, caller_name, child_depth):
            dispatch_log.append(("SYNC", agent_class, instance_name))
            return f"sync result from {instance_name}"

        def fake_run_async(caller_name, function_id, agent_class, instance_name, args, child_depth):
            dispatch_log.append(("ASYNC", agent_class, instance_name))
            return f"async result from {instance_name}"

        dispatcher._run_child_sync = fake_run_sync
        dispatcher._run_child_async = fake_run_async

        mock_instance = _make_mock_instance("main", "orchestrator")

        calls = [
            {"instance_name": "coder1", "agent_class": "coder", "task": "code"},
            {"instance_name": "reviewer1", "agent_class": "reviewer", "task": "review"},
            {"instance_name": "security1", "agent_class": "security", "task": "scan"},
        ]

        for call_args in calls:
            dispatcher.handle_call_agent(
                args=call_args,
                messages=[],
                instance=mock_instance,
            )

        # All take SYNC since caller holds slot
        assert len(dispatch_log) == 3
        assert all(path == "SYNC" for path, _, _ in dispatch_log)


# ──────────────────────────────────────────────
# Test 4: No deadlock with sequential children
# ──────────────────────────────────────────────
# When 3 sequential children are launched, they don't timeout
# (they run sequentially via SYNC path).

class TestNoDeadlockSequentialChildren:
    """When 3 sequential children are launched, they don't timeout
    (they run sequentially via SYNC path)."""

    def test_three_sequential_children_complete(self):
        """3 sequential children should all complete without timeout."""
        pool = _make_mock_pool(
            templates={
                "coder": _make_mock_template(agent_class="coder"),
                "reviewer": _make_mock_template(agent_class="reviewer"),
                "security": _make_mock_template(agent_class="security"),
            },
            instances={"main": _make_mock_instance("main", "orchestrator")},
            instance_classes={"main": "orchestrator"},
            agent_concurrency={
                "coder": 0,
                "reviewer": 0,
                "security": 0,
            },
        )

        dispatcher = ToolDispatcher(pool)
        mock_engine = MagicMock()
        dispatcher.set_engine(mock_engine)

        caller = pool.get_instance("main")
        caller._slot_release = lambda: None

        completed = []

        def fake_run_sync(agent_class, instance_name, args, slot_holder, caller_name, child_depth):
            completed.append(instance_name)
            return f"result from {instance_name}"

        def fake_run_async(caller_name, function_id, agent_class, instance_name, args, child_depth):
            completed.append(f"{instance_name}_async")
            return f"async result from {instance_name}"

        dispatcher._run_child_sync = fake_run_sync
        dispatcher._run_child_async = fake_run_async

        mock_instance = _make_mock_instance("main", "orchestrator")

        calls = [
            {"instance_name": "coder1", "agent_class": "coder", "task": "code"},
            {"instance_name": "reviewer1", "agent_class": "reviewer", "task": "review"},
            {"instance_name": "security1", "agent_class": "security", "task": "scan"},
        ]

        for call_args in calls:
            dispatcher.handle_call_agent(
                args=call_args,
                messages=[],
                instance=mock_instance,
            )

        # All 3 should complete via SYNC (no ASYNC calls)
        assert len(completed) == 3
        assert completed == ["coder1", "reviewer1", "security1"]
        assert all("_async" not in c for c in completed), \
            "All sequential children should use SYNC path, not ASYNC"

    def test_sequential_children_run_in_order(self):
        """Sequential children should execute in the order they were called."""
        pool = _make_mock_pool(
            templates={
                "coder": _make_mock_template(agent_class="coder"),
                "reviewer": _make_mock_template(agent_class="reviewer"),
                "security": _make_mock_template(agent_class="security"),
            },
            instances={"main": _make_mock_instance("main", "orchestrator")},
            instance_classes={"main": "orchestrator"},
            agent_concurrency={"coder": 0, "reviewer": 0, "security": 0},
        )

        dispatcher = ToolDispatcher(pool)
        mock_engine = MagicMock()
        dispatcher.set_engine(mock_engine)

        caller = pool.get_instance("main")
        caller._slot_release = lambda: None

        execution_order = []

        def fake_run_sync(agent_class, instance_name, args, slot_holder, caller_name, child_depth):
            execution_order.append(instance_name)
            return f"result from {instance_name}"

        def fake_run_async(caller_name, function_id, agent_class, instance_name, args, child_depth):
            execution_order.append(f"{instance_name}_async")
            return f"async result from {instance_name}"

        dispatcher._run_child_sync = fake_run_sync
        dispatcher._run_child_async = fake_run_async

        mock_instance = _make_mock_instance("main", "orchestrator")

        calls = [
            {"instance_name": "coder1", "agent_class": "coder", "task": "code"},
            {"instance_name": "reviewer1", "agent_class": "reviewer", "task": "review"},
            {"instance_name": "security1", "agent_class": "security", "task": "scan"},
        ]

        for call_args in calls:
            dispatcher.handle_call_agent(
                args=call_args,
                messages=[],
                instance=mock_instance,
            )

        # SYNC path executes immediately in order
        assert execution_order == ["coder1", "reviewer1", "security1"]

    def test_no_timeout_with_sequential_chain(self):
        """A chain of sequential children should not cause timeout."""
        pool = _make_mock_pool(
            templates={
                "coder": _make_mock_template(agent_class="coder"),
                "reviewer": _make_mock_template(agent_class="reviewer"),
                "security": _make_mock_template(agent_class="security"),
            },
            instances={"main": _make_mock_instance("main", "orchestrator")},
            instance_classes={"main": "orchestrator"},
            agent_concurrency={"coder": 0, "reviewer": 0, "security": 0},
        )

        dispatcher = ToolDispatcher(pool)
        mock_engine = MagicMock()
        dispatcher.set_engine(mock_engine)

        caller = pool.get_instance("main")
        caller._slot_release = lambda: None

        # Count how many times SYNC is called - if ASYNC was taken,
        # children would compete for the same slot and could timeout
        sync_count = [0]
        async_count = [0]

        def fake_run_sync(agent_class, instance_name, args, slot_holder, caller_name, child_depth):
            sync_count[0] += 1
            return f"result from {instance_name}"

        def fake_run_async(caller_name, function_id, agent_class, instance_name, args, child_depth):
            async_count[0] += 1
            return f"async result from {instance_name}"

        dispatcher._run_child_sync = fake_run_sync
        dispatcher._run_child_async = fake_run_async

        mock_instance = _make_mock_instance("main", "orchestrator")

        # Launch all 3
        for i in range(3):
            agent_classes = ["coder", "reviewer", "security"]
            dispatcher.handle_call_agent(
                args={
                    "instance_name": f"worker{i}",
                    "agent_class": agent_classes[i],
                    "task": "test",
                },
                messages=[],
                instance=mock_instance,
            )

        assert sync_count[0] == 3, f"Expected 3 SYNC calls, got {sync_count[0]}"
        assert async_count[0] == 0, f"Expected 0 ASYNC calls, got {async_count[0]}"