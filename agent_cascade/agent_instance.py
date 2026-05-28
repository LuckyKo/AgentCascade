"""
Unified Agent Instance Model — Phase 1 of the AgentCascade Architecture Rewrite.

Every agent (including the "main" orchestrator) is represented as an AgentInstance.
There is no inheritance hierarchy — OrchestratorAgent is simply the first instance
created in the pool with agent_class="Orchestrator".

See DESIGN_REWRITE.md §2.1 for design rationale.
"""

import threading
from dataclasses import dataclass, field
from typing import List, Optional

from agent_cascade.llm.schema import Message


@dataclass(slots=True)
class AgentInstance:
    """
    The canonical representation of any agent — main or sub.

    Every field is mandatory (except where the design explicitly allows None).
    There are no optional fields that distinguish "main" from "sub".
    The instance_name uniquely identifies an agent in the pool.

    Design principle: AgentInstance is primarily DATA. All orchestration logic
    lives in ExecutionEngine, not here. This dataclass just holds state.
    """

    # ── Identity ──────────────────────────────────────────────────────
    instance_name: str                    # Unique identifier (e.g., "Maine", "Coder1", "Researcher3")
    agent_class: str                     # Template class name (e.g., "Orchestrator", "coder", "researcher")

    # ── Conversation State ────────────────────────────────────────────
    conversation: List[Message]          # Full cumulative history for this instance

    # ── Execution State ───────────────────────────────────────────────
    is_active: bool                      # Currently executing a run() turn
    max_turns: Optional[int]             # Per-instance turn limit (None = use default 50)

    # NOTE: halt state is NOT stored here — it lives in pool._halted_instances set.
    #       ExecutionEngine checks via self.pool.is_instance_halted(instance.instance_name)
    #       to ensure a single source of truth across threads.

    # ── Metadata ──────────────────────────────────────────────────────
    parent_instance: Optional[str]       # Who called this agent (None for root/main)
    created_at: float                    # time.monotonic() timestamp
    last_activity: float                 # time.monotonic() timestamp of last message

    # ── Compression State ─────────────────────────────────────────────
    compression_summary: Optional[str]   # Current cumulative summary (if any)
    latest_marker_index: int             # Index in conversation where latest summary marker was inserted
    _compression_lock: threading.RLock = field(default_factory=threading.RLock)  # RLock: recovery paths may re-acquire via instance_conversations.__setitem__

    # ── Token Count Cache (Fix #2) ────────────────────────────────────────
    _cached_token_count: int = field(default=0)                  # Cached cumulative token count for conversation
    _last_token_count_conversation_length: int = field(default=0)  # Length of conversation when tokens were last counted


@dataclass
class CompressResult:
    """
    Return type of compress_context(). Matches agent_cascade/compression/result.py.

    Carries the outcome of a compression operation so callers can decide how to
    proceed (retry, yield error, update summaries, etc.).
    """
    success: bool
    summary_text: Optional[str] = None
    marker_message: Optional[Message] = None
    messages_discarded: int = 0
    tail_count: int = 0
    error: Optional[str] = None
    mode: str = ""


class LoopDetectedError(Exception):
    """Raised when detect_loop() finds a repetitive pattern in agent conversation."""

    def __init__(self, reason: str, pop_count: int):
        super().__init__(reason)
        self.reason = reason
        self.pop_count = pop_count  # How many messages to roll back


@dataclass
class PoolSettings:
    """Configurable thresholds and timeouts for the agent pool."""

    idle_timeout_seconds: float = 300.0       # Auto-dismiss after this much inactivity
    idle_check_interval: float = 60.0         # Check every N seconds
    compression_force_threshold: float = 95.0 # Force compress at X% usage
    compression_warning_threshold: float = 85.0  # Warn at X% usage
    compression_timeout: float = 120.0        # Max seconds for compression to complete
    security_check_timeout: float = 120.0     # Max seconds for security advisor
    max_auto_rollbacks: int = 3               # Max loop recovery retries