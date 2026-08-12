# Hierarchical Memory Consolidation — Implementation Plan (Revised)

**Feature**: When ≥8 compression markers accumulate, consolidate the oldest 7 into one higher-level summary marker.  
**Goal**: Prevent unbounded marker stacking while preserving chronological narrative and key events.

---

## 1. Overview

Current behavior: each `compress_context` call appends a marker → `[M0][M1][M2]...[MN]`. No upper bound; markers accumulate indefinitely.

New behavior (post-consolidation):
- After a normal compression, check if total markers ≥ `COMPRESSION_CONSOLIDATION_THRESHOLD` (default 8).
- If so, take markers M0..M6 (all except the newest), feed their summary texts to the Compressor with a **consolidation prompt**, get back one higher-level summary S.
- Replace M0's position in pool with a new L2 marker containing S.
- Remove markers M1..M6 from pool, **preserving all raw message segments between them**.
- Final layout: `[SYS][U0][S-L2][raw segments that were between markers][M7][tail...]`

This is **hierarchical**: S summarizes summaries, not raw messages. The newest marker (M7) remains untouched because it represents the most recent context window the agent is actively using.

Key constraints:
- Tail past last marker must remain identical in pool and JSONL (existing invariant).
- Consolidation runs synchronously after compression; if it fails, normal compression still succeeded.
- No recursion: consolidation itself must not trigger another consolidation.
- Raw message segments between markers must be preserved in both pool and JSONL.

---

## 2. Trigger Mechanism

**Where**: Post-compression in `compress_context()` (`core.py`, after step 13).

**When**: After a successful compression (not dry-run), if `count_markers(history) >= COMPRESSION_CONSOLIDATION_THRESHOLD`.

**Why here**:
- `compress_context` is the single entry point for all compression paths.
- Pool and logger are already in consistent state after steps 10–12.
- We can reuse the existing pool snapshot; no need to refetch.

**Guard against recursion**: Use a module-level thread-local flag `_consolidating_agents: set[str]` that tracks which agents are currently undergoing consolidation. If an agent name is in this set, skip consolidation. This prevents re-entry if something unexpected calls `compress_context()` during consolidation.

---

## 3. New Prompt: Consolidation Prompt

Located in `agent_cascade/prompts/dna.py`.

### Rationale
- Compressor is already tuned for summarization; we just need to tell it the input is "summaries of summaries" and it should go higher-level.
- Emphasize chronological structure, key decisions, dropped details are acceptable.

### Prompt Text

```python
CONSOLIDATION_PROMPT = (
    "You are consolidating multiple existing conversation summaries into a single higher-level summary.\n\n"
    "The input below contains several sequential summaries from earlier compression cycles. "
    "Each represents a compressed window of past conversation.\n\n"
    "Your task: merge them into ONE cohesive, chronological narrative that:\n"
    "1. Preserves the overall story arc and major milestones.\n"
    "2. Keeps key decisions, architectural choices, important facts, and task outcomes.\n"
    "3. Drops redundant details, minor steps, and intermediate reasoning that is no longer actionable.\n"
    "4. Is significantly shorter than the total input — you are going one level higher in abstraction.\n\n"
    "CRITICAL RULES:\n"
    "- Output ONLY the consolidated summary. No intro/outro remarks like 'Here is the summary'.\n"
    "- Do not include meta-commentary or thinking process.\n"
    "- Maintain chronological order implicitly (earliest events first).\n"
    "- If conflicting information appears across summaries, prefer the most recent version.\n\n"
    "--- START EXISTING SUMMARIES ---\n{summaries_text}\n--- END EXISTING SUMMARIES ---\n\n"
    f"Present your consolidated summary below and always terminate it with last line `{COMPRESSION_END_MARKER}` to indicate the end."
)
```

### Marker Header Format (hierarchy level encoding)

Add a level tag to the header for observability:

- Normal compression marker: `--- CONTEXT COMPRESSED (L1, 70% of history summarized) ---`
- Consolidation marker:     `--- CONTEXT COMPRESSED (L2, 7 summaries consolidated) ---`

