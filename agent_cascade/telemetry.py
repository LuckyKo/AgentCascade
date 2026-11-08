"""
Telemetry Collector — Performance & Usage Tracking for A/B Testing.

Captures structured events (LLM calls, tool calls, agent instance delegations,
loop detections, compressions) and persists them as JSONL for offline analysis.

Each event is tagged with a config fingerprint so different framework configurations
(prompts, sampling params, model selection) can be compared.
"""

import collections
import datetime
import hashlib
import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from agent_cascade.cost import tokens_to_cost
from agent_cascade.log import _AppLoggerProxy

# Late-bound delegate to the live app logger (see agent_cascade.log._AppLoggerProxy).
# A plain ``logging.getLogger('agent_cascade.telemetry')`` is a SIBLING of the
# handler-bearing 'agent_cascade_logger', so its DEBUG/INFO records never reach
# console.log (same class as BUG_0103).
_logger = _AppLoggerProxy()

# Module-level RLock protecting ALL telemetry shared state and file I/O.
# RLock (reentrant) prevents deadlocks if methods call each other while holding the lock.
# Telemetry is not on the hot path, so a single coarse-grained lock avoids complexity.
_telemetry_lock = threading.RLock()

# Truncation limit for error messages in telemetry events to avoid bloating logs
MAX_ERROR_MESSAGE_LENGTH = 200

# Cache-waste diagnosis thresholds (borrow-audit #7, mirrors pi cache-stats.ts).
# A detected miss is only counted as waste when the reprocessed prompt exceeds
# this floor — small deltas are normal prompt variation, not wasted recompute.
CACHE_MISS_NOISE_FLOOR_TOKENS = 1024
# An idle gap larger than this between two calls on the same instance is treated
# as an evicted (cold) KV cache rather than an active-cache miss.
CACHE_IDLE_TTL_MS = 5 * 60 * 1000

from agent_cascade.instance_id import get_instance_id, make_instance_dir
from agent_cascade.settings import DEFAULT_RECENT_EVENT_COUNT, MAX_EVENTS_IN_MEMORY, MODEL_PRICING


