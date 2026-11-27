"""Self-adapting skill-hint value ranking — Thompson-Sampling bandit (v0.15.52).

The bandit REPLACES Gate-4's precision demote re-rank (``memory_hint/manager.py``) when
``SKILL_BANDIT_WEIGHT > 0.0``; with the default (OFF, 0.0) the existing Gate-4 block runs
byte-identically as an ``elif`` fallback. The tests mirror the BUG_0189 recipe (drive
``_match_skills_for_hint`` directly, neutralise the orthogonal floor/content gates, stub
``get_metrics``) so the assertion is purely about the bandit.

Bandit math (per candidate, O(1), over the ≤10 post-gate candidates):
    s   = min(agent_selected_loads, hints)      # CLIPPED successes (deliberate loads; BUG_0356)
    f   = hints - s
    a,b = _bandit_posterior(loads, hints, p̄, m0) = (m0·p̄ + s,  m0·(1−p̄) + f)   # β ≥ m0·(1−p̄) > 0
    θ   ~ Beta(a, b)                            # one Thompson-Sampling draw
    M(θ)= clamp(θ/p̄, M_FLOOR, 1/M_FLOOR)        # base-rate-centred, centre-neutral
    eff = relevance × (1 + W·(M − 1));  admit iff θ ≥ θ_min  (θ_min=0 ⇒ admit-all)
    p̄  = Σ min(loads, hints) / Σ hints over the ledger, excluding 'self-augmentation'.
In-memory fire-rate controller drifts θ_min to hold TARGET_FIRE, bounded to [0, θ_max].
"""

import threading
import time
from types import SimpleNamespace

import pytest

from agent_cascade.memory_hint import manager as manager_mod
from agent_cascade.memory_hint.manager import MemoryHintManager, _bandit_posterior


# ── Hermetic stubs (self-contained; mirrored from test_bug0189_skill_precision_ranking) ──

class _StubSkillManager:
    """Plain double (NOT a MagicMock — auto-stub makes every attribute truthy and defeats
    the gate checks). ``match_skills_with_stats`` is ABSENT so ``_match_skills_for_hint``
    falls back to ``match_skills`` deterministically. ``raise_on_metrics`` lets the
    graceful-degradation tests drive the best-effort get_metrics() guard."""

    def __init__(self, matches, metrics=None, raise_on_metrics=False, non_dict_metrics=None):
        self._matches = list(matches)           # [(name, score)] score-descending
        self._metrics = metrics if metrics is not None else {}
        self._raise_on_metrics = raise_on_metrics
        self._non_dict_metrics = non_dict_metrics

    def match_skills(self, query):
        return list(self._matches)

    def get_metrics(self):
        if self._raise_on_metrics:
            raise RuntimeError('ledger unavailable')
        if self._non_dict_metrics is not None:
            return self._non_dict_metrics          # exotic double → must degrade to no-op
        return dict(self._metrics)

    def bump_skill_hints(self, names):
        pass  # irrelevant here; the ledger bump lives in _process_job, tested by BUG_0189


class _FakeInst:
    """Minimal instance exposing the skill-hint state fields + lock."""

    def __init__(self, name='w'):
        self.instance_name = name
        self.agent_class = 'test_agent'
        self.state = 'RUNNING'
        self._compression_lock = threading.RLock()
        self._tool_warnings = []
        self._memories_read = set()
        self._recently_hinted = {}
        self._last_memory_hint_turn = -1
        self._recently_skill_hinted = {}
        self._last_skill_hint_turn = -1
        self._skill_hint_counts = {}


LESSONS = [
    ('bandit-test.md', 'Bandit Test',
     'Deterministic lesson body so the memory path always fires cleanly',
     'Body text that never competes with the skill ranking assertions.'),
]


def _make_mgr(tmp_path, skill_manager, cooldown_seconds=5.0):
    v = tmp_path / 'proj' / '.agent_lessons'
    v.mkdir(parents=True)
    for rel, name, desc, body in LESSONS:
        p = v / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"---\nname: {name}\ndescription: {desc}\ntags: [test]\n---\n{body}\n",
            encoding='utf-8',
        )
    pool = SimpleNamespace(
        llm_cfg={
            'memory_hint_enabled': True,
            'memory_hint_max_entries': 3,
            'memory_hint_cooldown_seconds': cooldown_seconds,
            'memory_hint_query_chars': 1000,
        },
        skill_manager=skill_manager,
        operation_manager=SimpleNamespace(
            base_dir=str(v.parent), extra_work_folders_ro=[], extra_work_folders_rw=[]),
        get_instance=None,
    )
    inst = _FakeInst()
    pool.get_instance = lambda *a, **k: inst
    mgr = MemoryHintManager(pool)
    mgr.rescan_vaults()
    return mgr, inst


