"""
Telemetry Collector — Performance & Usage Tracking for A/B Testing.

Captures structured events (LLM calls, tool calls, agent instance delegations,
loop detections, compressions) and persists them as JSONL for offline analysis.

Each event is tagged with a config fingerprint so different framework configurations
(prompts, sampling params, model selection) can be compared.
"""

import logging
import hashlib
import json
import time
import datetime
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional

_logger = logging.getLogger('agent_cascade.telemetry')

# Token estimation: ~4 chars per token (rule of thumb for LLM tokenization)
TOKEN_ESTIMATE_CHAR_DIVISOR = 4

# Max characters for system prompt before hashing in config fingerprint
SYSTEM_PROMPT_HASH_MAX_CHARS = 2000

# Default number of recent events to return in get_recent_events()
DEFAULT_RECENT_EVENT_COUNT = 50

# Max events to keep in memory before trimming
MAX_EVENTS_IN_MEMORY = 5000


class TelemetryCollector:
    """Collects and persists telemetry events for agent performance tracking."""

    def __init__(self, log_dir: str = "workspace/telemetry"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Session-level tracking
        self.session_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"telemetry_{self.session_id}.jsonl"

        # In-memory event buffer for real-time aggregation
        self.events: List[Dict] = []
        self.max_events = MAX_EVENTS_IN_MEMORY

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
            "total_tool_latency_ms": 0,
            "total_loops_detected": 0,
            "total_retries": 0,
            "total_compressions": 0,
            "tool_calls_by_name": defaultdict(int),
            "tool_failures_by_name": defaultdict(int),
            "tool_latency_by_name": defaultdict(float),
            "llm_calls_by_model": defaultdict(int),
            "agent_instance_calls": 0,
        }

        # Per-config aggregates for A/B comparison
        self._config_stats: Dict[str, Dict] = {}

        # Write session header
        self._write_event({
            "type": "session_start",
            "session_id": self.session_id,
            "timestamp": _now_iso(),
        })

    # ── Config Fingerprinting ─────────────────────────────────────────────

    @staticmethod
    def fingerprint_config(
        model: str = "",
        generate_cfg: Optional[Dict] = None,
        system_prompt: str = "",
        tools: Optional[List[str]] = None,
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

        fingerprint_data = {
            "model": model,
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
    ) -> Dict:
        """Return a human-readable config description for display."""
        cfg = generate_cfg or {}
        return {
            "model": model,
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

    # ── Event Recording ───────────────────────────────────────────────────

    def record_turn_start(
        self,
        instance_name: str,
        config_fingerprint: str = "",
        config_description: Optional[Dict] = None,
    ):
        """Mark the start of a new agent turn."""
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

        event = {
            "type": "turn_start",
            "instance": instance_name,
            "config_fingerprint": config_fingerprint,
            "timestamp": _now_iso(),
        }
        self._write_event(event)

    def record_turn_end(self, instance_name: str):
        """Mark the end of an agent turn and aggregate stats."""
        turn = self._active_turns.pop(instance_name, None)
        if not turn:
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
        self._write_event(event)

        # Update session stats
        self._session_stats["total_turns"] += 1
        self._session_stats["total_retries"] += turn["retries"]

        # Update per-config stats
        if fp:
            if fp not in self._config_stats:
                self._config_stats[fp] = {
                    "config_description": turn.get("config_description", {}),
                    "turns": 0,
                    "llm_calls": 0,
                    "tool_calls": 0,
                    "input_tokens_est": 0,
                    "output_tokens_est": 0,
                    "total_duration_ms": 0,
                    "loops_detected": 0,
                    "retries": 0,
                    "tool_calls_by_name": defaultdict(int),
                    "tool_failures_by_name": defaultdict(int),
                }
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

    def record_llm_call_start(self, instance_name: str, input_tokens_est: int = 0, model: str = ""):
        """Mark the start of an LLM API call."""
        self._active_llm_calls[instance_name] = {
            "start_time": time.perf_counter(),
            "input_tokens_est": input_tokens_est,
            "model": model,
            "first_token_time": 0,
        }

    def record_llm_first_token(self, instance_name: str):
        """Record Time To First Token (TTFT)."""
        call = self._active_llm_calls.get(instance_name)
        if call and call["first_token_time"] == 0:
            call["first_token_time"] = time.perf_counter()

    def record_llm_call_end(self, instance_name: str, output_tokens_est: int = 0):
        """Mark the end of an LLM API call."""
        call = self._active_llm_calls.pop(instance_name, None)
        if not call:
            return

        end_time = time.perf_counter()
        latency_ms = (end_time - call["start_time"]) * 1000
        ttft_ms = ((call["first_token_time"] - call["start_time"]) * 1000) if call["first_token_time"] > 0 else 0

        event = {
            "type": "llm_call",
            "instance": instance_name,
            "model": call["model"],
            "input_tokens_est": call["input_tokens_est"],
            "output_tokens_est": output_tokens_est,
            "latency_ms": round(latency_ms, 1),
            "ttft_ms": round(ttft_ms, 1),
            "tps": round(output_tokens_est / (latency_ms / 1000), 1) if latency_ms > 0 and output_tokens_est > 0 else 0,
            "timestamp": _now_iso(),
        }
        self._write_event(event)

        # Update session stats
        self._session_stats["total_llm_calls"] += 1
        self._session_stats["total_input_tokens_est"] += call["input_tokens_est"]
        self._session_stats["total_output_tokens_est"] += output_tokens_est
        self._session_stats["total_llm_latency_ms"] += latency_ms
        self._session_stats["llm_calls_by_model"][call["model"]] += 1

        # Update active turn
        turn = self._active_turns.get(instance_name)
        if turn:
            turn["llm_calls"] += 1
            turn["input_tokens_est"] += call["input_tokens_est"]
            turn["output_tokens_est"] += output_tokens_est

    def record_tool_call_start(self, instance_name: str, tool_name: str):
        """Mark the start of a tool execution."""
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
    ):
        """Mark the end of a tool execution."""
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

        self._write_event(event)

        # Update session stats
        self._session_stats["total_tool_calls"] += 1
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

    def record_agent_instance_call(
        self,
        instance_name: str,
        agent_class: str,
        caller: str,
        latency_ms: float = 0,
    ):
        """Record an agent instance delegation."""
        event = {
            "type": "agent_instance_call",
            "instance": instance_name,
            "agent_class": agent_class,
            "caller": caller,
            "latency_ms": round(latency_ms, 1),
            "timestamp": _now_iso(),
        }
        self._write_event(event)
        self._session_stats["agent_instance_calls"] += 1

    def record_loop_detected(self, instance_name: str, reason: str, auto_rolled_back: bool = False, pop_count: int = 0):
        """Record a loop detection event."""
        event = {
            "type": "loop_detected",
            "instance": instance_name,
            "reason": reason,
            "auto_rolled_back": auto_rolled_back,
            "pop_count": pop_count,
            "timestamp": _now_iso(),
        }
        self._write_event(event)
        self._session_stats["total_loops_detected"] += 1

        turn = self._active_turns.get(instance_name)
        if turn:
            turn["loops_detected"] += 1
            if auto_rolled_back:
                turn["retries"] += 1

    def record_compression(
        self,
        instance_name: str,
        fraction: float,
        tokens_before: int = 0,
        tokens_after: int = 0,
    ):
        """Record a context compression event."""
        event = {
            "type": "compression",
            "instance": instance_name,
            "fraction": fraction,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "tokens_saved": tokens_before - tokens_after,
            "timestamp": _now_iso(),
        }
        self._write_event(event)
        self._session_stats["total_compressions"] += 1

    # ── Aggregation & Reporting ───────────────────────────────────────────

    def get_session_summary(self) -> Dict:
        """Get aggregate session-level telemetry summary."""
        stats = dict(self._session_stats)

        # Calculate derived metrics
        total_tokens = stats["total_input_tokens_est"] + stats["total_output_tokens_est"]
        total_llm_time_sec = stats["total_llm_latency_ms"] / 1000 if stats["total_llm_latency_ms"] > 0 else 0

        avg_tps = stats["total_output_tokens_est"] / total_llm_time_sec if total_llm_time_sec > 0 else 0
        avg_llm_latency = stats["total_llm_latency_ms"] / stats["total_llm_calls"] if stats["total_llm_calls"] > 0 else 0
        avg_tool_latency = stats["total_tool_latency_ms"] / stats["total_tool_calls"] if stats["total_tool_calls"] > 0 else 0

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
            "total_loops_detected": stats["total_loops_detected"],
            "total_retries": stats["total_retries"],
            "total_compressions": stats["total_compressions"],
            "agent_instance_calls": stats["agent_instance_calls"],
            "llm_calls_by_model": dict(stats["llm_calls_by_model"]),
            "tool_effectiveness": tool_success_rates,
        }

    def get_config_comparison(self) -> List[Dict]:
        """Get per-config stats for A/B comparison."""
        result = []
        for fp, cs in self._config_stats.items():
            total_tokens = cs["input_tokens_est"] + cs["output_tokens_est"]
            total_time_sec = cs["total_duration_ms"] / 1000 if cs["total_duration_ms"] > 0 else 0
            avg_turn_time = cs["total_duration_ms"] / cs["turns"] if cs["turns"] > 0 else 0
            avg_tokens_per_turn = total_tokens / cs["turns"] if cs["turns"] > 0 else 0

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
                "config_description": cs["config_description"],
                "turns": cs["turns"],
                "llm_calls": cs["llm_calls"],
                "tool_calls": cs["tool_calls"],
                "input_tokens_est": cs["input_tokens_est"],
                "output_tokens_est": cs["output_tokens_est"],
                "total_tokens": total_tokens,
                "total_duration_sec": round(total_time_sec, 1),
                "avg_turn_duration_ms": round(avg_turn_time, 1),
                "avg_tokens_per_turn": round(avg_tokens_per_turn),
                "loops_detected": cs["loops_detected"],
                "retries": cs["retries"],
                "tool_effectiveness": tool_rates,
            })

        return result

    def get_recent_events(self, count: int = DEFAULT_RECENT_EVENT_COUNT) -> List[Dict]:
        """Get the most recent N events."""
        return self.events[-count:]

    def export_jsonl(self) -> str:
        """Return the path to the raw telemetry log file."""
        return str(self.log_path)

    # ── Persistence ───────────────────────────────────────────────────────

    def _write_event(self, event: Dict):
        """Append an event to the JSONL log file and in-memory buffer."""
        self.events.append(event)
        # Trim old events if over cap to prevent memory bloat
        if len(self.events) > self.max_events:
            self.events = self.events[-self.max_events:]

        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            _logger.debug(f"Failed to write telemetry event: {e}")


def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.datetime.now().isoformat()
