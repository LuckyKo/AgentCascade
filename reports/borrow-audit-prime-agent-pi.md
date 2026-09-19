# Borrow-audit: prime-agent & pi → AgentCascade

**Date:** 2026-08-29 · **Mode:** read-only investigation (no code changes to either repo)
**Repos:** `N:\work\WD\prime-agent` (at origin/main), `N:\work\WD\pi` (~10 commits behind origin/main; working tree treated as authoritative for structure)
**Target:** `N:\work\WD\AgentCascade` (`agent_cascade/`)

> Canonical report for todo.md line 38. Reviewer corrections applied inline (see Verification section).

---

## TL;DR

**Most borrowable (in priority order):**

1. **Session JSONL tree model with `id`/`parentId` + versioned migration** (pi `session-format.md` + `session-manager.ts`) — AC's `load_session_from_log` is linear (finds last compression marker, rebuilds `[SYS][U0][COMP...][tail]`); a tree model enables in-place branching without new files. *High value, Med effort.*
2. **Bash completion fence** (prime `prime-agent-runtime/src/rlm/bash.py`) — a per-run random token emitted as a fence lets the parent detect true command completion independent of process exit/EOF. AC's `async_shell_pkg/tracker.py` detects done via process exit; the fence closes the "background child holds stdout" ambiguity. *Med effort, high robustness payoff.*
3. **Validated, framed wire protocol with typed schemas + conformance tests** (pi `packages/protocol/`) — the structural pattern (typed schemas, strict no-unknown-fields, incremental `FrameDecoder`) is directly applicable to AC's headless/REST/WS control surface (todo #40). *Med effort.*
4. **RPC `steer`/`follow_up`/`clear_queue`/`abort` with `id` correlation** (pi `docs/rpc.md`) — precise semantics for injecting into a *running* agent turn. AC's `send_message` queues but lacks steer-vs-follow-up distinction and abort. *Low–Med effort.*
5. **Refinement governance: proposal→apply→rollback with versioned harness entries** (prime `core/refinement/refinement.ts`) — turns AC's auto-skill/`.agent_lessons/` writes into evidence-backed, reversible, versioned edits. *Med effort, high strategic value for AC's "Active Self-Improvement" goal.*

**Also worth borrowing (lower priority):** daemon protocol versioning + capability negotiation (prime `daemon-protocol.ts`), cache-miss/cost-waste telemetry (pi `cache-stats.ts`), eval harness with model-backed judges (pi `evals/`), message rate limiting + family reach (prime `agent-messages.ts`), persistent goal-state with budget (prime `goals.ts`), cron/interval scheduled prompts (prime `cron-jobs.ts`), token budgets on skills (prime `skills.ts`), model registry (prime `model-registry.ts`).

