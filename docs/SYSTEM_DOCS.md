# Agent Cascade — System Documentation

**Version:** 0.15.2 (keep in sync with `agent_cascade.__version__`)
**Last Updated:** 2026-11-11
**Architecture:** Unified Single-Instance Model

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Principles](#2-architecture-principles)
3. [Core Components](#3-core-components)
4. [Data Flow](#4-data-flow)
5. [Key Mechanisms](#5-key-mechanisms)
6. [Parallel Instance Separation](#6-parallel-instance-separation)
7. [Agent Templates & DNA](#7-agent-templates--dna)
8. [WebUI Architecture](#8-webui-architecture)
9. [Memory-Hint System](#9-memory-hint-system)
10. [Skill System](#10-skill-system)
11. [Tool System & MCP Discovery](#11-tool-system--mcp-discovery)

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
| **Loop Detection & Recovery** | The system detects when agents enter repetitive behavior patterns and automatically rolls back to a prior state with a corrective hint, retrying up to `max_auto_rollbacks` (default 5) before escalating |
| **Parallel Execution** | Multiple sub-agents can run concurrently in a thread pool, each with its own execution loop, while the caller waits for notifications via message queues |
| **Halt/Resume/Terminate** | Any agent instance can be paused or resumed at any time from the UI. Agents also auto-dismiss after a configurable idle period to keep the pool clean |
| **Security Review** | Sensitive tool calls (shell commands, file writes) trigger a Security Advisor agent that analyzes the request and approves or rejects it before execution proceeds |
| **Skill System** | Reusable cross-project procedures (`SKILL.md`) are discovered, auto-loaded into context, evolved via `propose_skill`, and **automatically invalidated/rebalanced** — low-quality or stale skills are auto-retired, so an agent never accumulates dead expertise. See §10 |

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

**What it represents:** The WebSocket state broadcaster **and** the configuration / control REST surface. It does not execute agents or own conversation state. It receives WebSocket connections, broadcasts pool state updates, relays user messages into the pool's queue system, and exposes an HTTP API for reading and mutating server-side configuration and control state.

**Key responsibilities:**

- **WebSocket Broadcasting:** Periodically snapshots the pool state and sends it to all connected clients via WebSocket
- **Message Ingestion:** Receives user input through the API router and enqueues it into the appropriate agent's message queue
- **State Serialization:** Converts `AgentInstance` data into JSON-serializable formats for UI rendering, always reading from `pool.instances`
- **Security Review Orchestration:** When a tool call requires approval, triggers the Security Advisor agent and waits for its verdict
- **Configuration / Control REST Surface:** Serves HTTP endpoints that read and mutate server-side configuration and control state (the UI's settings panels call these). Endpoint families, by name:
  - **Endpoint config:** `/api/endpoints` (GET / PUT / DELETE, bulk update, priorities)
  - **MCP server config:** `/api/mcp-config` (GET / PUT), `/api/mcp-status` (GET), `/api/mcp-toggle` (POST) — see the MCP subsection below
  - **Media-gen config:** `/api/image_gen*`, `/api/media_gen*`
  - **Skills:** `/api/skills*` (including threshold, toggle, migrate)
  - **Sessions:** `/api/sessions`, `/api/session/restore`, `/api/state/save`, `/api/state/load`
  - **Control:** `/api/restart`, `/api/approve`, `/api/reject`, `/api/afk`, `/api/auto_security`
  - **Diagnostics / utility:** `/api/telemetry*`, `/api/find_file`, `/api/parse`

  These endpoints read and write config files and pool flags; they do not run agent loops.

**What it does NOT do:**

- ❌ Does not hold any copy of agent conversation history
- ❌ Does not execute agents directly
- ❌ Does not manage agent lifecycle (creation, dismissal, resurrection)
- ❌ Does not perform compression or loop detection

#### MCP Server Configuration

The API Server also owns the MCP (Model Context Protocol) server configuration — the set of external tool servers the system can connect to. This is a self-contained config/control feature:

**Source of truth:** `config/mcp_servers.json` — a raw servers-map keyed by server name. Each entry holds `command`, `args`, optional `env`, and an `enabled` boolean. A server's `enabled` flag **defaults to true when the key is absent** (the status handler treats a missing flag as enabled); the shipped file sets both of its entries to `false`, so they are present but disabled until toggled on. Servers are initialized at startup from this file — it is not derived from `PoolSettings`.

**REST surface** (in `agent_cascade/api_server.py`; cite by route name, as this file's line numbers shift between edits):

| Route | Method | Behavior |
|-------|--------|----------|
| `/api/mcp-config` | GET | Return the raw `config/mcp_servers.json` contents (`{}` if the file is absent) |
| `/api/mcp-config` | PUT | Validate and write the servers-map back to `config/mcp_servers.json` |
| `/api/mcp-status` | GET | Per-server `[{name, enabled, connected, tool_count}]` — config from the file, live connection state read from the `MCPManager` singleton |
| `/api/mcp-toggle` | POST | Toggle one server on/off: connect/disconnect it live, then persist the `enabled` flag to the file |

`config/mcp_servers.json` is the single source of truth for the `enabled` flag; live connection state is read separately from `MCPManager` under its lock.

**WebUI:** an on-demand settings tab, `sub-mcp-config`, that edits `config/mcp_servers.json`. It is **not** part of the normal sub-agent tab set — it is opened only when the user clicks the "Open MCP Config" button, and is deliberately excluded from sub-agent tab cleanup so it is never auto-closed.

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
| **System-Triggered (Forced)** | Token usage exceeds the force threshold (`compression_force_threshold`, default 96%) | Runs inline within the agent's execution thread via `_pre_llm_checks()`. Optimized for speed — no agent overhead. |
| **Tool-Triggered (Explicit)** | Agent calls `compress_context` tool | Delegates to a dedicated Compression Agent for quality control. Better prompts and reasoning about what to preserve. |

Both paths ultimately call the same underlying compression logic in `agent_cascade/compression/core.py`, but they differ in their approach: system-triggered is fast and automatic, while tool-triggered produces higher-quality summaries through deliberate agent reasoning.

**Overfeeding Safeguard:** Before sending messages to the compressor, the system performs actual token counting (not rough estimates) to verify that target messages fit within 90% of the compressor's context window. If they don't — TRUE overfeeding is detected and the agent is terminated with a clear diagnostic showing exact token counts. A safety net counter (default 100 compressions, configurable via `AGENT_CASCADE_DEFAULT_COMPRESSION_MAX_ATTEMPTS`) guards against infinite loops if token counting fails silently.

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

#### 4.3.1 Settings Snapshot (backend-authoritative)

Every user setting is described once in `agent_cascade/settings_registry.py` (`SettingSpec`:
kind, type, default, persist bucket, handler, broadcast flag, sensitivity). The same table
drives validation of incoming deltas, the `pool_settings` snapshot emitted in `state` /
`stream_update` frames, and `GET /api/settings`.

- **Buckets.** `pool`/`extra` keys persist to `config/pool_settings.json`; `llm` keys
  (`LLM_CFG_KEYS`) persist to `config/api_endpoints.json::default_llm_cfg`; `session` keys
  persist to `config/session_state.json`.
- **Broadcast.** `build_pool_settings_snapshot()` emits every `broadcast=True` registry key,
  plus the LLM bucket: `_add_pool_runtime_settings()` emits `api_base`/`model` (falling back
  to `''`) and every other non-sensitive `LLM_CFG_KEYS` entry present in `default_llm_cfg`.
  `api_key` is `sensitive` and never leaves the server. The key set is derived from the
  registry, so a new LLM key cannot silently fall out of the snapshot.
- **Hydration.** The WebUI's `POOL_SETTINGS_MAP` maps each server key to its DOM control;
  `syncPoolSettings()` applies the snapshot on every frame (skipping only the focused
  control). A key missing from the map stays on its HTML default, which the next save would
  then re-send over the persisted value — add the map entry in the same change as the key.
- **Writes.** The UI sends only changed keys as `update_settings {base_revision, changes}`
  deltas, never a full `update_config` push. `queueSettingChange()` drops changes before the
  first authoritative snapshot has arrived and no-ops values that already match it, so the
  UI cannot stomp persisted settings with un-hydrated defaults.

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
    a) Auto-trigger: token usage exceeds the force threshold (compression_force_threshold, default 96% of context window). A separate proactive path fires at 95% and a warning at 90%; the forced path is the one described here.
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
  - Configurable via env var: AGENT_CASCADE_DEFAULT_COMPRESSION_MAX_ATTEMPTS (default 100)
```

**On Session Reload (crash recovery):** The system performs a single forward pass through the JSONL file, finds all compression markers, stacks them in order, and takes the tail after the last marker. This produces the same working set that would exist in memory — no backward scanning or complex reconstruction needed.

#### 5.2a Hierarchical Consolidation (L2)

When an agent accumulates many compression markers over a long session, the markers themselves become a token burden. L2 consolidation merges older markers into a single higher-level summary, reducing marker count while preserving all raw message history in the JSONL log.

**Trigger:** After every successful `compress_context()` call (L1), the system checks the agent's current marker count. If it meets or exceeds `COMPRESSION_CONSOLIDATION_THRESHOLD` (default **8**, env: `AGENT_CASCADE_COMPRESSION_CONSOLIDATION_THRESHOLD`), consolidation is triggered automatically. Consolidation failure is **non-fatal** — normal compression has already succeeded; markers will be consolidated on the next cycle.

**Strategy:** All markers except the newest are consolidated into one L2 marker. The newest marker is preserved because it represents the most recent summary boundary and may reference context not yet superseded.

```
Before consolidation (8 markers):
  Pool:  [SYS][U0][M0][raw...][M1][raw...][M2][raw...][M3][raw...][M4][raw...][M5][raw...][M6][raw...][M7][tail...]
  JSONL: [SYS][U0][raw...][M0][raw...][M1][raw...][M2][raw...][M3][raw...][M4][raw...][M5][raw...][M6][raw...][M7][tail...]

After consolidation:
  Pool:  [SYS][U0][L2(M0..M6)][raw...][M7][tail...]
  JSONL: [SYS][U0][raw...][L2(M0..M6)][raw...][M7][tail...]
                          ↑ M1..M6 marker lines removed; raw segments between them preserved
```

**Pool mutation (authoritative):**
1. Extract summary text from each of the N-1 markers being consolidated (parsing `<context_summary>` tags). If no valid summaries are extracted, consolidation aborts entirely (no partial writes).
2. Token-size guard: if total summary tokens exceed `COMPRESSION_MAX_CONSOLIDATION_TOKENS` (default **100,000**, env: `AGENT_CASCADE_COMPRESSION_MAX_CONSOLIDATION_TOKENS`), abort to prevent compressor failure.
3. Invoke the Compressor agent with a consolidation-specific prompt (summarize summaries, not raw messages; keep chronological/important events, drop details). Each summary in that input is labelled with the **start timestamp** of the window it covers, so multi-day sessions keep their date anchors in the consolidated narrative.
4. Build new L2 marker via `build_consolidation_marker_message()` — header reads `"L2, <first start> → <last end>, N summaries consolidated"` when ranges are known.
   - **Time range:** the span is positional — the **first** consolidated marker's covered-window start → the **last** consolidated marker's covered-window end. It is read via `get_marker_time_range()`, which prefers the marker's explicit `summary_start_ts`/`summary_end_ts` fields, then the header interval (JSONL-reload fallback), and only falls back to `msg.ts` as a degenerate point for legacy markers. `msg.ts` is the marker's **creation/completion** time (the window's END) and must never stand in for the start; doing so collapses a multi-day span on every consolidation hop.
   - The L2 marker itself stores the resulting `summary_start_ts`/`summary_end_ts`, so a later L2→L2 hop preserves the original start (these fields, like `ts`, are excluded from dumps and never sent to a provider).
5. Under `_compression_lock`: re-read pool state (defensive against concurrent changes), re-verify threshold still met, then rebuild conversation:
   - Replace the **first** consolidated marker's position with the new L2 marker.
   - Remove all **intermediate** markers (positions 1..N-2 of the consolidation set).
   - **Preserve all raw message segments** between markers — only marker messages are removed/replaced.
6. Update via `rebuild_conversation()` (atomic).

**JSONL sync (non-fatal, best-effort):**
After pool mutation succeeds, `_consolidate_markers_in_jsonl()` is called (outside the compression lock):
1. Reads the full JSONL file from disk.
2. Determines which marker in the JSONL corresponds to the newest marker in the **pool state** by matching content. The newest L1 marker may not yet be in the JSONL (it will be appended later by `reset_history`), so this step can yield "not found."
3. Applies `filter_jsonl_for_consolidation()`:
   - If the newest pool marker IS in the file: first marker before it → **replaced** with the new L2 marker; intermediate markers (M1..M6) → **removed**; the matching last marker (M7) → **kept as-is**.
   - If the newest pool marker is NOT yet in the file: ALL markers currently in the file are consolidation candidates — first → replaced with L2, rest → removed. The newest L1 will be appended later by `reset_history`.
   - All non-marker (raw) messages → **always preserved** in both cases.
4. Rewrites the entire JSONL file atomically (single write under `_write_lock`).
5. Updates internal logger tracking to match new pool state.

If JSONL sync fails, a warning is logged: "pool is authoritative; JSONL will be corrected on next compression." The pool remains correct regardless.

**Recursion guard:** A module-level `_consolidating_agents` set (protected by `_consolidation_lock`) prevents re-entry. If `compress_context()` is called for an agent already undergoing consolidation, the consolidation step is skipped. The guard is cleared in a `finally` block.

**L2 markers are still markers:** The L2 marker starts with `COMPRESSION_MARKER` and contains `<context_summary>` tags, so it counts toward the threshold on future consolidations. This means repeated long sessions can produce L2→L3-style consolidation (the header remains "L2" cosmetically; there is no depth tracking yet).

**Invariants:**
- Raw messages are **never deleted** from JSONL by consolidation — only redundant marker lines are removed.
- The tail past the last (newest) marker is **never touched**.
- Pool and JSONL may diverge in message count (pool has fewer due to discarded raw segments from L1; JSONL retains full history), but the tail-past-last-marker invariant holds in both.
- Consolidation reduces marker count from N to 2 (one L2 + one kept newest), far below threshold — no re-trigger until 6 more L1 compressions occur.

### 5.3 Loop Detection & Recovery

Agent Cascade uses two complementary loop detection systems: one operates at the turn level (inter-turn), and the other operates in real time during LLM streaming (intra-generation).

#### Inter-Turn Loop Detection (Two-Tier: Exact + Fuzzy)

Agents can get stuck in repetitive behavior patterns — requesting the same tool repeatedly, producing similar responses, or cycling through failed attempts without making progress. Detection runs **inline** inside `ExecutionEngine._pre_llm_checks()` (in `engine/llm_call.py`); it is no longer raised as an exception to a consumer. Two tiers run in priority order:

> The old single matcher `agent_cascade/loop_detection.py` (window 40 / max period 20) was removed in the 2026-08 two-tier redesign. That module is now a **deprecated backward-compatibility stub**. `LoopDetectedError` still exists there but is **no longer raised by production code** — it is kept only because `api_integration_pkg/runner.py` and existing tests import it.

**Tier 1 — Exact matcher (rollback):** `agent_cascade/exact_loop_detect.py`

- `detect_exact_loop(messages)` scans a window of the last **60** non-SYSTEM messages (minimum 6) for a contiguous, byte-for-byte repeating period of length **1 up to 12**.
- Features are role + content signatures; FUNCTION features are wrapper-stripped so tool calls compare on their canonical content. SYSTEM messages are excluded from the window.
- Guards carried over from the old detector: skip length-1 FUNCTION/USER periods, skip all-FUNCTION periods, contiguity required.
- On a hit it returns `(reason, pop_count)` — the number of trailing messages to remove so that exactly one occurrence of the period remains.
- Gated by `loop_exact_rollback_enabled`. An exact hit returns **before** Tier 2 runs, so it has priority.

**Tier 2 — Fuzzy tool-call detector (warning-first):** `agent_cascade/tool_loop_detect.py`

- `detect_tool_loop(messages)` scans trailing runs of (function_call, function_output) pairs:
  - **Layer 1:** ≥5 trailing same-action pairs with byte-identical or terminal-error outputs (e.g. async-shell polling loops).
  - **Layer 2:** ≥6 trailing same-tool failing pairs with near-duplicate argument cores and an identical failure class.
- This tier is **advisory**: it emits a warning by default. A full rollback happens only when `tool_loop_fuzzy_rollback_enabled` is set (**default off**, `settings.py`).
- Gated by `loop_fuzzy_warning_enabled` AND the legacy kill switch `tool_loop_detection_enabled`.

Both tiers are skipped on the turn immediately after a compression, because compressed state concentrates patterns that false-positive easily.

**Recovery (exact tier):**

```
Exact Loop Detected in _pre_llm_checks()
   │
   ▼
┌───────────────────────────────────┐
│ auto_rollback_on_loop?            │ ← If False: log + continue to the LLM call (no rollback)
└───────────┬───────────────────────┘
            │ True
            ▼
┌───────────────────────────────────┐
│ _inline_rollback_and_hint()       │ ← engine/compression_exec.py
│ pool._rollback_instance(pop_count)│   Pops N messages, clears caches, syncs logger
└───────────┬───────────────────────┘
            │
            ▼
┌───────────────────────────────────┐
│ Inject Corrective Hint            │ ← Random message from LOOP_FEEDBACK_MESSAGES
└───────────┬───────────────────────┘
            │
            ▼
┌───────────────────────────────────┐
│ Enforce max_auto_rollbacks        │ ← pool.settings.max_auto_rollbacks (default 5; -1 = unlimited)
│ rollbacks > limit → terminate     │   Terminates this instance (and its children), not the pool
└───────────────────────────────────┘
```

**Safety Guarantees** (enforced in `pool/rollback.py::_rollback_instance`):

- Never removes the SYSTEM message or the first USER message (conversation core)
- Refines the pop count to avoid leaving dangling tool calls at the cut boundary
- Bounded by `max_auto_rollbacks` (default 5); beyond that the instance is terminated with a diagnostic that names the detected pattern

#### Intra-Generation Loop Detection (Inner Loop Detector)

While inter-turn detection catches repetitive patterns across completed turns, the inner-loop detector operates in real time during LLM streaming, catching degenerate output before the model wastes excessive tokens. A fresh `InnerLoopDetector` is created for each LLM-call attempt; each streaming delta is fed via `InnerLoopDetector.feed(delta)` (`agent_cascade/inner_loop_detect.py`), which returns `None` or a loop-event dict `{loop, reason, score}`.

> The earlier scoring-based design (five signals — sentence / n-gram / block repetition, entropy collapse — accumulating toward a 300-point threshold) was removed in the Phase 3 cleanup. The `InnerLoopSettings` dataclass still exists, but its scoring fields are deprecated and unused.

**Active detection modes** (in priority order, per `InnerLoopDetector.feed`):

| Mode | Mechanism | Trigger |
|------|-----------|---------|
| Max-chars guard | Counts total characters fed | Total chars ≥ `default_max_chars` (default 40960, ~8K tokens) — force-triggers |
| Character run | Consecutive identical characters | A run longer than `char_run_limit` (default 129) — strong signal, returns immediately |
| Two-phase semantic loop | `TwoPhaseLoopDetector` (`two_phase_loop_detect.py`) | An n-gram occurrence count reaching `loop_suspicion_threshold` (default 7) confirmed by `loop_confirm_required` exact matches (default 5); a `loop_cooldown_feeds` (default 50) cooldown follows a failed confirmation |

The two-phase detector only starts evaluating once accumulated output reaches `default_min_chars` (default 4000); the character-run and max-chars guards run from the first chunk.

**How It Works:**

```
LLM Streaming Delta Arrives
   │
   ▼
┌───────────────────────────────────┐
│ inner_loop_detect_enabled?        │ ← Gated by pool.settings.inner_loop_detect_enabled
└───────────┬───────────────────────┘
            │ True
            ▼
┌───────────────────────────────────┐
│ InnerLoopDetector.feed(delta)     │ ← Fresh instance per LLM-call attempt
└───────────┬───────────────────────┘
            │ returns loop event
            ▼
┌───────────────────────────────────┐
│ save_loop_sample(...)             │ ← Debug sample under workspace/logs/loop_samples/
│ _abort_llm_stream(...)            │   Stop generation immediately
└───────────┬───────────────────────┘
            │
            ▼
┌───────────────────────────────────┐
│ raise CharacterRunDetected        │ ← "inner_loop: ..."; once the bounded retry
│   retry budget = retry_max_attempts   │   budget is spent, "inner_loop_exhausted" instead
└───────────────────────────────────┘
```

**Separate safety net:** independently of the detector, a max-output-token guard aborts the stream and raises `MaxTokenExceeded` if generated output exceeds the configured token budget — a backstop if the detector misses a loop.

**Configuration:** all tunables live in the `InnerLoopSettings` dataclass in `settings.py` (character-run limit, max-chars guard, two-phase suspicion / confirm / cooldown thresholds, min-chars activation). The `inner_loop_detect_enabled` toggle gates the whole feature and takes effect on the next streaming chunk.

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
│ Check if caller holds     │ ← tool_dispatcher.py (_run_child_sync / _run_child_async decision)
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

- All `call_agent` invocations go through the async path via `register_async_call()` (in `pool/slots.py`) → `AsyncToolRegistry` → `ThreadPoolExecutor` by default
- **Automatic sync fallback**: If the caller already holds a concurrency slot, `_run_child_sync()` is invoked directly (no thread pool) to prevent deadlock — this occurs when the same thread would otherwise hold the semaphore while trying to spawn a child that also needs it (the sync/async decision in `tool_dispatcher.py`)
- **Concurrency control** is enforced by `EndpointScheduler` per API endpoint:
  - `concurrency_limit = -1`: unlimited (no scheduling)
  - `concurrency_limit = 0`: sequential — agents run one at a time; all seq endpoints share the same slot to avoid cache trashing
  - `concurrency_limit = N>0`: up to N agents can run simultaneously on that endpoint
- Slot acquisition happens via `_acquire_slot()` (in `pool/slots.py`) → `router.scheduler.acquire()`
- The caller continues its turn and transitions to **SLEEPING** at end-of-turn if pending async calls exist. It wakes up to drain results from `pool.drain_async_results()`, which returns `[Parallel Agent ...]` prefixed messages
- Completion is communicated via the message queue system — the caller drains its queue on the next iteration and sees the result
- If the pool reaches capacity, new requests block until a slot opens (via endpoint slot acquisition)
- The `active_stack` tracks which agents are currently running for UI rendering and concurrency counting

### 5.4.1 Async Tool Architecture

Async tools allow Agent Cascade to initiate long-running operations that execute independently of the main agent loop. This architecture ensures that the agent remains responsive while background tasks progress, providing a mechanism for monitoring, controlling, and receiving results from operations that would otherwise block execution.

**Core Operation Pattern: Launch → Track → Notify**
The async lifecycle follows a three-stage pattern designed to decouple operation execution from agent interaction:

1. **Launch**: The agent invokes an async tool (e.g., `shell_cmd`). The tool initializes the background process and immediately returns a unique `tool_id` to the agent. This ID serves as a handle for all subsequent interactions.
2. **Track**: A dedicated tracker thread is spawned to monitor the operation. This thread runs independently of the agent loop, capturing output streams and monitoring process state (running, completed, failed).
3. **Notify**: The tracker thread delivers updates to the agent via async messages injected through the pool's message queue system. These messages can be triggered by timers (heartbeats), specific events (completion/error), or manual requests from the agent.

**Output Delivery Model (No Duplication)**
To prevent redundant data delivery across multiple notification mechanisms, Agent Cascade employs a position-indexed output buffer:

- **Separate Buffers**: Output is captured in separate stdout and stderr line lists within the task state object. When reading for delivery, these are combined into a single ordered list.
- **Position Tracking**: The system maintains a `last_heartbeat_sent_pos` pointer, which is a line index into the combined output list indicating how many lines have been delivered to the agent.
- **Consumptive Reads**: Any mechanism that retrieves output—be it an automatic heartbeat, a manual `__status` call, or a final completion message—reads all lines from the current position onward and advances the pointer to the end of the available buffer.
- **Result**: Each line of output is delivered exactly once, regardless of which notification trigger occurs first.

```text
[ Buffer: ["Line 1", "Line 2", "Line 3"] ]
  ^ (last_heartbeat_sent_pos = 0)

-- Heartbeat Triggered --
  Agent receives: Line 1, Line 2
  Pointer updates: [ Buffer: ["Line 1", "Line 2", "Line 3"] ]
                              ^ (pos = 2)

-- Agent calls __status --
  Agent receives: Line 3
  Pointer updates: [ Buffer: ["Line 1", "Line 2", "Line 3"] ]
                                         ^ (pos = 3)
```

**Agent Interaction Commands**
Agents interact with running async operations using a specialized set of control commands passed as arguments to the tool.

Core Framework Commands:

- `__status`: Performs a manual heartbeat check. Returns the current operation metadata (e.g., PID, elapsed time) and any output accumulated since the last read.
- `__kill`: Terminates the background process immediately.
- `__heartbeat=N`: Adjusts the frequency of automatic status updates (in seconds).
- `__ctrl_c`: Sends an interrupt signal to the process (SIGINT), allowing for graceful termination of interactive tasks.

Tool-Specific Commands:

- `__wait` (shell_cmd only): Briefly blocks the agent's next turn to poll for new output or completion, useful for timing-sensitive operations. This is a convenience command implemented by shell_cmd and not part of the core framework.

**Early Detection Optimization**
To minimize latency for short-lived operations, the system implements early detection:

- **Synchronous Completion**: If an operation completes during the launch phase (before the tracker thread is fully initialized), the result is returned immediately in the launch response, bypassing the async lifecycle entirely.
- **Immediate Output**: Operations that produce output during the launch sequence include that output in the initial response rather than waiting for the first heartbeat.
- **Pointer Advancement**: When early output or completion is detected during launch, the `last_heartbeat_sent_pos` pointer is immediately advanced past those lines. This ensures that subsequent heartbeats do not re-send the initial output.
- **Race Condition Prevention**: Event-based synchronization ensures that if an operation completes while the tracker thread is starting, the completion signal is captured and delivered correctly without duplicate output or missed events.

**Design Principles for Future Async Tools**
When implementing new async capabilities, adhere to these constraints:

- **Unique Identity**: Every async operation must generate a unique `tool_id` scoped to the session.
- **Daemon Threads & Cleanup**: Tracker threads must be daemon threads so they do not block system shutdown. These threads should be joined before the task is removed from the registry to ensure clean resource release.
- **Position-Based Consumption**: All output must be read via a line-index pointer to guarantee exactly-once delivery across heartbeats, status checks, and completion messages.
- **Agent Agency**: The agent must always maintain the ability to query, modify, or terminate any running operation it launched.
- **Resource Cleanup**: Tools must implement mechanisms to clean up background processes and threads upon completion or termination to prevent resource leaks.

**Concrete Example: `shell_cmd`**
The `shell_cmd` tool implements this architecture by launching a subprocess via `subprocess.Popen`, spawning a tracker thread that reads stdout/stderr non-blockingly, updating a shared state object with output and process metadata, and responding to agent commands by querying this shared state and advancing the read pointer. It also provides the `__wait` convenience command for polling behavior.

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

### 5.5.1 Stop Cancellation of In-Flight Shell Commands

A user Stop (`do_stop()` → `pool.stop_session()`) sets `pool.stopped`, but a **sync** `shell_cmd` blocks inside `proc.wait(timeout=...)` and cannot observe phase-boundary checks. Without help it keeps running for its whole timeout and later returns a stale result into the next turn (BUG_0285 — a 300 s pytest run survived Stop and its timeout result was misattributed to a newer command).

`ShellMixin.execute_shell_command` therefore runs a small watcher thread while it waits:

- Every `SHELL_STOP_POLL_INTERVAL` seconds (default 0.5; `AGENT_CASCADE_SHELL_STOP_POLL_INTERVAL`, `0` disables the watcher) it checks two signals via `_sync_shell_superseded()`:
  - `pool.stopped` is `True` (user pressed Stop), or
  - `pool._run_generation` changed since launch (Stop followed by a new turn — `_start_gen_thread` clears `pool.stopped` almost immediately, so the generation bump is the durable signal).
- On cancellation it kills the whole process tree through the same `_terminate_shell_process()` used by the timeout path and the call returns a distinct message: `ERROR: Command cancelled — the user pressed Stop (elapsed Ns)...` (never the `Command timed out after N seconds` wording, so loop detection does not treat a cancellation as a timeout). Partial output captured before the kill is still returned.
- A `SHELL_STOP_POLL_INTERVAL` of `0` restores the old behaviour (run to timeout) for debugging.

**Windows tree kill:** the first `taskkill` pass uses `/F /T` so the `cmd.exe` wrapper's children die with the root. The second pass stays plain `/F`, and WMIC-discovered leaf PIDs are killed individually without `/T` — a leaf can have been reparented, and `/T` there could reach unrelated processes.

**Async shells are unaffected:** they are tracked tasks with `__status` / `__kill` control, so they keep running across a Stop (only `terminate_instance`/`dismiss` calls `kill_all`). This is deliberate: the output stays recoverable.

### 5.6 Auto-Dismiss and Resurrection

Idle agents are automatically removed from the pool to prevent resource accumulation. However, their full history persists on disk, enabling resurrection.

**Auto-Dismiss Conditions (ALL must be true):**

1. Agent is NOT currently executing (not in `active_stack`)
2. Last activity exceeds `idle_timeout_seconds` (default: 1600 seconds / ~27 minutes). System agents (Compressor, Security) use a separate, shorter `system_agent_idle_timeout_seconds` (default 300 seconds / 5 minutes)
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

### 5.7 Approval & Security Flow

Sensitive tool calls (shell commands, file writes, etc.) are gated by an approval step before execution. The flow lives in `agent_cascade/operation_manager/approval.py`. Each pending approval is registered with a unique `request_id`; the user answers with `/yes` or `/no` (or the WebUI approve/reject controls, backed by `/api/approve/{request_id}` and `/api/reject/{request_id}`).

**AFK auto-reject (server-side):**

When the user is away-from-keyboard (AFK), an approval is auto-rejected **once, at registration** — this replaced the browser's renderApprovals loop, so the rejection is deterministic and race-free. It is **skipped while Auto-Ask Security is active**: in that case the pending approval must reach the Security Advisor, which vets it and auto-applies a YES/NO verdict. Only when no automated review will handle the call (security off) does AFK reject immediately, so the call never waits on an absent user.

**Telegram approval notification (fire-and-forget):**

When a real human decision is pending, the system pushes a fire-and-forget notification to the Telegram bridge (the phone can reply `/yes` or `/no`). The push is **suppressed** in two cases:

- The AFK auto-reject above already returned (no decision is pending).
- Auto-Ask Security is active (the Security Advisor auto-applies a verdict — no human decision is pending).

Sub-agent approvals **do** push: a sub-agent's approval is still a real user decision. There is no dedup (approvals are unique events), and the `request_id` is embedded in the message body because the bridge's `instance_name` is log-only. A bridge failure never breaks the approval flow (guarded with a `hasattr` check + `try/except`).

**Auto-Ask Security:** when enabled, the Security Advisor vets each pending approval and auto-applies a verdict, so no human prompt is surfaced and no Telegram push is sent. The `/api/auto_security` control endpoint toggles this.

---

## 6. Parallel Instance Separation

Multiple AgentCascade processes can run on the same machine simultaneously without file collisions. Each instance gets its own isolated paths for logs, pool settings, telemetry data, and agent session files.

### Problem Solved

Without instance separation, running two AC processes (e.g., one for development testing and one for production) causes:
- Shared console log → interleaved output that's unreadable
- Shared pool_settings.json → race conditions when either process saves state
- Shared telemetry directories → metrics from different environments mixed together
- Shared agent logs → impossible to distinguish which instance owns which session

### How It Works

Instance ID is configured via:
- **CLI flag:** `--instance-id <name>` (e.g., `python start_api_server.py --instance-id prod`)
- **Environment variable:** `AGENT_CASCADE_INSTANCE_ID=<name>`

The CLI flag takes precedence over the environment variable. When an instance ID is set, paths are modified as follows:

| Path Type | Default (no ID) | With Instance ID "prod" |
|-----------|-----------------|-------------------------|
| Console log | `logs/console_log.log` | `logs/console_log_prod.log` |
| Pool settings | `config/pool_settings.json` | `config/pool_settings_prod.json` |
| Telemetry dir | `logs/telemetry/` | `logs/telemetry_prod/` |
| Agent logs | `logs/orchestrator_main_*.jsonl` | `logs/orchestrator_main_prod_*.jsonl` |

### Example: Running Two Instances

Terminal 1 (development):
```bash
python start_api_server.py --instance-id dev --port 12345
```

Terminal 2 (production):
```bash
python start_api_server.py --instance-id prod --port 12346
```

Each instance now operates independently:
- Console output goes to separate log files
- Pool settings are loaded/saved from separate JSON files
- Telemetry metrics are isolated per environment
- Agent sessions are distinguishable by their log filenames

### Implementation Details

The logic lives in `agent_cascade/instance_id.py`:
- `get_instance_id()` → returns the configured ID or empty string
- `get_instance_aware_path(base_path, instance_id)` → appends `<id>` before file extension or as subdirectory for telemetry

Components using instance-aware paths:
- `log.py` — console logger and agent log filenames
- `agent_pool.py` — pool_settings.json load/save
- `telemetry.py` — telemetry directory creation
- `start_api_server.py` / `start_multi_agent.py` — CLI argument parsing and initialization

### Backward Compatibility

Leaving the instance ID empty (the default) preserves legacy behavior:
- All paths remain unchanged from pre-separation versions
- Existing log files, pool settings, and telemetry data continue to work without migration
- No breaking changes for single-instance deployments

---

## 7. Agent Templates & DNA

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

## 8. WebUI Architecture

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

## 9. Memory-Hint System

The **memory-hint** feature proactively reminds an agent to re-read relevant project memories (`.agent_lessons/*.md` lessons) — and, optionally, to load relevant skills — when a turn's text strongly matches them. It is a **best-effort, non-critical** subsystem: everything runs off the main execution loop on a background daemon worker, every failure is swallowed, and a hint can never alter the agent's behavior except by surfacing a short advisory note.

> Design source: `plans/memory_hint_PLAN.md` (original feature) and
> `plans/skills_in_memory_hints_PLAN.md` (skill-suggestion extension). This section documents the implemented system as it stands.

### 9.1 What It Does

- **Memory hints:** When a turn's assistant text/reasoning strongly matches one or more lessons the agent has *not* recently read, a hint is queued telling the agent to re-read those specific lesson files (shown as absolute paths).
- **Skill suggestions:** On the same delivery path, relevant skills that are *not* already loaded may be suggested so the agent can pick up expertise it hasn't activated yet.

The hint rides the existing **tool-warning queue** (`_queue_tool_warning` → `_tool_warnings`) and is drained into the next tool result. There is **no USER-message injection**: if no tool result ever follows, the hint is simply dropped. This keeps KV-cache prefixes stable and avoids perturbing the conversation.

### 9.2 Architecture Overview

```
Turn loop (engine/core.py)
   │  _maybe_submit_memory_hint() — Phase-3/4 boundary, any turn with text/reasoning
   │  (tool-only turns & SLEEPING instances skipped; query = first N chars of text+reasoning)
   ▼
MemoryHintManager.submit()          [non-blocking, most-recent-wins per instance]
   │  job dict → queue.Queue  (stale duplicates filtered by generation counter)
   ▼
Daemon worker thread (_run / _process_job)     [background; never blocks main loop]
   │
   ├── Memory sub-pipeline  _match_memories()      TF-IDF cosine + self-calibrating gate
   └── Skill  sub-pipeline  _match_skills_for_hint() keyword-fraction floor (INDEPENDENT)
   ▼
_build_combined() → ONE hint string (memory block + optional skill block)
   ▼
_deliver() → _queue_tool_warning(pool, instance, text)   [drained into next tool result]
```

**Key architectural constraint:** the memory matcher scores on **TF-IDF cosine similarity** while the skill matcher scores on **keyword-fraction (0..1)**. These are different scales and are *never* mixed into the same floor/gap arithmetic. Each sub-pipeline has its own independent gate; they are merged only at the text-building stage.

### 9.3 Module Layout (`agent_cascade/memory_hint/`)

| File | Responsibility |
|------|----------------|
| `manager.py` | `MemoryHintManager` — daemon worker, job queue, vault-index orchestration, both sub-pipelines, hint building & delivery, read-tracking callback. Owns the gate constants. |
| `matcher.py` | `MemoryMatcher` — stdlib TF-IDF + cosine similarity over a merged per-vault lesson index (identity field weighted 3×). |
| `vault.py` | Vault discovery (`.agent_lessons/` under base_dir + extra RO/RW folders), best-effort frontmatter parsing, and `VaultIndex` (per-vault mtime-cached rebuild). |
| `stats.py` | `memory_stats.json` sidecar — per-lesson read counts, atomic (`os.replace`) best-effort diagnostics. |
| `__init__.py` | Public exports (manager, matcher, vault helpers, stats, gate constants). |

**Wiring points outside the package:**

| Location | Role |
|----------|------|
| `pool/core.py` (~L240) | Creates `MemoryHintManager(pool)`; starts the worker + initial `rescan_vaults()` only when `memory_hint_enabled`. Always created so a runtime UI toggle takes effect without restart. |
| `engine/core.py` `_maybe_submit_memory_hint` / `_extract_memory_hint_query` | Per-turn submit hook at the Phase-3/4 boundary; builds the query from assistant text + reasoning only (never tool calls). |
| `tools/custom/file_ops.py` `_track_memory_read` | ReadFile hook: when a lesson under a vault is read, calls `on_memory_read` to record it in `_memories_read` and bump stats. |
| `operation_manager/__init__.py` (~L127) | Triggers a vault rescan on work-folder change (`set_extra_work_folders`). |

### 9.4 The Memory Gate (Self-Calibrating Specificity Check)

The memory gate is **not** a fixed threshold; it is a self-calibrating *specificity* check designed to fire only on a single clear winner and stay silent on diffuse/generic turns:

1. **Signal floor** — `top1 < floor` → skip (no real signal). The `floor` is an EWMA of per-turn top-1 scores, bounded below by `FLOOR_MIN`.
2. **Specificity gap** — `top1 − top2 < GAP` → skip (a diffuse tie / multi-topic query).
3. **Noise gate** — more than `MAX_HINTS_PER_TURN` docs at/above the floor → skip (query too generic).

The EWMA floor is fed on *every* matched job (fire or skip) so it tracks the corpus's own score distribution; it is in-memory only (a fresh process re-seeds and re-calibrates). The `memory_hint_threshold` setting is an **optional override that can only RAISE the floor** (make hints rarer), never lower it — `0` = pure adaptive behavior.

| Constant | Value | Meaning |
|----------|-------|---------|
| `GAP` | 0.03 | Minimum top1−top2 separation for a single clear winner |
| `FLOOR_SEED` | 0.20 | Initial adaptive floor before any turns are seen |
| `FLOOR_MIN` | 0.12 | Hard lower bound on the EWMA floor |
| `EWMA_ALPHA` | 0.05 | Per-turn learning rate for the adaptive floor |
| `MAX_HINTS_PER_TURN` | 4 | More than this many strong matches → treated as noise, skip |

These were **tuned empirically** from a labeled replay of real agent queries through the actual matcher (see `plans/memory_hint_TUNING.md`) — not derived from first principles. Re-tune if the vault or query mix shifts materially.

### 9.5 The Skill Gate (Independent)

The skill sub-pipeline uses a **three-gate adaptive system** (BUG_0041 fix) over `SkillManager.match_skills_with_stats`. It is completely independent of the memory cosine floor — separate EWMA state, separate lock, separate constants. It has *no* specificity-gap requirement, because suggesting 2–3 relevant skills on a diffuse query is legitimate.

**Gate 0 — Absolute junk guard:** `top1 < SKILL_JUNK_MIN` (0.0375) → skip. Non-adaptive; bounds the worst case regardless of EWMA state.

**Gate 1 — Adaptive EWMA floor:** The floor is an EWMA of per-turn *length-corrected* top-1 scores (`top1 × n_in_vocab / max(n_in_vocab, SKILL_REF_TOKENS)`), seeded at `SKILL_FLOOR_SEED` (0.055) and bounded below by `SKILL_FLOOR_MIN` (0.035). The `memory_hint_skill_threshold` setting is a **raise-only override** (clamped to [0, 1.0]); `0` = pure adaptive behavior.

**Gate 2 — Relative noise gate (post-dedup):** After cooldown/loaded dedup, if more than `max(SKILL_HINT_MAX_ENTRIES, int(len(matches) × SKILL_HINT_NOISE_RATIO))` skills survive → skip as generic. Evaluated on the post-dedup list so already-loaded/cooldown skills don't suppress a valid hint.

- **Cap:** `SKILL_HINT_MAX_ENTRIES` = `settings.MAX_AUTO_SKILLS_PER_CALL` (3), applied after dedup/cooldown in score-descending order.
- **Dedup:** skips skills already in the instance's `_loaded_skill_names` (read under `_compression_lock`).
- **Cooldown:** per-instance `_recently_skill_hinted` map, applied with `max(memory_hint_cooldown_seconds, SKILL_HINT_ENTRY_COOLDOWN_SECONDS)` where `SKILL_HINT_ENTRY_COOLDOWN_SECONDS` = 1800 (30 min). The dedicated floor prevents a low memory-re-read cooldown (the BUG_0233 session had 5 s) from turning skill suggestions into per-turn spam; a longer memory cooldown still applies.

> **Note:** `SKILL_HINT_MIN_SCORE` (= 0.15) is retained as a nominal AUTO-mode score for backwards compatibility and as the junk-guard reference (`SKILL_JUNK_MIN = SKILL_HINT_MIN_SCORE × 0.25`). It is **not** the hint gate anymore.

### 9.6 Dedup, Cooldown & Per-Instance State

All state is stored on `AgentInstance`, mutated under `_compression_lock`, and reset together by `_reset_memory_hint_state()` (on L1/L2 compression and session reset):

| Field | Type | Purpose |
|-------|------|---------|
| `_memories_read` | `set` | Vault-relative lesson paths already read this session — never re-hint these. |
| `_recently_hinted` | `dict` | Lesson path → last-hint monotonic ts (memory cooldown guard). |
| `_last_memory_hint_turn` | `int` | Last turn a memory hint fired (diagnostics). |
| `_recently_skill_hinted` | `dict` | Skill name → last-hint monotonic ts (skill cooldown guard). |
| `_last_skill_hint_turn` | `int` | Last turn a skill hint fired (diagnostics). |

Cooldown entries older than the window are pruned on each match, bounding dict growth. Cooldowns are recorded **before** delivery (only for what is actually delivered) so a fast re-match is throttled.

### 9.7 Hint Text Format

Deterministic ordering: memory entries (score-desc) first, then skills (score-desc). Memory-only output is **byte-identical** to the legacy format (regression-guarded by tests). The `[MEMORY HINT]` umbrella tag always leads so log/UI filtering keyed on that tag catches every variant.

```
[MEMORY HINT] Relevant memories you may want to re-read (2):
  - N:\work\WD\AgentWorkspace\.agent_lessons\compression-debug.md
  - N:\work\WD\AgentWorkspace\.agent_lessons\lock-ordering.md
Skills you may want to load (2):
  - docker-best-practices
  - pytest-docker-subprocess-hang
```

Memory paths are clipped to `HINT_ENTRY_MAX_CHARS` (256, sized so absolute Windows vault paths survive); skill names are short and not clipped. A skill-only hint reads `[MEMORY HINT] Skills you may want to load (N):`.

### 9.8 Concurrency & Failure Isolation

- **Non-blocking submit:** `submit()` is a non-blocking queue put of a small dict; the main loop never waits.
- **Most-recent-wins:** a per-instance generation counter means only the newest pending job per instance is processed; stale duplicates in the transport queue are filtered by generation on pop.
- **TTL drop:** jobs older than `JOB_TTL_SECONDS` (30 s) are dropped so an agent is never hinted about a turn it has long since passed.
- **Failure isolation:** each sub-pipeline is independently guarded (`try/except → []`, plus an `isinstance(matches, list)` guard for `MagicMock` test pools). A skill failure can never break the memory path or vice versa; any exception anywhere in the hint path is swallowed (best-effort, logged at debug).
- **Locking:** the memory `_index_lock` and the skill manager's internal lock are **never nested**, so there is no lock-ordering hazard. Cooldown/read-state updates happen under a single `inst._compression_lock` block so memory + skill state are never partially updated.

### 9.9 Vault Discovery & Rescan

A "vault" is a `.agent_lessons/` directory directly under one of the working directories (base_dir ∪ extra RO/RW). Vaults are discovered at startup and rescanned:
- on **work-folder change** (`set_extra_work_folders` callback), and
- as a **safety net** each idle worker tick when `pool._config_version` advances.

Each vault keeps an mtime map so `rescan()` rebuilds only changed files and drops deleted ones; the merged index is keyed by bare vault-relative path (first-vault-wins on cross-vault filename collisions). Display paths are resolved to absolute strings at match time using a root snapshot captured under the same lock, keeping "first vault wins" consistent between scoring and display.

### 9.10 Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `memory_hint_enabled` | `False` | Master on/off switch for the whole feature (off by default). |
| `memory_hint_threshold` | `0.0` | Optional floor override — can only RAISE the adaptive floor (make hints rarer). `0` = pure adaptive behavior. |
| `memory_hint_max_entries` | `3` | Max memory entries listed per hint. |
| `memory_hint_query_chars` | `1000` | How many leading chars of a turn's text/reasoning form the query. |
| `memory_hint_cooldown_seconds` | `600` | Cooldown before the same memory may be re-hinted to an instance. Skill entries additionally enforce `SKILL_HINT_ENTRY_COOLDOWN_SECONDS` (1800 = 30 min) so a low memory cooldown cannot make skill hints repeat per-turn (BUG_0233). |
| `memory_hint_skill_suggestions` | `True` | Sub-toggle under `memory_hint_enabled`: independently enable/disable skill suggestions within hints. (Config-only in v1; no dedicated UI checkbox.) |
| `memory_hint_skill_threshold` | `0.0` | Raise-only override floor for the skill sub-pipeline (BUG_0041). Clamped to [0, 1.0]. `0` = pure adaptive behavior. Deliberately separate from `memory_hint_threshold` (different score measure). |

The gate internals (`GAP`, `FLOOR_*`, `EWMA_ALPHA`, `MAX_HINTS_PER_TURN`, `SKILL_HINT_MIN_SCORE`, `SKILL_HINT_MAX_ENTRIES`) are **module constants**, not user settings — matching the design philosophy of exposing only high-level knobs. Settings are read live each cycle from `pool.llm_cfg` with defensive fallbacks so a bad persisted value can never crash the worker.

### 9.11 Testing

- **Unit:** `tests/memory_hint/test_memory_hint.py` (gate behavior, both sub-pipelines, dedup/cooldown, byte-identical memory-only regression guard, failure isolation, toggle-off).
- **E2E:** `tests/memory_hint/test_memory_hint_e2e.py` (submit → worker → tool-warning drain path).

Run the full `tests/memory_hint/` suite serially (per lesson `pytest-docker-subprocess-hang`, host `shell_cmd` is reliable for pytest; avoid code_interpreter subprocesses).

---

## 10. Skill System

The **Skill System** is the lifecycle manager for reusable, cross-project *procedures* ("how to do X"). A skill is discovered on demand, optionally auto-loaded into an agent's context, evolved by agents via proposals, and — critically — **automatically invalidated and rebalanced**: skills that prove useless, bad, or stale are auto-retired to `INACTIVE` without any manual cleanup.

> **Why this section matters:** the skill system already owns skill lifecycle and invalidation. A reader checking whether "skills need a load/usage gate" or "we need to suppress useless skills somewhere" should see that this is handled here — in the durable-metrics scoring + rebalance pass — and NOT somewhere ad-hoc (e.g. in the hint pipeline). The hint gate only *suggests*; **invalidation lives entirely in this section's rebalance pass.**

The hint pipeline (§9) *suggests* skills but never decides which remain eligible to be suggested — that is this section. §9.5 explains *how* a skill is suggested; this section explains *whether a skill still exists to be suggested at all*.

Module map: `agent_cascade/skills/` (manager, scoring, matcher, advisor, parser) + `agent_cascade/memory_hint/manager.py` (auto-load + the precision re-rank) + knobs in `agent_cascade/settings.py`.

### 10.1 What a Skill Is

A skill is a single **`SKILL.md`** file: YAML frontmatter (`name`, `description`, `version`, `triggers`) followed by a markdown body of the procedure. The frontmatter `description`/`triggers` are what scoring and `scan_skills` match against; the body is the expertise injected into context.

- **Location:** discovered from `agents/global/skills/` (the global root), with per-agent (`agents/*/skills/`) and user (`workspace/skills/`) roots that override on name collision; a pending upgrade candidate under `agents/global/candidates/` is *strictly highest* priority while it exists (`skills/manager.py:72-79`).
- **Skill vs memory:** a skill is a **cross-project procedure** ("how to do X"); a `.agent_lessons/*.md` memory is a **project-specific fact** ("what happened with Y"). They live in separate trees and are surfaced by separate subsystems (this section vs §9's memory path).

### 10.2 Discovery & Loading

| Mechanism | Entry point | What it does |
|-----------|-------------|--------------|
| **Discover/rank** | `scan_skills` tool (`tools/custom/scan_skills.py:32`) | Relevance-scored listing of registered skills for a query; `preview="<name>"` returns a skill's raw text. Read-only. |
| **Explicit load** | `load_skill` tool (`tools/custom/load_skill.py:21`) | Injects the selected skills' full instructions into the *calling* agent's context this turn. |
| **Auto-load (count)** | `_maybe_autoload_skills` (`memory_hint/manager.py:613`) | If a skill has been *hinted* `memory_hint_skill_autoload_threshold` times (default **3**) and was never loaded, it is auto-injected via the same `inject_skill` path an explicit `load_skill` uses (BUG_0263). Best-effort; never breaks the hint path. |

Auto-load is gated by `skill_autoload_enabled` (ON by default) and read live from the pool config each cycle (`memory_hint/manager.py:380-394`).

### 10.3 Evolution (`propose_skill`)

`propose_skill` (`tools/custom/propose_skill.py:338`) creates a new skill **or** updates an existing one. There is no manual review step — a rating-based decision gate finalizes both paths:

- **New skill** — created **unrated** (no usage/ratings yet) and served immediately. Once validated it is auto-promoted into `agents/global/skills/` (`AUTO_SKILL_AUTO_PROMOTE=True`, `settings.py:920`). Being unrated means scoring treats it as neutral quality (`q̂ = q0` at `n=0`) and it enters the PROTECTED/fair-window so a brand-new skill is never instantly evicted.
- **Update** — written as a **live-serving candidate** under `agents/global/candidates/<name>/SKILL.md`. A candidate always wins the serving race while it exists, so the new version serves right away (`_PRIORITY_CANDIDATE=4`, `manager.py:74-79`). It is later **promoted** or **discarded** by `evaluate_candidates` (the rating-based decision gate, `manager.py:2496`): once the candidate accumulates `CANDIDATE_MIN_RATINGS` (5) ratings it is compared to the incumbent (2-decimal averages) — candidate ≥ incumbent → promote, else the incumbent is restored. A candidate short of the bar after `CANDIDATE_MAX_PENDING_DAYS` (14) is promoted anyway (it has been serving since registration, so the swap is a no-op; prevents a hanging unrated candidate). Triggered on startup, at each registration, and on a 30 s safety timer.

### 10.4 Invalidation & Rebalance (the lifecycle gate — the key behavior)

`rebalance_active_skills` (`manager.py:755`) is a one-shot pass that classifies every active skill and evicts the losers. It runs in a background thread at startup (`pool/core.py:318-347`), gated by the master switch `skill_auto_invalidate_enabled` (ON by default). It never raises — a failure logs and can never break startup.

**Scoring** (`skills/scoring.py`, pure — no I/O). Each skill's score is a bounded product of usage, quality, load-waste, and recency factors:

`score = u(n) · [ q̂(n,avg) / 10 ] · w(L,n) · r(A)`

where `u` = saturating usage (`n`=loads), `q̂` = shrinkage quality vs the neutral baseline `q0=5.0`, `w` = load-waste penalty (`L` loads vs `n` actually used — penalizes skills loaded but never used), and `r` = bounded activity-recency tiebreaker over `A` = user-turns since the skill last had a chance to be used.

**Classification** (`skill_classify`, `scoring.py:59`; precedence BAD > PROTECTED > {USEFUL, USELESS} > UNPROVEN, keyed off `q̂`, `n`, and `A` — *not* the raw score). Defaults: `q0=5.0`, `δq=0.5`, `n_min=5`, `fair_window_turns=50`.

| Class | Trigger | Eviction behavior |
|-------|---------|-------------------|
| **BAD** | `q̂ ≤ q0−δq` **and** `n ≥ n_min` (demonstrably harmful, confident) | Evicted by the absolute gate **even if young** |
| **PROTECTED** | `A < fair_window` (~50 turns) unless BAD | **Immune** to both gates while the fair window is open (a new/just-used skill can't be instantly retired) |
| **USEFUL** | `q̂ ≥ q0+δq` **and** `n ≥ n_min` | Kept; lowest eviction priority |
| **USELESS** | `|q̂−q0| ≤ δq` (neutral quality, not earning its keep) | Evicted by the absolute gate **once past the fair window** (while young it is PROTECTED) |
| **UNPROVEN** | otherwise (below/above baseline but `n < n_min`) | Monitor / keep (exploration) — only cap-eligible |

**Eviction is OR-composed** (`manager.py:776-872`): a skill is removed if **either** the absolute gate catches it (`_ABSOLUTE_GATE_CLASSES = {BAD, USELESS}`, `manager.py:67`) **or** the count-cap budget selects it. PROTECTED skills are excluded from the candidate set entirely. The eviction set is ordered by `eviction_rank_key` (`scoring.py:79`) so `BAD < USELESS < UNPROVEN < USEFUL` evicts first, and bounded by `max_evictions_per_pass` so a bad config can't nuke the corpus. The count target is `raw = round(K × N_qualified)` clamped to `[SKILL_ACTIVE_MIN_CAP=20, SKILL_ACTIVE_MAX_CAP=200]` with `SKILL_ACTIVE_TARGET_K=1.0` (`settings.py:802-804`); `min_cap` is a **lower bound on eviction**, never a force-enable mandate.

**An evicted skill is set `status='inactive'`** and added to `_disabled_names`, then dropped from the discovery cache + registry (`manager.py:883-897`) → it is **no longer servable and no longer hintable**. A later rebalance can re-enable it if its ranking recovers (bounded by the servable corpus).

**`prune_stale_metrics`** (`manager.py:351`) then removes the metrics entry for skills that leave the live set entirely (file gone everywhere). Disabled, platform-incompatible, `INACTIVE/`-subfolder, and candidate skills are **kept** (their on-disk artifact still exists). A scan error on an existing root aborts the prune — a partial scan never triggers deletion.

### 10.5 Durable Metrics Store (`agents/global/skills-metrics.json`)

A **durable, cross-restart per-skill clock** — not a per-session counter (`manager.py:258`, schema 1.3, `_default_metrics_entry` at `manager.py:135`). This persistence is what makes invalidation correct: usage/recency survives restarts and session boundaries, so a skill loaded once across many sessions isn't mistaken for fresh.

| Field | Meaning |
|-------|---------|
| `total_loads` | Cumulative loads (drives the usage + load-waste factors) |
| `hints` | Cumulative hint credits (feeds the precision re-rank, §10.6) |
| `status` | `active` \| `inactive` — the live eligibility flag |
| `last_activity_turn` | Global-turn value at the skill's last activity (for the recency age `A`) |
| `by_version` | Per-version rating history (avg rating + `last_used`), schema 1.2 |
| *(top-level)* `global_activity_turns` | Cumulative user-turn clock bumped once per user turn; drives `A` for every skill |

**Store path and write safety.** The path is `AGENT_CASCADE_SKILLS_METRICS_FILE` (env) or the CWD-relative default `agents/global/skills-metrics.json`; empty/unset = default (`_DEFAULT_METRICS_FILE`, `manager.py:85`). Every flush writes a **uniquely named** sibling temp (`_tmp_sibling`, `manager.py:95`: `<name>.<pid>.<rand>.tmp`) and `os.replace`s it over the target, so the server, a CLI and a test process can flush the same store concurrently without consuming each other's temp file. Because the default is CWD-relative and `AgentPool` builds a `SkillManager` on construction, tests redirect the path to a per-test temp file (autouse `_isolate_skill_metrics_store` in `tests/conftest.py`) — a test-constructed pool can never write the live store.

### 10.6 Precision Demotion (in the hint gate)

Inside the §9.5 skill gate, after dedup/cooldown, `SKILL_PRECISION_WEIGHT` triggers a **re-rank, not a hard drop** (`memory_hint/manager.py:900-927`, knobs `settings.py:762-770`). A skill's effective order is `score × w` where `w = max(SKILL_PRECISION_FLOOR, min(1, (loads+K)/(hints+K)))` with `K=SKILL_PRECISION_K=1.0` (Laplace smoothing) and `SKILL_PRECISION_FLOOR=0.35`. A heavy-hinted but rarely-loaded skill is softly demoted (surfaced lower) — but never buried below `FLOOR × score`, so a rare-but-correct skill stays promotable. When `SKILL_PRECISION_WEIGHT=0` the layer is a no-op and order stays score-descending.

### 10.7 Settings (knobs)

| Setting | Default | Purpose |
|---------|---------|---------|
| `skill_auto_invalidate_enabled` | `True` | Master switch for the whole rebalance/invalidate pass (`pool/core.py:319`). |
| `memory_hint_skill_autoload_threshold` | `3` | Hint-count threshold before auto-loading a repeatedly-hinted skill (`memory_hint/manager.py:382`). |
| `memory_hint_skill_autoload_enabled` | `True` | On/off for the auto-load above. |
| `SKILL_ACTIVE_TARGET_K` | `1.0` | Active target = `round(K × N_qualified)` (`settings.py:802`). |
| `SKILL_ACTIVE_MIN_CAP` / `SKILL_ACTIVE_MAX_CAP` | `20` / `200` | Clamp on the active count (`settings.py:803-804`). |
| `SKILL_PRECISION_WEIGHT` | `1` | Enable the hint re-rank (§10.6); `0` = off (`settings.py:762`). |
| `SKILL_PRECISION_K` / `SKILL_PRECISION_FLOOR` | `1.0` / `0.35` | Smoothing pseudo-count / floor weight for the re-rank (`settings.py:766,770`). |
| `CANDIDATE_MIN_RATINGS` / `CANDIDATE_MAX_PENDING_DAYS` | `5` / `14` | Decision-gate bar for promoting/discarding an update candidate (`settings.py:921-928`). |
| `AGENT_CASCADE_SKILLS_METRICS_FILE` | `agents/global/skills-metrics.json` | Env override for the durable metrics store path; empty/unset = default. The test suite points it at a per-test temp file so the live store is never written. |
| `AGENT_CASCADE_DEBUG_SLOTS` | `0` (off) | Env switch to emit redundant per-module slot traces (`EndpointScheduler.acquire` internals, `SLOT_REACQUIRED`, router recovery, `last_released_endpoint`, `SECURITY_SLOT_*`), the per-turn `[MEMORY_HINT]` submit line, and toggle-off `[STATE_SAVE_SKIP]`/`[STATE_RESTORE_SKIP]` repeats. Set to `1` to re-enable. Off for routine DEBUG runs; deliberately independent of `AGENT_CASCADE_DEBUG` (`settings.py`; gated sites: `scheduler.py`, `engine/slot_lifecycle.py`, `slot_queue.py`, `security_handler.py`, `memory_hint/manager.py`). |
| `AGENT_CASCADE_DEBUG_HTTP` | `0` (off) | Sibling env log switch: raises `httpx`→INFO and `httpcore`→DEBUG (per-request wire trace); otherwise both stay at WARNING (failures only). Lives in `log.py` (configures third-party logger levels, not app call sites). |

Scoring internals (`q0`, `kq`, `n_half`, `g_half`, `r_floor`, `tau_turns`, `δq`, `n_min`, `fair_window_turns`) are module constants in `skills/scoring.py` / `manager.py`, not user settings.

### 10.8 API

| Endpoint | Purpose |
|----------|---------|
| `GET /api/skills` | List registered skills (`api_server.py:1611`). |
| `GET /api/skills/threshold` | Read the active skill threshold setting (`api_server.py:1619`). |
| `POST /api/skills/toggle` | Enable/disable a single skill (`api_server.py:1726`). |
| `POST /api/skills/migrate` | One-time metrics-store migration (`api_server.py:1740`). |

> **Not present here, by design:** there is no separate "zero-load suppression" gate in the hint pipeline. Suppression of never-used skills is handled here (the load-waste factor `w`, the `USELESS`/`BAD` classification, and the rebalance eviction), so the hint gate does not duplicate it.

---

## 11. Tool System & MCP Discovery

Agent Cascade offers two classes of tools to the LLM:

- **Core tools** (file ops, `shell_cmd`, web, `call_agent`, `tool_search`, …) — always present in the offered `functions=` set.
- **MCP tools** (from connected Model Context Protocol servers, e.g. `chrome-devtools`, `blender`) — *deferred*: they are **not** in the offered set until an agent discovers them.

### 11.1 Why MCP Tools Are Deferred

Offering every MCP tool on every call bloats the `functions=` payload (each tool definition is ~200–500 tokens). With dozens of MCP tools that is thousands of tokens per call for tools the agent rarely uses. Deferral keeps the offered set small (core tools only) and lets the agent pull MCP tools in on demand.

### 11.2 `tool_search` — On-Demand Discovery

`tool_search` is the discovery mechanism:

- **Search mode** (`query` non-empty): ranks the not-yet-unlocked MCP tools by BM25 relevance to the query and **unlocks** the top matches for the next call. Unlocked names are appended to the instance's discovered set, so they appear in the offered `functions=` set from the following turn.
- **Catalog mode** (`query` empty): a read-only listing of all *enabled* MCP tools (grouped by server, one-line description each), for orienting without unlocking anything.

Unlocks are **append-only** on the instance's discovered set; a tool, once discovered, stays offered for the rest of the session. The result carries a trailing JSON envelope (`{"tool_search": {"loaded": [...]}}`) that `session_io` re-derives on load, so a reloaded session offers the same tools.

### 11.3 The Cache-Bust Cost (Important)

The `functions=` array is serialized into the prompt **right after the system prompt** — i.e. inside the KV-cache prefix. Because the cache is strictly prefix-based:

- **Any change to the offered tool set** (unlocking a new MCP tool) **invalidates the entire prefix** → a full prefill of the whole conversation at ~0% cache hit.
- This is a **one-time cost per array change**: the first call after an unlock pays the full prefill; subsequent calls with the same tool set are back to ~97–99% cache hits.

**Trade-off:** deferral saves tokens on *every* call, but each unlock pays a full prefill. That is acceptable when unlocks are rare (a few per session) and not when an agent drip-unlocks tools one at a time — prefer a single `tool_search` that grabs everything the task will need.

> **Note:** an earlier plan claimed the "append-only discovered tail preserves the prefix." That is **false** for a prefix-based KV cache — appending to the serialized tool array changes the prefix, so *every* unlock is a full prefill. The deferral win is token savings, not cache preservation.

---

## Appendix: Quick Reference

### Configuration Defaults (`PoolSettings`)

| Setting | Default | Description |
|---------|---------|-------------|
| `idle_timeout_seconds` | 1600 | Auto-dismiss regular agents after this much inactivity |
| `system_agent_idle_timeout_seconds` | 300 | Auto-dismiss system agents (Compressor, Security) after this much inactivity |
| `idle_check_interval` | 60 | Check for idle agents every N seconds |
| `compression_force_threshold` | 96.0 | Force compression at X% token usage |
| `compression_warning_threshold` | 90.0 | Warn agent at X% token usage |
| `compression_timeout` | 120 | Max seconds for compression to complete |
| `compression_consolidation_threshold` | 8 | Trigger L2 consolidation when marker count reaches this value |
| `compression_max_consolidation_tokens` | 100,000 | Abort consolidation if input summaries exceed this token count |
| `security_check_timeout` | 120 | Max seconds for Security Advisor response |
| `max_auto_rollbacks` | 5 | Max loop recovery retries before escalation |

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
| AgentPool + Managers | `agent_pool.py` is a **facade** re-exporting `from .pool.core import AgentPool`. Real implementation is the package `pool/` (e.g. `core.py`, `conversation_map.py`, `parallel_manager.py`, `idle_manager.py`, `logger_mgr.py`, `slots.py`, `lifecycle.py`, `session_io.py`, `config_persist.py`, `rollback.py`) |
| ExecutionEngine | `execution_engine.py` is a **facade** re-exporting `from agent_cascade.engine.core import ExecutionEngine`. Real implementation is the package `engine/` (e.g. `core.py`, `run_loop.py`, `turn_setup.py`, `llm_call.py`, `response_pipeline.py`, `tool_execution.py`, `slot_lifecycle.py`, `compression_exec.py`, `auto_skill.py`) |
| Loop Detection | `loop_detection.py` is a **deprecated stub** (holds only `LoopDetectedError`). Real detectors: `exact_loop_detect.py` (Tier 1 exact), `tool_loop_detect.py` (Tier 2 fuzzy), `inner_loop_detect.py` + `two_phase_loop_detect.py` (intra-generation) |
| API Server | `api_server.py` (single file; also hosts the config/control REST surface and MCP config — see §3.4) |
| API Router | `api_router.py` is a **facade** re-exporting `from agent_cascade.api_router_pkg.router import APIRouter`. Real implementation is the package `api_router_pkg/` (e.g. `router.py`, `scheduler.py`, `endpoints.py`, `breaker_gate.py`, `helpers.py`) |
| MCP Server Config | `config/mcp_servers.json` (raw servers-map; `enabled` defaults true when absent) — served by `api_server.py` MCP routes (see §3.4) |
| Compression | `compression/core.py`, `compression/agent_invoker.py` (also `handler.py`, `helpers.py`) |
| Memory-Hint System | `memory_hint/manager.py`, `memory_hint/matcher.py`, `memory_hint/vault.py`, `memory_hint/stats.py` (see §9) |
| Skill System | `skills/manager.py`, `skills/scoring.py`, `skills/matcher.py`, `skills/advisor.py`, `skills/parser.py`; tools `tools/custom/{scan_skills,load_skill,propose_skill}.py`; durable store `agents/global/skills-metrics.json` (see §10) |
| Agent Base Classes | `agent_cascade/agent.py`, `agent_cascade/agent_instance.py`, `agent_cascade/agents/fncall_agent.py`, `agent_cascade/agents/assistant.py` |
| Tools | `tools/*.py` |

### Testing & CI

Test selection, capability markers (`requires_docker`, `heavy_concurrency`, `windows_only`, `skip_if_no_local`), pytest plugins, and the CI job matrix are documented in **`docs/ci_testing.md`** — see it rather than duplicating the policy here.
