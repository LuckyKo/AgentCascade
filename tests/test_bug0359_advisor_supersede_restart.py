"""BUG_0359 regression: a transient AFK-admin advisor must not be silently killed by an
unrelated real run's ``pool._run_generation`` bump, and a superseded review must never leave
the orchestrator IDLE with pending text and no runner.

Hermetic:
- The generation-supersede gate (Part A) is exercised on a real ``ExecutionEngine``
  (constructed without the heavy ``__init__`` handler fan-out) against a ``MagicMock`` pool
  whose ``_run_generation`` is a REAL int (not a MagicMock auto-attr) and whose
  ``is_instance_terminated`` returns a real bool.
- The re-drive path (Part B) drives the REAL nested ``_afk_admin_nudge`` through
  ``create_app`` with a self-contained pool double; the advisor is stubbed to simulate the
  resume message arriving mid-advisor (flipping ``has_messages`` to True) and the advisor
  being superseded (returning a failed ``AdvisorResult``).

See ``.agent_lessons/bug-0359-superseded-advisor-restart-idle.md``.
"""

import threading
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

from agent_cascade.agent_instance import AgentState
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade import advisor_runner
from agent_cascade import security_reuse


# ── Part A: generation-supersede gate ───────────────────────────────────────────────────


def _make_engine(is_advisor: bool, my_gen: int, pool_gen: int):
    """Build an ``ExecutionEngine`` WITHOUT running the heavy ``__init__`` (handlers), then
    set only the attributes the generation-supersede gate reads. ``pool._run_generation`` is
    a real int and ``pool.is_instance_terminated`` a real bool — never MagicMock auto-attrs."""
    engine = object.__new__(ExecutionEngine)
    pool = MagicMock()
    pool._run_generation = pool_gen
    pool.stopped = False
    pool.is_instance_terminated.return_value = False
    engine.pool = pool
    engine._my_generation = my_gen
    engine._is_advisor_run = is_advisor
    return engine, pool


class _IdleInstance:
    """Minimal instance double: a real state lock + state, enough for ``_finalize_state_transition``."""

    def __init__(self, name='root'):
        self.instance_name = name
        self.state = AgentState.RUNNING
        self._state_lock = threading.Lock()
        self.transitions = []

    def _transition(self, target):
        self.transitions.append(target)
        self.state = target


def test_advisor_run_survives_unrelated_gen_bump():
    """Part A (root fix): an advisor run captured at gen 0 is NOT terminated when an
    unrelated real run bumps the pool to gen 1 — the gen-mismatch term is a no-op for
    advisor runs, so its finalize transition still runs (leaving the root IDLE, not clobbered)."""
    engine, _pool = _make_engine(is_advisor=True, my_gen=0, pool_gen=1)
    assert engine._is_terminal_stop('Admin_afk_x') is False
    inst = _IdleInstance('root')
    engine._finalize_state_transition(inst, preserve=False)
    assert inst.transitions == [AgentState.IDLE]


def test_non_advisor_run_still_superseded():
    """Part A (safety): a NORMAL run's gen-supersede behavior is UNCHANGED — a gen mismatch is
    terminal, and finalize is skipped (shared state left to the live run)."""
    engine, _pool = _make_engine(is_advisor=False, my_gen=0, pool_gen=1)
    assert engine._is_terminal_stop('root') is True
    inst = _IdleInstance('root')
    engine._finalize_state_transition(inst, preserve=False)
    assert inst.transitions == []  # early-return: no state write by a superseded thread


def test_non_advisor_matching_gen_not_terminal():
    """Part A (safety): a normal run whose gen matches the pool is not terminal (unchanged)."""
    engine, _pool = _make_engine(is_advisor=False, my_gen=1, pool_gen=1)
    assert engine._is_terminal_stop('root') is False


def test_advisor_still_honors_global_stop_and_termination():
    """Part A (safety): only the gen-mismatch term is relaxed for advisors — a global pool stop
    or a terminated instance is still terminal."""
    engine, pool = _make_engine(is_advisor=True, my_gen=0, pool_gen=1)
    pool.stopped = True
    assert engine._is_terminal_stop('Admin_afk_x') is True
    pool.stopped = False
    pool.is_instance_terminated.return_value = True
    assert engine._is_terminal_stop('Admin_afk_x') is True


