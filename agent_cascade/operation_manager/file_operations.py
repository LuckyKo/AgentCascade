"""File operations — directory listing, read, write, edit, re-indent, delete, copy, move, backup cleanup."""

import difflib
import fnmatch
import json
import logging
import os
import re
import shutil
import time
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from agent_cascade.operation_manager import file_mutation
from agent_cascade.operation_manager.approval_provenance import APPROVER_REASON_LABEL
from agent_cascade.operation_manager.grep_ignore import DEFAULT_IGNORED_DIRS
from agent_cascade.tool_utils import truncate_with_spillover

# ─── list_dir filter helpers ──────────────────────────────────────────────

_SIZE_RE = re.compile(r'^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?$', re.IGNORECASE)
_SIZE_UNITS = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}

# Cap on files counted by _compute_scope_info for directory scope reporting.
# Prevents rglob('*') from walking arbitrarily large trees on the approval
# critical path (A5). Only delete_file and copy_file use this helper.
_SCOPE_INFO_FILE_CAP = 1000

# Default cap on entries rendered by list_dir. Also used when a caller supplies an
# invalid (<= 0) max_entries: -1 is a common "unlimited" idiom for max_depth, so
# silently honouring it would either truncate to nothing or unbounded-output.
_DEFAULT_LIST_DIR_MAX_ENTRIES = 500


def _parse_size(s: Optional[str]) -> Optional[int]:
    """Parse human-readable size string to bytes. Returns None if invalid/empty.

    No isdigit() fast-path: the regex handles bare numbers uniformly so a
    decimal like "1.5" is not truncated by an int cast on a fast path.
    """
    if not s or not s.strip():
        return None
    m = _SIZE_RE.match(s.strip())
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or 'B').upper()
    return int(value * _SIZE_UNITS[unit])


_RELATIVE_RE = re.compile(
    r'^(\d+(?:\.\d+)?)\s*(second|minute|hour|day|week|month|year)s?\s+ago$',
    re.IGNORECASE,
)
# Compact unit aliases: "2h", "90min", "1d", "2.5w"
_COMPACT_RE = re.compile(r'^(\d+(?:\.\d+)?)\s*([a-zA-Z]+)$')
_UNIT_ALIASES = {
    's': 1,
    'sec': 1,
    'second': 1,
    'seconds': 1,
    'm': 60,
    'min': 60,
    'mins': 60,
    'minute': 60,
    'minutes': 60,
    'h': 3600,
    'hr': 3600,
    'hrs': 3600,
    'hour': 3600,
    'hours': 3600,
    'd': 86400,
    'day': 86400,
    'days': 86400,
    'w': 604800,
    'wk': 604800,
    'week': 604800,
    'weeks': 604800,
    # Approximate: 1mo=30d, 1y=365d (leap years / variable month lengths ignored)
    'mo': 2592000,
    'month': 2592000,
    'months': 2592000,
    'y': 31536000,
    'yr': 31536000,
    'year': 31536000,
    'years': 31536000,
}


def _parse_time(s: Optional[str]) -> Optional[float]:
    """Parse a time string to epoch seconds. Returns None if invalid/empty."""
    if not s or not s.strip():
        return None
    s = s.strip()

    # 1. Pure digits → epoch seconds (UTC)
    if s.isdigit():
        return float(s)

    # 2. Relative expression: "N unit(s) ago" (full words, case-insensitive)
    m = _RELATIVE_RE.match(s)
    if m:
        value = float(m.group(1))
        unit = m.group(2).lower()
        return time.time() - value * _UNIT_ALIASES[unit]

    # 3. Compact form: "2h", "90min", "1d", "2.5w" (no "ago" suffix)
    m = _COMPACT_RE.match(s)
    if m:
        value = float(m.group(1))
        unit = m.group(2).lower()
        if unit in _UNIT_ALIASES:
            return time.time() - value * _UNIT_ALIASES[unit]

    # 4. ISO date/datetime (local timezone)
    try:
        # Normalize space separator to 'T' for fromisoformat compat (Py < 3.11)
        s_norm = s.replace(' ', 'T', 1) if re.match(r'^\d{4}-\d{2}-\d{2}\s', s) else s
        # Handle date-only by appending midnight
        if re.match(r'^\d{4}-\d{2}-\d{2}$', s_norm):
            dt = datetime.strptime(s_norm, '%Y-%m-%d')
        else:
            dt = datetime.fromisoformat(s_norm)
        return dt.timestamp()  # local timezone (tz-aware if suffix present)
    except (ValueError, TypeError):
        return None


def _parse_patterns(patterns_str: Optional[str]) -> Tuple[str, ...]:
    """Split a comma-separated glob string into a tuple of patterns (empty if none)."""
    if not patterns_str or not patterns_str.strip():
        return ()
    return tuple(p.strip() for p in patterns_str.split(',') if p.strip())


def _build_pattern_fn_from_patterns(patterns: Tuple[str, ...]) -> Optional[Callable]:
    """Build a name-matching callable from an already-parsed pattern tuple.

    All patterns are compiled into ONE anchored alternation and every candidate is
    normcased once per call, instead of running one ``fnmatch`` (which normcases the
    candidate again) per pattern. Semantics are unchanged: ``fnmatch.fnmatch`` is
    ``fnmatchcase(normcase(name), normcase(pattern))`` and the per-branch ``\\Z``
    keeps each alternative fully anchored. This removed ~3x the regex work in
    recursive listings, where every file is tested against every pattern.
    """
    if not patterns:
        return None
    combined = re.compile('|'.join(
        fnmatch.translate(os.path.normcase(p)) for p in patterns))
    normcase = os.path.normcase

    def _match(name: str) -> bool:
        return combined.match(normcase(name)) is not None

    return _match


def _build_pattern_fn(patterns_str: Optional[str]) -> Optional[Callable]:
    """Build a name-matching callable from a comma-separated glob string."""
    return _build_pattern_fn_from_patterns(_parse_patterns(patterns_str))


# DEFAULT_IGNORED_DIRS pre-split for the per-subdir prune check: literal names are
# O(1) set lookups, only real glob entries ('*.egg-info') need the regex engine.
# Both halves are normcased once so the Windows case-insensitivity of
# fnmatch.fnmatch is preserved.
_IGNORED_DIR_NAMES = frozenset(
    os.path.normcase(d) for d in DEFAULT_IGNORED_DIRS
    if not any(c in d for c in '*?['))
_IGNORED_DIR_GLOBS = tuple(
    os.path.normcase(d) for d in DEFAULT_IGNORED_DIRS
    if any(c in d for c in '*?['))


def _is_ignored_dir_name(name: str) -> bool:
    """True when *name* is pruned by grep's DEFAULT_IGNORED_DIRS (fnmatch semantics)."""
    norm = os.path.normcase(name)
    if norm in _IGNORED_DIR_NAMES:
        return True
    return any(fnmatch.fnmatchcase(norm, pat) for pat in _IGNORED_DIR_GLOBS)


def _pattern_names_ignored_dir(pattern: str) -> bool:
    """True when *pattern* explicitly names a DEFAULT_IGNORED_DIRS category.

    Mirrors the original ``any(fnmatch(p.rstrip('/*'), d) for d in DEFAULTS)`` check
    ('.git' and '.git/*' both name '.git') without looping every default.
    """
    return _is_ignored_dir_name(pattern.rstrip('/*'))


def _safe_relpath(path: str, root: str) -> str:
    """Return *path* relative to *root* (forward slashes, '' when equal).

    ``os.path.relpath`` is unsafe for directory entries whose final component is a
    Windows reserved device name (``nul``, ``con``, ``aux``, ``prn``, ``com1``…):
    ``ntpath.abspath`` rewrites such a path to the device form (``\\\\.\\nul``), so
    the drive comparison fails and relpath raises
    ``ValueError: path is on mount '\\\\.\\nul', start on mount 'N:'``. A real file
    with that name can exist on a network share or be created via a ``\\\\?\\`` path,
    and listing the containing directory must not abort because of it (BUG_0162,
    BUG_0052). ``PurePath.relative_to`` is pure string arithmetic — no Win32
    abspath call — so it stays correct for these names. Any path that is not a
    descendant of *root* falls back to its basename.
    """
    p = Path(path)
    r = Path(root)
    if p == r:
        return ''
    try:
        return p.relative_to(r).as_posix()
    except ValueError:  # different drive, or not a descendant
        return p.name


def _walk_relpath(path: str, root: str) -> str:
    """Fast relative path (forward slashes, '' when equal) for ``os.walk`` output.

    ``os.walk`` builds every yielded directory as ``root + os.sep + components``
    verbatim, so for those paths the relative part is pure string slicing. This
    matters for recursive listings: the old hot path went through
    ``Path.relative_to`` (pathlib part reloading, ``drive``, ``__str__``) for
    every file and directory and dominated runtime (BUG list_dir perf).

    Falls back to :func:`_safe_relpath` when the textual prefix does not hold
    (non-descendant path, or a root written with a different separator), keeping
    the same semantics including reserved device names (BUG_0162).
    """
    if path == root:
        return ''
    prefix = root if root.endswith(('/', '\\')) else root + os.sep
    if path.startswith(prefix):
        return path[len(prefix):].replace(os.sep, '/')
    # Windows paths are case-insensitive; os.walk preserves the case of the root
    # it was given, so this only helps callers passing an independently-cased root.
    if os.name == 'nt' and path[:len(prefix)].lower() == prefix.lower():
        return path[len(prefix):].replace(os.sep, '/')
    return _safe_relpath(path, root)


# Windows FILE_ATTRIBUTE_REPARSE_POINT: set on junctions, mount points and symlinks.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_junction_dir(path) -> bool:
    """True if *path* is a Windows junction / reparse-point directory.

    ``os.walk(followlinks=False)`` refuses to follow symlinks, but a junction is NOT
    reported as a symlink (``os.path.islink()`` is False), so the walk descends into it.
    That is what lets a junction inside the workspace expose a directory outside every
    allowed root (BUG_0163). ``os.path.isjunction`` exists on Python 3.12+; the
    ``st_file_attributes`` fallback covers older interpreters on Windows and returns
    False on POSIX (where symlinked dirs are already handled by ``followlinks=False``).
    """
    isjunction = getattr(os.path, 'isjunction', None)
    if isjunction is not None:
        try:
            return bool(isjunction(path))
        except OSError:
            return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, 'st_file_attributes', 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_within_roots(path, roots: List[Path]) -> bool:
    """True if *path* (after resolving links) is one of *roots* or lives under one.

    Resolution failure (broken junction, symlink loop) returns False: an entry whose
    target cannot be verified must not be traversed.
    """
    try:
        target = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    for root in roots:
        if target == root or root in target.parents:
            return True
    return False


def _walk_with_entries(root: str, onerror=None):
    """``os.walk(topdown=True, followlinks=False)`` that also yields DirEntry objects.

    Yields ``(dirpath, subdirs, filenames, subdir_entries)`` where *subdir_entries*
    maps a subdir name to its ``os.DirEntry``. For the prune/reorder consumer (the
    only one: it removes/reorders ``subdirs`` entries, never adds or renames) this
    matches ``os.walk``: pre-order DFS, directory symlinks are listed but not
    descended, scandir errors go to *onerror*, and mutating ``subdirs`` in place
    prunes the descent. Two deliberate simplifications vs ``os.walk`` (benign for
    that consumer): the descent symlink check reads the cached ``DirEntry
    .is_symlink()`` rather than re-checking the fresh path (os.walk re-checks per
    bpo-23605 in case the caller mutated entries), and an ``is_symlink()`` OSError
    skips the descent where os.walk would descend.

    The point of the extra tuple is that ``DirEntry.is_junction()`` /
    ``is_symlink()`` read attributes already fetched by the parent directory scan,
    whereas ``os.walk``'s own ``islink()`` plus a per-subdir ``lstat`` junction
    check cost one syscall each (seconds on slow/network volumes for big trees).
    """
    stack = [root]
    while stack:
        dirpath = stack.pop()
        try:
            with os.scandir(dirpath) as it:
                entries = list(it)
        except OSError as err:
            if onerror is not None:
                onerror(err)
            continue

        subdirs: List[str] = []
        filenames: List[str] = []
        subdir_entries: dict = {}
        for entry in entries:
            try:
                is_dir = entry.is_dir()  # follows symlinks, like os.walk
            except OSError:
                is_dir = False
            if is_dir:
                subdirs.append(entry.name)
                subdir_entries[entry.name] = entry
            else:
                filenames.append(entry.name)

        yield dirpath, subdirs, filenames, subdir_entries

        # The consumer may have pruned `subdirs` in place. Push the survivors in
        # reverse so the LIFO stack yields them in scan order (pre-order DFS).
        descend = []
        for name in subdirs:
            entry = subdir_entries.get(name)
            if entry is None:
                continue
            try:
                if entry.is_symlink():  # followlinks=False
                    continue
            except OSError:
                continue
            descend.append(os.path.join(dirpath, name))
        stack.extend(reversed(descend))


def _entry_is_junction(entry) -> bool:
    """Junction check from a scandir ``DirEntry`` (no extra syscall on Python 3.12+)."""
    is_junction = getattr(entry, 'is_junction', None)
    if is_junction is not None:
        try:
            return bool(is_junction())
        except OSError:
            return False
    return _is_junction_dir(entry.path)


# ─── edit_file delete_and_insert residue heuristic (BUG_0213) ─────────────────

_MD_HEADING_RE = re.compile(r'^#{1,6}(?:\s|$)')
_MD_LIST_ITEM_RE = re.compile(r'^(?:[-*+]|\d+[.)])\s')
# Punctuation that closes a phrase/sentence/bracket. A deleted block ending in one of
# these is presumed complete, so the surviving line is treated as a new block.
_TERMINAL_TAIL_CHARS = ('.', '!', '?', ':', ';', ')', ']', '}', '"', "'", '`',
                        '\u2019', '\u201d', '\u00bb')


def _orphan_residue_reason(last_deleted: str, next_line: str) -> Optional[str]:
    """Return a short reason when *next_line* looks orphaned by a deleted range.

    BUG_0213: ``delete_and_insert`` operates on raw line numbers, so a range that ends
    mid-paragraph leaves the tail of the block on the surviving side. The success
    message used to report only line arithmetic — this heuristic supplies the missing
    "does the range cut a block in half?" signal. Purely advisory: it never changes
    what is written.

    A surviving line that starts its own markdown block (heading, list item,
    blockquote, table row, fence, horizontal rule) is never flagged.
    """
    last = last_deleted.strip()
    nxt = next_line.strip()
    if not last or not nxt:
        return None
    if (_MD_HEADING_RE.match(nxt)
            or _MD_LIST_ITEM_RE.match(nxt)
            or nxt[0] in '|>'
            or nxt.startswith('```')
            or nxt.startswith('~~~')
            or re.fullmatch(r'(?:[-*_]\s*){3,}', nxt)):
        return None
    # A deleted heading took its section title with it; whatever survives below is
    # orphaned even if it reads like a normal paragraph (the second BUG_0213 shape).
    if _MD_HEADING_RE.match(last):
        return 'may be orphaned — it follows a deleted heading'
    # Plain prose/code: only flag when the deleted block did not close cleanly.
    if last.endswith(_TERMINAL_TAIL_CHARS):
        return None
    return 'continues the deleted block'


# ─── edit_file delete_and_insert structural-opener heuristic (BUG_0224) ────

# A markdown table separator row: pipes/dashes/colons/spaces with at least one dash
# (e.g. "|---|---|" or "| --- |").
_MD_TABLE_SEP_RE = re.compile(r'\s*\|?[\s:|-]*-[\s:|-]*\|?\s*')

def _structural_opener_kind(line: str) -> Optional[str]:
    """Return a structural-opener kind for *line*, or None if it is not one.

    BUG_0224: a ``delete_and_insert`` range whose FIRST deleted line is a
    structural opener (markdown heading, code fence, def/class header, table
    separator) can silently drop the section's heading when the replacement
    carries only the body. The kind is used only to decide whether to flag the
    edit — purely advisory. Deliberately conservative: prefer no note over a
    false positive, so only a handful of unambiguous openers are recognised.
    """
    s = line.strip()
    if not s:
        return None
    if _MD_HEADING_RE.match(s):
        return 'heading'
    if s.startswith('```') or s.startswith('~~~'):
        return 'fence'
    if re.match(r'^(?:def|class)\s+\w', s):
        return 'defclass'
    if '|' in s and _MD_TABLE_SEP_RE.fullmatch(s):
        return 'table_sep'
    return None


def _new_content_restores_opener(new_content: str, kind: str) -> bool:
    """True when *new_content* re-supplies a structural opener of *kind*.

    Any line of the same kind counts (a re-heading of a section is a valid edit),
    so this errs toward "restored" to keep the advisory conservative.
    """
    if not new_content:
        return False
    for line in new_content.splitlines():
        if _structural_opener_kind(line) == kind:
            return True
    return False


# ─── edit_file line-ending preservation / concurrent-write guard ─────────────

def _detect_newline_style(raw_text: str) -> str:
    """Return the file's dominant line ending: '\\r\\n', '\\r', or '\\n'.

    Edit matching works on a newline-normalized view, but the file must be written
    back with its own endings: ``write_text`` with default newline handling would
    translate every line to the OS native style and silently rewrite the whole file
    on Windows. A tie (mixed file) prefers CRLF; a file with no newline defaults to
    '\\n' so an added line stays portable.
    """
    crlf = raw_text.count('\r\n')
    lf = raw_text.count('\n') - crlf
    cr = raw_text.count('\r') - crlf
    if crlf and crlf >= lf and crlf >= cr:
        return '\r\n'
    if cr and cr > lf:
        return '\r'
    return '\n'


def _render_with_newlines(text: str, newline: str) -> bytes:
    """Encode *text* for writing, using *newline* for every line ending.

    Any CR/CRLF introduced by ``new_content`` is normalized first, so a caller that
    pastes CRLF text into an LF file (or vice versa) cannot create mixed endings and
    cannot turn the render into ``\\r\\r\\n``.
    """
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    if newline != '\n':
        normalized = normalized.replace('\n', newline)
    return normalized.encode('utf-8')


# ─── edit_file post-edit duplicate-line advisory (BUG_0250 family) ───────────

def _adjacent_duplicate_note(old_text: str, new_text: str,
                             match_mode: str = 'exact') -> Optional[str]:
    """Return a note when the edit NEWLY creates an adjacent identical-line pair.

    BUG_0250: a caller reusing an `exact`-style `new_content` (anchor line repeated)
    for `delete_and_insert` inserts the anchor verbatim, duplicating the surviving
    line above the insertion point. The same shape can come from any match/range
    boundary, so this compares the *count* of adjacent identical non-blank line
    pairs before and after the edit instead of reasoning about one mode. A count
    that increased means this edit is what put the duplicates next to each other.

    BUG_0262: the wording is mode-aware. Only `delete_and_insert` (verbatim insert,
    no anchor concept) can actually repeat its anchor or leave it at a range
    boundary, so that mode keeps the diagnostic "may repeat the anchor / range
    boundary" message. Every other mode (exact/heuristic/heuristic_agnostic) most
    often introduces intended duplicates (a repeated test seed, table row, or
    assertion), so the note stays neutral and does not assert an anchor-repeat or
    range-cause that cannot occur there. Detection is unchanged; only the message
    differs.

    Content-keyed counts (not positions) so pre-existing duplicate pairs elsewhere
    in the file never fire, and a pre-existing pair that merely moved is not
    re-reported. Message-only: the written bytes are never changed.
    """
    def _counts(text: str) -> dict:
        lines = text.splitlines()
        counts = {}
        for prev, cur in zip(lines, lines[1:]):
            if prev == cur and prev.strip():
                counts[cur] = counts.get(cur, 0) + 1
        return counts

    old_counts = _counts(old_text)
    new_counts = _counts(new_text)
    new_lines = new_text.splitlines()
    for content, count in new_counts.items():
        if count <= old_counts.get(content, 0):
            continue
        line_no = next(i + 1 for i in range(len(new_lines) - 1)
                       if new_lines[i] == content and new_lines[i + 1] == content)
        snippet = content.strip()
        if len(snippet) > 60:
            snippet = snippet[:60] + '…'
        if match_mode == 'delete_and_insert':
            return (f'note: the edit left two identical adjacent lines at '
                    f'{line_no}-{line_no + 1} ("{snippet}") — new_content may repeat the '
                    f'anchor/matched line it was meant to replace (delete_and_insert inserts '
                    f'new_content verbatim and has no anchor concept), or the range boundary '
                    f'left the anchor behind. Re-read the file before relying on this edit.')
        return (f'note: the edit left two identical adjacent lines at '
                f'{line_no}-{line_no + 1} ("{snippet}"); new_content introduced lines identical '
                f'to their neighbours — verify this is intended (e.g. a repeated test seed, '
                f'table row, or assertion). Re-read the file before relying on this edit.')
    return None


