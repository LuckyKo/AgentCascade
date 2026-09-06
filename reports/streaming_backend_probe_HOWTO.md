# Streaming Backend Probe — HOWTO

**Purpose:** A small, **debug-gated, non-spammy, evidence-gathering-only** server-side
instrumentation probe to diagnose the streaming **backlog** bug (user sees message N still
streaming in the UI while the LLM is already doing tool calls for message N+1).

It measures **`yield_to_enqueue_ms`** — the time between the moment the engine *yields* a tick
and the moment that frame is *enqueued* onto the WebSocket send queue. This isolates whether the
delay lives in the **backend enqueue path** (this probe) vs. upstream LLM / frontend render
(frontend has its own separate probe — `web_ui/app.js` was NOT touched).

> **Status: TEMPORARY.** Everything is behind one flag. When done, set the flag to `False`
> (or delete the marked snippets) and delete `logs/stream_probe_backend.log`. **Not committed.**

---

## 1. The master flag

| Item | Value |
|------|-------|
| **Flag name** | `STREAM_BACKEND_DEBUG` |
| **Location** | `agent_cascade/api_integration_pkg/streaming.py:24` |
| **Default** | `True` (set to `False` / remove after diagnosis) |

Every probe line of code is gated on this flag. When `False`:
- `_probe_record()` returns immediately (first statement).
- The timing block in `broadcast_stream_update()` is skipped.
- No file is opened, nothing is logged, no measurable overhead.

The optional `yield_time` kwarg defaults to `None` and is **ignored** when the flag is off or
when a caller doesn't pass it — so existing callers are unaffected.

---

## 2. Where the probe log is written

| Item | Value |
|------|-------|
| **File** | `<project_root>/logs/stream_probe_backend.log` |
| **project_root** | `N:\work\WD\AgentCascade` (resolved as `Path(__file__).resolve().parent.parent.parent` from `streaming.py`) |
| **Logger name** | `stream_probe_backend` (dedicated; `propagate=False`) |

It uses a **separate** logger — NOT the app's main `logger` — so probe lines never pollute
`console.log`. The `logs/` dir is created if missing. Lines are compact, one-per-event:

```
HH:MM:SS.mmm [BACKLOG ]inst=<name> yield_to_enqueue_ms=<X> avg=<Y> max=<Z> n=<count> resp_len=<L> tick=<0|1> len_chg=<0|1> qsize=<Q>
```

- `yield_to_enqueue_ms` — **THE key number** (engine-yield → enqueue).
- `avg` / `max` — running average and max for that instance.
- `n` — total broadcasts observed for that instance.
- `resp_len` — `len(turn_output)` at broadcast time.
- `tick` — 1 if this was a streaming tick / tool event, 0 otherwise.
- `len_chg` — 1 if response length changed (new committed message).
- `qsize` — `ws_queue.qsize()` at enqueue time (-1 if unavailable).

---

## 3. Sampling / threshold rules (why it's not spammy)

A line is written **only** when one of these fires, and output is further rate-capped:

| Rule | Condition | Bypasses rate cap? |
|------|-----------|--------------------|
| **Threshold** | `yield_to_enqueue_ms > 100` (meaningful backend delay) | No |
| **Heartbeat** | every **50th** broadcast (running avg/max) | No |
| **Backlog jump** | previous sample `< 50ms` **and** current `> 500ms` → single `BACKLOG` line | **Yes** (always logged) |

**Rate cap:** at most ~**1 line/sec per instance** (non-backlog lines suppressed if the last
line for that instance was written < 1.0s ago). This guarantees a pathological case cannot flood
the file. Backlog-detection lines bypass the cap so a real regression is never missed.

Per-instance state (`count`, `sum_ms`, `max_ms`, `prev_delay_ms`, `last_log_wall`) lives in the
module-level `_PROBE_STATE` dict, guarded by `_PROBE_LOCK` (safe across execution threads).
The whole `_probe_record()` body is wrapped in `try/except: pass` so the probe can **never**
break the broadcast path.

---

## 4. Exact file:line of each addition

### `agent_cascade/api_integration_pkg/streaming.py`
| Line | Addition |
|------|----------|
| 8–10 | imports: `threading`, `time`, `from pathlib import Path` |
| 19–27 | probe banner comment + **`STREAM_BACKEND_DEBUG = True`** (line 24) + `_PROBE_LOCK` / `_PROBE_STATE` |
| 30–54 | `_probe_get_logger()` — lazily builds the dedicated file logger |
| 57–110 | `_probe_record(...)` — timing math, sampling/threshold gating, backlog detection, rate cap |
| 142 | `yield_time: Optional[float] = None` added to `broadcast_stream_update()` signature |
| 223–235 | **timing block**: captures `t_enqueue = time.monotonic()` right before dispatch and calls `_probe_record(...)` (gated on flag + `yield_time is not None`) |

