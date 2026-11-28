"""Self-adapting memory-hint value ranking — Thompson-Sampling bandit (BUG_0317).

Extends the skill-hint bandit (BUG_0316) to MEMORY hints. The re-rank is RANKING-ONLY:
when ``MEMORY_BANDIT_WEIGHT > 0.0`` it multiplies the cosine ``strong`` score by the shared
base-rate-centred value multiplier via ``MemoryHintManager._value_rank`` (the SAME helper the
skill pipeline uses), COMPOSING with the EWMA floor rather than re-gating. With the default
(OFF, 0.0) the ``if MEMORY_BANDIT_WEIGHT > 0.0:`` block is skipped entirely → ``to_hint`` flows
to the cap in matcher score-descending order → deploy-time behaviour byte-identical.

Bandit math (per candidate, shared with skills via _value_rank):
    s   = min(reads, hints)                       # CLIPPED successes (reads may lead hints)
    f   = hints - s
    a,b = _bandit_posterior(reads, hints, p̄, m0) = (m0·p̄ + s, m0·(1−p̄) + f)
    θ   ~ Beta(a, b)                              # one Thompson-Sampling draw
    M(θ)= clamp(θ/p̄, M_FLOOR, 1/M_FLOOR)          # base-rate-centred, centre-neutral
    eff = relevance × (1 + W·(M − 1));  admit iff θ ≥ θ_min  (θ_min=0 ⇒ admit-all)
    p̄  = Σ min(reads, hints) / Σ hints over the merged lesson ledger mirror.
The reward ledger is the durable per-lesson ``memory_stats.json`` {reads, hints}; a durable
``reads`` can lead ``hints`` (a read from a non-hint path), hence the ``min`` clip.
In-memory fire-rate controller drifts θ_min to hold TARGET_FIRE, bounded to [0, θ_max].

Mirrors test_skill_hint_bandit.py; drives ``_match_memories`` directly with a stub matcher and
a seeded ``_memory_stats`` mirror (no disk) so the assertion is purely about the bandit.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from agent_cascade.memory_hint import manager as manager_mod
from agent_cascade.memory_hint.manager import MemoryHintManager, _bandit_posterior


# ── Hermetic stubs ──────────────────────────────────────────────────────────

class _StubMatcher:
    """Plain double (NOT a MagicMock — auto-stub makes truthy attrs and defeats the gates).
    ``match`` returns a caller-supplied, score-descending ``[(path, score)]`` list verbatim;
    ``set_documents`` is a no-op so a test may install it after ``rescan_vaults`` replaced the
    real matcher. ``match`` returns the same list every call (a single-turn test)."""

    def __init__(self, matches):
        self._matches = list(matches)          # [(path, score)] score-descending

    def match(self, query, allowed_scope=None, recency=None, importance=None, weights=None):
        # Accept the production matcher's recall-scoring kwargs (BUG_0428) so the stub
        # stays signature-compatible with memory_hint/matcher.py::match(); the stub
        # ignores them and returns the caller-supplied list verbatim.
        return list(self._matches)

    def set_documents(self, documents):        # called by rescan_vaults — keep it harmless
        pass


class _FakeInst:
    """Minimal instance exposing the fields ``_match_memories`` + bookkeeping touch + lock."""

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


def _make_mgr(tmp_path, skill_manager=None):
    """Build a manager over a real (but irrelevant) vault so construction is valid, then the
    test installs a _StubMatcher and seeds _memory_stats. The vault lessons never compete with
    the stubbed matcher, so only the stub drives the gate."""
    v = tmp_path / 'proj' / '.agent_lessons'
    v.mkdir(parents=True, exist_ok=True)
    p = v / 'bandit-test.md'
    p.write_text(
        '---\nname: Bandit Test\ndescription: filler\ntags: [test]\n---\nfiller body.\n',
        encoding='utf-8',
    )
    pool = SimpleNamespace(
        llm_cfg={
            'memory_hint_enabled': True,
            'memory_hint_max_entries': 3,
            'memory_hint_cooldown_seconds': 5.0,
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
    return mgr, inst, v


def _install_stub(mgr, matches):
    """Replace the real matcher with a score-descending stub after construction."""
    mgr._matcher = _StubMatcher(matches)


def _seed_ledger(mgr, ledger):
    """Seed the in-memory lesson ledger mirror and mark it ready so the bandit block's
    _refresh_memory_stats() is a no-op (controlled ledger, zero disk reads). Keys are the
    stub's bare rel paths; values are {'reads': int, 'hints': int}."""
    mgr._memory_stats = {k: {'reads': int(d['reads']), 'hints': int(d['hints'])}
                         for k, d in ledger.items()}
    mgr._memory_bandit_pbar = None            # force fresh p̄ over the seeded ledger
    mgr._memory_stats_ready = True


