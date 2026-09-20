# Implementation Plan — Add Skills to Memory Hints

**Todo item:** `todo.md` line 151 — "add skills to memory hints"
**Status:** PLAN ONLY (no implementation)
**Prepared:** 2026-09-20 · **Baseline verified against:** `747a2ac` (master, v0.1.122)

> All file:line anchors below were re-read against the current working tree on the
> baseline above (per the `plan-anchor-verification` procedure). Line numbers are from
> that tree; if code in scope has moved since, re-trace before implementing.

---

## 1. Overview

The **memory-hint** feature (`agent_cascade/memory_hint/manager.py`) runs an async daemon
worker. On each agent turn the engine submits a job (`submit()`); the worker matches the
turn's text against a TF-IDF index of project-memory lessons (`.agent_lessons/*.md`), applies
a **self-calibrating specificity gate** (adaptive EWMA floor + top1−top2 gap + noise cap),
dedups/cooldowns against per-instance state, and — on a strong specific match — delivers a
hint via `_queue_tool_warning` telling the agent to re-read the relevant lesson file(s).

This feature extends that same delivery path so that **relevant skills** are also surfaced.
Skills live in `agent_cascade/skills/manager.py` (`SkillManager`) and are matched by
`SkillMatcher` (`agent_cascade/skills/matcher.py`) — a keyword inverted index over
name+description+triggers whose score is the **fraction of query tokens matched** (0..1).

The goal: when a turn's text matches relevant skills, the memory-hint delivery ALSO tells the
agent "you may want to load skill X" so it can pick up expertise it hasn't loaded yet.

**Key architectural constraint (drives most design decisions):** the skill matcher score is on
a **different scale** from the memory matcher's cosine similarity. They must NOT be mixed into
the same top1/top2/floor math. Skills get their own independent gate and are merged into the
hint at the *text* stage, not the *scoring* stage.

**Scope:** one new daemon-worker code path inside `MemoryHintManager`, two new per-instance
state fields, one new UI setting (a master sub-toggle), and tests. No changes to the engine
submit hook, the pool wiring, or the skill matcher itself.

---

## 2. Design Decisions

Each decision lists the chosen approach, rationale, and rejected alternatives.

### D1 — Separate skill gate; do NOT piggyback on the memory cosine floor

**Chosen:** In `_process_job`, run **two independent sub-pipelines**:
- **Memory sub-pipeline** — the existing gate (cosine EWMA floor + `GAP` + noise cap + dedup +
  cooldown), refactored verbatim into a helper that returns the hinted memory entries.
- **Skill sub-pipeline** — a NEW, simpler gate (min-score floor + max-count) over
  `SkillManager.match_skills`, returning the skills to suggest.

The two are merged only when building the hint text. The skill score is NEVER fed into
`_update_floor`, `_current_floor`, or the top1/top2/gap arithmetic.

**Rationale:**
- The memory gate constants (`GAP=0.03`, `FLOOR_SEED=0.20`, `FLOOR_MIN=0.12`, `EWMA_ALPHA=0.05`)
  were **empirically tuned against cosine similarity** of the TF-IDF matcher (see the module
  docstring, manager.py L55-73). Skill scores are keyword-fraction — a fundamentally different
  distribution. Mixing them would corrupt the EWMA floor and the gap gate.
- The memory gate is a *specificity* gate (single clear winner). Skills are **more actionable**
  and it's legitimate to suggest 2–3 relevant ones, so forcing the same "diffuse-tie → skip"
  behavior on skills would be wrong.

**Rejected alternatives:**
- *(a) Run `match_skills` inside `_process_job` and append skill lines while reusing the memory
  gate outcome.* Rejected: couples skill firing to the memory gate (a turn with no strong memory
  match but a strong skill match would get nothing — defeating the purpose), and risks mixing
  score scales.
- *(b) Normalize skill scores onto the cosine scale before merging into top1/top2.* Rejected:
  there is no principled mapping between keyword-fraction and TF-IDF cosine; inventing one is
  exactly the "trust the expected similarity scale" trap (see `retrieval-gate-calibration`).

### D2 — Skill min-score floor = `SKILL_MATCH_THRESHOLD` (0.15), data-derived

**Chosen:** A skill is suggested only if its matcher score ≥ `SKILL_HINT_MIN_SCORE`, where
`SKILL_HINT_MIN_SCORE` defaults to the existing `SKILL_MATCH_THRESHOLD` constant (0.15). This
reuses **one source of truth** for "what counts as a relevant skill match" — so the hint suggests
exactly the skills that AUTO mode (`resolve_load_skill_names`, manager.py L806-826) would load.

**Rationale (measured, not guessed):** I replayed realistic queries through the **real**
`SkillMatcher` against a realistic 8-skill index, generating **n=60** queries across the
memory-hint length range (150–1000 chars): 30 "specific" (target one skill's distinctive
keywords) and 30 "generic/diffuse" (topic-neutral filler). Top-1 score distribution:

| Class | n | min | p50 | p75 | p90 | max |
|---|---|---|---|---|---|---|
| **specific** | 30 | 0.065 | 0.134 | **0.196** | 0.240 | 0.481 |
| **generic/diffuse** | 30 | 0.071 | 0.077 | 0.094 | 0.107 | **0.139** |

