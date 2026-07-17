"""
Unified Agent Instance Model — Phase 1 of the AgentCascade Architecture Rewrite.

Every agent (including the "main" orchestrator) is represented as an AgentInstance.
There is no inheritance hierarchy — the orchestrator is simply the first instance
created in the pool with agent_class="Orchestrator".

See DESIGN_REWRITE.md §2.1 for design rationale.
"""

import json                           # NEW: for serializing non-string values in cache preview
import threading
from collections import deque         # NEW: rolling buffer for cache pool
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, List, Optional

from agent_cascade.llm.schema import Message
from agent_cascade.settings import (
    DEFAULT_COMPRESSION_COOLDOWN_SECONDS, DEFAULT_COMPRESSION_MAX_ATTEMPTS,
    COMPRESSION_FORCE_THRESHOLD, COMPRESSION_WARNING_THRESHOLD, COMPRESSION_TIMEOUT,
    COMPRESSION_SECURITY_CHECK_TIMEOUT,
    AGENT_IDLE_TIMEOUT, SYSTEM_AGENT_IDLE_TIMEOUT, AGENT_IDLE_CHECK_INTERVAL,
    AGENT_MAX_AUTO_ROLLBACKS, AGENT_MAX_NESTING_DEPTH, AGENT_MAX_WORKERS,
    AGENT_SLEEPING_TIMEOUT, AGENT_SLEEPING_WAKEUP_INTERVAL,
    CI_EXECUTION_TIMEOUT, CI_WATCHDOG_TIMEOUT, CI_STALE_CONTAINER_TTL,
    CACHE_POOL_ENABLED, CACHE_POOL_SIZE, CACHE_THRESHOLD_CHARS,
    DEFAULT_LOAD_SKILL_MODE,
)


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


# ── Cache Pool Data Structures ────────────────────────────────────────────────

@dataclass(slots=True)
class CacheEntry:
    """Single entry in the argument/output cache pool.
    
    Stores a tool argument dict or output string along with metadata
    for display and resolution via {USE_CACHED_ENTRY_N} syntax.
    """
    index: int                    # Sequential N for {USE_CACHED_ENTRY_N} (monotonic, never wraps)
    category: str                 # "arg" or "output"
    source_tool: str              # Tool name that generated this entry
    value: Any                    # Full cached value (for resolution via deep copy)
    preview: str                  # Truncated display string (< 200 chars)
    char_count: int               # Length of full string representation


