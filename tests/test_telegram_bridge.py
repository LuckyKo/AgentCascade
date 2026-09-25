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
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Ensure top-level imports work (mirror tests/test_api_endpoints.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.telegram_bridge.ac_client import ACClient, ACError, encrypt_payload  # noqa: E402
from agent_cascade.telegram_bridge.bot import chunk_text, send_chunked, on_message  # noqa: E402
from agent_cascade.telegram_bridge.commands import (  # noqa: E402
    COMMANDS,
    parse_command,
)
from agent_cascade.telegram_bridge.config import BridgeConfig  # noqa: E402
from agent_cascade.telegram_bridge.waiter import (  # noqa: E402
    WaiterResult,
    extract_final_message,
    fetch_final_message,
    wait_for_completion,
)

# Deliberately fake Telegram user IDs for tests — never a real identifier, so
# no personal data is ever committed.
FAKE_ALLOWED_USER_ID = 1111111111
FAKE_STRANGER_USER_ID = 9999999999


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
        self.command_calls = []     # paths of command endpoints hit (token-auth)
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

        # Command endpoints read the token from the QUERY string (mirrors the real
        # api_server.py signatures `token: str = None`). A body-only token must 401.
        if path in ('/api/stop', '/api/restart', '/api/auto_security',
                    '/api/afk', '/api/session/restore'):
            self.command_calls.append(path)
            token = request.url.params.get('token')
            if not token or token not in self.sessions:
                return httpx.Response(401, json={'message': 'Invalid session token'})
            return httpx.Response(200, json={'status': 'ok', 'path': path})

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
    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[FAKE_ALLOWED_USER_ID])
    ac = MagicMock()
    ac.inject_message = MagicMock(side_effect=AssertionError('AC must not be called'))
    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    context.bot.send_message = MagicMock()

    update = _make_update(user_id=FAKE_STRANGER_USER_ID)   # NOT in allowlist
    _run(on_message(update, context))

    ac.inject_message.assert_not_called()
    context.bot.send_message.assert_not_called()


def test_auth_gate_allows_listed_user_and_injects():
    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[FAKE_ALLOWED_USER_ID])
    ac = MagicMock()

    async def _inject(text, target=None):
        return {'status': 'success', 'queued': True, 'target': target or 'Maine'}
    ac.inject_message.side_effect = _inject
    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    # Prevent the spawned waiter from actually running against a mock AC.
    context.bot.send_message = MagicMock(side_effect=lambda **kw: asyncio.sleep(0))

    async def go():
        await on_message(_make_update(user_id=FAKE_ALLOWED_USER_ID, text='do it'), context)
        # Drain the fire-and-forget waiter task so no pending task leaks out of the loop.
        for t in list(context.bot_data.get('waiters', ()) or ()):
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    _run(go())
    ac.inject_message.assert_called_once()


def test_auth_gate_ignores_empty_text():
    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[FAKE_ALLOWED_USER_ID])
    ac = MagicMock()
    ac.inject_message = MagicMock(side_effect=AssertionError('must not inject empty'))
    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    context.bot.send_message = MagicMock()

    _run(on_message(_make_update(FAKE_ALLOWED_USER_ID, text='   '), context))
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


# ---------------------------------------------------------------------------
# 7. run_bridge launcher (env-var handling)
# ---------------------------------------------------------------------------

def test_run_bridge_apply_env_sets_unset_vars_and_enables(monkeypatch):
    """apply_env fills unset vars from CLI args and force-enables the bridge."""
    from types import SimpleNamespace
    from agent_cascade.telegram_bridge.run_bridge import apply_env

    # Start from a clean slate for the relevant keys.
    for k in ('AC_BASE_URL', 'ALLOWED_USERS', 'TG_TARGET_AGENT',
              'TG_POLL_INTERVAL_SEC', 'TG_TASK_TIMEOUT_SEC', 'TG_BRIDGE_ENABLED'):
        monkeypatch.delenv(k, raising=False)

    args = SimpleNamespace(
        base_url='http://127.0.0.1:8126', allowed_users=str(FAKE_ALLOWED_USER_ID),
        target_agent=None, poll_interval_sec='2.0', task_timeout_sec=None,
    )
    apply_env(args)

    import os as _os
    assert _os.environ['AC_BASE_URL'] == 'http://127.0.0.1:8126'
    assert _os.environ['ALLOWED_USERS'] == str(FAKE_ALLOWED_USER_ID)
    assert _os.environ['TG_POLL_INTERVAL_SEC'] == '2.0'
    assert _os.environ['TG_BRIDGE_ENABLED'] == 'true'
    # None-valued args must NOT create env vars.
    assert 'TG_TARGET_AGENT' not in _os.environ
    assert 'TG_TASK_TIMEOUT_SEC' not in _os.environ


