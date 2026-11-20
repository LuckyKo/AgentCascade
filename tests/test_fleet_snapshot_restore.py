"""BUG_0323 S3: fleet rehydration & restore engine (D-CONTINUE).

Covers the restore-side contract from plan §9:
  9.1 round-trip (capture → disk → load → dismiss → restore) re-forms identity,
      conversations, topology, KV arm, and message queues — and the captured
      _last_actual_token_count SURVIVES the log re-attach (rebuild_conversation
      zeroes it, so restore re-applies it).
  9.2 wall↔monotonic clock projection is an exact inverse, and the 0.0 sentinel
      passes through EXACTLY on both sides.
  9.3 D-CONTINUE: active_stack / children / blocked parents re-formed, and the
      execution-graph m2 invariant holds (pool._instance_threads untouched, no
      threads spawned).
  9.4 log-integrity gate REFUSES (zero mutation) when a referenced log diverged.
  9.5 KV restore is fail-closed per instance; an unrestorable KV never blocks the
      other instances' rehydration.
  9.6 restore is a byte-identical no-op when ENABLE_STACK_SWAP is off.
  9.7 a name collision against a live instance is refused before any mutation.
"""

import json
import threading

import pytest

from agent_cascade import snapshot, state_ops
from agent_cascade.agent_instance import AgentState
from agent_cascade.snapshot import (
    SnapshotIntegrityError,
    capture_fleet_snapshot,
    load_snapshot_from_disk,
    monotonic_to_wall,
    restore_fleet_snapshot,
    wall_to_monotonic,
    write_snapshot_to_disk,
)
from tests._snapshot_fleet_double import (
    CAPABLE_CFG,
    LoggerDouble,
    PoolDouble,
    ROOT,
    make_fleet,
    make_instance,
    register,
    write_jsonl,
)

WORKER = 'coder_worker1'
PARKED = 'coder_parked'
PLAIN = 'coder_plain'
FLEET = {ROOT, WORKER, PARKED, PLAIN}


@pytest.fixture(autouse=True)
def _enable_swap(monkeypatch):
    """Restore is gated OFF by default; enable it for every test except 9.6."""
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)


@pytest.fixture
def pool(tmp_path):
    return PoolDouble(log_dir=tmp_path)


def _saver(inst, kv_cfg):
    """KV saver double — emits a coherent, save-capable kv_block for a streaming instance."""
    return {
        'capable': True,
        'saved': True,
        'reason': None,
        'label': inst.instance_name,
        'session_digest': kv_cfg['session_digest'],
        'session_digest_endpoint': kv_cfg['session_digest_endpoint'],
        'endpoint_cfg': {'api_base': CAPABLE_CFG['api_base'], 'model': CAPABLE_CFG['model']},
    }


def _write_all(tmp_path, pool, count=4):
    for name, path in [(n, tmp_path / p) for n, p in
                       [(ROOT, 'maine.jsonl'), (WORKER, 'worker.jsonl'),
                        (PARKED, 'parked.jsonl'), (PLAIN, 'plain.jsonl')]]:
        write_jsonl(path, name, count)


def _dismiss(pool):
    """Simulate process death: wipe in-memory fleet, leave the on-disk logs intact."""
    with pool._pool_lock:
        pool.instances.clear()
        pool.instance_summaries.clear()
        pool.terminated_instances.clear()
    pool.children.clear()
    pool._execution.active_stack.clear()
    pool._halted_instances.clear()
    pool._compression_halted.clear()
    with pool._queue_lock:
        pool.message_queues.clear()


# ── 9.1 round-trip ───────────────────────────────────────────────────────────

