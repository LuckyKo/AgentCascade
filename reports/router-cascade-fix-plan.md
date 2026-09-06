# Fix Plan — Router Endpoint-Allocation Cascade (single-GPU model-swap storm)

Status: **PLAN v2 — not yet implemented. Requires approval before any code.**
Prepared by: Maine (consolidating verified research from `router-cascade-research`).
**Review:** `reports/router-cascade-fix-plan_REVIEW.md` → APPROVE-WITH-CHANGES. All review
findings folded into this v2 (see §10 changelog).
Incident ref: `logs/console.log` ~L4350-5002 (2026-08-22 07:18–07:24). Memory: `.agent_lessons/endpoint-allocation-cascade-single-gpu.md`.

---

## 0. Guiding principle (non-negotiable)

**The router is the SINGLE POINT OF CONTROL.** Agents ask permission; the scheduler
hands back tickets and tells them when it's their turn — one serialized pointer per
physical server, advanced only on clean release. The router must **defer to the
scheduler** instead of unilaterally swapping models on a shared physical server.

Concretely: when an endpoint fails with a *server-busy-loading* signature (503
"Failed to load model … failed to start"), that is NOT "try the next model now." It is
"this physical server is busy; back off and wait your turn." The router must stop
walking the priority chain and firing successive model-load requests at one loader.

### Hard constraints (do NOT change)
- **FIFO / queue / re-acquire semantics are OUT OF SCOPE.** They work as designed.
  A failed ticket = "ask again, back of the line" is correct FCFS. Do not touch
  `slot_queue.py` grant order, `scheduler.acquire/release`, or `core.reacquire_for`.
- **Different physical server → failover immediately** (unchanged). Only *same physical
  server, different model* triggers back-off. This distinction is the crux of not
  breaking legitimate cross-server failover.

---

## 1. Verified current-state map

### Config (`config/api_endpoints.json`)
- **9 endpoints on `http://127.0.0.1:1234/v1`**, all `concurrency_limit=0`, `max_retries=2`:
  LMS-35B (qwen3.6-35b-a3b), Qwen3.8-27B, Qwen3.8-27B-Ablit, gemma-4-31b-it,
  gemma-4-31B-scotoma-2, qwen3-vl-4b, Agents-A1-APEX-I-Quality,
  Qwen3.8-27B-NVFP4-MTP-ako, and **Ornith on `http://localhost:1234/v1`** (same server).
- Remote/other: opencode.ai/zen/v1 ×2 (conc=1, rpm60), 127.0.0.1:4315 (conc=1, rpm30),
  openrouter.ai (conc=0, rpm60), router.huggingface.co (conc=1, rpm30).
- All 6 agent types have `agent_priorities`; every chain mixes multiple 1234 models + remotes.

### Router (`api_router_pkg/router.py`)
- `__init__` L43-107: `_endpoint_failure_times: Dict[str,float]` at **L86**, keyed by raw `api_base`.
- `get_endpoint_chain` L406-584:
  - Tier 1 own endpoints L453-462.
  - **Tier 3 last-successful L464-482** matches by **raw** `ep.api_base == api_base` (**L473**) — must use normalized base.
  - **Cooldown filter L487-507** keyed by **raw** cfg `api_base` (L492-494) — must use normalized key.
  - Tier 4 default appended L509-519, always last, **NOT** cooldown-filtered.
  - Per-instance cursor rotation L528-551.
- `advance_instance_endpoint` L592-603; `reset_instance_endpoint` L605-615 (cursor — leave as-is).
- `call_with_fallback` L690-970:
  - chain build L721-723.
  - per-cfg endpoint resolution **L739-746**: matches the **FIRST** endpoint whose `ep.api_base == endpoint_base`. With 9 endpoints sharing a base this is an ambiguity (all values identical here, but fragile) — resolve by `(normalized_base, model)` not first-match.
  - attempt loop L775: `range(max_retries+1)` → **3 attempts** per endpoint (`max_retries=2` from config).
  - rate limiting L786-819 keyed raw `endpoint_base`.
  - success → `_last_successful_endpoint_cfg` L833-838.
  - except L840: `AgentTerminatedError` re-raise L843-844; context-exceeded gate L853-924; generic error → backoff via `calculate_backoff` + `_interruptible_sleep` L942-950.
  - after attempts exhausted: "Moving to next" L952; **cooldown record L956-965 keyed `endpoint_base` only**.
  - final raise "All API endpoints exhausted" L967-970.
- **No circuit breaker, no model in any failure key, no normalization anywhere.**

