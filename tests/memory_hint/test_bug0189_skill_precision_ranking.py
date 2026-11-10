"""BUG_0189 #5 regression: skill-hint selector PRECISION (Gate 4).

The memory-hint skill sub-pipeline used to list skills purely by match SCORE, so a
skill that is topical-but-never-loaded outranked a slightly-less-topical skill that is
actually loaded. Gate 4 adds a demote-only re-rank driven by the durable load ledger
(``agents/global/skills-metrics.json``):

    w(s) = max(FLOOR, min(1.0, (loads+K)/(hints+K)))          # demote-only multiplier
    sort to_hint by (score * w(s)) desc                        # stable; ties keep order

These tests isolate Gate 4: the adaptive floor / EWMA feed and the content gate are
orthogonal (covered by the BUG_0041 / BUG_0226 suites), so the ranking tests neutralise
them to keep the assertion about WHICH names and in what ORDER purely about precision.
The ledger bump itself lives in ``_process_job`` (NOT ``_match_skills_for_hint``), so the
"no phantom credit" case drives ``_process_job`` and reads the manager double's recorded
calls — it never asserts on ``_process_job``'s return (which is ``None``).
"""

import threading
import time
from types import SimpleNamespace

import pytest

from agent_cascade.memory_hint import manager as manager_mod
from agent_cascade.memory_hint.manager import MemoryHintManager

# Skill names used across the ranking tests.
LOADED = 'skill-really-gets-loaded'
NEVER = 'skill-topical-but-never-loaded'
DROP = 'skill-zero-load-over-suppressed'

LESSONS = [
    ('precision-test.md', 'Precision Test',
     'Deterministic lesson body so the memory path always fires cleanly',
     'Body text that never competes with the skill ranking assertions.'),
]


def _write_lesson(vault, rel_path, name, description, body):
    p = vault / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\ntags: [test]\n---\n{body}\n",
        encoding='utf-8',
    )


class _StubSkillManager:
    """Purpose-built plain double (NOT a MagicMock — an auto-stub makes every attribute
    truthy and defeats the gate checks). ``match_skills_with_stats`` is deliberately ABSENT
    so ``_match_skills_for_hint`` falls back to ``match_skills`` deterministically.

    Records every ``bump_skill_hints`` call so a test can assert exactly WHICH names were
    credited to the ledger (the no-phantom-credit guard)."""

    def __init__(self, matches, metrics=None, field_index=None):
        self._matches = list(matches)           # [(name, score)] score-descending
        self._metrics = metrics or {}
        self.bumps = []                         # recorded bump_skill_hints(list) payloads
        if field_index is not None:
            self._matcher = SimpleNamespace(_field_index=field_index)

    def match_skills(self, query):
        return list(self._matches)

    def get_metrics(self):
        return dict(self._metrics)

    def bump_skill_hints(self, names):
        self.bumps.append(list(names))


class _FakeInst:
    """Minimal instance exposing the memory-hint state fields + lock."""

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


def _make_mgr(tmp_path, skill_manager, cooldown_seconds=5.0):
    """Vault + pool wiring mirrored from test_bug0233_skill_hint_entry_cooldown, but with
    an injected skill-manager double so the caller controls matches/metrics/field_index."""
    v = tmp_path / 'proj' / '.agent_lessons'
    v.mkdir(parents=True)
    for rel, name, desc, body in LESSONS:
        _write_lesson(v, rel, name, desc, body)

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
    return mgr, inst, skill_manager


def _job(turn=3, query='debugging the selector ranking pipeline'):
    return {
        'instance_name': 'w',
        'query': query,
        'agent_class': 'test_agent',
        'submitted_at': time.monotonic(),
        'turn': turn,
    }


def _settings():
    # Keys read by _match_skills_for_hint on the skill path only.
    return {'skill_suggestions': True, 'cooldown_seconds': 5.0, 'skill_threshold': 0.0}


@pytest.fixture
def gate4_off(monkeypatch):
    """Default Gate-4 state for the ranking tests: precision re-rank ON, suppression OFF-ish
    (N large enough to never fire), and the orthogonal gates neutralised so the assertion is
    purely about precision. Neutralises the adaptive floor + content gate (tested elsewhere)."""
    monkeypatch.setattr(manager_mod, 'SKILL_PRECISION_WEIGHT', 1)
    monkeypatch.setattr(manager_mod, 'SKILL_PRECISION_K', 1.0)
    monkeypatch.setattr(manager_mod, 'SKILL_PRECISION_FLOOR', 0.35)


