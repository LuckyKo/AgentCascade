---
name: skill-creator
description: Meta-skill that guides agents to create reusable skills from completed tasks
source: manual
version: "1.0.0"
triggers:
  - "create skill"
  - "new skill"
  - "propose skill"
  - "skill generation"
  - "reusable skill"
---

## Goal

Create high-quality, reusable SKILL.md files that capture patterns, procedures, and best practices discovered during task execution.

## When to Create a Skill

Propose a new skill when you notice:
- **Repeated patterns**: You applied the same multi-step procedure that could help future similar tasks.
- **Domain-specific knowledge**: You discovered tools, conventions, or workflows specific to a domain.
- **Novel combinations**: You combined techniques in a way not covered by existing skills.
- **Tool-heavy tasks**: The task required ≥5 tool calls with a coherent workflow.

## How to Write Effective SKILL.md

### Frontmatter Fields (Required)

```yaml
---
name: snake-case-name
description: Clear, specific description of what this skill covers (≥20 chars)
source: auto-generated
version: "1.0.0"
triggers:
  - "keyword1"
  - "keyword2"
  - "keyword3"
generated_by: coder
generated_from_task: "Original task description"
---
```

**Field rules:**
- **`name`**: Snake case, starts with lowercase letter (`[a-z][a-z0-9_-]*`), unique across all skills.
- **`description`**: ≥20 characters, specific enough to distinguish from similar skills.
- **`triggers`**: At least 1 entry — keywords/phrases that should match this skill during auto-discovery.
- **`source`**: Always `"auto-generated"` for skills created via this meta-skill.

### Body Structure (≥100 characters)

```markdown
## Goal

One sentence: what this skill enables.

## Procedure

### Step 1 — First Action
Concrete, actionable instructions.

### Step 2 — Second Action
Include code examples, commands, or configuration snippets.

## Tips

- Domain-specific best practices
- Common pitfalls to avoid
```

## Quality Checklist

Before proposing a skill, verify:
- [ ] Name is unique (check existing skills via `scan_skills`)
- [ ] Description is specific (not generic like "coding" or "debugging")
- [ ] Triggers cover how the skill will be matched in practice
- [ ] Body has concrete, actionable steps (not vague advice)
- [ ] Total file size ≤ 15 KB
- [ ] The skill is reusable — it applies to a class of tasks, not just one

## How to Use the propose_skill Tool

```
propose_skill(params={
    "skill_content": "<full SKILL.md with frontmatter and body>",
    "test_task": "<the task text that triggered this skill creation>"
})
```

The tool will:
1. Write the skill to a pending location
2. Validate structure (frontmatter, required fields, uniqueness)
3. Run self-match validation against the test task
4. Auto-promote to `.qwen/skills/` if validated

## Agent-Specific Guidance

### Coder Agents
Focus on: coding patterns, testing strategies, debugging workflows, build tool usage, code review checklists, refactoring patterns.

### Researcher Agents
Focus on: search strategies, source evaluation, fact-checking methods, information synthesis, citation handling.

### Reviewer Agents
Focus on: code review checklists, consistency checks, edge case identification, security review patterns.

### Writer Agents
Focus on: content structures, tone adaptation, editing workflows, style guide enforcement.