def test_roundtrip_rehydrates_fleet(pool, tmp_path, monkeypatch):
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=(WORKER,))
    pool.instances[WORKER]._last_actual_token_count = 7   # must survive re-attach
    write_jsonl(tmp_path / 'maine.jsonl', ROOT, 12)
    write_jsonl(tmp_path / 'worker.jsonl', WORKER, 5)
    write_jsonl(tmp_path / 'parked.jsonl', PARKED, 2)
    write_jsonl(tmp_path / 'plain.jsonl', PLAIN, 1)
    pool.message_queues[WORKER] = ['ping', 'pong']

    env = capture_fleet_snapshot(pool, kv_saver=_saver)
    path = write_snapshot_to_disk(env, tmp_path / 'snap')
    reloaded = load_snapshot_from_disk(path)

    names_before = set(pool.instances)
    _dismiss(pool)
    restore_fleet_snapshot(pool, reloaded)

    assert set(pool.instances) == set(reloaded.instances)
    root = pool.instances[ROOT]
    assert root.agent_class == 'orchestrator'
    assert root.display_name == 'Maine'
    assert root.parent_instance is None
    assert root._nest_depth == 0
    worker = pool.instances[WORKER]
    assert worker.parent_instance == ROOT
    assert worker._nest_depth == 1

    # conversations re-attached from the real on-disk logs
    assert len(root.conversation) == 12
    assert len(worker.conversation) == 5
    # token count survived rebuild_conversation (which zeroes it) — re-applied after re-attach
    assert worker._last_actual_token_count == 7

    # topology re-formed
    assert pool.children[ROOT] == [WORKER, PARKED, PLAIN]
    assert root._child_instances == [WORKER, PARKED, PLAIN]

    # KV verify+arm (no HTTP): engine consumes this at turn start
    assert worker._state_label == WORKER
    assert worker._last_endpoint_config['state_save_enabled'] is True
    assert worker._last_endpoint_config['api_base'] == CAPABLE_CFG['api_base']
    assert pool.instances[PARKED]._state_label is None   # not streaming → not armed

    # S1 identity carry: names restored verbatim (stamped root stays stamped, clean
    # roots stay clean), pool back-ref re-seeded.
    assert set(pool.instances) == names_before
    for name in names_before:
        assert pool.instances[name].instance_name == name
    for inst in pool.instances.values():
        assert inst._pool_ref is pool                      # back-reference re-seeded

    # message queues re-enqueued in FIFO order
    assert pool.message_queues[WORKER] == ['ping', 'pong']


def test_clean_identity_fleet_restores_verbatim(pool, tmp_path, monkeypatch):
    """A fleet whose root carries a CLEAN identity (no birth stamp) round-trips
    exactly — the restore engine never mints a stamp onto a clean name."""
    import agent_cascade.state_ops as state_ops

    clean_root, clean_worker = 'Maine', 'coder_worker1'
    root = make_instance(clean_root, agent_class='orchestrator', display='Maine',
                         depth=0, cfg=CAPABLE_CFG)
    worker = make_instance(clean_worker, agent_class='coder', parent=clean_root,
                           depth=1, cfg=CAPABLE_CFG)
    for inst in (root, worker):
        inst._llm_call_active = False
        register(pool, inst)
    pool.instance_loggers = {
        clean_root: LoggerDouble(str(tmp_path / 'maine.jsonl')),
        clean_worker: LoggerDouble(str(tmp_path / 'worker.jsonl')),
    }
    pool.children = {clean_root: [clean_worker]}

    monkeypatch.setattr(state_ops, 'resolve_held_endpoint', lambda p, inst: None)

    write_jsonl(tmp_path / 'maine.jsonl', clean_root, 6)
    write_jsonl(tmp_path / 'worker.jsonl', clean_worker, 3)
    pool.message_queues[clean_worker] = ['hi']

    env = capture_fleet_snapshot(pool, kv_saver=_saver)
    path = write_snapshot_to_disk(env, tmp_path / 'snap')
    reloaded = load_snapshot_from_disk(path)

    names_before = set(pool.instances)
    _dismiss(pool)
    restore_fleet_snapshot(pool, reloaded)

    assert set(pool.instances) == {clean_root, clean_worker}
    assert pool.instances[clean_root].instance_name == clean_root    # clean, not stamped
    assert pool.instances[clean_worker].instance_name == clean_worker
    assert pool.instances[clean_root].display_name == 'Maine'
    for inst in pool.instances.values():
        assert inst._pool_ref is pool


