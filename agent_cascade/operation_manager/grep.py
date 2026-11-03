"""Grep subsystem — pattern compilation, tool availability check, and mixin for file search."""

import base64
import fnmatch
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

from agent_cascade.tool_utils import truncate_with_spillover, format_truncation_notice
from agent_cascade.operation_manager.grep_ignore import DEFAULT_IGNORED_DIRS, IgnoreResolver

# ─── Module-level cached helpers ──────────────────────────────────────────


def _resolve_spill_path(spill_file_path: Optional[str]) -> Optional[Path]:
    """Convert optional string spill path to Path, or None."""
    return Path(spill_file_path) if spill_file_path else None


@lru_cache(maxsize=256)
def _compile_grep_pattern(pattern: str, *, flags: int = 0):
    """Cache compiled regex patterns for grep to avoid recompiling on each call.

    Args:
        pattern: The regex pattern string.
        flags: Optional re.IGNORECASE flag for case-insensitive matching (smart_case).
            Keyword-only to prevent cache key collisions between positional and keyword calls.
    """
    return re.compile(pattern, flags)


# Marker injected in front of the matching line in context mode. Content lines never get
# any other decoration — see _format_grep_entry (BUG_0144).
_GREP_MATCH_MARKER = '>>>'

# BUG_0056: the marker above was undocumented and unlabelled in the output, so a reader seeing
# '>>>    async def ...' hypothesised the '>>>' was in the file content. This one-line legend is
# prepended to the result body whenever at least one match line carries the marker (context mode
# with a match), making it self-explanatory. It never appears on clean greps (no marker ⇒ none).
_GREP_MARKER_LEGEND = "# note: '>>>' marks matched lines in context mode (unmarked lines are surrounding context)"


def _format_grep_entry(rel_path: str, line_num: int, content: str, marker: str = '') -> str:
    """Render one grep result line as ``<path>:<line>: [<marker>]<content>``.

    BUG_0144: the content column is the RAW file text. It is never indented, padded, or
    otherwise re-wrapped. Context lines used to be rendered with a 5-space indent
    (4 prefix spaces + the separator space) to set them apart from matched lines. That
    indent is invisible whitespace, so anyone transcribing a multi-line block out of grep
    output into ``edit_file`` (``match_mode="exact"``) silently injected leading spaces
    that do not exist in the file and got "Pattern not found". Context lines now carry no
    decoration at all; only matched lines get ``_GREP_MATCH_MARKER``, which is visible and
    trivially stripped, and the always-present ``<path>:<line>: `` prefix tells the reader
    exactly where the content begins.

    Every render path (rg --json, GNU grep, Python fallback, single-file) MUST go through
    here so their byte-level output cannot drift apart.
    """
    return f"{rel_path}:{line_num}: {marker}{content}"


def _grep_body_with_legend(body_lines: list, context: int, match_count: int) -> str:
    """Join the grep body lines, prepending the '>>>' legend when the marker is present.

    BUG_0056: the ``>>>`` marker (injected on matched lines in context mode) was undocumented,
    so readers mistook it for file content. The legend is emitted only when at least one match
    line carries the marker — i.e. context mode with a match. Clean greps are byte-identical
    (no marker ⇒ no legend), and which lines match / how they are numbered is unchanged.
    """
    body = '\n'.join(body_lines)
    if context > 0 and match_count > 0:
        return _GREP_MARKER_LEGEND + '\n' + body
    return body


def _ignore_advisory_note(n: int) -> str:
    """BUG_0202 (F4): the zero-match advisory for matches hidden behind ignore rules.

    States what was consulted (the ignore rules) and never asserts git status — a tracked file
    can be pruned by an ignore rule it does not apply to. One helper so the count and line
    paths cannot drift apart.
    """
    return (f" [note: {n} match(es) were skipped by ignore rules (.gitignore/.ignore/.rgignore); "
            f"grep follows ignore rules, not the git index — re-run with ignore_vcs=False "
            f"to include them]")


def _over_cap_partial_note(budget_s: float) -> str:
    """BUG_0202 (F5): the note for a truncated over-cap DIAGNOSTIC walk.

    Worded per the walk's OWN budget (GREP_LARGE_FILE_SCAN_BUDGET_S), never the caller's
    deadline — a walk that merely ran out of its budget is not a caller timeout. One helper so
    the count path's zero and non-zero branches cannot drift apart.
    """
    return (f" [note: the over-cap file list is partial — its {budget_s:.2f} s diagnostic walk "
            'did not finish, so the list of skipped files above is incomplete]')


def _exclude_matches(basename: str, rel_path: str, pattern: str) -> bool:
    """grep exclude semantics: no separator → basename; separator → relative path."""
    if not pattern:
        return False
    if '/' not in pattern and '\\' not in pattern:
        return fnmatch.fnmatch(basename, pattern)
    return fnmatch.fnmatch(rel_path, pattern)


# BUG_0167: LLMs sometimes HTML-escape code-like arguments, so a regex with a lookbehind
# (`(?<!x)`) arrives as `(?&lt;!x)`. This gate keeps the recovery cost off the hot path: only
# patterns that actually contain an entity are examined.
_HTML_ENTITY_RE = re.compile(r'&(?:lt|gt|amp|quot|apos|nbsp|#\d+|#x[0-9a-fA-F]+);', re.IGNORECASE)


def _recover_html_escaped_pattern(pattern: str):
    """Recover an HTML-escaped regex, or return it unchanged (BUG_0167).

    Returns ``(pattern_to_search, note)``. A pattern that already compiles is NEVER rewritten
    (so searching for the literal text ``&lt;`` keeps working); only a pattern that fails
    ``re.compile`` but whose HTML-unescaped form compiles is recovered, and the note tells the
    caller what happened. Handles double-escaping (``&amp;lt;``) by unescaping up to 3 times.
    """
    import html as _html

    try:
        re.compile(pattern)
        return pattern, ''
    except re.error:
        pass

    current = pattern
    for _ in range(3):
        candidate = _html.unescape(current)
        if candidate == current:
            break
        try:
            re.compile(candidate)
        except re.error:
            current = candidate
            continue
        return candidate, (
            " [note: the pattern contained HTML entities (e.g. '&lt;' for '<'); searched for the "
            "unescaped form — send '<' literally in lookbehinds/named groups]")
    return pattern, ''


def _rg_lines_text(lines_data: dict) -> str:
    """Extract one line's text from a ripgrep ``--json`` ``lines`` object.

    ripgrep carries the content in exactly one of two mutually exclusive fields:
    ``lines.text`` when the line is valid UTF-8, or ``lines.bytes`` (base64 of the raw
    line bytes, newline included) when it is not. Reading only ``text`` silently rendered
    every non-UTF-8 match and context line with an EMPTY content column (BUG_0145) — the
    path/line metadata still arrived, so the result looked like a genuine blank line.

    The ``bytes`` fallback is decoded with ``errors='replace'``, matching the lossy
    decoding used for the subprocess's stdout and for ``read_file`` (so the content column
    shows what the canonical read view shows instead of dropping the bytes).
    """
    text = lines_data.get('text')
    if text is None:
        text = base64.b64decode(lines_data.get('bytes', '')).decode('utf-8', errors='replace')
    return text.rstrip('\r\n')


def _rg_match_fields(obj: dict):
    """Extract ``(normalized_path, line_num, match_text)`` from a ripgrep ``--json`` ``match`` event.

    BUG_0190: the single source of truth for match-event field extraction, shared by
    :func:`_iter_rg_json_matches` (the count path and the BUG_0061 ignored-path probe) and the
    line-mode ``_parse_rg_json``, so the three cannot drift apart. Returns ``None`` when ``obj``
    is not a ``match`` event. The line text goes through :func:`_rg_lines_text` (BUG_0145: decodes
    ``lines.bytes`` for non-UTF-8 lines and strips the trailing newline).
    """
    if obj.get('type') != 'match':
        return None
    data = obj.get('data', {})
    file_path = data.get('path', {}).get('text', '')
    line_num_data = data.get('line_number', 0)
    if isinstance(line_num_data, dict):
        line_num = line_num_data.get('start', 0)
    else:
        line_num = line_num_data
    match_text = _rg_lines_text(data.get('lines', {}))
    return file_path.replace('\\', '/'), line_num, match_text


def _iter_rg_json_matches(stdout_text: str):
    """Yield ``(normalized_path, line_num, match_text)`` for each ripgrep ``--json`` ``match`` event.

    BUG_0190: the shared rg-JSON match parser for the count-only path and the ignored-path probe.
    One ``match`` event == one reported match line (submatches are deliberately NOT counted).
    Malformed lines are skipped, never raised.
    """
    for line in (stdout_text.split('\n') if stdout_text.strip() else []):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        fields = _rg_match_fields(obj)
        if fields is not None:
            yield fields


def _split_top_level_alternatives(pattern: str):
    """Split a regex at top-level ``|`` (depth 0, outside ``[…]`` classes and ``(…)`` groups).

    BUG_0190: builds the per-alternative count breakdown. Returns the list of alternatives, or
    ``None`` when the pattern is not confidently parseable (unbalanced brackets/groups, or no
    top-level ``|``) so the caller omits the per-pattern section rather than emit a wrong
    breakdown. Honours backslash escapes; a ``]`` closes a class only if it is not the first
    character after ``[``/``[^``.
    """
    parts = []
    depth = 0
    in_class = False
    class_start = -1
    start = 0
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == '\\':
            i += 2
            continue
        if in_class:
            if c == ']' and (i - class_start) > 1:
                in_class = False
            i += 1
            continue
        if c == '[':
            in_class = True
            class_start = i
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth < 0:
                return None
        elif c == '|' and depth == 0:
            parts.append(pattern[start:i])
            start = i + 1
        i += 1
    if depth != 0 or in_class:
        return None
    parts.append(pattern[start:])
    if len(parts) < 2:
        return None
    return parts


def _compile_alternatives(pattern: str, literal: bool, flags: int):
    """Compile each top-level alternative of ``pattern`` (BUG_0190).

    Returns ``(alternatives, compiled_regexes)`` aligned by index, or ``(None, None)`` when the
    pattern has no confidently-parseable top-level alternatives (per-pattern section omitted).
    Each alternative is compiled with the same flags as the whole pattern; for ``literal=True``
    each alternative is ``re.escape``-d first. Any compile failure returns ``(None, None)``.
    """
    alts = _split_top_level_alternatives(pattern)
    if not alts:
        return None, None
    compiled = []
    for alt in alts:
        candidate = re.escape(alt) if literal else alt
        try:
            compiled.append(_compile_grep_pattern(candidate, flags=flags))
        except re.error:
            return None, None
    return alts, compiled