def _job(turn=3, query='debugging the selector ranking pipeline'):
    return {
        'instance_name': 'w',
        'query': query,
        'agent_class': 'test_agent',
        'submitted_at': time.monotonic(),
        'turn': turn,
    }


def _settings(threshold=0.0, cooldown_seconds=5.0, max_entries=3):
    return {'threshold': threshold, 'cooldown_seconds': cooldown_seconds,
            'max_entries': max_entries}


def _neutralize_gates(mgr, monkeypatch):
    """Neutralise the orthogonal memory gate so the assertion is purely about the bandit:
    signal floor → 0.0 (never blocks); EWMA floor feed → no-op; specificity GAP → 0.0 (so a
    near-tie among high candidates doesn't trip the single-winner skip). Dedup is inert with a
    fresh _FakeInst (no _memories_read / _recently_hinted entries)."""
    mgr._current_floor = lambda threshold=0.0: 0.0
    mgr._update_floor = lambda *a, **k: None
    monkeypatch.setattr(manager_mod, 'GAP', 0.0)


def _bandit_on(monkeypatch, weight=1.0):
    monkeypatch.setattr(manager_mod, 'MEMORY_BANDIT_WEIGHT', weight)


def _settings_skill():
    """Settings dict for driving the skill sub-pipeline (mirrors test_skill_hint_bandit)."""
    return {'skill_suggestions': True, 'cooldown_seconds': 5.0, 'skill_threshold': 0.0}


def _neutralize_skill_gates(mgr, monkeypatch):
    """Neutralise the skill adaptive floor / EWMA feed and the content gate so the skill path
    reaches the Gate-4 bandit block (mirrors test_skill_hint_bandit._neutralize_gates). Used by
    the _value_rank delegation test, which drives the skill pipeline to prove the shared call
    site."""
    mgr._current_skill_floor = lambda threshold=0.0: 0.0
    mgr._update_skill_floor = lambda *a, **k: None
    monkeypatch.setattr(manager_mod, 'filter_stopword_only_names',
                        lambda query, names, sm: list(names))
    monkeypatch.setattr(manager_mod, 'describe_dropped',
                        lambda query, kept, candidates, sm: '')


def _determinize_sampler(mgr):
    """Make Thompson Sampling deterministic (θ = posterior mean) so ranking tests are
    reproducible. Mirrors _determinize_sampler for the skill bandit (same shared _sample_beta)."""
    mgr._sample_beta = lambda a, b: (a / (a + b)) if (a + b) > 0.0 else 0.0


def _run_match(mgr, inst):
    """Run the memory sub-pipeline and return the ORDERED bare-path list (the bandit's output)."""
    return mgr._match_memories(_job(), _settings(), inst)[0]


# ── (a) Default OFF → block skipped, byte-identical raw order ─────────────────