### Scheduler (`api_router_pkg/scheduler.py`)
- `_get_or_create_pool` L48-72 and `acquire` L74-170: `slot_key = '_shared_sequential_slot_' if conc==0 else raw api_base` (L62-63, L110-111).
  - **Key finding:** ALL `concurrency=0` endpoints across ALL servers share ONE pool. So the localhost/127.0.0.1 split is masked for conc=0; normalization matters for (a) conc>0 pools keyed by raw base, and (b) the NEW per-server breaker/cooldown keying we are adding.
- `count_active` L185, `get_slot_info` L343-344 derive `slot_key` the same way — keep consistent.

### SlotPool (`slot_queue.py`)
- `acquire` L139-228: strict FIFO head-of-line grant, 1s interruptible ticks, deadline → `SlotQueueTimeout`. A blocked agent holds a **waiter ticket**, not a permit.
- `release` L230-247 `notify_all`; cancel/terminate paths exist (L355-431).

### Lifecycle
- `engine.run` acquires slot at start (`core.py` L448-461), releases in finally (L858). The agent **holds its lifecycle slot for the entire turn**, including all LLM calls and retries — by design.
- Sync child: `tool_dispatcher._run_child_sync` L480-578 — parent releases before child (L496-504), reacquires in finally (L544-578) via `core.reacquire_for` (L2049-2127, `REACQUIRE_TIMEOUT=30s`).
- Async child: `pool/slots.py register_async_call` L45+ — child `engine.run()` acquires its own slot inside the thread-pool worker; `_acquire_slot` L10-43 resolves raw `api_base`.

### Supporting facts
- `settings.py`: `ENDPOINT_COOLDOWN_SECONDS` default 60 (env `AGENT_CASCADE_ENDPOINT_COOLDOWN`) L166-174.
- `retry_policy.py`: `endpoint_max_retries=1` default; endpoint config `max_retries=2` overrides at router L729/L743. `classify_error` treats '503'/'service unavailable' as retryable.
- Error wrapping: `llm/oai.py` L613-615 wraps OpenAI error → `ModelServiceError(code=str(status_code))`. So a 503 arrives as `ModelServiceError(code='503')` with body "Failed to load model … failed to start".
- Engine outer loop: `engine/llm_call.py _execute_llm_call_with_retry` L221+ (default 3 attempts); classification in `core.py` L1386-1415.
- `state_ops._normalize_api_base` L20 **strips `/v1`** — different purpose (KV-state labels). Do NOT reuse it for pool/breaker keying; the new helper must keep the path.

---

## 2. Change A — api_base normalization helper

**New function** in `api_router_pkg/` (e.g. `normalization.py`, or a module-level
`normalize_api_base()` in `router.py` imported by `scheduler.py`). Distinct from
`state_ops._normalize_api_base`.

Normalizes to a canonical **server identity**:
- scheme lowercased (`HTTP://` → `http://`)
- host: `localhost` → `127.0.0.1` (and `[::1]` → `127.0.0.1` if present)
- strip exactly ONE trailing slash; **keep port and path** (`/v1` preserved)
- result used ONLY as a dict key / identity — never as the wire URL.

Example: `http://localhost:1234/v1/` → `http://127.0.0.1:1234/v1`.

**Every call site that must use it (identity keys, not wire URLs):**
1. `scheduler.py` pool keying for **conc>0** pools (`_get_or_create_pool` L62-63, `acquire` L110-111, `count_active` L185, `get_slot_info` L343-344). (conc=0 already collapses to `_shared_sequential_slot_`; leave that.)
2. `router.py` cooldown record L956-965 and cooldown filter L492-494 — key by `(normalized_base, model)` (see Change C).
3. New circuit-breaker state map key (Change B) — `normalized_base`.
4. `get_endpoint_chain` Tier 3 last-successful match L473 — compare normalized bases, not raw strings.
5. `call_with_fallback` per-cfg endpoint resolution L739-746 — resolve by `(normalized_base, model)` instead of first raw-base match.

The wire URL sent to the client is **never** changed — only identity keys are normalized.

---

## 3. Change B — Per-physical-server circuit breaker

New state map in `APIRouter.__init__` (under `self._lock`):
```
_server_breakers: Dict[normalized_base, {
    'state': 'closed' | 'open' | 'half_open',
    'opened_at': float,          # monotonic
    'window': float,             # current backoff seconds
    'probing': bool,             # exactly one probe in flight (half_open)
}]
```

