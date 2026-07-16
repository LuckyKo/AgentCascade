# Agent Cascade — System Documentation

**Version:** 1.0 (based on DESIGN_REWRITE.md)  
**Last Updated:** 2026-07-16  
**Architecture:** Unified Single-Instance Model

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Principles](#2-architecture-principles)
3. [Core Components](#3-core-components)
4. [Data Flow](#4-data-flow)
5. [Key Mechanisms](#5-key-mechanisms)
6. [Agent Templates & DNA](#6-agent-templates--dna)
7. [WebUI Architecture](#7-webui-architecture)

---

## 1. Overview

### What Is Agent Cascade?

Agent Cascade is a modular multi-agent system designed to delegate complex tasks across specialized AI agents that collaborate in real time. Rather than relying on a single monolithic model to handle every request, Agent Cascade breaks work into manageable pieces and assigns each piece to the most appropriate agent — coder for code generation, researcher for information gathering, orchestrator for coordination — while maintaining full visibility through a live WebSocket connection to the UI.

### The Problem It Solves

Previous versions of multi-agent systems suffered from **structural duality**: the "main" agent (orchestrator) and sub-agents were implemented through entirely separate code paths. Every feature — state management, loop detection, compression, logging, parallel execution — had to be coded twice with subtle differences between the two paths. This led to bugs where fixes applied to one path but not the other, creating inconsistent behavior and making maintenance error-prone.

Agent Cascade eliminates this duality entirely. Every agent — including the orchestrator — is treated as an equal instance within a shared pool, executing through the same loop, reading from the same data structures. There is no special-case handling for any agent type.

### Key Capabilities

| Capability | Description |
|------------|-------------|
| **Multi-Agent Delegation** | Agents call other agents via `call_agent`, which creates new instances in the pool — either synchronously (blocking) or in parallel (non-blocking, with notification on completion) |
| **Memory Persistence** | All agent conversations are written to append-only JSONL log files. Sessions can be restored from disk after a crash or restart using a marker stacking reload algorithm |
| **Context Compression** | When an agent's conversation approaches token limits, the system automatically compresses older messages into summary markers, keeping the working set within budget while preserving context through cumulative summaries |
| **Loop Detection & Recovery** | The system detects when agents enter repetitive behavior patterns and automatically rolls back to a prior state with a corrective hint, retrying up to three times before escalating |
| **Parallel Execution** | Multiple sub-agents can run concurrently in a thread pool, each with its own execution loop, while the caller waits for notifications via message queues |
| **Halt/Resume/Terminate** | Any agent instance can be paused or resumed at any time from the UI. Agents also auto-dismiss after a configurable idle period to keep the pool clean |
| **Security Review** | Sensitive tool calls (shell commands, file writes) trigger a Security Advisor agent that analyzes the request and approves or rejects it before execution proceeds |

---

## 2. Architecture Principles

### The Unified Single-Instance Model

At the heart of Agent Cascade is a simple but powerful principle:

> **Every agent instance — including the "main" orchestrator — is an instance of the same class, managed by the same pool, using the same data structures, executing through the same loop.**

The orchestrator is not a special super-agent. It is simply the first agent created in the pool, with its `instance_name` serving as the session identifier (e.g., "Maine"). When it calls `call_agent`, it invokes other instances of the exact same execution engine — nothing more, nothing less.

### Before vs. After: Eliminating Duality

```
OLD ARCHITECTURE (Dual Paths)          NEW ARCHITECTURE (Unified)
┌──────────────────────────┐           ┌──────────────────────────┐
│ "Main Agent" Path        │           │  Single Execution Loop   │
│ - session['history']     │           │                          │
│ - run_agent_thread()     │    →      │  ExecutionEngine.run()   │
│ - build_state()          │           │  (one path for all)      │
├──────────────────────────┤           ├──────────────────────────┤
│ "Sub-Agent" Path         │           │  AgentPool               │
│ - instance_conversations │           │  - instances dict        │
│ - _stream_sub_agent_call │           │  - single state source   │
│ - get_sub_agent_state()  │           │                          │
└──────────────────────────┘           └──────────────────────────┘
  Feature implemented twice              Feature implemented once
```

### Core Design Decisions

1. **No inheritance hierarchy for agent types.** The OrchestratorAgent extends the same base class (Assistant) as every other agent. There is no "parent" class with extra methods that sub-agents lack.

2. **One loop, one path.** A stateless `ExecutionEngine.run()` method handles all execution — LLM calls, tool execution, compression checks, async message injection, and loop detection — decomposed into focused phases.

3. **Single source of truth for state.** All conversation history lives in `pool.instances[name].conversation`. The API server never holds its own copy of any agent's messages; it always reads from the pool.

4. **`call_agent` is a regular tool.** There is no special interception in the orchestrator. When an agent calls `call_agent`, it goes through the standard tool execution path, which delegates to another agent instance via the pool.

5. **The API server is a state broadcaster, not an execution engine.** It reads state from the pool and broadcasts updates to connected WebSocket clients. All execution happens within the pool's thread pool or the ExecutionEngine.

---

## 3. Core Components

### 3.1 AgentInstance

**What it represents:** A single agent — any agent, regardless of type or role. This is the fundamental unit of computation in the system.

**Key fields:**

| Field | Type | Purpose |
|-------|------|---------|
| `instance_name` | `str` | Unique identifier (e.g., "Maine", "Coder1", "Researcher3") |
| `agent_class` | `str` | Template class name defining capabilities (e.g., "Orchestrator", "coder", "researcher") |
| `conversation` | `List[Message]` | Full cumulative message history for this instance — the single source of truth |
| `state` | `AgentState` | Current lifecycle state: IDLE, RUNNING, SLEEPING, COMPLETING, or TERMINATED |
| `max_turns` | `Optional[int]` | Per-agent turn limit (None = use default of 50) |
| `parent_instance` | `Optional[str]` | The instance that created this one (None for the root/main agent) |
| `created_at` | `float` | Monotonic timestamp when the instance was created |
| `last_activity` | `float` | Monotonic timestamp of the most recent message or action |
| `compression_summary` | `Optional[str]` | Current cumulative summary if compression has occurred |
| `latest_marker_index` | `int` | Index in conversation where the latest compression marker was inserted |

Halt state is **not** stored on the instance itself. It lives in the pool's `_halted_instances` set, ensuring a single source of truth for halt status across all threads.

### 3.2 AgentPool

**What it represents:** The central coordinator that manages all agent instances, their lifecycle, and cross-cutting concerns. It is intentionally lightweight — delegating specialized responsibilities to focused manager modules rather than owning everything directly.

**Responsibilities:**

- **Instance Registry:** Maintains `instances: Dict[str, AgentInstance]` — the authoritative source for all agent state
- **Template Registry:** Holds pre-loaded agent templates (`templates: Dict[str, Assistant]`) that define each agent class's capabilities, system prompts, and tool mappings
- **Message Routing:** Per-agent message queues (`message_queues: Dict[str, List[str]]`) handle async communication between agents
- **Halt State:** Simple set-based tracking (`_halted_instances: set`) for pause/resume functionality
- **Delegation:** Routes specialized concerns to focused manager modules

**Manager Delegation:**

| Manager | Responsibility | Why Delegated |
|---------|---------------|---------------|
| `ParallelAgentManager` | Parallel execution, active stack tracking, task lifecycle management | Complex thread pool coordination with RLock-based state management |
| `LoggerManager` | Logger creation, session recovery from JSONL logs, compression sync | Distinct lifecycle involving file I/O and disk persistence |
| `IdleManager` | Idle detection, auto-dismissal of abandoned agents, background cleanup thread | Background daemon thread with configurable timeout logic |

Halt state and message routing remain as simple attributes on the pool because they are straightforward data structures with minimal logic — wrapping them in a separate manager would add indirection without benefit.

```
┌─────────────────────────────────────────────────┐
│                  AgentPool                       │
│                                                  │
│  owned directly:                                 │
│    instances: Dict[str, AgentInstance]           │
│    templates: Dict[str, Assistant]               │
│    settings: PoolSettings                        │
│    _halted_instances: set                        │
│    message_queues: Dict[str, List[str]]          │
│                                                  │
│  delegated to managers:                          │
│    └─ ParallelAgentManager (thread pool)         │
│    └─ LoggerManager (file I/O)                   │
│    └─ IdleManager (background thread)            │
└─────────────────────────────────────────────────┘
```

### 3.3 ExecutionEngine

**What it represents:** The single, unified execution loop that drives every agent turn. It is stateless — receiving an `AgentInstance` as a parameter and yielding state updates to the caller.

**Phase-Based Design:**

The ExecutionEngine decomposes each agent turn into five focused phases:

```
ExecutionEngine.run() — Phase Flow
═══════════════════════════════════

┌─────────────────────┐
│ Phase 1: Setup      │ ← Prepare system message, check for manual commands
│ _setup_turn()       │   Build LLM-visible message set from conversation
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ Phase 2: Pre-LLM    │ ← Stop/halt checks, async message injection,
│ Checks              │   compression check/force, loop detection
│ _pre_llm_checks()   │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ Phase 3: LLM Call   │ ← Invoke LLM with active function injection
│ With Injection      │   Stream response back to caller
│ _call_llm_with_     │
│ injection()         │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ Phase 4: Response   │ ← Normalize response, handle auto-continue on
│ Processing & Tool   │   truncation, detect and execute tool calls
│ Execution           │
│ _process_response() │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ Phase 5: Post-Turn  │ ← Check for final answer, wait for parallel
│ Checks              │   agents to complete, drain post-generation queue
│ _post_turn_checks() │
└────────┬────────────┘
         │
         ▼
   (loop back to Phase 2 or exit)
```

Each phase is a focused method (~20-60 lines), making them independently testable and easy to reason about. The ExecutionEngine itself does not maintain internal state between calls — all context flows through the `AgentInstance` parameter.

### 3.4 API Server

**What it represents:** A pure state broadcaster. It does not execute agents or manage conversation state. Its sole responsibility is receiving WebSocket connections, broadcasting pool state updates, and relaying user messages into the pool's queue system.

**Key responsibilities:**

- **WebSocket Broadcasting:** Periodically snapshots the pool state and sends it to all connected clients via WebSocket
- **Message Ingestion:** Receives user input through the API router and enqueues it into the appropriate agent's message queue
- **State Serialization:** Converts `AgentInstance` data into JSON-serializable formats for UI rendering, always reading from `pool.instances`
- **Security Review Orchestration:** When a tool call requires approval, triggers the Security Advisor agent and waits for its verdict

**What it does NOT do:**

- ❌ Does not hold any copy of agent conversation history
- ❌ Does not execute agents directly
- ❌ Does not manage agent lifecycle (creation, dismissal, resurrection)
- ❌ Does not perform compression or loop detection

### 3.5 API Router

**What it represents:** A multi-endpoint failover and concurrency management layer that sits between the API server and the LLM provider (e.g., OpenAI, Anthropic). It ensures reliable LLM access through endpoint redundancy.

**Key features:**

- **Multi-Endpoint Support:** Routes requests across multiple API keys/endpoints for reliability
- **Concurrency Management:** Limits how many agents can simultaneously call a given agent class's endpoint, preventing rate limit violations
- **Failover Logic:** Automatically retries failed requests on alternative endpoints
- **Per-Class Quotas:** Different agent classes may have different concurrency limits based on their LLM cost and rate limits

### 3.6 Compression System

**What it represents:** The mechanism that keeps agent conversations within token budget by summarizing older messages while preserving critical context through cumulative markers.

**Two trigger paths:**

| Path | Trigger | Process |
|------|---------|---------|
| **System-Triggered (Forced)** | Token usage exceeds 95% threshold | Runs inline within the agent's execution thread via `_pre_llm_checks()`. Optimized for speed — no agent overhead. |
| **Tool-Triggered (Explicit)** | Agent calls `compress_context` tool | Delegates to a dedicated Compression Agent for quality control. Better prompts and reasoning about what to preserve. |

Both paths ultimately call the same underlying compression logic in `agent_cascade/compression/core.py`, but they differ in their approach: system-triggered is fast and automatic, while tool-triggered produces higher-quality summaries through deliberate agent reasoning.

**Overfeeding Safeguard:** Before sending messages to the compressor, the system performs actual token counting (not rough estimates) to verify that target messages fit within 90% of the compressor's context window. If they don't — TRUE overfeeding is detected and the agent is terminated with a clear diagnostic showing exact token counts. A safety net counter (default 100 compressions, configurable via `QWEN_AGENT_DEFAULT_COMPRESSION_MAX_ATTEMPTS`) guards against infinite loops if token counting fails silently.

---

## 4. Data Flow

### 4.1 End-to-End Request Flow

Here is how a user message flows through the entire system:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        END-TO-END DATA FLOW                         │
│                                                                     │
│  [User Types Message]                                               │
│       │                                                             │
│       ▼                                                             │
│  ┌──────────┐                                                      │
│  │  WebUI   │ Sends message via WebSocket                          │
│  └────┬─────┘                                                      │
│       │                                                             │
│       ▼                                                             │
│  ┌──────────┐                                                      │
│  │ API      │ Receives on WebSocket endpoint                       │
│  │ Server   │ Enqueues into pool.message_queues                    │
│  └────┬─────┘                                                      │
│       │                                                             │
│       ▼                                                             │
│  ┌──────────────────┐                                              │
│  │ ExecutionEngine  │ Picks up queued messages                     │
│  │ run() loop       │ Phase 2: drains queue, runs pre-LLM checks   │
│  └────┬─────────────┘                                              │
│       │                                                             │
│       ▼                                                             │
│  ┌──────────────────┐                                              │
│  │ LLM Provider     │ Receives messages via API Router             │
│  │ (OpenAI, etc.)   │ Returns streamed response                    │
│  └────┬─────────────┘                                              │
│       │                                                             │
│       ▼                                                             │
│  ┌──────────────────┐                                              │
│  │ ExecutionEngine  │ Phase 4: normalizes response                 │
│  │ _process_        │ Detects tool calls, executes tools           │
│  │ response()       │ Logs all messages                            │
│  └────┬─────────────┘                                              │
│       │                                                             │
│       ▼ (loop back to Phase 2 if more turns needed)                 │
│                                                                     │
│  ┌──────────────────┐                                              │
│  │ API Server       │ Broadcasts state updates                     │
│  │ broadcast_state()│ → WebSocket → WebUI                          │
│  └──────────────────┘                                              │
│                                                                     │
│  [User Sees Response in UI]                                         │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.2 Inter-Agent Communication Flow

When an agent calls another agent via `call_agent`:

```
Agent A (caller)                    AgentPool                      Agent B (callee)
═══════════════════════              ══════════                     ══════════════

1. Detect tool call: call_agent    2. Check concurrency limits     6. Create AgentInstance
   (instance_name, agent_class)      via _acquire_slot()                in pool.instances
                                      │                               7. Build system + task
                                      ▼                               messages for Agent B
                          All calls async now                        8. ExecutionEngine.run()
                              │                                     loop: Phases 1-5
                              ▼                                            │
                      ThreadPoolExecutor                                  │
                      .submit(task_wrapper)                               │
                                      │                                    9. On completion:
                                      │                                        pool.send_message() →
                                      │                                    Agent A's queue
                                      │                                         │
                                      │                                    10. Agent A drains
                                      │                                        queue on next iteration
```

### 4.3 State Broadcasting Flow

The API server broadcasts state updates to all connected WebUI clients:

```
Pool State Changes (any agent turn, compression, new instance)
       │
       ▼
┌───────────────────┐
│ build_state()     │ ← Snapshot pool.instances (thread-safe copy)
│ or                │
│ build_stream_     │ ← Lightweight delta for streaming updates
│ update()          │
└───────┬───────────┘
        │
        ▼
┌───────────────────┐
│ serialize_message │ → Convert Message objects to JSON-serializable dicts
│ ()                │   Take only last 100 messages per instance (performance)
└───────┬───────────┘
        │
        ▼
┌───────────────────┐
│ WebSocket         │ → Send JSON snapshot to all connected clients
│ broadcast()       │
└───────┬───────────┘
        │
        ▼
┌───────────────────┐
│ WebUI             │ ← Receives update, renders tabs/messages
│ (React/Vanilla JS)│   Builds agent tree from flat instances dict
└───────────────────┘
```

### 4.4 Session Persistence Flow

```
During Normal Operation:
  ┌──────────────┐     Append-only     ┌──────────────────┐
  │ AgentInstance │ ──────────────────→ │ JSONL Log File   │
  │ (in memory)   │                     │ (on disk, audit) │
  └──────────────┘                      └──────────────────┘

During Compression:
  Marker inserted at cut position in BOTH:
    - JSONL file (at appropriate position, O(n) insert)
    - In-memory conversation (after trimming discarded messages)

During Session Recovery (crash/restart):
  ┌──────────────────┐     Forward pass      ┌──────────────┐
  │ JSONL Log File   │ ─────────────────────→│ AgentInstance│
  │ (on disk)        │   Find markers, take  │ (in memory)  │
  │                  │   tail after last     │              │
  └──────────────────┘                       └──────────────┘

Resurrection via log_file parameter:
  Orchestrator calls call_agent(..., log_file="/path/to/AgentX.jsonl")
    → LoggerManager.load_session_from_log() reconstructs conversation
    → AgentInstance recreated in pool with restored history
```

---

## 5. Key Mechanisms

### 5.1 Smart Truncation

When tool results exceed reasonable length, the system truncates them before passing them to the LLM. This prevents a single tool output from consuming the entire token budget.

**How it works:**

1. After any tool executes (file read, shell command, web search), the result is checked against a configurable maximum length
2. If the result exceeds the limit, only the first N characters are kept with a truncation notice appended (e.g., "[Result truncated — X characters removed]")
3. The truncated result is logged to the JSONL file and added to the agent's conversation
4. The LLM receives context about what was omitted, allowing it to request additional chunks if needed

**Thresholds are configurable** in `PoolSettings`, typically set to keep tool results within 20-30% of the total token budget.

### 5.2 Context Compression

Context compression is how Agent Cascade manages conversations that grow beyond the LLM's context window. Instead of simply dropping old messages, it summarizes them into compact marker entries.

**The Marker Stacking Algorithm:**

```
Step 1: Compression Triggered
  Three trigger paths:
    a) Auto-trigger: token usage exceeds 95% of context window
    b) Agent tool call: agent invokes compress_context() during conversation
    c) Supervisor command: user types /compress in the chat interface to manually trigger compression for any active agent instance

Step 2: Acquire Per-Agent Lock
  inst._compression_lock prevents concurrent conversation mutation

Step 3: Generate Summary
  Compress older messages into a human-readable summary
  Cut position calculated from tail distance (tool chains stay together) and passed over to the logger to insert the marker relative to tail

Step 4: Insert Marker
  A COMPRESSION_MARKER message is inserted at the cut position in agent pool, `tail - cut_offset_from_tail` in JSON log
  In memory: [SYS][U0(first user message)][COMP1: "Summarized {([U0]/PrevMarker)[U1][A1]}"][U2][A2]
  In JSONL:  [SYS][U0][U1][A1][COMP1][U2][A2]  ← marker at original position

Step 5: Trim Working Set
  Discarded messages removed from in-memory conversation
  Only first user message + stacked compressed summaries + recent tail remain visible to LLM

Step 6: Release Lock
  Agent continues with reduced working set
```

**Cumulative Compression Timeline:**

```
Initial state:       [SYS][U0][U1][A1][U2][A2]

After 1st compress:  Memory: [SYS][U0][COMP1: "Summarized U1,A1"][U2][A2]
                       JSONL:  [SYS][U0][U1][A1][COMP1][U2][A2]

More turns happen:   JSONL:  [SYS][U0][U1][A1][COMP1][U2][A2][U3][A3]

Feed to compressor: [COMP1][U2][A2]

After 2nd compress:  Memory: [SYS][U0][COMP1...][COMP2: "Summarized U2,A2"][U3][A3]
                       JSONL:  [SYS][U0][U1][A1][COMP1][U2][A2][COMP2][U3][A3]
                       
Working set feeds to LLM: [SYS][U0][COMP1...][COMP2...][recent messages...]
```

NOTE: Agent memory and JSONL are NOT in full sync. the logs retain the full conversation history at all times. The rule is that the tail end past the last marker MUST be in sync at all times and have the EXACT same number of messages since the last compression marker.
All atomic operations on agent pool should be mirrored in the log AFTER they have changed the active message pool.

NOTE: Per OpenAI API rules, tool call pairs or parallel call chains must not be split by any other message types. Our marker insertion logic skips forwards past any of these pair (if it initial calculation lands in the middle of one) to ensure any of the `[Atc,F]` or `[Atc_1,Atc_2...Atc_n,F_1,F_2...F_n]` sequences are not split by our compression marker. Before the first `Atc` its always a safe insertion position.

**Overfeeding Protection — How Compression Avoids Its Own Token Budget:**

The compressor agent has a finite context window (default 125K tokens). To prevent sending more messages to the compressor than it can handle, the system uses a two-stage protection:

```
Stage 1: Rough Estimate Pre-Filter (core.py, ~line 126)
  - Read compressor's max_input_tokens from its LLM config
  - Reserve 90% for input messages (10% reserved for output generation)
  - Cap discard count using a rough estimate of ~5 chars/token

Stage 2: Actual Token Count Verification (core.py, after target messages assembled)
  - After selecting target messages to send to the compressor:
    a) Count ACTUAL tokens of each target message using qwen tokenizer
    b) Add estimated prompt overhead (~150 tokens for system + compression prompt)
    c) If an existing summary marker exists, count its token content too
    d) Compare total against available_for_messages (90% of compressor's context window)

  TRUE Overfeeding Detected → Compression fails gracefully:
    - Agent is terminated with a clear error message showing exact token counts
    - This means the agent's context is filling faster than compression can reduce it

Safety Net (handler.py, default 100 compressions):
  - A high-threshold counter guards against infinite loops if token counting fails silently
  - Configurable via env var: QWEN_AGENT_DEFAULT_COMPRESSION_MAX_ATTEMPTS (default 100)
```

**On Session Reload (crash recovery):** The system performs a single forward pass through the JSONL file, finds all compression markers, stacks them in order, and takes the tail after the last marker. This produces the same working set that would exist in memory — no backward scanning or complex reconstruction needed.

### 5.3 Loop Detection & Recovery

Agent Cascade uses two complementary loop detection systems: one operates at the turn level (inter-turn), and the other operates in real time during LLM streaming (intra-generation).

#### Inter-Turn Loop Detection (Legacy System)

Agents can get stuck in repetitive behavior patterns — requesting the same tool repeatedly, producing similar responses, or cycling through failed attempts without making progress. The system detects and recovers from these loops automatically.

**Detection:**

- Runs during Phase 2 (pre-LLM checks), every N turns
- Examines the last 40 messages in the agent's conversation
- Extracts identifying features (role + content signature) from each message
- Checks for repeated patterns: shorter patterns need fewer repetitions to flag (3 occurrences of a 1-message pattern), longer patterns are more lenient (2 occurrences of an 8-message pattern)
- Pattern lengths checked range from 1 to 20 messages

**Recovery:**

```
Loop Detected (LoopDetectedError raised in Phase 4)
       │
       ▼
┌─────────────────────┐
│ Consumer catches    │ ← Wrap ExecutionEngine.run() in try/except
│ LoopDetectedError   │   Max retries: pool.settings.max_auto_rollbacks (default 3)
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ Surgical Rollback   │ ← Remove the repetitive messages from conversation
│ pool.surgical_      │   Never removes SYSTEM or first USER message
│ rollback()          │   Caps at 50% of removable history per operation
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ Inject Corrective   │ ← Add a SYSTEM hint:
│ Hint Message        │   "Your last actions resulted in a repetitive loop.
│                     │    Try a different approach."
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ Restart Generator   │ ← ExecutionEngine.run() starts again from Phase 1
└─────────────────────┘
```

**Safety Guarantees:**

- Never removes the SYSTEM message or first USER message (conversation core)
- Caps rollback at 50% of removable history per operation to avoid excessive data loss
- Refines rollback count to avoid leaving dangling tool calls (rolls back an extra message if needed)
- Maximum of 3 automatic retries — beyond that, the error propagates up

#### Intra-Generation Loop Detection (Inner Loop Detector)

While inter-turn detection catches repetitive patterns across completed turns, the inner loop detector operates in real time during LLM streaming, catching degenerate output before the model wastes excessive tokens.

**What It Does:**

Monitors each streaming chunk as it arrives from the LLM and analyzes the text for five distinct signals of repetitive or stalled generation. When a loop is detected, the stream is aborted immediately and the generation is retried.

**Detection Signals:**

| Signal | Method | Score | Trigger Condition |
|--------|--------|-------|-------------------|
| Character Runs | Consecutive identical character count | +100 (instant) | >70 identical chars in a row |
| Sentence Repetition | Normalized sentence counter | +80 | Any sentence appears >=7 times |
| N-gram Repetition | 64-token sliding window counter | +60 | Same n-gram repeats >=5 times |
| Block Repetition | 128-token sliding window counter | +70 | Same block repeats >=4 times |
| Entropy Collapse | Shannon entropy of token distribution | +30 (one-time) | Entropy drops below 2.0 bits, scores only once per low-entropy period; resets when entropy recovers |

Signals accumulate toward a cumulative score threshold (default: 300 points). Score decays by 3% per feed cycle to prevent transient repetitions from triggering false positives. A hard cap of 500 prevents unbounded growth from edge cases.

**How It Works:**

```
LLM Streaming Chunk Arrives
        │
        ▼
┌─────────────────────┐
│ InnerLoopDetector   │ ← Fresh instance per retry attempt
│ .feed(chunk)        │   Activated after min_chars threshold (default: 4000)
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ Check 5 Signals     │ ← Character runs (always), others (after min_chars)
│ + Accumulate Score  │   Heavy checks also respect batch_interval gate
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ Score >= Threshold? │ ← Default: 300 points (cap: 500)
└───────┬─────────────┘
        │ Yes
        ▼
┌─────────────────────┐
│ Abort Stream        │ ← Stop LLM generation, save sample for debugging
│ + Raise Exception   │   inner_loop: or max_tokens: error raised
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ API Router catches  │ ← In api_router.py call_with_fallback()
│ exception           │    Treats as early exit signal, advances to next endpoint
└─────────────────────┘
```

**Configuration:**

All tunable parameters are centralized in the `InnerLoopSettings` dataclass in `settings.py`. This includes memory bounds (max tokens, counter entries), activation thresholds (min chars, batch interval), structural parameters (n-gram size, block size, entropy window), detection thresholds (sentence/n-gram/block repetition counts, entropy limit), and scoring parameters (score threshold, decay rate). The UI provides a toggle to enable or disable detection, which takes effect in real time on the next streaming chunk.

**Integration:**

- A fresh `InnerLoopDetector` is created at the start of each retry attempt
- Each streaming chunk is checked against the toggle setting (gated by `pool.settings.inner_loop_detect_enabled`)
- On detection: the stream is aborted, a sample is saved for debugging, and an exception (`inner_loop:` or `max_tokens:`) is raised
- The API router catches this exception and treats it as an early exit signal, advancing to the next endpoint in the chain rather than retrying the same model
- A max-output-token guard (default: 2048, configurable via max_tokens) serves as a safety net if the detector misses a loop

### 5.4 Parallel Execution

Agents can spawn sub-agents that run concurrently in a background thread pool, allowing the system to perform multiple independent tasks simultaneously. **All `call_agent` invocations use an async-by-default path**, with automatic fallback to synchronous execution when necessary to prevent deadlocks.

**How It Works:**

```
Agent A calls: call_agent(agent_class="researcher", instance_name="Researcher1",
                          task="Find latest info on X")
       │
       ▼
┌───────────────────────────┐
│ ExecutionEngine           │
│ _handle_call_agent()      │
└───────────┬───────────────┘
            │
            ▼
┌───────────────────────────┐
│ Check if caller holds     │ ← Lines 230-240, tool_dispatcher.py
│ a concurrency slot?       │ (detects deadlock risk)
└───────────┬───────────────┘
            │
    ┌───────┴───────┐
    │ YES           │ NO
    ▼               ▼
┌──────────┐    ┌───────────────────┐
│ _run_    │    │ _run_child_async()│
│ child_   │    │ → pool.           │
│ sync()   │    │   register_async  │
│ (direct  │    │   _call()         │
│ execution,│   └────────┬──────────┘
│ no thread│             │
│ pool)    │             ▼
└──────────┘    ┌───────────────────┐
                │ AsyncToolRegistry │
                │ (async_tools.py)  │
                └────────┬──────────┘
                         │
                         ▼
                  ThreadPoolExecutor
                   submits to background
                   thread
                         │
                         ▼
                   New thread runs:
                   ExecutionEngine.
                   run() loop
                         │
                         ▼
                   On completion:
                   pool.drain_async_results()
                   → [Parallel Agent ...] messages
```

**Key Details:**

- All `call_agent` invocations go through the async path via `register_async_call()` → `AsyncToolRegistry` → `ThreadPoolExecutor` by default
- **Automatic sync fallback**: If the caller already holds a concurrency slot, `_run_child_sync()` is invoked directly (no thread pool) to prevent deadlock — this occurs when the same thread would otherwise hold the semaphore while trying to spawn a child that also needs it (lines 230-240, tool_dispatcher.py)
- **Concurrency control** is enforced by `EndpointScheduler` per API endpoint:
  - `concurrency_limit = -1`: unlimited (no scheduling)
  - `concurrency_limit = 0`: sequential — agents run one at a time; all seq endpoints share the same slot to avoid cache trashing
  - `concurrency_limit = N>0`: up to N agents can run simultaneously on that endpoint
- Slot acquisition happens via `_acquire_slot()` in `agent_pool.py` → `router.scheduler.acquire()`
- The caller continues its turn and transitions to **SLEEPING** at end-of-turn if pending async calls exist. It wakes up to drain results from `pool.drain_async_results()`, which returns `[Parallel Agent ...]` prefixed messages
- Completion is communicated via the message queue system — the caller drains its queue on the next iteration and sees the result
- If the pool reaches capacity, new requests block until a slot opens (via endpoint slot acquisition)
- The `active_stack` tracks which agents are currently running for UI rendering and concurrency counting

### 5.5 Halt / Resume / Terminate

Any agent instance can be paused or resumed at any time from the WebUI. This is useful when a user wants to intervene, redirect an agent, or wait for approval on a security check.

**Mechanism:**

```
Halt:
  User clicks "Pause" in UI
    │
    ▼
  API Server calls pool.halt_instance(instance_name)
    │
    ▼
  instance_name added to pool._halted_instances set
  
  At next phase boundary, ExecutionEngine checks:
  if self.pool.is_instance_halted(instance.instance_name):
      yield final_response
      continue  (loop pauses at this point)

Resume:
  User clicks "Resume" in UI
    │
    ▼
  API Server calls pool.resume_instance(instance_name)
    │
    ▼
  instance_name removed from pool._halted_instances set
  
  ExecutionEngine continues from where it paused
```

**When Halt Is Used Automatically:**

| Scenario | What Gets Halted | Why |
|----------|-----------------|-----|
| Compression | Target agent only | Prevents concurrent conversation mutation |
| Security Review | Requesting agent only | Waits for Security Advisor verdict |
| System Shutdown | All instances | Emergency stop via `_stopped_event` |

**Global Terminate:** The pool has a `_stopped_event` that, when set, halts all active agents. This is used for emergency shutdowns or session termination.

### 5.6 Auto-Dismiss and Resurrection

Idle agents are automatically removed from the pool to prevent resource accumulation. However, their full history persists on disk, enabling resurrection.

**Auto-Dismiss Conditions (ALL must be true):**

1. Agent is NOT currently executing (not in `active_stack`)
2. Last activity exceeds `idle_timeout_seconds` (default: 300 seconds / 5 minutes)
3. Agent is NOT the main orchestrator ("Maine")
4. Agent is NOT currently halted (halted agents are intentionally paused)

**What Happens on Auto-Dismiss:**

- Instance removed from `pool.instances` — in-memory working set cleared
- Pending operation backups cleaned up
- UI tab closes in real-time via WebSocket broadcast
- **JSONL log file is preserved** on disk as an audit trail

**Resurrection:**

When the orchestrator calls `call_agent` with a `log_file` parameter pointing to a previously dismissed agent's JSONL log:

1. `LoggerManager.load_session_from_log()` reads the file
2. Applies marker stacking reload algorithm (finds all markers, builds working set)
3. New `AgentInstance` created in pool with restored conversation
4. New task message appended and execution begins

This means auto-dismissal is **non-destructive**. Only the in-memory state is cleared; the full history remains available for restoration.

---

## 6. Agent Templates & DNA

### What Are Agent Templates?

An agent template is a pre-configured `Assistant` instance that defines everything needed to create a new agent of that type: its system prompt, available tools, maximum turns, and behavioral constraints. Templates are loaded once at pool initialization and reused for every instance of that agent class.

### Template Loading Process

```
Pool Initialization
       │
       ▼
┌──────────────────────┐
│ _discover_agents()   │ ← Scan agents_dir for Python files
└────────┬─────────────┘
         │
         ▼
┌──────────────────────┐
│ agent_factory.py     │ ← Load each agent class from its module
└────────┬─────────────┘
         │
         ▼
┌──────────────────────┐
│ Create template      │ ← Instantiate with LLM config, workspace paths
│ pool.templates[name] │   Register in template registry
└──────────────────────┘
```

### Defining an Agent Class

Each agent type is implemented as a Python class that extends `Assistant` (which itself extends the base `FnCallAgent`). The class defines:

- **System Prompt:** Instructions, behavioral guidelines, and contextual information
- **Tool Map:** Available tools and their argument schemas
- **Function Handlers:** Methods that execute each tool call

### Built-In Agent Types

| Agent Class | Role | Key Tools | Notes |
|-------------|------|-----------|-------|
| `Orchestrator` | Session coordinator, task delegation | `call_agent`, `dismiss_agent`, `compress_context` | The root agent; manages overall workflow and delegates to specialists |
| `coder` | Code generation and modification | File read/write, shell commands, code analysis | Handles implementation tasks assigned by the orchestrator |
| `researcher` | Information gathering and analysis | Web search, document parsing, URL extraction | Gathers external data the orchestrator needs for decision-making |
| `SecurityAdvisor` | Tool call approval/rejection | Security analysis prompt | Special-purpose agent invoked automatically for sensitive operations |

### Template Composition

```
┌─────────────────────────────┐
│       FnCallAgent (base)    │  ← _call_llm, _call_tool, tool detection
└──────────────┬──────────────┘
               │ extends
               ▼
┌─────────────────────────────┐
│      Assistant              │  ← + RAG capabilities, knowledge prep
└──────────────┬──────────────┘
               │ extends
               ▼
┌─────────────────────────────┐
│      Orchestrator           │  ← System prompt + call_agent/dismiss_agent
└─────────────────────────────┘

┌─────────────────────────────┐
│      Orchestrator           │  ← Same base, different tools/config
│        (as "coder")         │  ← File tools, shell commands
└─────────────────────────────┘
```

Every agent class follows the same pattern: extend `Assistant`, define a system prompt and tool map, register in the factory. There is no special inheritance for the orchestrator — it uses the exact same base classes and execution engine as every other agent.

### Building Messages from Templates

When a new agent instance is created (either as the root session or via `call_agent`), the ExecutionEngine builds its initial conversation from the template:

1. **System Message:** Built by `_build_system_message(template, instance_name, caller)` — includes the template's system prompt plus session metadata
2. **Task Message:** Built by `_build_task_message(args, caller)` — contains the actual work assignment passed via `call_agent` arguments

These two messages form the foundation of every agent's conversation history.

---

## 7. WebUI Architecture

### Overview

The WebUI provides a real-time interface for interacting with Agent Cascade. It connects to the system via WebSocket and receives state updates that it renders as interactive chat tabs.

### Data Model on the Frontend

The backend sends a flat dictionary of instances with parent pointers:

```json
{
  "instances": {
    "Maine": {
      "instance_name": "Maine",
      "agent_class": "Orchestrator",
      "messages": [...],
      "active": true,
      "agent_state": "RUNNING",
      "is_halted": false,
      "parent_instance": null,
      "has_queued_messages": false,
      "is_waiting": false
    },
    "Coder1": {
      "instance_name": "Coder1",
      "agent_class": "coder",
      "messages": [...],
      "active": false,
      "agent_state": "IDLE",
      "is_halted": false,
      "parent_instance": "Maine",
      "has_queued_messages": true,
      "is_waiting": false
    }
  },
  "active_stack": ["Maine"],
  "session_name": "Maine",
  "stopped": false
}
```

### Tab Rendering

The frontend builds a tree from the flat instance list:

```javascript
// Step 1: Create nodes for all instances
nodes = { "Maine": {...}, "Coder1": {...} }

// Step 2: Link children to parents
// Coder1.parent_instance = "Maine" → Maine.children.push("Coder1")

// Step 3: Calculate depth via BFS
// Maine.depth = 0 (root)
// Coder1.depth = 1 (child of root)

// Step 4: Render tabs
// Depth 0 → main chat tab
// Depth 1+ → sub-agent tabs (possibly nested/indented)
```

### UI Indicators

| State | Visual Indicator |
|-------|-----------------|
| `active: true` | Pulsing/glowing tab indicator (agent is RUNNING) |
| `agent_state: "RUNNING"` | Real-time state name displayed in tooltip or status bar |
| `is_halted: true` | Pause icon on tab |
| `has_queued_messages: true` | Message bubble indicator |
| `is_waiting: true` | Spinner or "waiting" badge (blocked on API slot) |
| Instance dismissed | Tab closes automatically via WebSocket event |

### No Structural Duality in the UI

The frontend treats all instances uniformly. There is no special rendering logic for "main" vs. "sub" agents. Each instance gets a tab, and the tab structure is derived entirely from the `parent_instance` field. This eliminates the need for separate code paths for root agent tabs and sub-agent tabs.

### WebSocket Protocol

The API server broadcasts two types of messages:

| Message Type | Use Case | Content |
|-------------|----------|---------|
| `state` (full snapshot) | Initial connection, periodic updates | Full serialized state from `build_state()` |
| `stream_update` (delta) | During active LLM generation | Lightweight response delta + instance status from `build_stream_update()` |

Clients handle both types transparently — full snapshots replace the current view entirely, while stream updates merge deltas into the active response.

---

## Appendix: Quick Reference

### Configuration Defaults (`PoolSettings`)

| Setting | Default | Description |
|---------|---------|-------------|
| `idle_timeout_seconds` | 300 | Auto-dismiss agents after this much inactivity |
| `idle_check_interval` | 60 | Check for idle agents every N seconds |
| `compression_force_threshold` | 95.0 | Force compression at X% token usage |
| `compression_warning_threshold` | 85.0 | Warn agent at X% token usage |
| `compression_timeout` | 120 | Max seconds for compression to complete |
| `security_check_timeout` | 120 | Max seconds for Security Advisor response |
| `max_auto_rollbacks` | 3 | Max loop recovery retries before escalation |

### Agent Instance Lifecycle

```
Created (pool.instances[name] = AgentInstance)
       │
       ▼
Active Execution ──→ Phases 1-5 repeat until completion
       │
       ├── Halt ──→ Paused at phase boundary ──→ Resume ──→ Continue execution
       │
       ├── Idle Timeout ──→ Auto-dismiss (removed from pool, JSONL preserved)
       │
       ├── Resurrection ──→ Restored via call_agent(..., log_file=...)
       │
       └── Termination ──→ Removed from pool, session ends
```

### File Locations

| Component | Primary File(s) |
|-----------|----------------|
| AgentPool + Managers | `agent_pool.py` (includes ParallelAgentManager, LoggerManager, IdleManager) |
| ExecutionEngine | `execution_engine.py` |
| Loop Detection | `loop_detection.py` (standalone module) |
| API Server | `api_server.py` (single file, ~140KB) |
| API Router | `api_router.py` |
| Compression | `compression/core.py`, `compression/agent_invoker.py` |
| Agent Base Classes | `agent_cascade/agent.py`, `agent_cascade/agent_instance.py`, `agent_cascade/agents/fncall_agent.py`, `agent_cascade/agents/assistant.py` |
| Tools | `tools/*.py` (unchanged) |