Fire rate (top-1 ≥ threshold):

| threshold | specific fires | generic (false) fires |
|---|---|---|
| 0.20 | 23.3% | 0.0% |
| **0.15** | **43.3%** | **0.0%** |
| 0.12 | 66.7% | 6.7% |
| 0.10 | 80.0% | 20.0% |

**The robust finding:** generic/diffuse queries have a hard ceiling around **0.13–0.14 — every one
of the 30 generic samples landed below 0.15 (max 0.139)**, so at `0.15` the false-fire rate is
**0%**. Strong-specific queries fire ~43% at `0.15`. This is exactly the anti-spam property we
need: diffuse turns (the common case) get NO skill suggestion, while clearly-relevant turns do.
`0.15` sits in the valley between the two distributions and is **not structurally dead** on long
queries (denominator dilution lowers scores but strong matches still clear it).

> **Honesty / limits:** this is a *directional synthetic* study (hand-crafted topic seeds +
> neutral filler), **not** a production replay of real logged agent turns. The "specific" seeds are
> somewhat keyword-rich, so the 43% specific fire rate is likely optimistic vs real turns; the
> **generic ceiling (<0.15) is the load-bearing result** and held across all 30 samples. The gold
> standard — replaying real logged queries through the real index (as the memory gate was tuned on a
> ~47-sample replay, manager.py L55-73) — should be run as a **pre-implementation tuning step** to
> confirm `SKILL_HINT_MIN_SCORE` and re-tuned if `memory_hint_query_chars` or the skill corpus shift.

**Rejected alternatives:**
- *(a) Reuse the adaptive EWMA cosine floor for skills.* Rejected — different scale (D1).
- *(b) Feed a shorter "headline" query (first ~150 chars) to the skill matcher to lift scores.*
  Not needed: the measurement shows full-length queries already clear 0.15 for true matches.
  Keeping one shared query is simpler and avoids two divergent query strings. Revisit only if
  re-tuning shows long-query ceilings dipping below floor.
- *(c) A higher threshold (e.g. 0.20).* Rejected — at 0.20 only 23% of specific queries fire (vs
  43% at 0.15), so the feature would under-fire, especially on longer/diluted queries where strong
  matches sit near p50–p75 (0.13–0.20).

### D3 — Max skills per hint = `MAX_AUTO_SKILLS_PER_CALL` (3)

**Chosen:** Cap the suggested skill list to `SKILL_HINT_MAX_ENTRIES`, defaulting to the existing
`MAX_AUTO_SKILLS_PER_CALL` constant (3). Applied **after** dedup/cooldown, in score-descending
order (inherited from `match_skills`).

**Rationale:** Consistency with AUTO mode (same cap) and a hard bound against spam. 3 is enough
to cover "a task touching Docker + React + pytest" without becoming a wall of suggestions.

### D4 — Dedup against already-loaded skills + per-instance cooldown

**Chosen:**
- **Already-loaded exclusion:** skip any skill whose name (case-normalized) is in
  `inst._loaded_skill_names` (the per-run resolved skill list, agent_instance.py L263; set at
  engine/core.py L3368 and seeded with `'self-augmentation'` at helpers.py L575-576). No point
  suggesting a skill the agent already has active.
- **Cooldown:** a NEW per-instance map `inst._recently_skill_hinted` (skill name → last-hint
  monotonic timestamp), mirroring `_recently_hinted`. Re-suggesting the same ignored skill every
  turn would be spam; the cooldown throttles it. Reuses the SAME `memory_hint_cooldown_seconds`
  value (default 600 s) — one knob, no new setting.

**Rationale:** Mirrors the existing memory dedup/cooldown mechanics exactly (symmetric code, easy
to review), and `_loaded_skill_names` is already maintained by the load path so it's free to read.

