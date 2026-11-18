"""BUG_0344 regression: a compressor timeout must honour its budget while blocked on
the FIRST stream token, and must not let a fresh compressor re-hit an endpoint whose
previous run was abandoned with its LLM stream reader still in flight.

Fix A (deadline): the compressor's remaining ``COMPRESSION_AGENT_TIMEOUT`` budget is
scoped onto the real ``watch_stream`` total guard via ``stream_total_deadline``, so a
stall blocked before the first token trips the no-chunk total guard inside the budget
instead of waiting out the 900s ``STREAM_MAX_TOTAL_SECONDS`` transport default. The
yield-gated caller timeout (agent_invoker) and the yield-gated engine watchdog (llm_call)
both miss a first-token block, so this is the only lever that fires.

Fix B (ownership retention): the deferred drop-exit release keys on the ``watch_stream``
reader-thread completion Event — the authoritative "is the orphan reader still reading the
endpoint" signal — NOT ``_llm_call_active`` (Fix A's ``StreamStalledError`` is caught by
llm_call's retry handler, which stamps 'end' and clears the flag BEFORE ``_run_finalize``
runs, so the flag is False on exactly the reported first-token-stall path). ``_should_defer_drop_exit``
is the pure gate; ``_defer_drop_exit_release`` WAITS on that Event (bounded by the transport
idle-read cap) then releases the slot exactly once and clears ``_llm_call_active`` so the warm
instance recovers. The reuse gate in ``system_agent`` skips its leaked-permit reclaim (Gate 4)
and rejects reuse (Gate 5) while the Event is unset; it is INERT for a genuine leaked permit
(no Event, or the Event is set), so no already-working reuse path is diverted.

These tests drive the REAL ``watch_stream`` (no passthrough mock) and the REAL reuse gate
(real ``ExecutionEngine`` + fake pool), mirroring tests/test_compressor_instance_reuse.py.

Hermetic: the stalling fakes park the daemon reader on a threading.Event that is released in
the autouse teardown, so the reader unwinds and the test process exits promptly.
"""
import contextlib
import threading
import time

import pytest

import agent_cascade.compressor_reuse as compressor_reuse
import agent_cascade.security_reuse as security_reuse
import agent_cascade.compression.agent_invoker as agent_invoker
from agent_cascade.agent_instance import AgentInstance
from agent_cascade.compression.agent_invoker import (COMPRESSOR_WARM_NAME,
                                                     _execute_compressor_and_extract_summary)
from agent_cascade.engine import run_loop
from agent_cascade.engine.run_loop import RunLoopMixin
from agent_cascade.execution_engine import ExecutionEngine
from agent_cascade.llm.schema import Message, SYSTEM, ASSISTANT
from agent_cascade.utils import streaming
from agent_cascade.utils.streaming import (StreamStalledError, current_stream_reader_done,
                                           stream_total_deadline, watch_stream)

# Stalling streams park their reader forever; release them at teardown so the daemon reader
# unwinds instead of leaking across the run (mirrors tests/llm/test_stream_watchdog.py).
_BLOCKING_STREAMS: list = []

# ── Real-path reuse harness (copied from tests/test_compressor_instance_reuse.py) ──


@pytest.fixture(autouse=True)
def _release_blocking_streams():
    yield
    for ev in _BLOCKING_STREAMS:
        ev.set()
    _BLOCKING_STREAMS.clear()


@pytest.fixture(autouse=True)
def _clear_reuse_registries():
    compressor_reuse._clear_all()
    security_reuse._clear_all()
    yield
    compressor_reuse._clear_all()
    security_reuse._clear_all()


class _StallingIter:
    """Yields ``n`` items, then blocks on an Event that is never set.

    ``n=0`` blocks on the very first ``next()`` — the first-token stall the yield-gated
    guards cannot reach and that Fix A must bound. ``n>=1`` lets the first chunk land (so a
    consumer can observe the reader-completion Event before the reader parks on the 2nd
    ``next()``), modelling an abandoned in-flight stream.
    """

    def __init__(self, n):
        self.n = n
        self._never_set = threading.Event()
        _BLOCKING_STREAMS.append(self._never_set)

    def __iter__(self):
        for i in range(self.n):
            yield f'chunk{i}'
        self._never_set.wait()


def _make_warm_compressor(name=COMPRESSOR_WARM_NAME):
    """A clean, eligible warm Compressor (mirrors the reuse-suite factory)."""
    inst = AgentInstance(
        instance_name=name,
        agent_class='Compressor',
        conversation=[Message(role=SYSTEM, content='COMPRESSOR_SYS_PREFIX')],
        created_at=time.monotonic(),
        last_activity=time.monotonic() - 60.0,  # well past the idle epsilon
        latest_marker_index=-1,
    )
    inst._system_prompt_frozen = True
    return inst


