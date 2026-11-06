"""Grep subsystem — pattern compilation, tool availability check, and mixin for file search."""

import base64
import fnmatch
import json
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from agent_cascade.tool_utils import truncate_with_spillover, format_truncation_notice
from agent_cascade.operation_manager.grep_ignore import IgnoreResolver

# ─── Module-level cached helpers ──────────────────────────────────────────

# Default file excludes shared by every grep path (the fallback walker and the over-cap
# scan). BUG_0221: one literal instead of three copies that could drift.
_DEFAULT_EXCLUDES = frozenset({
    '*.pyc',
    '*.so',
    '*.dll',
    '*.exe',
    '*.zip',
})


@dataclass(frozen=True)
class _CountDisclosure:
    """The independent disclosure truths of one count-only result (BUG_0221 #9).

    BUG_0202/RC-B: these must NOT collapse into fewer flags — each one selects a different
    literal string, and two are pinned by absence assertions (`'partial' not in out`,
    `'TIMED OUT after' not in out`). Bundling the plumbing keeps them distinct without a
    22-parameter formatter.

    ``probe_total`` is the number of matches the ignore-rule probe found (0 when it found
    none); ``probe_partial`` is True when that probe itself was truncated (rg rc==2).
    """

    partial_error: bool = False
    rg_path_timed_out: bool = False
    fallback_truncated: bool = False
    over_cap_walk_partial: bool = False
    probe_partial: bool = False
    probe_total: int = 0


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

# BUG_0236: suffix appended to a rendered content column that exceeded GREP_MAX_LINE_CHARS.
# The omitted char count is the RAW line length remaining, so the reader can tell a 500-char
# code line from a 25 KB JSONL record and re-target with read_logs/read_file. The literal is
# also the detection key for the aggregate summary note, so keep it in exactly one place.
_GREP_LINE_ABBREVIATED_MARK = ' [\u2026 line abbreviated: +'
_GREP_LINE_ABBREVIATED_SUFFIX = ' chars]'


def _grep_max_line_chars() -> int:
    """Return the per-line render cap for grep (BUG_0236), or 0 when disabled.

    Read lazily from :mod:`agent_cascade.settings` so tests can patch the constant and so
    callers that never grep pay nothing. Falls back to the documented default when the
    settings module cannot be imported (e.g. an isolated unit-test harness).
    """
    try:
        from agent_cascade.settings import GREP_MAX_LINE_CHARS
        return int(GREP_MAX_LINE_CHARS)
    except Exception:  # noqa: BLE001 - a settings import hiccup must not break grep
        return 500


def _abbreviate_grep_line(content: str, max_chars: Optional[int] = None) -> str:
    """Abbreviate one content column to the per-line cap, disclosing the raw remainder.

    BUG_0236: the truncation budget is measured on rendered entries. A single-line JSONL
    document (or a minified log record) can be hundreds of KB, so a handful of matches used
    to consume the whole budget and force a multi-MB spillover. Capping here — the one
    renderer every grep path goes through — bounds both the visible body and the spillover.
    Lines at or under the cap are returned unchanged (byte-identical output).
    """
    if max_chars is None:
        max_chars = _grep_max_line_chars()
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    omitted = len(content) - max_chars
    return content[:max_chars] + _GREP_LINE_ABBREVIATED_MARK + f'{omitted}{_GREP_LINE_ABBREVIATED_SUFFIX}'


def _abbreviated_line_note(rendered_lines) -> str:
    """BUG_0236: aggregate note when any rendered line hit the per-line cap.

    One JSONL record per line defeats line-count-based size reporting ("1701 lines" can be
    one document), so name the real situation and point at the tools that page by range.
    Returns ``''`` when nothing was abbreviated (clean output stays byte-identical).
    """
    n = sum(1 for entry in rendered_lines if _GREP_LINE_ABBREVIATED_MARK in entry)
    if not n:
        return ''
    return (f" [note: {n} rendered line(s) exceeded {_grep_max_line_chars()} chars and were "
            f"abbreviated — single-line JSONL/log records can each be a whole document; use "
            f"count_only=true for counts, or read_logs/read_file with a range/offset to "
            f"inspect one record]")


