# Auto-Skill Generation System — Implementation Plan

## Status: Draft — Review Round 3

## Overview

Enable Agent Cascade agents to automatically propose new reusable skills after successfully completing tasks. The system reuses the existing **SkillManager**, **SkillMatcher**, **Tier 1/2 loading**, and **SKILL.md format** — no architectural overhaul required.

---

## Existing Infrastructure (What We Already Have)

| Component | Location | Role |
|---|---|---|
| `SkillManager` | `agent_cascade/skills/manager.py` | Discovery, registration, Tier 1/2 loading, `resolve_load_skill()` |
| `SkillMatcher` | `agent_cascade/skills/matcher.py` | Inverted-index keyword matching (indexes `name` + `description`) |
| `parse_skill_file()` | `agent_cascade/skills/parser.py` | YAML frontmatter + markdown body parsing |
| `scan_skills` tool | `agent_cascade/tools/custom/scan_skills.py` | Agent-facing skill discovery (wraps `SkillManager.get_all_metadata()`) |
| Tool registration | `agent_cascade/tools/custom/__init__.py` | `@register_tool()` decorator pattern |
| Tool factory | `agent_cascade/agent_factory.py` | Registers tools per agent class |
| Tool metadata | `agent_cascade/prompts/dna.py` | `TOOL_METADATA` + `AVAILABLE_TOOLS` lists |
| Skill storage | `.qwen/skills/*/SKILL.md` | Active skills |
| Pending skills | `.qwen/pending-skills/` | Staging area (exists but empty) |
| Settings | `agent_cascade/settings.py` | `LOAD_SKILL_AUTO`, `SKILL_MATCH_THRESHOLD`, etc. |
| Example skill | `.qwen/skills/version-control/SKILL.md` | Reference format (has `source: manual`, `triggers` list) |
| Execution engine | `agent_cascade/execution_engine.py` | `run()` loop, tool execution, `_check_for_tool_calls_in_output()` |
| Agent instance | `agent_cascade/agent_instance.py` | `AgentInstance` dataclass with state machine |

---

## Design Decisions

### 1. Skill Definition Format

**No change to the existing format.** We add three optional frontmatter fields:

```yaml
---
name: python-unit-testing
description: Writing pytest unit tests with fixtures, parametrization, and mocking
source: auto-generated
version: "1.0.0"
triggers:
  - "pytest"
  - "unit test"
  - "test fixture"
  - "mock"
generated_by: coder
generated_from_task: "Write unit tests for the CSV parser module"
---
```

New fields:
- **`source`**: `"manual"` (default) or `"auto-generated"` — tracks provenance. Backward compatible: missing `source` defaults to `"manual"` in the parser.
- **`generated_by`**: Agent class that generated the skill (e.g., `coder`, `researcher`)
- **`generated_from_task`**: Original task description that triggered generation

The `triggers` field already exists in the codebase (seen in `version-control/SKILL.md`).

### 2. Skill Registry

**No new registry file.** The existing `SkillManager._skills_registry` dict is the single source of truth.

**Change A — Store `triggers` in registry** (critical fix):
The existing `_register_single()` (line 118-126) parses frontmatter but only stores `name`, `description`, `source`, `file_path`. Add `triggers`:
```python
# Inside _register_single(), line 118:
self._skills_registry[name] = {
    'name': name,
    'description': frontmatter.get('description', ''),
    'source': frontmatter.get('source', ''),
    'triggers': frontmatter.get('triggers', []),  # NEW
    'file_path': str(skill_file),
    '_priority': priority,
    '_parsed_data': parsed,
}
```

**Change B — Include `triggers` in `get_all_metadata()`** (critical fix):
The existing `get_all_metadata()` (line 166-178) returns only `name` and `description`. Extend it:
```python
# Inside get_all_metadata(), line 174:
result.append({
    'name': data.get('name', name),
    'description': data.get('description', ''),
    'triggers': data.get('triggers', []),  # NEW
})
```

**Change C — `build_index()` concatenates triggers** (critical fix):
The existing `build_index()` (line 46-59) tokenizes `f"{skill_name} {description}"`. Extend to include triggers:
```python
# Inside build_index(), line 51-52:
description = meta.get('description', '')
triggers = meta.get('triggers', [])
trigger_text = ' '.join(triggers) if isinstance(triggers, list) else ''
text = f"{skill_name} {description} {trigger_text}"  # NEW: includes triggers
```

