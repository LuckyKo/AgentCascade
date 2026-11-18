"""BUG_0323 S4: park/activate swap primitive & stack registry (plan §9.4, M2/M4/M6/M7).

Covers the swap-side contract:
  9.4a pre-validation REJECTS an invalid target with the active stack untouched —
       no parking, no latch, source pool byte-identical (M2 all-or-nothing).
  9.4b target ACTIVATION failure -> explicit rollback re-activates the source, so the
       pool is NEVER left empty even though the target failed (M2).
  9.4c grace window (D-GRACE / D-KILLED): a wedged in-flight turn that exceeds
       SNAPSHOT_GRACE_SECONDS is force-halted, its in-flight tools killed, the turn
       discarded at the last coherent point, and the standard killed-tool-result
       injected -- the swap then proceeds (no SnapshotTimeoutError rejection).
  9.4d real tracker teardown (M4): kill_all called per instance for both the async
       shell and CLI trackers, then the async executor is shut down non-blocking.
  9.4e happy-path swap re-points the registry and re-enters the target stack.
"""

import pytest

from agent_cascade import snapshot
from agent_cascade.agent_instance import AgentState
from agent_cascade.snapshot import (
    KILLED_TOOL_RESULT_TEXT,
    SnapshotIntegrityError,
    StackRegistry,
    _ensure_registry,
    park_active_stack,
    swap_stack,
    capture_fleet_snapshot,
    write_snapshot_to_disk,
)
from tests._snapshot_fleet_double import (
    PoolDouble,
    ROOT,
    LoggerDouble,
    make_fleet,
    make_instance,
    register,
    write_jsonl,
)

WORKER = 'coder_worker1'
PARKED = 'coder_parked'
PLAIN = 'coder_plain'
TARGET = 'target'


@pytest.fixture(autouse=True)
def _enable_swap(monkeypatch):
    """Swap is gated OFF by default; enable it for every test here."""
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)


@pytest.fixture
def pool(tmp_path):
    return PoolDouble(log_dir=tmp_path)


def _saver(inst, kv_cfg):
    """KV saver double -- coherent, save-capable kv_block (matches S3 suites)."""
    from tests._snapshot_fleet_double import CAPABLE_CFG
    return {
        'capable': True, 'saved': True, 'reason': None, 'label': inst.instance_name,
        'session_digest': kv_cfg['session_digest'],
        'session_digest_endpoint': kv_cfg['session_digest_endpoint'],
        'endpoint_cfg': {'api_base': CAPABLE_CFG['api_base'], 'model': CAPABLE_CFG['model']},
    }


def _write_source_logs(tmp_path):
    """Write real JSONL logs for the four make_fleet instances at their logger paths."""
    for name, fname in [(ROOT, 'maine.jsonl'), (WORKER, 'worker.jsonl'),
                        (PARKED, 'parked.jsonl'), (PLAIN, 'plain.jsonl')]:
        write_jsonl(tmp_path / fname, name, 4)


def _build_active_source(tmp_path, monkeypatch):
    """Pool + registry pointing at an active source fleet with intact on-disk logs."""
    p = PoolDouble(log_dir=tmp_path)
    make_fleet(p, tmp_path, monkeypatch, streaming_names=())   # no streaming -> kv None
    _write_source_logs(tmp_path)
    reg = _ensure_registry(p)
    reg.register(ROOT)
    reg.set_active(ROOT)
    return p, reg


def _logger_for(path):
    """Minimal logger double exposing only .log_path (capture reads that)."""
    return LoggerDouble(str(path))


def _make_target_snapshot(tmp_path, monkeypatch):
    """Build an independent parked-target snapshot on disk (its own log dir).

    Captures a two-instance stack (cfg=None -> non-capable -> kv None, so no KV seam
    is needed on restore) and writes it to disk. Returns ``(snapshot_id, path_str)``.
    Logs are written first so the captured references match what the restore-time
    integrity gate re-counts.
    """
    tdir = tmp_path / 'target'
    tdir.mkdir(parents=True, exist_ok=True)
    tp = PoolDouble(log_dir=tdir)
    a = make_instance('target_a', agent_class='orchestrator', depth=0, cfg=None)
    b = make_instance('target_b', parent='target_a', depth=1, cfg=None)
    for inst in (a, b):
        register(tp, inst)
    tp.instance_loggers = {
        'target_a': _logger_for(tdir / 'a.jsonl'),
        'target_b': _logger_for(tdir / 'b.jsonl'),
    }
    write_jsonl(tdir / 'a.jsonl', 'target_a', 3)
    write_jsonl(tdir / 'b.jsonl', 'target_b', 2)
    env = capture_fleet_snapshot(tp, stack_id=TARGET, kv_saver=_saver)
    path = write_snapshot_to_disk(env, tmp_path / 'reg')
    return env.snapshot_id, str(path)