> **v1 assumption (documented per review):** reusing `memory_hint_cooldown_seconds` for skills
> assumes "re-read a memory" and "load a skill" want the same throttle. This is a simplification,
> not a verified preference — if users find skill hints too frequent/sparse, add a separate
> `memory_hint_skill_cooldown_seconds` (see Open Question #2). No internal multiplier constant is
> added in v1 (that would be over-engineering before we have feedback on whether the shared value
> is even wrong).

**Rejected alternatives:**
- *(a) No cooldown for skills.* Rejected — an ignored-but-still-matching skill would be re-suggested
  on every subsequent turn until the query drifts; that's exactly the spam we want to avoid.
- *(b) A separate `memory_hint_skill_cooldown_seconds` setting.* Deferred (see Open Questions) —
  adds config surface for marginal benefit; reuse the existing cooldown by default.

### D5 — Single combined hint message via the existing `_deliver` / `_queue_tool_warning`

**Chosen:** Build ONE combined hint string (memory section + skill section) and deliver it through
the **existing** `_deliver → _queue_tool_warning` path (manager.py L517-525). No second message.

**Rationale:**
- One message = one tool-warning drain, less queue churn, simpler for the agent to read.
- `_queue_tool_warning` has a dedup guard that skips an *identical* warning already queued in the
  current drain cycle (path_security.py L54-57). A single combined string is naturally idempotent;
  two separate messages would both queue and double the noise.
- KV-cache friendly: one appended tool-result line rather than two.

**Rejected alternatives:**
- *(a) Two messages (one memory, one skill).* Rejected — doubles queue churn and drain cost for no
  readability gain; also makes the "both fire" case harder to reason about in tests.

### D6 — Failure isolation: each sub-pipeline independently guarded

**Chosen:** Each sub-pipeline is wrapped so a failure in one never breaks the other:
- `_match_skills_for_hint` wraps `sm.match_skills(query)` in `try/except → return []`, and also
  validates `isinstance(matches, list)` before use (deterministic no-op for `MagicMock` pools in
  tests — see §7 edge cases).
- The memory sub-pipeline keeps its existing behavior; a skill failure cannot prevent memory
  delivery, and vice versa.
- No lock is held across the two sub-pipelines: the memory `_index_lock` is acquired only around
  `self._matcher.match(query)` (inside `_match_memories`), and the skill manager's own `_write_lock`
  is acquired internally by `match_skills`. The two locks are never nested, so there is no
  lock-ordering hazard.

**Rationale:** The feature contract is "best-effort, never break the main loop" (module docstring).
Skills must inherit that guarantee in both directions.

### D7 — One new UI setting (master sub-toggle); the rest are internal constants

**Chosen:** Add exactly **one** new user-facing setting: `memory_hint_skill_suggestions` (bool,
default `True`). It is a **sub-toggle** under `memory_hint_enabled`: when memory hints are off,
skill suggestions are off too; when memory hints are on, the user can independently disable just
the skill part. The skill gate parameters (min score, max count) stay **internal constants** that
reuse existing settings values (`SKILL_MATCH_THRESHOLD`, `MAX_AUTO_SKILLS_PER_CALL`), matching how
the memory feature keeps `GAP`/`FLOOR_*`/`EWMA_ALPHA`/`MAX_HINTS_PER_TURN` internal while exposing
only the high-level knobs.

**Rationale:** Minimal config surface. The existing design philosophy already separates "user knobs"
(from `pool.llm_cfg`, live) from "tuned gate internals" (module constants). Skills follow the same
split — one on/off knob, no per-parameter UI.

**Rejected alternatives:**
- *(a) Expose `memory_hint_skill_min_score` and `memory_hint_skill_max_entries` as UI settings.*
  Rejected — config sprawl; these are tuned internals, not user decisions (mirrors the memory gate).
- *(b) No toggle at all (skills always on when memory hints are on).* Rejected — if skill spam is a
  problem for some users there's no way to silence it without disabling memory hints entirely.

### D8 — Reuse the same query string for both matchers

**Chosen:** Both sub-pipelines use the SAME `job['query']` (the turn text, already capped at
`memory_hint_query_chars` upstream by `_extract_memory_hint_query`, engine/core.py L416-429).

**Rationale:** Simpler; one query to reason about. D2's measurement confirms full-length queries
work for the skill matcher. No per-matcher query transformation.

---

## 3. Exact Changes per File

> Pseudocode only — no full dumps. **Line refs are to the pre-change baseline `747a2ac`; they will
> shift once `_process_job` is refactored (§3.1c).** Re-trace each anchor against the current
> working tree before coding (the refactor moves ~130 lines of `_process_job` into two helpers).

### 3.1 `agent_cascade/memory_hint/manager.py` (primary change)

**(a) New import + module-level constants.** Add the import to the file's top import block (next to
`from agent_cascade.log import logger`), and the two constants near the existing gate constants (~L55-73):
```python
# ── top of file, with the other imports ──
from agent_cascade.settings import SKILL_MATCH_THRESHOLD, MAX_AUTO_SKILLS_PER_CALL

# ── module-level constants (near GAP / FLOOR_* / EWMA_ALPHA) ──
# Skill-hint gate — INDEPENDENT of the memory cosine floor (plan §D1). Skill scores are
# keyword-fraction (0..1), a different scale than TF-IDF cosine; never mix them.
SKILL_HINT_MIN_SCORE = SKILL_MATCH_THRESHOLD      # 0.15 — data-derived floor (plan §D2)
SKILL_HINT_MAX_ENTRIES = MAX_AUTO_SKILLS_PER_CALL # 3    — max skills listed (plan §D3)
```
> Note: importing from `agent_cascade.settings` is safe (settings is a leaf module; manager.py
> already imports `agent_cascade.log`). These are read at import time, consistent with how other
> settings constants behave.

**(b) `_settings()` (L231-263)** — add the master sub-toggle to the returned dict:
```python
# NEW (plan §D7): master sub-toggle for skill suggestions within memory hints.
skill_suggestions = bool(cfg.get('memory_hint_skill_suggestions', True))
...
return {
    'enabled': ..., 'threshold': ..., 'max_entries': ...,
    'cooldown_seconds': ..., 'query_chars': ...,
    'skill_suggestions': skill_suggestions,   # NEW
}
```
> The skill sub-pipeline reuses `settings['cooldown_seconds']` for its cooldown (plan §D4) — no new
> cooldown key needed.

