"""Convenience launcher for the v1 Telegram bridge.

Runs ``python -m agent_cascade.telegram_bridge`` in-process after validating
config, so you don't have to remember (or typo) the required env vars. It is a
thin wrapper around :mod:`agent_cascade.telegram_bridge.__main__` — it does NOT
re-implement any bridge logic.

Usage (all args optional; they only fill in env vars that are not already set):

    python -m agent_cascade.telegram_bridge.run_bridge \
        --base-url http://127.0.0.1:8126 --allowed-users <YOUR_TELEGRAM_USER_ID>

If a value is omitted, the corresponding env var (or its default) is used, so an
existing ``TG_BRIDGE_ENABLED`` / ``AC_BASE_URL`` / ``ALLOWED_USERS`` setup keeps
working unchanged. The bot token always comes from config/secrets.json (or the
TELEGRAM_BOT_TOKEN env var) — it is never a CLI arg.

Exit codes match __main__: 0 = stopped cleanly, 2 = config problem, 3 = AC client
failed to open.
"""

import argparse
import os
import sys

from agent_cascade.telegram_bridge.__main__ import main as _bridge_main
from agent_cascade.telegram_bridge.config import load_config, validate_config


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='run_bridge',
        description='Validate config and launch the AC Telegram bridge.',
    )
    p.add_argument('--base-url', dest='base_url', default=None,
                   help='AC REST base URL (sets AC_BASE_URL). Default: http://127.0.0.1:12345')
    p.add_argument('--allowed-users', dest='allowed_users', default=None,
                   help='Comma-separated Telegram user IDs (sets ALLOWED_USERS). Required to start.')
    p.add_argument('--target-agent', dest='target_agent', default=None,
                   help='AC target agent for injected messages (sets TG_TARGET_AGENT). Default: Maine')
    p.add_argument('--poll-interval-sec', dest='poll_interval_sec', default=None,
                   help='/api/status poll cadence in seconds (sets TG_POLL_INTERVAL_SEC). Default: 2.5')
    p.add_argument('--task-timeout-sec', dest='task_timeout_sec', default=None,
                   help='Max seconds to wait per task (sets TG_TASK_TIMEOUT_SEC). Default: 28800 (8h)')
    return p


def apply_env(args) -> None:
    """Populate bridge env vars from parsed CLI args (idempotent, env-wins).

    A CLI value is applied only when the corresponding env var is not already set
    (existence check, so even an explicit empty string is respected), meaning an
    existing environment always wins. ``TG_BRIDGE_ENABLED`` is force-set to 'true'
    only when unset — the launcher exists to start the bridge, which is off by
    default in :func:`load_config`.
    """
    mapping = {
        'AC_BASE_URL': args.base_url,
        'ALLOWED_USERS': args.allowed_users,
        'TG_TARGET_AGENT': args.target_agent,
        'TG_POLL_INTERVAL_SEC': args.poll_interval_sec,
        'TG_TASK_TIMEOUT_SEC': args.task_timeout_sec,
    }
    for env_key, value in mapping.items():
        if value is not None and env_key not in os.environ:
            os.environ[env_key] = str(value)

    if 'TG_BRIDGE_ENABLED' not in os.environ:
        os.environ['TG_BRIDGE_ENABLED'] = 'true'


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    apply_env(args)

    # Pre-validate so a bad config gives one clear message instead of a crash deep
    # in PTB. __main__ re-validates; this is just friendlier up-front.
    problems = validate_config(load_config())
    if problems:
        for p in problems:
            print(f'Config problem: {p}', file=sys.stderr)
        return 2

    return _bridge_main()


if __name__ == '__main__':
    sys.exit(main())