def test_default_engine_is_not_an_advisor():
    """Part A: a fresh ExecutionEngine defaults to a normal (non-advisor) run, so the relaxed
    gate can only ever be opt-in via the advisor path."""
    engine = ExecutionEngine(MagicMock())
    assert engine._is_advisor_run is False


def test_advisor_runner_tags_engine_as_advisor(monkeypatch):
    """Part A (wiring): run_lightweight_advisor tags the engine it builds, so the run is exempt
    from the gen-supersede gate. The advisor is short-circuited (acquire raises) so the real
    LLM path never runs; the function must still return cleanly (never raise into the caller)."""
    from agent_cascade import execution_engine as ee_mod

    engines = []

    class _SpyEngine:
        def __init__(self, pool):
            self.pool = pool
            self._is_advisor_run = False  # mimic the real engine default
            engines.append(self)

        def _telemetry(self):
            return None

    def _raise(*_a, **_k):
        raise RuntimeError('stop-here')

    monkeypatch.setattr(ee_mod, 'ExecutionEngine', _SpyEngine)
    monkeypatch.setattr(security_reuse, 'acquire_security_agent', _raise)
    monkeypatch.setattr(security_reuse, 'release_permit_on_abandon', lambda *a, **k: None)
    monkeypatch.setattr(security_reuse, 'release_claim', lambda *a, **k: None)

    result = advisor_runner.run_lightweight_advisor(
        pool=MagicMock(), agent_class='Admin', instance_name='Admin_afk_test',
        task='t', caller='w1')
    # The advisor never raises into the caller...
    assert result.was_error is True
    # ...and the engine it built was tagged as an advisor run.
    assert engines and engines[0]._is_advisor_run is True


# ── Part B: AFK-admin hand-off re-drive ──────────────────────────────────────────────────


class _AfkInstance:
    """Idle instance double (mirrors tests.test_api_endpoints._AfkLoopInstance)."""

    def __init__(self):
        self.conversation = []
        self._compression_lock = threading.Lock()
        self.instance_name = 'w1'


class _AfkOm:
    def __init__(self, afk_mode: str):
        self.afk_enabled = True
        self.afk_message = ''
        self.afk_mode = afk_mode
        # A real Path (not a str): get_session_log_dir does `base_dir / 'logs'` when
        # ENABLE_STACK_SWAP is on (create_app → snapshot.ensure_registry_loaded).
        self.base_dir = Path(tempfile.gettempdir())


class _AfkPool:
    """Pool double that lets the REAL nested ``_afk_admin_nudge`` run end-to-end.

    ``_has_messages`` is a mutable flag the (stubbed) advisor flips to simulate the resume
    message arriving mid-advisor; ``instances`` is a real dict (never a MagicMock auto-attr)."""

    def __init__(self, afk_mode: str):
        self.stopped = False
        self._instance = _AfkInstance()
        self.operation_manager = _AfkOm(afk_mode)
        self._enqueue_calls = []
        self._auto_security = None
        self._run_generation = 0
        self.instances = {}
        self._has_messages = False

    def set_auto_security(self, value):
        self._auto_security = bool(value)

    def get_instance(self, name):
        return self._instance

    def has_pending(self, name):
        return False

    def has_messages(self, name):
        return self._has_messages

    def enqueue_message(self, name, content):
        self._enqueue_calls.append((name, content))


def _drive_admin_dispatch(monkeypatch, pool):
    """Build the app and drive one AFK-admin dispatch, capturing the enqueued content and any
    relaunch_idle_agent call. Returns (enqueue_calls, relaunch_calls)."""
    import json
    from agent_cascade import api_server
    import agent_cascade.utils.wakeup_helpers as wh
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api_server, 'AFK_NUDGE_TICK_SECS', 0.02)
    monkeypatch.setattr(api_server, 'AFK_NUDGE_COOLDOWN', 1.0)  # one dispatch in the window

    relaunch_calls = []
    monkeypatch.setattr(
        wh, 'relaunch_idle_agent',
        lambda p, n: (relaunch_calls.append((p, n)), False)[1])

    app = api_server.create_app(agents=[], agent_pool=pool, config={'session_name': 'w1'})
    with TestClient(app):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not pool._enqueue_calls:
            time.sleep(0.02)
    return pool._enqueue_calls, relaunch_calls


