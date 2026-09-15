---
name: code-refinement-audit
description: Whole-repo audit for over-engineering. Scans the entire codebase instead of a diff, a ranked list of what to delete, simplify, or replace with stdlib/native equivalents. One-shot report, does not apply fixes.
triggers:
  - audit this codebase
  - audit for over-engineering
  - what can I delete from this repo
  - find bloat
---

Repo-wide over-engineering audit (the whole-tree counterpart to a diff review). Scan the entire tree instead of a diff; rank findings biggest cut first. One-shot report — lists findings, applies nothing.

## Tags

- `delete:` dead code, unused flexibility, speculative feature. Replacement: nothing.
- `stdlib:` hand-rolled thing the standard library ships. Name the function.
- `native:` dependency or code doing what the platform already does. Name the feature.
- `yagni:` abstraction with one implementation, config nobody sets, layer with one caller.
- `shrink:` same logic, fewer lines. Show the shorter form.

## Hunt

Deps the stdlib or platform already ships, single-implementation interfaces,
factories with one product, wrappers that only delegate, files exporting one
thing, dead flags and config, hand-rolled stdlib.

## Output

One line per finding, ranked: `<tag> <what to cut>. <replacement>. [path]`.
End with `net: -<N> lines, -<M> deps possible.` Nothing to cut: `Lean already. Ship.`

## Boundaries

Scope: over-engineering and complexity only. Correctness bugs, security holes,
and performance are explicitly out of scope. Route them to a normal review
pass. Lists findings, applies nothing. One-shot report — the user ends it by
asking for a normal review or by not following up.
