# AgentCascade

AgentCascade is a multi-agent orchestration system featuring a secure API for remote control and monitoring.

## Core Features

- **Secure Message Injection**: Uses X25519 key exchange and AES-GCM encryption for the `/api/message` endpoint.
- **Real-time Monitoring**: Full visibility into agent states and control via REST and WebSocket interfaces.
- **Flexible API Configuration**: Dynamic endpoint management and priority configuration.
- **Workspace Integration**: Direct access to workspace files and integrated document parsing utilities.
- **Comprehensive Telemetry**: Detailed session logging and telemetry export for performance analysis.

## Quick Start

### Prerequisites
- Python 3.10 or higher
- Docker (required for the `code_interpreter` tool)

### Installation
Install the package with necessary extensions:
```bash
pip install -U "agent-cascade[rag,code_interpreter,mcp]"
```

### Starting the Server
Run the API server specifying the desired port:
```bash
python start_api_server.py --port 8000
```

### First Connection
To send a secure message, you must perform a handshake to establish a shared secret.

```python
import requests
import base64
import json
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import os

BASE_URL = "http://localhost:8000"

# 1. Get Server Public Key (Base64)
server_pub_b64 = requests.get(f"{BASE_URL}/api/keys").json()["public_key"]
server_pub_bytes = base64.b64decode(server_pub_b64)
server_pub_key = x25519.X25519PublicKey.from_public_bytes(server_pub_bytes)

# 2. Generate Client Keys and Handshake
client_private_key = x25519.X25519PrivateKey.generate()
client_pub_bytes = client_private_key.public_key().public_bytes_raw()
client_pub_b64 = base64.b64encode(client_pub_bytes).decode('utf-8')

response = requests.post(f"{BASE_URL}/api/handshake", json={"public_key": client_pub_b64})
session_token = response.json()["session_token"]

# 3. Derive Shared Secret (ECDH)
shared_key = client_private_key.exchange(server_pub_key)
aesgcm = AESGCM(shared_key)

# 4. Encrypt and Inject Message
# The decrypted payload must be valid JSON with at least a "text" field.
nonce = os.urandom(12)
message_data = {"text": "Hello Agent!", "target": "Maine"}  # "target" is optional
ciphertext = aesgcm.encrypt(nonce, json.dumps(message_data).encode('utf-8'), None)

payload = {
    "session_token": session_token,
    "nonce": nonce.hex(),
    "ciphertext": ciphertext.hex()
}
requests.post(f"{BASE_URL}/api/message", json=payload)
```

## Configuration

### Environment Variables
Key configuration options are managed via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `QWEN_AGENT_MODEL` | LLM model name | - |
| `QWEN_AGENT_API_BASE` | LLM API base URL | - |
| `QWEN_AGENT_API_KEY` | LLM API key | - |
| `DASHSCOPE_API_KEY` | Alibaba DashScope API key | - |
| `QWEN_AGENT_IDLE_TIMEOUT` | Regular agent idle timeout | 900s |
| `QWEN_AGENT_SYSTEM_AGENT_IDLE_TIMEOUT` | System agent idle timeout | 900s |
| `QWEN_AGENT_IDLE_CHECK_INTERVAL` | Idle check frequency | 60s |

### CLI Flags
- `--port`: Sets the port for the API server (e.g., `--port 8080`).

## Navigation
- **API Reference**: Detailed endpoint and WebSocket specifications can be found in [docs/API_REFERENCE.md](docs/API_REFERENCE.md).
- **System Architecture**: For a deep-dive into the system design, see [docs/SYSTEM_DOCS.md](docs/SYSTEM_DOCS.md).