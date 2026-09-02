---
name: self-augmentation
description: Mandatory protocol for discovering, loading, and creating specialized skills at runtime. Agents MUST scan for and load relevant skills before executing tasks requiring domain expertise.
triggers:
  - skill discovery
  - loading skills
  - self-augmentation
  - specialized expertise
  - how to use skills
---

# Self-Augmentation Protocol

Agents must discover and load specialized skills when working on tasks that require domain expertise.

## WHEN TO ACT (Concrete Triggers)

- **When task mentions any technology, framework, library, or tool** (Docker, React, TensorFlow, etc.) → invoke the `scan_skills` tool with query='that technology' immediately
- **When delegating to sub-agents** → include `load_skill=[...]` parameter in call_agent with appropriate skills or set it on `AUTO` (set on `NONE` only for simple tasks)
- **When you notice a recurring pattern worth capturing as a skill** (multi-step procedure, domain-specific knowledge not covered by existing skills, at least 5 tool calls with coherent workflow) → invoke `load_skill` with `skill_names=['skill-creator']` to get instructions, then create the new skill using `propose_skill`.

## TOOL REFERENCE

- **`load_skill` tool**: Loads skills into YOUR context. Accepts either a list of skill names (e.g., `["docker-best-practices"]`).
- **`call_agent` with `load_skill` parameter**: Loads skills into SUB-AGENT context. Use same format as above.

Example call_agent usage:
```
call_agent(agent_class="coder", task="Set up Dockerfile", load_skill=["docker-best-practices"])
```

## REQUIRED WORKFLOW

1. **Scan**: Invoke the `scan_skills` tool with a query matching the requirements
2. **Load**: Invoke the `load_skill` tool to inject expertise
3. **Execute**: Follow loaded guidelines in your task
4. **Delegate**: Pass relevant skills to sub-agents when needed
5. **Improve**: After using a skill and you notice issues with it or areas where it could be improved/polished, submit a new version under the same name using `propose_skill`

## PROJECT MEMORIES

Skills are cross-project reusable procedures ("how to do X"). Project memories are facts specific to THIS project ("what happened with Y", "architecture decision Z") — they live in `.agent_lessons/` within the project workspace as markdown files.

- **When debugging X** → search for X and related terms in `.agent_lessons/` using `grep` before starting fresh
- **When working on a known component** → check if past investigations exist before reinvestigating (e.g., grep for "compression" when fixing compression bugs)
- **Before saving a new memory** → load the `project-memory-writing` skill for formatting guidance
- Use Obsidian-style backlinks `[[memory-name]]` to connect related memories, enabling discovery chains
- Memories are lightweight and discoverable via plain-text search — no custom tools or managers needed

Think of it this way: skills teach you HOW to work; project memories tell you WHAT already happened here.