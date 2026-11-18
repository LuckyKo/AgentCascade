"""BUG_0323 S5: REST /api/stacks surfaces, snapshot pruning & flag-off gating.

Covers (plan §9.5, acceptance #10, test plan #5):
  * GET  /api/stacks                       lists active stack + every stack's snapshots
  * POST /api/stacks/{id}/snapshot         captures a non-destructive fleet snapshot
  * POST /api/stacks/swap                  parks active + activates target (400 on a target
                                           with no snapshot / missing body)
  * DELETE /api/stacks/{id}                deletes a parked stack; 409 on the ACTIVE stack (m4)
  * prune_snapshots                        keeps the newest SNAPSHOT_RETENTION_COUNT per stack —
                                           INCLUDING the active stack — and NEVER touches KV
                                           (KV is a cache; the JSONL log is the source of truth)
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
    # A live-session pool has already run ensure_registry_loaded (its registry is built
    # and the active pointer chosen here, in-memory). Marking it loaded stops a later
    # list_stacks()/ensure_registry_loaded from re-reading the persisted active pointer
    # (which the helper never wrote) and resetting it to None. Faithful to a warm pool.
    p._swap_registry_loaded = True
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
    """A saved-KV block matching what capture stores (KV-bearing shape for the never-touched tests)."""
    return {
        'capable': True, 'saved': True, 'reason': None, 'label': label,
        'session_digest': 'd', 'session_digest_endpoint': ('http://x/v1', 'm'),
        'endpoint_cfg': {'api_base': CAPABLE_CFG['api_base'], 'model': CAPABLE_CFG['model']},
    }


def _make_kv_snapshot_on_disk(tmp_path, monkeypatch, *, stack_id, unsaved_names=()):
    """Write a valid snapshot whose instances carry saved KV; return (snapshot_id, path_str).

    Captures a valid (kv=None) envelope, then stamps saved-KV onto each instance so the
    "KV is never touched on prune/delete" invariant is exercised on a NON-vacuous,
    KV-bearing snapshot. ``unsaved_names`` marks those instances' KV as ``saved:false``
    (kept from the prior keep-set tests; harmless now that no KV is ever deleted).
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


def test_prune_trims_active_stack_to_retention(tmp_path, monkeypatch):
    """Prune trims EVERY stack — including the ACTIVE one — to the newest N. The active
    stack's *files* age out too; KV is never touched anywhere (KV is a cache, the JSONL
    log is the source of truth). Here we verify the shared prune path does not exempt the
    active stack's files."""
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_RETENTION_COUNT', 2)
    p, reg = _build_active_source(tmp_path, monkeypatch)  # active = ROOT
    reg.register(TARGET)

    # 4 snapshots on the ACTIVE stack (ROOT) -> newest 2 kept, oldest 2 pruned.
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

    # Both stacks now trimmed to the newest 2 (write order -> last two paths kept).
    assert len(reg.list_snapshots(ROOT)) == 2
    assert len(reg.list_snapshots(TARGET)) == 2
    for path in active_paths[-2:] + parked_paths[-2:]:
        assert os.path.exists(path)          # newest kept
    for path in active_paths[:2] + parked_paths[:2]:
        assert not os.path.exists(path)      # oldest retired
    assert pruned == 4                        # 2 active + 2 parked


def test_prune_never_deletes_kv_claimed_by_surviving_snapshot(tmp_path, monkeypatch):
    """Pruning a stack's oldest snapshot never calls state_ops.delete_state.

    KV is a cache whose source of truth is the JSONL log; orphaned KV files are
    acceptable, so the stack code must not orchestrate KV-file lifecycle at all —
    regardless of whether a newer (kept) snapshot still references the same labels."""
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_RETENTION_COUNT', 1)
    p, reg = _build_active_source(tmp_path, monkeypatch)  # active = ROOT
    reg.register(TARGET)

    calls = []
    monkeypatch.setattr('agent_cascade.state_ops.delete_state',
                        lambda api_base, model, label: calls.append((api_base, model, label)),
                        raising=False)

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


