---
name: memory-vault-consolidation
description: "Procedure for consolidating .agent_lessons/ vaults: smart-action triage, hub generation, de-indexing, and scope labeling. Run during AFK Admin tier 5 cleanup."
triggers:
  - "memory vault consolidation"
  - "agent lessons cleanup"
  - "hub generation"
  - "supersede lessons"
  - "de-index lessons"
  - "vault triage"
version: "1.0.0"
---

# Memory Vault Consolidation

Consolidate `.agent_lessons/` vaults: dedup, tier, hub, and scope. All actions are **de-index or additive** — nothing is deleted (house rule: recoverable-truncation-no-irreversible-memory-loss).

## Live vault set

`discover_vaults()` returns `.agent_lessons` children of `base_dir ∪ extra RO/RW`. Iterate ONLY these — never glob `N:\work\WD\*\` (impostor trees like `AgentCascade_unified` are excluded by construction).

## Feature A — Smart-action triage (supersede/merge/archive)

### Triage pipeline (scale: 2000+ files)

1. **Group by backlink graph + filename family.** Build an undirected graph: node = lesson; edge if (a) one file's `related:` names another's stem, or (b) they share the 2-segment filename prefix family. Take connected components.
2. **Multi-member clusters (≥2) → smart-action queue.** Solo files with no family siblings are NOT touched.
3. **Per cluster, the agent decides one of:**
   - `superseded` — older/less-complete member gets `status: superseded` + `superseded_by: [[survivor]]`; survivor gets `supersedes: [[…]]`
   - `merge` — fold unique content into survivor (use `lossless-merge-fold-then-gated-delete`), then mark folded files `status: superseded`
   - `archived` — historically interesting but no longer actionable → `status: archived`
   - `leave active` — distinct facets sharing a name family (default when unsure)

### Frontmatter schema

```yaml
status: active | superseded | archived   # strict enum, OPTIONAL, default=active
superseded_by: [[new-canonical]]          # present iff status: superseded
supersedes: [[old-lesson]]               # on the survivor
last_validated: 2026-11-11               # audit-only, ISO date
```

**Allowlist de-index rule:** a file is excluded from hinting ONLY if `status:` (lowercased, stripped) is exactly `superseded` or `archived`. Everything else (including 17 existing free-form values, no-frontmatter files) stays active. The code choke-point is `vault.py:rescan()` → `_is_deindexed(meta)`.

### Staleness de-index (Tier 2)

The ONLY automatic-ish prune reason. For a candidate: grep the cited symbol/path in the live project; if the path no longer exists, mark `status: archived` + write the grep command + date into the body. Expect ≈0 content-prunes on a healthy tree. Do NOT archive on filename age alone.

## Feature B — Hub generation (summary hierarchy)

### Clustering algorithm (no embeddings)

1. Undirected graph from `related:` backlinks + 2-segment filename families (same graph as Feature A).
2. **Connected components** = first-pass clusters.
3. **Orphans** (size 1): group by dominant `tags` (most-specific tag, not top-generic like `bugfix`/`testing`). Orphans with no distinctive tag stay solo.
4. **Only emit a hub for clusters ≥3.** Size 2 → Feature A merge. Size ≥8 → split by sub-family prefix.
5. Cap total hubs at ~10% of vault size.

### Hub file format

Lives in the **same vault** as its leaves, named `<cluster-slug>-hub.md`:

```yaml
---
tags: [<cluster-slug>, summary, hub]
hub_id: <cluster-slug>
status: active
last_validated: <date>
---
# <Cluster> — map of content
> Summary layer over K leaf lessons. Leaves are verbatim and never rewritten.
## One-line state of the cluster
<2-4 sentence synthesized state>
## Common gotchas (deduped across leaves)
- <recurring facts>
## Leaves
- [[leaf-1]] — one line
```

### Leaf side

Each leaf gets `hub: <cluster-slug>` in frontmatter (additive; body untouched). The hint render path appends ` (hub: #<slug>)` to the hinted leaf's line. **Leaves are never suppressed** — hubs are additive index files.

### AFK procedure (idempotent, incremental)

**Step 0 — Delta audit (do this FIRST, before any writes):**
1. Count existing state: `glob('*-hub.md')` per vault = hub count; grep `^hub:` per vault = tagged-leaf count; grep `^status:\s*(superseded|archived)` = de-indexed count.
2. Compare against the last `clusters.json` (in `logs/`). New files since last run = files on disk not in the previous `clusters.json`. If zero new files and zero new `related:` edges, **skip the run** — report "no delta, nothing to do" and stop.
3. If there IS a delta (new files, new backlinks, new clusters), proceed to Step 1.

**Step 1 — Execute (only the delta):**
1. **Snapshot** the vault dir to a dated backup before any write.
2. Run clustering → emit updated `clusters.json` (slug → leaf list). Deterministic.
3. For each **new** cluster (no existing `<slug>-hub.md`): spawn a sub-agent with the cluster's leaf bodies, ask for the hub template. Write with `write_file`.
4. Set `hub: <cluster-slug>` in each **new** leaf's frontmatter (only if absent — idempotent).
5. Update `last_validated:` on **all** hubs (cheap touch).
6. Run smart-action triage on **new** multi-member clusters only (supersede/merge/archive).

**Verification (always, even on skip):**
- Spot-check 3 random leaves: `hub:` field present and matches a real hub file.
- Spot-check 1 hub file: leaf list matches the actual files on disk.
- Confirm `status: superseded` files are excluded by `_is_deindexed()` (run 1 memory_hint test).

Re-run finds existing hubs, updates only `last_validated`, adds no duplicate links.

## Feature C — Scope labels + relocation

### Scope derivation (union rule)

`scope: [vault_default] ∪ [project tags from frontmatter]`
- workspace → `global`; project vaults → their project name
- A workspace file tagged `ninfer` → `scope: [global, ninfer]`
- A project-vault file with no tags → `scope: [<vault_default>]`
- Missing `scope:` → treated as `global` (always eligible)

### Matching rule (task side)

A job's active-scope = `{global} ∪ {project}`. A doc is eligible if its scope intersects the allowed set OR contains `global`. **Exact segment match** (not prefix) to avoid `llama-autoloader`↔`llama-cpp` false positives.

### Relocation (workspace → project vaults)

Files with a single unambiguous project tag move to that project's vault. **Framework/generic lessons must NOT move** (e.g. `framework-prefix-invalidation-after-compaction` is tagged `llama-cpp` but is cross-project). The single-project-tag heuristic over-flags this class — require manual review.

### Code (ships dark)

`matcher.match(query, allowed_scope=None)` — when `allowed_scope` is not None, skip docs whose scope doesn't intersect. Default `None` = no-op. The `project` field on `AgentInstance` is the v2 prerequisite; until then the filter is dark.

## Scripts

- `AgentWorkspace/scripts/relocate_lessons.py` — dry-run by default, `--execute` to move
- `AgentWorkspace/scripts/add_scope_labels.py` — dry-run by default, `--execute` to write
- `AgentWorkspace/scripts/memory_vault_common.py` — shared vault map, scope/alias maps, parser

## Invariants

- **Nothing is deleted.** Superseded/archived files stay on disk, greppable.
- **Hubs are additive.** Deleting a hub reverts to leaf-only hints.
- **Leaves are never suppressed** by a hub's presence.
- **Reversible:** remove `status:` line / delete hub file / restore from backup.
