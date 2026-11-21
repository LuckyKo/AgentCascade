"""E7 tests for the stable per-session KV save key ``<instance_name>_<kv_birth_ts>``.

Guard the locked plan reports/kv-save-label-plan-v2.md end to end:
  * label format + birth-TS format (E1/E2);
  * save -> load round-trip uses the SAME stamped key through the REAL helpers
    (only HTTP mocked) — proves save(key) is found by load(key) (E3/E4 funnel);
  * E6 cleanup keeps the live per-session key but purges a decoy old-stamp file;
  * E5 arm-guard accepts the stamped label and rejects the bare name;
  * D-CONTINUE rehydration carries kv_birth_ts VERBATIM (E1b) — the re-mint bug
    that would otherwise make the arm-guard silently refuse on every rehydrate;
  * LOAD-BEARING (E1b/E1c): the snapshot envelope is the single home. A D-CONTINUE
    rehydrate, an in-memory lifecycle_manager restart, AND a bare load_session_from_log
    import all CARRY kv_birth_ts (from the envelope) so the save key is unchanged; only
    a genuinely brand-new session (no prior envelope / force_fresh) MINTS a new one.
    The JSONL (and its filename timestamp) is NOT a home for kv_birth_ts.

A birth TS has second resolution, so "fresh vs carried" is asserted against an
obviously-old sentinel rather than between two same-second mints.
"""

import json
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent_cascade.state_ops as state_ops
from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.llm.schema import Message
from agent_cascade.pool.session_io import SessionIOMixin
from agent_cascade.lifecycle_manager import AgentLifecycleManager
from tests._snapshot_fleet_double import (
    CAPABLE_CFG, PoolDouble, ROOT, make_fleet, write_jsonl,
)

_TS_RE = re.compile(r'^\d{8}_\d{6}$')          # YYYYMMDD_HHMMSS (logger filename format)
_SENTINEL_TS = '20150304_050607'               # obviously-old birth TS for carry-vs-mint tests
WORKER, PARKED, PLAIN = 'coder_worker1', 'coder_parked', 'coder_plain'


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_cleanup_done_set():
    """_legacy_cleanup_done_instances is a module global mutated by cleanup; isolate per test."""
    state_ops._legacy_cleanup_done_instances.clear()
    yield
    state_ops._legacy_cleanup_done_instances.clear()


def _make_inst(name='coder'):
    """A REAL AgentInstance (so kv_birth_ts is minted by __post_init__) with a capable endpoint."""
    inst = AgentInstance(
        instance_name=name,
        agent_class='coder',
        conversation=[Message(role='system', content='sys')],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=0,
        parent_instance='Main',
    )
    inst._last_endpoint_config = dict(CAPABLE_CFG)
    return inst


def _resp200(json_data=None):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = json_data if json_data is not None else {}
    r.text = ''
    return r


# ─────────────────────────────────────────────────────────────────────────────
# E1 / E2 — birth-TS + label format
# ─────────────────────────────────────────────────────────────────────────────
def test_kv_birth_ts_format_and_label():
    from agent_cascade.state_ops import kv_save_label
    inst = _make_inst('build-kv-label')
    assert _TS_RE.match(inst.kv_birth_ts), inst.kv_birth_ts
    label = kv_save_label(inst)
    assert label == f'build-kv-label_{inst.kv_birth_ts}'
    assert label.endswith('_' + inst.kv_birth_ts)


# ─────────────────────────────────────────────────────────────────────────────
# E3 / E4 funnel — save -> load round-trip uses the SAME stamped key (real helpers)
# ─────────────────────────────────────────────────────────────────────────────
def test_save_load_roundtrip_label_matches():
    from agent_cascade.state_ops import kv_save_label
    inst = _make_inst('roundtrip')
    expected = kv_save_label(inst)
    posts = []

    def fake_post(url, **kw):
        posts.append((url, kw.get('json', {})))
        return _resp200()

    with patch.object(state_ops.httpx, 'post', side_effect=fake_post), \
         patch.object(state_ops.httpx, 'get', return_value=_resp200({'labels': []})):
        # SAVE side (save_instance_state -> save_state -> httpx.post /state/save).
        assert state_ops.save_instance_state(inst, set_label=True) is True
        assert inst._state_label == expected
        save_url, save_body = next(p for p in posts if p[0].endswith('/state/save'))
        assert save_body['label'] == expected

        # LOAD side, exactly as the 5 restore callers invoke it: label=kv_save_label(inst).
        posts.clear()
        assert state_ops.restore_instance_state(
            inst, held_endpoint_cfg=dict(CAPABLE_CFG), label=kv_save_label(inst)) is True
        load_url, load_body = next(p for p in posts if p[0].endswith('/state/load'))
        assert load_body['label'] == expected  # save(key) == load(key)


