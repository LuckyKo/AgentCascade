# `propose_skill` Pre-Approval Health Check — Implementation Plan

> **Status:** PLAN ONLY — no code changes made. Ready for a coder to implement without re-researching.
> **Source of task:** `todo.md` line 153 — "`propose_skill` does the match check after it's been sent
> for approval, wasting security calls. New skill health checks need to be done BEFORE approval."
> **Verified against live tree:** 2026-09-21, by direct `read_file` of every cited location (no stale
> report baseline — all anchors confirmed on the current working tree).

---

## 0. Verified baseline & anchors

Working dir: `N:\work\WD\AgentCascade`. Every anchor below was read directly (not grepped) and the
surrounding semantics match the description. This table doubles as the implementer's spot-check list.

| Location | Claim | Verified |
|---|---|---|
| `tools/custom/propose_skill.py` L109 | `parse_tool_params(params)` entry point | ✓ |
| `tools/custom/propose_skill.py` L128–138 | Rating-only short-circuit returns **before** any validation/approval | ✓ |
| `tools/custom/propose_skill.py` L140–148 | No-content guard + justification guard (pre-approval) | ✓ |
| `tools/custom/propose_skill.py` L160–167 | Lightweight frontmatter line-parse (`fm`) | ✓ |
| `tools/custom/propose_skill.py` L171–173 | `agent_name`, `existing_meta`, `is_update` computed | ✓ |
| `tools/custom/propose_skill.py` L181–207 | Similarity gate (`find_similar_skills` at L194) runs **BEFORE** approval; REJECTED message style at L203–207 | ✓ |
| `tools/custom/propose_skill.py` L209–218 | Version compute (new vs update) | ✓ |
| `tools/custom/propose_skill.py` L220–233 | Frontmatter patch (`name` always, `version` on update); `skill_content` reassigned at L233 inside `if fm_match:` | ✓ |
| `tools/custom/propose_skill.py` L235–247 | Approval description build | ✓ |
| `tools/custom/propose_skill.py` L250–258 | `request_user_approval(...)` | ✓ |
| `tools/custom/propose_skill.py` L267–270 | `register_skill_from_content(skill_content=..., source='auto-generated')` — **no `task_text` arg** (defaults to `''`) | ✓ |
| `skills/manager.py` L1682 | `register_skill_from_content(...)` def | ✓ |
| `skills/manager.py` L1708–1712 | Pending file write (`uuid` dir) — runs **after** approval | ✓ |
| `skills/manager.py` L1716 | `parse_skill_file(pending_file)` | ✓ |
| `skills/manager.py` L1718–1720 | `name = frontmatter.get('name','')`, fallback to dir name if empty | ✓ |
| `skills/manager.py` L1723 | **`validation_task = task_text or frontmatter.get('generated_from_task', '')`** | ✓ (load-bearing) |
| `skills/manager.py` L1728–1733 | Under `_write_lock`: `existing = set(registry.keys())`; if upgrade, `existing.discard(name)` | ✓ |
| `skills/manager.py` L1734 | `validate_skill(skill_content, name, existing, validation_task, check_injection=True)` | ✓ |
| `skills/validator.py` L35–137 | `validate_skill(...)` — Tier 1 structural + injection; Tier 2 self-match only if `task_text` non-empty | ✓ |
| `skills/parser.py` L38 | `parse_frontmatter(content) -> (dict, body)` available for import | ✓ |
| `skills/manager.py` L216 | `_skills_registry: Dict[str, Dict]` (holds ALL tiers: production + candidate) | ✓ |
| `skills/manager.py` L218 | `_write_lock = threading.RLock()` | ✓ |
| `settings.py` L524 / L648 / L659 / L662 / L663 | `SKILL_DUP_SIM_THRESHOLD=0.95`, `AUTO_SKILL_PROMOTION_THRESHOLD=0.3`, `AUTO_SKILL_MAX_SIZE_KB=15`, `MIN_SKILL_BODY_LENGTH=100`, `MIN_DESCRIPTION_LENGTH=20` | ✓ |
| `tools/custom/shell_cmd.py` L398–442 | Pre-approval validation pattern: input checks → approval gate (the in-tool precedent) | ✓ |
| `tests/test_skill_generation.py` L2163–2258 | `TestProposeSkillSimilarityGate` — `_make_tool()` builds a `MagicMock` pool; rejection asserted via `request_user_approval.assert_not_called()` | ✓ |

