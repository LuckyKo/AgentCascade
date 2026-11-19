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


def _drive_one_admin_nudge(monkeypatch, pool):
    """Build the app, stub the advisor to flip ``has_messages`` + fail (superseded), and poll
    until the (monkeypatched) ``relaunch_idle_agent`` is invoked — or the window elapses.
    Returns (app, re_drives)."""
    from agent_cascade import api_server
    from agent_cascade.api_server import create_app
    import agent_cascade.advisor_runner as _ar
    import agent_cascade.utils.wakeup_helpers as wh
    from agent_cascade.advisor_runner import AdvisorResult
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api_server, 'AFK_NUDGE_TICK_SECS', 0.02)
    monkeypatch.setattr(api_server, 'AFK_NUDGE_COOLDOWN', 0.05)

    # Admin template available so _afk_admin_analysis reaches the (stubbed) advisor call.
    pool.get_template = lambda name: (name == 'Admin')

    def _advisor(*_a, **_k):
        # Simulate the resume message arriving mid-advisor + the advisor being superseded.
        pool._has_messages = True
        return AdvisorResult(was_error=True, error_msg='superseded (gen 0 != 1)')

    monkeypatch.setattr(_ar, 'run_lightweight_advisor', _advisor)

    re_drives = []
    monkeypatch.setattr(
        wh, 'relaunch_idle_agent',
        lambda p, n: (re_drives.append((p, n)), True)[1])

    app = create_app(agents=[], agent_pool=pool, config={'session_name': 'w1'})
    with TestClient(app):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not re_drives:
            time.sleep(0.02)
    return app, re_drives


def test_superseded_review_re_drives_idle_root(monkeypatch):
    """Part B: when the Admin review is superseded (returns None) AND the root is IDLE with
    pending text, the hand-off re-drives the root via relaunch_idle_agent — no basic-nudge
    enqueue, so a superseded review never leaves the orchestrator idle-with-no-runner."""
    pool = _AfkPool(afk_mode='admin')
    _app, re_drives = _drive_one_admin_nudge(monkeypatch, pool)
    assert re_drives, 'expected a re-drive of the IDLE root after a superseded review'
    assert re_drives[0][1] == 'w1'
    assert pool._enqueue_calls == []  # re-drive path does not enqueue a basic nudge


def test_no_pending_text_keeps_basic_nudge_fallback(monkeypatch):
    """Part B (safety): when the review is superseded but there is NO pending text (the ordinary
    AFK case), the basic-nudge fallback is preserved and relaunch_idle_agent is NOT called."""
    from agent_cascade import api_server
    from agent_cascade.api_server import AFK_DEFAULT_MESSAGE
    import agent_cascade.utils.wakeup_helpers as wh

    pool = _AfkPool(afk_mode='admin')
    pool.get_template = lambda name: (name == 'Admin')

    import agent_cascade.advisor_runner as _ar
    from agent_cascade.advisor_runner import AdvisorResult
    monkeypatch.setattr(_ar, 'run_lightweight_advisor',
                        lambda *_a, **_k: AdvisorResult(was_error=True, error_msg='superseded'))
    # has_messages stays False → the basic-nudge fallback path (no re-drive).

    re_drives = []
    monkeypatch.setattr(wh, 'relaunch_idle_agent',
                        lambda p, n: (re_drives.append((p, n)), True)[1])

    from fastapi.testclient import TestClient
    monkeypatch.setattr(api_server, 'AFK_NUDGE_TICK_SECS', 0.02)
    monkeypatch.setattr(api_server, 'AFK_NUDGE_COOLDOWN', 1.0)  # one nudge in the window
    app = api_server.create_app(agents=[], agent_pool=pool, config={'session_name': 'w1'})
    with TestClient(app):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not pool._enqueue_calls:
            time.sleep(0.02)

    assert pool._enqueue_calls and pool._enqueue_calls[0][1] == AFK_DEFAULT_MESSAGE
    assert re_drives == []  # no pending text → no re-drive


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