This is cosmetic but useful for debugging and future multi-level generalization. We keep the same `<context_summary>` tags so existing parsing logic continues to work.

Implementation: modify `build_marker_message()` signature or add a new helper `build_consolidation_marker()`.

---

## 4. Architecture Diagram (Before/After)

### Before Consolidation (8 markers example)
```
Pool:   [SYS][U0][M0][raw seg A][M1][raw seg B][M2]...[M5][raw seg F][M6][raw seg G][M7][tail...]
JSONL:  Full history with all markers + all discarded messages between them
```

### After Consolidation (markers M0..M6 → S-L2)
```
Pool:   [SYS][U0][S-L2][raw seg A][raw seg B]...[raw seg F][raw seg G][M7][tail...]
JSONL:  Same structure but with M1..M6 marker lines removed, S-L2 replaces M0 position.
        All raw message segments preserved (including previously discarded ones).
```

Note: Raw segments between markers are the messages that survived in the pool after each compression cycle's tail was kept. Consolidation removes only the redundant marker messages, not the content they sit alongside.

---

## 5. Step-by-Step Implementation Tasks

Ordered by dependency. Each task is scoped to be implementable independently.

### Task 1: Add marker-counting utilities

**File**: `agent_cascade/agent_pool.py`

Add static methods next to `find_last_marker()`:

```python
@staticmethod
def count_markers(history: List[Message]) -> int:
    """Count compression markers in conversation."""
    count = 0
    for msg in history:
        role = AgentPool._msg_field(msg, 'role')
        content = AgentPool._msg_field(msg, 'content')
        if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
            count += 1
    return count

@staticmethod
def find_all_marker_indices(history: List[Message]) -> List[int]:
    """Return indices of all compression markers in chronological order."""
    indices = []
    for i, msg in enumerate(history):
        role = AgentPool._msg_field(msg, 'role')
        content = AgentPool._msg_field(msg, 'content')
        if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
            indices.append(i)
    return indices
```

### Task 2: Add consolidation prompt to DNA

**File**: `agent_cascade/prompts/dna.py`

Add `CONSOLIDATION_PROMPT` (see §3). No new template needed — use existing `COMPRESSION_BASELINE_TEMPLATE`, just embed level info in the header string.

### Task 3: Add consolidation marker builder

**File**: `agent_cascade/compression/helpers.py`

Add a helper function:

```python
def build_consolidation_marker_message(summary_text, num_summaries_consolidated):
    """Build a L2 consolidation marker message."""
    header = f"L2, {num_summaries_consolidated} summaries consolidated"
    content = COMPRESSION_BASELINE_TEMPLATE.format(
        header=header,
        summary=summary_text,
    )
    return Message(role=USER, content=str(content))
```

### Task 4: Add consolidation invocation to agent_invoker.py

**File**: `agent_cascade/compression/agent_invoker.py`

Add a new function parallel to `invoke_compression_agent()`:

```python
def invoke_consolidation_agent(
    agent_pool,
    marker_summaries: List[str],  # extracted summary texts from markers M0..M6
    caller_name=None,
):
    """Invoke Compressor to consolidate multiple existing summaries."""
    # Same pattern as invoke_compression_agent() but:
    # - Uses CONSOLIDATION_PROMPT instead of COMPRESSION_PROMPT
    # - Formats input as numbered summaries: "SUMMARY 1:\n...\n\nSUMMARY 2:\n..."
    # - System message mentions "consolidating summaries"
```

Key differences from normal compression invocation:
- Input is plain text summaries, not raw messages.
- Same slot bypass, same engine.run() pattern, same validation (end marker check).

### Task 5: Implement `_consolidate_markers()` in core.py

**File**: `agent_cascade/compression/core.py`

Add module-level recursion guard at top of file:

```python
import threading
_consolidating_agents = set()
_consolidation_lock = threading.Lock()
```

Add the consolidation function:

