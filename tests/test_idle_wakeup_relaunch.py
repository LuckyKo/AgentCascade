"""Fix B (idle-wakeup) tests — relaunch_idle_agent helper and its call sites.

Covers the approved plan §5 Fix B items:
1. IDLE parent + async result -> relaunch spawned (enqueue before relaunch).
2. SLEEPING parent -> NO relaunch (live poll thread handles it).
3. RUNNING parent -> NO relaunch.
4. Stopped / terminated pool -> NO relaunch.
5. Double-launch guard: L1-guard RuntimeError in the drive thread is caught,
   logged at DEBUG, not propagated/crashed.
6. Multiple results same tick to one IDLE parent -> exactly one effective run.

Deterministic by design: the thread factory is injectable so tests assert
spawn-attempt + exception handling without racing real threads. No LLM or
network connections required.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# NOTE: PropertyMock is not needed — pool.stopped is set as a plain attribute.

import pytest

from agent_cascade.agent_instance import AgentState
from agent_cascade.async_tools import AsyncToolRegistry
from agent_cascade.utils.wakeup_helpers import relaunch_idle_agent, _drive_run


# ============================================================================
# Fixtures and helpers
# ============================================================================

def make_instance(state=AgentState.IDLE):
    """Minimal duck-typed AgentInstance (state + _state_lock only)."""
    return SimpleNamespace(
        state=state,
        instance_name="Maine",
        _state_lock=threading.RLock(),
    )


def make_child_instance(state=AgentState.IDLE, parent=None):
    """Duck-typed child instance carrying the surface dismiss_instance touches."""
    inst = make_instance(state)
    inst.instance_name = "child1"
    inst.parent_instance = parent
    # Surface used by the non-active dismissal branch (inst.terminate()).
    inst.is_terminated = False
    inst.terminate = lambda: setattr(inst, 'is_terminated', True)
    return inst


def make_pool(instance=None, stopped=False, terminated=False):
    """Mock pool with the surface relaunch_idle_agent touches."""
    pool = MagicMock()
    pool.get_instance.return_value = instance
    pool.stopped = stopped
    pool.is_instance_terminated.return_value = terminated
    # Thread registry surface (mirrors AgentPool)
    pool._instance_threads = {}
    pool._instance_threads_lock = threading.Lock()
    return pool


def wait_for(predicate, timeout=5.0, interval=0.01):
    """Poll until predicate() is truthy or timeout; returns predicate's last value."""
    deadline = time.monotonic() + timeout
    value = False
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return True
        time.sleep(interval)
    return bool(value)


