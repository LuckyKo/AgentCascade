---
name: text-comparison-normalization-asymmetry
description: Diagnose and prevent a text-similarity dedup gate from silently under-firing when the two compared sides are extracted or normalized by different code paths (e.g. lightweight scalar parser vs robust YAML frontmatter parser).
source: auto-generated
version: "1.0.0"
triggers:
  - "similarity gate"
  - "dedup threshold"
  - "difflib ratio"
  - "SequenceMatcher"
  - "frontmatter parse"
  - "text comparison under-fires"
  - "duplicate detection"
generated_by: coder
generated_from_task: "Implement a hard-reject similarity gate in propose_skill using difflib SequenceMatcher over name description triggers frontmatter; the gate silently under-fired because the lightweight scalar frontmatter parser yielded an empty string for block-style YAML triggers while registered metadata stored the real list, so the two compared sides were normalized by different code paths."
---

## Goal
Catch and prevent a whole class of bug where a text-similarity / dedup / diff / near-duplicate gate **silently never fires** (or false-fires) because the two sides being compared were produced by *different* extraction or normalization code paths.

## Procedure

### Step 1 — When a similarity gate "doesn't trigger" on an obvious duplicate, suspect asymmetry first
Do NOT assume the threshold is wrong or the texts are genuinely different. The most common root cause: side A (e.g. the incoming proposal) and side B (e.g. stored/registered metadata) went through **different parsers or normalizers**, so they aren't comparable even when semantically identical.

### Step 2 — Print both sides' *actual* extracted values, not the raw input
Reproduce in isolation (code_interpreter / a scratch script) and log exactly what each side's text is right before the comparison:
```python
print("A:", repr(text_a))   # e.g. 'name desc '  <- triggers missing!
print("B:", repr(text_b))   # e.g. 'name desc docker compose'
print(ratio)                # ~0.88, not ~0.98
```
If one side is missing a field the other has (empty string where a list should be), that's your asymmetry.

### Step 3 — Find the two extraction paths and unify them
Trace how each side's fields are obtained. Typical trap: a **lightweight line-based parser** (fast, scalar-only) used for one path vs a **robust structured parser** (e.g. pyyaml `parse_frontmatter`) used to build the other. A block-style YAML list like:
```yaml
triggers:
- docker
- compose
```
is read as `''` by a scalar-line regex but as `['docker','compose']` by a real YAML parser. **Fix:** route BOTH sides through the same robust extraction/normalization before comparing. Never compare light-parsed output against fully-parsed stored data.

### Step 4 — Verify the fix with a symmetric near-identical pair
Confirm ratio ≈ 1.0 for an exact match and that the gate fires just above threshold. Use a **near-identical** test fixture: if identifiers are random (e.g. uuid suffixes), two independent random values differ in too many chars to exceed a high threshold like 0.95 — derive the "duplicate" by changing only 1 char of the incumbent's identifier so the ratio stays well above threshold.

## Tips
- **Symmetry is the invariant:** before trusting any A-vs-B text comparison, assert both sides are produced by the *same* function over equivalent inputs. This applies to dedup gates, fuzzy matchers, diff/patch heuristics, KV-cache key identity, and loop-detection.
- Lightweight parsers exist for speed but silently drop non-scalar structure (lists, nested maps, multi-line values). They are fine for "get a scalar field" but dangerous as the *source of truth* for a comparison against fully-parsed data.
- A gate that "passes everything" is often an under-firing bug, not a healthy pass — verify it actually rejects a known-positive duplicate (a negative test that asserts rejection).
- Keep the compared text cheap (frontmatter/fields only, not 15 KB bodies) so difflib stays fast, but never at the cost of dropping fields one side has and the other doesn't.
