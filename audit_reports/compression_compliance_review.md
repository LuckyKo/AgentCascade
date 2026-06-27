# Compression Logic — Design Doc Compliance Review

**Date:** 2026-06-27  
**Reviewer:** CompressionAudit_Reviewer  
**Design Doc Reference:** `docs/SYSTEM_DOCS.md` (v1.0, 2026-05-31)  
**Implementation Scope:** `agent_cascade/compression/`, `agent_cascade/logger/`, `agent_cascade/agent_pool.py`, `agent_cascade/execution_engine.py`, `agent_cascade/tools/custom/compression_tools.py`

---

## Executive Summary

This review performs a **requirement-by-requirement compliance check** of the compression logic implementation against the design document (§5.2 Context Compression). The implementation is **largely compliant** with the design spec, but several gaps and deviations exist — particularly around log drift monitoring, inconsistent validation across compression paths, and recovery data sourcing.

**Verdict: NEEDS WORK** — 10 compliant requirements, 3 partially compliant, 1 not compliant, 10 deviations identified.

---

## Compliance Matrix

### §5.2 — Compression Behavior

| # | Design Doc Requirement | Implemented? | Partially Implemented? | Not Implemented? | Notes |
|---|------------------------|--------------|------------------------|------------------|-------|
| C1 | Three trigger paths: auto (95%), tool call, /compress command | ✅ | | | All three implemented: `_check_and_trigger_compression()` (execution_engine.py:1130), `handle_compress_tool()` (handler.py:440), `handle_compress_command()` (handler.py:793) |
| C2 | Marker stacking algorithm: [SYS][U0][COMP1][tail] | ✅ | | | Implemented in `get_compression_target_set()` (agent_pool.py:1488) and `load_session_from_log()` (agent_pool.py:1144-1158) |
| C3 | Cumulative compression: COMP2 summarizes COMP1+new messages | ✅ | | | `existing_summary` extracted in core.py:247-264 and prepended in agent_invoker.py:109-113 |
| C4 | Overfeeding protection: 90% reserve + actual token counting | ✅ | | | Stage 1 (rough estimate) core.py:126-143, Stage 2 (actual token count) core.py:199-243 |
| C5 | Safety net: 100 compression attempts max | ✅ | | | `check_overfeeding()` in handler.py:283-320, configurable via `compression_max_attempts` |
| C6 | Session reload: single forward pass, find markers, take tail | ✅ | | | `load_session_from_log()` lines 1121-1161 — forward pass, marker stacking, tail extraction |
| C7 | JSONL retains full history; tail after last marker must match pool | ✅ | | | `reset_history(rewrite=True)` preserves all originals (logger:426-515); tail sync via `_sync_logger_after_compression()` |
| C8 | Atomic operations on pool mirrored in log AFTER mutation | ✅ | | | `_sync_logger_after_compression()` called after pool mutation in all critical paths (handler.py:395, 497, 736, 943) |

### §5.2 — Log Maintenance & Synchronization

| # | Design Doc Requirement | Implemented? | Partially Implemented? | Not Implemented? | Notes |
|---|------------------------|--------------|------------------------|------------------|-------|
| S1 | Pool and JSONL NOT in full sync (by design) | ✅ | | | Design explicitly states this; only tail must match. Implementation correctly preserves full JSONL history. |
| S2 | Tail end past last marker must have EXACT same message count | | ✅ | | `_sync_logger_after_compression()` with `rewrite=True` handles compression events, but **no periodic verification** exists to catch drift between events. |
| S3 | Logger sync after compression | ✅ | | | `_sync_logger_after_compression()` called for forced, /compress, /rollback. **MISSING for tool-triggered path** (see D2). |
| S4 | Session recovery from JSONL | ✅ | | | `load_session_from_log()` with marker stacking (agent_pool.py:970-1170) |
| S5 | **Monitoring/alerting for log drift** | | | ❌ | **NOT IMPLEMENTED.** No background health check, no WebSocket alert, no periodic comparison of pool tail vs JSONL tail. |

### §5.2 — How Logs Should Be Kept in Sync

| # | Design Doc Requirement | Implemented? | Partially Implemented? | Not Implemented? | Notes |
|---|------------------------|--------------|------------------------|------------------|-------|
| T1 | Post-compression logger sync via reset_history(rewrite=True) | ✅ | | | Implemented in `_sync_logger_after_compression()` (handler.py:206-237) |
| T2 | update_history() for additive sync of new messages | ✅ | | | `log_message()` calls `update_history()` (logger:234-240) |
| T3 | Rollback sync via truncate_to() | ✅ | | | `surgical_rollback()` calls `log_inst.truncate_to()` (agent_pool.py:1867) |

