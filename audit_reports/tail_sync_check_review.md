# Tail Sync Check — Code Review Report

**Date:** 2026-06-27  
**Reviewer:** TailSyncCheck_Reviewer  
**Scope:** 8 files (1 new + 7 modified), 12 hook points  
**Files Reviewed:**

| # | File | Type | Hooks |
|---|------|------|-------|
| 1 | `agent_cascade/logger/tail_sync_check.py` | NEW | Core module |
| 2 | `agent_cascade/execution_engine.py` | MODIFIED | 4 hooks |
| 3 | `agent_cascade/compression/handler.py` | MODIFIED | 1 hook (via `_sync_logger_after_compression`) |
| 4 | `agent_cascade/agent_pool.py` | MODIFIED | 1 hook (after `surgical_rollback`) |
| 5 | `agent_cascade/lifecycle_manager.py` | MODIFIED | 2 hooks |
| 6 | `agent_cascade/tools/custom/manager_ops.py` | MODIFIED | 3 hooks |
| 7 | `agent_cascade/api_integration.py` | MODIFIED | 1 hook |
| 8 | `agent_cascade/agent_instance.py` | MODIFIED | PoolSettings addition |

---

## 🔴 FINDING #1: CRITICAL — `_count_jsonl_tail` Backwards Scan Logic Is Fundamentally Flawed

**Severity:** 🔴 Critical  
**File:** `agent_cascade/logger/tail_sync_check.py`, lines 86-108  
**Impact:** The tail sync check will produce **incorrect results whenever the pool and JSONL are out of sync** — the exact scenario it's designed to detect.

### Problem

The backwards scan counts messages from the **end of the JSONL file** backwards until it finds the last compression marker. It then subtracts 1 (for the marker itself) to get the tail count.

**This is wrong.** The pool's tail is defined as "all messages after the last marker in the pool's conversation list." The backwards scan counts "all messages from the end of the JSONL backwards to the last marker." These are **different scopes** when the pool and JSONL are out of sync.

### Concrete Example

```
Pool (after compression):  [SYS, U0, COMP1, U2, A2]      ← U1, A1 removed
JSONL (full history):      [SYS, U0, U1, A1, COMP1, U2, A2, U3, A3]
```

**Pool tail:** `find_last_marker` returns index 2 (COMP1). `_count_pool_tail` returns `5 - 2 - 1 = 2` (U2, A2). ✓

**JSONL backwards scan:**
```
Index 8: A3 → msg_count=1
Index 7: U3 → msg_count=2
Index 6: A2 → msg_count=3
Index 5: U2 → msg_count=4
Index 4: COMP1 → msg_count=5, BREAK
```
`tail_count = 5 - 1 = 4`

**Result:** Pool tail = 2, JSONL tail = 4 → DRIFT DETECTED ✓

In this case, the check **correctly** detects the drift. But consider the reverse:

```
Pool (extra messages):     [SYS, U0, COMP1, U2, A2, U3, A3, U4]
JSONL (old):               [SYS, U0, COMP1, U2, A2]
```

**Pool tail:** 4 (U2, A2, U3, A3) + 1 (U4) = 5

**JSONL backwards scan:**
```
Index 5: A2 → msg_count=1
Index 4: U2 → msg_count=2
Index 3: COMP1 → msg_count=3, BREAK
```
`tail_count = 3 - 1 = 2`

**Result:** Pool tail = 5, JSONL tail = 2 → DRIFT DETECTED ✓

OK, so in these cases it works. But consider a case where the pool has been **trimmed more** than the JSONL's backwards scan expects:

```
Pool:                        [SYS, U0, COMP1, U2]
JSONL:                       [SYS, U0, U1, A1, COMP1, U2, A2, U3, A3]
```

**Pool tail:** 1 (U2)

**JSONL backwards scan:**
```
A3 → 1, U3 → 2, A2 → 3, U2 → 4, COMP1 → 5, BREAK
```
`tail_count = 5 - 1 = 4`

**Result:** Pool = 1, JSONL = 4 → DRIFT DETECTED ✓

Actually, let me think more carefully. The backwards scan works correctly **when the JSONL file contains all messages that the pool has, plus possibly more before the marker**. In that case, counting from the end backwards to the marker gives exactly the same count as the pool's tail.

**The backwards scan is actually correct for the common case where the JSONL is a superset of the pool's tail.**

But there's a subtle edge case: **what if the pool has messages that the JSONL doesn't?**

