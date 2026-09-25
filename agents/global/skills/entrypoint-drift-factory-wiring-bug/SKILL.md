---
name: entrypoint-drift-factory-wiring-bug
description: Diagnose and fix "side-effect wiring lives only in one launcher's __main__ block" bugs where a factory-based app (create_app) is launched by multiple entry points but a setup side effect (attaching a supervisor/manager/callback to a shared object) runs on only one of them, so the feature silently no-ops on the others.
source: auto-generated
version: "1.0.0"
triggers:
  - "feature not starting on restart"
  - "only works in one entry point"
  - "supervisor never attached"
  - "getattr returns None silent no-op"
  - "__main__ block never executed"
  - "create_app multiple launchers"
  - "bridge/child process not spawned"
generated_by: orchestrator
generated_from_task: "Telegram bridge does not auto-start on AgentCascade restart — supervisor attached only in api_server.py __main__, but production runs start_api_server.py which calls create_app() and never executes that block."
---

## Goal
Find and fix bugs where a required setup side effect is wired into ONE launcher's `if __name__ == '__main__':` block, but the app is actually launched through a shared factory (e.g. `create_app()`) by MULTIPLE entry points — so on every other path the object stays unset (`None`) and downstream consumers silently no-op with zero error/log.

## Why this class of bug is sneaky
- The wiring code is *present* in the repo, so a grep for the class name "looks fine."
- It fails **silently**: the consumer uses `getattr(shared_obj, 'attr', None)` and just skips when `None`. No exception, no log line — which is why it survives to production.
- The bug is invisible until you realize the *running* process was launched by a different file than the one containing the wiring.

## Procedure

### Step 1 — Prove WHICH entry point actually runs in production
Don't trust "where the code lives." Check the live process:
```bash
# Windows (AgentCascade): find the running launcher
wmic process where "name='python.exe'" get ProcessId,CommandLine   # or tasklist /v
```
Note the exact script (e.g. `start_api_server.py`) and its args. This is your ground truth for which code path executes.

### Step 2 — Locate every assignment of the wiring attribute (not just the class name)
Grep for the *attribute assignment* on the shared object, not the import:
```bash
# e.g. who sets agent_pool.telegram_supervisor?
grep -rn "telegram_supervisor\s*=" --include=*.py .
grep -rn "if __name__ == '__main__'" --include=*.py .   # map which file(s) guard it
```
If the ONLY assignment sits under a `if __name__ == '__main__':` guard, and the production launcher (Step 1) is a *different* file that imports the factory instead of running that block → **confirmed root cause.**

### Step 3 — Enumerate ALL launchers of the factory (the critical step reviewers often miss)
The bug is usually not just "one wrong entry point" — it's "every launcher except the one with the wiring." Grep for every caller of the factory:
```bash
grep -rn "create_app\|from <pkg>.api_server import" --include=*.py .
```
Each non-test file that calls `create_app(...)` and runs its own server (uvicorn/`server.run()`) is a separate launcher that ALSO lacks the wiring. Fixing only the production one leaves siblings broken — surface this to the user as a scope decision, don't silently pick.

### Step 4 — Confirm the silent no-op at every consumer
Grep for `getattr(<obj>, '<attr>', None)` and read each site. Verify that when the attr is `None`, the code path is skipped without logging (that's the "no log line to explain why" symptom). This confirms the failure mode matches the user report.

### Step 5 — Choose the fix: surgical vs root-cause
- **Option A (surgical, minimal):** duplicate the attach into each launcher that needs it, right after `create_app(...)` succeeds and before the server runs. Lowest risk; but creates N divergent sites (maintenance hazard — next entry-point change reintroduces it).
- **Option B (root cause):** move the wiring INTO the shared factory (`create_app`) or a shared init helper so every launcher gets it automatically, and delete the dead `__main__` duplicate. Single source of truth. More invasive: check whether the factory has access to values the wiring needs (e.g. the runtime port is often NOT passed into `create_app`, so you'd thread it through or derive it) and run the full test suite since many tests call the factory.
- **Always** present A vs B as a decision to the user with a risk/scope table; don't silently expand scope (prefer minimal safe changes), but make the recurrence risk of A explicit.

## Tips
- The f-string gotcha: when duplicating an attach that builds a URL from the port, use single-brace `f'http://127.0.0.1:{args.port}'`. If you copy-paste from a plan/template rendered through another layer it can arrive double-braced (`{{args.port}}`) → literal `{args.port}` as the base URL. Verify the actual bytes in the file.
- Keep the attach **non-critical**: wrap in try/except that logs a warning and continues — a supervisor-construction failure must never crash server startup.
- When a reviewer flags "can't verify no other files changed" citing a stale generated `diff_output.txt`/spillover artifact, resolve it with authoritative `git status --short` + `git diff <file>` — attribute each modified file to your session vs pre-existing work before committing.
- Verify scope of values in the new block: confirm each referenced name (`agent_pool`, module-level constants, `args.*`, `logger`) is genuinely bound at the insertion point (e.g. `agent_pool` bound earlier in the same `__main__` block).
- After the fix, validation = restart AC (or toggle off→on if a runtime handler exists), confirm the child process now exists, and confirm the expected log line appears where it previously was absent.
