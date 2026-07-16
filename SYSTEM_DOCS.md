# AgentCascade System Architecture Documentation

## Overview

This document provides a comprehensive understanding of the AgentCascade multi-agent framework architecture. AgentCascade is a sophisticated system that coordinates multiple AI agents working in concert to accomplish complex tasks through iterative collaboration, with built-in mechanisms for loop detection, context compression, tool execution, and secure operation.

---

## 1. Inner Loop Detection System

### Purpose and Role
The inner loop detection system prevents agents from getting stuck in repetitive patterns during generation. It operates at two distinct levels with different triggers, focus areas, and recovery actions:

- **Inner Loop Detector** (`inner_loop_detect.py`): Monitors real-time streaming text for immediate repetition signals during LLM generation
- **Pattern-Based Detector** (`loop_detection.py`): Analyzes conversation history for recurring message sequences before each LLM call

This system is critical for maintaining agent productivity and preventing infinite loops that could waste tokens or cause degradation.

### Two-Layer Detection Comparison

| Aspect | Inner Loop Detector | Pattern-Based Detector |
|--------|---------------------|------------------------|
| File | `inner_loop_detect.py` | `loop_detection.py` |
| Trigger | Per streaming chunk during LLM generation | Per turn (before LLM call) |
| Focus | Text generation repetition (chars, n-grams, entropy) | Conversation pattern loops in message history |
| Window | Sliding token buffer | Last 40 non-system messages |
| Action on detection | Abort stream, advance to next endpoint via api_router | Inline rollback and hint injection |

### Key Components/Files

#### `agent_cascade/inner_loop_detect.py`
- **InnerLoopDetector class**: Real-time streaming detector that processes text chunks as they are generated
- **Detection algorithms**:
  - Character run detection (consecutive identical characters)
  - Sentence repetition tracking
  - N-gram repetition with sliding window
  - Block repetition with larger token windows
  - Shannon entropy collapse detection

#### `agent_cascade/loop_detection.py`
- **detect_loop() function**: Pattern-based detection analyzing recent conversation history
- Extracts features from messages and checks for repeated sequences of length L repeating K times
- Returns (reason, pop_count) to enable automatic rollback

### Data Flow
1. During LLM streaming, `inner_loop_detect.py` receives text chunks via `.feed()` method (see `execution_engine.py:2105-2125`)
2. When a loop is detected, it returns an event dict: `{"loop": True, "reason": ..., "score": ...}`
3. This triggers the execution engine to abort the stream and raise an exception that is caught by `api_router.py` (see `execution_engine.py:2145-2149`)
4. The Pattern-Based Detector runs in `_pre_llm_checks()` at line 1610 (`execution_engine.py:1610-1645`)
5. On detection, the system calls `_inline_rollback_and_hint()` and returns `True` to continue with fresh state

### Configuration Points
Located in `agent_cascade/settings.py`:
```python
@dataclass
class InnerLoopSettings:
    max_counter_entries: int = 200      # Max entries per Counter before pruning
    max_tokens: int = 1000              # Max tokens in sliding window
    default_min_chars: int = 4000       # Min chars to accumulate before full detection
    ngram_size: int = 64                # Token window size for n-gram repetition
    block_size: int = 128               # Token window size for block repetition
    entropy_window: int = 128           # Token window for Shannon entropy calculation
    char_run_limit: int = 70            # Max consecutive identical chars before alert
    score_threshold: int = 250          # Cumulative score to trigger loop detection
    sentence_repetition_threshold: int = 9
    ngram_repetition_threshold: int = 5
    block_repetition_threshold: int = 4
    entropy_threshold: float = 2.0      # Shannon entropy below which a loop is suspected
    score_decay_rate: float = 0.97      # Multiplicative decay per feed cycle
    max_score: int = 500                # Hard cap to prevent unbounded score growth
```

### Important Design Decisions
- **Incremental processing**: Heavy checks (n-gram, block) run every N-th feed call to balance accuracy with CPU efficiency
- **Activation factor**: Detection becomes more sensitive as text accumulates (linear ramp from 0 at 4000 chars to 1.0 beyond that)
- **One-time scoring**: Items are only scored once per threshold crossing to avoid repeated triggers
- **Entropy gate**: Prevents continuous triggering when entropy stays low; resets when entropy recovers
- **Decay mechanism**: Scores gradually decay (0.97x per cycle) so transient repetitions don't accumulate forever

---

## 2. Execution Engine

### Purpose and Role
The `ExecutionEngine` is the central coordinator for all agent turns. It orchestrates the complete lifecycle of an agent's operation, from turn setup through LLM calling to post-turn processing. Every agent (including root and sub-agents) goes through this same engine.

### Key Components/Files
- **File**: `agent_cascade/execution_engine.py` (~4334 lines)
- **Core class**: `ExecutionEngine`
- **Delegated handlers** (initialized in two-phase pattern):
  - `AgentLifecycleManager`: Instance creation, reuse logic, settings propagation
  - `CompressionHandler`: Context compression and token management
  - `ToolDispatcher`: Tool execution and call_agent routing
  - `StreamPublisher`: Streaming output distribution

