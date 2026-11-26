"""
Log/telemetry retention for AgentCascade.

Bounds the unbounded growth of two top-level log corpora under the workspace:
  - ``<workspace>/logs/*.jsonl``      — per-agent session logs
  - ``<workspace>/telemetry/*.jsonl`` — ``telemetry_<session>.jsonl`` (append-only,
    written by ``telemetry.py``; this is the bulk of the growth)

Mirrors the media-cleanup scheduler in ``utils/media_utils.py``: a background daemon
thread sweeps on an interval, either gzipping stale logs into that root's ``archive/``
subdir (archive-first, reversible) or deleting them outright.

The sweep never touches a file that is still in active use: recent mtime is treated as
"still being written", and a sibling ``<name>.jsonl.lock`` (the cross-process telemetry
lock written by ``telemetry.py``) means another writer may still hold the file open.
"""

import gzip
import shutil
import threading
import time
from pathlib import Path

from agent_cascade.instance_id import make_instance_dir
from agent_cascade.settings import DEFAULT_WORKSPACE


def _default_sweep_roots() -> list[Path]:
    """Return the default top-level directories to sweep: session logs + telemetry.

    Agent session ``*.jsonl`` logs live under ``<workspace>/logs`` and
    ``telemetry_*.jsonl`` live under ``<workspace>/telemetry`` (``telemetry.py`` writes
    them there; see ``shared_init.py``). Both roots are made instance-aware to match how
    the writers isolate per-instance data — a no-op in single-instance mode.
    """
    base = Path(DEFAULT_WORKSPACE)
    return [
        Path(make_instance_dir(str(base / 'logs'))),
        Path(make_instance_dir(str(base / 'telemetry'))),
    ]


def _sweep_root(
    base: Path,
    result: dict,
    retention_days: int,
    archive: bool,
) -> None:
    """Archive/delete stale top-level ``*.jsonl`` files in one root, accumulating into result.

    Only the top level of ``base`` is scanned (no recursion), so previously archived
    files under ``base/archive/`` and unrelated subdirs (``backups/``, ``spillover/``,
    ``debug/``) are never re-processed. Per-file failures are collected into
    ``result['errors']`` and never abort the sweep.
    """
    cutoff = time.time() - retention_days * 86400
    archive_dir = base / 'archive'
    if archive:
        archive_dir.mkdir(parents=True, exist_ok=True)

    for f in sorted(base.glob('*.jsonl')):
        try:
            if not f.is_file():
                continue
            st = f.stat()
            if st.st_mtime >= cutoff:
                continue  # fresh file — likely still being written
            if Path(str(f) + '.lock').exists():
                continue  # a sibling lock implies a possibly active writer

            bytes_freed = st.st_size
            if archive:
                dst = archive_dir / (f.name + '.gz')
                if dst.exists():  # a recurring id must not clobber an earlier archive
                    dst = archive_dir / (f.name + f".{int(time.time())}.gz")
                with gzip.open(dst, 'wb') as gz, open(f, 'rb') as src:
                    shutil.copyfileobj(src, gz)
                f.unlink()
                result['archived'] += 1
            else:
                f.unlink()
                result['deleted'] += 1
            result['bytes_freed'] += bytes_freed
        except OSError as e:
            result['errors'].append(f"Failed to process {f}: {e}")
            continue  # one bad file must never abort the whole sweep


def archive_old_logs(
    retention_days: int,
    archive: bool = True,
    log_dir: Path | str | None = None,
) -> dict:
    """Archive (or delete) top-level ``*.jsonl`` logs older than ``retention_days``.

    When ``log_dir`` is None the sweep covers both default roots — the session-logs
    directory and the telemetry directory (see :func:`_default_sweep_roots`). When an
    explicit ``log_dir`` is given it is swept as a single root (used by tests and
    ad-hoc callers). Only the top level of each root is scanned, so archives and
    unrelated subdirs are never re-processed.

    A file is skipped when either:
      - its mtime is at/after the cutoff (fresh, likely still being written), or
      - a sibling ``<file>.lock`` exists (an active telemetry writer may still hold it).

    Args:
        retention_days: Logs modified more than this many days ago are archived/deleted.
            ``<= 0`` disables the sweep entirely (no-op).
        archive: True -> gzip the stale file into ``<root>/archive/<name>.jsonl.gz`` and
            remove the original; False -> delete the stale file outright.
        log_dir: Single directory to sweep. When None, the default logs + telemetry roots
            are swept; defaults derive from ``DEFAULT_WORKSPACE``.

    Returns:
        Dict with keys ``'archived'`` (int), ``'deleted'`` (int), ``'bytes_freed'``
        (int) and ``'errors'`` (list of str), aggregated across all swept roots. When
        disabled, also includes ``'disabled': True``. ``bytes_freed`` counts the
        original on-disk bytes removed from the root (gross, not the smaller .gz).
    """
    if retention_days <= 0:
        return {
            'archived': 0,
            'deleted': 0,
            'bytes_freed': 0,
            'errors': [],
            'disabled': True,
        }

    result = {'archived': 0, 'deleted': 0, 'bytes_freed': 0, 'errors': []}
    roots = [Path(log_dir)] if log_dir is not None else _default_sweep_roots()
    for base in roots:
        if base.exists():
            _sweep_root(base, result, retention_days, archive)

    return result


def _log_retention_worker(
    retention_days: int,
    interval_hours: float,
    archive: bool,
) -> None:
    """Background thread loop that periodically runs :func:`archive_old_logs`.

    Sleeps first (so a server boot does not sweep immediately), then sweeps on every
    tick. A failing tick is logged and swallowed; it never raises out of the thread.
    """
    interval_seconds = max(60.0, interval_hours * 3600)  # floor at 1 min: a 0/negative env must not busy-loop
    while True:
        time.sleep(interval_seconds)
        try:
            result = archive_old_logs(retention_days=retention_days, archive=archive)
            if result['archived'] + result['deleted'] > 0:
                from agent_cascade.log import logger
                logger.info(f"Log retention: archived {result['archived']} files, "
                            f"deleted {result['deleted']} files, "
                            f"freed {result['bytes_freed'] / (1024 * 1024):.1f} MB")
            if result['errors']:
                from agent_cascade.log import logger
                for err in result['errors']:
                    logger.warning(f"Log retention error: {err}")
        except Exception as e:
            from agent_cascade.log import logger
            logger.error(f"Log retention failed: {e}")


def start_log_retention_scheduler(
    retention_days: int,
    interval_hours: float,
    archive: bool,
) -> threading.Thread:
    """Start a background daemon thread that periodically sweeps old logs.

    Args:
        retention_days: Logs older than this many days are archived/deleted.
        interval_hours: How often to run the sweep.
        archive: True -> gzip stale logs into ``<root>/archive/``; False -> delete them.

    Returns:
        The started Thread object (daemon=True).

    Raises:
        RuntimeError: If the thread cannot be started. The caller treats this as
            non-critical (a failed scheduler must never take the server down).
    """
    try:
        thread = threading.Thread(
            target=_log_retention_worker,
            args=(retention_days, interval_hours, archive),
            name='log-retention-scheduler',
            daemon=True,
        )
        thread.start()
        return thread
    except Exception as e:
        raise RuntimeError(f"Failed to start log retention scheduler: {e}") from e