**No factual deviations found.** The caller's (Maine's) claims were all accurate. One assumption in
Q2 needed correction — see §3, RQ2.

---

## 1. Summary

Move the deterministic skill "health" validation (`validate_skill`: size, frontmatter, name
snake_case, description length, triggers, body length, prompt-injection, uniqueness, and Tier-2
self-match) to run **before** `request_user_approval` in `propose_skill.call()`, so an invalid
proposal is rejected without ever consuming a user-approval / security call. The authoritative
re-validation inside `register_skill_from_content` (under lock) is **kept unchanged** and remains
the source of truth; the new pre-check is a best-effort screen that reuses the manager's exact
name-exclusion + task-derivation logic so there is no drift between the two.

Recommended design: **Option (b)** — add a thin `SkillManager.prevalidate_skill()` method and call
it from the tool. Rationale in §4.

---

## 2. Root cause

`ProposeSkill.call()` asks the user to approve (L250) and *then* calls
`skill_manager.register_skill_from_content(...)` (L267). Inside `register_skill_from_content`,
**all** of the following execute only after approval:

1. Pending-file write (`uuid` staging dir) — manager L1708–1712.
2. `parse_skill_file` — L1716.
3. Full `validate_skill(...)` (Tier 1 structural + injection + uniqueness; Tier 2 self-match) — L1734.

If any of these fail, `register` returns `(False, errors)` and the tool reports failure (L283–286)
— but **the user already spent an approval/security interaction on a proposal that was doomed**.
Only the similarity gate (`find_similar_skills`, L194) currently runs before approval. That is the
wasted-call bug.

---

## 3. Research findings (answers to RQ1–RQ6)

### RQ1 — Which checks run after vs before approval today
- **Before approval:** param parse, rating-only short-circuit, no-content guard, justification
  guard, `_coerce_rating` of content rating, frontmatter line-parse, similarity gate
  (`find_similar_skills`, L194).
- **After approval (inside `register_skill_from_content`):** pending-file write, `parse_skill_file`,
  and the entire `validate_skill` — i.e. size, "no valid frontmatter", name present + snake_case,
  description ≥ `MIN_DESCRIPTION_LENGTH` (20), triggers list ≥1, version semver (soft warning only),
  **uniqueness** (`name in existing_names`), body ≥ `MIN_SKILL_BODY_LENGTH` (100), prompt-injection
  (≥2 patterns), and Tier-2 self-match (only when a task text is present).

### RQ2 — Design the fix; option (a) vs (b); drift risk
**Recommendation: Option (b)** — `SkillManager.prevalidate_skill(skill_content, task_text='')`.

- **Why not (a) [call `validate_skill` directly from the tool]:** it would require the tool to reach
  into registry internals for `existing_names` and to replicate two non-obvious derivations. More
  importantly, a naive (a) that hardcodes `task_text=''` is **drift-prone** (see correction below).
- **Why (b):** the manager owns `_skills_registry` + `_write_lock`; encapsulating the registry read,
  the upgrade name-exclusion (mirror of L1730–1733), and the task derivation (mirror of L1723) in one
  method keeps those concerns in the owner and makes the pre-check textually parallel to `register`.
  The tool stays thin.

