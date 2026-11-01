"""Shared C/C++ lexical helpers for the ``code_map`` and ``syntax_check`` tools.

Single source of truth for C/C++ lexical structure. Pure stdlib, no I/O side
effects, no third-party dependencies (D-3).

``neutralize`` produces a length- and line-preserving "code-only" view of C/C++
source: comments, string literals, char literals and raw-string literals are
blanked to spaces (newlines preserved) so a caller can run a plain
bracket/structure scan and keep exact (line, col) correspondence with the
original file.

``find_compiler`` is a best-effort, cached, never-raises C++ compiler probe.

DRY seam (D-6): the comment/string-blanking core here overlaps with
``syntax_check._c_family_neutralize`` (the C-family analogue that keeps the
regex/template branches). If you fix a comment/string edge case in one, apply
the same fix to the other.
"""

import os
import shutil

# ── neutralize ───────────────────────────────────────────────────────────────


def _preceding_backslashes_odd(content: str, i: int) -> bool:
    """True if the char at position ``i`` is escaped (an odd number of
    backslashes immediately precede it)."""
    j = i - 1
    count = 0
    while j >= 0 and content[j] == '\\':
        count += 1
        j -= 1
    return count % 2 == 1


def neutralize(content: str) -> str:
    """Return a same-length, same-line-number "code-only" view of ``content``.

    Every character that is not real code is replaced by a space:
      - ``//`` line comments and ``/* */`` block comments (incl. unterminated)
      - ``"..."`` string literals and ``'...'`` char literals (escape-aware, and
        a quote only closes a literal opened by the SAME quote type)
      - C++ raw string literals: ``R"(...)"``, ``R"delim(...)delim"``,
        including user delimiters and multi-line bodies

    Preprocessor line continuations (a ``\\`` at end of line) are understood, so
    a comment/string cannot leak across a physical newline because of a
    continuation. Newlines are always preserved, so the invariants hold:

        len(out) == len(src)
        out.count('\\n') == src.count('\\n')
        neutralize(neutralize(x)) == neutralize(x)   (idempotent)
    """
    out = []
    n = len(content)
    i = 0
    in_string = False
    string_char = ''
    in_block = False
    in_line = False
    in_raw = False
    raw_delim = ''
    while i < n:
        ch = content[i]
        # raw string body: R"delim( ... )delim"
        if in_raw:
            close_seq = ')' + raw_delim + '"'
            if content.startswith(close_seq, i):
                out.append(' ' * len(close_seq))
                i += len(close_seq)
                in_raw = False
                continue
            out.append('\n' if ch == '\n' else ' ')
            i += 1
            continue
        # block comment body
        if in_block:
            if i > 0 and content[i - 1] == '*' and ch == '/':
                in_block = False
            out.append('\n' if ch == '\n' else ' ')
            i += 1
            continue
        # line comment body (ends at a real newline; continues across a
        # backslash-continued newline)
        if in_line:
            if ch == '\n' and not _preceding_backslashes_odd(content, i):
                in_line = False
                out.append('\n')
                i += 1
                continue
            out.append('\n' if ch == '\n' else ' ')
            i += 1
            continue
        # string / char literal body
        if in_string:
            if ch == string_char and not _preceding_backslashes_odd(content, i):
                in_string = False
            out.append('\n' if ch == '\n' else ' ')
            i += 1
            continue
        # code context
        if ch == '/' and i + 1 < n:
            nxt = content[i + 1]
            if nxt == '/':
                in_line = True
                out.append('  ')
                i += 2
                continue
            if nxt == '*':
                in_block = True
                out.append('  ')
                i += 2
                continue
            out.append('/')
            i += 1
            continue
        if ch == 'R' and i + 1 < n and content[i + 1] == '"':
            prev = content[i - 1] if i > 0 else ''
            if prev == '' or not (prev.isalnum() or prev == '_'):
                # raw string: R"delim( ... )delim" — the user delimiter runs from
                # just after R" up to the body-opening '(' (there is no quote
                # between the delimiter and the '(').
                j = i + 2
                while j < n and content[j] != '(':
                    j += 1
                if j >= n:
                    # malformed (no body-opening paren): blank the rest
                    out.append(' ' * (n - i))
                    i = n
                    continue
                raw_delim = content[i + 2:j]
                open_paren = j
                in_raw = True
                out.append(' ' * (open_paren - i + 1))  # blank R"delim(
                i = open_paren + 1
                continue
        if ch in ('"', "'"):
            in_string = True
            string_char = ch
            out.append(' ')
            i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


# ── compiler discovery ───────────────────────────────────────────────────────

# Module-level cache: None = not probed, (exe, args) = found, False = probed, none.
_COMPILER_CACHE = None


def find_compiler():
    """Best-effort C++ compiler discovery.

    Returns an ``(executable, extra_args)`` tuple, or ``None`` if no compiler is
    discoverable. Probes ``clang`` -> ``g++`` -> ``cl`` on PATH, then a bounded
    search of the standard Visual Studio / Build Tools roots. The result is
    cached per process. This function never raises and never installs anything
    (D-3 / OQ-3: no CMakeCache reads).
    """
    global _COMPILER_CACHE
    if _COMPILER_CACHE is not None:
        return None if _COMPILER_CACHE is False else _COMPILER_CACHE
    for exe in ('clang', 'g++', 'cl'):
        path = shutil.which(exe)
        if path:
            _COMPILER_CACHE = (path, [])
            return _COMPILER_CACHE
    found = _probe_vs_roots()
    if found:
        _COMPILER_CACHE = found
        return found
    _COMPILER_CACHE = False
    return None


def _probe_vs_roots():
    """Bounded search for ``cl.exe`` under the standard VS/BuildTools roots.

    Only the fixed roots named by the ``ProgramFiles`` / ``ProgramFiles(x86)``
    environment variables are scanned (no recursive walk). Returns
    ``(cl.exe, [])`` or ``None``.
    """
    bases = []
    for env in ('ProgramFiles', 'ProgramFiles(x86)'):
        val = os.environ.get(env)
        if val:
            bases.append(val)
    editions = ('Community', 'Professional', 'Enterprise', 'BuildTools')
    years = ('2022', '2019')
    for base in bases:
        vs_root = os.path.join(base, 'Microsoft Visual Studio')
        for year in years:
            for edition in editions:
                # 1) toolset bin layout: .../VC/Tools/MSVC/<ver>/bin/<host>/<target>/cl.exe
                msvc = os.path.join(vs_root, year, edition, 'VC', 'Tools', 'MSVC')
                if os.path.isdir(msvc):
                    for toolset in sorted(os.listdir(msvc)):
                        for host in ('Hostx64', 'Hostx86'):
                            for target in ('x64', 'x86'):
                                cl = os.path.join(msvc, toolset, 'bin', host, target, 'cl.exe')
                                if os.path.isfile(cl):
                                    return (cl, [])
                # 2) BuildTools layout: .../VC/Tools/MSVC is absent; try the
                #    top-level bin tree used by older VS.
                legacy = os.path.join(vs_root, year, edition, 'VC', 'bin')
                if os.path.isdir(legacy):
                    for host in ('Hostx64', 'Hostx86'):
                        for target in ('x64', 'x86'):
                            cl = os.path.join(legacy, host, target, 'cl.exe')
                            if os.path.isfile(cl):
                                return (cl, [])
    return None