**Change D — New method** (sync, since file I/O is blocking):
```python
def register_skill_from_content(
    self,
    skill_content: str,
    source: str = "auto-generated",
    validate: bool = True,
) -> Tuple[bool, List[str]]:
    """Register a skill from raw SKILL.md content.

    Writes to a temp file, calls _register_single() for parsing + indexing,
    then moves to final location. Updates file_path in registry after move.
    """
```

**Implementation sequence** (inside `register_skill_from_content()`):
```python
# 1. Write to temp pending location
import uuid
pending_dir = Path(f".qwen/pending-skills/{uuid.uuid4().hex}")
pending_dir.mkdir(parents=True, exist_ok=True)
pending_file = pending_dir / "SKILL.md"
pending_file.write_text(skill_content)

# 2. Parse and register (stores in registry pointing to temp path)
await self._register_single(pending_file, priority=_PRIORITY_SYSTEM)
meta = self._skills_registry[meta['name']]  # Get registered entry
frontmatter = meta['_parsed_data']['frontmatter']
name = frontmatter['name']
task_text = frontmatter.get('generated_from_task', '')

# 3. Validate
existing = set(self._skills_registry.keys()) - {name}
passed, errors = validate_skill(skill_content, name, existing, task_text)
if not passed:
    return False, errors

# 4. Promote if validated
if AUTO_SKILL_AUTO_PROMOTE:
    target_dir = Path(f".qwen/skills/{name}")
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "SKILL.md"
    pending_file.rename(target_file)
    self._skills_registry[name]['file_path'] = str(target_file)

# 5. Rebuild index (under lock)
with self._write_lock:
    self._rebuild_index()

return True, []
```

**Change E — Write lock**:
Add to `__init__`:
```python
import threading  # NEW import
# ...
def __init__(self):
    self._skills_registry: Dict[str, Dict[str, Any]] = {}
    self._matcher = SkillMatcher()
    self._write_lock = threading.Lock()  # NEW
```

Use around registration + rebuild:
```python
with self._write_lock:
    await self._register_single(skill_path)
    self._rebuild_index()
```

**Hot-reload**: `_rebuild_index()` already exists (line 130-136) and is called after `discover()`. We reuse it after any registration.

### 3. Trigger Mechanism

**Zero-cost reflection approach** — no extra LLM call. The trigger fires at the end of agent execution.

**Conditions** (ALL must be true):
1. Task completed successfully (agent state = `IDLE`, no error)
2. Agent executed ≥5 tool calls during the task (tracked via `executed_tools` list in execution engine). **Rationale**: ≥3 fires on almost every non-trivial task. ≥5 ensures the task was substantial enough to warrant a reusable skill.
3. No existing skill already matched the task above `SKILL_MATCH_THRESHOLD` (checked via `skill_manager.match_skills(task_text)`)
4. Rate-limited: max 1 skill proposal per agent instance per session (tracked via `AgentInstance._auto_skill_proposed` flag, reset when instance is created/reused)

**Session definition**: A "session" is the lifetime of an `AgentInstance` — from `find_or_create_instance()` until the instance is dismissed or the pool is cleared. The `_auto_skill_proposed` flag is set on the instance and persists across turns within the same instance.

**Implementation**: The trigger is checked in `execution_engine.py` at line ~4251 (after `_create_completed = True`, before WebUI update at line 4253, before `finally` block at line 4263).

**Hook code** (inserted at line 4251, after `_create_completed = True`):
```python
# ── Auto-skill reflection trigger ──
if AUTO_SKILL_ENABLED and not getattr(inst, '_auto_skill_proposed', False):
    # Count tool calls from FUNCTION role messages in final_resp
    tool_count = sum(1 for m in final_resp if msg_field(m, 'role', '') == FUNCTION)
    matches = skill_manager.match_skills(task_text) if skill_manager else []
    if (tool_count >= AUTO_SKILL_MIN_TOOL_CALLS and
        not matches and
        inst.state == AgentState.IDLE):
        # Load skill-creator explicitly
        if skill_manager:
            creator = skill_manager.load_full_instructions("skill-creator")
            if creator:
                prompt = (f"## Skill Reflection\n\n{creator}\n\n"
                          f"You completed a task using {tool_count} tool calls. "
                          f"If the approach could help future similar tasks, "
                          f"propose a reusable skill by calling propose_skill.")
                self._append_and_log(inst, self._make_user_message(prompt))
                # Run one more turn
                for extra_resp in self.run(inst):
                    if self._is_stopped(instance_name):
                        break
                    if isinstance(extra_resp, tuple) and len(extra_resp) == 2:
                        final_resp = extra_resp[0]
                    else:
                        final_resp = extra_resp
                inst._auto_skill_proposed = True
```

