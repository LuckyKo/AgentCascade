---
name: project-memory-writing
description: Guidelines for writing well-formatted, discoverable project memory files in .agent_lessons/ using Obsidian-style markdown conventions.
triggers:
  - memory
  - lesson
  - record
  - save fact
  - document discovery
  - project knowledge
---

# Project Memory Writing Guide

Project memories are atomic facts about THIS project stored as markdown files in `.agent_lessons/`. Write them so the next agent can find and understand them instantly.

## Frontmatter Schema (YAML)

```yaml
---
tags: [compression, logging, tail-sync]      # REQUIRED: 3-5 tags for categorization
aliases: [log-tail-invariant]                 # OPTIONAL: For discoverability via grep/search
related: [[compression-pool-desync-fix]]      # OPTIONAL: Obsidian-style links to connected memories
confidence: verified                          # REQUIRED: one of the values below
---
```


**Confidence values:**
- `verified`: confirmed by testing, multiple sources, or root cause analysis
- `likely`: single source but plausible and consistent with known behavior
- `unconfirmed`: speculative or anecdotal, needs verification
- `deprecated`: outdated or superseded by newer information

## Search Before Creating

Before writing a new memory, search `.agent_lessons/` to avoid duplicates:
- Use `grep` with key terms from your discovery (e.g., component name, error type)
- If you find related existing memories, update them instead of creating duplicates
- If distinct but related, create the new memory and add a `[[related-memory]]` link explaining the difference

## Updating Existing Memories

- **Adding new info**: Edit the file and append your finding with context
- **Contradictory info**: Add a note explaining the conflict; update the `confidence:` field in frontmatter if needed
- Don't delete old memories — add clarifying context or set `confidence: deprecated` with explanation

## Content Guidelines

- **One atomic fact per file**; name files descriptively in kebab-case (e.g., `compression-tail-invariant.md`)
- Start with a clear statement of the fact; include evidence (code refs, paths, line numbers)
- Use `[[backlinks]]` to connect related memories; keep it concise — quick reference, not narrative

## When to Create a Memory

- Root cause analysis completed, architecture decision made, or non-obvious behavior discovered
- Something you'd want the next agent working on this project to know immediately