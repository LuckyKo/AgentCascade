# Telegram Bridge (v1)

A long-polling daemon thread that lets a single allowlisted user drive
AgentCascade from a phone: send a task → AC runs it → the root agent's final
assistant message is sent back as one reply (split across multiple Telegram
messages only if it exceeds 4096 chars).

This is **v1** — intentionally minimal. There is **no streaming**, **no
WebSocket client**, and **no approval buttons**. If AC hits a security/approval
prompt while you're on Telegram, the run simply pauses (you can resume it from
the normal AC UI/console). See the plan's V1 SCOPE ADDENDUM for what's deferred.

## How it works

```
📱 You (private chat)  --PTB long-poll getUpdates-->  [bridge daemon thread in AC]
     auth gate (ALLOWED_USERS)
     X25519 + AES-GCM handshake vs AC (cached; re-handshake on 401)
     POST /api/message  -> inject {target: 'Maine', text: <your message>}
     reply "🏃 Started"
     waiter polls GET /api/status?token=… until generating == false
     then GET /api/state (open) -> send the LAST assistant message as the reply
```

The bridge runs **in-process** as a daemon thread of AC (`tg-bridge`). It dies
with AC on restart (nothing to orphan). Standalone mode is still available via
`python -m agent_cascade.telegram_bridge` for development or external hosting.
It talks to AC over localhost REST only (`127.0.0.1` by default).

## Install

```bash
pip install -e ".[telegram]"     # or: pip install python-telegram-bot cryptography httpx
```

## Configure

The bot token is a **secret** — store it in AC's gitignored `config/secrets.json`:

```json
{ "telegram_bot_token": "<your BotFather token>" }
```

All other settings are plain environment variables (or `.env`):

| Var | Default | Meaning |
|---|---|---|
| `TG_BRIDGE_ENABLED` | `false` | Master switch. Bridge exits immediately unless true. |
| `ALLOWED_USERS` | — (required) | Comma-separated Telegram user IDs. Single id in v1. |
| `AC_BASE_URL` | `http://127.0.0.1:12345` | AC REST base URL. **Port must match how AC is actually launched.** |
| `TG_TARGET_AGENT` | `Maine` | `target` for `/api/message` (the root/orchestrator). |
| `TG_POLL_INTERVAL_SEC` | `2.5` | `/api/status` poll cadence while waiting for completion. |
| `TG_TASK_TIMEOUT_SEC` | `28800` | Per-task wait window; on expiry a "⏳ Still working" notice is sent and the waiter keeps polling (one notice per interval) until completion or the ceiling below; 8h since AC runs can be long. |
| `AGENT_CASCADE_TG_TASK_WAIT_CEILING_SEC` | `86400` | Outer hard ceiling on **total** wait time (measured from task start, not from the first "still working" notice); when reached the waiter stops and sends a final "gave up" message (24h). |

> The bot token may also be supplied via the `TELEGRAM_BOT_TOKEN` env var as a
> fallback, but prefer `config/secrets.json` (it is gitignored).

## Run

Simplest — use the launcher (validates config, fills env vars, no need to remember them):

```bash
python -m agent_cascade.telegram_bridge.run_bridge --base-url http://127.0.0.1:8126 --allowed-users <YOUR_TELEGRAM_USER_ID>
```

Or run the module directly with env vars set yourself:

```bash
python -m agent_cascade.telegram_bridge
```

- If `TG_BRIDGE_ENABLED` is not true → prints a note and exits (off by default).
- If enabled but the token or allowlist is missing → prints config problems and exits non-zero.
- SIGINT/SIGTERM → graceful shutdown (PTB stops polling; outstanding waiters are cancelled).

### Running standalone (optional)

The in-process daemon thread is the default and recommended mode. Standalone mode
is available for development, debugging, or hosting the bridge externally:

```bash
python -m agent_cascade.telegram_bridge
```

In standalone mode, `TG_BRIDGE_ENABLED` must be `true` and all settings come from
env vars / `.env`. The process runs until SIGINT/SIGTERM. For unattended
standalone operation, use a process manager (NSSM on Windows, systemd on Linux).

## Tests

Hermetic unit tests (no network, no real bot, no running AC):

```bash
python -m pytest tests/test_telegram_bridge.py -v -o addopts="" --timeout=60
```

The `-o addopts=""` override disables the xdist options pinned in `pytest.ini`.
