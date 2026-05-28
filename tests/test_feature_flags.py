"""Tests for config.unified feature flags.

After Phase 8 cleanup, all flags are set to True permanently with no
environment variable overrides. These tests verify that behavior.
"""

import sys


def _reimport_unified():
    """Force a fresh import of config.unified so we get the current state."""
    for mod_name in list(sys.modules):
        if mod_name == "config.unified" or mod_name.startswith("config.unified."):
            del sys.modules[mod_name]
    return __import__("config.unified", fromlist=["USE_UNIFIED_STATE"])


# ===========================================================================
# Flags are permanently True (no env var override)
# ===========================================================================

class TestPermanentlyEnabled:
    """Both feature flags should be hardcoded to True after Phase 8."""

    def test_state_is_true(self, clear_feature_env_vars):
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_STATE is True

    def test_loop_is_true(self, clear_feature_env_vars):
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_LOOP is True

    def test_both_are_true_together(self, clear_feature_env_vars):
        """Convenience: verify both flags in one shot."""
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_STATE is True
        assert mod.USE_UNIFIED_LOOP is True

    def test_flags_are_boolean_not_string(self, clear_feature_env_vars):
        """Ensure the flags are actual booleans, not truthy strings."""
        mod = _reimport_unified()
        assert type(mod.USE_UNIFIED_STATE) is bool
        assert type(mod.USE_UNIFIED_LOOP) is bool

    def test_env_var_does_not_override_state(self, clear_feature_env_vars, env_patch):
        """Setting the old env var has no effect since flags are hardcoded."""
        env_patch("AC_USE_UNIFIED_STATE", "0")
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_STATE is True  # Still True despite env var

    def test_env_var_does_not_override_loop(self, clear_feature_env_vars, env_patch):
        """Setting the old env var has no effect since flags are hardcoded."""
        env_patch("AC_USE_UNIFIED_LOOP", "0")
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_LOOP is True  # Still True despite env var


# ===========================================================================
# Module __all__ export list
# ===========================================================================

class TestModuleExports:
    """Verify the module's public API."""

    def test_all_exports(self, clear_feature_env_vars):
        mod = _reimport_unified()
        assert "USE_UNIFIED_STATE" in mod.__all__
        assert "USE_UNIFIED_LOOP" in mod.__all__
        # USE_UNIFIED_ARCHITECTURE was removed (dead code) in Phase 8
        assert "USE_UNIFIED_ARCHITECTURE" not in mod.__all__