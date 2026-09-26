"""Telegram bridge for AgentCascade.

Lets a single allowlisted user drive AC from a phone: send a task -> AC runs it
-> the root agent's final answer is pushed back once per run (chunked only if it
exceeds 4096 chars). The bridge runs as an in-process daemon thread on the agent
pool, not a separate process.

Scope notes (see plans/telegram-bridge-implementation-plan.md and
plans/tg-bridge-push-model_PLAN.md):
  - NO streaming / WebSocket client.
  - NO approval buttons.
  - Receiving: PTB long-poll -> auth gate -> X25519+AES-GCM handshake -> POST /api/message.
  - Completion: AC pushes the final answer at natural end of the root run (pre- and
    post-skill-reflection, deduped to one message per run). A per-task waiter still
    polls GET /api/status for "still working" pings and offline notices only.

Run with:  python -m agent_cascade.telegram_bridge
"""

__version__ = '0.1.0'