**Trip condition — classify as SERVER_BUSY_LOADING when:**
- `ModelServiceError` with `code == '503'`, OR error text contains
  `'failed to load model'` / `'failed to start'`; **AND**
- the normalized base hosts >1 enabled endpoint (a multi-model server).
  (Simpler acceptable variant: treat any 503-with-load-signature as server-busy.
  The ">1 endpoint" guard prevents a single-model remote from tripping the breaker on
  an unrelated transient — decide in review.)

**Real per-model errors do NOT trip the server breaker:** 404 model-not-found, 400
invalid-request, etc. → only per-(base,model) cooldown (Change C), and MAY failover to a
different PHYSICAL server (skipping other models on the same busy base).

**Transitions:**
- `closed` + SERVER_BUSY_LOADING → `open`, `opened_at=now`, `window = BASE` (e.g. 20s), cap exponential growth (e.g. ×2, max 120s).
- `open` + `now - opened_at >= window` → `half_open`, allow **exactly one** probe.
- probe success → `closed`, reset `window`.
- probe SERVER_BUSY_LOADING again → `open` again with grown `window`.

**Probe guard (REVIEW C1 — the "serialized by the shared slot" argument is FALSE).** Slot
serialization only holds among agents sharing the SAME pool. An agent on a conc>0 pool
targeting the same normalized base (or a future mixed-pool config) is NOT serialized with
the conc=0 pool, so two agents could both see `probing=False` and both fire. **Do not rely
on slot serialization for the single-probe guarantee.** Instead:
- Use an atomic claim: under `self._lock`, transition `half_open→probing` only if not
  already probing (compare-and-set on the state), so exactly one caller wins the probe.
- The winner fires; losers skip to a different-physical-server endpoint or fail fast.
- Clear `probing` in a `finally` so a hung/terminated probe cannot wedge the breaker.
- (Holding the lock across the HTTP call is NOT required — the atomic claim is enough, and
  holding a lock over a network call would block all router operations. The claim + finally
  release is the correct, low-contention design.)

**Where `call_with_fallback` consults it (the core fix):** BEFORE firing each endpoint in
the chain, check the breaker for that endpoint's normalized base:
- `closed` → proceed.
- `half_open` and not yet probing → allow this one probe; set `probing=True`.
- `open` or (`half_open` and already probing) → **do NOT fire.** Skip to next endpoint,
  but if the next endpoint is on the SAME normalized base, skip it too. If NO endpoint on
  a different physical server remains, **fail fast** (raise a typed error — see Change D)
  instead of retrying 3× per endpoint and hammering the loader.

Net effect: when the server is busy, agents make **zero** HTTP requests against it until
the window elapses and a single probe is allowed. The storm stops.

---

## 4. Decision — blocking vs release (RECOMMENDATION)

**Recommendation: hold the lifecycle slot + FAIL FAST on breaker-open (no mid-turn release).**

Rationale (verified against the code):
- The agent holds its lifecycle slot for the whole turn today (`engine.run` finally-block
  releases). **Any** in-agent wait — router or engine — holds the slot. So "release and
  requeue" is not a free alternative; it would require exiting `engine.run()` mid-turn,
  saving state, and re-entering later. **No such mechanism exists today**, and building it
  means deep surgery in `core.py` run() finally-blocks, `state_ops`, and the dispatcher
  reacquire logic — i.e. exactly the FIFO/re-acquire machinery we were told NOT to touch. High risk, out of scope.
- With fail-fast + hold-slot, breaker-open behavior is **self-limiting**: each queued agent
  gets the slot, checks the breaker, makes zero HTTP calls, raises fast, releases cleanly,
  and the engine-level retry reschedules it later (back of line). Turnover stays high; the
  queue drains quickly. When the window elapses, exactly ONE ticket holder probes.
- **Sync path:** parent released its slot for the child; the CHILD holds the slot during its
  own call. Child failing fast releases cleanly, so the parent's `reacquire_for` (30s bound)
  does not time out due to the breaker. No new deadlock.
- **Async path:** children are separate threads each acquiring their own ticket; fail-fast
  keeps each short so the pool doesn't accumulate long-held slots.
- **Deadlock risk: low** — no new lock-acquisition order; any sleeps use `_interruptible_sleep`
  (termination-aware). **Starvation: bounded** — FIFO guarantees rotation; fast-fails keep turnover high.

