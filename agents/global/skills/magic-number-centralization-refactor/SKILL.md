---
name: magic-number-centralization-refactor
description: Centralize scattered magic numbers into a settings module as named env-overridable constants while preserving existing user-facing env var names and live-config back-compat.
source: auto-generated
version: "1.0.0"
triggers:
  - "centralize magic numbers"
  - "settings.py constants"
  - "env override refactor"
  - "back-compat config default"
  - "tunable policy knobs"
generated_by: coder
generated_from_task: "Consolidate Telegram bridge scattered magic numbers into agent_cascade/settings.py as named, env-overridable constants; raise per-task timeout default to 28800s without breaking existing TG_* env overrides."
---

## Goal
Move hardcoded literals from feature modules into a central settings module as named constants with env overrides — behavior identical except for any explicitly intended default change — without breaking env vars that live deployments already set.

## Procedure

### Step 1 — Build the inventory before touching anything
Grep for every literal and its usages (including tests, docstrings, READMEs, CLI help). A verified inventory with file:line entries is the contract you refactor against; also note which literals are *policy knobs* (tunable) vs. protocol facts (e.g. a hard API limit — still centralize, but comment it as such).

### Step 2 — Decide env-var names BEFORE writing constants
The critical decision: for each knob, does an existing user-facing env var already exist?
- **Knob with an existing env var** → the new settings constant must read that SAME name (no prefix change), or live deployments silently lose their override. This intentionally deviates from the module's naming convention — add a comment block explaining why.
- **New knob, no prior env var** → follow the module's standard prefix convention (e.g. `AGENT_CASCADE_`).
Document in comments which knob reads which env var.

### Step 3 — Write constants matching the existing module convention exactly
Match style: `NAME: type = type(os.getenv('ENV', default))  # one-line comment`. Group under a clearly-commented section header. Keep defaults as literals, not chained imports.

### Step 4 — Wire modules, preserving the env-read layer
Where a loader does `os.environ.get('LEGACY_VAR', 'literal')`, keep reading the legacy var but swap the fallback for the constant: `os.environ.get('LEGACY_VAR', str(CONST)) or CONST`. The double-read (constant reads env too, loader re-reads) is redundant but safe and keeps precedence obvious. Function-signature defaults (`timeout=1800.0`) become `timeout=CONST` — verify with `inspect.signature` that the default tracks the constant.

### Step 5 — Update ALL docs in the same pass
Docstrings, CLI `--help` text, README tables, example blocks. Stale "Default: X" strings are the most common miss — grep for the old numeric literal across `.py` and `.md`.

### Step 6 — Verify back-compat at runtime, not just by inspection
Run a subprocess in the repo venv that (a) loads config with no env → asserts new defaults, (b) sets the legacy env vars → asserts they still win, (c) checks signature defaults. Then run the feature's full test files. Delegate an independent review checking: leftover literals, precedence bugs, circular imports (settings must not import from the feature), style consistency.

## Tips
- **Do NOT commit if the task says "I will review and commit"** — deliver edits + verification only.
- Local `DEFAULT_*` blocks inside a module that are internal policy (not user-facing env knobs) may stay put; moving them adds indirection without benefit. Use judgment per the task's allowance.
- Tests often pass values explicitly or as None — grep tests for the old literal before assuming the default change is safe; confirm by running, not by reading.
- Intentional behavior changes (e.g. 1800→28800) must be the ONLY delta; enumerate every other literal and confirm its value is unchanged.
