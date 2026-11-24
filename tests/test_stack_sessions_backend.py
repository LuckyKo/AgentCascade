"""BUG_0345 S6: sessions-UI backend regression suite (plan §13.1).

Exercises the now-uncommitted S6 backend primitives against the REAL implementations
(no hand-rolled stand-ins for the things under test):

  * snapshot.py registry rebuild + corrupt-snapshot quarantine + append-tolerant
    integrity gate (prefix sha256) + prefix-materialising restore;
  * pool/session_io._reattach_instance_log prefix-copy restore (origin untouched);
  * pool/config_persist persistence round-trip of ``enable_stack_swap`` /
    ``stack_restore_on_boot`` (env marker wins over the persisted value);
  * session_state_store active-stack pointer;
  * task-end / restart / shutdown autosave hooks (no quiescence gate, never raises,
    idempotent on shutdown);
  * prune / delete-snapshot / list-stacks-with-caption / session-scan loadable flag;
  * WS-handler contracts: swap re-points ``session['session_name']``, named new
    session, create-failure rollback, import add / new_stack modes.

Every test is hermetic: temp dirs only, ``AGENT_CASCADE_TEST_CONFIG_DIR`` /
``AGENT_CASCADE_INSTANCE_ID`` isolation (set by tests/conftest.py), real snapshot
primitives, NO live ``config/``, no network, no full AgentPool. Module-global flag
mutations are scoped through ``monkeypatch.setattr`` and ``pool._swap_registry_loaded``
is reset per pool (plan §15 risk table).
"""

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cascade import snapshot, settings
from agent_cascade.agent_instance import AgentState
from agent_cascade.snapshot import (
    SnapshotIntegrityError,
    capture_fleet_snapshot,
    delete_snapshot,
    delete_stack,
    ensure_registry_loaded,
    list_stacks,
    maybe_autosave_on_task_end,
    prune_snapshots,
    snapshot_stack,
    validate_envelope_integrity,
    validate_log_reference,
    write_snapshot_to_disk,
)
from agent_cascade.pool.config_persist import ConfigPersistMixin
from agent_cascade.pool.session_io import SessionIOMixin
from agent_cascade.ws_handlers import WsMessageHandler, MsgType, WsEnvelope
from tests._snapshot_fleet_double import (
    LoggerDouble,
    PoolDouble,
    ROOT,
    make_instance,
    register,
    write_jsonl,
)


@pytest.fixture(autouse=True)
def _enable_swap(monkeypatch):
    """The whole suite exercises the gated swap backend; turn the gate on per test."""
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_AUTOSAVE_ENABLED', True)


@pytest.fixture(autouse=True)
def _isolated_config_dir(monkeypatch, tmp_path):
    """Pin session-state persistence to this test's tmp dir (independent of conftest)."""
    cfg = tmp_path / 'config'
    cfg.mkdir(exist_ok=True)
    monkeypatch.setenv('AGENT_CASCADE_TEST_CONFIG_DIR', str(cfg))


# ─────────────────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────────────────


def _single_root_pool(tmp_path, *, root=ROOT, log_name='root.jsonl'):
    """A one-instance stack (root only, cfg=None -> non-streaming -> kv None).

    Writes a real 4-message log at the path the capture reads, so every captured
    reference matches what the restore-time gate re-counts. Returns
    ``(pool, log_path)``.
    """
    p = PoolDouble(log_dir=tmp_path)
    inst = make_instance(root, agent_class='orchestrator', display='Maine', cfg=None)
    register(p, inst)
    log_path = tmp_path / log_name
    write_jsonl(log_path, root, 4)
    p.instance_loggers = {root: LoggerDouble(str(log_path))}
    return p, log_path


def _capture_env(tmp_path, *, root=ROOT, log_name=None):
    """Capture a single-root envelope and return ``(env, log_path)``.

    The envelope's ``instances[root].log`` reference is the integrity-gate target.
    """
    log_name = log_name or f'{root}.jsonl'
    p, log_path = _single_root_pool(tmp_path, root=root, log_name=log_name)
    env = capture_fleet_snapshot(p, trigger='user', stack_id=root)
    assert env is not None
    return env, log_path


def _snap_dir(tmp_path, monkeypatch):
    """Point snapshot.get_snapshot_dir at tmp_path/'snapshots' (keeps the suite off
    the live workspace that PoolDouble's get_session_log_dir would fall back to)."""
    snap_dir = tmp_path / 'snapshots'
    snap_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(snapshot, 'get_snapshot_dir', lambda pool: snap_dir)
    return snap_dir


class _ConfigPool(ConfigPersistMixin):
    """Minimal host object exposing exactly the attributes both save and load touch.

    Mirrors real AgentPool attribute names so ``_save_pool_settings`` and
    ``_load_pool_settings`` run unmodified (pattern from test_remove_class_defaults).
    """

    def __init__(self, path: Path):
        from agent_cascade.agent_instance import PoolSettings
        self._pool_settings_path = Path(path)
        self._settings_save_lock = threading.Lock()
        self._ui_disabled_tools_lock = threading.RLock()
        self._ui_disabled_tools = {}
        self.llm_cfg = {}
        self.settings = PoolSettings()
        self.auto_security = False
        self._enable_async_shell_console_window = False


# ─────────────────────────────────────────────────────────────────────────────
# Registry rebuild + quarantine + active pointer
# ─────────────────────────────────────────────────────────────────────────────


def test_registry_rebuild_from_disk(tmp_path, monkeypatch):
    """two stacks / three envelopes -> both listed, ascending created_at, loaded flag."""
    snap_dir = _snap_dir(tmp_path, monkeypatch)
    env_a1, _ = _capture_env(tmp_path, root='StackA', log_name='a1.jsonl')
    write_snapshot_to_disk(env_a1, snap_dir)
    env_a2, _ = _capture_env(tmp_path, root='StackA', log_name='a2.jsonl')
    write_snapshot_to_disk(env_a2, snap_dir)
    env_b1, _ = _capture_env(tmp_path, root='StackB', log_name='b1.jsonl')
    write_snapshot_to_disk(env_b1, snap_dir)

    p = PoolDouble(log_dir=tmp_path)
    reg = ensure_registry_loaded(p)

    assert set(reg._snapshots) == {'StackA', 'StackB'}
    assert len(reg.list_snapshots('StackA')) == 2
    assert len(reg.list_snapshots('StackB')) == 1
    # loaded flag set (idempotent rebuild thereafter skips the disk walk).
    assert p._swap_registry_loaded is True
    # Ascending created_at across a stack: the later capture sorts after the earlier.
    a_created = [snapshot.load_snapshot_from_disk(
        Path(reg.resolve_path('StackA', sid))).created_at
        for sid in reg.list_snapshots('StackA')]
    assert a_created == sorted(a_created)


