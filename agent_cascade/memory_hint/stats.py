"""``memory_stats.json`` sidecar — per-lesson read counts, atomic best-effort.

The sidecar lives at ``<vault_root>/memory_stats.json`` and maps a vault-relative
path to an integer load count. It is *diagnostic only*: any failure (I/O, JSON)
is swallowed so stats can never affect the agent (plan §3.4 / R4).

Concurrency: a module-level lock guards the load-modify-write cycle, and the
write is atomic via ``os.replace`` (atomic on both POSIX and Windows), so a torn
read is impossible.
"""

import json
import os
import threading
from pathlib import Path
from typing import Dict

from agent_cascade.log import logger

STATS_FILENAME = 'memory_stats.json'

# Guards the load-modify-write cycle across concurrent read_file calls (plan §3.4).
_stats_lock = threading.Lock()


def _stats_path(vault_root) -> Path:
    return Path(vault_root) / STATS_FILENAME


def load_stats(vault_root) -> Dict[str, int]:
    """Load the sidecar for ``vault_root``. Returns ``{}`` on any failure."""
    path = _stats_path(vault_root)
    try:
        if not path.exists():
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out: Dict[str, int] = {}
        for k, v in data.items():
            try:
                out[str(k)] = int(v)
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:  # noqa: BLE001 — diagnostic only, never raise
        logger.debug('[MEMORY_HINT] load_stats failed for %s: %s', path, e)
        return {}


def bump_read_count(vault_root, rel_path: str) -> None:
    """Increment the read count for ``rel_path`` under ``vault_root`` (atomic).

    Best-effort: any exception is swallowed. Called from the ReadFile hook every
    time a lesson is read.
    """
    if not rel_path:
        return
    path = _stats_path(vault_root)
    with _stats_lock:
        try:
            data = load_stats(vault_root)
            data[rel_path] = data.get(rel_path, 0) + 1
            tmp = path.with_name(STATS_FILENAME + '.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001 — diagnostic only, never raise
            logger.debug('[MEMORY_HINT] bump_read_count failed for %s: %s', rel_path, e)
