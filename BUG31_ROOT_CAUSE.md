# Bug 31 — Root Cause Analysis: Very Slow UI Updates (Every Few Seconds)

## Executive Summary

The "every few seconds" UI update delay is caused by a **combination of two independent throttle layers** that compound during heavy execution, with an additional **token-stats cache eviction bottleneck** that can create multi-second stalls when the cache overflows.

### Throttle Stack (Front-to-Back):
| Layer | Location | Interval |
|-------|----------|----------|
| Server broadcast throttle | `run_agent_unified.py:156` / `execution_engine.py:1865` | **150ms** |
| Frontend render throttle | `web_ui/app.js:1176` | **100ms** |
| Token stats cache eviction | `api_integration.py:785-788` | **Unbounded (cache cap: 100 entries)** |

When all three layers align, effective update rate drops to ~200–500ms minimum, and during cache eviction events, updates can stall for several seconds.

---

## Detailed Findings

### 1. Server-Side Broadcast Throttle: `run_agent_unified.py` Line 156

**File:** `agent_cascade/run_agent_unified.py`, lines 154–183
```python
should_broadcast = (
    now - last_send > 0.15          # <-- THROTTLE: 150ms minimum interval
    or len_changed
    or has_tool_event
)
```

**File:** `agent_cascade/execution_engine.py`, lines 1863–1935 (sub-agent path)
```python
_sub_send_interval = 0.15  # Match main loop throttle
...
if now - _last_sub_send >= _sub_send_interval and not _stream_pushing_disabled:
```

**Impact:** The minimum interval between WebSocket broadcasts is **150ms**. During active LLM generation, this fires at ~6.7 Hz. This alone should be acceptable for near-instant UI updates. However, the expensive `build_stream_update_from_pool()` function (see Finding 2) must complete within this window each time — if it takes too long, the next tick may also miss its deadline, causing cascading delays.

---

### 2. Frontend Render Throttle: `web_ui/app.js` Line 1176

**File:** `web_ui/app.js`, lines 1174–1191
```javascript
const subThrottleContent = 100;  // <-- THROTTLE: 100ms render interval
if (stackChanged || subAgentNewVisibleMessage || subAgentContentChanged || 
    now - state.genStats.lastSubAgentRender > subThrottleContent) {
    renderSubAgents();
    state.genStats.lastSubAgentRender = now;
}
```

**Impact:** Even when the server sends updates every 150ms, the frontend only calls `renderSubAgents()` every **100ms minimum**. These two throttles are independent:

- Server sends at t=0, 150ms, 300ms, 450ms...
- Frontend renders at t=0, 100ms, 200ms, 300ms...

When the frontend receives a server update at t=150ms but its last render was at t=100ms, it must wait **50ms** for the next rendering window. This adds latency on top of network/queue processing time. The effective minimum visible update interval is therefore ~200–250ms in normal operation.

---

### 3. `build_stream_update_from_pool()` — Primary Bottleneck: `api_integration.py` Lines 405–518

This function is the **single most expensive operation** called on every server tick. It performs:

#### 3a. Iteration Over ALL Pool Instances (Line 465)
```python
instance_snapshot_data = dict(pool.instances)  # Shallow copy of all instances
for name, inst in instance_snapshot_data.items():  # Every instance processed
    with inst._compression_lock:
        current_msgs = list(inst.conversation)   # Full conversation copy per instance
    current_version = (len(current_msgs), id(current_msgs[-1]) if current_msgs else None)
```

**Cost:** O(N_instances × N_messages_per_instance). With 5 sub-agents averaging 20 messages each, that's 100 message copies per tick.

#### 3b. `slice_history_for_llm()` — Marker Scanning (Line 438, called per instance)
**File:** `agent_cascade/agent_pool.py`, lines 936–975

```python
for i in range(len(history)):                        # Full scan of conversation
    content = history[i].get('content', '') if isinstance(history[i], dict) else getattr(history[i], 'content', '')
    if isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
        marker_indices.append(i)
```

**Cost:** O(N_messages) per instance — iterates through every message looking for compression markers. During active LLM generation when no new messages are appended, this scan is wasteful but unavoidable. With a 50-message conversation, that's 50 iterations × N_instances calls per tick.

#### 3c. Token Stats via `get_history_stats()` (Line 442–444)
**File:** `agent_cascade/utils/utils.py`, lines 787–855

```python
def get_history_stats(messages: List[Union[Message, dict]]) -> dict:
    for m in messages:                                # Iterates ALL messages
        if isinstance(m, dict):
            ...
        else:
            tokens = qwen_count(content)              # EXPENSIVE tokenization per message
```

**Cost:** O(N_messages × cost_of_qwen_count). The `qwen_count` function (tokenizer call) is the most expensive operation. Even with a module-level LRU cache (`get_history_stats._msg_stats`, 512 entries), repeated calls with different messages cause cache misses and full re-tokenization.

#### 3d. Instance Serialization via `_serialize_instance()` (Lines 480, 488)
**File:** `agent_cascade/api_integration.py`, lines 727–802

```python
# Streaming optimization: only last 3 messages for conversations > 30
if streaming and inst.is_active and len(msgs) > 30:
    serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs[-3:], start_idx)]
else:
    serialized_msgs = [serialize_message(m, i) for i, m in enumerate(msgs)]  # ALL messages
```

