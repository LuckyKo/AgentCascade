"""BUG_0036 — EBADF-tolerant proxy for the terminal reporter's file sink.

Background (see .agent_lessons / BUG_0036 plan): under pytest-xdist on Windows,
the xdist CONTROLLER's stdout is a real tty, so ``TerminalWriter`` wraps it in
colorama.  An environmental trigger closes/detaches that console handle at some
point during the run (upstream pytest-dev/pytest#10434 — still open).  pytest 9's
``TerminalProgressPlugin`` then forces ``write_raw(..., flush=True)`` on EVERY
test report, so thousands of writes hit the dead handle and the whole session
dies with ``INTERNALERROR: OSError: [Errno 9] Bad file descriptor``.

colorama is a pass-through: it only performs the write/flush that exposes the
already-dead handle, it cannot create the EBADF itself.  We therefore do NOT try
to fix "who closes the handle" (out of scope); we make pytest's own terminal sink
TOLERANT so any progress/result write survives a dead handle:

- Only ``OSError`` with errno in {EBADF(9), EINVAL(22), EPIPE(32)} is swallowed.
  Every other exception still propagates, so genuine I/O problems are not masked.
- Everything else (``isatty``, ``fileno``, ``encoding``, ``mode``, ``closed``, …)
  delegates to the wrapped file via ``__getattr__``, so behavior is otherwise
  identical and colour output is preserved.

Tests never write through this sink (they use their own capture), so tolerating
errors here only affects pytest's OWN terminal writes.
"""

import errno


# Errnos that indicate a dead/detached console handle rather than a real I/O fault.
_TOLERATED_ERRNOS = {errno.EBADF, errno.EINVAL, errno.EPIPE}


def _is_dead_handle_error(exc: BaseException) -> bool:
    """True if *exc* is an OSError caused by a dead/detached file handle."""
    return isinstance(exc, OSError) and exc.errno in _TOLERATED_ERRNOS


class _TolerantFileProxy:
    """Wraps a real file object and tolerates dead-handle errors on write/flush.

    ``write`` / ``writelines`` / ``flush`` swallow only EBADF/EINVAL/EPIPE
    OSErrors (a closed or detached console handle); all other exceptions are
    re-raised unchanged.  Any other attribute access delegates to the wrapped
    file so the proxy is transparent to pytest's terminal writer.
    """

    def __init__(self, real_file):
        self._real = real_file

    # -- write path: tolerate dead-handle errors only -----------------------
    def write(self, data):
        try:
            return self._real.write(data)
        except OSError as exc:
            if not _is_dead_handle_error(exc):
                raise
            return 0

    def writelines(self, lines):
        try:
            return self._real.writelines(lines)
        except OSError as exc:
            if not _is_dead_handle_error(exc):
                raise

    def flush(self):
        try:
            return self._real.flush()
        except OSError as exc:
            if not _is_dead_handle_error(exc):
                raise

    # -- everything else: transparent delegation ----------------------------
    def __getattr__(self, name):
        return getattr(self._real, name)


def install_tolerant_terminal_sink(config) -> None:
    """Replace the terminal reporter's file sink with an EBADF-tolerant proxy.

    Called from ``pytest_configure`` (controller and every xdist worker).  Harmless
    in workers, where the sink is already a non-tty capture file — it only adds
    tolerance there.  Fully defensive: any pytest internal change must never break
    collection, so the whole thing is wrapped in try/except.
    """
    try:
        tr = config.pluginmanager.get_plugin('terminalreporter')
        tw = getattr(tr, '_tw', None)
        real = getattr(tw, '_file', None) if tw is not None else None
        # Only wrap a genuine file-like object (defensive against internal changes).
        if real is not None and hasattr(real, 'flush') and not isinstance(real, _TolerantFileProxy):
            tw._file = _TolerantFileProxy(real)
    except Exception:
        pass