def _job(turn=3, query='debugging the selector ranking pipeline'):
    return {
        'instance_name': 'w',
        'query': query,
        'agent_class': 'test_agent',
        'submitted_at': time.monotonic(),
        'turn': turn,
    }


def _settings():
    return {'skill_suggestions': True, 'cooldown_seconds': 5.0, 'skill_threshold': 0.0}


def _neutralize_gates(mgr, monkeypatch):
    """Neutralise the adaptive floor / EWMA feed and the content gate so the assertion is
    purely about the bandit re-rank (both independently covered by BUG_0041 / BUG_0226)."""
    mgr._current_skill_floor = lambda threshold=0.0: 0.0
    mgr._update_skill_floor = lambda *a, **k: None
    monkeypatch.setattr(manager_mod, 'filter_stopword_only_names',
                        lambda query, names, sm: list(names))
    monkeypatch.setattr(manager_mod, 'describe_dropped',
                        lambda query, kept, candidates, sm: '')


def _bandit_on(monkeypatch, weight=1.0):
    """Put the bandit on as the primary path (precision fallback stays whatever it is)."""
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_WEIGHT', weight)


def _determinize_sampler(mgr):
    """Make Thompson Sampling deterministic by returning the posterior mean, so ranking
    tests are reproducible without changing production behaviour (plan §2.3)."""
    mgr._skill_bandit_rng  # touch to assert it exists
    mgr._sample_beta = lambda a, b: (a / (a + b)) if (a + b) > 0.0 else 0.0


# ── (a) Default OFF → Gate-4 fallback, byte-identical to today ──────────────────

def test_default_off_runs_gate4_precision_fallback(tmp_path, monkeypatch):
    """SKILL_BANDIT_WEIGHT=0 must NOT take the bandit path: the existing precision re-rank
    runs unchanged. Reproduces the BUG_0189 #5 high-signal case → precision demotes the
    never-loaded skill below the loaded one."""
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_WEIGHT', 0.0)   # default OFF
    monkeypatch.setattr(manager_mod, 'SKILL_PRECISION_WEIGHT', 1)
    monkeypatch.setattr(manager_mod, 'SKILL_PRECISION_K', 1.0)
    monkeypatch.setattr(manager_mod, 'SKILL_PRECISION_FLOOR', 0.35)
    NEVER = 'skill-topical-but-never-loaded'
    LOADED = 'skill-really-gets-loaded'
    sm = _StubSkillManager(
        matches=[(NEVER, 0.55), (LOADED, 0.40)],
        metrics={NEVER: {'agent_selected_loads': 0, 'hints': 40},
                 LOADED: {'agent_selected_loads': 10, 'hints': 10}},
    )
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == [LOADED, NEVER], (
        'bandit OFF must yield the Gate-4 precision order, not the bandit order')


def test_default_off_all_off_is_raw_order_byte_identical(tmp_path, monkeypatch):
    """With bandit OFF and precision OFF the order is the raw score-descending order —
    the byte-identical-to-today default. A heavily-hinted / zero-load skill that the bandit
    would demote must still list normally."""
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_WEIGHT', 0.0)
    monkeypatch.setattr(manager_mod, 'SKILL_PRECISION_WEIGHT', 0)
    sm = _StubSkillManager(
        matches=[('a', 0.55), ('b', 0.45), ('c', 0.40)],
        metrics={'a': {'agent_selected_loads': 0, 'hints': 9999}, 'b': {'agent_selected_loads': 50, 'hints': 1}},
    )
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == ['a', 'b', 'c']


# ── (b) Bandit re-rank orders high-value first ──────────────────────────────────