# ── 9.2 clock projection + sentinel ─────────────────────────────────────────

def test_wall_to_monotonic_exact_inverse_and_sentinel():
    W, M = 1000.0, 400.0
    for v in (123.456, 0.001, 1e6):
        assert wall_to_monotonic(monotonic_to_wall(v, now_wall=W, now_mono=M),
                                 now_wall=W, now_mono=M) == pytest.approx(v)
    # sentinel parity: falsy passes through EXACTLY on both sides
    assert monotonic_to_wall(0.0, now_wall=W, now_mono=M) == 0.0
    assert wall_to_monotonic(0.0, now_wall=W, now_mono=M) == 0.0


def test_clock_projection_survives_capture_restore(pool, tmp_path, monkeypatch):
    # M3: a cross-process monotonic-origin shift changes the RAW monotonic value of
    # every clock — only the WALL projection is invariant. Capture at origin (1000, 400),
    # restore at a SHIFTED origin (1005, 900), then assert projection equality (<1.0s).
    def _frozen(t, m):
        # BUG_0345 added time.perf_counter() to _capture_log_reference (timing-log only);
        # a constant makes elapsed==0, which neither logs nor affects the projection math.
        return type('T', (), {'time': staticmethod(lambda: t),
                              'monotonic': staticmethod(lambda: m),
                              'perf_counter': staticmethod(lambda: 0.0)})
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
    root, parked = pool.instances[ROOT], pool.instances[PARKED]
    # ROOT carries all four NON-zero projected clocks; PARKED carries 0.0 sentinels.
    root.created_at = 250.0
    root.last_activity = 500.0
    root._last_force_compress_time = 600.0
    root._last_force_compress_fail_at = 700.0
    for field in ('last_activity', '_last_force_compress_time', '_last_force_compress_fail_at'):
        setattr(parked, field, 0.0)               # "never happened" sentinels
    _write_all(tmp_path, pool, count=3)

    # CAPTURE at origin (1000, 400) — the envelope stores each clock's wall projection.
    monkeypatch.setattr(snapshot, 'time', _frozen(1000.0, 400.0))
    env = capture_fleet_snapshot(pool)
    captured = {
        'created_at': env.instances[ROOT].clocks['created_at'],
        'last_activity': env.instances[ROOT].clocks['last_activity'],
        '_last_force_compress_time': env.instances[ROOT].compression['last_force_compress_time'],
        '_last_force_compress_fail_at': env.instances[ROOT].compression['last_force_compress_fail_at'],
    }
    assert captured['created_at'] != 250.0        # projected wall time, not the raw monotonic

    _dismiss(pool)
    # RESTORE at a DIFFERENT monotonic origin: raw values must shift, projections must not.
    RW, RM = 1005.0, 900.0
    monkeypatch.setattr(snapshot, 'time', _frozen(RW, RM))
    restore_fleet_snapshot(pool, env)
    nr = pool.instances[ROOT]

    # the raw monotonic is meaningless across the shift (origin moved ~500s) — that is M3.
    assert nr.created_at != pytest.approx(250.0)

    # projection equality: re-projecting the restored monotonic onto wall at the RESTORE
    # origin must land within 1.0s of the captured wall projection, for all four fields.
    for field, captured_wall in captured.items():
        restored_wall = monotonic_to_wall(getattr(nr, field), now_wall=RW, now_mono=RM)
        assert abs(restored_wall - captured_wall) < 1.0, field

    # sentinel parity: a 0.0 "never happened" clock restores to 0.0 EXACTLY, not epoch garbage.
    np = pool.instances[PARKED]
    assert np.last_activity == 0.0
    assert np._last_force_compress_time == 0.0
    assert np._last_force_compress_fail_at == 0.0