def _enqueue_text(content):
    """Normalize the enqueued content to a haystack for substring checks."""
    import json
    return content if isinstance(content, str) else json.dumps(content)


def test_admin_dispatch_delivers_trigger_text_with_pinned_identity(monkeypatch):
    """BUG_0392 (v2, observable layer): with the Admin template available, the daemon enqueues
    AFK-window TRIGGER text carrying the pinned call_agent identity (fixed name + class), and
    does NOT call relaunch_idle_agent (the BUG_0359 re-drive branch was dropped — under v2 the
    daemon drives the turn itself right after enqueue, so there is never a
    'pending text, no runner' window)."""
    pool = _AfkPool(afk_mode='admin')
    pool.get_template = lambda name: (name == 'Admin')  # template available

    calls, relaunch_calls = _drive_admin_dispatch(monkeypatch, pool)
    assert calls, 'expected the daemon to enqueue the AFK trigger text'
    text = _enqueue_text(calls[0][1])
    assert "instance_name='AFK_Admin'" in text, 'pinned instance_name missing from trigger'
    assert "agent_class='Admin'" in text, 'pinned agent_class missing from trigger'
    assert relaunch_calls == [], 'relaunch_idle_agent must not be invoked under v2'


def test_admin_template_unavailable_falls_back_to_basic_nudge(monkeypatch):
    """BUG_0392 (v2, observable layer / build-time #2): when the Admin template is unavailable
    the dispatch deterministically enqueues the basic nudge text (no LLM dependency, no retry
    loop), and relaunch_idle_agent is never invoked."""
    from agent_cascade.api_server import AFK_DEFAULT_MESSAGE

    pool = _AfkPool(afk_mode='admin')
    pool.get_template = lambda name: None  # Admin template missing

    calls, relaunch_calls = _drive_admin_dispatch(monkeypatch, pool)
    assert calls, 'expected a basic-nudge fallback enqueue when the Admin template is missing'
    assert calls[0][1] == AFK_DEFAULT_MESSAGE
    assert relaunch_calls == [], 'relaunch_idle_agent must not be invoked under v2'


# ── Part D: boot-restore state normalization ──────────────────────────────────────────────


def _make_instance(name: str, state: AgentState):
    """A minimal instance double with just the attributes normalize_boot_restore_states reads."""
    inst = MagicMock()
    inst.instance_name = name
    inst.state = state
    inst._state_lock = threading.RLock()
    return inst


def test_normalize_boot_restore_states_flips_live_run_to_idle():
    """BUG_0359: a boot-restore has no live run threads, so any restored instance left in a
    live-run state (RUNNING/SLEEPING/COMPLETING) must be normalized to IDLE; TERMINATED and
    IDLE are kept verbatim. Without this, the engine's IDLE->RUNNING entry guard rejects the
    first run and strands queued messages (the root looks 'idle' in the UI but is RUNNING)."""
    from agent_cascade import snapshot

    pool = MagicMock()
    pool.instances = {
        'root': _make_instance('root', AgentState.RUNNING),
        'sleeper': _make_instance('sleeper', AgentState.SLEEPING),
        'completer': _make_instance('completer', AgentState.COMPLETING),
        'dead': _make_instance('dead', AgentState.TERMINATED),
        'already_idle': _make_instance('already_idle', AgentState.IDLE),
    }

    snapshot.normalize_boot_restore_states(pool)

    assert pool.instances['root'].state is AgentState.IDLE
    assert pool.instances['sleeper'].state is AgentState.IDLE
    assert pool.instances['completer'].state is AgentState.IDLE
    assert pool.instances['dead'].state is AgentState.TERMINATED      # kept verbatim
    assert pool.instances['already_idle'].state is AgentState.IDLE    # unchanged
