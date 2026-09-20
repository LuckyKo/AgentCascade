---
name: frontend-early-exit-guard-stale-bookkeeping
description: Diagnose and fix "UI action does nothing / restore is skipped" bugs where a render function's performance early-exit guard short-circuits because the DOM was clobbered without invalidating its bookkeeping fields; includes the null-vs-undefined sentinel trap.
source: auto-generated
version: "1.0.0"
triggers:
  - "can't cancel edit"
  - "ui stuck"
  - "restore skipped"
  - "early-exit guard"
  - "prevContent"
  - "nothing changed return"
  - "re-render not happening"
  - "sentinel null undefined"
generated_by: orchestrator
generated_from_task: "Fix 'can't cancel message edits' in AgentCascade web_ui/app.js"
---

## Goal
Diagnose and fix the class of frontend bugs where a UI action (Cancel, restore, toggle-off) appears to do nothing because a render/update function has a **performance early-exit guard** that short-circuits — the DOM was mutated directly (e.g. replaced with an editor textarea) but the bookkeeping fields the guard reads were never invalidated, so the guard believes "nothing changed" and skips the re-render that would restore the UI.

## When this applies
- A vanilla-JS (or any incremental-DOM) frontend where a shared `updateXxx(el, data)` has an early return like `if (prev === cur && ...) return; // nothing changed`.
- Some code path mutates `el`'s DOM directly (`innerHTML = ...`, swaps in a control) **without** going through that update function.
- A later "restore/cancel" action calls the update function expecting it to rebuild, but the guard fires and the UI stays stuck (textarea remains, toggle doesn't flip back, etc.).

## Procedure

### Step 1 — Find the early-exit guard in the shared render/update function
Grep for the `// nothing changed` / `return;` short-circuit. Note exactly which fields it reads (e.g. `el._prevContent`, `el._wasGenerating`) and where those fields are WRITTEN (usually only inside the update function's full-re-render path, e.g. `el._prevContent = cur`).

### Step 2 — Find the DOM-clobbering path that bypasses the guard
Grep for direct DOM mutation of the same element (`innerHTML = ''`, `classList.add('editing')`, `appendChild` of a control). Confirm it does NOT update the bookkeeping fields. This is the desync: the guard's cached "previous" value still holds the ORIGINAL content, so on restore `prev === cur` and the guard returns early.

### Step 3 — Confirm the fingerprint (asymmetry between sibling actions)
A strong signal: the *Save/commit* sibling works but *Cancel/restore* doesn't. Why: Save mutates the data model first (`data.content = newContent`) so `cur !== prev` defeats the guard; Cancel leaves the model unchanged so the guard fires. This asymmetry is near-diagnostic.

### Step 4 — Fix by invalidating bookkeeping in the restore path
In the cancel/restore function, right before calling the update function, reset the fields so the guard cannot short-circuit:
```js
el._prevContent = undefined;   // forces prev !== cur
el._wasGenerating = undefined; // belt-and-suspenders on multi-field guards
```

### Step 5 — CRITICAL: pick the sentinel to match how downstream code reads it (null vs undefined)
Do NOT default to `null`. Trace EVERY read path of the field you're resetting:
- The guard often compares `prev === cur` → both `null` and `undefined` defeat a string compare.
- BUT fast/incremental paths frequently gate on `prev !== undefined` **and then** do `prev.length` / `prev.slice(...)`. With `null`, the gate passes (null !== undefined) and `.length` **throws TypeError**. With `undefined`, the gate fails and the dangerous path is skipped.
- Rule: prefer the sentinel the codebase already uses for "no value" — check how the field is initialized/read elsewhere (e.g. `el._prevContent !== undefined ? ... : fallback`). Match that idiom.

### Step 6 — Audit secondary clobber paths
Grep for every place that wipes/rebuilds the container (`scrollContainer.innerHTML = ''`, full re-render). Each one destroys any in-progress edit UI; if it also leaves global editing state (e.g. `state.editingIndex`) stale, later Save/Cancel mis-target a rebuilt element. Clear that state in each rebuild branch.

### Step 7 — Verify
No DOM/browser harness usually exists for vanilla-JS frontends (`test_*.js` are often bare-Node pure-expression scripts). Verify with: (a) `node --check file.js` for syntax, (b) an independent reviewer who specifically checks the sentinel against ALL read paths and every clobber branch. A reviewer catch on the null/undefined trap is common — budget for a fix cycle.

## Tips
- The guard's comment ("Performance: check if content actually changed") is the tell — any direct DOM mutation that bypasses it creates latent desync.
- Don't "fix" by removing the guard (perf regression); invalidate the cached fields instead.
- If multiple read paths exist, `undefined` is almost always the safer sentinel than `null` in JS because of the `!== undefined` + property-access pattern.
- Keep the fix minimal: reset fields in the restore path + clear global state in rebuild branches; don't refactor the render function.
