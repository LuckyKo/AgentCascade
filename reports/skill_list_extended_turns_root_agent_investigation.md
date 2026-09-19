# Investigation: loaded skills missing from the "list" during extended turns / for the root agent

**Todo:** todo.md:140 — "the skills loaded by the agent don't show up in the list during extended turns (or any skills for that matter for the root agent)"
**Date:** 2026-09-18 · **Investigator:** skill_list_research · **Mode:** Investigative (root cause)
**Confidence:** Root causes Confirmed; "which list" = High Confidence

---

## Executive Summary

The "list" the user means is the **`{loaded_skills}` rendered list inside the auto-skill
reflection prompt** (`prompts/dna.py:189`, `AUTO_SKILL_REFLECTION_PROMPT`). That prompt is
injected as a visible USER message **only during the auto-skill extended turns**, so it is the
one list that literally appears "during extended turns." It is populated from
`instance._loaded_skill_names`.

Two independent root causes make it show `(none)` / an incomplete list:

1. **Runtime-loaded skills are never tracked.** The `load_skill` tool
   (`tools/custom/load_skill.py`) enqueues each skill as a USER message and records telemetry,
   but **never updates `_loaded_skill_names`**. So any skill an agent loads mid-task via the
   tool (the primary self-augmentation mechanism) does not appear in the reflection list. Only
   creation-time (AUTO/explicit) skills do.

2. **The root/main agent never sets `_loaded_skill_names`.** The root agent is created by a
   completely separate path — `create_main_agent_instance` (`api_integration_pkg/runner.py:20`)
   — which only injects **self-augmentation** via `_inject_self_augmentation_skill` and then
   runs through `engine.run()` directly, **bypassing `_create_and_run_agent`** (the only place
   `_loaded_skill_names` is written). So the root agent's reflection list is always `(none)`,
   even though its system prompt *does* contain self-augmentation in the `## Active Skills`
   block (an internal inconsistency).

There is **no per-agent "loaded skills" field in the API/UI state** — web_ui only has a
telemetry "Skill Usage" table (aggregate metrics) and skill settings toggles. So the reflection
prompt list is the only place a user sees "which skills are loaded for this agent."

---

## Key Findings

### 1. WHERE is the "list of skills"?

**Primary (the one shown during extended turns):** the `{loaded_skills}` placeholder in
`AUTO_SKILL_REFLECTION_PROMPT`.

- Template: `agent_cascade/prompts/dna.py:185-204`; the list line is **line 189**:
  `'Skills loaded this run:\n{loaded_skills}\n\n'`.
- Renderer: `_build_auto_skill_reflection_prompt(loaded_skill_names, skill_creator_body,
  skill_manager)` — `agent_cascade/skills/manager.py:131-157`. Renders one line per name
  (`- name (avg 7.5/10, rated 4×)` or `- name (unrated)`), or **`(none)`** when the list is
  empty/None (line 152).
- Qualifier: `SkillManager.auto_skill_qualifies(inst, turns_effectuated, loaded_skill_names)`
  — `agent_cascade/skills/manager.py:1339-1381` (returns the built prompt or None).
- Injection into the conversation: `_try_auto_skill_extension` builds the prompt and appends it
  as a USER message — `agent_cascade/engine/core.py:232-338`, specifically **lines 324-328**
  (`user_msg = self._make_user_message(prompt)`; `_append_and_log`; `messages.append`;
  `llm_messages.append`). Because it is a normal USER message in the conversation, it is visible
  to the user in the UI during extended turns.

**Secondary (always present, not extended-turn-specific):** the `## Active Skills` block in the
system prompt.

- Builder: `_build_skills_block(loaded_skills)` — `agent_cascade/engine/helpers.py:412-443`.
- Injector: `_inject_skills_to_system_message(...)` — `helpers.py:446-514` (idempotent; skips if
  `## Active Skills` already present).
- This block is correct for creation-time skills and is NOT the "list during extended turns."

### 2. HOW does an agent record which skills it has loaded?

Single source of truth: **`AgentInstance._loaded_skill_names`** (Optional[List[str]], default
None) — `agent_cascade/agent_instance.py:263-264`.

Full write/read map (verified by repo-wide grep — exactly 3 code references):

| Location | Op | Detail |
|---|---|---|
| `agent_instance.py:263` | field def | default None |
| `engine/core.py:3323` | **WRITE** | `inst._loaded_skill_names = [name for name,_body in loaded_skills] if loaded_skills else None` — inside `_create_and_run_agent`, **else branch only** (new instance / external load) |
| `engine/core.py:986` | READ | `loaded_skill_names=getattr(instance,'_loaded_skill_names',None)` passed to `_try_auto_skill_extension` |

