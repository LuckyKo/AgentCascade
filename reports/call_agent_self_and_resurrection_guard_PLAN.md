# Plan: Reject self-call & identity-mismatched resurrection in `call_agent`

Todo ref: todo.md line 139 (open item).

## Problem
When an agent calls `call_agent` with a name that resolves to **itself**, or to an
**existing instance whose persisted identity (name/class) doesn't match** what was
requested, the current code silently reuses/spawns on that instance. For self-calls this
writes into the caller's OWN log file (`{agent_class}_{instance_name}_{ts}.jsonl`),
causing duplicate/corrupted logs and state corruption. Root cause: agents hallucinate or
misunderstand `call_agent` and pass their own name (or a case-variant / wrong-class name).

## Current guards in `handle_call_agent` (agent_cascade/tool_dispatcher.py)
- P2 (L225–231): if target name is already in the execution stack → silently clone to `{name}_child{N}`.
- P5 (L234–239): rejects when requested `agent_class` != existing instance's class (exact-string compare) → "Use a different instance name."
- Active Instance Guard (L258–280): rejects targets in RUNNING/SLEEPING/COMPLETING.
- GAP: plain self-call (same class, not currently stacked) is NOT rejected → reuses caller's own idle instance.

## Identity model (confirmed)
- Log filename = `{agent_class}_{instance_name}_{timestamp}.jsonl`; logger key = `(instance_name, normalized_agent_class)` where normalized = `(x or '').strip().lower()`.
- `_resolve_instance_name` (pool/lifecycle.py L21–48) is CASE-INSENSITIVE: returns the existing canonical name if a case-insensitive match exists.
- `pool.instance_classes` = `{name: inst.agent_class}`; `pool.instances` keyed by canonical name.

## Required behavior (user-confirmed)
Reject with an error directing the caller to use a DIFFERENT instance name, when EITHER:

1. **Self-call** — resolved target name == caller's canonical name (case-insensitive).
   - UNCONDITIONAL: applies even if the caller is currently active/stacked. This REPLACES
     P2's silent `_child{N}` cloning for the self case (no more `Maine_child1` clones).
2. **Resurrection identity mismatch** — an existing instance exists under the RESOLVED name,
   but its persisted identity differs from the request on EITHER dimension (case-insensitive):
   - resolved canonical name != requested `instance_name` (case-only variant, e.g. `maine` vs `Maine`), OR
   - existing class != requested `agent_class` (case-insensitive).

Everything else is left untouched: legitimate re-calling of a DISTINCT idle child still reuses
it; normal fresh creation still works.

## Implementation
All changes in `agent_cascade/tool_dispatcher.py`, inside `handle_call_agent`, AFTER arg
validation (`_validate_call_agent_args`) and BEFORE slot-collision routing. Use the pool's
case-insensitive resolution to get the canonical target name.

1. Resolve canonical target: `target_canonical = self.pool._resolve_instance_name(instance_name)`
   (or replicate its case-insensitive lookup). Caller canonical = `instance.instance_name`
   (already canonical in the pool).

2. **Self-call guard** (new, replaces P2's self-clone):
   ```
   if target_canonical.lower() == caller_name.lower():
       return error: "Cannot call_agent yourself ('{caller_name}'). Use a different instance name."
   ```
   Place BEFORE the existing P2 block. Keep P2 for NON-self names already in the stack (other
   agents' stacked names still clone) — but since self is now rejected first, P2 only sees
   non-self duplicates.

3. **Resurrection identity-mismatch guard** (extend/replace P5 to be case-insensitive + name-aware):
   ```
   existing = self.pool.instances.get(target_canonical)
   if existing is not None:
       req_name_ci  = instance_name.strip().lower()
       can_name_ci  = target_canonical.lower()
       req_class_ci = agent_class.strip().lower()
       have_class_ci= (existing.agent_class or '').strip().lower()
       name_mismatch   = (req_name_ci != can_name_ci)      # case-only variant
       class_mismatch  = (have_class_ci and req_class_ci and have_class_ci != req_class_ci)
       if name_mismatch or class_mismatch:
           return error explaining the existing identity ('{can_name_ci}' is '{have_class_ci}')
                      and instructing to use a different instance name.
   ```
   This supersedes the old P5 exact-string check (keep behavior, make CI + add name variant).

4. Update/replace the P2 comment block so it no longer implies self-cloning is supported.

## Error message style
Match existing guard messages (plain "Error: ..." string, actionable guidance to use a
different instance name). No `[status=...]` tag needed (call_agent returns plain strings;
the Active Instance Guard already does this).

## Tests (new file tests/test_call_agent_self_and_resurrection_guard.py)
- self-call same class → rejected, message contains caller name + "different instance name".
- self-call case-variant (`maine` vs `Maine`) → rejected.
- self-call while caller is stacked/active → still rejected (no `_child1` clone).
- resurrection case-only name variant (existing `Worker`, request `worker`, same class) → rejected.
- resurrection class mismatch (existing coder, request reviewer) → rejected (regression of P5).
- LEGITIMATE distinct idle child re-call (different name, same class) → NOT rejected (proceeds).
- fresh name not in pool → NOT rejected (proceeds).
Use a lightweight fake pool exposing `instances`, `instance_classes`, `_resolve_instance_name`,
`_execution.active_stack`, `get_instance`; drive `handle_call_agent` and assert on the returned
error string / that routing was NOT reached (mock `_run_child_sync`/`_run_child_async`).

## Regression scope
Run: new test file + existing call_agent suites:
tests/test_nested_agent_calls.py, tests/test_call_agent_sync_async_selection.py,
tests/test_e2e_agent_calls.py, tests/test_idle_wakeup_relaunch.py. Confirm no green→red.

## Out of scope
- Do NOT change lifecycle reuse logic for legitimate distinct children.
- Do NOT touch dismiss_agent or compression/security/invoker paths (they use force_fresh / their own flows).
