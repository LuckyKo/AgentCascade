# Second Opinion: SSE Streaming Burst Bug

## Verdict: Your investigation is excellent — and the answer is hiding in the data you already have.

The report is thorough and methodologically sound. The five PROVEN facts in §5 are solid. But I believe you can **skip the proposed "decisive next experiment"** because the existing `[DIAG-GEN-SUMMARY]` data already resolves the A-vs-B question, and the root cause is a **third option the report didn't consider**.

---

## The Smoking Gun You Already Have

From the live proxy logs:

```
[DIAG-GEN-SUMMARY] total_chunks=2500 stream_wall_ms=134807.8   (134 seconds)
[DIAG-GEN-SUMMARY] total_chunks=853  stream_wall_ms=42147.7    (42 seconds)
[DIAG-GEN-SUMMARY] total_chunks=625  stream_wall_ms=34806.8    (35 seconds)
```

And from the client repro:

```
chunks=2712  stream=11.3ms  → BURST
chunks=1955  stream=8.5ms   → BURST
```

**The generator yields chunks over 30-134 seconds of wall time, yet the client receives everything in ~10ms.** This eliminates hypothesis (A) (event-loop stalls) — if the loop were stalling, the generator wouldn't be able to yield incrementally over real time either. The loop is running fine.

But this also eliminates hypothesis (B) as stated (upstream-as-seen-by-proxy already coalesced) — because the DIAG-GEN-SUMMARY proves `aiter_bytes()` IS delivering chunks incrementally to the generator.

**So what's left?** The generator yields in real time. Starlette calls `await send()` in real time. Uvicorn calls `self.transport.write()` in real time. Yet the client receives nothing until the end. This points to a problem **downstream of `transport.write()`** — inside the OS TCP stack or at the asyncio transport layer.

---

## Root Cause: `transport.write()` is non-blocking and does NOT flush

Here's the critical chain I traced through the source:

### Starlette's `stream_response`:
```python
async for chunk in self.body_iterator:
    await send({"type": "http.response.body", "body": chunk, "more_body": True})
```

### Uvicorn's `RequestResponseCycle.send` (body path):
```python
if self.chunked_encoding:
    content = [b"%x\r\n" % len(body), body, b"\r\n"]
    self.transport.write(b"".join(content))
```

### The key: `self.transport.write()` on asyncio

`asyncio.Transport.write()` is **non-blocking**. It copies data into an internal write buffer. The actual socket `send()` happens later, driven by the event loop's writable-socket callback. The write buffer has a **high-water mark** (default 64KB in asyncio, uvicorn's `FlowControl` also sets `HIGH_WATER_LIMIT = 65536`).

Each SSE chunk is tiny (~100-300 bytes for a typical token). So for a stream of 2500 chunks × ~200 bytes = ~500KB, the transport buffers up many chunks before the write buffer hits the high-water mark and triggers `pause_writing()`.

**But here's the critical part:** even when below the high-water mark, `transport.write()` *should* still eventually flush to the socket via the event loop's `_sock_sendall`/writable callback on the *next* event loop iteration. The `await asyncio.sleep(0)` you added should have forced that. **So why doesn't it work?**

---

## The Real Culprit: **Nagle's Algorithm + TCP Delayed ACK on Windows Loopback**

This is the piece the investigation missed. On Windows:

1. **Nagle's algorithm** (enabled by default on TCP sockets) causes the sender to hold small writes in the kernel send buffer, waiting either for an ACK from the receiver or for enough data to fill an MSS (~64KB on loopback).

2. **TCP Delayed ACK** (enabled by default) causes the *receiver* (your client) to delay sending ACKs for up to ~200ms, waiting to piggyback the ACK on response data.

3. Together they create the classic **Nagle + Delayed-ACK deadlock**: the sender won't send because it's waiting for an ACK; the receiver won't ACK because it's waiting to piggyback. The data eventually flushes only when enough accumulates or a timeout fires.

**On Windows loopback specifically**, this behaves differently than on Linux. Windows loopback is implemented through a kernel driver (AFD.sys) that can aggressively coalesce small writes — even more so than real network interfaces. The result: many small `transport.write()` calls get merged in the kernel's send buffer, and the receiver sees them all arrive as one big batch.

### Why the harness passes but live fails:

Your mock upstream emits chunks with `~110ms + 2×1ms` cadence. At ~110ms cadence, the kernel has time to flush each write before the next one arrives. In the live case with the APEX model, the model's actual token cadence may create patterns where many chunks arrive within a small window (e.g., the MTP groups), AND there's more concurrent I/O on the live process's socket. The kernel write buffer fills with many pending chunks that all get flushed together.

**The 5/5 consistency** supports this: it's a deterministic TCP stack behavior, not random EDR noise.

---

## Answers to Your §10 Questions

### Q1: What structural difference between harness and live was NOT replicated?

> [!IMPORTANT]
> **Socket options.** Your harness creates its own `httpx.AsyncClient` which creates fresh TCP connections. The live proxy's `self.client` at [line 290](file:///N:/work/stuff/Beta/llama-autoloader/server.py#L290) also creates connections — but the **downstream** connection (uvicorn → client) is created by uvicorn, and may have different socket options. More importantly, the **client** side differs: your harness client and your live client (AgentCascade) have different read patterns.
>
> But most critically: the **kernel TCP state** differs. Live has a long-lived connection that has been through multiple requests (HTTP keep-alive), with a warm Nagle/ACK state. Your harness uses fresh connections.