Critical gaps:
- **Recall path does NOT write it.** In `_create_and_run_agent`, the recall branch
  (`_is_recall`, `core.py:3186-3199`) keeps conversation[0] verbatim and never sets
  `_loaded_skill_names` — it retains whatever value existed from creation (see
  `.agent_lessons/recall-fix-a-drops-loaded-skills.md`; the lossy "Fix A" refresh was reverted,
  so the system-prompt block is preserved on recall, but `_loaded_skill_names` is simply not
  re-derived).
- **The runtime `load_skill` tool does NOT write it.** `tools/custom/load_skill.py:156` enqueues
  the skill body as a USER message (`enqueue_message`) and line 167 records telemetry
  (`record_skills_loaded(..., 'runtime')`), but there is **no assignment to
  `inst._loaded_skill_names`**. → Runtime-loaded skills are invisible to the reflection list.

### 3. What are "extended turns" / auto-skill extra turns?

- Trigger site: `ExecutionEngine.run()`, Phase 5 post-turn checks — `engine/core.py:978-1007`.
  On **genuine natural completion**, it calls `_try_auto_skill_extension(instance, messages,
  llm_messages, loaded_skill_names=getattr(instance,'_loaded_skill_names',None))` (line 986).
- Trigger method: `_try_auto_skill_extension` — `engine/core.py:232-338`.
  Gates (all must pass): `skill_manager` present; `auto_skill_enabled`; global skills mode !=
  NONE; **`instance._current_turn > AUTO_SKILL_MIN_TURNS`** (line 269); one-shot
  `_auto_skill_proposed` flag. On success it snapshots the pre-reflection output, builds the
  prompt via `skill_manager.auto_skill_qualifies(...)` (line 312), injects it as a USER message,
  sets the flag, and returns True.
- Budget reset: when triggered, `run()` grants `AUTO_SKILL_EXTRA_TURNS` fresh turns
  (`core.py:989-1004`) and continues the loop for the reflection.
- Constants (`settings.py`): `AUTO_SKILL_ENABLED=False` (526), `AUTO_SKILL_EXTRA_TURNS=25`
  (527-529), `AUTO_SKILL_MIN_TURNS=40` (531-532).

**Is there a separate prompt-building/snapshot path that drops skills?** No. There is no second
prompt builder or snapshot that strips skills. The reflection prompt reads the *same*
`_loaded_skill_names` attribute; the problem is that **the attribute itself never contains
runtime-loaded skills (and is None for the root agent)** — not that a downstream path loses them.

### 4. Why does the ROOT/main agent specifically show no skills?

Not a `name == 'Maine'` special case — it is a **different code path**:

- Root agent creation: `create_main_agent_instance(pool, instance_name, system_message_content,
  ...)` — `api_integration_pkg/runner.py:20-125`. It builds the conversation with the system
  message, then (lines 67-76) calls **only** `_inject_self_augmentation_skill(pool, instance)`
  (`engine/helpers.py:517-573`), which injects **self-augmentation** into the `## Active Skills`
  block. It does **not** resolve AUTO/explicit skills and does **not** set
  `_loaded_skill_names`.
- Root agent execution: `run_agent_in_pool(pool, instance_name)` — `runner.py:128-170` →
  `engine.run(instance)` on the pre-existing instance. This path **never enters
  `_create_and_run_agent`**, so line 3323 (the only writer of `_loaded_skill_names`) is never
  reached for the root agent.
- Net effect: for the root agent, `_loaded_skill_names` stays **None** forever → the reflection
  list renders `(none)` every time extended turns fire. Meanwhile its system prompt *does* show
  self-augmentation in `## Active Skills` — so there is a visible inconsistency between the two.

Sub-agents, by contrast, are created through `call_agent` → `run_child_core`
(`child_runner.py:102`) → `engine._create_and_run_agent`, which *does* set `_loaded_skill_names`
at creation time (line 3323). So sub-agents show their **creation-time** skills in the list, but
still miss any skills they load later via the runtime `load_skill` tool.

### 5. Tests covering skill-list exposure

- `tests/test_skill_generation.py:1684` `test_prompt_contains_loaded_skills_list` — calls
  `auto_skill_qualifies(..., loaded_skill_names=['docker-best-practices','code-review'])` and
  asserts the names appear; `:1697` asserts `(none)` when None. These are **unit tests of the
  renderer** that pass the list in directly. They do **not** exercise the engine path that
  populates `_loaded_skill_names`, so they cannot catch either root cause.
