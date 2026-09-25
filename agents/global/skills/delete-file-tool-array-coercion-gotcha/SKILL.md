---
name: delete-file-tool-array-coercion-gotcha
description: AgentCascade delete_file tool silently coerces a JSON array of paths into one string, so batch deletion deletes nothing — always delete one path per call.
source: auto-generated
version: "1.0.0"
triggers:
  - "delete_file multiple files"
  - "batch delete scratch files"
  - "No files matched"
  - "delete_file array"
  - "clean up temp files"
generated_by: coder
generated_from_task: "todo165 delete_file backslash fix — cleanup of revert-proof scratch .bak files"
---

## Goal
Delete multiple files reliably via the AgentCascade `delete_file` tool without silently deleting nothing.

## The gotcha
The `delete_file` tool schema declares `path` as a **single string** (plus an optional `include` glob filter). If you pass a **JSON array** of paths — e.g. `"path": ["a.bak", "b.bak"]` — the harness **coerces the whole array into one literal string** (the JSON text, or the first element), which matches no real file. The tool then returns something like `No files matched` / deletes 0 entries, and you waste turns re-trying.

This is distinct from the *production* `OperationManager.delete_file(paths=[...])` API, which genuinely accepts a list — that's the code path under test in `tests/test_delete_file.py`. The **tool-call interface** exposed to agents does NOT behave that way.

## Procedure
### Step 1 — Delete one path per call
Issue a separate `delete_file` invocation per file, each with a single-string `path`:
```json
{"name": "delete_file", "arguments": {"path": "logs/scratch/a.bak", "justification": "..."}}
```
### Step 2 — For many files in one dir, use the `include` glob filter
Pass the base directory as `path` and a comma-separated `include` pattern to keep only matches (mirrors `list_dir` semantics; simple globs only, no `**`):
```json
{"name": "delete_file", "arguments": {"path": "logs/backups/build165", "include": "*.bak", "justification": "..."}}
```
### Step 3 — Verify before assuming success
A successful delete returns `OK: Deleted N of N`. If you see `No files matched` or `0 of N`, the path was wrong (likely an array coercion) — do NOT retry the same call; switch to single-path calls.

## Tips
- **Concurrent deletes on the same file are not supported** (no per-file fs lock); don't fire parallel `delete_file` calls targeting overlapping paths in one block.
- Deletions move files to a backup folder, so they're restorable — but a "0 matched" result means nothing was moved either.
- The `include` filter is applied **within the base directory of each path** (like `list_dir`), not as an absolute glob.
- Related: [[pytest-ini-addopts-xdist-serial-run]] for the sibling "silently does less than you asked" class of harness gotchas.
