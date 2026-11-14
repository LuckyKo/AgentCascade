"""``memory_stats.json`` sidecar — per-lesson read counts, atomic best-effort.

The sidecar lives at ``<vault_root>/memory_stats.json`` and maps a vault-relative
path to a ``{"reads": int, "hints": int}`` map (BUG_0317 — ``hints`` was added for
the memory-hint Thompson-sampling bandit). ``load_stats`` is backward-tolerant:
pre-BUG_0317 sidecars that hold a bare ``int`` per path are migrated on read to
``{"reads": <int>, "hints": 0}``. It is *diagnostic only*: any failure (I/O, JSON)
is swallowed so stats can never affect the agent (plan §3.4 / R4).

Concurrency: a module-level lock guards the load-modify-write cycle, and the
write goes through ``agent_cascade.utils.atomic_json.atomic_write_json``
(tmp file + ``os.replace``, atomic on both POSIX and Windows), so a torn
read is impossible.
"""

import json
import threading
from pathlib import Path
from typing import Dict, Optional

from agent_cascade.log import logger
from agent_cascade.utils.atomic_json import atomic_write_json

STATS_FILENAME = 'memory_stats.json'

# Guards the load-modify-write cycle across concurrent read_file calls (plan §3.4).
_stats_lock = threading.Lock()


def _stats_path(vault_root) -> Path:
    return Path(vault_root) / STATS_FILENAME


def _normalize_entry(v) -> Optional[Dict[str, int]]:
    """Normalise one raw sidecar value to ``{"reads": int, "hints": int}``.

    Backward-tolerant (BUG_0317): a legacy bare ``int`` migrates to
    ``{"reads": <int>, "hints": 0}``; a dict is read field-tolerantly. Returns
    ``None`` for an unrecoverable value (caller drops the key).
    """
    if isinstance(v, dict):
        try:
            return {'reads': int(v.get('reads', 0) or 0),
                    'hints': int(v.get('hints', 0) or 0)}
        except (ValueError, TypeError):
            return None
    try:
        return {'reads': int(v), 'hints': 0}
    except (ValueError, TypeError):
        return None


def load_stats(vault_root) -> Dict[str, Dict[str, int]]:
    """Load the sidecar for ``vault_root``. Returns ``{}`` on any failure.

    Each value is normalised to ``{"reads": int, "hints": int}`` (see
    :func:`_normalize_entry`); legacy bare-int values are migrated on read.
    """
    path = _stats_path(vault_root)
    try:
        if not path.exists():
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Dict[str, int]] = {}
        for k, v in data.items():
            entry = _normalize_entry(v)
            if entry is not None:
                out[str(k)] = entry
        return out
    except Exception as e:  # noqa: BLE001 — diagnostic only, never raise
        logger.debug('[MEMORY_HINT] load_stats failed for %s: %s', path, e)
        return {}


def _bump_field(vault_root, rel_path: str, field: str) -> None:
    """Increment ``field`` ('reads' or 'hints') for ``rel_path`` under ``vault_root``.

    ONE load-modify-write cycle under the single module-level ``_stats_lock`` and a
    single ``atomic_write_json`` — so a read bump and a hint bump for the same lesson
    are serialized and can never interleave a torn write (the consistency property
    behind the ``reads``/``hints`` bandit ratio). Best-effort: any exception is
    swallowed (diagnostic only, never affects the agent).
    """
    if not rel_path:
        return
    path = _stats_path(vault_root)
    with _stats_lock:
        try:
            data = load_stats(vault_root)
            entry = data.setdefault(rel_path, {'reads': 0, 'hints': 0})
            try:
                entry[field] = int(entry.get(field, 0) or 0) + 1
            except (TypeError, ValueError):
                entry[field] = 1
            atomic_write_json(path, data, backup=False)
        except Exception as e:  # noqa: BLE001 — diagnostic only, never raise
            logger.debug('[MEMORY_HINT] _bump_field(%s) failed for %s: %s', field, rel_path, e)


def bump_read_count(vault_root, rel_path: str) -> None:
    """Increment the read count for ``rel_path`` under ``vault_root`` (atomic).

    Best-effort: any exception is swallowed. Called from the ReadFile hook every
    time a lesson is read.
    """
    _bump_field(vault_root, rel_path, 'reads')


def bump_hint_count(vault_root, rel_path: str) -> None:
    """Increment the hint-render count for ``rel_path`` under ``vault_root`` (atomic).

    Shares :func:`_bump_field` (and therefore the same ``_stats_lock`` + atomic
    write) with :func:`bump_read_count`, so ``reads``/``hints`` for one lesson stay
    consistent. Best-effort. Only invoked when the memory bandit is ON
    (``MEMORY_BANDIT_WEIGHT > 0.0``) so the default-OFF deploy writes no new field.
    """
    _bump_field(vault_root, rel_path, 'hints')