### Data Flow (Main Loop)
```python
# Simplified flow from run() method
while turns_available > 0:
    # Phase 1: Setup
    messages, llm_messages, response = self._setup_turn(instance)
    
    # Phase 2-4: Main execution cycle
    if self._pre_llm_checks(...):  # Stop conditions, async injection, compression, loop detection
        continue
    
    # LLM call (streaming or non-streaming)
    result = self._execute_llm_call_with_retry(...)
    
    # Process response (tool calls, thinking detection, etc.)
    tools_to_execute = self._check_for_tool_calls_in_output(...)
    
    if tools_to_execute:
        await self._execute_detected_tools(...)
    
    # Post-turn checks and state management
    self._post_turn_checks(instance)
```

### Key Sections of Execution Engine

#### Main Retry Loop and Streaming Flow
- Uses generator pattern (`yield`) to provide streaming updates to UI/clients
- Handles both synchronous and asynchronous LLM calls
- Tracks generation ID to detect superseded executions (e.g., after Stop then Resume)
- Manages concurrency slots per endpoint via `pool._acquire_slot()`

#### Inner-Loop Detection Integration
Called in `_pre_llm_checks()` at `execution_engine.py:1610-1645`:
```python
if not getattr(instance, '_suppress_loop_detection_next_turn', False):
    loop_info = _canonical_detect_loop(messages)
    if loop_info:
        reason, pop_count = loop_info
        logger.debug(
            f"[LOOP_DETECTED] {inst_name}: pattern={reason}, "
            f"pop_count={pop_count}, messages={len(messages)}"
        )
        self._inline_rollback_and_hint(
            instance, inst_name, pop_count, reason,
            messages, llm_messages, response,
        )
        if rollbacks >= 3:
            logger.warning(
                f"Loop recovery for {inst_name}: rolled back "
                f"{rollbacks} times without success. Continuing."
            )
        if (tel := self._telemetry()) is not None:
            try:
                tel.record_loop_detected(
                    inst_name, reason=reason, auto_rolled_back=True, pop_count=pop_count,
                )
            except Exception:
                pass
        return True  # Continue loop with fresh state
```

#### Inner-Loop Streaming Detection Integration
During LLM streaming, `inner_loop_detect.py` is called per chunk in `_execute_llm_call_with_retry()` at `execution_engine.py:2105-2125`:
```python
if getattr(self.pool.settings, 'inner_loop_detect_enabled', False):
    _ev = _inner_detector.feed(_delta_text)
    if _ev:  # Loop detected mid-stream
        _sample_path = save_loop_sample(
            text=_total_text[:4000],
            reason=f"inner_loop ({_ev['reason']}, score={_ev['score']})",
            instance_name=inst_name,
        )
        yield from _abort_stream(
            f"Detected generation loop: {_ev['reason']} (score={_ev['score']})"
        )
        if _sample_path:
            logger.debug(f"  [LOOP_SAMPLE] Saved to {_sample_path}")
        if loop_retry_count >= _loop_max:
            raise Exception(
                f"inner_loop_exhausted: retried {_loop_max} times, "
                f"giving up — last reason: {_ev['reason']}"
            )
        raise Exception(f"inner_loop: {_ev['reason']}")
```

#### Compression Triggers
- **Force compression** at >95% token usage (`_force_compression()`)
- **Warning injection** at >85% token usage (`_inject_compression_warning()`)
- Uses ground-truth token counts from last LLM call when available for accuracy

#### Tool Dispatch Integration
- `_execute_detected_tools()` handles parallel tool calls and async/sync paths
- Delegates to `ToolDispatcher` which routes to specific handlers (call_agent, dismiss_agent, compress_context, standard tools)

### Configuration Points
- **Concurrency limits**: Per-endpoint in `APIEndpoint.concurrency_limit`
- **Turn limits**: `instance.max_turns` or `DEFAULT_MAX_TURNS` (50)
- **Compression thresholds**: 
  - Force: `COMPRESSION_FORCE_THRESHOLD` (95.0%)
  - Warning: `COMPRESSION_WARNING_THRESHOLD` (90.0%)

### Important Design Decisions
- **Two-phase initialization**: Handlers created with pool reference, then engine set after all constructed to break circular dependencies
- **Lock-based state management**: Each instance has `_compression_lock` for thread-safe message appending and cache synchronization
- **Active stack tracking**: `ParallelAgentManager` maintains stack of active instances for proper cleanup
- **Generation counter**: Prevents race conditions when Stop/Resume occurs
- **Incremental rebuild**: After compression, working sets are rebuilt from pool state rather than manual reconstruction

---

## 3. API Router

### Purpose and Role
The `APIRouter` manages multiple LLM endpoints with intelligent routing, fallback mechanisms, and concurrency control. It ensures high availability by switching between endpoints when one fails, while respecting rate limits and capacity constraints.