**(c) Refactor `_process_job` (L324-455)** into a thin orchestrator + two helpers. The memory gate
logic moves **verbatim** into `_match_memories`; the skill gate is new in `_match_skills_for_hint`.

```python
def _process_job(self, job):
    name = job['instance_name']; query = job['query']
    # (unchanged) TTL drop:
    if time.monotonic() - job.get('submitted_at', 0.0) > JOB_TTL_SECONDS: return
    settings = self._settings()
    if not settings['enabled']: return
    inst = self._pool.get_instance(name)
    if inst is None: return
    # (unchanged) SLEEPING guard (L345-351):
    ...

    # ── Memory sub-pipeline (existing gate, refactored; returns bare + display paths) ──
    memory_bare, memory_display = self._match_memories(job, settings, inst)   # ([], []) if no fire

    # ── Skill sub-pipeline (NEW independent gate; returns skill names) ──
    skill_names = self._match_skills_for_hint(job, settings, inst)           # [] if none/skipped

    if not memory_bare and not skill_names:
        return

    hint_text = self._build_combined(memory_display, skill_names)            # '' only if both empty
    if not hint_text:
        return

    # Record cooldowns BEFORE delivery (only for what we actually deliver).
    now = time.monotonic()
    with inst._compression_lock:
        for p in memory_bare:
            inst._recently_hinted[p] = now
        if memory_bare:
            inst._last_memory_hint_turn = job.get('turn', -1)
        for n in skill_names:
            inst._recently_skill_hinted[n] = now
        if skill_names:
            inst._last_skill_hint_turn = job.get('turn', -1)

    self._deliver(inst, name, hint_text)   # unchanged single-message delivery
```

> **Critical:** the current early `return` at L361 (`if not matches: return`) and L424
> (`if not to_hint: return`) move INSIDE `_match_memories` (as `return [], []`). This is what lets
> a strong skill match fire on a turn that has NO memory match.

**(d) New helper `_match_memories(self, job, settings, inst) -> (List[str], List[str])`** — the
existing L358-443 logic, unchanged except: every `return` becomes `return [], []`; the final
`display = [self._display_path(p, roots=roots_snapshot) for p in to_hint]` and the return become
`return to_hint, display`. Keep ALL existing debug logging (skip(floor)/skip(gap)/skip(noise),
dedup counts). Return `([], [])` when there is no memory match or the gate/dedup filters everything.

**(e) New helper `_match_skills_for_hint(self, job, settings, inst) -> List[str]`:**
```python
def _match_skills_for_hint(self, job, settings, inst):
    name = job['instance_name']
    if not settings['skill_suggestions']:          # master sub-toggle (plan §D7)
        return []
    sm = getattr(self._pool, 'skill_manager', None)
    if sm is None or not hasattr(sm, 'match_skills'):   # missing manager (defensive / tests)
        return []
    try:
        matches = sm.match_skills(job['query'])     # [(name, score)] desc; keyword-fraction 0..1
    except Exception as e:                          # plan §D6 — never break the memory path
        logger.debug('[MEMORY_HINT] %s: skill match failed: %s', name, e)
        return []
    # Deterministic no-op guard for `MagicMock` pools (existing tests): a bare MagicMock's
    # match_skills returns a MagicMock, NOT a list → skip. If a test stubs match_skills to
    # return a REAL list, that is intentional and SHOULD be processed (the guard correctly passes).
    if not isinstance(matches, list):
        return []
    if not matches:
        return []

    strong = [(n, s) for n, s in matches if s >= SKILL_HINT_MIN_SCORE]   # plan §D2 floor
    if not strong:                                   # generic/noise query → no skill spam
        logger.debug('[MEMORY_HINT] %s: skills all below min_score=%.2f → skip', name, SKILL_HINT_MIN_SCORE)
        return []

    now = time.monotonic(); cooldown = settings['cooldown_seconds']      # reuse memory cooldown (§D4)
    to_hint, skipped_loaded, skipped_cd = [], [], []
    with inst._compression_lock:
        # Read _loaded_skill_names UNDER the lock: the engine main loop writes it cross-thread
        # (engine/core.py L3368), so an unlocked read from this worker thread is a data race.
        # Matches how _memories_read / _recently_hinted are always accessed here (review fix).
        loaded = {str(x).lower() for x in (getattr(inst, '_loaded_skill_names', None) or [])}
        expired = [n for n, ts in inst._recently_skill_hinted.items() if now - ts >= cooldown]
        for n in expired:
            del inst._recently_skill_hinted[n]
        for n, _s in strong:
            if n.lower() in loaded:                 # already active this run (§D4)
                skipped_loaded.append(n); continue
            last = inst._recently_skill_hinted.get(n)
            if last is not None and (now - last) < cooldown:
                skipped_cd.append(n); continue
            to_hint.append(n)
    if not to_hint:
        logger.debug('[MEMORY_HINT] %s: all %d skill(s) filtered (loaded=%s, cooldown=%s)',
                     name, len(strong), skipped_loaded or '-', skipped_cd or '-')
        return []

    max_entries = SKILL_HINT_MAX_ENTRIES            # plan §D3 cap (after dedup/cooldown)
    if max_entries > 0:
        to_hint = to_hint[:max_entries]
    logger.debug('[MEMORY_HINT] %s: skill hint fire → %s', name, to_hint)
    return to_hint
```