**Not worth borrowing:** prime's REPL repair path (AC has no persistent kernel), pi's full daemon (ops burden), pi's extensions runner (AC skills already cover it), pi's in-run compaction trigger (AC's proactive compression already covers it).

---

## prime-agent — notable mechanisms & gaps

Prime Agent is a "self-improving RLM harness" built on top of pi (README). Its differentiators over AC:

### 1. Persistent Python REPL as the model's built-in tool (RLM) — **gap (design)**
- **Where:** `packages/coding-agent/src/core/kernel/repl-manager.ts` (1502 ln) + `prime-agent-runtime/src/rlm/repl.py`, `repl.md`.
- **What:** The kernel is a `python -m rlm.repl` subprocess (JSON-lines on stdin, events on stdout). The model *programs* a persistent environment: `rlm(...)` spawns real child agents as function calls, and file/shell/context ops run through code in that live namespace.
- **Why a gap:** AC's `code_interpreter` is Docker-backed and stateful, but the agent's *primary* tool surface is discrete tool calls, not a persistent programmable kernel where sub-agents are first-class function calls.
- **Borrow sketch:** Not a rewrite. The *pattern* (a durable in-process namespace where `rlm()` is a callable that maps to AC's `call_agent`) is borrowable as an optional "kernel" mode. High effort; lower priority than the items below.
- **Effort:** High.

### 2. Bash completion-fence sentinel + process containment — **gap (robustness)**
- **Where:** `prime-agent-runtime/src/rlm/bash.py` (`BashHandle`, `_consume_output` ~ln 327, `_status_script` ~ln 711, `_fence_printf` ~ln 699).
- **What:** Each command gets `secrets.token_hex(32)`, split into two halves; the shell wrapper `printf`s a fence `\036prime-agent-complete:<a><b>\037` (octal — bytes `0x1e` prefix, `0x1f` suffix) after the command. The parent watches the byte stream for the marker (handling cross-chunk splits) to mark the command *complete* even if background children keep the pipe open. Plus POSIX process-group + a "status socketpair" gate so a child can't run before it's journaled; Windows uses a kill-on-close job object. Orphan journal reaps leaked pids.
- **Why a gap:** AC's `async_shell_pkg/tracker.py` detects completion via process exit/EOF and does process-tree cleanup, but a command that spawns a background child and the parent shell exits can be ambiguous. The fence gives a *deterministic* done-signal independent of process lifecycle.
- **Borrow sketch:** Add an optional completion-fence wrapper to the `shell_cmd`/async-shell launch path: mint a per-run token, wrap the command with a post-exec fence `printf`, and scan the output pump for it (with cross-chunk retention). Reuse the existing bounded-buffer tail. Keep it POSIX-first, best-effort on Windows.
- **Effort:** Med.

### 3. Protocol frame repair (resilient long-lived subprocess) — **gap (resilience pattern)**
- **Where:** `packages/coding-agent/src/core/kernel/repl-manager.ts` `repairProtocolChild()` (~ln 403), `bootstrapRepairedKernel()` (~ln 450), `shared.ts` `protocolRepair` flag.
- **What:** If the kernel's JSON stream emits an invalid frame, "just crash" is avoided: kill child → respawn → restore namespace snapshot → re-run bootstrap → **loop-safety** (if the replacement corrupts again, discard instead of respawn-looping). Bounded `REPAIR_STEP_TIMEOUT_MS`.
- **Why a gap:** AC's `code_interpreter`/Docker and `async_shell` are more stateless; there is no first-class "detect corruption → bounded repair → re-bootstrap" contract for a long-lived child protocol.
- **Borrow sketch:** Generalize as a `ResilientChildProtocol` mixin: frame validation + a single bounded repair attempt + a "give up" owner flag. Applicable to any future persistent kernel/daemon child.
- **Effort:** Med (if a persistent kernel is added); otherwise Low as a reusable pattern.

### 4. Daemon mode & supervisor with versioned, capability-gated protocol — **gap (ops)**
- **Where:** `packages/coding-agent/src/modes/daemon/daemon-supervisor.ts` (202 KB), `daemon-mode.ts` (250 KB), `daemon-protocol.ts` (`DAEMON_PROTOCOL_VERSION = 7`, `DAEMON_SCHEMA_REVISION = 23`, command/event capability maps ~ln 716/722).
- **What:** Detached background service that keeps sessions, REPL state, schedules, and subagents alive after the terminal disconnects; `prime-agent attach/agents/status/doctor/shutdown` reattach. Every wire change is classified backward-compatible / capability-gated / incompatible; clients check the capability before using a command.
- **Why a gap:** AC has a REST API (`api_server.py`, `start_api_server.py`) and instance separation, but no *detached daemon process* with a versioned, capability-negotiated protocol. AC's API is in-process.
- **Borrow sketch:** Extract just the **governance** (not the 202 KB implementation): add `API_VERSION` + a capability list to AC's REST handshake, and require clients to check capabilities before calling newer endpoints. Directly relevant to todo #40.
- **Effort:** Low (governance only) / High (full daemon).

### 5. Model registry (unified multi-provider catalog) — **gap (ops)**
- **Where:** `packages/coding-agent/src/core/model-registry.ts` (55 KB), `model-resolver.ts`, `ai/src/providers/register-builtins.ts` (lazy loaders), `scripts/generate-models.ts` (generated catalog).
- **What:** One registry over many providers with custom models, per-provider overrides, lazy provider registration, and a generated model catalog.
- **Why a gap:** AC's `llm/` + `get_chat_model` resolve a model from a dict but there is no unified *catalog/registry* with provider override + generated metadata + per-class defaults surfaced to the UI.
- **Borrow sketch:** Add a `model_registry` (in-memory + JSON catalog) that AC's per-agent-class model config (todo #41 reasoning-effort) reads from. Med.

### 6. Refinement / continual-harness governance — **gap (self-improvement quality)**
- **Where:** `packages/coding-agent/src/core/refinement/refinement.ts` (1031 ln).
- **What:** `/refine` reviews the trajectory and emits a `RefinementProposal` (summary, rationale, `edits[]`, `expectedOutcome`). Edits are `create/update/delete` over versioned `HarnessEntry` (kind = prompt|memory|skill|subagent) with `before`/`after`, `version`, `refinements.jsonl` history, and `rollbackOf`. Never rewrites the immutable base prompt.
- **Why a gap:** AC's auto-skill generation + `.agent_lessons/` write lessons, but there is no *proposal→apply→rollback* governance with per-entry versioning and evidence fields.
- **Borrow sketch:** Wrap AC's skill/lesson writer with a proposal object + versioned store + a `rollback(refinement_id)` tool. Strong fit for AC's stated "Active Self-Improvement" goal.
- **Effort:** Med.

### 7. Persistent goals with budget + completion report — **gap (long-running)**
- **Where:** `packages/coding-agent/src/core/goals.ts` (`GoalState`: status, tokenBudget, tokensUsed, timeUsedSeconds, continuationsUsed; `goal.*` host requests).
- **What:** `/goal` keeps an objective active across turns until complete/paused; tracks token/time budget and emits a completion-budget report.
- **Why a gap:** AC has `enable_agent_budgeting` but no explicit *goal-state object* that survives turns and reports completion vs budget.
- **Borrow sketch:** Add a lightweight `GoalState` per instance + a `goal` tool (set/pause/clear) feeding the existing budgeting code. Low–Med.

### 8. Scheduled prompts / heartbeats re-entering a session — **gap (long-running)**
- **Where:** `packages/coding-agent/src/core/cron-jobs.ts` (`AgentCronJob`: once/cron/interval, `deliveryMode: steer|follow_up`, proper-lockfile, fsync persistence).
- **What:** Scheduled prompts delivered to a session; when the session is busy, `steer` interrupts the current turn or `follow_up` waits.
- **Why a gap:** AC has a per-slot FIFO scheduler but not *timed* scheduled prompts/heartbeats that re-enter a specific session.
- **Borrow sketch:** A `schedule` tool (cron/interval/once) that enqueues a prompt to a named instance using the existing `send_message`/queue with a steer/follow_up flag. Med.

### 9. Agent-to-agent family reach + rate limiting — **partial gap**
- **Where:** `packages/coding-agent/src/core/agent-messages.ts` (`AgentFamilyRelationship = parent|sibling|child`, `AGENT_FAMILY_REACH_ERROR`, `DEFAULT_AGENT_MESSAGE_RATE_LIMIT_*`, pending-per-session cap).
- **What:** Agents discover parent/siblings/children and can message within the family; messaging is rate-limited and capped per session.
- **Why a gap:** AC's `send_message` (verified in `pool/message_queue.py`) routes by name with no *family-reach* restriction and no per-destination rate limit.
- **Borrow sketch:** Add an optional reach policy (parent/sibling/child) + a per-destination rate limiter to `send_message`. Low.

---

## pi — notable mechanisms & gaps

pi is the base harness prime-agent builds on (prime README "Acknowledgements"). Structure: `packages/` = agent, ai, client, coding-agent, protocol, server, session-backends, telemetry, tui, evals.

### 1. `pi-protocol` — validated, framed, conformance-tested wire protocol — **gap (headless control)**
- **Where (two-level layout):**
  - **Frame layer** (top-level wire format): `packages/protocol/src/framing.ts` (`encodeFrame`, `FrameDecoder`, `DEFAULT_MAX_FRAME_LENGTH`), `packages/protocol/src/schemas.ts` (typed `CommandSchema`/`ServerEventSchema`, strict no-unknown-properties), `packages/protocol/src/codec.ts` (message encode/parse), `README.md` (strict RFC 8949 subset, 16 MiB / 1M / 64-depth limits).
  - **CBOR layer** (per-frame payload): `packages/protocol/src/cbor/encoder.ts` (`encodeCbor`), `packages/protocol/src/cbor/decoder.ts` (`decodeCbor`), `packages/protocol/src/cbor/options.ts` (depth/size limits).
- **What:** Binary layout `[uint32-be length][CBOR payload]`; incremental decoders accept arbitrary fragmentation/coalescing; every schema rejects unknown fields; conformance tests in `test/cbor/`.
- **Why a gap:** AC's headless control is a REST API (todo #40) + WS. There is no *validated, versioned, transport-neutral* message contract with strict schema checks and conformance tests.
- **Borrow sketch:** Define an AC protocol package: typed command/event schemas (prompt, steer, abort, set_model, list, attach) + a `FrameDecoder` (can be JSONL-framed rather than CBOR to stay Pythonic) + conformance tests. Single highest-leverage structural borrow for todo #40.
- **Effort:** Low–Med (JSONL variant) / Med (CBOR).

### 2. RPC mode — `steer`/`follow_up`/`clear_queue`/`abort` with `id` correlation — **gap (headless semantics)**
- **Where:** `packages/coding-agent/docs/rpc.md` (41 KB), `src/modes/rpc/rpc-mode.ts`, `rpc-client.ts`, `jsonl.ts`.
- **What:** JSONL over stdin/stdout; `id` correlates request/response; `prompt` during streaming requires `streamingBehavior: steer|followUp`; `clear_queue` returns queued text; `abort` stops the run. Strict LF-only framing (docs explicitly warn Node `readline` is non-compliant due to U+2028/2029).
- **Why a gap:** AC's `send_message` queues messages but the *injection semantics* (steer-now vs follow-up-later, clear-queue, abort) are not first-class protocol verbs.
- **Borrow sketch:** Add `steer`/`follow_up`/`clear_queue`/`abort` as explicit verbs to the AC headless protocol + `send_message`, mapping onto the existing message queue + a "busy" flag.
- **Effort:** Low–Med.

### 3. Session JSONL *tree* (`id`/`parentId`) + version migration + SQLite/FTS — **gap (persistence/search)**
- **Where:** `packages/coding-agent/docs/session-format.md` (v1 linear → v2 tree → v3; auto-migrate on load), `core/session-manager.ts` (51 KB), `packages/session-backends/sqlite-node/` (`SqliteSessionRepository`, `createSqliteSessionSearch` FTS5 + triggers).
- **What:** Entries form a tree via `id`/`parentId` → in-place branching without new files; versioned format with on-load migration; a durable SQLite backend with lazily-built FTS full-text search kept in sync by triggers.
- **Why a gap:** AC logs are per-agent JSONL and `load_session_from_log` (verified `pool/session_io.py`) loads them *linearly* (finds last marker, builds `[SYS][U0][COMP...][tail]`). No branching, no cross-session indexed search.
- **Borrow sketch:** (a) add `id`/`parentId` + a version header to AC log entries with a migration pass; (b) add an optional SQLite session index + FTS for cross-session search and fast resume. (a) is Low–Med, (b) is Med–High.
- **Effort:** Med (tree+version) / High (SQLite+FTS backend).

### 4. Cache-miss / cost-waste telemetry — **gap (cost observability)**
- **Where:** `packages/coding-agent/src/core/cache-stats.ts` (`CACHE_TTL_MS = 5 min`, `NOISE_FLOOR_TOKENS = 1024`, `detectMiss`, `CacheMiss{missedTokens, missedCost, idleMs, modelChanged}`, `CacheWasteTotals`).
- **What:** Scans the session, compares each turn's prompt to the previous request, flags cache misses above the noise floor, and converts them to wasted $ using per-model pricing.
- **Why a gap:** AC's `telemetry.py` records token usage (`record_token_usage`, `completion_tokens`) but does not analyze *cache hits/misses* or *wasted cost* — a key lever for AC's "least token usage" goal.
- **Borrow sketch:** Add a `cache_stats` pass over AC's logged usage (needs the provider to expose `cache_read`/`cache_write` tokens; verify provider support) and surface per-session waste in the UI/telemetry.
- **Effort:** Low (if usage already carries cache fields) / Med (else).

### 5. Evals harness — behavioral, model-backed, comparative — **gap (self-improvement measurement)**
- **Where:** `packages/evals/README.md` + `src/pi-harness.ts` (`createPiCodingAgentHarness`, `evalHarnessTable`, `createJudge`, candidate-vs-baseline pass-rate *lift*, `runs.jsonl` + native session JSONL artifacts).
- **What:** Real `AgentSession` adapted to a test harness; judges (deterministic or model-backed); comparative tables report pass-rate lift + token/latency/cost deltas.
- **Why a gap:** AC has `benchmark/` and ad-hoc regression logs but no *structured* eval harness with judges and candidate-vs-baseline lift to measure prompt/skill/model changes — the missing measurement layer for AC's self-improvement loop.
- **Borrow sketch:** Build an AC eval harness that runs a named task against a baseline vs candidate agent config and reports judge score + lift + cost delta. High effort, high value.
- **Effort:** High.

### 6. Unified multi-provider AI layer — **gap (ops)** (mirrors prime #5)
- **Where:** `packages/ai/` (providers, generated catalog, `completeSimple`, standardized events text/tool_call/thinking/usage/stop).
- **Borrow sketch:** Same registry idea as prime model-registry. Med.

### 7. In-run compaction after a large tool result — **partial (AC is stronger elsewhere)**
- **Where:** pi `packages/agent/src/agent-loop.ts`, `core/compaction/compaction.ts`.
- **Why:** AC already fixed forced compression to be *proactive*, so the "before next LLM call" trigger is largely covered. The borrowable sliver is the *specific trigger point* (after a big tool result, inside the same run) if AC's detector doesn't already fire there.
- **Effort:** Low (verify AC's trigger points first).

### 8. Extensions runner — **mostly covered by AC skills**
- **Where:** `packages/coding-agent/src/core/extensions/runner.ts`, `docs/extensions.md` (119 KB), `packages/agent/src/harness/`.
- **Why not a gap:** AC's runtime skill loading (`load_skill`/`scan_skills`) + `code_interpreter` + MCP already cover user-defined capabilities. pi's "extensions" are conceptually close to AC skills. No strong borrow.

---

## Borrow candidates table

| # | Idea | Source repo + file path | Why it's a gap for AC | Borrow sketch (agent_cascade/) | Effort |
|---|------|------------------------|----------------------|-------------------------------|--------|
| 1 | Session JSONL tree (`id`/`parentId`) + version migration | pi `docs/session-format.md`, `core/session-manager.ts` | `load_session_from_log` loads logs linearly; no branching/versioning | Add `id`/`parentId` + version header to log entries + on-load migration pass in `pool/session_io.py` | Med |
| 2 | Bash completion-fence sentinel | prime `prime-agent-runtime/src/rlm/bash.py` (`_consume_output`, `_status_script`, `_fence_printf`) | `async_shell` detects done via exit/EOF; background children make "done" ambiguous | Wrap `shell_cmd`/async-shell launch with per-run token fence; scan output pump for marker (cross-chunk retention) | Med |
| 3 | Validated framed wire protocol (typed schemas, conformance tests) | pi `packages/protocol/src/{framing,schemas,codec}.ts` + `cbor/{encoder,decoder,options}.ts` | AC headless control = REST (todo #40), no versioned/validated message contract | New `agent_cascade/protocol/` with typed command/event schemas + `FrameDecoder` (JSONL-framed) + conformance tests; wire into `api_server.py` | Low–Med |
| 4 | RPC `steer`/`follow_up`/`clear_queue`/`abort` + `id` correlation | pi `docs/rpc.md`, `src/modes/rpc/rpc-mode.ts` | `send_message` queues but injection semantics aren't first-class verbs | Add steer/follow_up/clear_queue/abort verbs to headless protocol + `send_message`, mapping to existing queue + busy flag | Low–Med |
| 5 | Refinement governance (proposal→apply→rollback, versioned entries) | prime `core/refinement/refinement.ts` | AC auto-skill/`.agent_lessons/` writes lack proposal/version/rollback | Wrap skill/lesson writer with proposal object + versioned store + `rollback(refinement_id)` tool | Med |
| 6 | SQLite session backend + FTS full-text search | pi `packages/session-backends/sqlite-node/` | No cross-session indexed search; resume is per-file | Optional SQLite session index + FTS5 (trigger-synced) for search/fast resume | High |
| 7 | Cache-miss / cost-waste telemetry | pi `core/cache-stats.ts` | `telemetry.py` records tokens but no cache hit/miss or wasted-cost analysis | `cache_stats` pass over logged usage (needs provider cache fields) + per-session waste in UI | Low–Med |
| 8 | Evals harness (judges + candidate-vs-baseline lift) | pi `packages/evals/` | No structured eval with judges/lift to measure prompt/skill/model changes | AC eval harness: named task, baseline vs candidate config, judge score + lift + cost delta | High |
| 9 | Daemon protocol versioning + capability negotiation | prime `modes/daemon/daemon-protocol.ts` (`DAEMON_PROTOCOL_VERSION=7`, `DAEMON_SCHEMA_REVISION=23`) | AC REST API has no version/capability handshake | Add `API_VERSION` + capability list to REST handshake; gate newer endpoints on capability | Low |
| 10 | Message rate limiting + family reach | prime `core/agent-messages.ts` | `send_message` routes by name, no per-destination rate limit or reach policy | Optional reach policy (parent/sibling/child) + per-destination rate limiter in `send_message` | Low |
| 11 | Persistent goal-state with budget | prime `core/goals.ts` | No goal object that survives turns and reports completion vs budget | Lightweight `GoalState` per instance + `goal` tool feeding existing budgeting | Low–Med |
| 12 | Cron/interval scheduled prompts | prime `core/cron-jobs.ts` | No timed scheduled prompts/heartbeats re-entering a session | `schedule` tool (cron/interval/once) enqueuing to named instance with steer/follow_up flag | Med |
| 13 | Token budgets on skills | prime `core/skills.ts` | No per-skill token budget | Add `tokenBudget` field to skill schema + enforcement in skill invocation | Low |
| 14 | Model registry (unified multi-provider catalog) | prime `core/model-registry.ts`, `packages/ai/` | No unified catalog/registry with provider override + generated metadata | `model_registry` (in-memory + JSON catalog) read by per-agent-class model config (todo #41) | Med |

---

## Not worth borrowing (and why)

- **Prime REPL repair path** (`repl-manager.ts` `repairProtocolChild`): AC has no persistent in-process kernel to repair. The *pattern* is reusable (see prime #3) but the implementation isn't.
- **Pi full daemon** (`daemon-mode.ts` 250 KB, `daemon-supervisor.ts` 202 KB): heavy ops burden; AC's in-process REST is simpler for its current scale. Borrow only the *governance* (version + capability handshake), not the daemon.
- **Pi extensions runner**: AC's skills + `code_interpreter` + MCP already cover user-defined capabilities. Conceptually redundant.
- **Pi in-run compaction trigger**: AC's proactive forced compression (already fixed) covers the "before next LLM call" case. Only borrow the specific "after big tool result, same run" trigger if AC's detector misses it.
- **Pi CBOR specifically** (vs the protocol *pattern*): AC is in-process + text-based today; the JSONL-framed variant of the protocol pattern is the right fit, not CBOR bytes.

---

## AC mechanisms worth *keeping* (pi/prime don't have)

- **Async-shell control commands**: `operation_manager/shell.py` intercepts `__status / __kill / __heartbeat=N / __ctrl_c / __wait` (text prefixes the agent sends as stdin) and routes them to `async_shell_pkg/tracker.py` public methods (`get_status`, `kill_task`, `update_heartbeat`, `send_ctrl_c`, `kill_all`). This is a clean pattern for controlling long-running shells; pi/prime are single-session REPLs without it.
- **Two-phase compression detector** (`compression/handler.py`): arguably more sophisticated than pi/prime's threshold-based compaction. Keep.
- **Instance separation + per-slot FIFO scheduler**: AC's `pool/` model is a different (and in some ways richer) architecture; don't replace it with pi's single-session model.

---

## Verification

- Read directly from source in both repos (read-only).
- Line numbers cited for the most load-bearing claims.
- An independent reviewer agent (delegated) spot-checked 14/14 table rows against source; three corrections applied after review:
  1. **Pi protocol layout** clarified into two levels — frame layer (`src/{framing,schemas,codec}.ts`) vs CBOR layer (`src/cbor/{encoder,decoder,options}.ts`). My initial single-path claim was wrong.
  2. **Async-shell `__*` items** are *shell control commands* (text prefixes intercepted by the shell tool), not tracker methods — wording corrected to name both the command set and the underlying public methods.
  3. **Bash-fence marker** annotated with hex values (`0x1e`/`0x1f`) — the octal `\036`/`\037` is correct but ambiguous, so hex was added.
- Not exhaustive — this is a survey, not an audit of every file.
