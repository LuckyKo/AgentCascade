"""Async REST client for AgentCascade's E2E-encrypted API.

Mirrors the client side of AC's X25519 + AES-GCM scheme (see api_server.py
around lines 868-966):

    GET  /api/keys       -> {public_key: b64, algorithm: 'X25519'}   (open)
    POST /api/handshake  -> {session_token}                          (client sends its pub key)
    POST /api/message    -> {status, queued, target}                 (AES-GCM encrypted payload)
    GET  /api/status     -> {generating, ...}                        (requires valid token)
    GET  /api/state      -> {'messages': [...], 'generating': ...}   (open, no token)

The session_token is in-memory on the AC side with NO TTL, so it only becomes
invalid after an AC restart or a wrong token. We cache ``(token, shared_secret)``.
``inject_message`` re-handshakes exactly once on a 401; ``get_status`` invalidates
the cached token on a 401 and raises, leaving the recovery to its caller (the
waiter re-resolves the token each poll via ``ensure_token()``).
"""

import asyncio
import base64
import json
import os
from typing import Any, Dict, Optional, Tuple

import httpx
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_cascade.log import logger
from agent_cascade.settings import TG_HTTP_REQUEST_TIMEOUT_SEC


class ACError(Exception):
    """Raised when an AC call fails in a way the caller should surface."""


def encrypt_payload(text: str, target: str, shared_secret: bytes) -> Tuple[str, str]:
    """Encrypt ``{target, text}`` with AES-GCM.

    Returns ``(payload_b64, nonce_b64)`` using a fresh 12-byte random nonce,
    exactly the shape AC's /api/message expects.
    """
    inner = json.dumps({'target': target, 'text': text}).encode('utf-8')
    aesgcm = AESGCM(shared_secret)
    nonce = os.urandom(12)  # standard 96-bit GCM nonce
    ciphertext = aesgcm.encrypt(nonce, inner, None)
    return (
        base64.b64encode(ciphertext).decode('utf-8'),
        base64.b64encode(nonce).decode('utf-8'),
    )