**(f) New helper `_skill_block(self, names) -> str`:**
```python
def _skill_block(self, names):
    if not names:
        return ''
    lines = [f"  - {n}" for n in names]   # skill names are short snake_case; no clip needed
    return f"Skills you may want to load ({len(names)}):\n" + '\n'.join(lines)
```

**(g) New helper `_build_combined(self, memory_display, skill_names) -> str`:**
```python
def _build_combined(self, memory_display, skill_names):
    mem_block = self._build_hint(memory_display)   # UNCHANGED method; '' if empty
    skill_block = self._skill_block(skill_names)   # '' if empty
    if mem_block and skill_block:
        return mem_block + '\n' + skill_block      # both sections, memory first (stable order)
    if mem_block:
        return mem_block                           # memory-only → BYTE-IDENTICAL to today's output
    if skill_block:
        return '[MEMORY HINT] ' + skill_block      # skill-only → umbrella tag + skill block
    return ''
```

> `_build_hint(paths)` (L495-515) is **left unchanged** — this keeps the existing
> `test_build_hint_*` tests green and guarantees memory-only hints are byte-identical to today
> (KV-cache + log-comparison friendly).

### 3.2 `agent_cascade/agent_instance.py` (new per-instance state)

**(a) New dataclass fields** next to the existing memory-hint fields (L379-383):
```python
# Skill-hint cooldown map: skill name -> last-hint monotonic timestamp
# (feature: skills-in-memory-hints; mirrors _recently_hinted).
_recently_skill_hinted: dict = field(default_factory=dict)
# Last turn a skill hint was issued (diagnostics; mirrors _last_memory_hint_turn).
_last_skill_hint_turn: int = field(default=-1)
```

**(b) Session-reset block** (L625-627, inside the method that resets conversation + memory-hint state):
```python
self._recently_skill_hinted = {}
self._last_skill_hint_turn = -1
```

**(c) `_reset_memory_hint_state()`** (L638-640):
```python
self._recently_skill_hinted = {}
self._last_skill_hint_turn = -1
```
Also update this method's docstring to note it now clears the skill-hint cooldown state too (the
current docstring only mentions "read-set + cooldown map").

> `_loaded_skill_names` is NOT reset here — it's a per-run field refreshed each run by the engine;
> we only read it. No change needed to it.

### 3.3 Settings wiring for `memory_hint_skill_suggestions` (one new bool)

Follow the standard settings pattern (see lesson `auto_skill_settings_ui_wiring_pattern`). Touch:

| File | Location | Change |
|---|---|---|
| `agent_cascade/constants.py` | L164-170 (memory-hint name tuple) | add `'memory_hint_skill_suggestions',` |
| `agent_cascade/config_handlers.py` | L80-84 (registry list) + new handler near L637 | add to the list; add `@register_config_handler('memory_hint_skill_suggestions')` writing `agent_pool.llm_cfg['memory_hint_skill_suggestions'] = bool(ui_cfg.get('memory_hint_skill_suggestions', True))` (mirror `_handle_memory_hint_enabled`, L637-641) |
| `agent_cascade/api_server.py` | L1463-1471 (defaults dict) | add `'memory_hint_skill_suggestions': True,` |
| `agent_cascade/pool/config_persist.py` | L75-76 (persisted-key tuple) | add `'memory_hint_skill_suggestions',` |

> The manager already reads it live via `_settings()` (§3.1b), so no other code change is required
> for the toggle to take effect. A UI checkbox mirroring `memory_hint_enabled` is a nice-to-have;
> the setting works via config even without a dedicated checkbox (see Open Questions).

### 3.4 No changes needed

- **Engine submit hook** (`engine/core.py` `_maybe_submit_memory_hint`, L379-414): unchanged — it
  already submits on every eligible turn; skills ride the same job.
- **Pool wiring** (`pool/core.py` L181 `skill_manager`, L240 `memory_hint_manager`): unchanged — both
  are already attributes on the same pool, so the worker reaches `pool.skill_manager` directly.
- **Skill matcher / manager** (`skills/matcher.py`, `skills/manager.py`): unchanged — we only call
  the public `match_skills(query)`.

---

## 4. New Constants / Settings Table

| Name | Kind | Default | Source | Purpose |
|---|---|---|---|---|
| `SKILL_HINT_MIN_SCORE` | internal const (manager.py) | `0.15` | = `settings.SKILL_MATCH_THRESHOLD` | Min keyword-fraction score for a skill to be suggested (data-derived, §D2). |
| `SKILL_HINT_MAX_ENTRIES` | internal const (manager.py) | `3` | = `settings.MAX_AUTO_SKILLS_PER_CALL` | Max skills listed per hint (§D3). |
| `memory_hint_skill_suggestions` | **UI setting** (bool) | `True` | `pool.llm_cfg` | Master sub-toggle: enable skill suggestions within memory hints (§D7). Off ⇒ skills never suggested even when memory hints are on. |
| `inst._recently_skill_hinted` | instance state (dict) | `{}` | — | Skill-name → last-hint monotonic ts; cooldown guard (§D4). Reuses `memory_hint_cooldown_seconds`. |
| `inst._last_skill_hint_turn` | instance state (int) | `-1` | — | Diagnostics; mirrors `_last_memory_hint_turn`. |

