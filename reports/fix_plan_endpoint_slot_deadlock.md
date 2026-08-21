# Fix Plan Index — Endpoint-Slot Deadlock (Security × Forced Compression)

**Date:** 2026-08-21
**Author:** deadlock-fixplan-1 (planning only — no implementation)
**Source investigation:** `reports/deadlock_investigation_security_compression_20260821.md` (BUG-1..BUG-10, verified)
**Codebase:** `N:\work\WD\AgentCascade\agent_cascade\`

## Summary of approach

Release the endpoint slot whenever an agent blocks in compression-halt suspension and re-acquire it after resume (reusing the existing sleep-transition / sync-child yield-reacquire idioms), so the Compressor can always win the slot it needs to clear the halt. Make failed compressions back off instead of instantly re-triggering (retry = fresh FIFO-tail acquire at the next natural checkpoint), make the suspension wait actually sleep, and make `run()`'s exit-finally preserve pending wakeups and prefer SLEEPING on suspension-driven exits. Additionally add guaranteed DEBUG-level tracing of the slot queue lifecycle (enqueue/grant/release/cancel) so future scheduling bugs trace from logs alone (BUG-11, observability-only).

**Timeout model (user decision):** ONE shared slot-wait timeout (`QUEUE_WAIT_TIMEOUT=300s`) for all agents, including Compressors — no per-agent overrides, no special-casing. A long wait is correct: with BUG-1 fixed the holder either finishes or releases on suspension.

## Design constraints (user, non-negotiable)

1. **Compression uses the SAME FIFO as everyone else** (`_shared_sequential_slot_`). No dedicated lane, no priority escalation, no bypass.
2. **A failed compression attempt re-queues at the TAIL** — retry is a fresh acquire that waits behind any current waiters. No reservations, no position holding.
3. **Minimal surgical changes**, reusing existing yield/release/re-acquire patterns (sleep transition `core.py:2074–2123`, sync child `tool_dispatcher.py:496–578`, `engine.reacquire_for` `core.py:1994–2072`).

## User clarification (2026-08-21, applies to BUG-4/8)

> Agents should wake up from any messages in the user queue even if they are IDLE or SLEEPING. The SLEEPING state was added to prevent idle-agent dismissal; otherwise message-queue response behavior should not differ between IDLE and SLEEPING.

Consequence: the BUG-4/8 fix does NOT add new wakeup machinery. Its job is narrow: (a) don't destroy queued wakeups / pending async registrations on a suspension-driven exit, and (b) use SLEEPING instead of IDLE when exiting with outstanding work so the idle checker doesn't dismiss a waiting agent. Re-driving from a queue happens through existing paths (new user message restarts generation via `ws_handlers`; async results enqueue to the queue).

## Per-bug plans

| File | Bug | Severity | One-liner |
|---|---|---|---|
| [`BUG-1_slot_release_reacquire_on_halt.md`](fix_plans/BUG-1_slot_release_reacquire_on_halt.md) | BUG-1 (root cause) + BUG-2 side effect | CRITICAL | Release/re-acquire endpoint slot around compression-halt waits |
| [`BUG-7_compression_failure_backoff.md`](fix_plans/BUG-7_compression_failure_backoff.md) | BUG-7 | MEDIUM | Failure backoff gate + honest return values on both failure paths (single shared slot timeout — no per-agent overrides) |
| [`BUG-4_8_suspension_aware_exit_finally.md`](fix_plans/BUG-4_8_suspension_aware_exit_finally.md) | BUG-4 + BUG-8 | MEDIUM-HIGH | Suspension-aware exit finally: keep wakeups, SLEEPING over IDLE when work outstanding |
| [`BUG-6_wait_loop_sleep.md`](fix_plans/BUG-6_wait_loop_sleep.md) | BUG-6 | MEDIUM | Replace global-event spin with real sleep tick |
| [`BUG-11_slot_queue_debug_tracing.md`](fix_plans/BUG-11_slot_queue_debug_tracing.md) | BUG-11 (observability) | LOW | DEBUG traces for slot queue lifecycle: enqueue, grant (w/ waited time), release (w/ held duration), cancel |

## Explicitly NOT fixing now

- **BUG-3 (Security check monopolizes shared slot):** intentional serialization per `scheduler.py` design comments. Once BUG-1 removes hold-during-suspension, Security checks no longer participate in the circular wait; they merely serialize normally. A yield-during-security-check redesign is a behavior change worth its own proposal.
- **BUG-5 (no URL normalization for conc>0 pools):** latent only — incident involved conc=0 (one shared key regardless of URL string). Orthogonal to deadlock; touches scheduler keying in 5 places.
- **BUG-9 (idle-dismissal cancels tickets but can't release held permits):** mode-1 (cancelling a waiter) is arguably correct cleanup; mode-2 (leaked permit on dead holder thread) needs a force-release API — new machinery, and BUG-1's fix eliminates the main producer of zombie holders.
- **BUG-10 ("No priorities configured" log noise):** cosmetic; tied to the known caller-context gap (`.agent_lessons/lessons_caller_context_endpoint_resolution.md`).

Deferral rationale: all four are non-deadlock issues whose blast radius shrinks once holders can no longer freeze mid-run while owning the slot.

## Ordered implementation steps

1. **BUG-6** (standalone 2-line change inside `_wait_for_compression_to_clear`; folded into step 2's edit if preferred).
2. **BUG-1** — release/wait-sleep/save-KV → re-acquire/restore inside `_wait_for_compression_to_clear`; all three call sites benefit automatically. *This is the root-cause fix.*
3. **BUG-7** — failure-streak backoff gate at top of `execute_force_compression` (before `halt_all_instances`), explicit `return False` on both failure paths. No timeout changes: single shared `QUEUE_WAIT_TIMEOUT` for all slots is by design.
4. **BUG-4/8** — suspension-aware exit finally in `run()` (+ `_compression_suspended_at` field on AgentInstance).
5. **BUG-11** (observability-only, independent) — DEBUG lifecycle tracing in `slot_queue.py`: enqueue / grant / release / cancel lines. Can be implemented in any order; placed last because it changes no behavior.
6. Full verification pass (per-plan test sections + integration repro below).

Steps are ordered by dependency: BUG-1 removes the frozen-holder failure mode that BUG-7's backoff guards against; BUG-4/8 depends on the suspension marker introduced with BUG-1.

## Shared verification strategy (integration repro of the original incident)

Scenario mirroring todo.md 10:11–10:16:

1. Pool with one conc=0 endpoint (shared sequential pool, capacity 1). Agents A (slot holder), B (triggers compression).
2. A acquires slot, enters a long LLM stream (mocked/slow backend).
3. B's post-tool hook crosses 95% → forced compression halts A.
4. **Assert:** A's slot is released within ~1 tick of entering the wait (`scheduler.get_status()` running count drops to 0); Compressor acquires the slot in seconds, not 300s; compression completes; A resumes and re-acquires (holder == A again).
5. Failure-injection variant: make the Compressor's LLM raise → assert attempt fails fast, backoff gate suppresses immediate re-halt (halt flags unchanged on next proactive check), and a later checkpoint retries successfully after backoff expiry.
6. Prior art: `.agent_lessons/e2e-stress-test-design.md` recommends exactly `concurrency_limit=0` scheduling stress tests; `run_tests.py` / `pytest.ini` for the suite.

## Risks & open questions (need user decision where marked)

1. **[DECISION — RESOLVED] Timeout model.** User decided 2026-08-21: ONE shared `QUEUE_WAIT_TIMEOUT` for all slots, including Compressors; the previously proposed scoped Compressor timeout (BUG-7b) is dropped. Long waits are correct because holders release on suspension (BUG-1).
2. **Backoff cap vs overflow pressure.** Cap proposed at 600s; if context is critically full and every compression attempt fails, the pre-LLM hard guard still raises `ContextWindowExceeded` only after `compression_max_attempts` (100). Acceptable? (Alternative: lower max-attempts or shorten cap.)
3. **Orphaned SLEEPING sub-agent re-drive.** If a suspension-driven exit preserves state (BUG-4/8 plan), the root agent is re-driven by user message/Resume, but an orphaned sub-agent relies on parent flows. Full auto-redrive is deliberately out of scope (would be new machinery).
4. **Second halt during re-acquire goes unnoticed until next checkpoint** (halt isn't checked inside SlotPool FIFO wait). Pre-existing characteristic of all re-acquire paths; documented as accepted limitation — no deadlock, only a possible advisory-halt violation for one turn segment.
5. **Re-acquire-failure policy after resume** (from BUG-1 plan). Plan follows the existing idiom (sync-child path degrades to slotless with warning, `tool_dispatcher.py:544–562`). Stricter alternative: abort `run()` (loud, keeps strict serialization guarantee, but turns a transient contention blip into a lost turn). Default chosen: degrade + loud `[SLOT_REACQUIRE_FAILED]` warning.