```python
def _consolidate_markers(
    agent_pool,
    target_agent_name: str,
) -> None:
    """
    Post-compression hierarchical consolidation.
    
    Called after successful compress_context when marker count >= threshold.
    Takes oldest N-1 markers (all except newest), consolidates them into one,
    replaces marker-0 position with the new L2 marker, removes intermediate markers.
    Preserves all raw message segments between markers.
    
    Thread-safe: Uses _consolidation_lock and recursion guard.
    Non-fatal: If anything fails, normal compression still succeeded.
    """
    from agent_cascade.settings import COMPRESSION_CONSOLIDATION_THRESHOLD
    
    # Recursion guard: prevent re-entry for this agent
    with _consolidation_lock:
        if target_agent_name in _consolidating_agents:
            logger.debug(
                f"Consolidation already in progress for '{target_agent_name}' — skipping (recursion guard)"
            )
            return
        _consolidating_agents.add(target_agent_name)
    
    try:
        history = agent_pool.get_conversation(target_agent_name)
        marker_indices = AgentPool.find_all_marker_indices(history)
        
        # Guard: need at least threshold markers to consolidate
        if len(marker_indices) < COMPRESSION_CONSOLIDATION_THRESHOLD:
            return
        
        # Select markers to consolidate: all except the newest (last one)
        consolidate_indices = marker_indices[:-1]   # M0..M6 (to be consolidated)
        keep_index = marker_indices[-1]             # M7 (newest, untouched)
        
        num_to_consolidate = len(consolidate_indices)
        logger.info(
            f"Consolidating {num_to_consolidate} markers for '{target_agent_name}' "
            f"(keeping newest marker at index {keep_index})"
        )
        
        # Extract summary texts from markers being consolidated
        summaries_to_consolidate = []
        for idx in consolidate_indices:
            try:
                msg = history[idx]
                content = extract_text_from_message(msg, add_upload_info=False)
                if not isinstance(content, str):
                    logger.warning(f"Marker at index {idx} has non-string content — skipping")
                    continue
                # Parse <context_summary>...</context_summary>
                if '<context_summary>' in content and '</context_summary>' in content:
                    summary_text = content.split('<context_summary>', 1)[1].split('</context_summary>', 1)[0].strip()
                    if summary_text:
                        summaries_to_consolidate.append(summary_text)
                    else:
                        logger.warning(f"Empty summary in marker at index {idx} — skipping")
                else:
                    logger.warning(
                        f"Marker at index {idx} missing <context_summary> tags — skipping. "
                        f"Content preview: {content[:100]}..."
                    )
            except Exception as e:
                logger.error(f"Failed to extract summary from marker at index {idx}: {e}")
                # Continue with other markers; we can consolidate what we have
        
        if not summaries_to_consolidate:
            logger.error(
                f"No valid summaries extracted from {num_to_consolidate} markers for '{target_agent_name}' — "
                f"aborting consolidation to avoid data loss"
            )
            return
        
        # Token size check before invoking compressor
        try:
            from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
            total_summary_tokens = sum(qwen_count(s) for s in summaries_to_consolidate)
            max_compressor_input = getattr(
                agent_pool.settings, 'compression_max_consolidation_tokens', 32000
            )
            if total_summary_tokens > max_compressor_input:
                logger.warning(
                    f"Consolidation input too large for '{target_agent_name}': "
                    f"{total_summary_tokens} tokens > {max_compressor_input} limit. "
                    f"Aborting to prevent compressor failure."
                )
                return
        except Exception as e:
            logger.debug(f"Token count check for consolidation skipped (non-fatal): {e}")
        
        # Invoke consolidation agent
        from agent_cascade.compression.agent_invoker import invoke_consolidation_agent
        try:
            consolidated_summary = invoke_consolidation_agent(
                agent_pool=agent_pool,
                marker_summaries=summaries_to_consolidate,
                caller_name=target_agent_name,
            )
        except RuntimeError as e:
            logger.error(f"Consolidation agent failed for '{target_agent_name}': {e}")
            return  # Non-fatal; normal compression succeeded
        
        if not consolidated_summary or not consolidated_summary.strip():
            logger.error(f"Empty consolidation result for '{target_agent_name}' — aborting")
            return
        
        # Build new L2 marker
        from agent_cascade.compression.helpers import build_consolidation_marker_message
        new_marker = build_consolidation_marker_message(consolidated_summary, len(summaries_to_consolidate))
        
        # Pool mutation: replace M0 position with new marker, remove M1..M6 only.
        # CRITICAL: Preserve all raw message segments between markers.
        # Strategy: iterate through history, skip indices in consolidate_indices[1:], 
        # replace index consolidate_indices[0] with new_marker.
        
        first_consolidate_idx = consolidate_indices[0]    # M0's position (will be replaced)
        remove_indices = set(consolidate_indices[1:])      # M1..M6 positions (to be removed)
        
        new_history = []
        for i, msg in enumerate(history):
            if i == first_consolidate_idx:
                # Replace M0 with new L2 marker
                new_history.append(new_marker)
            elif i not in remove_indices:
                # Keep everything else (raw segments, M7, tail)
                new_history.append(msg)
        
        logger.info(
            f"Consolidation pool mutation for '{target_agent_name}': "
            f"{len(history)} → {len(new_history)} messages, "
            f"removed {len(remove_indices)} markers, replaced 1"
        )
        
        # Atomic pool update via instance_conversations setter.
        # Thread safety: __setitem__ calls inst.rebuild_conversation() which acquires _compression_lock
        # and handles full cache invalidation (see agent_pool.py:_InstanceConversationMapping.__setitem__).
        try:
            agent_pool.instance_conversations[target_agent_name] = new_history
        except Exception as e:
            logger.error(f"Pool mutation during consolidation failed for '{target_agent_name}': {e}")
            return
        
        # Sync logger: need to remove intermediate markers from JSONL while preserving raw messages.
        # _sync_marker_single_write() only handles inserting the last marker; it won't remove M1..M6.
        # Solution: call _consolidate_markers_in_jsonl() (new helper in agent_instance_logger.py)
        try:
            log_inst = agent_pool.get_logger(target_agent_name, None)
            success = log_inst._consolidate_markers_in_jsonl(
                new_pool_state=new_history,
                marker_to_replace_idx=first_consolidate_idx,
                markers_to_remove_indices=remove_indices,
            )
            if not success:
                logger.warning(
                    f"JSONL consolidation sync failed for '{target_agent_name}' — "
                    f"pool is authoritative; JSONL will be corrected on next compression."
                )
        except Exception as e:
            logger.error(f"Logger sync during consolidation failed for '{target_agent_name}': {e}")
            # Non-fatal: pool is correct
        
    finally:
        # Always clear recursion guard
        with _consolidation_lock:
            _consolidating_agents.discard(target_agent_name)

```

