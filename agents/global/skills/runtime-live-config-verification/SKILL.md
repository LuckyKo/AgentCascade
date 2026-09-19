---
name: runtime-live-config-verification
description: Verify a config/setting value actually takes effect at runtime — catch import-time-bound module constants that make UI/persisted changes a no-op until process restart.
source: auto-generated
version: "1.0.0"
triggers:
  - "setting not taking effect"
  - "config change no-op"
  - "import-time constant"
  - "live update vs restart"
  - "UI value not applied"
  - "runtime setting verification"
  - "wired to UI but ignored"
generated_by: researcher
generated_from_task: "Wire AUTO_SKILL_MIN_TURNS from settings.py to the AgentCascade UI; discovered the runtime consumer reads an import-time module constant, so a UI change would be a no-op until restart."
---

## Goal
Confirm that a setting which has been wired through config → persistence → UI actually changes behavior at runtime, and specifically catch the case where the consumer reads an **import-time module-level constant** instead of live state (making every UI/persisted change silently ineffective until the process restarts).

This is the verification step that complements the *wiring* work in `fullstack-data-feature-plumbing`: you can wire all the seams correctly and still ship a no-op.

## Procedure

### Step 1 — Find every runtime CONSUMER of the value, not just its definition
Grep the setting's name (and its env-var form) across `.py`. The definition in `settings.py` is usually trivial; the **consumers** are where behavior actually depends on it. There may be several (gates, budget math, prompt builders). List each `file:line`.

### Step 2 — For each consumer, classify HOW it reads the value
- **Live read** (takes effect immediately): reads from a mutable runtime object — e.g. `getattr(pool.settings, 'key', DEFAULT)`, `instance.<attr>`, a dict lookup on shared state, or a getter that re-reads config.
- **Import-time bind** (no-op until restart): the module does `from settings import KEY` at load and then references the bare name `KEY`. The value is frozen at import; mutating the source object later has no effect.

The tell: if the consumer line references a bare uppercase constant that was imported from `settings.py`, it is import-time-bound. If it reads through an object attribute, it is live.

### Step 3 — Compare siblings to spot the asymmetry
A strong signal: an adjacent gate in the same function often mixes both styles. E.g. one line reads `getattr(settings, 'auto_skill_enabled', AUTO_SKILL_ENABLED)` (live) while the next reads `instance._current_turn <= AUTO_SKILL_MIN_TURNS` (import-time). The inconsistency is usually the bug/gap.

### Step 4 — If import-time-bound and live-update is required, change the read site
Switch the consumer to read from the live object with a safe fallback to the constant:
```python
settings = getattr(self.pool, 'settings', None)   # may be None
... <= getattr(settings, 'auto_skill_min_turns', AUTO_SKILL_MIN_TURNS)
```
The `getattr(obj, key, CONST)` form keeps behavior correct when the live object is absent/None. Do this at EVERY import-time consumer of that value (there are often 2–3, e.g. a gate plus budget-reset arithmetic).

### Step 5 — Decide restart-vs-live explicitly and state it
If changing all consumers to live reads is infeasible or undesirable, document that the setting is **restart-required** and surface that in the UI/plan (don't silently promise live behavior). State the decision in the report so the planner/orchestrator owns it.

## Tips
- **Persistence being correct does NOT imply runtime effect.** A value can be fully persisted to disk and broadcast to the UI yet never influence behavior if the consumer is import-time-bound. Always verify the consumer, not just the write path.
- **Generic persistence ≠ no work, but often less work.** If settings are a dataclass with `to_dict()=asdict(self)` / `from_dict()` via `hasattr` filtering, a new standard field auto-persists and auto-restores — you may need to touch NO persistence file. The explicit pop/save-list blocks are usually only for non-dataclass "extra" keys.
- **Budget/reset math is a hidden second consumer.** A turn/size limit often appears both in a gate AND in arithmetic that resets a counter (e.g. `turns_available = EXTRA_TURNS`). Grep for the constant name, not just the setting's snake_case key — the runtime may use the module constant under a different name than the UI key.
- **This is the same root cause as "feature wired but never fires in production."** Before assuming a feature is broken, check whether its gate reads an import-time constant that was changed via env/UI but not re-imported. (See project memory `feature-not-firing-in-production`.)
- **After fixing, run the feature's regression suite serially** and confirm with a live end-to-end change (set the value in UI → observe behavior change without restart) rather than only unit tests, since the bug is specifically about runtime binding.