def _reproduces_matched_block(new_lines, block_lines) -> bool:
    """True when *new_lines* starts by reproducing *block_lines*' non-blank lines.

    The heuristic sibling-append path (BUG_0069) assumes ``new_content`` is "the
    matched block plus appended lines" and slices it by the block's line count. A
    plain replacement that merely re-indents the block would be sliced to nothing
    and silently discarded, so only take that path when the prefix really is the
    matched block (whitespace-insensitive, same convention as heuristic matching).
    """
    want = [''.join(l.split()) for l in block_lines if l.strip()]
    if not want:
        return False
    have = [''.join(l.split()) for l in new_lines if l.strip()]
    return have[:len(want)] == want


def _describe_list_filters(
    include: Optional[str],
    exclude: Optional[str],
    min_size: Optional[str],
    max_size: Optional[str],
    modified_after: Optional[str],
    modified_before: Optional[str],
    files_only: bool,
    dirs_only: bool,
    skip_vcs: bool = False,
) -> str:
    """Describe the filters that can hide existing entries (used by empty-result reporting).

    Only filters the caller actually supplied are named, so the empty-result message can
    point at the knob responsible instead of listing every default. *skip_vcs* is named
    only when it actually pruned a directory (the caller passes ``ctx.vcs_pruned > 0``),
    so a recursive listing that hid nothing does not blame it (BUG_0166).
    """
    parts = []
    if include:
        parts.append(f"include={include!r}")
    if exclude:
        parts.append(f"exclude={exclude!r}")
    if min_size:
        parts.append(f"min_size={min_size!r}")
    if max_size:
        parts.append(f"max_size={max_size!r}")
    if modified_after:
        parts.append(f"modified_after={modified_after!r}")
    if modified_before:
        parts.append(f"modified_before={modified_before!r}")
    if files_only:
        parts.append('files_only')
    if dirs_only:
        parts.append('dirs_only')
    if skip_vcs:
        parts.append('skip_vcs')
    return ', '.join(parts)


@dataclass
class FilterContext:
    """Bundle of all list_dir filters threaded through the listing call chain."""
    include_fn: Optional[Callable] = None
    exclude_fn: Optional[Callable] = None
    # Raw exclude pattern list, so directory-level pruning can match against the
    # path RELATIVE to the listed root (slash-bearing globs like '.git/*'), not
    # just the bare basename. Kept separate from exclude_fn on purpose: the name
    # filter stays basename-only (BUG_0008 invariant), only dir PRUNING widens.
    exclude_patterns: Tuple[str, ...] = ()
    # Raw include pattern list, so directory-level pruning can check whether an
    # explicit include names a VCS/build category (BUG_0221 re-admission).
    include_patterns: Tuple[str, ...] = ()
    # When True, prune any subdir whose name is in grep's DEFAULT_IGNORED_DIRS
    # (mirrors grep's ignore_vcs=True default). Default False so the raw
    # _list_recursive/_list_flat helpers stay behavior-neutral; list_directory opts in.
    skip_vcs: bool = False
    min_size: Optional[int] = None  # bytes, or None
    max_size: Optional[int] = None  # bytes, or None
    modified_after: Optional[float] = None  # epoch, or None
    modified_before: Optional[float] = None  # epoch, or None
    files_only: bool = False
    dirs_only: bool = False
    # Reporting state (not a filter): directories pruned by skip_vcs during this
    # traversal. Lets the empty-result message name skip_vcs only when it actually
    # hid something instead of on every recursive empty listing (BUG_0166).
    vcs_pruned: int = 0


# ─── Mixin: File operations for OperationManager ─────────────────────────


def _classify_line_difference(old_line: str, file_line: str) -> str:
    """Name the *kind* of difference between two lines (BUG_0231).

    Exact-match failures are usually one of three things, and knowing which is
    the whole value of the diagnostic: a tab/space transcription mismatch, an
    indentation/whitespace mismatch, or a genuine wording change.
    """
    old_core, file_core = old_line.strip(), file_line.strip()
    if old_core != file_core:
        return 'text differs'

    old_lead = old_line[:len(old_line) - len(old_line.lstrip(' \t'))]
    file_lead = file_line[:len(file_line) - len(file_line.lstrip(' \t'))]
    old_trail = old_line[len(old_line.rstrip(' \t')):]
    file_trail = file_line[len(file_line.rstrip(' \t')):]
    lead_differs = old_lead != file_lead
    trail_differs = old_trail != file_trail

    if lead_differs and trail_differs:
        return 'leading and trailing whitespace differ'
    if lead_differs:
        return 'leading whitespace/indentation differs'
    if trail_differs:
        return 'trailing whitespace differs'
    # Same text and same whitespace runs: the only difference is tab vs spaces.
    return 'tab/space mismatch (same text)'


def _describe_exact_miss(old_content: str, file_content: str,
                         tab_width: int = 4) -> str:
    """Explain why an exact edit missed, in terms of the nearest candidate.

    BUG_0231: the BUG_0186 diagnostic compared ``old_content`` line *i* against
    file line *i*. For any block below line 1 that reports "first diff at line 1"
    against the file header, which is both useless and misleading.

    Instead, align ``old_content`` to the file with ``difflib``'s longest
    matching line block, then report the first mismatch in that region and what
    kind of mismatch it is. If not a single line is shared, say so plainly.
    """
    old_ls = old_content.splitlines()
    file_ls = file_content.splitlines()
    if not old_ls:
        return f'old_content is empty (file has {len(file_ls)} line(s))'

    def _norm(s: str) -> str:
        return s.replace('\t', ' ' * tab_width)

    match = difflib.SequenceMatcher(
        None, [_norm(x) for x in file_ls], [_norm(x) for x in old_ls],
        autojunk=False,
    ).find_longest_match(0, len(file_ls), 0, len(old_ls))

    if match.size == 0:
        return (f'pattern not found anywhere in the file (no line of '
                f'old_content matches); old_content has {len(old_ls)} line(s), '
                f'file has {len(file_ls)}')

    # Where old_content[0] would land if the matched block were aligned. May be
    # negative when the shared block starts deeper inside old_content than it
    # does in the file (old_content begins before the file's matching region).
    offset = match.a - match.b
    nearest_line = match.a + 1
    candidate_line = offset + 1 if offset >= 0 else nearest_line
    leading_unmatched = max(0, -offset)

    for i, a in enumerate(old_ls):
        f_idx = offset + i
        if f_idx < 0:
            continue
        if f_idx >= len(file_ls):
            return (f'old_content runs past the end of the file: line {i + 1} '
                    f'has no counterpart (nearest candidate at file line '
                    f'{candidate_line})')
        b = file_ls[f_idx]
        if _norm(a) == _norm(b):
            continue
        kind = _classify_line_difference(a, b)
        return (f"first diff at line {f_idx + 1} (nearest candidate at file "
                f"line {candidate_line}): old_content={a[:50]!r} vs "
                f"file={b[:50]!r} — {kind}")

    if leading_unmatched:
        return (f'old_content has {leading_unmatched} leading line(s) before the '
                f'nearest candidate at file line {nearest_line}')
    if len(old_ls) != len(file_ls):
        return (f'nearby content matches line-for-line from file line '
                f'{candidate_line}, but old_content has {len(old_ls)} line(s) and '
                f'the file has {len(file_ls)}')
    return (f'lines match after tab/space normalization from file line '
            f'{candidate_line}, but not as one contiguous block (check line '
            f'endings)')


# ─── Memory-vault write screen ──────────────────────────────────────────────
# Persistent indirect prompt-injection guard (OWASP LLM01). Files under
# .agent_lessons/ and admin_suggestion_box/ are re-loaded as context by future
# agents, so a poisoned note persists across sessions — the only persistent
# injection vector in AC. We reject writes whose content looks like an
# instruction-injection directive. Patterns are deliberately conservative and
# LINE-ANCHORED (^): a legitimate note that merely *quotes* an injection phrase
# mid-sentence (e.g. a note about this very screen) does not match; only a
# directive that opens a line does. The list was validated against the full
# existing vault corpus (254+ .agent_lessons notes + the suggestion box) with
# zero false positives.

_INJECTION_PATTERNS = re.compile(
    r'(?m)'
    r'^[ \t>*#-]*(?:ignore|disregard|forget)\s+'
    r'(?:all\s+|any\s+)?(?:previous|prior|above|earlier|preceding)\s+'
    r'(?:instructions?|prompts?|messages?|directives?|rules?)'
    r'|^[ \t>*#-]*(?:reveal|show|print|output|display|dump|expose)\s+'
    r'(?:me\s+)?(?:the|your|an|this)\s+'
    r'(?:initial|original|hidden|developer|full|entire|complete)?\s*'
    r'(?:system|developer|dev)\s+prompt\b'
    r'|^[ \t>*#-]*(?:system|assistant|admin|root|developer)\s*:\s*'
    r'(?:ignore|disregard|override|new\s+instructions|from\s+now\s+on|you\s+are\s+now)',
    re.IGNORECASE,
)

# Path fragments (forward-slash form) marking a write as targeting a memory
# vault or the admin suggestion box. Backslashes in the input are normalized
# to '/' before matching, so Windows paths are covered too.
_VAULT_PATH_MARKERS = (
    '.agent_lessons/',
    'admin_suggestion_box/',
)


def _is_vault_path(file_path: str) -> bool:
    """True when *file_path* targets a memory vault or the admin suggestion box.

    Backslashes are normalized to '/' so Windows paths match too. Single source of
    truth for the vault-marker decision, reused by the copy/move pre-check so
    non-vault destinations can skip reading their source entirely.
    """
    normalized = file_path.replace('\\', '/')
    return any(marker in normalized for marker in _VAULT_PATH_MARKERS)


def _screen_vault_write(file_path: str, content: str) -> Optional[str]:
    """Screen a memory-vault / suggestion-box write for injection directives.

    Returns ``None`` when the write is safe, or a short human-readable reason
    string when *content* matches ``_INJECTION_PATTERNS``. Only paths containing a
    fragment in ``_VAULT_PATH_MARKERS`` are screened; every other path returns
    ``None`` immediately. The flag is read as ``settings.MEMORY_VAULT_WRITE_SCREEN``
    (lazy import, like the other settings reads in this module) so tests can toggle
    it by patching the attribute on ``agent_cascade.settings``.
    """
    from agent_cascade import settings
    if not settings.MEMORY_VAULT_WRITE_SCREEN:
        return None
    if not _is_vault_path(file_path):
        return None
    match = _INJECTION_PATTERNS.search(content)
    if match:
        return f"matched pattern {match.group(0)!r}"
    return None


def _screen_vault_copy_move(src_path, dest_path) -> Optional[str]:
    """Screen a copy/move that would persist *src_path*'s text at *dest_path*.

    A copy or move writes the source file's current content at the destination, so
    it is screened with the same rules as a direct ``write_file``. Non-vault
    destinations return immediately (the source is never read, keeping non-vault
    copies behavior-neutral); directory sources have no single content to screen and
    non-UTF-8 (binary) sources cannot carry a text injection directive, so both are
    skipped. The authoritative flag + marker + regex check is still performed by
    ``_screen_vault_write``.
    """
    if not _is_vault_path(dest_path.as_posix()):
        return None
    if not src_path.is_file():
        return None
    try:
        content = src_path.read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError):
        return None
    return _screen_vault_write(dest_path.as_posix(), content)


# ── edit_file helpers (extracted from edit_file; pure functions) ───────────

def _edit_parse_range(range_str, total_lines: int) -> tuple:
    """Parse a line-range spec for delete_and_insert mode.

    Returns (start_idx, end_idx) as 0-based Python slice indices.
    - start is inclusive, end is exclusive.
    - 1-indexed input: '3:7' means lines 3 through 7.
    - Single number '4' means insert before line 4 (empty delete range).
    - Negative numbers count from end: -1 = last line.
    - 0 means append at end of file.
    """
    # Models often send the range as a JSON number (e.g. 5 or 0); ints are valid
    # specs, so coerce before parsing instead of failing with AttributeError.
    range_str = str(range_str).strip()

    if ':' in range_str:
        parts = range_str.split(':')
        if len(parts) != 2:
            raise ValueError(f"Range must have exactly one ':'. Got '{range_str}'")

        start_part, end_part = parts

        # Parse start (empty means delete all from beginning, i.e., start at line 1)
        if start_part.strip() == '':
            start = 1  # Delete from the very first line
        elif start_part.strip() == '0':
            start = total_lines + 1  # Append beyond last line
        else:
            start = int(start_part)

        # Parse end (empty means delete all from start to end of file)
        if end_part.strip() == '':
            end = total_lines + 1  # Delete everything from start onward
        elif end_part.strip() == '0':
            end = total_lines + 1  # Append at end (same as empty)
        else:
            end = int(end_part)
        if start < 0:
            start = total_lines + 1 + start  # 1-indexed: -1 → last line
        if end < 0:
            end = total_lines + 1 + end  # consistent 1-indexed semantics

        # Clamp to valid 1-indexed bounds. The floor is 1, not 0: a negative
        # input that underflows (e.g. '-100:-100' on 5 lines) must land on the
        # first line. Clamping to 0 and then returning start-1=-1 would slice
        # from the END of the file and duplicate content (Python negative index).
        start = max(1, min(start, total_lines + 1))
        end = max(1, min(end, total_lines + 1))

        if start > end:
            raise ValueError(f"Start ({start}) must be <= end ({end})")

        # Convert to 0-based slice indices
        return start - 1, end

    else:
        # Single number = insert-only (or delete single line if new_content is empty)
        if range_str == '0':
            return total_lines, total_lines  # Append at end
        start = int(range_str)
        if start < 0:
            start = total_lines + 1 + start
        # Clamp to [1, total_lines+1] so that out-of-range means append
        start = max(1, min(start, total_lines + 1))
        zero_idx = start - 1  # Convert to 0-based index
        return zero_idx, zero_idx  # Empty range at position (insert point)


def _edit_detect_line_ending(line: str) -> str:
    """Detect the line ending style of a line."""
    if '\r\n' in line:
        return '\r\n'
    elif '\n' in line:
        return '\n'
    elif '\r' in line:
        return '\r'
    return ''


def _edit_get_leading_whitespace(s: str) -> str:
    """Get leading whitespace of first non-blank line."""
    for line in s.splitlines():
        if line.strip():
            return line[:len(line) - len(line.lstrip())]
    return ''


def _edit_get_indent_width(indent_str: str) -> int:
    """Calculate indent width in spaces (tab=4)."""
    return sum(4 if c == '\t' else 1 for c in indent_str if c in ' \t')


def _edit_detect_indent_char(indent_str: str) -> str:
    """Detect whether file uses tabs or spaces for indentation."""
    return '\t' if '\t' in indent_str else ' '