### Task 6: Add JSONL consolidation helper to logger

**File**: `agent_cascade/logger/agent_instance_logger.py`

Add a new method to handle marker removal during consolidation. This is needed because `_sync_marker_single_write()` only inserts the last marker at mirrored position; it doesn't remove intermediate markers.

```python
def _consolidate_markers_in_jsonl(
    self,
    new_pool_state: List[Any],
    marker_to_replace_idx: int,
    markers_to_remove_indices: set,
) -> bool:
    """Surgically update JSONL for hierarchical consolidation.
    
    Reads existing JSONL, removes lines corresponding to intermediate markers (M1..M6),
    and replaces the first consolidated marker's line with the new L2 marker.
    Preserves all raw message segments (including previously discarded ones).
    
    Design doc §5.2 rule: JSONL retains FULL history. Consolidation only removes 
    redundant marker messages, not raw content between them.
    
    Args:
        new_pool_state: New pool working set after consolidation.
        marker_to_replace_idx: Index in old pool state of M0 (replaced with L2 marker).
        markers_to_remove_indices: Set of indices in old pool state of M1..M6 (removed).
    
    Returns:
        True on success, False on error.
    """
    # Close cached handle before writing
    if self._file_handle and not self._file_handle.closed:
        self._file_handle.flush()
        self._file_handle.close()
        self._file_handle = None
    
    from agent_cascade.llm.schema import USER as USER_ROLE
    
    try:
        # Read existing log messages from disk (full history)
        if not self.log_path or not os.path.exists(self.log_path):
            logger.debug(f"Log file missing for {self.instance_name} — writing pool state directly.")
            existing_msgs = []
        else:
            existing_msgs = []
            with open(self.log_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict) and "metadata" not in item and "event" not in item:
                            existing_msgs.append(item)
                    except json.JSONDecodeError:
                        logger.debug(f"Skipping corrupted JSONL line {line_num} in {self.log_path}")
        
        # Find the new L2 marker in pool state (it's at position marker_to_replace_idx)
        new_marker_msg = None
        if marker_to_replace_idx < len(new_pool_state):
            new_marker_msg = self._format_message(new_pool_state[marker_to_replace_idx])
        
        # Build result: iterate through existing JSONL messages, removing lines that match
        # the content of markers being removed (M1..M6). Replace M0's line with new L2 marker.
        # 
        # Strategy: identify markers in JSONL by content matching against pool markers that
        # are being removed/replaced. We need to extract those marker contents from the old pool state
        # before consolidation happened. But we don't have the old pool state here — so instead,
        # we use a simpler approach: remove ALL marker messages from JSONL except the newest one
        # (M7), then insert the new L2 marker at the appropriate position.
        
        if not new_marker_msg:
            logger.warning(
                f"No new marker found in pool state for consolidation sync — "
                f"skipping JSONL update. Pool is authoritative; JSONL will be corrected on next compression."
            )
            return False
        
        # Find newest marker in pool (M7 — the one we kept)
        newest_marker_content = None
        for i in range(len(new_pool_state) - 1, -1, -1):
            msg = new_pool_state[i]
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if role == USER_ROLE and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                newest_marker_content = content
                break
        
        # Filter JSONL: keep all messages except markers that are neither the new L2 nor the newest kept marker
        result_msgs = []
        new_marker_inserted = False
        
        for msg in existing_msgs:
            content = msg.get('content', '')
            
            # Check if this is a compression marker line
            if isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                # Keep it if it matches the newest kept marker (M7) — don't touch that one
                if newest_marker_content and content == newest_marker_content:
                    result_msgs.append(msg)
                elif not new_marker_inserted:
                    # This is a marker being consolidated — replace first occurrence with new L2 marker
                    result_msgs.append(new_marker_msg)
                    new_marker_inserted = True
                # else: skip this redundant marker line (M1..M6)
            else:
                # Not a marker — always keep raw messages
                result_msgs.append(msg)
        
        # If new L2 marker wasn't inserted (e.g., no old markers found in JSONL), insert at beginning of message area
        if not new_marker_inserted and new_marker_msg:
            result_msgs.insert(0, new_marker_msg)
        
        # Single write to disk
        lines = [json.dumps({"metadata": self.data["metadata"]}, ensure_ascii=False) + '\n']
        for msg in result_msgs:
            lines.append(json.dumps(msg if isinstance(msg, dict) else self._format_message(msg), ensure_ascii=False) + '\n')
        
        with open(self.log_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        
        self._file_handle = None
        logger.info(
            f"Consolidation JSONL sync for {self.instance_name}: "
            f"{len(existing_msgs)} → {len(result_msgs)} messages in file"
        )
        
        # Update internal tracking — pool state for in-memory history
        self.data["history"] = [self._format_message(msg) for msg in new_pool_state]
        self._file_history_synced = True
        
        return True
    
    except Exception as e:
        logger.error(f"Failed to consolidate markers in JSONL for {self.instance_name}: {e}")
        return False
```