**⚠️ Correction to the caller's assumption ("Tier 2 self-match is not applicable for agent-proposed
skills since no generating task"):** This is **incomplete / partially wrong.** `register` derives
`validation_task = task_text or frontmatter.get('generated_from_task', '')` (L1723). The tool calls
`register` with **no** `task_text` (defaults to `''`), so `validation_task` becomes the skill's own
`generated_from_task` frontmatter field — and agent/auto-generated skills **commonly carry that field**
(the test helper `_make_skill_content` sets it by default). Therefore Tier 2 self-match **can and does
run** for agent-proposed skills. If the pre-check hardcoded `task_text=''`, a proposal whose
`generated_from_task` fails self-match (score < `AUTO_SKILL_PROMOTION_THRESHOLD`=0.3) would pass the
pre-check but still fail in `register` → **the exact wasted-approval bug we are fixing.** Hence the
pre-check MUST replicate the derivation `task_text or frontmatter.get('generated_from_task','')`, not
hardcode `''`.

**Drift risk (registry changed between pre-check and register):** Possible in a rare race (another
agent registers a colliding name after our pre-check). This is **acceptable and already present today**
(similarity gate → register has the same window). Correctness is preserved because `register` re-runs
`validate_skill` under `_write_lock` as the authoritative check; worst case is one wasted approval in
a race, never an invalid skill getting registered.

### RQ3 — How other tools handle pre-approval validation (pattern consistency)
`shell_cmd.py` (`_call_async`, L398–442) is the precedent: it runs **input validation before the
approval gate** — head/tail-pipe denial guard (L387), cwd resolution (L391–396), command-length check
(L406–407), `_validate_command_input` (L409–411), restricted-agent hard-reject (L417–420) — and only
then calls `request_user_approval` (L429). The proposed change follows the identical shape:
*screen → reject early → approve*. Consistent.

### RQ4 — Existing tests & what's affected / needed
All `propose_skill` tests live in **`tests/test_skill_generation.py`**:
- L1621 section header; L1863–~L2038 `TestProposeSkillRatingModes` (rating-only + content+rating).
- L2039 / L2091 update & candidate-flow tests.
- L2163–2258 `TestProposeSkillSimilarityGate` — **the template to copy.** Its `_make_tool(fresh_manager)`
  (L2166–2172) builds `pool = MagicMock(); pool.skill_manager = fresh_manager;
  pool.operation_manager.request_user_approval.return_value = (True, '')`. Its hard-reject assertion
  is exactly the pattern we need: **`pool.operation_manager.request_user_approval.assert_not_called()`**
  (L2209) plus `result.startswith('REJECTED:')`.

**Affected:** none should break. Valid-content tests still reach approval (auto-approved by the mock).
Invalid-content proposals that previously reached approval+register now return earlier — but no existing
test asserts that invalid content *reaches* approval, so nothing regresses. **New tests needed** in §6.

### RQ5 — Edge cases
- **Rating-only mode (L128–138):** returns before the pre-check is ever reached → untouched by design.
  Existing `TestProposeSkillRatingModes` must keep passing.
- **Update flow (`is_update`):** the pre-check must exclude the incumbent's own name from
  `existing_names` (mirror of L1730–1733) so an update isn't rejected by its own uniqueness check.
- **No-frontmatter content:** the patch block (L222–233) is skipped when `fm_match` is None; the
  pre-check must still run at function-body level (outside that `if`) and reject on "no valid frontmatter".
- **Error-message style:** match the existing similarity-gate REJECTED format (L203–207): a
  `REJECTED: proposed skill '<name>' ...` line, a bulleted list of issues, and a short remediation hint.

### RQ6 — Settings / constants involved
`MIN_DESCRIPTION_LENGTH=20`, `MIN_SKILL_BODY_LENGTH=100`, `AUTO_SKILL_MAX_SIZE_KB=15`,
`AUTO_SKILL_PROMOTION_THRESHOLD=0.3` (Tier 2), all consumed by `validate_skill` (no new settings).
`SKILL_DUP_SIM_THRESHOLD=0.95` is the existing similarity gate (unchanged). **No new settings required.**

---

## 4. Design decision (a vs b) — final