def _format_grep_entry(rel_path: str, line_num: int, content: str, marker: str = '') -> str:
    """Render one grep result line as ``<path>:<line>: [<marker>]<content>``.

    BUG_0144: the content column is the RAW file text. It is never indented or padded.
    (BUG_0236 abbreviates only lines that exceed GREP_MAX_LINE_CHARS, with a visible suffix.)
    Context lines used to be rendered with a 5-space indent
    (4 prefix spaces + the separator space) to set them apart from matched lines. That
    indent is invisible whitespace, so anyone transcribing a multi-line block out of grep
    output into ``edit_file`` (``match_mode="exact"``) silently injected leading spaces
    that do not exist in the file and got "Pattern not found". Context lines now carry no
    decoration at all; only matched lines get ``_GREP_MATCH_MARKER``, which is visible and
    trivially stripped, and the always-present ``<path>:<line>: `` prefix tells the reader
    exactly where the content begins.

    Every render path (rg --json, Python fallback, single-file) MUST go through
    here so their byte-level output cannot drift apart.

    BUG_0236: the content column is abbreviated to ``GREP_MAX_LINE_CHARS`` by
    :func:`_abbreviate_grep_line`, so a single-line JSONL record cannot consume the
    whole output budget. Short lines are unchanged.
    """
    content = _abbreviate_grep_line(content)
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


def _collect_file_matches(results: list, lines: list, pattern_re, rel_path: str,
                          context: int, timeout: float, start_time: float) -> tuple:
    """Append one file's rendered matches to ``results``; return ``(count, timed_out, hit_limit)``.

    BUG_0221 (#6): the per-file match renderer shared by :meth:`GrepMixin._grep_single_file`
    and the Python fallback. Both had byte-identical context-window, marker, timeout and
    5000-result-cap loops; keeping one copy is what stops the two paths from drifting.
    """
    match_count = 0
    was_timed_out = False
    hit_result_limit = False
    if context > 0:
        for line_num, line in enumerate(lines, 1):
            if pattern_re.search(line):
                match_count += 1
                start = max(1, line_num - context)
                end = min(len(lines), line_num + context)
                for ctx_line in range(start - 1, end):
                    marker = _GREP_MATCH_MARKER if ctx_line + 1 == line_num else ''
                    results.append(_format_grep_entry(rel_path, ctx_line + 1,
                                                      lines[ctx_line], marker))
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
                results.append(_format_grep_entry(rel_path, line_num, line))
            if len(results) % 500 == 0 and time.time() - start_time > timeout:
                was_timed_out = True
                break
            if len(results) > 5000:
                hit_result_limit = True
                break
    return match_count, was_timed_out, hit_result_limit


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
    """BUG_0202 (F5) / BUG_0227: disclose a truncated over-cap DIAGNOSTIC walk.

    Worded per the walk's OWN budget (GREP_LARGE_FILE_SCAN_BUDGET_S, widened to the
    caller's ``timeout`` when larger — BUG_0227), never the caller's deadline — a walk
    that merely ran out of its budget is not a caller timeout.

    Count-only output has no skipped-file list and presents its total as the answer, so
    this says what the number IS (a lower bound) instead of pointing at a "list above"
    that does not exist in this mode. One helper so the zero and non-zero branches
    cannot drift apart.
    """
    return (f"[search INCOMPLETE — the over-cap file enumeration stopped at its "
            f"{budget_s:.2f} s budget, so files it never reached are unaccounted "
            f"for; treat the count as a lower bound]")


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


def _incomplete_note(skipped_count: int, skipped_paths: list, max_mb: int, tail: str = '',
                     *, detail: str = '') -> str:
    """BUG_0185/BUG_0190/BUG_0221: the shared ``[search INCOMPLETE — …]`` note.

    A count/line claim over a scope where over-size files were skipped must never read as
    conclusive. Every line-mode and count-mode path routes through here so the prefix, the
    10-path cap and the ``(+N more)`` suffix cannot drift apart. Returns ``''`` when nothing
    was skipped and no ``detail`` was given. ``tail`` is an optional trailing clause (e.g.
    ``use read_file to inspect their headers/content``); ``detail`` replaces the default
    ``skipped N file(s) …`` body for the header-scan messages, which describe scanned rather
    than skipped files.
    """
    if detail:
        body = detail
    elif skipped_count:
        more = f" (+{skipped_count - 10} more)" if skipped_count > 10 else ''
        body = (f"skipped {skipped_count} file(s) > {max_mb} MB: "
                f"{', '.join(skipped_paths[:10])}{more}")
    else:
        return ''
    tail_part = f"; {tail}" if tail else ''
    return f" [search INCOMPLETE — {body}{tail_part}]"


