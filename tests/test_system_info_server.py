"""Unit tests for the system_info tool: AC Server address + help mode.

Covers:
  - The three AC server resolution paths (server_info, env, default).
  - The optional `help` argument that renders sections from config/ac_system_help.yaml.

The fake pool is injected the same way SystemInfo resolves it: via the
constructor `agent_pool` parameter, which is stored as self.agent_pool.
No real server is started.
"""
import pytest

from agent_cascade.tools.custom import system_info as system_info_module
from agent_cascade.tools.custom.system_info import SystemInfo


class _FakePool:
    """Minimal stand-in for AgentPool exposing only what SystemInfo reads."""

    def __init__(self, server_info=None):
        self.server_info = server_info
        # Attributes SystemInfo may touch; keep them inert so the tool runs.
        self.instance_conversations = {}
        self.operation_manager = None
        self.get_instance = lambda name: None
        self.get_conversation = lambda name: []


def _run(server_info, monkeypatch=None):
    """Build a SystemInfo with a fake pool and return its output string."""
    tool = SystemInfo(agent_pool=_FakePool(server_info=server_info), agent_name="orchestrator")
    return tool.call("")


class TestSystemInfoServer:
    def test_server_info_tuple_reported(self):
        """A valid (host, port) tuple is rendered as http://host:port."""
        out = _run(("0.0.0.0", 9999))
        assert "http://0.0.0.0:9999" in out
        # 0.0.0.0 binds all interfaces -> note is appended.
        assert "(all interfaces)" in out

    def test_server_info_none_env_port_used(self, monkeypatch):
        """server_info None + QWEN_AGENT_PORT set -> env port appears."""
        monkeypatch.setenv("QWEN_AGENT_PORT", "7777")
        out = _run(None)
        assert "http://0.0.0.0:7777" in out

    def test_server_info_none_no_env_default(self, monkeypatch):
        """server_info None + no env var -> default 8765, tool does not raise."""
        monkeypatch.delenv("QWEN_AGENT_PORT", raising=False)
        out = _run(None)
        assert "http://0.0.0.0:8765" in out

    def test_malformed_server_info_does_not_raise(self, monkeypatch):
        """A malformed server_info (wrong arity / falsy) falls back safely."""
        monkeypatch.delenv("QWEN_AGENT_PORT", raising=False)
        for bad in [("0.0.0.0",), ("", 9999), "not-a-tuple"]:
            out = _run(bad)
            # Falls back to default port; must not crash the tool.
            assert "http://0.0.0.0:8765" in out

    def test_non_numeric_env_port_falls_back(self, monkeypatch):
        """A non-numeric QWEN_AGENT_PORT does not raise; defaults to 8765."""
        monkeypatch.setenv("QWEN_AGENT_PORT", "not-a-number")
        out = _run(None)
        assert "http://0.0.0.0:8765" in out

    def test_pool_none_does_not_raise(self, monkeypatch):
        """No agent_pool at all -> still resolves via env/default without error."""
        monkeypatch.delenv("QWEN_AGENT_PORT", raising=False)
        tool = SystemInfo(agent_pool=None, agent_name="orchestrator")
        out = tool.call("")
        assert "http://0.0.0.0:8765" in out


class TestSystemInfoHelp:
    """Tests for the optional `help` argument that renders YAML help sections."""

    def _make_tool(self):
        return SystemInfo(agent_pool=None)

    def test_help_rest_api_section(self, monkeypatch):
        """help='rest_api' returns the section with header and known endpoints."""
        out = self._make_tool().call('{"help": "rest_api"}')
        assert "AC Help: rest_api" in out
        assert "/api/keys" in out

    def test_help_unknown_section_lists_valid(self, monkeypatch):
        """A bogus help value returns usage guidance listing valid sections."""
        out = self._make_tool().call('{"help": "nope"}')
        assert "Unknown" in out
        assert "rest_api" in out  # at least one valid section listed

    def test_no_help_returns_normal_info(self):
        """Empty/absent help -> normal system info (no 'AC Help:')."""
        out = self._make_tool().call("")
        assert "AC Help:" not in out
        assert "System Information" in out

    def test_missing_file_graceful_error(self, monkeypatch, tmp_path):
        """Pointing to a nonexistent file returns an error string, does not raise."""
        fake_path = tmp_path / "nonexistent_help.yaml"
        monkeypatch.setattr(system_info_module, "_help_file_path", lambda: fake_path)
        out = self._make_tool().call('{"help": "rest_api"}')
        assert "not found" in out

    def test_json_string_params_work(self):
        """params as JSON string with help works the same as dict form."""
        tool = self._make_tool()
        out_str = tool.call('{"help": "websocket"}')
        out_dict = tool.call({"help": "websocket"})
        assert out_str == out_dict
        assert "AC Help: websocket" in out_str

    def test_case_insensitive_match(self):
        """Section matching is case-insensitive."""
        out = self._make_tool().call('{"help": "REST_API"}')
        assert "AC Help: rest_api" in out