def test_restore_state_targets_stamped_key_when_label_passed():
    """The label kwarg overrides the consume-once _state_label — the safe-mode/manual seam."""
    from agent_cascade.state_ops import kv_save_label
    inst = _make_inst('explicit')
    inst._state_label = None  # simulate a system agent having consumed it
    captured = {}

    def fake_post(url, **kw):
        captured['label'] = kw.get('json', {}).get('label')
        return _resp200()

    with patch.object(state_ops.httpx, 'post', side_effect=fake_post):
        assert state_ops.restore_instance_state(
            inst, held_endpoint_cfg=dict(CAPABLE_CFG), label=kv_save_label(inst)) is True
    assert captured['label'] == kv_save_label(inst)


# ─────────────────────────────────────────────────────────────────────────────
# E6 — cleanup keeps the live per-session key, purges a decoy old-stamp file
# ─────────────────────────────────────────────────────────────────────────────
def test_cleanup_keeps_live_key_deletes_decoy():
    live = 'coder_20200101_000000'
    decoy = 'coder_20100101_000000'
    other = 'othername_20200101_000000'
    deleted = []

    def fake_get(url, **kw):
        return _resp200({'labels': [live, decoy, other]})

    with patch.object(state_ops.httpx, 'get', side_effect=fake_get), \
         patch.object(state_ops, '_delete_state',
                      side_effect=lambda base, model, lbl: deleted.append(lbl)):
        state_ops._cleanup_old_states('http://x', 'm-a', 'coder', keep_label=live)

    assert deleted == [decoy]   # decoy purged; the live key (and another instance's file) kept


# ─────────────────────────────────────────────────────────────────────────────
# E5 — arm-guard accepts the stamped label, rejects the bare name
# ─────────────────────────────────────────────────────────────────────────────
def _kv_block(inst, label):
    cfg = inst._last_endpoint_config
    return {
        'capable': True, 'saved': True, 'label': label,
        'endpoint_cfg': {'api_base': cfg['api_base'], 'model': cfg['model']},
        'session_digest_endpoint': [cfg['api_base'], cfg['model']],
    }


def test_arm_guard_accepts_stamped_rejects_bare():
    from agent_cascade.state_ops import kv_save_label
    inst = _make_inst('armed')
    inst._state_label = None
    pool = MagicMock()
    held = {'api_base': CAPABLE_CFG['api_base'], 'model': CAPABLE_CFG['model']}
    with patch.object(state_ops, 'resolve_held_endpoint', return_value=held):
        # bare name must be refused (would 404 / load the wrong session's KV)
        assert state_ops.verify_and_restore_instance_kv(
            pool, inst, _kv_block(inst, inst.instance_name)) is False
        assert inst._state_label is None
        # stamped key arms
        stamped = kv_save_label(inst)
        assert state_ops.verify_and_restore_instance_kv(
            pool, inst, _kv_block(inst, stamped)) is True
        assert inst._state_label == stamped


# ─────────────────────────────────────────────────────────────────────────────
# E1b — D-CONTINUE rehydration carries kv_birth_ts VERBATIM (no re-mint)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def pool(tmp_path):
    return PoolDouble(log_dir=tmp_path)


@pytest.fixture(autouse=True)
def _enable_swap(monkeypatch):
    """Restore is OFF by default; the rehydrate test needs it ON."""
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)


