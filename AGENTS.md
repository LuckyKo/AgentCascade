# AGENTS.md — AgentCascade (AC) codebase conventions

Conventions for AI agents (and humans) working in this repo. Keep this file short and
actionable — it is read by agents with limited context.

## Official documentation
- `docs/SYSTEM_DOCS.md` is the authoritative system reference (architecture, components,
  data flow, key mechanisms). **When you add a subsystem or change a key mechanism, update
  the matching section (and the TOC) in the same commit.** A stale SYSTEM_DOCS misleads the
  next agent.

## Build / test
- Scoped tests: `python -m pytest tests/<file> -q`. Full suite only at milestones.
- Pre-commit hooks auto-fix line endings / whitespace / quotes. Expect **1 retry** on
  commit: re-stage the auto-fixed files, then commit again. (See skill
  `precommit-hook-commit-retry`.)
- Do NOT strip `pytest.ini` addopts (`-o addopts=""`) — it drops `--timeout=60`.

## Comment style
- A comment on a setting/constant documents **what it does**: purpose, units, default,
  and off-state (`0 = off`).
- **No changelog archaeology.** Do not reference `BUG_XXXX`, "Fix #N", version numbers,
  or "changed from X to Y" history — `git blame` is the history.
- A one-line rationale for a non-obvious *default value* is fine.

## Settings / knobs
- Env-backed: `os.getenv('AGENT_CASCADE_X', default)`. `0` / `None` disables a cap.
- Keep knob names stable; always document the off-state.

## Git / commit hygiene
- **Never stage the user's uncommitted WIP.** If a file mixes your change with the
  user's, use partial-hunk staging (blob surgery: `git show HEAD:<file>` + your hunk +
  `git hash-object -w` + `git update-index --cacheinfo`).
- Commit: `type: summary`.
- Resolve short hashes to full via `git rev-parse` before use.

## Versioning
- The version is `MAJOR.MINOR.PATCH` in `agent_cascade/__init__.py` (`__version__`).
- **Bug fix:** bump the **patch** number (last, e.g. `0.3.278 -> 0.3.279`).
- **Feature:** bump the **minor** number (middle, e.g. `0.3.278 -> 0.4.0`) and reset patch to `0`.
- **Major** number (first) is **user-reserved** — agents must never change it.
- Bump `__version__` in the same commit as the change; there is no separate auto
  `chore(version)` commit (the post-commit auto-bump hook has been removed).

## Bug tracking & lessons
- **Bug tracker** (`.bug_tracker/`, gitignored): one file per bug — `BUG_NNNN_<slug>.md`
  (zero-padded, next free number, never reused). Open bugs live at the root; when fixed,
  **move** the file into `.bug_tracker/fixed/` (don't delete). Full template + severity +
  lifecycle: see skill `bug-tracker-entry-format`. Check `.bug_tracker/` before filing —
  the bug may already be tracked.
- **Lessons** (`.agent_lessons/`, gitignored): project memories ("what happened with X"),
  snake_case `.md` files. **Before debugging a known area, grep `.agent_lessons/`** for the
  topic — you'll often find the exact gotcha. After a non-trivial fix, save a lesson
  (format: skill `project-memory-writing`).

## Cost model (important)
- **Cache-aware cost ≠ raw token count.** The prompt cache bills only the uncached
  delta, so shrinking the prefix (e.g. compressing earlier) *breaks* the cache and is
  net-negative. Do not reduce per-call input tokens as an objective.
- Compress only at meaningful thresholds; tiny compressions have no value.

## Memory loss / recoverability (important)
- **Minimal memory loss.** Never trade recoverable information for a minor
  performance/cost gain. A cap that can silently drop content the agent may
  later need is a regression, not an optimization.
- **Everything must be recoverable.** Any truncation / cap / lossy op must leave
  the omitted content reachable — point at the source (file path + offset, the
  full log) so the agent can re-read the missing part on demand. A blunt
  head+tail drop with no pointer is not allowed. (AC already truncates this
  way: the agent re-reads the source for the elided middle.)

## Test discipline
- Coder: run scoped tests for the files touched.
- Orchestrator: run the full suite at milestones (not a re-run of the coder's scoped
  files).
- Isolate flaky failures with a targeted single-file run before blaming a regression.
- When writing tests, never hardcode magic numbers/strings to match expected values;
  import and reference the constants from the code under test (single source of truth,
  keeps tests in sync when the value changes).

## Operations
- **Restarting the server:** committed changes and module-level constants are inert
  until a restart. **If the full regression suite passes, you may restart freely
  (no user gate).** Run the full suite first; restart only on green.

## Do / don't
- Don't add a cap/limit that double-covers an existing bound (redundant logic).
- Don't clobber vetted content with a blunt cap.
- Don't shrink a prompt/input to save tokens/latency if it can drop information
  the agent may need — make it recoverable (point at the source) instead.
- Don't leave trash in project root, place your temp files in /plans, /reports, /temp accordingly.
- Prefer minimal, safe changes; DRY; single source of truth.