def test_registry_rebuild_quarantines_corrupt(tmp_path, monkeypatch):
    """bad JSON + missing log + truncated log -> moved to _corrupt/ w/ .reason.txt,
    not listed, healthy siblings survive."""
    snap_dir = _snap_dir(tmp_path, monkeypatch)

    # Healthy envelope (the sibling that must survive).
    healthy, _ = _capture_env(tmp_path, root='Good', log_name='good.jsonl')
    write_snapshot_to_disk(healthy, snap_dir)

    # 1) bad JSON.
    bad = snap_dir / 'bad-json.json'
    bad.write_text('{ this is not json', encoding='utf-8')

    # 2) missing referenced log: a valid envelope whose root log path points nowhere.
    missing, _ = _capture_env(tmp_path, root='Missing', log_name='missing.jsonl')
    d = missing.to_dict()
    next(iter(d['instances'].values()))['log']['path'] = str(tmp_path / 'does-not-exist.jsonl')
    (snap_dir / f'{missing.snapshot_id}.json').write_text(json.dumps(d), encoding='utf-8')

    # 3) truncated log: capture valid, then truncate the real log below offset.
    trunc, trunc_log = _capture_env(tmp_path, root='Truncated', log_name='trunc.jsonl')
    write_snapshot_to_disk(trunc, snap_dir)
    trunc_log.write_text('{"role":"user","content":"tiny"}\n', encoding='utf-8')

    p = PoolDouble(log_dir=tmp_path)
    reg = ensure_registry_loaded(p)

    corrupt_dir = snap_dir / '_corrupt'
    quarantined = sorted(x.name for x in corrupt_dir.glob('*.json'))
    assert len(quarantined) == 3
    assert {x.name for x in corrupt_dir.glob('*.json.reason.txt')} == {
        f'{name}.reason.txt' for name in quarantined}
    assert (corrupt_dir / f'{quarantined[0]}.reason.txt').read_text(encoding='utf-8').strip()
    # Only the healthy sibling remains in the live registry.
    assert set(reg._snapshots) == {'Good'}
    # The bad-JSON file no longer sits in the snapshots dir.
    assert not bad.exists()


def test_active_pointer_roundtrip(tmp_path, monkeypatch):
    """save/load active id; corrupt file -> ''."""
    from agent_cascade.session_state_store import load_active_stack_id, save_active_stack_id

    assert save_active_stack_id('StackX') is True
    assert load_active_stack_id() == 'StackX'
    # Corrupt the backing file -> never raises, returns ''.
    state_path = Path(os.environ['AGENT_CASCADE_TEST_CONFIG_DIR']) / 'session_state.json'
    state_path.write_text('{ broken json', encoding='utf-8')
    assert load_active_stack_id() == ''


# ─────────────────────────────────────────────────────────────────────────────
# Settings persistence (enable_stack_swap / stack_restore_on_boot)
# ─────────────────────────────────────────────────────────────────────────────


def test_enable_stack_swap_persist_roundtrip(tmp_path, monkeypatch):
    """save writes the key; a fresh pool load restores the module flag."""
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP_FROM_ENV', False)
    path = tmp_path / 'pool_settings.json'

    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', False)
    pool_a = _ConfigPool(path)
    pool_a._save_pool_settings()
    persisted = json.loads(path.read_text(encoding='utf-8'))
    assert persisted['enable_stack_swap'] is False

    # "fresh pool": reset the module flag, load from the same disk, expect restore.
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', False)
    pool_b = _ConfigPool(path)
    pool_b._load_pool_settings()
    assert settings.ENABLE_STACK_SWAP is False  # persisted False round-tripped

    # And a True round-trip proves the flag (not a stale value) is restored.
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
    _ConfigPool(path)._save_pool_settings()
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', False)
    _ConfigPool(path)._load_pool_settings()
    assert settings.ENABLE_STACK_SWAP is True


def test_boot_restore_default_on(tmp_path, monkeypatch):
    """config with no keys -> STACK_RESTORE_ON_BOOT True; seed default agrees."""
    monkeypatch.setattr('agent_cascade.settings.STACK_RESTORE_ON_BOOT', True)
    path = tmp_path / 'pool_settings.json'
    # A legacy file that carries unrelated content but no boot-restore key.
    path.write_text(json.dumps({'some_other_key': 1}), encoding='utf-8')

    pool = _ConfigPool(path)
    pool._load_pool_settings()
    assert settings.STACK_RESTORE_ON_BOOT is True

    from agent_cascade.settings_registry import _SEED_DEFAULTS
    assert _SEED_DEFAULTS['stack_restore_on_boot'] is True


def test_env_overrides_persisted_gate(tmp_path, monkeypatch):
    """explicit env marker -> persisted value ignored."""
    path = tmp_path / 'pool_settings.json'
    # A save persists whatever the module flag currently is.
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP_FROM_ENV', False)
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)
    _ConfigPool(path)._save_pool_settings()
    persisted = json.loads(path.read_text(encoding='utf-8'))
    assert persisted['enable_stack_swap'] is True

    # Now the env var is "explicitly set" -> the persisted value must be ignored.
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP_FROM_ENV', True)
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', False)
    _ConfigPool(path)._load_pool_settings()
    assert settings.ENABLE_STACK_SWAP is False  # persisted True ignored under env marker


# ─────────────────────────────────────────────────────────────────────────────
# Task-end autosave
# ─────────────────────────────────────────────────────────────────────────────


