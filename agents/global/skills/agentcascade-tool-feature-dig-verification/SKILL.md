---
name: agentcascade-tool-feature-dig-verification
description: DIG-phase verification for AgentCascade tool features — confirm a feature is visible to the LLM (dna.py TOOL_METADATA) AND actually fires in production (real data path, not a fixture that bypasses discover).
source: auto-generated
version: "1.0.0"
triggers:
  - "agentcascade tool feature"
  - "dna.py TOOL_METADATA"
  - "dig phase tool plan"
  - "feature not firing tool"
  - "verify tool schema"
  - "scan_skills load_skill"
generated_by: researcher
generated_from_task: "DIG plan for load_skill re-enable + scan_skills include-disabled"
---

## Goal
Before building an AgentCascade tool feature, verify it is (a) visible to the LLM and (b) actually fires in production — not merely present in code or a test.

## Procedure
### Step 1 — Check BOTH schema surfaces (the LLM sees TOOL_METADATA, not the tool class)
The LLM-facing schema is built from `agent_cascade/prompts/dna.py::TOOL_METADATA` (single source of truth; `agent_cascade/tools/_agent_instance_proxy.py:61-69` raises `ValueError` if a tool is missing there). A param declared in the tool class's `parameters` dict (e.g. `ScanSkills.parameters`) is DEAD for the LLM unless it is ALSO in `TOOL_METADATA`. For any "add a param / new behavior" task, diff the tool-class `parameters` against `TOOL_METADATA['<tool>']` — the gap is usually the real fix.

### Step 2 — Trace the manager data path; don't trust "code presence"
A feature can be wired end-to-end in code yet non-functional live. For skills: `discover()` drops disabled skills from `_skills_registry` (`skills/manager.py:1214`, `:1285`) and `disable_skill` → `invalidate_cache` forces the next `_ensure_discovered` to re-scan, so `get_all_metadata()` (registry-only, `:1383`) never contains a disabled skill in production. Trace the actual runtime path end-to-end, not just that a method exists.

### Step 3 — Interrogate the test that "proves" it
A test that manually injects state into `_skills_registry` (or any internal) and SKIPS `discover()`/the real entry point validates logic on an unrealistic fixture, not production behavior. Flag it as a fixture that masks the bug; add a new test that drives the real entry point (the tool's `call()` → `_ensure_discovered()`).

### Step 4 — Check prior investigation before reinvestigating
Grep `.agent_lessons/` (and the `[MEMORY HINT]` list) for the component + behavior. Prior-investigation memories (e.g. `[[skill-invalidation-phase1-implementation]]`, `[[hermetic-skill-manager-test-factory]]`, `[[skill-system-architecture-map]]`) often hold the exact gotcha and the canonical hermetic test-factory pattern.

## Tips
- "Wired in the tool class but missing from TOOL_METADATA" = invisible to the LLM — the classic gap.
- For hermetic SkillManager tests use the `make_hermetic_skill_manager` factory (redirects all four write roots, resets `_metrics`/`_disabled_names`/`_global_activity_turns`); to genuinely evict a disabled skill from an already-built registry you must `disable_skill` then re-`discover` (a status flip alone does NOT clear the registry).
- Default-arg changes to shared methods (e.g. `get_all_metadata(include_disabled=False)`) must keep the default path byte-identical; guard with a snapshot test and enumerate all hidden callers (advisor.py, propose_skill.py, `_rebuild_index`).
- Lock order is `_write_lock → _metrics_lock`; `_write_lock` is a reentrant `RLock` (get_all_metadata is called from `_rebuild_index` while discover holds it) — do not swap to a plain `Lock`.
- Run pytest from the host shell, not code_interpreter (see [[pytest-docker-subprocess-hang]]).
- Verify the reviewer's file:line citations back against source before trusting them (a reviewer once mis-cited a dedup block's line range).