**Caveat to document:** `QUEUE_WAIT_TIMEOUT=300s` on `acquire`. During a long breaker window,
waiters may hit the 300s timeout. Mitigation: keep breaker windows modest (20–30s base, capped
exponential), and rely on existing `SlotQueueTimeout` handling upstream (degrade-to-slotless /
error message). Do not raise the queue timeout as part of this fix.

---

## 5. Change C — Per-(base, model) cooldown + global backoff

- Change `_endpoint_failure_times` key from raw `api_base` to `(normalized_base, model)`
  (L86 declaration; record L956-965; filter L492-494). This lets shared-base endpoints be
  distinguished.
- **Global backoff instead of per-endpoint retry-walk:** when the breaker for a base is
  `open`, `call_with_fallback` must NOT spend 3 attempts × N endpoints re-hitting that
  base. It skips all same-base endpoints (Change B) and only retries *different physical
  servers*. If none remain, fail fast once (Change D). This removes the "retry each
  endpoint's 3 attempts in sequence" hammering.
- Keep `ENDPOINT_COOLDOWN_SECONDS` semantics for per-(base,model) real errors; the breaker
  window is a separate, server-level knob.

---

## 5b. Change E — Close the HTTP exit points that bypass the breaker (REVIEW C2, M2)

The breaker only helps if **every** path that talks to a physical server consults it. The
incident log showed "LLM infrastructure changed. Re-detecting context" *during* the storm —
that path bypasses `call_with_fallback` entirely. Verified exit points:

1. **Context-window detection — `llm/oai.py _detect_context_window` (L290-362).** Fires
   `requests.get(f"{base}/models")` (L294) AND a second per-model GET (L362). Triggered
   mid-stream from `_chat_stream` on config change (L420) and on model-id drift (L467).
   **Fix:** before firing, consult the breaker for the normalized base; if `open`, skip the
   probe this cycle (context length is non-critical — use last-known/cached value) and do not
   retry in a tight loop. This is the single most important bypass to close.
2. **Vision captioning — `router.py caption_images` (L1021+).** Calls `get_chat_model`/a model
   directly, not `call_with_fallback`. **Fix:** route its endpoint selection through the same
   normalized-base breaker check (or reuse the chain-filter), so a caption call cannot hammer a
   busy server. Decide in review whether captioning may failover to a different physical server
   or must simply wait/skip.
3. **Image generation — `tools/image_gen.py L43 get_chat_model(llm_cfg)`.** A full LLM chat path
   that bypasses `call_with_fallback`. If its `llm_cfg` targets a local 1234 base it can fire
   model-load + context-detection requests during a storm. **Fix:** route through the same
   normalized-base breaker check (low likelihood — tool usually gets a remote cfg — but close it).
4. **Autoloader KV-state save/restore — `state_ops.py` (L151/170/199/229).** These hit
   `/v1/models/{model}/state/save|load|...`. **Downgrade: nice-to-have, not critical** — they are
   already best-effort (try/except, never block), only fire for autoloader endpoints
   (`is_autoloader_endpoint` gates on `:1234/`/`:9123/`), and do NOT trigger model loads (KV file
   ops). Optionally skip when the base is open; not a storm contributor.

Note (researcher sweep): all other `requests.`/`httpx.` calls in `agent_cascade/` (utils download,
web search tools, image_zoom, amap) target EXTERNAL URLs, not configured endpoints — out of scope.

**Rule going forward:** any new code that opens an HTTP connection to a configured endpoint MUST
either go through `call_with_fallback` or explicitly consult the per-normalized-base breaker.
Add this as a note in the router module docstring so it's not forgotten.

---

## 6. Change D — Fail-fast behavior + engine integration

**REVIEW C3 correction:** the engine retry loop (`llm_call.py _execute_llm_call_with_retry`,
classification in `core.py` L1386-1415) is **within-turn and string/pattern-based**. There is
NO existing "reschedule the turn later, back of line" mechanism — a new exception would just
consume within-turn retry attempts and eventually surface a hard failure. So do NOT assume
the engine re-queues. Two acceptable designs (pick one in implementation; both avoid hammering):

- **D1 (preferred, self-contained in router):** when the breaker is open and no
  different-physical-server endpoint remains, `call_with_fallback` does a **bounded
  interruptible wait** (`_interruptible_sleep`, termination-aware) for the remaining breaker
  window — capped (e.g. ≤ the queue-wait budget) — then retries the chain. This keeps the agent
  in its slot but makes ZERO HTTP calls while open, and self-limits via the cap. No engine
  changes. Risk: holds the slot up to the cap; acceptable because fail-fast already means no
  network load and FIFO turnover is preserved for agents that DO have a different-server option.