def test_prune_never_deletes_any_kv_label(tmp_path, monkeypatch):
    """Pruning a snapshot never calls state_ops.delete_state, even when the pruned
    snapshot carries a label the kept snapshot does not (the pre-change "over-suppression
    guard" would have deleted it). KV is never touched — orphaned files are acceptable."""
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_RETENTION_COUNT', 1)
    p, reg = _build_active_source(tmp_path, monkeypatch)  # active = ROOT
    reg.register(TARGET)

    calls = []
    monkeypatch.setattr('agent_cascade.state_ops.delete_state',
                        lambda api_base, model, label: calls.append((api_base, model, label)),
                        raising=False)

    # Layout: pruned snapshot saves target_a + target_b; the kept one saves target_a only
    # (target_b unsaved). Under the new invariant nothing is deleted — this layout exists to
    # prove even an unreferenced label is never touched.
    old_sid, old_path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=TARGET)
    new_sid, new_path = _make_kv_snapshot_on_disk(
        tmp_path, monkeypatch, stack_id=TARGET, unsaved_names=('target_b',))
    reg.add_snapshot(TARGET, old_sid, old_path)
    reg.add_snapshot(TARGET, new_sid, new_path)

    pruned = snapshot.prune_snapshots(p, registry=reg)
    assert pruned == 1
    assert os.path.exists(new_path) and not os.path.exists(old_path)

    # Nothing is deleted under any layout — the stack code never orchestrates KV lifecycle.
    assert calls == []


def test_prune_never_deletes_kv_across_stacks(tmp_path, monkeypatch):
    """Pruning one parked stack's oldest snapshot never calls state_ops.delete_state —
    there is no cross-stack keep-set anymore and no KV is deleted at all. KV is a cache;
    orphaned files are acceptable (the JSONL log is the source of truth)."""
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_RETENTION_COUNT', 1)
    p, reg = _build_active_source(tmp_path, monkeypatch)  # active = ROOT
    reg.register(TARGET)
    reg.register(PARKED)

    calls = []
    monkeypatch.setattr('agent_cascade.state_ops.delete_state',
                        lambda api_base, model, label: calls.append((api_base, model, label)),
                        raising=False)

    # Parked stack TARGET: old (saved both labels -> pruned) + new (saved target_a only,
    # target_b unsaved). So the kept TARGET snapshot references only target_a.
    old_sid, old_path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=TARGET)
    new_sid, new_path = _make_kv_snapshot_on_disk(
        tmp_path, monkeypatch, stack_id=TARGET, unsaved_names=('target_b',))
    reg.add_snapshot(TARGET, old_sid, old_path)
    reg.add_snapshot(TARGET, new_sid, new_path)

    # Parked stack PARKED: one surviving snapshot that STILL saves target_b (the label
    # TARGET's kept snapshot no longer claims). target_b is referenced cross-stack.
    b_sid, b_path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=PARKED)
    reg.add_snapshot(PARKED, b_sid, b_path)

    pruned = snapshot.prune_snapshots(p, registry=reg)
    assert pruned == 1
    assert os.path.exists(new_path) and not os.path.exists(old_path)  # TARGET trimmed
    assert os.path.exists(b_path)                                    # PARKED untouched

    # target_b is absent from TARGET's own kept set but present in PARKED's. There is no
    # keep-set anymore and KV is never deleted — this is exactly the old cross-stack-deletion
    # case, now inert. Pre-fix (same-stack-only keep-set) this was
    # [(CAPABLE_CFG['api_base'], CAPABLE_CFG['model'], 'target_b')].
    assert calls == []


