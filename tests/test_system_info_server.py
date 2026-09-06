"""Unit tests for the AC Server address section in the system_info tool.

Covers the three resolution paths added to SystemInfo.call():
  1. agent_pool.server_info present and valid -> reported as-is.
  2. server_info missing + QWEN_AGENT_PORT env set -> env port used.
  3. server_info missing + no env var -> default 8765 (never raises).

The fake pool is injected the same way SystemInfo resolves it: via the
constructor `agent_pool` parameter, which is stored as self.agent_pool.
No real server is started.
"""
import pytest

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
