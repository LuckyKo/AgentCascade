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


def _exclude_matches(basename: str, rel_path: str, pattern: str) -> bool:
    """grep exclude semantics: no separator → basename; separator → relative path."""
    if not pattern:
        return False
    if '/' not in pattern and '\\' not in pattern:
        return fnmatch.fnmatch(basename, pattern)
    return fnmatch.fnmatch(rel_path, pattern)


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
                               literal: bool = False) -> Optional[int]:
        """Best-effort count of matches hiding behind VCS ignore rules (BUG_0061).

        With ignore_vcs=True the fast path passes NO explicit ignore flag, so ripgrep applies
        its built-in .gitignore/.ignore/.rgignore rules and silently prunes matching files. A
        search matching only gitignored files is then byte-identical to a genuine absence, and
        `rg --json` offers no way to recover the pruned paths (it emits only begin/end/match/
        context/summary — no ignored-path message). The only way to disambiguate is to ask rg
        a second time with the ignores turned off.

        Returns the number of `match` messages ripgrep reports when `--no-ignore` is forced
        on, or None if the probe could not be completed (ripgrep missing, timeout, error).
        Callers MUST treat None as "unknown" and stay silent — this probe exists only to
        disambiguate a zero-match result and must never turn a good search into an error.

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
            return None

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
            return None

        if result.returncode not in (0, 1, 2):
            return None

        # `json` and `re` are already module-level imports in this file (grep.py L4-5).
        count = 0
        for line in result.stdout.split('\n'):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get('type') == 'match':
                # One `match` message == one reported match line, matching the existing
                # _parse_rg_json counting. Deliberately NOT counting submatches.
                count += 1
        return count

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
                formatted = []
                match_count = 0
                lines = stdout_text.split('\n') if stdout_text.strip() else []

                for line in lines:
                    if not line.strip():
                        continue

                    try:
                        json_obj = json.loads(line)
                        entry_type = json_obj.get('type', '')

                        if entry_type == 'match':
                            data = json_obj.get('data', {})
                            file_path = data.get('path', {}).get('text', '')

                            line_num_data = data.get('line_number', 0)
                            if isinstance(line_num_data, dict):
                                line_num = line_num_data.get('start', 0)
                            else:
                                line_num = line_num_data

                            # rg's line payload always carries a trailing newline
                            # ('\n' on POSIX, '\r\n' on Windows) — _rg_lines_text strips it
                            # so the entries don't gain blank lines when joined below.
                            # BUG_0145: the shared extractor also decodes lines.bytes, which
                            # rg emits for non-UTF-8 lines, instead of rendering them blank.
                            match_text = _rg_lines_text(data.get('lines', {}))

                            normalized_path = file_path.replace('\\', '/')

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

                            # Strip the trailing newline (same as 'match' branch above) —
                            # would otherwise create blank lines between entries.
                            # BUG_0145: lines.bytes fallback lives in _rg_lines_text.
                            match_text = _rg_lines_text(data.get('lines', {}))
                            normalized_path = file_path.replace('\\', '/')
                            # BUG_0144: no indent on context lines — raw file bytes only.
                            formatted.append(_format_grep_entry(normalized_path, line_num, match_text))

                    except json.JSONDecodeError as e:
                        logger.debug('ripgrep JSON parse error: %s', e)

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
                f"grep subprocess timed out after {timeout}s (pattern/scope too broad); returning error instead of falling back to Python"
            )
            return None, 0, True, False, 0, None, 0, 0

        except (FileNotFoundError, PermissionError, OSError) as e:
            logger.debug(f"grep subprocess unavailable (falling back to Python): {e}")

        return None, 0, False, False, 0, None, 0, 0

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
                          literal: bool = False) -> str:
        """Search a single file for a regex pattern. Used when path is a file instead of directory."""

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
        output_text = '\n'.join(results)

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
             spill_file_path: Optional[str] = None) -> str:
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
                                              literal=literal)

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
                if _diag['large_files']:
                    _total_gb = _diag['total_bytes'] / (1024 ** 3)
                    return (f"ERROR: Search timed out after {int(timeout)}s — the scope contains "
                            f"{_diag['large_files']} file(s) larger than {_max_mb} MB "
                            f"(>= {_total_gb:.1f} GB total). grep does not stream files that large; "
                            f"use read_file to inspect a file's header/metadata, or narrow "
                            f"include/exclude to smaller files.")
                return f"ERROR: Search timed out after {int(timeout)}s — the pattern/scope was too broad to be useful. Narrow your search to a specific directory or use a more specific pattern."
            if results is not None:
                if count == 0 and not _sub_truncated:
                    logger.debug(f"grep: subprocess found no matches for '{pattern}'")
                    summary = f"No matches found for '{pattern}' in {path}{searched_note}"
                    # BUG_0061: with ignore_vcs=True the fast path passes no explicit ignore
                    # flag, so ripgrep's built-in .gitignore rules can prune the only matching
                    # file — making "no matches" indistinguishable from "matches were hidden".
                    # Probe once with --no-ignore; report only if it finds something. Advisory
                    # text appended to the summary line, so the existing "No matches found"
                    # prefix every format consumer matches on stays byte-preserved.
                    if ignore_vcs:
                        _hidden = self._probe_ignored_matches(pattern=pattern,
                                                              path=resolved,
                                                              include=include,
                                                              exclude=exclude,
                                                              context=context,
                                                              smart_case=smart_case,
                                                              timeout=timeout,
                                                              literal=literal)
                        if _hidden:
                            summary += (f" [note: {_hidden} match(es) exist in gitignored path(s) "
                                        f"excluded by ignore_vcs=True; re-run with ignore_vcs=False "
                                        f"to include them]")
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
                            _summary += (f" [note: matched in the first {_head_kb} KiB of "
                                         f"{_large['scanned']} file(s) larger than {_max_mb} MB{_partial}; "
                                         f"full file content was not streamed — use read_file for more]")
                            return f"{_summary}:\n\n" + '\n'.join(_large['formatted'])
                        if _large['large_files']:
                            summary += (f" [note: {_large['large_files']} file(s) larger than "
                                        f"{_max_mb} MB were not searched in full (only the first "
                                        f"{_head_bytes // 1024} KiB was scanned); use read_file to "
                                        f"inspect their headers/content]")
                    return f"{summary}:\n\n"
                else:
                    output_text = '\n'.join(results)
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
                        if _large_scanned < _scan_limit:
                            _large_scanned += 1
                            try:
                                _rel_large = str(file_path.relative_to(resolved)).replace('\\', '/')
                            except ValueError:
                                _rel_large = file_path.name
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
                    return f"Search timed out after {int(timeout)}s before finding any matches for '{pattern}'. Narrow your pattern or scope."
                exclude_info = f", excluding {exclude}" if exclude else ''
                _large_note = ''
                if _large_skipped:
                    _large_note = (f" [note: {_large_skipped} file(s) larger than {_max_mb} MB were "
                                   f"not read in full (only the first {_head_bytes // 1024} KiB was "
                                   f"scanned); use read_file to inspect their headers/content]")
                return (f"No matches found for pattern '{pattern}' in {path}/**/{include}"
                        f"{exclude_info}{searched_note}{_large_note}")

            summary = f"Found {match_count} matches for '{pattern}'{searched_note}"
            if context > 0:
                summary += f" (with {context} line(s) of context)"
            if _large_skipped:
                summary += (f" [note: {_large_skipped} file(s) larger than {_max_mb} MB were not "
                            f"read in full; only their first {_head_bytes // 1024} KiB was scanned "
                            f"— use read_file for more]")
            output_text = '\n'.join(results)

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