def test_delete_stack_never_deletes_any_kv(tmp_path, monkeypatch):
    """Deleting a parked stack never calls state_ops.delete_state — delete_stack retires
    only the envelope files. KV is never touched regardless of any shared-label layout
    (KV is a cache; orphaned files are acceptable)."""
    p, reg = _build_active_source(tmp_path, monkeypatch)  # active = ROOT
    reg.register(TARGET)
    reg.register(PARKED)

    calls = []
    monkeypatch.setattr('agent_cascade.state_ops.delete_state',
                        lambda api_base, model, label: calls.append((api_base, model, label)),
                        raising=False)

    # Stack to delete (TARGET) saves BOTH shared labels.
    a_sid, a_path = _make_kv_snapshot_on_disk(tmp_path, monkeypatch, stack_id=TARGET)
    reg.add_snapshot(TARGET, a_sid, a_path)

    # Layout: deleted stack (TARGET) saves both shared labels; surviving stack (PARKED) saves
    # target_a only (target_b unsaved). Under the new invariant neither label is ever deleted;
    # the layout exists only to prove shared/unreferenced KV is never touched.
    b_sid, b_path = _make_kv_snapshot_on_disk(
        tmp_path, monkeypatch, stack_id=PARKED, unsaved_names=('target_b',))
    reg.add_snapshot(PARKED, b_sid, b_path)

    result = snapshot.delete_stack(p, TARGET, registry=reg)
    assert result == {'stack_id': TARGET, 'removed_snapshots': 1}
    assert TARGET not in reg._snapshots
    assert not os.path.exists(a_path)
    assert os.path.exists(b_path)

    # Nothing is deleted: delete_stack only removes snapshot files, never KV.
    assert calls == []


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

# ── BUG_0345 S6 additions (plan §13.2) ──────────────────────────────────────

def _register_target_snapshots(pool, reg, tmp_path, monkeypatch, *, count):
    """Register TARGET with ``count`` distinct on-disk snapshots. Returns a list of
    (snapshot_id, path_str) in write order (oldest first)."""
    reg.register(TARGET)
    out = []
    for _ in range(count):
        sid, path = _make_target_snapshot(tmp_path, monkeypatch)
        reg.add_snapshot(TARGET, sid, path)
        out.append((sid, path))
    return out


def test_stacks_list_carries_details_and_enabled(tmp_path, monkeypatch):
    """GET /api/stacks exposes ``details`` (per-stack display/count/instances) and
    ``enabled`` (the feature gate) so the UI renders without a second round-trip."""
    p, reg = _build_active_source(tmp_path, monkeypatch)
    sid, _ = _registered_target(p, reg, tmp_path, monkeypatch)
    client = _make_app(p)

    body = client.get('/api/stacks').json()
    assert body['enabled'] is True                       # gate on for this suite
    assert 'details' in body, "GET /api/stacks must carry a 'details' map"
    assert ROOT in body['details'] and TARGET in body['details']

    target_details = body['details'][TARGET]
    for field in ('display_name', 'instance_count', 'latest_snapshot_at', 'snapshots'):
        assert field in target_details, f"target details missing {field!r}"
    sids = [s['snapshot_id'] for s in target_details['snapshots']]
    assert sid in sids, 'listed snapshot id must match the registered snapshot'
    for entry in target_details['snapshots']:
        for field in ('snapshot_id', 'created_at', 'trigger', 'caption'):
            assert field in entry, f"snapshot detail missing {field!r}"