def _make_pool(inst=None):
    """Minimal fake pool exposing exactly what the reuse gate touches."""
    from unittest.mock import MagicMock
    pool = MagicMock()
    pool.instances = {inst.instance_name: inst} if inst else {}
    pool.message_queues = {}
    exec_obj = MagicMock()
    exec_obj._state_lock = threading.RLock()
    exec_obj.active_stack = []
    pool._execution = exec_obj
    pool.is_instance_halted.return_value = False
    pool.is_instance_terminated.return_value = False
    return pool


def _make_engine(pool):
    engine = ExecutionEngine(pool)
    from unittest.mock import MagicMock
    engine._update_webui_state = MagicMock()
    engine.stream_publisher = MagicMock()
    return engine


def _acquire(engine, name=COMPRESSOR_WARM_NAME, task='summarize', caller='Maine', rid='r1',
             agent_class='Compressor'):
    return engine._acquire_reusable_system_agent(
        agent_class=agent_class, instance_name=name, task=task, caller=caller, rid=rid)


# ---------------------------------------------------------------------------
# Fix A — deadline honoured while blocked on the first token (red -> green)
# ---------------------------------------------------------------------------


def test_stream_total_deadline_honored_while_blocked_on_first_token():
    """A first-token stall is cut off inside the scoped deadline, NOT after 900s.

    The total guard is handed the production default ``max_total_seconds=900`` and the ONLY
    thing lowering it is the scoped deadline — faithful to the real path where the backend
    passes the transport default and only the compressor's budget can shorten it. Pre-Fix A
    this blocks ~900s (the reported 351s-and-counting stall); post-Fix A it trips at ~2s.
    """
    start = time.monotonic()
    with pytest.raises(StreamStalledError, match='total'):
        with stream_total_deadline(time.monotonic() + 2.0):
            list(watch_stream(_StallingIter(0), max_silence_seconds=180.0, max_total_seconds=900.0))
    elapsed = time.monotonic() - start
    assert 1.0 <= elapsed < 5.0, f'expected ~2s deadline trip, got {elapsed:.1f}s'


def test_watch_stream_not_cut_without_deadline_scope_inertness_control():
    """Inertness control (discriminating): with NO scope a stall obeys its OWN 5s total.

    Own total is 5.0s — strictly larger than the 2.0s used by the scoped deadline test — so
    if an ambient deadline had leaked onto this thread the stream would trip at ~2s and this
    would fail. A trip at ~5s proves the clamp code path saw ``None`` and left
    ``max_total_seconds`` untouched.
    """
    start = time.monotonic()
    with pytest.raises(StreamStalledError, match='total'):
        list(watch_stream(_StallingIter(0), max_silence_seconds=1.0, max_total_seconds=5.0))
    elapsed = time.monotonic() - start
    assert elapsed >= 4.5, f'expected own 5s total, tripped at {elapsed:.1f}s (ambient leak?)'
    assert elapsed < 8.0


def test_fast_stream_completes_identically_with_and_without_generous_deadline():
    """A fast, normally-completing stream is never diverted to a deferred/short path."""
    plain = list(watch_stream(iter(['a', 'b', 'c']), max_silence_seconds=1.0, max_total_seconds=5.0))
    with stream_total_deadline(time.monotonic() + 10.0):  # generous, must not cut
        scoped_in = list(watch_stream(iter(['a', 'b', 'c']), max_silence_seconds=1.0, max_total_seconds=5.0))
    assert plain == scoped_in == ['a', 'b', 'c']


def test_deadline_resets_on_exit_and_exception():
    """The ambient deadline never leaks onto a later stream, even if the block raises."""
    assert streaming._STREAM_TOTAL_DEADLINE.get() is None
    with stream_total_deadline(time.monotonic() + 2.0):
        assert streaming._STREAM_TOTAL_DEADLINE.get() is not None
    assert streaming._STREAM_TOTAL_DEADLINE.get() is None
    with pytest.raises(ValueError):
        with stream_total_deadline(time.monotonic() + 2.0):
            raise ValueError('boom')
    assert streaming._STREAM_TOTAL_DEADLINE.get() is None


