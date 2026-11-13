"""BUG_0312 skill-evolution telemetry tests.

Proves the durable per-skill Gate-B / churn ledger increments at each seam, that the
counters round-trip through the on-disk serialization, and that the read-only API
projection (_build_skill_evolution_table / _build_skill_evolution_aggregate) is correct
and defensive. Mirrors the BUG_0311 hint-telemetry test style.

Covers:
  manager.py  — promotions/discards (Gate B), updates (candidate register), transitions
                (disable/enable), pruned_total (prune), get_metrics aggregation, and
                durability through _flush_metrics_to_disk.
  api_server  — table projection (sort/exclude/.get-defaults/None) + aggregate
                (acceptance_rate incl. divide-by-zero) + empty-slice shape.

No browser, no live pool: the seam tests drive the manager methods directly, exactly as
the rating-gate tests do. The static UI wiring guard lives in
tests/test_skill_evolution_telemetry_ui_wiring.py.
"""

from agent_cascade.skills.manager import SkillManager, _default_metrics_entry
from agent_cascade.api_server import (
    _build_skill_evolution_table,
    _build_skill_evolution_aggregate,
    _empty_skill_evolution_slice,
)


# ── Ledger field defaults (M1) ────────────────────────────────────────────────

def test_default_entry_has_evolution_fields():
    """_default_metrics_entry ships the four BUG_0312 counters with zero defaults.

    This is the single source the promote/discard/flip seams read; if a field is dropped
    here the .get(field, 0) reads still work, but the documented default shape breaks.
    """
    e = _default_metrics_entry()
    for f in ('transitions', 'promotions', 'discards', 'updates'):
        assert e[f] == 0, f"{f} default must be 0, got {e[f]!r}"


# ── Gate B seams: promote / discard ───────────────────────────────────────────

def test_promote_increments_promotions_and_transitions(hermetic_skill_manager, tmp_path):
    m = hermetic_skill_manager
    name = 'promo-skill'
    # Seed a metrics entry (the gate only mutates an EXISTING entry).
    with m._metrics_lock:
        m._metrics[name] = dict(_default_metrics_entry())

    cand_file = tmp_path / 'cand' / 'SKILL.md'
    cand_file.parent.mkdir(parents=True, exist_ok=True)
    cand_file.write_text('---\ndescription: x\n---\nbody', encoding='utf-8')
    parsed = {'frontmatter': {'description': 'x', 'triggers': []}, 'version': '2.0.0'}

    # _promote_candidate expects the caller to hold _write_lock (RLock — reentrant, safe).
    with m._write_lock:
        m._promote_candidate(name, cand_file, parsed, '1.0.0')

    entry = m.get_metrics(name)
    assert entry['promotions'] == 1, 'Gate B ACCEPT must bump promotions'
    assert entry['transitions'] == 1, 'a promotion is a lifecycle transition'
    assert entry['discards'] == 0


def test_discard_increments_discards_and_transitions(hermetic_skill_manager):
    m = hermetic_skill_manager
    name = 'disc-skill'
    with m._metrics_lock:
        m._metrics[name] = dict(_default_metrics_entry())

    # _discard_candidate acquires _write_lock itself; no candidate file required (the
    # candidate-dir removal and registry restore are best-effort / guarded).
    m._discard_candidate(name, '1.0.0')

    entry = m.get_metrics(name)
    assert entry['discards'] == 1, 'Gate B REJECT must bump discards'
    assert entry['transitions'] == 1, 'a discard is a lifecycle transition'
    assert entry['promotions'] == 0


def test_acceptance_rate_aggregates_promote_and_discard(hermetic_skill_manager, tmp_path):
    """The headline metric: 1 promote + 1 discard across two skills → 0.5 acceptance."""
    m = hermetic_skill_manager
    cand = tmp_path / 'c' / 'SKILL.md'
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text('---\ndescription: x\n---\nbody', encoding='utf-8')
    parsed = {'frontmatter': {'description': 'x'}, 'version': '2.0.0'}

    for n in ('s1', 's2'):
        with m._metrics_lock:
            m._metrics[n] = dict(_default_metrics_entry())
    with m._write_lock:
        m._promote_candidate('s1', cand, parsed, '1.0.0')
    m._discard_candidate('s2', '1.0.0')

    agg = _build_skill_evolution_aggregate(
        _build_skill_evolution_table(m.get_metrics()))
    assert agg['promotions'] == 1
    assert agg['discards'] == 1
    assert agg['acceptance_rate'] == 0.5


# ── updates seam (candidate register) ─────────────────────────────────────────

def test_register_upgrade_increments_updates(hermetic_skill_manager, tmp_path):
    m = hermetic_skill_manager
    name = 'upd-skill'
    pending = tmp_path / 'pending' / 'SKILL.md'
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text('---\ndescription: y\nversion: 2.0.0\n---\nbody', encoding='utf-8')
    parsed = {'frontmatter': {'description': 'y', 'triggers': []}, 'version': '2.0.0'}
    frontmatter = {'description': 'y', 'triggers': []}

    # Called while caller holds _write_lock (RLock reentrant).
    with m._write_lock:
        ok = m._register_candidate_upgrade(name, pending, parsed, frontmatter, 'test')

    assert ok is True
    # No metrics entry existed → the upgrade path creates a bare dict; updates must still
    # land via .get(field, 0), proving the bare-dict tolerance.
    assert m.get_metrics(name)['updates'] == 1


