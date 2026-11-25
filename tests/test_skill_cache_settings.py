"""Task-aware skill cache: defaults ON + UI toggle wiring.

Covers:
1. The two compiled defaults are ON (cache ships live out of the box).
2. The two config handlers (``skill_cache_active_enabled`` / ``skill_cache_commit_enabled``)
   write ``agent_pool.llm_cfg`` — default True when the key is absent, explicit False
   respected, and no-op when there is no pool.
3. The 6-seam wiring smoke test — each key has a registered handler, is in the save/persist
   set, is broadcast, is seeded in ``initial_llm_cfg``, is restored in ``config_persist``.

The cache reads these live from ``agent_pool.llm_cfg`` via
``SkillManager._cache_enabled()`` / ``_cache_commit_enabled()`` (skills/manager.py), so the
handlers are what make a UI toggle flip take effect without a restart.

Related: [[skill-cache-enable-default-on]]; mirrors TestAlwaysProtectedWiring in
test_skill_scoring_settings.py.
"""

from pathlib import Path
from unittest.mock import MagicMock

_KEYS = ('skill_cache_active_enabled', 'skill_cache_commit_enabled')


class TestDefaults:
    def test_cache_defaults_are_on(self):
        """Both switches default ON so the cache is live out of the box."""
        import agent_cascade.settings as s
        assert s.SKILL_CACHE_ACTIVE_ENABLED is True
        assert s.SKILL_CACHE_COMMIT_ENABLED is True


class TestHandlers:
    def test_handlers_registered(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS
        for key in _KEYS:
            assert key in CONFIG_HANDLERS, f"missing handler for {key}"

    def test_handler_defaults_to_true_when_absent(self):
        """A save that omits the key must leave the cache ON (matches compiled default)."""
        from agent_cascade.config_handlers import CONFIG_HANDLERS
        for key in _KEYS:
            pool = MagicMock()
            pool.llm_cfg = {}
            CONFIG_HANDLERS[key]({}, pool, [])
            assert pool.llm_cfg[key] is True, f"{key}: absent key must default True"

    def test_handler_respects_explicit_values(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS
        for key in _KEYS:
            pool = MagicMock()
            pool.llm_cfg = {}
            CONFIG_HANDLERS[key]({key: False}, pool, [])
            assert pool.llm_cfg[key] is False
            pool.llm_cfg = {}
            CONFIG_HANDLERS[key]({key: True}, pool, [])
            assert pool.llm_cfg[key] is True

    def test_handler_noop_without_pool(self):
        """A pool-less update_settings (e.g. session-only change) must not raise."""
        from agent_cascade.config_handlers import CONFIG_HANDLERS
        for key in _KEYS:
            CONFIG_HANDLERS[key]({key: False}, None, [])  # must not raise


class TestWiring:
    """Each key must be present in every Python seam that carries it (sibling pattern)."""

    def test_in_persist_list_and_broadcast(self):
        from agent_cascade.config_handlers import POOL_SETTINGS_KEYS
        from agent_cascade.constants import POOL_SETTINGS_TO_BROADCAST
        for key in _KEYS:
            assert key in POOL_SETTINGS_KEYS, f"{key} missing from POOL_SETTINGS_KEYS"
            assert key in POOL_SETTINGS_TO_BROADCAST, f"{key} missing from broadcast tuple"

    def test_persist_save_list_and_restore_block(self):
        """Save seam: both keys must be in the registry's LLM_CFG_PERSIST_KEYS (the list
        config_persist iterates to persist llm_cfg). Restore seam: both keys appear in the
        config_persist restore block."""
        from agent_cascade.settings_registry import LLM_CFG_PERSIST_KEYS
        src = Path('agent_cascade/pool/config_persist.py').read_text(encoding='utf-8')
        for key in _KEYS:
            assert key in LLM_CFG_PERSIST_KEYS, f"{key} missing from LLM_CFG_PERSIST_KEYS (save seam)"
            assert f"'{key}'" in src, f"{key} missing from config_persist save/restore"

    def test_initial_llm_cfg_seed(self):
        src = Path('agent_cascade/api_server.py').read_text(encoding='utf-8')
        for key in _KEYS:
            assert f"'{key}': True" in src, f"{key} missing/unseeded-True in api_server initial_llm_cfg"

    def test_registry_spec_llm_cfg_sourced_and_true(self):
        from agent_cascade.settings_registry import SETTINGS_REGISTRY
        for key in _KEYS:
            spec = SETTINGS_REGISTRY.get(key)
            assert spec is not None, f"{key} missing from SETTINGS_REGISTRY"
            assert spec.value_source == 'llm_cfg'
            assert spec.default is True