# ── 9.3 D-CONTINUE graph re-form + m2 ────────────────────────────────────────

def test_continue_reforms_execution_graph(pool, tmp_path, monkeypatch):
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
    _write_all(tmp_path, pool, count=3)

    env = capture_fleet_snapshot(pool)
    captured_stack = [list(x) for x in env.pool_state.active_stack]

    sentinel_threads = object()
    pool._instance_threads = sentinel_threads   # m2: restore must never read/write this

    _dismiss(pool)
    threads_before = {t.name for t in threading.enumerate()}
    restore_fleet_snapshot(pool, env)
    new_threads = {t.name for t in threading.enumerate()} - threads_before

    assert [list(x) for x in pool._execution.active_stack] == captured_stack
    assert set(pool.children[ROOT]) == {WORKER, PARKED, PLAIN}
    # a parent with children on the stack is re-blocked (RUNNING), not parked
    assert pool.instances[ROOT].state == AgentState.RUNNING

    # m2 invariant: no live thread/slot handle resurrected, none spawned
    assert pool._instance_threads is sentinel_threads
    assert new_threads == set()


def test_idle_sibling_off_stack_stays_idle(pool, tmp_path, monkeypatch):
    # spec §649 / plan §6 "never blanket": an idle sibling that is OFF the captured
    # execution stack must stay IDLE and must NOT be put back on the stack.
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
    pool.instances[PARKED].state = AgentState.IDLE            # idle sibling
    pool.active_stack[:] = [e for e in pool.active_stack if e[0] != PARKED]  # off the stack
    _write_all(tmp_path, pool, count=3)

    env = capture_fleet_snapshot(pool)
    assert PARKED not in [e[0] for e in env.pool_state.active_stack]   # precondition

    _dismiss(pool)
    restore_fleet_snapshot(pool, env)

    assert pool.instances[PARKED].state == AgentState.IDLE                # never blanket'd
    assert PARKED not in [e[0] for e in pool._execution.active_stack]     # still off the stack


# ── 9.4 log-integrity gate ───────────────────────────────────────────────────

def test_validate_envelope_integrity_raises_with_name_and_path(pool, tmp_path, monkeypatch):
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
    _write_all(tmp_path, pool, count=4)
    env = capture_fleet_snapshot(pool)
    (tmp_path / 'worker.jsonl').write_text('{}\n', encoding='utf-8')   # diverged after capture
    with pytest.raises(SnapshotIntegrityError) as exc:
        snapshot.validate_envelope_integrity(env)
    msg = str(exc.value)
    assert WORKER in msg and 'worker.jsonl' in msg


def test_append_to_captured_log_is_tolerant_and_restores_prefix(pool, tmp_path, monkeypatch):
    """BUG_0345: append-only growth of a captured log is now LEGAL. validate_log_reference
    is append-tolerant (size >= offset with an intact captured prefix is valid), and
    restore re-attaches EXACTLY the captured prefix — the appended turn is ignored.

    (This replaced the S3 append-REFUSAL test, which asserted the old strict rule that any
    post-capture growth was corruption. Truncation and missing-path refusals are kept.)"""
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
    _write_all(tmp_path, pool, count=4)
    env = capture_fleet_snapshot(pool)
    captured_count = env.instances[WORKER].log['message_count']
    assert captured_count == 4

    # Append one live turn AFTER capture: the on-disk log now holds one extra message.
    with open(tmp_path / 'worker.jsonl', 'a', encoding='utf-8') as f:
        f.write('{"role":"user","content":"extra"}\n')

    # Unit level: the reference validates (size > offset is normal for a live fleet).
    assert snapshot.validate_log_reference(env.instances[WORKER].log) is None
    # Envelope gate level: no longer refuses on append.
    snapshot.validate_envelope_integrity(env)

    _dismiss(pool)
    restore_fleet_snapshot(pool, env)
    # Restore yields exactly the captured prefix; the appended turn is not re-attached.
    assert len(pool.instances[WORKER].conversation) == captured_count
    assert len(pool.instances[WORKER].conversation) < 5   # the appended line was dropped


