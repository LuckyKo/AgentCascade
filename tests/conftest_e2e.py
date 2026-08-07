"""Shared helpers for E2E encrypted API tests.

Extracted from test_api_endpoints.py to avoid duplication across test files.
Used by: test_startup_integration.py, test_api_endpoints.py (when fixed)
"""

import base64
import json
import os

from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_client_keypair():
    """Generate X25519 key pair. Returns (private_key, public_key_b64)."""
    private_key = x25519.X25519PrivateKey.generate()
    public_key_b64 = base64.b64encode(
        private_key.public_key().public_bytes_raw()
    ).decode("utf-8")
    return private_key, public_key_b64


def derive_shared_secret(client_private_key: x25519.X25519PrivateKey, server_public_b64: str) -> bytes:
    """Derive the shared secret from client private key and server public key."""
    server_public_bytes = base64.b64decode(server_public_b64)
    server_public_key = x25519.X25519PublicKey.from_public_bytes(server_public_bytes)
    return client_private_key.exchange(server_public_key)


def encrypt_payload(shared_secret: bytes, payload: dict) -> tuple[str, str]:
    """Encrypt a JSON payload with AES-GCM using the shared secret.

    Returns (encrypted_b64, nonce_b64) ready for /api/message.
    """
    aesgcm = AESGCM(shared_secret)
    nonce = os.urandom(12)  # 96-bit nonce for AES-GCM
    plaintext = json.dumps(payload).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return base64.b64encode(ciphertext).decode("utf-8"), base64.b64encode(nonce).decode("utf-8")