**Option (b).** Add `SkillManager.prevalidate_skill()`; call it from `ProposeSkill.call()` after the
frontmatter patch and before approval. `register_skill_from_content` keeps its under-lock
`validate_skill` as the authoritative check. The two call sites are textually parallel (same
`name` derivation, same `validation_task` derivation, same upgrade exclusion), which is what makes
drift unlikely and reviewable.

---

## 5. Exact changes (file + function level)

### Change 1 — `skills/manager.py`: add `parse_frontmatter` to the parser import
- **Line 38** currently: `from .parser import parse_skill_file`
- Change to: `from .parser import parse_frontmatter, parse_skill_file`
- (`validate_skill` is already imported at L41; `_write_lock`, `_skills_registry` already exist.)

### Change 2 — `skills/manager.py`: new method `prevalidate_skill`
Place it immediately before `register_skill_from_content` (i.e. just above L1682) so the two read
together. Exact body:

```python
    def prevalidate_skill(self, skill_content: str, task_text: str = '') -> Tuple[bool, List[str]]:
        """Pre-approval health check mirroring register_skill_from_content's validate_skill.

        Best-effort screen that runs BEFORE the user-approval prompt so an invalid proposal
        never wastes an approval/security call. Reads the registry under lock (no writes, no
        pending file) and reuses the SAME name derivation + validation_task derivation + upgrade
        name-exclusion as register (L1718 / L1723 / L1730-1733). The authoritative re-validation
        still runs under lock inside register_skill_from_content; this only pre-screens.

        Args:
            skill_content: Full SKILL.md content, with the frontmatter name already patched to the
                           authoritative name (as propose_skill passes it).
            task_text: Optional generating-task text for Tier 2 self-match. When empty, falls back
                       to the frontmatter's generated_from_task field — identical to register's
                       `task_text or frontmatter.get('generated_from_task','')`.

        Returns:
            Tuple of (passed, error_messages) — same shape as validate_skill.
        """
        frontmatter, _ = parse_frontmatter(skill_content)
        name = frontmatter.get('name', '')
        # Mirror register L1723 exactly so Tier 2 self-match behaves identically pre/post approval.
        validation_task = task_text or frontmatter.get('generated_from_task', '')
        with self._write_lock:
            existing = set(self._skills_registry.keys())
        if name and name in existing:
            existing.discard(name)  # upgrade exclusion — mirror register L1730-1733
        return validate_skill(skill_content, name, existing, validation_task, check_injection=True)
```

Notes:
- `name` is derived from frontmatter exactly like register L1718. The tool has already patched the
  name to `proposed_name`, so this equals it. If frontmatter has no name, `name=''` and
  `validate_skill` rejects on "Missing required field: 'name'" regardless (the uniqueness check also
  skips empty names) → **no observable drift** vs register's uuid-fallback path.
- The `_write_lock` is held only to copy `.keys()`; `validate_skill` (pure, no I/O) runs after release.
  No nesting with `_metrics_lock` → respects the established `_write_lock -> _metrics_lock` ordering.

### Change 3 — `tools/custom/propose_skill.py`: insert the pre-approval health check
Insert a **new block at function-body indent (8 spaces)** after the frontmatter-patch block
(which ends at L233) and before the description-building block starting at L235 (`if is_update:`).
It must be OUTSIDE the `if fm_match:` block so it runs even when there is no frontmatter:

```python
        # ── Pre-approval health check (NEW) ─────────────────────────────────────
        # Screen invalid proposals BEFORE the user-approval prompt so a bad skill never wastes an
        # approval/security call. Mirrors register's validate_skill (name derivation + upgrade
        # exclusion + generated_from_task task derivation); register re-runs it under lock as the
        # authoritative check, so no drift can slip through. Runs on the patched content (the
        # authoritative name is already applied). Rating-only mode (above) and the similarity gate
        # are unaffected. Defensive: a pre-check error must not break propose — fall through to the
        # authoritative register validation.
        try:
            passed, health_errors = skill_manager.prevalidate_skill(skill_content)
        except Exception as e:  # pragma: no cover - defensive; pre-check must not break propose
            logger.warning('[PROPOSE-SKILL] pre-approval health check skipped (error): %s', e)
            passed, health_errors = True, []
        if not passed:
            lines = [f"  - {err}" for err in health_errors]
            logger.warning('[PROPOSE-SKILL] REJECTED health check %s (%d issues)',
                           proposed_name, len(health_errors))
            return (f"REJECTED: proposed skill '{proposed_name}' failed pre-approval health checks:\n"
                    + '\n'.join(lines) +
                    "\nFix the issues and re-propose.")
```

