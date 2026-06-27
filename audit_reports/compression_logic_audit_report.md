# Compression Logic Audit Report — AgentCascade_unified

**Date:** 2026-06-27  
**Auditor:** CompressionAudit_Researcher  
**Scope:** `N:\work\WD\AgentCascade_unified\agent_cascade`  
**Design Doc Reference:** `docs/SYSTEM_DOCS.md` (v1.0, 2026-05-31)

---

## Executive Summary

This audit examines the compression logic across three dimensions: **log synchronization checks**, **compression rebuild logic**, and **design document compliance**. The codebase implements a sophisticated multi-layered compression system with built-in validation, recovery, and logging mechanisms. Overall, the implementation is robust and well-structured, but several gaps and deviations from the design document were identified.

**Key Findings:**
- 12 significant compliance deviations found
- 8 log sync mechanisms identified (6 implemented, 2 missing)
- Compression rebuild logic is correctly implemented but has edge-case recovery gaps

---

## 1. Log Synchronization Checks

### 1.1 Mechanisms Identified

The codebase implements the following log synchronization and validation mechanisms:

| # | Mechanism | Location | Description |
|---|-----------|----------|-------------|
| 1 | **`_sync_logger_after_compression()`** | `compression/handler.py:206-237` | Unified helper that calls `reset_history(conv, rewrite=True)` on the logger after every compression path (forced, tool, /compress command, /rollback). This is the **primary** sync mechanism. |
| 2 | **`validate_message_pool()`** | `utils/pool_validation.py:14-94` | Post-compression validation that checks: (a) pool not empty, (b) first message is SYSTEM, (c) no excessive duplicate consecutive messages (>10% threshold), (d) all roles are valid strings, (e) no unexpected types (bool/None). Called after all compression paths. |
| 3 | **Post-mutation race condition check** | `compression/core.py:357-372` | After pool mutation, re-reads conversation and compares length. If mismatch detected, compression is aborted with "Concurrent modification detected" error. |
| 4 | **Logger `update_history()`** | `logger/agent_instance_logger.py:301-422` | Additive sync that matches messages by timestamp identity, performs surgical merge, and rewrites when content changes. Uses `_file_history_synced` flag to prevent duplicate loads. |
| 5 | **Logger `reset_history(rewrite=True)`** | `logger/agent_instance_logger.py:426-515` | Full rewrite mode that reads existing log, inserts the new marker at a mirrored tail offset, and overwrites the file. Preserves all original messages while adding the new marker. |
| 6 | **`_InstanceConversationMapping` lazy sync** | `agent_pool.py:33-75` | Version-based lazy sync of `instance_conversations` mapping. Uses `_instances_version` counter to detect when instances have changed and triggers `_sync_from_instances()`. |
| 7 | **API Server recovery on resume** | `api_server.py:1534-1600` | When resuming a session, validates each sub-agent's conversation with `validate_message_pool()`. If invalid, reads directly from JSONL log files on disk and restores. |
| 8 | **API Server history sync on save** | `api_server.py:309-320` | When saving session history, calls `logger_inst.update_history(history)` and sets `_file_history_synced = True`. |

### 1.2 Missing Sync Mechanisms

| # | Gap | Severity | Description |
|---|-----|----------|-------------|
| M1 | **No periodic log-health monitoring** | Medium | No background daemon or scheduled check compares pool state against JSONL files. Sync only happens reactively (after compression, on resume, on save). |
| M2 | **No explicit "out of sync" notification/alert** | Medium | When `validate_message_pool()` fails or recovery occurs, the event is logged but there is no WebSocket broadcast or UI notification informing the user that logs were out of sync. |

### 1.3 Locking & Concurrency Safety

- **`_compression_lock`** (RLock) is used consistently across all pool mutations, compression operations, and logger accesses.
- The `instance_conversations` mapping uses version-based lazy sync to avoid O(n) work during streaming.
- **Potential issue**: In `compression/core.py:342`, the pool is mutated via `agent_pool.instance_conversations[target_agent_name] = new_history`, but this bypasses the `_compression_lock` on the target instance. The lock is held by the caller (forced compression halts all other agents), but this is an implicit assumption, not enforced by the code.

---

## 2. Compression Rebuild Logic

### 2.1 Full Flow Trace

```
Trigger Sources:
  1. System-triggered (forced): _check_and_trigger_compression() [execution_engine.py:1130]
     → Checks actual_tokens vs allocated_max
     → Calls _force_compression() [execution_engine.py:1242]
     → compression_handler.check_cooldown() → check_overfeeding() → execute_force_compression()

  2. Tool-triggered (explicit): compress_context tool [tools/custom/compression_tools.py]
     → Syncs local messages to pool (lines 83-124)
     → Delegates to compress_context() [core.py]

  3. User command (/compress): handle_compress_command() [handler.py:793]
     → detect_and_parse_compress_command()
     → generate_compression_preview() (dry_run)
     → request_user_approval()
     → apply_approved_compression()

  4. User command (/rollback): handle_rollback_command() [handler.py:910]
     → detect_and_parse_rollback_command()
     → pool.surgical_rollback()
     → validate + recover + sync
```