def _neutralize_gates(mgr, monkeypatch):
    """Neutralise the adaptive floor / EWMA feed and the content gate so these tests assert
    ONLY Gate 4's reorder + suppression. Both are independently covered (BUG_0041/BUG_0226)."""
    mgr._current_skill_floor = lambda threshold=0.0: 0.0
    mgr._update_skill_floor = lambda *a, **k: None
    monkeypatch.setattr(manager_mod, 'filter_stopword_only_names',
                        lambda query, names, sm: list(names))
    monkeypatch.setattr(manager_mod, 'describe_dropped',
                        lambda query, kept, candidates, sm: '')


def test_precision_demotes_never_loaded_skill_below_a_loaded_one(tmp_path, monkeypatch, gate4_off):
    """A more topical-but-never-loaded skill must NOT outrank a loaded, high-precision one.

    Raw score-descending order is [NEVER(0.55), LOADED(0.40)]. NEVER has loads=0/hints=40,
    so w = max(0.35, min(1, 1/41)) = 0.35 → effective 0.55*0.35 = 0.1925. LOADED has
    loads=10/hints=10 → w=1.0 → effective 0.40. Re-ranked desc → [LOADED, NEVER]. NEVER has
    hints=40 < N=50, so it is DEMOTED only, not suppressed."""
    sm = _StubSkillManager(
        matches=[(NEVER, 0.55), (LOADED, 0.40)],
        metrics={NEVER: {'total_loads': 0, 'hints': 40},
                 LOADED: {'total_loads': 10, 'hints': 10}},
    )
    mgr, inst, _ = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)

    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == [LOADED, NEVER], (
        'BUG_0189 #5: precision must promote the loaded skill above the never-loaded one')
    # Sanity: the raw topically-descending order was the opposite.
    assert out != [NEVER, LOADED]


def test_missing_hints_field_is_neutral_not_dropped(tmp_path, monkeypatch, gate4_off):
    """A metrics entry with no 'hints' key (a legacy ledger record) must read as hints=0,
    NOT crash and NOT suppress — it gets full weight and is listed normally."""
    sm = _StubSkillManager(
        matches=[(LOADED, 0.40)],
        metrics={LOADED: {'total_loads': 3, 'by_version': {}}},   # no 'hints' key
    )
    mgr, inst, _ = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)
    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == [LOADED]


def test_gate4_is_byte_identical_no_op_when_both_knobs_off(tmp_path, monkeypatch):
    """With WEIGHT=0 and SUPPRESS=0 the gate must not touch the order or drop anything,
    even when a skill is loaded-0 and hinted heavily (the exact profile it would demote)."""
    monkeypatch.setattr(manager_mod, 'SKILL_PRECISION_WEIGHT', 0)
    sm = _StubSkillManager(
        matches=[(NEVER, 0.55), (LOADED, 0.45), (DROP, 0.40)],
        metrics={NEVER: {'total_loads': 0, 'hints': 9999},
                 LOADED: {'total_loads': 50, 'hints': 1}},   # would flip the order if on
    )
    mgr, inst, _ = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)

    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert out == [NEVER, LOADED, DROP], (
        'BUG_0189 #5: both knobs off must yield the raw score-descending order byte-identically')


def test_ledger_is_credited_only_for_rendered_survivors(tmp_path, monkeypatch):
    """No phantom credit: a skill dropped by the REAL content gate must never be bumped.

    This drives ``_process_job`` (where the ledger bump actually happens) with a manager
    double carrying a real ``_matcher._field_index`` so Gate 3 genuinely removes 'junk'.
    The double records every ``bump_skill_hints`` payload, so we assert the ledger saw only
    'good'."""
    sm = _StubSkillManager(
        matches=[('good', 0.70), ('junk', 0.65)],
        metrics={'good': {'total_loads': 5, 'hints': 2}},
        field_index={
            'alpha': {'good': {}},          # good shares 2 CONTENT terms → kept
            'beta': {'good': {}},
            'the': {'junk': {}},            # junk shares only a stopword → dropped
        },
    )
    mgr, inst, sm = _make_mgr(tmp_path, sm)
    # Neutralise ONLY the adaptive floor (content gate stays REAL on purpose here).
    mgr._current_skill_floor = lambda threshold=0.0: 0.0
    mgr._update_skill_floor = lambda *a, **k: None

    mgr._process_job(_job(query='alpha beta the'))
    flat = [n for call in sm.bumps for n in call]
    assert 'good' in flat, 'the surviving skill must be credited'
    assert 'junk' not in flat, (
        'BUG_0189 #5: a Gate-3-dropped skill must never be credited to the ledger')
