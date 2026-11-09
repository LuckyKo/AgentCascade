"""BUG_0280 regression: the SHIPPED default of the zero-load hard-drop threshold
(``SKILL_ZERO_LOAD_SUPPRESS_HINTS``) is low enough to actually drop the observed
0-load / heavily-hinted cohort, while a skill near the auto-load threshold survives.

Unlike the BUG_0189 boundary tests — which monkeypatch N to 5/50/0 purely to probe
the gate's boundary semantics — these tests drive the gate with the shipped default
NOT monkeypatched, so they pin the value the product actually ships with:

    loads == 0 and hints >= N  -> the skill is hard-dropped from hints
    loads == 0 and hints  < N  -> survives
    loads  > 0 (any hints)     -> never 0-load-dropped

The gate reads the constant off ``agent_cascade.memory_hint.manager`` (imported at
module load from settings.py), so the constant is read from the code under test.
The two module constants below are domain facts, not the gate threshold:
``ZERO_LOAD_COHORT_HINTS`` is the floor of the observed most-missed cohort, and
``AUTO_LOAD_HINTS`` is the auto-load threshold (``skill_autoload_threshold``,
``manager.py:630``, default 3; the per-session recurrence marker ``c >= 3`` in
``_skill_block`` also reads 3). Keeping them one apart encodes the suppress/
auto-load non-race
invariant — the tuned default sits just above auto-load so a near-auto-load skill
is demoted only, never hard-dropped.
"""

from agent_cascade.memory_hint import manager as manager_mod
from tests.memory_hint.test_bug0189_skill_precision_ranking import (
    _StubSkillManager,
    _job,
    _make_mgr,
    _neutralize_gates,
    _settings,
)

# Floor of the observed most-missed 0-load cohort (hints 4-6 / loads 0-1).
ZERO_LOAD_COHORT_HINTS = 4
# Auto-load threshold (``skill_autoload_threshold``, manager.py:630, default 3;
# the per-session recurrence marker ``c >= 3`` in _skill_block also reads 3). A
# skill at this hint depth is demoted only, never 0-load-hard-dropped.
AUTO_LOAD_HINTS = 3


def test_shipped_default_drops_zero_load_cohort_at_the_boundary(tmp_path, monkeypatch):
    """(loads=0, hints==cohort-floor) is suppressed; hints==auto-load survives; a
    skill ever loaded is never 0-load-dropped — all against the shipped default.

    Precision re-rank is disabled so the ONLY thing changing the output is the hard
    drop; the content gate / adaptive floor are neutralised to isolate Gate 4."""
    monkeypatch.setattr(manager_mod, 'SKILL_PRECISION_WEIGHT', 0)

    drop = 'cohort-zero-load-at-cohort-floor'      # loads=0, hints==cohort floor -> drop
    keep = 'near-auto-load-not-suppressed'         # loads=0, hints==auto-load -> keep
    loaded = 'ever-loaded-never-zero-dropped'      # loads=1, hints==cohort floor -> keep
    sm = _StubSkillManager(
        matches=[(drop, 0.70), (keep, 0.65), (loaded, 0.60)],
        metrics={
            drop: {'total_loads': 0, 'hints': ZERO_LOAD_COHORT_HINTS},
            keep: {'total_loads': 0, 'hints': AUTO_LOAD_HINTS},
            loaded: {'total_loads': 1, 'hints': ZERO_LOAD_COHORT_HINTS},
        },
    )
    mgr, inst, _ = _make_mgr(tmp_path, sm)
    _neutralize_gates(mgr, monkeypatch)

    out = mgr._match_skills_for_hint(_job(), _settings(), inst)
    assert drop not in out, (
        f'BUG_0280: a 0-load skill at the cohort floor (hints={ZERO_LOAD_COHORT_HINTS}) '
        'must be hard-dropped by the shipped default')
    assert keep in out, (
        f'a 0-load skill at the auto-load depth (hints={AUTO_LOAD_HINTS}) must survive '
        '(demote-only, not suppressed)')
    assert loaded in out, 'a skill ever loaded is never 0-load-dropped, regardless of hints'
    assert out == [keep, loaded], 'survivors keep their raw score-descending order'


def test_shipped_default_sits_just_above_the_auto_load_threshold():
    """Guard the tune itself: the shipped default must equal the cohort floor and sit
    strictly above the auto-load threshold, so hard-drop and auto-load never race.

    References the constant from the code under test (not a duplicated threshold) —
    if the default ever drifts back above the cohort floor, the cohort survives
    (the BUG_0280 false-positive waste) and this guard catches it directly."""
    assert manager_mod.SKILL_ZERO_LOAD_SUPPRESS_HINTS == ZERO_LOAD_COHORT_HINTS
    assert manager_mod.SKILL_ZERO_LOAD_SUPPRESS_HINTS > AUTO_LOAD_HINTS