**Additional turn mechanism**: Re-enters `self.run(inst)` for one more iteration. The `run()` generator yields once per turn, so this adds exactly one extra turn. Tool count is derived from `final_resp` (FUNCTION role messages), which is already in scope.

### 4. Meta-Skill "skill-creator"

A SKILL.md that guides agents through the skill creation process. **Loaded explicitly** when the trigger fires (via direct call to `load_full_instructions("skill-creator")`, not via keyword matching).

**Location**: `.qwen/skills/skill-creator/SKILL.md`

**Content covers**:
- When to create a skill (pattern recognition guidance)
- How to write effective SKILL.md (frontmatter fields, body structure)
- Quality checklist (specific, actionable, testable)
- How to use the `propose_skill` tool

**Agent-specific adaptations**: The meta-skill includes a section per agent class with domain-specific guidance. The reflection prompt includes the agent's class name so the LLM can reference the relevant section.

### 5. Validation Mechanism

**New module**: `agent_cascade/skills/validator.py`

**API**:
```python
def validate_skill(
    skill_content: str,
    skill_name: str,
    existing_names: set,
    task_text: str = "",
) -> Tuple[bool, List[str]]:
    """Validate a proposed skill. Returns (passed, error_messages)."""
```

**Two-tier validation**:

**Tier 1 — Structural** (immediate, programmatic):
- Valid YAML frontmatter (use existing `parse_skill_file()` on temp file)
- Required fields present: `name` (snake_case, `[a-z][a-z0-9_-]*`), `description` (≥20 chars), `triggers` (list, ≥1 entry)
- Unique name (no duplicate in `existing_names`)
- Body non-empty and ≥100 characters
- Total file size ≤ `AUTO_SKILL_MAX_SIZE_KB` (15KB default)

**Tier 2 — Self-Match** (dry-run, only if task_text provided):
- Create a temporary `SkillMatcher` instance
- Call `matcher.build_index([{name, description, triggers}])` with only the proposed skill's data
- Query with `generated_from_task` text
- Check that the match score ≥ `AUTO_SKILL_PROMOTION_THRESHOLD` (0.3)
- This ensures the validation uses the **same scoring logic** as actual skill matching, avoiding asymmetry
- Threshold: `AUTO_SKILL_PROMOTION_THRESHOLD = 0.3` (higher than `SKILL_MATCH_THRESHOLD = 0.15` to ensure quality gate is stricter than discovery threshold)

**Lifecycle**:
```
proposed → .qwen/pending-skills/{uuid}/SKILL.md
   → validated (Tier 1 + Tier 2 pass)
   → promoted → .qwen/skills/{name}/SKILL.md  (if AUTO_SKILL_AUTO_PROMOTE=True)
   → _rebuild_index() called for hot-reload
```

When `AUTO_SKILL_AUTO_PROMOTE=False`, validated skills stay in `.qwen/pending-skills/` until manually moved to `.qwen/skills/`.

**Pending cleanup**: Stale pending files (>24 hours old) are cleaned up on startup.

### 6. Integration with Existing Agents

**New tool**: `propose_skill` — allows any agent to propose a new skill.

**Tool file**: `agent_cascade/tools/custom/propose_skill.py`
```python
from agent_cascade.tools.base import BaseTool, register_tool

@register_tool('propose_skill', allow_overwrite=True)
class ProposeSkill(BaseTool):
    name = 'propose_skill'
    description = (
        'Propose a new reusable skill for future tasks. '
        'Provide the full SKILL.md content including YAML frontmatter '
        'with name, description, and triggers fields.'
    )

    async def call(self, params: str, **kwargs) -> str:
        """Execute propose_skill.

        Args:
            params: JSON string with 'skill_content' (required) and
                    'test_task' (optional, for self-match validation).
        """
```

**Tool registration** (same pattern as `scan_skills`):
- Import in `agent_cascade/tools/custom/__init__.py`
- Add to `AVAILABLE_TOOLS` and `TOOL_METADATA` in `agent_cascade/prompts/dna.py`
- Register in `agent_cascade/agent_factory.py` (line ~127, after `scan_skills`)