def test_default_off_is_raw_score_order_byte_identical(tmp_path, monkeypatch):
    """MEMORY_BANDIT_WEIGHT=0.0 must SKIP the re-rank block: to_hint keeps the matcher's
    score-descending order. A heavily-hinted/zero-read lesson the bandit WOULD demote must
    still list at its raw score position (proves no value ranking ran)."""
    monkeypatch.setattr(manager_mod, 'MEMORY_BANDIT_WEIGHT', 0.0)   # default OFF
    HIGH, LOW, BASE = 'high-value', 'low-value', 'base-rate-keeper'
    mgr, inst, _ = _make_mgr(tmp_path)
    _install_stub(mgr, [(LOW, 0.50), (HIGH, 0.50), (BASE, 0.40)])
    _seed_ledger(mgr, {HIGH: {'reads': 20, 'hints': 20},
                       LOW: {'reads': 0, 'hints': 20},
                       BASE: {'reads': 15, 'hints': 100}})
    _neutralize_gates(mgr, monkeypatch)
    out = _run_match(mgr, inst)
    assert out == [LOW, HIGH, BASE], 'bandit OFF must yield the raw score-descending order'


def test_memory_hint_bump_is_weight_gated(tmp_path, monkeypatch):
    """The per-lesson `hints` ledger bump is WEIGHT-GATED in ``_process_job`` (the
    ``if MEMORY_BANDIT_WEIGHT > 0.0 and memory_bare:`` branch).
    Drive the real deploy path with ``bump_hint_count`` spied: with MEMORY_BANDIT_WEIGHT=0.0 the
    OFF deploy must NOT bump (the default deploy writes nothing new); with it >0.0 and a real
    lesson actually rendered, the spy IS called — proving the gate, not merely its absence.

    The stub matcher returns a bare path that EXISTS on disk in the test vault so
    _resolve_vault_root resolves it and the weight>0 branch reaches bump_hint_count."""
    from agent_cascade.memory_hint import stats as stats_mod
    calls = []
    # bump_hint_count is a function-local import inside _process_job, so patch the module attr
    # it resolves at call time (not the manager's name).
    monkeypatch.setattr(stats_mod, 'bump_hint_count', lambda root, rel: calls.append(rel))

    # OFF deploy: a rendered lesson must NOT bump the hints ledger.
    monkeypatch.setattr(manager_mod, 'MEMORY_BANDIT_WEIGHT', 0.0)
    mgr, _inst, _ = _make_mgr(tmp_path)
    _install_stub(mgr, [('bandit-test.md', 0.80)])   # bare path that exists on disk in the vault
    _neutralize_gates(mgr, monkeypatch)
    mgr._process_job(_job())
    assert calls == [], 'MEMORY_BANDIT_WEIGHT=0.0 must write nothing to the hints ledger'

    # ON deploy: the same rendered lesson MUST bump. A fresh manager so the OFF run's cooldown
    # cannot suppress the re-fire (the gate is `MEMORY_BANDIT_WEIGHT > 0.0 and memory_bare`).
    monkeypatch.setattr(manager_mod, 'MEMORY_BANDIT_WEIGHT', 1.0)
    mgr2, _inst2, _ = _make_mgr(tmp_path)
    _install_stub(mgr2, [('bandit-test.md', 0.80)])
    _neutralize_gates(mgr2, monkeypatch)
    mgr2._process_job(_job())
    assert 'bandit-test.md' in calls, ('weight>0.0 with a rendered lesson must bump its '
                                       'hints ledger via bump_hint_count')


# ── (b) Bandit re-rank orders high value first (ACCEPTANCE case) ───────────────

def test_high_reread_outranks_high_match_low_reread_at_equal_cosine(tmp_path, monkeypatch):
    """ACCEPTANCE: at EQUAL cosine, a high-min(reads,hints) lesson must rank ABOVE a
    heavily-hinted / low-read lesson. Raw order puts the low-read one first; the bandit must
    flip it. Determinized sampler (θ = posterior mean)."""
    _bandit_on(monkeypatch)
    HIGH, LOW, BASE = 'high-reread', 'high-match-low-reread', 'base-rate-keeper'
    mgr, inst, _ = _make_mgr(tmp_path)
    _install_stub(mgr, [(LOW, 0.50), (HIGH, 0.50), (BASE, 0.40)])   # raw order: LOW first
    _seed_ledger(mgr, {HIGH: {'reads': 20, 'hints': 20},
                       LOW: {'reads': 0, 'hints': 20},
                       BASE: {'reads': 15, 'hints': 100}})          # p̄ = 35/140 = 0.25
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = _run_match(mgr, inst)
    assert out[0] == HIGH, ('acceptance: the high-re-read lesson must outrank the equal-cosine '
                            'low-read one')


