"""
Unit tests for instance separation feature (agent_cascade.instance_id).

Tests validation, retrieval, and path helper functions.
"""

import os
import pytest
from pathlib import Path

from agent_cascade.instance_id import (
    validate_instance_id,
    get_instance_id,
    get_instance_suffix,
    make_instance_dir,
)


class TestValidateInstanceId:
    """Tests for validate_instance_id()."""

    def test_validate_valid_simple(self):
        assert validate_instance_id("prod") == "prod"

    def test_validate_valid_with_underscore(self):
        assert validate_instance_id("dev_1") == "dev_1"

    def test_validate_valid_alphanumeric_mixed(self):
        assert validate_instance_id("AC_backup") == "AC_backup"

    def test_validate_valid_all_lowercase(self):
        assert validate_instance_id("staging") == "staging"

    def test_validate_valid_numbers_only(self):
        assert validate_instance_id("123") == "123"

    def test_validate_valid_max_length(self):
        """Exactly 64 characters should be accepted."""
        valid_id = "a" * 64
        assert validate_instance_id(valid_id) == valid_id

    def test_validate_invalid_hyphen(self):
        with pytest.raises(ValueError, match="Invalid instance ID"):
            validate_instance_id("prod-server")

    def test_validate_invalid_dot(self):
        with pytest.raises(ValueError, match="Invalid instance ID"):
            validate_instance_id("dev.v2")

    def test_validate_invalid_space(self):
        with pytest.raises(ValueError, match="Invalid instance ID"):
            validate_instance_id("my instance")

    def test_validate_invalid_special_chars(self):
        with pytest.raises(ValueError, match="Invalid instance ID"):
            validate_instance_id("prod@server#1")

    def test_validate_too_long(self):
        with pytest.raises(ValueError, match="exceeds maximum length"):
            validate_instance_id("a" * 65)

    def test_validate_empty_string(self):
        """Empty string returns empty string (legacy mode)."""
        assert validate_instance_id("") == ""

    def test_validate_none(self):
        """None input returns empty string."""
        assert validate_instance_id(None) == ""

    def test_validate_whitespace_only(self):
        """Whitespace-only input treated as empty (legacy mode)."""
        assert validate_instance_id("   ") == ""

    def test_validate_leading_trailing_spaces_stripped(self):
        """Leading/trailing spaces are stripped."""
        assert validate_instance_id("  prod  ") == "prod"


class TestGetInstanceId:
    """Tests for get_instance_id()."""

    def test_get_instance_id_default(self, monkeypatch):
        """Returns empty string when env var not set."""
        monkeypatch.delenv("AGENT_CASCADE_INSTANCE_ID", raising=False)
        assert get_instance_id() == ""

    def test_get_instance_id_custom(self, monkeypatch):
        """Returns value from env var."""
        monkeypatch.setenv("AGENT_CASCADE_INSTANCE_ID", "prod")
        assert get_instance_id() == "prod"


class TestGetInstanceSuffix:
    """Tests for get_instance_suffix()."""

    def test_get_instance_suffix_no_instance(self, monkeypatch):
        """Returns empty string when no instance ID set."""
        monkeypatch.delenv("AGENT_CASCADE_INSTANCE_ID", raising=False)
        assert get_instance_suffix() == ""

    def test_get_instance_suffix_with_instance(self, monkeypatch):
        """Returns '_<instance_id>' suffix when instance ID is set."""
        monkeypatch.setenv("AGENT_CASCADE_INSTANCE_ID", "prod")
        assert get_instance_suffix() == "_prod"


class TestMakeInstanceDir:
    """Tests for make_instance_dir()."""

    def test_make_instance_dir_no_instance(self, monkeypatch):
        """Returns base path unchanged when no instance ID."""
        monkeypatch.delenv("AGENT_CASCADE_INSTANCE_ID", raising=False)
        assert make_instance_dir("workspace/telemetry") == "workspace/telemetry"

    def test_make_instance_dir_with_instance_posix(self, monkeypatch):
        """Appends instance suffix correctly for POSIX-style paths."""
        monkeypatch.setenv("AGENT_CASCADE_INSTANCE_ID", "prod")
        assert make_instance_dir("workspace/telemetry") == "workspace/telemetry_prod"

    def test_make_instance_dir_with_instance_nested(self, monkeypatch):
        """Works with deeply nested paths."""
        monkeypatch.setenv("AGENT_CASCADE_INSTANCE_ID", "dev")
        result = make_instance_dir("/some/path/logs")
        assert result == "/some/path/logs_dev"

    def test_make_instance_dir_with_instance_windows_style(self, monkeypatch):
        """Handles Windows-style paths correctly — normalized to forward slashes."""
        monkeypatch.setenv("AGENT_CASCADE_INSTANCE_ID", "test")
        result = make_instance_dir("workspace\\logs")
        assert result == "workspace/logs_test"

    def test_make_instance_dir_empty_suffix(self, monkeypatch):
        """Empty instance ID returns original path."""
        monkeypatch.setenv("AGENT_CASCADE_INSTANCE_ID", "")
        assert make_instance_dir("config/settings") == "config/settings"