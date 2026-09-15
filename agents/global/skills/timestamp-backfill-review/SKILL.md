---
name: timestamp-backfill-review
description: Independent code review methodology for localized fixes that backfill Unix timestamps from ISO strings (e.g. during session/data load) — emphasis on timezone correctness and idempotency validation.
source: auto-generated
version: "1.0.0"
triggers:
  - "timestamp backfill"
  - "timezone correctness"
  - "ISO string to timestamp"
  - "session load"
---

## Goal

Systematic, evidence-based review of a localized fix that backfills Unix timestamps from ISO-formatted strings during session/data load, with focus on timezone correctness and idempotency guarantees.

## Procedure

1. **Verify timestamp source + format.** Identify where the timestamp strings originate in the log/data source. Confirm whether the ISO strings are naive local time, UTC with offset, or Zulu.
2. **Cross-reference live timestamp stamping.** Find how Unix timestamps are assigned in the live system. Key invariant: `message.ts = time.time()` should be called at commit time.
3. **Validate the backfill implementation.** Ensure idempotent, non-fatal, consistent behavior:
   ```python
   if msg.ts is not None:
       return
   msg.ts = datetime.datetime.fromisoformat(ts_raw).timestamp()
   ```
4. **Test the timezone assumption.** Verify backfilling produces matching timestamps in the same environment. If the system may be moved to a different timezone, document that as a constraint.
5. **Review integration points.** Confirm how backfilled timestamps affect downstream functionality (e.g. context-compression boundary markers that require `msg.ts` non-None).
6. **Check loop behavior.** Ensure the modified loop preserves skip-on-malformed behavior and causes no double-appends or silent drops.

## Tips

**Red flags (🔴 Critical):** backfill uses `utcfromtimestamp()` inconsistently; no guard against overwriting existing `ts`; parsing errors raise exceptions instead of being caught.
**Yellow flags (🟠 Major):** timestamps stored as aware UTC datetimes but backfill interprets naive local time; no documentation of the same-machine reload assumption.
**Green signals (✅ Pass):** `fromisoformat().timestamp()` on naive strings matches `time.time()` behavior; idempotent design (`if msg.ts is not None: return`); graceful degradation for malformed entries.

## Expected verdict structure

Your review MUST include: severity ratings (🔴/🟠/🟡/🔵) per issue; file:line references for all findings; concrete fixes/suggestions for every problem; final PASS / FAIL / NEEDS WORK verdict with required changes listed before it.

## Domain-specific knowledge

- `time.time()` returns seconds since epoch with local-time semantics.
- `datetime.fromisoformat(naive_string).timestamp()` interprets the string as **local time**.
- If a log is created on machine A and loaded on machine B in a different timezone, backfill will be incorrect — document this constraint.
- Downstream features (e.g. context-compression boundary markers) may require `msg.ts` non-None; otherwise they fall back to a generic label.