def test_compressor_deadline_is_scoped_from_compression_agent_timeout(monkeypatch):
    """The invoker scopes the deadline from COMPRESSION_AGENT_TIMEOUT (Fix A wiring).

    Monkeypatches the invoker's ``stream_total_deadline`` to CAPTURE the deadline argument it
    is called with, and pins ``COMPRESSION_AGENT_TIMEOUT`` to a known value, then drives the
    real ``_execute_compressor_and_extract_summary`` run-block (empty engine.run → the
    ``with stream_total_deadline(start_time + max_poll_time)`` is entered exactly once). This
    asserts the BUDGET (not a hard-coded value) is what bounds the stream — no source-text
    assertions.
    """
    monkeypatch.setattr(agent_invoker, 'COMPRESSION_AGENT_TIMEOUT', 7.0)
    captured = []

    @contextlib.contextmanager
    def _record(deadline_monotonic):
        captured.append(deadline_monotonic)
        yield
    monkeypatch.setattr(agent_invoker, 'stream_total_deadline', _record)

    from unittest.mock import MagicMock
    comp_instance = MagicMock()
    comp_instance._compression_lock = MagicMock()
    comp_instance._compression_lock.__enter__ = MagicMock(return_value=comp_instance._compression_lock)
    comp_instance._compression_lock.__exit__ = MagicMock(return_value=False)
    comp_instance.conversation = [
        Message(role=SYSTEM, content='sys'),
        Message(role=ASSISTANT, content='no end marker'),  # harvest raises after the loop; irrelevant here
    ]
    engine = MagicMock()
    engine.run.return_value = iter([])
    engine._telemetry.return_value = None
    pool = MagicMock()
    pool.stopped = False
    pool.get_instance = MagicMock(return_value=None)

    before = time.monotonic()
    with pytest.raises(RuntimeError):
        _execute_compressor_and_extract_summary(
            agent_pool=pool, engine=engine, comp_instance=comp_instance,
            comp_state_key='Compressor_test', caller_name=None, timeout_label='Compression')

    assert len(captured) == 1, 'deadline scoped exactly once around the engine.run loop'
    delta = captured[0] - before
    # start_time is taken just inside the call, so the scoped deadline is ~7.0s ahead of now.
    assert 6.5 <= delta < 7.3, f'deadline should be ~COMPRESSION_AGENT_TIMEOUT(7.0) ahead, got {delta:.2f}s'


# ---------------------------------------------------------------------------
# Fix B — deferral predicate keys on the reader-completion Event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('terminated, reader_state, want_defer', [
    (False, 'unset', True),    # abandoned run, reader still reading endpoint -> defer
    (False, 'set', False),     # reader completed -> release now (inert)
    (False, 'none', False),    # no stream opened this run -> release now (inert)
    (True, 'unset', False),    # terminated (stop path owns slot-freeing) -> release now
])
def test_should_defer_drop_exit_gate(terminated, reader_state, want_defer):
    if reader_state == 'unset':
        ev = threading.Event()
    elif reader_state == 'set':
        ev = threading.Event()
        ev.set()
    else:
        ev = None
    assert RunLoopMixin._should_defer_drop_exit(terminated, ev) is want_defer


def test_deferred_release_waits_on_reader_completion_not_a_blind_cap(monkeypatch):
    """The deferred release releases ONCE when the reader completes — in milliseconds, not
    after a fixed wedge — and clears ``_llm_call_active`` so the warm instance recovers.

    ``HTTP_READ_TIMEOUT`` is pinned LARGE (60s) so the only way the release can happen
    quickly is by observing the reader-completion Event, proving the rework keys off
    completion, not a blind sleep. Also asserts the instance carries the reader Event
    (``_stream_reader_done``) as the single source of truth for the reuse gate.
    """
    monkeypatch.setattr(run_loop._ac_settings, 'HTTP_READ_TIMEOUT', 60.0)
    calls = []

    class _Stub:
        def _release_slot(self, instance, name, action=None):
            calls.append((name, action))

    inst = _make_warm_compressor(name='comp-defer')
    inst._llm_call_active = True  # what the deferred path must clear (acceptance #3)
    reader_done = threading.Event()  # still UNSET = orphan reader in flight

    RunLoopMixin._defer_drop_exit_release(_Stub(), inst, 'comp-defer', reader_done)
    assert inst._stream_reader_done is reader_done  # single source of truth attached

    # Not yet released: the reader is still in flight (the endpoint stays BUSY).
    time.sleep(0.2)
    assert calls == []

    # Reader unwinds -> completion signal fires -> release happens promptly.
    set_at = time.monotonic()
    reader_done.set()
    deadline = time.monotonic() + 2.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == [('comp-defer', 'drop-exit-deferred')], 'released exactly once on completion'
    assert time.monotonic() - set_at < 2.0, 'released on completion, not after the 60s cap'
    assert inst._llm_call_active is False, 'poisoning flag cleared so warm instance recovers'


