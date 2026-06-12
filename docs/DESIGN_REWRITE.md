# AgentCascade Rewrite — Unified Single-Instance Architecture

**Date:** 2026-05-25
**Author:** DesignPlanner (Deep Research & Analysis)
**Status:** Draft Design Document
**Source Codebase:** `N:\work\WD\AgentCascade_unified`
**Key Files Analyzed:**
- `agent_orchestrator.py` (~1966 lines) — OrchestratorAgent, _run(), _stream_sub_agent_call(), loop detection, compression injection, tool execution
- `api_server.py` (~2769 lines) — WebSocket handling, session management, state broadcasting, run_agent_thread, build_state/build_stream_update
- `agent_pool.py` (~1109 lines) — AgentPool, instance_conversations, sub_agent_state, message_queues, halt/resume, surgical_rollback, slice_history_for_llm
- `api_router.py` (~644 lines) — APIEndpoint, EndpointScheduler, APIRouter, call_with_fallback, concurrency management
- `agent_cascade/compression/core.py` (~553 lines) — compress_context() unified entry point
- `agent_cascade/compression/agent_invoker.py` (~12.5 KB) — Compression Agent invocation via call_agent
- `agent_logger.py` (~475 lines) — AgentInstanceLogger, JSONL append-only logging, insert_compression_marker
- `agent_cascade/agents/fncall_agent.py` — Base FnCallAgent.run() loop (_call_llm, _call_tool, tool detection)
- `agent_cascade/agents/assistant.py` — Assistant class (extends FnCallAgent with RAG)

---

## 1. Core Architecture Principle

### 1.1 The Fundamental Problem

The current architecture has a **structural duality** at every level:

| Layer | "Main Agent" Path | "Sub-Agent" Path |
|-------|-------------------|-------------------|
| State storage | `session['history']` in api_server.py | `agent_pool.instance_conversations[name]` |
| Execution loop | `api_server.run_agent_thread()` → `agent_runner.run()` | `OrchestratorAgent._stream_sub_agent_call()` |
| Logger | Created once for session name | Created per instance via `get_logger(name, agent_class)` |
| UI rendering | `build_state()` serializes `session['history']` | `get_sub_agent_state()` reads `sub_agent_state[name]` |
| Loop detection | `detect_loop()` in api_server.py (every 10 ticks) | `detect_loop()` inside `_stream_sub_agent_call()` internal retry loop |
| Compression injection | `_inject_compression_warning_for_agent()` in _run() | Monkey-patched `_call_llm` hook inside _stream_sub_agent_call() |
| Async message injection | `drain_queue(session_name)` at top of while loop | `drain_queue(instance_name)` inside monkey-patched _call_llm |
| Parallel execution | N/A (main agent is sequential) | `ParallelAgentManager.submit_task()` in thread pool |
| Halt/resume | `stopped` flag + per-instance halt flags | Same, but checked at different points |

Every feature is implemented **twice** — once for the main orchestrator and once for sub-agents. This is the root cause of bugs where one path gets a fix and the other doesn't.

### 1.2 The Unified Principle

**Every agent instance — including the "main" agent — is an instance of the same class, managed by the same pool, using the same data structures, executing through the same loop.**

The orchestrator is NOT a special super-agent. It is simply the first agent instance created in the pool, with `instance_name = session_name` (e.g., "Maine"). When it calls `call_agent`, it invokes other instances of the same execution engine — nothing more, nothing less.

### 1.3 Key Design Decisions

1. **No inheritance hierarchy for agent types.** OrchestratorAgent extends Assistant just like every other agent. There is no "parent" class that has extra methods.
2. **One loop, one path.** A stateless `ExecutionEngine.run()` method handles all execution — LLM calls, tool execution, compression checks, async injection, loop detection — decomposed into focused phases.
3. **Single source of truth for state.** `agent_pool.instances[name]` holds the conversation. `session['history']` is eliminated entirely.
4. **call_agent is a regular tool.** The orchestrator doesn't intercept `call_agent` in `_run()`. It goes through the standard tool execution path, which delegates to another agent instance's `run()` method via the pool.
5. **The API server is a state broadcaster, not an execution engine.** It reads state from the pool and broadcasts it. It does not run agents directly.

---

## 2. Agent Instance Model

### 2.1 Single Data Structure: `AgentInstance`

```python
@dataclass(slots=True)
class AgentInstance:
    """
    The canonical representation of any agent — main or sub.
    
    Every field is mandatory. There are no optional fields that distinguish
    "main" from "sub". The instance_name uniquely identifies an agent in the pool.
    """
    # ── Identity ──────────────────────────────────────────────────────
    instance_name: str                    # Unique identifier (e.g., "Maine", "Coder1", "Researcher3")
    agent_class: str                     # Template class name (e.g., "Orchestrator", "coder", "researcher")
    
    # ── Conversation State ────────────────────────────────────────────
    conversation: List[Message]          # Full cumulative history for this instance
    
    # ── Execution State ───────────────────────────────────────────────
    is_active: bool                      # Currently executing a run() turn
    # NOTE: halt state is NOT stored here — it lives in pool._halted_instances set.
    #       ExecutionEngine checks via self.pool.is_instance_halted(instance.instance_name)
    #       to ensure a single source of truth across threads.
    max_turns: Optional[int]             # Per-instance turn limit (None = use default 50)
    
    # ── Metadata ──────────────────────────────────────────────────────
    parent_instance: Optional[str]       # Who called this agent (None for root/main)
    created_at: float                    # time.monotonic() timestamp
    last_activity: float                 # time.monotonic() timestamp of last message
    
    # ── Compression State ─────────────────────────────────────────────
    compression_summary: Optional[str]   # Current cumulative summary (if any)
    latest_marker_index: int             # Index in conversation where latest summary marker was inserted
    _compression_lock: threading.Lock = field(default_factory=threading.Lock)  # Per-agent lock for compression safety

@dataclass
class CompressResult:
    """Return type of compress_context(). Matches agent_cascade/compression/result.py."""
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
    idle_timeout_seconds: float = 300       # Auto-dismiss after this much inactivity
    idle_check_interval: float = 60         # Check every N seconds
    compression_force_threshold: float = 95.0  # Force compress at X% usage
    compression_warning_threshold: float = 85.0  # Warn at X% usage
    compression_timeout: float = 120        # Max seconds for compression to complete
    security_check_timeout: float = 120     # Max seconds for security advisor
    max_auto_rollbacks: int = 3             # Max loop recovery retries
```

### 2.2 AgentPool as a Thin Coordinator with Focused Managers

**Current problem:** The pool holds ~25 attributes across loosely-related categories (instance state, templates, logging, parallel execution, message routing, halt/resume, tool args cache, threading, callbacks, external dependencies). This is a god object that's hard to test and change.

**New design:** The pool becomes a thin coordinator (~200 lines) that owns only the instance registry, template registry, and delegates all other concerns to focused managers:

```python
class AgentPool:
    """
    Thin coordinator for all agent state. Delegates to focused managers
    rather than holding 25+ unrelated attributes.
    
    The pool coordinates — it doesn't own everything.
    """
    
    def __init__(self, llm_cfg, agents_dir, workspace_dir,
                 api_router=None, telemetry=None, operation_manager=None):
        # ── Injected dependencies (not owned) ─────────────────────────
        self.api_router = api_router
        self.telemetry = telemetry
        self.operation_manager = operation_manager
        
        # ── Core registries (owned directly) ───────────────────────────
        self.instances: Dict[str, AgentInstance] = {}  # instance_name → AgentInstance
        self.templates: Dict[str, Assistant] = {}      # agent_class → template
        
        # ── Configuration ──────────────────────────────────────────────
        self.settings = PoolSettings()                  # Configurable thresholds and timeouts
        
        # ── Focused managers (delegation targets) ─────────────────────
        # Only LoggerManager and IdleManager get their own files — they have
        # distinct lifecycles (file I/O, background thread). Halt state and
        # message routing are simple data structures that belong on the pool.
        self._execution = ParallelAgentManager(self)       # parallel execution, active_stack
        self._logger = LoggerManager(self, workspace_dir)  # logger lifecycle, recovery
        self._idle = IdleManager(self)                     # idle detection + auto-dismiss
        
        # ── Simple state (owned directly by pool, no separate manager) ──
        self._halted_instances: set = set()                # per-instance halt state
        self.message_queues: Dict[str, List[str]] = {}     # per-agent message queues
        
        # ── Global state ───────────────────────────────────────────────
        self._stopped_event = threading.Event()         # M3 fix: stopped flag for emergency shutdown
        
        # ── Agent discovery (unchanged) ───────────────────────────────
        self._discover_agents(agents_dir)
    
    @property
    def stopped(self) -> bool:
        """Check if pool has been told to stop."""
        return self._stopped_event.is_set()
    
    # ── Delegation methods — stable interfaces for callers ────────────
    
    def send_message(self, from_name: str, to_name: str, text: str):
        """Route a message to an agent."""
        self.message_queues.setdefault(to_name, []).append(text)
    
    def enqueue_message(self, instance_name: str, text: str):
        """Push a message into a specific agent's queue (no sender tracking)."""
        self.message_queues.setdefault(instance_name, []).append(text)
        self._mark_activity(instance_name)
    
    def drain_queue(self, instance_name: str) -> List[str]:
        """Drain all pending messages for an instance."""
        return self.message_queues.pop(instance_name, [])
    
    def has_messages(self, instance_name: str) -> bool:
        """Check if there are pending messages for an instance."""
        return bool(self.message_queues.get(instance_name))
    
    def halt_instance(self, instance_name: str):
        """Halt a specific instance."""
        self._halted_instances.add(instance_name)
    
    def resume_instance(self, instance_name: str):
        """Resume a halted instance."""
        self._halted_instances.discard(instance_name)
    
    def is_instance_halted(self, instance_name: str) -> bool:
        """Query halt state for an instance."""
        return instance_name in self._halted_instances
    
    def submit_parallel(self, agent_class, instance_name, args, history, caller):
        """Submit a parallel sub-agent task."""
        return self._execution.submit_task(agent_class, instance_name, args, history, caller)
    
    def find_last_marker(self, history: List[Message]) -> int:
        """Find the index of the last COMPRESSION_MARKER message in a conversation."""
        for i in range(len(history) - 1, -1, -1):
            content = history[i].get('content', '') if isinstance(history[i], dict) else getattr(history[i], 'content', '')
            if isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                return i
        return -1
    
    def clear_conversation(self, instance_name: str):
        """Remove an agent instance from the pool (used by IdleManager)."""
        if instance_name in self.instances:
            del self.instances[instance_name]
    
    def surgical_rollback(self, instance_name: str, pop_count: int, soft: bool = False, reason: str = None):
        """Remove the last `pop_count` messages from an agent's conversation (used by loop recovery)."""
        inst = self.instances.get(instance_name)
        if inst and inst.conversation:
            del inst.conversation[-pop_count:]
    
    def _mark_activity(self, instance_name: str):
        """Update last_activity timestamp for an instance."""
        inst = self.instances.get(instance_name)
        if inst:
            inst.last_activity = time.monotonic()
```

