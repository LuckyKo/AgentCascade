# Compression Bug Fix Implementation Plan

## Overview
This document provides a detailed, line-by-line implementation plan for fixing the three compression bugs identified in root cause analysis. Fixes are ordered by dependency — each fix builds on previous ones.

---

## Bug Summary

| Bug | Root Cause | Symptom |
|-----|-----------|---------|
| **Bug 1** | Pool validation fails after compression due to logger duplicates | Pool doubles from ~30 → ~62 messages during recovery |
| **Bug 2** | Compression marker + notification are both USER role messages | OpenAI API alternation violation (consecutive USER messages) |
| **Bug 3** | Logger sync happens AFTER validation and recovery | Recovery reads stale logger data with pre-compression duplicates |

---

## Fix Order & Dependencies

```
Fix 1 → Fix 2 → Fix 3 → Fix 4 → Fix 5
(Logger sync   (Notification  (Duplicate    (Marker      (Recovery
 before        pattern)       accumulation) position)     logger sync)
 validation)
```

---

## FIX 1: Move Logger Sync Before Validation

**File:** `agent_cascade/compression/handler.py`
**Bug addressed:** Bug 3 (Logger sync too late), which contributes to Bug 1 (pool doubling)

### Problem
In the forced compression path, `_sync_logger_after_compression` runs at line 313, AFTER validation (line 285-286) and recovery (lines 289-310). When recovery reads the logger at line 290, it gets stale pre-compression data.

### Current Flow (handler.py lines 235-313)
```
Compress → Rebuild working set → Inject notification USER msg → Validate → [Recovery if fail] → Sync logger
```

### New Flow
```
Compress → Rebuild working set → Inject notification USER msg → Sync logger → Validate → [Recovery if fail]
```

### Exact Changes — Forced Compression Path

**Location:** `handler.py` lines 281-315

Change the order: move `_sync_logger_after_compression` call from line ~313 to right before validation at line ~285.

**OLD (lines 280-316):**
```python
                    # Re-fetch conv after notification append so validation includes the notification message
                    conv = self.pool.get_conversation(inst_name)

                    # Item 10: Validate message pool after forced compression (now includes notification)
                    from agent_cascade.utils.pool_validation import validate_message_pool
                    if not validate_message_pool(conv, inst_name):
                        logger.error(f"[MSG POOL VALIDATION] Pool invalid after forced compression for '{inst_name}'. Attempting recovery from log...")
                        # Recovery: reload from the logger's history (which is unaffected)
                        try:
                            recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                            if recov and validate_message_pool(recov, inst_name):
                                instance.rebuild_conversation(list(recov))
                                self.engine._rebuild_working_set(messages, llm_messages, inst_name)
                                logger.info(f"Recovered message pool from log for '{inst_name}' ({len(recov)} messages)")
                                conv = recov
                                # Set cooldown flag after successful recovery (compression occurred)
                                instance._suppress_loop_detection_next_turn = True
                            else:
                                logger.error("Recovery from log also failed — message pool may be corrupted")
                                notification_text = f"[SYSTEM] Compression corrupted pool: Forced compression and recovery both failed for {inst_name}. Agent halted to prevent corruption."
                                notif_msg = Message(role=USER, content=notification_text)
                                instance.append_message(notif_msg)
                                if response is not None:
                                    response.append(notif_msg)
                                self.pool.halt_instance(inst_name)
                        except Exception as e:
                            logger.error(f"Recovery attempt failed for '{inst_name}': {e}")

                    # Item 11: Sync logger state to match pool after forced compression
                    self._sync_logger_after_compression(inst_name, instance.agent_class, "forced compression")
                    # Set cooldown flag to suppress loop detection on next turn after compression
                    instance._suppress_loop_detection_next_turn = True
```