**Cost:** O(N_messages) per instance. During full state updates (`streaming=False`), this serializes every message. The version comparison optimization (lines 467–490) only applies to incremental stream updates and skips serialization for unchanged instances — but it still runs `slice_history_for_llm` and `get_history_stats` on each tick regardless.

---

### 4. Token Stats Cache Eviction: `api_integration.py` Lines 785–788

```python
# Evict oldest entry if cache is full (cap at 100 entries to prevent memory leak)
if len(_token_stats_cache) >= 100:
    oldest_key = next(iter(_token_stats_cache))
    del _token_stats_cache[oldest_key]
_token_stats_cache[cache_key] = stats
```

**This is the critical bottleneck that causes multi-second stalls.**

The cache key is `(len(msgs), id(msgs[-1]))` — one entry per unique (message_count, last_message_id) tuple. With multiple instances each having different conversation lengths:

- 5 instances × 20 messages = potentially 100 unique cache keys just for the active generation
- When a new sub-agent is created with a long conversation, it adds fresh entries
- The 100-entry cap causes **LRU eviction of previously-cached token stats**
- On the next tick, instances whose cache was evicted must re-tokenize ALL their messages

**Example scenario causing "every few seconds" delay:**
1. Server has 5 active instances with conversations of 15–30 messages each
2. Token stats cache fills to 100 entries (one per unique message count)
3. A new sub-agent is created with 25 messages → adds fresh cache entry
4. Eviction triggers, removing cached stats for several existing instances
5. Next tick: `get_history_stats()` must re-tokenize ALL messages for evicted instances
6. Re-tokenization of 100+ messages via qwen_count takes **hundreds of milliseconds to seconds**
7. During this time, no WebSocket updates are sent (the blocking call completes before the next tick)

---

### 5. `_get_max_tokens_for_instance()` — Unnecessary Recomputation: `api_integration.py` Lines 670–676

```python
def _get_max_tokens_for_instance(pool: AgentPool, instance: AgentInstance) -> int:
    return _resolve_max_tokens(pool, instance)
```

**Called from:** `build_stream_update_from_pool()` (line 452) and `_serialize_instance()` (line 792) — **twice per tick per instance**.

**File:** `api_integration.py`, lines 584–667 — the resolution chain involves:
- API router lookup (line 619-625)
- Per-instance override check (line 627-631)
- Runtime-detected LLM limit check (line 634-642)
- Static config check (line 645-656)

**Cost:** Multiple dictionary lookups and attribute checks per call. While individually cheap, this runs **twice per instance per tick** with no caching.

---

## Root Cause Summary Table

| # | Bottleneck | File:Line | Severity | Impact |
|---|-----------|-----------|----------|--------|
| 1 | Server broadcast throttle (150ms) | `run_agent_unified.py:156` | Low | Sets minimum ~6.7 Hz update rate — acceptable alone |
| 2 | Frontend render throttle (100ms) | `web_ui/app.js:1176` | Low-Medium | Adds 50–150ms latency on top of server throttle |
| 3 | `build_stream_update_from_pool()` iterates ALL instances + messages | `api_integration.py:465-480` | **High** | O(N_instances × N_messages) per tick — the main cost driver |
| 4 | `slice_history_for_llm()` scans all messages for compression markers | `agent_pool.py:936-975` | Medium | Called per instance per tick — wasteful during active generation |
| 5 | `get_history_stats()` tokenizes every message via qwen_count | `utils.py:787-855` | **Critical** | Most expensive operation; cache misses cause multi-second stalls |
| 6 | Token stats cache capped at 100 entries, LRU eviction | `api_integration.py:785-788` | **Critical** | Cache overflow → full re-tokenization on next tick = seconds of delay |
| 7 | `_get_max_tokens_for_instance()` called twice per instance/tick with no caching | `api_integration.py:452,792` | Low-Medium | Unnecessary recomputation (5+ lookups × 2 calls × N_instances per tick) |

## Primary Root Cause

**The token stats cache eviction at `api_integration.py:785-788` is the primary cause of the "every few seconds" delay.** When the 100-entry cache overflows (which happens with multiple active instances and long conversations), all affected instances must re-tokenize their full message histories on the next tick. This re-tokenization via `qwen_count` can take several seconds, during which no WebSocket updates are sent.

The secondary cause is the **compound throttle effect**: even when token stats are cached, the 150ms server + 100ms frontend throttles create a minimum ~200-250ms visible update interval. During heavy execution with many instances, the expensive `build_stream_update_from_pool()` function may not complete within its 150ms window, causing the server to skip ticks entirely.

## Recommended Fixes (Priority Order)

1. **Increase `_token_stats_cache` cap or make it instance-scoped** (`api_integration.py:785`): The 100-entry global cap is too small for multi-instance scenarios. Consider per-instance caching or a much larger global cache (e.g., 2000 entries).

2. **Cache `_get_max_tokens_for_instance()` result per instance** (`api_integration.py:452,792`): The max tokens resolution never changes during a session — cache it once per instance.

3. **Skip `slice_history_for_llm()` during active generation** (`api_integration.py:438`): When no new messages are appended to an instance's conversation, the working set hasn't changed — reuse the previous result.

4. **Reduce frontend render throttle from 100ms to 50ms** (`web_ui/app.js:1176`): The 100ms throttle is unnecessary given the content-key change detection already prevents redundant DOM work.

5. **Add per-instance token stats cache in `AgentInstance`** (`api_instance.py`): Store token stats on the instance itself, invalidated only when messages are appended. This eliminates the need to recompute or rely on a shared global cache.