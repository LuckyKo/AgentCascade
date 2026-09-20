---
name: safe-metrics-pruning
description: Safely prune orphaned metrics entries while respecting retention rules for disabled, inactive, and platform-incompatible resources.
source: auto-generated
version: "1.0.0"
triggers:
  - metrics cleanup
  - orphan removal
  - stale data pruning
  - safe deletion
generated_by: reviewer
generated_from_task: "Plan review for adding metrics cleanup (prune dead/deactivated skill entries) to AgentCascade SkillManager lifecycle."
---

## Goal

Provide a repeatable pattern for removing stale metrics from persistent storage without accidentally deleting data for resources that should be retained (disabled, inactive, platform-incompatible, or temporarily missing).

## Procedure

### Step 1 — Compute live set with full coverage
Build the set of "live" resource names from multiple sources:
- All entries in the current registry
- All on-disk artifacts found by scanning configured roots (including both frontmatter `name:` and directory names)
- Any candidate/pending resources that may not yet be in the registry

**Rule:** A resource should be kept if it exists *anywhere* on disk, regardless of whether it's currently served or disabled. Only delete metrics for resources with **no on-disk artifact at all**.

### Step 2 — Validate scan integrity
Before performing any deletions:
- Track success/failure for each scanned root
- If **any** root raises an unhandled `OSError`/`PermissionError`, abort pruning entirely and log a clear error
- Do not modify persistent storage on partial failure

### Step 3 — Acquire locks in consistent order
Follow established lock ordering to avoid deadlocks:
1. Acquire `_write_lock` (or equivalent) first to snapshot the registry state
2. Then acquire `_metrics_lock` for modifications
3. Release in reverse order if needed

### Step 4 — Perform deletion with audit
For each key in metrics not found in live set:
- Delete the entry
- Log the removal: `logger.info("[METRICS] Pruned stale metrics for '%s'", name)`
- Track count of removed entries

### Step 5 — Flush only if changed
Only write the metrics file if at least one entry was removed. This avoids unnecessary I/O and lock contention.

## Tips

- **Name normalization:** Use lowercase comparisons on case-insensitive filesystems to avoid duplicates.
- **Symlink safety:** Skip symlinks during scanning to prevent following unintended targets.
- **Retention policies:** Remember that disabled, INACTIVE, platform-incompatible, and pending resources should retain their metrics for potential reactivation or audit.
- **Test coverage:** Include tests for edge cases: name mismatch, failed scan, concurrent modifications, and interaction with decision gates.

## Common Pitfalls

- **Calling prune too early** (e.g., before a candidate decision gate) — can destroy data needed for decisions.
- **Reading registry without lock** — leads to race conditions with concurrent mutations.
- **Proceeding after partial scan failure** — causes accidental deletion of metrics for resources on failed roots.
- **Forgetting both names** (frontmatter `name:` AND directory name) — can keep or delete incorrectly when they differ.
