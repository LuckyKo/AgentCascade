import datetime
import json
import logging
import os
import platform
import sys
from pathlib import Path

from agent_cascade import __version__ as AC_VERSION
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.settings import DEFAULT_MAX_TURNS, DEFAULT_WORKSPACE
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.utils.utils import get_history_stats

logger = logging.getLogger(__name__)


def _help_file_path() -> Path:
    """Return the resolved path to the help YAML file (config/ac_system_help.yaml).

    Exposed at module level so tests can monkeypatch it.
    """
    return Path(__file__).resolve().parents[3] / 'config' / 'ac_system_help.yaml'


def _mcp_servers_path() -> Path:
    """Return the resolved path to the MCP servers config (config/mcp_servers.json).

    Exposed at module level so tests can monkeypatch it.
    """
    return Path(__file__).resolve().parents[3] / 'config' / 'mcp_servers.json'


def _pct(v) -> str:
    """Format a 0..1 ratio as a percentage string; 'n/a' for non-numeric/None."""
    return f"{v * 100:.1f}%" if isinstance(v, (int, float)) else 'n/a'


@register_tool('system_info', allow_overwrite=True)
class SystemInfo(BaseTool):
    """Tool to get the current system information including OS, time, date, cwd, python version, and session stats."""

    name = 'system_info'
    description = TOOL_METADATA['system_info']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'help': {
                'type': 'string',
                'description': TOOL_METADATA['system_info']['parameters']['help'],
            },
        },
        'required': [],
    }

    def __init__(self, agent_pool=None, agent_name=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool
        self.agent_name = agent_name

    def _is_path_allowed(self, abs_path: str, allowed_prefixes: set) -> bool:
        """Check if a path is within an allowed directory (mirrors code_interpreter._is_path_allowed)."""
        for prefix in allowed_prefixes:
            try:
                if os.path.commonpath([abs_path, prefix]) == prefix:
                    return True
            except ValueError:
                # Different drive letters on Windows
                continue
        return False

    def _render_budget_block(self, inst) -> str:
        """Compact self-scoped Budget/Cost block (Slice 4).

        Returns '' when nothing is configured, so the block is ABSENT from the
        default (no-``help``) output by default — no placeholder. Shown only when
        this instance carries a ``token_budget`` OR its endpoint is priced.

        ``consumed`` is this instance's OWN session-lifetime spend
        (``get_instance_total_tokens``) — the descendant-inclusive subtree total
        belongs to the ``call_agent`` completion message (Slice 3), not here.
        ``$`` is rendered only when ``get_instance_cost`` returns a value; ``None``
        (unpriced) never renders a misleading ``$0``.
        """
        telem = getattr(self.agent_pool, 'telemetry', None)
        if telem is None:
            return ''
        try:
            cost = telem.get_instance_cost(inst)
        except Exception:
            cost = None
        token_budget = int(getattr(inst, 'token_budget', 0) or 0)
        # Default-off gate: no token budget and unpriced → omit the whole block.
        if token_budget <= 0 and cost is None:
            return ''
        try:
            consumed = int(telem.get_instance_total_tokens(inst))
        except Exception:
            consumed = 0
        lines = ['Budget / Cost:']
        lines.append(f"  consumed: {consumed:,} tokens")
        if token_budget > 0:
            unit = getattr(inst, 'budget_unit', 'tokens') or 'tokens'
            lines.append(f"  budget: {token_budget:,} tokens (budget_unit={unit})")
            lines.append(f"  remaining: {max(0, token_budget - consumed):,} tokens")
        if cost is not None:
            lines.append(f"  cost: ${cost:.6f}")
        return '\n'.join(lines)

    def _render_telemetry_dump(self) -> str:
        """Render a live, human-readable dump of the current session telemetry.

        Computed from ``self.agent_pool.telemetry`` (the same getters the
        ``/api/telemetry`` endpoint uses) — never raises; degrades to a clear
        message if telemetry is unavailable. Excludes per-event detail
        (``recent_events``) to stay compact; raw JSONL is at /api/telemetry/export.
        """
        telem = getattr(self.agent_pool, 'telemetry', None)
        if not telem:
            return ('system_info telemetry dump unavailable: no telemetry collector '
                    'attached to the agent pool.')
        try:
            session = telem.get_session_summary() or {}
            configs = telem.get_config_comparison() or []
            agent_classes = telem.get_agent_class_summary() or []
            skills = telem.get_skill_usage_summary() or []
            cache_attribution = (
                telem.get_cache_attribution() or []
                if hasattr(telem, 'get_cache_attribution') else []
            )

            lines = ['--- AC Telemetry Dump (live) ---']

            # ── Session totals ──
            lines.append('')
            lines.append('Session:')
            for k in ('total_runs', 'total_llm_calls', 'total_tool_calls', 'total_input_tokens_est',
                      'total_output_tokens_est', 'avg_tps', 'total_retries', 'total_compressions',
                      'total_llm_latency_ms', 'total_tool_latency_ms', 'total_ttft_ms',
                      'total_streaming_time_ms', 'total_turn_time_ms', 'call_agent_latency_ms'):
                if k in session:
                    lines.append(f"  {k}: {session[k]}")

            # Session spend ($) — present only when an endpoint carries pricing.
            if 'session_cost' in session:
                lines.append(f"  session_cost: ${session['session_cost']:.6f}")

            # Prompt cache (RFC 9211) hit/miss stats — only when LLM calls exist.
            if session.get('total_llm_calls', 0) > 0:
                lines.append('  Prompt cache:')
                lines.append(f"    hits={session.get('llm_cache_hits', 0)}  "
                             f"misses={session.get('llm_cache_misses', 0)}  "
                             f"unknown={session.get('llm_cache_unknown', 0)}")
                lines.append(f"    hit_ratio={_pct(session.get('llm_cache_hit_ratio'))}   "
                             f"coverage={_pct(session.get('llm_cache_classified_ratio'))}")
                waste_line = (f"    wasted_tokens={session.get('llm_wasted_tokens', 0)}  "
                              f"waste_events={session.get('llm_waste_count', 0)}")
                if 'llm_wasted_cost' in session:
                    waste_line += f"  wasted_cost=${session['llm_wasted_cost']:.6f}"
                lines.append(waste_line)

            # llm_calls_by_model — per-model call counts
            by_model = session.get('llm_calls_by_model') or {}
            if by_model:
                lines.append('  LLM calls by model:')
                for model, cnt in sorted(by_model.items(), key=lambda x: -x[1]):
                    lines.append(f"    {model}: {cnt}")

            # ── Config fingerprint (A/B) comparison ──
            if configs:
                lines.append('')
                lines.append('Config fingerprints (A/B, by resolved model):')
                for c in configs:
                    llm_calls = c.get('llm_calls', 0)
                    # No llm_calls==0 skip here: get_config_comparison is the single
                    # source of truth and already filters zero-call rows (BUG_0273),
                    # so this table lists only models that served a resolved call.
                    fp = c.get('config_fingerprint', '?')
                    model = (c.get('config_description') or {}).get('model', '?')
                    avg_tps = c.get('avg_tps', '')
                    # runs is a turn-start count with no single resolved-model home
                    # under this re-key, so it is intentionally omitted from the line.
                    lines.append(f"  {fp}  config_model={model}  "
                                 f"llm_calls={llm_calls}  "
                                 f"llm_time={c.get('total_llm_latency_sec', '')}s  "
                                 f"turn_time={c.get('total_duration_sec', '')}s  "
                                 f"avg_tps={avg_tps}")
                    # No cache columns here (BUG_0273): per-model cache counters are
                    # now fallback-only — 0 for any real routed call that had a
                    # resolved endpoint — so showing them would recreate the "wrong
                    # model row" trap this fix removes. Cache is reported by endpoint
                    # in the table below.

            # ── Cache by endpoint (resolved) ── (BUG_0273 Option A)
            # Authoritative cache view. The prompt KV cache belongs to the endpoint
            # that actually served the call, so hits/misses are attributed by the
            # resolved endpoint's key (api_base|model) — not the model-only A/B row.
            # A use_last_endpoint agent's fresh-instance misses now appear under its
            # own endpoint instead of inflating the configured model's miss count.
            if cache_attribution:
                lines.append('')
                lines.append('Cache by endpoint (resolved):')
                for c in cache_attribution:
                    lines.append(f"  {c.get('cache_key', '?')}  "
                                 f"hits={c.get('cache_hits', 0)}  "
                                 f"misses={c.get('cache_misses', 0)}  "
                                 f"cache_hit={_pct(c.get('cache_hit_ratio'))}")

            # ── Agent class usage ──
            if agent_classes:
                lines.append('')
                lines.append('Agent class usage:')
                for a in agent_classes:
                    cls = a.get('agent_class', '?')
                    acc = a.get('tool_usage_accuracy')
                    acc_str = f"{acc:.1f}%" if isinstance(acc, (int, float)) else 'n/a'
                    # BUG_0181: surface llm_calls (actual LLM turns) alongside
                    # runs (engagements) — a multi-turn agent (e.g. the warm-reused
                    # Security guard) runs far more LLM turns than engagements, and
                    # showing only `runs` made the guard look ~2.7x cheaper than it is.
                    lines.append(f"  {cls}: runs={a.get('runs', '')} "
                                 f"llm_calls={a.get('llm_calls', '')} "
                                 f"turn_time={a.get('total_time_sec', '')}s "
                                 f"llm_time={a.get('llm_time_sec', '')}s "
                                 f"tool_time={a.get('tool_time_sec', '')}s "
                                 f"tokens_gen={a.get('tokens_generated', '')} "
                                 f"tool_acc={acc_str}")

            # ── Skill usage ── (get_skill_usage_summary returns a list of
            # {skill, loads, agent_classes, top_mode}, sorted by loads desc)
            if skills:
                lines.append('')
                lines.append('Skill usage:')
                for s in skills:
                    name = s.get('skill', '?')
                    loads = s.get('loads', '')
                    top_mode = s.get('top_mode', '')
                    classes = s.get('agent_classes') or []
                    cls_str = f" [{', '.join(classes)}]" if classes else ''
                    mode_str = f" top_mode={top_mode}" if top_mode else ''
                    lines.append(f"  {name}: loads={loads}{cls_str}{mode_str}")

            # BUG_0263: "Most-missed hinted skills" — aggregates the per-instance hint
            # ledger (_skill_hint_counts) against what was actually loaded
            # (_loaded_skill_names) across every pool instance. Sits below Skill usage
            # because it answers a different question: hinted-but-never-loaded demand,
            # i.e. the skill the advisor keeps pointing at but nobody ever loads. Omitted
            # entirely when no instance has any hint counts, so existing dumps are unchanged.
            missed = self._render_missed_skills()
            if missed:
                lines.append('')
                lines.append(missed)

            lines.append('')
            lines.append('(Raw JSONL export: GET /api/telemetry/export)')
            return '\n'.join(lines)

        except Exception as e:
            logger.warning(f"system_info telemetry dump failed: {e}")
            return f"system_info telemetry dump unavailable: {e}"

    def _render_missed_skills(self, top_n: int = 5) -> str:
        """Aggregate "Most-missed hinted skills" across all pool instances (BUG_0263).

        For each skill name seen in any instance's ``_skill_hint_counts`` (the per-session
        hint ledger incremented by the memory-hint pipeline), sum the hints across instances
        and count how many of those instances actually carry the skill in
        ``_loaded_skill_names``. "Missed" = hinted but never loaded. Rows are sorted by
        missed-first (``hints - loaded_instances`` desc), then raw hints desc, then name
        asc, so the skill the advisor most persistently flags but nobody loads tops the
        list. Returns '' (section omitted) when there is nothing to show.

        Never raises — a malformed pool / instance / counter is skipped or the whole section
        degrades to '', so it can't corrupt the surrounding telemetry dump.
        """
        try:
            # skill_lower -> [display_name, total_hints, loaded_instance_count]
            agg: dict = {}
            instances = list(getattr(self.agent_pool, 'instances', {}).values())
            for inst in instances:
                counts = getattr(inst, '_skill_hint_counts', None) or {}
                if not isinstance(counts, dict):
                    continue
                loaded = {str(x).lower() for x in (getattr(inst, '_loaded_skill_names', None) or [])
                          if isinstance(x, str)}
                for skill, cnt in counts.items():
                    if not isinstance(cnt, int) or cnt <= 0:
                        continue
                    key = str(skill).lower()
                    rec = agg.setdefault(key, [str(skill), 0, 0])
                    rec[1] += cnt
                    if key in loaded:
                        rec[2] += 1
            if not agg:
                return ''
            rows = list(agg.values())  # each [name, hints, loaded_instances]
            rows.sort(key=lambda r: (-(r[1] - r[2]), -r[1], str(r[0]).lower()))
            out = ['Most-missed hinted skills:']
            for name, hints, loaded in rows[:top_n]:
                out.append(f"  {name}: hinted={hints} loaded={loaded}")
            return '\n'.join(out)
        except Exception as e:  # noqa: BLE001 — never degrade the rest of the dump
            logger.debug(f"system_info missed-skills render failed: {e}")
            return ''

    def _render_help(self, key: str) -> str:
        """Render a help section from the YAML file. Never raises."""
        try:
            import yaml
            path = _help_file_path()
            if not path.is_file():
                return f"system_info help file not found at {path}"
            data = yaml.safe_load(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                return f"system_info help unavailable: YAML root is not a mapping\n(Help file: {path})"

            # Available sections = top-level keys NOT starting with '_'
            sections = [k for k in data if not str(k).startswith('_')]

            # Case-insensitive match
            matched_key = None
            for s in sections:
                if str(s).lower() == key.lower():
                    matched_key = s
                    break

            if matched_key is None:
                lines = [f"Unknown system_info help section: '{key}'.", '']
                lines.append(
                    'How to use: system_info(help="<section>") fetches one help section; omit `help` for normal system info.'
                )
                lines.append('')
                lines.append('Available sections:')
                for s in sections:
                    lines.append(f"  - {s}")
                usage = data.get('_usage')
                if isinstance(usage, list):
                    lines.append('')
                    for bullet in usage:
                        lines.append(f"  \u2022 {bullet}")
                return '\n'.join(lines)

            # Render the matched section
            sec = data[matched_key]
            if not isinstance(sec, dict):
                return f"--- AC Help: {matched_key} ---\n{sec}"

            out_lines = [f"--- AC Help: {matched_key} ---"]

            # Description first
            desc = sec.get('description')
            if desc:
                out_lines.append(str(desc))

            # Notes (before endpoints)
            notes = sec.get('notes')
            if isinstance(notes, list):
                for n in notes:
                    out_lines.append(f"- {n}")

            # Endpoints table
            endpoints = sec.get('endpoints')
            if isinstance(endpoints, list) and endpoints:
                # Compute column widths (cap path ~40, method ~7)
                m_width = max(7, min(max(len(str(e.get('method', ''))) for e in endpoints), 10))
                p_width = max(20, min(max(len(str(e.get('path', ''))) for e in endpoints), 40))
                header = f"{'METHOD'.ljust(m_width)}  {'PATH'.ljust(p_width)}  SUMMARY"
                out_lines.append('')
                out_lines.append(header)
                for ep in endpoints:
                    if not isinstance(ep, dict):
                        continue
                    method = str(ep.get('method', ''))
                    path_str = str(ep.get('path', ''))
                    summary = str(ep.get('summary', ''))
                    out_lines.append(f"{method.ljust(m_width)}  {path_str.ljust(p_width)}  {summary}")

            # Notes after endpoints
            notes_after = sec.get('notes_after')
            if isinstance(notes_after, list):
                for n in notes_after:
                    out_lines.append(f"- {n}")

            # Any other keys as "Key: value"
            known_keys = {'description', 'endpoints', 'notes', 'notes_after'}
            for k, v in sec.items():
                if k not in known_keys:
                    out_lines.append(f"{k}: {v}")

            return '\n'.join(out_lines)

        except Exception as e:
            logger.warning(f"system_info help render failed: {e}")
            try:
                path_str = str(_help_file_path())
            except Exception:
                path_str = '<unknown>'
            return f"system_info help unavailable: {e}\n(Help file: {path_str})"

    def _render_agents(self) -> str:
        """Render agent classes (name, tagline, active tools) plus live instances.

        Classes are computed from ``self.agent_pool`` (``list_agents()`` +
        ``get_agent_info()``) — the same accessors the ``/api/agents`` endpoint uses,
        so each tool list is already filtered by the instance's disabled_tools config.
        An ``Active Instances (live)`` section is appended (see
        ``_render_active_instances``), restoring the per-instance runtime view of the
        deprecated ``list_agents`` tool; full context-% remains at ``/api/status``.
        Never raises; degrades to a clear message if no agent pool is attached.
        """
        pool = getattr(self, 'agent_pool', None)
        if not pool:
            return 'system_info agents list unavailable: no agent pool attached to this tool.'
        try:
            names = list(pool.list_agents())
        except Exception as e:
            return f'system_info agents list unavailable: {e}'

        lines = ['--- AC Agent Classes (live) ---']
        if names:
            for name in sorted(names):
                try:
                    info = pool.get_agent_info(name) or {}
                except Exception as e:
                    lines.append('')
                    lines.append(f"### {name}")
                    lines.append(f"  (info unavailable: {e})")
                    continue
                tagline = (info.get('tagline') or '').strip() or 'No tagline.'
                tools = info.get('tools') or []
                lines.append('')
                lines.append(f"### {name}")
                lines.append(f"  {tagline}")
                if tools:
                    lines.append(f"  Active tools ({len(tools)}): {', '.join(tools)}")
                else:
                    lines.append('  Active tools: (none)')

            lines.append('')
            lines.append('Active tools are derived per class from its disabled_tools config (the same set '
                         'the model sees in its prompt). The six core classes (orchestrator, security, '
                         'compressor, generalist, reviewer, writer) carry seeded defaults; coder, designer, '
                         'ponytail, researcher, and Admin fall back to the safe baseline when unconfigured.')
        else:
            lines.append('(no agent templates loaded)')

        # Live per-instance sessions (the runtime view the old list_agents tool showed).
        lines.append(self._render_active_instances())

        # Primary Endpoint: the LLM endpoint these agents run on (omitted when no
        # router / empty chain). Placed last so the per-instance list reads first.
        primary_ep = self._render_primary_endpoint()
        if primary_ep:
            lines.append(primary_ep)

        return '\n'.join(lines)

    def _render_active_instances(self) -> str:
        """Render live per-instance sessions appended to the agent-class list.

        Shows ALL instances in the pool, in every state (active, idle, parked,
        …) — not just the active ones — so parked/idle sessions stay visible. The
        context-token figure reuses the same helpers ``/api/status`` uses —
        ``get_history_stats`` (message tokens) plus
        ``estimate_instance_tool_schema_tokens`` (the serialized tool schemas sent
        with every call, BUG_0136) — so the number matches what is actually in
        context. Single source of truth is the pool's ``instances`` dict.
        Never raises: degrades to a short note on any pool/instance error.
        """
        lines = ['', '--- Instances (live) ---']
        pool = getattr(self, 'agent_pool', None)
        if not pool:
            return '\n'.join(lines + ['(no instances)'])
        try:
            from agent_cascade.agent_instance import AgentState
            from agent_cascade.engine.helpers import estimate_instance_tool_schema_tokens

            snapshot = list(getattr(pool, 'instances', {}).items())  # name -> AgentInstance
        except RuntimeError:
            snapshot = []  # dict resized mid-copy (same guard as /api/status)
        except Exception as e:
            return '\n'.join(lines + [f'(instance snapshot unavailable: {e})'])

        rows = []
        for name, inst in snapshot:
            try:
                agent_class = getattr(inst, 'agent_class', '') or 'unknown'
                state = (getattr(inst, 'state', None) or AgentState.IDLE).name
                turn = int(getattr(inst, '_current_turn', 0) or 0)
                max_turns = getattr(inst, 'max_turns', None) or DEFAULT_MAX_TURNS
                try:
                    stats = get_history_stats(list(getattr(inst, 'conversation', []) or []))
                except Exception:
                    stats = {'tokens': 0}
                try:
                    schema = estimate_instance_tool_schema_tokens(inst, pool)
                except Exception:
                    schema = 0
                tokens = int(stats.get('tokens', 0)) + int(schema)
                rows.append(f"  - {name} ({agent_class}) [{state}]  "
                            f"turn {turn}/{max_turns}  context {tokens:,} tok")
            except Exception as e:
                rows.append(f"  - {name}  (details unavailable: {e})")

        lines.extend(rows or ['(no instances)'])
        return '\n'.join(lines)

    @staticmethod
    def _indent_block(text: str, prefix: str = '  ') -> str:
        """Prefix every non-empty line of ``text`` with ``prefix``.

        Used by ``call`` to render section CONTENT at a uniform 2-space indent
        under flush-left ``--- Section ---`` headers (consistency pass), without
        touching wording or the data shown. Blank lines and the trailing newline
        are preserved, and multi-line sub-structures nested by the renderer (e.g.
        the ``  - ...`` folder items, or the ``  consumed:`` budget sub-items)
        shift to ``prefix``+existing indent — i.e. 2 spaces deeper than their
        parent line, exactly as intended.
        """
        return '\n'.join(prefix + ln if ln.strip() else ln for ln in text.split('\n'))

    @staticmethod
    def _fmt_per_m(v: float) -> str:
        """Compact $/1M display with enough precision for sub-cent rates.

        Uses general ('g') formatting so a small real rate (e.g. 0.00142) never
        rounds down to a misleading "$0" — only genuinely-zero rates would, and
        those are filtered out before this is called.
        """
        return f"{v:g}"

    def _render_primary_endpoint(self) -> str:
        """Render the ``Primary Endpoint`` block appended to the agents view.

        Shows the LLM endpoint the agents actually run on. ``model`` / ``api_base``
        come from the routing chain head; pricing and limits are read from the
        matched ``APIEndpoint`` OBJECT — the single source of truth — matched back
        to the head by the normalized ``(api_base, model)`` routing key, mirroring
        ``router.get_endpoint_pricing`` exactly. The ``Pricing:`` line is shown only
        when the endpoint carries a non-zero rate; ``Limits`` always shows
        ``max_input_tokens`` and renders rate/concurrency as ``unlimited`` when no
        explicit cap is set. Returns ``''`` (whole block omitted) when there is no
        router, the chain is empty, or on any error, so the agents list never
        breaks or shows a half-built block.
        """
        pool = getattr(self, 'agent_pool', None)
        router = getattr(pool, 'api_router', None) if pool is not None else None
        if router is None:
            return ''
        try:
            from agent_cascade.api_router_pkg.normalization import normalize_api_base

            # Resolve the agent class to look up: the current instance's class when
            # known, else orchestrator (mirrors the get_instance fallback used
            # elsewhere in this tool).
            agent_class = 'orchestrator'
            get_inst = getattr(pool, 'get_instance', None)
            if callable(get_inst):
                try:
                    inst = get_inst(getattr(self, 'agent_name', None) or 'orchestrator')
                    if inst is not None:
                        agent_class = getattr(inst, 'agent_class', None) or 'orchestrator'
                except Exception:
                    pass

            chain = router.get_endpoint_chain(agent_class)
            if not chain:
                return ''
            head = chain[0]
            head_base_raw = head.get('api_base') or head.get('model_server', '')
            head_base = normalize_api_base(head_base_raw)
            head_model = head.get('model', '')

            # Match head back to its endpoint object by normalized (api_base,
            # model) — the same routing key get_endpoint_pricing uses. Two enabled
            # endpoints sharing that key resolve to the first (dict order).
            def _match():
                for ep in getattr(router, 'endpoints', {}).values():
                    if (getattr(ep, 'enabled', True)
                            and normalize_api_base(getattr(ep, 'api_base', '')) == head_base
                            and getattr(ep, 'model', '') == head_model):
                        return ep
                return None

            lock = getattr(router, '_lock', None)
            if lock is not None:
                with lock:
                    ep = _match()
            else:
                ep = _match()

            lines = ['--- Primary Endpoint ---',
                     f'  Model: {head_model}',
                     f'  API Base: {head_base_raw}']

            if ep is not None:
                # Pricing: present only when the endpoint carries any non-zero rate.
                rate_parts = []
                for key, label in (('input_per_m', 'in'), ('output_per_m', 'out'),
                                   ('cache_read_per_m', 'cache-read'),
                                   ('cache_write_per_m', 'cache-write')):
                    v = getattr(ep, key, 0) or 0
                    if v > 0:
                        rate_parts.append(f'${self._fmt_per_m(v)}/M {label}')
                if rate_parts:
                    lines.append(f'  Pricing: {', '.join(rate_parts)}')

                # Limits: max input always shown; rate/concurrency render their
                # value when an explicit cap is set, else "unlimited" (a
                # non-positive value means no cap). max_tokens is shown only when
                # set (>0) since 0 means "use global default", not unlimited.
                max_input = int(getattr(ep, 'max_input_tokens', 0) or 0)
                rate_rpm = int(getattr(ep, 'rate_limit_rpm', 0) or 0)
                conc = int(getattr(ep, 'concurrency_limit', 0) or 0)
                max_out = int(getattr(ep, 'max_tokens', 0) or 0)
                limit_parts = [
                    f'max input {max_input} tok',
                    f'rate {rate_rpm} rpm' if rate_rpm > 0 else 'rate unlimited',
                    f'concurrency {conc}' if conc > 0 else 'concurrency unlimited',
                ]
                if max_out > 0:
                    limit_parts.append(f'max output {max_out} tok')
                lines.append(f'  Limits: {'; '.join(limit_parts)}')

            return '\n'.join(lines)
        except Exception as e:  # noqa: BLE001 — agents view must never break here
            logger.debug(f"system_info primary endpoint render failed: {e}")
            return ''

    def _render_mcp_servers(self) -> str:
        """Render the list of enabled MCP servers (name + description if present).

        Reads config/mcp_servers.json; a server counts as enabled when its
        ``enabled`` flag is True or absent (absent defaults to enabled, matching
        the MCP config validator). Never raises; degrades to a clear message if
        the config is missing, empty, or unreadable.
        """
        path = _mcp_servers_path()
        if not path.exists():
            return '  (no MCP servers configured)'
        try:
            with open(path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception as e:
            return f'  (MCP config unreadable: {e})'
        if not isinstance(cfg, dict) or not cfg:
            return '  (no MCP servers configured)'
        lines = []
        for name, spec in cfg.items():
            if not isinstance(spec, dict) or not spec.get('enabled', True):
                continue
            desc = spec.get('description')
            desc = desc.strip() if isinstance(desc, str) else ''
            lines.append(f"  - {name} — {desc}" if desc else f"  - {name}")
        return '\n'.join(lines) if lines else '  (no MCP servers enabled)'

    def _query_nvidia_gpus(self) -> list:
        """Query per-GPU status via nvidia-smi.

        Returns a list of dicts keyed index/name/total_mb/used_mb/free_mb/util
        (memory in MB, util as integer percent). An empty list is the single
        "unavailable" signal the renderer relies on — it covers both "no
        NVIDIA GPU" and "nvidia-smi missing/failed/timed out", which render
        identically. Never raises.
        """
        import shutil
        import subprocess
        try:
            exe = shutil.which('nvidia-smi')
            if not exe:
                return []
            cmd = [
                exe,
                '--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu',
                '--format=csv,noheader,nounits',
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
            if result.returncode != 0:
                return []
            gpus = []
            for line in result.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(',')]
                if len(parts) != 6:
                    continue  # malformed line
                try:
                    gpus.append({
                        'index': parts[0],
                        'name': parts[1],
                        'total_mb': int(parts[2]),
                        'used_mb': int(parts[3]),
                        'free_mb': int(parts[4]),
                        'util': int(parts[5]),
                    })
                except (ValueError, IndexError):
                    continue
            return gpus
        except Exception as e:
            logger.debug(f"nvidia-smi GPU query failed: {e}")
            return []

    def _available_help_topics(self) -> list:
        """Return the full list of valid system_info(help=...) topics.

        The special-cased topics (agents, telemetry) plus the YAML-defined
        reference sections. Never raises; degrades to the special-cased set
        if the help file is unavailable.
        """
        topics = ['agents', 'telemetry']
        try:
            import yaml
            path = _help_file_path()
            if path.is_file():
                data = yaml.safe_load(path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    for k in data:
                        s = str(k)
                        if not s.startswith('_') and s not in topics:
                            topics.append(s)
        except Exception as e:
            logger.debug(f"help topics unavailable: {e}")
        return topics

    def _render_system_resources(self) -> str:
        """Render a host system-resources summary (CPU, RAM, GPU/VRAM).

        Each source is isolated in its own try/except so a missing source
        (no psutil, no nvidia-smi) never suppresses the others and never
        crashes the tool. psutil and nvidia-smi are imported lazily to avoid
        a hard module-level dependency. Returns a short note only when no
        source yields anything.
        """
        lines = []

        # CPU: model + core count (+ live load when psutil is importable).
        try:
            model = platform.processor() or platform.machine() or 'unknown'
            cores = os.cpu_count()
            cores_part = f"{cores} cores" if cores else 'n/a cores'
            try:
                import psutil
                load_part = f", {psutil.cpu_percent(interval=0.1):.0f}% load"
            except Exception:
                load_part = ''
            lines.append(f"  CPU: {model} ({cores_part}{load_part})")
        except Exception as e:
            logger.debug(f"CPU info unavailable: {e}")

        # RAM: used / total in GB with used percent.
        try:
            import psutil
            vm = psutil.virtual_memory()
            lines.append(
                f"  RAM: {vm.used / 1024**3:.1f} / {vm.total / 1024**3:.1f} GB ({vm.percent:.0f}%)"
            )
        except Exception as e:
            logger.debug(f"RAM info unavailable: {e}")

        # GPU/VRAM: one line per GPU; an empty query result renders the single
        # "no info" line. _query_nvidia_gpus never raises, but the outer guard
        # keeps a future change from breaking the whole section.
        try:
            gpus = self._query_nvidia_gpus()
            if gpus:
                for g in gpus:
                    lines.append(
                        f"  GPU {g['index']}: {g['name']}  "
                        f"VRAM {g['used_mb']} / {g['total_mb']} MB ({g['util']}% util)"
                    )
            else:
                lines.append('  GPU: (no NVIDIA GPU info available)')
        except Exception as e:
            logger.debug(f"GPU info unavailable: {e}")

        return '\n'.join(lines) if lines else '  (system resources unavailable)'

    def call(self, params: str, **kwargs) -> str:
        # Optional help mode — return an editable reference section instead of system info.
        try:
            p = params if isinstance(
                params, dict) else (json.loads(params) if isinstance(params, str) and params.strip() else {})
            if not isinstance(p, dict):
                p = {}
            help_key = str(p.get('help') or '').strip()
        except Exception:
            help_key = ''
        if help_key:
            # Live telemetry dump is computed from the running pool, not static YAML.
            if help_key.lower() == 'telemetry':
                return self._render_telemetry_dump()
            # Live agent-class list (name + tagline + active tools), computed from the
            # pool — same accessors as /api/agents, not static YAML.
            if help_key.lower() == 'agents':
                return self._render_agents()
            return self._render_help(help_key)

        # Just return the information as a string
        now = datetime.datetime.now()
        os_info = f"{platform.system()} {platform.release()} ({platform.version()})"
        py_version = sys.version

        # Determine current history and agent name
        agent_name = kwargs.get('agent_instance_name') or self.agent_name or 'orchestrator'
        history = kwargs.get('messages')
        agent_obj = kwargs.get('agent_obj')

        # Resolve instance early for cache pool, template access, and max_context detection
        inst = None
        if hasattr(self, 'agent_pool') and self.agent_pool:
            try:
                inst = self.agent_pool.get_instance(agent_name)
            except Exception as e:
                logger.debug(f"Failed to get instance '{agent_name}': {e}")

        # Fallback to agent pool if messages not in kwargs
        if not history and hasattr(self, 'agent_pool') and self.agent_pool:
            history = self.agent_pool.get_conversation(agent_name)

        history = history or []
        stats_str = f"Current Agent ({agent_name}) History Length: {len(history)} messages"

        # Max context detection
        max_context = 'Unknown'
        if agent_obj:
            # _get_max_tokens requires an instance argument - use defensive guards
            if hasattr(agent_obj, '_get_max_tokens') and inst is not None:
                try:
                    max_context = agent_obj._get_max_tokens(inst)
                except Exception as e:
                    logger.warning(f"Failed to call _get_max_tokens for {agent_name}: {e}")
            elif hasattr(agent_obj, 'llm') and hasattr(agent_obj.llm, 'cfg'):
                from agent_cascade.settings import DEFAULT_MAX_INPUT_TOKENS
                cfg = agent_obj.llm.cfg
                max_context = cfg.get(
                    'generate_cfg',
                    {}).get('max_input_tokens') or cfg.get('max_input_tokens') or DEFAULT_MAX_INPUT_TOKENS

        # Use centralized history stats (tokens & words)
        try:
            stats = get_history_stats(history)
            # Add the tool-schema payload (sent as tools=, absent from the message list).
            schema_tokens = 0
            try:
                from agent_cascade.engine.helpers import estimate_instance_tool_schema_tokens
                schema_tokens = estimate_instance_tool_schema_tokens(inst, self.agent_pool)
            except Exception:
                schema_tokens = 0
            total_tokens = stats['tokens'] + int(schema_tokens)
            total_words = stats['words']
            stats_str += (f"\nCurrent Agent Usage: ~{total_tokens} tokens (incl. tool schemas), "
                          f"~{total_words} words (Max Context: {max_context})")
        except Exception as e:
            logger.warning(f"Failed to calculate stats for {agent_name}: {e}")

        if hasattr(self, 'agent_pool') and self.agent_pool:
            stats_str = f"Number of running sessions: {len(self.agent_pool.instance_conversations)}\n" + stats_str

            # Turn info — show current turn / max turns from instance state
            if inst is not None:
                effective_max = getattr(inst, 'max_turns', None) or DEFAULT_MAX_TURNS
                stats_str += f"\nCurrent Turn: {inst._current_turn} / {effective_max}"
                # Slice 4: self-scoped budget/cost — present only when configured.
                budget_block = self._render_budget_block(inst)
                if budget_block:
                    stats_str += '\n' + budget_block
        else:
            stats_str = 'Agent Pool not connected.\n' + stats_str

        # Tools information — Fix #6: Use template from pool for ExecutionEngine compatibility
        tools_str = ''
        template = None
        if agent_obj:
            # Reuse `inst` from early lookup to get template (works for both Agent and ExecutionEngine paths)
            if inst is not None and hasattr(self.agent_pool, 'get_template'):
                agent_class = getattr(inst, 'agent_class', 'orchestrator')
                template = self.agent_pool.get_template(agent_class)

            # Fallback to agent_obj attributes if template not available
            if template and hasattr(template, 'function_map'):
                all_tools = sorted(template.function_map.keys())
                active_tools_schemas = template._get_active_functions(
                    pool=self.agent_pool) if hasattr(template, '_get_active_functions') else []
            elif hasattr(agent_obj, 'function_map'):
                all_tools = sorted(agent_obj.function_map.keys())
                active_tools_schemas = agent_obj._get_active_functions(
                    pool=self.agent_pool) if hasattr(agent_obj, '_get_active_functions') else []
            else:
                all_tools = []
                active_tools_schemas = []

            active_tools = sorted([t['name'] for t in active_tools_schemas]) if isinstance(active_tools_schemas,
                                                                                           list) else []
            # Show all tools and which are disabled (if any) — enabled is redundant since full schemas are provided
            disabled = set(all_tools) - set(active_tools)
            tools_str = f"Available Tools: {', '.join(all_tools)}\n"
            if disabled:
                tools_str += f"Disabled Tools: {', '.join(sorted(disabled))}\n"

        # Update max_context from template if still Unknown
        if max_context == 'Unknown' and template and hasattr(template, 'llm') and template.llm:
            from agent_cascade.settings import DEFAULT_MAX_INPUT_TOKENS
            cfg = template.llm.cfg
            max_context = cfg.get('generate_cfg',
                                  {}).get('max_input_tokens') or cfg.get('max_input_tokens') or DEFAULT_MAX_INPUT_TOKENS

        # Resolve Model and API base — primary source is template.llm (works for ExecutionEngine path too)
        model = 'Unknown'
        api_base = 'Unknown'

        # Primary: template.llm (works for both Agent and ExecutionEngine paths)
        if template and hasattr(template, 'llm') and template.llm:
            model = getattr(template.llm, 'model', 'Unknown') or 'Unknown'
            if hasattr(template.llm, 'cfg'):
                cfg = template.llm.cfg
                api_base = cfg.get('api_base') or cfg.get('base_url') or cfg.get('model_server') or 'Unknown'

        # Fallback: agent_obj.llm (works when agent_obj is an Agent instance)
        if model == 'Unknown' and agent_obj and hasattr(agent_obj, 'llm') and agent_obj.llm:
            model = getattr(agent_obj.llm, 'model', 'Unknown')
            if hasattr(agent_obj.llm, 'cfg'):
                cfg = agent_obj.llm.cfg
                api_base = cfg.get('api_base') or cfg.get('base_url') or cfg.get('model_server') or 'Unknown'

        # Workspace and Folders information (with Docker container mount paths)
        default_ws = DEFAULT_WORKSPACE
        ro_folders = []
        rw_folders = []
        has_docker_context = False
        if hasattr(self, 'agent_pool') and self.agent_pool and self.agent_pool.operation_manager:
            om = self.agent_pool.operation_manager
            default_ws = str(om.base_dir)
            ro_folders = [str(p) for p in om.extra_work_folders_ro]
            rw_folders = [str(p) for p in om.extra_work_folders_rw]
            has_docker_context = True

        # Build folder info with Docker mount mappings (mirrors code_interpreter.py validation logic)
        # Only claim Docker paths when operation_manager is available (Docker context exists)
        if has_docker_context:
            folders_info = f"Default Workspace (RW): {default_ws} → /workspace (Docker)\n"

            # Filter and mount RW folders: validate dir exists + security check via _is_path_allowed
            allowed_prefixes = {os.path.realpath(default_ws)} if default_ws else set()
            for fp in [*rw_folders, *ro_folders]:  # include both RW and RO (mirrors code_interpreter.py:644)
                allowed_prefixes.add(os.path.realpath(fp))

            rw_idx = 0
            if rw_folders:
                folders_info += 'Additional RW Folders:\n'
                for folder in rw_folders:
                    abs_path = os.path.realpath(folder)
                    # Skip if path doesn't exist as a directory
                    if not os.path.isdir(abs_path):
                        continue
                    # Skip if path is outside allowed directories (security check)
                    if not self._is_path_allowed(abs_path, allowed_prefixes):
                        continue
                    docker_path = f'/extra_rw_{rw_idx}'
                    folders_info += f"  - {folder} → {docker_path} (Docker)\n"
                    rw_idx += 1

            # Filter and mount RO folders: same validation as RW but with read-only Docker flag
            ro_idx = 0
            if ro_folders:
                folders_info += 'Additional RO Folders:\n'
                for folder in ro_folders:
                    abs_path = os.path.realpath(folder)
                    # Skip if path doesn't exist as a directory
                    if not os.path.isdir(abs_path):
                        continue
                    # Skip if path is outside allowed directories (security check)
                    if not self._is_path_allowed(abs_path, allowed_prefixes):
                        continue
                    docker_path = f'/extra_ro_{ro_idx}'
                    folders_info += f"  - {folder} → {docker_path} (Docker, read-only)\n"
                    ro_idx += 1
        else:
            # No Docker context — don't claim Docker mount paths
            folders_info = f"Default Workspace (RW): {default_ws} → /workspace (no Docker context)\n"

        # Cache pool state section
        if inst is not None and hasattr(inst, 'cache_pool') and inst.cache_pool is not None:
            cache_state = f"\n{inst.cache_pool.get_state_summary(max_display=10)}"
        else:
            cache_state = '  (not initialized)'

        # AC Server address — resolve the API server host:port this tool runs under.
        # Primary source is agent_pool.server_info set by the launcher; fall back to
        # AGENT_CASCADE_PORT env var, then the multi-agent default port (8765).
        ac_server_str = 'Unknown'
        try:
            si = self.agent_pool.server_info if self.agent_pool else None
            if isinstance(si, (tuple, list)) and len(si) == 2 and si[0] and si[1]:
                host, port = si[0], si[1]
            else:
                env_port = os.getenv('AGENT_CASCADE_PORT')
                try:
                    port = int(env_port) if env_port is not None else 8765
                except (ValueError, TypeError):
                    port = 8765
                host = '0.0.0.0'
            ac_server_str = f"http://{host}:{port}" + (' (all interfaces)' if str(host) == '0.0.0.0' else '')
        except Exception as e:
            logger.warning(f"Failed to resolve AC server address: {e}")
            ac_server_str = 'Unknown'

        # BUG_0091: report the host TZ offset so agents can reconcile sandbox (UTC) timestamps
        # with host-local log timestamps. The code_interpreter container always runs UTC.
        host_tz = now.astimezone().tzname() or 'UTC'
        host_offset_h = now.astimezone().utcoffset().total_seconds() / 3600 if now.astimezone().utcoffset() else 0
        tz_line = f"  Host Timezone: {host_tz} (UTC{host_offset_h:+.0f})\n" if host_offset_h != 0 else f"  Host Timezone: {host_tz} (UTC)\n"

        info = (f"--- System Information ---\n"
                f"  OS: {os_info}\n"
                f"  Current Time: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"{tz_line}"
                f"  Python Version: {py_version}\n"
                f"  API Endpoint: {api_base}\n"
                f"  Model Used: {model}\n"
                f"\n--- System Resources ---\n"
                f"{self._render_system_resources()}\n"
                f"\n--- AgentCascade Server ---\n"
                f"  AgentCascade Version: {AC_VERSION}\n"
                f"  Server Address: {ac_server_str}\n"
                f"  Help Topics: {', '.join(self._available_help_topics())}\n"
                f"\n--- MCP Servers ---\n"
                f"{self._render_mcp_servers()}\n"
                f"\n--- Workspace & Permissions ---\n"
                f"{self._indent_block(folders_info)}"
                f"\n--- Session Stats ---\n"
                f"{self._indent_block(stats_str)}\n"
                f"\n--- Cache Pool State ---{cache_state}")
        return info
