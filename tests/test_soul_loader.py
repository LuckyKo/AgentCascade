"""Unit tests for agent_cascade.soul_loader.

Covers load_soul, build_system_prompt, _preprocess_soul_content, and
_format_value with well-formed soul files, malformed input, and edge cases.
"""

import sys
from pathlib import Path

import pytest
import yaml

# Ensure the project root is on sys.path so imports resolve
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.soul_loader import (
    _format_value,
    _preprocess_soul_content,
    build_system_prompt,
    load_soul,
)

# ---------------------------------------------------------------------------
# Paths to the real soul files
# ---------------------------------------------------------------------------
_AGENTS_DIR = PROJECT_ROOT / "agents"

SOUL_FILES = {
    "orchestrator": _AGENTS_DIR / "orchestrator_soul.md",
    "coder": _AGENTS_DIR / "coder_soul.md",
    "security": _AGENTS_DIR / "Security_soul.md",
    "researcher": _AGENTS_DIR / "researcher_soul.md",
}


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(params=list(SOUL_FILES.keys()))
def soul_file(request):
    """Parameterized fixture yielding each real soul file path."""
    return SOUL_FILES[request.param]


@pytest.fixture
def temp_soul_file(tmp_path):
    """Helper to write content to a temp .md file and return its path."""
    def _write(content: str) -> str:
        p = tmp_path / "test_soul.md"
        p.write_text(content, encoding="utf-8")
        return str(p)
    return _write


# ===========================================================================
# 1. Good (well-formatted) soul files
# ===========================================================================

class TestLoadSoulGoodFiles:
    """load_soul with the four real soul files."""

    def test_load_returns_dict(self, soul_file):
        config = load_soul(str(soul_file))
        assert isinstance(config, dict)

    def test_load_has_name(self, soul_file):
        config = load_soul(str(soul_file))
        assert "name" in config
        assert isinstance(config["name"], str)

    def test_load_has_identity(self, soul_file):
        config = load_soul(str(soul_file))
        assert "identity" in config

    def test_load_has_communication(self, soul_file):
        config = load_soul(str(soul_file))
        assert "communication" in config


class TestBuildSystemPromptGoodFiles:
    """build_system_prompt with real soul files."""

    def test_prompt_non_empty(self, soul_file):
        config = load_soul(str(soul_file))
        prompt = build_system_prompt(config)
        assert len(prompt) > 0

    def test_prompt_contains_who_you_are(self, soul_file):
        config = load_soul(str(soul_file))
        prompt = build_system_prompt(config)
        assert "## Who You Are" in prompt

    def test_prompt_contains_how_you_communicate(self, soul_file):
        config = load_soul(str(soul_file))
        prompt = build_system_prompt(config)
        assert "## How You Communicate" in prompt

    def test_prompt_contains_role(self, soul_file):
        config = load_soul(str(soul_file))
        prompt = build_system_prompt(config)
        role = config.get("identity", {}).get("role", "")
        if role:
            assert role in prompt

    def test_prompt_contains_mission(self, soul_file):
        config = load_soul(str(soul_file))
        prompt = build_system_prompt(config)
        mission = config.get("identity", {}).get("mission", "")
        if mission:
            assert mission in prompt

    def test_prompt_contains_principles(self, soul_file):
        config = load_soul(str(soul_file))
        prompt = build_system_prompt(config)
        principles = config.get("communication", {}).get("principles", [])
        if principles:
            assert "Principles" in prompt


# ===========================================================================
# 2. Badly formatted soul files (temp files)
# ===========================================================================

