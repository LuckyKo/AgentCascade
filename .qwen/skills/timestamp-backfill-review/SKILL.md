---
name: timestamp-backfill-review
description: Independent code review methodology for localized fixes involving timestamp backfilling from ISO strings to unix timestamps, with emphasis on timezone correctness and idempotency validation
source: auto-generated
version: "1.0.0"
triggers:
  - "timestamp backfill"
  - "timezone correctness"
  - "ISO string to timestamp"
  - "session load"
generated_by: tsbackfill_review1
generated_from_task: "Review a code fix that backfills timestamps from ISO strings to unix timestamps. Verify timezone handling and idempotency."
---

## Goal

Provide a systematic, evidence-based code review methodology for localized fixes that backfill Unix timestamps from ISO-formatted strings during session load or similar data restoration operations, with particular focus on timezone correctness and idempotency guarantees.

## Procedure

### Step 1 — Verify Timestamp Source and Format
Identify where timestamp strings originate in the log or data source. Confirm whether the ISO strings are naive local time, UTC with offset, or Zulu time.

### Step 2 — Cross-Reference Live Timestamp Stamping
Find how Unix timestamps are assigned in the live system. Key invariant: message.ts = time.time() should be called at commit time.

### Step 3 — Validate Backfill Implementation
Review the backfill code for correctness and safety. Ensure idempotent, non-fatal, and consistent behavior.

```python
if msg.ts is not None:
    return
msg.ts = datetime.datetime.fromisoformat(ts_raw).timestamp()
```

### Step 4 — Test Timezone Assumption
Verify that backfilling produces matching timestamps in the same environment. If the system may be moved to a different timezone, document this as a constraint.

### Step 5 — Review Integration Points
Confirm how backfilled timestamps affect downstream functionality, e.g., compression marker headers.

### Step 6 — Check Loop Behavior
Ensure the modified loop preserves skip-on-malformed behavior and no double-appends or silent drops occur.

## Tips

### Red Flags (🔴 Critical)
- Backfill uses utcfromtimestamp() inconsistently
- No guard against overwriting existing ts values
- Parsing errors raise exceptions instead of being caught

### Yellow Flags (🟠 Major)
- Timestamps are stored as aware UTC datetimes but backfill interprets naive local time
- No documentation of the same-machine reload assumption

### Green Signals (✅ Pass)
- fromisoformat().timestamp() on naive strings matches time.time() behavior
- Idempotent design: if msg.ts is not None: return
- Graceful degradation for malformed entries

## Expected Verdict Structure

Your review MUST include:
1. Severity ratings (🔴 Critical, 🟠 Major, 🟡 Minor, 🔵 Nit) for each issue
2. File:line references for all findings
3. Concrete fixes or suggestions for every identified problem
4. Final PASS/FAIL/NEEDS WORK verdict with required changes listed before it

## Domain-Specific Knowledge

- time.time() returns seconds since epoch representing local time semantics.
- datetime.datetime.fromisoformat(naive_string).timestamp() interprets the string as local time.
- If the log file is created on machine A and loaded on machine B with a different timezone, backfill will be incorrect. Document this constraint.
- Compression markers require msg.ts to be non-None; otherwise they fall back to "N messages summarized".