@lru_cache(maxsize=1)
def _check_tool_availability():
    """Check whether ripgrep is available at runtime.

    Returns a 2-tuple ``(rg_available, grep_available)``. ``grep_available`` is retained
    only so the many existing patch sites keep their shape; BUG_0221 removed the GNU-grep
    branch, so it is always False and no path reads it.

    Uses lru_cache for performance — tool availability doesn't change during execution.
    """
    import shutil

    return shutil.which('rg') is not None, False


def _has_inline_case_flag(pattern: str) -> bool:
    """True when the pattern carries its own case-sensitivity switch (``(?i:``/``(?-i:``)."""
    return '(?-i:' in pattern or '(?i:' in pattern


def _smart_case_flags(pattern: str, smart_case: bool) -> int:
    """Flags for the Python matcher under smart case (no uppercase → case-insensitive).

    BUG_0221: one rule, one implementation. This block was hand-copied into every Python
    search path; a divergence would make the same pattern case-sensitive on one path and
    case-insensitive on another.
    """
    if not smart_case:
        return 0
    if re.search(r'[A-Z]', pattern) and not _has_inline_case_flag(pattern):
        return 0
    return re.IGNORECASE


def _smart_case_ignorecase(pattern: str, smart_case: bool) -> bool:
    """True when the subprocess argv needs ``-i`` under smart case.

    Deliberately NOT the same predicate as :func:`_smart_case_flags`: the argv path relies on
    an inline ``(?i:``/``(?-i:`` switch instead of adding a global ``-i``, while the Python
    matcher still receives ``re.IGNORECASE`` in that case. Preserved as-is.
    """
    return bool(smart_case and not re.search(r'[A-Z]', pattern)
                and not _has_inline_case_flag(pattern))


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


# ─── Mixin: Grep methods for OperationManager ─────────────────────────────


