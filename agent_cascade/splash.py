"""Startup splash screen for AgentCascade.

Renders a compact, ASCII-only banner (wordmark + info box) at server startup so the
user immediately sees the version and how to reach the running instance.

Design constraints (see plan review):
  - ASCII-only art/box (``+``, ``-``, ``|``) for maximum Windows/codepage compatibility —
    no unicode box-drawing characters that mangle on cp437/936 consoles.
  - Never blocks or crashes startup: the whole render is guarded and degrades to a single
    plain line on any failure.
  - Non-TTY (piped / CI) and ``AGENT_CASCADE_NO_BANNER`` → compact one-liner only (no art),
    so automation output stays clean while logs still record a useful line.
  - Narrow terminal (< art width) → compact one-liner, no borders (avoids wrapping).

Call it from the entry points right after ``init_logging()`` (stdout is then captured into
console.log, so the banner also lands in the log file — consistent with other startup output).
"""
import os
import shutil
import sys

# ASCII wordmark "AgentCascade" — generated with pyfiglet (font='standard') and
# hand-verified to render legibly. Each line is exactly 65 columns wide. Kept as a
# plain list so the splash has no third-party runtime dependency on pyfiglet.
_ART = [
    '    _                    _    ____                        _      ',
    '   / \\   __ _  ___ _ __ | |_ / ___|__ _ ___  ___ __ _  __| | ___ ',
    "  / _ \\ / _` |/ _ \\ '_ \\| __| |   / _` / __|/ __/ _` |/ _` |/ _ \\",
    ' / ___ \\ (_| |  __/ | | | |_| |__| (_| \\__ \\ (_| (_| | (_| |  __/',
    '/_/   \\_\\__, |\\___|_| |_|\\__|\\____\\__,_|___/\\___\\__,_|\\__,_|\\___|',
    '        |___/                                                    ',
]

# Width of the widest art line (drives the narrow-terminal check).
_ART_WIDTH = max(len(line) for line in _ART)


def _resolve_version(version):
    """Return the version string to display. Explicit arg wins; else import; else 'unknown'."""
    if version:
        return str(version)
    try:
        from agent_cascade import __version__
        return __version__
    except Exception:  # noqa: BLE001 - never let a version lookup block startup
        return 'unknown'


def _compact_line(mode, port, host, version):
    """A single-line banner that is safe in pipes / narrow terminals."""
    return f"AgentCascade v{version} [{mode}] http://{host}:{port}"


def _info_rows(mode, port, host, version):
    """Return the (key, value) rows for the info box."""
    url = f"http://{host}:{port}"
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return [
        ('Version', version),
        ('Mode', mode),
        ('URL', url),
        ('Python', pyver),
    ]


def _inner_width(rows):
    """Inner width of the info box: fits both the wordmark and the widest info cell.

    A long URL/host can make the box wider than the art, so we take the max of the two.
    """
    return max(_ART_WIDTH, max(len(k) + 2 + len(v) for k, v in rows))


def _banner_width(rows):
    """Total rendered banner width (in columns): the box is ``+`` + inner + ``+``."""
    return _inner_width(rows) + 2


def _build_banner(rows):
    """Build the full multi-line banner (art + info box). Caller handles output."""
    lines = list(_ART)
    lines.append('')

    inner_w = _inner_width(rows)
    top = '+' + '-' * inner_w + '+'
    bottom = '+' + '-' * inner_w + '+'

    box = [top]
    for k, v in rows:
        cell = f" {k:<8} {v}"
        pad = inner_w - len(cell)
        box.append('|' + cell + ' ' * max(pad, 0) + '|')
    box.append(bottom)

    lines += box
    return '\n'.join(lines)


def _terminal_stream():
    """Return the real terminal stream to write the banner to, or None.

    After ``init_logging()`` runs, ``sys.stdout`` is a capturing stream that BOTH logs
    (with a timestamp prefix) AND echoes to the original stdout — so a multi-line banner
    would appear twice on screen. To avoid that, we write directly to the underlying real
    stream (``agent_cascade.log._original_stdout``) when it's available. Falls back to
    ``sys.stdout`` (pre-logging or if the log module wasn't imported).
    """
    try:
        from agent_cascade import log as _log_mod
        original = getattr(_log_mod, '_original_stdout', None)
        if original is not None:
            return original
    except Exception:  # noqa: BLE001 - logging not set up yet; use sys.stdout
        pass
    return sys.stdout


def print_startup_banner(mode, port, host='127.0.0.1', version=None):
    """Print the startup banner. Never raises; degrades gracefully.

    Args:
        mode: Human label for the run mode (e.g. "API Server" or "Multi-Agent").
        port: Port the server binds to.
        host: Host the server binds to ("127.0.0.1" or "0.0.0.0"). Shown in the URL.
        version: Optional explicit version; if omitted, read from agent_cascade.__version__.
    """
    try:
        ver = _resolve_version(version)
        rows = _info_rows(mode, port, host, ver)

        # Resolve the real terminal stream once (see _terminal_stream for why).
        out = _terminal_stream()

        def emit(text):
            out.write(text + '\n')
            try:
                out.flush()
            except Exception:  # noqa: BLE001 - broken pipe etc. is non-fatal
                pass

        # Non-TTY (piped/CI) or explicit opt-out → compact one-liner only.
        non_tty = False
        try:
            non_tty = not out.isatty()
        except Exception:  # noqa: BLE001 - isatty can raise on odd streams
            non_tty = True

        if os.getenv('AGENT_CASCADE_NO_BANNER') or non_tty:
            emit(_compact_line(mode, port, host, ver))
            return

        # Narrow terminal → compact one-liner (no art/box to avoid wrapping).
        # Compare against the ACTUAL rendered width (the box can be wider than the
        # wordmark when the URL/host is long), not just _ART_WIDTH. The +2 margin
        # leaves a little breathing room so the box never touches the terminal edge.
        try:
            cols = shutil.get_terminal_size((80, 24)).columns
        except Exception:  # noqa: BLE001
            cols = 80
        if cols < _banner_width(rows) + 2:
            emit(_compact_line(mode, port, host, ver))
            return

        banner = _build_banner(rows)
        # Blank line before/after for clean separation from surrounding logs.
        emit('')
        emit(banner)
        emit('')
    except Exception:  # noqa: BLE001 - the splash must never block startup
        try:
            out.write(f"AgentCascade v{_resolve_version(version)} [{mode}] starting...\n")
            out.flush()
        except Exception:  # noqa: BLE001 - absolute last resort
            pass