class TelemetryCollector:
    """Collects and persists telemetry events for agent performance tracking."""

    def __init__(self, log_dir: str = 'workspace/telemetry', instance_id: str = ''):
        if not instance_id:
            instance_id = get_instance_id()

        self.log_dir = Path(make_instance_dir(log_dir)) if instance_id else Path(log_dir)

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"Cannot create telemetry directory {self.log_dir}: {e}") from e

        # Session-level tracking — use _now_iso() for consistent UTC timestamps
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_str = now_dt.isoformat()
        # Include microseconds in session_id to avoid collisions when multiple sessions start simultaneously
        self.session_id = now_dt.strftime('%Y%m%d_%H%M%S%f')
        self.log_path = self.log_dir / f"telemetry_{self.session_id}.jsonl"

        # In-memory event buffer — deque with maxlen gives O(1) rotation
        self.events: collections.deque[Dict] = collections.deque(maxlen=MAX_EVENTS_IN_MEMORY)

        # Active turn tracking (keyed by instance_name)
        self._active_turns: Dict[str, Dict] = {}

        # Active LLM call tracking (keyed by instance_name)
        self._active_llm_calls: Dict[str, Dict] = {}

        # Cache-waste diagnosis (borrow-audit #7): per-instance snapshot of the
        # PREVIOUS call, used to detect needlessly reprocessed prompts. Keyed by
        # instance_name -> {prev_prompt_tokens, prev_ts_ms, prev_model, reported_cache}.
        # reported_cache is STICKY: once a call on this instance reports cached_tokens > 0
        # it stays True forever, so a provider that never reports caching (never sets it
        # True) is never mistaken for a true cache miss (avoids false-positive waste).
        self._cache_scan_state: Dict[str, Dict] = {}

        # Active tool call tracking (keyed by instance_name + tool_name)
        self._active_tool_calls: Dict[str, Dict] = {}

        # Session-level aggregates (updated on each event)
        self._session_stats = {
            'total_runs': 0,
            'total_user_runs': 0,
            'total_llm_calls': 0,
            'total_tool_calls': 0,
            'total_input_tokens_est': 0,
            'total_output_tokens_est': 0,
            # Output tokens produced by streaming calls only — the numerator that
            # matches total_streaming_time_ms for TPS (BUG_0133).
            'total_streamed_output_tokens': 0,
            'total_llm_latency_ms': 0,
            'total_ttft_ms': 0,
            'total_streaming_time_ms': 0,
            'total_tool_latency_ms': 0,
            # Sum of turn wall-clock durations across all agents. Can exceed real
            # elapsed time when agents run in parallel (same caveat as wallclock_ms).
            'total_turn_time_ms': 0,
            'call_agent_calls': 0,
            'call_agent_latency_ms': 0,
            'total_loops_detected': 0,
            'loops_outer': 0,
            'loops_inner': 0,
            'total_auto_continues': 0,
            'total_retries': 0,
            'total_compressions': 0,
            'write_failures': 0,  # Track write failures for diagnostics
            'tool_calls_by_name': defaultdict(int),
            'tool_failures_by_name': defaultdict(int),
            'tool_latency_by_name': defaultdict(float),
            'llm_calls_by_model': defaultdict(int),
            'agent_instance_calls': 0,
            # Skill Advisor (AUTO Skill Helper — Advanced mode) counters
            'skill_advisor_calls': 0,
            'skill_advisor_denials': 0,
            'skill_advisor_fallbacks': 0,
            # RFC 9211 Cache-Status (prompt-cache hit/miss) counters
            'llm_cache_hits': 0,
            'llm_cache_misses': 0,
            'llm_cache_unknown': 0,
            # Cache-waste diagnosis (borrow-audit #7): prompt tokens needlessly
            # reprocessed because a warm KV cache could not be reused, plus how
            # many such misses were detected. llm_wasted_cost ($-only) accumulates
            # only when MODEL_PRICING has the model; token totals are the primary
            # provider-agnostic metric and are always present.
            'llm_wasted_tokens': 0,
            'llm_waste_count': 0,
            'llm_wasted_cost': 0.0,
            # Session spend ($) across all priced calls (per-endpoint pricing).
            # Accumulated only when an endpoint carries pricing; surfaced in the
            # summary ONLY when > 0 so unpriced installs see no cost key.
            'session_cost': 0.0,
        }

        # Per-config aggregates for A/B comparison
        self._config_stats: Dict[str, Dict] = {}

        # Per-ENDPOINT cache attribution (BUG_0273 Option A). The model-only
        # config_fingerprint groups A/B rows by model and cannot represent the
        # endpoint that actually served a call, so a use_last_endpoint agent's
        # fresh-reusable-instance cache misses were booked to the wrong model
        # row. Cache hits/misses are instead attributed here, keyed by the
        # resolved endpoint's normalized ``api_base|model`` (cache_attribution_key).
        # Additive: session-global llm_cache_hits/misses are unchanged.
        self._cache_stats: Dict[str, Dict] = {}

        # Per-instance TOTAL-token accumulator (cost/token-budget Slice 2).
        # Session-lifetime, keyed by instance name; updated at the single
        # record_llm_call_end choke point (under _telemetry_lock). Backs the
        # tokens-mode budget warnings (run_loop) and the per-instance "budget
        # consumed" reads that Slices 3 & 4 use. TOTAL (prompt+completion) —
        # distinct from the per-class `tokens_generated` counter, which is
        # completion-only. Each value tracks:
        #   total_tokens : prompt+completion consumed, summed session-lifetime
        #                  over every call (prompt is the server ground-truth
        #                  once classified, else the char estimate)
        #   cost         : Σ tokens_to_cost over CACHE-CLASSIFIED priced calls,
        #                  accumulated with the identical usage tuple + gate as
        #                  session_cost, so get_instance_cost ties out with it
        #   priced       : True once any cache-classified priced call was seen
        self._instance_usage: Dict[str, Dict[str, float]] = {}

        # Per-agent-class aggregates for the "Agent Class Usage" table.
        # Keyed by agent_class (one model can serve multiple classes now that the
        # fingerprint is model-only), so this is a separate accumulator from _config_stats.
        self._agent_class_stats: Dict[str, Dict] = {}

        # Per-skill usage aggregates for the "Skill Usage" table.
        # Keyed by skill name; each entry tracks how many times it was injected
        # this session, which agent classes loaded it, and the acquisition-mode
        # breakdown (explicit / auto / advisor / self-augmentation / runtime).
        self._skill_usage_stats: Dict[str, Dict] = {}

        # Guard against duplicate session_end events (BUG 8 fix)
        self._session_ended = False

        # Keep file handle open to avoid per-event open/close overhead
        self._log_file = None
        try:
            self._log_file = open(self.log_path, 'a', encoding='utf-8')
        except Exception as e:
            _logger.warning('Failed to open telemetry log file %s: %s', self.log_path, e)

        # Write session header
        self._write_event({
            'type': 'session_start',
            'session_id': self.session_id,
            'timestamp': now_str,
        })

    # ── Config Fingerprinting ─────────────────────────────────────────────

    @staticmethod
    def fingerprint_config(
        model: str = '',
        generate_cfg: Optional[Dict] = None,
        system_prompt: str = '',
        tools: Optional[List[str]] = None,
        api_base: str = '',
    ) -> str:
        """
        Create a stable hash fingerprint from the current agent configuration.

        This is the A/B grouping key: one row per model. The key is the MODEL ONLY —
        runs are grouped by model for A/B comparison regardless of endpoint, sampling
        params, system prompt, or tool set (so different endpoints serving the same
        model collapse into a single row). ``generate_cfg`` / ``system_prompt`` /
        ``tools`` / ``api_base`` are kept in the signature purely for call-site
        compatibility; they no longer affect the fingerprint (the "prompt print"
        grouping concern was removed). Do not assume sampling params matter here.

        NOTE: keep passing these args at call sites (e.g. engine/core.py) — do not
        "clean up" the unused params, it would break callers that still supply them.

        Returns a stable 12-char hex string so it stays compatible with
        ``_config_stats`` keying and JSONL export.
        """
        raw = model or ''
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    @staticmethod
    def cache_attribution_key(api_base: str = '', model: str = '') -> str:
        """Build the cache-attribution key from a RESOLVED endpoint.

        Distinct from ``fingerprint_config`` (model-only, one row per model for
        A/B token/cost grouping). The prompt KV cache belongs to the endpoint
        that actually served the call, not the configured model, so cache
        hits/misses are attributed to this key instead of the model-only
        fingerprint (BUG_0273). Derived from ``instance._last_endpoint_config``
        (engine/llm_call.py), the ground-truth endpoint that ran the call
        regardless of router fallbacks / ``use_last_endpoint``.

        Normalization is deliberately light so the key stays a faithful, readable
        label: ``api_base`` is lowercased and its trailing slash stripped (scheme
        and host are case-insensitive), the model is kept verbatim (model names
        are case-sensitive to the server). Returns ``''`` when the ``api_base``
        host is unknown — an endpoint key requires a host, so a model-only value
        (or a hostless/blank api_base) must fall back to the model-only
        fingerprint rather than form a ``|model`` key that would bypass fallback.
        """
        base = (api_base or '').strip().rstrip('/').lower()
        mdl = (model or '').strip()
        if not base:
            return ''
        return f'{base}|{mdl}'

    @staticmethod
    def describe_config(
        model: str = '',
        generate_cfg: Optional[Dict] = None,
        tools: Optional[List[str]] = None,
        api_base: str = '',
    ) -> Dict:
        """Return a human-readable snapshot of the config at first sight.

        The fingerprint is model-only (see ``fingerprint_config``), so this
        description is a sample of the first turn that used the model, not a
        property of the whole row (BUG_0131).
        """
        cfg = generate_cfg or {}
        return {
            'model': model,
            'api_base': api_base,
            'temperature': cfg.get('temperature'),
            'top_p': cfg.get('top_p'),
            'top_k': cfg.get('top_k'),
            'min_p': cfg.get('min_p'),
            'max_tokens': cfg.get('max_tokens'),
            'max_input_tokens': cfg.get('max_input_tokens'),
            'presence_penalty': cfg.get('presence_penalty'),
            'frequency_penalty': cfg.get('frequency_penalty'),
            'repetition_penalty': cfg.get('repetition_penalty'),
            'tools_count': len(tools or []),
        }

    # ── Helper: per-config stats initialization ───────────────────────

    def _ensure_config_stats(self, fingerprint: str, config_description: Optional[Dict] = None):
        """Ensure per-config stats dict exists for *fingerprint*, creating it if needed.

        ``config_description`` is captured on first sight and intentionally not
        refreshed: the fingerprint groups by model only, so sampling/endpoint
        details are not part of the row identity. Callers must not present this
        description's sampling values as describing the whole row (BUG_0131).
        """
        if fingerprint and fingerprint not in self._config_stats:
            self._config_stats[fingerprint] = {
                'config_description': config_description or {},
                'runs': 0,
                'llm_calls': 0,
                'cache_hits': 0,
                'cache_misses': 0,
                # Cache-waste diagnosis (borrow-audit #7), additive; read with
                # .get(...,0) so rows persisted before this field still work.
                'wasted_tokens': 0,
                'waste_count': 0,
                'tool_calls': 0,
                'input_tokens_est': 0,
                'output_tokens_est': 0,
                # Streaming-only output tokens: the TPS numerator that matches
                # total_streaming_time_ms (BUG_0133).
                'streamed_output_tokens': 0,
                'total_duration_ms': 0,
                'total_llm_latency_ms': 0,
                'total_ttft_ms': 0,
                'total_streaming_time_ms': 0,
                'loops_detected': 0,
                'retries': 0,
                'total_compressions': 0,
                'tool_calls_by_name': defaultdict(int),
                'tool_failures_by_name': defaultdict(int),
            }

    def _ensure_cache_stats(self, cache_key: str):
        """Ensure the per-endpoint cache-attribution bucket exists for *cache_key*.

        ``cache_key`` is built by ``cache_attribution_key`` from the resolved
        endpoint. Unlike ``_ensure_config_stats`` this row carries ONLY cache
        counters — token/cost/latency/A-B stay on the model-only ``_config_stats``
        row (BUG_0273 Option A). Returns the (created or existing) bucket so callers
        can subscript it directly. Caller must hold ``_telemetry_lock``.
        """
        if cache_key and cache_key not in self._cache_stats:
            self._cache_stats[cache_key] = {
                'cache_hits': 0,
                'cache_misses': 0,
            }
        return self._cache_stats.get(cache_key)

    # ── Event Recording (all thread-safe via _telemetry_lock) ──────────────

    def record_turn_start(
        self,
        instance_name: str,
        config_fingerprint: str = '',
        config_description: Optional[Dict] = None,
        agent_class: str = '',
    ):
        """Mark the start of a new agent turn.

        ``agent_class`` is optional; when non-empty it drives the per-agent-class
        usage accumulator (see ``get_agent_class_summary()``).
        """
        with _telemetry_lock:
            # A second start without an end means the previous turn will never be
            # recorded (the engine closes turns in run()'s finally, so this is an
            # upstream bug). Report the window that is about to be dropped instead
            # of silently overwriting it — whether or not the fingerprint changed.
            existing_turn = self._active_turns.get(instance_name)
            if existing_turn:
                elapsed_ms = (time.perf_counter() - existing_turn['start_time']) * 1000
                _logger.warning(
                    'Instance %r started a new turn while the previous one was still '
                    'open (%.1f ms, fingerprint %s -> %s); the previous turn will not '
                    'be recorded. Missing record_turn_end?',
                    instance_name, elapsed_ms,
                    existing_turn.get('config_fingerprint') or '(none)',
                    config_fingerprint or '(none)',
                )

            self._active_turns[instance_name] = {
                'start_time': time.perf_counter(),
                'config_fingerprint': config_fingerprint,
                'config_description': config_description or {},
                'agent_class': agent_class,
                'llm_calls': 0,
                'tool_calls': 0,
                'tool_calls_detail': [],
                'input_tokens_est': 0,
                'output_tokens_est': 0,
                # Per-turn time breakdown, flushed onto the turn_end event so the
                # JSONL can reproduce the Config/Agent-Class tables offline (BUG_0129).
                'llm_latency_ms': 0.0,
                'ttft_ms': 0.0,
                'streaming_time_ms': 0.0,
                'tool_latency_ms': 0.0,       # excludes call_agent
                'call_agent_latency_ms': 0.0,
                'loops_detected': 0,
                'retries': 0,
                'compressions': 0,
            }

            # Initialize per-config stats on first turn — use helper method
            self._ensure_config_stats(config_fingerprint, config_description)

            # Live per-config / per-class turn count (moved from record_turn_end).
            # A run counts as an engagement the moment it starts.
            if config_fingerprint:
                self._config_stats[config_fingerprint]['runs'] += 1
            if agent_class:
                self._ensure_agent_class_stats(agent_class)['runs'] += 1

        event = {
            'type': 'turn_start',
            'instance': instance_name,
            'config_fingerprint': config_fingerprint,
            'agent_class': agent_class,
            'timestamp': _now_iso(),
        }
        self._write_event(event)

    def record_turn_end(self, instance_name: str):
        """Mark the end of an agent turn and aggregate stats."""
        with _telemetry_lock:
            turn = self._active_turns.pop(instance_name, None)
            if not turn:
                # Normal for the two terminal-stop returns that happen before
                # record_turn_start; the engine calls end from run()'s finally for
                # every exit path (BUG_0127), so this is not an error.
                _logger.debug(
                    'record_turn_end called for %r but no matching turn_start found.',
                    instance_name,
                )
                return

            duration_ms = (time.perf_counter() - turn['start_time']) * 1000
            fp = turn['config_fingerprint']

            event = {
                'type': 'turn_end',
                'instance': instance_name,
                'config_fingerprint': fp,
                'agent_class': turn.get('agent_class') or '',
                'duration_ms': round(duration_ms, 1),
                'llm_calls': turn['llm_calls'],
                'tool_calls': turn['tool_calls'],
                'tool_calls_detail': turn['tool_calls_detail'],
                'input_tokens_est': turn['input_tokens_est'],
                'output_tokens_est': turn['output_tokens_est'],
                # Per-turn time breakdown (BUG_0129): lets the exported JSONL be
                # reconciled against the live Config/Agent-Class tables.
                'llm_latency_ms': round(turn['llm_latency_ms'], 1),
                'ttft_ms': round(turn['ttft_ms'], 1),
                'streaming_time_ms': round(turn['streaming_time_ms'], 1),
                'tool_latency_ms': round(turn['tool_latency_ms'], 1),
                'call_agent_latency_ms': round(turn['call_agent_latency_ms'], 1),
                'loops_detected': turn['loops_detected'],
                'retries': turn['retries'],
                'compressions': turn['compressions'],
                'timestamp': _now_iso(),
            }

            # Update session stats (inside lock since RLock is reentrant)
            self._session_stats['total_runs'] += 1
            self._session_stats['total_turn_time_ms'] += duration_ms
            self._session_stats['total_retries'] += turn['retries']

            # Update per-config stats — duration is only known at turn end. The
            # other counters (turns, llm_calls, tool_calls, tokens, loops, retries,
            # total_compressions) are now accumulated live in the individual record_*
            # methods as events happen, so they must NOT be re-flushed here or they
            # would double-count.
            if fp:
                self._ensure_config_stats(fp, turn.get('config_description'))
                cs = self._config_stats[fp]
                cs['total_duration_ms'] += duration_ms

            # Update per-agent-class stats — same live-accumulation rule as above;
            # only total_time_ms is flushed here (duration known at end).
            agent_class = turn.get('agent_class') or ''
            if agent_class:
                acs = self._ensure_agent_class_stats(agent_class)
                acs['total_time_ms'] += duration_ms

        self._write_event(event)

    def record_user_turn(self, instance_name: str):
        """Count one user-initiated turn (a fresh agent run/budget). Thread-safe.

        ``instance_name`` is accepted for call-site symmetry with the other
        record_* methods; the counter is a session-level aggregate.
        """
        with _telemetry_lock:
            self._session_stats['total_user_runs'] += 1

    def _ensure_agent_class_stats(self, agent_class: str):
        """Ensure per-agent-class stats dict exists for *agent_class*, creating it if needed.

        Caller must hold ``_telemetry_lock``.
        """
        if agent_class not in self._agent_class_stats:
            self._agent_class_stats[agent_class] = {
                'runs': 0,
                'total_time_ms': 0,          # turn wall-clock (sum of turn_end durations)
                'total_llm_latency_ms': 0,   # sum of this class's LLM call latencies
                'total_streaming_time_ms': 0,
                'total_tool_latency_ms': 0,  # excludes call_agent (matches session total)
                'call_agent_latency_ms': 0,
                'tokens_generated': 0,
                'tool_calls': 0,
                'tool_failures': 0,
                'llm_calls': 0,
            }
        return self._agent_class_stats[agent_class]

    def record_llm_call_start(self, instance_name: str, input_tokens_est: int = 0,
                              model: str = '', pricing: Optional[Dict] = None,
                              cache_key: str = ''):
        """Mark the start of an LLM API call.

        If a call for ``instance_name`` is already in flight, the previous attempt
        never recorded an end (aborted by termination, or a caller that forgot to
        end it). Finalize it first so its latency/tokens are still counted instead
        of being silently overwritten by the new start (BUG_0128). The nested end
        call is safe under the reentrant telemetry lock.
        """
        with _telemetry_lock:
            if instance_name in self._active_llm_calls:
                _logger.warning(
                    'record_llm_call_start for %r while a previous call was still '
                    'active; finalizing the previous attempt (missing '
                    'record_llm_call_end?).',
                    instance_name,
                )
                self.record_llm_call_end(instance_name)
            self._active_llm_calls[instance_name] = {
                'start_time': time.perf_counter(),
                'input_tokens_est': input_tokens_est,
                'model': model,
                # Snapshot the turn's config fingerprint onto the active call so the
                # cache classifier in record_token_usage can attribute hits/misses to
                # the right config row (BUG_0260). Captured here, not read at
                # classification time, because a call can be finalized after its turn
                # closes (BUG_0128). Empty when no turn is open — such calls move only
                # the session-global counters.
                'config_fingerprint': self._active_turns.get(instance_name, {}).get('config_fingerprint', ''),
                # Cache-attribution key from the RESOLVED endpoint (BUG_0273),
                # snapshotted here parallel to the model-only fingerprint above.
                # Best-effort at start: the endpoint is only resolved later inside
                # _do_call, so the engine re-binds the authoritative key via
                # record_llm_call_endpoint once known. Empty here (first call, or a
                # hostless/blank api_base) means the classifier falls back to the
                # model-only fingerprint.
                'cache_key': cache_key,
                'first_token_time': 0,
                'completion_tokens': 0,  # Will be updated by record_token_usage callback if available
                'usage_details': None,
                # Per-endpoint pricing snapshotted per-call (not looked up by the
                # model-only fingerprint afterward — G1). Used to compute this
                # call's cost in record_llm_call_end. None = unpriced.
                'pricing': pricing,
            }

    def record_llm_first_token(self, instance_name: str):
        """Record Time To First Token (TTFT)."""
        with _telemetry_lock:
            call = self._active_llm_calls.get(instance_name)
            if call and call['first_token_time'] == 0:
                call['first_token_time'] = time.perf_counter()

    def record_llm_call_endpoint(self, instance_name: str, cache_key: str):
        """Bind the RESOLVED endpoint's cache-attribution key to the in-flight call.

        ``record_llm_call_start`` may carry only a best-effort (possibly stale, or
        empty on a first call) key because the endpoint is not resolved until
        inside the engine's ``_do_call``. Once ``_last_endpoint_config`` is written,
        the engine calls this with the authoritative ``cache_attribution_key`` so
        ``record_token_usage`` classifies against the endpoint that ACTUALLY served
        this call — not the previous call's endpoint under cursor rotation / router
        fallback (BUG_0273). A no-op when ``cache_key`` is empty, preserving the
        model-fingerprint fallback. Caller must have already called
        ``record_llm_call_start`` for ``instance_name``.
        """
        if not cache_key:
            return
        with _telemetry_lock:
            call = self._active_llm_calls.get(instance_name)
            if call is not None:
                call['cache_key'] = cache_key

    @staticmethod
    def classify_cache_hit(cached_tokens, prompt_tokens):
        """Classify a single LLM call's prompt-cache outcome from server-reported data.

        Uses the authoritative ``prompt_tokens_details.cached_tokens`` value that
        llama.cpp (and compatible OpenAI backends) report in the usage object — the
        exact number of prompt tokens served from the KV cache rather than reprocessed.
        This replaces the old TTFB-based ``Cache-Status`` header heuristic.

        Returns:
            "hit"   — a non-trivial share of the prompt was cached.
                      Floor: ``cached_tokens >= max(1, int(0.05 * prompt_tokens))`` so a
                      1-token overlap on a large prompt is not counted as a hit.
            "miss"  — usage present with a real prompt (prompt_tokens > 0) but no
                      meaningful cached share: ``cached_tokens`` is 0, absent, or below
                      the floor. (A backend that reports usage but omits the field means
                      nothing was served from cache.)
            None    — unknown: no usable prompt count (no usage / non-supporting backend).
                      Callers treat this as "not measured".

        Pure and total: never raises on bad input (bad values degrade to "miss").
        """
        try:
            pt = int(prompt_tokens or 0)
            if pt <= 0:
                # No prompt reported — cannot meaningfully classify a cache outcome.
                return None
            ct = int(cached_tokens or 0)  # absent / None → treated as 0 (a miss)
            floor = max(1, int(0.05 * pt))
            if ct >= floor:
                return 'hit'
            return 'miss'
        except (TypeError, ValueError):
            return None

    @staticmethod
    def detect_cache_waste(prev, prompt_tokens, cached_tokens, model, now_ms):
        """Estimate needlessly reprocessed prompt tokens for a call vs. its predecessor.

        Token-level, provider-agnostic cache-waste detector (borrow-audit #7), mirroring
        pi's ``cache-stats.ts`` ``detectMiss``. Compares this call against the previous
        classified call on the same instance (``prev`` = the per-instance snapshot) to
        decide whether the KV cache that *should* have been reusable was lost, and tag
        the likely root cause.

        Returns ``None`` when the call is not a meaningful miss:
          * no prior baseline (first call on the instance), or
          * no real prompt (``prompt_tokens <= 0``), or
          * a provider that has never reported caching — sticky ``reported_cache`` guard:
            ``cached_tokens == 0`` on such a provider means "not reported", not "missed",
            so counting it would be a false positive, or
          * reprocessed share at/below ``CACHE_MISS_NOISE_FLOOR_TOKENS`` (normal variation).

        Otherwise returns ``{'missed': int, 'root_cause': str, 'idle_ms': float}`` where
        ``root_cause`` is one of ``'model_change'`` (different model than prev — the cache
        key moved), ``'idle_ttl'`` (gap over ``CACHE_IDLE_TTL_MS`` — cache evicted), or
        ``'other'``. ``missed = min(prev_prompt, cur_prompt) - cached``: the portion of the
        overlapping prompt that was reprocessed rather than served from cache.

        Pure: mutates nothing and raises nothing on bad input (degrades to ``None``).
        """
        try:
            prompt_tokens = int(prompt_tokens or 0)
            cached_tokens = int(cached_tokens or 0)
            if prev is None or prompt_tokens <= 0:
                return None
            # Sticky reported_cache guard: a provider that never reports cached_tokens > 0
            # is never blamed for "missing" a cache it never claims to have.
            if cached_tokens == 0 and not prev.get('reported_cache', False):
                return None
            missed = min(int(prev.get('prev_prompt_tokens') or 0), prompt_tokens) - cached_tokens
            if missed <= CACHE_MISS_NOISE_FLOOR_TOKENS:
                return None
            idle_ms = now_ms - (prev.get('prev_ts_ms') or 0)
            if model != prev.get('prev_model'):
                root_cause = 'model_change'
            elif idle_ms > CACHE_IDLE_TTL_MS:
                root_cause = 'idle_ttl'
            else:
                root_cause = 'other'
            return {'missed': missed, 'root_cause': root_cause, 'idle_ms': idle_ms}
        except (TypeError, ValueError):
            return None

    def record_token_usage(
        self,
        instance_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        details: Optional[Dict] = None,
    ):
        """Record actual token usage from LLM API response.

        Called by the streaming layer when usage data arrives on the wire via _on_usage callback.
        Updates the active LLM call's input_tokens_est with ground-truth prompt_tokens,
        and stores completion_tokens for use in record_llm_call_end.
        """
        with _telemetry_lock:
            call = self._active_llm_calls.get(instance_name)
            if not call:
                _logger.debug('record_token_usage called without active LLM call for %s', instance_name)
                return

            # Update input_tokens_est with ground-truth value (was char-count estimate at call start)
            if prompt_tokens > 0:
                call['input_tokens_est'] = prompt_tokens

            # Store completion_tokens on the active call for use in record_llm_call_end
            call['completion_tokens'] = completion_tokens
            if details:
                call['usage_details'] = details

            # Authoritative cache hit/miss from server-reported cached_tokens
            # (llama.cpp / OpenAI usage object). Guarded by "cache_classified" so a call
            # is counted at most once even if the usage callback fires more than once.
            # Backends that don't report usable usage leave the call unmeasured.
            try:
                if not call.get('cache_classified'):
                    cached = None
                    if details and isinstance(details, dict):
                        cached = details.get('cached_tokens')
                    status = self.classify_cache_hit(cached, prompt_tokens)
                    if status is not None:
                        call['cache_classified'] = True
                        call['cache_status'] = status
                        # Snapshot the prompt count at classification time so the
                        # [KV_CACHE_MISS] line reports the value the classifier used,
                        # even if a subsequent multi-callback overwrites input_tokens_est.
                        call['classified_prompt_tokens'] = int(prompt_tokens)
                        # Prompt-cache attribution (BUG_0260 + BUG_0273 Option A).
                        # Prefer the cache key derived from the RESOLVED endpoint
                        # (threaded by record_llm_call_start); fall back to the
                        # model-only fingerprint only when no resolved endpoint is
                        # known. The resolved endpoint — not the turn-start config
                        # — owns the prompt KV cache, so its hits/misses must land
                        # on its own bucket, not the global model row (a
                        # use_last_endpoint agent's fresh-instance misses were
                        # inflating the configured model's miss count). Read once;
                        # the per-bucket counters move in lockstep with the
                        # session-global counters in this same branch, so there is
                        # no second classification. Session-global totals always
                        # move and stay the un-keyed source of truth.
                        cache_key = call.get('cache_key') or ''
                        fp = call.get('config_fingerprint') or ''
                        if status == 'hit':
                            self._session_stats['llm_cache_hits'] += 1
                            if cache_key:
                                self._ensure_cache_stats(cache_key)['cache_hits'] += 1
                            elif fp and fp in self._config_stats:
                                self._config_stats[fp]['cache_hits'] += 1
                        else:
                            self._session_stats['llm_cache_misses'] += 1
                            if cache_key:
                                self._ensure_cache_stats(cache_key)['cache_misses'] += 1
                            elif fp and fp in self._config_stats:
                                self._config_stats[fp]['cache_misses'] += 1
                            # Retained for the [KV_CACHE_MISS] debug line emitted in
                            # record_llm_call_end (todo.md:105 / BUG_0101 tracking).
                            call['cached_tokens'] = int(cached or 0)
            except Exception as e:
                _logger.debug('Cache classification from usage failed for %s: %s', instance_name, e)

    def record_llm_call_end(self, instance_name: str, output_tokens_est: int = 0, last_output=None):
        """Mark the end of an LLM API call."""
        with _telemetry_lock:
            call = self._active_llm_calls.pop(instance_name, None)
            if not call:
                return

            # Three-tier priority for output tokens:
            # (1) Ground-truth from streaming layer callback via record_token_usage()
            # (2) Caller-provided value if > 0 (backward compat during transition)
            # (3) Char-count estimate of last_output as final fallback
            actual_output = call.get('completion_tokens', 0) or output_tokens_est

            # Final fallback: char-count estimate if nothing else available and we have output messages
            if actual_output == 0 and last_output:
                total_chars = 0
                for m in last_output:
                    c = getattr(m, 'content', '') or ''
                    rc = getattr(m, 'reasoning_content', '') or ''
                    fc = getattr(m, 'function_call', None)

                    # Normalize content fields to strings
                    if isinstance(c, list):
                        c = ' '.join(str(x) for x in c if isinstance(x, str))
                    elif c:
                        c = str(c)

                    if isinstance(rc, list):
                        rc = ' '.join(str(x) for x in rc if isinstance(x, str))
                    elif rc:
                        rc = str(rc)

                    # Handle function_call (dict or object with name/arguments)
                    fc_str = ''
                    if fc:
                        if isinstance(fc, dict):
                            fc_str = str(fc.get('name', '')) + str(fc.get('arguments', ''))
                        elif hasattr(fc, 'name'):
                            fc_str = str(getattr(fc, 'name', '')) + str(getattr(fc, 'arguments', ''))

                    total_chars += len(c) + len(rc) + len(fc_str)

                actual_output = max(total_chars // 4, 1) if total_chars > 0 else 0

            end_time = time.perf_counter()
            latency_ms = (end_time - call['start_time']) * 1000
            ttft_ms = ((call['first_token_time'] - call['start_time']) * 1000) if call['first_token_time'] > 0 else 0

            # ── Cache-waste diagnosis (borrow-audit #7) ──────────────────────────
            # Compare this call against the previous one on the same instance to
            # measure needlessly reprocessed prompt tokens and tag the likely root
            # cause (see detect_cache_waste). Only calls the server actually reported
            # usage for are measurable: an unclassified call carries only a char-count
            # prompt estimate and no authoritative cached_tokens, so it must neither be
            # attributed as waste nor poison the baseline snapshot.
            scan = None
            if call.get('cache_classified'):
                usage = call.get('usage_details') or {}
                # Authoritative cached_tokens for BOTH hit and miss comes from the usage
                # details; record_token_usage only copies it onto the call for misses.
                cached_tokens = int(usage.get('cached_tokens') or call.get('cached_tokens') or 0)
                prompt_tokens = int(call.get('classified_prompt_tokens') or call.get('input_tokens_est') or 0)
                model = call.get('model') or ''
                now_ms = time.time() * 1000
                prev = self._cache_scan_state.get(instance_name)
                scan = self.detect_cache_waste(prev, prompt_tokens, cached_tokens, model, now_ms)
                if scan is not None:
                    self._session_stats['llm_wasted_tokens'] += scan['missed']
                    self._session_stats['llm_waste_count'] += 1
                    # Optional $-cost: only for models with pricing configured. Token
                    # waste is the primary, provider-agnostic metric regardless.
                    pricing = MODEL_PRICING.get(model)
                    if pricing:
                        delta = max(0.0, pricing.get('input_per_m', 0.0) - pricing.get('cache_read_per_m', 0.0))
                        self._session_stats['llm_wasted_cost'] += scan['missed'] * delta / 1e6
                    # Attribute to the config row via the fingerprint snapshotted at call
                    # start (same source as the cache hit/miss classifier, BUG_0260).
                    fp = call.get('config_fingerprint') or ''
                    if fp and fp in self._config_stats:
                        self._config_stats[fp]['wasted_tokens'] += scan['missed']
                        self._config_stats[fp]['waste_count'] += 1
                # Refresh the baseline for the NEXT call. A model switch is NOT a reset
                # (it is itself a counted waste event above); only a compression clears it.
                self._cache_scan_state[instance_name] = {
                    'prev_prompt_tokens': prompt_tokens,
                    'prev_ts_ms': now_ms,
                    'prev_model': model,
                    'reported_cache': (prev.get('reported_cache', False) if prev else False) or (cached_tokens > 0),
                }

                # Session spend ($): accumulate this call's cost using the
                # authoritative server-usage tokens (this block runs only for
                # calls the server reported usage for — never a char estimate).
                # Per-endpoint pricing is snapshotted at call start (model-only
                # fingerprint cannot look it up). cost.py returns None when the
                # endpoint is unpriced, so default-off installs stay at $0 → no
                # session_cost key in the summary.
                endpoint_pricing = call.get('pricing')
                if endpoint_pricing:
                    _call_cost = tokens_to_cost(
                        {
                            'prompt_tokens': prompt_tokens,
                            'completion_tokens': int(call.get('completion_tokens') or 0),
                            'cached_tokens': cached_tokens,
                        },
                        endpoint_pricing,
                    )
                    if _call_cost:
                        self._session_stats['session_cost'] += _call_cost
                    # Attribute the SAME authoritative cost to the per-instance
                    # accumulator here (not in the broad token block below): reusing
                    # this _call_cost under the same cache-classified + priced gate
                    # guarantees get_instance_cost ties out with session_cost — no
                    # second, conflicting $ figure.
                    _iu = self._ensure_instance_usage(instance_name)
                    _iu['priced'] = True
                    if _call_cost:
                        _iu['cost'] += _call_cost

            # todo.md:105 / BUG_0101 — one greppable debug line per KV-cache miss so
            # unexpected prompt reprocesses can be tracked from console.log. The server's
            # cached_tokens is authoritative: a miss means the full prompt was reprocessed
            # (the latency/ttft values show how expensive that was). When the waste
            # detector also flagged this miss, append the root cause and wasted tokens.
            if call.get('cache_status') == 'miss':
                pt = int(call.get('classified_prompt_tokens') or 0)
                ct = int(call.get('cached_tokens') or 0)
                waste_suffix = ''
                waste_args = ()
                if scan is not None:
                    waste_suffix = ' wasted_tokens=%d idle_ms=%.0f rootCause=%s'
                    waste_args = (scan['missed'], scan['idle_ms'], scan['root_cause'])
                _logger.debug(
                    '[KV_CACHE_MISS] %s model=%s prompt_tokens=%d cached_tokens=%d '
                    'cached_pct=%.1f latency_ms=%.1f ttft_ms=%.1f — prompt reprocessed' + waste_suffix,
                    instance_name, call.get('model') or '?', pt, ct,
                    (100.0 * ct / pt) if pt else 0.0, latency_ms, ttft_ms, *waste_args,
                )

            # Direct measurement: streaming time = end_time - first_token_time (not indirect subtraction)
            streaming_time_ms = max((end_time - call['first_token_time']) * 1000, 0) if call['first_token_time'] > 0 else 0

            event = {
                'type': 'llm_call',
                'instance': instance_name,
                'model': call['model'],
                'input_tokens_est': call['input_tokens_est'],
                'output_tokens_est': actual_output,
                'latency_ms': round(latency_ms, 1),
                'ttft_ms': round(ttft_ms, 1),
                'streaming_time_ms': round(streaming_time_ms, 1),
                'tps': round(actual_output / (streaming_time_ms / 1000), 1) if streaming_time_ms > 0 and actual_output > 0 else 0,
                'timestamp': _now_iso(),
            }

            # Additive cache-waste fields (borrow-audit #7) — present only on calls
            # the detector flagged as a meaningful miss, so the JSONL event schema
            # stays backward compatible for existing consumers.
            if scan is not None:
                event['wasted_tokens'] = scan['missed']
                event['waste_root_cause'] = scan['root_cause']
                event['idle_ms'] = round(scan['idle_ms'], 1)

            # Update session stats
            self._session_stats['total_llm_calls'] += 1
            self._session_stats['total_input_tokens_est'] += call['input_tokens_est']
            self._session_stats['total_output_tokens_est'] += actual_output
            if streaming_time_ms > 0:
                # Only streamed output has streaming time, so only it belongs in
                # the TPS numerator (BUG_0133).
                self._session_stats['total_streamed_output_tokens'] += actual_output
            self._session_stats['total_llm_latency_ms'] += latency_ms
            self._session_stats['total_ttft_ms'] += ttft_ms
            self._session_stats['total_streaming_time_ms'] += streaming_time_ms
            self._session_stats['llm_calls_by_model'][call['model']] += 1
            # Cache coverage (BUG_0132): a call that never received a usable
            # usage classification is "unknown", never silently neither-hit-nor-
            # miss. Guarantees hits + misses + unknown == total_llm_calls.
            if not call.get('cache_classified'):
                self._session_stats['llm_cache_unknown'] += 1

            # ── Per-instance TOTAL-token accumulator (Slice 2) ───────────────────
            # "Budget consumed" for tokens-mode = prompt+completion summed session-
            # lifetime over EVERY call, so the budget still advances on calls the
            # server never classified. The $ cost is NOT accumulated here — it is
            # attributed in the cache-waste block above, so it matches session_cost
            # exactly. Read via get_instance_total_tokens() / get_instance_cost().
            _iu = self._ensure_instance_usage(instance_name)
            _iu['total_tokens'] = _iu['total_tokens'] + int(call['input_tokens_est'] or 0) + int(actual_output or 0)

            # Update active turn and per-config latency stats
            turn = self._active_turns.get(instance_name)
            if turn:
                turn['llm_calls'] += 1
                # Per-turn time breakdown (BUG_0129) — flush onto the turn_end event.
                turn['llm_latency_ms'] += latency_ms
                turn['ttft_ms'] += ttft_ms
                turn['streaming_time_ms'] += streaming_time_ms
                # Attribute this LLM call to the calling instance's agent class.
                # Guarded: no active turn / empty class → not attributed (no spurious row).
                ac = turn.get('agent_class') or ''
                if ac:
                    acs = self._ensure_agent_class_stats(ac)
                    acs['llm_calls'] += 1
                    # Time breakdown: the same latency/streaming values that are
                    # credited to the config row, so the two tables reconcile.
                    acs['total_llm_latency_ms'] += latency_ms
                    acs['total_streaming_time_ms'] += streaming_time_ms
                turn['input_tokens_est'] += call['input_tokens_est']
                turn['output_tokens_est'] += actual_output
                # Update per-config latency fields (BUG 5 fix)
                fp = turn.get('config_fingerprint', '')
                if fp and fp in self._config_stats:
                    cs = self._config_stats[fp]
                    cs['total_llm_latency_ms'] += latency_ms
                    cs['total_ttft_ms'] += ttft_ms
                    cs['total_streaming_time_ms'] += streaming_time_ms
                    # Live per-config llm_calls + tokens (moved from record_turn_end).
                    cs['llm_calls'] += 1
                    cs['input_tokens_est'] += call['input_tokens_est']
                    cs['output_tokens_est'] += actual_output
                    if streaming_time_ms > 0:
                        cs['streamed_output_tokens'] += actual_output
                if ac:
                    self._ensure_agent_class_stats(ac)['tokens_generated'] += actual_output

        self._write_event(event)

    def _resolve_instance_key(self, instance) -> str:
        """Accept an instance name (str) or an object exposing `.instance_name`."""
        if isinstance(instance, str):
            return instance
        return getattr(instance, 'instance_name', '') or ''

    def _ensure_instance_usage(self, instance_name: str) -> Dict[str, float]:
        """Return (creating if needed) this instance's accumulator row.

        Caller must hold ``_telemetry_lock``. ``total_tokens`` accumulates over
        every call; ``cost``/``priced`` are set only by the cache-classified cost
        path, so per-instance cost stays in lockstep with ``session_cost``.
        """
        rec = self._instance_usage.get(instance_name)
        if rec is None:
            rec = self._instance_usage[instance_name] = {'total_tokens': 0, 'cost': 0.0, 'priced': False}
        return rec

    def get_instance_total_tokens(self, instance) -> int:
        """Total tokens (prompt+completion, Σ over calls) consumed by one instance.

        Read-only view of the session-lifetime per-instance accumulator — the
        "budget consumed" signal used by tokens-mode warnings (run_loop) and
        Slices 3 & 4. Named TOTAL to avoid colliding with the per-class
        ``tokens_generated`` counter, which is completion-only. Returns 0 unknown.
        """
        with _telemetry_lock:
            rec = self._instance_usage.get(self._resolve_instance_key(instance))
            return int(rec['total_tokens']) if rec else 0

    def get_instance_cost(self, instance) -> Optional[float]:
        """Cost ($) for one instance, or None when the instance was never priced.

        Accumulated from cache-classified priced calls with the identical usage
        tuple as ``session_cost``, so the per-instance costs sum to the session
        total. The ``None`` return mirrors ``cost.is_priced`` semantics so
        consumers never render a misleading ``$0`` for an unpriced endpoint.
        """
        with _telemetry_lock:
            rec = self._instance_usage.get(self._resolve_instance_key(instance))
            if not rec or not rec.get('priced'):
                return None
            return float(rec['cost'])

    def reset_instance_total_tokens(self, instance) -> None:
        """Clear the per-instance accumulator (Slice 4 may re-baseline a run)."""
        with _telemetry_lock:
            self._instance_usage.pop(self._resolve_instance_key(instance), None)

    def record_tool_call_start(self, instance_name: str, tool_name: str):
        """Mark the start of a tool execution."""
        with _telemetry_lock:
            key = f"{instance_name}:{tool_name}"
            self._active_tool_calls[key] = {
                'start_time': time.perf_counter(),
                'tool_name': tool_name,
                'instance': instance_name,
            }

    def record_tool_call_end(
        self,
        instance_name: str,
        tool_name: str,
        success: bool = True,
        result_chars: int = 0,
        truncated: bool = False,
        error: str = '',
        is_call_agent: bool = False,
    ):
        """Mark the end of a tool execution."""
        with _telemetry_lock:
            key = f"{instance_name}:{tool_name}"
            call = self._active_tool_calls.pop(key, None)
            if not call:
                return

            latency_ms = (time.perf_counter() - call['start_time']) * 1000

            event = {
                'type': 'tool_call',
                'instance': instance_name,
                'tool_name': tool_name,
                'latency_ms': round(latency_ms, 1),
                'success': success,
                'result_chars': result_chars,
                'truncated': truncated,
                'timestamp': _now_iso(),
            }
            if error:
                event['error'] = error[:MAX_ERROR_MESSAGE_LENGTH]

            # Update session stats (call_agent excluded from avg tool latency)
            self._session_stats['total_tool_calls'] += 1
            if is_call_agent:
                # Single source of truth for call_agent metrics — both the count
                # and the latency are driven by the is_call_agent flag, so they
                # can never disagree (count used to be derived from the tool name).
                self._session_stats['call_agent_calls'] += 1
                self._session_stats['call_agent_latency_ms'] += latency_ms
            else:
                self._session_stats['total_tool_latency_ms'] += latency_ms
            self._session_stats['tool_calls_by_name'][tool_name] += 1
            if not success:
                self._session_stats['tool_failures_by_name'][tool_name] += 1
            self._session_stats['tool_latency_by_name'][tool_name] += latency_ms

            # Update active turn
            turn = self._active_turns.get(instance_name)
            if turn:
                turn['tool_calls'] += 1
                # Per-turn time breakdown (BUG_0129), split like the session total.
                if is_call_agent:
                    turn['call_agent_latency_ms'] += latency_ms
                else:
                    turn['tool_latency_ms'] += latency_ms
                turn['tool_calls_detail'].append({
                    'tool_name': tool_name,
                    'latency_ms': round(latency_ms, 1),
                    'success': success,
                    'truncated': truncated,
                })
                # Live per-config tool counts (moved from record_turn_end).
                fp = turn.get('config_fingerprint', '')
                if fp and fp in self._config_stats:
                    cs = self._config_stats[fp]
                    cs['tool_calls'] += 1
                    cs['tool_calls_by_name'][tool_name] += 1
                    if not success:
                        cs['tool_failures_by_name'][tool_name] += 1
                # Live per-class tool counts (moved from record_turn_end). Bumping
                # tool_calls here — not just on failure — keeps tool_usage_accuracy live.
                ac = turn.get('agent_class') or ''
                if ac:
                    acs = self._ensure_agent_class_stats(ac)
                    acs['tool_calls'] += 1
                    if not success:
                        acs['tool_failures'] += 1
                    # Time breakdown, mirroring the session-level split: call_agent
                    # wait is reported separately so it is never double-counted with
                    # the child agent's own LLM/tool time.
                    if is_call_agent:
                        acs['call_agent_latency_ms'] += latency_ms
                    else:
                        acs['total_tool_latency_ms'] += latency_ms

        self._write_event(event)

    def record_agent_instance_call(
        self,
        instance_name: str,
        agent_class: str,
        caller: str,
        latency_ms: float = 0,
    ):
        """Record an agent instance delegation."""
        with _telemetry_lock:
            event = {
                'type': 'agent_instance_call',
                'instance': instance_name,
                'agent_class': agent_class,
                'caller': caller,
                'latency_ms': round(latency_ms, 1),
                'timestamp': _now_iso(),
            }
            self._session_stats['agent_instance_calls'] += 1

        self._write_event(event)

    def record_loop_detected(self, instance_name: str, reason: str, auto_rolled_back: bool = False, pop_count: int = 0, loop_type: str = 'outer', warned: bool = True):
        """Record a loop detection event.

        ``warned`` (two-tier redesign): for fuzzy-warning events, True when an
        advisory USER message was actually injected and False when the trigger
        was suppressed by the per-run throttle / escalation countdown. Default
        True keeps all pre-existing callers unchanged.
        """
        with _telemetry_lock:
            event = {
                'type': 'loop_detected',
                'instance': instance_name,
                'reason': reason,
                'auto_rolled_back': auto_rolled_back,
                'pop_count': pop_count,
                'loop_type': loop_type,
                'warned': warned,
                'timestamp': _now_iso(),
            }
            self._session_stats['total_loops_detected'] += 1
            if loop_type == 'inner':
                self._session_stats['loops_inner'] += 1
            else:
                self._session_stats['loops_outer'] += 1

            turn = self._active_turns.get(instance_name)
            if turn:
                turn['loops_detected'] += 1
                if auto_rolled_back:
                    turn['retries'] += 1
                # Live per-config loop/retry counts (moved from record_turn_end).
                fp = turn.get('config_fingerprint', '')
                if fp and fp in self._config_stats:
                    self._config_stats[fp]['loops_detected'] += 1
                    if auto_rolled_back:
                        self._config_stats[fp]['retries'] += 1

        self._write_event(event)

    def record_auto_continue(self, instance_name: str, reason: str):
        """Record an auto-continue (malformed output recovery) event.

        Session-level only — intentionally not tracked per-turn, since the
        count is surfaced via get_session_summary() and the UI card rather
        than the turn_end/config stats payloads.
        """
        with _telemetry_lock:
            event = {
                'type': 'auto_continue',
                'instance': instance_name,
                'reason': reason,
                'timestamp': _now_iso(),
            }
            self._session_stats['total_auto_continues'] += 1

        self._write_event(event)

    def record_skill_advisor_decision(
        self,
        instance_name: str,
        verdict: str,
        skill_count: int = 0,
        latency_ms: float = 0.0,
        was_fallback: bool = False,
    ):
        """Record a Skill Advisor (AUTO Skill Helper — Advanced) decision.

        Session-level counters (skill_advisor_calls / _denials / _fallbacks) are
        surfaced via get_session_summary(). ``verdict`` is one of
        "approve" | "deny" | "ambiguous".
        """
        with _telemetry_lock:
            event = {
                'type': 'skill_advisor_decision',
                'instance': instance_name,
                'verdict': verdict,
                'skill_count': skill_count,
                'latency_ms': round(latency_ms, 1),
                'was_fallback': was_fallback,
                'timestamp': _now_iso(),
            }
            self._session_stats['skill_advisor_calls'] += 1
            if verdict == 'deny':
                self._session_stats['skill_advisor_denials'] += 1
            elif was_fallback:
                self._session_stats['skill_advisor_fallbacks'] += 1

        self._write_event(event)

    def record_compression(
        self,
        instance_name: str,
        fraction: float,
        tokens_before: int = 0,
        tokens_after: int = 0,
    ):
        """Record a context compression event."""
        with _telemetry_lock:
            # Reset this instance's cache-waste baseline (borrow-audit #7). A
            # compression legitimately rewrites/shrinks the prompt, so the previous
            # call's prompt length is no longer a valid comparison target — without
            # this reset the first post-compression call (smaller prompt, cached=0)
            # would be miscounted as waste. Dropping prev makes the next call a fresh
            # baseline. Done first so the reset holds even if the write below fails.
            self._cache_scan_state.pop(instance_name, None)
            event = {
                'type': 'compression',
                'instance': instance_name,
                'fraction': fraction,
                'tokens_before': tokens_before,
                'tokens_after': tokens_after,
                'tokens_saved': tokens_before - tokens_after,
                'timestamp': _now_iso(),
            }
            self._session_stats['total_compressions'] += 1

            # Update active turn compression counter if one exists
            turn = self._active_turns.get(instance_name)
            if turn:
                turn['compressions'] += 1
                # Also update per-config stats for this compression
                fp = turn.get('config_fingerprint', '')
                if fp and fp in self._config_stats:
                    self._config_stats[fp]['total_compressions'] += 1

        self._write_event(event)

    # ── Aggregation & Reporting (thread-safe, return defensive copies) ─────

    def get_session_summary(self) -> Dict:
        """Get aggregate session-level telemetry summary."""
        with _telemetry_lock:
            # Deep copy mutable structures (defaultdicts inside are still shared refs with shallow copy)
            stats = {
                k: dict(v) if isinstance(v, dict) else v
                for k, v in self._session_stats.items()
            }

        # Calculate derived metrics
        total_tokens = stats['total_input_tokens_est'] + stats['total_output_tokens_est']
        total_streaming_time_sec = stats.get('total_streaming_time_ms', 0) / 1000

        # TPS numerator must be the streamed subset: non-streaming calls have
        # output tokens but no streaming time (BUG_0133).
        avg_tps = (stats.get('total_streamed_output_tokens', 0) / total_streaming_time_sec
                   if total_streaming_time_sec > 0 else 0)
        avg_llm_latency = stats['total_llm_latency_ms'] / stats['total_llm_calls'] if stats['total_llm_calls'] > 0 else 0
        # Exclude call_agent from the avg tool-latency denominator to match its
        # numerator. Derived from the is_call_agent-driven counter (single source
        # of truth) so it always agrees with call_agent_latency_ms.
        call_agent_count = stats.get('call_agent_calls', 0)
        non_agent_tool_calls = stats['total_tool_calls'] - call_agent_count
        avg_tool_latency = stats['total_tool_latency_ms'] / non_agent_tool_calls if non_agent_tool_calls > 0 else 0

        # Prompt-cache hit ratio (unknowns excluded from the denominator).
        # Derived from server-reported cached_tokens (authoritative) + header fallback.
        _cache_total = stats.get('llm_cache_hits', 0) + stats.get('llm_cache_misses', 0)
        llm_cache_hit_ratio = round(stats.get('llm_cache_hits', 0) / _cache_total, 3) if _cache_total > 0 else None

        # Coverage: fraction of LLM calls that were actually classified (hit or miss).
        # Low coverage means most traffic came from non-supporting backends (no usage),
        # so the hit ratio above is based on a small sample. Exposed for the WebUI to
        # display measurement confidence.
        _total_llm = stats.get('total_llm_calls', 0)
        llm_cache_classified_ratio = round(_cache_total / _total_llm, 3) if _total_llm > 0 else None

        # Tool success rates
        tool_success_rates = {}
        for name, count in stats['tool_calls_by_name'].items():
            failures = stats['tool_failures_by_name'].get(name, 0)
            tool_success_rates[name] = {
                'total': count,
                'failures': failures,
                'success_rate': round((count - failures) / count * 100, 1) if count > 0 else 100.0,
                'avg_latency_ms': round(stats['tool_latency_by_name'].get(name, 0) / count, 1) if count > 0 else 0,
            }

        return {
            'session_id': self.session_id,
            'total_runs': stats['total_runs'],
            'total_user_runs': stats['total_user_runs'],
            'total_llm_calls': stats['total_llm_calls'],
            'total_tool_calls': stats['total_tool_calls'],
            'total_input_tokens_est': stats['total_input_tokens_est'],
            'total_output_tokens_est': stats['total_output_tokens_est'],
            'total_streamed_output_tokens': stats.get('total_streamed_output_tokens', 0),
            'total_tokens': total_tokens,
            'avg_tps': round(avg_tps, 1),
            'avg_llm_latency_ms': round(avg_llm_latency, 1),
            'avg_tool_latency_ms': round(avg_tool_latency, 1),
            'call_agent_count': call_agent_count,
            'call_agent_latency_ms': stats.get('call_agent_latency_ms', 0),
            'total_loops_detected': stats['total_loops_detected'],
            'loops_outer': stats.get('loops_outer', 0),
            'loops_inner': stats.get('loops_inner', 0),
            'total_auto_continues': stats.get('total_auto_continues', 0),
            'total_retries': stats['total_retries'],
            'total_compressions': stats['total_compressions'],
            'write_failures': stats.get('write_failures', 0),
            'agent_instance_calls': stats['agent_instance_calls'],
            'llm_calls_by_model': dict(stats['llm_calls_by_model']),
            'tool_effectiveness': tool_success_rates,
            # Skill Advisor (AUTO Skill Helper — Advanced) session counters.
            'skill_advisor_calls': stats.get('skill_advisor_calls', 0),
            'skill_advisor_denials': stats.get('skill_advisor_denials', 0),
            'skill_advisor_fallbacks': stats.get('skill_advisor_fallbacks', 0),
            # RFC 9211 prompt-cache hit/miss counters + derived hit ratio.
            'llm_cache_hits': stats.get('llm_cache_hits', 0),
            'llm_cache_misses': stats.get('llm_cache_misses', 0),
            'llm_cache_unknown': stats.get('llm_cache_unknown', 0),
            'llm_cache_hit_ratio': llm_cache_hit_ratio,
            # Coverage, NOT a hit rate: fraction of ALL LLM calls that were classifiable
            # (classified/total). Distinct from llm_cache_hit_ratio above, which is
            # hits/(hits+misses) over only the classified calls. (BUG_0260)
            'llm_cache_classified_ratio': llm_cache_classified_ratio,
            # ── Cache-waste diagnosis (borrow-audit #7) ─────────────────────────
            # Wasted prompt tokens + how many misses were detected, provider-agnostic.
            # llm_wasted_cost ($-only) is surfaced ONLY when MODEL_PRICING is configured;
            # without pricing the key is absent so consumers see no misleading $-value.
            'llm_wasted_tokens': stats.get('llm_wasted_tokens', 0),
            'llm_waste_count': stats.get('llm_waste_count', 0),
            **({'llm_wasted_cost': round(stats.get('llm_wasted_cost', 0.0), 6)}
               if MODEL_PRICING else {}),
            # Session spend ($) across priced calls. Present ONLY when > 0, so a
            # default-off / unpriced install (no priced endpoint) emits no key
            # and consumers never render a misleading "$0".
            **({'session_cost': round(stats.get('session_cost', 0.0), 6)}
               if stats.get('session_cost', 0.0) > 0 else {}),
            # ── Raw time totals (BUG_0130) ──────────────────────────────────────
            # Exposed so the session cards/tables can be reconciled without re-adding
            # averages. All are agent-exclusive work sums; with parallel agents they
            # can exceed real elapsed time.
            'total_llm_latency_ms': stats.get('total_llm_latency_ms', 0),
            'total_ttft_ms': stats.get('total_ttft_ms', 0),
            'total_streaming_time_ms': stats.get('total_streaming_time_ms', 0),
            'total_tool_latency_ms': stats.get('total_tool_latency_ms', 0),
            # Sum of turn wall-clock durations (equals the sum of the Agent Class
            # Usage "Turn Time" column when every turn has a class).
            'total_turn_time_ms': stats.get('total_turn_time_ms', 0),
            # Kept for API compatibility. It is *not* wall-clock: it is the sum of
            # LLM + non-call_agent tool latency, and excludes call_agent wait (nested
            # child time, already counted under the child's own instance), sleeping,
            # queue/slot waits and idle time. See total_turn_time_ms for the
            # turn-duration view.
            'wallclock_ms': stats.get('total_llm_latency_ms', 0) + stats.get('total_tool_latency_ms', 0),
        }

    def get_config_comparison(self) -> List[Dict]:
        """Get per-config stats for A/B comparison."""
        with _telemetry_lock:
            result = []
            for fp, cs in self._config_stats.items():
                total_tokens = cs['input_tokens_est'] + cs['output_tokens_est']
                total_time_sec = cs['total_duration_ms'] / 1000 if cs['total_duration_ms'] > 0 else 0
                avg_turn_time = cs['total_duration_ms'] / cs['runs'] if cs['runs'] > 0 else 0
                avg_tokens_per_turn = total_tokens / cs['runs'] if cs['runs'] > 0 else 0

                # Streaming time and TPS for this config
                config_streaming_sec = cs['total_streaming_time_ms'] / 1000 if cs['total_streaming_time_ms'] > 0 else 0
                config_tps = (cs['streamed_output_tokens'] / config_streaming_sec
                              if config_streaming_sec > 0 else 0)

                # Tool success rates for this config
                tool_rates = {}
                for name, count in cs['tool_calls_by_name'].items():
                    failures = cs['tool_failures_by_name'].get(name, 0)
                    tool_rates[name] = {
                        'total': count,
                        'success_rate': round((count - failures) / count * 100, 1) if count > 0 else 100.0,
                    }

                # Prompt-cache counters + derived hit ratio (BUG_0260). Same formula
                # as the session-level llm_cache_hit_ratio: hits/(hits+misses), None
                # when nothing classified. .get(...,0) tolerates rows persisted before
                # the field existed. NOTE (BUG_0273): since cache is now attributed to
                # the RESOLVED endpoint via get_cache_attribution(), these per-model
                # fields carry only the fallback bucket — classified calls whose
                # resolved endpoint was unknown. Treat get_cache_attribution() as the
                # authoritative cache view; a model row's hit ratio no longer
                # describes that model's endpoints' cache behavior.
                _cache_hits = cs.get('cache_hits', 0)
                _cache_misses = cs.get('cache_misses', 0)
                _cache_total = _cache_hits + _cache_misses

                result.append({
                    'config_fingerprint': fp,
                    'config_description': dict(cs['config_description']),
                    'runs': cs['runs'],
                    'llm_calls': cs['llm_calls'],
                    'cache_hits': _cache_hits,
                    'cache_misses': _cache_misses,
                    'cache_hit_ratio': (
                        round(_cache_hits / _cache_total, 3)
                        if _cache_total > 0 else None
                    ),
                    # Cache-waste diagnosis (borrow-audit #7). .get(...,0) tolerates
                    # rows persisted before the field existed.
                    'wasted_tokens': cs.get('wasted_tokens', 0),
                    'waste_count': cs.get('waste_count', 0),
                    'tool_calls': cs['tool_calls'],
                    'input_tokens_est': cs['input_tokens_est'],
                    'output_tokens_est': cs['output_tokens_est'],
                    'total_tokens': total_tokens,
                    'total_duration_sec': round(total_time_sec, 1),
                    'avg_turn_duration_ms': round(avg_turn_time, 1),
                    'avg_tokens_per_turn': round(avg_tokens_per_turn),
                    # ── Time breakdown (comparable with get_agent_class_summary) ──
                    # total_duration_sec is turn wall-clock (same measure as the
                    # agent-class total_time_sec); total_llm_latency_sec and
                    # total_ttft_sec are the LLM-side components.
                    'total_llm_latency_sec': round(cs['total_llm_latency_ms'] / 1000, 1),
                    'total_ttft_sec': round(cs['total_ttft_ms'] / 1000, 1),
                    'total_streaming_time_sec': round(config_streaming_sec, 1),
                    'avg_tps': round(config_tps, 1),
                    'loops_detected': cs['loops_detected'],
                    'retries': cs['retries'],
                    'tool_effectiveness': tool_rates,
                })

        return result

    def get_cache_attribution(self) -> List[Dict]:
        """Per-ENDPOINT cache hit/miss attribution (BUG_0273 Option A).

        The model-only ``config_fingerprint`` groups A/B rows by model and cannot
        distinguish the endpoint that actually served a call, so attributing
        cache counters to it let ``use_last_endpoint`` agents' fresh-reusable
        instance misses inflate a global model row's miss count and deflate its
        hit ratio. This view is keyed by the resolved endpoint (see
        ``cache_attribution_key``), so each endpoint's own cache behavior is
        reported honestly.

        Sorted by classified-call count desc then key asc. ``cache_hit_ratio``
        is ``hits/(hits+misses)`` — identical formula to the session-level
        ratio — ``None`` when nothing was classified. Each entry:
        ``{cache_key, cache_hits, cache_misses, cache_hit_ratio}``.
        """
        with _telemetry_lock:
            result = []
            for cache_key, cs in self._cache_stats.items():
                hits = cs['cache_hits']
                misses = cs['cache_misses']
                total = hits + misses
                result.append({
                    'cache_key': cache_key,
                    'cache_hits': hits,
                    'cache_misses': misses,
                    'cache_hit_ratio': (
                        round(hits / total, 3) if total > 0 else None
                    ),
                })
        result.sort(key=lambda r: (-(r['cache_hits'] + r['cache_misses']), r['cache_key']))
        return result

    def get_agent_class_summary(self) -> List[Dict]:
        """Get per-agent-class usage stats for the "Agent Class Usage" table.

        Returns a snapshot list (not live dicts) so callers can iterate safely
        without holding the lock. Each entry:
        ``{agent_class, llm_calls, tool_usage_accuracy, total_time_sec,
        llm_time_sec, streaming_time_sec, tool_time_sec, call_agent_time_sec,
        tokens_generated, turns}``.

        ``total_time_sec`` is turn wall-clock (the same measure as the config
        table's ``total_duration_sec``) and therefore includes prefill, tools,
        waiting on sub-agents and idle time. ``llm_time_sec`` is the class's share
        of LLM call latency; it reconciles with the config table's
        ``total_llm_latency_sec`` (both are attributed per LLM call). Tool time
        excludes ``call_agent`` wait, which is reported separately, mirroring the
        session-level split.
        """
        with _telemetry_lock:
            result = []
            for agent_class in sorted(self._agent_class_stats):
                acs = self._agent_class_stats[agent_class]
                tool_calls = acs['tool_calls']
                if tool_calls > 0:
                    accuracy = round((tool_calls - acs['tool_failures']) / tool_calls * 100, 1)
                else:
                    accuracy = None
                result.append({
                    'agent_class': agent_class,
                    'llm_calls': acs['llm_calls'],
                    'tool_usage_accuracy': accuracy,
                    'total_time_sec': round(acs['total_time_ms'] / 1000, 1),
                    'llm_time_sec': round(acs['total_llm_latency_ms'] / 1000, 1),
                    'streaming_time_sec': round(acs['total_streaming_time_ms'] / 1000, 1),
                    'tool_time_sec': round(acs['total_tool_latency_ms'] / 1000, 1),
                    'call_agent_time_sec': round(acs['call_agent_latency_ms'] / 1000, 1),
                    'tokens_generated': acs['tokens_generated'],
                    'runs': acs['runs'],
                })
        return result

    def record_skills_loaded(self, agent_class: str, skill_names, mode: str):
        """Record that *skill_names* were injected into an agent of *agent_class*.

        One injection event = one count per (deduped) skill. ``mode`` is the
        acquisition path: "explicit" | "auto" | "advisor" | "self-augmentation" |
        "runtime". No-op when ``skill_names`` is empty/None. Duplicate names within
        a single call are counted once.
        """
        if not skill_names:
            return
        with _telemetry_lock:
            for name in dict.fromkeys(skill_names):  # dedupe, preserve order
                entry = self._skill_usage_stats.get(name)
                if entry is None:
                    entry = {
                        'loads': 0,
                        'agent_classes': set(),
                        'modes': collections.Counter(),
                    }
                    self._skill_usage_stats[name] = entry
                entry['loads'] += 1
                entry['agent_classes'].add(agent_class)
                entry['modes'][mode] += 1

    def get_skill_usage_summary(self) -> List[Dict]:
        """Get per-skill usage stats for the "Skill Usage" table.

        Returns a snapshot list (not live dicts) sorted by ``loads`` desc then name
        asc, so callers can iterate safely without holding the lock. Each entry:
        ``{skill, loads, agent_classes (sorted list), top_mode}`` where ``top_mode``
        is the most common acquisition mode (ties broken alphabetically). Returns
        ``[]`` when no skills have been recorded.
        """
        with _telemetry_lock:
            result = []
            for name, entry in self._skill_usage_stats.items():
                modes = entry['modes']
                if modes:
                    # Most common mode; ties broken alphabetically (sort by -count then name).
                    top_mode = sorted(modes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                else:
                    top_mode = ''
                result.append({
                    'skill': name,
                    'loads': entry['loads'],
                    'agent_classes': sorted(entry['agent_classes']),
                    'top_mode': top_mode,
                })
        result.sort(key=lambda r: (-r['loads'], r['skill']))
        return result

    def get_recent_events(self, count: int = DEFAULT_RECENT_EVENT_COUNT) -> List[Dict]:
        """Get the most recent N events."""
        with _telemetry_lock:
            return list(self.events)[-count:]  # deque slicing returns new deque; convert to list

    def export_jsonl(self) -> str:
        """Return the path to the raw telemetry log file."""
        return str(self.log_path)

    # ── Persistence ───────────────────────────────────────────────────────

    def record_session_end(self):
        """Write a session_end event with final session statistics.

        Call this during graceful shutdown to close out the telemetry session
        that was opened in __init__(). Includes a full summary of all aggregated
        session stats and per-config breakdowns for A/B comparison.

        Safe to call multiple times — only writes once (idempotent).
        Robust against partial write failures: logs summary at INFO level as fallback.
        """
        with _telemetry_lock:
            if self._session_ended:
                return

            self._session_ended = True
            summary = self.get_session_summary()
            config_comparison = self.get_config_comparison()
            # Persist the agent-class and skill tables too, so the JSONL can
            # reproduce every dashboard table offline (BUG_0129); previously only
            # the config comparison was written.
            agent_class_comparison = self.get_agent_class_summary()
            skill_usage = self.get_skill_usage_summary()
            # Per-endpoint cache attribution (BUG_0273 Option A), persisted so
            # historical hits/misses can be re-keyed offline to confirm the global
            # model row's hit ratio recovers once last-endpoint misses are booked
            # to their own resolved endpoint.
            cache_attribution = self.get_cache_attribution()

        event = {
            'type': 'session_end',
            'session_id': self.session_id,
            'timestamp': _now_iso(),
            'summary': summary,
            'config_comparison': config_comparison,
            'cache_attribution': cache_attribution,
            'agent_class_comparison': agent_class_comparison,
            'skill_usage': skill_usage,
        }
        try:
            self._write_critical_event(event)
            self.close()  # Flush and close file handle after final event
        except Exception as e:
            # Fallback — log the full summary at INFO so it appears in console logs
            _logger.info(
                '[TELEMETRY] Session %s end (summary: runs=%d, llm_calls=%d, tool_calls=%d, '
                'write_failures=%d) — file write failed: %s',
                self.session_id, summary['total_runs'], summary['total_llm_calls'],
                summary['total_tool_calls'], summary.get('write_failures', 0), e,
            )

    def _write_to_file(self, event: Dict, raise_on_error: bool = False):
        """Write a single event to the JSONL log file and in-memory buffer.

        Thread-safe via module-level lock. Uses cached file handle with reopen fallback.
        Both events.append() and file write are inside the lock for atomicity: an event
        is either fully persisted (in-memory + on disk) or neither, preventing inconsistency on crash.

        Args:
            event: The event dict to persist.
            raise_on_error: If True, raises on I/O failure (for critical events like session_end).
                           If False, logs warning and increments write_failures counter.
        """
        line = json.dumps(event, ensure_ascii=False, default=str) + '\n'

        try:
            with _telemetry_lock:
                # Append to in-memory buffer inside lock for atomicity with file write
                self.events.append(event)

                if self._log_file is not None and not self._log_file.closed:
                    self._log_file.write(line)
                    self._log_file.flush()  # Flush to avoid data loss on crash
                else:
                    # Fallback: reopen file handle so subsequent writes don't pay open/close cost
                    fh = open(self.log_path, 'a', encoding='utf-8')
                    fh.write(line)
                    fh.flush()
                    self._log_file = fh  # Update cached handle for future writes
        except Exception as e:
            if raise_on_error:
                raise
            _logger.warning(
                'Failed to write telemetry event [%s]: %s', event.get('type', 'unknown'), e,
            )
            # Increment under lock since this runs outside the main with block above (exception path)
            with _telemetry_lock:
                self._session_stats['write_failures'] += 1

    def _write_critical_event(self, event: Dict):
        """Append an event to the JSONL log file; raise on I/O failure.

        Unlike ``_write_event`` this does not swallow exceptions — use for
        events that should be guaranteed to persist (e.g., session_end).
        Thread-safe via module-level lock. Uses cached file handle.
        """
        self._write_to_file(event, raise_on_error=True)

    def _write_event(self, event: Dict):
        """Append an event to the JSONL log file and in-memory buffer.

        Thread-safe via module-level lock. Uses cached file handle.
        Failures are logged at WARNING level with a failure counter.
        Every write is flushed to avoid data loss on crash.
        """
        self._write_to_file(event, raise_on_error=False)

    def close(self):
        """Flush and close the log file handle. Call during shutdown cleanup."""
        with _telemetry_lock:
            if self._log_file is not None and not self._log_file.closed:
                try:
                    self._log_file.flush()
                    self._log_file.close()
                except Exception as e:
                    _logger.warning('Error closing telemetry log file: %s', e)

    def __del__(self):
        """Cleanup guarantee: close file handle if still open (handles crashes/uncaught exceptions)."""
        # Defensive no-op if already closed or not yet initialized
        try:
            if hasattr(self, '_log_file') and self._log_file is not None and not self._log_file.closed:
                try:
                    self._log_file.close()
                except Exception as e:
                    _logger.warning('Error in __del__ closing telemetry log file: %s', e)
        except Exception:
            # Last resort: never let __del__ raise (avoids interpreter shutdown issues)
            pass

    # ── Aggregation & Reporting (moved above persistence for logical grouping) ─


def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
