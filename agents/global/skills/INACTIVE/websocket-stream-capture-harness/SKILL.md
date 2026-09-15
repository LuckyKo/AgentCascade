---
name: websocket-stream-capture-harness
description: Build a test-only harness that captures WebSocket stream_update frames with monotonic arrival timestamps from AgentPool._ws_send_queue, for measuring broadcast timing, throttle behavior, and burst patterns in the streaming pipeline — without modifying production code.
source: auto-generated
version: "1.0.0"
triggers:
  - "stream_update capture"
  - "websocket frame timing"
  - "broadcast throttle test"
  - "streaming burst measurement"
  - "_ws_send_queue"
  - "inter-arrival gap"
  - "streaming benchmark harness"
---

## Goal

Measure exact frame timing (inter-arrival gaps, sub-threshold bursts) in a WebSocket streaming pipeline by capturing every stream-update frame with monotonic arrival timestamps, without modifying production code.

## Generic approach (stack-agnostic)

1. **Build a real pool + session** so payload sizes are realistic (load a real log with many messages; suppress mid-turn compression).
2. **Swap the broadcast queue for a test-owned one** — replace the production send queue/loop with your own `asyncio.Queue` on a dedicated event-loop thread, and mark the pool as running.
3. **Mock only the LLM generator** to yield deterministic chunks at a controlled cadence; restore the original after the run (try/finally).
4. **Drive the broadcast loop** by replicating the production per-tick broadcast call sequence, plus the turn-end forced-final frame that bypasses throttling, then push final state.
5. **Drain the queue on the asyncio loop** (`run_coroutine_threadsafe`), recording `(time.monotonic(), item)` per arrival; signal completion via an event set in `finally`.
6. **Measure**: filter stream-update frames, compute inter-arrival gaps, count sub-threshold (e.g. <10ms / <100ms) bursts and the final-window burst; compare against a control profile to isolate structural vs content-driven bursts.

## AgentCascade implementation notes

The concrete recipe below is for AgentCascade's classes (`AgentPool`, `ExecutionEngine`, `broadcast_stream_update`). Imports are examples — substitute your stack's equivalents.

**1 — Build real pool + session.**
```python
import agent_cascade.agent_pool as ap_mod
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.agents.assistant import Assistant

llm_cfg = {"model": "mock", "api_base": "http://127.0.0.1:9/v1",
           "model_server": "http://127.0.0.1:9/v1", "api_key": "EMPTY"}
pool = ap_mod.AgentPool(llm_cfg, agents_dir=str(cfg_dir))
t = Assistant(llm=dict(llm_cfg), name="researcher", description="test")
pool.templates["researcher"] = t

status = pool.load_session_from_log(str(session_log_path), target_instance=INSTANCE_NAME)  # 50+ msgs
instance = pool.get_instance(INSTANCE_NAME)
instance._generate_cfg_override = {"max_input_tokens": 1_000_000}  # suppress compression
instance.max_turns = 1
```

**2 — Wire the WS queue for capture.** Replace the production queue with a test-owned one:
```python
import asyncio, threading, time
send_queue = asyncio.Queue()
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
pool._ws_send_queue = send_queue
pool._ws_loop = loop
pool.stopped = False
```

**3 — Mock only the LLM generator.** Patch `ExecutionEngine._execute_llm_call` to yield deterministic chunks:
```python
from agent_cascade.llm.schema import Message, ASSISTANT

def _mock_turn(self, instance, template, messages, active_functions):
    parts = []
    for i in range(N_DELTAS):
        time.sleep(GAP)  # simulate LLM streaming cadence
        parts.append(f"chunk_{i}_")
        yield [Message(role=ASSISTANT, content="".join(parts), reasoning_content="")]

original = ExecutionEngine._execute_llm_call
ExecutionEngine._execute_llm_call = _mock_turn
try:
    for resp in engine.run(instance): ...
finally:
    ExecutionEngine._execute_llm_call = original
```