> No new cooldown, threshold-override, or query-length setting is introduced. Cooldown reuses
> `memory_hint_cooldown_seconds`; the min score reuses `SKILL_MATCH_THRESHOLD`.

---

## 5. Hint Text Format Examples

Deterministic ordering: memory entries (score-desc) first, then skills (score-desc). Memory-only
output is **byte-identical** to today's `_build_hint` output.

**(a) Memories only** (unchanged from current behavior):
```
[MEMORY HINT] Relevant memories you may want to re-read (2):
  - N:\work\WD\AgentWorkspace\.agent_lessons\compression-debug.md
  - N:\work\WD\AgentWorkspace\.agent_lessons\lock-ordering.md
```

**(b) Skills only** (no memory match this turn — the key new capability):
```
[MEMORY HINT] Skills you may want to load (1):
  - docker-best-practices
```

**(c) Both memories and skills:**
```
[MEMORY HINT] Relevant memories you may want to re-read (2):
  - N:\work\WD\AgentWorkspace\.agent_lessons\compression-debug.md
  - N:\work\WD\AgentWorkspace\.agent_lessons\lock-ordering.md
Skills you may want to load (2):
  - docker-best-practices
  - pytest-docker-subprocess-hang
```

Notes:
- Memory paths are clipped to `HINT_ENTRY_MAX_CHARS` (256) as today; skill names are short and not
  clipped.
- The `[MEMORY HINT]` umbrella tag always leads, so any log/UI filtering keyed on that tag still
  catches skill-only hints.
- Entry count is shown in each section header; the whole message is a single tool-warning string.
- **Intentional header difference (per review):** the skill-only header (`[MEMORY HINT] Skills you
  may want to load (N):`) reads slightly differently from the memory-only header (`[MEMORY HINT]
  Relevant memories you may want to re-read (N):`). This is deliberate — both keep the single
  `[MEMORY HINT]` umbrella tag for uniform filtering; the sub-header just names the section. Not a
  bug; acceptable as-is.

---

## 6. Test Plan

**Primary file:** `tests/memory_hint/test_memory_hint.py` (unit — add a new class
`TestManagerSkillHints`). **Secondary:** `tests/memory_hint/test_memory_hint_e2e.py` (one e2e case).

Existing fixtures to reuse: `_FakeInst` (L264-275) and the `_manager_with_lesson(tmp_path, enabled)`
helper (L279-303). **Extend both:** add `self._recently_skill_hinted = {}`,
`self._last_skill_hint_turn = -1` to `_FakeInst`; and in `_manager_with_lesson`, set
`pool.skill_manager = None` by default (explicit memory-only intent) with an optional
`skill_manager=` parameter to inject a real/stub `SkillManager`.

> **Important for existing tests:** they use `MagicMock()` pools, so `pool.skill_manager` is a
> `MagicMock` (truthy). The `isinstance(matches, list)` guard in `_match_skills_for_hint` makes the
> skill sub-pipeline a deterministic no-op on such pools, so **existing memory-only tests stay green
> unchanged.** Still, set `pool.skill_manager = None` in the shared helper for clarity.

### New unit test cases (`TestManagerSkillHints`)

| Test name | What it asserts |
|---|---|
| `test_skill_hint_fires_on_strong_match` | A query strongly matching one skill (score ≥ 0.15) → delivered hint contains "Skills you may want to load" + the skill name; `_recently_skill_hinted` records it. |
| `test_skill_hint_below_min_score_suppressed` | A generic/diffuse query whose top skill score < 0.15 → NO skill section (no spam). |
| `test_skill_only_when_no_memory_match` | No memory match but a strong skill match → skill hint still delivered (verifies the removed early-return, §3.1c). |
| `test_skill_hint_excludes_already_loaded` | `inst._loaded_skill_names = [skill]` → that skill is NOT suggested. |
| `test_skill_hint_cooldown_blocks_repeat` | Process twice within cooldown → second process adds no new skill hint. |
| `test_skill_hint_prunes_expired_recently_skill_hinted` | After cooldown elapses, the entry is pruned and re-suggestion is allowed. |
| `test_skill_hint_max_entries_caps` | 4+ skills above floor → capped to `SKILL_HINT_MAX_ENTRIES` (3), top-scored kept. |
| `test_skill_and_memory_combined_format` | Both fire → ONE message with memory block (byte-format preserved) + skill section appended; assert exact combined string shape. |
| `test_memory_only_output_byte_identical` | Only memories fire → hint equals the legacy `_build_hint(display)` output exactly (regression guard). |
| `test_skill_manager_missing_no_crash` | `pool.skill_manager = None` → no exception; memory-only behavior preserved. |
| `test_skill_manager_magicmock_noop` | `MagicMock` pool (no explicit skill_manager) → skill sub-pipeline no-ops deterministically (`isinstance` guard); memory path unaffected. |
| `test_skill_suggestions_toggle_off` | `memory_hint_skill_suggestions=False` + strong match → NO skill section. |
| `test_skill_failure_isolated_from_memory` | Stub `skill_manager.match_skills` that raises → memory hint still delivered; no exception propagates. |
| `test_feature_disabled_skips_both` | `memory_hint_enabled=False` → neither memory nor skill hints (existing guard, §3.1c). |