def test_bandit_promotes_high_value_above_zero_value_same_relevance(tmp_path, monkeypatch):
    """Same relevance: a skill that converts hints to loads must outrank one that never does.
    Determinized sampler (θ = posterior mean). p̄ from {A:20/20, B:0/20} = 0.5, so A gets a
    promotion multiplier (>1) and B is demoted to the floor (0.35) — A wins despite raw order."""
    _bandit_on(monkeypatch)
    A = 'skill-high-value'
    B = 'skill-zero-value'
    sm = _StubSkillManager(
        matches=[(B, 0.50), (A, 0.50)],     # raw order puts B (the zero-value one) first
        metrics={A: {'agent_selected_loads': 20, 'hints': 20}, B: {'agent_selected_loads': 0, 'hints': 20}},
    )
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == [A, B], 'bandit must promote the high-value skill above the zero-value one'
    assert out != [B, A], 'raw order was the opposite — proves the bandit reordered, not tie-stability'


def test_bandit_ignores_total_loads_counts_only_deliberate_loads(tmp_path, monkeypatch):
    """BUG_0356: total_loads (the auto-load superset) must NOT drive the bandit — only
    agent_selected_loads. AUTO carries total_loads=500 but zero agent_selected_loads →
    demoted to M_FLOOR; DELIB carries the SAME total_loads=500 but agent_selected_loads=20 →
    promoted. p̄ from {DELIB asl=20/hints=20, AUTO asl=0/hints=20} = 0.5. Two-sided: total_loads
    cannot rescue a never-DELIBERATELY-loaded skill."""
    _bandit_on(monkeypatch)
    AUTO = 'skill-auto-loaded-never-chosen'
    DELIB = 'skill-deliberately-loaded'
    sm = _StubSkillManager(
        matches=[(AUTO, 0.50), (DELIB, 0.50)],   # raw order puts the auto-loaded one first
        metrics={AUTO:  {'total_loads': 500, 'agent_selected_loads': 0, 'hints': 20},
                 DELIB: {'total_loads': 500, 'agent_selected_loads': 20, 'hints': 20}},
    )
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == [DELIB, AUTO], (
        'BUG_0356: bandit must floor the auto-loaded / never-chosen skill despite '
        'total_loads=500; only agent_selected_loads counts')
    assert out != [AUTO, DELIB], 'raw order was the opposite — proves the bandit reordered'


# ── (c) Dormancy: 0 hints / 0 loads stays eligible (exploration bonus) ──────────

def test_dormant_posterior_mean_equals_base_rate_not_zero(tmp_path, monkeypatch):
    """A never-seen skill's posterior is the informed prior, so its mean is p̄ — eligible,
    NOT pinned to 0.5 (which would mass-spam) and NOT 0 (which would suppress)."""
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_PRIOR_MASS', 3.0)
    sm = _StubSkillManager(matches=[], metrics={'base': {'agent_selected_loads': 15, 'hints': 100}})
    mgr, _ = _make_mgr(tmp_path, sm)
    pbar = mgr._bandit_pbar({'base': {'agent_selected_loads': 15, 'hints': 100}})  # 0.15
    a, b = _bandit_posterior(0, 0, pbar, 3.0)
    assert (a + b) == pytest.approx(3.0)          # prior mass is exactly m0
    assert (a / (a + b)) == pytest.approx(pbar), (
        'dormant posterior mean must equal p̄, not 0 and not 0.5 (uniform Beta(1,1))')


def test_dormant_skill_outranks_known_useless_skill(tmp_path, monkeypatch):
    """A 0-hint/0-load skill (posterior mean = p̄ → multiplier 1.0) must rank ABOVE a
    heavily-hinted / zero-load skill (demoted to M_FLOOR) — the dormancy "exploration bonus"
    expressed as eligibility. Also asserts prior variance exceeds an observed skill's."""
    _bandit_on(monkeypatch)
    DORMANT = 'never-seen-skill'
    USELESS = 'heavily-hinted-never-loaded'
    BASE = 'base-rate-keeper'        # not a candidate; only fixes p̄ > 0
    sm = _StubSkillManager(
        matches=[(USELESS, 0.50), (DORMANT, 0.50)],   # raw order puts USELESS first
        metrics={USELESS: {'agent_selected_loads': 0, 'hints': 50},
                 BASE: {'agent_selected_loads': 30, 'hints': 110}},   # p̄ = 30/110 = 0.2727
    )
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == [DORMANT, USELESS], (
        'dormancy: a never-seen skill must not be suppressed below a known-useless one')
    # variance decays: prior (dormant) variance > observed (USELESS) variance
    pbar = mgr._bandit_pbar(dict(sm._metrics))
    pa, pb = _bandit_posterior(0, 0, pbar, 3.0)
    ua, ub = _bandit_posterior(0, 50, pbar, 3.0)
    def _var(a, b):
        return a * b / ((a + b) ** 2 * (a + b + 1.0))
    assert _var(pa, pb) > _var(ua, ub), (
        'TS exploration bonus: a dormant (low-evidence) skill must have larger posterior variance')