def test_bandit_promotes_high_value_above_zero_value_same_relevance(tmp_path, monkeypatch):
    """Same relevance: a lesson that converts hints to reads outranks one that never does."""
    _bandit_on(monkeypatch)
    HIGH, LOW, BASE = 'skill-high-value', 'zero-value', 'base-rate-keeper'
    mgr, inst, _ = _make_mgr(tmp_path)
    _install_stub(mgr, [(LOW, 0.50), (HIGH, 0.50), (BASE, 0.40)])
    _seed_ledger(mgr, {HIGH: {'reads': 20, 'hints': 20},
                       LOW: {'reads': 0, 'hints': 20},
                       BASE: {'reads': 15, 'hints': 100}})
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = _run_match(mgr, inst)
    assert out[0] == HIGH, 'bandit must promote the high-value lesson above the equal-cosine zero-value one'


# ── (c) Dormancy: 0-read / 0-hint lesson stays eligible ───────────────────────

def test_dormant_posterior_mean_equals_base_rate_not_zero(tmp_path, monkeypatch):
    """A never-seen lesson's posterior is the informed prior → mean = p̄ — eligible, NOT pinned
    to 0.5 (mass-spam) and NOT 0 (suppression)."""
    monkeypatch.setattr(manager_mod, 'MEMORY_BANDIT_PRIOR_MASS', 3.0)
    pbar = 0.15
    a, b = _bandit_posterior(0, 0, pbar, 3.0)
    assert (a + b) == pytest.approx(3.0)               # prior mass exactly m0
    assert (a / (a + b)) == pytest.approx(pbar), (
        'dormant posterior mean must equal p̄, not 0 and not 0.5 (uniform Beta(1,1))')


def test_dormant_lesson_outranks_known_useless_lesson(tmp_path, monkeypatch):
    """A 0-read/0-hint lesson (mean = p̄ → multiplier 1.0) must rank ABOVE a heavily-hinted /
    zero-read lesson (demoted to M_FLOOR) — the dormancy exploration bonus as eligibility."""
    _bandit_on(monkeypatch)
    DORMANT, USELESS, BASE = 'never-seen-lesson', 'heavily-hinted-never-read', 'base-rate-keeper'
    mgr, inst, _ = _make_mgr(tmp_path)
    _install_stub(mgr, [(USELESS, 0.50), (DORMANT, 0.50)])   # raw: USELESS first; BASE only in mirror (sets p̄)
    _seed_ledger(mgr, {USELESS: {'reads': 0, 'hints': 50},
                       BASE: {'reads': 30, 'hints': 110}})                 # p̄ = 30/110 = 0.2727
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = _run_match(mgr, inst)
    assert out[0] == DORMANT, ('dormancy: a never-read lesson must not be suppressed below a '
                               'known-useless one')
    # variance decays: prior (dormant) variance > observed (USELESS) variance
    pbar = mgr._memory_pbar()
    pa, pb = _bandit_posterior(0, 0, pbar, 3.0)
    ua, ub = _bandit_posterior(0, 50, pbar, 3.0)
    def _var(a, b):
        return a * b / ((a + b) ** 2 * (a + b + 1.0))
    assert _var(pa, pb) > _var(ua, ub), ('TS exploration bonus: dormant lesson must have '
                                          'larger posterior variance')


# ── (d) min(reads, hints) clip keeps the Beta well-formed ─────────────────────

