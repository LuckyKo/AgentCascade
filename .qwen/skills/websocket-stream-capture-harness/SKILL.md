---
name: websocket-stream-capture-harness
description: Build a test-only harness that captures WebSocket stream_update frames with arrival timestamps from AgentPool._ws_send_queue, for measuring broadcast timing, throttle behavior, and burst patterns in streaming pipelines.
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
generated_by: researcher
generated_from_task: "Build and run a test-only reproduction harness for a sub-agent streaming burst bug, capturing every stream_update on pool._ws_send_queue with arrival timestamps"
---

## Goal

Enable researchers to measure exact frame timing (inter-arrival gaps, sub-threshold bursts) in AgentCascade's WebSocket streaming pipeline by capturing every `stream_update` event on `pool._ws_send_queue` with monotonic timestamps, without modifying production code.

## Procedure

### Step 1 — Build the Real Pool + Session

```python
import agent_cascade.agent_pool as ap_mod
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.agents.assistant import Assistant

llm_cfg = {"model": "mock", "api_base": "http://127.0.0.1:9/v1",
           "model_server": "http://127.0.0.1:9/v1", "api_key": "EMPTY"}
pool = ap_mod.AgentPool(llm_cfg, agents_dir=str(cfg_dir))
# Register template(s) needed by the session log's agent_class
t = Assistant(llm=dict(llm_cfg), name="researcher", description="test")
pool.templates["researcher"] = t

# Load a real session (50+ messages for realistic payload sizes)
status = pool.load_session_from_log(str(session_log_path), target_instance=INSTANCE_NAME)
instance = pool.get_instance(INSTANCE_NAME)
instance._generate_cfg_override = {"max_input_tokens": 1_000_000}  # suppress compression
instance.max_turns = 1
```

### Step 2 — Wire the WebSocket Queue for Capture

Replace the production queue with a test-owned one:

```python
import asyncio, threading, time

send_queue = asyncio.Queue()
loop = asyncio.new_event_loop()
loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
loop_thread.start()
pool._ws_send_queue = send_queue
pool._ws_loop = loop
pool.stopped = False
```

### Step 3 — Mock Only the LLM Generator

Patch `ExecutionEngine._execute_llm_call` to yield deterministic chunks:

```python
from agent_cascade.llm.schema import Message, ASSISTANT

def _mock_turn(self, instance, template, messages, active_functions):
    parts = []
    for i in range(N_DELTAS):
        time.sleep(GAP)  # simulate LLM streaming cadence
        parts.append(f"chunk_{i}_")
        yield [Message(role=ASSISTANT, content="".join(parts), reasoning_content="")]

# Apply/restore around engine.run():
original = ExecutionEngine._execute_llm_call
ExecutionEngine._execute_llm_call = _mock_turn
try:
    for resp in engine.run(instance):
        ...
finally:
    ExecutionEngine._execute_llm_call = original
```

### Step 4 — Drive the Broadcast Loop (Replicate core.py Sequence)

For sub-agent path, replicate the exact `broadcast_stream_update` call sequence:

```python
from agent_cascade.api_integration_pkg.streaming import broadcast_stream_update

_last_sub_send = 0.0
_sub_last_resp_len = 0
_tick_num = 0

for resp in engine.run(instance):
    now_mono = time.monotonic()
    final_resp, is_streaming_tick = resp if isinstance(resp, tuple) else (resp, False)
    _in_last_send = _last_sub_send
    _last_sub_send, _sub_last_resp_len = broadcast_stream_update(
        pool=pool, instance_name=instance.instance_name,
        turn_output=final_resp, is_streaming_tick=is_streaming_tick,
        tick_num=_tick_num, now_sec=now_mono,
        last_send=_in_last_send, last_resp_len=_sub_last_resp_len,
        yield_time=now_mono)
    _tick_num += 1

# Turn-end: forced final bypass (last_send=0.0 always passes throttle)
now_mono = time.monotonic()
broadcast_stream_update(pool=pool, instance_name=instance.instance_name,
    turn_output=final_resp, is_streaming_tick=False, tick_num=_tick_num,
    now_sec=now_mono, last_send=0.0, last_resp_len=0)

# Then push_final_state (queues another frame, no throttle)
engine.stream_publisher.push_final_state(instance, caller_name)
```