### 2.2 Compression Core Flow (`core.py`)

```
compress_context():
  0. Validate fraction range (0.0-1.0)
  0b. Validate manual mode has summary_text
  1.  get_compression_target_set() → (active_start_idx, active_set, latest_summary_idx)
  2.  Guard: <3 messages AND <200 tokens → skip
  3.  compute_discard_count() → target_discard_count (with tool-chain refinement)
  3b. Cap discard count using compressor's max_input_tokens * 0.9 / 500
  4a. Guard: <3 messages → skip
  4b. Guard: not enough to discard (unless force=True)
  5.  Force mode guard: discard_count == 0 → fail
  6.  Determine target_messages (with existing summary marker if applicable)
  6b. TRUE overfeeding detection: actual token count vs available_for_messages
  7.  Extract existing_summary from latest marker
  8.  Generate summary (precomputed / manual / invoke_compression_agent)
  9.  Build marker message via build_marker_message()
  10. Apply to pool: copy-and-replace at insert_pos
  10b. Post-mutation race check (length comparison)
  11. Calculate tail_count
  12. Return CompressResult
```

### 2.3 Post-Compression Rebuild

After any successful compression, the following rebuild steps occur:

1. **`_rebuild_working_set()`** (execution_engine.py:1276): Replaces both `messages` and `llm_messages` lists with deepcopies of pool state. Invalidates token caches and LLM preprocessing cache.

2. **`_sync_logger_after_compression()`** (handler.py:206): Calls `reset_history(conv, rewrite=True)` on the logger.

3. **`validate_message_pool()`** (handler.py:398): Validates the compressed pool. If invalid, attempts recovery from logger's history.

4. **`push_periodic_update()`** (handler.py:424): Forces immediate stream update to UI.

### 2.4 Recovery Path

When `validate_message_pool()` fails after compression:

```
Pool Invalid → Read logger.data['history'] → Validate recovered data
  → If valid: instance.rebuild_conversation() + _rebuild_working_set() + _sync_logger_after_compression()
  → If invalid: notification + halt agent
```

### 2.5 Identified Issues

| # | Issue | Severity | Location | Description |
|---|-------|----------|----------|-------------|
| R1 | **Recovery uses stale logger data** | Medium | `handler.py:403,743,950` | Recovery reads from `self.pool.get_logger(...).data.get('history', [])`. After `_sync_logger_after_compression()` calls `reset_history(conv, rewrite=True)`, the logger's `data['history']` is set to the compressed pool state (smaller working set), NOT the full JSONL file. This means recovery from a failed compression may restore an already-corrupted state. |
| R2 | **No recovery for tool-triggered compression in handler** | Low | `handler.py:440-505` | The `handle_compress_tool()` method does NOT call `_sync_logger_after_compression()` or `validate_message_pool()` after successful compression. Only the `/compress` command path and forced compression path include these checks. |
| R3 | **`insert_compression_marker()` is deprecated but not removed** | Low | `logger/agent_instance_logger.py:284-297` | Method is a no-op placeholder. Should be removed or flagged as deprecated in docstrings. |

---

## 3. Design Document Compliance

### 3.1 Requirements Extracted from SYSTEM_DOCS.md

The design document specifies the following compression-related requirements:

#### 3.1.1 Compression Behavior (§5.2)

| Req # | Requirement | Status | Notes |
|-------|-------------|--------|-------|
| C1 | Three trigger paths: auto (95%), tool call, /compress command | ✅ Compliant | All three implemented |
| C2 | Marker stacking algorithm: [SYS][U0][COMP1][tail] | ✅ Compliant | Implemented in `get_compression_target_set()` and `load_session_from_log()` |
| C3 | Cumulative compression: COMP2 summarizes COMP1+new messages | ✅ Compliant | `existing_summary` is extracted and prepended in `agent_invoker.py:109-113` |
| C4 | Overfeeding protection: 90% reserve + actual token counting | ✅ Compliant | Stage 1 (rough estimate) and Stage 2 (actual token count) both implemented |
| C5 | Safety net: 100 compression attempts max | ✅ Compliant | `check_overfeeding()` in handler.py:283-320 |
| C6 | Session reload: single forward pass, find markers, take tail | ✅ Compliant | `load_session_from_log()` lines 1121-1161 |
| C7 | JSONL retains full history; tail after last marker must match pool | ✅ Compliant | `reset_history(rewrite=True)` preserves all originals |
| C8 | Atomic operations on pool mirrored in log AFTER mutation | ✅ Compliant | `_sync_logger_after_compression()` called after pool mutation |