# ---------------------------------------------------------------------------
# Fix B — end-to-end: abandoned in-flight stream blocks reuse until the reader ends
# ---------------------------------------------------------------------------


def _drive_abandoned_stream():
    """Start a REAL watch_stream over a 1-chunk-then-block backend on a consumer thread.

    Consumes the first chunk (so the reader-completion Event is published), then abandons
    the consumer on the SECOND next() — the reader daemon parks in next() and the completion
    Event stays UNSET, exactly modelling an abandoned in-flight stream. Returns the reader
    Event and the backend's release Event.
    """
    backend = _StallingIter(1)
    reader_event_holder = {}
    started = threading.Event()

    def _consumer():
        g = watch_stream(iter(backend), max_silence_seconds=180.0, max_total_seconds=900.0)
        it = g.__iter__()
        first = next(it)  # reader publishes reader_done, then parks on the 2nd next()
        assert first == 'chunk0'
        reader_event_holder['done'] = current_stream_reader_done()
        started.set()
        with contextlib.suppress(Exception):
            next(it)  # blocks in the consumer queue.get; abandoned by teardown/gc

    t = threading.Thread(target=_consumer, daemon=True, name='e2e-consumer')
    t.start()
    assert started.wait(5.0), 'consumer did not publish the reader Event in time'
    return reader_event_holder['done'], backend._never_set


def test_e2e_abandoned_stream_blocks_reuse_until_reader_completes():
    """The reported incident, end to end on the real seams.

    abandon -> deferred release holds the permit -> warm-reuse probe REJECTS the warm name
    while the reader is in flight -> reader unwinds -> deferred release fires once + clears
    the flag -> the endpoint/warm instance recovers and reuse succeeds again. No passthrough
    mock: the completion Event is produced by the real ``watch_stream`` reader thread; the
    reuse gate is the real ``ExecutionEngine._acquire_reusable_system_agent``; the release is
    the real ``RunLoopMixin._defer_drop_exit_release``.
    """
    monkey_timeout = 60.0
    orig_timeout = run_loop._ac_settings.HTTP_READ_TIMEOUT
    run_loop._ac_settings.HTTP_READ_TIMEOUT = monkey_timeout
    try:
        reader_done, release_backend = _drive_abandoned_stream()
        assert reader_done is not None and not reader_done.is_set()  # orphan alive

        warm = _make_warm_compressor()
        engine = _make_engine(_make_pool(warm))
        # Model the deferred-exit state: reader still in flight, ``_llm_call_active`` already
        # False (the reported path), permit still HELD by the abandoned run.
        release_calls = []
        warm._stream_reader_done = reader_done
        warm._llm_call_active = False
        warm._slot_key = 'endpoint-key'
        warm._slot_release = lambda *a, **k: release_calls.append(a)

        # 1. Warm-reuse probe must REJECT the BUSY warm instance (Gate 5 via reader in flight).
        assert _acquire(engine, rid='probe1') is None, 'warm reuse must reject while reader in flight'
        assert warm._slot_release is not None, 'Gate 4 must NOT reclaim the held permit while reader in flight'
        assert release_calls == [], 'deferred permit left intact while the orphan reader is alive'

        # 2. Drive the real deferred release on the warm instance, then let the reader unwind.
        class _Stub:
            def _release_slot(self, instance, name, action=None):
                release_calls.append(action)

        RunLoopMixin._defer_drop_exit_release(_Stub(), warm, warm.instance_name, reader_done)
        set_at = time.monotonic()
        release_backend.set()  # the parked reader's iterator unblocks -> reader finally -> event sets
        deadline = time.monotonic() + 2.0
        while 'drop-exit-deferred' not in release_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert release_calls.count('drop-exit-deferred') == 1, 'released exactly once on reader completion'
        assert time.monotonic() - set_at < 2.0, 'released on completion, not after the capped wedge'
        assert reader_done.is_set()
        assert warm._llm_call_active is False

        # 3. Endpoint recovered: a fresh caller can now reuse the warm instance again. The
        #    deferred release nullified the permit, so model the post-recovery state (a later
        #    leaked permit, reader Event set) and confirm the reuse gate reclaims + reuses.
        warm._slot_release = lambda *a, **k: None
        warm._slot_key = 'endpoint-key'
        got = _acquire(engine, rid='probe2')
        assert got is not None and got[1] is True, 'warm reuse proceeds once the reader is done'
        assert got[0] is warm
    finally:
        run_loop._ac_settings.HTTP_READ_TIMEOUT = orig_timeout


