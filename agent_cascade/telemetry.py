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
import logging
import threading
import time
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional

_logger = logging.getLogger('agent_cascade.telemetry')

# Module-level RLock protecting ALL telemetry shared state and file I/O.
# RLock (reentrant) prevents deadlocks if methods call each other while holding the lock.
# Telemetry is not on the hot path, so a single coarse-grained lock avoids complexity.
_telemetry_lock = threading.RLock()

from agent_cascade.settings import (
    SYSTEM_PROMPT_HASH_MAX_CHARS,
    DEFAULT_RECENT_EVENT_COUNT,
    MAX_EVENTS_IN_MEMORY,
)


class TelemetryCollector:
    """Collects and persists telemetry events for agent performance tracking."""

    def __init__(self, log_dir: str = "workspace/telemetry"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Session-level tracking — use _now_iso() for consistent UTC timestamps
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_str = now_dt.isoformat()
        # Include microseconds in session_id to avoid collisions when multiple sessions start simultaneously
        self.session_id = now_dt.strftime("%Y%m%d_%H%M%S%f")
        self.log_path = self.log_dir / f"telemetry_{self.session_id}.jsonl"

        # In-memory event buffer — deque with maxlen gives O(1) rotation
        self.events: collections.deque[Dict] = collections.deque(maxlen=MAX_EVENTS_IN_MEMORY)

        # Active turn tracking (keyed by instance_name)
        self._active_turns: Dict[str, Dict] = {}

        # Active LLM call tracking (keyed by instance_name)
        self._active_llm_calls: Dict[str, Dict] = {}

        # Active tool call tracking (keyed by instance_name + tool_name)
        self._active_tool_calls: Dict[str, Dict] = {}

        # Session-level aggregates (updated on each event)
        self._session_stats = {
            "total_turns": 0,
            "total_llm_calls": 0,
            "total_tool_calls": 0,
            "total_input_tokens_est": 0,
            "total_output_tokens_est": 0,
            "total_llm_latency_ms": 0,
            "total_ttft_ms": 0,
            "total_streaming_time_ms": 0,
            "total_tool_latency_ms": 0,
            "call_agent_latency_ms": 0,
            "total_loops_detected": 0,
            "total_retries": 0,
            "total_compressions": 0,
            "write_failures": 0,  # Track write failures for diagnostics
            "tool_calls_by_name": defaultdict(int),
            "tool_failures_by_name": defaultdict(int),
            "tool_latency_by_name": defaultdict(float),
            "llm_calls_by_model": defaultdict(int),
            "agent_instance_calls": 0,
        }

        # Per-config aggregates for A/B comparison
        self._config_stats: Dict[str, Dict] = {}

        # Guard against duplicate session_end events (BUG 8 fix)
        self._session_ended = False

        # Keep file handle open to avoid per-event open/close overhead
        self._log_file = None
        try:
            self._log_file = open(self.log_path, "a", encoding="utf-8")
        except Exception as e:
            _logger.warning("Failed to open telemetry log file %s: %s", self.log_path, e)

        # Write session header
        self._write_event({
            "type": "session_start",
            "session_id": self.session_id,
            "timestamp": now_str,
        })

    # ── Config Fingerprinting ─────────────────────────────────────────────

    @staticmethod
    def fingerprint_config(
        model: str = "",
        generate_cfg: Optional[Dict] = None,
        system_prompt: str = "",
        tools: Optional[List[str]] = None,
        api_base: str = "",
    ) -> str:
        """
        Create a stable hash fingerprint from the current agent configuration.
        This groups runs by their config for A/B comparison.
        """
        cfg = generate_cfg or {}

        # Extract only the params that matter for comparison
        relevant_keys = [
            "temperature", "top_p", "top_k", "min_p",
            "max_tokens", "max_input_tokens",
            "presence_penalty", "frequency_penalty",
            "repetition_penalty", "repeat_penalty",
        ]
        params = {k: cfg.get(k) for k in relevant_keys if cfg.get(k) is not None}

        # Group by API endpoint (api_base) rather than specific model,
        # so that all models from the same provider/endpoint share a fingerprint.
        fingerprint_data = {
            "api_base": api_base or model,  # fallback to model if api_base empty
            "params": params,
            "system_prompt_hash": hashlib.md5(system_prompt[:SYSTEM_PROMPT_HASH_MAX_CHARS].encode()).hexdigest()[:8],
            "tools": sorted(tools or []),
        }

        raw = json.dumps(fingerprint_data, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    @staticmethod
    def describe_config(
        model: str = "",
        generate_cfg: Optional[Dict] = None,
        tools: Optional[List[str]] = None,
        api_base: str = "",
    ) -> Dict:
        """Return a human-readable config description for display."""
        cfg = generate_cfg or {}
        return {
            "model": model,
            "api_base": api_base,
            "temperature": cfg.get("temperature"),
            "top_p": cfg.get("top_p"),
            "top_k": cfg.get("top_k"),
            "min_p": cfg.get("min_p"),
            "max_tokens": cfg.get("max_tokens"),
            "max_input_tokens": cfg.get("max_input_tokens"),
            "presence_penalty": cfg.get("presence_penalty"),
            "frequency_penalty": cfg.get("frequency_penalty"),
            "repetition_penalty": cfg.get("repetition_penalty"),
            "tools_count": len(tools or []),
        }

    # ── Helper: per-config stats initialization ───────────────────────

    def _ensure_config_stats(self, fingerprint: str, config_description: Optional[Dict] = None):
        """Ensure per-config stats dict exists for *fingerprint*, creating it if needed."""
        if fingerprint and fingerprint not in self._config_stats:
            self._config_stats[fingerprint] = {
                "config_description": config_description or {},
                "turns": 0,
                "llm_calls": 0,
                "tool_calls": 0,
                "input_tokens_est": 0,
                "output_tokens_est": 0,
                "total_duration_ms": 0,
                "total_llm_latency_ms": 0,
                "total_ttft_ms": 0,
                "total_streaming_time_ms": 0,
                "loops_detected": 0,
                "retries": 0,
                "tool_calls_by_name": defaultdict(int),
                "tool_failures_by_name": defaultdict(int),
            }

    # ── Event Recording (all thread-safe via _telemetry_lock) ──────────────

    def record_turn_start(
        self,
        instance_name: str,
        config_fingerprint: str = "",
        config_description: Optional[Dict] = None,
    ):
        """Mark the start of a new agent turn."""
        with _telemetry_lock:
            # Warn if an active turn already exists with a different fingerprint
            existing_turn = self._active_turns.get(instance_name)
            if existing_turn and existing_turn["config_fingerprint"] != config_fingerprint:
                _logger.warning(
                    "Instance %r started new turn with fingerprint %s (was %s). "
                    "Config changed mid-session.",
                    instance_name, config_fingerprint, existing_turn["config_fingerprint"],
                )

            self._active_turns[instance_name] = {
                "start_time": time.perf_counter(),
                "config_fingerprint": config_fingerprint,
                "config_description": config_description or {},
                "llm_calls": 0,
                "tool_calls": 0,
                "tool_calls_detail": [],
                "input_tokens_est": 0,
                "output_tokens_est": 0,
                "loops_detected": 0,
                "retries": 0,
            }

            # Initialize per-config stats on first turn — use helper method
            self._ensure_config_stats(config_fingerprint, config_description)

        event = {
            "type": "turn_start",
            "instance": instance_name,
            "config_fingerprint": config_fingerprint,
            "timestamp": _now_iso(),
        }
        self._write_event(event)

    def record_turn_end(self, instance_name: str):
        """Mark the end of an agent turn and aggregate stats."""
        with _telemetry_lock:
            turn = self._active_turns.pop(instance_name, None)
            if not turn:
                _logger.warning(
                    "record_turn_end called for %r but no matching turn_start found.",
                    instance_name,
                )
                return

            duration_ms = (time.perf_counter() - turn["start_time"]) * 1000
            fp = turn["config_fingerprint"]

            event = {
                "type": "turn_end",
                "instance": instance_name,
                "config_fingerprint": fp,
                "duration_ms": round(duration_ms, 1),
                "llm_calls": turn["llm_calls"],
                "tool_calls": turn["tool_calls"],
                "tool_calls_detail": turn["tool_calls_detail"],
                "input_tokens_est": turn["input_tokens_est"],
                "output_tokens_est": turn["output_tokens_est"],
                "loops_detected": turn["loops_detected"],
                "retries": turn["retries"],
                "timestamp": _now_iso(),
            }

            # Update session stats (inside lock since RLock is reentrant)
            self._session_stats["total_turns"] += 1
            self._session_stats["total_retries"] += turn["retries"]

            # Update per-config stats — ensure entry exists before updating
            if fp:
                self._ensure_config_stats(fp, turn.get("config_description"))
                cs = self._config_stats[fp]
                cs["turns"] += 1
                cs["llm_calls"] += turn["llm_calls"]
                cs["tool_calls"] += turn["tool_calls"]
                cs["input_tokens_est"] += turn["input_tokens_est"]
                cs["output_tokens_est"] += turn["output_tokens_est"]
                cs["total_duration_ms"] += duration_ms
                cs["loops_detected"] += turn["loops_detected"]
                cs["retries"] += turn["retries"]
                for td in turn["tool_calls_detail"]:
                    cs["tool_calls_by_name"][td["tool_name"]] += 1
                    if not td.get("success", True):
                        cs["tool_failures_by_name"][td["tool_name"]] += 1

        self._write_event(event)

    def record_llm_call_start(self, instance_name: str, input_tokens_est: int = 0, model: str = ""):
        """Mark the start of an LLM API call."""
        with _telemetry_lock:
            self._active_llm_calls[instance_name] = {
                "start_time": time.perf_counter(),
                "input_tokens_est": input_tokens_est,
                "model": model,
                "first_token_time": 0,
            }

    def record_llm_first_token(self, instance_name: str):
        """Record Time To First Token (TTFT)."""
        with _telemetry_lock:
            call = self._active_llm_calls.get(instance_name)
            if call and call["first_token_time"] == 0:
                call["first_token_time"] = time.perf_counter()

    def record_llm_call_end(self, instance_name: str, output_tokens_est: int = 0):
        """Mark the end of an LLM API call."""
        with _telemetry_lock:
            call = self._active_llm_calls.pop(instance_name, None)
            if not call:
                return

            end_time = time.perf_counter()
            latency_ms = (end_time - call["start_time"]) * 1000
            ttft_ms = ((call["first_token_time"] - call["start_time"]) * 1000) if call["first_token_time"] > 0 else 0

            # Direct measurement: streaming time = end_time - first_token_time (not indirect subtraction)
            streaming_time_ms = max((end_time - call["first_token_time"]) * 1000, 0) if call["first_token_time"] > 0 else 0

            event = {
                "type": "llm_call",
                "instance": instance_name,
                "model": call["model"],
                "input_tokens_est": call["input_tokens_est"],
                "output_tokens_est": output_tokens_est,
                "latency_ms": round(latency_ms, 1),
                "ttft_ms": round(ttft_ms, 1),
                "streaming_time_ms": round(streaming_time_ms, 1),
                "tps": round(output_tokens_est / (streaming_time_ms / 1000), 1) if streaming_time_ms > 0 and output_tokens_est > 0 else 0,
                "timestamp": _now_iso(),
            }

            # Update session stats
            self._session_stats["total_llm_calls"] += 1
            self._session_stats["total_input_tokens_est"] += call["input_tokens_est"]
            self._session_stats["total_output_tokens_est"] += output_tokens_est
            self._session_stats["total_llm_latency_ms"] += latency_ms
            self._session_stats["total_ttft_ms"] += ttft_ms
            self._session_stats["total_streaming_time_ms"] += streaming_time_ms
            self._session_stats["llm_calls_by_model"][call["model"]] += 1

            # Update active turn and per-config latency stats
            turn = self._active_turns.get(instance_name)
            if turn:
                turn["llm_calls"] += 1
                turn["input_tokens_est"] += call["input_tokens_est"]
                turn["output_tokens_est"] += call["output_tokens_est"]
                # Update per-config latency fields (BUG 5 fix)
                fp = turn.get("config_fingerprint", "")
                if fp and fp in self._config_stats:
                    self._config_stats[fp]["total_llm_latency_ms"] += latency_ms
                    self._config_stats[fp]["total_ttft_ms"] += ttft_ms
                    self._config_stats[fp]["total_streaming_time_ms"] += streaming_time_ms

        self._write_event(event)

    def record_tool_call_start(self, instance_name: str, tool_name: str):
        """Mark the start of a tool execution."""
        with _telemetry_lock:
            key = f"{instance_name}:{tool_name}"
            self._active_tool_calls[key] = {
                "start_time": time.perf_counter(),
                "tool_name": tool_name,
                "instance": instance_name,
            }

    def record_tool_call_end(
        self,
        instance_name: str,
        tool_name: str,
        success: bool = True,
        result_chars: int = 0,
        truncated: bool = False,
        error: str = "",
        is_call_agent: bool = False,
    ):
        """Mark the end of a tool execution."""
        with _telemetry_lock:
            key = f"{instance_name}:{tool_name}"
            call = self._active_tool_calls.pop(key, None)
            if not call:
                return

            latency_ms = (time.perf_counter() - call["start_time"]) * 1000

            event = {
                "type": "tool_call",
                "instance": instance_name,
                "tool_name": tool_name,
                "latency_ms": round(latency_ms, 1),
                "success": success,
                "result_chars": result_chars,
                "truncated": truncated,
                "timestamp": _now_iso(),
            }
            if error:
                event["error"] = error[:200]

            # Update session stats (call_agent excluded from avg tool latency)
            self._session_stats["total_tool_calls"] += 1
            if is_call_agent:
                self._session_stats["call_agent_latency_ms"] += latency_ms
            else:
                self._session_stats["total_tool_latency_ms"] += latency_ms
            self._session_stats["tool_calls_by_name"][tool_name] += 1
            if not success:
                self._session_stats["tool_failures_by_name"][tool_name] += 1
            self._session_stats["tool_latency_by_name"][tool_name] += latency_ms

            # Update active turn
            turn = self._active_turns.get(instance_name)
            if turn:
                turn["tool_calls"] += 1
                turn["tool_calls_detail"].append({
                    "tool_name": tool_name,
                    "latency_ms": round(latency_ms, 1),
                    "success": success,
                    "truncated": truncated,
                })

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
                "type": "agent_instance_call",
                "instance": instance_name,
                "agent_class": agent_class,
                "caller": caller,
                "latency_ms": round(latency_ms, 1),
                "timestamp": _now_iso(),
            }
            self._session_stats["agent_instance_calls"] += 1

        self._write_event(event)

    def record_loop_detected(self, instance_name: str, reason: str, auto_rolled_back: bool = False, pop_count: int = 0):
        """Record a loop detection event."""
        with _telemetry_lock:
            event = {
                "type": "loop_detected",
                "instance": instance_name,
                "reason": reason,
                "auto_rolled_back": auto_rolled_back,
                "pop_count": pop_count,
                "timestamp": _now_iso(),
            }
            self._session_stats["total_loops_detected"] += 1

            turn = self._active_turns.get(instance_name)
            if turn:
                turn["loops_detected"] += 1
                if auto_rolled_back:
                    turn["retries"] += 1

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
            event = {
                "type": "compression",
                "instance": instance_name,
                "fraction": fraction,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "tokens_saved": tokens_before - tokens_after,
                "timestamp": _now_iso(),
            }
            self._session_stats["total_compressions"] += 1

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
        total_tokens = stats["total_input_tokens_est"] + stats["total_output_tokens_est"]
        total_streaming_time_sec = stats.get("total_streaming_time_ms", 0) / 1000

        avg_tps = stats["total_output_tokens_est"] / total_streaming_time_sec if total_streaming_time_sec > 0 else 0
        avg_llm_latency = stats["total_llm_latency_ms"] / stats["total_llm_calls"] if stats["total_llm_calls"] > 0 else 0
        # Exclude call_agent count from denominator to match numerator (call_agent latency routed separately)
        call_agent_count = stats["tool_calls_by_name"].get("call_agent", 0)
        non_agent_tool_calls = stats["total_tool_calls"] - call_agent_count
        avg_tool_latency = stats["total_tool_latency_ms"] / non_agent_tool_calls if non_agent_tool_calls > 0 else 0

        # Tool success rates
        tool_success_rates = {}
        for name, count in stats["tool_calls_by_name"].items():
            failures = stats["tool_failures_by_name"].get(name, 0)
            tool_success_rates[name] = {
                "total": count,
                "failures": failures,
                "success_rate": round((count - failures) / count * 100, 1) if count > 0 else 100.0,
                "avg_latency_ms": round(stats["tool_latency_by_name"].get(name, 0) / count, 1) if count > 0 else 0,
            }

        return {
            "session_id": self.session_id,
            "total_turns": stats["total_turns"],
            "total_llm_calls": stats["total_llm_calls"],
            "total_tool_calls": stats["total_tool_calls"],
            "total_input_tokens_est": stats["total_input_tokens_est"],
            "total_output_tokens_est": stats["total_output_tokens_est"],
            "total_tokens": total_tokens,
            "avg_tps": round(avg_tps, 1),
            "avg_llm_latency_ms": round(avg_llm_latency, 1),
            "avg_tool_latency_ms": round(avg_tool_latency, 1),
            "call_agent_count": call_agent_count,
            "call_agent_latency_ms": stats.get("call_agent_latency_ms", 0),
            "total_loops_detected": stats["total_loops_detected"],
            "total_retries": stats["total_retries"],
            "total_compressions": stats["total_compressions"],
            "write_failures": stats.get("write_failures", 0),
            "agent_instance_calls": stats["agent_instance_calls"],
            "llm_calls_by_model": dict(stats["llm_calls_by_model"]),
            "tool_effectiveness": tool_success_rates,
        }

    def get_config_comparison(self) -> List[Dict]:
        """Get per-config stats for A/B comparison."""
        with _telemetry_lock:
            result = []
            for fp, cs in self._config_stats.items():
                total_tokens = cs["input_tokens_est"] + cs["output_tokens_est"]
                total_time_sec = cs["total_duration_ms"] / 1000 if cs["total_duration_ms"] > 0 else 0
                avg_turn_time = cs["total_duration_ms"] / cs["turns"] if cs["turns"] > 0 else 0
                avg_tokens_per_turn = total_tokens / cs["turns"] if cs["turns"] > 0 else 0

                # Streaming time and TPS for this config
                config_streaming_sec = cs["total_streaming_time_ms"] / 1000 if cs["total_streaming_time_ms"] > 0 else 0
                config_tps = cs["output_tokens_est"] / config_streaming_sec if config_streaming_sec > 0 else 0

                # Tool success rates for this config
                tool_rates = {}
                for name, count in cs["tool_calls_by_name"].items():
                    failures = cs["tool_failures_by_name"].get(name, 0)
                    tool_rates[name] = {
                        "total": count,
                        "success_rate": round((count - failures) / count * 100, 1) if count > 0 else 100.0,
                    }

                result.append({
                    "config_fingerprint": fp,
                    "config_description": dict(cs["config_description"]),
                    "turns": cs["turns"],
                    "llm_calls": cs["llm_calls"],
                    "tool_calls": cs["tool_calls"],
                    "input_tokens_est": cs["input_tokens_est"],
                    "output_tokens_est": cs["output_tokens_est"],
                    "total_tokens": total_tokens,
                    "total_duration_sec": round(total_time_sec, 1),
                    "avg_turn_duration_ms": round(avg_turn_time, 1),
                    "avg_tokens_per_turn": round(avg_tokens_per_turn),
                    "total_streaming_time_sec": round(config_streaming_sec, 1),
                    "avg_tps": round(config_tps, 1),
                    "loops_detected": cs["loops_detected"],
                    "retries": cs["retries"],
                    "tool_effectiveness": tool_rates,
                })

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

        event = {
            "type": "session_end",
            "session_id": self.session_id,
            "timestamp": _now_iso(),
            "summary": summary,
            "config_comparison": config_comparison,
        }
        try:
            self._write_critical_event(event)
            self.close()  # Flush and close file handle after final event
        except Exception as e:
            # Fallback — log the full summary at INFO so it appears in console logs
            _logger.info(
                "[TELEMETRY] Session %s end (summary: turns=%d, llm_calls=%d, tool_calls=%d, "
                "write_failures=%d) — file write failed: %s",
                self.session_id, summary["total_turns"], summary["total_llm_calls"],
                summary["total_tool_calls"], summary.get("write_failures", 0), e,
            )

    def _write_critical_event(self, event: Dict):
        """Append an event to the JSONL log file; raise on I/O failure.

        Unlike ``_write_event`` this does not swallow exceptions — use for
        events that should be guaranteed to persist (e.g., session_end).
        Thread-safe via module-level lock. Uses cached file handle.
        """
        self.events.append(event)

        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        with _telemetry_lock:
            if self._log_file is not None and not self._log_file.closed:
                self._log_file.write(line)
                self._log_file.flush()
            else:
                # Fallback: reopen file handle so subsequent writes don't pay open/close cost
                try:
                    fh = open(self.log_path, "a", encoding="utf-8")
                    fh.write(line)
                    fh.flush()
                    self._log_file = fh  # Update cached handle for future writes
                except Exception:
                    raise

    def _write_event(self, event: Dict):
        """Append an event to the JSONL log file and in-memory buffer.

        Thread-safe via module-level lock. Uses cached file handle.
        Failures are logged at WARNING level with a failure counter.
        Every write is flushed to avoid data loss on crash.
        """
        self.events.append(event)

        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        try:
            with _telemetry_lock:
                if self._log_file is not None and not self._log_file.closed:
                    self._log_file.write(line)
                    self._log_file.flush()  # Flush to avoid data loss on crash
                else:
                    # Fallback: reopen file handle so subsequent writes don't pay open/close cost
                    fh = open(self.log_path, "a", encoding="utf-8")
                    fh.write(line)
                    fh.flush()
                    self._log_file = fh  # Update cached handle for future writes
        except Exception as e:
            _logger.warning(  # Upgraded from debug to warning for visibility
                "Failed to write telemetry event [%s]: %s", event.get("type", "unknown"), e,
            )
            self._session_stats["write_failures"] += 1

    def close(self):
        """Flush and close the log file handle. Call during shutdown cleanup."""
        with _telemetry_lock:
            if self._log_file is not None and not self._log_file.closed:
                try:
                    self._log_file.flush()
                    self._log_file.close()
                except Exception as e:
                    _logger.warning("Error closing telemetry log file: %s", e)

    # ── Aggregation & Reporting (moved above persistence for logical grouping) ─


def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