**NEW:**
```python
                    # Re-fetch conv after notification append so validation includes the notification message
                    conv = self.pool.get_conversation(inst_name)

                    # ── FIX 1: Sync logger BEFORE validation ──────────────────────
                    # This ensures that if recovery is needed, the logger already has
                    # the clean compressed state instead of stale pre-compression data.
                    self._sync_logger_after_compression(inst_name, instance.agent_class, "forced compression")

                    # Item 10: Validate message pool after forced compression (now includes notification)
                    from agent_cascade.utils.pool_validation import validate_message_pool
                    if not validate_message_pool(conv, inst_name):
                        logger.error(f"[MSG POOL VALIDATION] Pool invalid after forced compression for '{inst_name}'. Attempting recovery from log...")
                        # Recovery: reload from the logger's history (now synced with compressed state)
                        try:
                            recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                            if recov and validate_message_pool(recov, inst_name):
                                instance.rebuild_conversation(list(recov))
                                self.engine._rebuild_working_set(messages, llm_messages, inst_name)
                                logger.info(f"Recovered message pool from log for '{inst_name}' ({len(recov)} messages)")
                                conv = recov
                            else:
                                logger.error("Recovery from log also failed — message pool may be corrupted")
                                notification_text = f"[SYSTEM] Compression corrupted pool: Forced compression and recovery both failed for {inst_name}. Agent halted to prevent corruption."
                                notif_msg = Message(role=USER, content=notification_text)
                                instance.append_message(notif_msg)
                                if response is not None:
                                    response.append(notif_msg)
                                self.pool.halt_instance(inst_name)
                        except Exception as e:
                            logger.error(f"Recovery attempt failed for '{inst_name}': {e}")

                    # Set cooldown flag to suppress loop detection on next turn after compression
                    instance._suppress_loop_detection_next_turn = True
```

### Exact Changes — /compress Command Path

**Location:** `handler.py` lines 643-675 (inside `apply_approved_compression`)

Move the `_sync_logger_after_compression` call from line 675 to before validation at line ~643:

**OLD order:** Validate → Recovery → Rebuild → Sync logger
**NEW order:** Sync logger → Validate → Recovery → Rebuild

Insert this BEFORE line 643 (before `# Validate message pool after compression`):
```python
            # ── FIX 1: Sync logger BEFORE validation (same as forced compression path) ──
            self._sync_logger_after_compression(inst_name, instance.agent_class, "/compress command")
```

Remove the duplicate `_sync_logger_after_compression` call at line ~675.

### Exact Changes — /rollback Command Path

**Location:** `handler.py` lines 849-879 (inside `handle_rollback_command`)

Insert logger sync before validation:

**OLD order:** Validate → Recovery → Rebuild → Sync logger
**NEW order:** Sync logger → Validate → Recovery → Rebuild

Insert this BEFORE line 852 (`# Validate message pool after rollback`):
```python
            # ── FIX 1: Sync logger BEFORE validation (same as forced compression path) ──
            self._sync_logger_after_compression(inst_name, instance.agent_class, "/rollback command")
```

Remove the duplicate `_sync_logger_after_compression` call at line ~879.

---

## FIX 2: Change Forced Compression Notification to In-Tool-Response Pattern

**File:** `agent_cascade/compression/handler.py`
**Bug addressed:** Bug 2 (OpenAI API alternation violation — consecutive USER messages)

### Problem
After compression, the pool looks like:
```
[...tail messages...] → [USER marker] → [USER notification]
```
Both are USER role. OpenAI API requires alternating roles.