def test_dcontinue_rehydrate_carries_kv_birth_ts(pool, tmp_path, monkeypatch):
    from agent_cascade.snapshot import capture_fleet_snapshot, restore_fleet_snapshot
    make_fleet(pool, tmp_path, monkeypatch)          # no streaming -> no KV save during capture
    pool.instances[ROOT].kv_birth_ts = _SENTINEL_TS  # the prior session's stable birth TS
    for name, fn in [(ROOT, 'maine.jsonl'), (WORKER, 'worker.jsonl'),
                     (PARKED, 'parked.jsonl'), (PLAIN, 'plain.jsonl')]:
        write_jsonl(tmp_path / fn, name, 2)

    env = capture_fleet_snapshot(pool)
    assert env.instances[ROOT].clocks.get('kv_birth_ts') == _SENTINEL_TS  # captured verbatim

    with pool._pool_lock:
        pool.instances.clear()
    pool._execution.active_stack.clear()
    restore_fleet_snapshot(pool, env)

    rehydrated = pool.instances[ROOT]
    assert isinstance(rehydrated, AgentInstance)
    assert rehydrated.kv_birth_ts == _SENTINEL_TS   # carried, NOT re-minted by __post_init__


def test_find_birth_ts_for_log_reads_real_envelope(pool, tmp_path, monkeypatch):
    """The bare-log-import carrier must read the EXACT envelope schema a real capture writes
    (instances[*].log.path -> identity join, instances[*].clocks.kv_birth_ts -> value).
    Drives capture_fleet_snapshot + write_snapshot_to_disk, then the helper — not a hand-built dict.
    """
    from agent_cascade.snapshot import (capture_fleet_snapshot, write_snapshot_to_disk,
                                        find_birth_ts_for_log)
    make_fleet(pool, tmp_path, monkeypatch)
    pool.instances[ROOT].kv_birth_ts = _SENTINEL_TS   # the prior session's stable birth TS
    env = capture_fleet_snapshot(pool)               # ROOT.clocks.kv_birth_ts captured verbatim

    snap_dir = tmp_path / 'snaps'
    snap_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr('agent_cascade.snapshot.get_snapshot_dir', lambda p: snap_dir)
    write_snapshot_to_disk(env, snap_dir)            # REAL serialized envelope on disk

    root_log = str(tmp_path / 'maine.jsonl')          # the ROOT log path the envelope references
    assert find_birth_ts_for_log(pool, root_log) == _SENTINEL_TS   # real-schema round-trip
    assert find_birth_ts_for_log(pool, 'logs/no_such_zzz.jsonl') is None   # miss -> caller mints


# ─────────────────────────────────────────────────────────────────────────────
# E1c — LOAD-BEARING: resume-from-log and lifecycle restart CARRY the birth TS
# ─────────────────────────────────────────────────────────────────────────────
class _FakeInstanceLogger:
    def __init__(self, *a, **k):
        pass
    def rewrite_log_with_history(self, *a, **k):
        return None


@pytest.fixture
def _no_real_instance_logger(monkeypatch):
    """Swap AgentInstanceLogger for a no-op so load_session_from_log writes NO file to CWD
    (this pool double has log_dir='.'). Requested ONLY by the session_io load tests — the
    fleet-restore test needs the REAL logger (it uses log_dir=tmp_path and calls copy_session_file)."""
    import agent_cascade.logger.agent_instance_logger as ail
    monkeypatch.setattr(ail, 'AgentInstanceLogger', _FakeInstanceLogger)


class _SessionPool(SessionIOMixin):
    """Minimal pool double that runs the REAL load_session_from_log."""
    def __init__(self):
        self.instances = {}
        self._instances_version = 0
        self.instance_summaries = {}
        self._execution = SimpleNamespace(_state_lock=threading.RLock())
        self._logger = SimpleNamespace(
            _lock=threading.RLock(), _loggers={}, log_dir='.', workspace_dir=None)

    def _dismiss_all_instances(self, exclude=None):
        return 0

    def _resolve_instance_name(self, name):
        return str(name).strip() if name else 'RecoveredSession'


def _stamped_log(instance_name, current_log_path):
    lines = [
        json.dumps({'metadata': {'instance_name': instance_name,
                                 'current_log_path': current_log_path,
                                 'agent_class': 'Orchestrator'}}),
        json.dumps({'role': 'system', 'content': 'You are a test orchestrator.'}),
        json.dumps({'role': 'user', 'content': 'Do the thing.'}),
    ]
    return '\n'.join(lines)


def _point_snapshot_dir(monkeypatch, tmp_path):
    """Point snapshot.get_snapshot_dir at an isolated tmp dir so the carrier reads planted
    envelopes (and never scans the real workspace logs). _SessionPool has no operation_manager,
    so without this the helper would resolve to DEFAULT_WORKSPACE/logs."""
    snap_dir = tmp_path / 'snapshots'
    snap_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr('agent_cascade.snapshot.get_snapshot_dir', lambda pool: snap_dir)
    return snap_dir