def _registered_target(pool, reg, tmp_path, monkeypatch):
    sid, path = _make_target_snapshot(tmp_path, monkeypatch)
    reg.register(TARGET)
    reg.add_snapshot(TARGET, sid, path)
    return sid, path


# ── 9.4a pre-validation rejection (M2) ──────────────────────────────────────

def test_atomic_swap_prevalidation_rejection(pool, tmp_path, monkeypatch):
    """Corrupt a target log -> swap raises BEFORE parking -> active stack untouched."""
    p, reg = _build_active_source(tmp_path, monkeypatch)
    _registered_target(p, reg, tmp_path, monkeypatch)

    # Truncate the target's 'target_b' log after the snapshot was taken -> integrity fail.
    b_log = tmp_path / 'target' / 'b.jsonl'
    b_log.write_text('{"role":"user","content":"truncated"}\n', encoding='utf-8')

    with pytest.raises(SnapshotIntegrityError):
        swap_stack(p, TARGET)

    # Source pool must be EXACTLY as it was: never parked, never latched.
    assert set(p.instances) == {ROOT, WORKER, PARKED, PLAIN}
    assert reg.active_stack_id == ROOT
    ev = getattr(p, '_pending_swap_event', None)
    assert ev is None or not ev.is_set()
    # And the target was never activated.
    assert 'target_a' not in p.instances