### Key Components/Files
- **File**: `agent_cascade/api_router.py` (~73 KB)
- **Classes**:
  - `APIEndpoint`: Configuration for a single LLM endpoint
  - `EndpointScheduler`: Per-API-base scheduling with lifecycle-aware serialization
  - `APIRouter`: Main router class that orchestrates endpoint selection and fallback

### Data Flow (call_with_fallback)
```python
def call_with_fallback(agent_type, call_fn, *args, allocated_tokens=None, **kwargs):
    chain = self.get_endpoint_chain(agent_type, allocated_tokens=allocated_tokens)
    
    for cfg_idx, llm_cfg in enumerate(chain):
        # Resolve endpoint-specific settings (max_retries, concurrency_limit, etc.)
        
        # Acquire semaphore for concurrency control
        if concurrency_limit >= 0:
            sem = self._semaphores[endpoint_base][0]
            sem.acquire()
        
        try:
            result = call_fn(llm_cfg, *args, **kwargs)
            # Track last successful endpoint for recovery
            return result
        except Exception as e:
            if "Rate limit exceeded" in err_msg:
                skip to next endpoint
            elif err_msg.startswith("inner_loop:") or err_msg.startswith("max_tokens:"):
                skip to next endpoint  # Bridge with loop detection
            
            if attempt < max_retries:
                exponential backoff
```

### Endpoint Chain Building (Tier 1/2/3)
- **get_endpoint_chain()** builds a prioritized list of endpoints for a given agent type
- Tiers are determined by configuration and runtime availability
- Includes vision-capable endpoints when images are present
- Fallback to default endpoint if no specific endpoint configured

### call_with_fallback() Mechanism
1. Selects endpoint chain based on agent type and token allocation requirements
2. For each endpoint:
   - Acquires semaphore if concurrency limit set
   - Checks rate limits (sliding window)
   - Executes LLM call with up to `max_retries` attempts
   - On success, records last successful config for recovery
3. If all endpoints exhausted, raises RuntimeError with all errors

### Concurrency Control
- **Layer 1** (EndpointScheduler): Serializes entire agent lifecycles per API base
- **Layer 2** (per-endpoint semaphore): Limits parallel API calls within an agent's window
- For `concurrency=0` endpoints: Both layers ensure strict sequential execution

### Rate Limiting
- Tracks call timestamps per endpoint in `_endpoint_call_history`
- Uses sliding window algorithm to enforce RPM limits
- Rate limit errors skip retries and move immediately to next endpoint

### Configuration Points
Per `APIEndpoint`:
```python
id: str = ""                        # UUID auto-generated
name: str = "Unnamed Endpoint"      # Human-friendly label
api_base: str = ""                  # e.g. "http://localhost:1234/v1"
api_key: str = "EMPTY"              # API key (may be "EMPTY" for local)
model: str = ""                     # Model name/ID
model_type: str = "qwenvl_oai"      # "qwenvl_oai", "openai", etc.
enabled: bool = True                # Toggle on/off without deleting
max_retries: int = 2                # Per-endpoint retry count
concurrency_limit: int = -1         # -1=unlimited, 0=sequential, 1+=parallel limit
max_input_tokens: int = 0           # 0=unlimited/auto, 1+=specific limit
base_retry_delay: float = 1.0       # Base delay for retry backoff
max_retry_delay: float = 30.0       # Maximum cap on retry delay
rate_limit_rpm: int = 0             # Rate limit in requests per minute (0=unlimited)

# Sampler parameters (per-endpoint overrides)
temperature: float = 0.0            # 0.0 = use global/default
top_p: float = 0.0                  # 0.0 = use global/default
...
```

### Important Design Decisions
- **Vision detection**: Automatically routes image-containing messages to vision-capable endpoints
- **Image captioning fallback**: When no vision endpoint available, generates alt-text descriptions
- **Error classification**: Distinguishes between retryable errors (connection) and skip-worthy errors (loop/token limit)
- **Exponential backoff with jitter**: Prevents thundering herd problems during recovery
- **Last successful endpoint tracking**: Enables automatic recovery when preferred endpoints become unavailable

---

## 4. Agent Pool & Lifecycle

### Purpose and Role
The `AgentPool` is the central state manager for all agent instances. It maintains instance registries, conversation history, message queues, and coordination logic. The pool acts as a thin coordinator that delegates to specialized managers rather than owning all functionality directly.

### Key Components/Files
- **File**: `agent_cascade/agent_pool.py` (~119 KB)
- **Main class**: `AgentPool`
- **Delegated managers** (created in `__init__`):
  - `ParallelAgentManager`: Parallel execution, active stack tracking
  - `LoggerManager`: Logger lifecycle, recovery, and file I/O
  - `IdleManager`: Idle detection and auto-dismissal

