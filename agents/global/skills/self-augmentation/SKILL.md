---
name: self-augmentation
description: Protocol for discovering, loading, and creating specialized skills at runtime — scan for and load relevant skills before tasks needing domain expertise, pass skills to sub-agents, and capture recurring patterns as new skills.
source: auto-generated
version: 1.1.0
triggers:
  - "skill discovery"
  - "loading skills"
  - "self-augmentation"
  - "specialized expertise"
  - "how to use skills"
---

# Self-Augmentation Protocol

Discover and load specialized skills when a task needs domain expertise.

## When to act

- **Task mentions any technology/framework/library/tool** → `scan_skills` with query='that technology' immediately.
- **Delegating to sub-agents** → include `load_skill=[...]` in `call_agent`.
- **Recurring pattern worth capturing** (multi-step procedure, ≥5 tool calls with coherent workflow) → load `skill-creator`, then `propose_skill`.
- **Unrelated bug/issue in the harness** (not part of your task) → don't fix inline; log via `bug-tracker-entry-format`.

## MEMORY HINT response protocol

The hint system matches your **assistant text and reasoning only** — tool calls, file contents, and code are invisible to it. This means:

1. **Verbalize what you're doing.** Before a multi-step operation or when you encounter an issue, state in one sentence what the problem is or what you're about to do and why. This gives the matcher far better signal than silence followed by 8 tool calls.

2. **Treat name-matched hints as mandatory.** If a hinted skill's name directly describes your current action (e.g., `reroute-module-logger-audit-downstream-sinks` while you're changing which logger a module uses), load it before proceeding. "You may want to" is the system's politeness, not an invitation to skip.

3. **Ignore tangential hints.** A skill hinted at 0.3 relevance while you're mid-refactor of an unrelated subsystem is noise. Load only when the name/description maps to what you're doing *right now*.

4. **Repeated hints escalate.** If the same skill appears in 2+ separate hints, the system considers it highly relevant. Ignoring a third appearance is a process failure, not a judgment call.

## Tool reference

- **`load_skill`**: loads skills into YOUR context. Takes a list of names (e.g. `["docker-best-practices"]`).
- **`call_agent` with `load_skill` param**: loads skills into SUB-AGENT context, same format.
  ```
  call_agent(agent_class="coder", task="Set up Dockerfile", load_skill=["docker-best-practices"])
  ```

## Workflow

1. **Scan** — `scan_skills` with a query matching the requirements.
2. **Load** — `load_skill` to inject expertise.
3. **Execute** — follow loaded guidelines.
4. **Delegate** — pass relevant skills to sub-agents when needed.
5. **Improve** — after using a skill, if you spot issues or gaps, submit a new version via `propose_skill`.

## Project memories

Skills are cross-project procedures ("how to do X"). Memories are project-specific facts ("what happened with Y") in `.agent_lessons/`.

- **Debugging X** → grep `.agent_lessons/` for X before starting fresh.
- **Known component** → check past investigations before reinvestigating.
- **`[MEMORY HINT]` memory hits** → read the referenced lesson files before proceeding; they usually contain the exact gotcha.
- **Before saving a new memory** → load `project-memory-writing` for formatting guidance.
- Use `[[memory-name]]` backlinks to connect related memories.

Skills teach HOW to work; memories tell WHAT already happened here.