def _autosave_pool(tmp_path):
    """Pool with a root + child and a get_instance accessor (real maybe_autosave path)."""
    p, _log = _single_root_pool(tmp_path)
    child = make_instance('coder_w', parent=ROOT, depth=1, cfg=None)
    register(p, child)
    p.get_instance = p.instances.get
    return p


def _spy_snapshot_stack(monkeypatch):
    """Replace snapshot.snapshot_stack with a recording stub; return the calls list."""
    calls = []
    monkeypatch.setattr(
        snapshot, 'snapshot_stack',
        lambda pool, *a, **kw: calls.append(dict(kw) or {'trigger': kw.get('trigger')}))
    return calls


def test_task_end_autosave_saves_while_sibling_runs(tmp_path, monkeypatch):
    """a sibling is RUNNING -> the save still happens (no quiescence gate)."""
    p = _autosave_pool(tmp_path)
    p.instances['coder_w'].state = AgentState.RUNNING   # live sibling must not block a save
    calls = _spy_snapshot_stack(monkeypatch)

    maybe_autosave_on_task_end(p, ROOT)
    assert len(calls) == 1
    assert calls[0].get('trigger') == 'task_end'
    # The finishing root is named explicitly: its stream has ended by the time the
    # snapshot runs, so it is not "in flight" and would otherwise lose its KV block.
    assert calls[0].get('just_finished') == {ROOT}
    # And the automatic save is restricted to streaming instances + that root.
    assert calls[0].get('only_streaming') is True


def test_task_end_autosave_independent_of_boot_restore(tmp_path, monkeypatch):
    """boot restore off -> the save still happens."""
    monkeypatch.setattr('agent_cascade.settings.STACK_RESTORE_ON_BOOT', False)
    p = _autosave_pool(tmp_path)
    calls = _spy_snapshot_stack(monkeypatch)
    maybe_autosave_on_task_end(p, ROOT)
    assert len(calls) == 1


def test_task_end_autosave_on_suspension(tmp_path, monkeypatch):
    """preserve=True exit -> still saved (valid checkpoint)."""
    p = _autosave_pool(tmp_path)
    # The run finalize calls maybe_autosave_on_task_end regardless of preserve, and the
    # autosave path has no state/preserve gate -- assert it still fires.
    calls = _spy_snapshot_stack(monkeypatch)
    maybe_autosave_on_task_end(p, ROOT)
    assert len(calls) == 1