### Call sites — each captures `t_yield = <time>.monotonic()` right after unpacking the engine
yield, then passes it as `yield_time=...`:
| File | Line (capture) | Line (kwarg passed) |
|------|----------------|---------------------|
| `agent_cascade/run_agent_unified.py` | 158 (`t_yield`) | 213 |
| `agent_cascade/advisor_runner.py` | 176 (`_t_yield`) | 189 |
| `agent_cascade/security_handler.py` | 633 (`_sec_t_yield`) | 647 |
| `agent_cascade/compression/agent_invoker.py` | 358 (`_comp_t_yield`, uses `_time.monotonic()`) | 371 |

> Note: in `run_agent_unified.py` the capture sits right after unpack (line ~156) and before the
> `is_stopped()` break, so a tick that is dropped by a stop condition simply isn't broadcast —
> no probe line for it. That's correct (we only time broadcasts that actually happen).

---

## 5. How to interpret the output

The diagnostic question: **where does the delay live?** Three candidates:
(a) engine yields late, (b) backend enqueue path delays frames, (c) frontend delivers/renders late.
This probe measures **(b)** directly. The frontend probe covers **(c)**. If both are small, the
delay is upstream — **(a)**, i.e. the LLM/engine genuinely produced the tokens slowly.

### Reading `yield_to_enqueue_ms`
- **Small / stable (≲ 10–50ms):** the backend enqueue path is **fine**. Frames get from engine-yield
  to queue quickly. The perceived backlog is therefore **upstream** (LLM token cadence) or in the
  **frontend** — cross-check the frontend probe's delivery/render numbers.
- **Large (≳ 100ms, sustained):** the **backend enqueue path is the culprit**. Something between
  `broadcast_stream_update` computing the frame and `put_nowait()` is slow (e.g. GIL contention,
  a blocking call in the same thread, or `build_stream_update_from_pool` serialization cost).

### Reading `qsize` (send-queue depth)
- **Growing / large qsize:** the async **WebSocket send loop is the bottleneck** — frames are being
  enqueued faster than they're being sent out. The queue backs up; this points past yield→enqueue
  to the consumer side of the queue.
- **qsize stays ~0–1 while `yield_to_enqueue_ms` is large:** the queue isn't backing up, so the
  delay is on the *producer* side (serialization / thread contention), not the send loop.

### Reading `BACKLOG` lines
A `BACKLOG` line fires when a single sample jumps from `<50ms` to `>500ms`. These mark the exact
moments the backend stalled — correlate their timestamps with the user-visible lag and with any
tool-call activity for message N+1.

### Reading heartbeats (`n=50/100/...`)
The `avg`/`max` on heartbeat lines give a trend. A rising `avg` over successive heartbeats =
growing backlog; a flat low `avg` = healthy.

---

## 6. Verification performed
- **Syntax:** `ast.parse()` OK on all 5 edited files (streaming.py, run_agent_unified.py,
  advisor_runner.py, security_handler.py, compression/agent_invoker.py).
- **Existing tests:** `tests/test_state_builder.py` → **3 passed**.
  (`tests/test_streaming_buffering_fixes.py` **does not exist** — noted per task.)
- **Functional smoke test:** 200 fast broadcasts produced exactly **1 line** (heartbeat at n=50,
  rest rate-capped) and a forced `<50ms→600ms` jump produced a single `BACKLOG` line that bypassed
  the cap. Confirmed non-spammy + backlog detection + separate-file logging.

## 7. Zero-behavior-change guarantee
- Probe only **reads** (`qsize`, timing) and **logs** to the separate file. It does not alter
  throttling, queue contents, ordering, or any return value.
- `yield_time` defaults to `None`; when the flag is off it is never used.
- `_probe_record()` is fully exception-swallowed; a probe failure cannot affect broadcasting.

## 8. How to remove (trivial)
1. Set `STREAM_BACKEND_DEBUG = False` at `streaming.py:24` (immediate no-op), **or** delete the
   marked snippets: lines 8–10, 19–110, 142, 223–235 in `streaming.py`; the one-line capture +
   `yield_time=` kwarg at each of the 4 call sites (see §4).
2. Delete `logs/stream_probe_backend.log`.