### Design Decision
Use the `_append_system_notification` pattern (appends text to last message's content) instead of injecting a separate USER message. The notification should be appended to the compression marker or the last tail message.

### Exact Changes — Forced Compression Path

**Location:** `handler.py` lines 249-278 (inside `execute_force_compression`)

Replace the notification injection block:

**OLD (lines 249-278):**
```python
                    # Inject system notification as a USER message into the pool so the agent sees it.
                    notification_text = (
                        f"[SYSTEM] Context exceeded {usage_pct:.1f}%."
                        f"Forced compression applied. Continue your work — context has been preserved."
                    )

                    # ── Dedup Guard: Prevent duplicate forced compression notifications ────────────────
                    with instance._compression_lock:
                        notification_exists = any(
                            m.role == USER and isinstance(m.content, str) and notification_text == m.content
                            for m in instance.conversation
                        )

                        if not notification_exists:
                            notification_msg = Message(role=USER, content=notification_text)
                            instance.append_message(notification_msg)
                            if response is not None:
                                response.append(notification_msg)
                            logger.info(f"Compression notification injected into conversation pool for '{inst_name}'")
                        else:
                            logger.debug(f"Compression notification already exists in conversation for '{inst_name}' — skipping. Conv length: {len(instance.conversation)}")
```

**NEW:**
```python
                    # ── FIX 2: Append notification to last message content (in-tool-response pattern) ──
                    # This avoids OpenAI API alternation violations (consecutive USER messages).
                    # The marker is the last message in conv; append notification text to its content.
                    notification_text = (
                        f"[SYSTEM] Context exceeded {usage_pct:.1f}%."
                        f"Forced compression applied. Continue your work — context has been preserved."
                    )

                    with instance._compression_lock:
                        # Dedup guard: check if notification already appended to last message content
                        last_msg = instance.conversation[-1] if instance.conversation else None
                        if last_msg is not None:
                            last_content = (last_msg.get('content', '') if isinstance(last_msg, dict)
                                           else getattr(last_msg, 'content', ''))
                            if notification_text in str(last_content):
                                logger.debug(f"Compression notification already in last message for '{inst_name}' — skipping")
                            else:
                                # Append to last message content (in-tool-response pattern)
                                new_content = f"{last_content}\n\n{notification_text}" if last_content else notification_text
                                if isinstance(last_msg, dict):
                                    last_msg['content'] = new_content
                                else:
                                    last_msg.content = new_content
                                logger.info(f"Compression notification appended to last message for '{inst_name}'")

                        # Also append a copy to the response list for streaming/accumulation
                        if response is not None and last_msg is not None:
                            resp_notif = Message(role=USER, content=notification_text)
                            response.append(resp_notif)
```

**Key changes:**
1. No new `Message` appended to conversation — notification text is appended to the LAST message's content (the marker)
2. Dedup check looks at last message content instead of scanning for a standalone USER message
3. A copy is still appended to the `response` list for streaming/accumulation purposes

### Exact Changes — /compress Command Path

**Location:** `handler.py` lines 682-691 (inside `apply_approved_compression`)

Change from creating a new Message to appending to last message content:

**OLD:**
```python
            # Append notification as a new Message object (not mutating last message content)
            notification_text = f"[SYSTEM] Compression applied successfully for {inst_name}."
            notif_msg = Message(role=USER, content=notification_text)
            instance.append_message(notif_msg)
            if response is not None:
                response.append(notif_msg)
```

**NEW:**
```python
            # ── FIX 2: Append success notification to last message content ──
            notification_text = f"[SYSTEM] Compression applied successfully for {inst_name}."
            conv_for_notif = self.pool.get_conversation(inst_name)
            if conv_for_notif and len(conv_for_notif) > 0:
                last_msg = conv_for_notif[-1]
                last_content = (last_msg.get('content', '') if isinstance(last_msg, dict)
                               else getattr(last_msg, 'content', ''))
                new_content = f"{last_content}\n\n{notification_text}" if last_content else notification_text
                if isinstance(last_msg, dict):
                    last_msg['content'] = new_content
                else:
                    last_msg.content = new_content

            # Append to response for streaming/accumulation
            if response is not None:
                resp_notif = Message(role=USER, content=notification_text)
                response.append(resp_notif)
```

### Exact Changes — /rollback Command Path

**Location:** `handler.py` lines 889-894 (inside `handle_rollback_command`)

Same pattern:

**OLD:**
```python
            notification_text = f"[SYSTEM] Rollback applied: Rolled back {actual_count} message(s) for {inst_name}."
            notif_msg = Message(role=USER, content=notification_text)
            instance.append_message(notif_msg)
            if response is not None:
                response.append(notif_msg)
```

**NEW:**
```python
            # ── FIX 2: Append rollback notification to last message content ──
            notification_text = f"[SYSTEM] Rollback applied: Rolled back {actual_count} message(s) for {inst_name}."
            conv_for_notif = self.pool.get_conversation(inst_name)
            if conv_for_notif and len(conv_for_notif) > 0:
                last_msg = conv_for_notif[-1]
                last_content = (last_msg.get('content', '') if isinstance(last_msg, dict)
                               else getattr(last_msg, 'content', ''))
                new_content = f"{last_content}\n\n{notification_text}" if last_content else notification_text
                if isinstance(last_msg, dict):
                    last_msg['content'] = new_content
                else:
                    last_msg.content = new_content

            # Append to response for streaming/accumulation
            if response is not None:
                resp_notif = Message(role=USER, content=notification_text)
                response.append(resp_notif)
```

---

## FIX 3: Filter Empty-Content Messages During Turn Output Collection

**File:** `agent_cascade/execution_engine.py`
**Bug addressed:** Bug 1 (logger duplicate accumulation — ~20% of entries are empty duplicates)

### Problem
The logger accumulates messages during normal execution. Empty assistant messages (content is "" or whitespace-only) get logged as duplicates because they're created during tool call processing and LLM response handling but have no substantive content. These account for ~20% of log entries.

### Root Cause Location
In `_log_messages_to_jsonl` at lines 1712-1756, ALL messages in `turn_output` are logged without filtering empty ones. The `_drain_and_inject` helper at line 587 already filters empty content (line 622), but the turn output from LLM doesn't go through this filter.

### Exact Changes — Turn Output Logging

**Location:** `execution_engine.py` lines 1749-1756

**OLD:**
```python
        # Persist turn_output messages to JSONL log file (P1: LoggerManager migration)
        try:
            # Log turn_output messages from this LLM call
            for msg in turn_output:
                log_inst.log_message(msg)
        except Exception as e:
            logger.debug(f"Logging message to file failed for {inst_name} (non-critical): {e}")
```

**NEW:**
```python
        # Persist turn_output messages to JSONL log file (P1: LoggerManager migration)
        try:
            # ── FIX 3: Filter out empty-content messages before logging ──
            # Empty assistant duplicates are ~20% of log entries and a major source of duplication.
            # A message is "empty" if its content is None, empty string, or whitespace-only.
            for msg in turn_output:
                content = (msg.get('content', '') if isinstance(msg, dict)
                          else getattr(msg, 'content', ''))
                # Skip truly empty messages (None, "", or whitespace-only)
                if not content or (isinstance(content, str) and not content.strip()):
                    continue
                log_inst.log_message(msg)
        except Exception as e:
            logger.debug(f"Logging message to file failed for {inst_name} (non-critical): {e}")
```

### Exact Changes — Pre-Existing Message Logging

**Location:** `execution_engine.py` lines 1736-1748

Apply the same filter when logging pre-existing messages:

**OLD:**
```python
        if already_logged_count == 0 and conv:
            # First time logging — log all pre-existing messages (system + user).
            for msg in conv:
                if isinstance(msg, Message) or (isinstance(msg, dict) and 'role' in msg):
                    log_inst.log_message(msg)
        elif already_logged_count < len(conv):
            # Partial sync — log only messages added since last sync.
            for msg in conv[already_logged_count:]:
                if isinstance(msg, Message) or (isinstance(msg, dict) and 'role' in msg):
                    log_inst.log_message(msg)
```

**NEW:**
```python
        if already_logged_count == 0 and conv:
            # First time logging — log all pre-existing messages (system + user).
            for msg in conv:
                if isinstance(msg, Message) or (isinstance(msg, dict) and 'role' in msg):
                    # FIX 3: Skip empty-content messages to prevent duplicate accumulation
                    content = (msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', ''))
                    if not content or (isinstance(content, str) and not content.strip()):
                        continue
                    log_inst.log_message(msg)
        elif already_logged_count < len(conv):
            # Partial sync — log only messages added since last sync.
            for msg in conv[already_logged_count:]:
                if isinstance(msg, Message) or (isinstance(msg, dict) and 'role' in msg):
                    content = (msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', ''))
                    if not content or (isinstance(content, str) and not content.strip()):
                        continue
                    log_inst.log_message(msg)
```

---

## FIX 4: Marker Position Calculation on Active Pool with Tail-Synced Logger Offset

**Files:** `agent_cascade/compression/core.py`, `agent_cascade/compression/result.py`
**Bug addressed:** Ensures pool and logger are tail-synced (design requirement from user)

### Problem
The marker insertion position is calculated in `compress_context()` at line 282 using the raw history. The logger's `reset_history(rewrite=True)` independently calculates marker position by searching for markers in the pool. If the pool state changes between compression and logger sync, these calculations can drift.

### Design Requirement
> "Marker insertion position should be calculated on the active pool, then the offset from tail is passed to log handling logic so they are tail-synced"

### Current Architecture
1. `core.py` line 282: `insert_pos = active_start_idx + target_discard_count` (calculated on history copy)
2. Pool mutation at line 335: actual insert happens
3. Logger sync in `handler.py`: calls `_sync_logger_after_compression` → `reset_history(conv, rewrite=True)`
4. Logger's `reset_history` at lines 468-479 independently searches for the last marker and calculates tail offset

### Solution
The current architecture already does this correctly:
- `compress_context()` returns `tail_count = len(active_set) - effective_discard` (line 370)
- Logger's `reset_history(rewrite=True)` at lines 468-485 finds the last marker in pool and mirrors tail distance

**What needs fixing:** The logger's `reset_history` method searches for markers by looking backward from the end of `new_history`. But after Fix 2 (notification appended to marker content), the marker is still a USER message with `<context_summary>` tags — so it should still be found correctly.

The real issue is that the logger search at line 473 checks `content.startswith(COMPRESSION_MARKER)`, but the notification text was APPENDED to this content. The startswith check still works because the notification is appended, not prepended.

**Verification needed:** No code change required for Fix 4 if Fix 2 appends (not prepends). But we should verify that the marker detection in `reset_history` is robust:

### Exact Changes — Robustness Improvement

**Location:** `agent_cascade/logger/agent_instance_logger.py` lines 468-475

The current marker detection uses `content.startswith(COMPRESSION_MARKER)`. After Fix 2, the content will be:
```
[COMPRESSION_MARKER...]\n\n[SYSTEM] Context exceeded...
```

This still starts with COMPRESSION_MARKER, so no change is needed. But let's add a more robust check as defense-in-depth:

**OLD (lines 468-475):**
```python
                from agent_cascade.llm.schema import USER as USER_ROLE
                last_marker_idx = -1
                for i in range(len(new_history) - 1, -1, -1):
                    msg = new_history[i]
                    role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                    content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
                    if role == USER_ROLE and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                        last_marker_idx = i
                        break
```

**NEW (more robust check that also handles appended notifications):**
```python
                from agent_cascade.llm.schema import USER as USER_ROLE
                last_marker_idx = -1
                for i in range(len(new_history) - 1, -1, -1):
                    msg = new_history[i]
                    role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                    content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
                    # FIX 4: Use 'in' check instead of startswith to handle notifications appended to marker content.
                    # After Fix 2, the marker content may have system notification text appended via "\n\n".
                    if role == USER_ROLE and isinstance(content, str) and COMPRESSION_MARKER in content:
                        last_marker_idx = i
                        break
```

This is a one-word change (`startswith` → `in`) that makes the detection more robust against Fix 2's notification appending.

---

## FIX 5: Recovery Path Should Also Sync Logger

**File:** `agent_cascade/compression/handler.py`
**Bug addressed:** Ensures logger reflects recovered state after successful recovery

### Problem
After successful recovery from the log, the pool is updated but the logger isn't synced again. If another compression happens before the next turn's natural sync, the logger still has stale data.

### Exact Changes — Forced Compression Recovery Path

**Location:** `handler.py` lines 289-300 (inside `execute_force_compression`)

After successful recovery, add a logger sync:

**OLD (lines 289-296):**
```python
                        try:
                            recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                            if recov and validate_message_pool(recov, inst_name):
                                instance.rebuild_conversation(list(recov))
                                self.engine._rebuild_working_set(messages, llm_messages, inst_name)
                                logger.info(f"Recovered message pool from log for '{inst_name}' ({len(recov)} messages)")
                                conv = recov
```

**NEW:**
```python
                        try:
                            recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                            if recov and validate_message_pool(recov, inst_name):
                                instance.rebuild_conversation(list(recov))
                                self.engine._rebuild_working_set(messages, llm_messages, inst_name)
                                logger.info(f"Recovered message pool from log for '{inst_name}' ({len(recov)} messages)")
                                conv = recov

                                # ── FIX 5: Sync logger after recovery to reflect recovered state ──
                                self._sync_logger_after_compression(inst_name, instance.agent_class, "recovery")
```

### Exact Changes — /compress Command Recovery Path

**Location:** `handler.py` lines 649-654 (inside `apply_approved_compression`)

After successful recovery:

**OLD:**
```python
                    recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                    if recov and validate_message_pool(recov, inst_name):
                        instance.rebuild_conversation(list(recov))
                        logger.info(f"Recovered message pool after /compress for '{inst_name}' ({len(recov)} messages)")
                        self.engine._rebuild_working_set(messages, llm_messages, inst_name)
```

**NEW:**
```python
                    recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                    if recov and validate_message_pool(recov, inst_name):
                        instance.rebuild_conversation(list(recov))
                        logger.info(f"Recovered message pool after /compress for '{inst_name}' ({len(recov)} messages)")
                        self.engine._rebuild_working_set(messages, llm_messages, inst_name)

                        # ── FIX 5: Sync logger after recovery to reflect recovered state ──
                        self._sync_logger_after_compression(inst_name, instance.agent_class, "/compress recovery")
```

### Exact Changes — /rollback Command Recovery Path

**Location:** `handler.py` lines 857-862 (inside `handle_rollback_command`)

After successful recovery:

**OLD:**
```python
                    recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                    if recov and validate_message_pool(recov, inst_name):
                        instance.rebuild_conversation(list(recov))
                        logger.info(f"Recovered message pool after /rollback for '{inst_name}' ({len(recov)} messages)")
                        self.engine._rebuild_working_set(messages, llm_messages, inst_name)
```

**NEW:**
```python
                    recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                    if recov and validate_message_pool(recov, inst_name):
                        instance.rebuild_conversation(list(recov))
                        logger.info(f"Recovered message pool after /rollback for '{inst_name}' ({len(recov)} messages)")
                        self.engine._rebuild_working_set(messages, llm_messages, inst_name)

                        # ── FIX 5: Sync logger after recovery to reflect recovered state ──
                        self._sync_logger_after_compression(inst_name, instance.agent_class, "/rollback recovery")
```

---

## Implementation Summary Table

| Fix | File(s) | Lines Affected | Change Type | Risk Level |
|-----|---------|---------------|-------------|------------|
| **Fix 1** | `handler.py` | ~280-315, ~643-675, ~849-879 | Reorder operations | Low (just reordering) |
| **Fix 2** | `handler.py` | ~249-278, ~682-691, ~889-894 | Logic change (append vs insert) | Medium (changes notification pattern) |
| **Fix 3** | `execution_engine.py` | ~1736-1756 | Add filtering logic | Low (only affects logging) |
| **Fix 4** | `agent_instance_logger.py` | ~468-475 | One-word change (`startswith` → `in`) | Very low |
| **Fix 5** | `handler.py` | ~289-300, ~649-654, ~857-862 | Add sync calls after recovery | Low (defensive addition) |

---

## Testing Checklist

After implementing all fixes:

### Unit Tests
- [ ] Forced compression triggers and completes without validation errors
- [ ] /compress command works with various fractions
- [ ] /rollback command works with various counts
- [ ] No consecutive USER messages after compression (check pool roles)
- [ ] Logger history matches pool state after each operation

### Integration Tests
- [ ] Run a multi-turn conversation with forced compression triggered mid-conversation
- [ ] Verify no pool doubling occurs during recovery paths
- [ ] Verify logger file doesn't accumulate empty-content duplicates over time
- [ ] Test recovery path: force validation failure → verify recovery reads clean data

### Regression Tests
- [ ] Compression marker appears correctly in conversation history
- [ ] Notification text is visible to the LLM (appended to marker content)
- [ ] Streaming updates still work (response list gets notification copies)
- [ ] No alternation violations: check that USER↔ASSISTANT roles alternate properly

---

## Important Notes for Implementation

1. **Fix order matters:** Implement Fix 1 first (logger sync before validation), then Fix 2 (notification pattern change), as Fix 2 changes how notifications appear in the pool which affects what Fix 1 validates.

2. **No dedup hacks:** Per design decision, we're not adding dedup logic to `validate_message_pool`. Instead, we fix the root cause by ensuring logger sync happens before validation and empty messages aren't logged.

3. **Notification visibility:** After Fix 2, notifications are appended to the marker message content with `\n\n` separator. The LLM will see them as part of the marker text. This is fine — the notification is still visible in context.

4. **Response list handling:** For streaming/accumulation, we still append a `Message(role=USER)` copy to the `response` list. This ensures UI gets the notification even though it's not a standalone message in the pool.

5. **Lock safety:** All changes respect existing `_compression_lock` patterns. No new lock acquisitions are added — we just reorder operations within existing locked sections.

---

## File Reference Map

| File | Purpose | Total Lines |
|------|---------|-------------|
| `agent_cascade/compression/handler.py` | Main compression handler (forced, /compress, /rollback) | 907 |
| `agent_cascade/compression/core.py` | compress_context() entry point | 389 |
| `agent_cascade/execution_engine.py` | _rebuild_working_set, turn logging, notification injection | 3216 |
| `agent_cascade/utils/pool_validation.py` | validate_message_pool function | 94 |
| `agent_cascade/compression/helpers.py` | build_marker_message, rebuild_working_set | 390 |
| `agent_cascade/logger/agent_instance_logger.py` | Logger with reset_history/update_history | 630 |