def test_run_bridge_apply_env_existing_env_wins(monkeypatch):
    """A pre-set env var is never overwritten by a CLI arg (env-wins)."""
    from types import SimpleNamespace
    from agent_cascade.telegram_bridge.run_bridge import apply_env

    monkeypatch.setenv('AC_BASE_URL', 'http://127.0.0.1:9999')
    monkeypatch.setenv('TG_BRIDGE_ENABLED', 'false')  # explicit off must be respected

    args = SimpleNamespace(
        base_url='http://127.0.0.1:8126', allowed_users='1',
        target_agent=None, poll_interval_sec=None, task_timeout_sec=None,
    )
    apply_env(args)

    import os as _os
    assert _os.environ['AC_BASE_URL'] == 'http://127.0.0.1:9999'   # env wins over CLI
    assert _os.environ['TG_BRIDGE_ENABLED'] == 'false'             # explicit off not clobbered


def test_run_bridge_apply_env_respects_explicit_empty_string(monkeypatch):
    """An explicitly-set empty-string env var is still 'set', so a CLI arg must not clobber it."""
    from types import SimpleNamespace
    from agent_cascade.telegram_bridge.run_bridge import apply_env

    # Explicitly set (to the empty string) — this is a deliberate, if broken, value.
    monkeypatch.setenv('AC_BASE_URL', '')
    monkeypatch.setenv('TG_BRIDGE_ENABLED', '')

    args = SimpleNamespace(
        base_url='http://127.0.0.1:8126', allowed_users=None,
        target_agent=None, poll_interval_sec=None, task_timeout_sec=None,
    )
    apply_env(args)

    import os as _os
    # Empty string is an existing value -> must NOT be overwritten by the CLI arg.
    assert _os.environ['AC_BASE_URL'] == ''
    # Explicitly-set (even empty) TG_BRIDGE_ENABLED must not be force-enabled.
    assert _os.environ['TG_BRIDGE_ENABLED'] == ''


def test_run_bridge_parser_defaults_and_flags():
    """The parser exposes all documented flags with correct dest names."""
    from agent_cascade.telegram_bridge.run_bridge import _build_parser

    p = _build_parser()
    args = p.parse_args(['--base-url', 'http://x:1', '--allowed-users', '42'])
    assert args.base_url == 'http://x:1'
    assert args.allowed_users == '42'
    assert args.target_agent is None
    assert args.poll_interval_sec is None
    assert args.task_timeout_sec is None


def test_load_config_resolves_allowed_users_from_secrets_when_env_absent(monkeypatch):
    """Regression (supervisor path): when the AC-owned supervisor spawns the child,
    it does NOT put ALLOWED_USERS in the child env. load_config() must therefore
    resolve the allowlist from config/secrets.json (key 'telegram_allowed_users'),
    exactly like the bot token — otherwise the child exits 2 (empty allowlist) and
    the UI toggle can never start the bridge.
    """
    import agent_cascade.telegram_bridge.config as cfg_mod

    # Ensure the env fallback is absent so only the secrets.json path can supply it.
    monkeypatch.delenv('ALLOWED_USERS', raising=False)

    fake_secrets = {'telegram_allowed_users': str(FAKE_ALLOWED_USER_ID)}
    try:
        from config import secrets_loader as _sl
        real_get_secret = _sl.get_secret
        monkeypatch.setattr(_sl, 'get_secret', lambda k: fake_secrets.get(k))
        cfg = cfg_mod.load_config()
        assert cfg.allowed_users == [FAKE_ALLOWED_USER_ID]
    finally:
        try:
            from config import secrets_loader as _sl2
            monkeypatch.setattr(_sl2, 'get_secret', real_get_secret)
        except Exception:
            pass