**4 — Drive the broadcast loop (replicate core.py sequence).** For the sub-agent path, replicate the exact `broadcast_stream_update` call sequence per tick, then a turn-end forced-final bypass (`last_send=0.0` always passes throttle), then `push_final_state`:
```python
from agent_cascade.api_integration_pkg.streaming import broadcast_stream_update
_last_sub_send = 0.0; _sub_last_resp_len = 0; _tick_num = 0
for resp in engine.run(instance):
    now_mono = time.monotonic()
    final_resp, is_streaming_tick = resp if isinstance(resp, tuple) else (resp, False)
    _last_sub_send, _sub_last_resp_len = broadcast_stream_update(
        pool=pool, instance_name=instance.instance_name, turn_output=final_resp,
        is_streaming_tick=is_streaming_tick, tick_num=_tick_num, now_sec=now_mono,
        last_send=_in_last_send := _last_sub_send, last_resp_len=_sub_last_resp_len, yield_time=now_mono)
    _tick_num += 1
broadcast_stream_update(pool=pool, instance_name=instance.instance_name, turn_output=final_resp,
    is_streaming_tick=False, tick_num=_tick_num, now_sec=time.monotonic(), last_send=0.0, last_resp_len=0)
engine.stream_publisher.push_final_state(instance, caller_name)
```

**5 — Drain the queue on a separate thread.** The drain must run ON the asyncio loop (`run_coroutine_threadsafe`) because `asyncio.Queue.get_nowait()` is not thread-safe from outside. Record `(time.monotonic(), item)` per arrival; signal completion via an `asyncio.Event` set in a `finally`.
```python
events = []; done = asyncio.Event()
# _consumer(): drive engine.run + broadcasts, finally loop.call_soon_threadsafe(done.set)
threading.Thread(target=_consumer, daemon=True).start()
async def _drain():
    while True:
        try: item = send_queue.get_nowait()
        except asyncio.QueueEmpty:
            if done.is_set():
                await asyncio.sleep(0.05)
                try: item = send_queue.get_nowait()
                except asyncio.QueueEmpty: break
            else: await asyncio.sleep(0.01); continue
        events.append((time.monotonic(), item))
drain_future = asyncio.run_coroutine_threadsafe(_drain(), loop)
drain_future.result(timeout=30.0)
```

**6 — Measure and report.**
```python
arrivals = [(t, ev) for t, ev in events if isinstance(ev, dict) and ev.get("type") == "stream_update"]
gaps = [arrivals[i+1][0] - arrivals[i][0] for i in range(len(arrivals)-1)]
sub_10ms  = sum(1 for g in gaps if g < 0.01)
sub_100ms = sum(1 for g in gaps if g < 0.1)
last_t = arrivals[-1][0]
final_200ms = [a for a in arrivals if last_t - 0.2 <= a[0] <= last_t]
for t, ev in final_200ms:
    inst = (ev.get("instances") or {}).get(INSTANCE_NAME)
    print(f"t={t:.3f} is_partial={inst['is_partial']} history_count={inst['history_count']} payload_bytes={len(json.dumps(ev))}")
```

## Tips / pitfalls

- **Always `time.monotonic()`** for arrival timestamps — wall clock can jump.
- **Drain on the asyncio loop** via `run_coroutine_threadsafe` (not direct loop access) or it deadlocks; call `done.set()` in a `finally`.
- **Suppress compression** (`_generate_cfg_override = {"max_input_tokens": 1_000_000}`) or long sessions trigger mid-turn compression and pollute the stream.
- **`instance.max_turns = 1`** to limit to a single turn; use a real session log (50+ msgs) for realistic payload sizes — synthetic small sessions won't reveal size-dependent throttling.
- **The `last_send=0.0` final bypass is the key burst mechanism** — it makes the throttle check `(now - 0.0 > 0.1)` always true, guaranteeing a turn-end frame regardless of when the last loop tick fired.
- **Compare against a control profile** (same token volume, different content) to isolate structural vs content-driven bursts.
- **Mock must yield `[Message(...)]`** (a list), not a bare Message — engine expects a list of responses.
- Import classes at the top of any thread closure (not outer scope) to avoid `NameError`.
- Print findings via `print()` with pytest `-s`, or use a standalone script (xdist workers may swallow print output).
