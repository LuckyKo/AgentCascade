---
name: self-augmentation
description: Guide for discovering and loading specialized skills at runtime using scan_skills and load_skill tools.
triggers:
  - skill discovery
  - loading skills
  - self-augmentation
  - specialized expertise
  - how to use skills
---

# Self-Augmentation via Runtime Skill Loading

When working on tasks that require specialized knowledge or unfamiliar workflows, you can dynamically load skills into your context at runtime.

## When to Load a Skill

Consider loading a skill when:

- The task involves a domain you're not deeply familiar with (e.g., Docker, Kubernetes, Terraform).
- The task requires following specific conventions or best practices.
- You notice repeated patterns suggesting a reusable expertise would help.
- A sub-agent task seems to need targeted guidance beyond your base instructions.

## How to Discover Skills

Use `scan_skills(query)` to find available skills:

```json
{"name": "scan_skills", "arguments": {"query": "docker containerization"}}
```

This returns a list of matching skills with relevance scores and descriptions. Use the score and description to decide which skill(s) are relevant.

To list all registered skills (no query):

```json
{"name": "scan_skills", "arguments": {}}
```

## How to Load Skills

Once you've identified a skill, load it into your context:

```json
{"name": "load_skill", "arguments": {"skill_names": ["docker-best-practices"]}}
```

You can load multiple skills at once:

```json
{"name": "load_skill", "arguments": {"skill_names": ["code-review", "security-checklist"]}}
```

The skill's full instructions are injected as a user message and you should apply them to your current task.

## Typical Workflow

1. **Scan**: `scan_skills(query="your task domain")` → see what's available.
2. **Evaluate**: Check scores and descriptions for relevance.
3. **Load**: `load_skill(skill_names=[...])` → inject into context.
4. **Apply**: Follow the loaded skill's guidelines while working on the task.

## For Sub-Agent Delegation

When delegating to sub-agents via `call_agent`, you can also load skills at init time using the `load_skill` parameter:

```json
{"name": "call_agent", "arguments": {"agent_class": "coder", "instance_name": "worker1", "task": "...", "load_skill": ["code-review"]}}
```

This injects the skill into the sub-agent's system prompt at initialization. Use runtime `load_skill` when you need to augment your own context mid-task.