### Task 7: Hook consolidation into compress_context()

**File**: `agent_cascade/compression/core.py`

At the end of `compress_context()`, after step 13 (logging), add:

```python
# ── 14. Post-compression hierarchical consolidation check ──
if not dry_run and result.success:
    try:
        from agent_cascade.settings import COMPRESSION_CONSOLIDATION_THRESHOLD
        
        post_history = agent_pool.get_conversation(target_agent_name)
        marker_count = AgentPool.count_markers(post_history)
        
        if marker_count >= COMPRESSION_CONSOLIDATION_THRESHOLD:
            logger.info(
                f"Triggering hierarchical consolidation for '{target_agent_name}': "
                f"{marker_count} markers present, will consolidate oldest {marker_count - 1}"
            )
            _consolidate_markers(agent_pool, target_agent_name)
    except Exception as e:
        logger.error(
            f"Hierarchical consolidation failed for '{target_agent_name}' (non-fatal): {e}. "
            f"Normal compression succeeded; markers will be consolidated on next cycle."
        )
```

Key design choice: consolidation failure is **non-fatal**. Normal compression already succeeded. We just log and let the next compression cycle retry consolidation.

### Task 8: Add settings constants

**File**: `agent_cascade/settings.py`

Add tunable parameters:

```python
# Hierarchical memory consolidation settings
COMPRESSION_CONSOLIDATION_THRESHOLD: int = int(os.getenv(
    'QWEN_AGENT_COMPRESSION_CONSOLIDATION_THRESHOLD', 8))  # Markers at which to trigger consolidation
COMPRESSION_MAX_CONSOLIDATION_TOKENS: int = int(os.getenv(
    'QWEN_AGENT_COMPRESSION_MAX_CONSOLIDATION_TOKENS', 32000))  # Max tokens for consolidation input before aborting
```