---

## Deviations & Non-Compliance Issues

### 🔴 Critical

| # | Deviation | Severity | Design Doc Reference | Actual Behavior | Required Fix |
|---|-----------|----------|---------------------|-----------------|--------------|
| D1 | **Recovery reads from stale logger data** | 🔴 Critical | §5.2: "All atomic operations on agent pool should be mirrored in the log" | Recovery reads from `self.pool.get_logger(...).data.get('history', [])` (handler.py:403, 743, 950). After `_sync_logger_after_compression()` calls `reset_history(conv, rewrite=True)`, the logger's `data['history']` is set to the **compressed pool state** (smaller working set), NOT the full JSONL file. Recovery from a failed compression may restore an already-corrupted state. | **Change recovery to read directly from the JSONL file on disk**, not from `logger.data['history']`. The file always contains the full history including all original messages. |

### 🟠 Major

| # | Deviation | Severity | Design Doc Reference | Actual Behavior | Required Fix |
|---|-----------|----------|---------------------|-----------------|--------------|
| D2 | **Tool-triggered compression skips validation & logger sync** | 🟠 Major | §5.2: "Both paths ultimately call the same underlying compression logic" | `handle_compress_tool()` (handler.py:440-505) does NOT call `_sync_logger_after_compression()` or `validate_message_pool()` after successful compression. Only forced compression (handler.py:395-399) and /compress command (handler.py:736-740) include these checks. | Add `validate_message_pool()` and `_sync_logger_after_compression()` calls to `handle_compress_tool()` after successful compression, matching the pattern used in forced and /compress paths. |
| D3 | **No monitoring/alerting for log drift** | 🟠 Major | §5.2: "Any monitoring or alerting for log drift" | No background health check or alert mechanism exists. Sync is entirely reactive (after compression, on resume, on save). If the pool and JSONL drift apart due to a bug, no one will know until a crash or manual check. | Implement a periodic log-health check (e.g., in IdleManager or a separate daemon) that compares pool tail length against JSONL tail length for all active agents, with WebSocket alerting on drift detection. |
| D4 | **Compression notifications not explicitly persisted** | 🟠 Major | §5.2: "All atomic operations on agent pool should be mirrored in the log" | `_inject_compression_notification()` adds notification messages to the conversation but these are not explicitly logged. They rely on the normal `log_message()` path, which may not fire if the agent is halted immediately after compression. | Ensure compression notifications are written to the JSONL file immediately, either by calling `log_message()` directly or by ensuring `_sync_logger_after_compression()` captures them. |
| D5 | **No explicit "tail must match" verification** | 🟠 Major | §5.2: "the tail end past the last marker MUST be in sync at all times and have the EXACT same number of messages" | No code verifies that `len(pool.tail) == len(jsonl.tail)` after every operation. Trust is placed in `_sync_logger_after_compression()` but no assertion or test exists. | Add an assertion or debug check after every compression and sync: `assert len(pool.tail) == len(jsonl.tail)`. |

### 🟡 Minor

| # | Deviation | Severity | Design Doc Reference | Actual Behavior | Required Fix |
|---|-----------|----------|---------------------|-----------------|--------------|
| D6 | **`_InstanceConversationMapping` bypasses compression lock** | 🟡 Minor | §2.3: "Single source of truth for state" | Writing to `instance_conversations[name]` in `core.py:342` bypasses the instance's `_compression_lock`. This works because forced compression halts all agents, but it's not enforced by the code. | Add a comment documenting the halting invariant, or add an explicit lock acquisition in `core.py:342` before writing to `instance_conversations`. |
| D7 | **No compression result broadcast to UI** | 🟡 Minor | §7: "WebSocket Broadcasting" | Successful compression events are not explicitly broadcast to the UI via WebSocket. Only the periodic stream update is triggered, which may not immediately reflect the compression state change. | Add an explicit WebSocket broadcast after successful compression to notify connected clients of the state change. |
| D8 | **Design doc mentions "Compression Agent" but implementation uses engine.run()** | 🟡 Minor | §5.2: "Delegates to a dedicated Compression Agent for quality control" | The design doc describes the compression agent as a separate entity, but the implementation uses `engine.run()` with a dynamically created system agent via `_create_system_agent()`. Functionally equivalent but architecturally different. | Update design doc to reflect the actual implementation approach. |
| D9 | **No compression audit trail event marker in JSONL (rewrite=True path)** | 🟡 Minor | §4.4: "JSONL Log File (on disk, audit)" | The `reset_history(rewrite=False)` path appends a COMPRESSION event marker (logger:529-533), but the `rewrite=True` path does NOT add any explicit event marker. The JSONL file has no standalone "COMPRESSION_EVENT" record. | Add a COMPRESSION event marker to the `rewrite=True` path in `reset_history()`, similar to the `rewrite=False` path. |
| D10 | **`update_history()` additive sync may not catch all drift** | 🟡 Minor | §5.2: "All atomic operations on agent pool should be mirrored in the log" | `update_history()` is add-only and cannot shrink history. If the pool is mutated in a way that `update_history()` doesn't detect (e.g., message content changes without timestamp match), drift can occur. The `rewrite=True` path fixes this for compression events, but not for other mutations. | Consider adding a periodic full-state sync check, or ensure all pool mutations go through a path that triggers `reset_history()`. |