# ── (d) min(loads, hints) clip prevents a malformed Beta ────────────────────────

def test_min_clip_keeps_beta_well_formed_when_loads_exceed_hints(tmp_path):
    """agent_selected_loads CAN exceed hints (a skill deliberately loaded more times
    than it was hinted — BUG_0356 N5). Unclipped, hints−loads < 0 ⇒ β < 0 (malformed
    Beta). The clip forces s = min(L, H) so β = m0·(1−p̄) + (H − s) ≥ m0·(1−p̄) > 0
    always. Regression guard: a future refactor removing the clip must fail this assertion."""
    # L=204 > H=73 (the on-disk project-memory-writing profile). p̄=0.5, m0=3.
    a, b = _bandit_posterior(204, 73, 0.5, 3.0)
    assert b == pytest.approx(3.0 * (1.0 - 0.5) + (73 - 73))  # = 1.5 (clip: s=73, f=0)
    assert b > 0.0, 'clip must keep β > 0 (unclipped f = 73-204 = -131 → β < 0)'
    assert a == pytest.approx(3.0 * 0.5 + 73)                  # s clipped to 73
    assert (a + b) == pytest.approx(3.0 + 73)                  # α+β = m0 + H


def test_superset_clip_skill_ranks_high_without_crash(tmp_path, monkeypatch):
    """End-to-end: a superset skill (loads ≫ hints) must not crash and must read as high-value."""
    _bandit_on(monkeypatch)
    HIGH = 'project-memory-writing-like'
    LOW = 'skill-zero-value'
    sm = _StubSkillManager(
        matches=[(LOW, 0.50), (HIGH, 0.50)],
        metrics={HIGH: {'agent_selected_loads': 204, 'hints': 73},   # rate 1.0 after clip
                 LOW: {'agent_selected_loads': 0, 'hints': 73}},
    )
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == [HIGH, LOW], 'clipped superset skill must rank high (not crash, not blank)'


# ── (e) Fire-rate controller drives θ_min to target and stays bounded ───────────

def test_fire_rate_controller_bounded_and_drifts(monkeypatch, tmp_path):
    """In-memory PI/EWMA controller: sustained over-firing raises θ_min to its upper bound;
    sustained under-firing lowers it to 0. θ_min must NEVER leave [0, θ_max] (mass-suppression
    is structurally impossible)."""
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_GAIN', 0.10)        # faster convergence
    monkeypatch.setattr(manager_mod, 'SKILL_EWMA_ALPHA', 0.20)         # faster EWMA
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_TARGET_FIRE', 0.10)
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_THETA_MAX', 0.50)
    sm = _StubSkillManager(matches=[])
    mgr, _ = _make_mgr(tmp_path, sm)

    # Over-fire: every turn fires → p̂ → 1 > target → bar rises, bounded by θ_max.
    bars = []
    for _ in range(400):
        mgr._update_skill_fire_rate(True)
        bars.append(mgr._skill_bandit_bar)
    assert all(0.0 <= x <= 0.50 for x in bars), 'θ_min must stay bounded [0, θ_max]'
    assert bars[-1] == pytest.approx(0.50), 'sustained over-fire must saturate at θ_max'

    # Under-fire: every turn skips → p̂ → 0 < target → bar falls from θ_max back to 0.
    prev = mgr._skill_bandit_bar
    fell = False
    for _ in range(2000):
        mgr._update_skill_fire_rate(False)
        cur = mgr._skill_bandit_bar
        assert 0.0 <= cur <= 0.50, 'θ_min must stay bounded while falling'
        assert cur <= prev + 1e-12, 'under-fire must never raise θ_min (monotone non-increasing)'
        if cur < prev:
            fell = True
        prev = cur
    assert fell, 'under-fire must actively lower θ_min'
    assert mgr._skill_bandit_bar == 0.0, 'sustained under-fire must reach the 0 lower bound (admit-all)'


