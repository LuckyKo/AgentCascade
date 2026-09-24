"""Minimal v1 Telegram bridge for AgentCascade.

A standalone long-polling process that lets a single allowlisted user drive AC
from a phone: send a task -> AC runs it -> the root agent's final assistant
message is sent back as one reply (chunked only if it exceeds 4096 chars).

v1 scope (see plans/telegram-bridge-implementation-plan.md, V1 SCOPE ADDENDUM):
  - NO streaming / WebSocket client.
  - NO approval buttons.
  - Receiving: PTB long-poll -> auth gate -> X25519+AES-GCM handshake -> POST /api/message.
  - Completion: a per-task waiter polls GET /api/status until generating==false,
    then reads the last assistant message from the open GET /api/state.

Run with:  python -m agent_cascade.telegram_bridge
"""

__version__ = '0.1.0'
