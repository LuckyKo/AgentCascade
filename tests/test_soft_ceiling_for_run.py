"""BUG_0380 regression fix: ``soft_turn_limit`` must grant overage at ALL effort levels.

``soft_ceiling_for_run`` returns the absolute turn ceiling for the soft-turn-limit path.
When ``soft_turn_limit`` is ON the ceiling must NOT be notch-collapsed — the BUG_0380
regression collapsed it to the effort notch, so a low/med/high run hard-stopped at the
notch with zero soft overage despite the setting being ON (sub-agents were force-ended).
When OFF the ceiling is notch-collapsed (the notch is the hard per-run ceiling).
"""
from agent_cascade.settings import SOFT_TURN_LIMIT_ABSOLUTE_MAX, soft_ceiling_for_run


def test_soft_on_medium_not_capped_to_notch():
    """The core regression: medium (notch 50) + soft ON -> ceiling is the absolute max, not 50."""
    assert soft_ceiling_for_run(50, 'medium', True) == SOFT_TURN_LIMIT_ABSOLUTE_MAX
    assert soft_ceiling_for_run(50, 'medium', True) > 50  # overage headroom now exists


def test_soft_on_all_notches_not_capped():
    """soft ON grants overage at every non-ultra notch (low/med/high)."""
    for level in ('low', 'medium', 'high'):
        cap = soft_ceiling_for_run(30, level, True)
        assert cap == SOFT_TURN_LIMIT_ABSOLUTE_MAX
        assert cap > 30


def test_soft_off_notch_collapsed():
    """soft OFF -> ceiling is notch-collapsed (the notch is the hard per-run ceiling)."""
    assert soft_ceiling_for_run(250, 'medium', False) == 50    # min(max(250,1000), 50)
    assert soft_ceiling_for_run(250, 'low', False) == 30
    assert soft_ceiling_for_run(250, 'high', False) == 100


def test_soft_on_ultra_uses_absolute_max():
    """ultra has no notch; soft ON keeps the absolute max."""
    assert soft_ceiling_for_run(250, 'ultra', True) == SOFT_TURN_LIMIT_ABSOLUTE_MAX


def test_ceiling_never_below_initial():
    """The ceiling is never below the initial turn count (safety floor)."""
    assert soft_ceiling_for_run(5000, 'medium', True) == 5000   # initial > ABSOLUTE_MAX
