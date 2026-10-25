# Copyright 2023 The Qwen team, Alibaba Group. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import logging.handlers
import os
import shutil
import sys
import threading
from pathlib import Path


# Lazy import of get_instance_id to avoid circular dependency:
# __init__.py → agent.py → log.py → instance_id.py would deadlock when
# importing via 'import agent_cascade.instance_id' (package init runs first).
def _get_instance_id():
    from agent_cascade.instance_id import get_instance_id
    return get_instance_id()


class _WindowsSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    RotatingFileHandler that uses shutil.move() instead of os.rename() for file rotation.

    On Windows, os.rename() fails with PermissionError when the target file is open
    in another process (e.g., editors, file watchers, antivirus). shutil.move() handles
    this more gracefully by falling back to copy+delete.

    Also suppresses errors during rotation rather than letting them cascade into
    infinite logging recursion.
    """

    def rotate(self, source, dest):
        try:
            if os.path.exists(dest):
                os.remove(dest)
            shutil.move(source, dest)
        except OSError as e:
            # Don't use logger here - we're inside emit() already, calling logger.warning()
            # would cause recursion/nested emit on the same handler -> "Logging error" messages.
            # Write directly to stderr for observability without recursion risk.
            try:
                sys.__stderr__.write(f"[LOG ROTATION FAILED] {source} -> {dest}: {e}\n")
                sys.__stderr__.flush()
            except Exception:
                pass  # Silently ignore - rotation will retry next time file size threshold is hit


class _SafeStreamHandler(logging.StreamHandler):
    """
    StreamHandler that tolerates a closed/broken target stream without printing
    "--- Logging error ---" tracebacks to stderr.

    The default StreamHandler.emit() catches write failures (e.g. "ValueError: I/O
    operation on closed file") and delegates to Handler.handleError(), which prints a
    full traceback + call stack to stderr whenever logging.raiseExceptions is True
    (the default). This fires at teardown — after pytest closes its captured stdout, or
    during interpreter shutdown — when a record is emitted to an already-closed stream.

    Such failures are benign: the message simply can't reach the console. Override
    handleError() to suppress that noise. Normal logging behavior is unchanged; only the
    error-reporting for a dead console stream is silenced (the message was unreachable
    anyway). NOTE: wrapping emit() in try/except does NOT work here — StreamHandler.emit
    never re-raises, it calls handleError() internally.
    """

    def handleError(self, record):
        # Suppress the "--- Logging error ---" stderr dump ONLY for a dead/broken console
        # stream (closed stdout at teardown / broken pipe) — the benign case. Any other
        # error type (formatter bug, encoding problem, genuine I/O failure) is passed to
        # the base implementation so it still gets reported and isn't silently masked.
        _, exc, _ = sys.exc_info()
        if isinstance(exc, (ValueError, OSError)):
            return
        super().handleError(record)


class _CapturingStream:
    """
    Replacement for sys.stdout / sys.stderr that routes every write()
    through the logger so it ends up in console.log.

    Writes are logged at INFO level (stdout) or WARNING level (stderr).
    A per-instance threading.RLock allows re-entrant calls (e.g., when logging
    system writes error messages back to stderr during handler failures).
    """

    def __init__(self, stream_type, original_stream):
        self._stream_type = stream_type  # 'stdout' or 'stderr'
        self._original = original_stream
        self._lock = threading.RLock()

    def write(self, msg):
        # Type guard: ensure msg is a string (handles non-string writes gracefully)
        msg = str(msg)
        with self._lock:
            logged = False
            if msg and msg.strip():
                level = logging.INFO if self._stream_type == 'stdout' else logging.WARNING
                try:
                    logger.log(level, msg.rstrip('\n\r'))
                    logged = True
                except Exception:
                    logged = False
            # The logging tree (console handler + rotating file handler) is the
            # ONLY sink. Writing to the original stream as well double-emitted
            # every WARNING+ record. Only bypass it when logging itself failed.
            if not logged and msg and msg.strip():
                try:
                    self._original.write(msg)
                    self._original.flush()
                except (OSError, ValueError):
                    pass

    def flush(self):
        try:
            self._original.flush()
        except (OSError, ValueError):
            pass

    def isatty(self):
        """Delegate to the original stream's isatty, falling back to False."""
        try:
            return self._original.isatty()
        except Exception:
            return False

    def fileno(self):
        """Delegate to the original stream's fileno, falling back gracefully.

        Some third-party libraries (e.g., the MCP SDK stdio transport) call
        fileno() on sys.stdout/sys.stderr. The real underlying stream is a
        file object that supports it; if for any reason it does not, raise
        io.UnsupportedOperation so callers handle it the same way they would
        for a non-socket stream (rather than crashing with AttributeError).
        """
        try:
            return self._original.fileno()
        except Exception:
            import io
            raise io.UnsupportedOperation('fileno') from None


class _AppLogBridgeHandler(logging.Handler):
    """Forward a third-party logger's records into the app logger (BUG_0147).

    Records from loggers that use ``logging.getLogger(__name__)`` (asyncio, httpx, uvicorn,
    MCP, …) have no handler on their own logger and none on root, so ``logging`` sends them
    to ``logging.lastResort``: a WARNING-level ``_StderrHandler`` bound to ``sys.stderr`` at
    emit time. Since ``init_logging()`` replaces ``sys.stderr`` with :class:`_CapturingStream`,
    that turns an asyncio **ERROR** into a nameless **WARNING** attributed to ``log.py`` — and
    silently drops every INFO/DEBUG record.

    This handler re-dispatches the *record object* (not formatted text) to the live app logger,
    so the emitting library's level, ``filename``/``lineno`` and traceback all survive.
    """

    def emit(self, record):
        try:
            # Enforce the app logger's level: Logger.handle() bypasses isEnabledFor(), so a
            # record that passed the source logger's gate must still respect ours.
            if not logger.isEnabledFor(record.levelno):
                return
            logger.handle(record)
        except Exception:  # noqa: BLE001 - logging must never raise into the emitter
            self.handleError(record)


# Profile callback that CPython 3.12 runs on the Windows Proactor loop when a transport dies.
_PROACTOR_TEARDOWN_CALLBACK = '_ProactorBasePipeTransport._call_connection_lost'


class _ProactorTeardownFilter(logging.Filter):
    """Downgrade the benign Windows proactor teardown traceback to one DEBUG line (BUG_0146).

    ``asyncio.proactor_events._ProactorBasePipeTransport._call_connection_lost`` closes the
    socket with an unguarded ``sock.shutdown(SHUT_RDWR)`` inside a ``finally``. When the peer
    has already reset the connection, that raises ``ConnectionResetError`` (WSAECONNRESET 10054)
    out of the callback, and asyncio reports "Exception in callback …" plus a traceback. It
    means only "the other end went away" — most often a browser tab reload dropping its
    WebSocket and idle keep-alive HTTP connections to uvicorn.

    Filtered records are DROPPED (not mutated), and a single DEBUG line is emitted so the event
    stays traceable under ``AGENT_CASCADE_DEBUG=1`` without a 6-line traceback in normal runs.
    Anything else — a different callback, a different exception type, a real asyncio failure —
    passes through untouched.
    """

    _BENIGN = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        if not isinstance(exc, self._BENIGN):
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed record: let the normal path report it
            return True
        if _PROACTOR_TEARDOWN_CALLBACK not in message:
            return True
        try:
            logger.debug('Suppressed benign Windows proactor teardown (%s) on %s - peer closed the socket',
                         type(exc).__name__,
                         _PROACTOR_TEARDOWN_CALLBACK)
        except Exception:  # noqa: BLE001 - never let the downgrade itself break logging
            pass
        return False


_asyncio_bridge_installed = False


def _install_asyncio_log_bridge():
    """Route the ``asyncio`` logger into the app log instead of ``logging.lastResort``.

    Idempotent and lock-free on purpose: it is called from :func:`setup_logger`, which
    ``init_logging()`` already invokes under ``_init_lock``. ``propagate=False`` is set on the
    *asyncio* logger only — the app logger keeps propagating to root so ``caplog`` and any
    root-level handler still see AC's own records.
    """
    global _asyncio_bridge_installed
    asyncio_logger = logging.getLogger('asyncio')
    if _asyncio_bridge_installed or any(isinstance(h, _AppLogBridgeHandler) for h in asyncio_logger.handlers):
        _asyncio_bridge_installed = True
        return
    bridge = _AppLogBridgeHandler()
    bridge.addFilter(_ProactorTeardownFilter())
    asyncio_logger.addHandler(bridge)
    asyncio_logger.propagate = False
    _asyncio_bridge_installed = True


def setup_logger(level=None):
    if level is None:
        if os.getenv('AGENT_CASCADE_DEBUG', '0').strip().lower() in ('1', 'true'):
            level = logging.DEBUG
        else:
            level = logging.INFO

    # Capture original stdout/stderr BEFORE creating the StreamHandler,
    # so the handler writes to the real terminal and NOT our capturing stream
    # (which would cause infinite recursion: write → log → handler → captured stdout → log → ...)
    _original_stdout = sys.stdout
    _original_stderr = sys.stderr

    instance_id = _get_instance_id()

    handler = _SafeStreamHandler(stream=_original_stdout)
    # Do not run handler.setLevel(level) so that users can change the level via logger.setLevel later
    formatter = logging.Formatter('%(asctime)s - %(filename)s - %(lineno)d - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    _logger_name = 'agent_cascade_logger'
    if instance_id:
        _logger_name += f".{instance_id}"
    _logger = logging.getLogger(_logger_name)
    _logger.setLevel(level)

    # Only add handlers once (prevent duplicates on restart/reload)
    if not _logger.handlers:
        _logger.addHandler(handler)

        # File handler — instance-specific console log (RotatingFileHandler with max 10MB per file, 5 backups)
        log_dir = Path(__file__).resolve().parent.parent / 'logs'
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"Cannot create log directory {log_dir}: {e}") from e

        log_filename = f"console_{instance_id}.log" if instance_id else 'console.log'

        try:
            file_handler = _WindowsSafeRotatingFileHandler(
                log_dir / log_filename,
                maxBytes=10 * 1024 * 1024,  # 10MB per file
                backupCount=5,
                encoding='utf-8',
                delay=True  # Open lazily to avoid locking issues on startup
            )
        except OSError as e:
            raise RuntimeError(f"Cannot open console log file {log_dir / log_filename}: {e}") from e

        file_handler.setFormatter(formatter)
        _logger.addHandler(file_handler)

    # BUG_0147: give third-party loggers (starting with asyncio) a path into the app log.
    _install_asyncio_log_bridge()

    return _logger, _original_stdout, _original_stderr


# ─── Module-level State ───────────────────────────────────────────────────
_initialized = False
_original_stdout = None
_original_stderr = None
_init_lock = threading.Lock()  # Protects check-then-act in init_logging()

# Public logger reference — assigned by init_logging(). Safe to import before
# calling init_logging(); calls will use a basic logger until initialized.
logger = logging.getLogger('agent_cascade_logger')


class _AppLoggerProxy:
    """Late-bound delegate to the live agent_cascade.log.logger.

    The app's handlers live on 'agent_cascade_logger' (or a per-instance child
    'agent_cascade_logger.<id>'), and init_logging() REBINDS this module's
    `logger` global to that handler-bearing object. A module-level
    `from agent_cascade.log import logger` executed at import time binds the
    pre-init, handler-less object, and `logging.getLogger(__name__)` never
    reaches the app tree at all (its dotted name is a sibling of
    'agent_cascade_logger', so its DEBUG/INFO records fall to logging.lastResort
    and vanish). This proxy resolves the current `log.logger` on every attribute
    access, so records are emitted through whichever logger is live — before and
    after init_logging(), with or without an instance id.

    Canonical copy: import this from agent_cascade.log rather than redefining the proxy
    per module. (Several modules still carry their own copy — consolidating them is a
    tracked, behavior-neutral follow-up, deliberately out of scope for this fix.)
    """
    __slots__ = ()

    def __getattr__(self, name):
        from agent_cascade.log import logger as _live
        return getattr(_live, name)


def _logging_excepthook(exc_type, exc_value, exc_tb):
    """Replace sys.excepthook to log uncaught main-thread exceptions."""
    try:
        logger.error('Uncaught exception', exc_info=(exc_type, exc_value, exc_tb))
    except Exception:
        # During interpreter shutdown logger may be unavailable; fall back to sys.__excepthook__
        sys.__excepthook__(exc_type, exc_value, exc_tb)


def _threading_excepthook(args):
    """Replace threading.excepthook to log uncaught thread exceptions."""
    try:
        thread_name = args.thread.name if args.thread else 'unknown'
        # Python 3.12+ renamed exc_tb -> exc_traceback in _thread._ExceptHookArgs
        if hasattr(args, 'exc_traceback'):
            tb = args.exc_traceback
        elif hasattr(args, 'exc_tb'):
            tb = args.exc_tb
        else:
            tb = None
        logger.error('Uncaught exception in thread %s', thread_name, exc_info=(args.exc_type, args.exc_value, tb))
        if tb is None:
            logger.warning('Uncaught exception in thread %s — no traceback available', thread_name)
    except Exception:
        # During interpreter shutdown logger may be unavailable; fall back to sys.__excepthook__
        try:
            exc_tb = getattr(args, 'exc_traceback', None) or getattr(args, 'exc_tb', None)
            sys.__excepthook__(args.exc_type, args.exc_value, exc_tb)
        except Exception:
            pass  # Final safety net during interpreter shutdown


def init_logging(level=None) -> None:
    """
    Initialize the logging system.

    Sets up handlers, replaces sys.stdout/sys.stderr with capturing streams,
    and installs exception hooks. Must be called once at application startup.

    Args:
        level: Optional logging level (default: INFO, or DEBUG if AGENT_CASCADE_DEBUG is set).

    Raises:
        RuntimeError: If logging has already been initialized.
    """
    global _initialized, logger, _original_stdout, _original_stderr

    with _init_lock:
        if _initialized:
            raise RuntimeError('Logging has already been initialized')

        _logger, _original_stdout, _original_stderr = setup_logger(level)
        logger = _logger

        # Redirect sys.stdout and sys.stderr so that ALL print() calls and uncaught
        # thread exceptions are routed through the logging system → console.log.
        # The StreamHandler above already writes to _original_stdout, so no recursion.
        sys.stdout = _CapturingStream('stdout', _original_stdout)
        sys.stderr = _CapturingStream('stderr', _original_stderr)

        # Install global exception hooks for main thread and daemon threads
        sys.excepthook = _logging_excepthook
        threading.excepthook = _threading_excepthook

        _initialized = True  # Set inside lock to prevent concurrent init


def reset_logging() -> None:
    """
    Reset the logging system to its default state.

    Restores original sys.stdout/sys.stderr and uninstalls exception hooks.
    Useful for testing or clean shutdown.
    """
    global _initialized, logger, _original_stdout, _original_stderr

    if not _initialized:
        return

    # Restore original streams
    if _original_stdout is not None:
        sys.stdout = _original_stdout
    if _original_stderr is not None:
        sys.stderr = _original_stderr

    # Uninstall exception hooks only if they are our custom ones
    if hasattr(sys, '__excepthook__') and sys.excepthook is _logging_excepthook:
        sys.excepthook = sys.__excepthook__

    try:
        if hasattr(threading, 'excepthook') and threading.excepthook is _threading_excepthook:
            # Python 3.8+: restore default excepthook
            if hasattr(threading, '__standard_excepthook__'):
                threading.excepthook = threading.__standard_excepthook__
            else:
                threading.excepthook = None  # Triggers Python to use default
    except Exception:
        pass

    # Restore the module-level logger to the base parent logger AND reset its
    # level to NOTSET. init_logging()/setup_logger() set the (possibly per-instance
    # child) logger's level to INFO; without resetting it, late-bound modules keep
    # emitting through a level-INFO logger after teardown, which filters DEBUG records
    # before they reach a handler and breaks order-dependent log-count tests (caplog
    # sets the root level, but a logger carrying an explicit level stops propagation
    # at that level — the rebind alone is a no-op when there is no instance id, so the
    # base parent IS the emitter and its level must be reset too).
    logger = logging.getLogger('agent_cascade_logger')
    logger.setLevel(logging.NOTSET)

    _initialized = False