# ── transitions seam (disable / enable) ───────────────────────────────────────

def test_disable_enable_bump_transitions(hermetic_skill_manager):
    m = hermetic_skill_manager
    name = 'churn-skill'
    with m._metrics_lock:
        m._metrics[name] = dict(_default_metrics_entry())

    ok, _ = m.disable_skill(name)
    assert ok
    entry = m.get_metrics(name)
    assert entry['status'] == 'inactive'
    assert entry['transitions'] == 1

    ok, _ = m.enable_skill(name)
    assert ok
    entry = m.get_metrics(name)
    assert entry['status'] == 'active'
    assert entry['transitions'] == 2, 'each flip is one transition'


# ── pruned_total seam (global deletion counter) ───────────────────────────────

def test_prune_bumps_pruned_total(hermetic_skill_manager):
    m = hermetic_skill_manager
    # The hermetic fixture loads _pruned_total from the REAL metrics file before redirecting
    # paths — reset it here (same reason it resets _global_activity_turns).
    m._pruned_total = 0
    # Seed one stale entry; the hermetic manager has no live skills for it, so prune deletes it.
    with m._metrics_lock:
        m._metrics['gone-skill'] = dict(_default_metrics_entry())

    m.prune_stale_metrics()

    assert 'gone-skill' not in m.get_metrics()
    assert m._pruned_total == 1, 'each pruned skill must count in the durable global total'


def test_pruned_total_roundtrips_to_disk(hermetic_skill_manager):
    """M6c: _flush_metrics_to_disk serializes the top-level pruned_total (the one field
    NOT auto-persisted by the per-skill deepcopy — the DIG-flagged serialization caveat)."""
    m = hermetic_skill_manager
    m._pruned_total = 7
    with m._metrics_lock:
        m._metrics['keep'] = dict(_default_metrics_entry())
    m._flush_metrics_to_disk()

    # Re-instantiate reading the same (tmp) file to prove durability.
    m2 = SkillManager()
    m2._metrics_file = m._metrics_file
    m2._load_metrics()
    assert m2._pruned_total == 7, 'pruned_total must survive a flush+reload'


# ── API projection: table ─────────────────────────────────────────────────────

def test_evolution_table_excludes_self_augmentation():
    metrics = {
        'self-augmentation': {'updates': 5, 'promotions': 3, 'discards': 1, 'transitions': 4},
        'real-skill': {'updates': 1, 'promotions': 1, 'discards': 0, 'transitions': 1},
    }
    rows = _build_skill_evolution_table(metrics)
    assert {r['skill'] for r in rows} == {'real-skill'}


def test_evolution_table_sorts_by_transitions_desc_then_name():
    metrics = {
        'alpha': {'transitions': 2},
        'bravo': {'transitions': 5},
        'charlie': {'transitions': 2},
    }
    rows = _build_skill_evolution_table(metrics)
    assert [r['skill'] for r in rows] == ['bravo', 'alpha', 'charlie']


def test_evolution_table_zero_defaults_and_none_entry():
    """Pre-BUG_0312 entries (no new fields) and None values read as all-zero, never crash."""
    metrics = {
        'legacy': {},                      # no evolution fields
        'none_val': None,                  # entry value is None
        'partial': {'promotions': 2},      # only one field present
    }
    rows = _build_skill_evolution_table(metrics)
    by = {r['skill']: r for r in rows}
    for name in ('legacy', 'none_val'):
        r = by[name]
        assert r['updates'] == 0 and r['promotions'] == 0 and r['discards'] == 0
        assert r['transitions'] == 0
    assert by['partial']['promotions'] == 2
    # status passes through (None tolerated)
    assert by['legacy']['status'] is None


def test_evolution_table_none_metrics_returns_empty():
    assert _build_skill_evolution_table(None) == []
    assert _build_skill_evolution_table({}) == []


# ── API projection: aggregate ─────────────────────────────────────────────────

def test_aggregate_acceptance_rate_rounds_and_guards_zero_denominator():
    rows = [
        {'skill': 'a', 'updates': 3, 'promotions': 2, 'discards': 1, 'transitions': 3, 'status': 'active'},
        {'skill': 'b', 'updates': 1, 'promotions': 0, 'discards': 0, 'transitions': 1, 'status': 'inactive'},
    ]
    agg = _build_skill_evolution_aggregate(rows, pruned_total=4)
    assert agg['promotions'] == 2
    assert agg['discards'] == 1
    assert agg['updates'] == 4
    assert agg['net_inactive'] == 1
    assert agg['pruned_total'] == 4
    assert agg['acceptance_rate'] == round(2 / 3, 3)


def test_aggregate_acceptance_rate_none_when_no_decisions():
    rows = [{'skill': 'a', 'updates': 0, 'promotions': 0, 'discards': 0,
             'transitions': 0, 'status': 'active'}]
    agg = _build_skill_evolution_aggregate(rows, pruned_total=0)
    assert agg['acceptance_rate'] is None
    assert agg['promotions'] == 0 and agg['discards'] == 0


def test_empty_evolution_slice_shape():
    """The fallback slice must be well-shaped so the client never reads undefined."""
    s = _empty_skill_evolution_slice()
    assert set(s) == {'aggregate', 'table'}
    assert s['table'] == []
    agg = s['aggregate']
    for k in ('promotions', 'discards', 'updates', 'net_inactive', 'pruned_total'):
        assert agg[k] == 0
    assert agg['acceptance_rate'] is None