### Agent Instance Management
- **Instance creation**: `create_instance()` registers new instances in `self.instances` dict
- **Instance reuse**: Existing inactive (IDLE/TERMINATED) instances are reused when possible to preserve conversation history
- **Active states**: RUNNING, SLEEPING, COMPLETING, IDLE, TERMINATED
- **Parent-child relationships**: Tracked via `children` dict for cascade termination

### Session Persistence
- Conversation history is maintained per instance in `instance.conversation` list
- Log files provide backup recovery (`LoggerManager.load_history_from_file()`)
- Sessions can be loaded from log files using `load_session_from_log()`
- Compression and rollback operations maintain consistency across all message lists

### Settings Propagation
Settings flow from multiple sources:
1. **Template defaults**: Agent class template provides base configuration (loaded via `agent_factory.load_agent_template()`)
2. **Pool-level settings**: `pool.settings` contains global thresholds
3. **Instance overrides**: Per-instance parameters like `max_turns`
4. **UI configuration**: Real-time tool assignment via `_ui_disabled_tools`

### Agent Types vs Implementation Reality
**Important clarification:** The agent types listed in the UI (orchestrator, coder, researcher, writer, reviewer, compressor, security, generalist) are NOT separate Python classes. They are logical roles represented by **configuration profiles** loaded through `agent_factory.load_agent_template()`. All agents use the same base class (`Assistant`/`FnCallAgent`).

The mapping between user-facing agent types and their implementation is defined in `api_router.py` via `CANONICAL_AGENT_TYPES`:
```python
CANONICAL_AGENT_TYPES: Dict[str, str] = {
    "coder": "Coder",
    "researcher": "Researcher",
    "orchestrator": "Orchestrator",
    "security": "Security",
    "writer": "Writer",
    "reviewer": "Reviewer",
    "compressor": "Compressor",
    "generalist": "Generalist",
}
```

These canonical names are used for:
- Tool configuration (each agent type has a different set of enabled/disabled tools)
- API routing and endpoint selection
- UI display purposes

The actual agent templates are loaded dynamically from the `agent_cascade/agents/` directory, where each agent is configured via its own `soul.md` prompt file and function definitions. See `agent_cascade/agent_factory.py:234` for the `load_agent_template()` function that loads these profiles.

### Key Methods in AgentPool
- `get_template(agent_class)`: Retrieves agent template with case-insensitive fallback
- `create_instance(instance_name, agent_class, parent_instance, max_turns, conversation)`
- `get_instance(instance_name)`: Returns instance or None
- `remove_instance(instance_name)`: Cleans up all state and logs
- `send_message(instance_name, text)`: Adds user message to queue
- `enqueue_message()`, `drain_queue()`: Asynchronous message handling
- `add_async_result()`, `drain_async_results()`: Async tool result buffering
- `_acquire_slot()`, `_release_slot()`: Concurrency control primitives

### Important Design Decisions
- **Thin pool pattern**: Pool coordinates but doesn't own complex logic (delegates to managers)
- **Lock-based concurrency**: Each instance has `_compression_lock` for thread-safe operations
- **Version tracking**: `_instances_version` increments on create/remove/dismiss/reset to signal changes to listeners
- **Cache invalidation**: Token count caches are invalidated after conversation mutations via `_invalidate_token_cache()`
- **Memory leak prevention**: Dismissal callbacks and cache eviction for `api_integration` module-level caches

---

## 5. Compression System

### Purpose and Role
The compression system manages the context window to prevent token overflow and maintain performance. It includes both forced compression (triggered by high token usage) and manual `/compress` commands. The goal is to intelligently reduce conversation history while preserving essential information.

### Key Components/Files
- **File**: `agent_cascade/compression/handler.py` (~50 KB) - Main handler
- **Supporting files**:
  - `core.py`: Compression logic and templates
  - `helpers.py`: Token counting, message truncation utilities
  - `result.py`: Compression result handling
  - `agent_invoker.py`: Agent-based compression execution

### How Compression Works
1. **Detection**: `_check_and_trigger_compression()` in execution engine monitors token usage
2. **Trigger thresholds**:
   - Warning: >85% usage (inline hint to LLM)
   - Force: >95% usage (halts other agents, compresses immediately)
3. **Execution**: `CompressionHandler.execute_force_compression()` delegates to agent-based compression
4. **Recovery validation**: After compression, message pool is validated; if invalid, recovery from log files attempted

### Compression Agent Workflow
- A dedicated "compressor" agent is created with special template and minimal tools
- It receives the conversation and applies compression templates/prompts
- The result replaces the conversation history (typically keeping 30% by default)
- Working sets are rebuilt from compressed state

#### Compressor Agent Isolation
The compressor agent has a **restricted toolset** to prevent it from spawning new agents or performing unintended actions during compression:

```python
# agent_cascade/constants.py:44-50
DEFAULT_COMPRESSOR_DISABLED_TOOLS: frozenset[str] = (
    ALL_USER_APPROVAL_TOOLS | frozenset({
        'call_agent',   # Delegate tasks to specialized agent instances
        'dismiss_agent',  # End sub-agent sessions and clear context
        'list_agents',  # List available agent classes and active instances
    })
)
```

This means the compressor:
- **Cannot call other agents** (`call_agent` disabled) - prevents spawning sub-agents during compression
- **Cannot dismiss agents** (`dismiss_agent` disabled) - maintains pool stability
- **Cannot list agents** (`list_agents` disabled) - reduces information exposure
- **Has all approval tools disabled** - cannot perform any user-approved actions

This isolation ensures the compressor focuses solely on context reduction without side effects. See `agent_cascade/compression/agent_invoker.py:219-255` for how this restricted toolset is applied.

### Force vs Manual Compression
- **Force compression**: Automatic trigger based on token usage percentage, halts other agents, includes cooldown to prevent thrashing
- **Manual `/compress` command**: User-initiated via UI, follows same execution path but without forcing agent halt

### Token Tracking
- Uses ground-truth counts from last LLM API call when available (`instance._last_actual_token_count`)
- Fallback to manual counting on full conversation when ground-truth unavailable
- `_count_history_tokens()` uses conservative estimate (5.0 chars per token) for system prompt overhead
- Image tokens estimated at 255 each, messages at 500 tokens

### Configuration Points
```python
# In settings.py
DEFAULT_COMPRESSION_COOLDOWN_SECONDS: float = 2.0      # Min seconds between forced compressions
COMPRESSION_FORCE_THRESHOLD: float = 95.0              # Force compress at X% token usage
COMPRESSION_WARNING_THRESHOLD: float = 90.0            # Warn at X% token usage
COMPRESSION_TIMEOUT: float = 120.0                      # Max seconds for compression to complete
COMPRESSION_DEFAULT_FRACTION: float = 0.7              # Default fraction of history to discard (70%)
COMPRESSION_MIN_FRACTION: float = 0.1                  # Minimum allowed compression fraction
COMPRESSION_MAX_FRACTION: float = 0.9                  # Maximum allowed compression fraction
```

### Important Design Decisions
- **Cooldown mechanism**: Prevents thrashing by enforcing minimum time between forced compressions
- **Validation and recovery**: After compression, message pool is validated; if invalid, recovers from log files
- **Notification queue**: Compression notifications are queued for injection into tool responses to avoid consecutive USER messages
- **Token accuracy**: Uses ground-truth counts when available, falls back to conservative estimates
- **Compression security check timeout**: 120 seconds max for compression agent execution

---

## 6. Tool System

### Purpose and Role
The tool system provides a flexible framework for agents to execute functions, call other agents, and interact with external systems. It supports both synchronous and asynchronous tool execution, parallel calls, and advanced features like result truncation and spillover handling.

### Key Components/Files
- **File**: `agent_cascade/tool_dispatcher.py` (~34 KB) - Main dispatcher
- **Supporting files**:
  - `tool_utils.py`: Helper functions for truncation and spillover management
  - `agents/`: Agent templates with tool definitions (`function_map`)

### Tool Execution Flow
1. LLM detects a tool call in its output (via function calling or XML protocols)
2. `_execute_detected_tools()` extracts tool name and arguments
3. `ToolDispatcher.execute_tool()` routes to appropriate handler:
   - `call_agent` → `handle_call_agent()`
   - `dismiss_agent` → `handle_dismiss_agent()`
   - `compress_context` → `CompressionHandler.handle_compress_tool()`
   - Other tools → `template._call_tool()`

### Parallel Tool Calls
- The engine can detect multiple tool calls in a single LLM response
- `_execute_detected_tools()` handles them sequentially but can be extended for true parallelism
- Each tool call is executed and result collected before returning to the agent
- Results are formatted as FUNCTION role messages and appended to conversation

### XML-Based Protocols
The system supports multiple output formats:
- **Function calling** (structured JSON)
- **XML tags** (`<function_calls>`, `<tool_call>`) for human-readable protocol
- **Thinking blocks** (`<think>...</think>`) that are stripped before processing
- The `_detect_tool()` function in `execution_engine.py` parses various formats

### Special Tool Handlers

#### call_agent
- Allows agents to delegate tasks to other agent instances
- Supports synchronous (blocking) and asynchronous (non-blocking) paths
- Synchronous: Child agent runs to completion before parent continues
- Async: Parent transitions to SLEEPING state while child runs in background
- Parent-child relationships tracked for cascade termination

#### dismiss_agent
- Removes an agent instance from the pool
- Triggers cleanup of conversation, logs, and caches
- Fires dismissal callbacks for UI updates

#### compress_context
- Manual compression triggered by tool call
- Delegates to `CompressionHandler.handle_compress_tool()`
- Includes validation and recovery on failure

### Configuration Points
- **Tool result truncation**: `DEFAULT_TOOL_RESULT_MAX_CHARS` (10000)
- **Spillover handling**: When results exceed limit, writes to spillover file and returns truncated reference
- **Placeholder resolution**: `_resolve_placeholders()` handles variable substitution in tool arguments