Notes:
- The `try/except` mirrors the similarity gate's defensive posture (L183–187, "gate must not break propose").
- The REJECTED message follows the existing similarity-gate style (L203–207): prefix line + bulleted
  issues + remediation hint.
- `logger` is already module-level in this file (L19). No new imports needed in the tool.

**What stays unchanged:** rating-only block (L128–138), no-content/justification guards, similarity
gate (L181–207), version compute (L209–218), frontmatter patch (L220–233), description build
(L235–247), `request_user_approval` (L250–258), and the entire `register_skill_from_content`
(including its under-lock `validate_skill` at L1734). This bounds the diff to: 1 import line, 1 new
manager method, 1 new tool block.

---

## 6. Test plan

Add a new class **`TestProposeSkillPreApprovalHealthCheck`** in `tests/test_skill_generation.py`,
reusing the existing `_make_tool` pattern from `TestProposeSkillSimilarityGate` (L2166–2172) and the
`fresh_manager` fixture + `_isolate_metrics(fresh_manager, tmp_path, reset=True)` autouse fixture.
Copy the `_make_tool` helper (MagicMock pool, auto-approve `(True, '')`).

New tests:

1. **`test_invalid_skill_rejected_before_approval`** — propose content that fails a Tier-1 check
   (e.g. `_make_skill_content(name=..., triggers=[])` → empty triggers, or `body='x'` → body < 100).
   Assert: `result.startswith('REJECTED:')`; `'health check' in result`;
   **`pool.operation_manager.request_user_approval.assert_not_called()`**;
   `m.get_skill_metadata(name) is None`. (Directly proves the fix: no approval consumed.)