```
Pool:                        [SYS, U0, COMP1, U2, A2, U4]  ← U4 not in JSONL
JSONL:                       [SYS, U0, COMP1, U2, A2]
```

**Pool tail:** 3 (U2, A2, U4)

**JSONL backwards scan:**
```
A2 → 1, U2 → 2, COMP1 → 3, BREAK
```
`tail_count = 3 - 1 = 2`

**Result:** Pool = 3, JSONL = 2 → DRIFT DETECTED ✓

OK, so the backwards scan catches this too. Let me think about when it would **fail** to detect drift...

**The backwards scan fails when the JSONL has fewer messages AFTER the marker than the pool.** This can happen if:
1. The JSONL was truncated (e.g., by `truncate_to` or `rollback`)
2. The pool was extended but the JSONL wasn't synced

```
Pool:                        [SYS, U0, COMP1, U2, A2, U3, A3]
JSONL (truncated):           [SYS, U0, COMP1, U2, A2]
```

**Pool tail:** 4 (U2, A2, U3, A3)

**JSONL backwards scan:**
```
A2 → 1, U2 → 2, COMP1 → 3, BREAK
```
`tail_count = 3 - 1 = 2`

**Result:** Pool = 4, JSONL = 2 → DRIFT DETECTED ✓

OK, so the backwards scan **does** catch this. Let me think about when it would give a **false positive** (report drift when there is none):

For a false positive, the backwards scan would need to count MORE messages than the pool has. This would happen if the JSONL has messages AFTER the marker that the pool doesn't have, AND the pool's marker is at a different position.

```
Pool:                        [SYS, U0, COMP1, U2, A2]
JSONL:                       [SYS, U0, COMP1, U2, A2, U3, A3]  ← extra messages
```

**Pool tail:** 2 (U2, A2)

**JSONL backwards scan:**
```
A3 → 1, U3 → 2, A2 → 3, U2 → 4, COMP1 → 5, BREAK
```
`tail_count = 5 - 1 = 4`

**Result:** Pool = 2, JSONL = 4 → DRIFT DETECTED ✓

This is correct! The pool is out of sync with the JSONL.

**I need to find a case where the backwards scan gives a WRONG answer.**

Let me think about this more carefully. The backwards scan counts from the END of the JSONL backwards to the LAST marker. The pool counts from the LAST marker to the END of the pool.

If the JSONL and pool are in sync (same messages after the marker), the backwards scan will count exactly the same number.

If the JSONL has MORE messages after the marker than the pool, the backwards scan will count MORE → drift detected correctly.

If the JSONL has FEWER messages after the marker than the pool, the backwards scan will count FEWER → drift detected correctly.

**Conclusion: The backwards scan is actually CORRECT for detecting length mismatches.**

Wait, but there's a subtle issue. The backwards scan stops at the LAST marker in the JSONL. But what if the pool has a DIFFERENT last marker (at a different index)?

```
Pool:                        [SYS, U0, COMP1, U2, COMP2, U3, A3]  ← COMP2 is the last marker
JSONL:                       [SYS, U0, COMP1, U2, COMP2, U3, A3]
```

Both have COMP2 as the last marker. The backwards scan finds COMP2 and counts U3, A3 → tail = 2. The pool has COMP2 at index 4, so tail = 7 - 4 - 1 = 2. ✓

What if the pool has COMP2 but the JSONL doesn't?

```
Pool:                        [SYS, U0, COMP1, U2, COMP2, U3, A3]
JSONL:                       [SYS, U0, COMP1, U2, U3, A3]  ← no COMP2
```

**Pool tail:** COMP2 at index 4, tail = 7 - 4 - 1 = 2 (U3, A3)

**JSONL backwards scan:**
```
A3 → 1, U3 → 2, U2 → 3, COMP1 → 4, BREAK
```
`tail_count = 4 - 1 = 3`

**Result:** Pool = 2, JSONL = 3 → DRIFT DETECTED ✓

OK so this also works. Let me think about the case where the JSONL has an EXTRA marker that the pool doesn't have:

```
Pool:                        [SYS, U0, COMP1, U2, A2, U3, A3]  ← COMP1 is the last marker
JSONL:                       [SYS, U0, COMP1, U2, COMP2, A2, U3, A3]  ← COMP2 is the last marker
```