def _write_prior_envelope(snap_dir, log_path, kv_birth_ts):
    """Minimal raw envelope JSON referencing ``log_path`` with clocks.kv_birth_ts.
    find_birth_ts_for_log only needs instances[*].log.path and clocks.kv_birth_ts."""
    (snap_dir / 'prior.json').write_text(json.dumps({
        'instances': {'Main': {'log': {'path': log_path},
                               'clocks': {'kv_birth_ts': kv_birth_ts}}},
    }), encoding='utf-8')


def test_load_session_from_log_carries_prior_snapshot_birth_ts(
        _no_real_instance_logger, monkeypatch, tmp_path):
    """LOAD-BEARING: a bare-log import CARRIES kv_birth_ts from the prior snapshot envelope
    (NOT re-mint, NOT the JSONL). The prior envelope references this log; the imported log's
    metadata current_log_path basename matches it, so the carried birth TS must survive (key
    unchanged) — otherwise the prior <name>_<oldTS> KV is orphaned and the session cold-starts."""
    log_path = 'logs/coder_Main_20150304_050607.jsonl'
    snap_dir = _point_snapshot_dir(monkeypatch, tmp_path)
    _write_prior_envelope(snap_dir, log_path, _SENTINEL_TS)
    pool = _SessionPool()
    pool.load_session_from_log(
        _stamped_log('Main', log_path),
        target_instance='Main', clear_sub_agents_before_load=False)
    assert pool.instances['Main'].kv_birth_ts == _SENTINEL_TS   # carried from the envelope


def test_load_session_from_log_mints_when_no_prior_snapshot(
        _no_real_instance_logger, monkeypatch, tmp_path):
    """Brand-new: no prior snapshot envelope references the log -> __post_init__ mints a fresh
    kv_birth_ts (the documented fallback)."""
    _point_snapshot_dir(monkeypatch, tmp_path)   # empty snapshot dir -> carrier finds nothing
    pool = _SessionPool()
    pool.load_session_from_log(
        _stamped_log('Main', 'logs/coder_Main.jsonl'),
        target_instance='Main', clear_sub_agents_before_load=False)
    ts = pool.instances['Main'].kv_birth_ts
    assert _TS_RE.match(ts), ts                              # a fresh, valid mint
    assert ts != _SENTINEL_TS


def _mgr_pool():
    pool = MagicMock()
    pool._resolve_instance_name = lambda n: n
    pool.instances = {}
    pool._update_child_relationship = lambda *a, **k: None
    return pool


def test_lifecycle_restart_carries_prior_birth_ts():
    pool = _mgr_pool()
    prior = _make_inst('resumer')
    prior.kv_birth_ts = _SENTINEL_TS
    prior.state = AgentState.RUNNING          # active prior -> force the create-new carry path
    pool.instances['resumer'] = prior
    mgr = AgentLifecycleManager(pool)

    inst, is_reuse, _ = mgr.find_or_create_instance('coder', 'resumer', 'Main', 0, force_fresh=False)
    assert is_reuse is False
    assert inst.kv_birth_ts == _SENTINEL_TS   # carried across restart, key unchanged


def test_lifecycle_force_fresh_mints_new_birth_ts():
    pool = _mgr_pool()
    prior = _make_inst('resumer')
    prior.kv_birth_ts = _SENTINEL_TS
    prior.state = AgentState.RUNNING
    pool.instances['resumer'] = prior
    mgr = AgentLifecycleManager(pool)

    inst, is_reuse, _ = mgr.find_or_create_instance('coder', 'resumer', 'Main', 0, force_fresh=True)
    assert is_reuse is False
    assert inst.kv_birth_ts != _SENTINEL_TS   # force_fresh = brand-new session -> minted
    assert _TS_RE.match(inst.kv_birth_ts)


def test_lifecycle_brand_new_mints_new_birth_ts():
    pool = _mgr_pool()
    mgr = AgentLifecycleManager(pool)
    inst, is_reuse, _ = mgr.find_or_create_instance('coder', 'brandnew', 'Main', 0, force_fresh=False)
    assert is_reuse is False
    assert _TS_RE.match(inst.kv_birth_ts)
    assert inst.kv_birth_ts != _SENTINEL_TS
