"""Unit tests for the system_info tool: AC Server address + help mode.

Covers:
  - The three AC server resolution paths (server_info, env, default).
  - The optional `help` argument that renders sections from config/ac_system_help.yaml.

The fake pool is injected the same way SystemInfo resolves it: via the
constructor `agent_pool` parameter, which is stored as self.agent_pool.
No real server is started.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import yaml

from agent_cascade.agent_instance import AgentState
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
    tool = SystemInfo(agent_pool=_FakePool(server_info=server_info), agent_name='orchestrator')
    return tool.call('')


class TestSystemInfoServer:

    def test_server_info_tuple_reported(self):
        """A valid (host, port) tuple is rendered as http://host:port."""
        out = _run(('0.0.0.0', 9999))
        assert 'http://0.0.0.0:9999' in out
        # 0.0.0.0 binds all interfaces -> note is appended.
        assert '(all interfaces)' in out

    def test_server_info_none_env_port_used(self, monkeypatch):
        """server_info None + AGENT_CASCADE_PORT set -> env port appears."""
        monkeypatch.setenv('AGENT_CASCADE_PORT', '7777')
        out = _run(None)
        assert 'http://0.0.0.0:7777' in out

    def test_server_info_none_no_env_default(self, monkeypatch):
        """server_info None + no env var -> default 8765, tool does not raise."""
        monkeypatch.delenv('AGENT_CASCADE_PORT', raising=False)
        out = _run(None)
        assert 'http://0.0.0.0:8765' in out

    def test_malformed_server_info_does_not_raise(self, monkeypatch):
        """A malformed server_info (wrong arity / falsy) falls back safely."""
        monkeypatch.delenv('AGENT_CASCADE_PORT', raising=False)
        for bad in [('0.0.0.0',), ('', 9999), 'not-a-tuple']:
            out = _run(bad)
            # Falls back to default port; must not crash the tool.
            assert 'http://0.0.0.0:8765' in out

    def test_non_numeric_env_port_falls_back(self, monkeypatch):
        """A non-numeric AGENT_CASCADE_PORT does not raise; defaults to 8765."""
        monkeypatch.setenv('AGENT_CASCADE_PORT', 'not-a-number')
        out = _run(None)
        assert 'http://0.0.0.0:8765' in out

    def test_pool_none_does_not_raise(self, monkeypatch):
        """No agent_pool at all -> still resolves via env/default without error."""
        monkeypatch.delenv('AGENT_CASCADE_PORT', raising=False)
        tool = SystemInfo(agent_pool=None, agent_name='orchestrator')
        out = tool.call('')
        assert 'http://0.0.0.0:8765' in out


