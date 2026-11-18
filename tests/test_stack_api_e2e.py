"""BUG_0323 S5: REST /api/stacks surfaces, snapshot pruning & flag-off gating.

Covers (plan §9.5, acceptance #10, test plan #5):
  * GET  /api/stacks                       lists active stack + every stack's snapshots
  * POST /api/stacks/{id}/snapshot         captures a non-destructive fleet snapshot
  * POST /api/stacks/swap                  parks active + activates target (400 on a target
                                           with no snapshot / missing body)
  * DELETE /api/stacks/{id}                deletes a parked stack; 409 on the ACTIVE stack (m4)
  * prune_snapshots                        keeps the newest SNAPSHOT_RETENTION_COUNT, NEVER
                                           touches the active stack's snapshot (m3), and calls
                                           state_ops.delete_state for saved-KV snapshots (m3)
  * ENABLE_STACK_SWAP off                  every /api/stacks endpoint returns 501 (M1)

Reuses the S3/S4 ``PoolDouble`` fleet doubles (``tests/_snapshot_fleet_double``) and the
active-source / registered-target harness from ``test_stack_swap_primitive``.
"""

import copy
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cascade import snapshot
from agent_cascade.api_server import build_stacks_router
from agent_cascade.snapshot import (
    _ensure_registry,
    capture_fleet_snapshot,
    load_snapshot_from_disk,
    write_snapshot_to_disk,
)
from tests._snapshot_fleet_double import (
    CAPABLE_CFG,
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
    """Swap is gated OFF by default; enable it for every test (off-path covered separately)."""
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', True)


@pytest.fixture
def pool(tmp_path):
    return PoolDouble(log_dir=tmp_path)


def _make_app(p):
    """Minimal app carrying ONLY the /api/stacks router (isolation, no create_app startup)."""
    app = FastAPI()
    app.include_router(build_stacks_router(p))
    return TestClient(app, raise_server_exceptions=False)


def _logger_for(path):
    return LoggerDouble(str(path))


def _write_source_logs(tmp_path):
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


def _make_target_snapshot(tmp_path, monkeypatch, *, stack_id=TARGET):
    """Independent parked-target snapshot on disk (its own log dir); (snapshot_id, path_str)."""
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
    env = capture_fleet_snapshot(tp, stack_id=stack_id, kv_saver=None)
    path = write_snapshot_to_disk(env, tmp_path / 'reg')
    return env.snapshot_id, str(path)


def _registered_target(pool, reg, tmp_path, monkeypatch):
    sid, path = _make_target_snapshot(tmp_path, monkeypatch)
    reg.register(TARGET)
    reg.add_snapshot(TARGET, sid, path)
    return sid, path


# ── REST surfaces ───────────────────────────────────────────────────────────

def test_stacks_rest_endpoints(tmp_path, monkeypatch):
    """GET list, POST snapshot, POST swap (happy) and swap-with-bad-target (400)."""
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_GRACE_SECONDS', 0)
    p, reg = _build_active_source(tmp_path, monkeypatch)
    _registered_target(p, reg, tmp_path, monkeypatch)
    client = _make_app(p)

    # GET — active stack + both stacks present, ROOT active.
    r = client.get('/api/stacks')
    assert r.status_code == 200
    body = r.json()
    assert body['active_stack_id'] == ROOT
    assert set(body['stacks']) == {ROOT, TARGET}

    # POST snapshot on the active stack — non-destructive, adds one snapshot id.
    before = client.get('/api/stacks').json()['stacks'][ROOT]
    r = client.post(f'/api/stacks/{ROOT}/snapshot')
    assert r.status_code == 200
    snap = r.json()
    assert snap['stack_id'] == ROOT and snap['snapshot_id']
    after = client.get('/api/stacks').json()['stacks'][ROOT]
    assert len(after) == len(before) + 1
    assert snap['snapshot_id'] in after
    # Non-destructive: the live fleet is untouched.
    assert set(p.instances) == {ROOT, WORKER, PARKED, PLAIN}

    # POST swap with a never-registered target -> 400 (ValueError, active untouched).
    r = client.post('/api/stacks/swap', json={'target_stack_id': 'nope'})
    assert r.status_code == 400
    assert reg.active_stack_id == ROOT

    # POST swap to the registered target -> 200, active re-points to TARGET.
    r = client.post('/api/stacks/swap', json={'target_stack_id': TARGET})
    assert r.status_code == 200
    assert r.json()['active_stack_id'] == TARGET
    assert reg.active_stack_id == TARGET


def test_swap_missing_target_stack_id_rejected(tmp_path, monkeypatch):
    """POST /api/stacks/swap without a usable target_stack_id -> 400 before any parking."""
    p, reg = _build_active_source(tmp_path, monkeypatch)
    client = _make_app(p)
    r = client.post('/api/stacks/swap', json={})
    assert r.status_code == 400
    assert reg.active_stack_id == ROOT  # nothing happened


def test_delete_active_stack_rejected(tmp_path, monkeypatch):
    """DELETE the ACTIVE stack -> 409 (m4); DELETE a parked stack -> 200 and files vanish."""
    p, reg = _build_active_source(tmp_path, monkeypatch)
    sid, path = _registered_target(p, reg, tmp_path, monkeypatch)
    client = _make_app(p)

    # Active stack delete is refused.
    r = client.delete(f'/api/stacks/{ROOT}')
    assert r.status_code == 409
    assert ROOT in reg._snapshots  # registry untouched

    # Parked stack delete succeeds and removes the on-disk snapshot file.
    assert os.path.exists(path)
    r = client.delete(f'/api/stacks/{TARGET}')
    assert r.status_code == 200
    assert r.json()['removed_snapshots'] == 1
    assert TARGET not in reg._snapshots
    assert not os.path.exists(path)  # file actually deleted


# ── Pruning ─────────────────────────────────────────────────────────────────

def _saved_kv(label):
    """A saved-KV block matching what capture stores (the shape _delete_snapshot_kv reads)."""
    return {
        'capable': True, 'saved': True, 'reason': None, 'label': label,
        'session_digest': 'd', 'session_digest_endpoint': ('http://x/v1', 'm'),
        'endpoint_cfg': {'api_base': CAPABLE_CFG['api_base'], 'model': CAPABLE_CFG['model']},
    }


def _make_kv_snapshot_on_disk(tmp_path, monkeypatch, *, stack_id, unsaved_names=()):
    """Write a valid snapshot whose instances carry saved KV; return (snapshot_id, path_str).

    Captures a valid (kv=None) envelope, then stamps saved-KV onto each instance so the
    KV-delete seam is exercised deterministically without depending on capture's
    save-capability gating. ``unsaved_names`` marks those instances' KV as ``saved:false``
    (so the label is NOT retained and IS eligible for deletion when pruned).
    """
    sid, path = _make_target_snapshot(tmp_path, monkeypatch, stack_id=stack_id)
    env = load_snapshot_from_disk(snapshot.Path(path))
    for name, inst in env.instances.items():
        kv = copy.deepcopy(_saved_kv(name))
        if name in unsaved_names:
            kv['saved'] = False
        inst.kv = kv
    new_path = write_snapshot_to_disk(env, tmp_path / 'reg')
    return sid, str(new_path)


def test_prune_never_touches_active_stack(tmp_path, monkeypatch):
    """Keeps newest N of a parked stack but NEVER prunes the active stack's snapshots (m3)."""
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_RETENTION_COUNT', 2)
    p, reg = _build_active_source(tmp_path, monkeypatch)  # active = ROOT
    reg.register(TARGET)

    # 4 snapshots on the ACTIVE stack (ROOT) -> all survive regardless of retention.
    active_paths = []
    for _ in range(4):
        sid, path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=ROOT)
        reg.add_snapshot(ROOT, sid, path)
        active_paths.append(path)

    # 4 snapshots on a PARKED stack (TARGET) -> newest 2 kept, oldest 2 pruned.
    parked_paths = []
    for _ in range(4):
        sid, path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=TARGET)
        reg.add_snapshot(TARGET, sid, path)
        parked_paths.append(path)

    pruned = snapshot.prune_snapshots(p, registry=reg)

    # Active stack: fully intact.
    assert len(reg.list_snapshots(ROOT)) == 4
    for path in active_paths:
        assert os.path.exists(path)
    # Parked stack: trimmed to the newest 2 (write order -> last two paths).
    assert len(reg.list_snapshots(TARGET)) == 2
    for path in parked_paths[:2]:
        assert not os.path.exists(path)   # oldest deleted
    for path in parked_paths[-2:]:
        assert os.path.exists(path)       # newest kept
    assert pruned == 2