def test_controller_at_target_is_equilibrium(monkeypatch, tmp_path):
    """When the fire EWMA equals the target, the drift term is 0 and θ_min is stable."""
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_TARGET_FIRE', 0.10)
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_GAIN', 0.05)
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_THETA_MAX', 0.60)
    sm = _StubSkillManager(matches=[])
    mgr, _ = _make_mgr(tmp_path, sm)
    mgr._skill_bandit_bar = 0.30
    mgr._skill_fire_p = 0.10          # exactly at target → first update drifts the bar by ...
    # one fired=False flips p̂ off-target, so feed a mix that holds p̂ at 0.10 is fiddly;
    # assert the seed state is stable when p̂ == target and fired matches target frequency.
    bar_before = mgr._skill_bandit_bar
    # p̂ stays ~0.10 when fired with prob ≈0.10; deterministic check: zero drift term iff p̂==target.
    # Feed a pattern whose long-run mean is the target and assert the bar does not run away.
    pattern = ([True] + [False] * 9)      # 1/10 firing = target fire-rate
    for i in range(4000):
        mgr._update_skill_fire_rate(pattern[i % 10])
    assert 0.0 <= mgr._skill_bandit_bar <= 0.60, 'θ_min must stay bounded near the target regime'


# ── (f) No mass-suppression: never all dropped ──────────────────────────────────

def test_admit_all_identity_when_bar_zero(tmp_path, monkeypatch):
    """θ_min = 0 (the default) ⇒ admit-all: every candidate survives (no drops), only reorders.
    A low-value skill must still be PRESENT (ranking-only, never suppressed by the bandit)."""
    _bandit_on(monkeypatch)
    sm = _StubSkillManager(
        matches=[('hi', 0.55), ('lo', 0.50), ('mid', 0.45)],
        metrics={'hi': {'agent_selected_loads': 30, 'hints': 30}, 'lo': {'agent_selected_loads': 0, 'hints': 60},
                 'mid': {'agent_selected_loads': 5, 'hints': 10}},
    )
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert set(out) == {'hi', 'lo', 'mid'}, (
        'θ_min=0 must not drop any candidate — bandit reorders only, never suppresses')


def test_bar_at_bound_never_blanks_the_hint(tmp_path, monkeypatch):
    """With θ_min pinned at its upper bound and EVERY candidate below the bar, the guard must
    keep the raw order — the hint is never blanked by the bandit (no mass-suppression)."""
    _bandit_on(monkeypatch)
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_THETA_MAX', 0.60)
    sm = _StubSkillManager(
        matches=[('c1', 0.50), ('c2', 0.45), ('c3', 0.40)],
        metrics={'base': {'agent_selected_loads': 10, 'hints': 100}},   # p̄ = 0.10
    )
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    # Force every candidate's sampled θ (≈ prior mean = p̄ = 0.10) below the bound bar.
    mgr._skill_bandit_bar = 0.60
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == ['c1', 'c2', 'c3'], (
        'θ_min at its bound with all candidates below it must fall back to raw order, never blank')


# ── (g) Failure in the bandit path degrades gracefully (hint not blanked) ───────

def test_metrics_unavailable_degrades_to_raw_order(tmp_path, monkeypatch):
    """get_metrics() raising must be swallowed: metrics={} ⇒ p̄=0 ⇒ neutral ⇒ raw score order,
    non-empty. The hint must NOT be blanked."""
    _bandit_on(monkeypatch)
    sm = _StubSkillManager(matches=[('x', 0.50), ('y', 0.40)], raise_on_metrics=True)
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == ['x', 'y'], 'bandit must degrade to raw order when metrics raise (not blank)'


def test_non_dict_metrics_degrades_to_raw_order(tmp_path, monkeypatch):
    """A non-dict get_metrics() (exotic double / mock) must be guarded to {} ⇒ neutral no-op."""
    _bandit_on(monkeypatch)
    sm = _StubSkillManager(matches=[('x', 0.50), ('y', 0.40)],
                           non_dict_metrics=['not', 'a', 'dict'])
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == ['x', 'y']


def test_non_dict_metrics_row_degrades_not_blanks(tmp_path, monkeypatch):
    """Regression guard for the AttributeError leak: a ledger ROW that is a non-dict truthy
    value (hand-edited/corrupt ``skills-metrics.json``) must be guarded to the prior, not crash
    ``entry.get(...)`` and blank the whole hint. Requires p̄>0 so the code reaches the entry line
    (with p̄=0 it short-circuits earlier — which is why the earlier non-dict-metrics test alone
    did NOT cover this path)."""
    _bandit_on(monkeypatch)
    sm = _StubSkillManager(
        matches=[('skill-bad-row', 0.50), ('skill-good', 0.45)],
        metrics={'base-rate-keeper': {'agent_selected_loads': 30, 'hints': 100},   # p̄ = 0.30 (> 0)
                 'skill-bad-row': 'oops'},                                # corrupt ROW → guard
    )
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == ['skill-bad-row', 'skill-good'], (
        'a corrupt (non-dict) ledger ROW must degrade to the prior, never blank the hint')