class TestSystemInfoMcpServers:
    """Tests for the enabled-MCP-servers section (name + description; disabled excluded)."""

    def _tool(self):
        return SystemInfo(agent_pool=None)

    def test_enabled_listed_disabled_excluded_description_shown(self, monkeypatch, tmp_path):
        """Enabled servers appear (with description if present); disabled ones are omitted."""
        cfg = tmp_path / 'mcp_servers.json'
        cfg.write_text(json.dumps({
            'chrome-devtools': {'command': 'npx', 'args': ['-y', 'x'], 'enabled': True,
                               'description': 'Chrome DevTools MCP'},
            'blender': {'command': 'uvx', 'args': [], 'enabled': False},
            'no-flag': {'command': 'node', 'args': []},  # enabled absent -> defaults to enabled
        }), encoding='utf-8')
        monkeypatch.setattr(system_info_module, '_mcp_servers_path', lambda: cfg)
        out = self._tool()._render_mcp_servers()
        assert 'chrome-devtools — Chrome DevTools MCP' in out
        assert 'no-flag' in out
        assert 'blender' not in out

    def test_missing_config_reports_none(self, monkeypatch, tmp_path):
        """A missing config file degrades to a clear message, does not raise."""
        monkeypatch.setattr(system_info_module, '_mcp_servers_path', lambda: tmp_path / 'nope.json')
        assert 'no MCP servers configured' in self._tool()._render_mcp_servers()

    def test_all_disabled_reports_none(self, monkeypatch, tmp_path):
        """All servers disabled -> 'no MCP servers enabled'."""
        cfg = tmp_path / 'mcp_servers.json'
        cfg.write_text(json.dumps({'x': {'command': 'npx', 'enabled': False}}), encoding='utf-8')
        monkeypatch.setattr(system_info_module, '_mcp_servers_path', lambda: cfg)
        assert 'no MCP servers enabled' in self._tool()._render_mcp_servers()

    def test_mcp_section_in_full_output(self, monkeypatch, tmp_path):
        """The --- MCP Servers --- section appears in the normal call() output."""
        cfg = tmp_path / 'mcp_servers.json'
        cfg.write_text(json.dumps({'chrome-devtools': {'command': 'npx', 'enabled': True}}),
                       encoding='utf-8')
        monkeypatch.setattr(system_info_module, '_mcp_servers_path', lambda: cfg)
        out = self._tool().call('')
        assert '--- MCP Servers ---' in out
        assert 'chrome-devtools' in out

    def test_non_string_description_does_not_crash(self, monkeypatch, tmp_path):
        """A non-string description (e.g. a number) is coerced to '' — no crash."""
        cfg = tmp_path / 'mcp_servers.json'
        cfg.write_text(json.dumps({'x': {'command': 'npx', 'enabled': True, 'description': 123}}),
                       encoding='utf-8')
        monkeypatch.setattr(system_info_module, '_mcp_servers_path', lambda: cfg)
        out = self._tool()._render_mcp_servers()
        assert 'x' in out
        assert '123' not in out  # the numeric description is dropped, not rendered

    def test_corrupt_config_reports_unreadable(self, monkeypatch, tmp_path):
        """A corrupt/truncated config file degrades to a clear message, does not raise."""
        cfg = tmp_path / 'mcp_servers.json'
        cfg.write_text('{ not json', encoding='utf-8')
        monkeypatch.setattr(system_info_module, '_mcp_servers_path', lambda: cfg)
        assert 'MCP config unreadable' in self._tool()._render_mcp_servers()

    def test_non_dict_server_entry_skipped(self, monkeypatch, tmp_path):
        """A non-dict server entry is skipped gracefully (no crash)."""
        cfg = tmp_path / 'mcp_servers.json'
        cfg.write_text(json.dumps({'x': 'notadict'}), encoding='utf-8')
        monkeypatch.setattr(system_info_module, '_mcp_servers_path', lambda: cfg)
        assert 'no MCP servers enabled' in self._tool()._render_mcp_servers()


class TestSystemInfoHelp:
    """Tests for the optional `help` argument that renders YAML help sections."""

    def _make_tool(self):
        return SystemInfo(agent_pool=None)

    def test_help_rest_api_section(self, monkeypatch):
        """help='rest_api' returns the section with header and known endpoints."""
        out = self._make_tool().call('{"help": "rest_api"}')
        assert 'AC Help: rest_api' in out
        assert '/api/keys' in out

    def test_help_unknown_section_lists_valid(self, monkeypatch):
        """A bogus help value returns usage guidance listing valid sections."""
        out = self._make_tool().call('{"help": "nope"}')
        assert 'Unknown' in out
        assert 'rest_api' in out  # at least one valid section listed

    def test_no_help_returns_normal_info(self):
        """Empty/absent help -> normal system info (no 'AC Help:')."""
        out = self._make_tool().call('')
        assert 'AC Help:' not in out
        assert 'System Information' in out

    def test_missing_file_graceful_error(self, monkeypatch, tmp_path):
        """Pointing to a nonexistent file returns an error string, does not raise."""
        fake_path = tmp_path / 'nonexistent_help.yaml'
        monkeypatch.setattr(system_info_module, '_help_file_path', lambda: fake_path)
        out = self._make_tool().call('{"help": "rest_api"}')
        assert 'not found' in out

    def test_json_string_params_work(self):
        """params as JSON string with help works the same as dict form."""
        tool = self._make_tool()
        out_str = tool.call('{"help": "websocket"}')
        out_dict = tool.call({'help': 'websocket'})
        assert out_str == out_dict
        assert 'AC Help: websocket' in out_str

    def test_case_insensitive_match(self):
        """Section matching is case-insensitive."""
        out = self._make_tool().call('{"help": "REST_API"}')
        assert 'AC Help: rest_api' in out


