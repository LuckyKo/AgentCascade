"""Shared shell utilities — constants and helpers used by both async_shell and operation_manager/shell.

This module eliminates duplication between the async shell tracker (background execution)
and the sync shell executor (blocking execution). Both share:
- Common pipe read sizes, timeouts, and Windows UTF-8 configuration
- Pipe draining logic for stdout/stderr capture
"""

import subprocess
import threading
from typing import List

from agent_cascade.log import logger

# ─── Shared constants ──────────────────────────────────────────────
PIPE_READ_SIZE = 4096  # Bytes per read call on stdout/stderr pipes
DRAIN_THREAD_JOIN_TIMEOUT = 3  # Seconds to wait for drain threads after process ends
WINDOWS_UTF8_CODE_PAGE = '65001'  # Windows code page for UTF-8 output

# ─── Shared pipe drain function (line-based, used by async_shell) ──


def drain_pipe_lines(pipe, target_list: list, lock: threading.Lock) -> None:
    """Read from a pipe line-by-line and append to target_list under lock.

    Uses readline() instead of read(chunk_size) so that output arrives incrementally
    even when the shell buffers stdout in full-buffer mode (common on Linux pipes).
    This is critical for async heartbeats: if we wait for 4KB chunks, no heartbeat
    fires until the process exits.

    Used by the async shell tracker where output is consumed line-by-line for heartbeat
    tracking. The lock ensures thread-safe access when the polling loop reads concurrently.

    Args:
        pipe: TextIO pipe (stdout or stderr) to read from.
        target_list: List to extend with drained lines.
        lock: Threading lock for synchronized list access.
    """
    try:
        while True:
            line = pipe.readline()
            if not line:
                break
            # Strip trailing newline; readline returns '\n'-terminated strings
            stripped = line.rstrip('\n').rstrip('\r')
            with lock:
                target_list.append(stripped)
    except Exception as e:
        logger.warning(f"[Shell] Pipe drain error: {e}")


# ─── Shared pipe drain function (chunk-based, used by sync shell) ──


def drain_pipe_chunks(pipe, chunks: list, errors: List[Exception]) -> None:
    """Read from a pipe in chunks and append full text blocks.

    Used by the sync shell executor where output is collected as complete strings
    without line-by-line tracking. Errors are appended to an error list instead of logging.

    Args:
        pipe: TextIO pipe (stdout or stderr) to read from.
        chunks: List to append drained text blocks to.
        errors: List to collect exceptions during draining.
    """
    try:
        while True:
            chunk = pipe.read(PIPE_READ_SIZE)
            if not chunk:
                break  # EOF
            chunks.append(chunk)
    except Exception as e:
        errors.append(e)


# ─── Shared UTF-8 config helper for Windows ────────────────────────


def _translate_semicolons_for_cmd(command: str) -> str:
    """Translate unquoted ``;`` separators to cmd.exe's ``&`` equivalent.

    cmd.exe does NOT treat ``;`` as a command separator (unlike Unix shells), so a
    user command like ``A; B`` would run as the single command ``A`` with ``; B``
    as arguments — the second command never runs. This walks the command tracking
    double-quote state and replaces each UNQUOTED ``;`` with ``&`` (cmd's true
    equivalent of Unix ``;``: run the next command regardless of prior exit status).

    Only double quotes are tracked: cmd.exe treats single ``'`` as a literal
    character, so ``'a;b'`` inside double quotes is still protected by the outer
    ``"``. Quoted ``;`` (e.g. ``python -c "print('a;b')"``) is preserved as data.

    Unbalanced quotes: if an odd number of ``"`` remains open, the remainder of the
    string is treated as quoted (safe default — no translation after an unclosed
    quote). This errs on the side of leaving the command untouched rather than
    splitting a literal that happens to contain an unpaired quote.

    Args:
        command: The raw user command string (before the chcp prefix is added).

    Returns:
        The command with each unquoted ``;`` replaced by ``&``.
    """
    result = []
    in_quotes = False
    for char in command:
        if char == '"':
            in_quotes = not in_quotes
            result.append(char)
        elif char == ';' and not in_quotes:
            result.append('&')
        else:
            result.append(char)
    return ''.join(result)


def configure_windows_utf8(command: str, create_new_console: bool = False) -> tuple:
    """Prepend chcp 65001 to force CMD into UTF-8 mode on Windows.

    The user command is first passed through ``_translate_semicolons_for_cmd`` so
    that unquoted ``;`` separators (Unix idiom) become cmd.exe ``&`` chains — without
    this, cmd.exe mis-parses ``A; B`` as a single command and the second part never
    runs. The chcp prefix itself is left untouched.

    Args:
        command: Shell command string to execute.
        create_new_console: If True, also pop a console window (for async shells).

    Returns:
        Tuple of (modified_command, creationflags) ready for subprocess.Popen.
    """
    flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    if create_new_console:
        flags |= subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
    translated = _translate_semicolons_for_cmd(command)
    return (f'chcp {WINDOWS_UTF8_CODE_PAGE} > nul 2>&1 & {translated}', flags)