### New e2e case (`test_memory_hint_e2e.py`)

- `test_skill_suggestion_rides_tool_warning_queue`: drive a real turn whose text matches a
  registered skill through the submit→worker path; assert the drained `_tool_warnings` contains the
  skill line. (Follows the existing e2e harness in that file.)

### Regression / verification

- Run the full `tests/memory_hint/` suite serially (per lesson `pytest-docker-subprocess-hang`,
  host `shell_cmd` is reliable; avoid code_interpreter subprocesses for pytest).
- Confirm **all pre-existing memory-hint tests pass unchanged** (the byte-identical + no-op guards).
- Spot-check: a strong-skill turn in a real session logs `[MEMORY_HINT] ... skill hint fire → [...]`.

---

## 7. Risks & Edge Cases

| # | Case | Handling / Risk |
|---|---|---|
| E1 | **No skills registered** | `match_skills` returns `[]` (empty index) → skill sub-pipeline returns `[]`. No-op. |
| E2 | **Skill manager missing on pool** | Production always sets it (`pool/core.py` L181). Guarded by `sm is None or not hasattr(...)` → `[]`. Defensive for tests/misconfig. |
| E3 | **`MagicMock` pool in existing tests** | `pool.skill_manager` is a truthy `MagicMock`; `match_skills` returns a non-list. The `isinstance(matches, list)` guard makes it a deterministic no-op → existing tests stay green. |
| E4 | **Empty / whitespace query** | Rejected upstream in `submit()` (L143); skill matcher also returns `[]` for an empty token set. |
| E5 | **SLEEPING instance** | Existing guard (L345-351) returns before BOTH sub-pipelines → neither memory nor skill hints. Consistent with today. |
| E6 | **Feature disabled** | `settings['enabled']` False → return before both (§3.1c). Sub-toggle off → skills skipped, memories unaffected. |
| E7 | **Thread-safety vs skill registry lock** | `match_skills` acquires the skill manager's `_write_lock` (RLock) internally; if a `discover()` rescan holds it, the worker briefly blocks (rescans are infrequent/small — acceptable, better than a torn read). The memory `_index_lock` and the skill `_write_lock` are **never nested** (D6), so no lock-ordering deadlock. |
| E8 | **Behavioral change: hints fire more often** | Today most turns produce NO hint (memory gate rarely fires). Adding an independent skill gate means some previously-silent turns will now emit a skill hint. Bounded by min-score floor + already-loaded dedup + cooldown, but this IS a real increase in hint frequency — the master toggle (D7) is the user's escape hatch. Flag to reviewer as intended behavior, not a bug. |
| E9 | **Worker-thread cost** | `match_skills` runs on the daemon worker (not the main loop); keyword-index match is O(index size), trivial for realistic skill counts. No main-loop latency impact. Lazy index rebuild (first call) also happens off the main loop. |
| E10 | **Long-query ceiling dipping below floor** | Measured 0.172 at ~1007 chars (above 0.15). If `memory_hint_query_chars` is raised far beyond ~1000, re-tune `SKILL_HINT_MIN_SCORE` (same caveat the memory gate carries for its constants). |
| E11 | **Skill name case-sensitivity in dedup** | `_loaded_skill_names` and cooldown keys compared case-normalized (`n.lower()`); skill names are snake_case so this is safe. Keep consistent with the load-path dedup (todo line 147). |

---

## 8. Open Questions

1. **UI checkbox vs config-only for `memory_hint_skill_suggestions`?** The setting works via config
   even without a dedicated checkbox. Should we add a visible checkbox next to `memory_hint_enabled`,
   or leave it config/env-driven for v1? (Recommendation: config-only for v1, checkbox as follow-up —
   keeps the change minimal.)
2. **Separate skill cooldown setting?** Currently reuses `memory_hint_cooldown_seconds`. If skill
   suggestions should throttle differently from memory re-reads (e.g., shorter), add
   `memory_hint_skill_cooldown_seconds`. Deferred unless feedback says the shared value is wrong.
3. **Should a diffuse-but-matching skill set be suggested at all?** The chosen gate has NO
   specificity-gap requirement for skills (unlike memories) — so 2–3 borderline-above-floor skills
   on a diffuse query WILL be listed (bounded to 3). If that's too noisy, add an optional
   `SKILL_HINT_GAP` (top1−top2 ≥ g) secondary gate. Measured gaps: specific ≈ 0.12–0.15, generic ≈
   0.066, so a gap of ~0.10 would separate them — but this is an enhancement, not in the base design.
4. **Rating-aware ordering?** Should higher-rated skills be preferred when scores tie (cf.
   `rating_sort_key`)? Currently ties break by name (deterministic). Optional refinement; out of scope.
5. **Should skill hints be logged at INFO (like memory delivery, L522) or DEBUG?** Memory delivery
   logs at INFO with the full text. Recommend the combined `_deliver` keeps its single INFO log
   (no change), which will naturally include skill lines. Confirm no PII concern in logging skill names
   (skill names are not sensitive — fine).