def _incomplete_note(skipped_count: int, skipped_paths: list, max_mb: int, tail: str = '') -> str:
    """BUG_0185/BUG_0190: the shared ``[search INCOMPLETE — …]`` note.

    A count/line claim over a scope where over-size files were skipped must never read as
    conclusive. The count-only path reuses this so the integrity guarantee carries over from
    BUG_0185. Returns ``''`` when nothing was skipped. ``tail`` is an optional trailing clause
    (e.g. ``use read_file to inspect their headers/content``).
    """
    if not skipped_count:
        return ''
    more = f" (+{skipped_count - 10} more)" if skipped_count > 10 else ''
    tail_part = f"; {tail}" if tail else ''
    return (f" [search INCOMPLETE — skipped {skipped_count} file(s) > {max_mb} MB: "
            f"{', '.join(skipped_paths[:10])}{more}{tail_part}]")


@lru_cache(maxsize=1)
def _check_tool_availability():
    """Check if ripgrep or system grep are available at runtime.

    Returns a tuple of (rg_available, grep_available).
    Uses lru_cache for performance — tool availability doesn't change during execution.
    """
    import os
    import shutil
    rg_path = shutil.which('rg')
    grep_path = shutil.which('grep')

    rg_available = rg_path is not None
    grep_available = (grep_path is not None) and (os.name != 'nt')

    return rg_available, grep_available


def _grep_large_file_limits() -> tuple:
    """Return ``(max_file_size_mb, head_bytes, scan_limit, budget_s)`` for BUG_0169.

    Read lazily from :mod:`agent_cascade.settings` so tests can patch the module constants
    and callers that never grep pay nothing. Falls back to the documented defaults when the
    settings module cannot be imported (e.g. an isolated unit-test harness).
    """
    try:
        from agent_cascade.settings import (GREP_LARGE_FILE_HEAD_KB, GREP_LARGE_FILE_SCAN_BUDGET_S,
                                            GREP_LARGE_FILE_SCAN_LIMIT, GREP_MAX_FILE_SIZE_MB)
        return (int(GREP_MAX_FILE_SIZE_MB), int(GREP_LARGE_FILE_HEAD_KB) * 1024,
                int(GREP_LARGE_FILE_SCAN_LIMIT), float(GREP_LARGE_FILE_SCAN_BUDGET_S))
    except Exception:  # noqa: BLE001 - a settings import hiccup must not break grep
        return 100, 64 * 1024, 50, 0.75


def _grep_max_file_size_mb() -> int:
    """The streaming cap in MB (0 disables it)."""
    return _grep_large_file_limits()[0]


# ─── Mixin: Grep methods for OperationManager ─────────────────────────────