def _edit_render_indent(width: int, indent_char: str) -> str:
    """Render an indent of the given space-width using the file's indent char."""
    width = max(0, int(round(width)))
    if indent_char == '\t':
        return '\t' * (width // 4)
    return ' ' * width


def _edit_validate_indentation_consistency(content: str, file_path: str) -> list:
    """Detect indentation anomalies via increment-based threshold analysis."""
    from collections import Counter

    warnings = []
    indent_widths = []

    for i, line in enumerate(content.splitlines()):
        if not line.strip():
            continue
        leading_ws = line[:len(line) - len(line.lstrip())]
        width = _edit_get_indent_width(leading_ws)
        indent_widths.append((i + 1, width))

    if len(indent_widths) >= 2:
        increments = [
            abs(indent_widths[j][1] - indent_widths[j - 1][1]) for j in range(1, len(indent_widths))
        ]
        positive_increments = [inc for inc in increments if inc > 0]

        if len(positive_increments) >= 2:
            typical_increment = Counter(positive_increments).most_common(1)[0][0]
            threshold_val = max(typical_increment * 3, 8)
        else:
            threshold_val = 16

        for j in range(1, len(indent_widths)):
            prev_line_num, prev_w = indent_widths[j - 1]
            curr_line_num, curr_w = indent_widths[j]
            diff = abs(curr_w - prev_w)
            if diff > threshold_val:
                direction = 'increased' if curr_w > prev_w else 'decreased'
                warnings.append(f"Indentation anomaly at line {curr_line_num} in {file_path}: "
                                f"indent {direction} from {prev_w} to {curr_w} "
                                f"(jump of {diff} spaces, threshold={threshold_val})")

    return warnings


class FileOpsMixin:
    """File operation methods. Expects self to have __init__-set attributes."""

    @staticmethod
    def _approver_reason_line(approver_reason: str) -> str:
        """BUG_0039: the approver-supplied reason appended to approval result messages.

        Kept in one place so all five emitter sites (write/edit/re_indent/copy/move)
        stay byte-identical; the label itself lives in APPROVER_REASON_LABEL, which is
        also what tool_loop_detect._SECURITY_JUST_RE matches — a single source of truth.
        """
        return f"\n{APPROVER_REASON_LABEL} {approver_reason}"

    # ─── Backup cleanup (registered via atexit in __init__) ──────────────

    def cleanup_backups(self, agent_name: Optional[str] = None):
        """Archive .bak backup files (and their .manifest.json companions) into zip archives
        and remove the originals.

        If agent_name is provided, only processes that agent's backup directory.
        Otherwise, performs global cleanup across all agents.
        """
        # This runs via atexit during interpreter shutdown, where the log handler's
        # file stream may already be closed. Logging here would otherwise print a noisy
        # "I/O operation on closed file" traceback from logging's handleError. So every
        # log call is routed through _safe_log, which swallows I/O errors raised by an
        # already-closed handler. (Most of these are DEBUG-level no-ops anyway.)
        try:
            from agent_cascade.log import logger as _logger
        except Exception:
            _logger = None

        def _safe_log(level: int, msg: str, *args):
            if _logger is None:
                return
            try:
                _logger.log(level, msg, *args)
            except Exception:
                pass  # handler may be closed during atexit teardown — never surface it

        backup_base = self.base_dir / 'logs' / 'backups'
        if not backup_base.exists():
            _safe_log(logging.DEBUG, 'Backup directory does not exist: %s', backup_base)
            return

        if agent_name:
            safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
            agent_backup_dir = backup_base / safe_agent
            if not agent_backup_dir.exists():
                _safe_log(logging.DEBUG, 'Agent backup directory does not exist: %s', agent_backup_dir)
                return

            archive_path = agent_backup_dir / 'backup_archive.zip'
            bak_files = list(agent_backup_dir.glob('*.bak'))
            manifest_files = list(agent_backup_dir.glob('*.manifest.json'))

            if not bak_files and not manifest_files:
                _safe_log(logging.DEBUG, 'No .bak files to archive for agent %s', agent_name)
            else:
                if archive_path.exists():
                    timestamp = int(time.time())
                    archive_path.rename(archive_path.with_name(f'backup_archive.{timestamp}.zip'))

                old_zips = list(agent_backup_dir.glob('backup_archive.*.zip'))

                with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for bak_file in bak_files:
                        zf.write(bak_file, arcname=bak_file.name)
                    # Keep manifests (BUG_0219) with their backups.
                    for manifest in manifest_files:
                        zf.write(manifest, arcname=manifest.name)
                    for old_zip in old_zips:
                        zf.write(old_zip, arcname=old_zip.name)

                _safe_log(logging.DEBUG, 'Archived %d .bak files to %s', len(bak_files), archive_path)

            for old_zip in agent_backup_dir.glob('backup_archive.*.zip'):
                try:
                    old_zip.unlink()
                except Exception as e:
                    _safe_log(logging.WARNING, 'Failed to delete old archive %s: %s', old_zip, e)

            for bak_file in bak_files:
                try:
                    bak_file.unlink()
                except Exception as e:
                    _safe_log(logging.WARNING, 'Failed to delete backup file %s: %s', bak_file, e)
            for manifest in manifest_files:
                try:
                    manifest.unlink()
                except Exception as e:
                    _safe_log(logging.WARNING, 'Failed to delete backup manifest %s: %s', manifest, e)

        else:
            archive_path = backup_base / 'backup_archive.zip'
            bak_files = list(backup_base.rglob('*.bak'))
            manifest_files = list(backup_base.rglob('*.manifest.json'))

            if not bak_files and not manifest_files:
                _safe_log(logging.DEBUG, 'No .bak files to archive globally')
            else:
                if archive_path.exists():
                    timestamp = int(time.time())
                    archive_path.rename(archive_path.with_name(f'backup_archive.{timestamp}.zip'))

                old_zips = list(backup_base.glob('backup_archive.*.zip'))

                with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for bak_file in bak_files:
                        arcname = str(bak_file.relative_to(backup_base))
                        zf.write(bak_file, arcname=arcname)
                    # Keep manifests (BUG_0219) with their backups.
                    for manifest in manifest_files:
                        arcname = str(manifest.relative_to(backup_base))
                        zf.write(manifest, arcname=arcname)
                    for old_zip in old_zips:
                        zf.write(old_zip, arcname=old_zip.name)

                _safe_log(logging.DEBUG, 'Archived %d .bak files globally to %s', len(bak_files), archive_path)

            for old_zip in backup_base.glob('backup_archive.*.zip'):
                try:
                    old_zip.unlink()
                except Exception as e:
                    _safe_log(logging.WARNING, 'Failed to delete old archive %s: %s', old_zip, e)

            for bak_file in bak_files:
                try:
                    bak_file.unlink()
                except Exception as e:
                    _safe_log(logging.WARNING, 'Failed to delete backup file %s: %s', bak_file, e)
            for manifest in manifest_files:
                try:
                    manifest.unlink()
                except Exception as e:
                    _safe_log(logging.WARNING, 'Failed to delete backup manifest %s: %s', manifest, e)

    # ─── Static helpers for formatting and filtering ─────────────────────

    @staticmethod
    def _format_size(size_bytes: Optional[int]) -> str:
        """Convert bytes to human-readable string (B, KB, MB, GB)."""
        if size_bytes is None:
            return '?'
        if size_bytes < 1024:
            return f"{size_bytes} B"
        units = ['KB', 'MB', 'GB', 'TB']
        idx = -1
        val = float(size_bytes)
        while val >= 1024 and idx < len(units) - 1:
            val /= 1024
            idx += 1
        return f"{val:.1f} {units[idx]}"

    @staticmethod
    def _format_mtime(mtime_value) -> str:
        """Format modification time from a float timestamp like '2026-06-27 14:32'."""
        if mtime_value is None:
            return '?'
        try:
            dt = datetime.fromtimestamp(mtime_value)
            return dt.strftime('%Y-%m-%d %H:%M')
        except (OSError, ValueError):
            return '?'

    @staticmethod
    def _backup_path_str(backup_path) -> str:
        """Return the absolute path string for backup reporting."""
        return str(backup_path)

    @staticmethod
    def _sanitize_backup_name(name: str) -> str:
        """Replace characters that complicate downstream tooling with '_'.

        BUG_0219: names containing parens (e.g. ``APIRouter()``) made backup
        destinations awkward to handle. Alphanumerics (incl. Unicode), dots,
        hyphens and underscores are kept, so ordinary names (``foreign2.txt``,
        ``café.txt``) are unchanged and the ``<name>.<epoch>.bak`` convention is
        preserved. The original name is never lost — when sanitisation changes
        the name, it is recorded in the backup manifest.
        """
        return re.sub(r'[^\w.\-]', '_', name)

    @staticmethod
    def _manifest_path(backup_path) -> Path:
        """Path of the manifest file that sits next to *backup_path*."""
        return backup_path.parent / (backup_path.name + '.manifest.json')

    @staticmethod
    def _write_backup_manifest(backup_path, source: Path, timestamp: int) -> None:
        """Write a manifest mapping the sanitized backup name to the original entry name.

        Stored next to the backup as ``<backup>.manifest.json`` so a restorer
        (or downstream tooling) can recover the original name. Best-effort by
        contract: a manifest failure must NEVER affect the delete outcome, so
        every exception is swallowed here.
        """
        manifest_path = FileOpsMixin._manifest_path(backup_path)
        try:
            manifest_path.write_text(json.dumps({
                'original_name': source.name,
                'source_path': str(source),
                'created': timestamp,
            }, indent=2))
        except Exception:
            pass

    @staticmethod
    def _detect_binary(path) -> bool:
        """Check if file is binary by reading first 1 KiB for null bytes."""
        try:
            with open(path, 'rb') as f:
                return b'\x00' in f.read(1024)
        except OSError:
            return False

    @staticmethod
    def _compute_scope_info(path) -> str:
        """Compute pre-operation scope info string for reporting (file stats or directory count)."""
        if path.is_dir():
            # Cap the file count so a huge tree doesn't stall the approval prompt (A5).
            file_count = 0
            for entry in path.rglob('*'):
                if entry.is_file():
                    file_count += 1
                    if file_count >= _SCOPE_INFO_FILE_CAP:
                        return f"(directory, {_SCOPE_INFO_FILE_CAP}+ files)"
            return f"(directory, {file_count} files)"

        size_str = FileOpsMixin._format_size(path.stat().st_size)

        # Binary check
        if FileOpsMixin._detect_binary(path):
            return f"(file, binary, {size_str})"

        # Text file: count lines
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                line_count = sum(1 for _ in f)
            return f"(file, {line_count} lines, {size_str})"
        except (UnicodeDecodeError, OSError):
            return f"(file, binary, {size_str})"

    @staticmethod
    def _matches_filters(
        name: str,
        is_dir: bool,
        size: Optional[int],
        mtime: Optional[float],
        ctx: 'FilterContext',
        rel: Optional[str] = None,
    ) -> bool:
        """Check if an entry passes all filters in *ctx*.

        Name filter (include/exclude) applies to every entry. When *rel* (the entry's
        path relative to the listed root, forward-slashes) is supplied, a slash-bearing
        pattern like 'sub/*.gguf' or '.git/*' can match it too — the relative-path
        extension that makes path-qualified patterns work (the latent half of BUG_0047).
        Basename matching is always tried first and is never replaced, so the common
        cases (exclude='*.tmp', include='*.py') keep working. Size and date
        filters apply ONLY to files (directories are unaffected). Files whose
        stat failed (size/mtime = None) are excluded when a size or date
        filter is active, since we cannot verify they meet the criteria.
        """
        if ctx.include_fn and not (ctx.include_fn(name) or (rel is not None and ctx.include_fn(rel))):
            return False
        # Exclude drops any name that MATCHES the pattern. (This direction is easy
        # to get backwards: an inverted check would keep only the excluded names
        # and drop everything else.)
        if ctx.exclude_fn and (ctx.exclude_fn(name) or (rel is not None and ctx.exclude_fn(rel))):
            return False

        if is_dir:
            return True  # size/date filters never apply to directories

        if ctx.min_size is not None and (size is None or size < ctx.min_size):
            return False
        if ctx.max_size is not None and (size is None or size > ctx.max_size):
            return False
        if ctx.modified_after is not None and (mtime is None or mtime <= ctx.modified_after):
            return False
        if ctx.modified_before is not None and (mtime is None or mtime >= ctx.modified_before):
            return False
        return True

    @staticmethod
    def _dir_is_excluded(current_dir: Path, d_name: str, root: Path, ctx: 'FilterContext') -> bool:
        """True if subdir *d_name* (under *current_dir*) should be PRUNED from traversal.

        Asymmetric with the name filter on purpose: include never prunes (BUG_0008 —
        a matching file must be found even under a name-filter-failing dir), but an
        exclude that NAMES a directory means "drop this whole subtree". Prunes when
        ANY of: (a) the bare name matches an exclude pattern ('.git', '__pycache__');
        (b) the dir's path relative to *root* matches an exclude pattern (slash-bearing
        globs like 'sub/deep', pruned at the correct depth); (c) the pattern names THIS
        dir or globs its whole subtree ('.git', '.git/*', 'sub/**' — via rstrip('/*')).
        "Everything under a bare dir" is achieved by pruning the dir itself via (a), not
        a prefix match. When ctx.skip_vcs is set, also prunes any dir whose name is in
        grep's DEFAULT_IGNORED_DIRS.
        """
        if ctx.skip_vcs and _is_ignored_dir_name(d_name):
            # BUG_0221: an explicit include naming a VCS category re-admits it over
            # skip_vcs, unless an explicit exclude also names this dir (exclude wins).
            # Only specific patterns (not catch-alls like '*') can trigger re-admission:
            # a pattern "names a VCS category" iff its rstripped form matches a
            # DEFAULT_IGNORED_DIRS entry (e.g. '.git' → '.git', '.git/*' → '.git').
            if ctx.include_patterns and any(
                    _pattern_names_ignored_dir(p) for p in ctx.include_patterns):
                rel = _walk_relpath(os.path.join(str(current_dir), d_name), str(root))
                include_matches = any(
                    fnmatch.fnmatch(d_name, p) or fnmatch.fnmatch(rel, p)
                    or p.rstrip('/*') == rel
                    for p in ctx.include_patterns
                )
                exclude_matches = any(
                    fnmatch.fnmatch(d_name, p) or fnmatch.fnmatch(rel, p)
                    or p.rstrip('/*') == rel
                    for p in ctx.exclude_patterns
                ) if ctx.exclude_patterns else False
                if include_matches and not exclude_matches:
                    return False  # include re-admits, exclude does not override
            ctx.vcs_pruned += 1
            return True
        if ctx.exclude_fn is None:
            return False
        if ctx.exclude_fn(d_name):  # bare name: ".git", "__pycache__"
            return True
        rel = _walk_relpath(os.path.join(str(current_dir), d_name), str(root))
        for pat in ctx.exclude_patterns:
            # fnmatch(rel, pat): the dir's own relative path matches the pattern
            #   (e.g. 'sub/deep' prunes the 'deep' dir at the right depth).
            # pat.rstrip('/*') == rel: the pattern names THIS dir / globs its whole
            #   subtree ('.git', '.git/*', 'sub/**').
            # Deliberately NO 'pat.startswith(rel + "/")' clause: that over-prunes a
            #   parent whose only matching descendant is deeper (e.g. exclude='sub/*.gguf'
            #   must NOT drop sub/other.txt); the deeper dir is pruned at its own depth.
            if fnmatch.fnmatch(rel, pat) or pat.rstrip('/*') == rel:
                return True
        return False

    @staticmethod
    def _make_entry(name: str, is_dir: bool, stat_info) -> dict:
        """Create a standardized entry dict from a name, type flag, and optional stat info."""
        if stat_info is None:
            return {'name': name, 'is_dir': is_dir, 'size': 0 if is_dir else None, 'mtime': None}
        return {
            'name': name,
            'is_dir': is_dir,
            'size': 0 if is_dir else stat_info.st_size,
            'mtime': stat_info.st_mtime,
        }

    @staticmethod
    def _mark_empty(entry: dict, resolved: Path) -> None:
        """Mark an empty directory with ``is_empty`` so readers know it exists.

        Shared by the eager and lazy flat-listing paths. Scan errors are swallowed
        (leaving ``is_empty`` unset), matching the behavior both call sites had.
        """
        try:
            with os.scandir(str(resolved / entry['name'])) as sub_it:
                entry['is_empty'] = next(sub_it, None) is None
        except Exception:
            pass

    @staticmethod
    def _sort_entries(entries: list, sort_by: str) -> list:
        """Sort entries: directories always before files, then by requested key."""
        dirs = [e for e in entries if e['is_dir']]
        files = [e for e in entries if not e['is_dir']]

        def sort_key(entry):
            name_lower = entry['name'].lower()
            size = entry.get('size') or 0
            mtime = entry.get('mtime') or 0
            ext = Path(entry['name']).suffix.lower()
            return {
                'name': (name_lower,),
                'size': (-size, name_lower),
                'date': (-mtime, name_lower),
                'type': (ext, name_lower),
            }.get(sort_by, (name_lower,))

        dirs.sort(key=sort_key)
        files.sort(key=sort_key)

        return dirs + files

    # ─── Directory listing helpers ────────────────────────────────────────

    def _list_allowed_roots(self, fallback: Path) -> List[Path]:
        """Resolved roots a recursive listing is allowed to traverse.

        base_dir plus any extra work folders (RW or RO). Falls back to *fallback*
        (the directory being listed) for mixin users without the PathSecurityMixin
        attributes, e.g. the MockOpMgr test double in tests/scripts/check_oob_fixes.py.
        """
        candidates = [getattr(self, 'base_dir', None)]
        candidates.extend(getattr(self, 'extra_work_folders_rw', None) or [])
        candidates.extend(getattr(self, 'extra_work_folders_ro', None) or [])
        roots = []
        for candidate in candidates:
            if not candidate:
                continue
            try:
                roots.append(Path(candidate).resolve())
            except (OSError, RuntimeError):
                continue
        return roots or [fallback]

    def _list_flat(self, resolved: Path, ctx: 'FilterContext', sort_by: str,
                   max_entries: int) -> Tuple[str, int, int, int, int]:
        """Flat directory listing via os.scandir.

        Returns (output_str, dirs, files, size, raw_entries) where *raw_entries* is the
        pre-filter entry count (used to tell an empty directory from an all-filtered one).
        """
        dirs = []
        files = []
        raw_entries = 0

        # When the ordering needs no size/mtime (name/type sort) AND no size/date filter
        # is active, a file's stat is only needed to RENDER it -- never to filter or sort
        # it. Defer both the stat and the per-directory empty-scan to the entries that
        # actually survive truncation (H1/H4, O1/O4): collect lightweight (name, is_dir,
        # entry) records, sort/truncate by name, then stat + scan the <= max_entries
        # survivors. When a size/date sort or filter is active every candidate is
        # required, so the eager path below is kept unchanged.
        lazy = sort_by in ('name', 'type') and not (
            ctx.min_size is not None or ctx.max_size is not None
            or ctx.modified_after is not None or ctx.modified_before is not None
        )

        with os.scandir(str(resolved)) as it:
            for entry in it:
                raw_entries += 1
                is_dir = entry.is_dir()
                # Early exit for files_only/dirs_only (no stat needed)
                if ctx.files_only and is_dir:
                    continue
                if ctx.dirs_only and not is_dir:
                    continue
                if lazy:
                    # No size/date filter, so size/mtime never affect the predicate --
                    # pass None and defer the stat. Membership matches the eager path
                    # exactly (dirs pass on name; files pass on name alone).
                    if not self._matches_filters(entry.name, is_dir, size=None, mtime=None, ctx=ctx):
                        continue
                    (dirs if is_dir else files).append(
                        {'name': entry.name, 'is_dir': is_dir,
                         'size': None, 'mtime': None, '_entry': entry}
                    )
                    continue
                # Eager path: stat needed to filter and/or order by size/date.
                # Fetch stat BEFORE filter (needed for size/date checks)
                try:
                    stat_info = entry.stat()
                except OSError:
                    stat_info = None
                # Full filter (name + size + date) via context
                if not self._matches_filters(
                        entry.name,
                        is_dir,
                        size=stat_info.st_size if stat_info else None,
                        mtime=stat_info.st_mtime if stat_info else None,
                        ctx=ctx,
                ):
                    continue
                e = self._make_entry(entry.name, is_dir, stat_info)
                # Cheaply mark empty directories so readers know they exist.
                if is_dir:
                    self._mark_empty(e, resolved)
                (dirs if is_dir else files).append(e)

        sorted_dirs = self._sort_entries(dirs, sort_by)
        sorted_files = self._sort_entries(files, sort_by)

        overflow_count = 0
        if len(sorted_dirs) + len(sorted_files) > max_entries:
            remaining = max_entries
            show_dirs = sorted_dirs[:remaining]
            remaining -= len(show_dirs)
            show_files = sorted_files[:remaining]
            overflow_count = (len(sorted_dirs) - len(show_dirs)) + (len(sorted_files) - len(show_files))
            sorted_dirs, sorted_files = show_dirs, show_files

        if lazy:
            # Materialize survivors only: stat for size/mtime and scan dirs for the empty
            # marker. Rendered bytes are identical to the eager path -- the only difference
            # is that entries beyond max_entries were never stat'd or dir-scanned.
            for e in sorted_dirs + sorted_files:
                entry = e.pop('_entry')
                try:
                    stat_info = entry.stat()
                except OSError:
                    stat_info = None
                # Reuse _make_entry so size/mtime derivation is identical to the
                # eager path; assign back to preserve the surviving dict's identity.
                made = self._make_entry(e['name'], e['is_dir'], stat_info)
                e['size'] = made['size']
                e['mtime'] = made['mtime']
                if e['is_dir']:
                    self._mark_empty(e, resolved)

        total_dirs = len(sorted_dirs)
        total_files = len(sorted_files)
        total_size = sum(f['size'] for f in sorted_files if f.get('size'))

        output = ''
        if sorted_dirs:
            output += 'Directories:\n'
            for e in sorted_dirs:
                marker = ' (empty)' if e.get('is_empty') else f" (modified: {self._format_mtime(e['mtime'])})"
                output += f"  {e['name']}/{marker}\n"
        if sorted_files:
            output += '\nFiles:\n'
            for e in sorted_files:
                output += f"  {e['name']} ({self._format_size(e['size'])}, modified: {self._format_mtime(e['mtime'])})\n"
        if overflow_count > 0:
            output += f"  ... and {overflow_count} more entries (output limited to {max_entries} entries); use include/exclude or size/date filters to narrow\n"

        return output, total_dirs, total_files, total_size, raw_entries

    def _list_recursive(self, path: str, resolved: Path, ctx: 'FilterContext', sort_by: str, max_depth: int,
                        max_entries: int) -> Tuple[str, int, int, int, int, List[str]]:
        """Recursive directory listing via an os.walk-equivalent scandir traversal.

        Returns (output_str, dirs, files, size, raw_entries, notes) where *raw_entries*
        is the number of files+subdirs seen while walking, BEFORE name/size/date
        filtering (used to tell an empty tree from an all-filtered one), and *notes*
        are warning lines about data that is missing from the listing (unreadable
        directories, junctions that were not traversed).
        """
        entries_by_dir: dict = OrderedDict()
        visited_dirs: set = set()
        blocked_dirs: dict = {}  # abs path -> reason (junctions not traversed)
        walk_errors: List[OSError] = []
        # Entry budget shared by directory headers and file lines. Both modes now
        # count "entries" the same way (BUG_0167): a directory header and a file line
        # each consume one slot. The budget is consumed in collection order (dirs
        # before files within each walked directory); rendering may reorder entries
        # by sort_by, but the number of rendered lines stays capped.
        remaining = max_entries
        truncated = False
        raw_entries = 0
        allowed_roots = self._list_allowed_roots(resolved)
        # Computed once: every walk path is a textual descendant of this string,
        # so relative paths are string slices instead of Path.relative_to calls.
        root_str = str(resolved)

        def _collect_walk_error(err: OSError) -> None:
            walk_errors.append(err)

        for dirpath, subdirs, filenames, subdir_entries in _walk_with_entries(
                root_str, onerror=_collect_walk_error):
            current_dir = Path(dirpath)
            # Cycle guard: cheap (st_dev, st_ino) identity key from os.stat -- one
            # O(1) path-open vs Path.resolve()'s O(depth). os.stat follows junction/
            # symlink reparse points so an alias shares its target's key (os.lstat
            # returns the reparse point's own inode and would list the aliased dir
            # twice); resolve() covers st_ino==0 (FAT/exFAT/network) and dangling links.
            try:
                st = os.stat(dirpath)
                current_key = (st.st_dev, st.st_ino) if st.st_ino else current_dir.resolve()
            except OSError:
                current_key = current_dir.resolve()

            if current_key in visited_dirs:  # symlink / junction cycle guard
                subdirs.clear()
                continue
            visited_dirs.add(current_key)

            # Depth without any syscall: count the components below the (already
            # resolved) walk root. os.walk yields clean descendant paths, so this
            # reproduces the old parts-based depth arithmetic.
            rel_path = _walk_relpath(dirpath, root_str)
            current_depth = len(rel_path.split('/')) if rel_path else 0
            # Count before exclude/skip_vcs pruning so an all-pruned tree is not
            # misreported as a genuinely empty directory (BUG_0160).
            raw_entries += len(subdirs) + len(filenames)

            # Directory traversal is decoupled from file filtering: we always descend
            # into every subdirectory (os.walk is never pruned by the include/exclude
            # filter), so a matching file at any depth is found even when its parent's
            # own name fails the *file* filter (e.g. include="*.txt" must still reveal
            # a/inner.txt).
            #
            # Every walked subdir is stored as a [DIR] entry in its parent group so
            # that render_dir() descends into it -- the tree structure depends on this.
            # Whether the dir line is actually PRINTED is decided later by
            # displayed_dirs: a dir header prints iff it passes the filter on its own
            # name. A dir that fails the name filter is omitted as a header, but we
            # still descend into it so its matching files are rendered (indented under
            # the omitted parent). An empty/leaf dir whose name passes renders as
            # "[DIR] name/ (empty)".
            #
            # Directory-level EXCLUDE pruning. Asymmetric with INCLUDE on purpose:
            # include never prunes traversal (BUG_0008: a matching file must be found
            # even under a name-filter-failing dir), but an exclude that NAMES a
            # directory means "drop this whole subtree" — so prune it here. This is
            # what makes exclude='.git' (bare) and exclude='.git/*' suppress the whole
            # .git subtree instead of only its header.
            subdirs[:] = [d for d in subdirs if not self._dir_is_excluded(current_dir, d, resolved, ctx)]

            # Junction guard (BUG_0163): the os.walk-equivalent traversal does not
            # follow symlinks, but a Windows junction is not reported as a symlink,
            # so the walk DOES descend into it. A junction inside the workspace can
            # therefore expose a directory outside every allowed root (base_dir /
            # extra work folders). Record such entries so they stay visible but are
            # not traversed; render_dir marks them "(not traversed: ...)".
            for d_name in subdirs:
                full = current_dir / d_name
                entry = subdir_entries.get(d_name)
                is_junction = _entry_is_junction(entry) if entry is not None else _is_junction_dir(full)
                if is_junction and not _is_within_roots(full, allowed_roots):
                    blocked_dirs[str(full)] = 'target is outside the workspace'

            dir_entries = []
            for d_name in sorted(subdirs):
                # Early exit for files_only (no stat needed) — dirs are never files.
                if ctx.files_only:
                    continue
                # Only a dir that passes its own name filter gets a [DIR] header, so
                # only those consume the entry budget. A non-displayable dir still
                # needs an entry so render_dir descends into it (BUG_0008), but it
                # must not count against max_entries.
                displayable = self._matches_filters(d_name, True, None, None, ctx=ctx)
                if displayable and remaining <= 0:
                    truncated = True
                    break
                # A non-displayable dir never renders its [DIR] line (render_dir prints
                # only for displayed_dirs), so its mtime is never shown -- skip its stat
                # entirely (O1). The entry is still created so render_dir descends (BUG_0008).
                stat_info = None
                if displayable:
                    try:
                        stat_info = (current_dir / d_name).stat()
                    except OSError:
                        stat_info = None
                d_entry = self._make_entry(d_name, True, stat_info)
                d_entry['dirpath'] = dirpath  # parent dir, for relative-path rendering
                dir_entries.append(d_entry)
                if displayable:
                    remaining -= 1

            file_entries = []
            if ctx.dirs_only:
                pass  # dirs_only: files are never rendered and must not consume budget
            elif remaining <= 0:
                # Budget used up by directory headers. Files are not scanned; say so
                # rather than silently pretending the listing is complete.
                truncated = truncated or bool(filenames)
            else:
                # Only stat a file when its size/mtime is actually required: for a
                # size/date filter, to order by size/date, or to render the file line.
                # Under a pure name filter + name/type sort the survivors are exactly the
                # entries that render, so we filter on name first (no stat) and stat only
                # those survivors (H1/O1). With a size/date sort or filter every candidate
                # is needed, so the eager stat-before-filter order is kept unchanged.
                needs_stat = (ctx.min_size is not None or ctx.max_size is not None
                              or ctx.modified_after is not None
                              or ctx.modified_before is not None
                              or sort_by in ('size', 'date'))
                if needs_stat:
                    candidates = sorted(filenames)
                else:
                    # Name-only filter: drop non-matching names BEFORE sorting so a
                    # huge directory only sorts the survivors. Output order is
                    # unchanged -- sorting a subset preserves the relative order of
                    # the full sort, and the budget still selects the same first N.
                    survivors = []
                    for f_name in filenames:
                        rel = f'{rel_path}/{f_name}' if rel_path else f_name
                        if self._matches_filters(f_name, False, size=None, mtime=None,
                                                 ctx=ctx, rel=rel):
                            survivors.append(f_name)
                    candidates = sorted(survivors)
                for f_name in candidates:
                    if needs_stat:
                        # Pass the path RELATIVE to the listed root so slash-bearing
                        # include/exclude patterns ('sub/*.gguf', '.git/*') can match;
                        # basename is still tried first.
                        rel = f'{rel_path}/{f_name}' if rel_path else f_name
                        try:
                            stat_info = (current_dir / f_name).stat()
                        except OSError:
                            stat_info = None
                        size = stat_info.st_size if stat_info else None
                        mtime = stat_info.st_mtime if stat_info else None
                        if not self._matches_filters(
                                f_name,
                                False,
                                size=size,
                                mtime=mtime,
                                ctx=ctx,
                                rel=rel,
                        ):
                            continue
                    else:
                        stat_info = None  # filtered on name above; stat only if it renders
                    if remaining <= 0:
                        truncated = True
                        break
                    if not needs_stat:
                        # Survivor that will render: stat now to build its size/mtime line.
                        try:
                            stat_info = (current_dir / f_name).stat()
                        except OSError:
                            stat_info = None
                    f_entry = self._make_entry(f_name, False, stat_info)
                    f_entry['dirpath'] = dirpath  # parent dir, for relative-path rendering
                    file_entries.append(f_entry)
                    remaining -= 1

            # Record every walked directory (even with no displayable files) so the
            # tree structure and empty-dir markers are preserved.
            entries_by_dir[dirpath] = self._sort_entries(dir_entries + file_entries, sort_by)

            # Do not let os.walk descend into a blocked junction: it is displayed as a
            # [DIR] entry above, but its target is outside every allowed root.
            if blocked_dirs:
                subdirs[:] = [d for d in subdirs if str(current_dir / d) not in blocked_dirs]

            if remaining <= 0:
                # Budget exhausted: no further entry can be rendered. Any remaining
                # subdirectories were not scanned, so record that output was limited.
                if subdirs:
                    truncated = True
                subdirs.clear()
                continue
            if max_depth != -1 and current_depth >= max_depth - 1:
                subdirs.clear()

        # Decide which directories are actually displayed:
        # a dir's HEADER line is printed only when it passes the filter on its own
        # name. The traversal still always descends into every subdir regardless, so
        # matching files beneath a name-filter-failing dir are found and rendered --
        # they simply appear indented under their (omitted) parent without a [DIR]
        # header. A dir that fails the name filter is therefore omitted entirely
        # (documented in the task spec); only its displayable files show. The root is
        # always shown. "displayable descendant" here means a descendant whose own
        # header would print -- not merely any file, so we do NOT promote a failing
        # parent just because it holds a matching file.
        # A directory's HEADER line prints iff it passes the filter on its own name.
        # (The traversal always descends into every subdir regardless, so files beneath
        # a name-filter-failing dir are still rendered -- just without a [DIR] header.)
        # This is exactly the previous recursive behavior: _dir_displayed() returned
        # False immediately for any dir failing its own name filter (making the child
        # promotion branch inert), and returned True for the root WITHOUT adding it to
        # displayed_dirs. A single O(n) pass over entries_by_dir reproduces both --
        # the old recursion rescanned all of entries_by_dir per directory -> O(n^2).
        displayed_dirs = set()
        for dirpath, entries in entries_by_dir.items():
            if dirpath == root_str:
                # Root is never recorded as a displayed dir, but its dir-children
                # must still be added (they may not be keys in entries_by_dir if
                # the entry budget was exhausted before the walk descended).
                for e in entries:
                    if e['is_dir'] and self._matches_filters(e['name'], True, None, None, ctx=ctx):
                        displayed_dirs.add(os.path.join(dirpath, e['name']))
                continue
            if self._matches_filters(Path(dirpath).name, True, None, None, ctx=ctx):
                displayed_dirs.add(dirpath)
            for e in entries:
                if e['is_dir'] and self._matches_filters(e['name'], True, None, None, ctx=ctx):
                    displayed_dirs.add(os.path.join(dirpath, e['name']))

        # Blocked junctions are never walked, so they are absent from entries_by_dir;
        # add them to the display set explicitly (subject to the same name filter) so
        # the caller can see that the entry exists but was not traversed.
        for dirpath in blocked_dirs:
            if self._matches_filters(Path(dirpath).name, True, None, None, ctx=ctx):
                displayed_dirs.add(dirpath)

        # Totals count only what is actually displayed. Dirs are counted from the
        # displayable set; files/size are accumulated in render_dir below so they
        # stay consistent even under max_entries truncation (which drops some
        # entries). This mirrors flat mode and keeps the top-level "empty directory"
        # message accurate. Under files_only no [DIR] header is ever rendered, so the
        # displayable set must not count as output (otherwise a tree containing only
        # directories renders an empty listing with no empty-result message).
        total_dirs = 0 if ctx.files_only else len(displayed_dirs)
        rendered_files = 0
        rendered_size = 0

        # Render as a depth-indented tree (2 spaces per level). Every directory
        # appears even when empty (marked "(empty)"). Each entry is labelled with its
        # path RELATIVE to the listed root so parentage is unambiguous -- a file in a
        # subdirectory shows "fixed/BUG_0004.md" rather than a bare indented name.
        # Root-level entries render as just their own name (empty relative dir).
        def rel_of(dirpath: str) -> str:
            """Relative path of *dirpath* with respect to the listed root ('' for root)."""
            return _walk_relpath(dirpath, root_str)

        # Descent set for files_only. Under files_only no [DIR] entries are created
        # (L584 skips them), so render_dir's "if e['is_dir']" branch never fires and
        # nested files would be collected into entries_by_dir but never rendered.
        # Derive the descent set from the keys of entries_by_dir (every walked dir)
        # instead of from [DIR] entries. Build a parent->children map in a SINGLE O(n)
        # pass so render_dir stays linear (the old per-dir rescan of entries_by_dir
        # was O(n^2), a regression BUG_0049 deliberately removed).
        #
        # This automatically respects the exclude/skip_vcs prune: excluded subtrees
        # are removed from subdirs[:] at L579 BEFORE os.walk descends, so they are
        # never yielded as a dirpath and therefore never become keys of entries_by_dir.
        # Hence they cannot appear in children_by_parent either. Same for max_depth /
        # max_entries truncation, which clear subdirs before descent.
        children_by_parent: dict = {}
        if ctx.files_only:
            for k in entries_by_dir:
                parent_key = str(Path(k).parent)
                if parent_key in entries_by_dir and parent_key != k:
                    children_by_parent.setdefault(parent_key, set()).add(k)

        def render_dir(root_dirpath: str) -> None:
            """Render the collected tree iteratively (BUG_0168).

            An explicit stack of (dirpath, depth, entry-iterator) replaces the former
            recursion: a tree deeper than Python's recursion limit (~1000 levels) used
            to raise RecursionError and turn the whole listing into an error string.
            A stack of iterators preserves the original depth-first order.
            """
            nonlocal output, rendered_files, rendered_size
            stack = [(root_dirpath, 0, iter(entries_by_dir.get(root_dirpath, [])))]
            _cur_dirpath = None  # tracks which stack frame is active (for prefix/base_rel caching)
            while stack:
                dirpath, depth, entries = stack[-1]
                entry = next(entries, None)
                if entry is None:
                    stack.pop()
                    if ctx.files_only:
                        # No [DIR] entries exist under files_only, so descend into the
                        # walked child dirs after rendering this group. reverse=True
                        # keeps ascending order on the LIFO stack.
                        for child in sorted(children_by_parent.get(dirpath, set()), reverse=True):
                            stack.append((child, depth + 1, iter(entries_by_dir.get(child, []))))
                    continue

                # Compute per-group constants once (on first entry of each stack frame)
                if dirpath != _cur_dirpath:
                    _cur_dirpath = dirpath
                    prefix = '  ' * depth
                    base_rel = rel_of(dirpath)
                if entry['is_dir']:
                    child = os.path.join(dirpath, entry['name'])
                    blocked_reason = blocked_dirs.get(child)
                    # Print the dir line only when it passed its own name filter (it is
                    # in displayed_dirs); a non-displayable dir is skipped but we still
                    # descend below so matching files are not lost.
                    if child in displayed_dirs:
                        if blocked_reason:
                            marker = f" (not traversed: {blocked_reason})"
                        else:
                            is_empty = not entries_by_dir.get(child)
                            marker = ' (empty)' if is_empty else f" (modified: {self._format_mtime(entry['mtime'])})"
                        rel_name = entry['name'] if not base_rel else f"{base_rel}/{entry['name']}"
                        output += f"{prefix}[DIR] {rel_name}/{marker}\n"
                    # Never descend into a junction whose target is outside the
                    # allowed roots (BUG_0163).
                    if not blocked_reason:
                        stack.append((child, depth + 1, iter(entries_by_dir.get(child, []))))
                else:
                    rel_name = entry['name'] if not base_rel else f"{base_rel}/{entry['name']}"
                    output += f"{prefix}{rel_name} ({self._format_size(entry['size'])}, modified: {self._format_mtime(entry['mtime'])})\n"
                    rendered_files += 1
                    rendered_size += entry['size'] or 0

        output = ''
        render_dir(root_dirpath=str(resolved))

        # total_files/total_size are accumulated in render_dir (rendered_files /
        # rendered_size) so they reflect exactly the file lines actually printed,
        # staying consistent with the output even under max_entries truncation.
        total_files = rendered_files
        total_size = rendered_size

        if truncated:
            output += (f"\n  ... (output limited to {max_entries} entries; "
                       'use include/exclude or size/date filters to narrow)\n')

        # BUG_0165: os.walk with onerror=None silently skips directories it cannot
        # read, so a permission-denied subtree simply vanished from the listing and
        # could seed a false-absence claim. Report every skipped directory instead.
        notes: List[str] = []
        if walk_errors:
            def _err_label(err: OSError) -> str:
                filename = getattr(err, 'filename', None) or ''
                rel = _safe_relpath(str(filename), str(resolved)) if filename else ''
                reason = getattr(err, 'strerror', None) or str(err)
                return f"{rel or '.'} ({reason})"

            shown = walk_errors[:5]
            details = '; '.join(_err_label(err) for err in shown)
            extra = f"; +{len(walk_errors) - len(shown)} more" if len(walk_errors) > len(shown) else ''
            noun = 'directory' if len(walk_errors) == 1 else 'directories'
            possessive = 'its' if len(walk_errors) == 1 else 'their'
            notes.append(f"[warning] {len(walk_errors)} {noun} could not be read; "
                         f"{possessive} contents are missing from this listing: {details}{extra}")

        if ctx.files_only and blocked_dirs:
            names = ', '.join(sorted(_safe_relpath(p, str(resolved)) for p in blocked_dirs))
            notes.append(f"[warning] not traversed (target is outside the workspace): {names}")

        return output, total_dirs, total_files, total_size, raw_entries, notes

    # ─── Public directory listing API ─────────────────────────────────────

    def list_directory(
        self,
        path: str = '.',
        recursive: bool = False,
        max_depth: int = -1,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        sort_by: str = 'name',
        show_summary: bool = False,
        max_entries: int = _DEFAULT_LIST_DIR_MAX_ENTRIES,
        char_limit: int = 3000,
        agent_name: str = 'unknown',
        min_size: Optional[str] = None,
        max_size: Optional[str] = None,
        modified_after: Optional[str] = None,
        modified_before: Optional[str] = None,
        files_only: bool = False,
        dirs_only: bool = False,
        skip_vcs: bool = True,
    ) -> str:
        """List contents of a directory with optional recursive traversal, filtering, sorting, and summary.

        Truncates output to *char_limit* characters via spillover files for the given *agent_name*.
        """
        try:
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return f"Directory not found: {path}"
            if not resolved.is_dir():
                return f"Not a directory: {path}"

            valid_sort_keys = {'name', 'size', 'date', 'type'}
            if sort_by not in valid_sort_keys:
                sort_by = 'name'

            # Build the filter context (single object threaded through the call chain).
            warnings = []
            # BUG_0164: normalize values that would otherwise corrupt the listing.
            # max_entries <= 0 used to truncate every entry while still printing
            # "output limited to N" (and, recursively, blame skip_vcs for the empty
            # result); max_depth < -1 pruned every subdir at the root with no warning.
            if max_entries < 1:
                warnings.append(f"[warning] max_entries={max_entries} is invalid (must be >= 1); "
                                f"using {_DEFAULT_LIST_DIR_MAX_ENTRIES}.")
                max_entries = _DEFAULT_LIST_DIR_MAX_ENTRIES
            if max_depth < -1:
                warnings.append(f"[warning] max_depth={max_depth} is invalid (use -1 for unlimited); treating as -1.")
                max_depth = -1
            if recursive and max_depth == 0:
                recursive = False
            ctx_min_size = _parse_size(min_size)
            if min_size and ctx_min_size is None:
                warnings.append(f"[warning] min_size='{min_size}' not recognized; no size filter applied.")
            ctx_max_size = _parse_size(max_size)
            if max_size and ctx_max_size is None:
                warnings.append(f"[warning] max_size='{max_size}' not recognized; no size filter applied.")
            ctx_modified_after = _parse_time(modified_after)
            if modified_after and ctx_modified_after is None:
                warnings.append(f"[warning] modified_after='{modified_after}' not recognized; no date filter applied.")
            ctx_modified_before = _parse_time(modified_before)
            if modified_before and ctx_modified_before is None:
                warnings.append(
                    f"[warning] modified_before='{modified_before}' not recognized; no date filter applied.")

            # Parse exclude once: the pattern tuple feeds both the name filter and the
            # directory-pruning helper (avoids splitting the string twice).
            ex_patterns = _parse_patterns(exclude)
            inc_patterns = _parse_patterns(include)
            ctx = FilterContext(
                include_fn=_build_pattern_fn(include),
                exclude_fn=_build_pattern_fn_from_patterns(ex_patterns),
                exclude_patterns=ex_patterns,
                include_patterns=inc_patterns,
                skip_vcs=skip_vcs,
                min_size=ctx_min_size,
                max_size=ctx_max_size,
                modified_after=ctx_modified_after,
                modified_before=ctx_modified_before,
                files_only=files_only,
                dirs_only=dirs_only,
            )

            header = f"Contents of {path}/ (Absolute path: {resolved}):\n\n"

            notes: List[str] = []
            if recursive:
                output, total_dirs, total_files, total_size, raw_entries, notes = self._list_recursive(
                    path, resolved, ctx, sort_by, max_depth, max_entries)
                is_empty = not (total_dirs or total_files)
            else:
                output, total_dirs, total_files, total_size, raw_entries = self._list_flat(
                    resolved, ctx, sort_by, max_entries)
                is_empty = not (total_dirs or total_files)

            # BUG_0221: disclose VCS pruning in the header region (survives
            # head-truncation) when the caller's patterns name a pruned category.
            # Preserves the BUG_0166 invariant: skip_vcs is only blamed when it hid
            # something. Suppressed on empty results (the BUG_0160/0166 message already
            # names skip_vcs there).
            if (recursive and not is_empty and ctx.vcs_pruned > 0):
                all_pats = ctx.include_patterns + ctx.exclude_patterns
                if any(fnmatch.fnmatch(p.rstrip('/*'), d)
                       for p in all_pats for d in DEFAULT_IGNORED_DIRS):
                    output = (f"  [note] {ctx.vcs_pruned} VCS/build dir(s) pruned by skip_vcs "
                              f"(your include/exclude patterns name a pruned category)\n"
                              + output)

            # BUG_0160: distinguish a genuinely empty directory from a listing whose
            # entries all failed the filters. The old single message ("empty directory
            # or all entries filtered out") was true for both opposite situations, so a
            # negative finding could not be based on a filtered listing alone.
            if is_empty:
                if raw_entries == 0:
                    if notes:
                        # BUG_0165: an unreadable root is not an empty directory.
                        output += '  (nothing could be listed; see warning below)\n'
                    else:
                        output += '  (directory contains no entries)\n'
                else:
                    active = _describe_list_filters(
                        include, exclude, min_size, max_size, modified_after, modified_before,
                        files_only, dirs_only,
                        skip_vcs=recursive and skip_vcs and ctx.vcs_pruned > 0)
                    detail = f': {active}' if active else ''
                    noun = 'entry exists' if raw_entries == 1 else 'entries exist'
                    output += f'  (no entries matched the active filters{detail})\n'
                    output += (f'  {raw_entries} {noun} before filtering; '
                               'adjust or remove the filters to list them.\n')
                    # BUG_0166: flat mode matches include/exclude against the basename
                    # only, so a slash-bearing pattern silently matches nothing. Point
                    # at recursive mode instead of leaving the caller with a mystery.
                    if not recursive and include and ('/' in include or '\\' in include):
                        output += ("  [hint] include contains '/', which is matched against relative "
                                   'paths only in recursive mode; set recursive=true to match it.\n')

            # Notes about data missing from the listing (unreadable dirs, junctions
            # not traversed) are surfaced with the other warnings at the end.
            warnings.extend(notes)

            if show_summary:
                # BUG_0169: these are the counts of lines actually listed, not totals
                # for the whole tree -- max_entries/max_depth truncation can hide
                # entries, so calling them "Total" was misleading.
                output += f"\nSummary:\n"
                output += f"  Listed directories: {total_dirs}\n"
                output += f"  Listed files:       {total_files}\n"
                output += f"  Listed size:        {self._format_size(total_size)}\n"

            # Surface unrecognized size/date filter values so the agent knows a
            # filter was silently ignored (prevents silent no-op confusion).
            if warnings:
                output += '\n' + ''.join(f"  {w}\n" for w in warnings)

            result = header + output
            return truncate_with_spillover(
                result,
                char_limit,
                instance_name=agent_name,
                tool_name='list_dir',
                base_dir=self.base_dir,
                operation_mode='head',
            )
        except Exception as e:
            from agent_cascade.log import logger
            logger.debug(f"Error listing directory: {e}")
            return f"Error listing directory: {str(e)}"

    # ─── Read file ────────────────────────────────────────────────────────

    def read_file(self, path: str, start_line: int = 1, limit: Optional[int] = None) -> str:
        """Read a file. Uses line-by-line iteration for memory efficiency when range is specified.

        If *limit* is not provided, falls back to ``DEFAULT_READ_FILE_MAX_LINES`` from settings.
        """
        if limit is None:
            from agent_cascade.settings import DEFAULT_READ_FILE_MAX_LINES
            limit = DEFAULT_READ_FILE_MAX_LINES
        try:
            resolved = self._resolve_path(path, mode='ro')
            if not resolved.exists():
                return f"File not found: {path}"
            if not resolved.is_file():
                return f"Not a file: {path}"

            start_line = max(1, start_line)  # Defensive: prevent negative/zero line numbers

            # Count total lines first to validate start_line bounds (streaming, no memory)
            with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
                total_lines = sum(1 for _ in f)

            if total_lines == 0:
                return f"OK: Read {path} (0 lines, {self._format_size(resolved.stat().st_size)})"

            if start_line > total_lines:
                return f"ERROR: start_line {start_line} exceeds file length ({total_lines} lines)"

            end_line = start_line + limit - 1
            hit_end = False

            lines = []
            with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
                for line_num, line in enumerate(f, 1):
                    if line_num < start_line:
                        continue
                    if line_num > end_line:
                        hit_end = True
                        break
                    lines.append(line.rstrip('\n'))

            actual_end = start_line + len(lines) - 1
            file_size_str = self._format_size(resolved.stat().st_size)

            # Encoding warning via replacement character count
            content_text = ''.join(lines)
            repl_count = content_text.count('\ufffd')
            encoding_note = f" [encoding: utf-8 with {repl_count} replacement(s)]" if repl_count > 0 else ''

            header = f"OK: Read {path} lines {start_line}-{actual_end}/{total_lines} (text, {file_size_str}){encoding_note}"
            if hit_end:
                header += ' [TRUNCATED]'

            content = ''.join([f"{start_line + i}:\t{lines[i]}" for i in range(len(lines))])
            result = f"{header}\n```\n{content}\n```"

            # Compact pagination footer when truncated
            if hit_end:
                result += f"\n→ continue at start_line={actual_end + 1}"

            return result
        except Exception as e:
            return f"ERROR: {str(e)}"

    # ─── Write file ──────────────────────────────────────────────────────

    def write_file(self, path: str, content: str, agent_name: str, justification: str = '') -> str:
        """Write a file — auto-approved for new files and owned files."""
        try:
            resolved = self._resolve_path(path, mode='rw')
        except Exception as e:
            return f"ERROR: {str(e)}"
        is_new = not resolved.exists()

        # Clobber-guard baseline for the overwrite branch: capture the target's mtime+size
        # now, BEFORE the unbounded approval wait, so the commit region can detect a write
        # that landed while we were parked. O(1) stat — no full re-read of a file we are
        # about to overwrite. A brand-new file has no baseline, so it is left unguarded.
        if not is_new:
            try:
                _stat = resolved.stat()
                baseline_mtime_ns = _stat.st_mtime_ns
                baseline_size = _stat.st_size
            except Exception:
                baseline_mtime_ns = baseline_size = None
        else:
            baseline_mtime_ns = baseline_size = None

        if not self._is_auto_approved(path, agent_name, creating_new=True):
            description = f"Overwrite existing file: {path} ({len(content)} chars)"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='write_file',
                tool_args={
                    'path': path,
                    'content': content,
                    'justification': justification
                },
                description=description,
            )
            if not approved:
                return f"REJECTED: {reason}"
            approver_reason = reason  # BUG_0039: keep provenance — do not overwrite the caller's justification
        else:
            approver_reason = ''

        # Memory-vault write screen (OWASP LLM01, persistent injection vector).
        # Runs before the backup/write so a rejected write leaves no orphan .bak.
        # Uses the already-resolved destination (not the raw caller path) so a
        # relative or traversal spelling cannot dodge the vault-marker check.
        _rejection = _screen_vault_write(resolved.as_posix(), content)
        if _rejection:
            from agent_cascade.log import logger
            logger.warning('[MEMORY_SCREEN] Rejected write to %s: %s', path, _rejection)
            return (f"ERROR: Write to '{path}' rejected by memory-vault injection screen "
                    f"({_rejection}). The content matches prompt-injection patterns. "
                    f"Remove the offending content and retry.")

        try:
            resolved = self._resolve_path(path, mode='rw')

            # Commit region (guard → backup → write → own) serialized by the process-global
            # mutation lock; acquired after the re-resolve so the guard and the write target
            # the same resolved path. The response construction below stays outside the lock.
            with file_mutation.mutate():
                # Clobber guard (overwrite branch only): if the file changed since the
                # pre-approval snapshot — by an external editor, a second AC instance, or a
                # sibling thread that released the lock before us — abort instead of silently
                # overwriting. mtime+size is a cheap O(1) check; the tradeoff is no full
                # re-read of a file being overwritten, so a same-size rewrite landing inside
                # one clock tick can slip past it; edit_file uses the strictly stronger
                # raw-byte compare, and that asymmetry is intentional.
                #
                # The unguarded case is the new-file branch (baseline None). That also covers
                # a path that was absent at the pre-approval check but created externally
                # during the unbounded approval wait: with no baseline the write proceeds,
                # backing up and overwriting whatever appeared. This is an accepted, inherent
                # gap of not re-reading the target, not a bug.
                if baseline_mtime_ns is not None:
                    try:
                        _live = resolved.stat()
                    except Exception:
                        _live = None
                    if (_live is None
                            or _live.st_mtime_ns != baseline_mtime_ns
                            or _live.st_size != baseline_size):
                        return (f"ERROR: {path} changed on disk between read and write "
                                f"(concurrent modification). The write was NOT applied — "
                                f"re-read the file and retry with the current content.")

                # Backup if overwriting. is_new_now is re-derived inside the lock (unlike the
                # pre-approval is_new baseline) so the backup decision reflects the file's
                # state at commit time, just before the verbatim write.
                backup_path_str = ''
                is_new_now = not resolved.exists()
                if not is_new_now:
                    safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
                    backup_dir = self.base_dir / 'logs' / 'backups' / safe_agent
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    backup_path = backup_dir / f"{resolved.name}.{int(time.time())}.bak"
                    shutil.copy2(resolved, backup_path)
                    backup_path_str = self._backup_path_str(backup_path)

                resolved.parent.mkdir(parents=True, exist_ok=True)
                # Verbatim write: newline='' disables OS line-ending translation so the
                # caller's bytes land unchanged (an LF file is not flipped to CRLF on
                # Windows). A full overwrite has no source style to preserve, so this is not
                # _render_with_newlines — a new file has no newline style to detect.
                with resolved.open('w', encoding='utf-8', newline='') as _f:
                    _f.write(content)
                self._own(resolved, agent_name)

            line_count = len(content.splitlines())
            file_size_str = self._format_size(len(content.encode('utf-8')))
            verb = 'Created' if is_new_now else 'Overwrote'
            msg = f"OK: {verb} {path} ({line_count} lines, {file_size_str})"

            if justification:
                msg += f"\nSecurity Justification: {justification}"
            if approver_reason:
                msg += self._approver_reason_line(approver_reason)

            if backup_path_str:
                msg += f'\n  backup → {backup_path_str}'

            return msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Edit file (with nested helpers) ──────────────────────────────────

    def edit_file(self,
                  path: str,
                  agent_name: str,
                  old_content: str,
                  new_content: str,
                  match_mode: str = 'exact',
                  range_param: str = None,
                  occurrence: int = None,
                  justification: str = '') -> str:
        """Edit a file surgically — auto-approved for agent-owned files."""
        try:
            resolved = self._resolve_path(path, mode='rw')
        except Exception as e:
            return f"ERROR: {str(e)}"

        if not resolved.exists():
            return f"File not found for surgical edit: {path}"

        try:
            original_bytes = resolved.read_bytes()
        except Exception as e:
            return f"ERROR: Failed to read file: {str(e)}"
        try:
            raw_text = original_bytes.decode('utf-8')
        except UnicodeDecodeError:
            return f"ERROR: File has invalid UTF-8 encoding: {path}"
        # Preserve the file's own line endings. read_text()/write_text() translate every
        # ending to the OS native style, which silently rewrites the whole file on Windows
        # (one-line edit on an LF file → every line becomes CRLF). Match against a
        # normalized view and re-render in the file's style on write.
        newline_style = _detect_newline_style(raw_text)
        file_content = raw_text.replace('\r\n', '\n').replace('\r', '\n')
        actual_old_content = old_content
        match_ratio = 1.0
        # Initialize variables before match-mode dispatch (defensive scoping)
        exact_start_line = None
        exact_end_line = None
        indent_warnings = []  # Initialized to avoid NameError risk
        start_idx = None  # d&i mode range indices
        end_idx = None  # d&i mode range indices
        delta_width = 0  # Indent delta for heuristic feedback
        absorbed_tail = False  # BUG_0168: set when heuristic span is extended to absorb overlapping tail
        absorbed_suffix = ''  # BUG_0260: text after the replacement tail in an absorbed line
        count = 0  # exact mode occurrence count (read only in exact response paths)
        total_lines = 0  # d&i mode total line count (read only in d&i response paths)

        from agent_cascade.settings import DEFAULT_HEURISTIC_MATCH_THRESHOLD

        if match_mode == 'exact':
            count = file_content.count(old_content)
            _normalized_fallback = False
            if count == 0:
                # BUG_0186: tab/space normalization fallback — the caller may have
                # transcribed read_file output with spaces where the file uses tabs
                # (or vice versa). Normalize both sides (tab → 4 spaces) and retry.
                _TAB_W = 4
                norm_old = old_content.replace('\t', ' ' * _TAB_W)
                norm_file = file_content.replace('\t', ' ' * _TAB_W)
                norm_count = norm_file.count(norm_old)

                if norm_count == 0:
                    # BUG_0231: locate the nearest candidate region instead of
                    # comparing old_content line 1 against file line 1.
                    _hint = _describe_exact_miss(old_content, file_content, _TAB_W)
                    return (f"ERROR: Pattern not found in {path} (exact) — {_hint}. "
                            f"Try heuristic match_mode or include more context lines")

                # Normalized match found — extract the raw (tab-indented) file content
                # so the replacement operates on the file's actual bytes.
                _normalized_fallback = True
                count = norm_count
                norm_start = norm_file.index(norm_old)
                # Mid-line guard: if the match starts mid-line in the normalized file,
                # the line-snapping approach would over-capture (include the line's
                # prefix). Reject it and suggest a safer match mode.
                if norm_start > 0 and norm_file[norm_start - 1] != '\n':
                    return (f"ERROR: Pattern not found in {path} (exact) — the "
                            f"tab/space-normalized match starts mid-line, which is "
                            f"ambiguous. Try heuristic match_mode, or include the "
                            f"full line in old_content.")
                _sl = norm_file[:norm_start].count('\n')
                norm_end = norm_start + len(norm_old)
                # End-of-match guard: if the match ends mid-line in the normalized
                # file, the line-snapping reconstruction would over-capture the
                # line's suffix. Reject it. Skipped when norm_old ends with a
                # newline — the match ends at a line boundary by definition.
                if (not norm_old.endswith(('\n', '\r'))
                        and norm_end < len(norm_file)
                        and norm_file[norm_end] not in ('\n', '\r')):
                    return (f"ERROR: Pattern not found in {path} (exact) — the "
                            f"tab/space-normalized match ends mid-line, which is "
                            f"ambiguous. Try heuristic match_mode, or include the "
                            f"full line in old_content.")
                _el = (norm_file[:norm_end - 1].count('\n')
                       if norm_old.endswith(('\n', '\r'))
                       else norm_file[:norm_end].count('\n'))
                _raw_lines = file_content.splitlines(keepends=True)
                actual_old_content = ''.join(_raw_lines[_sl:_el + 1])
                # If old_content has no trailing newline, the line-snapping
                # reconstruction may include the file's trailing newline, which
                # would cause the replacement to glue to the next line.
                if not old_content.endswith(('\n', '\r')) and actual_old_content.endswith(('\n', '\r')):
                    actual_old_content = actual_old_content.rstrip('\r\n')
                match_start_pos = file_content.index(actual_old_content)
                match_end_pos = match_start_pos + len(actual_old_content)
                exact_start_line = _sl + 1
                exact_end_line = _el + 1
            else:
                # Raw match — existing position computation
                match_start_pos = file_content.index(old_content)
                match_end_pos = match_start_pos + len(actual_old_content)
                exact_start_line = file_content[:match_start_pos].count('\n') + 1
                # BUG FIX: Trailing newline is a terminator, not the start of a new line.
                # Count newlines up to just before the trailing newline if present.
                if actual_old_content and (actual_old_content[-1] == '\r' or actual_old_content[-1] == '\n'):
                    exact_end_line = file_content[:match_end_pos - 1].count('\n') + 1
                else:
                    exact_end_line = file_content[:match_end_pos].count('\n') + 1

            if occurrence is not None:
                # Coerce/validate the occurrence index (LLMs may send a string).
                # A 1-based index selects the Nth match; -1 is a sentinel meaning
                # "replace ALL occurrences" (BUG_0059).
                try:
                    occurrence = int(occurrence)
                except (ValueError, TypeError):
                    return (f"ERROR: 'occurrence' must be a positive integer or -1 "
                            f"(replace all), got {occurrence!r}")
                if occurrence != -1 and (occurrence < 1 or occurrence > count):
                    return (f"ERROR: occurrence={occurrence} out of range — pattern found "
                            f"{count} time(s) in {path} (valid range: 1-{count}, or -1 to replace all)")
            if count > 1 and occurrence is None:
                return f"ERROR: Pattern found {count} times in {path} — add more context lines to old_content to disambiguate"

            # BUG_0095: an identity replacement changes nothing. Returning here (before the
            # approval/backup/write section) keeps the agent from believing a fix landed when
            # nothing changed, and leaves no backup artifact behind.
            # (Skipped for the normalized fallback: the file has tabs, old_content has
            # spaces, so the replacement always changes bytes.)
            if not _normalized_fallback and new_content == old_content:
                return 'NO-OP: old_content and new_content are identical — nothing changed. No backup written.'

            # Advance to Nth occurrence (raw match only — the normalized fallback
            # already computed match_start_pos for the 1st match).
            if (not _normalized_fallback
                    and occurrence is not None and occurrence != -1):
                for _ in range(occurrence - 1):
                    match_start_pos = file_content.index(old_content, match_start_pos + len(old_content))
            elif _normalized_fallback and occurrence is not None and occurrence not in (1, -1):
                return (f"ERROR: occurrence={occurrence} is not supported with the "
                        f"tab/space-normalized fallback (only the 1st match is found). "
                        f"Use occurrence=1 or match_mode='heuristic'.")
        elif match_mode in ('heuristic', 'heuristic_agnostic'):
            file_lines = file_content.splitlines(keepends=True)

            # Map normalized (whitespace-stripped, non-blank) lines of the raw file
            file_line_info = []
            for idx, line in enumerate(file_lines):
                norm = ''.join(line.split())
                if norm:
                    file_line_info.append((idx, norm))

            old_line_info = []
            for line in old_content.splitlines(keepends=True):
                norm = ''.join(line.split())
                if norm:
                    old_line_info.append(norm)

            if not old_line_info:
                return "ERROR: The 'old_content' contains only whitespace. Heuristic match mode requires at least some non-whitespace content to match."

            file_line_map = {}
            for list_idx, (orig_idx, norm) in enumerate(file_line_info):
                if norm not in file_line_map:
                    file_line_map[norm] = []
                file_line_map[norm].append(list_idx)

            candidates = set()
            n_old_non_empty = len(old_line_info)
            n_file_non_empty = len(file_line_info)

            for old_idx, norm in enumerate(old_line_info):
                if norm and norm in file_line_map:
                    if len(file_line_map[norm]) <= 20:
                        for list_idx in file_line_map[norm]:
                            start_list_idx = list_idx - old_idx
                            if 0 <= start_list_idx <= n_file_non_empty - n_old_non_empty:
                                candidates.add(start_list_idx)

            if len(candidates) > 100:
                return f"ERROR: Heuristic match found {len(candidates)} candidates in {path} — add more unique context to narrow down"

            norm_old_joined = ''.join(old_line_info)
            threshold = DEFAULT_HEURISTIC_MATCH_THRESHOLD
            matches = []

            for start_list_idx in candidates:
                best_ratio = 0.0
                best_match_info = None

                for size in range(max(1, n_old_non_empty - 2),
                                  min(n_file_non_empty - start_list_idx + 1, n_old_non_empty + 3)):
                    candidate_slice = file_line_info[start_list_idx:start_list_idx + size]
                    candidate_norms = [item[1] for item in candidate_slice]
                    norm_candidate_joined = ''.join(candidate_norms)

                    ratio = difflib.SequenceMatcher(None, norm_old_joined, norm_candidate_joined).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match_info = {
                            'start_list_idx': start_list_idx,
                            'end_list_idx': start_list_idx + size,
                            'ratio': ratio
                        }

                if best_match_info and best_ratio >= threshold:
                    matches.append(best_match_info)

            if len(matches) == 0:
                return f"ERROR: Heuristic match not found in {path} (threshold={threshold:.0%}) — add more context or try exact mode"
            if len(matches) > 1:
                return f"ERROR: Heuristic match found {len(matches)} times in {path} above threshold — add more unique context to narrow down"

            unique_match = matches[0]
            orig_start_idx = file_line_info[unique_match['start_list_idx']][0]
            orig_end_idx = file_line_info[unique_match['end_list_idx'] - 1][0]

            # BUG_0168: If new_content's last non-blank line is a prefix of the next
            # non-blank file line (ending at a word boundary), extend the span to absorb
            # it. This prevents duplicate/residue lines when the replacement tail overlaps
            # the following line (e.g. new_content ends with "from 129 columns." and the
            # next file line is "from 129 columns. So micro 512 ...").
            #
            # Word-boundary guard: the prefix must end where the next line has a
            # non-identifier character (space, punctuation), so that distinct names that
            # merely share a prefix (e.g. "data_source_primary" vs
            # "data_source_primary_extended_field") are NOT absorbed.
            _pre_absorb_end = orig_end_idx  # Save for indent anchor (Phase 2 regression guard)
            _new_lines = new_content.splitlines(keepends=True)
            _last_new_line = next((l for l in reversed(_new_lines) if l.strip()), '')
            if _last_new_line:
                _last_stripped = _last_new_line.lstrip().rstrip('\r\n')
                _next_list_idx = unique_match['end_list_idx']
                if _next_list_idx < len(file_line_info) and len(_last_stripped) >= 5:
                    _next_orig_idx = file_line_info[_next_list_idx][0]
                    _next_stripped = file_lines[_next_orig_idx].lstrip()
                    if _next_stripped.startswith(_last_stripped):
                        _plen = len(_last_stripped)
                        _next_body = _next_stripped.rstrip('\r\n')
                        if _plen == len(_next_body):
                            # Exact match (modulo line ending) → absorb
                            orig_end_idx = _next_orig_idx
                            absorbed_tail = True
                        elif _plen < len(_next_body):
                            # Proper prefix: require word boundary at the junction
                            _junction_char = _next_stripped[_plen]
                            if not (_junction_char.isalnum() or _junction_char == '_'):
                                orig_end_idx = _next_orig_idx
                                absorbed_tail = True
                                # BUG_0260: the absorbed line continues past the
                                # replacement tail; that suffix is dropped, so keep it for
                                # the success message (visibility, not a behavior change).
                                absorbed_suffix = _next_body[_plen:]

            # Store line range for response construction (1-based)
            exact_start_line = orig_start_idx + 1
            exact_end_line = orig_end_idx + 1

            actual_old_content = ''.join(file_lines[orig_start_idx:orig_end_idx + 1])
            # BUG_0168: use pre-absorption content for the indent anchor so the absorbed
            # line (which may have different indentation) doesn't skew Phase 2 re-anchoring.
            _indent_anchor = (''.join(file_lines[orig_start_idx:_pre_absorb_end + 1])
                              if absorbed_tail else actual_old_content)
            match_ratio = unique_match['ratio']

            last_matched_line = file_lines[orig_end_idx]

            file_indent = _edit_get_leading_whitespace(_indent_anchor)
            old_indent = _edit_get_leading_whitespace(old_content)
            indent_delta = _edit_get_indent_width(file_indent) - _edit_get_indent_width(old_indent)
            # Store at method scope for response construction
            delta_width = indent_delta

            # Collect indent widths of non-blank lines in the matched block (used for base indent)
            file_block_lines = _indent_anchor.splitlines(keepends=True)
            file_indent_by_line = {}
            for idx, fl in enumerate(file_block_lines):
                norm = ''.join(fl.split())
                if norm:
                    leading_ws = fl[:len(fl) - len(fl.lstrip())] if fl.strip() else ''
                    file_indent_by_line[idx] = leading_ws

            # Phase 2 — Preservation: re-anchor new_content's RELATIVE indentation to the
            # file block's actual base indent. This preserves the nesting structure of
            # newly-added lines instead of flattening them to a neighbor's indent.
            new_content_lines = new_content.splitlines(keepends=True)

            _new_ws = [l[:len(l) - len(l.lstrip())] for l in new_content_lines if l.strip()]
            _file_ws = list(file_indent_by_line.values())
            new_base = min((_edit_get_indent_width(w) for w in _new_ws), default=0)
            file_base = min((_edit_get_indent_width(w) for w in _file_ws), default=0)
            indent_offset = file_base - new_base
            indent_char = _edit_detect_indent_char(file_indent)

            # BUG_0069 guard: a shallower-sibling append reproduces the matched block and then
            # appends a block whose base indent is shallower than the matched block's base. A
            # uniform re-anchor (offset>0) over-shifts both. Detect it via a relative-structure
            # mismatch and handle it by keeping the matched block at its real file indent and
            # appending the trailing lines verbatim.
            #
            # The sibling path slices new_content by the matched block's line count, so it is
            # only valid when new_content actually STARTS by reproducing that block. A plain
            # replacement that merely re-indents would otherwise be sliced to nothing and
            # silently discarded (or, with empty new_content, turn a deletion into a no-op).
            _new_rels  = sorted(_edit_get_indent_width(w) - new_base  for w in _new_ws)
            _file_rels = sorted(_edit_get_indent_width(w) - file_base for w in _file_ws)
            _sibling_append = (bool(new_content.strip())
                               and new_base < file_base
                               and _new_rels != _file_rels
                               and _reproduces_matched_block(new_content.splitlines(),
                                                             file_block_lines))

            if _sibling_append:
                # Keep the matched block at its real file indent and append the trailing
                # (sibling) lines exactly as the caller wrote them. If the matched block is the
                # file's last line with no trailing newline, actual_old_content lacks one, so
                # insert a separator to avoid gluing the sibling onto that line.
                _ncl = new_content.splitlines(keepends=True)
                _sep = '' if actual_old_content.endswith(('\n', '\r')) else '\n'
                # BUG_0168: when absorbed_tail is True, the absorbed line is already in
                # actual_old_content (retained at its original file position). Siblings
                # are the lines between the matched block and the absorbed line; they
                # are appended after the block. Exclude the absorbed line to avoid
                # duplication. Find it by identity (last non-blank line) so a trailing
                # blank line in new_content cannot re-introduce the duplicate.
                _block_lines = len(file_block_lines)
                if absorbed_tail:
                    _absorbed_pos = len(_ncl) - 1
                    while _absorbed_pos >= 0 and not _ncl[_absorbed_pos].strip():
                        _absorbed_pos -= 1
                    new_content = actual_old_content + _sep + ''.join(_ncl[_block_lines:_absorbed_pos])
                else:
                    new_content = actual_old_content + _sep + ''.join(_ncl[_block_lines:])
            else:
                adjusted_lines = []
                for line in new_content_lines:
                    own_ws = line[:len(line) - len(line.lstrip())]
                    base_width = _edit_get_indent_width(own_ws) + indent_offset
                    body = line.lstrip()
                    if not body:
                        # Blank/whitespace-only line: lstrip() strips the newline too (it is
                        # leading whitespace), which would drop the line. Recover it so a truly
                        # empty line stays empty instead of being flattened away. The slice
                        # keeps the original ending (\n or \r\n); 'or' covers lines with no ending.
                        body = line[len(own_ws.rstrip(' \t')):] or '\n'
                    adjusted_lines.append(_edit_render_indent(base_width, indent_char) + body)

                new_content = ''.join(adjusted_lines)
                # BUG_0186: preserve trailing lines when new_content is shorter than the
                # matched region. If the replacement covers fewer non-blank lines than
                # the matched block, shorten the deletion span so the uncovered trailing
                # lines (e.g. a function's parameter list) survive the edit.
                _n_new_nb = sum(1 for l in new_content.splitlines() if l.strip())
                _n_old_nb = sum(1 for l in file_lines[orig_start_idx:orig_end_idx + 1] if l.strip())
                if _n_new_nb < _n_old_nb:
                    _trailing_start = orig_end_idx + 1  # default: no trailing lines
                    _nb = 0
                    for _i in range(orig_start_idx, orig_end_idx + 1):
                        if file_lines[_i].strip():
                            _nb += 1
                            if _nb == _n_new_nb:
                                _trailing_start = _i + 1
                                break
                    if _trailing_start <= orig_end_idx:
                        # Deletion-aware guard: if every non-blank line in new_content
                        # is already present in the full matched region, the edit is an
                        # intentional deletion — don't resurrect the trailing lines.
                        # Compare whitespace-normalized content (file_lines use keepends,
                        # new_content.splitlines() does not).
                        _norm = lambda s: ''.join(s.split())
                        _matched_norm = {_norm(l) for l in file_lines[orig_start_idx:orig_end_idx + 1] if l.strip()}
                        _new_nb = [l for l in new_content.splitlines() if l.strip()]
                        if all(_norm(l) in _matched_norm for l in _new_nb):
                            # Intentional deletion — leave the trailing lines to be
                            # removed by the full-span replacement.
                            pass
                        else:
                            # Ensure new_content ends with a newline so it doesn't glue
                            # to the first preserved tail line.
                            if new_content and not new_content.endswith(('\n', '\r')):
                                _last_covered = file_lines[_trailing_start - 1]
                                if _last_covered.endswith('\r\n'):
                                    new_content += '\r\n'
                                elif _last_covered.endswith('\n'):
                                    new_content += '\n'
                                elif _last_covered.endswith('\r'):
                                    new_content += '\r'
                            # Shorten the deletion span — the trailing lines survive
                            # because they are excluded from actual_old_content.
                            actual_old_content = ''.join(file_lines[orig_start_idx:_trailing_start])
                            exact_end_line = _trailing_start
                            # BUG_0260: if the absorbed line is inside the surviving tail,
                            # the absorption was cancelled by this preservation path. Clear
                            # the flag so the message does not claim a line was absorbed.
                            if absorbed_tail and _trailing_start <= orig_end_idx:
                                absorbed_tail = False
                                absorbed_suffix = ''

            # Phase 3 — Validation: increment-based indentation anomaly detection
            indent_warnings = _edit_validate_indentation_consistency(new_content, path)

            # Track heuristic edit history per file
            resolved_path_key = resolved.as_posix()
            self._heuristic_edit_counts[resolved_path_key] = \
                self._heuristic_edit_counts.get(resolved_path_key, 0) + 1

            has_trailing_newline = last_matched_line.endswith('\n') or last_matched_line.endswith('\r')
            if has_trailing_newline:
                if new_content and not (new_content.endswith('\n') or new_content.endswith('\r')):
                    if last_matched_line.endswith('\r\n'):
                        ending = '\r\n'
                    elif last_matched_line.endswith('\n'):
                        ending = '\n'
                    else:
                        ending = '\r'
                    new_content = new_content + ending
        elif match_mode == 'delete_and_insert':
            # delete_and_insert mode: line-range-based surgery, delegated to a helper.
            # Advisory notes for this mode are emitted in _edit_build_response.
            err, start_idx, end_idx, new_file_content, total_lines = \
                self._edit_file_delete_and_insert(
                    file_content=file_content,
                    range_param=range_param,
                    new_content=new_content,
                )
            if err is not None:
                return err

        else:
            return f"ERROR: Invalid match_mode '{match_mode}'."

        # Reuse file_content from the first read — no need to re-read.
        # Compute the post-edit content for every mode BEFORE approval/backup. This is pure
        # string work, so the shared no-op guard below can return without prompting the user
        # or leaving a backup artifact.
        if match_mode == 'delete_and_insert':
            # new_file_content was already computed above — skip string replace
            pass
        elif match_mode == 'exact' and occurrence == -1:
            # BUG_0059: occurrence=-1 replaces ALL (non-overlapping) occurrences,
            # matching str.count()/str.replace() semantics.
            new_file_content = file_content.replace(actual_old_content, new_content)
        elif match_mode == 'exact' and occurrence is not None:
            # Replace only the Nth match — match_start_pos points at its start (computed
            # in the exact branch above). The default path below stays byte-identical.
            new_file_content = (file_content[:match_start_pos]
                                + new_content
                                + file_content[match_start_pos + len(actual_old_content):])
        else:
            new_file_content = file_content.replace(actual_old_content, new_content, 1)

        # BUG_0095: a range that deletes N lines and re-inserts the same N lines (or a
        # replacement that re-renders old_content identically) is byte-identical to the
        # original. Reporting OK with a +0 diff misleads the agent into thinking a fix
        # landed. One guard for every mode — no write, no backup.
        if new_file_content == file_content:
            return (f"WARNING: No-op edit — the result is byte-identical to the current file "
                    f"({path}); nothing was written and no backup created. A range that deletes "
                    f"lines and re-inserts identical lines, or a replacement that renders "
                    f"old_content unchanged, changes nothing. To adjust blank lines use "
                    f"match_mode='exact' and include surrounding non-blank context in old_content.")

        description = f"Surgical edit to: {path} (mode: {match_mode})"
        if match_mode == 'delete_and_insert':
            tool_args = {'path': path, 'range': range_param, 'new_content': new_content, 'match_mode': match_mode}
        else:
            tool_args = {'path': path, 'old_content': old_content, 'new_content': new_content, 'match_mode': match_mode}
        tool_args['justification'] = justification

        if not self._is_auto_approved(path, agent_name):
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='edit_file',
                tool_args=tool_args,
                description=description,
            )
            if not approved:
                return f"REJECTED: {reason}"
            approver_reason = reason  # BUG_0039: keep provenance — do not overwrite the caller's justification
        else:
            approver_reason = ''

        # Memory-vault write screen (OWASP LLM01, persistent injection vector).
        # edit_file assembles its result in memory (new_file_content), so the screen
        # runs here — before the backup/write below — to leave no orphan .bak on
        # rejection. Uses the already-resolved destination (not the raw caller path)
        # so a relative or traversal spelling cannot dodge the vault-marker check.
        _rejection = _screen_vault_write(resolved.as_posix(), new_file_content)
        if _rejection:
            from agent_cascade.log import logger
            logger.warning('[MEMORY_SCREEN] Rejected edit_file write to %s: %s', path, _rejection)
            return (f"ERROR: Edit to '{path}' rejected by memory-vault injection screen "
                    f"({_rejection}). The edited content matches prompt-injection patterns. "
                    f"Remove the offending content and retry.")

        try:
            resolved = self._resolve_path(path, mode='rw')

            # The commit region (re-read → guard → backup → write → own) is serialized by a
            # process-global mutex so two agent threads editing the same file cannot race
            # through it. The lock is acquired here, AFTER the re-resolve, so the guard's
            # re-read and the write below operate on the same resolved path. The match
            # computation above and the response construction below stay outside the lock.
            with file_mutation.mutate():
                # Concurrent-modification guard: approval can take an arbitrary amount of time,
                # and another process/agent may have written the file since our read. Writing the
                # stale result would silently clobber that work. Compare raw bytes (cheap relative
                # to the edit) and abort without a backup or write if anything changed. Under the
                # lock this also sees writes from a sibling thread that finished after our read.
                try:
                    current_bytes = resolved.read_bytes()
                except Exception as e:
                    return f"ERROR: Failed to re-read file before writing: {str(e)}"
                if current_bytes != original_bytes:
                    return (f"ERROR: {path} changed on disk between read and write (concurrent "
                            f"modification). The edit was NOT applied — re-read the file and retry "
                            f"with the current content.")

                safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
                backup_dir = self.base_dir / 'logs' / 'backups' / safe_agent
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_path = backup_dir / f"{resolved.name}.{int(time.time())}.bak"
                shutil.copy2(resolved, backup_path)
                backup_path_str = self._backup_path_str(backup_path)

                # BUG_0250: advisory-only check for an adjacent duplicate line that this edit
                # newly introduced (anchor text repeated in new_content, or a boundary that
                # left the anchor behind). Computed before the write; the bytes are untouched.
                duplicate_note = _adjacent_duplicate_note(file_content, new_file_content, match_mode)

                # Generate unified diff before writing (both old and new content available)
                # keepends=False: avoids double-newlines when joined with '\n' below
                old_lines = file_content.splitlines()
                new_lines = new_file_content.splitlines()
                diff_lines = list(
                    difflib.unified_diff(old_lines, new_lines, fromfile=f'a/{path}', tofile=f'b/{path}', lineterm=''))
                # Skip --- and +++ headers (first 2), keep @@ headers and content
                diff_content = '\n'.join(diff_lines[2:]) if len(diff_lines) > 2 else ''
                # Limit to 20 lines max; truncate with ellipsis if longer
                if diff_content:
                    all_diff_lines = diff_content.splitlines()
                    if len(all_diff_lines) > 20:
                        first_lines = all_diff_lines[:8]
                        last_lines = all_diff_lines[-8:]
                        diff_content = '\n'.join(first_lines + ['...'] + last_lines)

                resolved.write_bytes(_render_with_newlines(new_file_content, newline_style))
                self._own(resolved, agent_name)

            # Response construction delegated to _edit_build_response. Any
            # exception it raises is caught by this try, as before extraction.
            return self._edit_build_response(
                path=path, match_mode=match_mode, resolved=resolved,
                file_content=file_content, actual_old_content=actual_old_content,
                new_content=new_content, occurrence=occurrence, count=count,
                start_idx=start_idx, end_idx=end_idx, total_lines=total_lines,
                exact_start_line=exact_start_line, exact_end_line=exact_end_line,
                match_ratio=match_ratio, delta_width=delta_width,
                absorbed_tail=absorbed_tail, absorbed_suffix=absorbed_suffix,
                indent_warnings=indent_warnings, duplicate_note=duplicate_note,
                diff_content=diff_content, backup_path_str=backup_path_str,
                justification=justification, approver_reason=approver_reason,
            )
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    def _edit_file_delete_and_insert(self, *, file_content, range_param,
                                         new_content) -> tuple:
        """delete_and_insert mode: line-range-based surgery.

        Returns (error, start_idx, end_idx, new_file_content, total_lines).
        When error is not None the caller must return it verbatim; the other
        fields are then meaningless. Pure string work - no I/O.
        """
        # ── delete_and_insert mode: line-range-based surgery ─────────────
        file_lines = file_content.splitlines(keepends=True)
        total_lines = len(file_lines)

        # int 0 is a valid "append at EOF" spec; only None/blank-string mean "missing".
        if range_param is None or (isinstance(range_param, str) and not range_param.strip()):
            return ("ERROR: delete_and_insert mode requires the 'range' parameter (e.g., '5:10').", None, None, None, total_lines)

        # On empty files, any valid range that resolves to inserting at position 0 is fine;
        # _edit_parse_range clamps out-of-range values so this will work naturally.

        try:
            start_idx, end_idx = _edit_parse_range(range_param, total_lines)
        except (ValueError, IndexError) as e:
            return (f"ERROR: Invalid range '{range_param}' for delete_and_insert mode: {str(e)}", None, None, None, total_lines)

        # BUG_0109: a range that spans the entire file (top through bottom — ':' , 'N:',
        # '1:total') plus non-empty new_content means "delete everything and rewrite". That
        # is almost always an unintended append; treat it as an error-by-design and point to
        # the right tool/args. A genuine whole-file rewrite belongs in write_file.
        if total_lines and start_idx == 0 and end_idx >= total_lines and new_content:
            return (f"ERROR: range '{range_param}' spans the entire file ({total_lines} line(s)) "
                    f"and new_content is non-empty — this would delete and rewrite the whole file. "
                    f"For a whole-file replace use write_file; to append use range '0' (or a "
                    f"line number past the end), or match_mode='exact' on the last lines.",
                    None, None, None, total_lines)

        # Split file into before/deleted/after sections
        before = file_lines[:start_idx]
        after = file_lines[end_idx:]  # end_idx is exclusive (Python slice semantics)

        if new_content:
            inserted = new_content.splitlines(keepends=True)
            # Insert new_content VERBATIM — the caller owns the block's indentation.
            # No indent is inferred from the replaced line: a mixed rule (unindented
            # lines inherit, indented lines stay absolute) silently corrupts naturally
            # typed nested blocks, and relative vs absolute indentation cannot be
            # distinguished unambiguously from the text alone.
            # Preserve line ending style from surrounding context for ALL inserted lines
            # (runs AFTER the indent step; only rstrips '\r\n' and appends, so it
            # never disturbs the leading whitespace set above)
            if after:
                ref_ending = _edit_detect_line_ending(after[0])
                if not ref_ending and before:
                    ref_ending = _edit_detect_line_ending(before[-1])
                if ref_ending:
                    for i in range(len(inserted)):
                        # Normalize each line's ending to match file style
                        inserted[i] = inserted[i].rstrip('\r\n') + ref_ending
            elif before:
                # BUG_0220: append-at-EOF (after is empty) — the `if after:` branch above
                # never runs, so a pure append used to glue new_content onto the file's
                # last line when that line has no trailing newline. file_content is read
                # with newlines normalized to '\n'; emit a separating '\n' only when the
                # tail is unterminated (a terminated tail must not gain a blank line).
                # The write re-renders in the file's own ending style, and the
                # inserted lines stay verbatim (no forced trailing NL), matching the
                # pre-existing append behavior.
                if _edit_detect_line_ending(before[-1]) == '':
                    before[-1] = before[-1] + '\n'
            file_lines = before + inserted + after
        else:
            # Delete-only: no insertion
            file_lines = before + after

        new_file_content = ''.join(file_lines)
        return (None, start_idx, end_idx, new_file_content, total_lines)

    def _edit_build_response(self, *, path, match_mode, resolved, file_content,
                             actual_old_content, new_content, occurrence, count,
                             start_idx, end_idx, total_lines, exact_start_line,
                             exact_end_line, match_ratio, delta_width,
                             absorbed_tail, absorbed_suffix, indent_warnings,
                             duplicate_note, diff_content, backup_path_str,
                             justification, approver_reason) -> str:
        """Build the success message for a completed edit.

        Called from inside edit_file's post-write try, so any exception it
        raises surfaces as edit_file's 'Approved but execution failed' branch
        exactly as before extraction. The d&i advisory-note family
        (BUG_0060/0175/0213/0224/0250) is emitted here.
        """
        # Line delta computation using splitlines()
        if match_mode == 'delete_and_insert':
            # BUG FIX: In d&i mode, actual_old_content is the range string (e.g. "3:7"),
            # not the deleted file content. Compute old_lc from the actual range.
            # BUG_0109: end_idx is an unclamped slice index (':' yields total+1), so
            # end_idx - start_idx over-reports by one on ranges with an empty end. Clamp
            # to the real number of lines removed.
            old_lc = min(end_idx, total_lines) - start_idx
        else:
            old_lc = len(actual_old_content.splitlines()) if actual_old_content else 0
        new_lc = len(new_content.splitlines()) if new_content else 0
        net_delta = new_lc - old_lc
        sign = '+' if net_delta >= 0 else ''

        res_msg = f"OK: Edited {path}"

        if match_mode == 'exact':
            # Exact mode: line range + delta
            if occurrence == -1:
                total_net = net_delta * count
                total_sign = '+' if total_net >= 0 else ''
                res_msg += (f" (exact, replaced all {count} occurrence(s), first at lines "
                           f"{exact_start_line}-{exact_end_line}, net {total_sign}{total_net})")
            else:
                res_msg += f" lines {exact_start_line}-{exact_end_line} (exact, -{old_lc} +{new_lc} = {sign}{net_delta}net)"

        elif match_mode in ('heuristic', 'heuristic_agnostic'):
            # Discriminated labels for heuristic vs heuristic_agnostic
            mode_label = 'heuristic' if match_mode == 'heuristic' else 'heur_ag'
            resolved_path_str = resolved.as_posix()

            res_msg += f" lines {exact_start_line}-{exact_end_line} ({mode_label} {match_ratio:.0%}, -{old_lc} +{new_lc} = {sign}{net_delta}net)"
            if absorbed_tail:
                res_msg += ' [absorbed overlapping tail line to prevent duplicate]'
                if absorbed_suffix:
                    _suffix_snip = absorbed_suffix.strip()
                    if len(_suffix_snip) > 60:
                        _suffix_snip = _suffix_snip[:60] + '…'
                    res_msg += (f'\n  note: the absorbed line had additional text after '
                                f'the replaced tail ("{_suffix_snip}") that this edit '
                                f'dropped — re-read the file if you did not intend to '
                                f'replace the whole line.')

            # Indent adjustment info (only if non-zero delta detected in heuristic mode)
            if match_mode == 'heuristic':
                if delta_width != 0:
                    res_msg += f", indent adjusted"
                if indent_warnings:
                    for w in indent_warnings:
                        res_msg += f"\n  {w}"
                # Heuristic edit count warning
                edit_count = self._heuristic_edit_counts.get(resolved_path_str, 0)
                if edit_count >= 3:
                    res_msg += f" [edit #{edit_count} this session]"

        elif match_mode == 'delete_and_insert':
            d_start = start_idx + 1  # 0-based to 1-based
            # Clamp to the real last line so the shown range matches the corrected
            # deleted count (':' / 'N:' yield end_idx = total+1).
            d_end = min(end_idx, total_lines)
            if old_lc == 0:
                # Insert-only variant. Also covers ranges that reach past EOF (e.g.
                # '4:' on a 3-line file), which delete nothing and used to print a
                # nonsensical "lines 4-3".
                res_msg += f" line {d_start} (d&i, INSERT-ONLY, -0 +{new_lc} = {sign}{net_delta}net)"
            elif new_content:
                # Normal delete+insert
                res_msg += f" lines {d_start}-{d_end} (d&i, deleted {old_lc} lines, inserted at line {d_start}, -{old_lc} +{new_lc} = {sign}{net_delta}net)"
                # BUG_0060: echo the deleted first/last ~80 chars so range drift is visible
                # without reading the diff. If a prior edit in the same parallel block
                # shifted the lines, the deleted content will not match what the caller
                # described — the echo makes that obvious. Read from the ORIGINAL
                # file_content (pre-edit); file_lines was rebound to the post-edit list
                # earlier, so do NOT use it here.
                _orig = file_content.splitlines()
                _d_first = _orig[start_idx].rstrip('\r\n') if start_idx < len(_orig) else ''
                _d_last_i = d_end - 1  # d_end is already clamped to total_lines
                _d_last = _orig[_d_last_i].rstrip('\r\n') if 0 <= _d_last_i < len(_orig) else ''

                def _snip(s):
                    s = s.strip()
                    return (s[:80] + '…') if len(s) > 80 else (s if s else '(blank)')

                if start_idx == _d_last_i:
                    res_msg += f'\n  deleted: "{_snip(_d_first)}"'
                else:
                    res_msg += (f'\n  deleted first line: "{_snip(_d_first)}"\n'
                                f'  deleted last line:  "{_snip(_d_last)}"')
                # BUG_0175: a suffix range 'N:' (N>1) parses to end_idx=total+1, so the
                # range deletes through EOF and the success message above reports only line
                # arithmetic — the tail deletion is silent. The whole-file guard (BUG_0109)
                # only fires for start_idx==0, so this case slips past it. Make the message
                # honest about the deleted tail; the edit itself is unchanged.
                if end_idx >= total_lines and start_idx > 0 and old_lc > 0:
                    # Read the last-deleted line from the ORIGINAL file_content (file_lines
                    # was rebound to the post-edit list at :1772/:1775). d_end is already
                    # clamped to the real last line, so d_end-1 is the last deleted index.
                    last_deleted = file_content.splitlines()[d_end - 1].rstrip('\r\n')
                    snippet = last_deleted[:60] + ('...' if len(last_deleted) > 60 else '')
                    res_msg += (f"\n  note: this also deleted the file's trailing lines "
                               f"{d_start}-{d_end} (last deleted line: \"{snippet}\"); "
                               f"to append without deleting use range '0' (or a line number "
                               f"past the end), or match_mode='exact' on the last lines.")
            else:
                # Delete-only variant. BUG_0096: label it explicitly — a bare "-K +0" reads
                # like a replacement that lost its text, which is exactly the confusion this
                # path used to cause (a replacement passed under the wrong kwarg name).
                res_msg += f" lines {d_start}-{d_end} (d&i, DELETE-ONLY, -{old_lc} +0 = {sign}{net_delta}net)"

            # BUG_0213: the range may end mid-block, leaving the block's tail on the
            # surviving side. The preview above echoes only the deleted lines, so also
            # surface the first surviving line and say when it reads like residue.
            # Only meaningful while something survives (d_end == total_lines is the
            # suffix-to-EOF case already covered by the BUG_0175 note above) and when
            # the edit actually deleted lines (insert-only has nothing to orphan).
            _orig_msg_lines = file_content.splitlines()
            if old_lc > 0 and d_end < len(_orig_msg_lines):
                _next_line = _orig_msg_lines[d_end]
                _residue_reason = _orphan_residue_reason(
                    _orig_msg_lines[d_end - 1], _next_line)
                if _residue_reason:
                    _next_snip = _next_line.strip()
                    if len(_next_snip) > 60:
                        _next_snip = _next_snip[:60] + '…'
                    res_msg += (
                        f'\n  note: line {d_end + 1} after the range '
                        f'{_residue_reason}: "{_next_snip}" — if that line belongs to '
                        f'the replaced block, re-read and extend the range to cover it '
                        f'(or use match_mode=\'exact\').')

            # BUG_0224: the opposite direction of the residue check — the range may
            # START on a structural opener (heading / fence / def-class / table
            # separator) that the replacement does not re-supply, silently leaving a
            # section without its heading. Advisory only: re-heading a section on
            # purpose is valid, so never reject the edit.
            if old_lc > 0 and start_idx < len(_orig_msg_lines):
                _first_deleted = _orig_msg_lines[start_idx].rstrip('\r\n')
                _opener_kind = _structural_opener_kind(_first_deleted)
                if _opener_kind and not _new_content_restores_opener(new_content, _opener_kind):
                    _fd_snip = _first_deleted.strip()
                    if len(_fd_snip) > 60:
                        _fd_snip = _fd_snip[:60] + '…'
                    res_msg += (
                        f'\n  note: range started on a heading/structural opener '
                        f'("{_fd_snip}") that the replacement does not restore — '
                        f'the section may now be missing its heading.')

        if duplicate_note:
            res_msg += f'\n  {duplicate_note}'

        if justification:
            res_msg += f"\nSecurity Justification: {justification}"
        if approver_reason:
            res_msg += self._approver_reason_line(approver_reason)

        # Unified diff snippet in code block (no redundant ---/+++ headers, both point to same file)
        if diff_content:
            res_msg += f'\n```\n{diff_content}\n```'

        if backup_path_str:
            res_msg += f'\n  backup → {backup_path_str}'

        return res_msg

    # ─── Re-indent ────────────────────────────────────────────────────────

    def re_indent(self,
                  path: str,
                  agent_name: str,
                  lines: str,
                  indent: int,
                  indent_type: str,
                  mode: str = 'min',
                  justification: str = '') -> str:
        """Re-indents a block of code in a file."""
        try:
            resolved = self._resolve_path(path, mode='rw')
        except Exception as e:
            return f"ERROR: {str(e)}"

        if not resolved.exists():
            return f"File not found for re-indentation: {path}"

        try:
            file_content = resolved.read_text(encoding='utf-8')
        except Exception as e:
            return f"ERROR: Failed to read file: {str(e)}"

        file_lines = file_content.splitlines(keepends=True)
        total_lines = len(file_lines)

        try:
            parts = lines.split(':')
            if len(parts) != 2:
                raise ValueError("lines must be in 'start:end' format")
            start_str, end_str = parts
            start = int(start_str) if start_str.strip() else 1
            end = int(end_str) if end_str.strip() else total_lines

            if start < 1:
                return f"ERROR: Line numbers must be >= 1. Got start={start}. Use 1-based format like '1:10'."
            if end < 1:
                return f"ERROR: Line numbers must be >= 1. Got end={end}. Use 1-based format like '1:10'."

            # Check bounds before clamping — give clear error messages
            if start > total_lines:
                return f"ERROR: start_line {start} exceeds file length ({total_lines} lines). Use a line number between 1 and {total_lines}."
            if end > total_lines:
                return f"ERROR: end_line {end} exceeds file length ({total_lines} lines). Use a line number between 1 and {total_lines}."

            # Convert to 0-based indexing
            start = start - 1

            # Clamp to valid range (handles edge cases where values equal total_lines)
            start = max(0, min(start, total_lines))
            end = max(0, min(end, total_lines))

            if start >= end:
                return f"ERROR: Invalid lines range: {lines}. Start must be less than end."
        except Exception as e:
            return f"ERROR: Invalid lines format '{lines}': {str(e)}. Use 1-based line range (e.g., '1:10')."

        block_lines = file_lines[start:end]

        def count_leading_ws(line: str) -> int:
            return len(line) - len(line.lstrip(' \t'))

        def count_visual_columns(line: str, tab_width: int) -> int:
            count = 0
            for ch in line:
                if ch == '\t':
                    count += tab_width
                elif ch == ' ':
                    count += 1
                else:
                    break
            return count

        ws_info_list = []
        ws_info_visual = []

        for line in block_lines:
            if not line.strip():
                ws_info_list.append(None)
                ws_info_visual.append(None)
            else:
                ws_count = count_leading_ws(line)
                visual_col = count_visual_columns(line, indent)
                stripped = line.lstrip(' \t')
                ws_info_list.append((ws_count, stripped))
                ws_info_visual.append((visual_col, stripped))

        ws_counts = [info[0] for info in ws_info_list if info is not None]
        min_visual_col = min((info[0] for info in ws_info_visual if info is not None), default=0)

        if not ws_counts:
            new_block_lines = block_lines
            base_trim = 0
        else:
            # --- Mode dispatch: determine base_trim and validate indent ---
            if mode == 'min':
                base_trim = min(ws_counts)
            elif mode == 'shift':
                base_trim = None  # shift mode is self-contained below
            elif mode == 'flat':
                base_trim = None
            elif mode == 'convert':
                base_trim = min_visual_col
            else:
                return f"ERROR: Invalid mode '{mode}'. Choose 'shift' (add/remove), 'min' (default, trim-to-min then apply target), 'flat', or 'convert'."

            # Negative indent is only allowed in shift mode
            if mode != 'shift' and indent < 0:
                return f"ERROR: indent must be non-negative in {mode} mode, got {indent}."

            if mode == 'convert' and indent_type == 'tab' and indent == 0:
                return "ERROR: indent must be > 0 when indent_type='tab' in convert mode (used as tab width)."

            # --- shift mode: self-contained logic, no base_trim ---
            if mode == 'shift':
                if indent > 0:
                    # Prepend N characters of the specified type to each non-blank line's existing whitespace
                    prefix = ('\t' * indent) if indent_type == 'tab' else (' ' * indent)
                    new_block_lines = []
                    for i, info in enumerate(ws_info_list):
                        if info is None:
                            new_block_lines.append(block_lines[i])  # blank lines pass through unchanged
                            continue
                        ws_count, stripped_content = info
                        actual_ws = block_lines[i][:ws_count]
                        new_block_lines.append(prefix + actual_ws + stripped_content)  # prepend to existing ws

                elif indent < 0:
                    # Strip up to N leading whitespace characters from each non-blank line (type-agnostic removal)
                    remove_n = abs(indent)
                    new_block_lines = []
                    for i, info in enumerate(ws_info_list):
                        if info is None:
                            new_block_lines.append(block_lines[i])  # blank lines pass through unchanged
                            continue
                        ws_count, stripped_content = info
                        actual_ws = block_lines[i][:ws_count]
                        chars_to_remove = min(remove_n, len(actual_ws))
                        remaining_ws = actual_ws[chars_to_remove:]
                        new_block_lines.append(remaining_ws + stripped_content)

                else:  # indent == 0
                    new_block_lines = list(block_lines)  # no-op, copy as-is
            else:
                # --- min/flat/convert modes: shared processing loop ---
                new_block_lines = []
                for i, info in enumerate(ws_info_list):
                    if info is None:
                        original_line = block_lines[i]
                        suffix = ''
                        if original_line.endswith('\r\n'):
                            suffix = '\r\n'
                        elif original_line.endswith('\n'):
                            suffix = '\n'
                        elif original_line.endswith('\r'):
                            suffix = '\r'
                        new_block_lines.append(suffix)
                        continue

                    ws_count, stripped_content = info

                    if mode == 'flat':
                        relative_offset = 0
                    elif mode == 'convert':
                        visual_col, _ = ws_info_visual[i]
                        relative_offset = visual_col - base_trim
                        total_visual_columns = indent + relative_offset

                        if indent_type == 'tab':
                            num_tabs = total_visual_columns // indent
                            remainder_spaces = total_visual_columns % indent
                            new_ws = '\t' * num_tabs + ' ' * remainder_spaces
                        else:
                            new_ws = ' ' * total_visual_columns

                        new_block_lines.append(new_ws + stripped_content)
                        continue
                    else:  # mode == 'min'
                        relative_offset = ws_count - base_trim

                    total_indent_units = indent + relative_offset

                    if indent_type == 'tab':
                        new_ws = '\t' * total_indent_units
                    else:
                        new_ws = ' ' * total_indent_units

                    new_block_lines.append(new_ws + stripped_content)

        # Track original whitespace unit for feedback message
        if ws_counts:
            first_non_blank_line = None
            for line in block_lines:
                if line.strip():
                    first_non_blank_line = line
                    break
            if first_non_blank_line:
                first_ws_len = len(first_non_blank_line) - len(first_non_blank_line.lstrip(' \t'))
                leading_ws = first_non_blank_line[:first_ws_len]
                original_ws_unit = 'tab' if '\t' in leading_ws else 'space'
            else:
                original_ws_unit = 'unknown'
        else:
            original_ws_unit = 'unknown'

        description = f"Re-indent block in: {path} (lines: {lines}, indent: {indent}, type: {indent_type}, mode: {mode})"
        tool_args = {
            'path': path,
            'lines': lines,
            'indent': indent,
            'indent_type': indent_type,
            'mode': mode,
            'justification': justification
        }

        if not self._is_auto_approved(path, agent_name):
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='re_indent',
                tool_args=tool_args,
                description=description,
            )
            if not approved:
                return f"REJECTED: {reason}"
            approver_reason = reason  # BUG_0039: keep provenance — do not overwrite the caller's justification
        else:
            approver_reason = ''

        try:
            resolved = self._resolve_path(path, mode='rw')

            # Re-read file after approval to avoid stale-content race condition
            # (enhances safety vs pre-approval read — another agent/tool may have modified the file while waiting for approval)
            try:
                file_content = resolved.read_text(encoding='utf-8')
            except Exception as e:
                return f"ERROR: Failed to re-read file after approval: {str(e)}"
            file_lines = file_content.splitlines(keepends=True)

            safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
            backup_dir = self.base_dir / 'logs' / 'backups' / safe_agent
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{resolved.name}.{int(time.time())}.bak"
            shutil.copy2(resolved, backup_path)
            backup_path_str = self._backup_path_str(backup_path)

            new_file_lines = list(file_lines)
            new_file_lines[start:end] = new_block_lines
            new_content_val = ''.join(new_file_lines)

            # Generate unified diff before writing (both old and new content available)
            import difflib
            diff_content = ''
            uniform_shift_info = None  # For sanity warning later

            if ws_counts:
                changed_count = sum(1 for i in range(len(block_lines)) if block_lines[i] != new_block_lines[i])
                if changed_count > 0:
                    old_lines = file_content.splitlines()
                    new_lines = new_content_val.splitlines()
                    diff_lines = list(
                        difflib.unified_diff(old_lines,
                                             new_lines,
                                             fromfile=f'a/{path}',
                                             tofile=f'b/{path}',
                                             lineterm=''))

                    # Check if all changes are uniform whitespace shifts (same content, different leading ws)
                    # Uses keepends=True to match block_lines/new_block_lines representation used in changed_count
                    block_old = file_content.splitlines(keepends=True)[start:end]
                    block_new = new_content_val.splitlines(keepends=True)[start:end]
                    is_uniform_shift = True
                    shift_amount = None
                    for ol, nl in zip(block_old, block_new):
                        if ol == nl:
                            continue  # unchanged line (e.g., blank)
                        if ol.lstrip(' \t') != nl.lstrip(' \t'):
                            is_uniform_shift = False
                            break
                        new_ws = len(nl) - len(nl.lstrip(' \t'))
                        old_ws = len(ol) - len(ol.lstrip(' \t'))
                        diff_amt = new_ws - old_ws
                        if shift_amount is None:
                            shift_amount = diff_amt
                        elif diff_amt != shift_amount:
                            is_uniform_shift = False
                            break

                    if is_uniform_shift and changed_count > 0:
                        uniform_shift_info = (changed_count, shift_amount)

                    # Skip --- and +++ headers (first 2), keep @@ headers and content
                    diff_content = '\n'.join(diff_lines[2:]) if len(diff_lines) > 2 else ''

                    # For uniform whitespace shifts: use compact summary instead of misleading truncated diff
                    if is_uniform_shift and changed_count > 0:
                        sign = '+' if shift_amount >= 0 else '-'
                        diff_content = f"(uniform indent shift: {changed_count} lines: {sign}{abs(shift_amount)}sp each)"
                    # For non-uniform diffs with many lines: truncate per-hunk to preserve @@ headers
                    elif diff_content:
                        all_diff_lines = diff_content.splitlines()
                        if len(all_diff_lines) > 20:
                            # Split into hunks (each starting with @@), truncate each independently
                            hunks = []
                            current_hunk = []
                            for line in all_diff_lines:
                                if line.startswith('@@'):
                                    if current_hunk:
                                        hunks.append(current_hunk)
                                    current_hunk = [line]
                                else:
                                    current_hunk.append(line)
                            if current_hunk:
                                hunks.append(current_hunk)

                            truncated_hunks = []
                            for hunk in hunks:
                                if len(hunk) <= 12:
                                    truncated_hunks.append(hunk)
                                else:
                                    # Keep header + up to 5 content lines from start, ellipsis, last 4 from end
                                    header = [hunk[0]]
                                    body = hunk[1:]
                                    truncated_hunks.append(header + body[:5] + ['...'] + body[-4:])

                            diff_content = '\n'.join(line for hunk in truncated_hunks for line in hunk)
            else:
                changed_count = 0

            resolved.write_text(new_content_val, encoding='utf-8')

            self._own(resolved, agent_name)

            display_start = start + 1
            total_in_range = end - start  # lines in the requested range
            indent_type_label = 'sp' if indent_type == 'space' else 'tabs'

            if not ws_counts:
                # No-op: blank-only block
                res_msg = f"OK: Re-indented {path} lines {display_start}-{end} ({mode}, no-op: {total_in_range} total / 0 changed)"
            elif mode == 'shift':
                if indent > 0:
                    res_msg = f"OK: Re-indented {path} lines {display_start}-{end} (shift +{indent}{indent_type_label}, {total_in_range} total / {changed_count} changed)"
                elif indent < 0:
                    res_msg = f"OK: Re-indented {path} lines {display_start}-{end} (shift -{abs(indent)}{indent_type_label}, {total_in_range} total / {changed_count} changed)"
                else:
                    res_msg = f"OK: Re-indented {path} lines {display_start}-{end} (shift 0, {total_in_range} total / {changed_count} changed)"
            elif mode == 'min':
                base_unit_label = 'sp' if original_ws_unit == 'space' else 'tabs'
                res_msg = f"OK: Re-indented {path} lines {display_start}-{end} ({mode}, base {base_trim}{base_unit_label}→{indent}{indent_type_label}, {total_in_range} total / {changed_count} changed)"
            elif mode == 'flat':
                res_msg = f"OK: Re-indented {path} lines {display_start}-{end} (flat→{indent}{indent_type_label}, {total_in_range} total / {changed_count} changed)"
            elif mode == 'convert':
                res_msg = f"OK: Re-indented {path} lines {display_start}-{end} (convert, tab-width={indent}→{indent_type_label}, {total_in_range} total / {changed_count} changed)"
            else:
                res_msg = f"OK: Re-indented {path} lines {display_start}-{end} ({mode}, {total_in_range} total / {changed_count} changed)"

            if justification:
                res_msg += f"\nSecurity Justification: {justification}"
            if approver_reason:
                res_msg += self._approver_reason_line(approver_reason)

            # Sanity warning for large uniform indent shifts (high changed_count relative to range)
            if uniform_shift_info and total_in_range > 0:
                _, shift_amt = uniform_shift_info
                ratio = changed_count / total_in_range
                if ratio > 0.7:
                    res_msg += f"\n(uniform indent shift — confirm block boundaries are intended)"

            # Unified diff snippet in code block
            if diff_content:
                res_msg += f'\n```\n{diff_content}\n```'

            if backup_path_str:
                res_msg += f'\n  backup → {backup_path_str}'

            return res_msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Delete file ──────────────────────────────────────────────────────

    def delete_file(
        self,
        path: Optional[str],
        agent_name: str,
        paths: Optional[List[str]] = None,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        min_size: Optional[str] = None,
        max_size: Optional[str] = None,
        modified_after: Optional[str] = None,
        modified_before: Optional[str] = None,
        justification: str = '',
    ) -> str:
        """Delete one or more files/directories.

        Accepts a single ``path`` (backward-compatible) and/or a ``paths`` list for
        bulk deletion. Filters (``include``/``exclude``/size/date) are applied within
        the resolved base directory of ``path``, mirroring ``list_dir`` semantics.

        Flow: symlink-check(s) → resolve/expand targets → empty check (B6) → approval
        (B3, single aggregate prompt when needed) → per-target delete with
        continue-on-error (B4). Every target is backed up before deletion (A2/A3/B8).
        """
        explicit = []
        if path:
            explicit.append(path)
        if paths:
            for p in paths:
                if isinstance(p, str) and p.strip():
                    explicit.append(p)

        # ── B6a: nothing requested → clear error (no approval prompt). ──────────
        if not explicit:
            return "ERROR: No path(s) provided. Supply 'path' or a non-empty 'paths' list."

        # ── A9 (symlink security): check the RAW input path BEFORE _resolve_path. ─
        # Path.resolve() follows symlinks, so is_symlink() on the resolved path is
        # always False; a workspace symlink pointing out-of-bounds would otherwise
        # pass containment and its real target would be deleted. Reject in v1.
        for p in explicit:
            if os.path.islink(p) or Path(p).is_symlink():
                return f"ERROR: Symlink deletion is not supported: {p}"

        # ── B2: resolve + expand into a concrete, de-duplicated target set. ──────
        targets: List[Path] = []
        seen: set = set()
        errors: List[str] = []

        def _add_target(resolved: Path) -> None:
            key = os.path.normcase(str(resolved))
            if key not in seen:
                seen.add(key)
                targets.append(resolved)

        for p in explicit:
            try:
                resolved = self._resolve_path(p, mode='rw')
            except Exception as e:
                errors.append(f"{p}: {e}")
                continue
            if not resolved.exists():
                errors.append(f"File not found: {p}")
                continue
            # Filters are applied within the base directory of a path (B2).
            if self._has_filters(include, exclude, min_size, max_size, modified_after, modified_before):
                matched = self._expand_filtered(resolved, include, exclude, min_size, max_size, modified_after,
                                                modified_before)
                for m in matched:
                    _add_target(m)
            else:
                _add_target(resolved)

        # ── B6: empty resolved set → return BEFORE any approval prompt. ──────────
        if not targets:
            detail = '; '.join(errors) if errors else 'no entries matched the given path/filter'
            return f"No files matched (0 of {len(explicit)} requested). {detail}"

        # ── B3: approval model for bulk deletes. ────────────────────────────────
        # Auto-approve only if ALL targets are agent-owned. Otherwise fire a SINGLE
        # aggregate prompt. The WebUI (web_ui/app.js renderApprovals) renders ONLY the
        # justification field (prominently) + JSON.stringify(tool_args) — it does NOT
        # render `description`. So we compose the human-readable scope summary into
        # tool_args['justification'] (which request_user_approval stores on the
        # PendingApproval and app.js shows most prominently), and keep tool_args itself
        # COMPACT (count + a short paths_preview, never the full list) so the JSON block
        # stays small even for huge bulk deletes. `description` is still passed for any
        # future/other client that does render it, but we don't rely on it.
        # _is_auto_approved takes a str and resolves it internally (mode="rw"); passing
        # the already-resolved path string is idempotent (re-resolution lands on the same
        # contained path) — safe, though it does re-resolve per target.
        non_owned = [t for t in targets if not self._is_auto_approved(str(t), agent_name)]
        if non_owned:
            # _build_delete_approval_description returns ONLY the scope summary (no
            # justification) so we can append the agent's reason exactly once here.
            description = self._build_delete_approval_description(targets, non_owned)
            # Compose the readable scope summary with the original agent justification so
            # the UI shows both (audit trail preserved). Keep tool_args compact.
            paths_preview = [str(t) for t in targets[:5]]
            visible_justification = description if not justification else \
                f"{description}\nJustification: {justification}"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='delete_file',
                tool_args={
                    'count': len(targets),
                    'paths_preview': paths_preview,
                    'justification': visible_justification
                },
                description=description,
            )
            if not approved:
                return f"REJECTED: {reason}"
            # Keep the agent's stated reason (A4); append the user's approval note.
            if reason and reason not in justification:
                justification = f"{justification}\n{reason}".strip()

        # ── B4: per-target execution with continue-on-error + aggregated report. ─
        # BUG_0219: process targets bottom-up (deepest first) so a child is backed
        # up BEFORE its parent is moved; the parent's single move then covers all
        # remaining content. A target that no longer exists was covered by a
        # previously deleted ancestor — report it as deleted, not a failure.
        targets.sort(key=lambda p: len(p.parts), reverse=True)
        deleted: List[Tuple[Path, str]] = []  # (resolved, backup_path_str)
        failures: List[str] = []
        warnings: List[str] = []
        for t in targets:
            if not t.exists():
                # BUG_0219 safety net: a target that vanished before its turn
                # was either covered by an already-deleted ancestor (the normal
                # case under deepest-first ordering) or removed by a race.
                cover = next((ab for a, ab in deleted if a != t and t.is_relative_to(a)), None)
                if cover is not None:
                    deleted.append((t, f'covered by ancestor backup ({cover})'))
                else:
                    deleted.append((t, 'none (vanished before delete)'))
                    warnings.append(f"{t}: vanished before delete (not backed up)")
                continue
            try:
                backup_str, warning = self._delete_one(t, agent_name, justification)
            except Exception as e:
                failures.append(f"{t}: {e}")
                continue
            deleted.append((t, backup_str))
            if warning:
                warnings.append(f"{t}: {warning}")

        total = len(targets)
        ok_count = len(deleted)
        lines = [f"OK: Deleted {ok_count} of {total} entries"]
        for t, backup_str in deleted:
            lines.append(f"  ✓ {t}  →  backup {backup_str if backup_str else 'none (see warnings)'}")
        # A4 (audit trail): on a clean full delete, echo the justification exactly like
        # the single-delete path did. On partial failures it is omitted to keep the
        # per-target failure list the focus of the report.
        if ok_count == total and not errors and justification:
            lines.append(f"Security Justification: {justification}")
        if failures:
            lines.append('Failures:')
            lines.extend(f"  ✗ {f}" for f in failures)
        if warnings:
            lines.append('Warnings (deleted without backup):')
            lines.extend(f"  ⚠ {w}" for w in warnings)
        # B6a errors (unresolvable / not-found explicit entries) are surfaced here.
        if errors:
            lines.append('Skipped (unresolved):')
            lines.extend(f"  - {e}" for e in errors)
        return '\n'.join(lines)

    # ─── delete_file helpers (B2/B3 + A2/A3 core) ──────────────────────────────

    @staticmethod
    def _has_filters(include, exclude, min_size, max_size, modified_after, modified_before) -> bool:
        """True if any filter argument is a non-empty string."""
        return any(
            v and str(v).strip() for v in (include, exclude, min_size, max_size, modified_after, modified_before))

    def _build_filter_ctx(self, include, exclude, min_size, max_size, modified_after, modified_before):
        """Build a FilterContext from raw filter strings (mirrors list_directory)."""
        return FilterContext(
            include_fn=_build_pattern_fn(include),
            exclude_fn=_build_pattern_fn(exclude),
            min_size=_parse_size(min_size),
            max_size=_parse_size(max_size),
            modified_after=_parse_time(modified_after),
            modified_before=_parse_time(modified_before),
        )

    def _expand_filtered(self, base: Path, include, exclude, min_size, max_size, modified_after,
                         modified_before) -> List[Path]:
        """Expand *base* into matched targets using the shared list_dir filter helpers.

        Mirrors list_directory semantics:
          - If *base* is a file, apply the filters to it directly (its own name/size/mtime).
          - If *base* is a directory, walk recursively and collect every entry whose own
            name passes the include/exclude filter; size/date filters apply only to files.
        Every returned path is inside *base*, so containment is guaranteed by the caller's
        _resolve_path(base). De-duplication + stable order are applied by the caller.
        """
        ctx = self._build_filter_ctx(include, exclude, min_size, max_size, modified_after, modified_before)
        results: List[Path] = []

        if base.is_file():
            try:
                st = base.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = None, None
            if self._matches_filters(base.name, False, size=size, mtime=mtime, ctx=ctx):
                results.append(base)
            return results

        # Directory: recursive walk (consistent with list_dir's recursion).
        # DELETION SEMANTICS DIFFER FROM LISTING: a bare filter (no include/exclude name
        # pattern) must match FILES only. Otherwise every subdirectory — including the
        # auto-created logs/backups/ tree that holds this very operation's backups — would
        # be swept in as a delete target. Directories are only matched when the caller
        # explicitly supplies a directory-name filter (include/exclude).
        match_dirs = ctx.include_fn is not None or ctx.exclude_fn is not None

        visited: set = set()
        for dirpath, subdirs, filenames in os.walk(str(base), topdown=True):
            current_dir = Path(dirpath)
            abs_path = current_dir.resolve()
            if abs_path in visited:  # symlink cycle guard (mirrors _list_recursive)
                subdirs.clear()
                continue
            visited.add(abs_path)

            if match_dirs:
                for name in sorted(subdirs):
                    d = current_dir / name
                    try:
                        st = d.stat()
                        size, mtime = 0, None  # dirs: size/date filters never apply
                    except OSError:
                        size, mtime = None, None
                    if self._matches_filters(name, True, size=size, mtime=mtime, ctx=ctx):
                        results.append(d)

            for name in sorted(filenames):
                f = current_dir / name
                try:
                    st = f.stat()
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    size, mtime = None, None
                if self._matches_filters(name, False, size=size, mtime=mtime, ctx=ctx):
                    results.append(f)

        return results

    def _build_delete_approval_description(self, targets: List[Path], non_owned: List[Path]) -> str:
        """Human-readable scope summary for the aggregate approval prompt (B3).

        Carries the readable count, total size and a sample of the paths. The caller is
        responsible for appending any justification (it composes this into the field the
        WebUI actually renders) — so this method deliberately does NOT take one, to avoid
        duplicating it in the prompt. NOTE: web_ui/app.js approval card does not render
        `description`; see delete_file().
        """
        n = len(targets)
        n_files = sum(1 for t in targets if not t.is_dir())
        n_dirs = n - n_files

        # Cap the walk so a huge directory can't stall the approval prompt (UI lag).
        # The size is only an informational summary, not used for any decision. When the
        # cap trips we mark the total as approximate ("+") so the number the user approves
        # against is honest rather than silently under-reported (mirrors _compute_scope_info).
        total_size = 0
        counted_files = 0
        capped = False
        for t in targets:
            try:
                if t.is_file():
                    total_size += t.stat().st_size
                    counted_files += 1
                else:
                    for f in t.rglob('*'):
                        if f.is_file():
                            total_size += f.stat().st_size
                            counted_files += 1
                            if counted_files >= _SCOPE_INFO_FILE_CAP:
                                capped = True
                                break
            except OSError:
                pass

        sample = targets[:5]
        sample_str = '\n'.join(f"  - {t}" for t in sample)
        more = f"\n  … and {n - len(sample)} more" if n > len(sample) else ''

        size_str = self._format_size(total_size) + ('+' if capped else '')
        desc = (f"Delete {n} entr{'y' if n == 1 else 'ies'} "
                f"({n_files} file(s), {n_dirs} dir(s), total {size_str}):")
        desc += '\n' + sample_str + more
        if non_owned:
            desc += f"\n\n{len(non_owned)} of these are not owned by you and require approval."
        return desc

    def _delete_one(self, resolved: Path, agent_name: str, justification: str = '') -> Tuple[str, Optional[str]]:
        """Hardened single-target delete (A2 backup+verify, A3 ownership cleanup).

        Returns ``(backup_path_str, warning)``. On a clean backup, ``warning`` is
        None and ``backup_path_str`` is the backup path. BUG_0219: when the
        entry's path has already vanished (a deeper target's move took an
        ancestor with it — the ancestor-already-moved signature), the entry is
        deleted best-effort, ``backup_path_str`` is '' and the warning names the
        missing backup — a partial tree is worse than a deleted-but-unbacked
        entry. Genuine backup failures (disk full, permissions) and integrity
        failures still raise with the original preserved (A2), so the bulk loop
        can collect per-target status (continue-on-error, B4).
        """
        is_directory = resolved.is_dir()

        safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
        backup_dir = self.base_dir / 'logs' / 'backups' / safe_agent
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time())
        # BUG_0219: sanitise the backup filename so the destination is always a
        # plain name; the original is recorded in the manifest.
        safe_name = self._sanitize_backup_name(resolved.name)
        counter = 0
        while True:
            if counter == 0:
                backup_filename = f"{safe_name}.{timestamp}.bak"
            else:
                backup_filename = f"{safe_name}.{timestamp}_{counter}.bak"
            backup_path = backup_dir / backup_filename
            if not backup_path.exists():
                break
            counter += 1

        # A2: back up, verify integrity, then delete — never delete the original
        # unless we are sure the backup is complete. Partial backups are removed.
        warning = None
        try:
            shutil.move(resolved, backup_path)
        except Exception:
            # Fallback: copy to backup, verify, then remove the original.
            try:
                if is_directory:
                    shutil.copytree(resolved, backup_path)
                else:
                    shutil.copy2(resolved, backup_path)
            except Exception as copy_err:
                if isinstance(copy_err, (FileNotFoundError, NotADirectoryError)) and not resolved.exists():
                    # BUG_0219: ancestor-already-moved signature — a deeper
                    # target's move already took this entry with it, so its
                    # path is gone; clean up the partial backup and report the
                    # missing one. (A6: name the failed step.)
                    self._remove_partial_backup(backup_path)
                    warning = f"backup failed before delete ({copy_err}); deleted without backup"
                else:
                    # Copy failed before any deletion — clean up the partial
                    # backup, leave the original untouched. (A6: name the step.)
                    self._remove_partial_backup(backup_path)
                    raise RuntimeError(f"backup failed before delete: {copy_err}")

            if warning is None:
                try:
                    if is_directory:
                        if not self._verify_dir_backup(resolved, backup_path):
                            raise RuntimeError('backup integrity check failed (file count/size mismatch)')
                        shutil.rmtree(resolved)
                    else:
                        if not self._verify_file_backup(resolved, backup_path):
                            raise RuntimeError('backup integrity check failed (missing or size mismatch)')
                        resolved.unlink()
                except Exception as del_err:
                    # Delete/integrity step failed — clean up partial backup.
                    # Original is only gone if rmtree/unlink itself succeeded first.
                    self._remove_partial_backup(backup_path)
                    raise RuntimeError(f"delete failed after backup: {del_err}")

        # Best-effort manifest (BUG_0219): only when sanitisation changed the
        # name, and outside the backup-critical path so a manifest problem can
        # never destroy a valid backup or block the delete.
        if warning is None and safe_name != resolved.name:
            try:
                self._write_backup_manifest(backup_path, resolved, timestamp)
            except Exception:
                pass

        backup_path_str = '' if warning is not None else self._backup_path_str(backup_path)

        # A3: normalized ownership cleanup (atomic + normcase under lock).
        # Operates on the ownership dict, so it is safe after the entry is gone.
        if is_directory:
            self._unown_recursive(resolved)
        else:
            self._unown(resolved)

        return backup_path_str, warning

    # ─── delete_file helpers (A2/A3) ───────────────────────────────────────

    @staticmethod
    def _remove_partial_backup(backup_path) -> None:
        """Best-effort removal of a partially-created backup (file or dir) and its manifest."""
        try:
            if backup_path.is_dir():
                shutil.rmtree(backup_path, ignore_errors=True)
            elif backup_path.exists():
                backup_path.unlink()
            manifest = FileOpsMixin._manifest_path(backup_path)
            if manifest.exists():
                manifest.unlink()
        except Exception:
            pass

    @staticmethod
    def _verify_file_backup(source, backup_path) -> bool:
        """Verify a file backup exists and matches the source size."""
        if not backup_path.is_file():
            return False
        try:
            return backup_path.stat().st_size == source.stat().st_size
        except OSError:
            return False

    @staticmethod
    def _verify_dir_backup(source, backup_path) -> bool:
        """Verify a directory backup has file-count and total-size parity with the source.

        File count is the primary check (cheap); total size is a lightweight
        spot-check that catches most partial copies without hashing every file.
        Known v1 limitation: count+size parity is a spot-check, not a content guarantee —
        two distinct trees with identical file count and total size could pass. This only
        runs on the cross-volume copy fallback (the same-volume path uses an atomic move).
        """
        if not backup_path.is_dir():
            return False
        try:
            src_files = [f for f in source.rglob('*') if f.is_file()]
            bak_files = [f for f in backup_path.rglob('*') if f.is_file()]
            if len(src_files) != len(bak_files):
                return False
            src_size = sum(f.stat().st_size for f in src_files)
            bak_size = sum(f.stat().st_size for f in bak_files)
            return src_size == bak_size
        except OSError:
            return False

    # ─── Copy file ────────────────────────────────────────────────────────

    def copy_file(self, source: str, destination: str, agent_name: str, justification: str = '') -> str:
        """Copy a file — auto-approved if destination is new or agent-owned."""
        try:
            src_path = self._resolve_path(source, mode='ro')
            self._resolve_path(destination, mode='rw')  # validate destination is writable (side effect)
        except Exception as e:
            return f"ERROR: {str(e)}"
        if not src_path.exists():
            return f"Source not found: {source}"

        if not self._is_auto_approved(destination, agent_name, creating_new=True):
            description = f"Copy: {source} → {destination}"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='copy_file',
                tool_args={
                    'source': source,
                    'destination': destination,
                    'justification': justification
                },
                description=description,
            )
            if not approved:
                return f"REJECTED: {reason}"
            approver_reason = reason  # BUG_0039: keep provenance — do not overwrite the caller's justification
        else:
            approver_reason = ''

        # Memory-vault write screen (OWASP LLM01). A copy into the vault persists the
        # source file's text at the destination, so it is screened like a direct write
        # — before the backup/write below so a rejected copy leaves no orphan .bak.
        # Directories and binary sources are skipped (see _screen_vault_copy_move).
        _rejection = _screen_vault_copy_move(src_path, self._resolve_path(destination, mode='rw'))
        if _rejection:
            from agent_cascade.log import logger
            logger.warning('[MEMORY_SCREEN] Rejected copy_file to %s: %s', destination, _rejection)
            return (f"ERROR: Copy to '{destination}' rejected by memory-vault injection screen "
                    f"({_rejection}). The source content matches prompt-injection patterns. "
                    f"Remove the offending content and retry.")

        try:
            dest_path = self._resolve_path(destination, mode='rw')

            scope_info = self._compute_scope_info(src_path)

            backup_path_str = ''
            was_overwrite = False
            if dest_path.exists():
                safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
                backup_dir = self.base_dir / 'logs' / 'backups' / safe_agent
                backup_dir.mkdir(parents=True, exist_ok=True)

                timestamp = int(time.time())
                counter = 0
                while True:
                    if counter == 0:
                        backup_filename = f"{dest_path.name}.{timestamp}.bak"
                    else:
                        backup_filename = f"{dest_path.name}.{timestamp}_{counter}.bak"
                    backup_path = backup_dir / backup_filename
                    if not backup_path.exists():
                        break
                    counter += 1

                if dest_path.is_dir():
                    shutil.copytree(dest_path, backup_path)
                else:
                    shutil.copy2(dest_path, backup_path)

                backup_path_str = self._backup_path_str(backup_path)
                was_overwrite = True

            # BUG_0249: destination is authoritative — treat it as a directory ONLY when
            # explicitly so (raw trailing separator) or it already exists as one. Never infer
            # dir-ness from the source, which silently created a directory at a file destination.
            dest_explicit_dir = destination.endswith(('/', '\\')) or dest_path.is_dir()
            if src_path.is_dir() and not dest_explicit_dir:
                return (
                    f"ERROR: source is a directory but destination is a file path "
                    f"(source={source}, destination={destination}). "
                    f"End the destination with '/' (or '\\') for a directory copy."
                )
            if src_path.is_dir():                       # dest explicitly a directory
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
            else:                                        # file source → always a FILE at dest_path
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dest_path)
            self._own(dest_path, agent_name)

            if was_overwrite:
                msg = f"OK: Copied {source} → {destination} (overwrote{scope_info})"
            else:
                msg = f"OK: Copied {source} → {destination} {scope_info}"

            if justification:
                msg += f"\nSecurity Justification: {justification}"
            if approver_reason:
                msg += self._approver_reason_line(approver_reason)

            if backup_path_str:
                msg += f'\n  backup → {backup_path_str}'

            return msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Move file ────────────────────────────────────────────────────────

    def move_file(self, source: str, destination: str, agent_name: str, justification: str = '') -> str:
        """Move a file — auto-approved if source is agent-owned."""
        try:
            src_path = self._resolve_path(source, mode='rw')
            self._resolve_path(destination, mode='rw')  # validate destination is writable (side effect)
        except Exception as e:
            return f"ERROR: {str(e)}"
        if not src_path.exists():
            return f"Source not found: {source}"

        if not self._is_auto_approved(source, agent_name):
            description = f"Move: {source} → {destination}"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='move_file',
                tool_args={
                    'source': source,
                    'destination': destination,
                    'justification': justification
                },
                description=description,
            )
            if not approved:
                return f"REJECTED: {reason}"
            approver_reason = reason  # BUG_0039: keep provenance — do not overwrite the caller's justification
        else:
            approver_reason = ''

        # Memory-vault write screen (OWASP LLM01). A move into the vault persists the
        # source file's text at the destination, so it is screened like a direct write
        # — before the backup/unlink/move below, so a rejected move never destroys the
        # source. Directories and binary sources are skipped (see _screen_vault_copy_move).
        _rejection = _screen_vault_copy_move(src_path, self._resolve_path(destination, mode='rw'))
        if _rejection:
            from agent_cascade.log import logger
            logger.warning('[MEMORY_SCREEN] Rejected move_file to %s: %s', destination, _rejection)
            return (f"ERROR: Move to '{destination}' rejected by memory-vault injection screen "
                    f"({_rejection}). The source content matches prompt-injection patterns. "
                    f"Remove the offending content and retry.")

        try:
            dest_path = self._resolve_path(destination, mode='rw')

            backup_path_str = ''
            if dest_path.exists():
                safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
                backup_dir = self.base_dir / 'logs' / 'backups' / safe_agent
                backup_dir.mkdir(parents=True, exist_ok=True)

                timestamp = int(time.time())
                counter = 0
                while True:
                    if counter == 0:
                        backup_filename = f"{dest_path.name}.{timestamp}.bak"
                    else:
                        backup_filename = f"{dest_path.name}.{timestamp}_{counter}.bak"
                    backup_path = backup_dir / backup_filename
                    if not backup_path.exists():
                        break
                    counter += 1

                if dest_path.is_dir():
                    shutil.copytree(dest_path, backup_path)
                else:
                    shutil.copy2(dest_path, backup_path)

                backup_path_str = self._backup_path_str(backup_path)

                if dest_path.is_dir():
                    shutil.rmtree(dest_path)
                else:
                    dest_path.unlink()

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src_path, dest_path)
            self._unown(src_path)
            self._own(dest_path, agent_name)
            msg = f"OK: Moved {source} → {destination}"

            if justification:
                msg += f"\nSecurity Justification: {justification}"
            if approver_reason:
                msg += self._approver_reason_line(approver_reason)

            if backup_path_str:
                msg += f'\n  backup → {backup_path_str}'

            return msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Utilities ────────────────────────────────────────────────────────

    def get_file_owner(self, path: str) -> Optional[str]:
        """Get the owner of a file (normalized lookup)."""
        return self._get_owner(path)