class _FakeTelemetry:
    """Minimal stand-in exposing the four getters SystemInfo's dump reads."""

    def get_session_summary(self):
        return {
            'total_runs': 3,
            'total_llm_calls': 7,
            'total_tool_calls': 12,
            'total_input_tokens_est': 5000,
            'total_output_tokens_est': 1500,
            'avg_tps': 42.5,
            'total_retries': 1,
            'total_compressions': 0,
            'llm_calls_by_model': {
                'qwen3.8-27b': 5,
                'gemma-4-31b-it': 2
            },
            # RFC 9211 prompt-cache counters + derived ratios (0..1).
            'llm_cache_hits': 6,
            'llm_cache_misses': 1,
            'llm_cache_unknown': 0,
            'llm_cache_hit_ratio': 0.857,
            'llm_cache_classified_ratio': 1.0,
            'llm_wasted_tokens': 12345,
            'llm_waste_count': 2,
        }

    def get_config_comparison(self):
        return [{
            'config_fingerprint': 'abc123def456',
            'config_description': {
                'model': 'qwen3.8-27b',
                'api_base': 'http://x'
            },
            'runs': 3,
            'llm_calls': 7,
            'cache_hits': 5,
            'cache_misses': 2,
            'cache_hit_ratio': 0.714,
            'avg_tps': 42.5,
        }]

    def get_cache_attribution(self):
        # BUG_0273 Option A: cache is keyed by the RESOLVED endpoint, so the
        # last-active reusable instance Security hits reports its own (cold) hits/
        # misses instead of inflating the model row.
        return [{
            'cache_key': 'http://127.0.0.1:1234/v1|qwen3.8-27b',
            'cache_hits': 5,
            'cache_misses': 1,
            'cache_hit_ratio': 0.833,
        }, {
            'cache_key': 'http://127.0.0.1:8080/v1|gemma-4-31b-it',
            'cache_hits': 1,
            'cache_misses': 1,
            'cache_hit_ratio': 0.5,
        }]

    def get_agent_class_summary(self):
        return [{
            'agent_class': 'orchestrator',
            'tool_usage_accuracy': 91.7,
            'total_time_sec': 12.3,
            'tokens_generated': 1500,
            'runs': 3,
            # BUG_0181: LLM-turn count (distinct from engagements/turns) must be surfaced.
            'llm_calls': 12,
        }]

    def get_skill_usage_summary(self):
        return [{
            'skill': 'docker-best-practices',
            'loads': 4,
            'agent_classes': ['coder'],
            'top_mode': 'auto',
        }]