### Important Design Decisions
- **Two-phase initialization**: `ToolDispatcher.set_engine()` called after all handlers constructed
- **Result truncation with spillover**: Large results are split across files, referenced by filename
- **Async/sync path separation**: Clear distinction between blocking and non-blocking agent calls
- **Security checks**: Some tools (like file operations) may require approval via Security Handler
- **Tool registry**: Central registry (`TOOL_REGISTRY`) maps names to callable functions

---

## 7. Cache Pool System

### Purpose and Role
The cache pool system provides a mechanism for caching tool arguments and outputs to avoid redundant computations. This is particularly useful for expensive operations like code execution, web searches, or image processing that might be repeated across turns.

### Key Components/Files
- **File**: `agent_cascade/agent_instance.py` - Contains `AgentInstance` class with cache pool attributes
- **Configuration**: Settings in `settings.py` control cache behavior

### How Cache Pool Works
1. Each agent instance has a rolling buffer (`_cache_pool`) of cached entries
2. Entries are stored when tools are executed with certain characteristics (size, type)
3. The cache can be accessed via placeholder syntax `{USE_CACHED_ENTRY_N}` in prompts or tool arguments
4. Caching is enabled/disabled globally and per-instance

### Cache Pool Configuration
```python
# In settings.py
CACHE_POOL_ENABLED: bool = True               # Toggle cache pool on/off (default: enabled)
CACHE_POOL_SIZE: int = 64                     # Rolling buffer entries per instance
CACHE_THRESHOLD_CHARS: int = 1000             # Min chars for output & granular arg caching
```

### Cache Operations
- **Caching**: When a tool result exceeds threshold, it may be cached in the pool
- **Accessing**: Placeholders like `{USE_CACHED_ENTRY_0}` are resolved to actual cached data
- **Eviction**: Cache is LRU (least recently used) with fixed size limit
- **Invalidation**: After conversation mutations, relevant cache entries may be cleared

### Important Design Decisions
- **Per-instance isolation**: Each agent has its own independent cache pool
- **Size limits**: Prevents unbounded memory growth
- **Threshold-based caching**: Only caches sufficiently large outputs to avoid noise
- **Graceful degradation**: If cache misses, falls back to normal execution
- **Memory management**: Cache manager (`CacheManager` in `api_integration.py`) handles eviction and clearing

---

## 8. Security Handler

### Purpose and Role
The security handler implements a safety layer for potentially dangerous operations. It provides human-in-the-loop approval workflows for actions like file system modifications, network access, or code execution. The system can operate in auto-approve or manual-approval modes.

### Key Components/Files
- **File**: `agent_cascade/security_handler.py` (~30 KB) - Main handler class
- **Supporting files**:
  - `operation_manager.py`: Manages approval requests and timeouts
  - `prompts/dna.py`: Security advisor prompt template

### Security Checks Process
1. When a restricted tool is called, the system creates an approval request
2. Request includes: tool name, arguments, context (workspace info), risk assessment
3. If auto-approval is disabled, the request is sent to the UI via WebSocket
4. A dedicated "Security" agent instance runs with a security advisor prompt
5. The user reviews and responds [YES]/[NO] or provides custom feedback
6. Verdict is parsed and action approved/rejected accordingly

### Approval Workflows
- **Auto-approve mode**: Pre-approved actions execute without human intervention
- **Manual approval required**: All restricted tools require explicit user consent
- **Timeout handling**: Security checks have a 120-second timeout; after warning, auto-reject if no response
- **Context preservation**: The security agent sees the full conversation context up to that point

### Implementation Details
- **Thread-safe tracking**: Uses `active_security_checks` set with lock to prevent duplicate checks
- **WebSocket integration**: Messages sent via `send_queue` from `SecurityAdvisorHandler.run_check()`
- **Agent lifecycle**: Security instance is created, used, and cleaned up per check
- **Verdict parsing**: Multiple fallback strategies to handle different response formats

### Configuration Points
```python
# In operation_manager.py
SECURITY_ADVISOR_TIMEOUT_SECONDS: int = 120   # Max seconds for security advisor check
SECURITY_ADVISOR_WARNING_SECONDS: int = 60    # Warning before timeout
```

### Important Design Decisions
- **Dedicated security agent**: Runs with specialized prompt and limited toolset
- **Non-blocking approval**: Approval process runs in background thread, doesn't block main execution
- **Graceful degradation**: If security service unavailable, falls back to auto-approve (configurable)
- **Request deduplication**: Prevents overlapping checks for same request_id
- **Cleanup on completion**: Security instance removed from pool after verdict received

---

## 9. Telemetry

### Purpose and Role
The telemetry system collects structured events about agent performance, usage patterns, and system health. This data is used for A/B testing configuration changes, debugging issues, and understanding how the framework performs in real-world scenarios.

