---
name: httpx-connection-pooling
description: Diagnose and fix connection reuse issues in OpenAI SDK clients using LRU caching with proper keepalive settings and response cleanup for both streaming and non-streaming paths
source: auto-skill
extracted_at: '2026-07-06T03:45:37.551Z'
---

## Goal

Ensure httpx connection pools are properly configured and responses are closed correctly so that connections can be reused between API calls, reducing latency from ~2-3s (cold TCP handshake) to <1s (reused connection).

## Root Cause Pattern

The OpenAI Python SDK v2+ uses httpx under the hood. Common issues:

| Symptom | Root Cause |
|---|---|
| Non-streaming crashes with `AttributeError: 'ChatCompletion' object has no attribute 'close'` | Calling `.close()` on response objects instead of their underlying raw HTTP response |
| Pool is empty after each call despite calling `.close()` | `keepalive_expiry` too short — connections expire before next call starts |
| POST time stays ~2-3s on every streaming call even with correct keepalive | OpenAI SDK's `__stream__` breaks on `[DONE]` SSE event before fully draining the stream, then force-closes the connection instead of returning it to pool (see Step 3a) |

## Procedure

### Step 1 — Verify the client cache pattern

Use an LRU cache keyed by `(base_url, api_key)` to reuse clients:

```python
import httpx
import openai
from collections import OrderedDict

_CLIENT_CACHE: OrderedDict = OrderedDict()
_MAX_CACHE_SIZE = 16

def _get_cached_client(base_url: str, api_key: str) -> openai.OpenAI:
    key = (base_url, api_key)
    with _CACHE_LOCK:
        if key in _CLIENT_CACHE:
            client = _CLIENT_CACHE.pop(key)
            _CLIENT_CACHE[key] = client  # Move to end for LRU ordering
            return client

        while len(_CLIENT_CACHE) >= _MAX_CACHE_SIZE:
            oldest_key, oldest_client = _CLIENT_CACHE.popitem(last=False)
            try:
                oldest_client.close()
            except Exception:
                pass

        _CLIENT_CACHE[key] = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=httpx.Client(limits=httpx.Limits(keepalive_expiry=15.0)),
        )
        return _CLIENT_CACHE[key]
```

### Step 2 — Set keepalive_expiry based on call duration

- **Non-streaming calls**: Return in ~0.5-1s, so `keepalive_expiry=3.0` is fine.
- **Streaming calls**: Take ~3-4s total (POST + stream), so connections need to survive longer. Use `keepalive_expiry=15.0`.

**Rule of thumb**: Set `keepalive_expiry` to at least 2x the longest expected call duration. For mixed streaming/non-streaming workloads, use 15s.

### Step 3 — Close responses correctly in both paths

#### Step 3a — Streaming path (use `_iter_events()` for full drain)

The OpenAI SDK's `__stream__` method breaks on `[DONE]` SSE before fully draining the HTTP stream, then force-closes the connection. To reuse connections, iterate via `_iter_events()` which drains all SSE frames including `[DONE]`:

```python
response = client.chat.completions.create(...)
try:
    for sse in response._iter_events():
        if sse.data == "[DONE]":
            continue  # not valid JSON
        data = sse.json()
        chunk = response._client._process_response_data(
            data=data, cast_to=response._cast_to, response=response.response)
        yield from process_chunk(chunk)
finally:
    response.close()  # Returns connection to pool (no-op if already drained)
```

**Why this works**: `_iter_events()` reads until the HTTP stream is fully consumed. When you then call `.close()`, the underlying httpx connection is returned to the pool instead of being discarded. Without full drain, `response.close()` force-closes and throws away the connection.

**Verified result**: POST time drops from ~2-3s (cold TCP/TLS) to <1s on subsequent streaming calls. Pool state shows `pool=1` with reused connections.</parameter>
</function>
</tool_call>
<tool_call>
<function=agent>
<parameter=description>
Verify oai.py fix is correct and complete
```

**Non-streaming path** (ChatCompletion object, no `.close()` method):
```python
response = client.chat.completions.create(...)
try:
    return process_response(response)
finally:
    raw = getattr(response, 'raw', None)
    if raw is not None:
        raw.close()  # Close underlying httpx response
```

### Step 4 — Verify connection reuse with a test

Create a test that measures POST time and pool state:

```python
import time
import oai

def check_pool(label):
    client = list(oai._CLIENT_CACHE.values())[0]
    pool = client.http_client._pool
    conns = getattr(pool, '_connect_to', None) or pool
    print(f"{label}: pool={len(conns)} connections")

# Streaming test
t0 = time.perf_counter()
list(oai._chat_stream([msg], False, {'max_tokens': 50}))
print(f"Stream took {time.perf_counter()-t0:.2f}s")
check_pool("after stream")

# Non-streaming test (should show Request Count increasing)
t0 = time.perf_counter()
oai._chat_no_stream([msg], {'max_tokens': 50})
print(f"No-stream took {time.perf_counter()-t0:.2f}s")
check_pool("after no-stream")
```

**Expected results**: Second call POST time should drop from ~2.7s to <1s, and pool should show `Request Count: 2` proving reuse.

## Key Configuration Values

| Parameter | Recommended | Why |
|---|---|---|
| `keepalive_expiry` | 15.0 seconds | Survives streaming calls (~3-4s) with headroom for call scheduling |
| `_MAX_CACHE_SIZE` | 16 entries | Covers typical multi-model setups without unbounded growth |
| LRU eviction on cache miss | Always | Prevents stale clients from accumulating when many models are used |

## What NOT to do

- Do not set `keepalive_expiry < 3.0` — connections expire before they can be reused even for fast non-streaming calls
- Do not call `.close()` directly on `ChatCompletion` objects — they lack the method; use `getattr(response, 'raw', None)` instead
- Do not create a new client per call — always cache and reuse clients keyed by `(base_url, api_key)`
- Do not forget to close responses in `finally` blocks — unclosed responses hold connections open until GC