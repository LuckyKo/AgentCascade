# Investigation: shell_cmd access for sub-agents spawned during auto-skill extended turns

**Date:** 2026-09-20
**Investigator:** researcher_shellcmd-extturn-investigator_20260920_064156.jsonl
**Scope:** `agent_cascade/engine/core.py`, `agent_cascade/lifecycle_manager.py`, `agent_cascade/utils/disabled_tools.py`, `agent_cascade/constants.py`, `agent_cascade/agent_factory.py`
**Subject todo:** `todo.md:136` — *"agents spawned during the auto skill-gen extra turns lack shell_cmd access, and any agent they spawn lack it too"* (currently **unchecked**)

---

## Verdict: **ALREADY FIXED / NOT REACHABLE at current HEAD**

The described bug (reflection-phase `call_agent` spawns losing `shell_cmd`, transitively) **does not manifest in the current code**. A sub-agent spawned during any auto-skill reflection turn where the parent actually has tools gets its normal tool set — including `shell_cmd` when the parent is an Orchestrator/Generalist/Reviewer (the normal main-agent case). The mechanism that *would* have caused it — the last-turn "disable ALL tools" override leaking into the extension window and propagating via the union — is explicitly suppressed by commit **`a0e1e8e4`** (`todo.md:149`).

> Note on framing: `todo.md:136` and `todo.md:149` share one root mechanism (the last-turn tool-disable firing around the reflection boundary). `149` was fixed directly (`a0e1e8e4`); `136` is resolved as a consequence of the same fix plus the in-loop restructure `3ec6d3d`. I recommend **checking off `todo.md:136`** (or annotating it "resolved by a0e1e8e4/3ec6d3d"). One narrow *residual* edge case is documented at the end — it is a separate latent issue, not the reflection scenario in 136.

**Confidence: Confirmed** (traced full code path + corroborated by existing tests).

---

## 1. How `shell_cmd` access is gated

`shell_cmd` is a normal registered tool (`agent_factory.py:92-94`, `ShellCmd()`), like every other tool. Its availability for an instance is decided **entirely by the `disabled_tools` policy** — there is no separate per-tool permission flag, role check, or turn-phase condition specific to `shell_cmd`.

The single source of truth is `resolve_disabled_tools_for_agent()` in `agent_cascade/utils/disabled_tools.py:68-153`, which accumulates a disabled-set from 4 layers (top-down union):
1. **Instance override** — `instance._generate_cfg_override['disabled_tools']` (`disabled_tools.py:117-120`)
2. **Template config** — `template.llm.generate_cfg['disabled_tools']` (`disabled_tools.py:122-125`)
3. **Agent-class defaults** (defense-in-depth, always applied) (`disabled_tools.py:127-140`, values in `constants.py:35-131`)
4. **Default safe baseline** for dynamically-loaded agents with no config (`disabled_tools.py:142-151` → `DEFAULT_NEW_AGENT_DISABLED_TOOLS`, `constants.py:110-131`)

**Key fact for this bug:** among the *Layer-3* class defaults, only **Writer** disables `shell_cmd` (`constants.py:101-103`); Security/Compressor also do. Coder / Researcher / Orchestrator / Generalist / Reviewer have no Layer-3 `shell_cmd` entry.

**Layer-4 nuance (read-only baseline):** Coder and Researcher are *not* in the Layer-3 if-elif chain (`disabled_tools.py:129-140`), so a **root/standalone** Coder/Researcher with no explicit `disabled_tools` config falls to Layer 4 → `DEFAULT_NEW_AGENT_DISABLED_TOOLS` (`constants.py:110-131`), which **does** disable `shell_cmd`. This is the by-design "dynamically-loaded agents start read-only" baseline — and it applies identically to normal and reflection spawns, so it is **not** the reflection bug in 136.