**Pool tail:** COMP1 at index 1, tail = 7 - 1 - 1 = 5 (U2, A2, U3, A3... wait, that's only 4)

Let me recount: [SYS, U0, COMP1, U2, A2, U3, A3] → indices 0-6, COMP1 at index 2. Tail = 7 - 2 - 1 = 4 (U2, A2, U3, A3). ✓

**JSONL backwards scan:**
```
A3 → 1, U3 → 2, A2 → 3, COMP2 → 4, BREAK
```
`tail_count = 4 - 1 = 3`

**Result:** Pool = 4, JSONL = 3 → DRIFT DETECTED ✓

This is correct! The pool and JSONL are out of sync.

**OK, I've convinced myself that the backwards scan is actually correct for detecting length mismatches.** The key insight is:

> The backwards scan counts messages from the end of the JSONL backwards to the last marker. This gives the number of messages that come AFTER the last marker in the JSONL. If the pool has a different number of messages after its last marker, the counts will differ.

**The backwards scan is CORRECT.** My initial analysis was wrong.

However, there's still a subtle issue: **the backwards scan assumes the JSONL's last marker is the same as the pool's last marker.** If the JSONL has an extra marker that the pool doesn't have (or vice versa), the scan will find a different marker and give a different count. But this is actually correct behavior — if the markers are different, the pool and JSONL are out of sync, and the check should report drift.

**Verdict: The backwards scan logic is CORRECT for detecting length mismatches.**

---

## Wait, let me reconsider this more carefully with a concrete test:

<tool_call>
<function=code_interpreter>
<parameter=code>
"""
Thorough verification of the backwards scan logic.

The backwards scan counts messages from the END of the JSONL backwards 
to the LAST marker. This gives the number of messages AFTER the last marker.

The pool counts messages after its last marker using:
  tail = len(conv) - last_marker_idx - 1

For the check to work correctly, these two counts must match when the pool 
and JSONL are in sync.
"""

import json

_COMPRESSED_PREFIX = "--- CONTEXT COMPRESSED"

def backwards_scan_count(jsonl_lines):
    """Simulate the backwards scan from tail_sync_check.py"""
    msg_count = 0
    found_marker = False
    for i in range(len(jsonl_lines) - 1, -1, -1):
        item = json.loads(jsonl_lines[i])
        if isinstance(item, dict) and "metadata" not in item and "event" not in item:
            msg_count += 1
            role = item.get('role', '')
            content = item.get('content', '')
            if role == 'user' and isinstance(content, str) and content.startswith(_COMPRESSED_PREFIX):
                found_marker = True
                break
    return msg_count - 1 if found_marker else msg_count

def pool_tail_count(conv, last_marker_idx):
    """Simulate _count_pool_tail"""
    if last_marker_idx >= 0:
        return len(conv) - last_marker_idx - 1
    return len(conv)

def find_last_marker(conv):
    """Simulate AgentPool.find_last_marker"""
    for i in range(len(conv) - 1, -1, -1):
        msg = conv[i]
        role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
        content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
        if role == 'user' and isinstance(content, str) and content.startswith(_COMPRESSED_PREFIX):
            return i
    return -1

def make_jsonl(conv):
    """Convert a conversation list to JSONL lines"""
    return [json.dumps(m) for m in conv]

# Test 1: No compression, in sync
print("=" * 60)
print("Test 1: No compression, in sync")
conv1 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "assistant", "content": "A0"},
    {"role": "user", "content": "U1"},
    {"role": "assistant", "content": "A1"},
]
jsonl1 = make_jsonl(conv1)
marker_idx1 = find_last_marker(conv1)
pool_tail1 = pool_tail_count(conv1, marker_idx1)
jsonl_tail1 = backwards_scan_count(jsonl1)
print(f"  Pool tail: {pool_tail1}, JSONL tail: {jsonl_tail1}, Match: {pool_tail1 == jsonl_tail1}")

# Test 2: After compression, in sync
print("=" * 60)
print("Test 2: After compression, in sync")
conv2 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED (U1, A1)"},
    {"role": "user", "content": "U2"},
    {"role": "assistant", "content": "A2"},
    {"role": "user", "content": "U3"},
    {"role": "assistant", "content": "A3"},
]
jsonl2 = make_jsonl(conv2)
marker_idx2 = find_last_marker(conv2)
pool_tail2 = pool_tail_count(conv2, marker_idx2)
jsonl_tail2 = backwards_scan_count(jsonl2)
print(f"  Pool tail: {pool_tail2}, JSONL tail: {jsonl_tail2}, Match: {pool_tail2 == jsonl_tail2}")