- No test covers (a) runtime `load_skill` updating `_loaded_skill_names`, or (b) root-agent
  skill-list exposure. Both are untested — consistent with the bug being live.

---

## Supporting Evidence (file:line citations)

- `_loaded_skill_names` field: `agent_cascade/agent_instance.py:263`.
- Only writer: `agent_cascade/engine/core.py:3323` (else branch of `_create_and_run_agent`).
- Only reader: `agent_cascade/engine/core.py:986` → `_try_auto_skill_extension` (core.py:232).
- Recall branch (no write): `engine/core.py:3186-3199`.
- Reflection prompt template + list line: `prompts/dna.py:185-204` (line 189).
- Renderer `(none)` fallback: `skills/manager.py:142-157` (line 152).
- Qualifier: `skills/manager.py:1339-1381`.
- Prompt injection as USER msg: `engine/core.py:324-328`.
- Extended-turn budget reset: `engine/core.py:987-1006`.
- Runtime tool (no `_loaded_skill_names` write): `tools/custom/load_skill.py:156,167`.
- Root creation (self-aug only): `api_integration_pkg/runner.py:67-76`; run path `runner.py:128-170`.
- Self-aug injector (no `_loaded_skill_names` write): `engine/helpers.py:517-573`.
- Sub-agent path goes through `_create_and_run_agent`: `child_runner.py:102`.
- No per-agent UI skills panel: web_ui grep shows only telemetry "Skill Usage" table
  (`app.js:6033` `updateTelemetrySkillTable`, `index.html:954`) + settings toggles.

---

## Decision Support

**Most likely intended "list":** the `{loaded_skills}` list in the auto-skill reflection prompt
(High Confidence — it is the only skill list that appears specifically *during extended turns*,
and there is no per-agent skills field in API/UI state).

**Recommendation (fix direction, NOT implemented — research only):**
1. Make `_loaded_skill_names` a running set for the whole run: initialize it from creation-time
   resolution (already done at core.py:3323) **and** append names when the runtime `load_skill`
   tool successfully loads a skill (`tools/custom/load_skill.py`). This fixes sub-agents.
2. Populate `_loaded_skill_names` for the root agent in `create_main_agent_instance`
   (`runner.py`) — at minimum set it to `['self-augmentation']` when self-aug is injected, and
   ideally run the same AUTO/explicit resolution as sub-agents so task-relevant skills load for
   the orchestrator too. This fixes "any skills for that matter for the root agent."
3. Add regression tests: (a) runtime `load_skill` → name appears in `_loaded_skill_names`;
   (b) root agent reflection prompt lists self-augmentation (not `(none)`).

**Alternatives considered:**
- Refresh/re-derive `_loaded_skill_names` on recall — not needed for the list itself (recall
  keeps the system-prompt block verbatim), and would not fix runtime/root gaps.
- Treat it as a UI bug / add a dedicated API field — rejected: no such field exists; the
  reflection prompt is where the user actually sees the list.

**Risks:**
- Appending to `_loaded_skill_names` from the tool must be thread-safe (tool may run on a worker
  thread); use the instance's compression lock or an atomic set update.
- Setting root-agent skills to include AUTO resolution changes orchestrator token usage/behavior
  (more skills in its system prompt) — confirm that is desired vs. self-aug-only.

**Remaining unknowns:**
- Whether the user actually observed `(none)` specifically (vs. a partial list). The code path
  strongly implies `(none)` for root and "creation-time only" for sub-agents, but a live log /
  API dump of an extended-turn run would confirm empirically.
- Exact UI surface the user was looking at (reflection USER message vs. system-prompt inspector);
  both are consistent with the findings, but the reflection prompt is the best fit for "during
  extended turns."

---

## Open Questions / Suggested Next Actions

1. Capture one live extended-turn run (root agent + a sub-agent that used `load_skill`) and dump
   the reflection USER message to confirm `(none)` / missing runtime skills empirically.
2. Decide product intent for the root agent: should the orchestrator get AUTO-matched task
   skills, or is self-augmentation-only intentional (in which case just track it so the list is
   honest)?
3. Related open todos in the same area (likely shared context): todo.md:138 (recall post
   extended-turns → duplicated messages / reprocessing) and todo.md:139 (final message not
   transmitted back if a recalled agent had an extended-turn round). See also
   `.agent_lessons/recall-fix-a-drops-loaded-skills.md`.