**Execution engine hook**:
After the `for resp in self.run(inst):` loop completes (line ~4206), before the `finally` block:
```python
# After run loop, before returning:
if AUTO_SKILL_ENABLED and not inst._auto_skill_proposed:
    # Check conditions: ≥5 tools, success state, no existing match
    if trigger_conditions_met(inst, task_text, skill_manager):
        # Load skill-creator explicitly
        creator_instructions = skill_manager.load_full_instructions("skill-creator")
        # Append reflection prompt + creator instructions
        # Run one additional turn
        inst._auto_skill_proposed = True
```
This runs only on successful completion (no exception path), before the `finally` block at line ~4240.

### 7. Progressive Loading

**Reuses existing Tier 1/Tier 2 system.** No changes needed — auto-generated skills follow the same loading pipeline as manual skills.

**Token budget enforcement** (new settings, enforced in `resolve_load_skill()`):
```python
MAX_SKILL_INJECTION_TOKENS = 8000     # Max total skill injection chars per call
MAX_SKILLS_PER_CALL = 5               # Max concurrent skills loaded
AUTO_SKILL_RATE_LIMIT = 1             # Max auto-skill proposals per session per agent
```

The `resolve_load_skill()` method will be updated to cap results at `MAX_SKILLS_PER_CALL` and stop loading once `MAX_SKILL_INJECTION_TOKENS` is reached.

### 8. Storage Structure

```
.qwen/
├── skills/
│   ├── version-control/
│   │   └── SKILL.md
│   ├── skill-creator/              # NEW: meta-skill
│   │   └── SKILL.md
│   └── [auto-generated skills]/    # Promoted from pending
│       └── SKILL.md
└── pending-skills/
    └── {uuid}/                     # Staging area for proposed skills
        └── SKILL.md
```

**Concurrency**: File writes use atomic operations (write to temp, then rename). The `SkillManager` uses a `threading.Lock` (`self._write_lock`) around the `_register_single()` + `_rebuild_index()` path to prevent concurrent write corruption.

---

## Implementation Phases

### Phase 1: Foundation (Core Infrastructure)

**Files to create/modify**:
- `agent_cascade/skills/validator.py` — NEW: `validate_skill(skill_content, name, existing_names, task_text) -> Tuple[bool, List[str]]`
- `agent_cascade/skills/manager.py` — Changes:
  - `__init__`: add `import threading`, `self._write_lock = threading.Lock()`
  - `_register_single()`: store `triggers` in registry dict (line 118)
  - `get_all_metadata()`: include `triggers` in return (line 174)
  - NEW: `register_skill_from_content()` method (sync)
- `agent_cascade/skills/matcher.py` — `build_index()`: concatenate triggers text with name+description (line 51-52)
- `.qwen/skills/skill-creator/SKILL.md` — NEW: meta-skill definition

**Deliverables**:
- Skill validation module (Tier 1 structural + Tier 2 self-match via SkillMatcher)
- Skill registration method with write lock, triggers stored/indexed
- Meta-skill for guiding skill creation

### Phase 2: Tool & Trigger

**Files to create/modify**:
- `agent_cascade/tools/custom/propose_skill.py` — NEW: propose_skill tool
- `agent_cascade/tools/custom/__init__.py` — ADD: import `propose_skill`
- `agent_cascade/prompts/dna.py` — ADD: `propose_skill` to `AVAILABLE_TOOLS` + `TOOL_METADATA`
- `agent_cascade/agent_factory.py` — ADD: `propose_skill` registration
- `agent_cascade/execution_engine.py` — ADD: post-task trigger hook (after `run()` loop, before return)
- `agent_cascade/agent_instance.py` — ADD: `_auto_skill_proposed` flag to AgentInstance
- `agent_cascade/settings.py` — ADD: all auto-skill settings

**Deliverables**:
- `propose_skill` tool available to all agents
- Trigger mechanism in execution engine
- Rate limiting and token budget enforcement
- Tool budget caps in `resolve_load_skill()`

### Phase 3: Integration & Testing

**Files to create/modify**:
- `tests/test_skill_generation.py` — NEW: comprehensive test suite
- `agent_cascade/prompts/dna.py` — UPDATE: ensure `propose_skill` tool description is clear