2. **`test_valid_content_still_goes_to_approval`** — valid `_make_skill_content(...)`; auto-approved
   mock. Assert: `not result.startswith('REJECTED:')`;
   `pool.operation_manager.request_user_approval.assert_called_once()`; `'registered successfully' in result`.
   (Proves the pre-check doesn't over-reject valid skills.)

3. **`test_update_of_own_name_not_rejected_by_precheck`** — register a skill via
   `m.register_skill_from_content(...)`; then propose an **update of the same name** with valid
   content. Assert: `not result.startswith('REJECTED:')` (the upgrade name-exclusion in the pre-check
   works) and `'updated' in result.lower()`. Mirrors L2212's intent for the new gate.

4. **`test_generated_from_task_tier2_replicated`** *(drift regression guard — the load-bearing one)*
   — propose a skill whose `generated_from_task` does **not** self-match (score < 0.3), e.g.
   `_make_skill_content(name='alpha-skill', description='Unrelated topic about weather forecasting '
   'and seasonal climate patterns', triggers=['weather','forecast'], generated_from_task='docker kubernetes deployment')`.
   Ensure name/description/triggers/body are otherwise valid so **only Tier 2 fails**. Assert:
   `result.startswith('REJECTED:')` and `request_user_approval.assert_not_called()`. This proves the
   pre-check replicates the `generated_from_task` derivation (RQ2 correction) rather than hardcoding
   `task_text=''`.

5. **Existing suites must keep passing (no regressions):** `TestProposeSkillRatingModes` (L1863),
   update/candidate-flow tests (L2039/L2091), and `TestProposeSkillSimilarityGate` (L2163) — run the
   whole file: `pytest tests/test_skill_generation.py`.

Run from the **host shell** (not `code_interpreter`) per project convention.

---

## 7. Risks / regressions

| # | Risk | Failure mode | Mitigation | Confidence |
|---|---|---|---|---|
| R1 | Pre-check hardcodes `task_text=''` → misses Tier-2 self-match that `register` would run via `generated_from_task` | Invalid (Tier-2-failing) proposal passes pre-check, fails in register → wasted approval (the bug we're fixing) | Derive `validation_task = task_text or frontmatter.get('generated_from_task','')` identically to L1723; guard with test #4 | High |
| R2 | Pre-check error breaks `propose_skill` entirely | A bug/exception in the new path 500s every proposal | Wrap call in `try/except` → log + fall through to authoritative register (mirrors similarity gate L183–187) | High |
| R3 | Registry changes between pre-check and register (race) | Approval "wasted" if a colliding name appears in the window | Acceptable; `register` re-validates under `_write_lock` as authoritative — no invalid skill ever registers. Same window already exists for the similarity gate | High |
| R4 | Update rejected by its own uniqueness check | Updating an existing name always fails pre-check | Upgrade exclusion `existing.discard(name)` mirroring L1730–1733; guard with test #3 | High |
| R5 | New I/O hot-path cost | Pre-check slows the approval path | `validate_skill` is pure (no disk); called once per proposal (already an approval-gated, low-frequency op). No new I/O | High |
| R6 | Lock-ordering violation | Deadlock with `_metrics_lock` | Pre-check holds `_write_lock` only to copy `.keys()`, releases before the pure `validate_skill`; never nests `_metrics_lock`. Matches established order | High |
| R7 | Frontmatter patch skipped (`fm_match` None) leaves pre-check inside the wrong scope | No-frontmatter content not screened / NameError | Insert block at function-body indent OUTSIDE `if fm_match:` (after L233, before L235); it runs unconditionally and rejects on "no valid frontmatter" | High |
| R8 | Over-rejection of valid skills | Valid proposals now blocked by pre-check | Pre-check uses the identical `validate_skill` register already requires — anything passing pre-check passes register's Tier-1; guard with test #2 | High |

---

## 8. Verification checklist

Before declaring done, confirm:

- [ ] **RQ1** — The only code that ran before approval was the similarity gate; after this change,
      `prevalidate_skill` (full `validate_skill`) also runs before `request_user_approval`.
- [ ] **Insertion point** — New block is at function-body indent, after L233 (patch) and before L235
      (description); it executes even when `fm_match` is None.
- [ ] **Manager method** — `prevalidate_skill` derives `name` from frontmatter (mirror L1718),
      `validation_task = task_text or frontmatter.get('generated_from_task','')` (mirror L1723), and
      applies upgrade exclusion (mirror L1730–1733). Reads `_skills_registry.keys()` under `_write_lock`.
- [ ] **Import** — `parse_frontmatter` added to manager.py L38; no other new imports.
- [ ] **Authoritative check unchanged** — `register_skill_from_content`'s under-lock `validate_skill`
      (L1734) is untouched and still runs after approval.
- [ ] **Rating-only untouched** — L128–138 returns before the pre-check; `TestProposeSkillRatingModes` passes.
- [ ] **Defensive wrapper** — Pre-check call wrapped in `try/except` that logs and falls through on error.
- [ ] **Message style** — REJECTED health-check message matches the similarity-gate format (L203–207).
- [ ] **Tests** — New `TestProposeSkillPreApprovalHealthCheck` (5 tests, esp. #4 Tier-2 drift guard) pass;
      `request_user_approval.assert_not_called()` holds for all invalid-content cases.
- [ ] **No regressions** — Full `pytest tests/test_skill_generation.py` green (host shell).
- [ ] **No new settings/constants** introduced.

### Open decisions
None — the design is fully pinned. (If a supervisor prefers Option (a) — calling `validate_skill`
directly from the tool with an added `get_registered_names()` accessor — that is viable but weaker on
DRY/ownership grounds; option (b) is recommended and this plan implements it.)
