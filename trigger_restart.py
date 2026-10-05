"""One-shot helper: X25519 handshake against the running AC server, then POST /api/restart.

After the restart, waits for the server to come back and injects a resume message so the
agent (Maine) picks up where it left off — no manual user ping needed.

Usage: python trigger_restart.py [port]   (default port 8126)

Flow:
  1. GET  /api/keys       -> server X25519 public key (base64)
  2. POST /api/handshake  {public_key: <our base64>} -> session_token + shared secret
  3. POST /api/restart?token=<session_token>         -> in-place os.execl restart
  4. Wait ~60s for the server to come back
  5. Fresh handshake (new server keys after restart)
  6. POST /api/message    {AES-GCM encrypted resume text} -> wakes the agent

The server re-execs with the same argv/env (server_restart.restart_server_process), so it
comes back up on the same port with the current source tree loaded.
"""
import base64
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import serialization


def _post_json(url: str, payload: dict):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode('utf-8'))


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


def _handshake(base: str):
    """Do X25519 handshake; return (token, shared_secret_bytes)."""
    keys = _get_json(f'{base}/api/keys')
    server_pub_b64 = keys['public_key']

    client_private = x25519.X25519PrivateKey.generate()
    client_pub_bytes = client_private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    client_pub_b64 = base64.b64encode(client_pub_bytes).decode('utf-8')

    hs = _post_json(f'{base}/api/handshake', {'public_key': client_pub_b64})
    token = hs.get('session_token')
    if not token:
        raise SystemExit(f'handshake did not return a token: {hs}')

    # Derive the shared secret (same as server does)
    server_pub_bytes = base64.b64decode(server_pub_b64)
    server_public_key = x25519.X25519PublicKey.from_public_bytes(server_pub_bytes)
    shared_secret = client_private.exchange(server_public_key)

    return token, shared_secret


def _encrypt_and_send(base: str, token: str, shared_secret: bytes, text: str, target: str = 'Maine'):
    """AES-GCM encrypt the message and POST /api/message."""
    aesgcm = AESGCM(shared_secret)
    nonce = os.urandom(12)
    payload = json.dumps({'target': target, 'text': text}).encode('utf-8')
    ciphertext = aesgcm.encrypt(nonce, payload, None)

    resp = _post_json(f'{base}/api/message', {
        'session_token': token,
        'payload': base64.b64encode(ciphertext).decode('utf-8'),
        'nonce': base64.b64encode(nonce).decode('utf-8'),
    })
    print(f'[restart] resume message sent: {resp}')


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8126
    base = f'http://127.0.0.1:{port}'

    # ── Phase 1: trigger the restart ──────────────────────────────────────────────
    print('[restart] Phase 1: triggering server restart...')
    token, shared_secret = _handshake(base)
    print(f'[restart] got session_token ({token[:8]}...)')

    try:
        req = urllib.request.Request(f'{base}/api/restart?token={token}', method='POST')
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f'[restart] server responded: {r.read().decode('utf-8')}')
    except Exception as e:
        print(f'[restart] restart call sent (connection dropped as expected: {e})')

    # ── Phase 2: wait for the server to come back ─────────────────────────────────
    print('[restart] Phase 2: waiting 60s for server to come back...')
    time.sleep(60)

    # Retry loop: the server may take a moment to bind
    for attempt in range(1, 7):
        try:
            _get_json(f'{base}/api/keys')
            print(f'[restart] server is back (attempt {attempt})')
            break
        except Exception as e:
            print(f'[restart] not yet up (attempt {attempt}): {e}')
            time.sleep(10)
    else:
        print('[restart] ERROR: server did not come back within 90s. Aborting resume.')
        return

    # ── Phase 3: fresh handshake + inject resume message ─────────────────────────
    print('[restart] Phase 3: injecting resume message...')
    try:
        token2, secret2 = _handshake(base)
        resume_text = (
            'Server restarted successfully. Please continue where you left off.'
        )
        _encrypt_and_send(base, token2, secret2, resume_text)
        print('[restart] done — resume message delivered.')
    except Exception as e:
        print(f'[restart] ERROR sending resume message: {e}')


if __name__ == '__main__':
    main()