**What each manager owns:**

| Manager | Responsibility | Size |
|---------|---------------|------|
| **ParallelAgentManager** | Parallel execution, active_stack, task lifecycle | ~150 lines |
| **LoggerManager** | Logger creation, session recovery from JSONL, compression sync | ~200 lines |
| **IdleManager** | Idle detection, auto-dismissal, background checker | ~150 lines |

Halt state (`_halted_instances: set`) and message routing (`message_queues: dict`) are simple data structures (each <10 lines of logic) that belong directly on the pool rather than warranting a separate file.

**Why delegation for LoggerManager/IdleManager but not halt/routing?**

```python
# Simple state → direct attribute access is fine:
pool._halted_instances.add(name)
pool.message_queues.get(name, [])

# Complex lifecycle (file I/O, background thread) → delegate to manager:
pool._logger.get_logger(instance_name, agent_class)
pool._idle.start()
```

LoggerManager and IdleManager have distinct lifecycles (file I/O, background threads) that justify separate files. Halt state and message routing are trivial data structures — wrapping them in a class adds indirection without benefit.

### 2.3 Conversation History — One-Way, No Bidirectional Sync

**Current problem:** There are two copies of history that must be kept in sync:
- `session['history']` in api_server.py (used for UI rendering)
- `agent_pool.instance_conversations[name]` (used by the agent pool)

These diverge when compression happens, when rollback happens, when async messages are injected.

**New design:** There is only ONE copy of history per instance — stored in `AgentInstance.conversation`. The API server never holds its own copy. It always reads from the pool:

```python
# api_server.py — NO MORE session['history']
def get_agent_state(instance_name: str) -> dict:
    """Read state from the single authoritative source."""
    inst = agent_pool.instances.get(instance_name)
    if not inst:
        return None
    return {
        'instance_name': inst.instance_name,
        'agent_class': inst.agent_class,
        'messages': list(inst.conversation),  # Read-only snapshot
        'is_active': inst.is_active,
        'is_halted': agent_pool.is_instance_halted(instance_name),
        'parent_instance': inst.parent_instance,
    }
```

### 2.4 State Persistence — Two-Layer Model with Marker Stacking

**Current problem:** `AgentInstanceLogger.update_history()` sometimes rewrites/truncates the log, creating inconsistency between what's in memory and what's on disk. The dual-state sync between logger's `memory_history` and pool's `instance_conversations` is inherently fragile.

**New design: Two layers — JSONL file (mostly append-only) + in-memory working set.**

```
Layer 1: JSONL Log File (on disk)
├── Append-only for normal operations (turns, tool calls/results)
├── Compression markers are INSERTED at cut positions (exception to append-only — O(n) write)
├── Supports insert/delete/rewrite ONLY when user edits messages via UI
├── Never truncated by system operations
└── Contains ALL messages ever generated — including discarded ones

Layer 2: In-Memory Working Set (owned by pool)
├── What the LLM sees during execution
├── Built by reconstructing from JSONL on reload
└── Mutated in-place during execution (compression inserts markers, deletes old msgs)
```

**The logger writes to Layer 1; the pool owns Layer 2.** No dual-state sync — there's only one source of truth for conversation state: `AgentInstance.conversation`. The JSONL file is an audit trail that gets appended to.

```python
class AgentInstanceLogger:
    """Writes to JSONL log (Layer 1). Pool owns in-memory state (Layer 2)."""
    
    def __init__(self, instance_name: str, agent_class: str, log_dir: str):
        self.instance_name = instance_name
        self.log_path = f"logs/{agent_class}_{instance_name}_{timestamp}.jsonl"
    
    def log_message(self, message: Message):
        """Append a single message to the JSONL file. Always appends."""
        formatted = self._format_message(message)
        self._append_line(formatted)  # Append to JSONL file
    
    def log_compression_marker(self, marker_msg: Message):
        """Append a compression marker to the JSONL file. Just another append."""
        self._append_line(self._format_message(marker_msg))
```

**No `memory_history` in the logger.** The pool owns the working set. The logger just writes to disk.

### 2.5 LoggerManager Interface

```python
class LoggerManager:
    """Manages per-instance loggers and session recovery."""
    
    def get_logger(self, instance_name: str, agent_class: str) -> AgentLogger:
        """Get or create a logger for an instance."""
        ...
    
    def load_session_from_log(self, log_path: str, target_instance: str = None):
        """Recover session state from a JSONL log file on startup."""
        ...
```

### 2.6 Compression with Marker Stacking — Insertion at Cut Position

**How compression works:**

1. Agent calls `compress_context(fraction=0.4)` or system forces at >95%
2. Compression agent generates a summary if cut out fraction
3. A marker message is **inserted into the JSONL file** at the cut position — equivalent to where it would be in memory after trimming, calculated from distance from tail (skipping past tool call/reply chain boundaries - it will include the tool chain into the compressed part so we never insert before a tool response)
4. In memory, discarded messages are removed and the marker is inserted at the correct position

**The key difference from the old design:** Markers are inserted at the cut position in JSONL, keeping file structure aligned with memory structure. On reload, we find all markers via forward pass and take tail after the last marker:

```python
# Reload algorithm — single forward pass, no backward scan
def load_session_from_log(log_path):
    markers = []
    last_marker_pos = -1
    
    for i, line in enumerate(log_lines):
        msg = json.loads(line)
        if is_compression_marker(msg):
            markers.append(msg)
            last_marker_pos = i
    
    # Working set = [SYSTEM] + [all markers stacked] + [messages after last marker]
    tail = [msg for i, msg in enumerate(log_lines[last_marker_pos+1:]) 
            if not is_event_marker(msg)]
    
    return [system_message] + markers + tail
```

**After 2 compressions, the working set looks like:**
```
[SYS][COMP1: "Summarized X"][COMP2: "Summarized Y"][recent messages...]
```

**Cumulative compression timeline:**

```
Initial:     [SYS][U1][A1][U2][A2]
Compress →   JSONL: [SYS][U1][A1][COMP1][U2][A2]
             Memory: [SYS][COMP1][U2][A2]    ← markers stacked, tail after last marker

More turns:  JSONL: [SYS][U1][A1][COMP1][U2][A2][U3][A3]
Compress →   JSONL: [SYS][U1][A1][COMP1][U2][A2][COMP2][U3][A3]
             Memory: [SYS][COMP1][COMP2][U3][A3]  ← both markers stacked, tail after last
             
Feed to compressor: [COMP1][U2][A2] <- only include last marker
```

