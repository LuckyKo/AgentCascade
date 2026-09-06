# Borrow-Audit — High-Level Implementation Plans (value-vs-effort)

**Date:** 2026-08-29 · **Mode:** planning only (read-only on AC source; no code changes)
**Input report:** `research_reports/borrow-audit-prime-agent-pi.md` (14-row borrow table)
**Purpose:** So the user can judge, per candidate, whether a "borrow" idea is a *real pain point* for AgentCascade today or a nice-to-have — and see the plan shape before committing effort.

> Every AC claim below is grounded in a file I actually read. Files cited inline. Where AC's exact behavior is uncertain I flag it explicitly rather than assume.

---

## TL;DR decision table

| # | Candidate | Real pain point? | Effort | Value (1 line) | Verdict |
|---|-----------|:----------------:|:------:|----------------|---------|
| 1a | Session JSONL **version header + on-load migration** | **Yes** (latent, not daily) | Low–Med | Future-proofs the log format; makes branching possible later without a second migration | **Do** (as a small, isolated first step) |
| 1b | Session JSONL **tree (`id`/`parentId`) + in-place branching** | **Maybe** (no user-visible need yet) | Med–High | In-place "fork this session" without new files — only valuable once you *want* to branch | **Defer** until a concrete branching need exists |
| 2 | SQLite session backend + FTS full-text search | **Yes** (grows with scale; real now at ~507 files/~150 MB in the *workspace* `logs/`) | Med–High | Cross-session "find that decision / error / commit across all my logs" + fast resume | **Do** (as a read-only index layer over `<workspace>/logs/`, not a storage migration) |
| 3 | Bash completion-fence sentinel | **No** for AC's current usage (mostly theoretical) | Med | Deterministic done-signal independent of process exit — but AC already detects via `proc.poll()` + drain-thread join | **Skip** (revisit only if a real background-child hang is observed) |
| 4a | Validated framed wire protocol + version/capability gating | **Maybe** (serves todo #40, not an active pain) | Med | Versioned, schema-validated control contract for headless/REST clients | **Do — scope to a lightweight JSONL validation layer** behind the existing REST; skip CBOR framing |
| 4b | RPC verbs `steer` / `follow_up` / `clear_queue` / `abort` + `id` correlation | **Yes** (for headless/automation use) | Med | Programmatic control of a *running* agent: interrupt now vs queue-for-later, abort run | **Do** (builds on 4a; the highest-leverage part of todo #40) |

**Recommended sequencing:** `2` → `1a` → `4a` → `4b`. Do the SQLite index first (highest value/effort ratio, fully additive), then the cheap session version header, then the protocol validation layer, then the RPC verbs on top. Defer 1b and skip 3 unless a concrete need appears.

---

## Candidate 1 — Session JSONL tree (`id`/`parentId`) + versioned migration (pi)

### What it is
Two related ideas from pi `docs/session-format.md` + `core/session-manager.ts`:
- **(a) Version header + on-load migration:** a format-version field on log entries with an auto-migrate pass when loading old logs.
- **(b) Tree model (`id`/`parentId`):** each entry references its parent, so the session is a *tree* — you can branch in-place (fork a conversation at any point) without writing a new file.

### Is this a real pain point for AC right now? — HONEST verdict
**Partially. The "separation" the user has in mind is mostly already solved; the tree part is speculative.**

I read `pool/session_io.py` end-to-end. AC's current session model:

- **Per-instance log files.** Each agent instance gets its own JSONL (`logger/agent_instance_logger.py`, e.g. `logs/coder_builder_20260828_061517.jsonl`). Instance separation is a first-class, working feature — this is *not* the gap pi's tree addresses (pi is single-session; AC is multi-instance).
- **Linear load with two boundary mechanisms** (`load_session_from_log`, `session_io.py:432`):
  - `_extract_last_session` (`session_io.py:286`) keeps only messages after the **last SYSTEM message** — this handles the "server restarted and appended to the same file" case.
  - Working set is rebuilt as `[SYS][U0 first-user][all compression markers][tail-after-last-marker]`, where markers are user messages starting with `COMPRESSION_MARKER = "--- CONTEXT COMPRESSED"` (`prompts/dna.py:88`).
- **No entry IDs, no `parentId`, no format version header** anywhere in the log or loader.

So concretely, "proper session separation" for AC means one of three things, and I'm being explicit about which are real:

1. **Instance-scoped logs / durable resume** — *already works.* Per-instance files + `load_session_from_log` + `copy_session_file` (`agent_instance_logger.py:118`) give you per-agent logs and restart recovery. Not a gap.
2. **Branching (fork a session in place)** — *not currently possible, but also not an observed need.* Nothing in AC forks conversations; the tree model only pays off once you want "continue from this point differently." I found no branching/fork concept in the codebase (grep for `branch|fork|parentId` returns only unrelated hits). This is speculative.
3. **Durable resume robustness** — *mostly works, with one real fragility.* The whole load path hinges on two heuristics: "last SYSTEM message" and "messages starting with `--- CONTEXT COMPRESSED`." Both are content-sniffing, not structural. If a user message legitimately contains that marker text, or a system message is missing/misplaced, the working-set rebuild silently mis-slices. **This is the one genuine pain point**, and it's exactly what a version header + explicit entry IDs fix — cheaply, without needing the full tree.

**Verdict:** (a) version header + migration = real latent robustness win, low cost → **Do**. (b) full tree/branching = nice-to-have with no current consumer → **Defer**.

### High-level plan
**Part (a) — version header + on-load migration (the part worth doing now):**
- Add a `format_version` field to the metadata line that `AgentInstanceLogger.__init__` writes (`logger/agent_instance_logger.py:63`, currently writes an unversioned metadata dict). Bump to v2.
- Define the version contract in one place (e.g. a small constant + docstring near `COMPRESSION_MARKER` in `prompts/dna.py`).
- Add a **migration pass** at the top of `_parse_json_input` / `load_session_from_log` (`pool/session_io.py`) that detects v1 logs (no version) and normalizes them to v2 in memory before the existing boundary logic runs. v1→v2 is identity for now; it just establishes the hook.
- Make the working-set rebuild **structural rather than content-sniffing**: tag compression markers with an explicit `event`/type field on write (the loader already skips `"event"` lines at `session_io.py:420`, so this fits the existing shape) so `_is_marker` no longer depends on a substring match.
- Keep the file format JSONL and append-only — only the *schema* of entries changes, not the transport.

**Part (b) — tree/branching (deferred; sketch only):**
- Add `id` + `parentId` to each message entry on write (`agent_instance_logger.py`).
- Extend the loader to reconstruct a chain by walking `parentId` instead of assuming line order, and add a "fork" operation that copies the suffix from a chosen node into a new instance log.
- This is where effort jumps (Med–High) and touches the core logging path on *every* write — which is why it's deferred.

### Migration / compat risk
- **(a) is low-risk and backward-compatible.** Old v1 logs have no version field → treated as v1 → migration pass normalizes in memory; nothing is rewritten on disk unless you explicitly migrate. Rollback = ignore the version field (old code never reads it). The content-sniffing marker change needs a one-time audit that no legitimate user message contains `--- CONTEXT COMPRESSED` (flag: I did not enumerate all logs for this — treat as a pre-implementation check).
- **(b) is higher-risk.** Adding `id`/`parentId` to every write touches the hot logging path (`_append_line`, `agent_instance_logger.py:221`) and the atomic rewrite paths (`rewrite_log_with_history`, `sync_compression_marker`). Existing logs lack IDs, so the loader must fall back to positional order for them — a permanent dual-path in the loader. Rollback is harder because branching data has no v1 equivalent.

### Effort
- (a): **Low–Med.** Isolated: one metadata field + a migration shim + hardening the marker check. Does not change the transport or the per-instance file model.
- (b): **Med–High.** Touches the core logging write path and adds a permanent dual-mode loader.

### Dependencies / ordering
- (a) is a prerequisite for (b) — you want the version header in place before adding structural fields.
- Independent of candidates 2, 3, 4.
- Do (a) early; it's cheap insurance and unblocks (b) later.

### Value added
Stops session recovery from silently mis-slicing on edge-case logs, and sets up the format so branching can be added later without a second migration.

---

## Candidate 2 — SQLite session backend + FTS full-text search (pi `packages/session-backends/sqlite-node/`)

### What it is
From pi's SQLite session repository: an optional **SQLite index** over sessions with lazily-built **FTS5** full-text search kept in sync by triggers. Pi's version is a *storage backend* (sessions live in SQLite). For AC the useful borrow is narrower: a **read-only search index** over the existing JSONL logs, not replacing the files as source of truth.

### Is this a real pain point for AC right now? — HONEST verdict
**Yes — and it's the strongest value/effort ratio of the four candidates.** Grounded in what I found:

- **Scale is real, not hypothetical.** Agent session logs are **workspace-scoped**, not repo-scoped: they live in `<workspace>/logs/` (e.g. `N:\work\WD\AgentWorkspace\logs/`), *not* in the AgentCascade repo's own `logs/` dir (which holds only console / shell-spillover / media files — 0 session `.jsonl`). The workspace log dir currently holds **~507 per-agent `.jsonl` files** (`orchestrator_*`, `coder_*`, `researcher_*`, `reviewer_*`), ranging from ~2 KB stubs to ~3.7 MB each — **~150 MB total** and growing with every session. This is one workspace; across workspaces it grows further.
- **There is no cross-session search today.** I grepped the entire `agent_cascade/` tree: **no `sqlite3`, no FTS, no full-text index** anywhere. The only "search" is `read_logs` (per-file) and `_extract_last_session` (per-file boundary). To find "what did we decide about sticky slots last week" or "which log has this exact traceback," the user must know *which file* to open. That's a real, recurring friction.
- **Resume is per-file and name-globbed.** `api_server.py:239` finds a session by globbing `*_{name}_*.jsonl` sorted by mtime — workable but brittle (relies on naming + newest-mtime), and there's no way to list/compare sessions by content.

**What SQLite actually buys AC (be precise, don't oversell):**
- **Cross-session full-text search** across all logs — the headline win. Genuinely new capability.
- **Queryable history / session listing** (filter by agent class, instance, date, token usage) — nice for triage and cost review.
- **Faster resume lookup** — *marginal.* Loading one known file is already fast; SQLite doesn't meaningfully speed up "load this one session." I'm flagging this so it's not sold as a benefit.

**What it does NOT buy:** it's not a storage migration, and it should not become the source of truth. JSONL stays canonical (it's append-only, human-readable, git-friendly); SQLite is a **derived index**. That framing keeps risk low and makes this additive.

### High-level plan
- Add an **isolated module** `agent_cascade/log_index/` (new package) — nothing in the existing logging path imports it on write. It's a separate concern.
- Define a small schema: `sessions(file_path, agent_class, instance_name, start_ts, last_update, n_messages, ...)` + a **FTS5 virtual table** over message content (role + text), keyed by file + line/offset so hits map back to the source JSONL.
- Build a **one-time import + incremental indexer**: scan the **workspace** log dir (`<workspace>/logs/*.jsonl` — where `AgentInstanceLogger` actually writes, e.g. `N:\work\WD\AgentWorkspace\logs/`; *not* the repo's own `logs/`), parse each (reuse `_parse_json_line` logic from `pool/session_io.py:398` or a thin copy), populate tables. The target dir must be resolved from the running instance's workspace path (respecting instance separation), not hard-coded. Incremental = track last-seen mtime/line-count per file; re-index only changed files.
- Keep it **in sync via a hook, not triggers on the file**: since JSONL is written by `AgentInstanceLogger`, add an optional callback (or a lightweight watcher) that marks a file dirty after `_append_line` / `rewrite_log_with_history`. Simpler and safer than trying to intercept writes.
- Expose search through the existing surface: a `/api/search?q=...` endpoint in `api_server.py` (fits todo #40's headless direction) and/or a CLI/GUI helper that returns file + line + snippet.
- Put it **behind a flag** (`enable_log_index`) so it can be turned off; default-on is fine once proven.

### Migration / compat risk
- **Very low — fully additive.** JSONL files are untouched and remain the source of truth. The SQLite DB is disposable: delete it → re-index from scratch. Rollback = set flag off / delete the `.db`. No data migration, no behavior change to logging or resume.
- Only real risks: index staleness (mitigated by the dirty-mark hook + a "rebuild all" escape hatch) and the one-time import cost on a large dir (one-off, backgroundable).

### Effort
**Med–High.** The module is isolated (that's what keeps it from being High), but FTS5 setup, incremental sync correctness, and mapping hits back to source lines take real care. Shape: **isolated module behind a flag**, not a core-logging change.

### Dependencies / ordering
- **None** — independent of all other candidates. Can be done first.
- Optional synergy with 4a/4b (expose search via the new protocol endpoints), but not required.

### Value added
"Search all my agent logs for X" becomes one query instead of opening dozens of files — a capability AC simply does not have today, at ~500 files and growing.

---

## Candidate 3 — Bash completion-fence sentinel (prime `rlm/bash.py`)

### What it is
From prime's `BashHandle`: each command gets a random token; the shell wrapper `printf`s a fence marker (`\x1e...complete:<a><b>\x1f`, bytes `0x1e`/`0x1f`) *after* the command. The parent scans the byte stream for that marker to declare the command complete **independent of process exit or pipe EOF** — specifically so a command that spawns a background child (which keeps the stdout pipe open) can't make "done" ambiguous.

### Is this a real pain point for AC right now? — HONEST verdict
**No — mostly theoretical for AC's current usage.** I read `async_shell_pkg/tracker.py` and found AC does **not** detect completion by EOF:

- Completion is detected in `_poll_loop` (`tracker.py:582`) by **`proc.poll() is not None`** (the *process* has exited) or timeout — line 605. It is explicitly re-checked every iteration to avoid the "exited right after sleep" race.
- After `proc.poll()` returns non-None, `_track_task` (`tracker.py:750`) sets `task.completed = True`, then **joins the stdout/stderr drain threads** with a bounded timeout (`DRAIN_THREAD_JOIN_TIMEOUT`, line 664) to flush remaining buffered output before building the completion message.
- So AC's model is "process exited + drained buffers," not "pipe closed." The specific ambiguity the fence fixes — *parent shell exits while a background child still holds the pipe* — would manifest in AC as: `proc.poll()` fires (shell process gone) → drain threads join (they finish when the Popen stdout handle closes, which is when **all** writers to that pipe close, i.e. including the orphaned child). 

That last point is the crux and I want to be precise rather than hand-wave: **if a background child inherits the shell's stdout fd, AC's drain-thread join can indeed block until that child closes its end**, which would delay (not corrupt) the completion message. So the fence *could* matter in exactly that scenario. But:
- AC already has a **bounded** join timeout (`DRAIN_THREAD_JOIN_TIMEOUT`), so worst case is a bounded delay, not a hang — and the command is already reported complete on `proc.poll()`.
- I did **not** observe this as an active bug; there's no report in `.agent_lessons/` or the logs of completion being delayed by background children. (Flag: I did not reproduce it.)

So the honest read: the fence converts a *bounded, rare* delay into a *deterministic* done-signal. For AC's current scale and usage that's robustness polish, not pain relief. **Skip** — revisit only if a concrete "async shell completion hangs/delays when the command spawns a background process" incident is observed.

### High-level plan (only if a real case emerges)
- Mint a per-run token in `AsyncShellTracker.launch` (`tracker.py:176`).
- Wrap the launched command with a post-exec fence `printf` (POSIX-first; best-effort/optional on Windows, matching AC's existing `ON_WINDOWS` handling in `async_shell_pkg/windows.py`).
- Scan the drain threads' output pump for the marker with cross-chunk retention (reuse the existing bounded-buffer tail logic).
- On marker sight, mark complete even if `proc.poll()` is still None; keep `proc.poll()` as the fallback.

### Migration / compat risk
Low in isolation (opt-in wrapper), but it changes what "complete" means on the core async-shell path — a change to behavior that other code (`__status`, heartbeats, completion messages) depends on. Not worth that surface area for a theoretical win.

### Effort
**Med.** Isolated to the launch/drain path, but touches the completion contract.

### Dependencies / ordering
None. Independent.

### Value added
Deterministic done-signal when a command spawns background children — but AC's current `proc.poll()` + bounded drain-join already covers the common case, so the marginal value is low today.

---

## Candidate 4 — Validated framed wire protocol + RPC verbs (steer/follow_up/clear_queue/abort) + version/capability gating (pi `packages/protocol/`, `docs/rpc.md`; prime `daemon-protocol.ts`)

### What it is
Two layered ideas:
- **(a) Protocol governance:** a versioned, schema-validated message contract with capability negotiation (clients check capabilities before using newer commands). From pi's frame/schema layer + prime's `DAEMON_PROTOCOL_VERSION`/capability maps.
- **(b) RPC verbs:** first-class `steer` (interrupt current turn), `follow_up` (queue for after the turn), `clear_queue`, `abort` (stop the run), with request/response `id` correlation. From pi `docs/rpc.md`.

This directly serves **todo line 40 (expand REST API for headless control)**.

### Is this a real pain point for AC right now? — HONEST verdict
**(b) steer/abort = yes, for anyone doing headless/automation. (a) full framed protocol = maybe; scope it down.**

Grounded in what I read:

- **Current control surface** (`api_server.py`): FastAPI with an X25519 handshake (`/api/handshake`, line 709) and an E2E-encrypted `/api/message` (line 735) that calls `agent_pool.enqueue_message(target, ...)` then starts a generation thread. Plus REST endpoints (`/api/status`, `/api/agents`, `/api/reset`, `/api/resume_all`, `/api/approve|reject`) and a WS chat channel.
- **No version/capability negotiation.** The handshake is purely cryptographic; there's no `API_VERSION` or capability list. For AC's *current* single-deployment, in-process REST this is fine — it's not an active pain. It becomes important the moment external/long-lived clients depend on endpoint shapes (which is exactly todo #40's direction). So (a) is **forward-looking**, not a present-day fix.
- **No steer-vs-follow-up distinction, no abort verb.** `enqueue_message` (`pool/message_queue.py:62`) just appends to a FIFO list. The engine drains the queue **at turn boundaries** — I confirmed in `engine/core.py:2115` (`_post_turn_checks`): after a turn completes it checks `has_messages` and loops back to process queued messages. So today, an injected message is inherently **follow-up** (delivered after the current LLM call finishes). There is no way to *steer* a running turn or *abort* one from the API. For interactive use the GUI "Stop" covers abort (`stop_session`, `session_io.py:143`), but that's not exposed as a clean headless verb, and steer doesn't exist at all.

So: **(b) is a real capability gap** for headless control (you can't programmatically say "interrupt what you're doing and do this" or "stop this run" through the API). **(a) is worth doing in lightweight form** as the substrate that makes (b)'s new verbs safe to add over time — but AC does not need pi's CBOR framing; a JSONL/JSON validation layer fits AC's text-based, in-process reality.

### High-level plan
**Part (a) — lightweight protocol governance (scope down from pi):**
- Add an `API_VERSION` constant + a **capability list** to the handshake response (`api_server.py:709`) and/or a `/api/capabilities` endpoint. Clients read capabilities before calling newer endpoints.
- Introduce a small **request/response schema layer** (typed, reject-unknown-fields) for the control verbs — *not* CBOR framing; keep JSON/JSONL to stay Pythonic and match AC's existing text transport. This is the borrowable structural pattern from pi `schemas.ts` (strict schemas + conformance tests), not the binary layout.
- Add **conformance tests** for the schema layer (the part of pi's protocol that's genuinely high-leverage and cheap).

**Part (b) — RPC verbs:**
- Model each verb explicitly rather than one opaque `enqueue`:
  - `follow_up` → current behavior (append to `message_queues`, drained at turn boundary in `engine/core.py`). No engine change.
  - `steer` → **the new part.** Requires the engine to check for a "steer" message *mid-turn* (e.g., before/after each LLM call in the loop, or by interrupting the current streaming call) and inject it ahead of the normal drain. This is the real work — it touches `engine/core.py`'s turn loop and/or `llm_call.py`.
  - `clear_queue` → wrap existing `dismiss_queue_message(instance, -1)` (`pool/message_queue.py:106`), which already clears all queued messages. Cheap.
  - `abort` → expose a clean stop verb mapping to the existing stop mechanism (`stop_session` / `_signal_stop`, `api_server.py:143`) so headless clients can halt a run deterministically.
- Add **`id` correlation** to request/response so a headless client can match an ack/terminal event to its call (pi's core ergonomics win).

### Migration / compat risk
- **(a) is low-risk and backward-compatible.** Adding a version/capability field to the handshake doesn't break existing clients (they ignore it). The schema layer starts by validating only the new verbs; existing `/api/message` keeps working. Rollback = stop advertising the capability.
- **(b) `steer` is the risky one** — it changes engine turn-loop behavior and must not break the existing follow-up drain, loop detection, or compression notification paths (`engine/core.py:305`, `_post_turn_checks`). Mitigate by making steer opt-in per call (default = follow_up) and gating it behind a capability flag. `clear_queue`/`abort` are low-risk wrappers over existing behavior.
- The E2E encryption on `/api/message` should be preserved for the new verbs (don't introduce unencrypted control paths).

### Effort
- (a): **Low–Med.** Mostly additive (version/capability + schema module + tests).
- (b): **Med**, driven entirely by `steer` (mid-turn injection into `engine/core.py`). `follow_up`/`clear_queue`/`abort` are near-free wrappers.

### Dependencies / ordering
- **(a) precedes (b)** — build the version/capability + schema substrate first, then add verbs as gated capabilities on top.
- Both serve todo #40; neither depends on candidates 1 or 2.

### Value added
Turns AC's REST API into a real headless control plane: programmatic steer/abort/queue-clear with correlated responses and safe versioning — the concrete substance of "expand REST API for headless control."

---

## Deferred / skipped (considered, not planned)

One line each so it's clear these were weighed:

- **Refinement governance (proposal→apply→rollback, versioned entries)** — *defer.* Strategically aligned with AC's self-improvement goal, but it wraps the skill/lesson writer and needs a versioned store + rollback tool; meaningful effort with value that only shows up over many iterations. Revisit after the higher-leverage items.
- **Cache-miss / cost-waste telemetry** — *defer (verify first).* AC's `llm/oai.py:179` already captures `prompt_tokens_details.cached_tokens`, so the data exists; a `cache_stats` pass over logged usage is Low–Med. But it's an observability nicety, not a pain point — do it only if cost waste is actually being felt.
- **Eval harness (judges + candidate-vs-baseline lift)** — *defer.* High effort, high long-term value for measuring prompt/skill/model changes, but AC has `benchmark/` and ad-hoc regression logs; the structured harness is a bigger build to defer until there's a concrete change to measure.
- **Message rate limiting + family reach** — *skip for now.* `send_message` (`pool/message_queue.py:57`) routes by name with no reach policy or per-destination limit, but at AC's current scale this isn't causing problems. Cheap to add later if agent-to-agent spam appears.
- **Persistent goal-state with budget** — *defer.* AC has `enable_agent_budgeting` but no turn-surviving `GoalState` object; Low–Med effort, useful for long-running goals but not a present pain.
- **Cron/interval scheduled prompts** — *defer.* No timed re-entry into a session today; would build on candidate 4's steer/follow_up verbs (so it naturally comes *after* 4b if ever wanted).
- **Token budgets on skills / model registry** — *skip for now.* Low-value at current scale; the model registry overlaps with todo #41 (per-agent-class reasoning-effort) and can be folded into that work rather than done as a separate borrow.

---

## Recommended sequencing (if you proceed)

1. **Candidate 2 — SQLite index + FTS.** Do first. Highest value/effort ratio, fully additive (no core-path risk), no dependencies. Immediately usable ("search all logs"), and it's the one that changes what the user can *do* rather than how internals work.
2. **Candidate 1a — session version header + migration.** Cheap, isolated, and hardens the one real fragility in `load_session_from_log` (content-sniffed boundaries). Establishes the format hook so 1b stays cheap later.
3. **Candidate 4a — lightweight protocol governance (version/capability + JSON schema layer + conformance tests).** Builds the safe substrate for headless control; low-risk and directly serves todo #40.
4. **Candidate 4b — RPC verbs (follow_up/clear_queue/abort first, steer last).** The near-free wrappers land quickly; `steer` (mid-turn injection into `engine/core.py`) is the riskiest piece, so it goes last and behind a capability flag.

**Defer:** 1b (tree/branching), refinement governance, cache-waste telemetry, eval harness, goal-state, cron prompts.
**Skip:** bash completion fence (revisit only on a real background-child incident), rate-limiting/family-reach, skill token budgets, standalone model registry (fold into todo #41).

**Why this order:** front-load the additive, high-value, low-risk work (2 → 1a) so you get usable capability fast with little blast radius; then build the headless-control foundation (4a → 4b) where the only genuinely risky change (`steer`) is isolated to the end and gated.

---

## Method / caveats
- Read-only on AC source; **no code was modified.** Only this plans file was written.
- Files actually read to ground verdicts: `pool/session_io.py` (full), `logger/agent_instance_logger.py` (structure + write paths), `async_shell_pkg/tracker.py` (`_poll_loop`, `_track_task`), `operation_manager/shell.py` (control commands), `api_server.py` (handshake, `/api/message`, endpoint list), `pool/message_queue.py` (queue semantics), `engine/core.py` (turn-boundary drain at line 2115), `telemetry.py` (`record_token_usage`), `llm/oai.py` (cache token capture), `prompts/dna.py` (`COMPRESSION_MARKER`).
- **Flagged uncertainties (not assumed):** (1) I did not reproduce the "background child delays async-shell completion" scenario — candidate 3's verdict rests on reading the code path, not observation. (2) I did not enumerate all logs for legitimate `--- CONTEXT COMPRESSED` substring collisions — treat as a pre-implementation check before changing the marker to be structural. (3) AC's exact behavior of drain-thread join when an orphaned child holds the stdout fd is inferred from the code, not measured.