class TestLoadSoulBadFiles:
    """load_soul with various malformed / edge-case inputs."""

    def test_trailing_whitespace(self, temp_soul_file):
        content = "name: Tester  \ntagline: A test agent  \nidentity:\n  role: Tester  \n"
        path = temp_soul_file(content)
        config = load_soul(path)
        assert config["name"] == "Tester"

    def test_continuation_lines(self, temp_soul_file):
        content = (
            "name: Tester\n"
            "rules:\n"
            "  - First rule\n"
            "      continued here\n"
        )
        path = temp_soul_file(content)
        config = load_soul(path)
        assert isinstance(config["rules"], list)

    def test_irregular_nested_indent(self, temp_soul_file):
        content = (
            "name: Tester\n"
            "rules:\n"
            "  - Rule one\n"
            "    - Sub one\n"
            "        - Deep one\n"
        )
        path = temp_soul_file(content)
        config = load_soul(path)
        # Should parse without error
        assert isinstance(config["rules"], list)

    def test_colons_in_list_items(self, temp_soul_file):
        content = (
            "name: Tester\n"
            "rules:\n"
            "  - Use colons: always\n"
        )
        path = temp_soul_file(content)
        config = load_soul(path)
        assert isinstance(config["rules"], list)

    def test_yaml_list_only(self, temp_soul_file):
        content = "- one\n- two\n- three\n"
        path = temp_soul_file(content)
        with pytest.raises(ValueError, match="YAML mapping"):
            load_soul(path)

    def test_plain_string(self, temp_soul_file):
        content = "just a plain string\n"
        path = temp_soul_file(content)
        with pytest.raises(ValueError, match="YAML mapping"):
            load_soul(path)

    def test_missing_file(self):
        with pytest.raises(FileNotFoundError, match="Soul file not found"):
            load_soul("/no/such/path.md")

    def test_malformed_yaml(self, temp_soul_file):
        content = "name: [unclosed bracket\n"
        path = temp_soul_file(content)
        with pytest.raises(yaml.YAMLError):
            load_soul(path)


# ===========================================================================
# 3. build_system_prompt edge cases
# ===========================================================================

class TestBuildSystemPromptEdgeCases:

    def test_empty_config(self):
        prompt = build_system_prompt({})
        assert "You are Assistant" in prompt

    def test_identity_as_string(self):
        config = {"identity": "just a string"}
        prompt = build_system_prompt(config)
        assert len(prompt) > 0

    def test_identity_as_list(self):
        config = {"identity": ["a", "b"]}
        prompt = build_system_prompt(config)
        assert len(prompt) > 0

    def test_communication_as_string(self):
        config = {"communication": "chatty"}
        prompt = build_system_prompt(config)
        assert len(prompt) > 0

    def test_capabilities_as_string(self):
        config = {"capabilities": "lots of tools"}
        prompt = build_system_prompt(config)
        assert len(prompt) > 0

    def test_role_mission_as_integers(self):
        config = {"identity": {"role": 42, "mission": 99}}
        prompt = build_system_prompt(config)
        assert "## Who You Are" in prompt

    def test_notes_as_list(self):
        config = {"notes": ["note1", "note2"]}
        prompt = build_system_prompt(config)
        assert len(prompt) > 0

    def test_whitespace_only_notes(self):
        config = {"notes": "   \n  "}
        prompt = build_system_prompt(config)
        assert "## Remember" not in prompt


# ===========================================================================
# 4. _preprocess_soul_content edge cases
# ===========================================================================

class TestPreprocessSoulContent:

    def test_empty_string(self):
        result = _preprocess_soul_content("")
        assert result == ""

    def test_multi_level_nesting_preserved(self):
        content = (
            "- Level 1\n"
            "  - Level 2\n"
            "    - Level 3\n"
        )
        result = _preprocess_soul_content(content)
        lines = result.strip().split('\n')
        assert lines[0] == "- Level 1"
        assert lines[1] == "  - Level 2"
        assert lines[2] == "    - Level 3"

    def test_blank_line_resets_indent_stack(self):
        content = (
            "- Item A\n"
            "  - Sub A\n"
            "\n"
            "- Item B\n"
        )
        result = _preprocess_soul_content(content)
        assert "- Item B" in result


# ===========================================================================
# 5. _format_value edge cases
# ===========================================================================

class TestFormatValue:

    def test_empty_list(self):
        result = _format_value([])
        assert result == ""

    def test_empty_dict(self):
        result = _format_value({})
        assert result == ""

    def test_none(self):
        result = _format_value(None)
        assert result == "None\n"

    def test_scalar_string(self):
        result = _format_value("hello")
        assert "hello" in result

    def test_scalar_int(self):
        result = _format_value(42)
        assert "42" in result

    def test_nested_lists(self):
        result = _format_value([["a", "b"]])
        assert "a" in result

    def test_dict_in_list(self):
        result = _format_value([{"key": "value"}])
        assert "Key" in result