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

# Hand-authored ASCII wordmark. Kept to a modest width so it fits common terminals.
_ART = [
    r'     _          _  __   ____   ',
    r'    | |__   ___| |/ /  / __ \  ',
    r"    | '_ \ / _ \ ' /  | |  | | ",
    r'    | |_) |  __/ . \  | |__| | ',
    r'    |_.__/ \___/_/\_\  \____/  ',
]

# The wordmark is "AgentCascade"; the art above spells "Agent". We append "Cascade"
# on a second visual row to complete the name without making the art too tall.
_ART_SUFFIX = [
    r'   ____   _          __  __       ',
    r'  / __ \ | |        |  \/  |      ',
    r' | |  | || |  __ _  | \  / | ___  ',
    r" | |__| || |_| '_ | | |\/| |/ _ \ ",
    r'  \____/ |____/.___| |_||_|_\___/ ',
]

# Width of the widest art line (drives both the box width and the narrow check).
_ART_WIDTH = max(len(line) for line in _ART + _ART_SUFFIX)


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


def _banner_width(rows):
    """Width (in columns) of the full banner: max(art width, box total width).

    The box is ``+`` + inner + ``+`` where inner fits both the art and the widest
    info cell — so a long URL/host can make the box wider than the wordmark.
    """
    inner_w = max(_ART_WIDTH, max(len(k) + 2 + len(v) for k, v in rows))
    return max(_ART_WIDTH, inner_w + 2)


def _build_banner(mode, port, host, version):
    """Build the full multi-line banner (art + info box). Caller handles output."""
    lines = list(_ART)
    lines += ['']
    lines += list(_ART_SUFFIX)
    lines.append('')

    rows = _info_rows(mode, port, host, version)
    inner_w = max(_ART_WIDTH, max(len(k) + 2 + len(v) for k, v in rows))
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

        # Non-TTY (piped/CI) or explicit opt-out → compact one-liner only.
        non_tty = False
        try:
            non_tty = not sys.stdout.isatty()
        except Exception:  # noqa: BLE001 - isatty can raise on odd streams
            non_tty = True

        if os.getenv('AGENT_CASCADE_NO_BANNER') or non_tty:
            print(_compact_line(mode, port, host, ver))
            return

        # Narrow terminal → compact one-liner (no art/box to avoid wrapping).
        # Compare against the ACTUAL rendered width (the box can be wider than the
        # wordmark when the URL/host is long), not just _ART_WIDTH.
        try:
            cols = shutil.get_terminal_size((80, 24)).columns
        except Exception:  # noqa: BLE001
            cols = 80
        if cols < _banner_width(_info_rows(mode, port, host, ver)) + 2:
            print(_compact_line(mode, port, host, ver))
            return

        banner = _build_banner(mode, port, host, ver)
        # Blank line before/after for clean separation from surrounding logs.
        print()
        print(banner)
        print()
    except Exception:  # noqa: BLE001 - the splash must never block startup
        try:
            print(f"AgentCascade v{_resolve_version(version)} [{mode}] starting...")
        except Exception:  # noqa: BLE001 - absolute last resort
            pass