def test_min_clip_keeps_beta_well_formed_when_reads_exceed_hints(tmp_path):
    """A durable read can lead hints (read via a non-hint path). Unclipped, hints−reads < 0 ⇒
    β < 0 (malformed Beta). The clip forces s = min(reads, hints). Regression guard for a
    future refactor removing the clip."""
    # reads=204 > hints=73, p̄=0.5, m0=3.
    a, b = _bandit_posterior(204, 73, 0.5, 3.0)
    assert b == pytest.approx(3.0 * (1.0 - 0.5) + (73 - 73))   # = 1.5 (clip: s=73, f=0)
    assert b > 0.0, 'clip must keep β > 0 (unclipped f = 73−204 = −131 → β < 0)'
    assert a == pytest.approx(3.0 * 0.5 + 73)                  # s clipped to 73
    assert (a + b) == pytest.approx(3.0 + 73)                  # α+β = m0 + hints


def test_superset_clip_lesson_ranks_high_without_crash(tmp_path, monkeypatch):
    """End-to-end: a superset lesson (reads ≫ hints) must not crash and must read as high-value."""
    _bandit_on(monkeypatch)
    HIGH, LOW, BASE = 'project-memory-writing-like', 'zero-value', 'base-rate-keeper'
    mgr, inst, _ = _make_mgr(tmp_path)
    _install_stub(mgr, [(LOW, 0.50), (HIGH, 0.50), (BASE, 0.40)])
    _seed_ledger(mgr, {HIGH: {'reads': 204, 'hints': 73},   # rate 1.0 after clip
                       LOW: {'reads': 0, 'hints': 73},
                       BASE: {'reads': 10, 'hints': 100}})
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = _run_match(mgr, inst)
    assert out[0] == HIGH, 'clipped superset lesson must rank high (not crash, not blank)'


# ── (e) Fire-rate controller drives θ_min to target and stays bounded ─────────

def test_memory_fire_rate_controller_bounded_and_drifts(monkeypatch, tmp_path):
    """In-memory EWMA controller: sustained over-firing raises θ_min to θ_max; sustained
    under-firing lowers it to 0. θ_min must NEVER leave [0, θ_max] (mass-suppression is
    structurally impossible). Mirrors the skill controller test."""
    monkeypatch.setattr(manager_mod, 'MEMORY_BANDIT_GAIN', 0.10)       # faster convergence
    monkeypatch.setattr(manager_mod, 'EWMA_ALPHA', 0.20)               # faster EWMA
    monkeypatch.setattr(manager_mod, 'MEMORY_BANDIT_TARGET_FIRE', 0.10)
    monkeypatch.setattr(manager_mod, 'MEMORY_BANDIT_THETA_MAX', 0.50)
    mgr, _inst, _ = _make_mgr(tmp_path)

    bars = []
    for _ in range(400):
        mgr._update_memory_fire_rate(True)
        bars.append(mgr._memory_bandit_bar)
    assert all(0.0 <= x <= 0.50 for x in bars), 'θ_min must stay bounded [0, θ_max]'
    assert bars[-1] == pytest.approx(0.50), 'sustained over-fire must saturate at θ_max'

    prev = mgr._memory_bandit_bar
    fell = False
    for _ in range(2000):
        mgr._update_memory_fire_rate(False)
        cur = mgr._memory_bandit_bar
        assert 0.0 <= cur <= 0.50, 'θ_min must stay bounded while falling'
        assert cur <= prev + 1e-12, 'under-fire must never raise θ_min (monotone non-increasing)'
        if cur < prev:
            fell = True
        prev = cur
    assert fell, 'under-fire must actively lower θ_min'
    assert mgr._memory_bandit_bar == 0.0, 'sustained under-fire must reach the 0 lower bound (admit-all)'


