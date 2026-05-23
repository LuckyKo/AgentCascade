# Lessons: Compression-Related Message Duplication Fixes

## Date: 2026-05-22
## Author: MsgPoolDupsInvestigator (reviewed by Reviewer)

---

## Fix 1: Logger State Not Synced After Forced Compression Recovery

**File:** `agent_orchestrator.py` (~line 1117)
**Problem:** After forced compression, the code re-synced `messages` and `llm_messages` from the pool but did NOT call `logger_inst.update_history(compressed)` to sync the logger's internal `data["history"]`. This meant the logger's tracking was stale, causing future `update_history()` calls to see compressed messages as "new" and re-append them — message duplication.

**Fix:** Added `logger_inst.update_history(compressed)` after pool re-sync in the forced compression block, wrapped in try/except with a warning log on failure.

**Key Insight:** The logger's internal `data["history"]` is the dedup anchor. If it falls behind the pool state, every subsequent sync will duplicate messages. Logger sync must always follow pool mutations.

---

## Fix 2: Compression Failure Path — Rollback on Logger Notification Failure

**File:** `agent_cascade/compression/core.py` (~line 250)
**Problem:** After pool mutation (trim + insert marker), if the logger notification (`insert_compression_marker`) failed, the pool was already mutated but the logger had no record of it. This left the system in an inconsistent partial state — pool reflected compression but logger didn't, leading to future duplication on recovery.

**Fix:** 
1. Added a `deepcopy` snapshot of pre-mutation pool state BEFORE any mutation occurs.
2. Changed logger notification failure from a non-fatal warning to a rollback + failure return: if `insert_compression_marker` raises, the pool is restored to its pre-mutation state and `CompressResult(success=False)` is returned.

**Key Insight:** Pool and logger must be in lockstep. If the logger can't record the compression, the pool shouldn't reflect it either — otherwise recovery logic will see messages as "new" and duplicate them. The deepcopy snapshot is the right approach because list references are mutable; shallow copy would not protect against in-place mutations.

**Design Decision:** Changed logger failure from non-fatal (warning) to fatal (rollback + error). This is a defensible choice: it's better to retry compression than to proceed with an inconsistent state. The next iteration will attempt forced compression again anyway.

---

## Fix 3: Document Timestamp Identity Requirement

**File:** `agent_logger.py` — `_format_message()` and `update_history()` docstrings
**Problem:** The timestamp field in messages serves as the PRIMARY KEY for message identity in deduplication logic, but this was not documented. A future developer might "fix" what they perceive as a bug (e.g., removing or randomizing timestamps), which would break dedup entirely.

**Fix:** Added clear documentation comments explaining that timestamps are identity markers, not just metadata, and explicitly warned against modifying them.

---

## Architecture Principle Learned

**Three-Component Consistency Rule:**
The agent pool (source of truth), the orchestrator's working copies (`messages`/`llm_messages`), and the logger's internal tracking (`data["history"]`) must always be in lockstep after any mutation event. Any mutation that touches one component MUST touch all three, or the system will diverge and cause duplication on the next sync cycle.

**Checklist for Compression Events:**
1. [ ] Pool mutated? ✓ (atomic copy-and-replace)
2. [ ] Logger notified? ✓ (insert_compression_marker with rollback on failure)
3. [ ] Orchestrator working copies re-synced? ✓ (messages/llm_messages from pool)
4. [ ] Logger internal state synced? ✓ (update_history after forced compression recovery)