def test_load_config_allowed_users_secrets_wins_over_env(monkeypatch):
    """Precedence matches the bot-token pattern: config/secrets.json is the
    authoritative store and wins when it has a value; env ALLOWED_USERS is only a
    fallback (e.g. for ad-hoc manual launches outside the repo)."""
    import agent_cascade.telegram_bridge.config as cfg_mod

    monkeypatch.setenv('ALLOWED_USERS', str(FAKE_STRANGER_USER_ID))
    try:
        from config import secrets_loader as _sl
        real_get_secret = _sl.get_secret
        # secrets.json has the allowed id; env has a different (stranger) id.
        monkeypatch.setattr(_sl, 'get_secret', lambda k: {'telegram_allowed_users': str(FAKE_ALLOWED_USER_ID)}.get(k))
        cfg = cfg_mod.load_config()
        # secrets.json value wins.
        assert cfg.allowed_users == [FAKE_ALLOWED_USER_ID]
    finally:
        try:
            from config import secrets_loader as _sl2
            monkeypatch.setattr(_sl2, 'get_secret', real_get_secret)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 8. Phase 2 — slash-command dispatcher (extensible COMMANDS registry)
# ---------------------------------------------------------------------------

def _cmd_ac(**overrides):
    """A mock ACClient whose high-level methods are AsyncMocks (awaitable, like the
    real client) returning {} by default. Production code that awaits them works
    unchanged; call counts/args are assertable on each method.
    """
    ac = MagicMock()
    for m in ('inject_message', 'reset', 'approve', 'reject', 'stop', 'restart',
              'set_auto_security', 'set_afk', 'restore_session'):
        setattr(ac, m, AsyncMock(return_value={}))
    # ensure_token is async and returns (token, shared_secret).
    ac.ensure_token = AsyncMock(return_value=('tok_test', b'secret'))
    for k, v in overrides.items():
        setattr(ac, k, v)
    return ac


def _cmd_context(text, user_id=FAKE_ALLOWED_USER_ID, ac=None):
    """Build (update, context) for a command test with a capturing bot."""
    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[FAKE_ALLOWED_USER_ID])
    if ac is None:
        ac = _cmd_ac()
    sent_texts = []

    async def _capture(**kwargs):
        sent_texts.append(kwargs['text'])
        return None

    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    context.bot.send_message = MagicMock(side_effect=_capture)
    update = _make_update(user_id=user_id, text=text)
    return update, context, ac, sent_texts


def test_parse_command_shapes():
    assert parse_command('/stop') == ('stop', '')
    assert parse_command('/afk on 300') == ('afk', 'on 300')
    assert parse_command('/help') == ('help', '')
    assert parse_command('/?') == ('help', '')          # alias
    assert parse_command('/STOP Now') == ('stop', 'Now')  # case-insensitive name
    assert parse_command('/cmd@MyBot arg') == ('cmd', 'arg')  # bot mention stripped
    assert parse_command('plain text') is None
    assert parse_command('') is None


def test_registry_contains_v1_command_set():
    expected = {'new', 'status', 'yes', 'no', 'stop', 'restart',
                'afk', 'security', 'restore', 'help'}
    assert set(COMMANDS) == expected
    # Every handler has a non-empty description (used by /help).
    for name, cmd in COMMANDS.items():
        assert cmd.name == name
        assert cmd.description.strip()


@pytest.mark.parametrize('text,method,expected', [
    ('/new', 'reset', call()),
    ('/stop', 'stop', call()),
    ('/restart', 'restart', call()),
    ('/security on', 'set_auto_security', call(True)),
    ('/security off', 'set_auto_security', call(False)),
    ('/afk on 300', 'set_afk', call(True, timeout_seconds=300)),
    ('/afk off', 'set_afk', call(False, timeout_seconds=None)),
    ('/restore mysession', 'restore_session', call('mysession')),
])
def test_command_calls_right_client_method(text, method, expected):
    """Each registered command routes to the correct ACClient method with right args."""
    update, context, ac, sent = _cmd_context(text)
    _run(on_message(update, context))

    getattr(ac, method).assert_called_once_with(*expected.args, **expected.kwargs)
    # The reply was actually sent back to Telegram.
    assert len(sent) == 1
    # Critical guarantee: system commands NEVER reach the agent.
    ac.inject_message.assert_not_called()


def test_command_new_replies_confirmation():
    update, context, ac, sent = _cmd_context('/new')
    _run(on_message(update, context))
    ac.reset.assert_called_once()
    assert 'New session' in sent[0]
    ac.inject_message.assert_not_called()