### Step 5 — Drain the Queue in a Separate Thread

```python
events = []
done = asyncio.Event()

def _consumer():
    try:
        # ... drive engine.run + broadcasts (Step 4) ...
    finally:
        loop.call_soon_threadsafe(done.set)

consumer_thread = threading.Thread(target=_consumer, daemon=True)
consumer_thread.start()

async def _drain():
    while True:
        try:
            item = send_queue.get_nowait()
        except asyncio.QueueEmpty:
            if done.is_set():
                await asyncio.sleep(0.05)
                try: item = send_queue.get_nowait()
                except asyncio.QueueEmpty: break
            else:
                await asyncio.sleep(0.01); continue
        events.append((time.monotonic(), item))  # arrival timestamp

drain_future = asyncio.run_coroutine_threadsafe(_drain(), loop)
drain_future.result(timeout=30.0)
consumer_thread.join(timeout=5.0)
loop.call_soon_threadsafe(loop.stop); loop_thread.join(timeout=2.0)
```

### Step 6 — Measure and Report

```python
stream_events = [ev for (_t, ev) in events if isinstance(ev, dict) and ev.get("type") == "stream_update"]
arrivals = [(t, ev) for t, ev in events if isinstance(ev, dict) and ev.get("type") == "stream_update"]
gaps = [arrivals[i+1][0] - arrivals[i][0] for i in range(len(arrivals)-1)]

sub_10ms = sum(1 for g in gaps if g < 0.01)
sub_100ms = sum(1 for g in gaps if g < 0.1)
last_t = arrivals[-1][0]
final_200ms = [a for a in arrivals if last_t - 0.2 <= a[0] <= last_t]

# Per-frame detail: payload bytes, is_partial, history_count, reasoning length
for t, ev in final_200ms:
    inst = (ev.get("instances") or {}).get(INSTANCE_NAME)
    print(f"t={t:.3f} is_partial={inst['is_partial']} "
          f"history_count={inst['history_count']} payload_bytes={len(json.dumps(ev))}")
```

## Tips

- **Always use `time.monotonic()`** for arrival timestamps — wall clock can jump.
- **The drain thread must run on the asyncio loop** (via `run_coroutine_threadsafe`) because `asyncio.Queue.get_nowait()` is not thread-safe from outside the loop.
- **Suppress compression** with `_generate_cfg_override = {"max_input_tokens": 1_000_000}` or long sessions will trigger mid-turn compression and pollute the stream.
- **Set `instance.max_turns = 1`** to limit execution to a single turn.
- **Use a real session log** (50+ messages) for realistic payload sizes — synthetic small sessions won't reveal size-dependent throttling behavior.
- **The `last_send=0.0` final bypass is the key burst mechanism** — it makes the throttle check `(now - 0.0 > 0.1)` always true, guaranteeing a frame at turn end regardless of when the last loop tick fired.
- **Compare against a control profile** (same token volume, different content type) to isolate whether the burst is structural or content-driven.
- **Print findings via `print()` in pytest with `-s`**, or use a standalone script for full output capture (pytest xdist workers may swallow print output).

## Common Pitfalls

| Pitfall | Fix |
|---------|-----|
| `NameError` inside consumer thread closure | Import classes at the top of the closure, not in the outer scope |
| Queue never drains (deadlock) | Ensure `done.set()` is called in a `finally` block; use `run_coroutine_threadsafe` not direct loop access |
| xdist workers swallow `-s` output | Run standalone script, or use `--dist=no` for single-process |
| Compression fires mid-turn | Set `_generate_cfg_override` with large `max_input_tokens` |
| Mock yields wrong type | Must yield `[Message(...)]` (list), not a bare Message; engine expects list of responses |