#### 3.1.2 Log Maintenance & Synchronization (§5.2, §6.2)

| Req # | Requirement | Status | Notes |
|-------|-------------|--------|-------|
| S1 | Pool and JSONL NOT in full sync (by design) | ✅ Compliant | Design explicitly states this; only tail must match |
| S2 | Tail end past last marker must have EXACT same message count | ⚠️ Partially Compliant | The `_sync_logger_after_compression()` with `rewrite=True` handles this for compression events, but there is no periodic verification |
| S3 | Logger sync after compression | ✅ Compliant | `_sync_logger_after_compression()` called for forced, /compress, /rollback |
| S4 | Session recovery from JSONL | ✅ Compliant | `load_session_from_log()` with marker stacking |
| S5 | Monitoring/alerting for log drift | ❌ **NOT COMPLIANT** | No monitoring or alerting exists (Gap M1, M2) |

#### 3.1.3 How Logs Should Be Kept in Sync (§5.2)

| Req # | Requirement | Status | Notes |
|-------|-------------|--------|-------|
| T1 | Post-compression logger sync via reset_history(rewrite=True) | ✅ Compliant | Implemented |
| T2 | update_history() for additive sync of new messages | ✅ Compliant | `log_message()` calls `update_history()` |
| T3 | Rollback sync via truncate_to() | ✅ Compliant | `surgical_rollback()` calls `log_inst.truncate_to()` |

### 3.2 Deviations and Non-Compliance Issues

| # | Deviation | Severity | Design Doc Reference | Actual Behavior |
|---|-----------|----------|---------------------|-----------------|
| D1 | **No monitoring/alerting for log drift** | **High** | §5.2: "Any monitoring or alerting for log drift" | No background health check or alert mechanism exists. Sync is entirely reactive. |
| D2 | **Tool-triggered compression skips validation** | **Medium** | §5.2: "Both paths ultimately call the same underlying compression logic" | `handle_compress_tool()` does NOT call `validate_message_pool()` or `_sync_logger_after_compression()`. Only forced and /compress paths include these checks. |
| D3 | **Overfeeding detection in core.py not in handler.py** | **Low** | §5.2: "TRUE overfeeding detected → Compression fails gracefully" | True overfeeding is detected in `core.py:199-243` but the handler's `check_overfeeding()` (line 283) only counts total compression attempts, not actual token overflow. The design doc conflates these two concepts. |
| D4 | **Compression notification sent to LLM but not persisted** | **Medium** | §5.2: "All atomic operations on agent pool should be mirrored in the log" | `_inject_compression_notification()` adds notification messages to the conversation but these are not explicitly logged. They rely on the normal `log_message()` path, which may not fire if the agent is halted immediately after. |
| D5 | **No explicit "tail must match" verification** | **Medium** | §5.2: "the tail end past the last marker MUST be in sync at all times and have the EXACT same number of messages" | No code verifies that `len(pool.tail) == len(jsonl.tail)` after every operation. Trust is placed in `_sync_logger_after_compression()` but no assertion or test exists. |
| D6 | **`_InstanceConversationMapping` bypasses compression lock** | **Medium** | §2.3: "Single source of truth for state" | Writing to `instance_conversations[name]` in `core.py:342` bypasses the instance's `_compression_lock`. This works because forced compression halts all agents, but it's not enforced by the code. |
| D7 | **Design doc mentions "Compression Agent" but implementation uses engine.run()** | **Low** | §5.2: "Delegates to a dedicated Compression Agent for quality control" | The design doc describes the compression agent as a separate entity, but the implementation uses `engine.run()` with a dynamically created system agent. This is functionally equivalent but architecturally different from what's described. |
| D8 | **No compression result broadcast to UI** | **Low** | §7: "WebSocket Broadcasting" | Successful compression events are not explicitly broadcast to the UI via WebSocket. Only the periodic stream update is triggered, which may not immediately reflect the compression state change. |
| D9 | **Design doc §5.2 line 427 says "tail after last marker" but code uses `last_marker_index + 1`** | **Low** | §5.2: "takes the tail after the last marker" | The code correctly implements this, but the marker itself IS included in the working set (markers are stacked). The description could be clearer about whether the marker is part of the "tail" or separate. |
| D10 | **No compression audit trail in JSONL** | **Low** | §4.4: "JSONL Log File (on disk, audit)" | While compression markers are logged, there is no explicit "COMPRESSION_EVENT" event marker in the JSONL file (the deprecated `insert_compression_marker` would have added one). The `reset_history(rewrite=False)` path appends a COMPRESSION event marker, but `rewrite=True` does not. |
| D11 | **Design doc mentions "marker at original position" but implementation uses tail-distance mirroring** | **Low** | §5.2: "marker at original position" | The design doc says the marker is inserted "at original position" in JSONL. The implementation uses tail-distance mirroring (`insert_pos = len(existing_msgs) - actual_tail_count`), which produces the same result but through a different calculation. |
| D12 | **`update_history()` additive sync may not catch all drift** | **Medium** | §5.2: "All atomic operations on agent pool should be mirrored in the log" | `update_history()` is add-only and cannot shrink history. If the pool is mutated in a way that `update_history()` doesn't detect (e.g., message content changes without timestamp match), drift can occur. The `rewrite=True` path fixes this for compression events, but not for other mutations. |