class GrepMixin:
    """Grep/search methods. Expects self to have __init__-set attributes including class-level exclude constants."""

    # Default exclude patterns for standard grep (basename matching)
    _GREP_DEFAULT_EXCLUDES = [
        '*.pyc',
        '*.so',
        '*.dll',
        '*.exe',
        '*.zip',
    ]

    # Default directory excludes for GNU grep --exclude-dir (may not be available on all systems).
    # BUG_0062: widened to the shared union so this path, the Python fallback, and the
    # rg fast path stop carrying three different dir lists. GNU grep cannot read ignore
    # files at all, so this stays an honest approximation — a documented, POSIX-only
    # residual divergence, not an oversight.
    _GREP_DEFAULT_EXCLUDE_DIRS = sorted(DEFAULT_IGNORED_DIRS)

    @staticmethod
    def _sanitize_glob_pattern(pattern: str, *, allow_traversal: bool = False) -> str:
        """Sanitize a glob pattern to prevent path traversal outside the search root.

        Args:
            pattern: The glob pattern to sanitize (e.g., "*.py", "src/*.js").
            allow_traversal: If True, skip traversal checks (for internal use only).

        Returns:
            The sanitized pattern with backslashes normalized to forward slashes.

        Raises:
            ValueError: If the pattern contains path traversal components, absolute paths, or UNC paths.
        """
        if not pattern:
            return pattern

        # Normalize backslashes to forward slashes
        cleaned = pattern.replace('\\', '/')

        # Reject UNC-style paths first (e.g., "//server/share" or "\\server\share")
        if cleaned.startswith('//'):
            raise ValueError('Invalid glob pattern: UNC paths are not allowed')

        # Reject absolute Unix paths
        if cleaned.startswith('/'):
            raise ValueError('Invalid glob pattern: absolute paths are not allowed')

        # Reject drive-letter absolute paths (e.g., "C:", "C:/", "C:*")
        if len(cleaned) >= 2 and cleaned[0].isalpha() and cleaned[1] == ':':
            raise ValueError('Invalid glob pattern: absolute paths are not allowed')

        if not allow_traversal:
            parts = cleaned.split('/')
            for part in parts:
                if part == '..':
                    raise ValueError("Invalid glob pattern: path traversal ('..') is not allowed")

        return cleaned

    @staticmethod
    def _extract_spill_path(truncated_with_notice: str) -> Optional[str]:
        """Recover the spillover file's relative path from a truncate_with_spillover result.

        truncate_with_spillover appends a notice of the form
        "...Full output saved to: <rel>" (same format read_file uses). The fast path
        needs that rel path to surface it in its own summary, but does its own
        line-based truncation instead of returning the notice verbatim. Returns None
        if no such notice is present (e.g. text was under the limit).
        """
        marker = 'Full output saved to: '
        idx = truncated_with_notice.rfind(marker)
        if idx == -1:
            return None
        path = truncated_with_notice[idx + len(marker):].strip()
        # The notice is closed with a trailing "]"; drop it if present.
        if path.endswith(']'):
            path = path[:-1].rstrip()
        return path or None

    def _build_rg_argv(self,
                       pattern: str,
                       include: str,
                       exclude: str,
                       context: int,
                       smart_case: bool,
                       *,
                       force_no_ignore: bool = False,
                       exclude_git: bool = False,
                       literal: bool = False) -> list:
        """Single source of truth for the ripgrep argv used by BOTH the fast path and the
        BUG_0061 ignored-path probe, so the two can never drift apart (a drifting probe
        would silently misreport how many matches are hidden).

        Two intentional knobs, set by each caller to match its own role:
          force_no_ignore — emit `--no-ignore`, disabling ripgrep's built-in .gitignore/
                            .ignore/.rgignore handling. The fast path wants this only when
                            ignore_vcs=False; the probe always wants it (its entire purpose).
          exclude_git     — append `--glob '!.git/'`. The fast path wants this only when
                            ignore_vcs=True (to keep the documented .git exclusion);
                            the probe always wants it (to avoid descending into .git/).
          literal         — emit `-F` (fixed-string mode) so ripgrep treats the pattern as
                            a literal string rather than a regex.

        Defaults are False/False/False so neither behaviour is imposed on a caller implicitly —
        each call site must state which behaviour it needs.

        Ordering is load-bearing: ripgrep uses "last match wins" for --glob, so the user
        globs must precede '!.git/' for the .git exclusion to hold against a user include
        glob such as **/*.py.
        """
        cmd = [
            'rg',
            '--no-heading',
            '-n',
            '--json',
            '--color',
            'never',
            '--no-mmap',
            # Search hidden files/dirs (default rg skips dotfiles) so results match the
            # Python fallback, which walks .hidden/ etc. via os.walk. Always applied.
            '--hidden',
            # Treat binary files as text (rg -a). Without this, any file containing a NUL
            # byte is classified as "binary" and silently skipped: if no other file matches
            # the tool falsely reports "No matches", and if other files DO match rg returns
            # rc=0 so the binary file's match is dropped with no Python fallback (todo line 138).
            '--text',
        ]

        # BUG_0169: `--text` on a multi-GB model artifact streams the whole file. Skip files
        # above the cap during the streaming search; grep() then scans only their headers and
        # reports the partial coverage instead of timing out with misleading advice.
        _max_file_mb = _grep_max_file_size_mb()
        if _max_file_mb > 0:
            cmd.append(f'--max-filesize={_max_file_mb}M')

        if force_no_ignore:
            cmd.append('--no-ignore')

        if literal:
            cmd.append('-F')

        if context > 0:
            cmd.extend(['-C', str(context)])

        has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
        if smart_case:
            if not re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
                cmd.append('-i')

        # "*" with --glob breaks ripgrep (matches only root files). Searches all by default.
        # BUG_0056: support comma-separated globs (e.g. "*.py,*.js") — one --glob per pattern.
        if include and include != '*':
            for _g in include.split(','):
                _g = _g.strip()
                if _g:
                    cmd.extend(['--glob', _g])

        # User-specified excludes (basename or path globs). Comma-separated supported.
        if exclude:
            for _e in exclude.split(','):
                _e = _e.strip()
                if _e:
                    cmd.extend(['--glob', f'!{_e}'])

        if exclude_git:
            # --hidden overrides ripgrep's implicit .git skip, so re-exclude .git to keep
            # the documented .git exclusion guarantee (matches the Python fallback, which
            # prunes .git from its os.walk when ignore_vcs=True). Added LAST because rg uses
            # "last match wins" for --glob: this guarantees .git stays excluded even if a
            # user include glob (e.g. **/*.py) would otherwise re-match files inside .git.
            # NOTE: use '!.git/' (directory form), NOT '!.git/**' — on ripgrep 15.1.0 the
            # '!.git/**' glob does not exclude .git, while '!.git/' reliably excludes both
            # top-level and nested .git files (verified on Windows + POSIX).
            cmd.extend(['--glob', '!.git/'])

        cmd.extend(['-e', pattern])
        return cmd

    # ─── BUG_0169: over-size file handling ────────────────────────────────

    @staticmethod
    def _head_scan_matches(file_path: Path, rel_path: str, pattern_re, head_bytes: int) -> list:
        """Search only the first ``head_bytes`` of a file and format matches.

        Used for files grep's streaming search refused (over ``GREP_MAX_FILE_SIZE_MB``).
        Model artifacts and archives carry their metadata header at the start (a JSON header
        in the reported case), so this recovers exactly the useful information without
        reading 19 GB. Line numbers are relative to the scanned prefix.
        """
        try:
            with file_path.open('rb') as fh:
                chunk = fh.read(head_bytes)
        except OSError:
            return []
        text = chunk.decode('utf-8', errors='replace')
        return [_format_grep_entry(rel_path, line_num, line.rstrip('\r'))
                for line_num, line in enumerate(text.split('\n'), 1)
                if pattern_re.search(line)]

    def _iter_grep_scope_files(self, root: Path, include: str, exclude: str,
                               ignore_vcs: bool, deadline: float):
        """Yield ``(path, rel_path)`` for files matching grep's include/exclude/ignore rules.

        Mirrors the Python fallback's filtering (default excludes, basename-vs-path exclude
        semantics, .gitignore handling) so the over-size scan reports the same candidate set
        the streaming search saw. Bounded by ``deadline`` (a ``time.monotonic()`` value) so a
        pathological tree can never add more than the configured budget to a grep.
        """
        import os

        resolver = IgnoreResolver(root) if ignore_vcs else None
        includes = [p.strip() for p in include.split(',') if p.strip()] if include != '*' else []
        excludes = [p.strip() for p in exclude.split(',') if p.strip()] if exclude else []
        default_excludes = ('*.pyc', '*.so', '*.dll', '*.exe', '*.zip')

        for dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() > deadline:
                return
            if resolver is not None:
                dirnames[:] = [d for d in dirnames
                               if not resolver.is_ignored(Path(dirpath) / d, is_dir=True)]
            for name in filenames:
                if time.monotonic() > deadline:
                    return
                if any(fnmatch.fnmatch(name, pat) for pat in default_excludes):
                    continue
                fp = Path(dirpath) / name
                try:
                    rel_str = str(fp.relative_to(root)).replace('\\', '/')
                except ValueError:
                    continue
                if includes and not any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_str, pat)
                                        for pat in includes):
                    continue
                if resolver is not None and resolver.is_ignored(fp, is_dir=False):
                    continue
                if any(_exclude_matches(name, rel_str, exc) for exc in excludes):
                    continue
                yield fp, rel_str

    def _scan_large_files(self, root: Path, include: str, exclude: str, pattern: str, *,
                          ignore_vcs: bool = True, smart_case: bool = True,
                          literal: bool = False, head_scan: bool = True) -> dict:
        """Find over-size files in the scope and (optionally) match their headers.

        Returns ``{'formatted', 'count', 'large_files', 'scanned', 'total_bytes', 'partial'}``:
        ``large_files`` is how many over-size files were found, ``scanned`` how many had their
        header searched, ``count``/``formatted`` the header matches, ``total_bytes`` their
        combined size, and ``partial`` True when the scan stopped at ``GREP_LARGE_FILE_SCAN_LIMIT``
        or the walk budget expired before the whole scope was seen.

        Best-effort by design: any I/O or regex failure yields whatever was gathered, never an
        exception into the grep result.
        """
        out = {'formatted': [], 'count': 0, 'large_files': 0, 'scanned': 0,
               'total_bytes': 0, 'partial': False}
        max_mb, head_bytes, scan_limit, budget_s = _grep_large_file_limits()
        if max_mb <= 0:
            return out

        has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
        if smart_case and re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
            flags = 0
        elif smart_case:
            flags = re.IGNORECASE
        else:
            flags = 0
        try:
            pattern_re = _compile_grep_pattern(re.escape(pattern) if literal else pattern, flags=flags)
        except re.error:
            return out

        max_bytes = max_mb * 1024 * 1024
        deadline = time.monotonic() + budget_s
        try:
            for fp, rel_str in self._iter_grep_scope_files(root, include, exclude,
                                                           ignore_vcs, deadline):
                try:
                    size = fp.stat().st_size
                except OSError:
                    continue
                if size <= max_bytes:
                    continue
                out['large_files'] += 1
                out['total_bytes'] += size
                if not head_scan:
                    continue
                if out['scanned'] >= scan_limit:
                    out['partial'] = True
                    continue
                out['scanned'] += 1
                matches = self._head_scan_matches(fp, rel_str, pattern_re, head_bytes)
                if matches:
                    out['formatted'].extend(matches)
                    out['count'] += len(matches)
        except OSError:
            pass
        if time.monotonic() > deadline:
            out['partial'] = True
        return out

    def _probe_ignored_matches(self,
                               pattern: str,
                               path: Path,
                               include: str,
                               exclude: str,
                               context: int,
                               smart_case: bool,
                               timeout: float,
                               literal: bool = False) -> tuple:
        """Best-effort match records hiding behind VCS ignore rules (BUG_0061 / BUG_0202).

        With ignore_vcs=True the fast path passes NO explicit ignore flag, so ripgrep applies
        its built-in .gitignore/.ignore/.rgignore rules and silently prunes matching files. A
        search matching only gitignored files is then byte-identical to a genuine absence, and
        `rg --json` offers no way to recover the pruned paths (it emits only begin/end/match/
        context/summary — no ignored-path message). The only way to disambiguate is to ask rg
        a second time with the ignores turned off.

        Returns ``(records, rc)`` where ``records`` is a list of ``(rel_path, line_number,
        match_text)`` tuples ripgrep reports when `--no-ignore` is forced on, and ``rc`` is
        ripgrep's return code (0/1/2). Both are None if the probe could not be completed
        (ripgrep missing, timeout, error). Callers MUST treat ``(None, None)`` as "unknown" and
        stay silent — this probe exists only to disambiguate a zero-match result and must never
        turn a good search into an error. ``rc == 2`` means ripgrep hit a partial result (some
        files were skipped), so the caller can surface that as a partial-coverage flag.

        BUG_0202 (F3): returns the match RECORDS (not just a count) so the caller can filter
        them through the resolver and recover the tracked / not-ignored survivors. R2: the
        probe forces ``force_no_ignore=True, exclude_git=True`` — the SAME builder as the fast
        path but with DIFFERENT flags, so it is NOT an "identical argv" re-run.

        Note: only meaningful on the ripgrep fast path. Since BUG_0062 the Python fallback
        also honours ignore files (.gitignore/.ignore/.rgignore), so it is no longer immune
        to this class of false negative — but it honours them only to PRUNE the walk and
        never re-runs a probe to count what it pruned. The note therefore stays a
        fast-path-only feature by design: the fallback reports zero matches, which under the
        now-shared pruning semantics is a true statement about the files it searched.
        """
        import subprocess

        from agent_cascade.log import logger

        _rg_available, _ = _check_tool_availability()
        if not _rg_available:
            return None, None

        # WHY these two flags (BUG_0061): the probe's whole purpose is to ask rg a second
        # question with ignores turned OFF, so force_no_ignore is always True. It still keeps
        # '--glob !.git/' (exclude_git always True) — without that, `--hidden --no-ignore`
        # descends into .git/ internals and the probe becomes enormous and irrelevant.
        # Everything else comes from the shared builder so it cannot drift from the fast path.
        cmd = self._build_rg_argv(pattern,
                                  include,
                                  exclude,
                                  context,
                                  smart_case,
                                  force_no_ignore=True,
                                  exclude_git=True,
                                  literal=literal)

        # Bounded: never let the diagnostic probe exceed the caller's time budget.
        # Cap at 5s for sanity, but also clamp to the caller's own timeout so a
        # sub-second caller is not held up by the probe.
        probe_timeout = min(float(timeout), 5.0)
        try:
            result = subprocess.run(cmd,
                                    cwd=str(path),
                                    capture_output=True,
                                    text=True,
                                    encoding='utf-8',
                                    errors='replace',
                                    # BUG_0063: same hazard as the main fast path — rg gets no
                                    # path operand, so it would read the parent's stdin and block
                                    # until probe_timeout, at which point this returns None and the
                                    # BUG_0061 note silently disappears.
                                    stdin=subprocess.DEVNULL,
                                    timeout=probe_timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError, OSError) as e:
            logger.debug('grep: ignored-path probe failed: %s', e)
            return None, None

        if result.returncode not in (0, 1, 2):
            return None, None

        # BUG_0190: reuse the shared rg-JSON match parser (one `match` event == one match line;
        # submatches deliberately NOT counted) so the probe and the count path cannot drift apart.
        # BUG_0202 (F3): return the records (and the rc, so a partial rc==2 is surfaced) so the
        # caller can filter them through the resolver.
        return list(_iter_rg_json_matches(result.stdout)), result.returncode

    def _recover_ignored_matches(self,
                                 pattern: str,
                                 resolved: Path,
                                 include: str,
                                 exclude: str,
                                 context: int,
                                 smart_case: bool,
                                 literal: bool,
                                 timeout: float,
                                 ignore_vcs: bool) -> tuple:
        """BUG_0202 (F3): shared recovery helper for both zero-match call sites.

        Runs the ``--no-ignore`` probe, then filters its records through the resolver (F1+F2):
        a record survives when its path is NOT ignored — which, after F2, includes git-TRACKED
        files that an ignore rule would otherwise prune. Returns ``(survivors, probe_total,
        probe_partial)`` where ``survivors`` is a list of ``(rel_path, line_no, text)``,
        ``probe_total`` is the number of probe records, and ``probe_partial`` is True when the
        probe hit a partial result (rg rc==2) so the caller can surface it.

        Returns ``([], 0, False)`` when ``ignore_vcs`` is False (no rules were applied, so
        nothing was hidden) or the probe found nothing / could not be completed.

        KNOWN LIMITATION (R5): recovery only fires when the MAIN search returned ZERO matches.
        A tracked-but-ignored file is therefore invisible whenever at least one non-ignored
        match exists — the fast path returns that match and never runs the probe. The Python
        fallback (which honours F2 directly) does surface it, so the two paths can diverge in
        exactly that case; that residual is accepted, not fixed here.
        """
        if not ignore_vcs:
            return [], 0, False
        probe, probe_rc = self._probe_ignored_matches(
            pattern, resolved, include, exclude, context, smart_case, timeout, literal)
        if probe is None:  # probe failed / no records (rc None) — nothing to recover, not partial
            return [], 0, False
        if not probe:  # probe ran (rc 0/1) but found nothing; rc==2 is a partial result
            return [], 0, (probe_rc == 2)
        resolver = IgnoreResolver(resolved)
        survivors = []
        for rel, ln, text in probe:
            path = resolved / rel
            if not resolver.is_ignored(path, is_dir=False):
                survivors.append((rel, ln, text))
        return survivors, len(probe), (probe_rc == 2)

    def _try_subprocess_grep(self,
                             pattern: str,
                             path: Path,
                             include: str,
                             char_limit: int,
                             timeout: float,
                             agent_name: str = 'unknown',
                             exclude: str = '',
                             ignore_vcs: bool = True,
                             context: int = 0,
                             smart_case: bool = True,
                             literal: bool = False,
                             spill_file_path: Optional[str] = None):
        """Fast-path grep using system ripgrep or grep via subprocess.

        Returns (results_list, count, was_timed_out, was_truncated, original_output_size,
                 spillover_rel_path, total_lines_before, shown_lines_after) on success, or
        (None, 0, False, False, 0, None, 0, 0) on failure.
        Output format matches Python fallback: "relative_path:line_number: content"
        """
        import subprocess

        from agent_cascade.log import logger

        _rg_available, _grep_available = _check_tool_availability()

        if not _rg_available and not _grep_available:
            logger.debug('grep: subprocess fast path unavailable (rg=%s, grep=%s), falling back to Python',
                         _rg_available, _grep_available)
            return None, 0, False, False, 0, None, 0, 0

        try:
            if _rg_available:
                is_vcs_search = not ignore_vcs
                # WHY these two flags: ignore_vcs=False means "search everything" (per
                # dna.py contract), including .git. --no-ignore disables rg's built-in
                # ignores; combined with --hidden this lets rg descend into .git, matching the
                # Python fallback which does not prune skip_dirs when ignore_vcs=False.
                # Conversely ignore_vcs=True must KEEP .git excluded (see the builder's
                # exclude_git note), so the two knobs are complementary here. The probe
                # passes force_no_ignore=True, exclude_git=True — both always on.
                cmd = self._build_rg_argv(pattern,
                                          include,
                                          exclude,
                                          context,
                                          smart_case,
                                          force_no_ignore=is_vcs_search,
                                          exclude_git=not is_vcs_search,
                                          literal=literal)
            else:
                cmd = [
                    'grep',
                    '-r',
                    '-n',
                    '-F' if literal else '-E',  # -F: fixed-string (literal); -E: extended regex
                ]
                # BUG_0056: support comma-separated globs — one --include per pattern.
                if include and include != '*':
                    for _g in include.split(','):
                        _g = _g.strip()
                        if _g:
                            cmd.append('--include=' + _g)

                has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
                if smart_case:
                    if not re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
                        cmd.append('-i')

                if context > 0:
                    cmd.extend(['-C', str(context)])

                # BUG_0056: comma-separated excludes — one --exclude per pattern.
                if exclude:
                    for _e in exclude.split(','):
                        _e = _e.strip()
                        if _e:
                            cmd.append('--exclude=' + _e)

                if ignore_vcs:
                    for _dir in self._GREP_DEFAULT_EXCLUDE_DIRS:
                        cmd.extend(['--exclude-dir', _dir])

                for _exc in self._GREP_DEFAULT_EXCLUDES:
                    cmd.append('--exclude=' + _exc)

                cmd.extend(['-e', pattern])

            result = subprocess.run(cmd,
                                    cwd=str(path),
                                    capture_output=True,
                                    text=True,
                                    encoding='utf-8',
                                    errors='replace',
                                    # BUG_0063: rg is invoked with `cwd=` and NO path operand, so
                                    # whenever stdin is not a TTY (CI runners, piped shells, any
                                    # captured-stdin parent) it falls back to reading stdin and blocks
                                    # forever — surfacing as a spurious "Search timed out". DEVNULL
                                    # guarantees rg never consumes the parent's stdin.
                                    stdin=subprocess.DEVNULL,
                                    timeout=timeout)

            # Parse ripgrep's --json stdout into (formatted, match_count). Shared by the rc==0
            # and rc==2 paths so a partial-error search never discards matches that were found.
            def _parse_rg_json(stdout_text):
                # BUG_0190: match-event field extraction goes through the shared
                # _rg_match_fields so the line-mode parser and the count path share one
                # rg-JSON truth. Context events are handled inline (line mode only) to
                # preserve the match/context interleaving rg emits per file.
                formatted = []
                match_count = 0
                lines = stdout_text.split('\n') if stdout_text.strip() else []

                for line in lines:
                    if not line.strip():
                        continue

                    try:
                        json_obj = json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.debug('ripgrep JSON parse error: %s', e)
                        continue

                    entry_type = json_obj.get('type', '')
                    if entry_type == 'match':
                        normalized_path, line_num, match_text = _rg_match_fields(json_obj)
                        # BUG_0144: marker only in context mode; BUG_0145 text via _rg_match_fields.
                        marker = _GREP_MATCH_MARKER if context > 0 else ''
                        formatted.append(_format_grep_entry(normalized_path, line_num, match_text, marker))
                        match_count += 1
                    elif entry_type == 'context' and context > 0:
                        data = json_obj.get('data', {})
                        file_path = data.get('path', {}).get('text', '')
                        line_num_data = data.get('line_number', 0)
                        if isinstance(line_num_data, dict):
                            line_num = line_num_data.get('start', 0)
                        else:
                            line_num = line_num_data
                        # BUG_0144: no indent on context lines — raw file bytes only.
                        # BUG_0145: lines.bytes fallback lives in _rg_lines_text.
                        match_text = _rg_lines_text(data.get('lines', {}))
                        normalized_path = file_path.replace('\\', '/')
                        formatted.append(_format_grep_entry(normalized_path, line_num, match_text))

                return formatted, match_count

            # rc==0 (matches found) and rc==2 (matches found but some files errored) both carry
            # valid match data on stdout — parse and surface them through the SAME truncation path
            # below so a partial-error search can never produce unbounded output. A genuine usage
            # error (rc=2 with no parseable matches) is detected inside this block and falls back
            # to Python before reaching the truncation code.
            _rg_partial_error = False
            if result.returncode == 0 or (result.returncode == 2 and _rg_available):
                lines = result.stdout.split('\n') if result.stdout.strip() else []

                formatted = []

                if _rg_available:
                    formatted, count = _parse_rg_json(result.stdout)
                    # rc==2 with NO parseable matches is a genuine error (e.g. invalid regex or an
                    # unrecognized flag), NOT "matches found but some files skipped". Fall back to
                    # Python so the real error surfaces (Python raises re.error for bad patterns).
                    if result.returncode == 2 and count == 0:
                        stderr_msg = (result.stderr or '').strip()[:500]
                        # BUG_0169: when --max-filesize filtered out EVERY candidate, rg exits 2
                        # with "No files were searched" — that is a normal empty result, not a
                        # usage error. Reporting it as one made grep() fall back to the Python
                        # scan instead of taking the over-size header path.
                        if 'No files were searched' in stderr_msg and _grep_max_file_size_mb() > 0:
                            return [], 0, False, False, 0, None, 0, 0
                        logger.warning(
                            f"grep subprocess failed with exit code 2 (no matches recovered; falling back to Python): {stderr_msg}")
                        return None, 0, False, False, 0, None, 0, 0
                    # rc==2 that DID yield matches means some files were skipped (e.g. permission
                    # denied) — warn once so the partial nature is visible without discarding matches.
                    if result.returncode == 2 and count > 0:
                        _rg_partial_error = True

                else:
                    _match_re = re.compile(r'^(.+?):(\d+):(.*)$')
                    _ctx_re = re.compile(r'^(.+?)-(\d+)-(.*)$')

                    for line in lines:
                        if not line:
                            continue
                        if line == '---' or line == '--':
                            formatted.append('---')
                            continue

                        m = _match_re.match(line)
                        if m:
                            raw_path, linenum, content = m.groups()
                            normalized_path = raw_path.replace('\\', '/')
                            # BUG_0144: content stays verbatim; only the match marker is added.
                            if context > 0 and normalized_path.startswith(' '):
                                normalized_path = normalized_path[1:]
                                formatted.append(_format_grep_entry(normalized_path, linenum, content))
                            elif context > 0:
                                formatted.append(_format_grep_entry(normalized_path, linenum, content,
                                                                    _GREP_MATCH_MARKER))
                            else:
                                formatted.append(_format_grep_entry(normalized_path, linenum, content))
                        elif context > 0:
                            c = _ctx_re.match(line)
                            if c:
                                ctx_path, ctx_linenum, ctx_content = c.groups()
                                normalized_ctx_path = ctx_path.replace('\\', '/')
                                formatted.append(_format_grep_entry(normalized_ctx_path, ctx_linenum, ctx_content))
                            else:
                                formatted.append(line)
                        else:
                            formatted.append(line)

                    if context > 0:
                        count = sum(1 for entry in formatted if '>>>' in entry)
                    else:
                        count = sum(1 for entry in formatted if entry != '---')

                _was_truncated = False
                _original_output_size = 0
                _spill_rel_path = None
                _total_lines_before = len(formatted)
                _shown_lines_after = len(formatted)
                if char_limit != -1 and count > 0:
                    output_size = sum(len(entry) for entry in formatted) + count
                    if output_size > char_limit:
                        _original_output_size = output_size

                        # Write spillover file for full output (side effect only;
                        # truncation is done below with line-aware byte budget).
                        # truncate_with_spillover returns the truncated text WITH a
                        # "...Full output saved to: <rel>" notice appended — we keep
                        # only that rel path here and do our own line-based truncation.
                        full_text = '\n'.join(formatted)
                        _truncated_with_notice = truncate_with_spillover(
                            full_text,
                            char_limit,
                            instance_name=agent_name,
                            tool_name='grep',
                            base_dir=self.base_dir,
                            operation_mode='head',
                            spill_path=_resolve_spill_path(spill_file_path),
                        )  # return value used only to recover the spillover rel path
                        _spill_rel_path = self._extract_spill_path(_truncated_with_notice)

                        byte_budget = char_limit
                        truncated = []
                        for line in formatted:
                            if byte_budget < len(line) + 1:
                                break
                            truncated.append(line)
                            byte_budget -= len(line) + 1
                        formatted = truncated
                        _shown_lines_after = len(formatted)
                        if context > 0:
                            count = sum(1 for entry in formatted if '>>>' in entry)
                        else:
                            count = sum(1 for entry in formatted if entry != '---')
                        _was_truncated = True

                if _rg_partial_error:
                    stderr_msg = (result.stderr or '').strip()[:500]
                    logger.warning(
                        f"grep subprocess found {count} match(es) but exited with code 2 "
                        f"(partial errors; some files may have been skipped): {stderr_msg}")

                return (formatted, count, False, _was_truncated, _original_output_size, _spill_rel_path,
                        _total_lines_before, _shown_lines_after)

            # Non-zero return code (e.g., grep returns 1 for no matches) — still valid.
            # Note: rc==2 is fully handled in the success path above (matches found → surfaced;
            # no matches → already returned None to fall back to Python), so it never reaches here.
            if result.returncode == 1:
                return [], 0, False, False, 0, None, 0, 0

            # Unexpected non-zero exit code (e.g., rg returns 3+ for internal errors)
            stderr_msg = (result.stderr or '').strip()[:500]
            logger.warning(
                f"grep subprocess failed with exit code {result.returncode} (falling back to Python): {stderr_msg}")
            return None, 0, False, False, 0, None, 0, 0

        except subprocess.TimeoutExpired:
            logger.warning(
                f"grep subprocess timed out after {int(timeout)}s; falling back to Python"
            )
            return None, 0, True, False, 0, None, 0, 0

        except (FileNotFoundError, PermissionError, OSError) as e:
            logger.debug(f"grep subprocess unavailable (falling back to Python): {e}")

        return None, 0, False, False, 0, None, 0, 0

    def _over_cap_rel_paths(self, root: Path, include: str, exclude: str,
                            ignore_vcs: bool, max_bytes: int, deadline: float):
        """BUG_0190/BUG_0202: ``(count, rel_paths, truncated)`` for in-scope files over the cap.

        Reuses :func:`_iter_grep_scope_files` (the same include/exclude/ignore filtering the rg
        ``--max-filesize`` fast path applies) so the over-cap set the INCOMPLETE note names is
        exactly the set rg skipped. Paths are capped at 11 (the note shows 10 + a "+N more").
        ``truncated`` (F5) is True when the walk hit its deadline before seeing the whole scope,
        so the caller knows the over-cap set — and any count built on it — is partial, not total.
        """
        paths: list = []
        count = 0
        for fp, rel_str in self._iter_grep_scope_files(root, include, exclude, ignore_vcs, deadline):
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            if size > max_bytes:
                count += 1
                if len(paths) < 11:
                    paths.append(rel_str)
        # F5 (BUG_0202/RC-B): the generator self-terminates at `deadline`, so a single post-loop
        # check is the per-call truncation signal (the same post-loop deadline idiom
        # _scan_large_files uses for its scan-limit budget). Without it a truncated over-cap
        # walk is indistinguishable from a complete one.
        truncated = time.monotonic() > deadline
        return count, paths, truncated

    def _grep_count_only(self,
                         resolved: Path,
                         pattern: str,
                         include: str,
                         exclude: str,
                         ignore_vcs: bool,
                         smart_case: bool,
                         literal: bool,
                         timeout: float,
                         agent_name: str,
                         searched_note: str) -> str:
        """BUG_0190: count matching lines per file (plus a per-alternative breakdown) without
        materializing the matched lines.

        The rg fast path reuses the exact ``_build_rg_argv`` (context forced to 0 — counts are
        line-level) and parses ``--json`` via the shared ``_iter_rg_json_matches``; the Python
        fallback walks via ``_iter_grep_scope_files`` and counts per file. Counts are matching
        LINES (submatches are not counted) and totals are computed before any display cap.
        Over-cap files are skipped and named with the shared ``[search INCOMPLETE ...]`` note
        (the BUG_0185 integrity link), so a count over a partially-searched scope is never read
        as a full-scope total.
        """
        import subprocess

        from agent_cascade.log import logger

        # Compile once for the Python fallback and the per-alternative breakdown.
        has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
        if smart_case and re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
            flags = 0
        elif smart_case:
            flags = re.IGNORECASE
        else:
            flags = 0
        _compile_pattern = re.escape(pattern) if literal else pattern
        try:
            pattern_re = _compile_grep_pattern(_compile_pattern, flags=flags)
        except re.error as e:
            return (f"ERROR: Invalid regex pattern '{pattern}': {str(e)}. "
                    f"Please provide a valid Python regular expression.")
        alts, alt_res = _compile_alternatives(pattern, literal, flags)

        _max_mb, _head_bytes, _scan_limit, _budget_s = _grep_large_file_limits()
        max_bytes = _max_mb * 1024 * 1024 if _max_mb > 0 else 0

        per_file: dict = {}
        per_pattern: dict = {}
        total = 0
        skipped_count = 0
        skipped_paths: list = []
        # F5 (BUG_0202/RC-B): two INDEPENDENT truncation flags. rg_path_timed_out = the rg fast
        # path was killed at the caller's deadline (the Python fallback below may still finish
        # the whole scope, in which case the total is COMPLETE); fallback_truncated = the Python
        # fallback walk itself hit the deadline (the total is genuinely partial). A "partial"
        # claim is only valid for the latter — a fast-path timeout whose fallback completed must
        # not be read as a partial total.
        rg_path_timed_out = False
        fallback_truncated = False
        # F5 (BUG_0202/RC-B): a separate flag for the over-cap DIAGNOSTIC walk hitting its own
        # 0.75 s budget (GREP_LARGE_FILE_SCAN_BUDGET_S) — independent of the caller's deadline,
        # so a walk that merely ran out of its budget is not misreported as a caller timeout.
        over_cap_walk_partial = False
        partial_error = False
        used_rg = False

        def _tally(rel: str, text: str):
            nonlocal total
            per_file[rel] = per_file.get(rel, 0) + 1
            total += 1
            if alt_res:
                for alt, r in zip(alts, alt_res):
                    if r.search(text):
                        per_pattern[alt] = per_pattern.get(alt, 0) + 1

        _rg_available, _ = _check_tool_availability()
        if _rg_available:
            is_vcs_search = not ignore_vcs
            # context=0: counts are line-level, so no context flag is ever needed.
            cmd = self._build_rg_argv(pattern,
                                      include,
                                      exclude,
                                      0,
                                      smart_case,
                                      force_no_ignore=is_vcs_search,
                                      exclude_git=not is_vcs_search,
                                      literal=literal)
            result = None
            try:
                result = subprocess.run(cmd,
                                        cwd=str(resolved),
                                        capture_output=True,
                                        text=True,
                                        encoding='utf-8',
                                        errors='replace',
                                        # BUG_0063: DEVNULL so rg never consumes the parent's stdin.
                                        stdin=subprocess.DEVNULL,
                                        timeout=timeout)
            except subprocess.TimeoutExpired:
                # The rg subprocess ran for the caller's `timeout` seconds before being killed,
                # so the FAST PATH hit the deadline. The Python fallback below may still finish
                # the whole scope — in which case the total is complete — so this is a
                # fast-path-only flag, NOT a "partial" claim (the fallback's own deadline check
                # sets `fallback_truncated` if it too runs out of time).
                rg_path_timed_out = True
                logger.warning(f"grep count subprocess timed out after {int(timeout)}s; falling back to Python")
            except (FileNotFoundError, PermissionError, OSError) as e:
                logger.debug(f"grep count subprocess unavailable (falling back to Python): {e}")
            if result is not None and result.returncode in (0, 1, 2):
                used_rg = True
                for p, _ln, text in _iter_rg_json_matches(result.stdout):
                    _tally(p, text)
                # rc==2 that DID yield matches = some files were skipped (e.g. permission denied).
                partial_error = (result.returncode == 2)
                # Over-cap files were skipped by --max-filesize: name them so the count is never
                # read as a full-scope total (the BUG_0185 integrity link). "No files were
                # searched" (every candidate over-cap) is a normal empty result — the note below.
                if max_bytes:
                    skipped_count, skipped_paths, _over_cap_truncated = self._over_cap_rel_paths(
                        resolved, include, exclude, ignore_vcs, max_bytes,
                        time.monotonic() + _budget_s)
                    # F5 (BUG_0202/RC-B): a truncated over-cap walk means the skip set (and thus
                    # the count) is partial — surface it through its own flag, NOT the caller-
                    # deadline flags (the walk's 0.75 s budget is not the caller's deadline).
                    over_cap_walk_partial = over_cap_walk_partial or _over_cap_truncated
        if not used_rg:
            # Python count fallback: walk the same scope, count per file, header-scan over-cap.
            deadline = time.monotonic() + timeout
            scanned = 0
            for fp, rel_str in self._iter_grep_scope_files(resolved, include, exclude, ignore_vcs, deadline):
                try:
                    size = fp.stat().st_size
                except OSError:
                    continue
                if max_bytes and size > max_bytes:
                    skipped_count += 1
                    if len(skipped_paths) < 11:
                        skipped_paths.append(rel_str)
                    if scanned < _scan_limit:
                        scanned += 1
                        _hits = self._head_scan_matches(fp, rel_str, pattern_re, _head_bytes)
                        if _hits:
                            per_file[rel_str] = per_file.get(rel_str, 0) + len(_hits)
                            total += len(_hits)
                    continue
                try:
                    # BUG_0145: 'replace' keeps non-UTF-8 lines countable, matching the rg path.
                    content = fp.read_text(encoding='utf-8', errors='replace')
                except Exception:
                    continue
                for line in content.split('\n'):
                    if pattern_re.search(line):
                        _tally(rel_str, line)
            # F5 (BUG_0202/RC-B): the generator self-terminates at `deadline`, so the in-loop
            # deadline check was unreachable — a single post-loop check is the per-call
            # truncation signal (the _scan_large_files idiom). Without it a truncated fallback
            # walk reports a bare total (or a bare zero) with no timeout disclosure.
            if time.monotonic() > deadline:
                fallback_truncated = True

        return self._format_count_result(
            pattern, searched_note, per_file, per_pattern, total,
            alts, partial_error, rg_path_timed_out, fallback_truncated, over_cap_walk_partial,
            skipped_count, skipped_paths, _max_mb,
            ignore_vcs, resolved, include, exclude, smart_case, literal, timeout,
            over_cap_budget_s=_budget_s)

    def _format_count_result(self,
                             pattern: str,
                             searched_note: str,
                             per_file: dict,
                             per_pattern: dict,
                             total: int,
                             alts,
                             partial_error: bool,
                             rg_path_timed_out: bool,
                             fallback_truncated: bool,
                             over_cap_walk_partial: bool,
                             skipped_count: int,
                             skipped_paths: list,
                             max_mb: int,
                             ignore_vcs: bool,
                             resolved: Path,
                             include: str,
                             exclude: str,
                             smart_case: bool,
                             literal: bool,
                             timeout: float,
                             *,
                             over_cap_budget_s: Optional[float] = None) -> str:
        """BUG_0190: render a count-only result.

        Zero matches get a bare line (plus the BUG_0061 hidden-matches probe note and the
        over-cap INCOMPLETE note). Non-zero gets the per-file list (capped at 50, totals always
        full) + the per-pattern breakdown + the integrity notes.
        """
        # F5 (BUG_0202/RC-B): word the timeout note PER PATH. A fallback truncation is a real
        # "partial" (the totals are incomplete); a fast-path timeout whose fallback completed is
        # a complete total computed by the fallback — say so, and do NOT claim "partial".
        if fallback_truncated:
            _timeout_note = f" [TIMED OUT after {int(timeout)}s — counts are partial, not totals]"
        elif rg_path_timed_out:
            _timeout_note = (f" [note: the ripgrep fast path timed out after {int(timeout)}s; "
                             'totals were computed by the Python fallback]')
        else:
            _timeout_note = ''
        # The over-cap budget is derived from the same settings source _grep_large_file_limits()
        # uses (no re-hardcoded default); a direct caller that omits it gets the live value.
        if over_cap_budget_s is None:
            over_cap_budget_s = _grep_large_file_limits()[3]
        if total == 0:
            # BUG_0190: no (N files searched) parenthetical — the rg path does not
            # track a file count (rg handles traversal internally), so the number
            # would be 0 on the fast path and non-zero on the fallback. The
            # searched_note already names the search root.
            # BUG_0202 (F3): recover matches hidden behind ignore rules. If any survive the
            # resolver filter (tracked or not-ignored), render them as a real count result
            # instead of a bare zero — a tracked file is a real match, not an absence.
            survivors, probe_total, probe_partial = self._recover_ignored_matches(
                pattern, resolved, include, exclude, 0, smart_case, literal, timeout, ignore_vcs)
            if survivors:
                per_file = {}
                for rel, _ln, _text in survivors:
                    per_file[rel] = per_file.get(rel, 0) + 1
                total = len(survivors)
                # The probe records carry no per-alternative breakdown, so drop `alts`; the
                # caller-deadline flags (rg_path_timed_out / fallback_truncated) pass through
                # unchanged, and the probe's rc==2 partial is ORed into partial_error so a
                # recovered result is never read as a clean total when the search was partial.
                alts = []
                partial_error = partial_error or probe_partial
                # fall through to the non-zero rendering below
            else:
                base = f"0 matching lines for '{pattern}'{searched_note}"
                if probe_total:
                    # F4 (BUG_0202/RC-C): state what was consulted, never assert git status.
                    base += _ignore_advisory_note(probe_total)
                if probe_partial:
                    # F5 (BUG_0202/RC-B): the probe itself was truncated (rg rc==2) — the "no
                    # survivors" verdict only covers a partial probe, so disclose it (mirrors the
                    # line-mode note; the count path previously dropped probe_partial here).
                    base += ' [note: the ignore-rule probe returned a partial result]'
                base += _incomplete_note(skipped_count, skipped_paths, max_mb)
                # F5 (BUG_0202/RC-B): a truncation must be disclosed in the zero-match branch too
                # (worded per path — a fast-path timeout whose fallback finished is not "partial").
                base += _timeout_note
                if over_cap_walk_partial:
                    base += _over_cap_partial_note(over_cap_budget_s)
                return base

        out = f"Count-only results for '{pattern}'{searched_note}:"
        items = sorted(per_file.items(), key=lambda kv: kv[1], reverse=True)
        shown = items[:50]
        for rel, c in shown:
            out += f"\n{rel}: {c}"
        if len(items) > 50:
            out += f"\n(+{len(items) - 50} more files — totals are complete)"
        out += f"\n[total: {total} matching lines in {len(per_file)} file(s)]"
        if alts:
            pp = ', '.join(f"'{alt}': {per_pattern.get(alt, 0)}" for alt in alts)
            out += f"\n[per-pattern: {pp} across {total} matching lines]"
        out += _incomplete_note(skipped_count, skipped_paths, max_mb)
        if partial_error:
            out += ' [note: some files were skipped due to errors — counts are partial]'
        out += _timeout_note
        if over_cap_walk_partial:
            out += _over_cap_partial_note(over_cap_budget_s)
        return out

    def _grep_single_file(self,
                          file_path: Path,
                          pattern: str,
                          char_limit: int,
                          include: str = '*',
                          exclude: str = '',
                          context: int = 0,
                          smart_case: bool = True,
                          agent_name: str = 'unknown',
                          spill_file_path: Optional[str] = None,
                          timeout: float = 5.0,
                          searched_note: str = '',
                          literal: bool = False,
                          *,
                          count_only: bool = False) -> str:
        """Search a single file for a regex pattern (used when path is a file). BUG_0190: count_only counts matching lines without materializing them."""

        try:
            normalized_rel_path = str(file_path.relative_to(self.base_dir)).replace('\\', '/')
        except ValueError:
            normalized_rel_path = file_path.name

        if not fnmatch.fnmatch(normalized_rel_path, include):
            return f"No matches found for pattern '{pattern}' in {file_path.name}{searched_note}"
        if exclude and fnmatch.fnmatch(normalized_rel_path, exclude):
            return f"No matches found for pattern '{pattern}' in {file_path.name} (excluded by {exclude}){searched_note}"

        has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
        if smart_case and re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
            flags = 0
        elif smart_case:
            flags = re.IGNORECASE
        else:
            flags = 0

        _compile_pattern = re.escape(pattern) if literal else pattern
        try:
            pattern_re = _compile_grep_pattern(_compile_pattern, flags=flags)
        except re.error as e:
            return f"ERROR: Invalid regex pattern '{pattern}': {str(e)}. Please provide a valid Python regular expression."

        try:
            # BUG_0145: decode with 'replace' (not 'ignore') so a line containing invalid
            # bytes still shows the surrounding text, matching read_file and the rg fast
            # path's lines.bytes decoding. 'ignore' silently deleted those bytes.
            content = file_path.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            return f"Error reading file {file_path.name}: {str(e)}"

        lines = content.split('\n')
        if count_only:
            # BUG_0190: count matching lines without building result lines.
            alts, alt_res = _compile_alternatives(pattern, literal, flags)
            cnt = 0
            per_pattern = {}
            for line in lines:
                if pattern_re.search(line):
                    cnt += 1
                    if alt_res:
                        for alt, r in zip(alts, alt_res):
                            if r.search(line):
                                per_pattern[alt] = per_pattern.get(alt, 0) + 1
            if cnt == 0:
                return f"0 matching lines for '{pattern}' in {file_path.name}{searched_note}"
            out = (f"Count-only results for '{pattern}'{searched_note}:\n"
                  f"{normalized_rel_path}: {cnt}\n"
                  f"[total: {cnt} matching lines in 1 file(s)]")
            if alts:
                pp = ', '.join(f"'{alt}': {per_pattern.get(alt, 0)}" for alt in alts)
                out += f"\n[per-pattern: {pp} across {cnt} matching lines]"
            return out

        results = []
        match_count = 0
        hit_result_limit = False
        was_timed_out = False
        start_time = time.time()

        if context > 0:
            for line_num, line in enumerate(lines, 1):
                if pattern_re.search(line):
                    match_count += 1
                    start = max(1, line_num - context)
                    end = min(len(lines), line_num + context)
                    for ctx_line in range(start - 1, end):
                        marker = _GREP_MATCH_MARKER if ctx_line + 1 == line_num else ''
                        results.append(_format_grep_entry(normalized_rel_path, ctx_line + 1, lines[ctx_line], marker))
                    results.append('---')
                if len(results) % 200 == 0 and time.time() - start_time > timeout:
                    was_timed_out = True
                    break
                if len(results) > 5000:
                    hit_result_limit = True
                    break
        else:
            for line_num, line in enumerate(lines, 1):
                if pattern_re.search(line):
                    match_count += 1
                    results.append(f"{normalized_rel_path}:{line_num}: {line}")
                if len(results) % 500 == 0 and time.time() - start_time > timeout:
                    was_timed_out = True
                    break
                if len(results) > 5000:
                    hit_result_limit = True
                    break

        if not results:
            return f"No matches found for pattern '{pattern}' in {file_path.name}{searched_note}"

        summary = f"Found {match_count} matches for '{pattern}'{searched_note}"
        if context > 0:
            summary += f" (with {context} line(s) of context)"
        output_text = _grep_body_with_legend(results, context, match_count)

        if was_timed_out:
            summary += f" [TIMED OUT after {int(timeout)}s]"
        elif hit_result_limit:
            summary += ' [TRUNCATED at 5000 results]'

        result = truncate_with_spillover(
            output_text,
            char_limit,
            instance_name=agent_name,
            tool_name='grep',
            base_dir=self.base_dir,
            operation_mode='head',
            spill_path=_resolve_spill_path(spill_file_path),
        )
        if result is not output_text:
            output_text = result
            summary += ' [TRUNCATED]'

        return f"{summary}:\n\n" + output_text

    def grep(self,
             pattern: str,
             path: str = '.',
             include: str = '*',
             char_limit: int = 2000,
             timeout: float = 5.0,
             agent_name: str = 'unknown',
             exclude: str = '',
             ignore_vcs: bool = True,
             context: int = 0,
             smart_case: bool = True,
             literal: bool = False,
             spill_file_path: Optional[str] = None,
             *,
             count_only: bool = False) -> str:
        """Search for text pattern in files.

        Uses subprocess-based grep (ripgrep or system grep) as a fast path,
        falling back to pure Python if the subprocess approach fails/times out.
        """

        try:
            from agent_cascade.log import logger
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return f"Directory not found: {path}"

            # BUG_0087 invariant: the absolute root that was actually searched is echoed in
            # EVERY result header — directory AND single-file. It is appended, never
            # reformatted, so prefix-matching consumers (see scripts/grep_compare.py) stay
            # byte-stable; as_posix() keeps grep's forward-slash output convention.
            searched_note = f" (searched: {resolved.as_posix()})"

            # BUG_0167: recover a pattern that arrived HTML-escaped (e.g. `&lt;` for `<`).
            # Done BEFORE the fast path so ripgrep gets the corrected pattern too, and before
            # searched_note is used so every result path carries the explanatory note.
            html_note = ''
            if not literal and _HTML_ENTITY_RE.search(pattern):
                pattern, html_note = _recover_html_escaped_pattern(pattern)
                searched_note += html_note

            # Sanitize glob patterns to prevent path traversal via include/exclude
            try:
                include = self._sanitize_glob_pattern(include)
            except ValueError as e:
                return f"ERROR: Invalid glob pattern in 'include': {e}"

            if exclude:
                try:
                    exclude = self._sanitize_glob_pattern(exclude)
                except ValueError as e:
                    return f"ERROR: Invalid glob pattern in 'exclude': {e}"

            if resolved.is_file():
                return self._grep_single_file(resolved,
                                              pattern,
                                              char_limit,
                                              include=include,
                                              exclude=exclude,
                                              context=context,
                                              smart_case=smart_case,
                                              agent_name=agent_name,
                                              spill_file_path=spill_file_path,
                                              timeout=timeout,
                                              searched_note=searched_note,
                                              literal=literal,
                                              count_only=count_only)

            # BUG_0190: count-only mode returns per-file matching-line counts (plus a
            # per-alternative breakdown) instead of the matched lines. Dispatched before the
            # line-mode fast/slow paths so the line-mode output stays byte-identical.
            if count_only:
                return self._grep_count_only(resolved,
                                             pattern,
                                             include,
                                             exclude,
                                             ignore_vcs,
                                             smart_case,
                                             literal,
                                             timeout,
                                             agent_name,
                                             searched_note)

            # ── Fast path: try subprocess-based grep (ripgrep or system grep) ──
            (results, count, was_timed_out, _sub_truncated, _original_output_size, _sub_spill_rel, _sub_total_lines,
             _sub_shown_lines) = self._try_subprocess_grep(
                  pattern=pattern,
                  path=resolved,
                  include=include,
                  char_limit=char_limit,
                  timeout=timeout,  # Use configurable timeout
                  agent_name=agent_name,
                  exclude=exclude,
                  ignore_vcs=ignore_vcs,
                  context=context,
                  smart_case=smart_case,
                  literal=literal,
                  spill_file_path=spill_file_path)
            if was_timed_out:
                # BUG_0169: name the real cause when the scope is dominated by over-size
                # artifacts. The old message claimed the pattern/scope was "too broad", which
                # was already false: a single directory + an extension filter is as narrow as
                # the caller can get.
                _max_mb, _head_bytes, _scan_limit, _budget_s = _grep_large_file_limits()
                _diag = self._scan_large_files(resolved, include, exclude, pattern,
                                               ignore_vcs=ignore_vcs, smart_case=smart_case,
                                               literal=literal, head_scan=False) \
                    if _max_mb > 0 else {'large_files': 0, 'total_bytes': 0}
                # BUG_0185: the diagnostic walk runs under a budget (GREP_LARGE_FILE_SCAN_BUDGET_S),
                # so on a huge tree it can expire BEFORE reaching the oversized files and report
                # large_files==0 even though they exist. partial=True is the helper's "I did not see
                # the whole scope" flag — when it is set the "no large files" reading is
                # inconclusive, so fall through to the Python fallback (skip-and-continue) instead of
                # claiming the scope was "too broad".
                if not _diag['large_files'] and not _diag.get('partial'):
                    return f"ERROR: Search timed out after {int(timeout)}s — the pattern/scope was too broad to be useful. Narrow your search to a specific directory or use a more specific pattern."
                # BUG_0185: large files in scope — fall through to the Python fallback,
                # which skips over-size files and continues searching the rest of the
                # corpus. The fallback reports the skipped files as a "search INCOMPLETE"
                # note so a caller knows absence is not conclusive.
                # Note: this means the scope is walked twice (diag scan + fallback walk).
                # Intentional: the diagnostic only counts large files; the fallback does
                # the actual content search and header recovery the diagnostic can't.
            if results is not None:
                if count == 0 and not _sub_truncated:
                    logger.debug(f"grep: subprocess found no matches for '{pattern}'")
                    summary = f"No matches found for '{pattern}' in {path}{searched_note}"
                    # BUG_0061 / BUG_0202 (F3): with ignore_vcs=True the fast path passes no
                    # explicit ignore flag, so ripgrep's built-in .gitignore rules can prune the
                    # only matching file — making "no matches" indistinguishable from "matches
                    # were hidden". Probe once with --no-ignore, then filter through the resolver
                    # (F1+F2): if any record survives (tracked or not-ignored), return it as a
                    # real result instead of "No matches" — a tracked file is a real match, not an
                    # absence. When nothing survives, keep "No matches found" byte-preserved and
                    # append the F4 advisory (states what was consulted, never asserting git
                    # status). The probe is gated on ignore_vcs inside _recover_ignored_matches.
                    survivors, probe_total, probe_partial = self._recover_ignored_matches(
                        pattern, resolved, include, exclude, context, smart_case,
                        literal, timeout, ignore_vcs)
                    if survivors:
                        # BUG_0202 (F3): route the recovered body through the shared render rule
                        # (_format_grep_entry / _grep_body_with_legend) so it cannot drift from
                        # the rg/GNU/Python paths, and cap it with truncate_with_spillover. The
                        # probe yields match events only (no context lines), so context is
                        # dropped here — disclose it rather than silently discarding it.
                        _body_lines = [_format_grep_entry(rel, ln, text)
                                      for rel, ln, text in survivors]
                        _recovered_body = _grep_body_with_legend(_body_lines, 0, len(survivors))
                        _ctx_note = (f"\n\n[context lines not shown — recovery re-runs the probe, "
                                     f"which yields match lines only]" if context > 0 else '')
                        _recovered_out = (f"Found {len(survivors)} matches for '{pattern}'"
                                         f"{searched_note}:\n\n" + _recovered_body + _ctx_note)
                        # truncate_with_spillover appends its own spill/truncation notice; the main
                        # line-mode path keeps [TRUNCATED] in the header, so do not double it here.
                        return truncate_with_spillover(
                            _recovered_out,
                            char_limit,
                            instance_name=agent_name,
                            tool_name='grep',
                            base_dir=self.base_dir,
                            operation_mode='head',
                            spill_path=_resolve_spill_path(spill_file_path),
                        )
                    if probe_total:
                        # F4 (BUG_0202/RC-C): state what was consulted, never assert git status.
                        summary += _ignore_advisory_note(probe_total)
                    if probe_partial:
                        # F5 (BUG_0202/RC-B): the probe itself was truncated (rg rc==2), so the
                        # "no survivors" verdict only covers a partial probe — disclose it rather
                        # than dropping the flag silently (count mode surfaces it in its own note).
                        summary += ' [note: the ignore-rule probe returned a partial result]'
                    # BUG_0169: the streaming search skipped over-size files. Recover header
                    # matches from them; otherwise say the scope was only partially covered
                    # instead of claiming a clean "No matches".
                    _max_mb, _head_bytes, _scan_limit, _budget_s = _grep_large_file_limits()
                    if _max_mb > 0:
                        _large = self._scan_large_files(resolved, include, exclude, pattern,
                                                        ignore_vcs=ignore_vcs, smart_case=smart_case,
                                                        literal=literal, head_scan=True)
                        if _large['count'] > 0:
                            _head_kb = _head_bytes // 1024
                            _summary = f"Found {_large['count']} matches for '{pattern}'{searched_note}"
                            _partial = ' (scan limit reached)' if _large['partial'] else ''
                            _summary += (f" [search INCOMPLETE — matched in the first {_head_kb} KiB of "
                                         f"{_large['scanned']} file(s) > {_max_mb} MB{_partial}; "
                                         f"full file content was not streamed — use read_file for more]")
                            return f"{_summary}:\n\n" + '\n'.join(_large['formatted'])
                        if _large['large_files']:
                            summary += (f" [search INCOMPLETE — {_large['large_files']} file(s) > "
                                        f"{_max_mb} MB were not searched in full (only the first "
                                        f"{_head_bytes // 1024} KiB was scanned); use read_file to "
                                        f"inspect their headers/content]")
                    return f"{summary}:\n\n"
                else:
                    output_text = _grep_body_with_legend(results, context, count)
                    summary = f"Found {count} matches for '{pattern}'{searched_note}"
                    if context > 0:
                        summary += f" (with {context} line(s) of context)"

                    if _sub_truncated:
                        # Fast path already line-truncated the body to fit the byte budget and
                        # wrote the full output to a spillover file (path captured in
                        # _try_subprocess_grep). Skip truncate_with_spillover here — calling it
                        # would be a no-op on already-bounded text and risks re-writing the
                        # spillover file with truncated content. Surface the captured path,
                        # matching the format read_file / the Python fallback produce.
                        if _sub_spill_rel:
                            summary += ' ' + format_truncation_notice(
                                shown_lines=_sub_shown_lines,
                                total_lines=_sub_total_lines,
                                total_chars=_original_output_size,
                                spill_path=_sub_spill_rel,
                            )
                        else:
                            summary += ' [TRUNCATED]'
                    else:
                        result = truncate_with_spillover(
                            output_text,
                            char_limit,
                            instance_name=agent_name,
                            tool_name='grep',
                            base_dir=self.base_dir,
                            operation_mode='head',
                            spill_path=_resolve_spill_path(spill_file_path),
                        )
                        if result is not output_text:
                            output_text = result
                            summary += ' [TRUNCATED]'

                    if _sub_truncated:
                        # BUG_0070: append an end-of-body marker so the truncation is visible at
                        # the point where the evidence stops, not only in the header. The header
                        # notice alone lets the (already line-truncated) body read as complete.
                        omitted = (_sub_total_lines or 0) - (_sub_shown_lines or 0)
                        if omitted > 0:
                            _marker = f"\n\n[… {omitted} more line(s) omitted — "
                            if _sub_spill_rel:
                                output_text += _marker + f"re-run with a narrower pattern or read the spillover file {_sub_spill_rel}]"
                            else:
                                output_text += _marker + 're-run with a narrower pattern to avoid truncation]'

                    return f"{summary}:\n\n" + output_text

            # ── Slow path: pure Python fallback ──
            import os

            _rg_avail, _grep_avail = _check_tool_availability()
            logger.debug(
                f"grep: subprocess fast path unavailable (rg={_rg_avail}, grep={_grep_avail}), falling back to Python")
            results = []

            has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
            if smart_case and re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
                flags = 0
            elif smart_case:
                flags = re.IGNORECASE
            else:
                flags = 0
            _compile_pattern = re.escape(pattern) if literal else pattern
            try:
                pattern_re = _compile_grep_pattern(_compile_pattern, flags=flags)
            except re.error as e:
                return f"ERROR: Invalid regex pattern '{pattern}': {str(e)}. Please provide a valid Python regular expression."

            start_time = time.time()
            # Use the configurable timeout (already passed as parameter)
            was_timed_out = False
            hit_result_limit = False
            file_count = 0
            match_count = 0

            # BUG_0062: honour .gitignore/.ignore/.rgignore so the fallback returns the SAME
            # file set as the ripgrep fast path when ignore_vcs=True. The resolver owns
            # DEFAULT_IGNORED_DIRS as its lowest-precedence source, so no local skip_dirs
            # literal remains here. Degrades to the hardcoded-dir-only behaviour when
            # `pathspec` is unavailable.
            _ignore_resolver = IgnoreResolver(resolved)

            # Default file excludes for Python fallback — aligns with subprocess behavior.
            # Match against basename only (fnmatch).
            _PY_DEFAULT_EXCLUDES = {'*.pyc', '*.so', '*.dll', '*.exe', '*.zip'}

            # BUG_0169: the fallback must not read a multi-GB artifact into memory either.
            # Over-size files get the same bounded header scan the ripgrep path uses.
            _max_mb, _head_bytes, _scan_limit, _budget_s = _grep_large_file_limits()
            _large_max_bytes = _max_mb * 1024 * 1024 if _max_mb > 0 else 0
            _large_skipped = 0
            _large_scanned = 0
            _large_skipped_paths: list = []

            # When include is "*" (all files), use os.walk with directory skipping to match
            # subprocess behavior. rglob("*") doesn't respect ignores and only matches root level.
            # BUG_0056: support comma-separated globs — union of rglob for each pattern.
            _include_patterns = [p.strip() for p in include.split(',') if p.strip()] if include != '*' else []
            if not _include_patterns:
                # Mutate dirs in-place to skip unwanted directories during walk. Pruning here
                # rather than post-hoc is a performance requirement: it is what stops the
                # fallback from descending into an ignored node_modules/ at all.
                def _walk_with_skips():
                    for root, dirs, files in os.walk(resolved):
                        if ignore_vcs:
                            dirs[:] = [d for d in dirs
                                       if not _ignore_resolver.is_ignored(Path(root) / d, is_dir=True)]
                        yield root, dirs, files

                file_iter_gen = (Path(os.path.join(root, f)) for root, dirs, files in _walk_with_skips() for f in files)
            else:
                # Union of rglob results across all patterns (dedup via set)
                _seen = set()

                def _multi_rglob():
                    for _pat in _include_patterns:
                        for _fp in resolved.rglob(_pat):
                            if _fp not in _seen:
                                _seen.add(_fp)
                                yield _fp

                file_iter_gen = _multi_rglob()

            for file_path in file_iter_gen:
                if time.time() - start_time > timeout:
                    was_timed_out = True
                    break
                if not file_path.is_file():
                    continue

                # Skip files under ignored directories (for rglob path; os.walk already prunes dirs)
                if ignore_vcs:
                    try:
                        file_path.relative_to(resolved)
                    except ValueError:
                        # File path is not relative to resolved search root — skip it
                        continue
                    # BUG_0062: the resolver also re-checks ancestors, so a file reached via
                    # the rglob branch (which never prunes mid-traversal) is still filtered by
                    # nested .gitignore files, not just by the hardcoded dir set.
                    if _ignore_resolver.is_ignored(file_path, is_dir=False):
                        continue

                # Apply default excludes (aligns with subprocess behavior)
                if any(fnmatch.fnmatch(file_path.name, pat) for pat in _PY_DEFAULT_EXCLUDES):
                    continue

                # Apply exclude pattern. Align with grep/rg semantics:
                # - If pattern has no path separator → match only against basename
                #   (e.g., "test*" excludes "test.py" but NOT "tests/main.py")
                # - If pattern has separators → match against full relative path
                # BUG_0056: support comma-separated excludes — check each pattern.
                if exclude:
                    try:
                        rel = file_path.relative_to(resolved)
                        rel_str = str(rel).replace('\\', '/')
                        should_exclude = False

                        for _exc in exclude.split(','):
                            _exc = _exc.strip()
                            if not _exc:
                                continue
                            if '/' not in _exc and '\\' not in _exc:
                                # No separator: match only basename (like grep --exclude)
                                if fnmatch.fnmatch(file_path.name, _exc):
                                    should_exclude = True
                                    break
                            else:
                                # Has separator: match against full relative path
                                if fnmatch.fnmatch(rel_str, _exc):
                                    should_exclude = True
                                    break

                        if should_exclude:
                            continue
                    except ValueError:
                        # If we can't compute a relative path and exclude is set, skip the file.
                        # Conservative: better to miss a match than process files outside scope.
                        continue

                # File reading and pattern matching — OUTSIDE the exclude block so it runs regardless
                if _large_max_bytes:
                    try:
                        _size = file_path.stat().st_size
                    except OSError:
                        _size = 0
                    if _size > _large_max_bytes:
                        # BUG_0169: skip the full read; scan only the header.
                        _large_skipped += 1
                        try:
                            _rel_large = str(file_path.relative_to(resolved)).replace('\\', '/')
                        except ValueError:
                            _rel_large = file_path.name
                        if len(_large_skipped_paths) < 11:
                            _large_skipped_paths.append(_rel_large)
                        if _large_scanned < _scan_limit:
                            _large_scanned += 1
                            _head_hits = self._head_scan_matches(file_path, _rel_large,
                                                                 pattern_re, _head_bytes)
                            if _head_hits:
                                results.extend(_head_hits)
                                match_count += len(_head_hits)
                        continue

                try:
                    # BUG_0145: 'replace' (not 'ignore') keeps non-UTF-8 lines renderable,
                    # matching read_file and the rg fast path (see _rg_lines_text).
                    content = file_path.read_text(encoding='utf-8', errors='replace')
                    lines = content.split('\n')

                    if context > 0:
                        try:
                            normalized_rel_path = str(file_path.relative_to(resolved)).replace('\\', '/')
                        except ValueError:
                            try:
                                normalized_rel_path = str(file_path.relative_to(self.base_dir)).replace('\\', '/')
                            except ValueError:
                                normalized_rel_path = file_path.name

                        for line_num, line in enumerate(lines, 1):
                            if pattern_re.search(line):
                                match_count += 1
                                start = max(1, line_num - context)
                                end = min(len(lines), line_num + context)
                                for ctx_line in range(start - 1, end):
                                    marker = _GREP_MATCH_MARKER if ctx_line + 1 == line_num else ''
                                    results.append(_format_grep_entry(normalized_rel_path, ctx_line + 1,
                                                                      lines[ctx_line], marker))
                                results.append('---')
                            if len(results) % 200 == 0 and time.time() - start_time > timeout:
                                was_timed_out = True
                                break
                            if len(results) > 5000:
                                hit_result_limit = True
                                break
                    else:
                        try:
                            rel_path = file_path.relative_to(resolved)
                        except ValueError:
                            try:
                                rel_path = file_path.relative_to(self.base_dir)
                            except ValueError:
                                rel_path = file_path.name
                        normalized_rel_path = str(rel_path).replace('\\', '/')

                        for line_num, line in enumerate(lines, 1):
                            if pattern_re.search(line):
                                match_count += 1
                                results.append(f"{normalized_rel_path}:{line_num}: {line}")
                            if len(results) % 500 == 0 and time.time() - start_time > timeout:
                                was_timed_out = True
                                break
                        if was_timed_out:
                            break

                    file_count += 1
                    if len(results) > 5000:
                        hit_result_limit = True
                        break
                except Exception as e:
                    logger.debug(f"Error reading file during grep (skipping): {e}")
                    continue

            if not results and not was_timed_out:
                logger.debug(
                    f"grep: Python fallback also found no matches for '{pattern}' (subprocess already confirmed)")

            if not results:
                if was_timed_out:
                    _timeout_msg = f"Search timed out after {int(timeout)}s before finding any matches for '{pattern}'. Narrow your pattern or scope."
                    if _large_skipped:
                        _skipped_list = ', '.join(_large_skipped_paths[:10])
                        _more = f" (+{_large_skipped - 10} more)" if _large_skipped > 10 else ''
                        _timeout_msg += (f" [search INCOMPLETE — skipped {_large_skipped} file(s) > {_max_mb} MB: "
                                         f"{_skipped_list}{_more}]")
                    return _timeout_msg
                exclude_info = f", excluding {exclude}" if exclude else ''
                _large_note = ''
                if _large_skipped:
                    _skipped_list = ', '.join(_large_skipped_paths[:10])
                    _more = f" (+{_large_skipped - 10} more)" if _large_skipped > 10 else ''
                    _large_note = (f" [search INCOMPLETE — skipped {_large_skipped} file(s) > {_max_mb} MB: "
                                   f"{_skipped_list}{_more}; use read_file to inspect their headers/content]")
                return (f"No matches found for pattern '{pattern}' in {path}/**/{include}"
                        f"{exclude_info}{searched_note}{_large_note}")

            summary = f"Found {match_count} matches for '{pattern}'{searched_note}"
            if context > 0:
                summary += f" (with {context} line(s) of context)"
            if _large_skipped:
                _skipped_list = ', '.join(_large_skipped_paths[:10])
                _more = f" (+{_large_skipped - 10} more)" if _large_skipped > 10 else ''
                summary += (f" [search INCOMPLETE — skipped {_large_skipped} file(s) > {_max_mb} MB: "
                            f"{_skipped_list}{_more}; use read_file for more]")
            output_text = _grep_body_with_legend(results, context, match_count)

            if was_timed_out:
                summary += f" [TIMED OUT after {int(timeout)}s]"
                output_text += f"\n\n[TOOL RESPONSE TIMED OUT — Searched {file_count} files before exceeding {int(timeout)} second limit. Narrow your pattern or scope to a specific directory.]"
            elif hit_result_limit:
                summary += ' [TRUNCATED at 5000 results]'

            result = truncate_with_spillover(
                output_text,
                char_limit,
                instance_name=agent_name,
                tool_name='grep',
                base_dir=self.base_dir,
                operation_mode='head',
                spill_path=_resolve_spill_path(spill_file_path),
            )
            if result is not output_text:
                output_text = result
                summary += ' [TRUNCATED]'

            return f"{summary}:\n\n" + output_text
        except Exception as e:
            return f"Error searching: {str(e)}"