class _FakeThreadFactory(MagicMock):
    """threading.Thread stand-in that records instances instead of spawning.

    Each call returns a new _FakeThread capturing the target callable so tests
    can assert spawn-attempt + drive behavior without racing real threads.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instances = []

    def __call__(self, *args, **kwargs):
        ft = _FakeThread(target=kwargs.get('target'), args=kwargs.get('args', ()),
                         name=kwargs.get('name', ''), daemon=kwargs.get('daemon', False))
        self.instances.append(ft)
        return ft


class _FakeThread:
    """Captures the target callable; run() invokes it synchronously (or not)."""

    def __init__(self, target=None, args=(), name="", daemon=False):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False
        self.exc = None  # set if run() raised

    def start(self):
        self.started = True

    def run(self):
        try:
            self.target(*self.args)
        except Exception as e:  # pragma: no cover - _drive_run catches internally
            self.exc = e

    def join(self, timeout=None):
        """No-op — fake threads never really start."""

    def is_alive(self):
        """Fake threads are never alive (they don't run in the background)."""
        return False


# ============================================================================
# relaunch_idle_agent — state / shutdown gates
# ============================================================================

class TestRelaunchGates:
    """relaunch_idle_agent must be a no-op (False, no thread) in every
    non-IDLE / stopped / terminated case."""

    def test_idle_parent_spawns_relaunch(self):
        pool = make_pool(make_instance(AgentState.IDLE))
        with patch('threading.Thread', new=_FakeThreadFactory()) as fake_thread:
            assert relaunch_idle_agent(pool, "Maine") is True
        ft = fake_thread.instances[0]
        assert ft.started
        assert ft.daemon is True
        # Target drives the instance through run_agent_in_pool.
        assert ft.target is _drive_run
        assert ft.args == (pool, "Maine")

    def test_sleeping_parent_no_relaunch(self):
        pool = make_pool(make_instance(AgentState.SLEEPING))
        with patch('threading.Thread', new=_FakeThreadFactory()) as fake_thread:
            assert relaunch_idle_agent(pool, "Maine") is False
        fake_thread.assert_not_called()

    def test_running_parent_no_relaunch(self):
        pool = make_pool(make_instance(AgentState.RUNNING))
        with patch('threading.Thread', new=_FakeThreadFactory()) as fake_thread:
            assert relaunch_idle_agent(pool, "Maine") is False
        fake_thread.assert_not_called()

    def test_terminated_state_no_relaunch(self):
        pool = make_pool(make_instance(AgentState.TERMINATED))
        with patch('threading.Thread', new=_FakeThreadFactory()) as fake_thread:
            assert relaunch_idle_agent(pool, "Maine") is False
        fake_thread.assert_not_called()

    def test_stopped_pool_no_relaunch(self):
        pool = make_pool(make_instance(AgentState.IDLE), stopped=True)
        with patch('threading.Thread', new=_FakeThreadFactory()) as fake_thread:
            assert relaunch_idle_agent(pool, "Maine") is False
        fake_thread.assert_not_called()

    def test_terminated_instance_no_relaunch(self):
        pool = make_pool(make_instance(AgentState.IDLE), terminated=True)
        with patch('threading.Thread', new=_FakeThreadFactory()) as fake_thread:
            assert relaunch_idle_agent(pool, "Maine") is False
        fake_thread.assert_not_called()

    def test_missing_instance_no_relaunch(self):
        pool = make_pool(None)  # get_instance -> None
        with patch('threading.Thread', new=_FakeThreadFactory()) as fake_thread:
            assert relaunch_idle_agent(pool, "ghost") is False
        fake_thread.assert_not_called()

    def test_none_pool_no_relaunch(self):
        with patch('threading.Thread', new=_FakeThreadFactory()) as fake_thread:
            assert relaunch_idle_agent(None, "Maine") is False
        fake_thread.assert_not_called()


# ============================================================================
# Double-launch guard (L1 race guard RuntimeError handling in the drive thread)
# ============================================================================

class TestDoubleLaunchGuard:
    """The spawned thread must catch the L1-guard RuntimeError from
    run_agent_in_pool / engine.run(), log it at DEBUG, and NOT propagate."""

    def test_runtime_error_caught_and_logged_debug(self):
        pool = make_pool(make_instance(AgentState.IDLE))
        with patch('threading.Thread', new=_FakeThreadFactory()) as fake_thread:
            assert relaunch_idle_agent(pool, "Maine") is True
        ft = fake_thread.instances[0]

        l1_error = RuntimeError(
            "[BUG] Maine entered engine.run() in state RUNNING — should be IDLE. L1 race guard failed!"
        )
        with patch(
            'agent_cascade.api_integration_pkg.runner.run_agent_in_pool',
            side_effect=l1_error,
        ), patch('agent_cascade.utils.wakeup_helpers.logger') as mock_logger:
            ft.run()  # drive the target synchronously

        assert ft.exc is None  # not propagated / crashed
        # Logged at DEBUG (expected concurrent wakeup), never at ERROR.
        debug_msgs = [str(c) for c in mock_logger.debug.call_args_list]
        assert any("concurrent wakeup" in m for m in debug_msgs)
        mock_logger.error.assert_not_called()

    def test_thread_registration_cleaned_up_after_guard_hit(self):
        pool = make_pool(make_instance(AgentState.IDLE))
        with patch('threading.Thread', new=_FakeThreadFactory()) as fake_thread:
            assert relaunch_idle_agent(pool, "Maine") is True
        ft = fake_thread.instances[0]

        with patch(
            'agent_cascade.api_integration_pkg.runner.run_agent_in_pool',
            side_effect=RuntimeError("L1 race guard failed!"),
        ):
            ft.run()

        # finally block popped the registration — no thread leak.
        assert pool._instance_threads == {}

    def test_other_exceptions_logged_not_swallowed_silently(self):
        """Non-RuntimeError failures must surface at ERROR (real errors are visible)."""
        pool = make_pool(make_instance(AgentState.IDLE))
        with patch('threading.Thread', new=_FakeThreadFactory()) as fake_thread:
            assert relaunch_idle_agent(pool, "Maine") is True
        ft = fake_thread.instances[0]

        with patch(
            'agent_cascade.api_integration_pkg.runner.run_agent_in_pool',
            side_effect=ValueError("boom"),
        ), patch('agent_cascade.utils.wakeup_helpers.logger') as mock_logger:
            ft.run()

        assert ft.exc is None  # thread itself must not crash
        error_msgs = [str(c) for c in mock_logger.error.call_args_list]
        assert any("boom" in m for m in error_msgs)


# ============================================================================
# Multiple results same tick -> exactly one effective run
# ============================================================================

class TestMultipleResultsSameTick:
    """Two async completions for the same IDLE parent in one tick must result
    in exactly ONE effective run(): the second launcher either sees non-IDLE
    (pre-check) or hits the L1 guard inside its drive thread."""

    def test_second_launcher_bails_or_hits_guard(self):
        pool = make_pool(make_instance(AgentState.IDLE))
        launched = []  # (relaunch_return, fake_thread) per call

        def recording_thread(target=None, args=(), name="", daemon=False):
            ft = _FakeThread(target=target, args=args, name=name, daemon=daemon)
            ft.start()
            launched.append(ft)
            return ft

        with patch('threading.Thread', recording_thread):
            r1 = relaunch_idle_agent(pool, "Maine")
            # First run won the IDLE->RUNNING race (as engine.run() would do).
            pool.get_instance.return_value.state = AgentState.RUNNING
            r2 = relaunch_idle_agent(pool, "Maine")

        assert r1 is True
        assert r2 is False  # pre-check: no longer IDLE -> no second thread
        assert len(launched) == 1

    def test_raced_second_launcher_hits_l1_guard(self):
        """If the second launcher races PAST the pre-check (still sees IDLE),
        its drive thread must hit the L1 guard and bail quietly — one effective run."""
        pool = make_pool(make_instance(AgentState.IDLE))
        launched = []

        def recording_thread(target=None, args=(), name="", daemon=False):
            ft = _FakeThread(target=target, args=args, name=name, daemon=daemon)
            ft.start()
            launched.append(ft)
            return ft

        with patch('threading.Thread', recording_thread):
            assert relaunch_idle_agent(pool, "Maine") is True
            assert relaunch_idle_agent(pool, "Maine") is True  # raced past pre-check

        assert len(launched) == 2

        # First run: normal drain. Second run: L1 guard RuntimeError -> DEBUG, no crash.
        gen = iter([])
        with patch('agent_cascade.api_integration_pkg.runner.run_agent_in_pool', side_effect=[
            lambda pool, name: iter(gen),
            lambda pool, name: (_ for _ in ()).throw(RuntimeError("L1 race guard failed!")),
        ]):
            launched[0].run()
            launched[1].run()

        assert launched[0].exc is None
        assert launched[1].exc is None  # guard swallowed inside the thread


# ============================================================================
# Call site: async completion (AsyncToolRegistry._execute)
# ============================================================================

class TestAsyncCompletionWiring:
    """AsyncToolRegistry must enqueue the result FIRST, then relaunch an IDLE parent."""

    @staticmethod
    def _make_pool_with_registry_surface(instance_state):
        pool = MagicMock()
        pool.enqueue_message = MagicMock()
        pool.stopped = False
        pool.is_instance_terminated.return_value = False
        pool.get_instance.return_value = make_instance(instance_state)
        pool._instance_threads = {}
        pool._instance_threads_lock = threading.Lock()
        return pool

    def _register_and_wait(self, registry, timeout=5.0):
        """Register a trivial tool and poll until it completes (no blind sleep)."""
        registry.register("Maine", lambda: "child_output", function_id="call_1")
        deadline = time.monotonic() + timeout
        while registry.has_pending("Maine") and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_idle_parent_async_result_relaunches_after_enqueue(self):
        pool = self._make_pool_with_registry_surface(AgentState.IDLE)
        registry = AsyncToolRegistry(pool=pool)

        # Patch the helper's _drive_run so no real thread is spawned; the
        # executor keeps its real threads, so completion is fast and
        # deterministic. The fake captures the spawn-attempt args.
        with patch('agent_cascade.utils.wakeup_helpers._drive_run') as mock_drive:
            self._register_and_wait(registry)

        assert not registry.has_pending("Maine")
        # Enqueue happened exactly once with the result.
        pool.enqueue_message.assert_called_once()
        agent_name, msg = pool.enqueue_message.call_args[0]
        assert agent_name == "Maine"
        assert "child_output" in msg
        # Relaunch was attempted for the IDLE instance (enqueue first: the
        # helper is called after enqueue in _execute).
        mock_drive.assert_called_once()
        spawn_args = mock_drive.call_args[0]
        assert spawn_args == (pool, "Maine")

    def test_sleeping_parent_async_result_no_relaunch(self):
        pool = self._make_pool_with_registry_surface(AgentState.SLEEPING)
        registry = AsyncToolRegistry(pool=pool)

        with patch('agent_cascade.utils.wakeup_helpers._drive_run') as mock_drive:
            self._register_and_wait(registry)

        pool.enqueue_message.assert_called_once()  # existing behavior preserved
        mock_drive.assert_not_called()  # live poll thread handles it

    def test_stopped_pool_async_result_no_relaunch(self):
        pool = self._make_pool_with_registry_surface(AgentState.IDLE)
        pool.stopped = True
        registry = AsyncToolRegistry(pool=pool)

        with patch('agent_cascade.utils.wakeup_helpers._drive_run') as mock_drive:
            self._register_and_wait(registry)

        pool.enqueue_message.assert_called_once()  # enqueue still happens
        mock_drive.assert_not_called()  # but no relaunch during shutdown


# ============================================================================
# Call site: dismiss path (LifecycleMixin.dismiss_instance)
# ============================================================================

class TestDismissWiring:
    """dismiss_instance must enqueue the dismissal result for an IDLE parent and
    relaunch it; SLEEPING parents keep the existing enqueue-only behavior."""

    @staticmethod
    def _build_mixin(parent_state):
        """Build a LifecycleMixin bound to a fake pool holding child 'child1'
        (IDLE) with parent 'Maine' in ``parent_state``."""
        from agent_cascade.pool.lifecycle import LifecycleMixin

        parent = make_instance(parent_state)
        child = make_child_instance(AgentState.IDLE, parent="Maine")

        pool = SimpleNamespace()
        pool.children = {}
        pool._children_lock = threading.RLock()
        pool.instances = {"child1": child, "Maine": parent}
        pool._pool_lock = threading.RLock()
        pool.terminated_instances = set()
        pool._instance_threads = {}
        pool._instance_threads_lock = threading.Lock()
        pool.settings = SimpleNamespace(dismiss_thread_join_timeout=0.1)
        pool.stopped = False
        pool.get_instance = lambda name: pool.instances.get(name)
        pool.enqueue_message = MagicMock()
        pool.is_instance_terminated = lambda name: False
        pool._async_registry = SimpleNamespace(
            get_parent_for_child=lambda cname: ("Maine", "call_1") if cname == "child1" else None,
            remove_child_mapping=MagicMock(),
        )
        # Surface touched by the post-wakeup tail of dismiss_instance.
        pool.api_router = None
        pool.remove_instance = MagicMock()

        mixin = LifecycleMixin.__new__(LifecycleMixin)
        for attr in ('children', '_children_lock', 'instances', '_pool_lock',
                     'terminated_instances', '_instance_threads', '_instance_threads_lock',
                     'settings', 'get_instance', 'enqueue_message', 'is_instance_terminated',
                     '_async_registry', 'api_router', 'remove_instance'):
            setattr(mixin, attr, getattr(pool, attr))
        mixin._clear_state_label = lambda inst: None
        return mixin, pool

    def test_idle_parent_dismiss_relaunches(self):
        from agent_cascade.pool.lifecycle import LifecycleMixin

        mixin, pool = self._build_mixin(AgentState.IDLE)

        # Patch ONLY the helper's thread factory so dismiss_instance itself runs
        # unpatched (its tail has no thread spawns; the relaunch is the only one).
        with patch('agent_cascade.utils.wakeup_helpers.threading.Thread',
                   new=_FakeThreadFactory()) as fake_thread:
            LifecycleMixin.dismiss_instance(mixin, "child1")

        # Dismissal result enqueued for the parent...
        pool.enqueue_message.assert_called_once()
        agent_name, msg = pool.enqueue_message.call_args[0]
        assert agent_name == "Maine"
        assert "Dismissed" in msg
        # ...and the IDLE parent was relaunched.
        ft = fake_thread.instances[0]
        assert ft.started
        assert ft.args == (mixin, "Maine")
        # Child mapping cleaned up.
        pool._async_registry.remove_child_mapping.assert_called_once_with("child1")

    def test_sleeping_parent_dismiss_no_relaunch(self):
        from agent_cascade.pool.lifecycle import LifecycleMixin

        mixin, pool = self._build_mixin(AgentState.SLEEPING)

        with patch('agent_cascade.utils.wakeup_helpers.threading.Thread',
                   new=_FakeThreadFactory()) as fake_thread:
            LifecycleMixin.dismiss_instance(mixin, "child1")

        # Existing SLEEPING behavior: enqueue only, no relaunch thread.
        pool.enqueue_message.assert_called_once()
        assert not fake_thread.instances
        pool._async_registry.remove_child_mapping.assert_called_once_with("child1")