def test_command_status_lists_pending_approvals():
    status_payload = {
        'generating': True, 'active_agent': 'Maine', 'agents': [],
        'active_stack': [], 'instance_halted': False,
        'pending_approvals': [
            {'request_id': 'abc123', 'agent_name': 'worker1', 'tool_name': 'shell_cmd',
             'description': 'run build'},
        ],
    }
    ac = _cmd_ac(get_status=AsyncMock(return_value=status_payload))
    update, context, ac, sent = _cmd_context('/status', ac=ac)
    _run(on_message(update, context))

    # get_status was called with the (mocked) token and its coroutine was awaited.
    assert ac.get_status.call_count == 1
    assert ac.get_status.call_args.args == ('tok_test',)
    assert 'Generating' in sent[0]
    assert 'shell_cmd' in sent[0] and 'abc123' in sent[0]
    ac.inject_message.assert_not_called()


def test_command_yes_approves_first_pending():
    status_payload = {'generating': True, 'pending_approvals': [
        {'request_id': 'rid_1', 'tool_name': 'shell_cmd'},
        {'request_id': 'rid_2', 'tool_name': 'write_file'},
    ]}

    async def _approve(rid):
        assert rid == 'rid_1'
        return {'status': 'ok', 'result': 'Approved: rid_1'}

    ac = _cmd_ac(get_status=AsyncMock(return_value=status_payload),
                 approve=AsyncMock(side_effect=_approve))
    update, context, ac, sent = _cmd_context('/yes', ac=ac)
    _run(on_message(update, context))

    ac.approve.assert_called_once_with('rid_1')
    assert 'Approved' in sent[0] and 'shell_cmd' in sent[0]
    ac.inject_message.assert_not_called()


def test_command_yes_matches_arg_request_id():
    status_payload = {'pending_approvals': [
        {'request_id': 'rid_1', 'tool_name': 'shell_cmd'},
        {'request_id': 'rid_2', 'tool_name': 'write_file'},
    ]}
    ac = _cmd_ac(get_status=AsyncMock(return_value=status_payload))
    update, context, ac, sent = _cmd_context('/yes rid_2', ac=ac)
    _run(on_message(update, context))

    ac.approve.assert_called_once_with('rid_2')
    assert 'write_file' in sent[0]


def test_command_no_rejects_first_pending():
    status_payload = {'pending_approvals': [
        {'request_id': 'rid_9', 'tool_name': 'delete_file'},
    ]}

    async def _reject(rid, reason='Rejected by user'):
        assert rid == 'rid_9'
        return {'status': 'ok', 'result': 'Rejected: rid_9'}

    ac = _cmd_ac(get_status=AsyncMock(return_value=status_payload),
                 reject=AsyncMock(side_effect=_reject))
    update, context, ac, sent = _cmd_context('/no', ac=ac)
    _run(on_message(update, context))

    ac.reject.assert_called_once()
    assert 'Rejected' in sent[0] and 'delete_file' in sent[0]
    ac.inject_message.assert_not_called()


def test_command_yes_nothing_pending_is_benign():
    ac = _cmd_ac(get_status=AsyncMock(return_value={'pending_approvals': []}))
    update, context, ac, sent = _cmd_context('/yes', ac=ac)
    _run(on_message(update, context))

    assert 'Nothing pending' in sent[0]
    ac.approve.assert_not_called()
    ac.inject_message.assert_not_called()


def test_command_yes_already_resolved_is_benign_noop():
    """A race with the UI can make the approval resolve between status and approve;
    the handler must not crash or report failure."""
    status_payload = {'pending_approvals': [
        {'request_id': 'rid_x', 'tool_name': 'shell_cmd'},
    ]}

    async def _approve(rid):
        return {'status': 'ok',
                'result': "ERROR: Request 'rid_x' not found or already resolved."}

    ac = _cmd_ac(get_status=AsyncMock(return_value=status_payload),
                 approve=AsyncMock(side_effect=_approve))
    update, context, ac, sent = _cmd_context('/yes', ac=ac)
    _run(on_message(update, context))

    assert 'Already resolved' in sent[0]
    ac.inject_message.assert_not_called()


def test_command_ac_error_replies_failure_without_crash():
    """An ACError from a command call becomes a short failure reply, not an exception."""
    async def _boom():
        raise ACError('AC /api/stop failed (503): Agent pool not initialized')

    ac = _cmd_ac(stop=AsyncMock(side_effect=_boom))
    update, context, ac, sent = _cmd_context('/stop', ac=ac)
    _run(on_message(update, context))

    assert len(sent) == 1
    assert 'failed' in sent[0].lower() or '⚠️' in sent[0]
    ac.inject_message.assert_not_called()


