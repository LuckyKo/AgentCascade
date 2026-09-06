# Deadlock Investigation — Endpoint Slot Pool: Security Check × Forced Compression

**Date:** 2026-08-21
**Incident:** 2026-08-21 ~10:11–10:17 (see `todo.md` lines 130–210)
**Investigator:** deadlock-research-1 (researcher agent)
**Scope:** Investigation only — no fixes proposed here.
**Codebase:** `N:\work\WD\AgentCascade\agent_cascade\` (authoritative package; workspace-root `agent_pool.py`/`tool_dispatcher.py` are stale Phase 4.3 duplicates per `.agent_lessons/lessons_caller_context_endpoint_resolution.md`).

---

## 1. Executive Summary

A forced compression triggered by Maine's post-tool hook deadlocked with an in-flight Security agent check. Both agents resolve to the **single shared sequential slot pool** (`_shared_sequential_slot_`, capacity 1) because both endpoint configs have `concurrency_limit=0`. The Security agent acquired the slot at the start of its `engine.run()` and held it across its entire lifecycle — **including while suspended by compression-halt**. The Compressor spawned by the forced compression needs that same slot to run, but the compressor is what must complete before `resume_all_instances()` clears the halt. Result: a circular wait (Security holds slot → waits for compression → compression waits for Compressor → Compressor waits for slot) that only broke via user intervention (auto-security OFF + manual approval). Compressor_4's queue ticket timed out after exactly 300s; Compressor_5 acquired the slot <1s after Security finally released it — losing that race by milliseconds.

The URL difference in the logs (`http://localhost:1234/v1` vs `http://127.0.0.1:1234/v1`) is **cosmetic in this incident**: for conc=0 endpoints all agents share one pool key regardless of api_base. It remains a latent hazard for concurrency>0 endpoints (BUG-5).

Secondary findings: suspension-exit mis-transition (RUNNING→IDLE instead of SLEEPING), hot busy-spin in the compression wait loop, a ~300s/300s timeout race feeding an immediate retry loop, message-queue drain on run exit (potential wakeup loss), and idle-checker dismissal cancelling queue tickets without releasing held permits.

Confidence in the root-cause chain: **High** (log timeline is fully consistent with code paths).

---

## 2. Line-Number Drift Note (incident log vs current tree)

The incident log references line numbers from the tree as it existed during the incident. Current files have drifted by a few to tens of lines (post-incident edits). Mapping used throughout this report:

| Incident log ref | Current tree ref | Symbol |
|---|---|---|
| `core.py:188` | `engine/core.py:188` | `[SLOT_ACQUIRE] initial` debug log |
| `core.py:391` | `engine/core.py:391` | `engine.run() ENTRY` |
| `core.py:455` | `engine/core.py:455` | `_acquire_slot_with_logging(instance, "initial")` |
| `core.py:703` | `engine/core.py:709–716` | "suspended by compression, waiting" |
| `security_handler.py:552` | `security_handler.py:551–555` | `[SECURITY_SLOT_YIELD]` caller-slot release |
| `security_handler.py:585` | `security_handler.py:585–588` | `[SECURITY_SLOT_ACQUIRE]` holders diagnostic |
| `handler.py:728` | `compression/handler.py:772–775` | "Context usage … forcing compression (attempt #N)" |
| `handler.py:799` | `compression/handler.py:843–845` | "Forced compression raised exception" |
| `compression_exec.py:241` | `engine/compression_exec.py:241–243` | proactive-check trigger log |
| `slots.py:31/42` | `pool/slots.py:31–34 / 41–43` | `_acquire_slot` debug log / error |

---

## 3. Answers to the Six Investigation Questions

### Q1 — How is the slot pool keyed? Do localhost/127.0.0.1 create two pools?

**No URL normalization exists anywhere**, but for this incident it didn't matter:

`api_router_pkg/scheduler.py:48–72` (`_get_or_create_pool`):
```python
if concurrency_limit == -1:
    return None
# BUG FIX: All concurrency=0 endpoints share the same slot to avoid cache trashing.
is_sequential = (concurrency_limit == 0)
slot_key = '_shared_sequential_slot_' if is_sequential else api_base   # L62–63

capacity = concurrency_limit if concurrency_limit > 0 else 1  # 0→1 (sequential)  # L65
```
Same keying repeated in `scheduler.acquire` (L109–111), `count_active` (L185), `get_slot_info` (L343–344), and `tool_dispatcher.py:119`.

- `concurrency_limit = -1` → unlimited, **no scheduling at all** (`scheduler.py:105–107` returns `None`; `slot_queue.py:151–152` returns a no-op release).
- `concurrency_limit = 0` → **one global shared pool `_shared_sequential_slot_`, capacity 1**, regardless of api_base string. This is what Security, Maine, and Compressor_4 all hit.
- `concurrency_limit = N>0` → per-endpoint pool keyed by the **raw api_base string** → here `http://localhost:1234/v1` and `http://127.0.0.1:1234/v1` **would** create two independent pools for one server (latent bug, BUG-5).

Where `concurrency_limit=0` came from: `router.get_effective_concurrency(agent_class)` (`api_router_pkg/router.py:243–280`) returns the matched endpoint's configured limit; if a default config has an `api_base` but no matching endpoint entry it conservatively returns `0` (sequential). Log line `slots.py:31` shows `concurrency_limit=0` for both `Security` and `Compressor`.

Proof both agents shared ONE pool despite different URLs: the timeout error itself —
`Timed out after 300s … pool '_shared_sequential_slot_' … Currently held by: Security_op_fbe874ad` (todo.md L169–175).

### Q2 — Slot acquire/release lifecycle in `engine/core.py`

Acquire sites (only three exist):
- `core.py:455` — `"initial"` at start of `run()`.
- `core.py:2184` — `"after_message_wakeup"` inside `_handle_sleeping_state`.
- `core.py:2253` — `"after_stable_drain"` inside sleep-loop stable drain.

```python
# core.py:168–181 (_acquire_slot_with_logging)
instance._slot_release = self.pool._acquire_slot(
    instance.agent_class, instance.instance_name
)
```
which routes through `pool/slots.py:10–43` → `router.scheduler.acquire(api_base, concurrency_limit, instance_name, agent_class, pool=self)`.

Release sites:
- `core.py:848` — generic `finally` at end of every `run()` (`self._release_slot(instance, ...)`).
- `core.py:2092–2108` — inline release when transitioning RUNNING→SLEEPING (`_transition_to_sleeping`); also saves KV state first.
- `security_handler.py:555`, `agent_invoker.py:224`, `tool_dispatcher.py:501` — explicit caller-yield releases.

**Key finding:** there is **NO release and NO re-acquire around compression-halt suspension**. The suspension wait sites block *inside* `run()` while `instance._slot_release` remains set:
- After LLM stream ends: `core.py:709–716` → `_wait_for_compression_to_clear` (blocks, holding slot).
- Post-turn checks: `core.py:1929–1934`.
- Tool-execution loop: `engine/tool_execution.py:101–106`.

Only the SLEEPING path releases the slot (`_transition_to_sleeping`, `core.py:2074–2123`) and re-acquires on wake (`core.py:2183–2190`, `2252–2257`). Compression-halt does neither. Design intent comments confirm slots are meant to span the whole task: `scheduler.py:26–29` ("agents are strictly serialized — one at a time, from task submission to full completion"), `core.py:432–441` ("parent acquires the slot… Nested agents now yield their caller's slot").

### Q3 — `security_handler.py` yield/acquire logic

Flow: approval event → worker thread spawned (`security_handler.py:314–321`, note `daemon=True`) → `_execute_check` builds prompt + creates `Security_op_<rid>` instance under prompt lock (L405–461) → acquires app-level `exec_lock` (RLock, L497–540) → **releases the CALLER's slot** → runs `engine.run(sec_instance)` synchronously on the worker thread (L591):

