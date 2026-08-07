# Investigation: "System prompt content CHANGED" log entries in AgentCascade

**Date:** 2026-08-06
**Investigator:** researcher (investigator_prompt_drift)
**Status:** Expected behavior — not prompt drift. Log-level recommendation provided.

---

## Executive Summary

The recurring `[CACHE_REBUILD] System prompt content CHANGED` entries are **not
mid-session prompt drift** and **not a functional bug**. They are the debugging
log of the system-prompt *injection/refresh* machinery firing during a cache
rebuild. The vast majority are **one-time injections at instance creation** that
never recur for that instance. There is **one legitimate churn pattern** (the root
`Maine` session ping-pong on `## Session Metadata`), but it is caused by a live
UI-driven workspace change, not a code defect.

**Recommendation:** demote line 1751 from `INFO` to `DEBUG` (it is routine,
expected bookkeeping). Optionally inject the AVAILABLE AGENTS block at creation
time so the first rebuild is byte-identical and the entry disappears entirely.

---

## 1. What code generates these entries?

**File:** `agent_cascade/execution_engine.py`, method `ExecutionEngine._setup_turn`
(lines 1567–1795).

- **Line 1640:** `logger.info("[CACHE_REBUILD] Rebuilding working set for ...")`
  — logged whenever the cache is invalidated (`_last_config_version` mismatch or no cache).
- **Line 1751:** `logger.info("[CACHE_REBUILD] System prompt content CHANGED for ...")`
  — logged only when the freshly-built system message differs from the stored one.
- **Line 1778:** `logger.debug("... textually identical — skipping pool update")`
  — the sibling branch when nothing changed (proves the guard is byte-accurate).

The system prompt is mutated in `_setup_turn` (lines 1675–1726) by three edits:
1. **Identity rename** (line 1682): `re.sub(r"(?i)You are\s+\w+\.", "You are {inst_name}.")`.
2. **Session Metadata refresh** (lines 1685–1697): `_build_session_metadata()` (line 649)
   reads live workspace paths from `operation_manager.extra_work_folders_ro/rw`
   (lines 695–696).
3. **AVAILABLE AGENTS block** (lines 1703–1725): `_build_resources_block()` (line 428)
   with `_replace_resources_block()` (line 767) → `_replace_section()` (line 729).

The comparison is used for KV-cache preservation: line 1729 `if m0_content !=
original_content:` only mutates and logs when different, deliberately keeping the
string byte-identical across retries (`_replace_section` even has an
"existing == new → return unchanged" guard, lines 756–759).

## 2. Real bug or expected behavior?

**Expected.** Three distinct benign origins:

### (a) One-time resources injection at first run — dominates sub-agent entries
The `## Active Skills` block is injected at *creation* (`execution_engine.py:4519`
→ `_inject_skills_to_system_message` line 517), but the `## AVAILABLE AGENTS` block
is only injected on the *first rebuild* in `_setup_turn` (lines 1703–1722). So the
first turn inserts AVAILABLE AGENTS before Active Skills and logs CHANGED. That is
exactly the shape in the report:
```
refinement_reviewer (len 6069→6602, first_diff@2820: orig='...f: PASS, NEEDS WORK, or FAIL\n\n## Active Skills\n\n### Skill 1\n' new='...f: PASS, NEEDS WORK, or FAIL\n\n\n\n## AVAILABLE AGENTS\nAvailab')
```
**Stability confirmed on reuse:** when `refinement_reviewer` was reused
console.log line 8269–8282, `_setup_turn` produced `[CACHE_HIT] Reusing cached
messages` — no rebuild, no CHANGED. The prompt is fixed after the first turn.

### (b) Live Session Metadata refresh — the root-session "ping-pong" (real churn, benign cause)
```
02:16:30  Maine  len 8453→8342  first_diff@163: 'Extra Paths (Read-Write): N:\w' → 'Log Path: n:\work\...'
02:19:46  Maine  len 8342→8453  first_diff@163: 'Log Path:...' → 'Extra Paths (Read-Write): N:\w'
```
`_build_session_metadata` recomputes `Extra Paths` from the live operation_manager
every rebuild (`execution_engine.py:695-696`). A UI change to workspace folders (or
transient read failure of the RW list) makes the block toggles between two states.
This *is* prompt churn, but the trigger is **a workspace config change**, not a code
fault. It does invalidate the LLM KV prefix on the root agent, which matters given
Maine's large context.