- **D2 (engine-aware):** introduce typed `ServerBusyBackoffRequired`; add an explicit branch in
  `core.py` classification / `llm_call.py` to back off with growing delay and, if attempts
  exhaust while breaker still open, surface a clean "server busy, will retry" degradation rather
  than a hard error. More invasive; only if D1's slot-hold is deemed unacceptable in review.

Both MUST: (a) make zero HTTP requests to the busy base while open, (b) NOT trigger the
context-compression path (`FallbackCompressionRequired` is a distinct type — safe), and
(c) be bounded so they cannot hot-loop (see M3 below).

---

## 7. Edge cases & regression risks

1. **Same physical server, different model → back off** (breaker). **Different physical
   server → failover immediately** (unchanged). The normalized base is what separates these.
2. **Genuinely down model on a healthy server** (404/400): must still failover to a
   DIFFERENT physical server. These do NOT trip the server breaker (Change B trip condition),
   only per-(base,model) cooldown. Verify the 503-load-signature check does not swallow real
   model errors.
3. **Tier 4 default fallback** is always appended and NOT cooldown-filtered (L509-519). It must
   ALSO respect the breaker — if the default is on a busy base, skip/fail-fast rather than fire.
4. **Vision endpoints / captioning** (`caption_images` L1021+): uses its own endpoint lookup;
   ensure it consults the same normalized-base breaker so a vision caption call doesn't hammer
   a busy server either (or explicitly document it as out of scope if it targets only remote).
5. **Per-instance cursor rotation** (`advance_instance_endpoint` L592-603): leave mechanics
   unchanged, but ensure the rotated chain is still filtered by the breaker before firing.
6. **First-match ambiguity** at L739-746: resolving by `(normalized_base, model)` fixes a latent
   bug where 9 shared-base endpoints could return the wrong endpoint's settings.
7. **Do not change** `state_ops._normalize_api_base` (strips `/v1`, used for KV labels) — the new
   helper is separate and keeps the path.
8. **Hot-loop bound (REVIEW M3):** the fail-fast/wait path (Change D) MUST be bounded so an agent
   cannot cycle "fail fast → re-attempt → fail fast" with zero progress while the breaker stays
   open. Enforce: a per-call cap on total wait/retry for the busy-base case, and rely on the
   breaker's own exponential window growth to space out probes. Document the max-attempts
   exhaustion path (REVIEW N1): if the engine's within-turn retries exhaust while the base is
   still open, surface a clean "server busy — will retry" degradation, NOT a hard crash.
9. **Tier-4 default endpoint (REVIEW M4):** the always-appended default (L509-519) is not
   cooldown-filtered today; it MUST also be breaker-checked before firing, or a busy-base default
   will still be hit.
10. **Config audit before normalizing conc>0 pools (REVIEW M1):** verify no conc>0 endpoint uses an
    ambiguous form (`localhost` vs `127.0.0.1`, trailing slash) whose pool identity would change on
    normalization. Current config: all conc>0 endpoints are remote with consistent formatting, so
    no merge is expected — but confirm at implementation time and document any intentional merge.

---

## 8. Rollout order (lowest-risk first)

0. **Config audit (REVIEW M1):** confirm no conc>0 endpoint uses an ambiguous api_base form.
   No code — just verify `config/api_endpoints.json` and document any expected pool merge.
1. **A: normalization helper** + apply to Tier-3 match (L473), cooldown keys (L956/492), and
   scheduler conc>0 pool keying. Pure identity-key change; no behavior change for conc=0.
   *Lowest risk, unblocks the rest.*
2. **C: per-(base,model) cooldown** keying. Isolated dict-key change.
3. **B: circuit breaker** state + atomic probe guard (C1) + consult-before-fire in
   `call_with_fallback` (incl. Tier-4 default, M4). The core fix.
4. **E: close HTTP exit points** — gate `_detect_context_window` (oai.py L290) and
   `caption_images` (router.py L1021) on the breaker. *Critical for the fix to actually hold.*
5. **D: fail-fast behavior** (bounded wait D1 preferred, or engine-aware D2) + hot-loop bound (M3).
6. **Fix L739-746 first-match ambiguity** as part of B's endpoint resolution.

Each step independently testable; A and C can land before B/D/E change runtime behavior. E is
required for the fix to be effective — landing B without E leaves the context-detection bypass
hammering the busy server (the exact thing seen in the incident).

---

## 9. Testing strategy

