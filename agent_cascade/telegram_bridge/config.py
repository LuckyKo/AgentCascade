"""Configuration loading for the v1 Telegram bridge.

Secrets (bot token) come from AC's existing ``config/secrets.json`` via
``config.secrets_loader.get_secret`` (gitignored), with an env-var fallback.
All non-secret settings are plain env vars so a standalone process needs no
extra machinery.

Env keys (see plan §8 / V1 SCOPE ADDENDUM):
    TG_BRIDGE_ENABLED      bool   default False  (master switch)
    TELEGRAM_BOT_TOKEN     str    secret; secrets.json key ``telegram_bot_token`` first, else env
    ALLOWED_USERS          str    comma-separated Telegram user IDs (single id in v1)
    AC_BASE_URL            str    default http://127.0.0.1:12345  (port MUST be configurable)
    TG_TARGET_AGENT        str    default Maine (root/orchestrator)
    TG_POLL_INTERVAL_SEC   float  default 2.5   (/api/status poll cadence)
    TG_TASK_TIMEOUT_SEC    int    default 28800 (per-task wait window; timeout is non-fatal, see below)
    AGENT_CASCADE_TG_TASK_WAIT_CEILING_SEC float default 86400 (outer ceiling after 'still working' notices; 24h)

A task timeout no longer abandons delivery: the waiter sends a "⏳ Still working"
notice and keeps polling until AC finishes or the ceiling is reached.
"""

import os
from dataclasses import dataclass, field
from typing import List

from agent_cascade.settings import (
    TG_POLL_INTERVAL_SEC,
    TG_TASK_TIMEOUT_SEC,
    TG_TASK_WAIT_CEILING_SEC,
)


def _load_bot_token() -> str:
    """Return the Telegram bot token from secrets.json, else env, else ''."""
    try:
        from config.secrets_loader import get_secret
        val = get_secret('telegram_bot_token')
        if isinstance(val, str) and val.strip():
            return val.strip()
    except Exception:
        # config package unavailable (e.g. running outside the repo) -> fall through to env
        pass
    return os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()


def _load_allowed_users_raw() -> str:
    """Return the raw ALLOWED_USERS string from secrets.json, else env, else ''.

    Mirrors ``_load_bot_token`` so the AC-owned supervisor path (which spawns the
    child WITHOUT an ALLOWED_USERS env var) still resolves the allowlist from
    config/secrets.json. The value is a comma-separated list of Telegram user ids.
    """
    try:
        from config.secrets_loader import get_secret
        val = get_secret('telegram_allowed_users')
        if isinstance(val, str) and val.strip():
            return val.strip()
        # Also accept a JSON list stored under the same key.
        if isinstance(val, (list, tuple)):
            return ','.join(str(v).strip() for v in val if str(v).strip())
    except Exception:
        pass
    return os.environ.get('ALLOWED_USERS', '').strip()


def _parse_bool(raw: str, default: bool = False) -> bool:
    if raw is None or raw == '':
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _parse_allowed_users(raw: str) -> List[int]:
    """Parse a comma-separated list of Telegram user IDs into ints."""
    ids: List[int] = []
    for part in (raw or '').replace(';', ',').split(','):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            # Ignore non-numeric tokens rather than crashing startup.
            pass
    return ids


@dataclass
class BridgeConfig:
    """Runtime configuration for the bridge process."""

    enabled: bool = False
    bot_token: str = ''
    allowed_users: List[int] = field(default_factory=list)
    ac_base_url: str = 'http://127.0.0.1:12345'
    target_agent: str = 'Maine'
    poll_interval_sec: float = TG_POLL_INTERVAL_SEC
    task_timeout_sec: int = TG_TASK_TIMEOUT_SEC
    task_wait_ceiling_sec: float = TG_TASK_WAIT_CEILING_SEC

    def is_allowed(self, user_id) -> bool:
        try:
            return int(user_id) in self.allowed_users
        except (TypeError, ValueError):
            return False


def load_config() -> BridgeConfig:
    """Build a BridgeConfig from secrets.json + environment variables."""
    return BridgeConfig(
        enabled=_parse_bool(os.environ.get('TG_BRIDGE_ENABLED'), default=False),
        bot_token=_load_bot_token(),
        allowed_users=_parse_allowed_users(_load_allowed_users_raw()),
        ac_base_url=os.environ.get('AC_BASE_URL', 'http://127.0.0.1:12345').rstrip('/'),
        target_agent=os.environ.get('TG_TARGET_AGENT', 'Maine') or 'Maine',
        # Existing user-facing env vars (unprefixed) still win; the settings constants
        # are only the fallback defaults. An empty/absent env value falls back to the
        # typed constant via `or`.
        poll_interval_sec=float(os.environ.get('TG_POLL_INTERVAL_SEC') or TG_POLL_INTERVAL_SEC),
        task_timeout_sec=int(os.environ.get('TG_TASK_TIMEOUT_SEC') or TG_TASK_TIMEOUT_SEC),
        # Newer knob: no legacy env var exists, so the AGENT_CASCADE_-prefixed name is
        # the only one (the settings constant already reads it).
        task_wait_ceiling_sec=float(
            os.environ.get('AGENT_CASCADE_TG_TASK_WAIT_CEILING_SEC') or TG_TASK_WAIT_CEILING_SEC),
    )


def validate_config(cfg: BridgeConfig) -> List[str]:
    """Return a list of human-readable problems (empty list == OK to start)."""
    problems: List[str] = []
    if not cfg.bot_token:
        problems.append(
            "TELEGRAM_BOT_TOKEN is empty. Set the 'telegram_bot_token' key in "
            'config/secrets.json or the TELEGRAM_BOT_TOKEN env var.'
        )
    if not cfg.allowed_users:
        problems.append('ALLOWED_USERS is empty. Set at least one Telegram user id.')
    return problems