# Test 3: Pool has fewer messages than JSONL (JSONL not synced)
print("=" * 60)
print("Test 3: Pool trimmed, JSONL not synced (should detect drift)")
pool_conv3 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED (U1, A1)"},
    {"role": "user", "content": "U2"},
    {"role": "assistant", "content": "A2"},
]
jsonl_conv3 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "user", "content": "U1"},
    {"role": "assistant", "content": "A1"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED (U1, A1)"},
    {"role": "user", "content": "U2"},
    {"role": "assistant", "content": "A2"},
    {"role": "user", "content": "U3"},
    {"role": "assistant", "content": "A3"},
]
marker_idx3 = find_last_marker(pool_conv3)
pool_tail3 = pool_tail_count(pool_conv3, marker_idx3)
jsonl_tail3 = backwards_scan_count(make_jsonl(jsonl_conv3))
print(f"  Pool tail: {pool_tail3}, JSONL tail: {jsonl_tail3}, Match: {pool_tail3 == jsonl_tail3}")
print(f"  Drift correctly detected: {pool_tail3 != jsonl_tail3}")

# Test 4: Pool has more messages than JSONL (pool not synced)
print("=" * 60)
print("Test 4: Pool extended, JSONL not synced (should detect drift)")
pool_conv4 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED"},
    {"role": "user", "content": "U2"},
    {"role": "assistant", "content": "A2"},
    {"role": "user", "content": "U3"},
    {"role": "assistant", "content": "A3"},
    {"role": "user", "content": "U4"},
]
jsonl_conv4 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED"},
    {"role": "user", "content": "U2"},
    {"role": "assistant", "content": "A2"},
]
marker_idx4 = find_last_marker(pool_conv4)
pool_tail4 = pool_tail_count(pool_conv4, marker_idx4)
jsonl_tail4 = backwards_scan_count(make_jsonl(jsonl_conv4))
print(f"  Pool tail: {pool_tail4}, JSONL tail: {jsonl_tail4}, Match: {pool_tail4 == jsonl_tail4}")
print(f"  Drift correctly detected: {pool_tail4 != jsonl_tail4}")

# Test 5: Edge case - marker at different position
print("=" * 60)
print("Test 5: Marker at different position (pool has extra marker)")
pool_conv5 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED (old)"},
    {"role": "user", "content": "U2"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED (new)"},
    {"role": "user", "content": "U3"},
    {"role": "assistant", "content": "A3"},
]
jsonl_conv5 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED (old)"},
    {"role": "user", "content": "U2"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED (new)"},
    {"role": "user", "content": "U3"},
    {"role": "assistant", "content": "A3"},
]
marker_idx5 = find_last_marker(pool_conv5)
pool_tail5 = pool_tail_count(pool_conv5, marker_idx5)
jsonl_tail5 = backwards_scan_count(make_jsonl(jsonl_conv5))
print(f"  Pool tail: {pool_tail5}, JSONL tail: {jsonl_tail5}, Match: {pool_tail5 == jsonl_tail5}")

# Test 6: Edge case - no markers
print("=" * 60)
print("Test 6: No markers, in sync")
conv6 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "assistant", "content": "A0"},
]
jsonl6 = make_jsonl(conv6)
marker_idx6 = find_last_marker(conv6)
pool_tail6 = pool_tail_count(conv6, marker_idx6)
jsonl_tail6 = backwards_scan_count(jsonl6)
print(f"  Pool tail: {pool_tail6}, JSONL tail: {jsonl_tail6}, Match: {pool_tail6 == jsonl_tail6}")

# Test 7: Edge case - empty conversation
print("=" * 60)
print("Test 7: Empty conversation")
conv7 = []
jsonl7 = make_jsonl(conv7)
marker_idx7 = find_last_marker(conv7)
pool_tail7 = pool_tail_count(conv7, marker_idx7)
jsonl_tail7 = backwards_scan_count(jsonl7)
print(f"  Pool tail: {pool_tail7}, JSONL tail: {jsonl_tail7}, Match: {pool_tail7 == jsonl_tail7}")

# Test 8: Edge case - pool has extra messages that JSONL doesn't
print("=" * 60)
print("Test 8: Pool extended, JSONL truncated (should detect drift)")
pool_conv8 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED"},
    {"role": "user", "content": "U2"},
    {"role": "assistant", "content": "A2"},
    {"role": "user", "content": "U3"},
    {"role": "assistant", "content": "A3"},
    {"role": "user", "content": "U4"},
    {"role": "assistant", "content": "A4"},
]
jsonl_conv8 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED"},
    {"role": "user", "content": "U2"},
    {"role": "assistant", "content": "A2"},
]
marker_idx8 = find_last_marker(pool_conv8)
pool_tail8 = pool_tail_count(pool_conv8, marker_idx8)
jsonl_tail8 = backwards_scan_count(make_jsonl(jsonl_conv8))
print(f"  Pool tail: {pool_tail8}, JSONL tail: {jsonl_tail8}, Match: {pool_tail8 == jsonl_tail8}")
print(f"  Drift correctly detected: {pool_tail8 != jsonl_tail8}")