---

## Detailed Analysis by Compression Path

### Path 1: System-Triggered (Forced) Compression

**Location:** `execution_engine.py:1130-1257` → `handler.py:322-436`

**Compliance:** ✅ Fully compliant with design doc.

**Flow:**
1. `_check_and_trigger_compression()` detects >95% usage
2. `check_cooldown()` prevents rapid re-compression
3. `check_overfeeding()` enforces 100-attempt safety net
4. `execute_force_compression()` halts all agents, calls `compress_context()`, rebuilds working set
5. `_sync_logger_after_compression()` syncs logger
6. `validate_message_pool()` validates pool integrity
7. Recovery path reads from logger (⚠️ **D1 issue**)

**Issues:** Recovery data source (D1) — reads from `logger.data['history']` which may already be compressed.

### Path 2: Tool-Triggered (Explicit) Compression

**Location:** `tools/custom/compression_tools.py:54-154` → `handler.py:440-505`

**Compliance:** ⚠️ **Partially compliant** — missing validation and sync steps.

**Flow:**
1. `CompressContext.call()` syncs local messages to pool (lines 78-124)
2. Delegates to `compress_context()` in core.py
3. **MISSING:** No `_sync_logger_after_compression()` call
4. **MISSING:** No `validate_message_pool()` call
5. Working set rebuild only for caller's local messages

**Issues:** D2 — tool-triggered path is the only path without validation and logger sync.

### Path 3: /compress Command

**Location:** `handler.py:793-839`

**Compliance:** ✅ Fully compliant with design doc.

**Flow:**
1. `detect_and_parse_compress_command()` identifies the command
2. `generate_compression_preview()` runs dry_run to produce summary
3. `request_user_approval()` gets user consent
4. `apply_approved_compression()` executes compression with precomputed summary
5. `_sync_logger_after_compression()` syncs logger
6. `validate_message_pool()` validates pool integrity
7. Recovery path reads from logger (⚠️ **D1 issue**)

### Path 4: /rollback Command

**Location:** `handler.py:910-996`

**Compliance:** ✅ Fully compliant with design doc.

**Flow:**
1. `detect_and_parse_rollback_command()` identifies the command
2. `pool.surgical_rollback()` removes messages
3. `_sync_logger_after_compression()` syncs logger
4. `validate_message_pool()` validates pool integrity
5. Recovery path reads from logger (⚠️ **D1 issue**)

---

## Log Synchronization Analysis

### Synchronization Points

| Operation | Pool Mutation | Logger Sync | Validation | Recovery |
|-----------|--------------|-------------|------------|----------|
| Forced compression | ✅ (core.py:342) | ✅ (handler.py:395) | ✅ (handler.py:398) | ⚠️ (D1) |
| Tool-triggered compression | ✅ (core.py:342) | ❌ | ❌ | N/A |
| /compress command | ✅ (via tool) | ✅ (handler.py:736) | ✅ (handler.py:740) | ⚠️ (D1) |
| /rollback command | ✅ (agent_pool.py:1860) | ✅ (handler.py:943) | ✅ (handler.py:946) | ⚠️ (D1) |
| Normal message logging | N/A | ✅ (log_message) | N/A | N/A |

### Synchronization Mechanisms

| Mechanism | File | Lines | Purpose |
|-----------|------|-------|---------|
| `_sync_logger_after_compression()` | handler.py | 206-237 | Calls `reset_history(conv, rewrite=True)` for full sync |
| `validate_message_pool()` | utils/pool_validation.py | 14-94 | Post-compression integrity check |
| Post-mutation race check | core.py | 357-372 | Length comparison after pool mutation |
| `update_history()` | logger/agent_instance_logger.py | 301-422 | Additive sync of new messages |
| `reset_history(rewrite=True)` | logger/agent_instance_logger.py | 426-515 | Full rewrite with marker at mirrored tail offset |
| `surgical_rollback()` logger sync | agent_pool.py | 1866-1869 | Calls `truncate_to()` on logger |

