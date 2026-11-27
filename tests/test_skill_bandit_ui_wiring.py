"""BUG_0417: UI wiring for the skill-hint bandit weight (``skill_bandit_weight``).

Verifies the registry-backed UI setting live-patches the consumer namespace
(``memory_hint.manager.SKILL_BANDIT_WEIGHT``) so a UI save takes effect without a
restart, that the handler clamps to [0,1] and persists to ``pool.settings``, and that
the default (0.0) leaves the bandit inert (byte-identical precision path).
"""
import agent_cascade.memory_hint.manager as _mgr
from agent_cascade import settings_registry
from agent_cascade.config_handlers import _handle_skill_bandit_weight, apply_skill_bandit_weight


class _FakeSettings:
    pass


class _FakePool:
    def __init__(self):
        self.settings = _FakeSettings()


def test_skill_bandit_weight_is_registered_pool_key_default_zero():
    """The key is a broadcast, save-trigger, pool-kind registry entry with default 0.0."""
    sp = settings_registry.spec('skill_bandit_weight')
    assert sp.kind == 'pool'
    assert sp.save_trigger is True
    assert sp.broadcast is True
    assert sp.value_source == 'settings'
    assert sp.default == 0.0
    # Present in both the persist set (pool_settings.json) and the broadcast snapshot set.
    assert 'skill_bandit_weight' in settings_registry.POOL_SETTINGS_KEYS
    assert 'skill_bandit_weight' in settings_registry.BROADCAST_KEYS


def test_apply_live_patches_manager_namespace():
    """apply_skill_bandit_weight writes into manager's module-level constant (no restart)."""
    saved = _mgr.SKILL_BANDIT_WEIGHT
    try:
        apply_skill_bandit_weight(0.5)
        assert _mgr.SKILL_BANDIT_WEIGHT == 0.5
        apply_skill_bandit_weight(1.0)
        assert _mgr.SKILL_BANDIT_WEIGHT == 1.0
        apply_skill_bandit_weight(0.0)  # back to off
        assert _mgr.SKILL_BANDIT_WEIGHT == 0.0
    finally:
        _mgr.SKILL_BANDIT_WEIGHT = saved


def test_handler_clamps_and_persists_to_pool_settings():
    """The handler clamps to [0,1], persists to pool.settings, and live-applies."""
    pool = _FakePool()
    saved = _mgr.SKILL_BANDIT_WEIGHT
    try:
        _handle_skill_bandit_weight({'skill_bandit_weight': '2.5'}, pool, [])  # out of range
        assert pool.settings.skill_bandit_weight == 1.0  # clamped to [0,1]
        assert _mgr.SKILL_BANDIT_WEIGHT == 1.0
        _handle_skill_bandit_weight({'skill_bandit_weight': '-1'}, pool, [])
        assert pool.settings.skill_bandit_weight == 0.0
        _handle_skill_bandit_weight({'skill_bandit_weight': 0.3}, pool, [])
        assert pool.settings.skill_bandit_weight == 0.3
        assert _mgr.SKILL_BANDIT_WEIGHT == 0.3
    finally:
        _mgr.SKILL_BANDIT_WEIGHT = saved


def test_default_zero_leaves_manager_constant_at_zero():
    """A missing/absent key (default 0.0) leaves the bandit inert (byte-identical path)."""
    saved = _mgr.SKILL_BANDIT_WEIGHT
    try:
        _mgr.SKILL_BANDIT_WEIGHT = 0.7  # simulate a previously-on state
        pool = _FakePool()
        _handle_skill_bandit_weight({}, pool, [])  # no key present → default 0.0
        assert _mgr.SKILL_BANDIT_WEIGHT == 0.0
        assert getattr(pool.settings, 'skill_bandit_weight', 0.0) == 0.0
    finally:
        _mgr.SKILL_BANDIT_WEIGHT = saved
