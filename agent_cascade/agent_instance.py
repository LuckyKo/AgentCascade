"""
Unified Agent Instance Model — Phase 1 of the AgentCascade Architecture Rewrite.

Every agent (including the "main" orchestrator) is represented as an AgentInstance.
There is no inheritance hierarchy — the orchestrator is simply the first instance
created in the pool with agent_class="Orchestrator".

See DESIGN_REWRITE.md §2.1 for design rationale.
"""

import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional

from agent_cascade.llm.schema import Message


class AgentState(Enum):
    """Agent lifecycle states for the state machine.
    
    States and valid transitions:
    - RUNNING: Agent is actively processing (initial state after creation)
    - SLEEPING: Agent is waiting for async background tools to complete
    - COMPLETING: Agent has finished its work, cleaning up
    - TERMINATED: Agent has been terminated (final state)
    - IDLE: Agent is idle (used for parallel execution tracking)
    
    Valid transitions are enforced by _transition() method.
    """
    RUNNING = auto()
    SLEEPING = auto()
    COMPLETING = auto()
    TERMINATED = auto()
    IDLE = auto()


class InvalidStateTransition(Exception):
    """Raised when an invalid state transition is attempted."""
    
    def __init__(self, current_state: AgentState, new_state: AgentState):
        self.current_state = current_state
        self.new_state = new_state
        super().__init__(f"Invalid transition from {current_state.name} to {new_state.name}")


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
    state: AgentState = field(default=AgentState.RUNNING)  # Current lifecycle state (state machine)
    _state_lock: threading.RLock = field(default_factory=threading.RLock)  # Lock for state transitions
    
    # SLEEPING state tracking fields (for async tools)
    sleeping_since: Optional[float] = None  # time.monotonic() when entered SLEEPING state
    _last_wakeup_log: float = field(default=0.0)  # Last time wakeup message was logged

    # ── Metadata ──────────────────────────────────────────────────────
    created_at: float                    # time.monotonic() timestamp
    last_activity: float                 # time.monotonic() timestamp of last message

    # ── Compression State ─────────────────────────────────────────────
    latest_marker_index: int             # Index in conversation where latest summary marker was inserted

    # ── Fields with defaults (must come after non-default fields) ──────
    is_terminated: bool = False          # Set when terminate_instance() is called on this instance (Fix Bug41)
    max_turns: Optional[int] = None      # Per-instance turn limit (None = use default 50)
    parent_instance: Optional[str] = None  # Who called this agent (None for root/main)
    compression_summary: Optional[str] = None  # Current cumulative summary (if any)
    _compression_lock: threading.RLock = field(default_factory=threading.RLock)  # RLock: recovery paths may re-acquire via instance_conversations.__setitem__

    # ── Token Count Cache (Fix #2) ────────────────────────────────────────
    _cached_token_count: int = field(default=0)                  # Cached cumulative token count for conversation
    _last_token_count_conversation_length: int = field(default=0)  # Length of conversation when tokens were last counted

    # ── Ground-Truth Token Counts (Feature 006: Fix Force Compression Loop) ──
    _last_actual_token_count: int = field(default=0)             # Actual token count from LLM API response (ground truth)
    _allocated_max_input_tokens: int = field(default=0)          # Max input tokens allocated for the last LLM call

    # ── Loop Cooldown for Forced Compression (Feature 018) ─────────────────
    _last_force_compress_time: float = field(default=0.0)        # Monotonic timestamp of last forced compression attempt
    _force_compress_count: int = field(default=0)                # Number of forced compressions in current session

    # ── Nesting Depth (Fix: prevent infinite nesting) ──────────────────────
    _nest_depth: int = field(default=0)                           # Depth in the agent call chain (0 = root)

    # ── Per-instance LLM config override (Fix: avoid template mutation) ────
    _generate_cfg_override: Optional[dict] = field(default=None)  # Merged into generate_cfg at call time without mutating template

    # ── Streaming State (Streaming UI Content Update Fix) ──────────────────
    _streaming_responses: List[Message] = field(default_factory=list)  # Partial LLM content during streaming, updated every ~150ms

    def _transition(self, new_state: AgentState) -> None:
        """Transition to a new state with validation.
        
        Args:
            new_state: The target state to transition to.
            
        Raises:
            InvalidStateTransition: If the transition is not valid.
        """
        # Valid transitions matrix
        valid_transitions = {
            AgentState.RUNNING: {AgentState.SLEEPING, AgentState.COMPLETING, AgentState.TERMINATED, AgentState.IDLE},
            AgentState.SLEEPING: {AgentState.RUNNING, AgentState.COMPLETING, AgentState.TERMINATED},
            AgentState.COMPLETING: {AgentState.TERMINATED},
            AgentState.TERMINATED: set(),  # Terminal state - no transitions out
            AgentState.IDLE: {AgentState.RUNNING, AgentState.TERMINATED},
        }
        
        if new_state not in valid_transitions.get(self.state, set()):
            raise InvalidStateTransition(self.state, new_state)
        
        self.state = new_state


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
    compression_force_cooldown: float = 2.0   # Minimum seconds between forced compressions (prevent thrashing)
    compression_max_attempts: int = 3         # Max forced compressions before considering overfeeding
    security_check_timeout: float = 120.0     # Max seconds for security advisor
    max_auto_rollbacks: int = 3               # Max loop recovery retries
    max_nesting_depth: int = 10               # Max depth of nested agent calls (prevent infinite chains)
    max_workers: int = 10                     # ThreadPoolExecutor workers for parallel agent execution
    auto_continue: bool = True                # Auto-continue on message truncation (respects user toggle)
    
    # SLEEPING state settings (for async tools)
    sleeping_timeout: float = 300.0           # Max seconds to wait for background tools before timeout
    sleeping_wakeup_interval: float = 5.0     # Interval between wakeup log messages while SLEEPING