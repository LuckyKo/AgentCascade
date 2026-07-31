"""
Minimal loader for non-LLM secrets stored in config/secrets.json.

Expected structure of config/secrets.json (example, DO NOT commit real keys):
{
  "serper_api_key": "your-serper-api-key-here"
}

This file is gitignored. If it does not exist, all lookups return None.
"""

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SECRETS_CACHE: dict = {}
_SECRETS_LOADED: bool = False

_DEFAULT_SECRETS = {
    "serper_api_key": "",
    "search_backend_priority": ["serper", "duckduckgo"],
}


def _load_secrets() -> dict:
    """Load secrets from config/secrets.json once and cache in memory.

    If the file does not exist, it is created with safe default values and a
    warning is logged so the user knows to configure their API keys.
    """
    global _SECRETS_CACHE, _SECRETS_LOADED
    if _SECRETS_LOADED:
        return _SECRETS_CACHE

    # config/ is a package root; resolve relative to this file's directory.
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, "secrets.json")

    _SECRETS_CACHE = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                _SECRETS_CACHE = data
    except FileNotFoundError:
        # Auto-create with safe defaults on first startup.
        try:
            os.makedirs(base_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_DEFAULT_SECRETS, f, indent=2)
            logger.warning(
                "config/secrets.json not found; created with default values. "
                "Please set your API keys."
            )
        except OSError as e:
            logger.error(f"Failed to create config/secrets.json: {e}")
    except json.JSONDecodeError:
        # If invalid JSON, treat as empty to avoid crashing.
        _SECRETS_CACHE = {}

    _SECRETS_LOADED = True
    return _SECRETS_CACHE


def get_secret(name: str) -> Optional[Any]:
    """
    Get a secret value by key from config/secrets.json.

    Returns None if the file does not exist or the key is not present.
    """
    secrets = _load_secrets()
    return secrets.get(name)