---

## 4. Architecture Observations

### 4.1 Strengths

1. **Layered validation**: Three independent validation layers exist — pre-computation guards (fraction, discard count), post-mutation checks (race condition), and post-compression validation (`validate_message_pool()`).

2. **Recovery from failure**: All critical compression paths include a recovery mechanism that reads from the logger's history and attempts to restore a valid pool state.

3. **Overfeeding protection**: Two-stage protection (rough estimate pre-filter + actual token counting) is well-implemented and includes a safety net counter.

4. **Tool-chain preservation**: The `_refine_tool_call_boundary()` function correctly handles ASSISTANT→FUNCTION pair splitting, preventing orphaned tool calls.

5. **Version-based lazy sync**: The `_InstanceConversationMapping` uses a version counter to avoid O(n) work during streaming, a smart performance optimization.

### 4.2 Concerns

1. **Recovery data source**: As noted in R1, recovery reads from `logger.data['history']` which may already be corrupted if the sync happened before the validation failed. The recovery should read directly from the JSONL file on disk.

2. **Inconsistent validation coverage**: Tool-triggered compression (`handle_compress_tool`) lacks the validation and sync steps present in forced compression and /compress command paths.

3. **No proactive health monitoring**: Log sync is entirely reactive. A proactive check (e.g., comparing pool tail length against JSONL tail length on each broadcast) would catch drift before it causes issues.

4. **Complexity of `update_history()`**: The surgical merge logic in `update_history()` (lines 339-422) is complex and has multiple edge cases. The comment about "timestamp identity matching" is critical but easy to miss.

---

## 5. Recommendations

### 5.1 High Priority

1. **Add periodic log-health check**: Implement a background task (e.g., in IdleManager or as a separate daemon) that periodically compares pool tail length against JSONL tail length for all active agents.

2. **Add UI notification for log sync events**: When recovery occurs or validation fails, broadcast a WebSocket event to inform the user.

3. **Fix recovery data source**: Change recovery paths to read directly from the JSONL file on disk rather than from `logger.data['history']`.

### 5.2 Medium Priority

4. **Add validation to tool-triggered compression**: Add `validate_message_pool()` and `_sync_logger_after_compression()` calls to `handle_compress_tool()`.

5. **Add explicit tail-length assertion**: After every compression and sync, add an assertion that `len(pool.tail) == len(jsonl.tail)`.

6. **Add COMPRESSION_EVENT marker to rewrite=True path**: Ensure the JSONL file always has an explicit event marker for compression events, regardless of the sync path.

### 5.3 Low Priority

7. **Remove deprecated `insert_compression_marker()`**: Either implement it properly or remove it entirely.

8. **Update design doc**: Clarify the distinction between the two overfeeding detection mechanisms and the marker positioning approach.

9. **Add compression result WebSocket broadcast**: Ensure successful compression events are explicitly broadcast to connected clients.

---

## 6. File Index

| Component | Primary File(s) | Lines of Interest |
|-----------|----------------|-------------------|
| Compression Core | `compression/core.py` | Full file (394 lines) |
| Compression Handler | `compression/handler.py` | 206-237 (sync), 283-320 (overfeeding), 322-436 (forced), 440-505 (tool), 793-839 (command) |
| Compression Agent Invoker | `compression/agent_invoker.py` | Full file (289 lines) |
| Compression Helpers | `compression/helpers.py` | 190-236 (discard), 239-257 (marker), 260-301 (rebuild) |
| Logger | `logger/agent_instance_logger.py` | 284-297 (deprecated), 301-422 (update), 426-554 (reset) |
| Agent Pool | `agent_pool.py` | 33-75 (mapping), 1456-1570 (compression compat), 1784-1797 (find_last_marker), 1799-1871 (rollback), 970-1220 (load_session) |
| Execution Engine | `execution_engine.py` | 1130-1188 (check_trigger), 1189-1240 (pre_llm), 1242-1257 (force), 1276-1334 (rebuild) |
| Pool Validation | `utils/pool_validation.py` | Full file (94 lines) |
| Compression Tool | `tools/custom/compression_tools.py` | Full file (154 lines) |
| API Server | `api_server.py` | 309-320 (save sync), 1534-1600 (resume recovery) |

---

*End of Report*