def test_validate_refuses_same_size_rewritten_prefix(pool, tmp_path, monkeypatch):
    """BUG_0345: a same-size, in-place rewrite of the captured prefix is corruption —
    it is refused by the prefix-hash comparison (size >= offset, so the truncation guard
    does not fire). Distinct from an append (tolerated) and a truncation (size guard)."""
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
    _write_all(tmp_path, pool, count=4)
    env = capture_fleet_snapshot(pool)
    ref = env.instances[WORKER].log
    assert ref['prefix_sha256'], 'capture must record a prefix hash for a present log'

    log = tmp_path / 'worker.jsonl'
    off = ref['offset']
    raw = log.read_bytes()
    flipped = bytearray(raw)
    i = raw.index(b'"', 0, off)              # an ASCII byte strictly inside the hashed prefix
    flipped[i] ^= 0x01                       # 1-for-1 byte swap: size-invariant, hash-changing
    log.write_bytes(bytes(flipped))
    assert log.stat().st_size == len(raw) == off  # same byte size, different content

    # Fresh-process precondition: production validate() runs in a process whose
    # (path, size, mtime)-keyed digest cache does NOT already hold this exact key, so it
    # takes the sha256 re-hash path — the real corruption catcher. Clearing the module
    # cache here makes the assertion order-independent; the mtime in the cache key already
    # makes a rewrite miss in the real process (write_bytes bumps mtime). We are NOT
    # papering over a defect — we are pinning the validator's contract.
    snapshot._hash_cache.clear()

    reason = snapshot.validate_log_reference(ref)
    assert reason and 'hash mismatch' in reason, f"expected prefix-hash refusal, got {reason!r}"
    with pytest.raises(SnapshotIntegrityError) as exc:
        snapshot.validate_envelope_integrity(env)
    assert WORKER in str(exc.value)


def test_integrity_gate_refuses_missing_log(pool, tmp_path, monkeypatch):
    # nit(ii): the path-missing branch of validate_log_reference (referenced log absent).
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
    _write_all(tmp_path, pool, count=4)
    env = capture_fleet_snapshot(pool)
    (tmp_path / 'worker.jsonl').unlink()                    # log disappears after capture

    # unit level: validate_log_reference reports the missing path, not a count mismatch.
    reason = snapshot.validate_log_reference(env.instances[WORKER].log)
    assert reason and 'missing' in reason and 'worker.jsonl' in reason

    # gate level: the envelope validator refuses, naming the instance.
    with pytest.raises(SnapshotIntegrityError) as exc:
        snapshot.validate_envelope_integrity(env)
    assert WORKER in str(exc.value) and 'missing' in str(exc.value)


# ── 9.5 KV fail-closed per instance ─────────────────────────────────────────

def _kv_block(label, *, api_base=None, model=None, digest=None, capable=True, saved=True):
    api_base = CAPABLE_CFG['api_base'] if api_base is None else api_base
    model = CAPABLE_CFG['model'] if model is None else model
    ep = digest if digest is not None else (api_base, model)
    return {'capable': capable, 'saved': saved, 'reason': None, 'label': label,
            'session_digest': 'd', 'session_digest_endpoint': list(ep),
            'endpoint_cfg': {'api_base': api_base, 'model': model}}


def _armed_inst(name):
    inst = make_instance(name)
    inst._state_label = None
    return inst


