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
import os
import sys
import threading
from pathlib import Path

from agent_cascade.instance_id import get_instance_id


class _CapturingStream:
    """
    Replacement for sys.stdout / sys.stderr that routes every write() 
    through the logger so it ends up in console.log.
    
    Writes are logged at INFO level (stdout) or WARNING level (stderr).
    A per-instance threading.Lock prevents interleaved writes from corrupting log lines.
    """
    def __init__(self, stream_type, original_stream):
        self._stream_type = stream_type  # 'stdout' or 'stderr'
        self._original = original_stream
        self._lock = threading.Lock()

    def write(self, msg):
        # Type guard: ensure msg is a string (handles non-string writes gracefully)
        msg = str(msg)
        with self._lock:
            if msg and msg.strip():
                level = logging.INFO if self._stream_type == 'stdout' else logging.WARNING
                logger.log(level, msg.rstrip('\n\r'))
            # Also write to the original stream so it still appears on screen
            try:
                self._original.write(msg)
                self._original.flush()
            except Exception:
                pass

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass

    def isatty(self):
        """Delegate to the original stream's isatty, falling back to False."""
        try:
            return self._original.isatty()
        except Exception:
            return False


def setup_logger(level=None):
    if level is None:
        if os.getenv('QWEN_AGENT_DEBUG', '0').strip().lower() in ('1', 'true'):
            level = logging.DEBUG
        else:
            level = logging.INFO

    # Capture original stdout/stderr BEFORE creating the StreamHandler,
    # so the handler writes to the real terminal and NOT our capturing stream
    # (which would cause infinite recursion: write → log → handler → captured stdout → log → ...)
    _original_stdout = sys.stdout
    _original_stderr = sys.stderr

    instance_id = get_instance_id()

    handler = logging.StreamHandler(stream=_original_stdout)
    # Do not run handler.setLevel(level) so that users can change the level via logger.setLevel later
    formatter = logging.Formatter('%(asctime)s - %(filename)s - %(lineno)d - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    _logger_name = "agent_cascade_logger"
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

        log_filename = f"console_{instance_id}.log" if instance_id else "console.log"

        from logging.handlers import RotatingFileHandler
        try:
            file_handler = RotatingFileHandler(
                log_dir / log_filename, 
                maxBytes=10 * 1024 * 1024,  # 10MB per file
                backupCount=5,
                encoding='utf-8'
            )
        except OSError as e:
            raise RuntimeError(f"Cannot open console log file {log_dir / log_filename}: {e}") from e

        file_handler.setFormatter(formatter)
        _logger.addHandler(file_handler)

    return _logger, _original_stdout, _original_stderr


# Module-level logger instance (set up before stdout capture so handlers exist)
logger, _original_stdout, _original_stderr = setup_logger()

# ─── Stdout/Stderr Capture ────────────────────────────────────────────────
# Redirect sys.stdout and sys.stderr so that ALL print() calls and uncaught
# thread exceptions are routed through the logging system → console.log.
# The StreamHandler above already writes to _original_stdout, so no recursion.

sys.stdout = _CapturingStream('stdout', _original_stdout)
sys.stderr = _CapturingStream('stderr', _original_stderr)

# ─── Global Exception Hooks ───────────────────────────────────────────────
# Catch uncaught exceptions in the main thread
def _logging_excepthook(exc_type, exc_value, exc_tb):
    """Replace sys.excepthook to log uncaught main-thread exceptions."""
    try:
        logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    except Exception:
        # During interpreter shutdown logger may be None; fall back to stderr
        import traceback
        _original_stderr.write(traceback.format_exception(exc_type, exc_value, exc_tb))
        _original_stderr.flush()

sys.excepthook = _logging_excepthook

# Catch uncaught exceptions in daemon/worker threads (Python 3.8+)
def _threading_excepthook(args):
    """Replace threading.excepthook to log uncaught thread exceptions."""
    try:
        thread_name = args.thread.name if args.thread else "unknown"
        # Python 3.12+ renamed exc_tb -> exc_traceback in _thread._ExceptHookArgs
        if hasattr(args, 'exc_traceback'):
            tb = args.exc_traceback
        elif hasattr(args, 'exc_tb'):
            tb = args.exc_tb
        else:
            tb = None
        logger.error(
            "Uncaught exception in thread %s", thread_name,
            exc_info=(args.exc_type, args.exc_value, tb)
        )
        if tb is None:
            logger.warning("Uncaught exception in thread %s — no traceback available", thread_name)
    except Exception:
        # During interpreter shutdown logger may be None; fall back to stderr
        import traceback
        _original_stderr.write(traceback.format_exception(
            args.exc_type, args.exc_value, tb))
        _original_stderr.flush()

threading.excepthook = _threading_excepthook