class TestSystemInfoTelemetryDump:
    """Tests for the live `help='telemetry'` dump (computed from pool.telemetry)."""

    def _tool_with(self, telemetry):
        pool = _FakePool(server_info=('0.0.0.0', 8765))
        pool.telemetry = telemetry
        return SystemInfo(agent_pool=pool)

    def test_ab_table_is_resolved_keyed_and_omits_runs_column(self):
        """BUG_0273 re-key at the VIEW: get_config_comparison is the single filter
        source (zero-call rows already removed), so the A/B dump renders the resolved
        row labelled 'by resolved model' with the turn-start runs column omitted. The
        ghost-row drop is verified at the getter in test_telemetry.py; this proves the
        dump faithfully renders the getter-supplied resolved rows."""
        class _ResolvedKeyingTelemetry(_FakeTelemetry):
            def get_config_comparison(self):
                # Getter already dropped the zero-call Swift-1.5 row; the dump now
                # renders whatever the getter returns, with no second filter.
                return [
                    # Resolved endpoint that actually served 2 calls.
                    {'config_fingerprint': 'dflashfp0001',
                     'config_description': {'model': 'dflash2'},
                     'runs': 0, 'llm_calls': 2,
                     'total_llm_latency_sec': 1.2, 'total_duration_sec': 3.0,
                     'avg_tps': 40.0},
                ]

        out = self._tool_with(_ResolvedKeyingTelemetry()).call('{"help": "telemetry"}')
        # Header reflects the resolved key.
        assert 'Config fingerprints (A/B, by resolved model):' in out
        # Resolved row present; model→llm_calls adjacency proves the runs column
        # was removed (old format had 'runs=' between them).
        assert 'config_model=dflash2  llm_calls=2' in out

    def test_ab_dump_does_not_refilter(self):
        """Defense against a second copy of the llm_calls==0 predicate drifting back
        into the dump: a getter that (hypothetically) returns a zero-call row would
        now be RENDERED, because the getter is the single source of truth."""
        class _GhostTelemetry(_FakeTelemetry):
            def get_config_comparison(self):
                return [
                    {'config_fingerprint': 'swiftfp00001',
                     'config_description': {'model': 'Swift-1.5'},
                     'runs': 1, 'llm_calls': 0,
                     'total_llm_latency_sec': 0.0, 'total_duration_sec': 0.0,
                     'avg_tps': 0.0},
                ]

        out = self._tool_with(_GhostTelemetry()).call('{"help": "telemetry"}')
        # The dump no longer filters: the ghost would render if the getter let it
        # through — which it does not in production (see test_telemetry.py).
        assert 'config_model=Swift-1.5  llm_calls=0' in out

    def test_populated_dump_renders_all_sections(self):
        out = self._tool_with(_FakeTelemetry()).call('{"help": "telemetry"}')
        assert 'AC Telemetry Dump' in out
        # Session totals + per-model breakdown
        assert 'total_llm_calls: 7' in out
        assert 'qwen3.8-27b: 5' in out
        # Prompt cache block (present because total_llm_calls > 0)
        assert 'Prompt cache:' in out
        assert 'hits=6  misses=1  unknown=0' in out
        assert 'hit_ratio=85.7%' in out
        assert 'coverage=100.0%' in out
        assert 'wasted_tokens=12345  waste_events=2' in out
        # Config fingerprint A/B — per-model cache column (BUG_0260)
        assert 'abc123def456' in out
        assert 'model=qwen3.8-27b' in out
        # BUG_0273: per-model cache/cache_hit columns were dropped (they showed a
        # misleading fallback-only number for real routed calls). Assert they are
        # gone; cache is reported only by the per-endpoint table below.
        assert 'cache=5/2' not in out
        assert 'cache_hit=71.4%' not in out
        # Per-endpoint cache attribution (BUG_0273 Option A): authoritative
        # cache view keyed by the resolved endpoint, not the model-only row.
        assert 'Cache by endpoint (resolved):' in out
        assert 'http://127.0.0.1:1234/v1|qwen3.8-27b  hits=5  misses=1  cache_hit=83.3%' in out
        # Agent class usage (accuracy formatted as %)
        assert 'orchestrator:' in out
        assert '91.7%' in out
        # BUG_0181: LLM-turn count surfaced alongside engagement turns.
        assert 'llm_calls=12' in out
        # Skill usage
        assert 'docker-best-practices: loads=4' in out
        assert 'top_mode=auto' in out
        # Export pointer
        assert '/api/telemetry/export' in out

    def test_waste_cost_shown_only_when_priced(self):
        """wasted_cost=$ renders only when the session summary carries the key (MODEL_PRICING set)."""
        class _Priced(_FakeTelemetry):
            def get_session_summary(self):
                d = super().get_session_summary()
                d['llm_wasted_cost'] = 0.0123
                return d
        out = self._tool_with(_Priced()).call('{"help": "telemetry"}')
        assert 'wasted_cost=$0.012300' in out
        # Unpriced default: the base _FakeTelemetry has no llm_wasted_cost key.
        out_unpriced = self._tool_with(_FakeTelemetry()).call('{"help": "telemetry"}')
        assert 'wasted_cost=' not in out_unpriced

    def test_case_insensitive(self):
        out = self._tool_with(_FakeTelemetry()).call('{"help": "TELEMETRY"}')
        assert 'AC Telemetry Dump' in out

    def test_no_telemetry_degrades_gracefully(self):
        """Pool with no telemetry -> clear message, does not raise."""
        pool = _FakePool(server_info=('0.0.0.0', 8765))
        # No .telemetry attribute set at all
        out = SystemInfo(agent_pool=pool).call('{"help": "telemetry"}')
        assert 'unavailable' in out

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
        assert 'AC Telemetry Dump' in out
        assert '/api/telemetry/export' in out

    def test_getter_exception_degrades_gracefully(self):
        """A getter raising is caught -> error string, does not propagate."""

        class _Boom:

            def get_session_summary(self):
                raise RuntimeError('boom')

            def get_config_comparison(self):
                return []

            def get_agent_class_summary(self):
                return []

            def get_skill_usage_summary(self):
                return []

        out = self._tool_with(_Boom()).call('{"help": "telemetry"}')
        assert 'unavailable' in out

    def test_prompt_cache_block_present_when_llm_calls(self):
        """Cache stats + total_llm_calls>0 -> block with % ratios."""

        class _WithCache:

            def get_session_summary(self):
                return {
                    'total_llm_calls': 4,
                    'llm_cache_hits': 3,
                    'llm_cache_misses': 1,
                    'llm_cache_unknown': 0,
                    'llm_cache_hit_ratio': 0.75,
                    'llm_cache_classified_ratio': 0.5,
                }

            def get_config_comparison(self):
                return []

            def get_agent_class_summary(self):
                return []

            def get_skill_usage_summary(self):
                return []

        out = self._tool_with(_WithCache()).call('{"help": "telemetry"}')
        assert 'Prompt cache:' in out
        assert 'hits=3  misses=1  unknown=0' in out
        assert 'hit_ratio=75.0%' in out
        assert 'coverage=50.0%' in out

    def test_prompt_cache_block_absent_when_no_llm_calls(self):
        """total_llm_calls==0 -> block is NOT printed (no noise on fresh sessions)."""

        class _NoCalls:

            def get_session_summary(self):
                return {
                    'total_runs': 1,
                    'total_llm_calls': 0,
                    'llm_cache_hits': 0,
                    'llm_cache_misses': 0,
                    'llm_cache_unknown': 0,
                    'llm_cache_hit_ratio': None,
                    'llm_cache_classified_ratio': None,
                }

            def get_config_comparison(self):
                return []

            def get_agent_class_summary(self):
                return []

            def get_skill_usage_summary(self):
                return []

        out = self._tool_with(_NoCalls()).call('{"help": "telemetry"}')
        assert 'Prompt cache:' not in out

    def test_prompt_cache_none_ratios_render_na(self):
        """Ratios None (but calls>0) -> 'n/a' instead of crashing."""

        class _NoneRatio:

            def get_session_summary(self):
                return {
                    'total_llm_calls': 2,
                    'llm_cache_hits': 0,
                    'llm_cache_misses': 0,
                    'llm_cache_unknown': 2,
                    'llm_cache_hit_ratio': None,
                    'llm_cache_classified_ratio': None,
                }

            def get_config_comparison(self):
                return []

            def get_agent_class_summary(self):
                return []

            def get_skill_usage_summary(self):
                return []

        out = self._tool_with(_NoneRatio()).call('{"help": "telemetry"}')
        assert 'Prompt cache:' in out
        assert 'hit_ratio=n/a' in out
        assert 'coverage=n/a' in out