---

## 9. Verified Anchors (baseline `747a2ac`)

| Claim | Location | Verified |
|---|---|---|
| `submit()` non-blocking, most-recent-wins via generation counter | manager.py L134-164 | ✓ |
| `_process_job` core flow; early returns at "no matches" / "no to_hint" | manager.py L324-455 (returns at L361, L424) | ✓ |
| Memory gate: floor/gap/noise + EWMA update | manager.py L372-398 (`_update_floor` L379) | ✓ |
| Dedup/cooldown under `inst._compression_lock`; cap; display; record; deliver | manager.py L400-455 | ✓ |
| `_build_hint(paths)` format + `HINT_ENTRY_MAX_CHARS`=256 | manager.py L495-515 (const L53) | ✓ |
| `_deliver` → `_queue_tool_warning(pool, name, text)` | manager.py L517-525 | ✓ |
| Gate constants GAP/FLOOR_SEED/FLOOR_MIN/EWMA_ALPHA tuned for cosine | manager.py L55-73 | ✓ |
| `_settings()` reads 4 live knobs from `pool.llm_cfg` | manager.py L231-263 | ✓ |
| `SkillMatcher.match` = keyword-fraction score, sorted desc, name-tiebreak | matcher.py L121-159 (norm L151) | ✓ |
| `SkillManager.match_skills` public API, lazy rebuild under `_write_lock` | skills/manager.py L618-632 | ✓ |
| AUTO mode uses `SKILL_MATCH_THRESHOLD` + `MAX_AUTO_SKILLS_PER_CALL` | skills/manager.py L806-826; settings.py L516-518 | ✓ |
| Pool: `skill_manager` (L181) & `memory_hint_manager` (L240) on same object | pool/core.py L181, L237-246 | ✓ |
| Submit hook gated on `memory_hint_enabled`; query extraction cap | engine/core.py L379-429 | ✓ |
| `_loaded_skill_names` per-run field; set at recall; seeded 'self-augmentation' | agent_instance.py L263; engine/core.py L3368; helpers.py L575-576 | ✓ |
| Memory-hint instance state + reset helper | agent_instance.py L379-383, L625-640 | ✓ |
| `_queue_tool_warning` resolves instance, appends to `_tool_warnings`, dedup guard | path_security.py L34-62 (dedup L54-57) | ✓ |
| Settings wiring: constants tuple / handlers / api defaults / persist keys | constants.py L164-170; config_handlers.py L80-84, L637-691; api_server.py L1463-1471; pool/config_persist.py L75-76 | ✓ |
| Existing test fixtures `_FakeInst`, `_manager_with_lesson`; delivery captured via real `_queue_tool_warning` onto `inst._tool_warnings` | tests/memory_hint/test_memory_hint.py L264-303, L305-316 | ✓ |

### Empirical basis for D2 (measured, not assumed)
Directional study (n=60) through the **real** `SkillMatcher` against a realistic 8-skill index,
queries spanning 150–1000 chars: **specific** top-1 p50/p75/max = 0.134/0.196/0.481; **generic**
top-1 p50/p90/max = 0.077/0.107/**0.139**. At threshold 0.15 the generic false-fire rate is **0%**
(all 30 generic samples < 0.15) while specific fires ~43%. Therefore `SKILL_MATCH_THRESHOLD` (0.15)
is a valid, non-degenerate min-score floor: it suppresses diffuse turns (the spam case) while still
firing on clearly-relevant turns. Full tables + honest limits are in §D2.

---

## 10. Review Notes

An independent reviewer (`reviewer` agent, `code-review` skill) verified this plan against the code
at baseline `747a2ac`. Verdict: **NEEDS WORK → addressed**. Findings and resolutions:

| # | Finding (severity) | Resolution in this plan |
|---|---|---|
| 1 | Empirical basis too small (n=4) — Major | Expanded to n=60 with percentiles + fire rates (§D2, §9); added honest limits + production-replay recommendation. |
| 2 | Unlocked cross-thread read of `_loaded_skill_names` — Major | Moved the read inside `with inst._compression_lock:` in `_match_skills_for_hint` (§3.1e), matching how all other per-instance hint state is accessed. |
| 3 | Cooldown reuse is an assumption, not verified — Major | Documented explicitly as a v1 simplification with a follow-up path (Open Q#2); no over-engineered multiplier constant added. |
| 4 | Anchor line numbers will drift post-refactor — Minor | Added explicit re-trace disclaimer to §3 header. |
| 5 | `isinstance` guard could mask list-returning mocks — Minor | Clarified: a real-list return is intentional and SHOULD be processed; the guard only no-ops bare-MagicMock pools. |
| 6 | New fields not in reset docstring — Minor | Added note to update `_reset_memory_hint_state` docstring (§3.2c). |
| 7 | Skill-only header differs from memory-only — Nit | Documented as intentional (single `[MEMORY HINT]` umbrella tag) in §5. |
| 8 | Import placement — Nit | Import now shown at top of file, separate from the constants block (§3.1a). |

No blocker/correctness flaws found: the refactor preserves existing memory semantics, the
independent skill gate avoids mixing score scales, and the locks are never nested.

---

*End of plan.*