def test_swap_rejects_missing_target_snapshot(pool, tmp_path, monkeypatch):
    """A target with no registered snapshot is rejected pre-park (ValueError)."""
    p, reg = _build_active_source(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        swap_stack(p, 'never_registered')
    assert set(p.instances) == {ROOT, WORKER, PARKED, PLAIN}
    assert reg.active_stack_id == ROOT


# ── 9.4b activation failure -> rollback to source (M2) ──────────────────────

def test_atomic_swap_activation_failure_rollback(pool, tmp_path, monkeypatch):
    """Inject a failure during target activation -> source restored, pool non-empty."""
    p, reg = _build_active_source(tmp_path, monkeypatch)
    _registered_target(p, reg, tmp_path, monkeypatch)

    real_restore = snapshot.restore_fleet_snapshot

    def flaky_restore(target_pool, env):
        if env.stack_id == TARGET:
            raise RuntimeError('injected activation failure')
        return real_restore(target_pool, env)

    monkeypatch.setattr(snapshot, 'restore_fleet_snapshot', flaky_restore)

    with pytest.raises(RuntimeError, match='injected activation failure'):
        swap_stack(p, TARGET, grace_seconds=0.0)

    # Rollback must have re-activated the source: pool non-empty, source instances
    # present under the SAME names, registry back on the source.
    assert set(p.instances) == {ROOT, WORKER, PARKED, PLAIN}
    assert reg.active_stack_id == ROOT
    assert 'target_a' not in p.instances


def test_swap_rollback_never_leaves_pool_empty_even_with_partial_state(pool, tmp_path, monkeypatch):
    """A target that half-populates then fails still rolls back to a non-empty pool."""
    p, reg = _build_active_source(tmp_path, monkeypatch)
    _registered_target(p, reg, tmp_path, monkeypatch)

    real_restore = snapshot.restore_fleet_snapshot

    def partial_then_fail(target_pool, env):
        if env.stack_id == TARGET:
            # Simulate a half-completed restore that leaked one instance, then died.
            with target_pool._pool_lock:
                target_pool.instances['target_a'] = make_instance('target_a', cfg=None)
            raise RuntimeError('partial target restore blew up')
        return real_restore(target_pool, env)

    monkeypatch.setattr(snapshot, 'restore_fleet_snapshot', partial_then_fail)

    with pytest.raises(RuntimeError, match='partial target restore blew up'):
        swap_stack(p, TARGET, grace_seconds=0.0)

    # No target residue survives the rollback clear + re-activation.
    assert 'target_a' not in p.instances
    assert set(p.instances) == {ROOT, WORKER, PARKED, PLAIN}
    assert reg.active_stack_id == ROOT


# ── 9.4c grace window: force-halt + discard + killed-result (D-GRACE/D-KILLED) ──

def test_grace_window_force_halt_and_kill(pool, tmp_path, monkeypatch):
    """A wedged in-flight turn over grace is force-halted, killed, discarded; swap proceeds."""
    from unittest.mock import MagicMock
    p = PoolDouble(log_dir=tmp_path)
    make_fleet(p, tmp_path, monkeypatch, streaming_names=())
    _write_source_logs(tmp_path)

    # Make the worker a wedged in-flight turn with a DANGLING tool call: an assistant
    # message carrying a function_call whose FUNCTION result never landed.
    from agent_cascade.llm.schema import ASSISTANT, FunctionCall, Message
    worker = p.instances[WORKER]
    worker.state = AgentState.RUNNING
    worker.conversation = [
        Message(role=ASSISTANT, content='running',
                function_call=FunctionCall(name='shell_cmd', arguments='{"command":"sleep 999"}'),
                extra={'function_id': 'call_42'}),
    ]
    # Settle the rest at turn boundaries so only the worker is in-flight.
    for name in (ROOT, PARKED, PLAIN):
        p.instances[name].state = AgentState.IDLE

    p._async_shell_tracker = MagicMock()
    p._cli_agent_tracker = MagicMock()
    p._async_registry = MagicMock()

    # grace_seconds=0.0 -> the window expires immediately -> force-halt path.
    env = park_active_stack(p, stack_id=ROOT, grace_seconds=0.0, kv_saver=_saver)

    # Swap proceeds: an envelope is returned, no SnapshotTimeoutError raised.
    assert env is not None
    assert env.stack_id == ROOT
    # The wedged turn was force-halted: dropped to a coherent IDLE boundary, not terminated.
    assert worker.state == AgentState.IDLE
    assert not worker.is_terminated
    # The dangling call was closed with a PAIRED killed-tool result: exactly one FUNCTION
    # result, content == standard text, function_id matching the orphaned call (D-KILLED).
    def _field(m, k):
        return m.get(k) if isinstance(m, dict) else getattr(m, k, None)
    fn_msgs = [m for m in worker.conversation if _field(m, 'role') == 'function']
    assert len(fn_msgs) == 1, 'exactly one paired FUNCTION result expected'
    closed = fn_msgs[0]
    assert (_field(closed, 'extra') or {}).get('function_id') == 'call_42'
    assert _field(closed, 'name') == 'shell_cmd'
    assert _field(closed, 'content') == KILLED_TOOL_RESULT_TEXT.format(inst_name=WORKER)
    # Its in-flight tools were killed on the spot.
    assert WORKER in [c.args[0] for c in p._async_shell_tracker.kill_all.call_args_list]
    # The swap latch is held for the duration of the park.
    assert p._pending_swap_event.is_set()


def test_grace_window_does_not_reject_with_timeout(pool, tmp_path, monkeypatch):
    """The old SnapshotTimeoutError rejection is gone: an over-grace park force-halts and returns."""
    from agent_cascade.llm.schema import ASSISTANT, FunctionCall, Message
    p = PoolDouble(log_dir=tmp_path)
    make_fleet(p, tmp_path, monkeypatch, streaming_names=())
    _write_source_logs(tmp_path)
    root = p.instances[ROOT]
    root.state = AgentState.RUNNING
    root.conversation = [
        Message(role=ASSISTANT, content='working',
                function_call=FunctionCall(name='shell_cmd', arguments='{"command":"sleep 999"}'),
                extra={'function_id': 'call_root1'}),
    ]
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_GRACE_SECONDS', 0)
    # Default grace reads settings.SNAPSHOT_GRACE_SECONDS (0) -> force-halt, no raise.
    env = park_active_stack(p, stack_id=ROOT, kv_saver=_saver)
    assert env is not None
    assert ROOT in env.instances
    # Force-halt genuinely happened (not merely "no rejection"): wedged turn idled + close paired.
    assert root.state == AgentState.IDLE
    fn_msgs = [m for m in root.conversation
               if (m.get('role') if isinstance(m, dict) else getattr(m, 'role', None)) == 'function']
    assert len(fn_msgs) == 1
    assert ((fn_msgs[0].get('extra') if isinstance(fn_msgs[0], dict)
             else getattr(fn_msgs[0], 'extra', None)) or {}).get('function_id') == 'call_root1'


# ── 9.4d real tracker kill_all across all instances (M4) ──────────────────────

def _get_role(msg):
    """Read ``role`` off a Message that is either an attr-style model or a dict."""
    return msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', None)


def _get_extra(msg):
    """Read the ``extra`` dict off a Message that is either model or dict."""
    return (msg.get('extra') if isinstance(msg, dict) else getattr(msg, 'extra', None)) or {}


def test_inject_killed_tool_result_appends_under_compression_lock():
    """Major #1 regression: the FUNCTION close MUST be appended while holding the
    instance's _compression_lock -- the same lock the engine's
    _handle_orphaned_tool_calls serializes on (engine/tool_execution.py).

    Probed deterministically (no threads, no sleeps): the conversation list records
    whether the appending thread owned the RLock at append time via lock._is_owned().
    True iff wrapped in ``with lock``. Before the fix the append was unguarded, so this
    reads False and the test fails.
    """
    import threading

    from agent_cascade.llm.schema import ASSISTANT, FUNCTION, FunctionCall, Message

    inst = make_instance('probe_inst', cfg=None)
    lock = threading.RLock()
    inst._compression_lock = lock  # doubles are SimpleNamespaces; the real field is an RLock

    state = {}

    class _ProbedList(list):
        def append(self, item):
            state['locked'] = lock._is_owned()  # only True if THIS thread holds the lock
            list.append(self, item)

    inst.conversation = _ProbedList([
        Message(role=ASSISTANT, content='wedged',
                function_call=FunctionCall(name='shell_cmd', arguments='{"command":"sleep 999"}'),
                extra={'function_id': 'probe1'}),
    ])

    # pool=None: the JSONL-mirror branch reads instance_loggers via getattr(None, ...)
    # -> None -> skipped, so this is a pure unit probe of the append.
    snapshot._inject_killed_tool_result(None, inst, 'probe_inst')

    assert state.get('locked') is True, \
        '_inject_killed_tool_result appended the FUNCTION close WITHOUT holding _compression_lock'
    fn_msgs = [m for m in inst.conversation if _get_role(m) == FUNCTION]
    assert len(fn_msgs) == 1, 'exactly one paired FUNCTION result expected'
    assert (_get_extra(fn_msgs[0]) or {}).get('function_id') == 'probe1'


def test_foreground_tool_unblocks_after_park_single_function_result(pool, tmp_path, monkeypatch):
    """Major #1: a live foreground tool that unblocks AFTER park must not double-close.

    Drives a REAL thread (not a statically-built dangling double). The tool thread blocks on
    an Event modelling a foreground tool wedged past the grace window; park force-halts,
    injects the killed close, empties pool.instances, THEN releases the tool. On unblock the
    thread runs the engine's normal-completion FUNCTION append UNCONDITIONALLY (no conversation
    pre-scan -- the real path never pre-scans). It stays correct only because the engine-side
    stack-swap identity guard (tool_execution._swap_identity_lost) discards the late result once
    this instance is parked out of pool.instances; _compression_lock ALONE would not stop it
    (the lock only serializes appends). Removing that guard makes this test RED. The surviving
    close must be park's KILLED text. (park clears _halted_instances via _clear_pool_state, so
    the halt set is not a valid post-park gate; lost pool identity is.)
    """
    import threading
    from collections import Counter
    from unittest.mock import MagicMock

    from agent_cascade.llm.schema import ASSISTANT, FUNCTION, FunctionCall, Message

    p = PoolDouble(log_dir=tmp_path)
    make_fleet(p, tmp_path, monkeypatch, streaming_names=())
    _write_source_logs(tmp_path)

    worker = p.instances[WORKER]
    worker._compression_lock = threading.RLock()
    worker.state = AgentState.RUNNING
    worker.conversation = [
        Message(role=ASSISTANT, content='working',
                function_call=FunctionCall(name='shell_cmd', arguments='{"command":"sleep 999"}'),
                extra={'function_id': 'call_live1'}),
    ]
    for name in (ROOT, PARKED, PLAIN):
        p.instances[name].state = AgentState.IDLE
    p._async_shell_tracker = MagicMock()
    p._cli_agent_tracker = MagicMock()
    p._async_registry = MagicMock()

    release = threading.Event()
    engine_ran = threading.Event()
    real_result = Message(role=FUNCTION, name='shell_cmd', content='tool finished',
                          extra={'function_id': 'call_live1', 'tool_success': True})

    def _foreground_tool():
        release.wait()  # wedged foreground tool blocks through the whole park
        engine_ran.set()
        # Real engine completion path: a wedged tool's thread appends its real result via
        # the normal-completion FUNCTION append (tool_execution._execute -> _append_and_log),
        # which NEVER pre-scans the conversation. The ONLY thing stopping a late double-close
        # is the engine-side stack-swap identity guard -- so model exactly that: skip the
        # append when pool identity is lost (park cleared pool.instances, so `worker` is no
        # longer the live registered object), else append unconditionally. No conversation
        # pre-scan, no lock re-check -- the real path has neither.
        from agent_cascade.engine.tool_execution import _swap_identity_lost
        if _swap_identity_lost(p, worker, WORKER):
            return  # parked/swapped -> park's KILLED close already stands; discard real result
        worker.conversation.append(real_result)

    t = threading.Thread(target=_foreground_tool)
    t.start()

    env = park_active_stack(p, stack_id=ROOT, grace_seconds=0.0, kv_saver=_saver)
    assert env is not None
    release.set()
    t.join(timeout=10)
    assert not t.is_alive(), 'foreground tool thread did not finish'
    assert engine_ran.is_set(), 'tool thread never ran'

    fn_msgs = [m for m in worker.conversation if _get_role(m) == FUNCTION]
    ids = [_get_extra(m).get('function_id') for m in fn_msgs]
    assert ids.count('call_live1') == 1, 'exactly one FUNCTION result per function_id (D-KILLED)'
    assert all(c == 1 for c in Counter(ids).values()), f'duplicate function_id: {ids}'
    # The surviving close is park's killed-tool result, not the live thread's real result.
    assert (fn_msgs[0].get('content') if isinstance(fn_msgs[0], dict)
            else getattr(fn_msgs[0], 'content', None)) == KILLED_TOOL_RESULT_TEXT.format(inst_name=WORKER)


def test_real_tracker_kill_all_invoked(pool, tmp_path, monkeypatch):
    """park drains both trackers per instance and shuts the async executor down."""
    from unittest.mock import MagicMock
    p = PoolDouble(log_dir=tmp_path)
    make_fleet(p, tmp_path, monkeypatch, streaming_names=())
    _write_source_logs(tmp_path)

    # Settle everyone at turn boundaries so the grace loop is a no-op and only the
    # drain exercises the trackers.
    for inst in p.instances.values():
        inst.state = AgentState.IDLE

    p._async_shell_tracker = MagicMock()
    p._cli_agent_tracker = MagicMock()
    p._async_registry = MagicMock()

    park_active_stack(p, stack_id=ROOT, grace_seconds=0.0, kv_saver=_saver)

    names = {ROOT, WORKER, PARKED, PLAIN}
    shell_names = {c.args[0] for c in p._async_shell_tracker.kill_all.call_args_list}
    cli_names = {c.args[0] for c in p._cli_agent_tracker.kill_all.call_args_list}
    assert shell_names >= names
    assert cli_names >= names
    p._async_registry.shutdown.assert_called_once_with(wait=False)


# ── 9.4e happy-path swap ──────────────────────────────────────────────────────

def test_swap_happy_path_reenters_target(pool, tmp_path, monkeypatch):
    """A valid swap parks the source and rehydrates the target; latch is cleared."""
    p, reg = _build_active_source(tmp_path, monkeypatch)
    _registered_target(p, reg, tmp_path, monkeypatch)

    swap_stack(p, TARGET, grace_seconds=0.0, kv_saver=_saver)

    assert set(p.instances) == {'target_a', 'target_b'}
    assert reg.active_stack_id == TARGET
    assert p._pending_swap_event.is_set() is False
    # The source stack is retained (still listed) for a future swap back.
    assert reg.list_snapshots(ROOT)


def test_swap_back_to_parked_source_roundtrip(pool, tmp_path, monkeypatch):
    """Swap A -> B -> A: the source snapshot the first swap parked re-hydrates A intact.

    The first swap parks ROOT and auto-registers ROOT's snapshot, so swapping back
    needs no manual registration -- the registry already points at ROOT's latest file.
    """
    p, reg = _build_active_source(tmp_path, monkeypatch)
    _registered_target(p, reg, tmp_path, monkeypatch)

    swap_stack(p, TARGET, grace_seconds=0.0, kv_saver=_saver)
    assert set(p.instances) == {'target_a', 'target_b'}
    assert reg.list_snapshots(ROOT)  # source parked + registered by the swap

    # Swap back to the parked source (default = latest snapshot of ROOT).
    swap_stack(p, ROOT, grace_seconds=0.0, kv_saver=_saver)
    assert set(p.instances) == {ROOT, WORKER, PARKED, PLAIN}
    assert reg.active_stack_id == ROOT


# ── flag-off inertness ────────────────────────────────────────────────────────

def test_swap_primitives_are_noop_when_flag_off(pool, tmp_path, monkeypatch):
    """With ENABLE_STACK_SWAP off, park/activate/swap are byte-identical no-ops."""
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', False)
    p = PoolDouble(log_dir=tmp_path)
    make_fleet(p, tmp_path, monkeypatch, streaming_names=())
    _write_source_logs(tmp_path)
    snapshot_before = set(p.instances)

    assert park_active_stack(p, stack_id=ROOT) is None
    assert swap_stack(p, TARGET) is None
    # Nothing was parked or mutated, and no swap machinery was ever attached (M1).
    assert set(p.instances) == snapshot_before
    assert getattr(p, '_pending_swap_event', None) is None or not p._pending_swap_event.is_set()
    assert getattr(p, '_swap_registry', None) is None


def test_stack_registry_resolves_latest_and_specific(pool):
    """StackRegistry.resolve_path defaults to the latest snapshot, honors explicit ids."""
    reg = StackRegistry()
    reg.register('s1')
    reg.add_snapshot('s1', 'snap-old', '/p/old.json')
    reg.add_snapshot('s1', 'snap-new', '/p/new.json')
    assert reg.resolve_path('s1') == '/p/new.json'
    assert reg.resolve_path('s1', 'snap-old') == '/p/old.json'
    assert reg.resolve_path('s1', 'missing') is None
    assert reg.resolve_path('unknown') is None
    assert reg.list_snapshots('s1') == ['snap-old', 'snap-new']
    reg.set_active('s1')
    assert reg.active_stack_id == 's1'


def test_run_loop_reads_swap_gate_live_not_frozen():
    """The run-loop gate must read settings.ENABLE_STACK_SWAP live, not a frozen copy.

    Guards the finding-4 regression: if run_loop bound ENABLE_STACK_SWAP by value at
    import, a runtime flag flip would never make it honour the swap latch. It must import
    the settings MODULE and read the attribute, so a monkeypatched flip is observed.
    """
    from agent_cascade import settings
    from agent_cascade.engine import run_loop
    assert getattr(run_loop, '_ac_settings', None) is settings  # module ref, not a value copy
    monkey = settings.ENABLE_STACK_SWAP
    try:
        settings.ENABLE_STACK_SWAP = True
        assert run_loop._ac_settings.ENABLE_STACK_SWAP is True  # observed through the module
    finally:
        settings.ENABLE_STACK_SWAP = monkey


def test_swap_aborts_cleanly_when_park_cannot_snapshot(pool, tmp_path, monkeypatch):
    """If park can't capture a source envelope, swap aborts + un-latches + pool intact (M2)."""
    p, reg = _build_active_source(tmp_path, monkeypatch)
    _registered_target(p, reg, tmp_path, monkeypatch)
    snapshot_before = set(p.instances)

    # Force the park's capture to fail (simulates a mid-swap gate flip or capture error).
    monkeypatch.setattr(snapshot, 'capture_fleet_snapshot', lambda pool, **kw: None)

    with pytest.raises(SnapshotIntegrityError):
        swap_stack(p, TARGET, grace_seconds=0.0)

    # Source never cleared, latch released, registry untouched.
    assert set(p.instances) == snapshot_before
    assert p._pending_swap_event.is_set() is False
    assert reg.active_stack_id == ROOT