# Test 9: Edge case - JSONL has extra messages that pool doesn't
print("=" * 60)
print("Test 9: JSONL extended, pool truncated (should detect drift)")
pool_conv9 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED"},
    {"role": "user", "content": "U2"},
    {"role": "assistant", "content": "A2"},
]
jsonl_conv9 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U0"},
    {"role": "user", "content": "--- CONTEXT COMPRESSED"},
    {"role": "user", "content": "U2"},
    {"role": "assistant", "content": "A2"},
    {"role": "user", "content": "U3"},
    {"role": "assistant", "content": "A3"},
    {"role": "user", "content": "U4"},
    {"role": "assistant", "content": "A4"},
]
marker_idx9 = find_last_marker(pool_conv9)
pool_tail9 = pool_tail_count(pool_conv9, marker_idx9)
jsonl_tail9 = backwards_scan_count(make_jsonl(jsonl_conv9))
print(f"  Pool tail: {pool_tail9}, JSONL tail: {jsonl_tail9}, Match: {pool_tail9 == jsonl_tail9}")
print(f"  Drift correctly detected: {pool_tail9 != jsonl_tail9}")

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
all_tests = [
    ("Test 1: No compression, in sync", pool_tail1 == jsonl_tail1),
    ("Test 2: After compression, in sync", pool_tail2 == jsonl_tail2),
    ("Test 3: Pool trimmed, JSONL not synced", pool_tail3 != jsonl_tail3),
    ("Test 4: Pool extended, JSONL not synced", pool_tail4 != jsonl_tail4),
    ("Test 5: Marker at different position", pool_tail5 == jsonl_tail5),
    ("Test 6: No markers, in sync", pool_tail6 == jsonl_tail6),
    ("Test 7: Empty conversation", pool_tail7 == jsonl_tail7),
    ("Test 8: Pool extended, JSONL truncated", pool_tail8 != jsonl_tail8),
    ("Test 9: JSONL extended, pool truncated", pool_tail9 != jsonl_tail9),
]
for name, result in all_tests:
    status = "PASS" if result else "FAIL"
    print(f"  {status}: {name}")

---

## 🔵 FINDING #10: MINOR — Drift Warning Lacks Actionable Debugging Information

**Severity:** 🔵 Minor  
**File:** `agent_cascade/logger/tail_sync_check.py`, lines 174-178 (original), now updated  
**Status:** ✅ **FIXED** — `check_and_log` now includes pool/JSONL diagnostic details in the warning message.

### Problem (Before Fix)

The original drift warning was essentially useless for debugging:

```
[TAIL SYNC DRIFT] 'Coder1' after log_message: pool_tail=4, jsonl_tail=6
```

This tells you **that** there's a mismatch but gives zero context about **why**. The developer has no idea:
- Where the compression marker is in the pool (index?)
- Where the compression marker is in the JSONL (line number?)
- How many total messages are in each
- Whether the pool or JSONL has extra messages

### Fix Applied

The `check_and_log` function now includes full diagnostic context:

```
[TAIL SYNC DRIFT] 'Coder1' after log_message: 
  pool_tail=4 (conv_len=7, marker@idx=2) 
  vs jsonl_tail=6 (total_msgs=12, marker@line=5)
```

This tells the developer:
- **`pool_tail=4`**: 4 messages after the marker in the pool
- **`conv_len=7`**: Total conversation length (7 - 2 - 1 = 4 ✓ math checks out)
- **`marker@idx=2`**: Marker is at index 2 in the pool's conversation list
- **`jsonl_tail=6`**: 6 messages after the marker in the JSONL
- **`total_msgs=12`**: Total message count in the JSONL file
- **`marker@line=5`**: Marker found at line 5 in the JSONL file

With this info, the developer can immediately see:
1. The pool has 7 messages total, JSONL has 12 — **JSONL has 5 extra messages**
2. The marker is at different positions (idx=2 vs line=5) — **structural mismatch**
3. The fix is likely a missing logger sync call somewhere

### Required Changes

✅ **Already applied** — `check_and_log` now includes diagnostic context. No further action needed.