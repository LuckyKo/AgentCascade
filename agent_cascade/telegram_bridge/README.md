# Telegram Bridge (v1)

A standalone long-polling process that lets a single allowlisted user drive
AgentCascade from a phone: send a task → AC runs it → the root agent's final
assistant message is sent back as one reply (split across multiple Telegram
messages only if it exceeds 4096 chars).

This is **v1** — intentionally minimal. There is **no streaming**, **no
WebSocket client**, and **no approval buttons**. If AC hits a security/approval
prompt while you're on Telegram, the run simply pauses (you can resume it from
the normal AC UI/console). See the plan's V1 SCOPE ADDENDUM for what's deferred.

## How it works

```
📱 You (private chat)  --PTB long-poll getUpdates-->  [bridge process]
     auth gate (ALLOWED_USERS)
     X25519 + AES-GCM handshake vs AC (cached; re-handshake on 401)
     POST /api/message  -> inject {target: 'Maine', text: <your message>}
     reply "🏃 Started"
     waiter polls GET /api/status?token=… until generating == false
     then GET /api/state (open) -> send the LAST assistant message as the reply
```

The bridge is a **separate process** from AC, so it survives an AC restart. It
talks to AC over localhost REST only (`127.0.0.1` by default).

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
| `TG_TASK_TIMEOUT_SEC` | `1800` | Max seconds to wait per task before giving up ("⏱️ Timed out"). |

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

### Running as a persistent service

The bridge is designed to run indefinitely. For production use, launch it under a
process manager so it survives terminal/session disconnects:

```bash
# Windows (Task Scheduler or NSSM)
nssm install AC_TelegramBridge "C:\Python312\python.exe" "-m agent_cascade.telegram_bridge"
nssm set AC_TelegramBridge AppDirectory "N:\work\WD\AgentCascade"
nssm set AC_TelegramBridge AppEnvironmentExtra TG_BRIDGE_ENABLED=true AC_BASE_URL=http://127.0.0.1:8126 ALLOWED_USERS=<your_id>
nssm start AC_TelegramBridge

# Linux (systemd unit)
# [Unit] Description=AC Telegram Bridge / After=network.target
# [Service] WorkingDirectory=/path/to/AgentCascade
#   Environment=TG_BRIDGE_ENABLED=true AC_BASE_URL=http://127.0.0.1:8126 ALLOWED_USERS=<your_id>
#   ExecStart=/usr/bin/python3 -m agent_cascade.telegram_bridge
# [Install] WantedBy=multi-user.target
```

> **Note:** If you launch it from an interactive shell (e.g. via AgentCascade's
> `shell_cmd`), the process is bound to that session's timeout (max 1 hour). For
> unattended operation, use a service manager as shown above.

## Tests

Hermetic unit tests (no network, no real bot, no running AC):

```bash
python -m pytest tests/test_telegram_bridge.py -v -o addopts="" --timeout=60
```

The `-o addopts=""` override disables the xdist options pinned in `pytest.ini`.