def test_memory_controller_at_target_is_equilibrium(monkeypatch, tmp_path):
    """Feeding exactly TARGET_FIRE keeps θ_min at its seed (no net drift): fire = target means
    the EWMA term is zero, so the bar must not move off 0.0."""
    monkeypatch.setattr(manager_mod, 'MEMORY_BANDIT_TARGET_FIRE', 0.10)
    monkeypatch.setattr(manager_mod, 'MEMORY_BANDIT_GAIN', 0.10)
    monkeypatch.setattr(manager_mod, 'EWMA_ALPHA', 0.10)
    mgr, _inst, _ = _make_mgr(tmp_path)
    # Feed ~10% fired: one fire per nine skips keeps p̂ near 0.10 once EWMA settles.
    seq = [True] + [False] * 9
    for i in range(2000):
        mgr._update_memory_fire_rate(seq[i % len(seq)])
    # Equilibrium: at-target firing keeps the drift term ≈0, so θ_min stays BOUNDED near the
    # target regime (never runs away to θ_max). Mirrors the skill equilibrium test's assertion.
    assert 0.0 <= mgr._memory_bandit_bar <= manager_mod.MEMORY_BANDIT_THETA_MAX, (
        'θ_min must stay bounded in the at-target regime (no runaway mass-suppression)')


# ── (f) Graceful degradation: never blank, never crash ────────────────────────

def test_admit_all_when_bar_zero_is_rank_only_identity(tmp_path, monkeypatch):
    """θ_min = 0 (the seed / admit-all) must admit every candidate — the bandit is pure
    re-rank, never a hard gate. Even a useless lesson stays in the output."""
    _bandit_on(monkeypatch)
    HIGH, USELESS = 'high-value', 'useless'
    mgr, inst, _ = _make_mgr(tmp_path)
    _install_stub(mgr, [(USELESS, 0.50), (HIGH, 0.50)])
    _seed_ledger(mgr, {HIGH: {'reads': 20, 'hints': 20}, USELESS: {'reads': 0, 'hints': 50}})
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    mgr._memory_bandit_bar = 0.0        # explicit admit-all
    out = _run_match(mgr, inst)
    assert USELESS in out, 'admit-all bar must not blank any candidate'
    assert out == [HIGH, USELESS]


def test_bar_at_bound_never_blanks_the_hint(tmp_path, monkeypatch):
    """Even at the upper θ_max bound, every candidate whose θ clears the bar is kept; if the
    bar filtered the top candidate it falls back to raw order — the hint is NEVER blanked."""
    _bandit_on(monkeypatch)
    A, B, C = 'a', 'b', 'c'
    mgr, inst, _ = _make_mgr(tmp_path)
    _install_stub(mgr, [(A, 0.80), (B, 0.60), (C, 0.40)])
    _seed_ledger(mgr, {A: {'reads': 5, 'hints': 10},
                       B: {'reads': 1, 'hints': 10},
                       C: {'reads': 0, 'hints': 10}})
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    mgr._memory_bandit_bar = manager_mod.MEMORY_BANDIT_THETA_MAX   # extreme bar
    out = _run_match(mgr, inst)
    assert out, 'bandit must never blank the hint at the θ_max bound'
    assert out == [A, B, C], ('admitted-empty at the extreme bar must fall back to the raw '
                              'score-descending order, never a partial reorder')


def test_empty_ledger_degrades_to_raw_order(tmp_path, monkeypatch):
    """p̄ = 0.0 (empty/cold ledger) ⇒ no value signal ⇒ raw score order (centre-neutral
    identity), and never a crash."""
    _bandit_on(monkeypatch)
    mgr, inst, _ = _make_mgr(tmp_path)
    _install_stub(mgr, [('a', 0.80), ('b', 0.60), ('c', 0.40)])
    _seed_ledger(mgr, {})            # empty mirror → p̄ = 0.0
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = _run_match(mgr, inst)
    assert out == ['a', 'b', 'c'], 'p̄=0 must yield raw order, never raise, never blank'