### Key Components/Files
- **File**: `agent_cascade/telemetry.py` (~30 KB) - Main collector class

### Metrics Collected
The telemetry system tracks:
- **Turn events**: Start/end of each agent turn
- **LLM calls**: Model used, latency, token counts, success/failure
- **Tool calls**: Name, arguments, results, latency, failures
- **Agent delegations**: call_agent invocations and their outcomes
- **Loop detections**: When inner-loop detection triggers
- **Compressions**: Forced and manual compression events
- **Retries**: API retries and their reasons
- **Config fingerprint**: Hash of configuration for A/B comparisons

### Data Structure
Each event is a JSON object with:
```json
{
  "type": "llm_call",
  "session_id": "...",
  "timestamp": "...",
  "instance_name": "...",
  "model": "...",
  "latency_ms": ...,
  "input_tokens": ...,
  "output_tokens": ...
}
```

### Configuration Points
- **Log directory**: `log_dir` parameter to `TelemetryCollector.__init__()` (default: "workspace/telemetry")
- **Event buffer size**: `MAX_EVENTS_IN_MEMORY` (5000) - max events kept in memory before trimming
- **Session ID generation**: Based on current timestamp with microseconds for uniqueness

### How They're Used
1. Events are written to JSONL files per session (one file per session start)
2. In-memory deque provides O(1) rotation when buffer full
3. Aggregates maintained in `_session_stats` and `_config_stats` for quick access
4. Config fingerprint groups runs by configuration for A/B comparison
5. File handle kept open to avoid per-event I/O overhead

### Important Design Decisions
- **Non-blocking writes**: Telemetry operations are wrapped in try/except to prevent failures from affecting main execution
- **Config fingerprinting**: Enables comparing different configurations while isolating variables
- **Session-based grouping**: All events in a session share the same `session_id`
- **Write failure tracking**: Counts write failures for diagnostics without stopping operation
- **Memory efficiency**: Uses deque with maxlen for O(1) trimming and bounded memory usage

---

## 10. WebSocket/API Layer

### Purpose and Role
The WebSocket/API layer provides real-time communication between the AgentCascade backend and frontend clients (WebUI, custom clients). It supports bidirectional streaming of agent output, command handling, state synchronization, and interactive features like message editing and security approvals.

### Key Components/Files
- **File**: `agent_cascade/api_server.py` (~62 KB) - Main server implementation
- **File**: `agent_cascade/ws_handlers.py` (~45 KB) - WebSocket message handlers
- **File**: `agent_cascade/api_integration.py` (~80 KB) - Integration layer between API and execution engine

### Communication Protocol (WebSocket)
**Client → Server messages:**
```json
{"type": "message", "text": "...", "agent_index": 0, "session_name": "..."}
{"type": "stop"}
{"type": "retry"}
{"type": "reset"}
{"type": "approve", "request_id": "..."}
{"type": "reject", "request_id": "...", "reason": "..."}
{"type": "edit_message", "index": N, "content": "new text"}
{"type": "delete_messages", "indices": [N, M, ...]}
{"type": "select_agent", "index": N}
{"type": "set_session_name", "name": "..."}
{"type": "inject", "text": "..."}
```

**Server → Client messages:**
```json
{"type": "state",  ...full state snapshot...}
{"type": "done",   ...final state snapshot...}
{"type": "error",  "message": "..."}
{"type": "approvals", "approvals": [...]}
```

### Message Types and Handlers
`ws_handlers.py` implements handlers for each message type:
- `handle_message()`: Processes new user messages
- `handle_continue()`, `handle_stop()`, `handle_retry()`: Control flow
- `handle_pause()`, `handle_resume()`: Pause/resume all agents
- `handle_terminate()`: Remove specific agent instance
- `handle_edit_message()`, `handle_delete_messages()`: Conversation manipulation
- `handle_approve()`, `handle_reject()`: Security approval responses
- `handle_ask_security()`: Trigger security check for an action
- `handle_update_config()`, `handle_update_endpoints()`: Configuration changes
- `handle_load_session()`, `handle_inject()`: Session management

### State Synchronization
The system maintains a "state snapshot" that represents the current view of all agents and conversations:
1. **Build state**: `build_state_from_pool()` in `api_integration.py` constructs the state dict
2. **Broadcast**: WebSocket clients receive periodic updates with type "state"
3. **Incremental updates**: During streaming, `_put_stream_update()` sends partial updates
4. **Full snapshot on changes**: After significant events (turn completion, compression), full state is sent

### State Structure
The state snapshot is built by `build_state_from_pool()` in `api_integration.py:708-823` and contains the following structure:

```json
{
  "instances": {
    "Maine": {
      "name": "Maine",
      "agent_class": "Orchestrator",
      "state": "RUNNING",
      "turns_remaining": 47,
      "max_turns": 50,
      "model": "qwen-plus-latest",
      "messages": [
        {"role": "user", "content": "Hello", "timestamp": "..."},
        {"role": "assistant", "content": "Hi there!", "timestamp": "..."}
      ],
      "token_stats": {
        "history_tokens": 1250,
        "response_tokens": 45,
        "total_tokens": 1295
      },
      "max_tokens": 8000,
      "summary": "",
      "is_streaming": true
    }
  },
  "responses": [
    {"role": "user", "content": "Hello", "timestamp": "..."},
    {"role": "assistant", "content": "Hi there!", "timestamp": "..."}
  ],
  "agents": [
    {
      "name": "Orchestrator",
      "tagline": "Coordinates multi-agent workflows",
      "tools": ["call_agent", "dismiss_agent", "list_agents"],
      "icon": "🎯"
    },
    {
      "name": "Coder",
      "tagline": "Writes and debugs code",
      "tools": ["execute_python", "read_file", "edit_file"],
      "icon": "💻"
    }
  ],
  "telemetry": {
    "total_turns": 12,
    "avg_turn_duration_ms": 2340,
    "token_usage": {
      "input": 1500,
      "output": 850
    },
    "loop_detections": 0,
    "compressions": 1
  },
  "approvals": [
    {
      "request_id": "req_abc123",
      "agent_name": "Coder",
      "tool_name": "execute_python",
      "tool_args": {"code": "import os"},
      "description": "Execute Python code in sandboxed environment",
      "timestamp": "2024-07-16T10:30:45Z"
    }
  ],
  "generating": true,
  "session_name": "Maine",
  "instance_name": "Maine",
  "total_tokens": 1295,
  "total_words": 324,
  "max_tokens": 8000,
  "summary": "",
  "has_queued_messages": false,
  "queued_messages": [],
  "stopped": false,
  "paused": false,
  "current_model": "qwen-plus-latest",
  "default_workspace": "/workspace",
  "is_waiting": false,
  "api_router": {
    "endpoints": [
      {"id": "ep_1", "name": "OpenAI", "model": "gpt-4", "enabled": true}
    ],
    "agent_priorities": {"orchestrator": ["ep_1"]}
  },
  "pool_settings": {
    "inner_loop_detect_enabled": true,
    "loop_min_chars": 4000,
    "loop_score_threshold": 300,
    "cache_pool_enabled": true,
    "cache_pool_size": 64
  }
}
```

### Important Design Decisions
- **WebSocket-first**: Real-time bidirectional communication preferred over polling
- **JSON protocol**: All messages are JSON for easy parsing across languages
- **State-driven UI**: Frontend rebuilds view from state snapshot, ensuring consistency
- **Streaming support**: LLM output streamed incrementally to reduce latency perception
- **Thread safety**: WebSocket handling runs in async context, agent execution in threads; synchronization via locks and queues
- **Graceful degradation**: If WebSocket disconnected, messages queued for later delivery (when possible)

---

## System Integration Points

### How Systems Interconnect

1. **Execution Engine as Central Coordinator**
   - Orchestrates all other systems during each turn
   - Calls `lifecycle_manager` for instance management
   - Invokes `compression_handler` for token management
   - Uses `tool_dispatcher` for tool execution
   - Integrates with `api_router` for LLM calls

2. **Data Flow Between Systems**
   ```
   API Layer → Execution Engine → AgentPool → Compressed State
       ↓            ↓              ↓           ↓
   WebSocket    Lifecycle        Tool         Telemetry
   Messages     Management       Dispatcher   Collection
   ```

3. **Error Handling Chain**
   - Inner loop detection → Rollback → Hint injection
   - API failure → Fallback to next endpoint
   - Compression failure → Recovery from log files
   - Security rejection → Action blocked, alternative suggested

4. **Configuration Propagation**
   - Global settings in `settings.py`
   - Per-endpoint overrides in configuration files
   - Runtime adjustments via UI (disabled tools, priorities)
   - Instance-specific parameters during creation

### Cross-Cutting Concerns

- **Thread Safety**: All shared state protected by locks; each instance has its own lock for fine-grained control
- **Memory Management**: Bounded data structures (deques with maxlen), cache eviction, periodic pruning
- **Error Resilience**: Retry logic with exponential backoff, fallback mechanisms, recovery procedures
- **Observability**: Comprehensive logging and telemetry tracking at all levels

---

## Conclusion

This architecture represents a mature, production-ready multi-agent framework with sophisticated safeguards against common failure modes (loops, overflow, security risks). The modular design allows individual components to be improved without affecting the whole system, while the integration points are well-defined and robust.

Key strengths:
- **Layered abstraction**: Clear separation of concerns between execution, routing, compression, etc.
- **Failure tolerance**: Multiple fallbacks and recovery mechanisms throughout
- **Observability**: Extensive telemetry and logging for debugging and analysis
- **Flexibility**: Configurable thresholds, multiple endpoints, custom agent templates

The system is designed to scale from simple single-agent tasks to complex multi-agent collaborations while maintaining stability and performance.