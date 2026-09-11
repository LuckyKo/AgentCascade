# Force Full Interval Policy — Implementation Summary

## Overview
Changed periodic full-frame pushes from every ~10s (tick-based) to every 60s (time-based), added a frontend `request_state` WS message type for on-demand refresh, and added rate-limited queue-full warnings.

## Files Changed

### 1. `agent_cascade/api_integration_pkg/streaming.py`
| Lines | Change |
|-------|--------|
| 8 | Added `import logging` |
| 12 | Added `Dict` to typing imports |
| 20-33 | New module-level `_last_force_full: Dict[str, float]`, `_last_force_full_lock`, `_qf_last_warn`, `_qf_drop_count`, `_qf_lock` |
| 256-288 | Updated `_put_stream_update`: added rate-limited warning (max once per 5s) when queue is full, protected by `_qf_lock` |
| 309 | Updated docstring: "Force full state serialization every ~60s (time-based, per-instance)" |
| 324-325 | Updated `tick_num` param doc: "kept for backward compat; no longer used for force_full scheduling" |
| 369-378 | Replaced `force_full = (tick_num % 100 == 0)` with time-based check using `_last_force_full` dict + lock |

### 2. `agent_cascade/ws_handlers.py`
| Lines | Change |
|-------|--------|
| 97 | Added `'request_state': self.handle_request_state` to dispatch table |
| 116-148 | New `handle_request_state` method: builds force_full frame via `build_stream_update_from_pool`, pushes via send queue, updates `_last_force_full` timer on success, logs at warning level on failure |

### 3. `web_ui/app.js`
| Lines | Change |
|-------|--------|
| 2064-2065 | Added `existing._resyncRequestedAt = null` when resync completes (timer cleanup) |
| 2084-2085 | Added `_maybeRequestState(name, existing)` call on "server ahead" resync detection |
| 2091-2092 | Added `_maybeRequestState(name, existing)` call on index mismatch resync detection |
| 2117-2120 | Updated comment: self-heal via 60s periodic + request_state mechanism |
| 4397-4403 | Added `ws.send({type: 'request_state', instance: name})` in `switchMainTab` |
| 4405-4418 | New `_maybeRequestState(instanceName, agentState)` helper function (5s timeout logic) |

## How Each Piece Connects

```
┌─────────────────────────────────────────────────────────────────────┐
│ PERIODIC (60s)                                                      │
│                                                                     │
│  broadcast_stream_update()                                          │
│    └─ checks _last_force_full[instance_name] >= 60s ago            │
│    └─ if yes: force_full=True → build full frame → queue           │
│    └─ updates _last_force_full[instance_name] = now                │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ ON-DEMAND (request_state)                                           │
│                                                                     │
│  Frontend triggers:                                                 │
│    • Tab switch → ws.send({type:'request_state', instance:name})   │
│    • Resync stuck >5s → _maybeRequestState() sends same            │
│                                                                     │
│  Backend:                                                           │
│    ws_handlers.handle_request_state()                               │
│      └─ build_stream_update_from_pool(force_full=True)             │
│      └─ push to send_queue                                         │
│      └─ reset _last_force_full timer (60s countdown restarts)      │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ QUEUE-FULL WARNING                                                  │
│                                                                     │
│  _put_stream_update()                                               │
│    └─ on QueueFull: increment counter                              │
│    └─ if ≥5s since last warn: log warning with drop count          │
│    └─ rate-limited to max 1 warning per 5s                         │
└─────────────────────────────────────────────────────────────────────┘
```

## Risks & Edge Cases

1. **First call always forces full**: `_last_force_full.get(instance_name, 0.0)` returns 0.0 for new instances, so the first broadcast is always force_full. This is correct behavior (establishes baseline).

2. **Queue-full during request_state**: If the send queue is full when `handle_request_state` pushes, the event is silently dropped but the timer is still reset. The frontend's `_maybeRequestState` will retry after 5s if resync is still stuck. Acceptable trade-off — avoids forcing another full snapshot during congestion.

3. **Thread safety**: Both `_last_force_full` and `_qf_*` globals are protected by `threading.Lock`. No nested locking, no deadlock risk (locks are held for <1μs).

4. **Memory growth of `_last_force_full`**: One entry per instance name. In practice bounded by number of concurrent agent instances (<20 typically). Could add eviction but not worth the complexity.

5. **Frontend `ws` variable scope**: The `switchMainTab` and `_maybeRequestState` functions reference the module-level `let ws = null`. This is safe — both check `ws && ws.readyState === WebSocket.OPEN` before sending.

6. **Backward compat**: `tick_num` parameter remains in `broadcast_stream_update` signature but is no longer used for force_full. No caller changes needed.