---

## 6. Pseudocode for Key Operations

### Marker Selection and Consolidation Flow

```
after successful compress_context():
    markers = find_all_marker_indices(history)
    
    if len(markers) >= CONSOLIDATION_THRESHOLD:
        to_consolidate = markers[:-1]      # M0..M6 (all except newest)
        keep_newest = markers[-1]          # M7
        
        summaries = []
        for idx in to_consolidate:
            s = extract_summary_text(history[idx])  # with try/except, skip malformed
            if s is not None:
                summaries.append(s)
        
        if not summaries:
            log error, abort (no valid data to consolidate)
        
        consolidated = invoke_consolidation_agent(summaries)
        new_marker = build_consolidation_marker(consolidated, len(summaries))
        
        # Pool mutation: iterate through history, replace M0 with new_marker, skip M1..M6
        first_idx = to_consolidate[0]      # M0 position (replace)
        remove_set = set(to_consolidate[1:])  # M1..M6 positions (skip)
        
        new_history = []
        for i, msg in enumerate(history):
            if i == first_idx:
                new_history.append(new_marker)
            elif i not in remove_set:
                new_history.append(msg)
            # else: skip this marker
        
        pool.instance_conversations[name] = new_history  # via setter → rebuild_conversation()
        
        # JSONL sync: surgically remove intermediate markers, replace M0 with L2
        logger._consolidate_markers_in_jsonl(new_history, first_idx, remove_set)
```

### Tail Sync Invariant Preservation

- The tail past the last marker (M7/newest) is **never touched** by consolidation.
- We only modify positions before M7: replace M0 with L2, remove M1..M6.
- Raw message segments between markers are preserved in both pool and JSONL.
- JSONL sync removes only marker lines (identified by `startswith(COMPRESSION_MARKER)`), keeping all raw messages.

---

## 7. Integration Points Summary

