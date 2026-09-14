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


class _FakeTelemetry:
    """Minimal stand-in exposing the four getters SystemInfo's dump reads."""

    def get_session_summary(self):
        return {
            "total_turns": 3,
            "total_llm_calls": 7,
            "total_tool_calls": 12,
            "total_input_tokens_est": 5000,
            "total_output_tokens_est": 1500,
            "avg_tps": 42.5,
            "total_retries": 1,
            "total_compressions": 0,
            "llm_calls_by_model": {"qwen3.8-27b": 5, "gemma-4-31b-it": 2},
            # RFC 9211 prompt-cache counters + derived ratios (0..1).
            "llm_cache_hits": 6,
            "llm_cache_misses": 1,
            "llm_cache_unknown": 0,
            "llm_cache_hit_ratio": 0.857,
            "llm_cache_classified_ratio": 1.0,
        }

    def get_config_comparison(self):
        return [{
            "config_fingerprint": "abc123def456",
            "config_description": {"model": "qwen3.8-27b", "api_base": "http://x"},
            "turns": 3,
            "llm_calls": 7,
            "avg_tps": 42.5,
        }]

    def get_agent_class_summary(self):
        return [{
            "agent_class": "orchestrator",
            "tool_usage_accuracy": 91.7,
            "total_time_sec": 12.3,
            "tokens_generated": 1500,
            "turns": 3,
        }]

    def get_skill_usage_summary(self):
        return [{
            "skill": "docker-best-practices",
            "loads": 4,
            "agent_classes": ["coder"],
            "top_mode": "auto",
        }]


class TestSystemInfoTelemetryDump:
    """Tests for the live `help='telemetry'` dump (computed from pool.telemetry)."""

    def _tool_with(self, telemetry):
        pool = _FakePool(server_info=("0.0.0.0", 8765))
        pool.telemetry = telemetry
        return SystemInfo(agent_pool=pool)

    def test_populated_dump_renders_all_sections(self):
        out = self._tool_with(_FakeTelemetry()).call('{"help": "telemetry"}')
        assert "AC Telemetry Dump" in out
        # Session totals + per-model breakdown
        assert "total_llm_calls: 7" in out
        assert "qwen3.8-27b: 5" in out
        # Prompt cache block (present because total_llm_calls > 0)
        assert "Prompt cache:" in out
        assert "hits=6  misses=1  unknown=0" in out
        assert "hit_ratio=85.7%" in out
        assert "measured=100.0%" in out
        # Config fingerprint A/B
        assert "abc123def456" in out
        assert "model=qwen3.8-27b" in out
        # Agent class usage (accuracy formatted as %)
        assert "orchestrator:" in out
        assert "91.7%" in out
        # Skill usage
        assert "docker-best-practices: loads=4" in out
        assert "top_mode=auto" in out
        # Export pointer
        assert "/api/telemetry/export" in out

    def test_case_insensitive(self):
        out = self._tool_with(_FakeTelemetry()).call('{"help": "TELEMETRY"}')
        assert "AC Telemetry Dump" in out

    def test_no_telemetry_degrades_gracefully(self):
        """Pool with no telemetry -> clear message, does not raise."""
        pool = _FakePool(server_info=("0.0.0.0", 8765))
        # No .telemetry attribute set at all
        out = SystemInfo(agent_pool=pool).call('{"help": "telemetry"}')
        assert "unavailable" in out

    def test_empty_telemetry_still_headers(self):
        """Telemetry present but all getters return empty -> headers + export only."""
        class _Empty:
            def get_session_summary(self):
                return {}
            def get_config_comparison(self):
                return []
            def get_agent_class_summary(self):
                return []
            def get_skill_usage_summary(self):
                return []
        out = self._tool_with(_Empty()).call('{"help": "telemetry"}')
        assert "AC Telemetry Dump" in out
        assert "/api/telemetry/export" in out

    def test_getter_exception_degrades_gracefully(self):
        """A getter raising is caught -> error string, does not propagate."""
        class _Boom:
            def get_session_summary(self):
                raise RuntimeError("boom")
            def get_config_comparison(self):
                return []
            def get_agent_class_summary(self):
                return []
            def get_skill_usage_summary(self):
                return []
        out = self._tool_with(_Boom()).call('{"help": "telemetry"}')
        assert "unavailable" in out

    def test_prompt_cache_block_present_when_llm_calls(self):
        """Cache stats + total_llm_calls>0 -> block with % ratios."""
        class _WithCache:
            def get_session_summary(self):
                return {
                    "total_llm_calls": 4,
                    "llm_cache_hits": 3,
                    "llm_cache_misses": 1,
                    "llm_cache_unknown": 0,
                    "llm_cache_hit_ratio": 0.75,
                    "llm_cache_classified_ratio": 0.5,
                }
            def get_config_comparison(self): return []
            def get_agent_class_summary(self): return []
            def get_skill_usage_summary(self): return []
        out = self._tool_with(_WithCache()).call('{"help": "telemetry"}')
        assert "Prompt cache:" in out
        assert "hits=3  misses=1  unknown=0" in out
        assert "hit_ratio=75.0%" in out
        assert "measured=50.0%" in out

    def test_prompt_cache_block_absent_when_no_llm_calls(self):
        """total_llm_calls==0 -> block is NOT printed (no noise on fresh sessions)."""
        class _NoCalls:
            def get_session_summary(self):
                return {
                    "total_turns": 1,
                    "total_llm_calls": 0,
                    "llm_cache_hits": 0,
                    "llm_cache_misses": 0,
                    "llm_cache_unknown": 0,
                    "llm_cache_hit_ratio": None,
                    "llm_cache_classified_ratio": None,
                }
            def get_config_comparison(self): return []
            def get_agent_class_summary(self): return []
            def get_skill_usage_summary(self): return []
        out = self._tool_with(_NoCalls()).call('{"help": "telemetry"}')
        assert "Prompt cache:" not in out

    def test_prompt_cache_none_ratios_render_na(self):
        """Ratios None (but calls>0) -> 'n/a' instead of crashing."""
        class _NoneRatio:
            def get_session_summary(self):
                return {
                    "total_llm_calls": 2,
                    "llm_cache_hits": 0,
                    "llm_cache_misses": 0,
                    "llm_cache_unknown": 2,
                    "llm_cache_hit_ratio": None,
                    "llm_cache_classified_ratio": None,
                }
            def get_config_comparison(self): return []
            def get_agent_class_summary(self): return []
            def get_skill_usage_summary(self): return []
        out = self._tool_with(_NoneRatio()).call('{"help": "telemetry"}')
        assert "Prompt cache:" in out
        assert "hit_ratio=n/a" in out
        assert "measured=n/a" in out