```python
# security_handler.py:551–555
if caller_inst_sec:
    logger.debug(
        f"[SECURITY_SLOT_YIELD] Releasing slot for '{caller_agent}' before Security check"
    )
    engine._release_slot(caller_inst_sec, caller_agent, "before_security_check")
```

The comment at L486–489 states the design: *"NO slot borrowing — every agent acquires its own slot."* The Security agent then acquires its own slot via normal `engine.run()` (log: holders empty → acquired). It **holds the slot for its ENTIRE multi-turn agentic execution** (LLM streaming included). On completion, `finally` at L644–657 releases `exec_lock` but relies on `run()`'s own finally to drop the slot ("No re-acquire needed — the next turn acquires its own slot naturally", L655).

Diagnostic helper `_describe_pool_holders` (L705–730) logs pool holders right before acquire (L585–588).

### Q4 — Forced compression trigger + execution model

Trigger chain (all SYNCHRONOUS, on the affected agent's own engine thread):
- Post-tool hook: `engine/tool_execution.py:466–467` → `_proactive_compression_check(..., check_label="post-tool")`.
- Async-drain hook: `engine/core.py:330–334` → same with `check_label="async-drain"`.
- `engine/compression_exec.py:200–253`: token-count check under lock; above threshold logs at L241–243 (the exact log lines seen at 10:11:34 and 10:16:34) and calls `_force_compression` (L245).
- `_force_compression` (L181–197) → `CompressionHandler.execute_force_compression`.

`compression/handler.py:738–848` (`execute_force_compression`):
```python
# handler.py:761–769
exempt = [inst_name]
if instance.parent_instance:
    exempt.append(instance.parent_instance)
for inst in self.pool.instances:
    if inst.startswith('Compressor_'):
        exempt.append(inst)

self.pool.halt_all_instances(except_instances=exempt)
...
result = _compress(                       # handler.py:778–784 — synchronous call
    agent_pool=self.pool,
    target_agent_name=inst_name,
    fraction=COMPRESSION_DEFAULT_FRACTION,
    mode='auto', force=True,
)
...
finally:
    self.pool.resume_all_instances()      # handler.py:847–848
```

`compress_context` (`compression/core.py:530–536`) calls `invoke_compression_agent` → `_execute_compressor_and_extract_summary` (`compression/agent_invoker.py:183–347`) which:
1. Releases the caller's slot (`agent_invoker.py:222–224`, `"before_compression"`).
2. Calls `engine.run(comp_instance)` **synchronously on the calling agent's thread** (L234).
3. Enforces `COMPRESSION_AGENT_TIMEOUT = 300s` measured from invocation start (L211, L245–250; `settings.py:125–128`).
4. On early break closes the generator so the compressor's `finally` runs (L279–289).

So yes — compression runs as a separate `Compressor_*` agent instance that must acquire its OWN endpoint slot, while the caller thread blocks waiting for it.

Halt mechanics are flag-only — they never touch slots. `pool/lifecycle.py:187–206`:
```python
for inst_name in self.instances:
    if inst_name not in skip:
        was_already_halted = inst_name in self._halted_instances
        self.halt_instance(inst_name)                 # → _halted_instances.add(name)  [slots.py:158–160]
        if not was_already_halted:
            self._compression_halted.add(inst_name)
```
Resume: `resume_all_instances` (`pool/lifecycle.py:208–212`) discards flags — nothing else.

### Q5 — Suspension states: SLEEPING vs IDLE

Suspension check/wait (`engine/core.py:1149–1172`):
```python
def _is_suspended_by_compression(self, inst_name):        # core.py:1149–1156
    return inst_name in self.pool._compression_halted

def _wait_for_compression_to_clear(self, inst_name):      # core.py:1158–1172
    while self._is_suspended_by_compression(inst_name):
        if self._is_terminal_stop(inst_name):
            return False
        self.pool.wait_if_paused(timeout=_COMPRESSION_WAIT_TIMEOUT)   # 1.0s, core.py:80
    return True
```

State transitions on exit — the generic `finally` of `run()`:
```python
# core.py:886–893
with instance._state_lock:
    current_state = instance.state
    if current_state in (AgentState.RUNNING, AgentState.SLEEPING, AgentState.COMPLETING):
        self.pool._mark_activity(instance.instance_name)
        instance._transition(AgentState.IDLE)
        logger.debug("EXIT - %s %s→IDLE", ...)
```
There is **no special handling for "was suspended by compression"** on exit: any halted-mid-run agent that leaves the turn loop goes straight to **IDLE**, even with pending async children. SLEEPING is only reached through `_transition_to_sleeping_if_pending` (`core.py:1831–1858`, requires `pool.has_pending(inst_name)`) which is step 3 of `_post_turn_checks` (`core.py:1944–1946`). Additionally, `run()`'s finally unconditionally clears pending async registrations and drains the message queue (`core.py:825–829`, `831–835`) — see BUG-8.

This matches the todo observation: "caller didn't go to SLEEPING state after compression, it went into IDLE."

### Q6 — FIFO queueing ("slot 0 fifos")

Implemented in `slot_queue.py`. One `SlotPool` per slot key; `_shared_sequential_slot_` IS "slot 0":
- Waiters: `OrderedDict[ticket_id → QueueTicket]`, strict FIFO (`slot_queue.py:132`, docstring L7–13).
- Grant predicate: head-of-line AND capacity free, 1s interruptible ticks (L186–189); only head proceeds (L198–202, `_is_head` L335–339).
- Timeout default `QUEUE_WAIT_TIMEOUT = 300` s (L37), used by `scheduler.acquire` (L124–125); timeout logging includes holder diagnostics (L342–350; wrapped with holder info at `scheduler.py:154–165`).
- Release is idempotent by acquisition_id (L209–217).

The FIFO itself worked correctly in the incident. The failure was upstream: a holder that never releases because it's suspended mid-run (BUG-1).

---

## 4. Timeline Reconstruction (code-correlated)

| Time | Event | Code path |
|---|---|---|
| 10:11:32,256 | Security check worker starts for op_fbe874ad | `security_handler.py:333–337`, daemon thread L314–321 |
| 10:11:32,305 | `Security_op_fbe874ad` created | `security_handler.py:430–461` |
| 10:11:32,305 | Caller (`fix-compression-stop-cascade`) slot released | `security_handler.py:551–555` |
| 10:11:32,307 | Security acquires THE shared sequential slot (conc=0, cap 1) | `core.py:455` → `pool/slots.py:38` → scheduler/slot_queue fast-path grant |
| 10:11:32,308+ | Security streams LLM call holding slot | engine streaming loop |
| 10:11:34,004 | Maine post-tool proactive check: 95.4% > 95% threshold | `tool_execution.py:467` → `compression_exec.py:240–245` |
| 10:11:34,005 | Forced compression attempt #1 begins; halts ALL except target/parent/Compressors — incl. `Security_op_fbe874ad` (flag-only, keeps slot!) and later the parent | `handler.py:761–769` → `lifecycle.py:187–206` |
| 10:11:34,26x | Compressor_4 created; `engine.run` tries acquire → enqueued as ticket 1 (waits ≤300s) | `agent_invoker.py:222–234` → `core.py:455` → `slot_queue.py:163–174` |
| 10:11:41,961 | Security finishes stream, detects halt → blocks in `_wait_for_compression_to_clear` WHILE STILL HOLDING SLOT | `core.py:709–716` |
| 10:11:45,172 | Duplicate-check guard ignores re-triggered check | `security_handler.py:293–299` |
| 10:14:53–57 | User disables auto-security, approves op_fbe874ad (manual intervention) | UI/config handlers |
| 10:15:25,724 | Parent (`fix-compression-stop-cascade`) also observed suspended mid-run | `core.py:709–716` |
| 10:16:34,265 | Compressor_4 ticket times out (exactly 300s): running=1/1, waiters=0 | `slot_queue.py:180–183`, `342–350` |
| 10:16:34,267 | Slot-acquire failure propagates | `pool/slots.py:41–43` → `core.py:195–197` |
| 10:16:34,268 | Compression attempt #1 fails; `except` swallows → `return True`; `finally` resumes all instances (flags cleared → Security can proceed) | `handler.py:843–845`, `847–848` |
| 10:16:34,324 | async-drain proactive check immediately triggers attempt #2 | `core.py:330–334` → `compression_exec.py:240–245` |
| 10:16:34,94x | Compressor_5 acquires the just-freed slot (<1s after Compressor_4 gave up) | `core.py:455` |
| 10:17:00 | idle_checker auto-dismisses zombie Compressor_4 | `idle_manager.py:171` |

Circular-wait diagram:

```
Security_op_fbe874ad ──holds──► _shared_sequential_slot_ (cap 1)
        │ blocked in _wait_for_compression_to_clear (unbounded, core.py:1168)
        ▼ wants compression to finish
Compressor_4 ──needs──► the SAME slot, held by Security
        ▲
Maine's thread ──blocked synchronously──► invoke_compression_agent (agent_invoker.py:234)
resume_all_instances() only runs AFTER compression completes (handler.py:848)
⇒ classic circular wait; broken only by user intervention or the 300s ticket timeout.
```

---

## 5. Candidate Bugs (severity-rated)

### BUG-1 (CRITICAL — root cause): Endpoint slot held across compression-halt suspension
- **Where:** wait sites `engine/core.py:709–716`, `core.py:1929–1934`, `engine/tool_execution.py:101–106`; halt is flag-only `pool/lifecycle.py:187–206` + `pool/slots.py:158–160`; resume flag-clear `pool/lifecycle.py:208–212`. No code touches `instance._slot_release` on halt/resume (grep confirms only 3 reacquire contexts, none compression-related: `core.py:455/2184/2253`).
- **Effect:** Any slot-holding agent halted mid-run freezes while keeping the permit. On the shared conc=0 pool (capacity 1) this starves everything else — including the Compressor whose completion is required to clear the halt ⇒ guaranteed circular wait whenever the compression targets a different agent than the slot holder.
- **Evidence:** todo.md 10:11:41 → 10:16:34 (Security suspended ≥4m52s while holding slot).

### BUG-2 (HIGH — enabling condition): Forced compression assumes a slot will be free for the Compressor
- **Where:** `compression/handler.py:761–784` — exempts all `Compressor_*` from halt and launches it synchronously, but performs no slot-availability guarantee; `agent_invoker.py:217–234` yields only the CALLER's slot, not other holders'.
- **Effect:** Compressor joins the FIFO behind a possibly-stalled holder; outer `COMPRESSION_AGENT_TIMEOUT` (300s) vs inner queue wait (300s) makes failure near-certain under contention. Caller thread is blocked the whole time (it's exempted from halt yet frozen anyway — the exemption is illusory).
- Note: exempting the parent (`instance.parent_instance`) doesn't help either — the parent is blocked inside the synchronous compress call.

### BUG-3 (HIGH): Security check monopolizes the single shared sequential slot for its whole multi-minute lifecycle
- **Where:** `security_handler.py:591` (`engine.run(sec_instance)` on worker thread) with slot acquired at `core.py:455`; intentional serialization per `scheduler.py:26–35`.
- **Effect:** A long verdict generation (or any suspension per BUG-1) blocks ALL other agents sharing the conc=0 pool. Converts routine slow checks into system-wide stalls when combined with compression.

### BUG-4 (MEDIUM-HIGH): Suspension-exit mis-transition — RUNNING→IDLE instead of SLEEPING
- **Where:** generic finally `engine/core.py:886–893` transitions any of {RUNNING, SLEEPING, COMPLETING} → IDLE with no compression-suspension awareness; SLEEPING only reachable via `_transition_to_sleeping_if_pending` (`core.py:1831–1858`) requiring `pool.has_pending()`.
- **Candidate trigger paths:**
  - (a) Parent halted mid-run (site `core.py:709` or `1929`) with a pending async child (the awaiting-security shell_cmd). On resume, if the turn loop completes (e.g., `has_pending()` already false because the registration was consumed/cleared earlier, or post-turn checks return False), the loop breaks → finally → **IDLE**, despite outstanding child work. Confidence: HIGH that this transition exists; MEDIUM-HIGH that it's the observed trigger (log lacks the EXIT line to disambiguate).
  - (b) Generator abandoned & closed via `gen.close()` (e.g., `agent_invoker.py:279–289`, router paths) unwinds into the same finally → IDLE. Confidence: MEDIUM.
  - (c) Early-exit path `_setup_turn` empty → return (`core.py:497–541`) → finally → IDLE. Confidence: LOW-MEDIUM (doesn't match "after compression").
- **Effect:** UI/state shows IDLE; agent won't poll pending children like a SLEEPING agent would; wakeups depend solely on queued messages (and see BUG-8).

### BUG-5 (MEDIUM, latent — NOT the incident cause): No URL normalization for per-endpoint pools (conc>0)
- **Where:** `scheduler.py:63,111,185`; `scheduler.get_slot_info` L343–344; `tool_dispatcher.py:119` — raw `api_base` string as key.
- **Effect:** `http://localhost:1234/v1` and `http://127.0.0.1:1234/v1` would form two independent pools for the same physical server, allowing over-subscription of the backend. In THIS incident both agents had conc=0 → same shared key (proven by the timeout message). Risk materializes only for concurrency>0 endpoints; mixed forms demonstrably occur in config/logs.

### BUG-6 (MEDIUM): Busy-spin in `_wait_for_compression_to_clear`
- **Where:** `engine/core.py:1158–1172` loops on `pool.wait_if_paused(timeout=_COMPRESSION_WAIT_TIMEOUT)` (`pool/slots.py:144–146`) — but that waits on the GLOBAL `_paused` Event, whereas compression-halt is per-instance (`_halted_instances`, set by `halt_instance` at `slots.py:158–160` which never clears `_paused`).
- **Effect:** While globally resumed, each `Event.wait(1.0)` returns immediately ⇒ tight hot loop (~100% CPU of that thread) for the entire suspension duration (here ~5 minutes × 2 agents). Correctness unaffected; responsiveness/CPU and log noise affected.

### BUG-7 (MEDIUM): 300s-vs-300s timeout race feeding an immediate retry loop
- **Where:** inner queue wait `QUEUE_WAIT_TIMEOUT=300` (`slot_queue.py:37`; applied `scheduler.py:124–125`) ≈ outer `COMPRESSION_AGENT_TIMEOUT=300` (`settings.py:125–128`, started ~2s earlier at invocation).
- **Failure handling:** `handler.py:843–845` catches the exception, logs, and `return True` → caller continues → next proactive check instantly spawns attempt #2 (observed 10:16:34,324). Under persistent slot contention this produces serial wasted 300s attempts and context stays critical the whole time. Compressor_4 lost its race by <1s (ticket expired 10:16:34,265; slot freed moments later, acquired by Compressor_5 at 10:16:34,94x).

### BUG-8 (MEDIUM): Queue/pending drain on `run()` exit can lose wakeups
- **Where:** `engine/core.py:825–829` (`_async_registry.clear_pending`) and `core.py:831–835` (`drain_queue`) execute unconditionally in the exit finally — including exits caused by suspension-related breaks (BUG-4a).
- **Effect:** Pending async-child completions arriving after this point may find no registered waiter; drained-but-unprocessed messages are discarded silently. Contributes to the IDLE-instead-of-SLEEPING symptom and to "agent never wakes" reports. Confidence in mechanism: HIGH; whether data was actually lost in THIS incident: UNKNOWN (no log line shows dropped items).

### BUG-9 (LOW-MEDIUM): Idle-dismissal cancels queue tickets but cannot release held permits
- **Where:** `AgentInstance.terminate()` → `scheduler.terminate_for_agent` (`agent_instance.py:683`) → `SlotPool.terminate_for_agent` cancels **tickets only** (`slot_queue.py:258–272`, returns `(n_cancelled, 0)`); there is no API to force-release a `_running` SlotHolder.
- **Two failure modes:**
  1. Dismissing an agent WAITING in a slot queue silently cancels its pending acquisition (work item evaporates; callers see failure results only indirectly).
  2. If a HOLDER's thread dies without reaching `run()`'s finally (security workers are daemon threads — `security_handler.py:314–321`), the permit leaks until process restart; `held_duration` grows unbounded (diagnosable via `scheduler.get_status`/`get_slot_holders` but not recoverable).
- In the incident, dismissing Compressor_4 (already dead, mode-1 irrelevant) was harmless cleanup.

### BUG-10 (LOW): Diagnostic noise — "No priorities configured for security"
- **Where:** `engine/llm_call.py:1149` (log at 10:16:34,291). Benign indicator that `Security` has no endpoint-priority chain; consistent with the known caller-context gap documented in `.agent_lessons/lessons_caller_context_endpoint_resolution.md` (system agents resolve endpoints/slots without `caller_agent_type`). No direct role in the deadlock.

---

## 6. Root-Cause Chain (summary)

1. All involved agents (Maine, Security, Compressor) resolve to `concurrency_limit=0` ⇒ one global `_shared_sequential_slot_` pool, capacity 1 (Q1).
2. Security acquires the sole slot at `run()` start and legitimately holds it for its full agentic execution (Q3).
3. Maine's post-tool hook fires forced compression synchronously; halting is FLAG-ONLY and exempts the Compressor (Q4).
4. Security is halted mid-stream → blocks in `_wait_for_compression_to_clear` **still holding the slot** (BUG-1).
5. Compressor_4 queues for that same slot (FIFO works correctly, Q6) → circular wait: Security→compression→Compressor→slot.
6. Both waits are effectively unbounded/coincidentally-bounded (BUG-6 spin, BUG-7 dual 300s timeouts); user intervention broke the stall; Compressor_4 timed out <1s before the slot freed; attempt #2 succeeded (BUG-7 retry churn).
7. Post-resume state damage: exiting agents transition to IDLE rather than SLEEPING (BUG-4), with potential wakeup loss (BUG-8) — matching the todo's second complaint.

## 7. Open Questions / Unknowns

- Which exact trigger path produced fix-compression-stop-cascade's IDLE transition (BUG-4 a/b/c) — needs the `EXIT - … →IDLE` debug line from the session JSONL log to pin down.
- Whether any real messages were dropped by the exit-drain (BUG-8) in this session.
- Why the Security verdict took ~5 minutes to finalize post-approval (model speed vs additional turns) — cosmetic for root cause.
- Whether any conc>0 endpoints exist in production config (determines BUG-5 exposure today vs latent).

## 8. Suggested Next Actions (investigation follow-ups, not fixes)

1. Reproduce in a stress test with conc=0 pool: agent A holds slot + long LLM call; agent B triggers forced compression; assert Compressor starvation (validates BUG-1 deterministically). Prior art: `.agent_lessons/e2e-stress-test-design.md` recommends exactly `concurrency_limit=0` scheduling tests.
2. Extract full DEBUG-level session JSONL around 10:15–10:17 to resolve BUG-4 trigger path.
3. Add holder-liveness diagnostics: alert when `SlotHolder.held_duration` exceeds N minutes (would have surfaced BUG-1/BUG-9-mode-2 immediately).

---

*Report generated from static code analysis cross-checked against the incident log excerpt in `todo.md` L130–210. All file:line refs verified against the working tree on 2026-08-21 (modulo drift table §2).*