class TestSystemInfoVersion:
    """The normal (non-help) output must surface the AgentCascade package version."""

    def test_version_line_present(self, monkeypatch):
        """Output contains an 'AgentCascade Version:' line matching agent_cascade.__version__."""
        import agent_cascade
        out = _run(None)
        assert 'AgentCascade Version:' in out
        assert f"AgentCascade Version: {agent_cascade.__version__}" in out

    def test_version_matches_import(self, monkeypatch):
        """The reported version equals the imported __version__ (single source of truth)."""
        import agent_cascade
        from agent_cascade.tools.custom.system_info import AC_VERSION
        assert AC_VERSION == agent_cascade.__version__


class TestRestApiHelpDrift:
    """Drift guard for the rest_api help section (root-cause fix for BUG_0154).

    config/ac_system_help.yaml's rest_api.endpoints list was hand-maintained and
    silently drifted from the real routes in agent_cascade/api_server.py. This test
    statically cross-checks the two sources so any future /api/* route added without
    a matching help entry fails CI. It compares only the (method, path) pairs —
    summaries are documentation and are not asserted.
    """

    _REPO_ROOT = Path(__file__).resolve().parents[1]

    @staticmethod
    def _norm(path: str) -> str:
        """Collapse every {…} path parameter ({id}, {endpoint_id}, {path:path}) to one
        wildcard token, so FastAPI param-name differences never cause false drift."""
        return re.sub(r'\{[^}]*\}', '{_}', path)

    def _yaml_pairs(self):
        """(METHOD, normalized_path) for every rest_api.endpoints entry in the help YAML."""
        help_path = self._REPO_ROOT / 'config' / 'ac_system_help.yaml'
        data = yaml.safe_load(help_path.read_text(encoding='utf-8'))
        return {
            (str(e['method']).upper(), self._norm(e['path']))
            for e in data['rest_api']['endpoints']
        }

    def _code_pairs(self):
        """(METHOD, normalized_path) for every @app.<verb>("<path>") route in api_server.py."""
        src = (self._REPO_ROOT / 'agent_cascade' / 'api_server.py').read_text(encoding='utf-8')
        pairs = set()
        # KNOWN LIMITATION: this regex only matches @app.<verb>("<path>") with a
        # directly-quoted first positional path argument. A future @app.api_route(..., methods=[...]),
        # @app.route(...), or keyword form (@app.post(path=...)) would be silently unguarded —
        # if such a form is ever introduced, extend this parser to cover it.
        for verb, path in re.findall(
                r"@app\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]", src):
            # Scope to discoverable API endpoints only. The non-/api catch-alls
            # ('/' and '/{path:path}') are the UI/static handlers, not API endpoints,
            # so they are intentionally exempt. /ws/chat is a websocket route and
            # is not matched by this decorator regex at all.
            if not path.startswith('/api/'):
                continue
            pairs.add((verb.upper(), self._norm(path)))
        return pairs

    def test_every_api_route_has_a_help_entry(self):
        """Every scoped code route must appear in the help YAML (code ⊆ YAML)."""
        missing = self._code_pairs() - self._yaml_pairs()
        assert not missing, (
            'rest_api help (config/ac_system_help.yaml) is missing entries for '
            'these live routes — add them:\n'
            + '\n'.join(f"  {method} {path}" for method, path in sorted(missing))
        )

    def test_path_param_names_are_normalized(self):
        """Normalization proves {id} (YAML) and {endpoint_id} (code) compare equal."""
        assert self._norm('/api/endpoints/{id}') == self._norm('/api/endpoints/{endpoint_id}')
        assert self._norm('/api/approve/{request_id}') == '/api/approve/{_}'
        assert self._norm('/{path:path}') == self._norm('/{proxy_path:path}') == '/{_}'