**Thread safety:** File-level locks during batch appends (compression's marker + metadata). Atomic file replacement via `os.replace()` for user-triggered rewrites. Per-line atomic writes for normal operations. Affected agent is halted during the operation.

---

## 3. Execution Engine

### 3.1 Phase-Based Execution Engine

The current `_run()` method handles **17 distinct concerns** in ~720 lines (knowledge prep, system prompt injection, logger sync, manual command detection, turn budget management, stop/halt checks, async message injection at multiple points, compression check/force, loop detection, LLM call with streaming, response normalization, auto-continue on truncation, tool detection and dispatch, tool result truncation, mid-tool urgent injection, parallel agent waiting, post-generation queue drain). Collapsing this into a single `AgentInstance.run()` generator would trade one monolith for another.

**New design: ONE loop for ALL agents, decomposed into phases.** The execution engine is stateless — it receives the AgentInstance as a parameter and orchestrates phases. Each phase is a focused method (~20-60 lines).

```python
class ExecutionEngine:
    """
    Coordinates execution of an AgentInstance through its turn loop.
    
    Stateless — receives AgentInstance as parameter. This makes testing
    straightforward: create an instance, set up state, call run(), inspect yields.
    """
    
    def __init__(self, pool: AgentPool):
        self.pool = pool
    
    def run(self, instance: AgentInstance) -> Iterator[List[Message]]:
        """Execute the agent's turn loop as a generator yielding state updates."""
        
        instance.is_active = True  # Mark active before execution starts
        try:
            # ── Phase 1: Setup ─────────────────────────────────────────────
            messages, llm_messages, response = self._setup_turn(instance)
            if not messages:
                return  # Manual command handled or error
            
            max_turns = instance.max_turns or 50
            turns_available = max_turns
            
            while turns_available > 0:
                # ── Phase 2: Pre-LLM Checks ────────────────────────────────
                # Stop/halt checks, async message injection, compression check/force, loop detection
                if self._pre_llm_checks(instance, messages, llm_messages, turns_available):
                    yield response
                    continue
                
                turns_available -= 1
                
                # ── Phase 3: LLM Call with Injection Points ────────────────
                turn_output = list(self._call_llm_with_injection(instance, llm_messages))
                
                if self.pool.stopped or self.pool.is_instance_halted(instance.instance_name):
                    yield response
                    continue
                
                # ── Phase 4: Response Processing and Tool Execution ─────────
                if self._process_response(instance, turn_output, messages, llm_messages, response):
                    yield response
                    continue
                
                # ── Phase 5: Post-Turn Processing ───────────────────────────
                if not self._post_turn_checks(instance, messages):
                    break
            
            # ── Cleanup: Turn limit reached ────────────────────────────────
            if turns_available <= 0:
                msg = Message(role=ASSISTANT, 
                    content="\n\n[SYSTEM: Turn limit reached. Ask me to continue if incomplete.]")
                response.append(msg)
                yield response
        
        except Exception as e:
            # C4 fix: Catch unhandled exceptions — log and yield error state
            logger.error(f"ExecutionEngine.run() failed for {instance.instance_name}: {e}")
            error_msg = Message(role=ASSISTANT, content=f"[SYSTEM ERROR: {e}]")
            yield [error_msg]
        finally:
            # C4 fix: Always clean up — mark inactive regardless of how we exit
            instance.is_active = False
    
    def _pre_llm_checks(self, instance, messages, llm_messages, turns_available):
        """Phase 2: Stop/halt checks, async injection, compression check, loop detection.
        
        Returns True if processing should continue to next iteration (yield + continue).
        Handles: stop/halt guard, async message drain, forced compression with rebuild,
        and loop detection (raises LoopDetectedError if found).
        """
        ...
    
    def _call_llm_with_injection(self, instance, messages):
        """Phase 3: LLM call with active function injection."""
        ...
    
    def _process_response(self, instance, turn_output, messages, llm_messages, response):
        """Phase 4: Normalize response, handle auto-continue on truncation, execute tools.
        
        Returns True if processing should continue to next iteration (tool was used or truncated).
        Handles: response normalization, logging, auto-continue check, tool detection/execution,
        mid-tool urgent injection.
        """
        ...
    
    def _post_turn_checks(self, instance, messages):
        """Phase 5: Check for final answer, wait for parallel agents, drain post-generation queue.
        
        Returns False when agent has truly completed (break from loop).
        Handles: final answer detection, thinking-only detection, parallel agent wait,
        and post-generation message drain.
        """
        ...

# ── Phase Methods (each ~20-60 lines, independently testable) ─────
    
    def _setup_turn(self, instance): ...
    
    # ── Token accounting helpers (used in compression checking) ────────
    def _get_max_tokens(self) -> int:
        """Get the maximum token budget from pool settings."""
        ...
    
    def _count_message_tokens(self, message: Message) -> int:
        """Count tokens in a single message."""
        ...
    
    def _is_truncated(self, output: List[Message]) -> bool:
        """Check if the LLM response was truncated (needs auto-continue)."""
        ...
    
    # ── Tool detection helpers ─────────────────────────────────────────
    def _get_active_functions(self, instance):
        """Get the list of active functions for an agent from its template."""
        ...
    
    def _detect_tool(self, message: Message) -> Tuple[bool, str, Any, str]:
        """Detect if a message contains a tool call. Returns (use_tool, tool_name, tool_args, result_key)."""
        ...
    
    # ── Dismiss agent handler ──────────────────────────────────────────
    def _handle_dismiss_agent(self, args: dict, instance: AgentInstance):
        """Handle dismiss_agent tool call — removes sub-agent from pool."""
        ...
    
    # ── Compression handler ───────────────────────────────────────────
    def _handle_compress_context(self, args: dict, messages: List[Message], target_agent_name: str) -> str:
        """Handle compress_context tool call — delegates to compression module."""
        ...

# ── Message builders (module-level — used by both ExecutionEngine and ParallelAgentManager) ────────


def _build_system_message(template, instance_name, caller):
    """Build the system message for an agent with session metadata."""
    ...


def _build_task_message(args, caller):
    """Build the task/user message from call_agent arguments."""
    ...


# ── Standalone utilities ────────────────────────────────────────────────

def extract_sub_agent_feedback(conversation: List[Message], instance_name: str) -> str:
    """Extract human-readable result text from an agent's final conversation messages."""
    ...


def serialize_message(msg: Message) -> dict:
    """Serialize a Message object to a JSON-serializable dict for UI rendering."""
    ...
```

**Key design decisions:**

- **Engine is stateless**: Receives AgentInstance as parameter. No internal state to manage between calls.
- **Each phase is independently testable**: `_pre_llm_checks()`, `_process_response()`, etc. can be unit tested in isolation. Detailed logic (compression, normalization, tool execution) lives within each phase method.
- **AgentInstance is primarily data** (dataclass with slots=True). All orchestration logic lives in ExecutionEngine.
- **Loop detection is a standalone module** (`agent_cascade/loop_detection.py`) — general-purpose, configurable.

### 3.2 Tool Execution in the Unified Model

All tools execute through a single `_execute_tool()` method on ExecutionEngine:

```python
# These are additional methods of ExecutionEngine (same class as run(), _call_llm(), etc.)
# They extend the class defined in section 3.1 — not a new class declaration.

    def _execute_tool(self, instance: AgentInstance, tool_name: str, 
                      tool_args: Union[str, dict], messages: List[Message]) -> str:
        """
        Execute any tool. Including call_agent and dismiss_agent.
        
        For call_agent/dismiss_agent: delegates to the pool's agent management.
        For all other tools: calls through to the template's function_map.
        """
        if tool_name == 'call_agent':
            return self._handle_call_agent(tool_args, messages, instance)
        elif tool_name == 'dismiss_agent':
            return self._handle_dismiss_agent(tool_args, instance)
        elif tool_name == 'compress_context':
            return self._handle_compress_context(tool_args, messages, instance.instance_name)
        else:
            # Standard tool execution via function_map — M5 fix: get template from pool
            template = self.pool.templates.get(instance.agent_class)
            if not template:
                raise ValueError(f"No template for agent class {instance.agent_class}")
            return template._call_tool(tool_name, tool_args, 
                                      agent_instance_name=instance.instance_name, 
                                      agent_obj=self)

    def _handle_call_agent(self, args: dict, messages: List[Message], instance: AgentInstance) -> str:
        """
        Unified call_agent handler. Works the same whether called by main agent or sub-agent.
        
        Steps:
        1. Check concurrency limits (enforced via slot acquisition in submit_task)
        2. All calls are now async - submit to thread pool (non-blocking)
        """
        instance_name = args['instance_name']
        agent_class = (args.get('agent_class') or '').strip().lower()
        
        # Concurrency enforcement happens in submit_task -> _acquire_slot()
        # Submit to background thread pool (all calls are async now)
        return self.pool.submit_parallel(
            agent_class, instance_name, args, messages, instance.instance_name
        )

    def _execute_agent_sync(self, agent_class: str, instance_name: str, 
                            args: dict, caller_history: List[Message], caller: str) -> str:
        """Execute an agent synchronously through the unified loop. Replaces _stream_sub_agent_call()."""
        
        template = self.pool.templates.get(agent_class)
        if not template:
            return f"Error: Agent class '{agent_class}' not found."
        
        # Create and run via shared helper
        inst, conv = self._create_and_run_agent(agent_class, instance_name, args, caller)
        
        try:
            result_str = extract_sub_agent_feedback(conv, instance_name)
            return f"[{instance_name}'s output]:\n{result_str}"
        finally:
            with self.pool._execution._state_lock:
                self.pool._execution.active_stack = [
                    n for n in self.pool._execution.active_stack if n != instance_name
                ]
    
    def _create_and_run_agent(self, agent_class, instance_name, args, caller):
        """Shared helper: create AgentInstance, build messages, run ExecutionEngine. Used by both sync and parallel paths."""
        template = self.pool.templates[agent_class]
        
        inst = AgentInstance(
            instance_name=instance_name, agent_class=agent_class,
            conversation=[], is_active=False, max_turns=None,
            parent_instance=caller, created_at=time.monotonic(), last_activity=time.monotonic(),
            compression_summary=None, latest_marker_index=-1,
        )
        self.pool.instances[instance_name] = inst
        
        # Build system + task messages
        sys_msg = _build_system_message(template, instance_name, caller)
        conv = [sys_msg]
        task_msg = _build_task_message(args, caller)
        conv.append(task_msg)
        inst.conversation = conv
        
        # Log and track
        logger_inst = self.pool._logger.get_logger(instance_name, agent_class)
        logger_inst.log_message(task_msg)
        
        with self.pool._execution._state_lock:
            self.pool._execution.active_stack.append(instance_name)
        
        # Execute through unified loop
        engine = ExecutionEngine(self.pool)
        final_resp = []
        for resp in engine.run(inst):
            if self.pool.stopped or self.pool.is_instance_halted(instance_name):
                break
            final_resp = resp
        
        conv.extend(final_resp)
        return inst, conv
```

### 3.3 Parallel Execution

Parallel execution remains a thread pool but uses the unified `ExecutionEngine.run()`:

```python
class ParallelAgentManager:
    def __init__(self, pool: AgentPool, max_workers: int = 10):
        self.pool = pool
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self.active_tasks: Dict[str, Tuple[concurrent.futures.Future, str, str]] = {}
        # key: instance_name → (Future, caller_session, agent_class)
        self.active_stack: List[str] = []  # Stack of currently executing agent names
        # RLock (re-entrant) not Lock — compression can run in the same thread as
        # the outer ExecutionEngine.run(), which may already hold this lock.
        # Using RLock prevents deadlock when compress_context acquires it again.
        self._state_lock = threading.RLock()
    
    def has_active_tasks(self, instance_name: str) -> bool:
        """Check if there are active parallel tasks for a given instance."""
        return any(caller == instance_name for _, caller, _ in self.active_tasks.values())
    
    def count_by_class(self, agent_class: str) -> int:
        """Count active tasks by agent class."""
        return sum(1 for _, _, cls in self.active_tasks.values() if cls == agent_class)
    
    def _acquire_slot(self, agent_class: str, instance_name: str) -> Optional[Callable[[], None]]:
        """Acquire an endpoint slot. Returns a release function or None."""
        ...  # Implementation via APIRouter acquisition
    
    def submit_task(self, agent_class: str, instance_name: str, 
                    args: dict, caller_history: List[Message], caller: str) -> str:
        """Submit a sub-agent to run in the background. Returns immediately."""
        
        # Acquire endpoint slot before submitting (blocks if at capacity)
        endpoint_release = self._acquire_slot(agent_class, instance_name)
        
        # Deep copy history for thread safety
        safe_history = copy.deepcopy(caller_history)
        
        def task_wrapper():
            try:
                engine = ExecutionEngine(self.pool)  # Create engine for this thread
                # Use shared helper for agent creation and execution
                inst, conv = engine._create_and_run_agent(agent_class, instance_name, args, caller)
                
                # Notify caller via async message queue (from_name=sub-agent, to_name=caller)
                result = extract_sub_agent_feedback(conv, instance_name)
                completion_msg = f"[Parallel Sub-Agent '{instance_name}' Finished]:\n{result}"
                self.pool.send_message(instance_name, caller, completion_msg)
                
            except Exception as e:
                error_msg = f"[Parallel Sub-Agent '{instance_name}' Failed]:\n{str(e)}"
                self.pool.send_message(instance_name, caller, error_msg)  # C3 fix: use send_message for sender tracking
            finally:
                # Release endpoint slot
                if endpoint_release:
                    endpoint_release()
                with self._state_lock:
                    self.active_stack = [
                        n for n in self.active_stack if n != instance_name
                    ]
                self.pool._mark_activity(instance_name)
                if instance_name in self.active_tasks:
                    del self.active_tasks[instance_name]
        
        future = self.executor.submit(task_wrapper)
        with self._state_lock:
            self.active_tasks[instance_name] = (future, caller, agent_class)
        
        return f"[Started agent '{instance_name}' in parallel. You will be notified when it finishes.]"
```

---

## 4. Message Flow

### 4.1 Unified Queue System

All messages flow through the same queue system, regardless of origin. See [Section 2.2](#22-pool-delegation-model) for the full pool interface including `send_message()`, `enqueue_message()`, `drain_queue()`, and `has_messages()`.

**Injection points in the unified loop:**
1. **Top of while loop** — standard async injection (same as current `_run()` line 950-962)
2. **Mid-tool-loop** — urgent injection when user sends a message during tool execution (same as current line 1433-1446)
3. **During parallel agent completion** — the parallel manager enqueues a "finished" or "failed" message to the caller's queue

### 4.2 Results Flow to UI

```python
# api_server.py — simplified state reading

def build_state(agent_pool: AgentPool, generating: bool = False) -> dict:
    """Build full state snapshot from the pool."""
    
    # C3 fix: Take a snapshot of instances to avoid RuntimeError during iteration
    # when agents are being added/removed concurrently
    instance_snapshot = dict(agent_pool.instances)
    
    all_instances = {}
    for name, inst in instance_snapshot.items():
        all_instances[name] = {
            'instance_name': inst.instance_name,
            'agent_class': inst.agent_class,
            'messages': [serialize_message(m) for m in inst.conversation],
            'is_active': inst.is_active,
            'is_halted': agent_pool.is_instance_halted(name),  # Mod7 fix: use delegation method
            'parent_instance': inst.parent_instance,
            'has_queued_messages': agent_pool.has_messages(name),
        }
    
    # M1/M4 fix: Derive session_name from root instance (parent_instance=None)
    # If multiple roots exist, take the first one — log a warning for debugging
    root_instances = [name for name, inst in instance_snapshot.items() 
                      if inst.parent_instance is None]
    session_name = root_instances[0] if root_instances else 'Maine'
    
    return {
        'instances': all_instances,
        'active_stack': list(agent_pool._execution.active_stack),
        'sub_agents': {name: state for name, state in all_instances.items() 
                       if state['parent_instance'] is not None},
        'approvals': agent_pool.operation_manager.get_pending_approvals(),
        'generating': generating,
        'session_name': session_name,
        'stopped': agent_pool.stopped,
    }

def build_stream_update(agent_pool: AgentPool, responses: List[Message], cached_h_stats=None) -> dict:
    """Build lightweight streaming delta."""
    # Take a snapshot before iterating — prevents RuntimeError during concurrent add/remove
    instance_snapshot_data = dict(agent_pool.instances)
    
    instance_snapshot = {
        name: {
            'instance_name': inst.instance_name,
            'agent_class': inst.agent_class,
            'is_active': inst.is_active,
            'is_halted': agent_pool.is_instance_halted(name),  # Mod7 fix: use delegation method
            'parent_instance': inst.parent_instance,
        }
        for name, inst in instance_snapshot_data.items()
    }
    return {
        'response_messages': [serialize_message(m) for m in responses],
        'instances': instance_snapshot,
        'active_stack': list(agent_pool._execution.active_stack),
        'generating': True,
    }
```

### 4.3 Parent-Child Relationships Without Structural Duality

The parent-child relationship is tracked via a simple field on `AgentInstance`:

```python
# No special data structure needed. Just:
inst.parent_instance = caller_name  # Set when creating the instance

# The active_stack tracks nesting for UI rendering:
# ["Maine", "Coder1", "Researcher2"] means Researcher2 was called by Coder1, which was called by Maine

# UI renders tabs based on active_stack depth:
# - Depth 0 (parent=None): Main chat tab
# - Depth 1: First-level sub-agent tabs
# - Depth 2+: Nested sub-agent tabs (indented)
```

---

## 5. State Management

### 5.1 Single Source of Truth

```python
# BEFORE (dual state):
# api_server.py: session['history'] = [...]   ← UI renders from this
# agent_pool.py: pool.instance_conversations[name] = [...]  ← agents use this
# Problem: These diverge during compression, rollback, async injection

# AFTER (single source):
# agent_pool.py: pool.instances[name].conversation = [...]  ← EVERYONE reads from here
# api_server.py NEVER holds its own copy. It always calls get_agent_state() which 
#               reads from pool.instances[name].conversation
```

### 5.2 API Server State Broadcasting

The API server becomes a pure state broadcaster:

```python
class APIServer:
    def __init__(self, agent_pool: AgentPool):
        self.pool = agent_pool
    
    def broadcast_state(self, event_type: str = 'state', data: dict = None):
        """Broadcast the current pool state to all connected WebSocket clients."""
        # C3 fix: Take a snapshot before iterating — prevents RuntimeError
        instance_snapshot = dict(self.pool.instances)
        
        snapshot = {
            'type': event_type,
            'instances': {},
            'active_stack': list(self.pool._execution.active_stack),
            'generating': any(inst.is_active for inst in instance_snapshot.values()),
            'stopped': self.pool.stopped,
        }
        
        # Only serialize instances that have messages or are active
        for name, inst in instance_snapshot.items():
            if inst.conversation or inst.is_active:
                snapshot['instances'][name] = {
                    'instance_name': inst.instance_name,
                    'agent_class': inst.agent_class,
                    'messages': [serialize_message(m) for m in inst.conversation[-100:]],  # Last 100 for perf
                    'is_active': inst.is_active,
                    'is_halted': self.pool.is_instance_halted(name),  # Mod7 fix: use delegation method
                    'parent_instance': inst.parent_instance,
                }
        
        # Send to all connected clients
        for ws in self.ws_connections:
            asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps(snapshot)),
                self.loop
            )
```

### 5.3 Halt/Resume — Uniform Across All Agents

```python
class AgentPool:
    def is_instance_halted(self, instance_name: str) -> bool:
        """Query halt state via simple set."""
        return instance_name in self._halted_instances
    
    def halt_instance(self, instance_name: str):
        """Halt a specific agent (manual pause from UI, or during compression/security)."""
        self._halted_instances.add(instance_name)
    
    def resume_instance(self, instance_name: str):
        """Resume a halted agent."""
        self._halted_instances.discard(instance_name)
    
    def halt_all_instances(self, except_instances: List[str] = None):
        """Halt all instances except the given ones — used for global emergencies (e.g., pool.stop)."""
        skip = set(except_instances or [])
        for name in self.instances:
            if name not in skip and name not in self._halted_instances:
                self.halt_instance(name)
```

---

### 5.4 Agent Lifecycle: Auto-Dismiss, Resurrection, and Session Restore

### Auto-Dismiss on Idle Timeout

The **IdleManager** runs a background thread that periodically checks for idle agents and dismisses them automatically. This prevents abandoned agent instances from accumulating in the pool indefinitely.

```python
# Settings example:
idle_timeout_seconds = 300      # 5 minutes — auto-dismiss after this much inactivity
idle_check_interval  = 60       # Check every 1 minute
```

**An agent is eligible for auto-dismissal when ALL of the following hold:**

1. It is NOT currently executing (not in active_stack)
2. Its last activity was more than `idle_timeout_seconds` ago
3. It is NOT the main orchestrator ("Maine")
4. It is NOT currently halted (halted agents are intentionally paused, e.g., during compression)

```python
class IdleManager:
    def __init__(self, pool):
        self.pool = pool
        self._stop_event = threading.Event()
    
    def start(self):
        """Start background idle checker thread."""
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
    
    def _check_loop(self):
        while not self._stop_event.is_set():
            # W5 fix: Iterate pool.instances instead of nonexistent _last_activity
            for instance_name in list(self.pool.instances.keys()):
                if self._is_idle(instance_name):
                    self._auto_dismiss(instance_name)
            # Wait for next interval (or stop event)
            self._stop_event.wait(timeout=self.pool.settings.idle_check_interval)
    
    def _is_idle(self, instance_name: str) -> bool:
        if instance_name == 'Maine':
            return False
        if instance_name in self.pool._execution.active_stack:
            return False
        if self.pool.is_instance_halted(instance_name):
            return False
        idle_secs = self._get_idle_seconds(instance_name)
        return idle_secs >= self.pool.settings.idle_timeout_seconds
    
    def _get_idle_seconds(self, instance_name: str) -> float:
        """Get seconds since last activity for an instance."""
        inst = self.pool.instances.get(instance_name)
        if not inst:
            return float('inf')  # Missing instances are always eligible
        now = time.monotonic()
        return now - inst.last_activity
    
    def _auto_dismiss(self, instance_name: str):
        """Dismiss and clean up an idle agent."""
        # Capture log_path before clearing (needed for potential resurrection)
        logger_inst = self.pool._logger.get_logger(instance_name)
        log_path = getattr(logger_inst, 'log_path', None) if logger_inst else None
        
        # Clear the instance from pool
        self.pool.clear_conversation(instance_name)
        
        # Clean up pending operation backups
        if hasattr(self.pool, 'operation_manager'):
            self.pool.operation_manager.cleanup_backups(instance_name)
        
        # Notify UI (tab closes in real-time)
        # log_path is preserved — agent can be resurrected later via call_agent with log_file
```

**What happens on auto-dismiss:** The instance is removed from `pool.instances`, its conversation cleared, and the UI tab closes. **The JSONL log file is NOT deleted** — it remains on disk as an audit trail and enables resurrection.

### Agent Resurrection via `log_file` Parameter

If an agent was auto-dismissed (or explicitly dismissed) but the orchestrator wants to resume working with it, the `call_agent` tool supports a `log_file` parameter:

```python
# In _handle_call_agent():

instance_name = args['instance_name']
agent_class = args.get('agent_class', '').strip().lower()
log_file = args.get('log_file')  # Path to JSONL log file

# If instance doesn't exist in pool and log_file is provided → restore from log
if log_file and instance_name not in self.pool.instances:
    load_result = self.pool._logger.load_session_from_log(
        log_file, 
        target_instance=instance_name
    )
    if load_result.startswith("Error"):
        return f"Failed to restore session from '{log_file}': {load_result}"
    # Session reconstructed in memory — agent resumes where it left off
```

**The resurrection flow:**

1. Orchestrator calls `call_agent(agent_class="coder", instance_name="Coder1", task="Continue...", log_file="/path/to/Coder1.jsonl")`
2. LoggerManager reads the JSONL, applies marker stacking reload algorithm (finds all markers, builds [SYS] + [markers] + [tail])
3. AgentInstance is recreated in pool with restored conversation
4. New task message is appended and ExecutionEngine runs

**This means auto-dismissal is non-destructive.** The agent's full history persists in JSONL on disk. Only the in-memory working set is cleared. Resurrection restores the working set from the log file.

### Session Restore at Pool Initialization

When the pool starts (e.g., after a crash or restart), it loads sessions from JSONL log files using the same marker stacking reload algorithm:

```python
# In LoggerManager.__init__() — session recovery:
def recover_session(log_path, target_instance_name):
    """Load a session from its JSONL log file into the pool."""
    markers = []
    last_marker_pos = -1
    
    for i, line in enumerate(jsonl_lines):
        msg = json.loads(line)
        if is_compression_marker(msg):
            markers.append(msg)
            last_marker_pos = i
    
    # Working set = [SYSTEM] + [markers stacked] + [tail after last marker]
    tail = [msg for i, msg in enumerate(jsonl_lines[last_marker_pos+1:]) 
            if not is_event_marker(msg)]
    
    working_set = [system_message] + markers + tail
    
    # Register in pool
    inst = AgentInstance(instance_name=target_instance_name, ...)
    inst.conversation = working_set
    pool.instances[target_instance_name] = inst
```

---

## 6. Compression Design

### 6.1 Preserving Log Integrity

```python
# JSONL FILE (on disk): Messages are appended during normal operation.
# During compression, a marker is INSERTED at the cut position — not appended.
# [meta]{...}
# [user]Hello
# [assistant]Hi there!
# [COMPRESSION_MARKER: "Summarized first 50%..."]  ← inserted at cut point
# [user]How are you?
# [assistant]Good, thanks!

# In-memory HISTORY (mutable, compression-aware):
# [SYSTEM] ... 
# [COMPRESSION_MARKER: "Summarized first 50%..."]
# [user]How are you?
# [assistant]Good, thanks!

# The in-memory history mirrors what the LLM sees.
# The JSONL file preserves all messages as audit trail; discarded ones simply aren't loaded into memory on reload.
```

### 6.2 Working Set Determination

The working set for LLM calls is determined by finding the last COMPRESSION_MARKER and slicing from there:

```python
# In ExecutionEngine._setup_turn():
conv = pool.instances[instance.instance_name].conversation
last_marker = pool.find_last_marker(conv)
if last_marker >= 0:
    working_set = conv[last_marker:]   # From marker (inclusive) to end — agent sees its own compression markers
else:
    working_set = conv                 # Full conversation (no compression yet)
```

### 6.3 Cumulative Compression

See §2.6 for the marker stacking algorithm and cumulative compression timeline.

The working set is determined by finding the last COMPRESSION_MARKER and slicing from there (§6.2). Multiple compressions create accumulated markers, each inserted at its cut position — after two compressions: [SYSTEM][COMP1...][COMP2: "Code task..."][recent messages...]. The system prompt includes ALL previous markers so the agent is aware of all accumulated summaries.

### 6.4 Compression in the Unified Loop

Compression is triggered at two points within the unified `ExecutionEngine.run()`:

1. **Pre-LLM check** (`_pre_llm_checks`): Before each LLM call, checks token usage. If >95%, forces compression. If >85%, injects a warning.
2. **Tool-triggered**: When the agent calls `compress_context`, the tool handler invokes `compress_context()` from `agent_cascade.compression.core`.

**Thread safety during compression:** Instead of halt/resume dance, use a per-agent threading lock:

```python
def _handle_compress_context(self, args, messages, target_agent_name):
    """Compress an agent's conversation safely using a threading lock."""
    inst = self.pool.instances[target_agent_name]
    
    # Lock prevents concurrent writes to this agent's conversation during compression
    with inst._compression_lock:
        compress_context(self.pool, target_agent_name, fraction=0.5, force=True)
```

The lock is simpler and more correct than halt/resume — it guarantees mutual exclusion on the conversation data without affecting the agent's execution state or requiring finally blocks to resume. Other parallel agents are unaffected since they have their own locks.

### 6.5 Special-Purpose Agent Invocation (Compression, Security)

Both the **Compression Agent** and **Security Advisor** are regular agents loaded from `agent_factory.py` just like any other agent class. They are invoked via `call_agent` — the same tool that orchestrators use to delegate work to sub-agents. This means they go through the same ExecutionEngine.run() loop, get their own tabs in the UI, and can be halted/resumed.

**Two paths to compression:**
- **System-triggered (forced):** `_pre_llm_checks()` detects high token usage and calls `compress_context()` directly as a library function. This is fast and doesn't require spinning up a Compression Agent tab. It runs inline within the agent's execution thread.
- **Tool-triggered (explicit):** The agent calls the `compress_context` tool → goes through the Compression Agent for quality control (better prompts, reasoning about what to keep).

The system-triggered path is optimized for speed (no agent overhead), while the tool-triggered path delegates to a proper agent for better results. Both paths ultimately call the same underlying compression logic in `agent_cascade/compression/core.py`.

#### Compression Agent Invocation

When `_pre_llm_checks()` or a tool-triggered `compress_context` fires, ExecutionEngine._handle_compress_context() is invoked:

**Key behaviors:**
- Parses args (handles both str and dict — C5 fix for Union[str, dict] tool_args)
- Acquires per-agent compression lock before modifying conversation (other parallel agents continue)
- Calls `compress_context()` from the compression module with orchestrator=self
- Lock released automatically via context manager — even on failure

#### Security Advisor Invocation

The Security Advisor is invoked when a tool requires user approval (e.g., shell commands, file writes). The API server detects a pending approval and triggers security analysis:

```python
# In api_server.py — _security_check():

prompt = SECURITY_ADVISOR_PROMPT.format(
    tool_name=ap.get('tool_name', 'unknown'),
    description=ap.get('description', ''),
    arguments=json.dumps(ap.get('tool_args', {})),
    os_info=f"{platform.system()} {platform.release()}",
    workspace_info=workspace_info,  # Includes base_dir, extra RO/RW folders
)

history = [
    {'role': USER, 'content': prompt},
]

# Get the requesting agent before constructing the Security Advisor instance
requesting_agent = ap.get('requesting_instance')

# Register in pool using unified model — Security Advisor is a regular agent instance
sec_state_key = 'Security'
sec_inst = AgentInstance(
    instance_name=sec_state_key,
    agent_class='SecurityAdvisor',
    conversation=list(history),
    is_active=False,
    max_turns=None,
    parent_instance=requesting_agent or None,
    created_at=time.monotonic(),
    last_activity=time.monotonic(),
    compression_summary=None,
    latest_marker_index=-1,
)
self.pool.instances[sec_state_key] = sec_inst

# Halt the requesting agent while waiting for security verdict
if requesting_agent:
    self.pool.halt_instance(requesting_agent)

try:
    # M7 fix: Actually enforce the timeout via safety net timer
    sec_timeout = self.pool.settings.security_check_timeout  # default: 120 seconds
    rid = ap.get('rid')  # Request ID for auto-reject on timeout
    
    def _on_timeout():
        """Auto-reject pending operation if Security Advisor doesn't respond in time."""
        logger.warning(f"Security check timeout after {sec_timeout}s — auto-rejecting")
        if rid and hasattr(self.pool, 'operation_manager'):
            self.pool.operation_manager.user_reject(rid)
        if requesting_agent:
            self.pool.resume_instance(requesting_agent)
    
    safety_timer = threading.Timer(sec_timeout, _on_timeout)
    safety_timer.start()
    
    # Run through ExecutionEngine (same as any other agent)
    engine = ExecutionEngine(self.pool)
    final_msgs = []
    for partial in engine.run(sec_inst):
        final_msgs = partial
finally:
    if 'safety_timer' in locals():
        safety_timer.cancel()
    # W8 fix: Always resume the requesting agent — even if ExecutionEngine fails
    if requesting_agent and not ap.get('_approved'):  # Only resume if not already handled
        self.pool.resume_instance(requesting_agent)
```

**What gets sent to the Security Advisor:**

1. **Tool name and description**: What tool is being requested (e.g., `shell_cmd`, `write_file`)
2. **Arguments**: The full JSON arguments of the pending tool call
3. **OS info**: Operating system and release version
4. **Workspace info**: Base directory, any extra RO/RW folders

**How Security Advisor runs:** It's a single-turn agent — it receives the prompt, analyzes the request, and responds with approval/denial reasoning. The API server parses the response for "yes/no" intent and applies the decision via `operation_manager.user_approve(rid)`.

**Halt behavior during security check:** No other agents are halted. The Security Advisor runs in its own thread/context independently. The requesting agent waits on the approval (via operation_manager's pending state), but all other parallel agents continue undisturbed.

**Security timeout (configurable):**
```python
# Settings example:
security_check_timeout = 120   # seconds — auto-reject if Security Advisor doesn't respond
security_warning_at    = 90    # seconds — inject warning into Security Advisor's queue
```
At `security_warning_at`, a system message is injected into the Security Advisor's queue urging a verdict. If `security_check_timeout` is reached, the request is auto-rejected and the agent generator is closed.

#### Common Pattern for Both

Both special-purpose agents share this lifecycle:

```
1. Trigger detected (compression needed / approval required)
2. For compression: acquire per-agent lock; for security: halt requesting agent
3. Load agent via agent_factory if not already loaded
4. Build prompt with context-specific data
5. Register in pool.instances → shows tab in UI
6. Execute via ExecutionEngine.run() or direct agent.run()
7. Parse result (summary text for compression, yes/no verdict for security)
8. Apply decision (insert marker + trim for compression, approve/reject for security)
9. Clean up pool.instances and active_stack
10. Release lock (compression) or resume requesting agent (security)
```

**Thread safety model:** Each agent runs in its own execution thread. Agent conversations are isolated — agent A's thread doesn't access agent B's conversation. Compression uses per-agent threading locks for mutual exclusion on conversation data. The only shared state is the pool's instances dict, protected by per-agent locks during mutations (marker insertion, trim). This means compression of agent A never blocks agent B on a different API endpoint.

---

## 7. Error Handling & Loop Detection

### 7.1 Unified Loop Detection

The `detect_loop()` function is shared across all agents — no separate implementations:

```python
# agent_orchestrator.py (or a new shared module):
def detect_loop(messages: List[Message]) -> Optional[Tuple[str, int]]:
    """
    Detect if the agent is stuck in a repetitive loop.
    
    Works by extracting identifying features from recent messages and
    checking for repeated patterns of length L repeating K times.
    
    Args:
        messages: Full conversation history (or active set)
    
    Returns:
        (reason, pop_count) if loop detected, else None
        pop_count = number of messages to remove from the end to break the loop
    """
    if len(messages) < 6:
        return None
    
    def get_feature(m):
        # Extract role + content signature
        ...
    
    # Check last 40 messages for repeated patterns
    window = messages[-40:]
    features = [get_feature(m) for m in window if m.role != SYSTEM]
    
    # Generic loop detection: pattern of length L repeating K times
    for L in range(1, 21):
        K = 3 if L < 5 else 2
        ...
```

### 7.2 Unified Loop Recovery

Loop recovery is handled at the **generator consumer** level (not inside the generator). The `ExecutionEngine.run()` yields normally; the caller wraps it in a try/except that catches loop detection and triggers rollback + retry:

```python
# In api_server.py or wherever the generator is consumed:

def _run_agent_with_recovery(pool, instance):
    """Run an agent with automatic loop recovery."""
    max_retries = pool.settings.max_auto_rollbacks or 3
    retry_count = 0
    
    while retry_count <= max_retries:
        try:
            # Run the standard generator (LLM call → tool execution → etc.)
            engine = ExecutionEngine(pool)
            for partial in engine.run(instance):
                yield partial
            break  # Completed successfully — no loop
            
        except LoopDetectedError as e:
            retry_count += 1
            if retry_count > max_retries:
                # Hard limit — propagate error up
                raise
            
            logger.warning(f"Loop detected for {instance.instance_name}: {e.reason}. "
                          f"Rolling back and retrying ({retry_count}/{max_retries}).")
            
            # 1. Surgical rollback via pool delegation
            pool.surgical_rollback(
                instance.instance_name, 
                e.pop_count, 
                soft=True, 
                reason=e.reason
            )
            
            # 2. Inject corrective hint
            hint = Message(
                role=USER, 
                content=f"[SYSTEM]: Your last actions resulted in a repetitive loop ({e.reason}). Try a different approach."
            )
            instance.conversation.append(hint)
            
            logger_inst = pool._logger.get_logger(instance.instance_name, instance.agent_class)
            logger_inst.log_message(hint)
            
            yield list(instance.conversation)  # UI sees the rollback hint
    
    # Normal completion — loop recovery not needed
```

**Why recovery is at the consumer level:** The `ExecutionEngine.run()` generator yields LLM responses and tool results. Loop detection runs in phase 4 of the generator (section 3.1) and raises `LoopDetectedError`. This exception propagates out of the generator to the consumer-level wrapper `_run_agent_with_recovery()`, which catches it, rolls back, injects a corrective hint, and restarts the generator. The retry count is bounded by `pool.settings.max_auto_rollbacks` (default 3).

### 7.3 Surgical Rollback in the Unified Pool

```python
class AgentPool:
    def surgical_rollback(self, agent_name: str, pop_count: int, soft: bool = False, reason: str = None):
        """
        Remove the last `pop_count` messages from an agent's conversation.
        
        Safety guarantees:
        1. Never removes SYSTEM message or first USER message
        2. Caps rollback at 50% of removable history per operation
        3. Refines pop_count to avoid leaving dangling tool calls
        """
        inst = self.instances.get(agent_name)
        if not inst:
            return
        
        conv = inst.conversation
        
        # Safety: never remove core messages
        keep_at_least = 0
        if len(conv) > 0 and conv[0].role == SYSTEM:
            keep_at_least = 1
            if len(conv) > 1 and conv[1].role == USER:
                keep_at_least = 2
        
        removable = len(conv) - keep_at_least
        if removable <= 0:
            return
        
        # Safety cap: 50% of removable per operation
        max_pop = max(1, removable // 2)
        if pop_count > max_pop:
            logger.warning(f"Surgical rollback for {agent_name}: capping from {pop_count} to {max_pop}")
            pop_count = max_pop
        
        # Refine: avoid leaving dangling tool calls
        while pop_count < removable:
            start_idx = len(conv) - pop_count
            if start_idx >= keep_at_least and conv[start_idx].role == FUNCTION:
                pop_count += 1
            elif start_idx >= keep_at_least and conv[start_idx].role == ASSISTANT and conv[start_idx].function_call:
                break
            else:
                break
        
        new_len = max(keep_at_least, len(conv) - pop_count)
        del conv[new_len:]
        
        # Sync logger (soft truncate — doesn't touch JSONL file) via LoggerManager
        try:
            self._logger.truncate_to(agent_name, new_len)
        except Exception as e:
            logger.warning(f"Logger truncate failed for {agent_name}: {e}")
```

---

## 8. UI Integration

### 8.1 Unified Tab System

All tabs are now identical representations of `AgentInstance` objects. The frontend receives a flat dictionary of instances with a parent pointer, and renders them accordingly:

```python
# API response structure (simplified):
{
    "instances": {
        "Maine": {
            "instance_name": "Maine",
            "agent_class": "Orchestrator",
            "messages": [...],
            "is_active": True,
            "is_halted": False,
            "parent_instance": null,      // Root instance — no parent
        },
        "Coder1": {
            "instance_name": "Coder1",
            "agent_class": "coder",
            "messages": [...],
            "is_active": False,
            "is_halted": False,
            "parent_instance": "Maine",   // Called by Maine
        },
        "Researcher2": {
            "instance_name": "Researcher2",
            "agent_class": "researcher",
            "messages": [...],
            "is_active": True,
            "is_halted": False,
            "parent_instance": "Coder1",  // Called by Coder1 (nested!)
        }
    },
    "active_stack": ["Maine", "Coder1", "Researcher2"],
}
```

### 8.2 Frontend Rendering

The frontend builds a tree from the flat instance list:

```javascript
// Build agent tree from flat instance list
function buildAgentTree(instances) {
    const nodes = {};
    
    // Create all nodes first
    for (const [name, inst] of Object.entries(instances)) {
        nodes[name] = {
            ...inst,
            children: [],
            depth: 0,
        };
    }
    
    // Link children to parents
    for (const [name, node] of Object.entries(nodes)) {
        if (node.parent_instance && nodes[node.parent_instance]) {
            nodes[node.parent_instance].children.push(name);
        } else {
            // Root-level instance
            rootNodes.push(name);
        }
    }
    
    // Calculate depth via BFS
    function calcDepth(name, depth) {
        nodes[name].depth = depth;
        for (const child of nodes[name].children) {
            calcDepth(child, depth + 1);
        }
    }
    
    rootNodes.forEach(name => calcDepth(name, 0));
    
    return nodes;
}

// Render tabs based on tree structure
function renderTabs(nodes, activeTab) {
    const tabs = [];
    
    function renderNode(name) {
        const node = nodes[name];
        tabs.push({
            name: node.instance_name,
            label: `${node.agent_class}: ${node.instance_name}`,
            isActive: name === activeTab,
            depth: node.depth,
            messageCount: node.messages.length,
        });
        
        for (const child of node.children) {
            renderNode(child);
        }
    }
    
    // Start with root nodes
    Object.entries(nodes).forEach(([name, node]) => {
        if (!node.parent_instance || !nodes[node.parent_instance]) {
            renderNode(name);
        }
    });
    
    return tabs;
}
```

### 8.3 No More Special "Root" Tab vs Sub-Agent Tabs

The frontend treats all instances uniformly:
- Each instance gets a tab (or nested tab if it has children)
- Clicking a tab shows that instance's message history
- Active agents have a pulsing indicator
- Halted agents show a pause icon
- Parallel-waiting agents show a spinner

---

## 9. Migration Path

### 9.1 What Changes in the Frontend

| Current | New | Impact |
|---------|-----|--------|
| `session['history']` for main agent | Read from `pool.instances[main_name].conversation` | API response format changes |
| `sub_agent_state[name]` for sub-agents | Read from `pool.instances[name]` | Same data structure for all agents |
| Separate tab rendering for root vs sub | Unified tree-based rendering | Significant JS rewrite needed |
| `active_stack` only tracks sub-agents | `active_stack` tracks ALL active instances | Minor API change |

### 9.2 What Changes in the Backend

| Current File | Change |
|-------------|--------|
| `api_server.py` | Remove `session['history']`. Replace `run_agent_thread()` with pool-based execution. Simplify `build_state()` to read from pool. |
| `agent_orchestrator.py` | Remove `_stream_sub_agent_call()`. OrchestratorAgent becomes a regular agent class with the unified `run()` loop. No special interception of `call_agent`. |
| `agent_pool.py` | Replace `instance_conversations` + `sub_agent_state` + `instance_classes` + `instance_loggers` + `instance_summaries` with unified `instances: Dict[str, AgentInstance]`. Keep helpers (drain_queue, halt/resume, surgical_rollback). |
| `api_router.py` | No changes needed — still handles LLM routing and concurrency. |
| `agent_logger.py` | Simplified — just JSONL writer, no memory_history, no dual state (~200 lines) |

### 9.3 Migration Strategy

**Clean break approach** — rewrite the core modules, then reconnect. Phased migration with dual reads (reading from both `session['history']` AND `pool.instances`) will create more bugs than it prevents.

**Phase 1: Core infrastructure**
- Write `agent_pool/` module with thin coordinator + managers
- Write `execution/engine.py` with phase-based ExecutionEngine
- Write simplified `agent_logger.py` (no memory_history)
- Implement session reload with marker stacking

**Phase 2: Connect the execution loop**
- Have OrchestratorAgent delegate to ExecutionEngine instead of its own `_run()`
- Remove `_stream_sub_agent_call()` — all agent calls go through `call_agent` tool → pool → ExecutionEngine.run()
- Test compression flow with marker stacking end-to-end

**Phase 3: Eliminate dual state**
- Remove `session['history']` from api_server.py
- All reads go through `pool.instances[name].conversation`
- Update WebSocket protocol to `{instances, active_stack}`

**Phase 4: Frontend unification**
- Unified tab rendering based on tree structure
- Unified message rendering (no separate root/sub functions)
- Unified state handling

### 9.4 Backward Compatibility Concerns

1. **WebSocket protocol**: The response format changes from `{history, sub_agents}` to `{instances, active_stack}`. Frontend must be updated before backend.
2. **Logging**: JSONL files remain compatible — normal operations are append-only. Existing logs work as-is with the marker stacking reload algorithm (finds markers at their positions, takes tail after last).
3. **Compression**: Existing compression markers in logs are handled correctly by the forward-pass reload (finds all markers regardless of position, stacks them, takes tail after last).
4. **Parallel agents**: Thread pool remains the same, but uses the unified ExecutionEngine internally.

### 9.5 Risk Assessment

| Risk | Level | Mitigation |
|------|-------|------------|
| Frontend breaking changes | HIGH | Feature flag + gradual rollout |
| Manager boundary disagreements | MEDIUM | Define interfaces FIRST, then migrate code under each interface |
| Execution engine phase boundaries unclear | MEDIUM | Write integration tests for each phase before extracting |
| Compression desync with marker stacking | MEDIUM | Comprehensive test suite: no compression, single, cumulative, crash mid-compression |
| Loop detection false positives | LOW | Same algorithm, no changes to detection logic |
| Performance regression | MEDIUM | Benchmark against current implementation |
| Data loss during migration | LOW | JSONL files preserve all messages; conversation can be reconstructed from logs via forward-pass reload |

---

## 10. Summary

The design eliminates structural duality by making every agent — including the orchestrator — an equal instance in the pool, executing through the same phase-based loop, with a single source of truth for state. Halt/routing state is inline on the pool; LoggerManager and IdleManager handle file I/O and background threads as separate modules. Compression uses per-agent threading locks instead of halt/resume. See Sections 1-9 for the detailed breakdown.

---

## 11. Data Flow Diagrams

### 11.1 Message Flow (High-Level)

```
[User] → [WebSocket] → pool.send_message() → message_queues
                                        ↓
                           ExecutionEngine.run() loop:
                             ├── _pre_llm_checks()    ← stop/halt, async drain, compression, loop detection
                             ├── _call_llm_with_injection()  ← LLM call
                             ├── _process_response()   ← normalize, auto-continue, tool execution (incl. call_agent)
                             └── _post_turn_checks()  ← final answer, parallel wait, queue drain
```

### 11.2 Compression Flow (with Marker Stacking)

```
Token usage >95% (or agent calls compress_context tool)
    │
    ▼
Acquire inst._compression_lock   ← prevents concurrent conversation mutation
    │
    ▼
compress_context(pool, target_agent_name, fraction=0.5, force=True)
    │
    ├── cut_position from tail → invoke compression agent → generate summary
    ├── INSERT marker at cut_position in JSONL and in-memory conversation
    └── DELETE discarded messages from in-memory working set
    │
    ▼
Release lock   ← agent continues with stacked working set: [SYS][COMP1...][COMP2...][tail]

On reload: Single forward pass through JSONL → find all markers → [SYS] + [markers] + [tail after last marker]
```

### 11.3 Parallel Agent Flow

```
Agent A calls call_agent()
    │
    ▼
pool.submit_parallel() → ThreadPoolExecutor.submit(task_wrapper)
    │                        (runs in background thread)
    ├── Create AgentInstance
    ├── ExecutionEngine.run() through phase-based loop
    ├── On success/failed: pool.send_message() → caller's queue
    └── Release endpoint slot, update active_stack
            │
            ▼
Agent A drains queue next iteration → sees completion message → continues
```

---

## 12. Implementation Checklist

### Module: `agent_pool/` (NEW — replaces agent_pool.py)
- [ ] `agent_pool.py` — Thin coordinator (~200 lines) with inline halt/routing state + delegation to managers
- [ ] `agent_instance.py` — AgentInstance dataclass (~100 lines)
- [ ] `execution_manager.py` — ParallelAgentManager, active_stack, task lifecycle (~150 lines)
- [ ] `logger_manager.py` — Logger creation, session recovery from JSONL (~200 lines)
- [ ] `idle_manager.py` — Idle detection, auto-dismissal, background checker (~150 lines)

  Halt state and message routing are simple attributes on the pool (`_halted_instances: set`, `message_queues: dict`) — no separate files needed.

### Module: `execution/` (NEW — replaces monolithic run())
- [ ] `engine.py` — ExecutionEngine with phase-based methods (~400 lines)
  - Phases: setup, async injection, compression check, loop detection, LLM call, normalization, auto-continue, tool execution, post-turn processing, cleanup
- [ ] `loop_detection.py` — Standalone loop detection module (~150 lines)
- [ ] `compression_checker.py` — Token accounting, compression triggers (~120 lines)

### Module: `api_server_unified.py` (MODIFIED — replaces api_server.py)
- [ ] Remove `session['history']` entirely
- [ ] Replace `run_agent_thread()` with pool-based execution via ExecutionEngine
- [ ] Simplify `build_state()` to read from `pool.instances`
- [ ] Simplify `build_stream_update()` for unified instances dict
- [ ] WebSocket protocol: `{instances, active_stack}` instead of `{history, sub_agents}`

### Module: `agent_orchestrator_unified.py` (MODIFIED — replaces agent_orchestrator.py)
- [ ] Remove `_stream_sub_agent_call()` entirely
- [ ] OrchestratorAgent becomes a regular agent class extending Assistant
- [ ] ExecutionEngine handles all orchestration logic

### Module: `agent_logger_unified.py` (MODIFIED — replaces agent_logger.py)
- [ ] Remove `memory_history` — pool owns the working set, logger just writes to JSONL
- [ ] Change `truncate_to()` to soft-only (doesn't touch JSONL file)
- [ ] Keep append-only behavior for `_append_line()` (normal operations only)
- [ ] Add `log_compression_marker()` — inserts marker at cut position in JSONL

### Module: `server/` (NEW — replaces api_server.py which is 156KB)
- [ ] `server/app.py` — FastAPI app creation, route registration (~200 lines)
- [ ] `server/state.py` — build_state(), broadcast_state() (~120 lines)
- [ ] `server/security.py` — Security advisor invocation, approval flow (~300 lines)
- [ ] Move OperationManager from root into `server/operations.py` (91KB monolith → focused module)

### Module: `agent_cascade/api/` (NEW — for api_router.py)
- [ ] Move `api_router.py` here — EndpointScheduler, APIRouter are agent-type-agnostic and belong in the core package
- [ ] Already well-structured (3 focused classes), just needs relocation

### Module: `agent_cascade/utils/` (EXTEND)
- [ ] Move `telemetry.py` into `agent_cascade/utils/telemetry.py` — already a focused TelemetryCollector class
- [ ] Move `agent_factory.py` logic into pool's factory method or `agent_pool/factory.py`

### Module: Frontend (web_ui/)
- [ ] Unified tab rendering based on tree structure
- [ ] Single message rendering function (no separate root/sub)
- [ ] Handle new API response format `{instances, active_stack}`
- [ ] Build agent tree from flat instances dict
- [ ] Render tabs with proper nesting/indentation

### Testing
- [ ] Unit tests for each ExecutionEngine phase independently
- [ ] Integration tests for compression (single + cumulative) with marker stacking
- [ ] Session reload tests: no markers, one marker, two markers, no tail after marker
- [ ] Compression Agent invocation tests: via call_agent pattern and direct fallback
- [ ] Security Advisor invocation tests: approval flow, timeout rejection, auto-apply
- [ ] Halt/resume during compression: verify only target agent halts, others continue
- [ ] Auto-dismiss on idle timeout: verify agent removed after threshold, not before
- [ ] Agent resurrection via log_file: verify session restored correctly with markers
- [ ] Loop detection and recovery tests
- [ ] Parallel agent execution tests
- [ ] Async message injection tests
- [ ] Frontend rendering tests

---

## 13. Files That Do NOT Change

The following files remain **unchanged** in the rewrite:

| File | Reason |
|------|--------|
| `api_router.py` → moves to `agent_cascade/api/` | LLM routing and concurrency management is agent-type-agnostic. Logic unchanged, just relocated. |
| `agent_cascade/agents/fncall_agent.py` | Base FnCallAgent provides `_call_llm`, `_call_tool` for templates |
| `agent_cascade/agents/assistant.py` | Assistant class with RAG — still the base for all agents |
| `agent_cascade/compression/core.py` | `compress_context()` logic stays, but references to pool need updating (instance_conversations → pool.instances) |
| `agent_cascade/compression/agent_invoker.py` | Compression Agent invocation pattern remains valid, uses call_agent via ExecutionEngine |
| `agent_cascade/tools/*.py` | All tool implementations work unchanged |
| `telemetry.py` → moves to `agent_cascade/utils/` | Telemetry recording is agent-type-agnostic. Logic unchanged, just relocated. |

---

## 14. Estimated Code Reduction

| Component | Current Lines | New Lines | Reduction |
|-----------|--------------|-----------|-----------|
| Execution loop | ~800 (split between _run + _stream_sub_agent_call) | ~400 (phase-based ExecutionEngine) | -50% |
| Pool/state management | ~1100 (god object) | ~1050 (thin coordinator + 2 managers + inline halt/routing) | -5% |
| UI state building | ~300 (build_state + build_stream_update) | ~120 (unified) | -60% |
| Sub-agent setup (_stream_sub_agent_call internals) | ~300 | 0 (merged into _create_and_run_agent helper) | -100% |
| Logger dual-state sync | ~150 (memory_history + update_history) | 0 (pool owns state, logger just writes) | -100% |
| **Total backend** | **~2650** | **~1690** | **-36%** |

The honest number is -36%, not the earlier inflated estimate. The pool itself goes from ~1100 lines to roughly ~700 (thin coordinator) + ~350 (LoggerManager + IdleManager) = ~1050 lines — a modest -5%. The real savings come from eliminating OrchestratorAgent's _stream_sub_agent_call() (~460 lines), merging sub-agent setup into the shared helper, and removing dual-state sync. The new module structure adds ~350 lines of clear manager abstractions but dramatically reduces per-file complexity (from ~1900 lines in orchestrator to ~400 in engine).

---