| Component | Change Type | Description |
|-----------|-------------|-------------|
| `prompts/dna.py` | Add | `CONSOLIDATION_PROMPT` constant |
| `agent_pool.py` | Add | `count_markers()`, `find_all_marker_indices()` static methods |
| `compression/helpers.py` | Add | `build_consolidation_marker_message()` |
| `compression/agent_invoker.py` | Add | `invoke_consolidation_agent()` function |
| `compression/core.py` | Add+Modify | Module-level recursion guard; `_consolidate_markers()` function; hook at end of `compress_context()` |
| `logger/agent_instance_logger.py` | Add | `_consolidate_markers_in_jsonl()` method for surgical JSONL marker removal |
| `settings.py` | Add | `COMPRESSION_CONSOLIDATION_THRESHOLD`, `COMPRESSION_MAX_CONSOLIDATION_TOKENS` |

No changes needed to:
- `compression_tools.py` — consolidation is internal, not agent-callable.
- `handler.py` — forced compression paths go through `compress_context()` which now hooks consolidation.

---

## 8. Edge Cases and Error Handling

| Scenario | Behavior |
|----------|----------|
| Consolidation agent fails (timeout, empty output) | Log error, do NOT roll back normal compression. Markers stay as-is; next compression will retry consolidation. |
| Fewer than threshold markers at consolidation time | Guard: `if len(marker_indices) < THRESHOLD: return`. Defensive no-op. |
| Marker content malformed (no `<context_summary>` tags) | Skip that marker's summary in extraction with warning log. If all fail, abort consolidation entirely to avoid data loss. |
| Pool mutation fails during consolidation | Wrapped in try/except; pool reverts because `rebuild_conversation()` is atomic. Log error. |
| JSONL rewrite fails after pool update | Pool is authoritative; JSONL will be corrected on next compression via normal `_sync_marker_single_write()`. Log warning. |
| Consolidation triggers during forced compression | Same path: forced compression calls `compress_context()` → post-compression hook runs consolidation. Slot bypass applies to both Compressor invocations. |
| Very long consolidated summary | Token size check before compressor invocation; if too large, abort with warning. Next cycle will try again or compress normally. |
| Recursive consolidation call | Recursion guard (`_consolidating_agents` set) prevents re-entry for the same agent. Log debug message and skip. |
| Nested consolidations (≥8 L2 markers over time) | L2 markers are still markers (`startswith(COMPRESSION_MARKER)`), so they count toward threshold. Same logic applies recursively. Future enhancement could differentiate levels with different prompts. |

---