class _FakeAgentsPool:
    """Pool stand-in exposing only what _render_agents reads.

    list_agents + get_agent_info drive the class list; `instances` (name -> object)
    drives the live Active Instances section. Instances are SimpleNamespace objects
    (not MagicMock) so the real read path is exercised without auto-attribute side
    effects.
    """

    def __init__(self, agents, instances=None):
        # agents: list of dicts with name / tagline / tools.
        self._agents = agents
        # instances: dict of name -> AgentInstance-shaped SimpleNamespace (or {}).
        self.instances = instances or {}

    def list_agents(self):
        return [a['name'] for a in self._agents]

    def get_agent_info(self, name):
        for a in self._agents:
            if a['name'] == name:
                return {'name': a['name'], 'tagline': a['tagline'], 'tools': list(a['tools'])}
        return None


def _inst(agent_class, state, turn=2, max_turns=10):
    """AgentInstance-shaped stand-in for the Active Instances section."""
    return SimpleNamespace(
        agent_class=agent_class,
        state=state,
        _current_turn=turn,
        max_turns=max_turns,
        conversation=[],
    )


class TestSystemInfoAgentsSection:
    """Tests for the dynamic help='agents' section (built from the running pool)."""

    _AGENTS = [
        {'name': 'coder', 'tagline': 'Pragmatic senior software engineer', 'tools': ['read_file', 'edit_file']},
        {'name': 'orchestrator', 'tagline': 'Technical lead', 'tools': ['call_agent', 'system_info']},
    ]

    def test_lists_name_tagline_and_active_tools(self):
        out = SystemInfo(agent_pool=_FakeAgentsPool(self._AGENTS)).call('{"help": "agents"}')
        assert 'AC Agent Classes' in out
        for name, tagline, tool in [
            ('coder', 'Pragmatic senior software engineer', 'edit_file'),
            ('orchestrator', 'Technical lead', 'call_agent'),
        ]:
            assert f'### {name}' in out
            assert tagline in out
            assert tool in out

    def test_uses_pool_as_single_source_of_truth(self):
        """Tool lists are not re-derived: whatever get_agent_info returns is rendered verbatim."""
        out = SystemInfo(agent_pool=_FakeAgentsPool(
            [{'name': 'writer', 'tagline': 'Writer', 'tools': ['web_search']}])).call('{"help": "agents"}')
        assert '### writer' in out
        assert 'web_search' in out
        # A tool the pool does not report must never appear (proves no hardcoded re-derivation).
        assert 'shell_cmd' not in out

    def test_without_pool_degrades_gracefully(self):
        out = SystemInfo(agent_pool=None).call('{"help": "agents"}')
        assert 'agents list unavailable' in out

    def test_empty_pool_does_not_raise(self):
        out = SystemInfo(agent_pool=_FakeAgentsPool([])).call('{"help": "agents"}')
        assert 'no agent templates loaded' in out

    def test_active_instances_section_lists_active_instance(self):
        """A known non-idle instance is listed in the Active Instances section."""
        pool = _FakeAgentsPool(self._AGENTS, instances={'worker1': _inst('coder', AgentState.RUNNING)})
        out = SystemInfo(agent_pool=pool).call('{"help": "agents"}')
        assert '--- Active Instances (live) ---' in out
        assert 'worker1' in out          # name
        assert '(coder)' in out          # agent_class
        assert '[RUNNING]' in out        # state

    def test_active_instances_filters_out_idle(self):
        """Idle instances are omitted, mirroring /api/status's ACTIVE_STATES filter."""
        pool = _FakeAgentsPool(self._AGENTS, instances={'idleguy': _inst('writer', AgentState.IDLE)})
        out = SystemInfo(agent_pool=pool).call('{"help": "agents"}')
        assert '--- Active Instances (live) ---' in out
        assert 'idleguy' not in out
        assert '(no active instances)' in out

    def test_active_instances_degrades_gracefully_when_none(self):
        """No live sessions -> placeholder, section still present and no crash."""
        out = SystemInfo(agent_pool=_FakeAgentsPool(self._AGENTS)).call('{"help": "agents"}')
        assert '--- Active Instances (live) ---' in out
        assert '(no active instances)' in out

    def test_active_instances_survives_broken_instance(self):
        """A malformed instance does not crash the section nor suppress valid ones."""
        boom = object()  # no .state attr -> skipped by the ACTIVE_STATES filter, not an error
        pool = _FakeAgentsPool(self._AGENTS, instances={'good': _inst('coder', AgentState.RUNNING), 'bad': boom})
        out = SystemInfo(agent_pool=pool).call('{"help": "agents"}')
        assert 'good' in out and '(coder)' in out


