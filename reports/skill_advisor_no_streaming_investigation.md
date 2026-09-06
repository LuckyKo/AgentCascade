# Skill Advisor: No Streaming Output in UI — Investigation Report

**Date:** 2026-08-30
**Status:** Root cause identified. Fix recommended, NOT implemented (per task scope).
**Related memory:** `.agent_lessons/auto-skill-helper-advanced.md`

## Symptom

When `call_agent(load_skill="AUTO")` triggers the Skill Advisor (Advanced mode), the UI
shows ~16s of complete silence, then the advisor's result appears all at once. Normal
sub-agents stream their LLM tokens to the UI in real time. The advisor does not.

## Root Cause

**The advisor's execution loop never calls `broadcast_stream_update()`.**

`run_lightweight_advisor()` (`agent_cascade/advisor_runner.py`) consumes the
`engine.run(instance)` generator purely for its first-yield timeout guard and then
extracts the final text at the end. It does not push any per-tick WebSocket
`stream_update` events — so the UI has nothing to render until the advisor finishes.

This is a deliberate design choice, documented in the file header and step-6 comment:

```python
# advisor_runner.py:3-8 (module docstring)
# "Intentionally separate from :mod:`agent_cascade.security_handler` (which handles
#  streaming, slot management, and locks in a daemon thread)."

# advisor_runner.py:135
# ── 6. Run engine with a simplified loop (no streaming ticks) ────────
for resp in engine.run(instance):
    if pool.stopped:
        break
    if not got_first_yield:
        ...   # only first-yield guard logic — no broadcast
```

Contrast with the **working** Security agent path, which is the reference implementation:

- `security_handler.py:601` — `for resp in engine.run(sec_instance):`
- `security_handler.py:624-642` — every tick unpacks `(turn_output, is_streaming_tick)`
  and calls `broadcast_stream_update(pool=..., instance_name=sec_state_key, ...)`.

That broadcast is what makes Security agent tokens visible in the UI. The advisor's
runner simply lacks this call.

### Why the UI shows nothing during those ~16s

The UI renders an agent only from two sources:

1. `pool.instance_state[name]` (agent list / tab metadata) — set ONCE at creation by
   `_create_system_agent()` → `_update_webui_state(..., is_initial=True)`
   (`engine/core.py:3277`) and again at cleanup as `active=False`
   (`advisor_runner.py:204-210`). No intermediate updates.
2. `stream_update` WebSocket events carrying per-instance `messages` / `is_partial`
   (built by `api_integration_pkg/state_builder.py:843-846`, consumed by
   `web_ui/app.js` partial-merge path at lines ~2014-2080).

Since the advisor emits no `stream_update` events, source 2 never fires for it. The tab
appears in the agent list (source 1) but its message panel stays empty until completion.
The "sudden appearance" is the final full-state broadcast that follows when the parent
agent's own execution loop resumes and broadcasts its next tick — which includes the
now-completed advisor instance data.

### Answers to the key questions

| Question | Answer |
|---|---|
| Is the advisor's instance visible to the UI at all? | **Partially.** It IS registered: `find_or_create_instance(force_fresh=True)` adds it to `pool.instances` and `_create_system_agent()` initializes `pool.instance_state` + pushes initial state (`engine/core.py:3243-3280`). So a tab/entry appears, but with no streaming content. |
| Does it go through the normal streaming path (SSE/WebSocket)? | **No.** `run_lightweight_advisor()` never calls `broadcast_stream_update()`. The generator yields are consumed but discarded (except for the first-yield guard). |
| Is there a flag suppressing streaming for system/security agents? | **No suppression flag exists.** Streaming is opt-in per execution path: whoever drives `engine.run()` must call `broadcast_stream_update()` each tick. The advisor's runner was written with an intentionally "simplified loop (no streaming ticks)". Note: the Security agent (same agent class) DOES stream — so it's not a class-level suppression, purely a missing call in this one runner. |
| What is needed to make streaming visible? | Add the same per-tick `broadcast_stream_update()` loop the Security handler uses (details below). No UI changes required — the frontend already renders any instance that emits stream_updates with `is_partial`. |

## Exact File/Line Locations

| Location | What |
|---|---|
| `agent_cascade/advisor_runner.py:135-154` | **The gap.** Engine loop with only first-yield guard; no `broadcast_stream_update()`. This is where the fix goes. |
| `agent_cascade/advisor_runner.py:156-159` | Output extracted post-hoc via `extract_instance_output()` — consistent with "no streaming". |
| `agent_cascade/advisor_runner.py:3-8` | Docstring stating intentional separation from the streaming-capable `security_handler`. |
| `agent_cascade/security_handler.py:601-649` | **Reference implementation** of the streaming loop to mirror (unpack tuple → `broadcast_stream_update` → update `instance_state['message_count']`). |
| `agent_cascade/api_integration_pkg/streaming.py:34+` | `broadcast_stream_update()` — shared helper; reads `pool._ws_send_queue` / `pool._ws_loop` when not passed explicitly. Safe from any thread via `run_coroutine_threadsafe`. |
| `agent_cascade/engine/core.py:2797-2914` | Skill Advisor gate inside `_create_and_run_agent()` — calls `run_skill_advisor()` synchronously **before** `find_or_create_instance()`. Runs on the caller agent's execution thread (which already has WS plumbing via the pool). |
| `agent_cascade/engine/core.py:3213-3282` | `_create_system_agent()` — registers instance + initial UI state (why the tab exists but is empty during the run). |
| `agent_cascade/api_integration_pkg/state_builder.py:843-850` | `is_partial=True` set when streaming responses exist; appends partial LLM content to serialized messages. Works for any instance, advisor included. |

