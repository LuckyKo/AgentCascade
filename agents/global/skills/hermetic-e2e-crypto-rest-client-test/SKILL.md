---
name: hermetic-e2e-crypto-rest-client-test
description: Hermetically unit-test a REST client that does an E2E-encrypted handshake (X25519 + AES-GCM) against AC's /api/keys,/api/handshake,/api/message, with no network and no live server.
source: auto-generated
version: "1.0.0"
triggers:
  - "X25519 handshake test"
  - "AES-GCM round-trip test"
  - "httpx MockTransport crypto client"
  - "test ac_client telegram bridge"
  - "mock /api/handshake"
generated_by: coder
generated_from_task: "Implement v1 Telegram bridge for AgentCascade; hermetic tests for the X25519+AES-GCM AC REST client."
---

## Goal
Prove a client-side E2E-encrypted REST client (X25519 key exchange + AES-GCM payload) interops with the server's scheme — without a live server, network, or real credentials — using `httpx.MockTransport` and a faithful in-process server-side implementation.

## Why it bites
A crypto handshake is easy to get "plausible but wrong": the client can encrypt with a nonce/derivation the server never accepts, and only a real round-trip catches it. Mocking the transport at the HTTP layer while re-implementing the SERVER side of the exchange in-test is what makes the test meaningful (it validates interop, not just that your code runs).

## Procedure
### Step 1 — Build an in-process mock server that mirrors the real server's crypto
Implement the endpoints with the SAME primitives the real server uses. For AC (`api_server.py` L868-966): `cryptography` `x25519` + `AESGCM`. Keep a `sessions: {token: shared_secret_bytes}` dict exactly like the server's `api_sessions`.
```python
class MockServer:
    def __init__(self):
        self.server_private = x25519.X25519PrivateKey.generate()
        self.sessions = {}   # token -> shared_secret (bytes)
    def handle(self, request):  # wired to httpx.MockTransport
        if request.url.path == '/api/keys':
            return httpx.Response(200, json={'public_key': b64(self.server_private.public_key().public_bytes_raw()), 'algorithm':'X25519'})
        if request.url.path == '/api/handshake':
            pub = x25519.X25519PublicKey.from_public_bytes(b64d(json.loads(request.content)['public_key']))
            secret = self.server_private.exchange(pub)          # <-- server-side ECDH
            tok = f"tok_{len(self.sessions)}"; self.sessions[tok] = secret
            return httpx.Response(200, json={'session_token': tok})
        if request.url.path == '/api/message':
            d = json.loads(request.content); sec = self.sessions.get(d['session_token'])
            if sec is None: return httpx.Response(401, json={'message':'bad token'})
            plain = AESGCM(sec).decrypt(b64d(d['nonce']), b64d(d['payload']), None)  # <-- server decrypts
            self.injected.append(json.loads(plain))
            return httpx.Response(200, json={'status':'success','queued':True})
```
Wire it: `ACClient(base_url='http://127.0.0.1:12345', transport=httpx.MockTransport(mock.handle))`.

### Step 2 — Assert a genuine round-trip, not just "no exception"
Do the handshake via the client, then decrypt the client's ciphertext on the SERVER side and assert it equals the expected inner payload:
```python
token, secret = await client.handshake()          # client derives its own shared_secret
payload_b64, nonce_b64 = encrypt_payload('hi', 'Maine', secret)
plain = AESGCM(mock.sessions[token]).decrypt(b64d(nonce_b64), b64d(payload_b64), None)
assert json.loads(plain) == {'target':'Maine','text':'hi'}
```
Also assert `len(b64d(nonce_b64)) == 12` (GCM nonce) and that payload/nonce are valid b64.

### Step 3 — Test the re-handshake-on-401 path for real
To exercise recovery without a restart, wrap the transport so a specific token returns 401 exactly once, then assert the client re-handshakes and the retry succeeds:
```python
def flaky(req):
    if req.url.path=='/api/message' and not done['x']:
        if json.loads(req.content)['session_token']=='tok_0':
            done['x']=True; return httpx.Response(401, json={'message':'stale'})
    return mock.handle(req)
```

### Step 4 — Run serially (pytest.ini may pin xdist)
`python -m pytest tests/test_x.py -o addopts="" --timeout=60`. See `pytest-ini-addopts-xdist-serial-run` for the `-n auto` addopts override and exit-5 trap.

## Tips / gotchas (learned the hard way)
- **`AESGCM.generate_nonce()` does NOT exist** in current `cryptography` (rust binding) — use `os.urandom(12)` for the 96-bit GCM nonce. The server only ever *decrypts*; the client generates the nonce. This is the #1 silent failure.
- **Run async coroutines with a fresh loop per test**: `asyncio.new_event_loop().run_until_complete(coro)`. Avoid `asyncio.get_event_loop()` inside production code (deprecated); use `asyncio.get_running_loop()`.
- **PTB `RetryAfter(retry_after=int_or_timedelta)`** has NO `message=` kwarg — constructing it with `message=` raises TypeError. To simulate 429 in a send test, `bot.send_message.side_effect` that raises `RetryAfter(retry_after=0)` once then returns None; assert call count == 2.
- **Fire-and-forget tasks leak**: if the code under test does `asyncio.create_task(...)`, drain it inside your `run_until_complete` wrapper (`await asyncio.wait_for(task, timeout)`) or pytest prints "Task was destroyed but it is pending!".
- **Chunking boundary tests**: assert exactly-at-limit stays ONE part and limit+1 splits losslessly (`''.join(parts)==original`, every `len<=limit`).
- The mock server should track call counts (`status_calls`, `state_calls`, `injected`) so you can assert the waiter polled the expected number of times and read state exactly once.
