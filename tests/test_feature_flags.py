"""Tests for config.unified feature flags.

Verifies that all three flags default to False and can be overridden via
environment variables. Uses reimport logic because the module reads env vars at
import time.
"""

import importlib
import sys


def _reimport_unified():
    """Force a fresh import of config.unified so env var changes take effect."""
    # Remove cached modules so they re-read environment variables on next import
    for mod_name in list(sys.modules):
        if mod_name == "config.unified" or mod_name.startswith("config.unified."):
            del sys.modules[mod_name]
    return __import__("config.unified", fromlist=["USE_UNIFIED_ARCHITECTURE"])


# ===========================================================================
# Default values (no env vars set)
# ===========================================================================

class TestDefaults:
    """All three feature flags should default to False."""

    def test_architecture_defaults_to_false(self, clear_feature_env_vars):
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_ARCHITECTURE is False

    def test_state_defaults_to_false(self, clear_feature_env_vars):
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_STATE is False

    def test_loop_defaults_to_false(self, clear_feature_env_vars):
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_LOOP is False

    def test_all_three_default_together(self, clear_feature_env_vars):
        """Convenience: verify all three in one shot."""
        mod = _reimport_unified()
        assert not mod.USE_UNIFIED_ARCHITECTURE
        assert not mod.USE_UNIFIED_STATE
        assert not mod.USE_UNIFIED_LOOP

    def test_flags_are_boolean_not_string(self, clear_feature_env_vars):
        """Ensure the flags are actual booleans, not truthy strings."""
        mod = _reimport_unified()
        assert type(mod.USE_UNIFIED_ARCHITECTURE) is bool
        assert type(mod.USE_UNIFIED_STATE) is bool
        assert type(mod.USE_UNIFIED_LOOP) is bool


# ===========================================================================
# Environment variable override: enable each flag individually
# ===========================================================================

class TestEnvOverrideEnable:
    """Setting the corresponding env var to '1' enables the flag."""

    def test_enable_architecture(self, clear_feature_env_vars, env_patch):
        env_patch("AC_USE_UNIFIED_ARCHITECTURE", "1")
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_ARCHITECTURE is True
        # Other flags should remain off
        assert mod.USE_UNIFIED_STATE is False
        assert mod.USE_UNIFIED_LOOP is False

    def test_enable_state(self, clear_feature_env_vars, env_patch):
        env_patch("AC_USE_UNIFIED_STATE", "1")
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_STATE is True
        assert mod.USE_UNIFIED_ARCHITECTURE is False
        assert mod.USE_UNIFIED_LOOP is False

    def test_enable_loop(self, clear_feature_env_vars, env_patch):
        env_patch("AC_USE_UNIFIED_LOOP", "1")
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_LOOP is True
        assert mod.USE_UNIFIED_ARCHITECTURE is False
        assert mod.USE_UNIFIED_STATE is False

    def test_enable_all_three(self, clear_feature_env_vars, env_patch):
        """All three flags can be enabled simultaneously."""
        env_patch("AC_USE_UNIFIED_ARCHITECTURE", "1")
        env_patch("AC_USE_UNIFIED_STATE", "1")
        env_patch("AC_USE_UNIFIED_LOOP", "1")
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_ARCHITECTURE is True
        assert mod.USE_UNIFIED_STATE is True
        assert mod.USE_UNIFIED_LOOP is True


# ===========================================================================
# Environment variable override: non-"1" values should NOT enable the flag
# ===========================================================================

class TestEnvOverrideNonEnable:
    """Only the value '1' enables a flag; other truthy strings should not."""

    def test_value_true_does_not_enable(self, clear_feature_env_vars, env_patch):
        """String 'true' (lowercase) is NOT treated as enabled."""
        env_patch("AC_USE_UNIFIED_ARCHITECTURE", "true")
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_ARCHITECTURE is False

    def test_value_yes_does_not_enable(self, clear_feature_env_vars, env_patch):
        env_patch("AC_USE_UNIFIED_STATE", "yes")
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_STATE is False

    def test_value_2_does_not_enable(self, clear_feature_env_vars, env_patch):
        """Only '1' is truthy — not '2'."""
        env_patch("AC_USE_UNIFIED_LOOP", "2")
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_LOOP is False

    def test_empty_string_does_not_enable(self, clear_feature_env_vars, env_patch):
        """Empty string should not enable the flag."""
        env_patch("AC_USE_UNIFIED_ARCHITECTURE", "")
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_ARCHITECTURE is False

    def test_value_0_does_not_enable(self, clear_feature_env_vars, env_patch):
        """Explicit '0' should keep the flag off."""
        env_patch("AC_USE_UNIFIED_ARCHITECTURE", "0")
        mod = _reimport_unified()
        assert mod.USE_UNIFIED_ARCHITECTURE is False


# ===========================================================================
# Module __all__ export list
# ===========================================================================

class TestModuleExports:
    """Verify the module's public API."""

    def test_all_exports(self, clear_feature_env_vars):
        mod = _reimport_unified()
        assert "USE_UNIFIED_ARCHITECTURE" in mod.__all__
        assert "USE_UNIFIED_STATE" in mod.__all__
        assert "USE_UNIFIED_LOOP" in mod.__all__