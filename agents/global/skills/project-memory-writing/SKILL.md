---
version: 1.0.1
name: project-memory-writing
description: Guidelines for writing well-formatted, discoverable project memory files in .agent_lessons/ using Obsidian-style markdown conventions.
triggers:
  - writing a new lesson or memory file
  - updating an existing .agent_lessons/ file
  - saving findings to project memory
---

# Project Memory Writing Guide

Project memories are atomic facts about the current project, stored as markdown files in `.agent_lessons/` (the standard per-project memory location). Write them so the next agent can find and understand them instantly.

## Frontmatter schema (YAML)

```yaml
---
tags: [compression, logging, tail-sync]              # REQUIRED: 3-5 tags for categorization
aliases: [log-tail-invariant]                        # OPTIONAL: discoverability via grep/search
related: [[compression-pool-desync-fix]]             # OPTIONAL: Obsidian-style links to connected memories
confidence: verified                                 # REQUIRED: one of the values below
author: orchestrator_Maine_20260917_222359.jsonl     # REQUIRED: JSONL log filename of the agent that created this file
contributors:                                        # OPTIONAL: agents who updated this file after creation
  - coder_worker1_20260918_103045.jsonl
---
```

**Confidence values:** `verified` (confirmed by testing/multiple sources/root-cause analysis); `likely` (single source but plausible and consistent); `unconfirmed` (speculative/anecdotal, needs verification); `deprecated` (outdated or superseded).

**Authorship convention:**
- `author` — the JSONL log filename of the agent instance that **created** this file. Use your own log path from session metadata (the basename, e.g. `orchestrator_Maine_20260917_222359.jsonl`). Do NOT use just the instance name — the full log filename uniquely identifies the exact session and is trivially retrievable from `logs/`.
- `contributors` — list of JSONL log filenames for agents that **substantially updated** this file (added new findings, corrected errors, changed confidence). Append your log filename when you make such an update. Minor typo fixes don't require a contributor entry.
- If you're unsure of your own log filename (e.g., in a test harness), use the instance name as a fallback: `author: <instance_name>`.

## Search before creating

Before writing a new memory, search `.agent_lessons/` to avoid duplicates: grep key terms (component name, error type). If you find related existing memories, update them instead of creating duplicates. If distinct but related, create the new one and add a `[[related-memory]]` link explaining the difference.

## Updating existing memories

- **Adding new info:** edit the file and append your finding with context. Add your JSONL log filename to `contributors:` in frontmatter if you made a substantial update.
- **Contradictory info:** add a note explaining the conflict; update `confidence:` if needed.
- Don't delete old memories — add clarifying context or set `confidence: deprecated` with an explanation.

## Content guidelines

- **One atomic fact per file**; name files descriptively in kebab-case (e.g., `compression-tail-invariant.md`).
- Start with a clear statement of the fact; include evidence (code refs, paths, line numbers).
- Use `[[backlinks]]` to connect related memories; keep it concise — quick reference, not narrative.

## When to create a memory

Root-cause analysis completed, architecture decision made, or non-obvious behavior discovered — anything you'd want the next agent working on this project to know immediately.

Apply the above guidelines to your current task.
