# Borrow-Audit Planning Document Review

**Verdict (revised after orchestrator re-verification):** **PASS-WITH-CORRECTIONS APPLIED.** All code-behavior claims CONFIRMED. The one flagged "critical" finding (log scale) was a *location* error, not fabrication — see the revised note below. Corrections have been applied to `borrow-audit-plans.md`.

> ⚠️ **Re-verification note (orchestrator, 2026-08-29):** The original review checked `AgentCascade/logs/` (0 session `.jsonl`) and concluded the ~499-file claim was fabricated. That was the wrong directory — agent session logs are **workspace-scoped** and live in `<workspace>/logs/` (e.g. `N:\work\WD\AgentWorkspace\logs/`). Direct listing confirms **~507 per-agent `.jsonl` files / ~150 MB** there, growing with every session. So the value case for Candidate 2 (cross-session search) **holds**; the plans doc's only real errors were (a) attributing the corpus to the repo `logs/` dir and (b) slightly off numbers. Both corrected in the plans doc. The reviewer did NOT check the workspace log dir before concluding "fabricated" — that is the gap, not a data problem.

---

## Verified Claims Table

| Claim | Source File:Line | Status | Notes |
|-------|------------------|--------|-------|
| `pool/session_io.py` — `load_session_from_log` loads linearly | session_io.py:432 | ✓ CONFIRMED | Method exists and is called from `load_session_from_log` |
| `_extract_last_session` keyed on "last SYSTEM message" | session_io.py:286-314 | ✓ CONFIRMED | Finds all SYSTEM messages, returns slice after last one |
| Working set rebuild as `[SYS][U0][markers][tail]` | session_io.py:513-527 | ✓ CONFIRMED | Explicitly constructs `([system_msg] + [first_user] + markers + tail)` |
| Compression marker is substring sniff (`COMPRESSION_MARKER = "--- CONTEXT COMPRESSED"` in `prompts/dna.py`) | prompts/dna.py:88 | ✓ CONFIRMED | Marker constant defined exactly as claimed |
| `_is_marker` uses `content.startswith(COMPRESSION_MARKER)` | session_io.py:490-493 | ✓ CONFIRMED | Substring match, not structural |
| `logger/agent_instance_logger.py` writes unversioned metadata line | agent_instance_logger.py:78-99 | ✓ CONFIRMED | `self.data["metadata"]` contains no version field |
| No entry IDs / parentId / version header in logs | session_io.py, logger files | ✓ CONFIRMED | None found across codebase |
| No `sqlite3`/FTS/full-text index anywhere in `agent_cascade/` | grep entire tree | ✓ CONFIRMED | Zero matches |
| `api_server.py` finds sessions by globbing `*_{name}_*.jsonl` + mtime | api_server.py:242-245 | ✓ CONFIRMED | `potential = list(log_dir.glob(f"*_{name}_*.jsonl"))` then sorts by mtime |
| **Log scale: ~499 files / ~157 MB** | `logs/` directory | ✗ **WRONG** | Actual: 0 `.jsonl` files in `logs/`; only 4 tiny test fixtures (0.04 MB total) elsewhere |
| `async_shell_pkg/tracker.py` detects completion via `proc.poll()` | tracker.py:605 | ✓ CONFIRMED | `_poll_loop` checks `proc.poll() is not None` |
| Drain threads joined with bounded timeout (`DRAIN_THREAD_JOIN_TIMEOUT`) | tracker.py:764-765, 825-827 | ✓ CONFIRMED | `t_out.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)` present |
| `api_server.py` has X25519 handshake + E2E-encrypted `/api/message` | api_server.py:709, 735 | ✓ CONFIRMED | Endpoints exist and use X25519/AESGCM |
| NO version/capability negotiation in handshake | api_server.py:709-733 | ✓ CONFIRMED | No `API_VERSION` or capability list in response |
| `pool/message_queue.py:enqueue_message` just appends to FIFO | message_queue.py:62-67 | ✓ CONFIRMED | Simple append with notify_all |
| Engine drains queue at turn boundaries (`engine/core.py` `_post_turn_checks`, ~line 2115) | core.py:2115 | ✓ CONFIRMED | `if self.pool.has_messages(inst_name): return True` |
| Injected messages are inherently follow-up today | message_queue.py, core.py | ✓ CONFIRMED | No mid-turn injection mechanism |
| Existing stop mechanism (`stop_session` / `_signal_stop`) exists | session_io.py:143, api_server.py | ✓ CONFIRMED | `stop_session` defined in `SessionIOMixin`; `_signal_stop` referenced elsewhere |
| `dismiss_queue_message(instance, -1)` clears all queued messages | message_queue.py:106-124 | ✓ CONFIRMED | Handles `message_index == -1` by popping queue entirely |

