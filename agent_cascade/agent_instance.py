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
from agent_cascade.settings import DEFAULT_COMPRESSION_COOLDOWN_SECONDS


class AgentState(Enum):
    """Agent lifecycle states for the state machine.
    
    States and valid transitions:
    - IDLE: Agent exists but is not currently executing (initial state after creation)
    - RUNNING: Agent is actively processing inside engine.run()
    - SLEEPING: Agent is waiting for async background tools to complete
    - COMPLETING: Agent has finished its work, cleaning up
    - TERMINATED: Agent has been terminated (final state)
    
    Valid transitions are enforced by _transition() method.
    
    The state machine fully replaces the old is_active boolean field:
    - IDLE replaces is_active=False (agent not executing)
    - RUNNING replaces is_active=True (agent executing)
    """
    IDLE = auto()
    RUNNING = auto()
    SLEEPING = auto()
    COMPLETING = auto()
    TERMINATED = auto()


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

    Required fields (no default): instance_name, agent_class, conversation, 
        created_at, last_activity, latest_marker_index.

    Optional fields (has defaults): state (default IDLE), and all remaining fields 
        for compression, token tracking, nesting, etc.

    The instance_name uniquely identifies an agent in the pool.

    Design principle: AgentInstance is primarily DATA. All orchestration logic
    lives in ExecutionEngine, not here. This dataclass just holds state.
    
    State machine replaces the old is_active boolean field. Use the state property
    or is_running() helper method to check execution status.
    """

    # ── Identity (no defaults) ─────────────────────────────────────────
    instance_name: str                    # Unique identifier (e.g., "Main", "Coder1", "Researcher3")
    agent_class: str                     # Template class name (e.g., "Orchestrator", "coder", "researcher")

    # ── Conversation State (no defaults) ────────────────────────────────
    conversation: List[Message]          # Full cumulative history for this instance

    # ── Metadata (no defaults) ──────────────────────────────────────────
    created_at: float                    # time.monotonic() timestamp
    last_activity: float                 # time.monotonic() timestamp of last message

    # ── Compression State (no defaults) ─────────────────────────────────
    latest_marker_index: int             # Index in conversation where latest summary marker was inserted

    # ── Execution State (with defaults) ─────────────────────────────────
    state: AgentState = field(default=AgentState.IDLE)  # Current lifecycle state (default: IDLE, not RUNNING)
    _state_lock: threading.RLock = field(default_factory=threading.RLock)  # Lock for state transitions
    
    # SLEEPING state tracking fields (for async tools) - part of Execution State
    sleeping_since: Optional[float] = None  # time.monotonic() when entered SLEEPING state
    _last_wakeup_log: float = field(default=0.0)  # Last time wakeup message was logged

    # ── Remaining Fields with defaults ──────────────────────────────────
    is_terminated: bool = False          # Set when terminate_instance() is called on this instance (Fix Bug41)
    max_turns: Optional[int] = None      # Per-instance turn limit (None = use default 50)
    parent_instance: Optional[str] = None  # Who called this agent (None for root/main)
    compression_summary: Optional[str] = None  # Current cumulative summary (if any)
    _compression_lock: threading.RLock = field(default_factory=threading.RLock)  # RLock: recovery paths may re-acquire via instance_conversations.__setitem__

    # ── System Prompt Initialization Tracking (Bug #41 fix) ────────────────
    _system_prompt_initialized: bool = field(default=False)  # Track if system prompt has been initialized once. Once set, session metadata and resources are frozen — they won't update even if the environment changes.

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

    # ── Concurrency Slot Management (Parent Slot Acquisition Fix) ───────────
    _slot_release: Optional[callable] = None  # Callback to release the endpoint concurrency slot when transitioning to SLEEPING or exiting

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
            AgentState.SLEEPING: {AgentState.RUNNING, AgentState.COMPLETING, AgentState.TERMINATED, AgentState.IDLE},
            AgentState.COMPLETING: {AgentState.TERMINATED, AgentState.IDLE},
            AgentState.TERMINATED: set(),  # Terminal state - no transitions out
            AgentState.IDLE: {AgentState.RUNNING, AgentState.TERMINATED},
        }
        
        if new_state not in valid_transitions.get(self.state, set()):
            raise InvalidStateTransition(self.state, new_state)
        
        self.state = new_state

    # ── Helper properties (replaces the old is_active boolean) ──────────

    @property
    def is_running(self) -> bool:
        """Check if this agent is currently executing inside engine.run().
        
        Replaces the old is_active boolean field. Returns True when state is RUNNING.
        Thread-safe: reads self.state under _state_lock protection.
        
        Returns:
            bool: True if state == AgentState.RUNNING, False otherwise.
        """
        with self._state_lock:
            return self.state == AgentState.RUNNING

    def is_executing(self) -> bool:
        """Alias for is_running, provided for code clarity in some contexts.
        
        Thread-safe: reads self.state under _state_lock protection.
        
        Returns:
            bool: True if agent is actively processing (RUNNING state).
        """
        with self._state_lock:
            return self.state == AgentState.RUNNING


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
    compression_force_cooldown: float = DEFAULT_COMPRESSION_COOLDOWN_SECONDS  # Minimum seconds between forced compressions (prevent thrashing)
    compression_max_attempts: int = 3         # Max forced compressions before considering overfeeding
    security_check_timeout: float = 120.0     # Max seconds for security advisor
    max_auto_rollbacks: int = 3               # Max loop recovery retries
    max_nesting_depth: int = 10               # Max depth of nested agent calls (prevent infinite chains)
    max_workers: int = 10                     # ThreadPoolExecutor workers for parallel agent execution
    auto_continue: bool = True                # Auto-continue on message truncation (respects user toggle)
    
    # SLEEPING state settings (for async tools)
    sleeping_timeout: float = 300.0           # Max seconds to wait for background tools before timeout
    sleeping_wakeup_interval: float = 5.0     # Interval between wakeup log messages while SLEEPING