def test_non_dict_mirror_row_not_blanked(tmp_path, monkeypatch):
    """A corrupt (non-dict) mirror ROW must degrade to prior (0,0) for that lesson, not crash
    and not blank the hint (the getter guards it)."""
    _bandit_on(monkeypatch)
    GOOD, BAD = 'good', 'bad-row'
    mgr, inst, _ = _make_mgr(tmp_path)
    _install_stub(mgr, [(BAD, 0.80), (GOOD, 0.50)])
    _seed_ledger(mgr, {GOOD: {'reads': 20, 'hints': 20}})
    mgr._memory_stats[BAD] = 5         # corrupt legacy bare-int row in the mirror
    _neutralize_gates(mgr, monkeypatch)
    _determinize_sampler(mgr)
    out = _run_match(mgr, inst)
    assert out and GOOD in out, 'corrupt row must degrade, not crash or blank'


def test_sampler_failure_falls_back_to_posterior_mean(tmp_path, monkeypatch):
    """If _sample_beta raises, the candidate uses its posterior mean (no raise, no blank)."""
    _bandit_on(monkeypatch)
    HIGH, LOW, BASE = 'high-value', 'zero-value', 'base-rate-keeper'
    mgr, inst, _ = _make_mgr(tmp_path)
    _install_stub(mgr, [(LOW, 0.50), (HIGH, 0.50), (BASE, 0.40)])
    _seed_ledger(mgr, {HIGH: {'reads': 20, 'hints': 20},
                       LOW: {'reads': 0, 'hints': 20},
                       BASE: {'reads': 15, 'hints': 100}})
    _neutralize_gates(mgr, monkeypatch)
    mgr._sample_beta = lambda a, b: (_ for _ in ()).throw(RuntimeError('sampler down'))
    out = _run_match(mgr, inst)
    assert out[0] == HIGH, 'sampler failure → posterior mean → HIGH still ranks first'


# ── (g) p̄ caching + knobs ────────────────────────────────────────────────────

def test_pbar_cached_after_first_nonzero(tmp_path, monkeypatch):
    """A non-zero p̄ is cached and returned verbatim (no re-scan) — steady-state cost is O(1)
    per turn regardless of corpus size. Mutating the mirror must NOT change the cached value."""
    mgr, _inst, _ = _make_mgr(tmp_path)
    _seed_ledger(mgr, {'x': {'reads': 15, 'hints': 100}})   # p̄ = 0.15
    first = mgr._memory_pbar()
    assert first == pytest.approx(0.15)
    assert mgr._memory_bandit_pbar == pytest.approx(0.15), 'non-zero p̄ must be cached'
    mgr._memory_stats['x'] = {'reads': 0, 'hints': 0}       # mutate the mirror
    second = mgr._memory_pbar()
    assert second == pytest.approx(0.15), 'cached p̄ returned verbatim, not re-derived'


def test_bandit_knobs_read_from_settings_at_import_defaults(tmp_path):
    """The six memory knobs exist with the plan's defaults and are read as module constants."""
    assert manager_mod.MEMORY_BANDIT_WEIGHT == 0.0
    assert manager_mod.MEMORY_BANDIT_PRIOR_MASS == 3.0
    assert manager_mod.MEMORY_BANDIT_TARGET_FIRE == 0.10
    assert manager_mod.MEMORY_BANDIT_GAIN == 0.02
    assert manager_mod.MEMORY_BANDIT_THETA_MAX == 0.6
    assert manager_mod.MEMORY_BANDIT_M_FLOOR == 0.35


