"""
Minimal loader for non-LLM secrets stored in config/secrets.json.

Expected structure of config/secrets.json (example, DO NOT commit real keys):
{
  "serper_api_key": "your-serper-api-key-here"
}

This file is gitignored. If it does not exist, all lookups return None.
"""

import json
import os
from typing import Any, Optional

_SECRETS_CACHE: Optional[dict] = None


def _load_secrets() -> dict:
    """Load secrets from config/secrets.json once and cache in memory."""
    global _SECRETS_CACHE
    if _SECRETS_CACHE is not None:
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
        # No secrets file is allowed; behave as empty config.
        pass
    except json.JSONDecodeError:
        # If invalid JSON, treat as empty to avoid crashing.
        _SECRETS_CACHE = {}

    return _SECRETS_CACHE


def get_secret(name: str) -> Optional[Any]:
    """
    Get a secret value by key from config/secrets.json.

    Returns None if the file does not exist or the key is not present.
    """
    secrets = _load_secrets()
    return secrets.get(name)