def test_unknown_command_forwarded_to_agent_without_unknown_reply():
    """/frobulate xyz is unregistered -> forwarded to the agent (inject_message
    called with the ORIGINAL text), and no "Unknown command" reply is sent."""
    update, context, ac, sent = _cmd_context('/frobulate xyz')

    async def go():
        await on_message(update, context)
        for t in list(context.bot_data.get('waiters', ()) or ()):
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    _run(go())

    ac.inject_message.assert_called_once()
    assert ac.inject_message.call_args.args[0] == '/frobulate xyz'
    # Only the "Started" ack is sent — never an unknown-command reply.
    for text in sent:
        assert 'Unknown command' not in text


def test_help_lists_all_registered_commands():
    update, context, ac, sent = _cmd_context('/help')
    _run(on_message(update, context))

    assert len(sent) == 1
    for name in COMMANDS:
        assert f'/{name}' in sent[0]
    ac.inject_message.assert_not_called()


def test_help_alias_question_mark():
    update, context, ac, sent = _cmd_context('/?')
    _run(on_message(update, context))

    assert len(sent) == 1
    for name in COMMANDS:
        assert f'/{name}' in sent[0]
    ac.inject_message.assert_not_called()


def test_afk_and_security_usage_errors_reply_without_ac_call():
    for text in ('/afk', '/afk maybe', '/security', '/restore'):
        update, context, ac, sent = _cmd_context(text)
        _run(on_message(update, context))
        assert len(sent) == 1
        assert 'Usage' in sent[0]
        ac.set_afk.assert_not_called()
        ac.set_auto_security.assert_not_called()
        ac.restore_session.assert_not_called()
        ac.inject_message.assert_not_called()


def test_non_command_slash_text_falls_through_to_inject():
    """Regression: unregistered '/...' text falls through to inject_message with
    the original text (no "Unknown command" reply)."""
    update, context, ac, sent = _cmd_context('/not-a-command please')

    async def go():
        await on_message(update, context)
        for t in list(context.bot_data.get('waiters', ()) or ()):
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    _run(go())
    ac.inject_message.assert_called_once()
    assert ac.inject_message.call_args.args[0] == '/not-a-command please'
    for text in sent:
        assert 'Unknown command' not in text

    # Plain (non-slash) text still goes through the inject path.
    update2, context2, ac2, sent2 = _cmd_context('do the thing')

    async def _inject(text, target=None):
        return {'status': 'success', 'queued': True, 'target': target or 'Maine'}
    ac2.inject_message = MagicMock(side_effect=_inject)
    context2.bot_data['ac_client'] = ac2

    async def go():
        await on_message(update2, context2)
        for t in list(context2.bot_data.get('waiters', ()) or ()):
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    _run(go())
    ac2.inject_message.assert_called_once()


def test_allowlist_gate_still_applies_to_commands():
    """A non-allowlisted user's /stop is ignored: no AC call, no reply."""
    update, context, ac, sent = _cmd_context('/stop', user_id=FAKE_STRANGER_USER_ID)
    _run(on_message(update, context))

    ac.inject_message.assert_not_called()
    ac.stop.assert_not_called()
    context.bot.send_message.assert_not_called()


def test_every_registered_command_never_injects():
    """Parametrized guarantee: for EVERY command in the registry, on_message never
    calls ac.inject_message (system commands must not reach the agent/logs)."""
    texts = {
        'new': '/new', 'status': '/status', 'yes': '/yes', 'no': '/no',
        'stop': '/stop', 'restart': '/restart', 'help': '/help', '?': '/?',
        'afk': '/afk on 300', 'security': '/security off', 'restore': '/restore s1',
    }
    for name, text in texts.items():
        update, context, ac, sent = _cmd_context(text)
        _run(on_message(update, context))
        try:
            ac.inject_message.assert_not_called()
        except AssertionError:
            raise AssertionError(f'/{name} must not call inject_message')
        assert len(sent) >= 1, f'/{name} should reply'