**Test coverage**:
- Unit: validation (valid, invalid, edge cases)
- Unit: registration (new skill, duplicate, disk error)
- Unit: self-match (pass at 0.3, fail below 0.3, threshold edge cases)
- Integration: full propose → validate → promote flow
- Integration: rate limiting (2nd proposal in same session rejected)
- Integration: trigger conditions (≥3 tools, success state, no existing match)
- Integration: hot-reload (new skill discoverable without restart)

**Deliverables**:
- Full integration tested
- At least 3 auto-generated skills created and validated during testing (committed to repo for regression)

### Phase 4: Polish

- Skill deprecation mechanism
- Usage tracking (increment counter on each skill match, stored in registry metadata)
- Pruning stale/unused skills (Phase 4)
- Pending-skills cleanup on startup (>24h old files)
- User notification when a new skill is auto-generated
- Telemetry: log proposal, validation, promotion, rejection events

---

## Edge Cases & Failure Modes

| # | Scenario | Handling |
|---|---|---|
| 1 | Duplicate skill name | Validation rejects, suggests name variant |
| 2 | Corrupt YAML frontmatter | Validation catches, returns error to agent |
| 3 | No triggers defined | Validation requires ≥1 trigger |
| 4 | Skill too large | Size check (≤15KB) |
| 5 | Registry overflow (>50 skills) | Pruning mechanism in Phase 4 |
| 6 | Two agents propose same skill | UUID-based pending, dedup on promotion |
| 7 | Disk full | Try/except around file I/O, log warning, continue |
| 8 | Stale skills (never matched) | Usage tracking + pruning |
| 9 | Skill conflicts with existing | Name uniqueness check |
| 10 | Trigger fires on trivial task | ≥3 tool calls threshold prevents this |
| 11 | Agent proposes bad skill | Two-tier validation catches it |
| 12 | Pending skills accumulate | Auto-promote after validation; cleanup >24h old |
| 13 | Concurrent registration | `threading.Lock` on SkillManager write path |
| 14 | New skill not discoverable | Hot-reload via `_rebuild_index()` after promotion |

---

## Settings to Add

```python
# agent_cascade/settings.py

# ── Auto-Skill Generation ──
AUTO_SKILL_ENABLED: bool = True                     # Master toggle
AUTO_SKILL_MIN_TOOL_CALLS: int = 5                  # Min tool calls to trigger (raised from 3)
AUTO_SKILL_MAX_PER_SESSION: int = 1                 # Rate limit per agent/session
AUTO_SKILL_MAX_SIZE_KB: int = 15                    # Max SKILL.md size
AUTO_SKILL_PROMOTION_THRESHOLD: float = 0.3         # Min self-match score (stricter than SKILL_MATCH_THRESHOLD=0.15)
AUTO_SKILL_AUTO_PROMOTE: bool = True                # Auto-promote validated skills; when False, skills stay in pending until manually approved
MAX_SKILL_INJECTION_TOKENS: int = 8000              # Total skill chars per call
MAX_SKILLS_PER_CALL: int = 5                        # Max concurrent skills
```


---

## Estimated Effort

| Phase | Lines of Code | Files | Complexity |
|---|---|---|---|
| Phase 1 | ~180 | 4 files | Low |
| Phase 2 | ~150 | 6 files | Medium |
| Phase 3 | ~120 | 2 files | Low |
| Phase 4 | ~100 | 3 files | Medium |
| **Total** | **~550** | **~13 files** | **Low-Medium** |

---

## Success Criteria

1. An agent completes a task and proposes a new skill
2. The skill passes validation and is stored in `.qwen/pending-skills/`
3. After validation, it's promoted to `.qwen/skills/`
4. The skill is immediately discoverable via `scan_skills` and AUTO loading (hot-reload)
5. The skill matches correctly for similar future tasks
6. No more than 1 skill proposal per agent per session
7. Total skill injection stays under token budget
8. Graceful degradation: task flow continues if skill generation fails

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Skill bloat | Registry grows unmanageable | Rate limiting + pruning |
| Low-quality skills | Noise in AUTO matching | Two-tier validation |
| Token overhead | Prompt bloat | Budget caps + progressive loading |
| Agent confusion | Too many skills loaded | MAX_SKILLS_PER_CALL limit |
| Duplicate proposals | Wasted validation | Dedup check in pending-skills |
| Disk I/O failure | Skill lost | Try/except + logging, task continues |
| Concurrent writes | Registry corruption | `threading.Lock` on write path |