def test_kv_verify_arms_on_happy_path(monkeypatch):
    # resolve_held_endpoint is patched to ignore its pool arg, so None is passed.
    monkeypatch.setattr(state_ops, 'resolve_held_endpoint',
                        lambda p, i: {'api_base': CAPABLE_CFG['api_base'], 'model': CAPABLE_CFG['model']})
    inst = _armed_inst(WORKER)
    assert state_ops.verify_and_restore_instance_kv(None, inst, _kv_block(WORKER)) is True
    assert inst._state_label == WORKER
    assert inst._last_endpoint_config == {'api_base': CAPABLE_CFG['api_base'],
                                          'model': CAPABLE_CFG['model'], 'state_save_enabled': True}


def test_kv_verify_preserves_pre_existing_label(monkeypatch):
    # 9.5(f): the arm guard is "if _state_label is None" — a label already pending
    # (e.g. from an earlier turn) is never clobbered, yet the call still returns True.
    monkeypatch.setattr(state_ops, 'resolve_held_endpoint',
                        lambda p, i: {'api_base': CAPABLE_CFG['api_base'], 'model': CAPABLE_CFG['model']})
    inst = make_instance(WORKER)
    inst._state_label = 'preexisting-label'               # already pending before restore
    assert state_ops.verify_and_restore_instance_kv(None, inst, _kv_block(WORKER)) is True
    assert inst._state_label == 'preexisting-label'        # NOT clobbered
    assert inst._last_endpoint_config == {'api_base': CAPABLE_CFG['api_base'],
                                          'model': CAPABLE_CFG['model'], 'state_save_enabled': True}


def test_kv_verify_fail_closed_cases(monkeypatch):
    # pool arg unused — every resolve_held_endpoint below is patched, so None is passed.
    monkeypatch.setattr(state_ops, 'resolve_held_endpoint',
                        lambda p, i: {'api_base': CAPABLE_CFG['api_base'], 'model': CAPABLE_CFG['model']})

    cases = [
        ('non-dict', 'not-a-dict'),
        ('label-mismatch', _kv_block('someone_else')),
        ('digest-incoherent', _kv_block(WORKER, digest=(CAPABLE_CFG['api_base'], 'other-model'))),
        ('not-capable', _kv_block(WORKER, capable=False)),
        ('not-saved', _kv_block(WORKER, saved=False)),
    ]
    for name, kv in cases:
        inst = _armed_inst(WORKER)
        assert state_ops.verify_and_restore_instance_kv(None, inst, kv) is False, name
        assert inst._state_label is None, name                       # never armed
        assert inst._last_endpoint_config is None, name

    # endpoint moved: resolver says a different endpoint than what was saved
    monkeypatch.setattr(state_ops, 'resolve_held_endpoint',
                        lambda p, i: {'api_base': CAPABLE_CFG['api_base'], 'model': 'm-moved'})
    inst = _armed_inst(WORKER)
    assert state_ops.verify_and_restore_instance_kv(None, inst, _kv_block(WORKER)) is False
    assert inst._state_label is None

    # no held endpoint
    monkeypatch.setattr(state_ops, 'resolve_held_endpoint', lambda p, i: None)
    inst = _armed_inst(WORKER)
    assert state_ops.verify_and_restore_instance_kv(None, inst, _kv_block(WORKER)) is False
    assert inst._state_label is None


def test_unrestorable_kv_does_not_block_rehydration(pool, tmp_path, monkeypatch):
    # Streaming worker, but the resolver reports a MOVED endpoint → KV cannot be armed,
    # yet every instance must still rehydrate (message-only cold start).
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=(WORKER,))
    _write_all(tmp_path, pool, count=3)
    env = capture_fleet_snapshot(pool, kv_saver=_saver)

    def moved_endpoint(p, i):
        if i.instance_name in (WORKER,):
            return {'api_base': CAPABLE_CFG['api_base'], 'model': 'm-moved'}
        return None
    monkeypatch.setattr(state_ops, 'resolve_held_endpoint', moved_endpoint)

    _dismiss(pool)
    restore_fleet_snapshot(pool, env)

    assert set(pool.instances) == FLEET                 # all present
    assert pool.instances[WORKER]._state_label is None  # KV not armed (cold start)
    assert len(pool.instances[WORKER].conversation) == 3