### (c) Identity rename — one-time at session load
`Maine (len 7580→8446, first_diff@8: 'You are Orchestrator. → You are Maine.')` is
line 1682 re-writing the identity to the instance name. Expected; only occurs once
per fresh session.

**Formatting cosmetic:** inserting `new_block` (which begins `"\n\n## AVAILABLE
AGENTS"`) via `new_block + '\n\n## Active Skills'` (lines 1715, 1722) yields an
extra blank-line gap (visual `\n\n\n\n`). Harmless, mentioned for completeness.

---

## 3. If expected → should the log level drop to DEBUG?

**Yes.** This event is routine, expected bookkeeping — exactly what DEBUG is for.
The "identical" sibling is already at DEBUG (line 1778), so CHANGED at INFO is
unbalanced: two outcomes of the same routine path at different levels.

**Recommended change:**
- `execution_engine.py:1751` → `logger.debug(...)`.
- The cache-miss marker (line 1640) could stay INFO or drop to DEBUG too — every
  fresh-turn with `conv_len>=2` on a new agent hits it, so it is also noise.

Optional refinement instead of a blunt downgrade:
- Log INFO **once per instance** (track a flag), then DEBUG after.
- Separate concern: raise a distinct INFO/WARNING **only when the change is
  config-level** (identity renames / tool-disable changes), where drift would be real.

---

## 4. If it were a bug — root cause & fix

It is not a bug, but the underlying *design gap* that makes these logs look scary
can be closed clean:

- **Root cause of the noise:** available-agents/skills injection is split across
  two stages — skills at creation (`_create_and_run_agent`, line 4519) and
  AVAILABLE AGENTS at first rebuild (`_setup_turn`, lines 1703-1722). The split
  guarantees the first rebuild differs.
- **Fix (optional, better KV prefix):** inject the `## AVAILABLE AGENTS` block at
  creation time alongside skills (in `_create_and_run_agent`/lifecycle build), so
  the first `_setup_turn` diff is empty and no CHANGED entry is ever logged. Commit
  `1f04e00` ("TODO #79: Rename resources header and fix system prompt injection
  order") already consolidated these; extending it to creation-time is natural.
- **For the Maine metadata ping-pong** `(b)`: make `_build_session_metadata`
  deterministic/byte-stable when `operation_manager` reports unchanged folders (it
  already sorts `extra_ro/rw` for determinism, `execution_engine.py:695-696`; verify
  order is also stable across rebuilds). Consider hashing the block and skipping the
  rewrite when only line ordering/churn, not content, changed.

---

## Supporting Evidence (file:line)

| Evidence | Location |
|---|---|
| CHANGED log site | `agent_cascade/execution_engine.py:1751` |
| trigger (rebuild) | `agent_cascade/execution_engine.py:1640` |
| identity rename `\w+\.` | `execution_engine.py:1682` |
| Session Metadata refresh | `execution_engine.py:1685-1697`; builder `:649`, paths `:695-696` |
| AVAILABLE block: build | `execution_engine.py:428` (`_build_resources_block`) |
| block replace/idempotency | `execution_engine.py:767` (`_replace_resources_block` → `_replace_section`:754-759) |
| skills injected at creation | `execution_engine.py:4519` → `_inject_skills_to_system_message:517` |
| tools NO SKILLS "identical" sibling | `execution_engine.py:1778` |

## Confidence

**Confirmed.** All three diff shapes traced to explicit injection code; reuse
stability verified directly in `logs/console.log` (lines 8269–8282 ref).

## Open Questions

- Whether the `Maine` metadata ping-pong is driven by deliberate UI workspace edits
  (then fine) or an `operation_manager` toggling a folder at startup (then worth
  stabilizing). Not determinable from this log; needs the UI action log.
- Whether forever caching-at first-injection is worth the effort vs simply dropping
  the log level.

## Suggested Next Actions

1. **Demote `execution_engine.py:1751` INFO → DEBUG** (and consider line 1640).
2. Optional: add **one-time-to-INFO** counter or per-context conditional.
3. Optional (better): merge AVAILABLE AGENTS injection into creation (`line 4519`
   path) so first rebuild is a true no-op and the log disappears.
4. Investigate the Maine metadata flip separately if it recurs with no UI action.
5. Save a project memory: `.agent_lessons/system_prompt_changed_log.md` (created).