def test_build_application_routes_commands_to_on_message():
    """CRITICAL: /stop must reach on_message through the registered PTB handler.

    Uses a real telegram.Update (PTB 22.x check_update requires isinstance Update).
    The message carries a bot_command entity so it is a genuine COMMAND update —
    under the pre-Phase 2 filter (filters.TEXT & ~filters.COMMAND) this exact
    update would NOT match, which is why that assertion is non-vacuous.
    """
    from telegram import Chat, Message, MessageEntity, Update
    from agent_cascade.telegram_bridge.bot import build_application

    cfg = BridgeConfig(enabled=True, bot_token='fake-token', allowed_users=[1])
    ac = MagicMock()
    app = build_application(cfg, ac)

    handler = None
    for h in app.handlers[0]:  # MessageHandler group
        if h.callback is on_message:
            handler = h
            break
    assert handler is not None, 'on_message must be registered'

    update = Update(
        update_id=1,
        message=Message(
            message_id=1, date='2026-09-24T00:00:00',
            chat=Chat(id=1, type='private'), text='/stop',
            entities=[MessageEntity(type='bot_command', offset=0, length=5)],
        ),
    )
    match = handler.check_update(update)
    assert match is not None, 'the registered filter must route /stop to on_message'

    # Sanity: the old (pre-Phase 2) filter combo WOULD have excluded this update.
    from telegram.ext import filters
    old_filter = filters.TEXT & ~filters.COMMAND
    assert not old_filter.check_update(update), 'test is vacuous if the old filter also matched'


# ---------------------------------------------------------------------------
# _token_post must send the session token as a QUERY param (the Phase 1 command
# endpoints read `token` from the query string, not the JSON body). This test
# exercises the REAL _token_post against a faithful mock server — the dispatcher
# tests above use AsyncMock for ACClient and therefore never cover this. A
# body-only token would 401 here, catching that regression class.
# ---------------------------------------------------------------------------

def test_token_post_sends_token_as_query_param():
    mock = _MockACServer()
    client = _make_client(mock)

    async def _go():
        await client.open()
        # stop() -> _token_post('/api/stop', {}) must authenticate via query token.
        res = await client.stop()
        assert res['status'] == 'ok'
        # A body-carrying endpoint too (auto_security) to cover the json=body path.
        res2 = await client.set_auto_security(True)
        assert res2['status'] == 'ok'

    _run(_go())
    assert mock.command_calls == ['/api/stop', '/api/auto_security']


def test_token_post_body_only_token_would_401():
    """Guard: if a client sent the token ONLY in the body (not query), it 401s.

    This documents WHY the query-param convention matters — the mock rejects a
    request whose token is absent from the query string, exactly like the real
    api_server.py endpoints do.
    """
    mock = _MockACServer()
    client = _make_client(mock)

    async def _go():
        await client.open()
        token = (await client.ensure_token())[0]
        # Deliberately send the token in the body only — must be rejected.
        resp = await client._request('POST', '/api/stop', json={'session_token': token})
        assert resp.status_code == 401, 'body-only token must NOT authenticate'

    _run(_go())


# ---------------------------------------------------------------------------
# 9. Phase 4 (a) — END-TO-END hermetic integration: real on_message -> command
# dispatcher -> REAL ACClient HTTP layer -> fake AC server (_MockACServer via
# httpx.MockTransport). No live Telegram, no live AC, no subprocess.
#
# This is the test that proves the whole receiving path together in one shot:
# a genuine PTB Update drives bot.on_message (auth gate included), which either
# dispatches a system command through commands.dispatch_command into the REAL
# ACClient's _token_post (query-param token, real handshake) — and returns
# WITHOUT injecting — or falls through to ac.inject_message for plain text.
# The section-8 dispatcher tests use an AsyncMock ACClient and therefore never
# exercise the real HTTP layer; this one does.
# ---------------------------------------------------------------------------

def _tg_update(user_id: int, text: str, chat_id: int = 100) -> 'Update':
    """Build a REAL telegram.Update (PTB 22.x) with an allowed-list user.

    Carries the entity Telegram always attaches to slash commands — without it
    filters.COMMAND would not match and any filter-level assertion is vacuous
    (see test_build_application_routes_commands_to_on_message).
    """
    from telegram import Chat, Message, MessageEntity, Update, User

    entities = None
    if text.startswith('/'):
        word = text[1:].split(' ', 1)[0]   # command word incl. any @bot mention
        entities = [MessageEntity(type='bot_command', offset=0, length=len(word))]
    return Update(
        update_id=1,
        message=Message(
            message_id=1, date='2026-09-24T00:00:00',
            chat=Chat(id=chat_id, type='private'), text=text,
            from_user=User(id=user_id, is_bot=False, first_name='tester'),
            entities=entities,
        ),
    )