**Unit:**
- `normalize_api_base`: localhost↔127.0.0.1, trailing slash, scheme case, `/v1` preserved,
  port preserved, **idempotency** (REVIEW N2: normalize(normalize(x)) == normalize(x)).
- Breaker state machine: closed→open on 503-load; open→half_open after window; **atomic single
  probe claim under concurrency**; probe success→closed; probe fail→open grown; real errors
  (404/400) do NOT trip it.
- Cooldown keying: two models on same base get independent cooldowns.
- Chain filtering: breaker-open base's endpoints skipped; different-base endpoint still tried;
  Tier-4 default respects breaker (M4).

**Concurrency (REVIEW M5 — required to catch C1):**
- Simulate TWO agents with DIFFERENT slot pools (one conc=0, one conc>0) both targeting the same
  normalized base during half_open. Assert **exactly one** probe request is fired; the other skips
  or fails fast. This directly exercises the atomic probe claim that slot serialization does NOT provide.

**Bypass-path (REVIEW C2/M2):**
- With the breaker open, assert `_detect_context_window` (oai.py L290) makes ZERO `/models` GETs
  for that base this cycle, and `caption_images` does not fire against the busy base.

**Integration (no GPU — mock endpoints):**
- Mock a "server" that returns `503 {detail: 'Failed to load model: Model X failed to start'}`
  for N models on one base, and a healthy different-base endpoint. Assert: no agent fires >1
  request at the busy base while breaker open; failover to the healthy base still works; queue
  drains (agents release cleanly); after window, exactly one probe; recovery resumes service.
- Reproduce the incident shape: two agents queued on the shared slot → confirm zero hammering
  and clean FCFS turnover instead of the observed 503 storm.
- Hot-loop check (M3): hold the breaker open longer than the per-call wait cap; assert an agent
  degrades cleanly ("server busy — will retry") rather than spinning or crashing.

---

## Open items for approval
1. **Fail-fast design (Change D):** D1 (bounded interruptible wait inside router, preferred) vs
   D2 (engine-aware typed exception). Recommend D1 — self-contained, no engine surgery. Confirm.
2. Breaker trip condition: require ">1 enabled endpoint on base" guard, or any 503-load-signature?
   (Recommendation: include the guard; cheap and avoids false trips on single-model remotes.)
3. Breaker window defaults — confirm 20s base / ×2 growth / 120s cap, and the per-call wait cap in D1.
4. `caption_images`: may it failover to a different physical server, or must it wait/skip on a busy base?

---

## 10. Changelog — review findings → resolution (v1 → v2)

| Review ID | Severity | Finding | Resolution in v2 |
|-----------|----------|---------|------------------|
| C1 | Critical | Half-open probe not serialized across conc pools; "concurrency is 1 by construction" is false | §3: atomic compare-and-set probe claim under `self._lock` + `finally` release; do NOT hold lock over HTTP. Added mixed-pool concurrency test (M5). |
| C2 | Critical | `_detect_context_window` (oai.py L290) fires `/models` GETs outside `call_with_fallback`, mid-stream — bypasses breaker | New §5b Change E: gate context detection on the breaker; skip probe when open. Verified against code. |
| C3 | Critical | Engine retry is within-turn + string-based; "reschedule later" does not exist | §6 rewritten: D1 bounded interruptible wait (preferred) or D2 engine-aware typed error; both bounded, zero HTTP while open, no context-compression trigger. |
| M1 | Major | Normalization could change conc>0 pool identity | Rollout step 0 config audit + edge case #10; document any intentional merge. |
| M2 | Major | `caption_images` bypasses breaker | §5b Change E item 2: route through breaker check. Open item #4 for failover-vs-wait policy. |
| M3 | Major | Fail-fast could hot-loop unbounded | Edge case #8: per-call wait cap + breaker exponential growth; clean degradation on exhaustion (N1). Hot-loop test added. |
| M4 | Minor | Tier-4 default not cooldown-filtered, must respect breaker | Edge case #9 + rollout step 3 explicitly include Tier-4 in the breaker check. |
| M5 | Minor | No test for mixed conc pools | Dedicated concurrency test asserting exactly one probe under concurrent different-pool access. |
| N1 | Nit | Max-attempts exhaustion path undocumented | Edge case #8: document clean "server busy — will retry" degradation. |
| N2 | Nit | Normalization idempotency untested | Unit test added: normalize is idempotent. |

**All 10 review findings addressed.** Verdict from review was APPROVE-WITH-CHANGES; v2 resolves
every item. Ready for approval to proceed to implementation.