def test_prune_keeps_kv_label_shared_by_surviving_snapshot(tmp_path, monkeypatch):
    """Major #1 (BUG_0323 S5): pruning a stack's OLDEST snapshot must NOT delete the KV
    label a KEPT (newer) snapshot of the SAME stack still references. KV files are keyed
    per instance_name (state_ops: autoloader overwrites the same file on each save), so
    deleting the shared file on the oldest prune silently drops the newer restore to a
    message-only cold start."""
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_RETENTION_COUNT', 1)
    p, reg = _build_active_source(tmp_path, monkeypatch)  # active = ROOT
    reg.register(TARGET)

    calls = []
    monkeypatch.setattr('agent_cascade.state_ops.delete_state',
                        lambda api_base, model, label: calls.append((api_base, model, label)))

    # Both snapshots reference the SAME per-instance labels (target_a, target_b).
    old_sid, old_path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=TARGET)  # oldest -> pruned
    new_sid, new_path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=TARGET)  # newest -> kept
    reg.add_snapshot(TARGET, old_sid, old_path)
    reg.add_snapshot(TARGET, new_sid, new_path)

    pruned = snapshot.prune_snapshots(p, registry=reg)
    assert pruned == 1
    assert os.path.exists(new_path) and not os.path.exists(old_path)  # old file gone, new kept

    # The kept snapshot still claims both labels -> delete_state must NOT be called for either.
    assert calls == []