## 9. Risk Assessment (Updated)

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Raw message segments lost during consolidation** | **HIGH → MITIGATED** | Pool mutation now iterates through history, only skipping marker indices. All non-marker messages preserved. JSONL sync similarly preserves all non-marker lines. |
| Consolidation loses critical information | Medium | Prompt emphasizes keeping key decisions/facts; hierarchical loss is expected and acceptable (that's the point). User can always check JSONL for full history. |
| Performance: consolidation adds latency post-compression | Low-Medium | Only triggers when ≥8 markers exist (relatively rare in short sessions). Same Compressor agent, similar payload size to normal compression. Can be made async in future if needed. |
| Marker parsing bugs cause silent data loss | Medium → MITIGATED | Defensive extraction with try/except per marker; log errors clearly; abort consolidation entirely if no valid summaries extracted (no partial writes). |
| JSONL desync between pool and file | Low | New `_consolidate_markers_in_jsonl()` surgically removes only marker lines. Fallback to `reset_history(rewrite=True)` if primary method fails. Tail sync check runs after all operations. |
| Infinite loop: consolidation re-triggers itself | None (designed out) | Consolidation reduces marker count from N to 2, far below threshold. Plus recursion guard prevents re-entry. |
| Thread safety issues during pool mutation | Low → MITIGATED | Pool mutation goes through `instance_conversations` setter → `rebuild_conversation()` which acquires `_compression_lock`. Same pattern as normal compression. |
| Compressor overload from large consolidation input | Medium → MITIGATED | Token size check before invocation; configurable limit (`COMPRESSION_MAX_CONSOLIDATION_TOKENS`, default 32000). Aborts gracefully if exceeded. |

---

## 10. Testing Strategy

### Unit Tests
1. `count_markers()` with various histories (0 markers, mixed messages, edge cases).
2. `find_all_marker_indices()` ordering correctness.
3. `build_consolidation_marker_message()` output format validation.
4. `_consolidate_markers()` with mocked agent_pool and Compressor:
   - Verify marker selection (oldest N-1).
   - Verify pool mutation preserves raw segments between markers.
   - Verify only marker indices are removed, not adjacent messages.
   - Verify logger `_consolidate_markers_in_jsonl()` called with correct params.
5. `_consolidate_markers_in_jsonl()`:
   - Verify intermediate marker lines removed from JSONL.
   - Verify raw message lines preserved.
   - Verify new L2 marker inserted at correct position.

### Integration Tests
1. Run 8+ compressions on a test agent, verify consolidation triggers automatically.
2. Verify pool after consolidation: markers M1..M6 removed, raw segments between them preserved, tail past M7 unchanged.
3. Verify JSONL after consolidation: intermediate marker lines removed, all raw messages retained.
4. Verify tail sync invariant holds post-consolidation.
5. Verify consolidation failure doesn't break normal operation (pool still has valid state).
6. Verify recursion guard works (mock scenario where compress_context called during consolidation).

### Manual Testing
1. Long-running session with frequent compression → observe marker count stays bounded (~2-3 markers instead of unbounded growth).
2. Check consolidated summaries are readable and preserve narrative coherence.
3. Inspect JSONL files after consolidation to verify raw messages preserved.

---

## 11. Future Enhancements (Out of Scope for This Task)

- **Multi-level hierarchy**: L1→L2→L3 with different prompts per level. Currently consolidation is "one level up" but doesn't track depth beyond cosmetic L2 header.
- **Async consolidation**: Run consolidation in background thread to avoid blocking post-compression. Low priority given infrequency.
- **Configurable threshold**: Already planned via settings constants.
- **Selective consolidation**: Only consolidate markers older than N hours or X tokens. Could save recent summaries that are more relevant.

---

## 12. Implementation Order Recommendation

1. Tasks 1–3 (utilities and helpers) — low risk, sets foundation.
2. Task 8 (settings constants) — needed by later tasks.
3. Task 4 (consolidation agent invocation) — reuses existing patterns.
4. Task 6 (JSONL consolidation helper in logger) — needed before main logic.
5. Task 5 (`_consolidate_markers()`) — core logic, depends on all above.
6. Task 7 (hook into `compress_context`) — wire it up.
7. Testing and review cycle.

Estimated complexity: **Medium-High**. ~300-400 lines of new code across 7 files. All following existing patterns but with careful attention to data preservation. No architectural changes required.

---

## Revision Notes (v2)

Changes from original plan addressing reviewer findings:

1. **CRITICAL FIX #1**: Pool mutation now iterates through history, replacing M0 and skipping only M1..M6 marker indices. All raw message segments between markers are preserved. Old slicing approach would have deleted them.
2. **CRITICAL FIX #2**: New `_consolidate_markers_in_jsonl()` method surgically removes intermediate marker lines from JSONL while preserving all raw messages. `reset_history(rewrite=True)` alone was insufficient.
3. **CRITICAL FIX #3**: Recursion guard implemented via module-level `_consolidating_agents` set with thread lock.
4. **MAJOR FIX #4**: Thread safety documented — pool mutation goes through setter → `rebuild_conversation()` which acquires `_compression_lock`.
5. **MAJOR FIX #5**: Marker extraction wrapped in try/except per marker; consolidation aborts entirely if no valid summaries extracted (no partial writes).
6. **MAJOR FIX #6**: Token size check before compressor invocation with configurable limit.
7. **MAJOR FIX #7**: Marker selection clarified: `consolidate_indices[0]` replaced, `consolidate_indices[1:]` removed.
8. **MINOR FIX #8**: Diagram updated to show correct structure with raw segments preserved.
9. **MINOR FIX #9**: Settings constants integrated throughout; no hardcoded `8`.
10. **MINOR FIX #10**: Logger return values checked and handled with fallbacks.