class ACClient:
    """Cached, async client for the AgentCascade REST API."""

    def __init__(self, base_url: str, target_agent: str = 'Maine',
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.base_url = (base_url or '').rstrip('/')
        self.target_agent = target_agent
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self._token: Optional[str] = None
        self._shared_secret: Optional[bytes] = None
        self._lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────
    async def __aenter__(self) -> 'ACClient':
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> None:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                transport=self._transport,
                timeout=httpx.Timeout(TG_HTTP_REQUEST_TIMEOUT_SEC),
            )

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ── handshake / token cache ──────────────────────────────────
    async def get_keys(self) -> Dict[str, Any]:
        """GET /api/keys (open). Returns {'public_key': b64, 'algorithm': ...}."""
        resp = await self._request('GET', '/api/keys')
        return resp.json()

    async def handshake(self) -> Tuple[str, bytes]:
        """Perform a fresh X25519 handshake. Returns (token, shared_secret)."""
        keys = await self.get_keys()
        server_pub_b64 = keys.get('public_key')
        if not server_pub_b64:
            raise ACError(f"/api/keys returned no public_key: {keys!r}")

        client_private = x25519.X25519PrivateKey.generate()
        client_pub_b64 = base64.b64encode(
            client_private.public_key().public_bytes_raw()
        ).decode('utf-8')

        resp = await self._request('POST', '/api/handshake', json={'public_key': client_pub_b64})
        if resp.status_code != 200:
            raise ACError(f"/api/handshake failed ({resp.status_code}): {resp.text[:200]}")
        token = resp.json().get('session_token')
        if not token:
            raise ACError(f"/api/handshake returned no session_token: {resp.text[:200]}")

        server_public_key = x25519.X25519PublicKey.from_public_bytes(
            base64.b64decode(server_pub_b64)
        )
        shared_secret = client_private.exchange(server_public_key)
        return token, shared_secret

    async def ensure_token(self) -> Tuple[str, bytes]:
        """Return a cached (token, shared_secret), handshaking lazily if needed."""
        async with self._lock:
            if self._token is None or self._shared_secret is None:
                self._token, self._shared_secret = await self.handshake()
                logger.info('AC handshake complete; session token cached')
            return self._token, self._shared_secret

    def invalidate_token(self) -> None:
        """Drop the cached token so the next call re-handshakes."""
        self._token = None
        self._shared_secret = None

    # ── high-level calls (with 401 retry) ────────────────────────
    async def inject_message(self, text: str, target: Optional[str] = None) -> Dict[str, Any]:
        """Encrypt and POST /api/message. Re-handshakes once on 401."""
        target = target or self.target_agent
        for attempt in (1, 2):
            token, secret = await self.ensure_token()
            payload_b64, nonce_b64 = encrypt_payload(text, target, secret)
            resp = await self._request('POST', '/api/message', json={
                'session_token': token,
                'payload': payload_b64,
                'nonce': nonce_b64,
            })
            if resp.status_code == 401 and attempt == 1:
                logger.warning('AC /api/message 401; re-handshaking and retrying once')
                self.invalidate_token()
                continue
            if resp.status_code != 200:
                raise ACError(f"/api/message failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()
        # Unreachable, but keeps type-checkers happy.
        raise ACError('/api/message retry loop exhausted')

    async def get_status(self, token: str) -> Dict[str, Any]:
        """GET /api/status?token=... (requires a valid token)."""
        resp = await self._request('GET', '/api/status', params={'token': token})
        if resp.status_code == 401:
            # Token went stale (e.g. AC restarted) -> force re-handshake next time.
            self.invalidate_token()
            raise ACError('AC /api/status 401 (session token invalid)')
        if resp.status_code != 200:
            raise ACError(f"/api/status failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    async def get_state(self) -> Dict[str, Any]:
        """GET /api/state (open, no token). Returns full build_state() dict."""
        resp = await self._request('GET', '/api/state')
        if resp.status_code != 200:
            raise ACError(f"/api/state failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    # ── system-command calls (Phase 2) ───────────────────────────
    # Thin wrappers over the Phase 1 REST endpoints. Token-auth ones follow the
    # get_status pattern: ensure_token() + one re-handshake on 401. approve/reject
    # and reset are OPEN endpoints (loopback only); passing the token is harmless,
    # so we keep the same call shape for simplicity.

    async def _token_post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST ``path`` with the session token as a QUERY param; re-handshake once on 401.

        The Phase 1 command endpoints read the token from the query string
        (``token: str = None`` in the FastAPI signature), NOT from the JSON body —
        same convention as get_status(). Sending it in the body would 401.
        """
        for attempt in (1, 2):
            token = (await self.ensure_token())[0]
            resp = await self._request('POST', path, params={'token': token}, json=body)
            if resp.status_code == 401 and attempt == 1:
                logger.warning('AC %s 401; re-handshaking and retrying once', path)
                self.invalidate_token()
                continue
            if resp.status_code != 200:
                raise ACError(f"{path} failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()
        raise ACError(f'{path} retry loop exhausted')

    async def approve(self, request_id: str) -> Dict[str, Any]:
        """POST /api/approve/{request_id} (open endpoint)."""
        resp = await self._request('POST', f'/api/approve/{request_id}')
        if resp.status_code != 200:
            raise ACError(f"/api/approve failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    async def reject(self, request_id: str, reason: str = 'Rejected by user') -> Dict[str, Any]:
        """POST /api/reject/{request_id}?reason=... (open endpoint)."""
        resp = await self._request('POST', f'/api/reject/{request_id}', params={'reason': reason})
        if resp.status_code != 200:
            raise ACError(f"/api/reject failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    async def stop(self) -> Dict[str, Any]:
        """POST /api/stop (session_token)."""
        return await self._token_post('/api/stop', {})

    async def restart(self) -> Dict[str, Any]:
        """POST /api/restart (session_token). The AC process exits right after replying.

        The server spawns a detached child and then os._exit(0)s, which can preempt the
        HTTP 200 — so a connection reset/timeout here does NOT mean the restart failed;
        it is treated as success (the /restart request was delivered). A 401 still
        re-handshakes once before being raised.
        """
        for attempt in (1, 2):
            token = (await self.ensure_token())[0]
            try:
                resp = await self._request('POST', '/api/restart', params={'token': token}, json={})
            except httpx.HTTPError as e:
                logger.warning('AC /api/restart connection dropped (%s) — treating as success '
                               '(the AC process exits right after the request)', e)
                return {'status': 'restarting'}
            if resp.status_code == 401 and attempt == 1:
                # Token went stale (e.g. AC restarted since our last handshake) — retry once.
                logger.warning('AC /api/restart 401; re-handshaking and retrying once')
                self.invalidate_token()
                continue
            break
        if resp.status_code != 200:
            raise ACError(f"/api/restart failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    async def reset(self) -> Dict[str, Any]:
        """POST /api/reset (open endpoint) — start a new session."""
        resp = await self._request('POST', '/api/reset')
        if resp.status_code != 200:
            raise ACError(f"/api/reset failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    async def set_auto_security(self, enabled: bool) -> Dict[str, Any]:
        """POST /api/auto_security (session_token)."""
        return await self._token_post('/api/auto_security', {'enabled': bool(enabled)})

    async def set_afk(self, enabled: bool, timeout_seconds: Optional[int] = None) -> Dict[str, Any]:
        """POST /api/afk (session_token)."""
        body: Dict[str, Any] = {'enabled': bool(enabled)}
        if timeout_seconds is not None:
            body['timeout_seconds'] = int(timeout_seconds)
        return await self._token_post('/api/afk', body)

    async def restore_session(self, name: str) -> Dict[str, Any]:
        """POST /api/session/restore (session_token)."""
        return await self._token_post('/api/session/restore', {'name': name})

    # ── low-level helper ─────────────────────────────────────────
    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._client is None or self._client.is_closed:
            await self.open()
        assert self._client is not None
        return await self._client.request(method, path, **kwargs)
