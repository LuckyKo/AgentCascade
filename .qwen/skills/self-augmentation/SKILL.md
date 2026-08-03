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

- **When task mentions any technology, framework, library, or tool** (Docker, React, TensorFlow, etc.) → `scan_skills(query='that technology')` immediately
- **When delegating to sub-agents** → ALWAYS include `load_skill=[...]` parameter in call_agent
- **When recurring patterns emerge** → Create new skill via `skill-creator`

## TOOL REFERENCE

- **`load_skill` tool**: Loads skills into YOUR context. Accepts either a list of skill names (e.g., `["docker-best-practices"]`), the string `"AUTO"`, or the string `"NONE"`.
- **`call_agent` with `load_skill` parameter**: Loads skills into SUB-AGENT context. Use same format as above.

Example call_agent usage:
```
call_agent(agent_class="coder", task="Set up Dockerfile", load_skill=["docker-best-practices"])
```

## REQUIRED WORKFLOW

1. **Scan**: Invoke the `scan_skills` tool with a query matching the technology name
2. **Load**: Invoke the `load_skill` tool to inject expertise
3. **Execute**: Follow loaded guidelines in your task
4. **Delegate**: Pass `load_skill=[...]` to sub-agents when needed

## EXAMPLE PATTERNS

**INCORRECT**
Agent receives "Set up Kubernetes deployment" and proceeds without skill discovery. Result: Generic, potentially flawed guidance.

**CORRECT**
Invoke the `scan_skills` tool with query="kubernetes", then invoke the `load_skill` tool with skill_names=["kubernetes-best-practices"] before executing task.

## EDGE CASES

- **No relevant skills found**: Proceed with caution, explicitly note limitations in output
- **Multiple skills match**: Choose based on relevance score from scan_skills results

## OUTPUT QUALITY RISK

Failing to load appropriate skills results in:
- Generic or inaccurate guidance
- Potential best practice violations requiring rework
- Inefficient problem-solving approaches

Proactively load skills to guarantee expert-level results.