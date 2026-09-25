---
name: shellcmd-long-running-service-lifecycle
description: Manage long-running background services (daemons, pollers, servers) via shell_cmd when the hard 1-hour timeout will kill them — restart strategy, health verification, and user handoff patterns.
source: auto-generated
version: "1.0.0"
triggers:
  - "long running process"
  - "background service"
  - "daemon timeout"
  - "shell_cmd timeout"
  - "restart bridge"
  - "persistent process"
generated_by: orchestrator_Maine_20260924_050909.jsonl
generated_from_task: "Telegram bridge E2E test — process kept dying at shell_cmd's 1-hour cap"
---

## Goal
Avoid wasted turns and user confusion when a long-running service launched via `shell_cmd` hits the hard 3600s timeout and is silently killed.

## Procedure

### Step 1 — Launch with max timeout + async mode
Always use `execution_mode: "async"` and `timeout: 3600` (the maximum). Set a reasonable `heartbeat_interval` (60-120s) so you get liveness signals without flooding context.

```
shell_cmd(command="...", execution_mode="async", timeout=3600, heartbeat_interval=120)
```

### Step 2 — Verify startup immediately
After launch, call `__status` once (or `__wait`) to confirm the process didn't exit with a config error. Many services print a banner or exit code on bad config — catch it in the first few seconds rather than discovering it at hour one.

### Step 3 — Don't echo heartbeats
Each heartbeat consumes a turn and context. If the service is stable (no new output = healthy for a poller), respond with a single short line. Do NOT restate the same "waiting for user" message every beat — it wastes turns that could be used for actual work.

### Step 4 — On timeout, decide: restart or hand off
- **User is present and about to test** → restart immediately with the same command.
- **User is AFK / not ready** → do NOT restart in a loop. Stop, document the exact launch command, and wait. Each restart burns ~2 turns (launch + verify) and the process will just die again at 1h.

### Step 5 — Document the production launch path
For any service meant to run indefinitely, add a "Running as a persistent service" section to its README with NSSM (Windows) or systemd (Linux) examples. The `shell_cmd` 1-hour cap is a development-session limitation, not a production one.

## Tips
- The timeout is **hard** — there is no way to extend it beyond 3600s. Plan around it.
- A "no new output" heartbeat from a long-polling service (PTB, gRPC server, etc.) is **normal and healthy**, not a sign of a hang. Only investigate if you see an error in the output or the process exits.
- If you must keep a service alive across multiple agent sessions, the user needs to run it in their own terminal or under a service manager. Agent-session-launched processes die with the session.
- Always include the full env-var setup in the restart command (don't assume previous `set` commands persist — each `shell_cmd` is a fresh shell).