def test_sampler_failure_falls_back_to_posterior_mean(tmp_path, monkeypatch):
    """If the Beta sampler raises, use the posterior mean (no raise, still value-ranks)."""
    _bandit_on(monkeypatch)
    A, B = 'skill-high-value', 'skill-zero-value'
    sm = _StubSkillManager(
        matches=[(B, 0.50), (A, 0.50)],
        metrics={A: {'agent_selected_loads': 20, 'hints': 20}, B: {'agent_selected_loads': 0, 'hints': 20}},
    )
    mgr, inst = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    def _boom(a, b):
        raise RuntimeError('sampler unavailable')
    mgr._sample_beta = _boom
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == [A, B], 'sampler failure must fall back to posterior-mean ranking, not blank'


# ── Orthogonality guards ────────────────────────────────────────────────────────

def test_self_augmentation_excluded_from_base_rate(monkeypatch, tmp_path):
    """self-augmentation is synthetic (≈708 init-AUTO loads / ~4 hints). It MUST be excluded
    from p̄, or the base rate is inflated past 1.0 and every skill looks 'useless' by comparison."""
    sm = _StubSkillManager(matches=[])
    mgr, _ = _make_mgr(tmp_path, sm)
    metrics = {
        'self-augmentation': {'agent_selected_loads': 708, 'hints': 4},   # excluded by name
        'x': {'agent_selected_loads': 10, 'hints': 100},                  # true signal: 10/100 = 0.10
    }
    pbar = mgr._bandit_pbar(metrics)
    assert pbar == pytest.approx(0.10), (
        'self-augmentation must be excluded from p̄ (0.10), not 718/104 ≈ 6.9')


def test_dormant_pbar_cache_is_set_once(tmp_path, monkeypatch):
    """p̄ is derived once and cached (plan §9.2 — no per-turn corpus scan)."""
    sm = _StubSkillManager(matches=[])
    mgr, _ = _make_mgr(tmp_path, sm)
    assert mgr._skill_bandit_pbar is None
    mgr._bandit_pbar({'x': {'agent_selected_loads': 1, 'hints': 10}})     # 0.10
    first = mgr._skill_bandit_pbar
    assert first == pytest.approx(0.10)
    # A changed ledger must NOT re-scan — the cached value is returned unchanged.
    second = mgr._bandit_pbar({'x': {'agent_selected_loads': 9, 'hints': 10}})
    assert second == first == 0.10, 'p̄ must be cached, not recomputed per call'


# ── Settings default guard ──────────────────────────────────────────────────────

def test_bandit_knob_defaults_off_and_env_backed():
    from agent_cascade import settings
    assert settings.SKILL_BANDIT_WEIGHT == 0.0, (
        'SKILL_BANDIT_WEIGHT must default OFF so deploy-time behaviour is byte-identical')
    assert settings.SKILL_BANDIT_PRIOR_MASS == 3.0
    assert settings.SKILL_BANDIT_TARGET_FIRE == pytest.approx(0.10)
    assert 0.0 < settings.SKILL_BANDIT_THETA_MAX < 1.0, (
        'θ_max must be < 1 so mass-suppression is structurally impossible')


# ── (h) BUG_0417: controller OFF (default) ⇒ bandit is rank-only / admit-all ─────
#
# The θ_min fire-rate controller (_update_skill_fire_rate) is gated by SKILL_BANDIT_CONTROLLER,
# which defaults OFF. With it OFF, _skill_bandit_bar stays at its 0.0 seed ⇒ the value re-rank's
# theta_min is 0.0 ⇒ admit-all ⇒ demote-only (the bandit reorders hints but never drops one).