**Why spawned children are unaffected by Layer 4:** `propagate_settings` *always* writes `cfg['disabled_tools'] = list(merged)` onto the child's override (`lifecycle_manager.py:665`). A non-empty dict containing the `disabled_tools` key makes `has_explicit_config=True` in the resolver (`disabled_tools.py:117-120`), so **Layer 4 is skipped for every call_agent-spawned child.** A spawned child's effective disabled set is therefore exactly the union computed at spawn (caller's resolved set ∪ child-specific-from-caller-dict ∪ live UI) — no Layer-4 baseline injected.

The only place in the engine that writes the instance override's `disabled_tools` is the **last-turn disable**:
- `core.py:908` — `instance._generate_cfg_override['disabled_tools'] = all_tools` (sets *every* tool, incl. `shell_cmd`)
- `core.py:1002` — `instance._generate_cfg_override.pop('disabled_tools', None)` (cleanup after the LLM call)

These are the **only two** `disabled_tools` writes in `engine/core.py` (verified by grep). There is no reflection-phase tool restriction anywhere.

## 2. What the extended-turn (skill-reflection) phase does to tool availability

The auto-skill reflection is an **in-loop budget extension**, not a separate run. Trigger lives at Phase 5 (`core.py:1023-1053`), firing only on *genuine* natural completion when `_try_auto_skill_extension()` returns True. It:
- snapshots the pre-reflection output (`core.py:348`),
- injects the reflection prompt (`core.py:364-367`),
- sets the one-shot flag `instance._auto_skill_proposed = True` (`core.py:370`),
- extends the budget: `max_turns = _current_turn + AUTO_SKILL_EXTRA_TURNS`, `turns_available = AUTO_SKILL_EXTRA_TURNS` (`core.py:1035-1050`).

**It sets NO tool-restricting flag.** `_try_auto_skill_extension()` (`core.py:271-377`) mutates only `_auto_skill_task_output`, the conversation, and `_auto_skill_proposed`. No `disabled_tools`, no restricted mode.

The interaction with the last-turn disable is the crux (`core.py:871-909`):
```python
if turns_available == 1:
    _auto_skill_will_extend = self._auto_skill_gates_met(instance)   # core.py:878
    ...
    if not _auto_skill_will_extend:                                  # core.py:900
        ...
        instance._generate_cfg_override['disabled_tools'] = all_tools  # core.py:908
        final_turn_tools_disabled = True
```
`_auto_skill_gates_met()` (`core.py:246-269`) returns True iff the extension *could* fire (skill manager present, `auto_skill_enabled`, load-mode ≠ NONE, `_current_turn > auto_skill_min_turns`, and one-shot flag **unset**).

Consequences at current HEAD:
- **Triggering turn** (gates met): tools are **NOT** disabled (`core.py:900` is skipped). The parent keeps `shell_cmd`.
- **Reflection turns 1..N−1** (turns_available > 1): the `turns_available == 1` block doesn't run → override's `disabled_tools` is **not set**. Parent keeps all tools and *can* call `call_agent`.
- **Reflection final turn** (turns_available == 1, `_auto_skill_proposed` now True → gates False): tools **are** disabled (`core.py:908`) — but the parent then has **zero tools**, so it *cannot* spawn anything on that turn.

So on every reflection turn where a spawn is even possible, the parent's override carries **no** `disabled_tools`.

## 3. How restrictions propagate to spawned sub-agents (the "transitive" part)

The sole propagation point is `lifecycle_manager.propagate_settings()`, called from the call_agent spawn path at `core.py:3391` (`_create_and_run_agent`). The child's disabled-set is computed at `lifecycle_manager.py:620-666`:
```python
caller_disabled = resolve_disabled_tools_for_agent(
    instance_override=caller_inst._generate_cfg_override,   # ← reads the CALLER's live override
    template_cfg=caller_template.llm.generate_cfg, ...)     # lifecycle_manager.py:620-625
...
existing_disabled = normalize_disabled_tools(cfg.get('disabled_tools'))
merged = merge_disabled_tools(existing_disabled, caller_disabled)          # union
merged = merge_disabled_tools(merged, child_disabled_from_caller_cfg)      # union
merged = merge_disabled_tools(merged, live_ui_disabled)                    # union
cfg['disabled_tools'] = list(merged)                                       # lifecycle_manager.py:665
```
`merge_disabled_tools()` is a plain **union** (`disabled_tools.py:191-204`) — "if *either* side disables a tool it stays disabled." This union is exactly what makes the bug *transitive*: whatever is in the caller's override `disabled_tools` at spawn time flows into the child, and then into any grandchild.

**Therefore the bug is reachable if and only if the caller's `_generate_cfg_override['disabled_tools']` includes `shell_cmd` (i.e., `all_tools`) at the moment of a reflection-phase spawn.** Per §2, that state never co-occurs with a spawn-capable parent turn at current HEAD → **not reachable**.

## 4. Is it present at current HEAD? — code-path trace

Reflection-phase `call_agent` spawn (parent on a non-last reflection turn):
1. Parent's `_generate_cfg_override['disabled_tools']` is **unset** (only `core.py:908` sets it, and that requires `turns_available==1` + gates-not-met).
2. `propagate_settings` → `caller_disabled = resolve_disabled_tools_for_agent(override-without-disabled_tools, ...)` = caller's **class defaults only**.
3. The caller's resolved set includes `shell_cmd` only if the caller is a root/standalone Coder/Researcher (Layer-4 baseline, §1) or a Writer/Security/Compressor (Layer 3). For an **Orchestrator / Generalist / Reviewer** main agent (the normal case), the resolved set has no `shell_cmd` → child keeps `shell_cmd`. ✅ (A root-Coder caller carrying `shell_cmd` via Layer 4 would affect *normal* spawns identically — not a reflection-specific effect.)
4. Grandchild spawn repeats the same union from the (clean) child override → also keeps `shell_cmd`. ✅

The only turn where `disabled_tools=all_tools` is set is a genuine final turn with **no** extension, on which the parent has no tools and cannot spawn. Net: **normal spawns and reflection spawns are behaviorally identical at HEAD** — both yield `shell_cmd`. The bug's distinguishing condition (reflection ≠ normal) no longer exists.

### Corroborating tests (all in `tests/test_skill_generation.py`, passing)
- `test_tools_enabled_on_triggering_turn` (`:1246`) — asserts `'disabled_tools' not in override` after a triggering natural end.
- `test_last_turn_natural_end_extension_keeps_tools_enabled` (`:1533`) — triggering LLM call sees `disabled_tools=None` (the `a0e1e8e4` regression guard).
- `test_tool_disable_prefix_stability_across_trigger` (`:1603`) — triggering turn and first reflection turn see the **same full** tool set.
- `test_reflection_final_turn_still_disables_tools` (`:1588`) — only the reflection's own final turn disables tools (clean final answer); that turn can't spawn.

## 5. What fixed it (commit/change + how)

- **`a0e1e8e4`** — `fix(engine): skip last-turn tool-disable when auto-skill reflection will extend (todo.md:149)`. Introduces the shared helper `_auto_skill_gates_met()` and makes the final-turn block (`core.py:871-909`) **skip both** the final-turn warning and the `disabled_tools=all_tools` override when the extension is about to fire. This removes the window in which a reflection-boundary spawn would inherit `all_tools` (and hence lose `shell_cmd`) via the §3 union.
- **`3ec6d3d`** — `todo.md:133` in-loop restructure. Replaced the old *post-run second `engine.run()`* skill-gen design with an in-loop budget reset at natural completion, so there is no longer a separate skill-gen sub-run inheriting a disabled-tool state from the main run's last turn.

Together these eliminate the tool-disable leak around the reflection boundary. (`a0e1e8e4`'s stated motivation was KV double-reprocess on `todo.md:149`; it incidentally closes `136` because both ride the same mechanism.)

## 6. Residual edge case — identified, but **already compensated** at HEAD (not a live risk)

While tracing I found a narrow theoretical gap, then confirmed it is neutralized:

- On a genuine final turn (gates **not** met), `core.py:908` sets `disabled_tools=all_tools` and `final_turn_tools_disabled=True`.
- The cleanup pop (`core.py:999-1002`) is **skipped** if the loop breaks on the stop paths — terminal stop (`core.py:984-987`) or compression-wait stop (`core.py:988-994`). So a stopped instance can momentarily carry `disabled_tools=all_tools` in its override.
- **However**, instance reuse routes through `initialize_conversation(is_reuse=True)`, which sets `instance._generate_cfg_override = None` at **`lifecycle_manager.py:363`** — and this runs *before* `propagate_settings` (`core.py:3382` → `:3391`). The stale override is therefore wiped on every recall, and propagation starts clean. The gap never reaches a spawn.

Conclusion: no action required for 136. (Optional defence-in-depth only: move the pop into a guaranteed `finally` so it also runs on the stop breaks — cosmetic at HEAD given line 363.)

---

## Open questions / remaining unknowns
- I did not reconstruct the exact pre-`3ec6d3d` state to prove 136 was *ever* reachable (not required for the verdict, which is about HEAD). The historical mechanism is inferred from the todo history (`133`/`149`), confidence **likely** on the "was it ever valid" sub-question, **confirmed** on "is it valid now."
- No test currently asserts end-to-end that a *reflection-phase `call_agent` child specifically* retains `shell_cmd`. The existing tests assert the parent's override state (which is sufficient to prove non-reachability), but a direct spawn-during-reflection assertion would make this regression-proof.

## Suggested next actions
1. **Check off `todo.md:136`** with an annotation: "resolved by a0e1e8e4 (+ 3ec6d3d); verified non-reachable at HEAD — see plans/shellcmd-extended-turns-investigation.md."
2. (Optional, cosmetic defence-in-depth only) Move the `disabled_tools` pop into a guaranteed `finally` so it also runs on the stop breaks — note this is already compensated at HEAD by `lifecycle_manager.py:363` clearing the override on reuse.
3. (Optional) Add one regression test: spawn a child via `call_agent` on a non-last reflection turn and assert `shell_cmd ∉ child._generate_cfg_override['disabled_tools']`.