class GrepMixin:
    """Grep/search methods. Expects self to have __init__-set attributes including class-level exclude constants."""

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
        _max_file_mb = _grep_large_file_limits()[0]
        if _max_file_mb > 0:
            cmd.append(f'--max-filesize={_max_file_mb}M')

        if force_no_ignore:
            cmd.append('--no-ignore')

        if literal:
            cmd.append('-F')

        if context > 0:
            cmd.extend(['-C', str(context)])

        if _smart_case_ignorecase(pattern, smart_case):
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
                               ignore_vcs: bool, deadline: float, resolver=None):
        """Yield ``(path, rel_path)`` for files matching grep's include/exclude/ignore rules.

        Mirrors the Python fallback's filtering (default excludes, basename-vs-path exclude
        semantics, .gitignore handling) so the over-size scan reports the same candidate set
        the streaming search saw. Bounded by ``deadline`` (a ``time.monotonic()`` value) so a
        pathological tree can never add more than the configured budget to a grep.

        ``resolver`` (BUG_0221 #7): an optional shared :class:`IgnoreResolver` so one
        count-only grep can reuse the same instance (and its cached ``git ls-files`` set)
        across the fallback walk, the over-cap walk and the ignored-match probe.
        """
        import os

        if resolver is None and ignore_vcs:
            resolver = IgnoreResolver(root)
        elif not ignore_vcs:
            resolver = None
        includes = [p.strip() for p in include.split(',') if p.strip()] if include != '*' else []
        excludes = [p.strip() for p in exclude.split(',') if p.strip()] if exclude else []
        default_excludes = _DEFAULT_EXCLUDES

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

        flags = _smart_case_flags(pattern, smart_case)
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
        # BUG_0221: context=0 — the probe parses match events only (_iter_rg_json_matches
        # discards every context event), so asking rg for -C context was pure wasted work.
        # The line-mode disclosure that context lines are unavailable stays in the caller.
        cmd = self._build_rg_argv(pattern,
                                  include,
                                  exclude,
                                  0,
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
                                 smart_case: bool,
                                 literal: bool,
                                 timeout: float,
                                 ignore_vcs: bool,
                                 resolver=None) -> tuple:
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
            pattern, resolved, include, exclude, smart_case, timeout, literal)
        if probe is None:  # probe failed / no records (rc None) — nothing to recover, not partial
            return [], 0, False
        if not probe:  # probe ran (rc 0/1) but found nothing; rc==2 is a partial result
            return [], 0, (probe_rc == 2)
        if resolver is None:
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
        """Fast-path grep using ripgrep via subprocess (the only supported engine).

        Returns (results_list, count, was_timed_out, was_truncated, original_output_size,
                 spillover_rel_path, total_lines_before, shown_lines_after, abbreviation_note,
                 total_matches_before)
        on success, or an 8-tuple starting with None on failure (the optional 9th/10th elements
        are tolerated by :meth:`grep` so legacy callers/tests keep working).
        Output format matches Python fallback: "relative_path:line_number: content"
        """
        import subprocess

        from agent_cascade.log import logger

        # BUG_0221: ripgrep is the only subprocess engine (the GNU-grep branch was deleted).
        _rg_available, _ = _check_tool_availability()

        if not _rg_available:
            logger.debug('grep: subprocess fast path unavailable (rg=%s), falling back to Python',
                         _rg_available)
            return None, 0, False, False, 0, None, 0, 0

        try:
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
            # Parse ripgrep's --json stdout into (formatted, match_count). Shared by the
            # rc==0/rc==2 paths AND the timeout path (BUG_0236: a killed rg still leaves
            # partial events on stdout) so a partial search never discards matches found.
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

            def _finish(formatted, count, *, timed_out=False):
                """Apply the char budget/spillover truncation and build the result tuple.

                Shared by the normal rc==0/rc==2 path and the BUG_0236 timeout path so a
                partial search can never bypass the output bound.

                The 9th element is the BUG_0236 abbreviation note computed from the FULL
                (pre-truncation) entry list: the note must still be reported when the
                abbreviated JSONL entries themselves fall outside the displayed budget.

                The 10th element (BUG_0232) is the pre-truncation MATCH count. Element 1
                (`count`) is recomputed from the surviving inline entries below, so it
                collapses to 0 when every match spills to the spillover file. The header
                must report the full match count, so it is returned separately rather than
                repurposing element 1 — tests and the `>>>`-legend decision rely on element
                1 keeping its post-truncation meaning.
                """
                _total_matches_before = count  # BUG_0232: full pre-truncation match count
                _abbrev_note = _abbreviated_line_note(formatted)
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

                return (formatted, count, timed_out, _was_truncated, _original_output_size,
                        _spill_rel_path, _total_lines_before, _shown_lines_after, _abbrev_note,
                        _total_matches_before)

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

            # rc==0 (matches found) and rc==2 (matches found but some files errored) both carry
            # valid match data on stdout — parse and surface them through the SAME truncation path
            # below so a partial-error search can never produce unbounded output. A genuine usage
            # error (rc=2 with no parseable matches) is detected inside this block and falls back
            # to Python before reaching the truncation code.
            _rg_partial_error = False
            if result.returncode == 0 or result.returncode == 2:
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
                    if 'No files were searched' in stderr_msg and _grep_large_file_limits()[0] > 0:
                        return [], 0, False, False, 0, None, 0, 0
                    logger.warning(
                        f"grep subprocess failed with exit code 2 (no matches recovered; falling back to Python): {stderr_msg}")
                    return None, 0, False, False, 0, None, 0, 0
                # rc==2 that DID yield matches means some files were skipped (e.g. permission
                # denied) — warn once so the partial nature is visible without discarding matches.
                if result.returncode == 2 and count > 0:
                    _rg_partial_error = True

                finished = _finish(formatted, count)
                if _rg_partial_error:
                    stderr_msg = (result.stderr or '').strip()[:500]
                    logger.warning(
                        f"grep subprocess found {finished[1]} match(es) but exited with code 2 "
                        f"(partial errors; some files may have been skipped): {stderr_msg}")
                return finished

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

        except subprocess.TimeoutExpired as e:
            # BUG_0236: a killed rg still leaves its already-emitted --json events on stdout.
            # Parsing them yields the matches found before the deadline, so the caller gets a
            # bounded partial answer (explicitly marked INCOMPLETE) instead of throwing the
            # work away and spending a second full timeout re-searching in Python.
            partial_stdout = e.stdout or ''
            if isinstance(partial_stdout, bytes):
                partial_stdout = partial_stdout.decode('utf-8', errors='replace')
            formatted, partial_count = _parse_rg_json(partial_stdout)
            if partial_count > 0:
                logger.warning(
                    f"grep subprocess timed out after {int(timeout)}s; returning "
                    f"{partial_count} partial match(es) from its stdout")
                return _finish(formatted, partial_count, timed_out=True)
            logger.warning(
                f"grep subprocess timed out after {int(timeout)}s; falling back to Python"
            )
            return None, 0, True, False, 0, None, 0, 0

        except (FileNotFoundError, PermissionError, OSError) as e:
            logger.debug(f"grep subprocess unavailable (falling back to Python): {e}")

        return None, 0, False, False, 0, None, 0, 0

    def _over_cap_rel_paths(self, root: Path, include: str, exclude: str,
                            ignore_vcs: bool, max_bytes: int, deadline: float, resolver=None):
        """BUG_0190/BUG_0202: ``(count, rel_paths, truncated)`` for in-scope files over the cap.

        Reuses :func:`_iter_grep_scope_files` (the same include/exclude/ignore filtering the rg
        ``--max-filesize`` fast path applies) so the over-cap set the INCOMPLETE note names is
        exactly the set rg skipped. Paths are capped at 11 (the note shows 10 + a "+N more").
        ``truncated`` (F5) is True when the walk hit its deadline before seeing the whole scope,
        so the caller knows the over-cap set — and any count built on it — is partial, not total.
        """
        paths: list = []
        count = 0
        for fp, rel_str in self._iter_grep_scope_files(root, include, exclude, ignore_vcs,
                                                       deadline, resolver=resolver):
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
        flags = _smart_case_flags(pattern, smart_case)
        _compile_pattern = re.escape(pattern) if literal else pattern
        try:
            pattern_re = _compile_grep_pattern(_compile_pattern, flags=flags)
        except re.error as e:
            return (f"ERROR: Invalid regex pattern '{pattern}': {str(e)}. "
                    f"Please provide a valid Python regular expression.")
        alts, alt_res = _compile_alternatives(pattern, literal, flags)

        _max_mb, _head_bytes, _scan_limit, _budget_s = _grep_large_file_limits()
        max_bytes = _max_mb * 1024 * 1024 if _max_mb > 0 else 0
        # BUG_0227: the over-cap enumeration is a diagnostic walk, but its budget must be
        # widen-able by the caller. A fixed 0.75 s cap meant raising `timeout` to handle a
        # large tree still produced a truncated skip set (and a lower-bound total). Take
        # whichever is larger: the safety budget or the caller's own deadline.
        _walk_budget_s = max(_budget_s, timeout)
        # BUG_0221 (#7): ONE resolver for this count-only grep. The fallback walk, the over-cap
        # walk and the ignored-match probe all reuse it, so the repo's `git ls-files -z` index is
        # queried at most once per grep instead of once per resolver instance.
        _count_resolver = IgnoreResolver(resolved) if ignore_vcs else None

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
                        time.monotonic() + _walk_budget_s, resolver=_count_resolver)
                    # F5 (BUG_0202/RC-B): a truncated over-cap walk means the skip set (and thus
                    # the count) is partial — surface it through its own flag, NOT the caller-
                    # deadline flags (the walk's 0.75 s budget is not the caller's deadline).
                    over_cap_walk_partial = over_cap_walk_partial or _over_cap_truncated
        if not used_rg:
            # Python count fallback: walk the same scope, count per file, header-scan over-cap.
            deadline = time.monotonic() + timeout
            scanned = 0
            for fp, rel_str in self._iter_grep_scope_files(resolved, include, exclude, ignore_vcs,
                                                           deadline, resolver=_count_resolver):
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

        # BUG_0202 (F3): recover matches hidden behind ignore rules. If any survive the
        # resolver filter (tracked or not-ignored), render them as a real count result
        # instead of a bare zero — a tracked file is a real match, not an absence.
        # BUG_0221 #9: recovery lives here (not in the renderer) so the formatter is a pure
        # renderer and the probe's flags/total travel in the same disclosure object.
        probe_total = 0
        probe_partial = False
        if total == 0:
            survivors, probe_total, probe_partial = self._recover_ignored_matches(
                pattern, resolved, include, exclude, smart_case, literal, timeout, ignore_vcs,
                resolver=_count_resolver)
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

        disclosure = _CountDisclosure(
            partial_error=partial_error,
            rg_path_timed_out=rg_path_timed_out,
            fallback_truncated=fallback_truncated,
            over_cap_walk_partial=over_cap_walk_partial,
            probe_partial=probe_partial,
            probe_total=probe_total,
        )
        return self._format_count_result(
            pattern, searched_note, per_file, per_pattern, total, alts, disclosure,
            skipped_count, skipped_paths, _max_mb, timeout,
            over_cap_budget_s=_walk_budget_s)

    def _format_count_result(self,
                             pattern: str,
                             searched_note: str,
                             per_file: dict,
                             per_pattern: dict,
                             total: int,
                             alts,
                             disclosure: _CountDisclosure,
                             skipped_count: int,
                             skipped_paths: list,
                             max_mb: int,
                             timeout: float,
                             *,
                             over_cap_budget_s: Optional[float] = None) -> str:
        """BUG_0190: render a count-only result.

        Zero matches get a bare line (plus the BUG_0061 hidden-matches probe note and the
        over-cap INCOMPLETE note). Non-zero gets the per-file list (capped at 50, totals always
        full) + the per-pattern breakdown + the integrity notes. A pure renderer since
        BUG_0221 #9 — recovery runs in :meth:`_grep_count_only`.
        """
        # F5 (BUG_0202/RC-B): word the timeout note PER PATH. A fallback truncation is a real
        # "partial" (the totals are incomplete); a fast-path timeout whose fallback completed is
        # a complete total computed by the fallback — say so, and do NOT claim "partial".
        if disclosure.fallback_truncated:
            _timeout_note = f" [TIMED OUT after {int(timeout)}s — counts are partial, not totals]"
        elif disclosure.rg_path_timed_out:
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
            base = f"0 matching lines for '{pattern}'{searched_note}"
            if disclosure.probe_total:
                # F4 (BUG_0202/RC-C): state what was consulted, never assert git status.
                base += _ignore_advisory_note(disclosure.probe_total)
            if disclosure.probe_partial:
                # F5 (BUG_0202/RC-B): the probe itself was truncated (rg rc==2) — the "no
                # survivors" verdict only covers a partial probe, so disclose it (mirrors the
                # line-mode note; the count path previously dropped probe_partial here).
                base += ' [note: the ignore-rule probe returned a partial result]'
            base += _incomplete_note(skipped_count, skipped_paths, max_mb)
            # F5 (BUG_0202/RC-B): a truncation must be disclosed in the zero-match branch too
            # (worded per path — a fast-path timeout whose fallback finished is not "partial").
            base += _timeout_note
            if disclosure.over_cap_walk_partial:
                # BUG_0227: count mode has no skipped-file list, so say what the zero IS.
                base += ' ' + _over_cap_partial_note(over_cap_budget_s)
            return base

        # BUG_0227: when the over-cap enumeration was truncated the total is a lower bound,
        # and that must be a header — a trailing note after "[total: …]" reads as a footnote
        # to a finished count. Count-only mode also has no file list for the old "list above"
        # wording to refer to.
        _over_cap_header = (
            _over_cap_partial_note(over_cap_budget_s)
            if disclosure.over_cap_walk_partial else '')
        out = (_over_cap_header + '\n' if _over_cap_header else '') \
            + f"Count-only results for '{pattern}'{searched_note}:"
        items = sorted(per_file.items(), key=lambda kv: kv[1], reverse=True)
        shown = items[:50]
        for rel, c in shown:
            out += f"\n{rel}: {c}"
        if len(items) > 50:
            out += f"\n(+{len(items) - 50} more files — totals are complete)"
        out += f"\n[total: {total} matching lines in {len(per_file)} file(s)]"
        if alts:
            pp = ', '.join(f"'{alt}': {per_pattern.get(alt, 0)}" for alt in alts)
            # BUG_0227: do not present a truncated breakdown as complete. Complete
            # results keep the byte-identical historical line.
            out += f"\n[per-pattern: {pp} across {total} matching lines"
            if disclosure.over_cap_walk_partial:
                out += ' — partial, lower bound'
            out += ']'
        out += _incomplete_note(skipped_count, skipped_paths, max_mb)
        if disclosure.partial_error:
            out += ' [note: some files were skipped due to errors — counts are partial]'
        out += _timeout_note
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

        flags = _smart_case_flags(pattern, smart_case)

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
        start_time = time.time()
        match_count, was_timed_out, hit_result_limit = _collect_file_matches(
            results, lines, pattern_re, normalized_rel_path, context, timeout, start_time)

        if not results:
            return f"No matches found for pattern '{pattern}' in {file_path.name}{searched_note}"

        summary = f"Found {match_count} matches for '{pattern}'{searched_note}"
        if context > 0:
            summary += f" (with {context} line(s) of context)"
        output_text = _grep_body_with_legend(results, context, match_count)
        summary += _abbreviated_line_note(results)

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

        Uses ripgrep via subprocess as a fast path, falling back to pure Python if the
        subprocess approach fails/times out. ripgrep is a hard dependency (BUG_0221).
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

            # ── Fast path: try subprocess-based ripgrep ──
            (results, count, was_timed_out, _sub_truncated, _original_output_size, _sub_spill_rel, _sub_total_lines,
             _sub_shown_lines, *_sub_extra) = self._try_subprocess_grep(
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
            # 9th element (BUG_0236): abbreviation count computed before truncation. Legacy
            # 8-tuple mocks/callers have no such element, so fall back to inspecting results.
            _sub_abbrev_note = _sub_extra[0] if _sub_extra else ''
            # BUG_0232: pre-truncation MATCH count for the header. Element 1 (`count`) is the
            # post-truncation count and reports 0 when every match spills to the spillover
            # file, so the header must read element 10 instead. Legacy 8/9-tuple mocks lack it;
            # fall back to `count` (only ever read on a non-truncated or zero-match legacy
            # result, where the two are equal by construction).
            _total_matches = _sub_extra[1] if len(_sub_extra) > 1 else count
            if was_timed_out and results:
                # BUG_0236: the killed rg process still produced matches on stdout. Keep them
                # (rendered with a partial-coverage note below) instead of discarding the work
                # and spending another full timeout in the Python fallback.
                logger.warning(
                    'grep subprocess timed out after %ss but yielded %s partial match(es); '
                    'returning partial results', int(timeout), count)
            elif was_timed_out:
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
                        pattern, resolved, include, exclude, smart_case,
                        literal, timeout, ignore_vcs)
                    if survivors:
                        # BUG_0202 (F3): route the recovered body through the shared render rule
                        # (_format_grep_entry / _grep_body_with_legend) so it cannot drift from
                        # the rg/Python paths, and cap it with truncate_with_spillover. The
                        # probe yields match events only (no context lines), so context is
                        # dropped here — disclose it rather than silently discarding it.
                        _body_lines = [_format_grep_entry(rel, ln, text)
                                      for rel, ln, text in survivors]
                        _recovered_body = _grep_body_with_legend(_body_lines, 0, len(survivors))
                        _ctx_note = (f"\n\n[context lines not shown — recovery re-runs the probe, "
                                     f"which yields match lines only]" if context > 0 else '')
                        _recovered_out = (f"Found {len(survivors)} matches for '{pattern}'"
                                         f"{searched_note}{_abbreviated_line_note(_body_lines)}:"
                                         f"\n\n" + _recovered_body + _ctx_note)
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
                            _summary += _abbreviated_line_note(_large['formatted'])
                            _partial = ' (scan limit reached)' if _large['partial'] else ''
                            _summary += _incomplete_note(
                                _large['large_files'], [], _max_mb,
                                tail='full file content was not streamed — use read_file for more',
                                detail=(f"matched in the first {_head_kb} KiB of {_large['scanned']} "
                                        f"file(s) > {_max_mb} MB{_partial}"))
                            return f"{_summary}:\n\n" + '\n'.join(_large['formatted'])
                        if _large['large_files']:
                            summary += _incomplete_note(
                                _large['large_files'], [], _max_mb,
                                tail='use read_file to inspect their headers/content',
                                detail=(f"{_large['large_files']} file(s) > {_max_mb} MB were not "
                                        f"searched in full (only the first "
                                        f"{_head_bytes // 1024} KiB was scanned)"))
                    return f"{summary}:\n\n"
                else:
                    output_text = _grep_body_with_legend(results, context, count)
                    # Header reports the pre-truncation match count (_total_matches), not the
                    # post-truncation `count` passed to the legend above — when every match spills
                    # inline, `count` is 0 but the search did find matches (BUG_0232).
                    summary = f"Found {_total_matches} matches for '{pattern}'{searched_note}"
                    if context > 0:
                        summary += f" (with {context} line(s) of context)"
                    summary += _sub_abbrev_note or _abbreviated_line_note(results)
                    if was_timed_out:
                        # BUG_0236: rg was killed at the deadline but its partial stdout still
                        # held matches. Render them with an explicit partial-coverage note
                        # instead of discarding the work and re-searching in Python.
                        summary += (f" [TIMED OUT after {int(timeout)}s — search INCOMPLETE: "
                                    f"only matches found before the deadline are shown]")

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
                            if _sub_shown_lines == 0 and _sub_spill_rel:
                                # BUG_0232: every match was too large to fit the inline budget, so
                                # the (line-truncated) body is empty. Make the spillover file the
                                # headline instead of a generic "more lines omitted" marker.
                                # Guarded by `_sub_shown_lines == 0` so a PARTIALLY-shown result
                                # keeps the marker below byte-identical — test_grep_usability's
                                # fast-path truncation tests have shown_lines > 0 and pin it.
                                output_text += (
                                    f"\n\n[all {_total_matches} match(es) were larger than the "
                                    f"inline budget and are not shown. Read the full results with "
                                    f"read_logs/read_file on {_sub_spill_rel}, or re-run with "
                                    f"count_only=true for counts and a narrower pattern to show "
                                    f"lines inline]")
                            else:
                                _marker = f"\n\n[… {omitted} more line(s) omitted — "
                                if _sub_spill_rel:
                                    output_text += _marker + f"re-run with a narrower pattern or read the spillover file {_sub_spill_rel}]"
                                else:
                                    output_text += _marker + 're-run with a narrower pattern to avoid truncation]'

                    return f"{summary}:\n\n" + output_text

            # ── Slow path: pure Python fallback ──
            _rg_avail, _ = _check_tool_availability()
            logger.debug(
                f"grep: subprocess fast path unavailable (rg={_rg_avail}), falling back to Python")
            results = []

            flags = _smart_case_flags(pattern, smart_case)
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

            # BUG_0169: the fallback must not read a multi-GB artifact into memory either.
            # Over-size files get the same bounded header scan the ripgrep path uses.
            _max_mb, _head_bytes, _scan_limit, _budget_s = _grep_large_file_limits()
            _large_max_bytes = _max_mb * 1024 * 1024 if _max_mb > 0 else 0
            _large_skipped = 0
            _large_scanned = 0
            _large_skipped_paths: list = []

            # BUG_0221 (#2): one walker for the fallback and the over-size scan
            # (_iter_grep_scope_files). It prunes ignored directories before descending,
            # applies the shared default excludes, and uses the same basename-vs-relative-path
            # include/exclude semantics as the rg fast path's scope. Two disclosed deltas from
            # the old rglob branch: include globs now also match the relative path (so
            # `src/*.py` works) and directory symlinks are not followed (followlinks=False,
            # which also aligns the fallback with rg).
            deadline = time.monotonic() + timeout
            for file_path, rel_str in self._iter_grep_scope_files(
                    resolved, include, exclude, ignore_vcs, deadline):
                if time.time() - start_time > timeout:
                    was_timed_out = True
                    break

                # File reading and pattern matching
                if _large_max_bytes:
                    try:
                        _size = file_path.stat().st_size
                    except OSError:
                        _size = 0
                    if _size > _large_max_bytes:
                        # BUG_0169: skip the full read; scan only the header.
                        _large_skipped += 1
                        if len(_large_skipped_paths) < 11:
                            _large_skipped_paths.append(rel_str)
                        if _large_scanned < _scan_limit:
                            _large_scanned += 1
                            _head_hits = self._head_scan_matches(file_path, rel_str,
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

                    _file_matches, _file_timed_out, _file_hit_limit = _collect_file_matches(
                        results, lines, pattern_re, rel_str, context, timeout, start_time)
                    match_count += _file_matches
                    file_count += 1
                    if _file_timed_out:
                        was_timed_out = True
                    if _file_hit_limit:
                        hit_result_limit = True
                    if was_timed_out or hit_result_limit:
                        break
                except Exception as e:
                    logger.debug(f"Error reading file during grep (skipping): {e}")
                    continue

            # The walker self-terminates at `deadline`, so when it stopped early the loop above
            # exits without seeing another file. Disclose that truncation here.
            if time.time() - start_time > timeout:
                was_timed_out = True

            if not results and not was_timed_out:
                logger.debug(
                    f"grep: Python fallback also found no matches for '{pattern}' (subprocess already confirmed)")

            if not results:
                if was_timed_out:
                    _timeout_msg = f"Search timed out after {int(timeout)}s before finding any matches for '{pattern}'. Narrow your pattern or scope."
                    _timeout_msg += _incomplete_note(_large_skipped, _large_skipped_paths,
                                                     _max_mb)
                    return _timeout_msg
                exclude_info = f", excluding {exclude}" if exclude else ''
                _large_note = _incomplete_note(
                    _large_skipped, _large_skipped_paths, _max_mb,
                    tail='use read_file to inspect their headers/content')
                return (f"No matches found for pattern '{pattern}' in {path}/**/{include}"
                        f"{exclude_info}{searched_note}{_large_note}")

            summary = f"Found {match_count} matches for '{pattern}'{searched_note}"
            if context > 0:
                summary += f" (with {context} line(s) of context)"
            summary += _incomplete_note(_large_skipped, _large_skipped_paths, _max_mb,
                                        tail='use read_file for more')
            summary += _abbreviated_line_note(results)
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