def test_value_rank_delegates_shared_helper_from_both_pipelines(tmp_path, monkeypatch):
    """The memory and skill pipelines must BOTH delegate to the one shared _value_rank helper,
    each passing its OWN ``neutral_keys``: the skill pipeline passes ``{'self-augmentation'}``
    (its synthetic self-improvement skill is never value-ranked) and the memory pipeline passes
    an EMPTY set (it has no such key). This drives the real call sites through a recording spy,
    so it FAILS if either pipeline stops delegating — unlike a ``__dict__`` membership check,
    which a helper the caller never invokes would still pass. (Replaces the prior existence-only
    test, whose ``src`` binding was dead and which asserted nothing about delegation.)"""
    real_vr = MemoryHintManager._value_rank
    seen = []

    def _spy(self, *args, **kwargs):
        # Effective neutral_keys: the skill site passes it as a kwarg; the memory site relies on
        # the frozenset() default, so capture both shapes.
        seen.append(kwargs.get('neutral_keys', frozenset()))
        return real_vr(self, *args, **kwargs)
    monkeypatch.setattr(MemoryHintManager, '_value_rank', _spy)

    # ── skill path: must delegate with neutral_keys={'self-augmentation'} ──
    class _StubSkillManager:
        def match_skills(self, query):
            return [('skill-a', 0.55), ('skill-b', 0.45)]   # both clear SKILL_JUNK_MIN=0.0375
        def get_metrics(self):
            return {'skill-a': {'total_loads': 20, 'hints': 20},
                    'skill-b': {'total_loads': 0, 'hints': 20}}
    sm = _StubSkillManager()
    monkeypatch.setattr(manager_mod, 'SKILL_BANDIT_WEIGHT', 1.0)
    mgr_skill, inst_skill, _ = _make_mgr(tmp_path, skill_manager=sm)
    _neutralize_skill_gates(mgr_skill, monkeypatch)
    seen.clear()
    mgr_skill._match_skills_for_hint(_job(), _settings_skill(), inst_skill)
    assert len(seen) == 1, 'skill pipeline must delegate to _value_rank exactly once'
    assert seen[0] == {'self-augmentation'}, (
        "skill pipeline must pass neutral_keys={'self-augmentation'} to the shared helper")

    # ── memory path: must delegate with an EMPTY neutral_keys ──
    monkeypatch.setattr(manager_mod, 'MEMORY_BANDIT_WEIGHT', 1.0)
    mgr_mem, inst_mem, _ = _make_mgr(tmp_path)
    _install_stub(mgr_mem, [('x', 0.80), ('y', 0.60)])
    _seed_ledger(mgr_mem, {'x': {'reads': 10, 'hints': 10}, 'y': {'reads': 0, 'hints': 10}})
    _neutralize_gates(mgr_mem, monkeypatch)
    _determinize_sampler(mgr_mem)
    seen.clear()
    _run_match(mgr_mem, inst_mem)
    assert len(seen) == 1, 'memory pipeline must delegate to _value_rank exactly once'
    assert not seen[0], 'memory pipeline has no neutral key; neutral_keys must be empty'


# ── (h) Sidecar int → dict migration round-trip ───────────────────────────────

def test_memory_stats_int_to_dict_migration_round_trip(tmp_path):
    """A legacy memory_stats.json with BARE INT values (the pre-BUG_0317 schema) must load as
    {path: {'reads': <int>, 'hints': 0}} via load_stats — the backward-tolerant migration.
    Round-trips: bump_hint_count on the migrated ledger yields {reads, hints} on re-load."""
    from agent_cascade.memory_hint.stats import load_stats, bump_hint_count, bump_read_count
    v = tmp_path / 'vault'
    v.mkdir()
    legacy = {'old-lesson.md': 7, 'another.md': 2}   # bare ints (legacy schema)
    (v / 'memory_stats.json').write_text(__import__('json').dumps(legacy), encoding='utf-8')
    loaded = load_stats(v)
    assert loaded['old-lesson.md'] == {'reads': 7, 'hints': 0}, 'legacy int must migrate to dict'
    assert loaded['another.md'] == {'reads': 2, 'hints': 0}
    bump_hint_count(v, 'old-lesson.md')              # new hints field increments
    assert load_stats(v)['old-lesson.md'] == {'reads': 7, 'hints': 1}
    bump_read_count(v, 'another.md')
    assert load_stats(v)['another.md'] == {'reads': 3, 'hints': 0}