class ArgumentCachePool:
    """Thread-safe rolling cache pool for tool arguments and outputs.
    
    Per-instance scope with a fixed-size deque that wraps around,
    overwriting oldest entries when the limit is reached.
    
    Indices grow monotonically (never reset). An evicted entry's index
    becomes stale — lookups return None, which signals callers to leave
    placeholders as-is.
    """
    __slots__ = ('_entries', '_lock', '_next_index', 'max_size', 'enabled')
    
    def __init__(self, max_size: int = 50):
        self._entries: deque[CacheEntry] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._next_index = 1          # Monotonically increasing (never wraps/reset)
        self.max_size = max_size
        self.enabled = True           # Toggle on/off
    
    def add(self, category: str, source_tool: str, value: Any,
            threshold: int = 0) -> int:
        """Add entry and return its index N. Returns -1 when disabled or below threshold.
        
        Args:
            category: "arg" or "output"
            source_tool: Tool name (e.g. "read_file", "read_file.path")
            value: The value to cache
            threshold: Min char count to trigger caching (0 = always cache)
        """
        # Serialize to string for preview/length — with error handling
        try:
            val_str = json.dumps(value) if not isinstance(value, str) else value
        except (TypeError, ValueError):
            # Fallback for unserializable objects (e.g., custom types, cycles)
            val_str = str(value)

        # Skip if below threshold
        if len(val_str) <= threshold:
            return -1

        # Store head + tail for meaningful mid-truncation display (start ... end)
        if len(val_str) > 200:
            preview = val_str[:100] + val_str[-100:]
        else:
            preview = val_str

        with self._lock:
            # Check enabled flag inside lock to avoid race with config toggle
            if not self.enabled:
                return -1

            entry = CacheEntry(
                index=self._next_index,
                category=category,
                source_tool=source_tool,
                value=value,
                preview=preview,
                char_count=len(val_str),
            )
            self._entries.append(entry)  # deque maxlen handles eviction automatically
            idx = self._next_index
            self._next_index += 1
            return idx
    
    def get(self, index: int) -> Optional['CacheEntry']:
        """Look up entry by its N index. Returns None if evicted or not found."""
        with self._lock:
            for entry in reversed(self._entries):
                if entry.index == index:
                    return entry
            return None  # Entry was evicted (too old) — caller should leave placeholder as-is
    
    def get_state_summary(self, max_display: int = 10) -> str:
        """Return truncated state string for system_info display.

        Uses _next_index as the insert head to show [idx-max_display : idx] range.
        Format (one line per entry, no code blocks):
          [N=  1] [OUT] system_info        (1247 chars)  "preview_head ... preview_tail"
        """
        with self._lock:
            entries = list(self._entries)
            head = self._next_index  # Next index to be assigned (exclusive upper bound)
        
        if not entries:
            return "  Cache Pool: empty\n"
        
        lines = [f"  Cache Pool: {len(entries)}/{self.max_size} entries (enabled={self.enabled})\n"]
        # Show entries in [head - max_display : head) range, newest first
        cutoff = head - max_display
        display_entries = [e for e in entries if e.index > cutoff]
        display_entries.sort(key=lambda e: e.index, reverse=True)
        for e in display_entries:
            marker = "ARG" if e.category == "arg" else "OUT"
            # Escape whitespace as visible sequences (\n, \r, \t), then mid-truncate
            p = e.preview.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
            if len(p) > 120:
                half = 50
                p = p[:half] + ' ... ' + p[-half:]
            # Pad source_tool to 24 chars for column alignment; wrap preview in quotes
            tool_label = f"{e.source_tool:<24}"
            lines.append(f"    [N={e.index:>3}] [{marker}] {tool_label}"
                        f"({e.char_count} chars)  \"{p}\"")
        
        older = [e for e in entries if e.index <= cutoff]
        if older:
            indices = ", ".join(str(e.index) for e in older[:5])
            lines.append(f"    ... and {len(older)} older entries (oldest: N={indices}...)")
        
        return "\n".join(lines)


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
    _current_turn: int = field(default=0)  # Current turn number during execution (for system_info display)
    parent_instance: Optional[str] = None  # Who called this agent (None for root/main)
    _child_instances: List[str] = field(default_factory=list)  # Direct children spawned by this agent (for per-instance tree tracking / recursive dismissal visibility)
    compression_summary: Optional[str] = None  # Current cumulative summary (if any)
    _compression_lock: threading.RLock = field(default_factory=threading.RLock)  # RLock: recovery paths may re-acquire via instance_conversations.__setitem__

    # ── System Prompt Initialization Tracking (Bug #41 fix) ────────────────
    _system_prompt_initialized: bool = field(default=False)  # Track if system prompt has been initialized once. Once set, session metadata and resources are frozen — they won't update even if the environment changes.

    # ── Token Count Cache (Fix #2) ────────────────────────────────────────
    _cached_token_count: int = field(default=0)                  # Cached cumulative token count for conversation
    _last_token_count_conversation_length: int = field(default=0)  # Length of conversation when tokens were last counted

    # ── Ground-Truth Token Counts from LLM API (Fix Force Compression Loop) ──
    _last_actual_token_count: int = field(default=0)             # Actual token count from LLM API response (ground truth)
    _allocated_max_input_tokens: int = field(default=0)          # Max input tokens allocated for the last LLM call

    # ── Compression Cooldown and Overfeeding Detection ───────────────────────
    _last_force_compress_time: float = field(default=0.0)        # Monotonic timestamp of last forced compression attempt
    _force_compress_count: int = field(default=0)                # Number of forced compressions in current session

    # ── Nesting Depth (Fix: prevent infinite nesting) ──────────────────────
    _nest_depth: int = field(default=0)                           # Depth in the agent call chain (0 = root)

    # ── Per-instance LLM config override (Fix: avoid template mutation) ────
    _generate_cfg_override: Optional[dict] = field(default=None)  # Merged into generate_cfg at call time without mutating template

    # ── Streaming State (Streaming UI Content Update Fix) ──────────────────
    _streaming_responses: List[Message] = field(default_factory=list)  # Partial LLM content during streaming, updated every ~150ms

    # ── Concurrency Slot Management (Parent Slot Acquisition Fix) ───────────
    _slot_release: Optional[Callable[[], None]] = None  # Callback to release the endpoint concurrency slot when transitioning to SLEEPING or exiting
    _skip_slot_acquire: bool = False  # When True, engine.run() skips slot acquisition (used for nested agents like Security/Compressor)

    # ── Persistent Working Set Caching (Fix LLM Reprocessing) ────────────────
    # These fields cache the working set to preserve LLM prefix caching across turns.
    # Simple model: if config unchanged, extend with new messages; otherwise rebuild.
    _cached_messages: List[Message] = field(default_factory=list)      # Full conversation working set
    _cached_llm_messages: List[Message] = field(default_factory=list)  # Sliced working set for LLM
    _last_config_version: int = field(default=-1)                      # Pool config version at last rebuild
    
    # ── Loop Detection Cooldown (Fix /compress Bug) ───────────────────────────
    # After compression/rollback, the conversation state has concentrated patterns that can trigger
    # false-positive loop detection. This flag suppresses loop detection on the next turn only.
    _suppress_loop_detection_next_turn: bool = field(default=False)     # Cooldown flag for loop detection after compression/rollback
    _loop_rollback_count: int = field(default=0)                       # Track rollback count to prevent infinite recovery loops

    # ── Continue Button Message Merge (Fix Duplication Bug Option B) ───────────
    # When Continue is clicked, the last assistant message is popped from conversation
    # and stored here temporarily. After LLM generates its response, this content is
    # merged with the new response to create a single concatenated message.
    _continue_saved_msg: Optional[Message] = field(default=None)  # Temporary storage for Continue button merge

    # ── Compression-Specific Notification Queue ───────────────────────────────
    # Used by compression/handler.py to queue notifications during forced compression.
    # These are drained and surfaced after compression completes.
    _pending_notifications: List[str] = field(default_factory=list)  # Compression notification queue

    # ── Generic Tool Warning Queue ────────────────────────────────────────────
    # Separate from _pending_notifications (compression-specific). Used by tools
    # like path resolution to queue warnings that are drained into tool results.
    _tool_warnings: List[str] = field(default_factory=list)  # Generic warning queue for tool responses

    # ── Cache Notification Queue ──────────────────────────────────────────────
    # Parallel to _tool_warnings but for cache pool events. Drained into tool
    # results so the agent knows when its args/outputs were cached by the system.
    _cache_notifications: List[str] = field(default_factory=list)

    # ── Cache Pool (Feature: USE_PREV_ARG → full caching system) ────────────
    # Initialized lazily by execution engine on first access to avoid issues with
    # dataclass default_factory and threading. Each instance gets its own pool.
    cache_pool: Optional['ArgumentCachePool'] = None

    # ── Centralized Message Mutation API (Phase 3) ───────────────────────
    # These methods encapsulate ALL conversation mutations, keeping cached lists
    # in sync and invalidating caches according to the update schema from todo.md:
    #   - append operations: extend cached lists, invalidate token cache only
    #   - edit/trim operations: invalidate working set cache (content changed)
    #   - rebuild/reset operations: full cache invalidation
    # All methods are thread-safe using _compression_lock.
    # Must be called after AgentInstance is fully initialized.

    def append_message(self, message: Message) -> None:
        """Append a single message. Updates cached lists atomically.

        Update Schema Operation: add message/tool response/user msg (append)
        Cache Behavior:
            - _cached_messages: EXTEND with new message
            - _cached_llm_messages: EXTEND with new message
            - _last_token_count_conversation_length: INVALIDATE (-1)
            - Working set cache: PRESERVED (no rebuild needed)

        Thread Safety: Uses _compression_lock for atomic update
        """
        with self._compression_lock:
            self.conversation.append(message)
            self._cached_messages.append(message)
            self._cached_llm_messages.append(message)
            self._last_token_count_conversation_length = -1

    def append_messages(self, messages: List[Message]) -> None:
        """Append multiple messages. Same as append_message but batched for efficiency.

        Update Schema Operation: add message/tool response/user msg (append, batched)
        Cache Behavior: Same as append_message but for multiple messages

        Thread Safety: Uses _compression_lock for atomic update
        """
        if not messages:
            return
        with self._compression_lock:
            self.conversation.extend(messages)
            self._cached_messages.extend(messages)
            self._cached_llm_messages.extend(messages)
            self._last_token_count_conversation_length = -1

    def edit_message_in_place(self, index: int, new_message: Message) -> None:
        """Replace message at index. Invalidates working set cache (content changed).

        Update Schema Operation: user history edit (edit)
        Cache Behavior:
            - _cached_messages: REPLACE at same index
            - _cached_llm_messages: REPLACE at same index (if within bounds)
            - _last_token_count_conversation_length: INVALIDATE (-1)
            - Working set cache: PARTIALLY INVALIDATED (content changed)

        Thread Safety: Uses _compression_lock for atomic update
        """
        with self._compression_lock:
            self.conversation[index] = new_message
            if index < len(self._cached_messages):
                self._cached_messages[index] = new_message
            if index < len(self._cached_llm_messages):
                self._cached_llm_messages[index] = new_message
            self._last_token_count_conversation_length = -1

    def insert_message_at_head(self, message: Message) -> None:
        """Insert message at the beginning (index 0). Invalidates working set cache.

        Used for system message injection. Inserting at head shifts all indices,
        requiring working set cache invalidation.

        Update Schema Operation: P7 system prompt injection (special case of edit)
        Cache Behavior:
            - _cached_messages: CLEAR (indices shifted)
            - _cached_llm_messages: CLEAR (indices shifted)
            - _last_token_count_conversation_length: INVALIDATE (-1)

        Thread Safety: Uses _compression_lock for atomic update

        Note: Clears cached lists rather than inserting into them.
        Inserting at index 0 shifts all indices, requiring a full rebuild on next turn.
        """
        with self._compression_lock:
            self.conversation.insert(0, message)
            self._last_token_count_conversation_length = -1
            self._cached_messages.clear()
            self._cached_llm_messages.clear()

    def trim_tail(self, count: int) -> List[Message]:
        """Remove last N messages. Returns removed messages. Updates cached lists.

        Update Schema Operation: rollback (edit) / retry (edit)
        Cache Behavior:
            - _cached_messages: TRIM tail to match conversation
            - _cached_llm_messages: TRIM tail to match conversation
            - _last_token_count_conversation_length: INVALIDATE (-1)

        Args:
            count: Number of messages to remove from the end

        Returns:
            List of removed Message objects

        Thread Safety: Uses _compression_lock for atomic update
        """
        if count <= 0:
            return []
        with self._compression_lock:
            new_len = max(0, len(self.conversation) - count)
            removed = list(self.conversation[new_len:])
            del self.conversation[new_len:]
            del self._cached_messages[new_len:]
            del self._cached_llm_messages[new_len:]
            self._last_token_count_conversation_length = -1
        return removed

    def insert_message_at(self, index: int, message: Message) -> None:
        """Insert message at arbitrary position. Invalidates working set cache.

        Used for re-inserting user messages during retry/resume operations.
        Inserting at arbitrary positions shifts indices, requiring working set cache invalidation.

        Update Schema Operation: retry resume (edit - special case of insert)
        Cache Behavior:
            - _cached_messages: CLEAR (indices shifted)
            - _cached_llm_messages: CLEAR (indices shifted)
            - _last_token_count_conversation_length: INVALIDATE (-1)
            - _cached_token_count: RESET to 0
            - _last_actual_token_count: RESET to 0

        Args:
            index: Position to insert the message (0-based)
            message: Message object to insert

        Thread Safety: Uses _compression_lock for atomic update

        Note: Clears cached lists rather than inserting into them.
        Inserting at arbitrary positions shifts all subsequent indices,
        requiring a full rebuild on next turn.
        """
        with self._compression_lock:
            # Clamp index to valid range
            index = max(0, min(index, len(self.conversation)))
            self.conversation.insert(index, message)
            self._last_token_count_conversation_length = -1
            self._cached_messages.clear()
            self._cached_llm_messages.clear()
            # Reset token count caches for consistency with rebuild_conversation (reviewer feedback)
            self._cached_token_count = 0
            self._last_actual_token_count = 0

    def rebuild_conversation(self, new_messages: List[Message]) -> None:
        """Replace entire conversation. Full cache invalidation.

        Update Schema Operation: compression (regen) / session load (replace from json) / server startup
        Cache Behavior:
            - _cached_messages: REPLACE entirely
            - _cached_llm_messages: REPLACE entirely
            - _last_token_count_conversation_length: INVALIDATE (-1)
            - _cached_token_count: 0
            - _last_actual_token_count: 0

        IMPORTANT: This method does NOT sync the logger or UI. Callers must handle
        logger/UI sync separately (e.g., log_inst.update_history(conv)).

        Args:
            new_messages: Complete replacement conversation

        Thread Safety: Uses _compression_lock for atomic update
        """
        with self._compression_lock:
            self.conversation = list(new_messages)
            self._cached_messages = list(new_messages)
            self._cached_llm_messages = list(new_messages)
            self._cached_token_count = 0
            self._last_token_count_conversation_length = -1
            self._last_actual_token_count = 0
            self._pending_notifications = []

    def reset_conversation(self) -> None:
        """Clear everything. Full cache invalidation.

        Update Schema Operation: new session (reset)
        Cache Behavior:
            - _cached_messages: CLEAR
            - _cached_llm_messages: CLEAR
            - _last_token_count_conversation_length: INVALIDATE (-1)
            - _cached_token_count: 0
            - _last_actual_token_count: 0
            - _last_force_compress_time: 0.0
            - _force_compress_count: 0

        Thread Safety: Uses _compression_lock for atomic update
        """
        with self._compression_lock:
            self.conversation.clear()
            self._cached_messages.clear()
            self._cached_llm_messages.clear()
            self._cached_token_count = 0
            self._last_token_count_conversation_length = -1
            self._last_actual_token_count = 0
            self._last_force_compress_time = 0.0
            self._force_compress_count = 0
            self._current_turn = 0
            self._pending_notifications = []
            self._tool_warnings = []
            self._cache_notifications = []

    def clear_working_set_cache(self) -> None:
        """Clear working set cache without touching conversation.

        Used when cached lists may be out of sync (e.g., after direct mutation
        bypassing this API).

        Thread Safety: Uses _compression_lock for atomic update
        """
        with self._compression_lock:
            self._cached_messages.clear()
            self._cached_llm_messages.clear()
            self._last_token_count_conversation_length = -1

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
class PoolSettings:
    """Configurable thresholds and timeouts for the agent pool."""

    idle_timeout_seconds: float = AGENT_IDLE_TIMEOUT  # Auto-dismiss regular agents after this much inactivity
    system_agent_idle_timeout_seconds: float = SYSTEM_AGENT_IDLE_TIMEOUT  # Auto-dismiss Compressor/Security after this much inactivity
    idle_check_interval: float = AGENT_IDLE_CHECK_INTERVAL  # Check every N seconds
    compression_force_threshold: float = COMPRESSION_FORCE_THRESHOLD  # Force compress at X% usage
    compression_warning_threshold: float = COMPRESSION_WARNING_THRESHOLD  # Warn at X% usage
    compression_timeout: float = COMPRESSION_TIMEOUT  # Max seconds for compression to complete
    compression_force_cooldown: float = DEFAULT_COMPRESSION_COOLDOWN_SECONDS  # Minimum seconds between forced compressions (prevent thrashing)
    compression_max_attempts: int = DEFAULT_COMPRESSION_MAX_ATTEMPTS  # Safety net max forced compressions (overridable via env var)
    security_check_timeout: float = COMPRESSION_SECURITY_CHECK_TIMEOUT  # Max seconds for security advisor
    max_auto_rollbacks: int = AGENT_MAX_AUTO_ROLLBACKS  # Max loop recovery retries
    max_nesting_depth: int = AGENT_MAX_NESTING_DEPTH  # Max depth of nested agent calls (prevent infinite chains)
    max_workers: int = AGENT_MAX_WORKERS  # ThreadPoolExecutor workers for parallel agent execution
    auto_continue: bool = True  # Auto-continue on message truncation (respects user toggle)

    # SLEEPING state settings (for async tools)
    sleeping_timeout: float = AGENT_SLEEPING_TIMEOUT  # Max seconds to wait for background tools before timeout
    sleeping_wakeup_interval: float = AGENT_SLEEPING_WAKEUP_INTERVAL  # Wakeup log interval while SLEEPING
    
    # Inner-loop detection toggle (off by default until sensitivity is fixed)
    inner_loop_detect_enabled: bool = False   # Enable in-message loop detection during streaming
    loop_min_chars: int = 4000                # Min chars before activating heavy checks
    loop_score_threshold: int = 300           # Cumulative score to trigger detection (aligned with InnerLoopSettings default)

    # Per-mode toggles for inner-loop detector (individual detection modes)
    loop_char_run_enabled: bool = True        # Character run detection
    loop_sentence_rep_enabled: bool = True    # Sentence repetition detection
    loop_ngram_rep_enabled: bool = True       # N-gram repetition detection
    loop_block_rep_enabled: bool = True       # Block repetition detection
    loop_entropy_enabled: bool = True         # Entropy collapse detection

    # Loop retry limit (dedicated budget for inner-loop retries, separate from LLM_MAX_RETRIES)
    loop_max_retries: int = 2                # Max retries after inner-loop detection before giving up

    # Code interpreter settings (Feature: CI session sharing)
    ci_execution_timeout: int = CI_EXECUTION_TIMEOUT      # Per-call code execution timeout (seconds)
    ci_watchdog_timeout: int = CI_WATCHDOG_TIMEOUT         # Kernel inactivity watchdog timeout (seconds)
    ci_stale_container_ttl: int = CI_STALE_CONTAINER_TTL   # Stale container cleanup TTL (seconds)

    # Tail sync check (design doc §5.2 compliance — D1 fix)
    tail_sync_check_enabled: bool = True      # Enable lightweight tail-length checks after writes

    # Cache pool settings (Feature: USE_CACHED_ENTRY_N)
    cache_pool_enabled: bool = CACHE_POOL_ENABLED      # Toggle on/off (default: enabled)
    cache_pool_size: int = CACHE_POOL_SIZE             # Rolling buffer entries per instance
    cache_threshold_chars: int = CACHE_THRESHOLD_CHARS  # Min chars for output & granular arg caching

    # Skills system settings
    default_load_skill_mode: str = DEFAULT_LOAD_SKILL_MODE  # "AUTO" (default) or "NONE" — controls whether skills auto-load on call_agent
    
    