def test_prune_calls_delete_state_only_for_unreferenced_labels(tmp_path, monkeypatch):
    """A pruned snapshot's KV label IS deleted (m3) when no KEPT sibling still references it —
    i.e. the kept snapshot does not carry saved KV for that instance."""
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_RETENTION_COUNT', 1)
    p, reg = _build_active_source(tmp_path, monkeypatch)  # active = ROOT
    reg.register(TARGET)

    calls = []
    monkeypatch.setattr('agent_cascade.state_ops.delete_state',
                        lambda api_base, model, label: calls.append((api_base, model, label)))

    # Old (pruned) references target_a + target_b; kept (new) only saves target_a
    # (target_b unsaved) -> target_b is unreferenced and eligible for delete.
    old_sid, old_path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=TARGET)
    new_sid, new_path = _make_kv_snapshot_on_disk(
        tmp_path, monkeypatch, stack_id=TARGET, unsaved_names=('target_b',))
    reg.add_snapshot(TARGET, old_sid, old_path)
    reg.add_snapshot(TARGET, new_sid, new_path)

    pruned = snapshot.prune_snapshots(p, registry=reg)
    assert pruned == 1
    assert os.path.exists(new_path) and not os.path.exists(old_path)

    # Only target_b is deleted; target_a is retained (still saved in the kept snapshot).
    assert calls == [(CAPABLE_CFG['api_base'], CAPABLE_CFG['model'], 'target_b')]


