"""BUG_0435 — append-only audit trail for the secure remote-control gate.

One JSON line per authenticated mutation and per failed access attempt (401 /
403 / 429 / 5xx), written to ``<workspace>/audit/audit_<instance>.jsonl``.
Mirrors the telemetry file convention (dir creation, ``.lock`` sidecar, in-process
serialization) so ops tooling can treat both the same. The raw API key is NEVER
written — only its sha256 ``key_id`` prefix (computed by :mod:`agent_cascade.auth`).

Schema (one object per line, per plan §6):
    {"ts", "key_id", "role", "ip", "method", "route", "action", "result", "dur_ms"}

Writes are best-effort and never raise into the request path; a logging failure is
warned-once and degrades (the gate's decision has already been made).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from agent_cascade import settings

_logger = logging.getLogger('agent_cascade.audit')


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class AuditLogger:
    """Append-only JSONL audit recorder (thread-safe, best-effort)."""

    def __init__(self, base_dir: Optional[str] = None, instance_id: str = ''):
        self.enabled = settings.AUDIT_ENABLED
        if base_dir is None:
            base_dir = os.path.join(settings.DEFAULT_WORKSPACE, 'audit')
        self.base_dir = Path(base_dir)

        if not instance_id:
            try:
                from agent_cascade.instance_id import get_instance_id
                instance_id = get_instance_id() or 'default'
            except Exception:
                instance_id = 'default'
        self.instance_id = instance_id or 'default'

        self._lock = threading.Lock()
        self._log_path: Optional[Path] = None
        self._dir_ready = False
        self._warned = False
        # Retention is a module-import-time constant; read it once.
        self._retention_days = settings.AUDIT_RETENTION_DAYS

    def _ensure_dir(self) -> bool:
        """Create ``<workspace>/audit/`` (like telemetry.py) and prune expired files.

        Returns True when the directory is usable. Never raises.
        """
        if self._dir_ready:
            return True
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self._prune_expired()
            self._dir_ready = True
        except OSError as e:
            self._warn_once(f'audit dir unusable ({e}); audit disabled for this process')
            self.enabled = False
        return self._dir_ready

    def _prune_expired(self) -> None:
        """Delete audit lines older than ``AGENT_CASCADE_AUDIT_RETENTION_DAYS``.

        Best-effort: silently skips unreadable mtimes. Keeps the floor of
        "append-only + retention" from plan §6 without a background scheduler.
        """
        if self._retention_days <= 0:
            return
        cutoff = time.time() - self._retention_days * 86400
        for p in self.base_dir.glob('audit_*.jsonl*'):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue

    def _acquire_lock(self) -> Optional[Path]:
        """Best-effort ``.lock`` sidecar (same discipline as telemetry.py:400).

        Records holder PID with ``O_CREAT | O_EXCL``. In-process writes are
        already serialized by ``self._lock``; the sidecar is a cross-process
        single-writer hint. We still write on contention (audit must not be lost
        for a security event); the file is append-only so lines cannot corrupt
        each other. Never raises.
        """
        if self._log_path is None:
            self._log_path = self.base_dir / f'audit_{self.instance_id}.jsonl'
        lock_path = Path(str(self._log_path) + '.lock')
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps({'pid': os.getpid(), 'ts': time.time()}).encode('utf-8'))
            finally:
                os.close(fd)
        except FileExistsError:
            pass  # another writer holds it; append anyway
        except OSError:
            pass
        return self._log_path

    def record(self, *, method: str, route: str, result: int, key_id: str,
               role: str, ip: str, dur_ms: int = 0) -> None:
        """Append one audit line. Best-effort: never raises into the request path.

        Call from :func:`asyncio.to_thread` in the middleware so disk I/O does not
        block the event loop.
        """
        if not self.enabled:
            return
        with self._lock:
            if not self._ensure_dir():
                return
            line = {
                'ts': _now_iso(),
                'key_id': key_id,
                'role': role,
                'ip': ip,
                'method': method.upper(),
                'route': route,
                'action': _route_action(route),
                'result': result,
                'dur_ms': dur_ms,
            }
            try:
                path = self._acquire_lock()
                # O_APPEND + a single small write; lines never interleave within
                # a process (self._lock) and are atomic for typical line sizes.
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, (json.dumps(line, ensure_ascii=False) + '\n').encode('utf-8'))
                finally:
                    os.close(fd)
            except OSError as e:
                self._warn_once(f'audit write failed: {e}')

    def _warn_once(self, msg: str) -> None:
        if not self._warned:
            _logger.warning('Audit logging: %s', msg)
            self._warned = True


def _route_action(route: str) -> str:
    """Derive a short action label from the route (last two path segments)."""
    parts = [p for p in route.strip('/').split('/') if p]
    if not parts:
        return 'root'
    return '/'.join(parts[-2:]) if len(parts) >= 2 else parts[-1]


# Process-wide singleton (created lazily so import never touches the filesystem).
_AUDIT_LOGGER: Optional[AuditLogger] = None
_AUDIT_LOCK = threading.Lock()


def get_audit_logger() -> AuditLogger:
    """Return the process-wide :class:`AuditLogger` (created on first use)."""
    global _AUDIT_LOGGER
    if _AUDIT_LOGGER is None:
        with _AUDIT_LOCK:
            if _AUDIT_LOGGER is None:
                _AUDIT_LOGGER = AuditLogger()
    return _AUDIT_LOGGER
