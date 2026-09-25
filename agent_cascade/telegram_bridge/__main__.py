"""Entry point:  python -m agent_cascade.telegram_bridge

Standalone long-polling process. Off unless TG_BRIDGE_ENABLED is true. On startup
it validates config, opens the AC client, builds the PTB Application, and runs
getUpdates polling. SIGINT/SIGTERM trigger graceful shutdown (PTB cancels the poll
loop; any outstanding waiters are cancelled).

Required config (see config.py):
    TG_BRIDGE_ENABLED=true
    telegram_bot_token  (config/secrets.json) or TELEGRAM_BOT_TOKEN env
    ALLOWED_USERS=<telegram user id>
Optional:
    AC_BASE_URL=http://127.0.0.1:12345   TG_TARGET_AGENT=Maine
    TG_POLL_INTERVAL_SEC=2.5             TG_TASK_TIMEOUT_SEC=28800
"""

import asyncio
import sys

from agent_cascade.log import logger

from .ac_client import ACClient
from .bot import build_application
from .config import load_config, validate_config


def main() -> int:
    cfg = load_config()

    if not cfg.enabled:
        logger.info('Telegram bridge is disabled (TG_BRIDGE_ENABLED is not true). Exiting.')
        return 0

    problems = validate_config(cfg)
    if problems:
        for p in problems:
            logger.error('Config problem: %s', p)
        logger.error('Telegram bridge config invalid — %d problem(s); see above.', len(problems))
        return 2

    # Open the AC httpx client in a one-shot event loop. PTB's run_polling() is a
    # *synchronous* method that creates and owns its own event loop, so it must be
    # called from a plain (non-async) context — we cannot wrap it in asyncio.run().
    ac = ACClient(base_url=cfg.ac_base_url, target_agent=cfg.target_agent)
    try:
        asyncio.run(ac.open())
    except Exception as e:
        logger.error('Failed to open AC client: %s', e)
        return 3

    # build_application registers the graceful-shutdown coroutine (cancels
    # outstanding waiters + closes the AC client) via the PTB builder.
    app = build_application(cfg, ac)
    try:
        # run_polling handles SIGINT/SIGTERM and the getUpdates long-poll loop.
        app.run_polling(allowed_updates=['message'], drop_pending_updates=False)
    except KeyboardInterrupt:
        logger.info('Interrupted; shutting down.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
