---
name: read-only-threshold-preview-endpoint
description: Add a READ-ONLY "current threshold / what would happen" display (helper + GET endpoint + UI status line) that reuses the production rebalance math verbatim and clamps unsaved setting inputs identically to the config handlers, so the displayed number always matches a real save+pass.
source: auto-generated
version: "1.0.0"
triggers:
  - "current threshold display"
  - "read-only preview endpoint"
  - "skill invalidation threshold"
  - "rebalance preview"
  - "what would happen if I change this setting"
  - "clamp like config handler"
generated_by: coder
generated_from_task: "Add a read-only current-threshold display for AgentCascade skill-invalidation rebalance: manager.py compute_rebalance_preview helper + GET /api/skills/threshold endpoint + web_ui status line, no apply/rebalance behavior."
---

## Goal
Surface a live, READ-ONLY preview of what an existing computation WOULD produce when the user changes a setting (e.g. a "current threshold" for skill-invalidation rebalance) — without adding any apply/trigger behavior and without touching the real math or its settings wiring. The hard guarantee: **the displayed number always equals what a real save+pass computes.**

## When to use
A backend already has a stateful/mutating function (e.g. `rebalance_active_skills`) whose result is only logged, and you need to show that result in the UI as the user tweaks inputs. Do NOT use for: adding live-apply behavior, changing existing math, or wiring new settings end-to-end (that's `fullstack-data-feature-plumbing`).

## Procedure

### 1 — Write a pure preview helper next to the real function
Insert a sibling method right after the mutating one (see `python-sibling-method-insertion`). It must:
- Run the **same read-only snapshot + same formula** as the real pass. Copy the exact expressions (`raw = round(k*n)`, `evict_threshold = max(min_cap, min(max_cap, raw))`, …) verbatim — do NOT re-derive or "clean up".
- **SKIP one-time side-effecting steps.** A migration/porting step (e.g. `_migrate_metrics_to_v13()`) has write side effects and is wrong for a pure preview; the real pass may run it, the preview must not.
- Apply NOTHING: no state mutation, no flush-to-disk, no cache invalidation, no re-scan. Take a `deepcopy` snapshot under the existing lock.
- **Never raise**: wrap in try/except and return a dict with an `'error'` key + `ok=False`. A preview must never break the caller/UI.

### 2 — Add the GET endpoint mirroring BOTH the guard AND the clamps
Follow the neighboring read-only endpoint's style exactly (e.g. `getattr(pool,'manager',None)` guard + 503 fallback). Two things are easy to get wrong:
- **Read defaults from the same source** the real pass reads (e.g. `pool.llm_cfg` with the same keys/defaults as pool/core.py), not hardcoded elsewhere.
- **Accept optional unsaved overrides via query params** (`?k=&min_cap=&max_cap=`) so typing updates live — but clamp them **IDENTICALLY to the config handlers**. Grep the handler file and copy each `min(max(lo, v), hi)` expression verbatim, including any cross-field constraint (e.g. `max_cap` floored at the already-clamped `min_cap`). If you skip this, an out-of-range typed value yields a threshold that a real save+pass would never produce — breaking the core guarantee.
- Parse overrides defensively: only override when the param is present AND parseable; fall back to the cfg value on `ValueError`. Return the preview dict directly (200).

### 3 — Frontend: display-only, plain fetch, graceful failure
- One muted `<div id="...">` in the settings section using the panel's existing secondary-text styling. **No input, no button** — display only.
- `refresh()` does a plain `fetch('/api/...?'+qs)` (no auth headers) built from the current input-box values; render the human-readable string on success, muted "n/a" on any failure/503 — never throw.
- Call once on init + on `input` of each relevant field with a ~300ms debounce (reuse an existing debounce helper if present).

### 4 — Test: parity AND side-effect-freedom (both required)
One test must assert the preview returns the **same** key numbers as the real mutating function for an identical fixture (run both on fresh managers), AND a second dimension proving it mutates nothing: deepcopy-compare the state before/after, assert no disable-set change, and assert no disk flush. Add a never-raises test by forcing one I/O step to throw.

## Tips / pitfalls
- **Mirror, don't reimplement.** The moment you "simplify" a formula in the preview, it drifts from production. Copy expressions verbatim; if the real function changes later, the preview must change with it — keep them adjacent so the diff is visible.
- **The clamp is part of the contract**, not an afterthought. Out-of-range query params are the #1 way the "always matches" guarantee silently breaks. Verify with a smoke test that `k=99→5.0`, `min_cap=-3→1`, `max_cap<min_cap→min_cap`.
- **Verify the endpoint live, not just via unit tests.** The code_interpreter sandbox often lacks the project's venv (e.g. no `fastapi`) — write a throwaway host-side script using FastAPI `TestClient` + a stub pool/manager, run it via shell, then delete it. Stub unknown pool attrs with `MagicMock()` (a bare `None` breaks callables in `create_app` init), and make the "no manager" stub return `None` for the manager attr specifically to exercise the 503 path.
- Keep the change additive: stable return shape, optional params, no new POST/apply route, no settings-wiring edits.