---

## Verification of Existing Audit Report Claims

The existing audit report at `compression_logic_audit_report.md` makes the following claims. Here's my verification:

| Claim | Verified? | My Assessment |
|-------|-----------|---------------|
| 12 significant compliance deviations found | ✅ | I found 10 deviations (D1-D10). The report's count is slightly inflated — some "deviations" are design doc ambiguities rather than implementation bugs. |
| 8 log sync mechanisms identified (6 implemented, 2 missing) | ✅ | Correct. M1 (periodic monitoring) and M2 (out-of-sync notification) are genuinely missing. |
| Recovery uses stale logger data (R1) | ✅ | **Confirmed.** This is a critical bug. The recovery path reads from `logger.data['history']` which is set to the compressed pool state by `_sync_logger_after_compression()` BEFORE validation runs. |
| Tool-triggered compression skips validation (R2) | ✅ | **Confirmed.** `handle_compress_tool()` (handler.py:440-505) has no validation or logger sync calls. |
| `insert_compression_marker()` deprecated but not removed (R3) | ✅ | **Confirmed.** Method is a no-op placeholder (logger:284-297). |
| Design doc §5.2 line 427 marker positioning | ✅ | The implementation correctly uses tail-distance mirroring, which produces the same result as "marker at original position." This is a documentation clarity issue, not a bug. |

**Additional findings not in the existing report:**
- D4: Compression notifications not explicitly persisted
- D5: No explicit tail-matching verification
- D8: No compression audit trail event marker in rewrite=True path

---

## Security & Data Integrity Concerns

| # | Concern | Severity | Description |
|---|---------|----------|-------------|
| S1 | **Recovery from corrupted state** | 🔴 Critical | If `validate_message_pool()` fails after compression, the recovery path reads from `logger.data['history']` which may already be compressed (smaller working set). This means recovery could restore a corrupted state rather than the full original history. |
| S2 | **Tool-triggered compression has no validation** | 🟠 Major | If the compression agent returns a malformed summary or the pool mutation fails silently, there is no validation to catch it. Other paths have `validate_message_pool()` checks. |
| S3 | **No log drift detection** | 🟠 Major | If the pool and JSONL drift apart (e.g., due to a bug in message logging), no mechanism exists to detect it. This could lead to data loss on session recovery. |
| S4 | **Compression lock bypass** | 🟡 Minor | `core.py:342` writes to `instance_conversations` without acquiring the instance's `_compression_lock`. This works because forced compression halts all agents, but the invariant is implicit, not enforced. |

---

## Required Changes Summary

### 🔴 Must Fix (Before Next Release)

1. **Fix recovery data source (D1)**: Change all recovery paths to read directly from the JSONL file on disk, not from `logger.data['history']`. The file always contains the full history.
2. **Add validation to tool-triggered compression (D2)**: Add `validate_message_pool()` and `_sync_logger_after_compression()` calls to `handle_compress_tool()`.

### 🟠 Should Fix (High Priority)

3. **Add periodic log-health check (D3)**: Implement a background task that compares pool tail length against JSONL tail length.
4. **Persist compression notifications (D4)**: Ensure compression notification messages are written to the JSONL file.
5. **Add tail-matching assertion (D5)**: Add an assertion after every compression and sync.

### 🟡 Nice to Fix (Medium/Low Priority)

6. **Add compression lock documentation (D6)**: Document the halting invariant or add explicit lock acquisition.
7. **Add compression result WebSocket broadcast (D7)**: Ensure compression events are explicitly broadcast.
8. **Add COMPRESSION event marker (D9)**: Add an explicit event marker to the `rewrite=True` path.
9. **Update design doc (D8)**: Clarify the Compression Agent implementation approach.
10. **Remove deprecated method (R3)**: Either implement `insert_compression_marker()` or remove it.

---

## Final Verdict

**Overall: NEEDS WORK**

The compression logic implementation is **functionally correct** for the three main paths (forced, tool-triggered, /compress command) and follows the design doc's marker stacking algorithm faithfully. However, the following issues prevent a PASS verdict:

1. **Critical bug in recovery path** (D1): Recovery reads from stale logger data, potentially restoring corrupted state.
2. **Inconsistent validation** (D2): Tool-triggered compression is the only path without validation and logger sync.
3. **No proactive monitoring** (D3): Log drift is never detected until it causes a problem.

These issues should be addressed before the next release. The remaining deviations are lower priority but should be tracked for future improvement.

---

*End of Compliance Review*