def test_swap_by_target_snapshot_id(tmp_path, monkeypatch):
    """POST /api/stacks/swap honours ``target_snapshot_id`` to pick a SPECIFIC snapshot
    of a multi-snapshot stack, and rejects an unknown snapshot id WITHOUT parking."""
    monkeypatch.setattr('agent_cascade.settings.SNAPSHOT_GRACE_SECONDS', 0)
    p, reg = _build_active_source(tmp_path, monkeypatch)
    older, newer = _register_target_snapshots(pool=p, reg=reg, tmp_path=tmp_path,
                                               monkeypatch=monkeypatch, count=2)
    client = _make_app(p)

    # Negative FIRST (TARGET still parked): a never-registered snapshot id is rejected
    # (ValueError -> 400) and the active stack is untouched (pre-validation precedes
    # parking, M2 all-or-nothing). Must precede the real swap: once TARGET is active the
    # self-swap guard short-circuits to a 200 no-op regardless of snapshot id.
    bad = client.post('/api/stacks/swap',
                      json={'target_stack_id': TARGET, 'target_snapshot_id': 'no-such-sid'})
    assert bad.status_code >= 400, bad.text
    assert reg.active_stack_id == ROOT          # parked, not activated

    # Positive: swap to the OLDER snapshot explicitly by id -> active re-points to TARGET.
    r = client.post('/api/stacks/swap',
                    json={'target_stack_id': TARGET, 'target_snapshot_id': older[0]})
    assert r.status_code == 200, r.text
    assert r.json()['active_stack_id'] == TARGET
    assert reg.active_stack_id == TARGET


def test_delete_snapshot_by_id(tmp_path, monkeypatch):
    """DELETE /api/stacks/{stack}/snapshots/{sid} removes exactly one snapshot; deleting
    the last one drops the stack; an unknown snapshot id returns 404."""
    p, reg = _build_active_source(tmp_path, monkeypatch)
    older, newer = _register_target_snapshots(pool=p, reg=reg, tmp_path=tmp_path,
                                               monkeypatch=monkeypatch, count=2)
    client = _make_app(p)

    assert len(reg.list_snapshots(TARGET)) == 2
    assert os.path.exists(older[1]) and os.path.exists(newer[1])

    # Delete the older snapshot only; the newer one survives.
    r = client.delete(f'/api/stacks/{TARGET}/snapshots/{older[0]}')
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['snapshot_id'] == older[0] and body['removed_stack'] is False
    assert not os.path.exists(older[1])                 # file gone
    assert os.path.exists(newer[1])                     # sibling kept
    assert len(reg.list_snapshots(TARGET)) == 1

    # Delete the last snapshot -> the stack key is dropped entirely.
    r = client.delete(f'/api/stacks/{TARGET}/snapshots/{newer[0]}')
    assert r.status_code == 200, r.text
    assert r.json()['removed_stack'] is True
    assert TARGET not in reg._snapshots
    assert not os.path.exists(newer[1])

    # Unknown snapshot id on a surviving stack -> 404 (ValueError mapped in the router).
    assert ROOT in reg._snapshots
    r = client.delete(f'/api/stacks/{ROOT}/snapshots/no-such-sid')
    assert r.status_code == 404, r.text


def test_flag_off_delete_snapshot_501(tmp_path, monkeypatch):
    """With ENABLE_STACK_SWAP off, the per-snapshot DELETE route also returns 501 (M1)."""
    # Build/register with the gate ON (capture_fleet_snapshot is a no-op when the gate is
    # off), THEN flip the flag off so the route is gated.
    p, reg = _build_active_source(tmp_path, monkeypatch)
    sid, _ = _registered_target(p, reg, tmp_path, monkeypatch)
    client = _make_app(p)
    monkeypatch.setattr('agent_cascade.settings.ENABLE_STACK_SWAP', False)

    assert client.delete(f'/api/stacks/{TARGET}/snapshots/{sid}').status_code == 501
    assert len(reg.list_snapshots(TARGET)) == 1         # off-path: nothing removed


def test_snapshot_retention_default_is_50(monkeypatch):
    """SNAPSHOT_RETENTION_COUNT falls back to 50 when AGENT_CASCADE_SNAPSHOT_RETENTION_COUNT
    is unset (plan §13.2). Re-parsing with the env cleared makes the assertion independent
    of any ambient value."""
    from agent_cascade import settings

    monkeypatch.delenv('AGENT_CASCADE_SNAPSHOT_RETENTION_COUNT', raising=False)
    assert settings._parse_snapshot_retention() == 50
