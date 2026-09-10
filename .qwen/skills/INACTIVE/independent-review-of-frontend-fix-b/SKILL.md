---
name: independent-review-of-frontend-fix-b
description: Independent review of frontend code changes focusing on correctness (no stale UI) and test quality, particularly for gating cache invalidation based on message type to prevent spurious full DOM rebuilds
source: auto-generated
version: "1.0.0"
triggers:
  - "independent review"
  - "frontend Fix B"
  - "gating invalidateAllPanelCaches"
  - "data.type state"
  - "no stale UI"
  - "test quality"
  - "full DOM rebuild"
generated_by: fixB_review1
generated_from_task: "Independent review of frontend Fix B for a sub-agent turn-end full-rebuild burst in AgentCascade web_ui/app.js before commit. The fix gates invalidateAllPanelCaches on data.type==='state' so routine 'done' frames no longer force a full DOM rebuild. Focus on correctness (no stale UI) and test quality."
---

## Goal

Perform independent review of frontend code changes to ensure correctness (no stale UI) and test quality, particularly for optimizations that gate cache invalidation on message types to prevent spurious full DOM rebuilds.

## Procedure

### Step 1 — Read Actual Source Code

Use `read_file` to examine the exact changed lines and related logic. Never review blind or trust summaries.

### Step 2 — Cross-Reference Backend Message Types

Check backend code to confirm when `'state'` vs `'done'` frames are sent. Use `grep` to find all occurrences.

### Step 3 — Verify Gating Logic

Confirm that:
- `'state'` frames (genuine resets) trigger invalidation
- `'done'` frames (routine updates) skip invalidation
- Edge cases are handled (halted agents, resync, manual reset)

### Step 4 — Ensure Cache Consistency

Verify that after renders, cache values are updated to actual counts. Incremental rendering should work without invalidation.

### Step 5 — Validate Regression Tests

Tests should spy on real functions and assert both directions:
- `'state'` → invalidate ✅
- `'done'` → skip ❌

### Step 6 — Provide Verdict

Numbered findings with severity (🔴 Critical / 🟠 Major / 🟡 Minor / 🔵 Nit). Final verdict: **PASS**, **NEEDS WORK**, or **FAIL**.

## Tips

- The sentinel `999999999` triggers full rebuilds — verify it's only set for genuine resets
- Recovery paths must not rely on skipped invalidation
- Be blunt but constructive; cite exact lines

## Decision Format

Every review MUST contain one of: **PASS**, **NEEDS WORK**, or **FAIL**