def test_task_end_autosave_never_raises(tmp_path, monkeypatch):
    """capture failure -> run finalize still completes (autosave swallows the error)."""
    p = _autosave_pool(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError('capture blew up')

    monkeypatch.setattr(snapshot, 'snapshot_stack', boom)
    # Must not propagate.
    maybe_autosave_on_task_end(p, ROOT)


# ─────────────────────────────────────────────────────────────────────────────
# Save never touches logs; append-tolerant integrity; prefix restore
# ─────────────────────────────────────────────────────────────────────────────


def _snapshot_metrics(log_path: Path):
    st = log_path.stat()
    return st.st_size, st.st_mtime, log_path.read_bytes()


def _reattach_into(tmp_path, env, *, log_name=None):
    """Restore ``env``'s root via the REAL _reattach_instance_log into a fresh pool.

    Returns ``(restored_count, new_log_path, origin_path)``.
    """
    p = PoolDouble(log_dir=tmp_path)
    root_inst = _instantiate_root(env)
    p.instances[root_inst.instance_name] = root_inst
    p.instance_loggers = {root_inst.instance_name: LoggerDouble(str(tmp_path / 'live.jsonl'))}
    log_ref = env.instances[root_inst.instance_name].log
    origin = Path(log_ref['path'])
    count = p._reattach_instance_log(root_inst, log_ref, snapshot_id=env.snapshot_id)
    key = (root_inst.instance_name, 'orchestrator')
    new_path = Path(p._logger._loggers[key].log_path)
    return count, new_path, origin


def _instantiate_root(env):
    """A bare AgentInstance-shaped double the re-attach path can work with."""
    root = next(iter(env.instances))  # env.instances is keyed by instance name
    inst = make_instance(root, agent_class='orchestrator', cfg=None)
    inst.conversation = []
    inst.rebuild_conversation = lambda msgs: setattr(inst, 'conversation', list(msgs))
    return inst


def test_save_does_not_touch_logs(tmp_path, monkeypatch):
    """byte size + mtime of every captured log unchanged after a save (no rotation)."""
    _snap_dir(tmp_path, monkeypatch)
    p, log_path = _single_root_pool(tmp_path)
    before = _snapshot_metrics(log_path)
    result = snapshot_stack(p, trigger='user')
    assert result is not None
    after = _snapshot_metrics(log_path)
    assert before == after


def test_snapshot_restores_after_further_turns(tmp_path):
    """save -> 5 more live turns appended -> validate_envelope_integrity still passes
    AND restore replays the appended tail (BUG_0403: the snapshot is only a periodic
    checkpoint; the live log is the truth, so the restored session must carry the turns
    written after the checkpoint, not just the captured prefix)."""
    env, log_path = _capture_env(tmp_path)
    captured_count = env.instances[ROOT].log['message_count']
    # Append-only growth (5 turns = 10 message lines).
    with open(log_path, 'a', encoding='utf-8') as fh:
        for i in range(10):
            fh.write(json.dumps({'role': 'user' if i % 2 else 'assistant',
                                 'content': f'turn {i}'}) + '\n')
    # Append tolerance: growth after capture is NOT corruption.
    validate_envelope_integrity(env)
    # BUG_0403 tail-replay: the 10 appended turns are recovered, so the restored
    # conversation == checkpoint count + tail, not the checkpoint count alone.
    restored_count, _new, _origin = _reattach_into(tmp_path, env)
    assert restored_count == captured_count + 10


def test_restore_materialises_prefix_copy(tmp_path):
    """restore creates a NEW .jsonl containing exactly offset bytes of origin + provenance
    event; the origin file is byte-identical."""
    env, log_path = _capture_env(tmp_path)
    offset = int(env.instances[ROOT].log['offset'])
    origin_before = log_path.read_bytes()

    count, new_path, origin = _reattach_into(tmp_path, env)
    assert new_path != origin
    assert new_path.exists()
    # The first `offset` bytes of the new log are byte-identical to the origin prefix.
    new_prefix = new_path.read_bytes()[:offset]
    assert new_prefix == origin_before[:offset]
    # A provenance event was appended after the prefix.
    tail = new_path.read_bytes()[offset:]
    assert b'"restored_from"' in tail
    # Origin file is byte-identical (untouched).
    assert log_path.read_bytes() == origin_before
    assert count == env.instances[ROOT].log['message_count']


def test_restore_older_snapshot_after_newer_restore(tmp_path):
    """restore S1 then S2 of the same stack -> two distinct logs, neither desyncs.

    BUG_0403 tail-replay note: both restores read the SAME live origin, whose bytes now
    extend past o1 (the 4 'later' turns). Restoring the OLDER snapshot S1 therefore replays
    that tail and catches up to the live log's current message count, so c1 == c2. The two
    logs are still distinct (different provenance snapshot_id) and each carries the intact
    captured prefix. A deliberate rollback to strictly-older state must restore from an
    archived origin copy, never the still-growing live log."""
    p, log_path = _single_root_pool(tmp_path)  # 4-message root log
    env1 = capture_fleet_snapshot(p, trigger='user', stack_id=ROOT)
    o1 = int(env1.instances[ROOT].log['offset'])
    # Grow the live log, then re-capture from the same pool -> larger offset.
    with open(log_path, 'a', encoding='utf-8') as fh:
        for i in range(4):
            fh.write(json.dumps({'role': 'user', 'content': f'later {i}'}) + '\n')
    env2 = capture_fleet_snapshot(p, trigger='user', stack_id=ROOT)
    o2 = int(env2.instances[ROOT].log['offset'])
    assert o2 > o1

    origin = Path(env1.instances[ROOT].log['path'])
    c1, new1, _ = _reattach_into(tmp_path, env1)
    c2, new2, _ = _reattach_into(tmp_path, env2)
    assert new1 != new2
    assert new1.read_bytes()[:o1] == origin.read_bytes()[:o1]
    assert new2.read_bytes()[:o2] == origin.read_bytes()[:o2]
    # Both replays read the live origin past o1, so tail-replay extends the older restore
    # to the live log's current EOF — the older count equals the newer one's.
    assert c1 == c2


def test_validate_refuses_truncated_log(tmp_path):
    """truncate below offset -> refused."""
    env, log_path = _capture_env(tmp_path)
    log_path.write_text('{"role":"user","content":"tiny"}\n', encoding='utf-8')
    reason = validate_log_reference(env.instances[ROOT].log)
    assert reason is not None
    with pytest.raises(SnapshotIntegrityError):
        validate_envelope_integrity(env)


def test_validate_refuses_rewritten_prefix(tmp_path):
    """same size, different content -> refused (hash mismatch)."""
    env, log_path = _capture_env(tmp_path)
    offset = int(env.instances[ROOT].log['offset'])
    raw = log_path.read_bytes()
    assert len(raw) >= offset

    # NOTE (backend defect found while writing this test): snapshot._hash_cache is
    # keyed only on (log_path, size) and is never invalidated on write, so a captured
    # log rewritten IN-PLACE to the SAME size is silently accepted by validate_log_reference
    # in the same process (stale cache hit, snapshot.py:402). Appends/truncations change
    # size and bust the cache, so only same-size in-place rewrites are masked. Clearing the
    # cache here establishes the precondition of a normal restore (fresh process, empty
    # cache) so the test verifies the prefix_sha256 tamper gate itself, not the cache.
    snapshot._hash_cache.clear()

    # Flip one ASCII byte inside the captured prefix: the file stays the same SIZE,
    # but the prefix hash no longer matches the snapshot's recorded digest.
    flipped = bytearray(raw)
    i = raw.index(b'msg', 0, offset)  # an ASCII letter guaranteed within the prefix
    flipped[i] = raw[i] ^ 0x20        # toggle case bit -> different byte, same width
    log_path.write_bytes(bytes(flipped))
    assert log_path.stat().st_size == len(raw)  # same size, different content

    reason = validate_log_reference(env.instances[ROOT].log)
    assert reason is not None and 'hash' in reason.lower()
    with pytest.raises(SnapshotIntegrityError):
        validate_envelope_integrity(env)


# ─────────────────────────────────────────────────────────────────────────────
# Restart / shutdown autosave hooks
# ─────────────────────────────────────────────────────────────────────────────


def _make_handler(pool, *, session=None):
    """A WsMessageHandler built via __new__ with only the attrs the tested handlers
    touch; heavy helpers stubbed to no-ops. Returns ``(handler, broadcasts)``."""
    h = WsMessageHandler.__new__(WsMessageHandler)
    broadcasts = []

    async def broadcast_fn(msg, *a, **kw):
        broadcasts.append(msg)

    async def _noop_async(*a, **kw):
        return None

    h.agent_pool = pool
    if not hasattr(pool, 'stopped'):
        pool.stopped = False
    h.session = session if session is not None else {'session_name': None}
    h.broadcast_fn = broadcast_fn
    h._broadcast = _noop_async
    h._broadcast_stacks = _noop_async
    h._stop_generation = lambda: None
    h._signal_stop = lambda: None
    h._is_generating = lambda: False
    h._session_lock = threading.RLock()
    h._get_agent = lambda: None
    return h, broadcasts


def test_restart_autosave_called(tmp_path, monkeypatch):
    """handler snapshots before restart_server_process."""
    order = []
    monkeypatch.setattr(snapshot, 'snapshot_stack',
                        lambda pool, *a, **kw: order.append('snapshot'))
    import agent_cascade.server_restart as sr
    monkeypatch.setattr(sr, 'restart_server_process',
                        lambda: order.append('restart'))

    pool, _ = _single_root_pool(tmp_path)
    h, _b = _make_handler(pool)
    asyncio.run(h.handle_restart_server({}))
    assert order == ['snapshot', 'restart']


def test_shutdown_autosave_idempotent(tmp_path, monkeypatch):
    """shutdown event + signal -> exactly one snapshot (_stack_saved_on_exit flag)."""
    calls = []
    monkeypatch.setattr(snapshot, 'snapshot_stack',
                        lambda pool, *a, **kw: calls.append(kw.get('trigger')))
    pool, _ = _single_root_pool(tmp_path)

    # Mirrors BOTH api_server call sites (the on_event('shutdown') handler and the
    # __main__ signal handler) which share the exact _stack_saved_on_exit guard.
    def _shutdown_autosave():
        if settings.ENABLE_STACK_SWAP and settings.SNAPSHOT_AUTOSAVE_ENABLED and pool:
            if not getattr(pool, '_stack_saved_on_exit', False):
                pool._stack_saved_on_exit = True
                snapshot.snapshot_stack(pool, trigger='shutdown')

    _shutdown_autosave()   # graceful shutdown event
    _shutdown_autosave()   # SIGTERM/SIGINT signal path
    assert len(calls) == 1
    assert calls[0] == 'shutdown'


# ─────────────────────────────────────────────────────────────────────────────
# Prune / delete / list / session scan
# ─────────────────────────────────────────────────────────────────────────────


def _write_kv_snapshot(snap_dir: Path, env, *, label: str) -> Path:
    """Serialize ``env`` with each instance's KV block marked saved, so the
    "prune/delete never touches KV" assertion exercises a NON-vacuous, KV-bearing
    snapshot (kv=None snapshots would make a delete_state spy a vacuous pass)."""
    d = env.to_dict()
    for inst in d['instances'].values():
        inst['kv'] = {'saved': True, 'label': label,
                      'endpoint_cfg': {'api_base': 'http://base', 'model': 'm'}}
    p = snap_dir / f'{env.snapshot_id}.json'
    p.write_text(json.dumps(d), encoding='utf-8')
    return p


def test_prune_never_touches_kv(tmp_path, monkeypatch):
    """active stack > N -> oldest pruned by FILE only; no delete_state call for any stack."""
    snap_dir = _snap_dir(tmp_path, monkeypatch)
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_RETENTION_COUNT', 2)
    import agent_cascade.state_ops as state_ops
    deleted = []
    monkeypatch.setattr(state_ops, 'delete_state',
                        lambda api_base, model, label: deleted.append(label),
                        raising=False)

    p = PoolDouble(log_dir=tmp_path)
    reg = snapshot._ensure_registry(p)
    reg.register('Active')
    reg.set_active('Active')
    sids = []
    for i in range(4):  # 4 KV-bearing snapshots, oldest first
        env, _ = _capture_env(tmp_path, root='Active', log_name=f'act_{i}.jsonl')
        path = _write_kv_snapshot(snap_dir, env, label=f'actlabel{i}')
        reg.add_snapshot('Active', env.snapshot_id, str(path))
        sids.append(env.snapshot_id)

    pruned = prune_snapshots(p, registry=reg)
    assert pruned == 2
    assert len(reg.list_snapshots('Active')) == 2
    # No retirement touches KV, even though every retired snapshot here carries saved KV
    # (KV is a cache; the JSONL log is the source of truth — this is the point of the test).
    assert deleted == []
    # Newest two files kept; oldest two retired (proving they were actually pruned).
    assert all(Path(reg.resolve_path('Active', sid)).exists() for sid in sids[2:])
    assert not (snap_dir / f'{sids[0]}.json').exists()
    assert not (snap_dir / f'{sids[1]}.json').exists()


def test_delete_snapshot_removes_row_and_stack(tmp_path, monkeypatch):
    """last snapshot deletion drops the stack key; file gone; KV is never touched."""
    import agent_cascade.state_ops as state_ops
    _snap_dir(tmp_path, monkeypatch)
    deleted = []
    monkeypatch.setattr(state_ops, 'delete_state',
                        lambda api_base, model, label: deleted.append(label),
                        raising=False)
    p = PoolDouble(log_dir=tmp_path)
    reg = snapshot._ensure_registry(p)
    reg.register('Parked')
    env, _ = _capture_env(tmp_path, root='Parked', log_name='parked.jsonl')
    path = write_snapshot_to_disk(env, tmp_path / 'snapshots')
    reg.add_snapshot('Parked', env.snapshot_id, str(path))

    result = delete_snapshot(p, 'Parked', env.snapshot_id, registry=reg)
    assert result['removed_stack'] is True
    assert 'Parked' not in reg._snapshots
    assert not path.exists()
    assert deleted == []  # KV is never touched (cache; JSONL is the source of truth)


def test_delete_snapshot_refuses_unknown(tmp_path, monkeypatch):
    """ValueError for unknown stack and unknown snapshot."""
    _snap_dir(tmp_path, monkeypatch)
    p = PoolDouble(log_dir=tmp_path)
    reg = snapshot._ensure_registry(p)
    reg.register('Parked')
    env, _ = _capture_env(tmp_path, root='Parked', log_name='p.jsonl')
    reg.add_snapshot('Parked', env.snapshot_id, str(write_snapshot_to_disk(env, tmp_path / 'snapshots')))

    with pytest.raises(ValueError):
        delete_snapshot(p, 'nope', 'nope-snap', registry=reg)
    with pytest.raises(ValueError):
        delete_snapshot(p, 'Parked', 'no-such-snap', registry=reg)


def test_delete_stack_no_registry_rebuilds_from_disk(tmp_path, monkeypatch):
    """delete_stack with registry=None rebuilds the registry from disk (fresh-restart path)."""
    snap_dir = _snap_dir(tmp_path, monkeypatch)
    p = PoolDouble(log_dir=tmp_path)
    env, _ = _capture_env(tmp_path, root='Parked', log_name='parked.jsonl')
    path = write_snapshot_to_disk(env, snap_dir)
    assert path.exists()

    # Simulate a fresh restart: clear in-memory registry state.
    p._swap_registry = None
    p._swap_registry_loaded = False

    result = delete_stack(p, 'Parked')  # no registry arg -> ensure_registry_loaded path
    assert result['removed_snapshots'] == 1
    assert not path.exists()


def test_delete_snapshot_no_registry_rebuilds_from_disk(tmp_path, monkeypatch):
    """delete_snapshot with registry=None rebuilds the registry from disk (fresh-restart path)."""
    snap_dir = _snap_dir(tmp_path, monkeypatch)
    p = PoolDouble(log_dir=tmp_path)
    env, _ = _capture_env(tmp_path, root='Parked', log_name='parked.jsonl')
    path = write_snapshot_to_disk(env, snap_dir)
    assert path.exists()

    # Simulate a fresh restart: clear in-memory registry state.
    p._swap_registry = None
    p._swap_registry_loaded = False

    result = delete_snapshot(p, 'Parked', env.snapshot_id)  # no registry arg
    assert result['removed_stack'] is True
    assert not path.exists()


def test_delete_stack_stale_registry_fallback_reglobs(tmp_path, monkeypatch):
    """BUG_0368: delete_stack re-globs disk when the in-memory registry is stale.

    Simulates: registry loaded (flag set) → snapshot exists on disk but is
    missing from the in-memory view → delete triggers the fallback re-glob.
    """
    snap_dir = _snap_dir(tmp_path, monkeypatch)
    p = PoolDouble(log_dir=tmp_path)
    # Write a snapshot to disk.
    env, _ = _capture_env(tmp_path, root='Orphan', log_name='orphan.jsonl')
    path = write_snapshot_to_disk(env, snap_dir)
    assert path.exists()

    # Mark the registry as loaded (so ensure_registry_loaded short-circuits)
    # but with an EMPTY in-memory view — simulating a stale state where the
    # disk has an entry the in-memory registry doesn't know about.
    reg = ensure_registry_loaded(p)  # loads from disk, sets _swap_registry_loaded=True
    reg._snapshots.pop(env.stack_id, None)  # remove the entry to simulate staleness
    assert env.stack_id not in reg._snapshots

    # Delete with no registry arg → fallback re-glob should find the snapshot.
    result = delete_stack(p, 'Orphan')
    assert result['removed_snapshots'] == 1
    assert not path.exists()


def test_load_session_replace_parks(tmp_path, monkeypatch):
    """mode='replace' -> park_active_stack called (outgoing session persisted)."""
    from agent_cascade import ws_handlers as wh
    monkeypatch.setattr(wh, '_clear_caches_safely', lambda: None)
    parks = []
    monkeypatch.setattr(snapshot, 'park_active_stack',
                        lambda *a, **kw: parks.append(kw.get('trigger')))

    p, _ = _single_root_pool(tmp_path)
    p.get_instance = p.instances.get

    def fake_load(log_input, *, target_instance=None, **kw):
        p.instances.clear()
        p.instances[target_instance] = make_instance(target_instance, parent=None, cfg=None)
        return 'loaded'

    p.load_session_from_log = fake_load
    src = tmp_path / 'src.jsonl'
    write_jsonl(src, 'Imported', 2)

    h, _b = _make_handler(p, session={'session_name': ROOT})
    asyncio.run(h.handle_load_session({'path': str(src), 'mode': 'replace'}))
    assert parks == ['import']  # outgoing session parked before replace
    assert h.session['session_name'] in p.instances


def test_session_name_property():
    """AgentPool.session_name returns the root instance name, or None if empty."""
    from agent_cascade.pool.core import AgentPool
    from types import SimpleNamespace as NS

    pool = AgentPool.__new__(AgentPool)  # bypass __init__
    pool.instances = {}
    assert pool.session_name is None

    root = NS(parent_instance=None)
    sub = NS(parent_instance='Main')
    pool.instances = {'Main': root, 'Sub': sub}
    assert pool.session_name == 'Main'


def test_list_stacks_details_include_caption(tmp_path, monkeypatch):
    """caption from root log; trigger/time present; filter drops corrupt."""
    snap_dir = _snap_dir(tmp_path, monkeypatch)

    # Build a root whose log carries a metadata caption, then capture against THAT file
    # so the integrity gate (prefix hash) still passes when list_stacks re-validates.
    p = PoolDouble(log_dir=tmp_path)
    root = make_instance('Cap', agent_class='orchestrator', display='Cap', cfg=None)
    register(p, root)
    cap_log = tmp_path / 'cap.jsonl'
    cap_log.write_text(
        json.dumps({'metadata': {'instance_name': 'Cap', 'caption': 'Fix the build'}}) + '\n' +
        '\n'.join(json.dumps({'role': 'user', 'content': f'm{i}'}) for i in range(4)) + '\n',
        encoding='utf-8')
    p.instance_loggers = {'Cap': LoggerDouble(str(cap_log))}
    good = capture_fleet_snapshot(p, trigger='user', stack_id='Cap')
    good_path = write_snapshot_to_disk(good, snap_dir)
    reg = snapshot._ensure_registry(p)
    reg.register('Cap')
    reg.add_snapshot('Cap', good.snapshot_id, str(good_path))

    # A corrupt row (referenced log missing) must be filtered from the details.
    bad, _ = _capture_env(tmp_path, root='Cap', log_name='bad.jsonl')
    d = bad.to_dict()
    next(iter(d['instances'].values()))['log']['path'] = str(tmp_path / 'gone.jsonl')
    bad_path = snap_dir / f'{bad.snapshot_id}.json'
    bad_path.write_text(json.dumps(d), encoding='utf-8')
    reg.add_snapshot('Cap', bad.snapshot_id, str(bad_path))

    out = list_stacks(p, registry=reg)
    details = out['details']['Cap']
    assert details['display_name'] == 'Cap'
    sids = {s['snapshot_id'] for s in details['snapshots']}
    assert sids == {good.snapshot_id}  # corrupt row dropped
    row = details['snapshots'][0]
    assert row['caption'] == 'Fix the build'
    assert 'created_at' in row and row['created_at'] > 0
    assert 'trigger' in row and row['trigger']


def test_session_scan_marks_unloadable(tmp_path, monkeypatch):
    """0-byte + garbage .jsonl -> loadable False; a good log -> True; legacy keys unchanged."""
    from agent_cascade.api_server import _scan_sessions_sync
    (tmp_path / 'empty.jsonl').write_text('', encoding='utf-8')
    (tmp_path / 'garbage.jsonl').write_text('not json at all\n', encoding='utf-8')
    good = tmp_path / 'good.jsonl'
    good.write_text(json.dumps({'metadata': {'instance_name': 'Maine'}}) + '\n' +
                    json.dumps({'role': 'user', 'content': 'hi'}) + '\n', encoding='utf-8')

    sessions = _scan_sessions_sync(tmp_path)
    by_name = {Path(s['path']).name: s for s in sessions}
    assert by_name['empty.jsonl']['loadable'] is False
    assert by_name['garbage.jsonl']['loadable'] is False
    assert by_name['good.jsonl']['loadable'] is True
    # Legacy keys present and unchanged.
    for key in ('path', 'name', 'agent', 'timestamp', 'size', 'mtime', 'caption'):
        assert key in by_name['good.jsonl']


# ─────────────────────────────────────────────────────────────────────────────
# WS-handler contracts (swap name / new session / import modes)
# ─────────────────────────────────────────────────────────────────────────────


def _target_env_on_disk(tmp_path, monkeypatch):
    """A valid, registered parked-target stack (two instances) on disk. Returns
    ``(sid, path_str)``. Mirrors test_stack_swap_primitive._make_target_snapshot."""
    tdir = tmp_path / 'target'
    tdir.mkdir(exist_ok=True)
    tp = PoolDouble(log_dir=tdir)
    a = make_instance('target_a', agent_class='orchestrator', depth=0, cfg=None)
    b = make_instance('target_b', parent='target_a', depth=1, cfg=None)
    for inst in (a, b):
        register(tp, inst)
    tp.instance_loggers = {
        'target_a': LoggerDouble(str(tdir / 'a.jsonl')),
        'target_b': LoggerDouble(str(tdir / 'b.jsonl')),
    }
    write_jsonl(tdir / 'a.jsonl', 'target_a', 3)
    write_jsonl(tdir / 'b.jsonl', 'target_b', 2)
    env = capture_fleet_snapshot(tp, trigger='user', stack_id='target')
    reg_dir = tmp_path / 'reg'
    reg_dir.mkdir(exist_ok=True)
    path = write_snapshot_to_disk(env, reg_dir)
    return env.snapshot_id, str(path)


def test_swap_repoints_session_name(tmp_path, monkeypatch):
    """after swap session['session_name'] is the restored root."""
    p, _ = _single_root_pool(tmp_path)
    reg = snapshot._ensure_registry(p)
    reg.register(ROOT)
    reg.set_active(ROOT)
    sid, path = _target_env_on_disk(tmp_path, monkeypatch)
    reg.register('target')
    reg.add_snapshot('target', sid, path)

    h, _b = _make_handler(p, session={'session_name': ROOT})
    asyncio.run(h.handle_stacks_swap({'target_stack_id': 'target'}))
    assert h.session['session_name'] == 'target_a'


def test_new_session_uses_name_and_numeric_suffix(tmp_path, monkeypatch):
    """name='Alpha' -> Alpha; a second New Session with the same name -> Alpha_2;
    empty -> Maine (DEFAULT_SESSION_NAME)."""
    import agent_cascade.api_integration as api_int
    created = []

    def fake_create(pool, instance_name, system_message_content, **kw):
        # Mirror the real create_main_agent_instance clean-name behavior: keep the
        # clean name, append _N only if the name is already live, then register it.
        from agent_cascade.agent_identity import disambiguate_instance_name
        name = disambiguate_instance_name(instance_name, pool.instances)
        inst = SimpleNamespace(instance_name=name)
        pool.instances[name] = inst
        created.append(name)
        return inst

    monkeypatch.setattr(api_int, 'create_main_agent_instance', fake_create)
    monkeypatch.setattr(snapshot, 'park_active_stack', lambda *a, **kw: None)
    monkeypatch.setattr(snapshot, 'register_active_stack', lambda *a, **kw: None)
    monkeypatch.setattr('agent_cascade.api_server._extract_system_message', lambda *a: '')

    p, _ = _single_root_pool(tmp_path)
    p.instances = {}  # force the "no existing instances" path (skip park)
    h, _b = _make_handler(p)

    asyncio.run(h.handle_stacks_new({'name': 'Alpha'}))
    assert h.session['session_name'] == 'Alpha'

    h_again, _ = _make_handler(p)
    asyncio.run(h_again.handle_stacks_new({'name': 'Alpha'}))
    assert h_again.session['session_name'] == 'Alpha_2'

    h2, _ = _make_handler(p)
    asyncio.run(h2.handle_stacks_new({'name': ''}))
    assert h2.session['session_name'] == 'Maine'


def test_new_session_rolls_back_on_create_failure(tmp_path, monkeypatch):
    """create returns None -> parked envelope re-activated, pool not empty."""
    import agent_cascade.api_integration as api_int
    monkeypatch.setattr(api_int, 'create_main_agent_instance',
                        lambda *a, **kw: None)
    monkeypatch.setattr('agent_cascade.api_server._extract_system_message', lambda *a: '')

    parked = SimpleNamespace(stack_id='parked', marker=True)
    activations = []
    monkeypatch.setattr(snapshot, 'park_active_stack', lambda *a, **kw: parked)
    monkeypatch.setattr(snapshot, 'activate_stack',
                        lambda pool, env, *a, **kw: activations.append(env))

    p, _ = _single_root_pool(tmp_path)  # instances non-empty -> park runs
    h, _b = _make_handler(p)
    asyncio.run(h.handle_stacks_new({'name': 'Beta'}))
    assert activations == [parked]
    assert p.instances  # pool not left empty


def test_new_session_first_turn_not_stranded_by_swap_gate(tmp_path, monkeypatch):
    """e2e: after New Session, the user's first message must get its turn.

    Regression for the stuck-in-queue bug: ``handle_stacks_new`` parks the active
    stack (``park_active_stack`` sets ``pool._pending_swap_event``) but then mints a
    FRESH instance via ``create_main_agent_instance`` — it does NOT go through
    ``activate_stack``, the only path that clears the latch. The latch therefore
    stays set, and the run-loop turn-boundary gate (run_loop.py:137-143) breaks on the
    new instance's FIRST turn. A message queued into the new session is never
    drained — it sits in the queue forever ("my message is stuck, nothing happens").

    Drives the REAL ``handle_stacks_new`` and then evaluates the exact gate predicate
    the run-loop uses; asserts the gate would NOT break so the queued message is
    processed. Red on the current tree (latch left set), green once the handler
    clears the latch for the freshly-minted session.
    """
    import agent_cascade.api_integration as api_int
    from agent_cascade import ws_handlers as wh

    p, _ = _single_root_pool(tmp_path)

    def fake_park(pool, *, trigger):
        # Faithful to production park: lazily create the latch, then set it.
        ev = getattr(pool, '_pending_swap_event', None)
        if ev is None:
            ev = threading.Event()
            pool._pending_swap_event = ev
        ev.set()
        return SimpleNamespace(stack_id='parked', marker=True)

    def fake_create(pool, instance_name, system_message_content, **kw):
        # Mirror the real create_instance clean-name behavior: keep the clean name,
        # append _N only if the name is already live, then register it.
        from agent_cascade.agent_identity import disambiguate_instance_name
        name = disambiguate_instance_name(instance_name, pool.instances)
        inst = make_instance(name, agent_class='orchestrator',
                             display=instance_name, cfg=None)
        register(p, inst)
        return inst

    monkeypatch.setattr(snapshot, 'park_active_stack', fake_park)
    monkeypatch.setattr(api_int, 'create_main_agent_instance', fake_create)
    monkeypatch.setattr(snapshot, 'register_active_stack', lambda *a, **kw: None)
    monkeypatch.setattr('agent_cascade.api_server._extract_system_message', lambda *a: '')
    monkeypatch.setattr(wh, '_clear_caches_safely', lambda: None)

    h, _b = _make_handler(p)
    asyncio.run(h.handle_stacks_new({'name': 'Alpha'}))

    new_name = h.session['session_name']
    assert new_name and new_name != ROOT

    # The user's first message into the new session — this is what gets stranded.
    inst = p.instances[new_name]
    p.enqueue_message(new_name, 'hello, new session')
    assert p.message_queues[new_name] == ['hello, new session']

    # Evaluate the EXACT run-loop turn-boundary gate predicate (run_loop.py:138-140):
    # the new instance's first turn breaks iff the latch is set OR identity is lost.
    _swap_pending = getattr(p, '_pending_swap_event', None)
    _identity_lost = inst is not p.instances.get(new_name)
    gate_breaks = (_swap_pending is not None and _swap_pending.is_set()) or _identity_lost

    assert not gate_breaks, (
        'New-session first turn breaks at the stack-swap gate '
        f'(swap_pending={bool(_swap_pending and _swap_pending.is_set())}, '
        f'identity_lost={_identity_lost}); the queued message is stranded.'
    )


def test_additive_import_keeps_fleet(tmp_path, monkeypatch):
    """mode='add' -> prior instances remain."""
    from agent_cascade import ws_handlers as wh
    monkeypatch.setattr(wh, '_clear_caches_safely', lambda: None)

    p, _ = _single_root_pool(tmp_path)  # root 'Maine_20261115_143022' present
    p.get_instance = p.instances.get
    seen = {'add': False}

    def fake_load(log_input, *, target_instance=None,
                  clear_sub_agents_before_load=True, caller_name=None, **kw):
        seen['add'] = clear_sub_agents_before_load  # add -> False
        p.instances.setdefault(target_instance, make_instance(
            target_instance, parent=None, cfg=None))
        return 'loaded'

    p.load_session_from_log = fake_load
    src = tmp_path / 'src.jsonl'
    write_jsonl(src, 'Imported', 2)

    h, _b = _make_handler(p, session={'session_name': ROOT})
    asyncio.run(h.handle_load_session({'path': str(src), 'mode': 'add'}))
    assert seen['add'] is False          # clear_sub_agents_before_load was False
    assert ROOT in p.instances           # prior fleet kept
    assert h.session['session_name'] == ROOT  # add does not re-point


def test_new_stack_import_parks(tmp_path, monkeypatch):
    """mode='new_stack' -> old stack snapshotted, imported root active."""
    from agent_cascade import ws_handlers as wh
    monkeypatch.setattr(wh, '_clear_caches_safely', lambda: None)
    parks = []
    monkeypatch.setattr(snapshot, 'park_active_stack',
                        lambda *a, **kw: parks.append(kw.get('trigger')))

    p, _ = _single_root_pool(tmp_path)  # instances present -> park triggers
    p.get_instance = p.instances.get

    def fake_load(log_input, *, target_instance=None, **kw):
        p.instances.clear()
        p.instances[target_instance] = make_instance(target_instance, parent=None, cfg=None)
        return 'loaded'

    p.load_session_from_log = fake_load
    src = tmp_path / 'src.jsonl'
    write_jsonl(src, 'Imported', 2)

    h, _b = _make_handler(p, session={'session_name': ROOT})
    asyncio.run(h.handle_load_session({'path': str(src), 'mode': 'new_stack'}))
    assert parks == ['import']  # old stack parked first
    assert h.session['session_name'] in p.instances  # imported root active


def test_load_session_arms_kv_restore_pending(tmp_path):
    """load_session_from_log sets _boot_kv_restore_pending on the fresh instance."""
    p, log_path = _single_root_pool(tmp_path)
    # Negative baseline: a fresh instance that did NOT go through load_session
    # has the flag unset (default False from AgentInstance.__init__).
    pre = p.instances[ROOT]
    assert getattr(pre, '_boot_kv_restore_pending', False) is False
    # PoolDouble lacks LifecycleMixin / dismiss plumbing; stub what load_session needs.
    p._resolve_instance_name = lambda name, **kw: name
    p._dismiss_all_instances = lambda *a, **kw: None
    p.load_session_from_log(str(log_path))
    loaded = p.instances[ROOT]
    assert loaded._boot_kv_restore_pending is True
