"""Hermetic unit tests for the v1 Telegram bridge (agent_cascade/telegram_bridge).

No network, no real Telegram bot, no running AC. The AC HTTP surface is mocked
with httpx.MockTransport; the PTB Bot is a MagicMock. Crypto uses the real
``cryptography`` library so the X25519+AES-GCM round-trip is genuinely exercised.

Run serially (pytest.ini pins xdist in addopts):
    python -m pytest tests/test_telegram_bridge.py -v -o addopts="" --timeout=60
"""

import asyncio
import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Ensure top-level imports work (mirror tests/test_api_endpoints.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.telegram_bridge.ac_client import ACClient, ACError, encrypt_payload  # noqa: E402
from agent_cascade.telegram_bridge.bot import chunk_text, send_chunked, on_message  # noqa: E402
from agent_cascade.telegram_bridge.config import BridgeConfig  # noqa: E402
from agent_cascade.telegram_bridge.waiter import (  # noqa: E402
    WaiterResult,
    extract_final_message,
    fetch_final_message,
    wait_for_completion,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine to completion on a fresh event loop."""
    return asyncio.new_event_loop().run_until_complete(coro)


class _MockACServer:
    """Implements the AC endpoints in-process for httpx.MockTransport.

    Mirrors api_server.py's crypto scheme so the bridge's client-side handshake
    is validated against a faithful server-side implementation. Tracks call counts
    and can simulate a stale token (401) on demand.
    """

    def __init__(self):
        self.server_private = x25519.X25519PrivateKey.generate()
        self.server_public_bytes = self.server_private.public_key().public_bytes_raw()
        self.sessions = {}          # token -> shared_secret (bytes)
        self.injected = []          # list of decrypted {target, text}
        self.status_calls = 0
        self.state_calls = 0
        self.fail_status_once_with_401 = False
        self._status_script = []    # queued 'generating' values to return in order

    def _server_handshake(self, client_pub_b64: str) -> bytes:
        client_public_key = x25519.X25519PublicKey.from_public_bytes(
            base64.b64decode(client_pub_b64))
        return self.server_private.exchange(client_public_key)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == '/api/keys':
            return httpx.Response(200, json={
                'public_key': base64.b64encode(self.server_public_bytes).decode('utf-8'),
                'algorithm': 'X25519',
            })

        if path == '/api/handshake':
            data = json.loads(request.content)
            client_pub = data.get('public_key')
            if not client_pub:
                return httpx.Response(400, json={'message': 'Missing public_key'})
            shared_secret = self._server_handshake(client_pub)
            token = f"tok_{len(self.sessions)}"
            self.sessions[token] = shared_secret
            return httpx.Response(200, json={'session_token': token})

        if path == '/api/message':
            data = json.loads(request.content)
            token = data.get('session_token')
            secret = self.sessions.get(token)
            if secret is None:
                return httpx.Response(401, json={'message': 'Invalid or expired session token'})
            try:
                aesgcm = AESGCM(secret)
                plain = aesgcm.decrypt(
                    base64.b64decode(data['nonce']),
                    base64.b64decode(data['payload']),
                    None,
                )
                payload = json.loads(plain.decode('utf-8'))
            except Exception as e:  # noqa: BLE001 - surface as AC's 400
                return httpx.Response(400, json={'message': f"Decryption failed: {e}"})
            self.injected.append(payload)
            return httpx.Response(200, json={
                'status': 'success', 'queued': True, 'target': payload.get('target')})

        if path == '/api/status':
            token = request.url.params.get('token')
            self.status_calls += 1
            if self.fail_status_once_with_401:
                self.fail_status_once_with_401 = False
                return httpx.Response(401, json={'message': 'Invalid session token'})
            if token not in self.sessions:
                return httpx.Response(401, json={'message': 'Invalid session token'})
            generating = (self._status_script.pop(0)
                          if self._status_script else False)
            return httpx.Response(200, json={
                'generating': generating, 'active_agent': 'Maine', 'agents': [],
                'active_stack': [], 'instance_halted': False})

        if path == '/api/state':
            self.state_calls += 1
            return httpx.Response(200, json=self._state_payload())

        return httpx.Response(404, json={'message': f'no route {path}'})

    def _state_payload(self):
        return {
            'agents': [],
            'messages': [
                {'role': 'user', 'content': 'do the thing'},
                {'role': 'assistant', 'content': 'thinking…', 'function_call': {'name': 'x'}},
                {'role': 'tool', 'content': 'ok'},
                {'role': 'assistant', 'content': self.final_text},
            ],
            'agent_instances': {},
            'active_stack': [],
            'generating': False,
            'session_name': 'Maine',
        }

    @property
    def final_text(self):
        return getattr(self, '_final_text', 'The final answer.')


def _make_client(mock: _MockACServer) -> ACClient:
    transport = httpx.MockTransport(mock.handle)
    return ACClient(base_url='http://127.0.0.1:12345', target_agent='Maine',
                    transport=transport)


# ---------------------------------------------------------------------------
# 1. Crypto round-trip (X25519 + AES-GCM) against a mocked AC server
# ---------------------------------------------------------------------------

def test_crypto_round_trip_handshake_and_encrypt():
    """Ephemeral X25519 keypair + mocked /api/keys & /api/handshake; the bridge's
    encrypt_payload output must decrypt (server-side) to {target, text}."""
    mock = _MockACServer()
    client = _make_client(mock)

    async def go():
        await client.open()
        token, secret = await client.handshake()
        assert token in mock.sessions  # server accepted our public key
        payload_b64, nonce_b64 = encrypt_payload('hello world', 'Maine', secret)
        # Server-side decrypt (mirrors api_server.py /api/message):
        aesgcm = AESGCM(mock.sessions[token])
        plain = aesgcm.decrypt(base64.b64decode(nonce_b64),
                               base64.b64decode(payload_b64), None)
        return json.loads(plain.decode('utf-8'))

    result = _run(go())
    assert result == {'target': 'Maine', 'text': 'hello world'}


def test_inject_message_payload_shape_and_full_round_trip():
    """inject_message sends {session_token, payload, nonce} and the server decrypts it."""
    mock = _MockACServer()
    client = _make_client(mock)

    async def go():
        await client.open()
        resp = await client.inject_message('run the build', target='Maine')
        await client.close()
        return resp

    resp = _run(go())
    assert resp['status'] == 'success' and resp['queued'] is True
    # Exactly one decrypted payload reached the (mock) server:
    assert mock.injected == [{'target': 'Maine', 'text': 'run the build'}]


def test_encrypt_payload_nonce_is_12_bytes_and_b64():
    secret = AESGCM.generate_key(256)  # 32-byte key, like a real shared secret
    payload_b64, nonce_b64 = encrypt_payload('x', 'Maine', secret)
    assert len(base64.b64decode(nonce_b64)) == 12
    base64.b64decode(payload_b64)  # must be valid b64


# ---------------------------------------------------------------------------
# 2. Waiter: poll status until generating==false, then read last assistant msg
# ---------------------------------------------------------------------------

def test_waiter_returns_last_assistant_message():
    mock = _MockACServer()
    mock._status_script = [True, True, False]   # generating true twice, then false
    client = _make_client(mock)

    async def go():
        await client.open()
        result = await wait_for_completion(client, poll_interval=0.01, timeout=5)
        assert result.status == WaiterResult.FINISHED
        text = await fetch_final_message(client)
        await client.close()
        return text

    text = _run(go())
    assert text == 'The final answer.'
    assert mock.status_calls >= 3
    assert mock.state_calls == 1


def test_waiter_timeout_returns_false():
    mock = _MockACServer()
    mock._status_script = [True] * 1000   # always generating (reachable, just busy)
    client = _make_client(mock)

    async def go():
        await client.open()
        result = await wait_for_completion(client, poll_interval=0.02, timeout=0.15,
                                           offline_after=999)   # keep it a pure timeout
        await client.close()
        return result.status

    assert _run(go()) == WaiterResult.TIMEOUT


def test_waiter_detects_offline_ac():
    """When AC is unreachable (connection error), the waiter reports OFFLINE."""
    mock = _MockACServer()

    def offline_handle(request: httpx.Request) -> httpx.Response:
        # /api/keys & /api/handshake work, but /api/status always fails to connect.
        if request.url.path == '/api/status':
            raise httpx.ConnectError('connection refused')
        return mock.handle(request)

    transport = httpx.MockTransport(offline_handle)
    client = ACClient(base_url='http://127.0.0.1:12345', target_agent='Maine',
                      transport=transport)

    async def go():
        await client.open()
        result = await wait_for_completion(client, poll_interval=0.02, timeout=5,
                                           offline_after=0.1)
        await client.close()
        return result.status

    assert _run(go()) == WaiterResult.OFFLINE


def test_extract_final_message_handles_multimodal_list_content():
    state = {'messages': [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'A'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'B'}, 'C']},
    ]}
    assert extract_final_message(state) == 'BC'


def test_extract_final_message_no_assistant_returns_empty():
    assert extract_final_message({'messages': [{'role': 'user', 'content': 'x'}]}) == ''
    assert extract_final_message({}) == ''


# ---------------------------------------------------------------------------
# 3. Chunking: >4096-char reply split correctly, all parts <= limit, lossless
# ---------------------------------------------------------------------------

def test_chunk_text_lossless_and_within_limit():
    # A long multi-paragraph text well over the hard limit.
    paragraph = 'Line one of a paragraph.\nSecond line here.\n' * 3
    text = (paragraph + '\n') * 40   # ~ several thousand chars
    assert len(text) > 4096
    parts = chunk_text(text, limit=4096)
    assert all(len(p) <= 4096 for p in parts)
    assert ''.join(parts) == text


def test_chunk_text_exact_boundaries():
    assert chunk_text('', limit=4096) == []
    short = 'a' * 4096
    assert chunk_text(short, limit=4096) == [short]
    over = 'a' * 4097   # single line longer than limit -> hard split
    parts = chunk_text(over, limit=4096)
    assert all(len(p) <= 4096 for p in parts)
    assert ''.join(parts) == over


def test_send_chunked_sends_all_parts_in_order():
    bot = MagicMock()
    # Make send_message a no-op coroutine.
    async def _noop(**kwargs):
        return None
    bot.send_message.side_effect = lambda **kw: _noop()

    text = ('word ' * 1000).strip()   # ~5000 chars, single logical line with spaces
    assert len(text) > 4096
    _run(send_chunked(bot, chat_id=42, text=text))

    sent = [c.kwargs['text'] for c in bot.send_message.call_args_list]
    assert all(len(p) <= 4096 for p in sent)
    assert ''.join(sent) == text
    # Every call targeted the right chat.
    assert all(c.kwargs.get('chat_id') == 42 for c in bot.send_message.call_args_list)


# ---------------------------------------------------------------------------
# 4. Auth gate: non-allowlisted user -> no AC call, no reply
# ---------------------------------------------------------------------------

def _make_update(user_id, text='hello', chat_id=100):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    return update


def test_auth_gate_rejects_non_allowlisted_user():
    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[<YOUR_TELEGRAM_USER_ID>])
    ac = MagicMock()
    ac.inject_message = MagicMock(side_effect=AssertionError('AC must not be called'))
    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    context.bot.send_message = MagicMock()

    update = _make_update(user_id=1234567890)   # NOT in allowlist
    _run(on_message(update, context))

    ac.inject_message.assert_not_called()
    context.bot.send_message.assert_not_called()


def test_auth_gate_allows_listed_user_and_injects():
    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[<YOUR_TELEGRAM_USER_ID>])
    ac = MagicMock()

    async def _inject(text, target=None):
        return {'status': 'success', 'queued': True, 'target': target or 'Maine'}
    ac.inject_message.side_effect = _inject
    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    # Prevent the spawned waiter from actually running against a mock AC.
    context.bot.send_message = MagicMock(side_effect=lambda **kw: asyncio.sleep(0))

    async def go():
        await on_message(_make_update(user_id=<YOUR_TELEGRAM_USER_ID>, text='do it'), context)
        # Drain the fire-and-forget waiter task so no pending task leaks out of the loop.
        for t in list(context.bot_data.get('waiters', ()) or ()):
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    _run(go())
    ac.inject_message.assert_called_once()


def test_auth_gate_ignores_empty_text():
    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[<YOUR_TELEGRAM_USER_ID>])
    ac = MagicMock()
    ac.inject_message = MagicMock(side_effect=AssertionError('must not inject empty'))
    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    context.bot.send_message = MagicMock()

    _run(on_message(_make_update(<YOUR_TELEGRAM_USER_ID>, text='   '), context))
    ac.inject_message.assert_not_called()


# ---------------------------------------------------------------------------
# 5. 429 handling and task-timeout path
# ---------------------------------------------------------------------------

def test_send_one_honors_retry_after_on_429():
    from telegram.error import RetryAfter

    bot = MagicMock()
    call_count = {'n': 0}

    async def _flaky(**kwargs):
        call_count['n'] += 1
        if call_count['n'] == 1:
            # Simulate Telegram's 429 with a (short) retry_after.
            raise RetryAfter(retry_after=0)   # PTB accepts int seconds or timedelta
        return None

    bot.send_message.side_effect = _flaky
    _run(send_chunked(bot, chat_id=7, text='hi'))

    assert call_count['n'] == 2   # failed once, then succeeded
    assert bot.send_message.call_args_list[0].kwargs.get('chat_id') == 7


def test_retry_after_seconds_handles_int_float_and_timedelta():
    """_retry_after_seconds must accept int, float, and timedelta forms of retry_after."""
    import datetime as _dt
    from agent_cascade.telegram_bridge.bot import _retry_after_seconds

    class _Exc:
        pass

    # int form (current PTB)
    e_int = _Exc(); e_int.retry_after = 5
    assert _retry_after_seconds(e_int, fallback=1.0) == 5.0

    # float form
    e_float = _Exc(); e_float.retry_after = 2.5
    assert _retry_after_seconds(e_float, fallback=1.0) == 2.5

    # timedelta form (upcoming PTB major)
    e_td = _Exc(); e_td.retry_after = _dt.timedelta(seconds=7, milliseconds=500)
    assert _retry_after_seconds(e_td, fallback=1.0) == pytest.approx(7.5)

    # missing attribute -> fallback
    e_none = _Exc()
    assert _retry_after_seconds(e_none, fallback=3.0) == 3.0

    # unparseable garbage -> fallback (never raises)
    e_bad = _Exc(); e_bad.retry_after = object()
    assert _retry_after_seconds(e_bad, fallback=4.0) == 4.0


def test_waiter_task_timeout_path_sends_timeout_notice():
    """When wait_for_completion times out, the waiter sends the timeout notice."""
    from agent_cascade.telegram_bridge.bot import _run_waiter

    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[1],
                       poll_interval_sec=0.02, task_timeout_sec=0.1)
    mock = _MockACServer()
    mock._status_script = [True] * 1000   # never finishes -> timeout
    client = _make_client(mock)

    bot = MagicMock()
    sent_texts = []

    async def _capture(**kwargs):
        sent_texts.append(kwargs['text'])
        return None
    bot.send_message.side_effect = _capture

    async def go():
        await client.open()
        await _run_waiter(client, bot, chat_id=99, cfg=cfg)
        await client.close()

    _run(go())
    assert any('Timed out' in t for t in sent_texts)


# ---------------------------------------------------------------------------
# 6. Re-handshake on 401 (token cache invalidation)
# ---------------------------------------------------------------------------

def test_inject_re_handshakes_on_401_then_succeeds():
    """First /api/message returns 401 (stale token); client re-handshakes once and retries."""
    mock = _MockACServer()
    # Make the server reject a specific token exactly once, then accept it again —
    # this simulates an AC restart that invalidates our cached session token.
    rejected_once = {'done': False}

    original_handle = mock.handle

    def flaky_handle(request: httpx.Request) -> httpx.Response:
        if (request.url.path == '/api/message' and not rejected_once['done']):
            data = json.loads(request.content)
            if data.get('session_token') == 'tok_0':
                rejected_once['done'] = True
                return httpx.Response(401, json={'message': 'Invalid or expired session token'})
        return original_handle(request)

    transport = httpx.MockTransport(flaky_handle)
    client = ACClient(base_url='http://127.0.0.1:12345', target_agent='Maine',
                      transport=transport)

    async def go():
        await client.open()
        resp = await client.inject_message('after restart', target='Maine')
        await client.close()
        return resp, rejected_once['done']

    resp, did_reject = _run(go())
    assert did_reject is True                 # the 401 path was actually exercised
    assert resp['status'] == 'success'        # ...and the retry after re-handshake worked
    assert mock.injected == [{'target': 'Maine', 'text': 'after restart'}]
