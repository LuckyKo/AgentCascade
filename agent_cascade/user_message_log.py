"""Append-only log of every message pushed to the human user's "Agent Messages" tab.

The UI's Agent Messages tab is populated by exactly one WS frame,
``agent_message_to_user``, whose single server-side producer is
``tools/custom/send_message.py::_send_to_user``.  This module persists a JSONL
record for each such message so there is a durable, grep-able audit trail that
survives the in-memory conversation record (which is rewritten on compression /
session restore).

Design constraints (see plans/t154_log_user_messages_PLAN.md):
- Instance-isolated directory via ``instance_id.get_session_log_dir`` — never
  re-derive the path here.
- One file per process: ``user_messages_<YYYYmmdd_HHMMSS>.jsonl``; the timestamp
  is computed lazily on first write so all entries of a run share a file.
- Thread-safe (single module-level lock around open+write+flush).
- **Never raises**: any failure is swallowed to a DEBUG log line tagged
  "user_message_log write failed" so a logging problem can never break an agent
  turn, and future triage can grep for that exact tag.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Cap on stored content; tool-argument blobs can be megabytes and would bloat the log.
_MAX_CONTENT_BYTES = 64 * 1024  # 64 KB

# Module-level, process-wide state (guarded by _LOCK).
_LOCK = threading.Lock()
_FILE_STAMP: Optional[str] = None  # lazily computed on first write; shared for the whole run
_SEQ = 0


def _utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with a 'Z' suffix."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _resolve_file_path(agent_pool) -> Path:
    """Resolve the instance-isolated user-message log file path.

    Must be called while holding ``_LOCK`` (it reads/mutates ``_FILE_STAMP``).
    """
    global _FILE_STAMP
    from agent_cascade.instance_id import get_session_log_dir  # lazy to avoid import cycle
    log_dir = get_session_log_dir(agent_pool)
    if _FILE_STAMP is None:
        _FILE_STAMP = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    return Path(log_dir) / f'user_messages_{_FILE_STAMP}.jsonl'


def log_user_message(
    agent_pool,
    *,
    content: str,
    channel: str = 'ui',
    source_instance: Optional[str] = None,
    kind: str = 'agent_message',
    delivered: bool = True,
) -> None:
    """Append one JSON record for a message sent to the user.

    This function **never raises** — any exception is logged at DEBUG and
    swallowed so that a logging failure cannot break the calling turn.

    Args:
        agent_pool: The AgentPool (used to resolve the instance-aware log dir).
            May be None or lack ``operation_manager``; ``get_session_log_dir``
            handles both by falling back to DEFAULT_WORKSPACE.
        content: Message text (truncated to 64 KB with a ``truncated`` flag).
        channel: Delivery channel — 'ui' for the browser Agent Messages tab.
        source_instance: The agent instance that produced the message, or None.
        kind: Record kind; currently always 'agent_message' (kept for forward compat).
        delivered: Whether the message actually reached a WS client. False when
            the WebSocket was unavailable (intent logged, delivery failed).
    """
    try:
        # Settings gate — default ON; return silently when disabled.
        settings = getattr(agent_pool, 'settings', None)
        if settings is not None and not getattr(settings, 'user_message_log_enabled', True):
            return

        global _SEQ
        with _LOCK:
            path = _resolve_file_path(agent_pool)
            path.parent.mkdir(parents=True, exist_ok=True)

            _SEQ += 1
            seq = _SEQ

            text = content if isinstance(content, str) else str(content)
            truncated = False
            encoded = text.encode('utf-8', errors='replace')
            if len(encoded) > _MAX_CONTENT_BYTES:
                # Truncate on a byte boundary without splitting a multi-byte char.
                clipped = encoded[:_MAX_CONTENT_BYTES]
                text = clipped.decode('utf-8', errors='ignore')
                truncated = True

            record = {
                'ts': _utc_now_iso(),
                'seq': seq,
                'channel': channel,
                'kind': kind,
                'instance': source_instance or 'unknown',
                'content': text,
                'delivered': bool(delivered),
                'truncated': truncated,
            }
            line = json.dumps(record, ensure_ascii=False) + '\n'

            with open(path, 'a', encoding='utf-8') as f:
                f.write(line)
                f.flush()
    except Exception:  # noqa: BLE001 - best-effort; a logging failure must never break the turn
        logger.debug('user_message_log write failed (non-fatal): %s', repr(content)[:200], exc_info=True)


def _reset_for_tests() -> None:
    """Reset module-level state. Test-only helper — not for production use."""
    global _FILE_STAMP, _SEQ
    with _LOCK:
        _FILE_STAMP = None
        _SEQ = 0