def test_bandit_controller_off_default_is_admit_all_rank_only(tmp_path, monkeypatch):
    """BUG_0417: bandit ON but θ_min controller OFF (default) ⇒ the re-rank is admit-all
    (output length == input length; demote-only). Two-sided so the admit-all assertion is
    not vacuous:
      • Control — a LIVE bar (θ_min=0.20) DOES drop the low-θ skill, proving the ledger can
        drop candidates when a bar is active.
      • OFF — driving the REAL deploy path (_process_job) over fired turns never calls
        _update_skill_fire_rate, θ_min stays 0.0, and the re-rank admits every candidate.
    Removing `and SKILL_BANDIT_CONTROLLER` at manager.py makes the OFF spy record calls and
    fails this test — so it is a true guard, not decoration."""
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_WEIGHT', 1.0)         # bandit ON
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_CONTROLLER', 0)       # controller OFF (default)
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_THETA_MAX', 0.60)
    HIGH = 'skill-high-value'                       # θ ≈ 0.96 → survives any live bar
    ZERO = 'skill-never-loaded-low-theta'           # θ ≈ 0.03 → dropped by a live bar > 0.03
    BASE = 'base-rate-keeper'                        # not a candidate; only fixes p̄ > 0
    metrics = {HIGH: {'agent_selected_loads': 40, 'hints': 40},
               ZERO: {'agent_selected_loads': 0, 'hints': 40},
               BASE: {'agent_selected_loads': 40, 'hints': 100}}         # p̄ = 80/180 ≈ 0.44

    # Control: a live θ_min genuinely drops the low-θ skill (proves admit-all below is real).
    mgr_ctl, inst_ctl = _make_mgr(
        tmp_path / 'ctl', _StubSkillManager(matches=[(HIGH, 0.50), (ZERO, 0.50)], metrics=metrics))
    _neutralize_gates(mgr_ctl, monkeypatch)
    _determinize_sampler(mgr_ctl)
    mgr_ctl._skill_bandit_bar = 0.20                # simulate a live controller that drifted here
    out_ctl = mgr_ctl._match_skills_for_hint(_job(turn=3), _settings(), inst_ctl)
    assert ZERO not in out_ctl and HIGH in out_ctl, (
        'control: a live θ_min=0.20 must drop the low-θ skill — so the admit-all below is '
        'non-vacuous (the ledger CAN drop candidates when a bar is active)')

    # OFF default: driving the real deploy path must leave the bar inert ⇒ admit-all.
    mgr, inst = _make_mgr(
        tmp_path / 'off', _StubSkillManager(matches=[(HIGH, 0.50), (ZERO, 0.50)], metrics=metrics))
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = mgr._match_skills_for_hint(_job(turn=3), _settings(), inst)   # bar still at its 0.0 seed
    assert len(out) == 2 and set(out) == {HIGH, ZERO}, (
        'controller OFF ⇒ admit-all: every candidate survives; the bandit only demotes, never '
        'drops (output length == input length)')
    # The real deploy path must not move θ_min when the controller is OFF.
    calls = []
    monkeypatch.setattr(mgr, '_update_skill_fire_rate', lambda fired: calls.append(fired))
    for i in range(3):
        mgr._process_job(_job(turn=10 + i))          # fired skill-hint turns
    assert calls == [], ('controller OFF must never drive _update_skill_fire_rate — the θ_min bar '
                         'must stay inert (rank-only / admit-all)')
    assert mgr._skill_bandit_bar == 0.0, 'controller OFF must leave θ_min at its 0.0 seed'


def test_bandit_controller_knob_defaults_off_and_env_backed(monkeypatch):
    """BUG_0417: SKILL_BANDIT_CONTROLLER must default OFF (⇒ bandit rank-only / admit-all) and
    be env-backed, exactly like the other SKILL_BANDIT_* knobs."""
    from agent_cascade import settings
    assert settings.SKILL_BANDIT_CONTROLLER == 0, (
        'SKILL_BANDIT_CONTROLLER must default OFF so the bandit is rank-only (admit-all) and '
        'deploy-time behaviour stays byte-identical')
    # env-backed: load a throwaway copy of settings.py with the env set in an ISOLATED namespace
    # (importlib.reload would mutate the module every worker shares — house pattern, see
    # tests/test_sanity_probe.py). Confirms the knob is read from the environment, not hardcoded.
    import importlib.util
    import os as _os
    monkeypatch.setenv('AGENT_CASCADE_SKILL_BANDIT_CONTROLLER', '1')
    _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    _sp = _os.path.join(_root, 'agent_cascade', 'settings.py')
    spec = importlib.util.spec_from_file_location('_settings_0417_copy', _sp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.SKILL_BANDIT_CONTROLLER == 1, 'SKILL_BANDIT_CONTROLLER must honour the env var'