## Recommended Fix Approach

**Goal:** make the advisor stream like the Security agent, with minimal change and no
new locks or threads.

### Change 1 — `agent_cascade/advisor_runner.py`, step-6 loop (lines ~135-154)

Mirror `security_handler.py:585-649`. Before the loop, initialize streaming state:

```python
from agent_cascade.api_integration_pkg.streaming import broadcast_stream_update

_last_advisor_send = 0.0
_advisor_tick_num = 0
_advisor_last_resp_len = 0

for resp in engine.run(instance):
    if pool.stopped:
        break

    if not got_first_yield:
        got_first_yield = True
        try:
            first_yield_timer.cancel()
        except Exception:
            pass
        if first_yield_event.is_set():
            result.was_timeout = True
            ...  # existing timeout handling, unchanged
            break

    now_sec = time.monotonic()

    # Unpack (turn_output, is_streaming_tick) from engine.run() yield
    if isinstance(resp, tuple) and len(resp) == 2:
        turn_output, is_streaming_tick = resp
    else:
        turn_output, is_streaming_tick = resp, False

    _last_advisor_send, _advisor_last_resp_len = broadcast_stream_update(
        pool=pool,
        instance_name=instance_name,
        turn_output=turn_output,
        is_streaming_tick=is_streaming_tick,
        tick_num=_advisor_tick_num,
        now_sec=now_sec,
        last_send=_last_advisor_send,
        last_resp_len=_advisor_last_resp_len,
    )
    _advisor_tick_num += 1

    # Keep instance_state fresh for the UI (same as security_handler.py:647-649)
    try:
        with pool._execution._state_lock:
            if instance_name in pool.instance_state:
                pool.instance_state[instance_name]['message_count'] = len(instance.conversation)
    except Exception:
        pass  # non-critical — never break the advisor over UI bookkeeping
```

Notes on safety of this placement:

- **Threading:** `run_skill_advisor()` is invoked synchronously from the caller agent's
  execution thread (`engine/core.py:2867`). That thread already drives normal streaming
  for other agents; `broadcast_stream_update()` uses `run_coroutine_threadsafe` against
  `pool._ws_loop`, so it is safe from this thread (same as Security/Compressor paths).
- **Locks:** the runner's docstring constraint ("no lock held during the LLM call") is
  preserved — `broadcast_stream_update()` acquires no pool locks itself; only the short
  `_state_lock` snapshot above, same pattern as `security_handler.py:647`.
- **No behavior change on failure:** all new code is additive inside the existing loop;
  the timeout/error/finally paths (steps 7-10) are untouched. The instance still gets
  marked inactive + removed from the active stack in `_cleanup_advisor_instance()`, so
  the final state broadcast shows it as idle — consistent with Security agent behavior.

### Change 2 (optional, cosmetic) — final-state flush

After the loop exits normally (step 7), consider one final `broadcast_stream_update()`
with `turn_output=None` to force a clean full-state push for the advisor instance before
cleanup marks it inactive. Not strictly required (the parent's next tick will broadcast
full state anyway), but removes any reliance on that timing and guarantees the completed
advisor text is committed in the UI immediately.

### What does NOT need to change

- **`web_ui/app.js`** — already handles `is_partial` merge for any instance name; no UI work.
- **`engine/core.py` gate block** — invocation site stays synchronous; only the runner's
  internals gain streaming.
- **`skills/advisor.py`** — parsing/verdict logic untouched.

### Risks / edge cases to verify during implementation

1. **Queue pressure:** advisor ticks now share `pool._ws_send_queue` with the parent.
   The helper already drops stale events on `QueueFull` (`streaming.py:30-32`) — no new
   backpressure risk, but confirm no event-ordering assumption is broken when two
   instances broadcast concurrently (parent paused waiting for advisor + advisor ticking).
2. **First-yield timeout path:** if the timer fires and we `break`, ensure no dangling
   partial state is left rendered as "streaming" — cleanup already sets `active=False`;
   verify the UI's partial-restore logic (`app.js:1794-1820`) handles an instance that
   disappears from server data mid-partial (it does: `cleanupStaleSubAgents` at 1811).
3. **Tests:** `tests/test_skill_advisor.py` and `tests/test_skill_advisor_integration.py`
   patch the LLM boundary only; adding a broadcast call requires stubbing
   `pool._ws_send_queue`/`_ws_loop` (or mocking `broadcast_stream_update`) in test fakes.
   Check that FakePool in the integration tests exposes `_execution._state_lock` and
   `instance_state` (it already does, per memory — the gate touches both).

## Summary

The Skill Advisor is fully registered for UI visibility but its runner
(`advisor_runner.py:135-154`) deliberately omits the per-tick
`broadcast_stream_update()` call that every other streaming path makes. The fix is a
~25-line additive change inside that loop, mirroring `security_handler.py:601-649`,
with no UI or gate-block changes required.