def test_prune_does_not_delete_kv_shared_by_another_parked_stack(tmp_path, monkeypatch):
    """S5 cross-stack (BUG_0323): KV files are keyed GLOBALLY per
    (model, api_base, instance_name) with no stack dimension, so a parked stack's
    instance name can collide with another parked stack's (shared subagent names).
    Pruning the OLDER stack's oldest snapshot must NOT delete_state a label that a
    kept snapshot of a DIFFERENT parked stack still restores from. The pre-fix
    same-stack-only keep-set had no visibility into the other stack and destroyed it."""
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_RETENTION_COUNT', 1)
    p, reg = _build_active_source(tmp_path, monkeypatch)  # active = ROOT
    reg.register(TARGET)
    reg.register(PARKED)

    calls = []
    monkeypatch.setattr('agent_cascade.state_ops.delete_state',
                        lambda api_base, model, label: calls.append((api_base, model, label)))

    # Parked stack TARGET: old (saved both labels -> pruned) + new (saved target_a only,
    # target_b unsaved). So TARGET's OWN kept set references only target_a.
    old_sid, old_path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=TARGET)
    new_sid, new_path = _make_kv_snapshot_on_disk(
        tmp_path, monkeypatch, stack_id=TARGET, unsaved_names=('target_b',))
    reg.add_snapshot(TARGET, old_sid, old_path)
    reg.add_snapshot(TARGET, new_sid, new_path)

    # Parked stack PARKED: one kept snapshot that STILL saves target_b (the label
    # TARGET's own keeper no longer claims). target_b is referenced cross-stack.
    b_sid, b_path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=PARKED)
    reg.add_snapshot(PARKED, b_sid, b_path)

    pruned = snapshot.prune_snapshots(p, registry=reg)
    assert pruned == 1
    assert os.path.exists(new_path) and not os.path.exists(old_path)  # TARGET trimmed
    assert os.path.exists(b_path)                                    # PARKED untouched

    # target_b is absent from TARGET's own kept set but present in PARKED's -> the
    # global keep-set protects it. Pre-fix (same-stack only) this was
    # [(CAPABLE_CFG['api_base'], CAPABLE_CFG['model'], 'target_b')].
    assert calls == []


def test_delete_stack_does_not_delete_kv_shared_by_another_parked_stack(tmp_path, monkeypatch):
    """S5 cross-stack (BUG_0323): deleting parked stack A must NOT delete_state a label
    that a surviving stack B still restores from — KV files are keyed globally per
    (model, api_base, instance_name) with no stack dimension. The shared label is
    preserved; only labels genuinely unreferenced by any other stack are deleted
    (over-suppression guard). Pre-fix delete_stack passed no keep-set at all."""
    p, reg = _build_active_source(tmp_path, monkeypatch)  # active = ROOT
    reg.register(TARGET)
    reg.register(PARKED)

    calls = []
    monkeypatch.setattr('agent_cascade.state_ops.delete_state',
                        lambda api_base, model, label: calls.append((api_base, model, label)))

    # Stack to delete (TARGET) saves BOTH shared labels.
    a_sid, a_path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=TARGET)
    reg.add_snapshot(TARGET, a_sid, a_path)

    # Surviving stack (PARKED) saves target_a only (target_b unsaved): target_a is the
    # shared label (must be preserved), target_b is unreferenced elsewhere (must delete).
    b_sid, b_path = _make_kv_snapshot_on_disk(
        tmp_path, monkeypatch, stack_id=PARKED, unsaved_names=('target_b',))
    reg.add_snapshot(PARKED, b_sid, b_path)

    result = snapshot.delete_stack(p, TARGET, registry=reg)
    assert result == {'stack_id': TARGET, 'removed_snapshots': 1}
    assert TARGET not in reg._snapshots
    assert not os.path.exists(a_path)
    assert os.path.exists(b_path)

    # target_a is still claimed by PARKED -> preserved (cross-stack safety).
    # target_b is unreferenced by any surviving stack -> deleted (over-suppression guard).
    # Pre-fix this was [(..., 'target_a'), (..., 'target_b')]: the shared label was wiped.
    assert calls == [(CAPABLE_CFG['api_base'], CAPABLE_CFG['model'], 'target_b')]


# ── Flag-off gating (M1) ────────────────────────────────────────────────────

def test_flag_off_all_endpoints_501(tmp_path, monkeypatch):
    """With ENABLE_STACK_SWAP off, every /api/stacks endpoint returns 501 and the pool is untouched."""
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', False)
    p, reg = _build_active_source(tmp_path, monkeypatch)
    client = _make_app(p)
    assert client.get('/api/stacks').status_code == 501
    assert client.post('/api/stacks/swap', json={'target_stack_id': 'x'}).status_code == 501
    assert client.post(f'/api/stacks/{ROOT}/snapshot').status_code == 501
    assert client.delete(f'/api/stacks/{ROOT}').status_code == 501
    # Off-path: pool untouched, active stack unchanged.
    assert set(p.instances) == {ROOT, WORKER, PARKED, PLAIN}
    assert reg.active_stack_id == ROOT
