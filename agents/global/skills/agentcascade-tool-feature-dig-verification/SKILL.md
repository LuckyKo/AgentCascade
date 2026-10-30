---
name: agentcascade-tool-feature-dig-verification
description: DIG-phase verification for AgentCascade tool features — confirm a feature is visible to the LLM (via the CORRECT schema path for that tool's kind) AND actually fires in production (real data path, not a fixture that bypasses discover).
source: auto-generated
version: 1.0.2
triggers:
  - "agentcascade tool feature"
  - "dna.py TOOL_METADATA"
  - "dig phase tool plan"
  - "feature not firing tool"
  - "verify tool schema"
  - "add param to tool"
  - "scan_skills load_skill"
generated_by: coder
generated_from_task: "Add preview param to scan_skills (todo 140); corrected the schema-source claim; post-t300 single-source-of-truth update"
---

## Goal
Before building an AgentCascade tool feature, verify it is (a) visible to the LLM and (b) actually fires in production — not merely present in code or a test.

## Procedure
### Step 1 — Check the RIGHT schema surface for that tool's KIND (proxy vs registered)
The LLM-facing function list is ALWAYS built from `BaseTool.function` (`tools/base.py:200-207`, returns `self.parameters`) consumed at `engine/helpers.py:160` and `agent.py:220`. Which `.parameters` a tool exposes depends on its kind:

- **Proxy tools — ONLY `call_agent` and `dismiss_agent`:** `_AgentInstanceFunctionProxy` (`tools/_agent_instance_proxy.py:45`) is instantiated for exactly these two (`agent_factory.py:50,57`). Their schema is built ENTIRELY from `dna.py::TOOL_METADATA` (no in-class schema); the proxy raises `ValueError` if a name is missing there. For THESE tools a param must be in TOOL_METADATA to be visible.
- **Registered tools — everything else (scan_skills, load_skill, read_file, …):** instantiated as the real class (`agent_factory.py:135-137`, or `agent.py:321` via `TOOL_REGISTRY[name](cfg)`). Their `.parameters` is a JSON schema built in-class, and a param there **IS** visible to the LLM and enforced at call time (`base.py:170-173`). **POST-t300 (2026-10-14): ALL registered tools source BOTH `description` and every param's description TEXT from `dna.py::TOOL_METADATA`** (single source of truth) — the class keeps the `type`/`enum`/`minimum`/`maximum`/nested-`properties` structure and pulls the description text from dna.py. So for a registered tool you must reconcile the dna.py entry AND keep the in-class schema in sync; the invariant is locked by `tests/test_skills_system.py::TestToolMetadataSingleSourceOfTruth`.

**Trap:** do NOT conflate the two kinds. Pre-t300 the "registered tools don't read dna.py — only a few (code_interpreter, doc_parser) imported description text" claim was true; it is now OUTDATED (all registered tools read dna.py). The KIND distinction that still matters: a proxy has NO in-class schema (dna.py is its only source), while a registered tool builds a real in-class JSON schema that sources its text from dna.py. Verify by tracing `agent_factory.py` (is this name a `_AgentInstanceFunctionProxy` or a direct subclass?) before trusting either surface.

### Step 2 — Trace the manager data path; don't trust "code presence"
A feature can be wired end-to-end in code yet non-functional live. For skills: `discover()` drops disabled skills from `_skills_registry` and `disable_skill` → `invalidate_cache` forces the next `_ensure_discovered` to re-scan, so `get_all_metadata()` (registry-only) never contains a disabled skill in production. Trace the actual runtime path end-to-end, not just that a method exists.

### Step 3 — Interrogate the test that "proves" it
A test that manually injects state into `_skills_registry` (or any internal) and SKIPS `discover()`/the real entry point validates logic on an unrealistic fixture, not production behavior. Flag it as a fixture that masks the bug; add a new test that drives the real entry point (the tool's `call()` → `_ensure_discovered()`).

### Step 4 — Check prior investigation before reinvestigating
Grep `.agent_lessons/` (and the `[MEMORY HINT]` list) for the component + behavior. Prior-investigation memories often hold the exact gotcha and the canonical hermetic test-factory pattern. See `lessons_tool_schema_refactor.md` for the post-t300 schema-source reality.

## Tips
- For registered tools, a schema test should assert against the class `parameters` / `.function` (the surface the LLM sees) AND, after t300, against dna.py TOOL_METADATA via the `TestToolMetadataSingleSourceOfTruth` snapshot — both must agree.
- When adding/changing a param on a registered tool, update BOTH the in-class JSON schema node and the dna.py TOOL_METADATA text; run the snapshot test to catch drift.
- For hermetic SkillManager tests use the `make_hermetic_skill_manager` factory (redirects all four write roots, resets `_metrics`/`_disabled_names`/`_global_activity_turns`); to genuinely evict a disabled skill from an already-built registry you must `disable_skill` then re-`discover` (a status flip alone does NOT clear the registry).
- Default-arg changes to shared methods must keep the default path byte-identical; guard with a snapshot test and enumerate all hidden callers.
- Lock order is `_write_lock → _metrics_lock`; `_write_lock` is a reentrant `RLock` — do not swap to a plain `Lock`.
- Run pytest from the host shell, not code_interpreter (see [[pytest-docker-subprocess-hang]]).
- Verify the reviewer's file:line citations back against source before trusting them.