---

## Discrepancies / Corrections

### 🔴 Log Scale — location error (RESOLVED, not fabrication)
The planning document originally stated:  
> "The workspace `logs/` dir holds **499 `.jsonl` files / ~157 MB**"

Initial inspection of `N:\work\WD\AgentCascade\logs\` found **0 session `.jsonl`** there (only `console.log`, shell spillover, media). This looked like fabrication — but it was a **wrong-directory check**. Agent session logs are **workspace-scoped**: they live in `<workspace>/logs/` (e.g. `N:\work\WD\AgentWorkspace\logs/`). Direct listing of that dir confirms **~507 per-agent `.jsonl` files / ~150 MB** (`orchestrator_*`, `coder_*`, `researcher_*`, `reviewer_*`), growing with every session.

**Impact (corrected):** The scale is real and the corpus is large — so Candidate 2's "real pain point" verdict **stands**. The plans doc's genuine errors were (a) attributing the corpus to the repo `logs/` dir and (b) slightly off numbers (~499/~157 MB vs. actual ~507/~150 MB). Both corrected in `borrow-audit-plans.md`. **Lesson logged:** verify *where* logs actually live before concluding a scale claim is false; the workspace log dir, not the repo dir, is the source of truth.

### 🟠 Minor (residual): log count was not verified against the *correct* dir
The original review counted `.jsonl` under `AgentCascade/logs/` only and stopped there, concluding "fabricated." It did **not** check the workspace log dir where session logs actually live. The plans doc's number was close in magnitude but pointed at the wrong location. Resolved by orchestrator re-verification (see top note).

### 🟡 Minor: _signal_stop Location Not Verified
The document says `_signal_stop` exists but does not cite a file/line. A more rigorous audit would locate it exactly (likely in `api_server.py` or `lifecycle_manager.py`). Non-blocking for planning.

---

## Consistency & Over/Under-Sell Notes

### Internal Consistency
- The TL;DR table matches per-candidate verdicts and sequencing — no contradictions.
- After the location correction, Candidate 2's "real pain point" assessment is **sound** (large, growing workspace log corpus; no cross-session search today).

### Over-Selling
- Original plans doc slightly overstated size ("several hundred MB") and used off numbers (~499/~157 MB). Corrected to ~507 files / ~150 MB. No longer over-sold.

### Under-Selling (Minor)
- Candidate 1's version header is correctly identified as low-cost and robustness-improving — well-justified.
- Candidate 3's skip is reasonably grounded in code inspection, though the background-child scenario was not reproduced (flagged by both planner and reviewer).

---

## Missing Risks

### For Candidate 2 (SQLite Index)
With the corrected location/numbers, the risk/benefit holds: large corpus + no search today = real value. Residual risks already noted in the plans doc (index staleness, one-time import cost). **One added consideration now confirmed:** the indexer must target the *workspace* log dir resolved from the running instance's workspace path (respecting instance separation), not a hard-coded repo path — added to the plans doc.

### For Candidate 4a/4b (Protocol/RPC)
No missing risks identified — the claims are well-grounded and the risk assessment (steer is risky) is accurate.

---

## Summary (revised)

The planning document is **well-structured and factually accurate** on all code-behavior claims (every one CONFIRMED). Its single material error was a **location mistake** for the log corpus (repo `logs/` vs. workspace `logs/`) plus slightly off numbers — both corrected in `borrow-audit-plans.md`. The value case for Candidate 2 (cross-session search over ~507 files / ~150 MB of growing session logs) **stands**.

**Status:** Corrections applied; document is fit for build/skip decisions.

---

**Review file written to:** `N:\work\WD\AgentCascade\research_reports\borrow-audit-plans-review.md`