# ── 9.6 no-op when flag off ──────────────────────────────────────────────────

def test_restore_noop_when_flag_off(pool, tmp_path, monkeypatch):
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=(WORKER,))
    _write_all(tmp_path, pool, count=2)
    env = capture_fleet_snapshot(pool, kv_saver=_saver)

    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', False)
    before_instances = pool.instances
    before_children = pool.children
    before_stack = pool._execution.active_stack
    before_queues = pool.message_queues

    restore_fleet_snapshot(pool, env)

    # byte-identical off-path: every container is the SAME object, unchanged
    assert pool.instances is before_instances
    assert pool.children is before_children
    assert pool._execution.active_stack is before_stack
    assert pool.message_queues is before_queues
    assert set(pool.instances) == FLEET


# ── 9.7 name collision refused ───────────────────────────────────────────────

def test_name_collision_refused(pool, tmp_path, monkeypatch):
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
    _write_all(tmp_path, pool, count=2)
    env = capture_fleet_snapshot(pool)

    _dismiss(pool)
    colliding = make_instance(WORKER, cfg=None)
    pool.instances[WORKER] = colliding   # a live instance the snapshot would clobber

    with pytest.raises(SnapshotIntegrityError) as exc:
        restore_fleet_snapshot(pool, env)
    assert WORKER in str(exc.value)
    # zero mutation: the collision is caught before any rehydration
    assert pool.instances == {WORKER: colliding}
    assert pool._execution.active_stack == []


# ── loader fail-closed (plan §10.5) ──────────────────────────────────────────

def test_load_snapshot_from_disk_fail_closed(pool, tmp_path, monkeypatch):
    # Every bad input on the load path must raise ValueError: corrupt JSON, an
    # out-of-window schema version, and structural damage (not a dict / missing key).
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
    _write_all(tmp_path, pool, count=2)
    good = capture_fleet_snapshot(pool).to_dict()

    def _refuses(name, payload):
        path = tmp_path / name
        path.write_text(payload, encoding='utf-8')
        with pytest.raises(ValueError):
            load_snapshot_from_disk(path)

    # (a) corrupt JSON — json.loads fails before any schema logic
    _refuses('bad_a.json', '{"schema_version": "1.0.0",,,}')
    # (b) out-of-window schema version — migrate_envelope rejects before the reader
    versioned = dict(good)
    versioned['schema_version'] = '99.0.0'
    _refuses('bad_b.json', json.dumps(versioned))
    # (c) structural damage: instances is not a dict
    instances_bad = dict(good)
    instances_bad['instances'] = 'not-a-dict'
    _refuses('bad_c.json', json.dumps(instances_bad))
    # (d) structural damage: a required top-level key is missing
    missing_key = dict(good)
    missing_key.pop('snapshot_id')
    _refuses('bad_d.json', json.dumps(missing_key))


def test_load_refuses_null_or_unknown_state(pool, tmp_path, monkeypatch):
    # Finding #1: a captured ``state`` block whose NAME is null or unknown is refused at
    # LOAD time (ValueError), so restore's strict ``AgentState[...]`` indexing can never
    # surface a raw TypeError mid-mutation. Deep-copy via json round-trip to keep `good`
    # intact across the two cases.
    make_fleet(pool, tmp_path, monkeypatch, streaming_names=())
    _write_all(tmp_path, pool, count=2)
    good = capture_fleet_snapshot(pool).to_dict()

    for bad_state in (None, 'NOT_A_STATE'):
        payload = json.loads(json.dumps(good))
        payload['instances'][ROOT]['state']['state'] = bad_state
        path = tmp_path / f'bad_state_{bad_state}.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        with pytest.raises(ValueError):
            load_snapshot_from_disk(path)