### Q2: Known uvicorn/httptools/httpx behavior coalescing SSE frames?

Yes — this is well-documented: asyncio's `transport.write()` is buffered. uvicorn does **not** call `transport.get_write_buffer_size()` or force a drain after each chunk. The `FlowControl.drain()` only fires after `pause_writing()` is triggered (at 64KB), and even then it just waits for `resume_writing()` — it doesn't force a TCP push.

The fix is to **disable Nagle** on the outbound socket, which causes each `transport.write()` to translate to an immediate TCP push with the `PSH` flag.

### Q3: Architectural mitigation?

**`TCP_NODELAY`** on the server socket. This is the correct and standard fix for SSE/streaming proxies. It costs slightly more kernel transitions but ensures each write is immediately pushed to the wire.

### Q4: Measuring per-recv byte arrival?

You can use `Wireshark` or `pktmon` (built into Windows 10+) on the loopback interface to capture TCP frames between ports 1234 and 9005/9007. Filter by `tcp.port == 1234` and look at frame sizes and timestamps. This will definitively show whether the kernel is coalescing.

### Q5: Synchronous httpx client buffering?

The `recv_calls=1` result from the raw-socket probe **confirms** the data arrives at the client as one TCP segment. This is consistent with the Nagle coalescing theory — the kernel batched it. The sync httpx client is not the issue.

---

## Recommended Fix

### Option 1: Disable Nagle on uvicorn's server socket (Correct fix)

Set `TCP_NODELAY` on uvicorn's listening socket. Uvicorn doesn't expose this directly, but you can do it via a server event handler:

```python
import socket

@app.on_event("startup")
async def set_tcp_nodelay():
    # uvicorn's server socket - get it from the running server
    pass  # see Option 2 for a more practical approach
```

**More practically**, pass it via uvicorn config. As of uvicorn 0.34+, you can use a custom `Server` class or monkey-patch the transport after `connection_made`:

```python
# In server.py, wrap the ASGI app to set TCP_NODELAY on each new connection
import socket

class NodelayMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # The transport is accessible through scope's server extension
            # but that's implementation-dependent. Safer approach below.
            pass
        return await self.app(scope, receive, send)
```

### Option 2: Monkey-patch uvicorn's protocol (Most reliable)

```python
# Add to server.py before uvicorn.run()
import uvicorn.protocols.http.httptools_impl as _hti

_orig_connection_made = _hti.HttpToolsProtocol.connection_made

def _patched_connection_made(self, transport):
    _orig_connection_made(self, transport)
    sock = transport.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

_hti.HttpToolsProtocol.connection_made = _patched_connection_made
```

This ensures every accepted connection has Nagle disabled, so each `transport.write()` (one per SSE chunk) gets pushed immediately.

### Option 3: Quick validation test

Before patching the production code, validate the theory with a one-liner in the repro script — create a raw TCP connection to the proxy and set `TCP_NODELAY` on the client side, then check if the burst persists. But actually, Nagle needs to be disabled on the **sender** (server) side to fix this. So:

```python
# Quick test: add this to server.py right before uvicorn.run()
import socket as _socket
_orig_socket = _socket.socket

class _NodelaySocket(_orig_socket):
    def accept(self, *args, **kwargs):
        conn, addr = super().accept(*args, **kwargs)
        try:
            conn.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        except Exception:
            pass
        return conn, addr

_socket.socket = _NodelaySocket
```

> [!WARNING]
> The monkey-patch approach (Option 2) is the cleanest. Option 3 is a hack for validation only — don't ship it.

---

## Why `asyncio.sleep(0)` Didn't Work

The report notes that adding `await asyncio.sleep(0)` after each yield was ineffective. This makes perfect sense under the Nagle theory:

1. `yield chunk` → Starlette calls `send()` → uvicorn calls `transport.write(chunk_bytes)` → data goes into the **kernel send buffer**
2. `await asyncio.sleep(0)` → yields to the event loop → the event loop processes pending callbacks
3. But `transport.write()` already returned immediately. The data is in the kernel buffer. The event loop has no pending "flush" callback — the kernel's TCP stack decides when to actually send based on Nagle/ACK timing.

`sleep(0)` yields control to the event loop, but the event loop has nothing to do with flushing the kernel's TCP send buffer. Only `TCP_NODELAY` or an explicit `setsockopt(TCP_CORK)` toggle can force that.

---

## Summary

| Aspect | Assessment |
|--------|-----------|
| Investigation quality | Excellent — methodical, well-evidenced |
| Hypothesis A (loop stalls) | **Ruled out by your own DIAG-GEN-SUMMARY data** |
| Hypothesis B (upstream coalesced) | **Ruled out by DIAG-GEN-SUMMARY showing incremental yields** |
| Actual root cause | **TCP Nagle + Delayed ACK on Windows loopback** — `transport.write()` buffers are coalesced in the kernel |
| Fix | **`TCP_NODELAY` on uvicorn's server socket** via protocol monkey-patch |
| Expected result | Each SSE chunk pushed immediately; client sees incremental streaming |
| Risk | Minimal — `TCP_NODELAY` is standard for SSE/WebSocket/streaming servers |