class TestListAgentsDeprecationStub:
    """Soft-deprecation: the registered list_agents tool now returns a redirect notice."""

    def test_stub_returns_deprecation_notice(self):
        from agent_cascade.tools.custom.manager_ops import ListAgents

        out = ListAgents().call('')
        assert 'DEPRECATED' in out
        assert 'system_info' in out and 'agents' in out

    def test_list_agents_removed_from_available_tools(self):
        """Soft-deprecate part 1: list_agents is no longer in the canonical AVAILABLE_TOOLS
        enabled-tool list (records deprecation intent; keeps hard removal to one line).
        It is also excluded from the model's offered set via HIDDEN_OFFERED_TOOLS (see
        test_hidden_from_offered_set_but_still_callable), yet stays registered/callable
        in function_map so stale calls resolve to the redirect notice (see
        test_stub_returns_deprecation_notice)."""
        from agent_cascade.prompts.dna import AVAILABLE_TOOLS

        assert 'list_agents' not in AVAILABLE_TOOLS

    def test_hidden_from_offered_set_but_still_callable(self):
        """Offer path (the real gate): order_offered_tool_names subtracts
        HIDDEN_OFFERED_TOOLS, so the registered stub is never advertised to the model
        even though it remains in function_map (callable)."""
        from agent_cascade.engine.helpers import order_offered_tool_names

        func_map = {
            'list_agents': object(),  # still registered -> callable stub
            'call_agent': object(),
            'read_file': object(),
        }
        offered = order_offered_tool_names(func_map, set())
        assert 'list_agents' not in offered  # hidden from the model
        assert 'call_agent' in offered and 'read_file' in offered  # others unaffected
        assert 'list_agents' in func_map  # remains callable