def test_run_finalize_glue_defers_and_attaches_real_reader_event():
    """BUILD Finding B: the defer DECISION + reader-Done attach happen through the REAL
    ``_run_finalize`` glue — not a hand-assigned instance state and a stubbed daemon.

    A real ``watch_stream`` is driven on THIS thread and abandoned after its first chunk, so
    ``current_stream_reader_done()`` is a live, UNSET Event. ``_run_finalize`` is then invoked
    directly (only its unrelated side effects — telemetry, terminal-stop check, state
    transition, immediate release — are stubbed); we assert:
      (a) the immediate ``drop-exit`` release was NOT taken (``_should_defer_drop_exit``
          read the ContextVar Event and chose to defer),
      (b) ``instance._stream_reader_done`` is the SAME Event object published by the real
          ``watch_stream`` reader (attached by the real ``_defer_drop_exit_release``),
      (c) once the reader unwinds, the deferred release fires exactly once and clears both
          ``_llm_call_active`` and ``_stream_reader_done`` (POLISH #3).
    """
    orig_timeout = run_loop._ac_settings.HTTP_READ_TIMEOUT
    run_loop._ac_settings.HTTP_READ_TIMEOUT = 60.0  # only completion can cut it short
    try:
        # 1. Real abandoned stream ON THIS THREAD -> live UNSET reader-Done Event.
        streaming.reset_stream_reader_done()
        backend = _StallingIter(1)
        g = watch_stream(iter(backend), max_silence_seconds=180.0, max_total_seconds=900.0)
        it = g.__iter__()
        assert next(it) == 'chunk0'  # reader publishes reader_done, then parks on the 2nd next()
        reader_done = current_stream_reader_done()
        assert reader_done is not None and not reader_done.is_set()

        inst = _make_warm_compressor(name='comp-glue')
        inst._compression_lock = threading.RLock()
        inst._continue_saved_msg = None
        inst._llm_call_active = True  # the deferred path must clear this on completion
        inst._slot_key = 'endpoint-key'
        inst._slot_release = lambda *a, **k: None  # permit HELD by the abandoned run
        release_calls = []

        pool = _make_pool(inst)
        pool.settings.tail_sync_check_enabled = False  # keep the fake-logger sync block inert
        engine = _make_engine(pool)
        # Stub ONLY the side effects unrelated to the reader-Done glue under test.
        engine._telemetry = lambda: None
        engine._is_terminal_stop = lambda name: False
        engine._finalize_state_transition = lambda i, p: None
        engine._release_slot = lambda i, name, action=None: release_calls.append((name, action))

        # 2. Drive the REAL glue.
        engine._run_finalize(inst, early_exit=True)

        # (a) defer chosen -> immediate drop-exit release NOT taken.
        assert ('comp-glue', 'drop-exit') not in release_calls, \
            'real _run_finalize must defer, not release the slot immediately'
        # (b) the real glue attached the SAME Event object that watch_stream published.
        assert inst._stream_reader_done is reader_done, \
            '_defer_drop_exit_release must attach the real reader-Done Event, not a copy'
        # endpoint still BUSY while the reader is parked — no release yet.
        time.sleep(0.2)
        assert release_calls == [], 'permit must stay held while the orphan reader is alive'

        # (c) reader unwinds -> deferred release fires once, clears flag + field.
        backend._never_set.set()
        deadline = time.monotonic() + 2.0
        while not release_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert release_calls == [('comp-glue', 'drop-exit-deferred')], \
            'released exactly once on reader completion'
        assert reader_done.is_set()
        assert inst._llm_call_active is False, 'poisoning flag cleared so warm instance recovers'
        assert inst._stream_reader_done is None, 'reader-Done field cleared after release (POLISH #3)'
    finally:
        run_loop._ac_settings.HTTP_READ_TIMEOUT = orig_timeout


def test_gate4_reclaims_genuine_leaked_permit_inertness():
    """Inertness for finding #5: a genuine leaked permit (no reader Event) is reclaimed by
    Gate 4 and reuse proceeds — the reader-done guard changes nothing for real leaks.
    """
    warm = _make_warm_compressor()
    engine = _make_engine(_make_pool(warm))
    reclaimed = []
    warm._slot_key = 'endpoint-key'
    warm._slot_release = lambda *a, **k: reclaimed.append(a)
    # No _stream_reader_done at all => a real leak, not a B1 deferral.
    got = _acquire(engine, rid='leak1')
    assert got is not None and got[1] is True, 'genuine leaked permit must be reclaimed and reused'
    assert warm._slot_release is None, 'leaked permit was cleared by Gate 4 reclaim'
