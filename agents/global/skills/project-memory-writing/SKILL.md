---
name: project-memory-writing
description: "Write and update atomic project memories in .agent_lessons/ — YAML frontmatter schema, authorship by log filename, backlinks, search-before-create, and pinning any cited line number to a commit because line numbers go stale."
source: auto-generated
version: 1.1.0
triggers:
  - "save a finding as a memory"
  - ".agent_lessons"
  - "project memory frontmatter"
  - "before delivering final result"
  - "memory line numbers stale"
generated_by: researcher
generated_from_task: "Multi-GPU session save/restore plan for Strata — prior memories carried line numbers that had drifted ~30 lines against the current commit"
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

Before writing a new memory, search `.agent_lessons/` to avoid duplicates: grep key terms (component name, error type). Search **every** `.agent_lessons/` directory that could hold one — a project repo and an agent workspace often each have their own, and the same fact may already exist in the other one. If you find related existing memories, update them instead of creating duplicates. If distinct but related, create the new one and add a `[[related-memory]]` link explaining the difference.

## Pin every cited line number to a commit

**Line numbers in a memory go stale silently.** A memory written against commit A is read months later against commit B, and every `file:line` in it is now wrong — usually by a large, uniform offset, which looks authoritative and is therefore trusted.

- State the pin **in the body**, not just implicitly: "At `<repo>` @ `<full SHA>`:" before the first citation. A memory without a pin is a claim about no tree in particular.
- Prefer **symbol-anchored** citations over pure line numbers where possible: `has_stage_parts` (`file.cpp:798-802`) — the name survives drift, the number does not. A reader can re-find a named function; a bare `:144` is unrecoverable.
- When **reading** a memory, treat its line numbers as stale until you re-verify them against the current tree. Re-derive from the symbol. A memory's line numbers are a pointer, not evidence.
- When **updating** a memory after the tree moves, add a dated addendum with the new SHA rather than silently rewriting the numbers — the old numbers record what was true at the old pin, and that history is useful.

## Updating existing memories

- **Adding new info:** edit the file and append your finding with context. Add your JSONL log filename to `contributors:` in frontmatter if you made a substantial update.
- **Contradictory info:** add a note explaining the conflict; update `confidence:` if needed.
- **Superseded by a newer tree:** add an addendum with the new SHA and what changed; set `confidence: deprecated` only if the fact itself no longer holds.
- Don't delete old memories — add clarifying context or set `confidence: deprecated` with an explanation.

## Content guidelines

- **One atomic fact per file**; name files descriptively in kebab-case (e.g., `compression-tail-invariant.md`).
- Start with a clear statement of the fact; include evidence (code refs, line numbers — pinned, see above).
- Use `[[backlinks]]` to connect related memories; keep it concise — quick reference, not narrative.
- Backlinks are bidirectional in practice: linking a new memory TO an existing one makes it discoverable from the existing one, so you do not always need to edit the older file.

## When to create a memory

Root-cause analysis completed, architecture decision made, or non-obvious behavior discovered — anything you'd want the next agent working on this project to know immediately.

## Tips

- A memory that says "X is blocked by guard Y at `:10444`" is dangerous without a pin: the guard may have moved or been removed. Say "guard Y (`generate.cpp:10444` at SHA …)" or state the guard's *name* and let the reader grep it.
- Negative findings are high-value memories and need their proof method recorded: "field X appears **zero** times in file.cpp (whole-file grep)" is re-checkable; "the format does not support X" is not.
- Link a memory to the report file it feeds, so the full analysis is reachable from the atomic fact and vice versa.