def _e2e_context(ac: ACClient):
    """Build (context, sent_texts) wiring a REAL BridgeConfig + real ACClient into
    the bot_data shape on_message expects, with a capturing mock Telegram bot."""
    cfg = BridgeConfig(enabled=True, bot_token='fake-token',
                       allowed_users=[FAKE_ALLOWED_USER_ID],
                       target_agent='Maine')
    sent_texts: List[str] = []

    async def _capture(**kwargs):
        sent_texts.append(kwargs['text'])
        return None

    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    context.bot.send_message = MagicMock(side_effect=_capture)
    return context, sent_texts


async def _drain_waiters(context) -> None:
    """Await the fire-and-forget waiter tasks on_message spawns, so none leak out
    of the event loop (and their real HTTP polls hit the fake server, not a live AC)."""
    for t in list(context.bot_data.get('waiters', ()) or ()):
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - waiter outcome is asserted separately
            pass


def test_e2e_stop_command_hits_api_stop_and_never_injects():
    """/stop end-to-end: real on_message -> dispatcher -> REAL ACClient HTTP -> fake AC.

    Proves the core Phase 4 guarantee through the real HTTP layer: the fake AC
    server receives an authenticated POST /api/stop (token as query param), and
    ac.inject_message is NEVER called — system commands never reach the agent/logs.
    """
    mock = _MockACServer()
    client = _make_client(mock)   # REAL ACClient on the fake transport
    context, sent_texts = _e2e_context(client)

    async def go():
        await client.open()
        update = _tg_update(FAKE_ALLOWED_USER_ID, '/stop')
        await on_message(update, context)
        await _drain_waiters(context)
        await client.close()

    _run(go())

    # The real HTTP layer delivered the command to the right endpoint...
    assert mock.command_calls == ['/api/stop']
    # ...and it authenticated: the mock only returns 200 when a VALID token is in
    # the query string (a missing/body-only token would have surfaced as an ACError
    # reply, not this success path).
    assert sent_texts == ['🛑 Stopped the current agent']
    # Core guarantee: NOTHING reached the agent — no encrypted /api/message at all.
    assert mock.injected == []
    # The dispatcher returned early, so no waiter was ever spawned either.
    assert context.bot_data.get('waiters') in (None, set())


def test_e2e_normal_text_injects_through_real_http():
    """Plain text end-to-end: real on_message -> REAL ACClient.inject_message -> fake AC.

    The encrypted payload is decrypted server-side by the mock and must equal the
    exact {target, text} that flowed through — proving the inject path (not just
    'no exception') over the real HTTP layer.
    """
    mock = _MockACServer()
    client = _make_client(mock)
    context, sent_texts = _e2e_context(client)

    async def go():
        await client.open()
        update = _tg_update(FAKE_ALLOWED_USER_ID, 'run the integration build')
        await on_message(update, context)
        await _drain_waiters(context)
        await client.close()

    _run(go())

    # The inject path was taken: exactly one decrypted payload reached the agent.
    assert mock.injected == [{'target': 'Maine', 'text': 'run the integration build'}]
    # No command endpoint was hit for plain text.
    assert mock.command_calls == []
    # Ack reply went out, and the waiter polled the real (fake) AC status endpoint.
    assert sent_texts[0].startswith('🏃 Started → Maine')
    assert mock.status_calls >= 1


def test_e2e_status_command_routes_to_api_status_not_inject():
    """/status end-to-end: dispatcher -> REAL ACClient.get_status (query token) -> fake AC.

    Shows the dispatcher routes different commands to different endpoints through
    the real HTTP layer, and that /status — like every system command — never
    injects into the agent.
    """
    mock = _MockACServer()
    client = _make_client(mock)
    context, sent_texts = _e2e_context(client)

    async def go():
        await client.open()
        update = _tg_update(FAKE_ALLOWED_USER_ID, '/status')
        await on_message(update, context)
        await _drain_waiters(context)
        await client.close()

    _run(go())

    # /status is served by the token-auth GET /api/status (not a command endpoint).
    assert mock.status_calls == 1
    assert mock.command_calls == []
    assert 'Idle' in sent_texts[0] and 'No pending approvals' in sent_texts[0]
    # Never reached the agent.
    assert mock.injected == []
