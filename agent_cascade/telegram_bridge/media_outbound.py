"""Outbound media detection for the Telegram bridge (todo.md:102).

Two deliberately separated layers, per plans/tg-outbound-media_PLAN.md §2.1:

* PURE (no I/O, no ``telegram`` import): ``split_media`` finds media references inside
  an outbound text message and reports them as ``MediaItem`` records with exact spans.
  Hermetic and unit-testable without PTB installed - the house convention for this
  package (tests/test_telegram_bridge.py keeps the Bot a MagicMock).
* RESOLUTION (file I/O, still no PTB): ``resolve_media_source`` turns an item into
  something PTB can send - a local path, decoded bytes, or an http URL - or refuses it
  with a reason, so the sender can keep the original link as text.

SINGLE CONTRACT (plan §2.1/§3): the parser NEVER edits the text. It only reports each
match's ``(start, end)`` offsets. The sender owns removal and excises a span ONLY
after that item was actually delivered, in descending offset order.
"""

import base64
import binascii
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import unquote

from agent_cascade.log import logger

# NOTE: the ROOT path_security module (agent_cascade/path_security.py, the /api/file
# guard) — NOT agent_cascade/operation_manager/path_security.py, which is a completely
# different module (see [[agentcascade-extra-paths-canonical-source]]). Reusing the
# /api/file policy keeps "what the phone can be sent" == "what the browser can be shown".
from agent_cascade.path_security import _is_path_allowed

# ── extension / MIME source of truth ───────────────────────────────────────────
# Keep in sync with web_ui/app.js:5646-5647 (the browser's own lists, which carry the
# matching "single source of truth … keep in sync" note). The JS side has no image
# list because the browser renders images through generic markdown <img>, so _IMAGE_EXTS
# is defined here (plan §1.4) and is the one to mirror into app.js if it ever needs one.
_IMAGE_EXTS = frozenset({'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tiff', 'tif'})
_AUDIO_EXTS = frozenset({'mp3', 'wav', 'ogg', 'm4a', 'flac', 'opus'})   # == app.js:5647
_VIDEO_EXTS = frozenset({'mp4', 'webm', 'mov', 'mkv', 'ogv'})           # == app.js:5646

# The bare-path detector's extension alternation is DERIVED from these frozensets —
# one source of truth, never a second hand-maintained list. Sorted longest-first so
# 'tiff' can never be shadowed by 'tif' inside the regex.
_EXT_ALT = '|'.join(sorted(_IMAGE_EXTS | _AUDIO_EXTS | _VIDEO_EXTS,
                           key=lambda s: (-len(s), s)))

# Alt labels emitted by api_integration_pkg/state_builder.py:970-994 as generic
# placeholders (![image](…), ![audio](…)). They are noise, never captions (plan §7.2).
_GENERIC_ALTS = frozenset({'', 'image', 'img', 'picture', 'photo', 'audio',
                           'sound', 'music', 'video', 'media'})

# (1) markdown media link. Requires the full ![...](...) shape, so [text](url) never
#     matches. alt: [^\]]* (empty allowed); target: [^)]* (empty allowed, stops at the
#     first ')'), which is why '![alt](img (1).jpg)' yields the target 'img (1' and
#     therefore no media at all (E16 — documented limitation, safer than a partial send).
_MD_LINK_RE = re.compile(r'!\[([^\]]*)\]\(([^)]*)\)')

# (2) bare absolute path with a media extension. Only ever run on the text left over
#     after (1)'s matched spans are masked out, so a markdown target is never double-
#     counted (verified: unmasked, the bare pass finds 'e:///N:/…/x.jpg' inside a link).
#     Trailing punctuation is not part of the token (the ext group ends it), so the
#     real media_gen.py:453 / file_ops.py:859 formats match exactly. A relative
#     'photo.jpg' cannot match (no leading '/' or drive) — excluded by design (§1.3).
_BARE_ABS_RE = re.compile(
    r'(?P<path>[A-Za-z]:[\\/][^\s"\'`,<>|]+|[\\/][^\s"\'`,<>|]+)'   # drive-absolute OR POSIX-absolute
    r'(?P<ext>\.(?:' + _EXT_ALT + r'))'
    r'(?![\w])')                                                    # not followed by a word char

# 'file:///N:/x.jpg' minus the scheme is '/N:/x.jpg'; a leading slash before a drive
# letter is drive-relative on Windows (Path('/N:/x.jpg').resolve() -> WindowsPath('N:x.jpg')),
# which silently turns a MUST-send photo into 'missing'. Repaired in _local_target_body.
_FILE_URL_PREFIX = 'file://'
_DRIVE_AFTER_SLASH_RE = re.compile(r'^/[A-Za-z]:')

# A drive-absolute path ('N:/x', 'C:\x') — the other half of _is_absolute_target.
_DRIVE_LEADING_RE = re.compile(r'^[A-Za-z]:[\\/]')

# data:<mime>[;params],<payload> — the browser-pasted-image shape (web_ui/app.js:5410-5412).
_DATA_URI_RE = re.compile(r'\Adata:(?P<mime>[^;,]*)(?P<params>[^,]*),(?P<payload>.*)\Z', re.DOTALL)

_SOURCE_DATA_URI = 'data_uri'
_SOURCE_FILE_URL = 'file_url'
_SOURCE_HTTP_URL = 'http_url'
_SOURCE_LOCAL_PATH = 'local_path'


@dataclass(frozen=True)
class MediaItem:
    """One detected media reference inside an outbound message.

    ``kind`` comes from the extension or the data-URI MIME, never from the alt text
    (plan §1.2 — the UI classifies the same way, and state_builder's alt labels are
    placeholders). ``value`` is the raw target text exactly as it appeared, so the
    model stays JSON-dumpable for logs/tests; bytes appear only in resolve_media_source's
    return. ``kind`` is 'image' | 'audio' ('video' only when TG_MEDIA_SEND_VIDEO is on).
    """
    kind: str
    source: str          # 'local_path' | 'file_url' | 'http_url' | 'data_uri'
    value: str
    alt: str             # markdown label, may be ''
    span: Tuple[int, int]


def _source_kind(raw: str) -> str:
    """Classify the raw target's syntax: data URI, file:// URL, http(s) URL, or path."""
    low = raw.strip().lower()
    if low.startswith('data:'):
        return _SOURCE_DATA_URI
    if low.startswith(_FILE_URL_PREFIX):
        return _SOURCE_FILE_URL
    if low.startswith('http://') or low.startswith('https://'):
        return _SOURCE_HTTP_URL
    return _SOURCE_LOCAL_PATH


def media_ext_of(value: str) -> Optional[str]:
    """Return the lowercase extension of ``value`` without its dot, or None.

    Strips the '?query' and '#fragment' parts FIRST (plan E9): '…img.jpg?v=2#x' ->
    'jpg', while a '.jpg' living in the query string is not trusted.
    """
    stripped = value.split('#', 1)[0].split('?', 1)[0]
    ext = os.path.splitext(stripped)[1].lstrip('.').lower()
    return ext or None


def data_uri_mime(value: str) -> Optional[str]:
    """Return the media kind ('image'|'audio'|'video') for a data URI, else None.

    The MIME gate is the ONLY classifier for this shape — a data URI has no extension
    (plan §6). Anything that is not image/*, audio/* (or video/*) is not media. The
    matched MIME itself is NOT returned: no caller reads it, the kind is all the sender
    needs (PTB infers the content type from the decoded bytes).
    """
    match = _DATA_URI_RE.match(value.strip())
    if not match:
        return None
    mime = (match.group('mime') or '').lower()
    for kind, prefix in (('image', 'image/'), ('audio', 'audio/'), ('video', 'video/')):
        if mime.startswith(prefix):
            return kind
    return None


def _local_target_body(raw: str) -> str:
    """Return a target's local-path BODY: strip a leading ``file://`` scheme, then
    repair a drive letter left after a leading slash ('/N:/x' -> 'N:/x').

    One definition of that rule, used by ``normalize_local_target`` and
    ``_is_absolute_target`` (``_source_kind`` shares the same prefix test via
    ``_FILE_URL_PREFIX``) so the three cannot diverge. Order is load-bearing (plan
    §4.2 step 2 / E24, E26): strip the scheme BEFORE the drive repair.
    """
    path = raw.strip()
    if path.lower().startswith(_FILE_URL_PREFIX):
        path = path[len(_FILE_URL_PREFIX):]
    if _DRIVE_AFTER_SLASH_RE.match(path):
        path = path[1:]
    return path


def normalize_local_target(raw: str) -> str:
    """Normalize a file:// URL or a local path to a resolved absolute path string.

    Order is load-bearing (plan §4.2 step 2 / E24, E26): _local_target_body strips the
    ``file://`` scheme and repairs the drive letter, THEN unquote EXACTLY ONCE, THEN
    resolve. ``_is_path_allowed`` unquotes again internally (path_security.py:78),
    so double-encoded names ('%2520') are documented as unsupported — a file literally
    named 'photo%201.jpg' is checked as 'photo 1.jpg'.
    """
    path = unquote(_local_target_body(raw))
    return str(Path(path).resolve())


def _is_absolute_target(raw: str) -> bool:
    """True if a local/file:// target is drive-, UNC- or POSIX-absolute.

    §1.3 EXCLUDEs relative paths ("cannot resolve against a safe root without guessing").
    The bare regex enforces that by construction, but a MARKDOWN target like
    ``![image](photos/a.jpg)`` would otherwise resolve against the bridge process CWD and
    could send an unrelated file. Same rule for both, so behaviour cannot diverge.
    """
    path = _local_target_body(raw)
    return bool(_DRIVE_LEADING_RE.match(path)) or path[:1] in ('/', '\\')


def classify_target(raw: str) -> Tuple[Optional[str], str, str]:
    """Return ``(kind | None, source_kind, normalized_value)`` for one link target.

    Order (plan §4.2 step 1): data-URI MIME gate -> extension gate (after ?/# stripping)
    -> not media. ``normalized_value`` is the resolved absolute path for local/file
    targets, and the raw text for http/data-URI targets.
    """
    value = raw.strip()
    if not value:
        return (None, '', '')

    source = _source_kind(value)
    if source in (_SOURCE_LOCAL_PATH, _SOURCE_FILE_URL) and not _is_absolute_target(value):
        # §1.3: relative targets stay text. Without this, '![image](photos/a.jpg)' would
        # resolve against the bridge process CWD and could send an unrelated file.
        return (None, '', '')
    if source == _SOURCE_DATA_URI:
        kind = data_uri_mime(value)
        if kind is None:
            return (None, '', '')
        return (kind, _SOURCE_DATA_URI, value)

    ext = media_ext_of(value)
    if ext is None:
        return (None, '', '')
    if ext in _IMAGE_EXTS:
        kind = 'image'
    elif ext in _AUDIO_EXTS:
        kind = 'audio'
    elif ext in _VIDEO_EXTS:
        kind = 'video'
    else:
        return (None, '', '')   # e.g. ![](doc.pdf), [text](notes.md) -> stays text

    if source == _SOURCE_HTTP_URL:
        return (kind, _SOURCE_HTTP_URL, value)
    try:
        return (kind, source, normalize_local_target(value))
    except (OSError, ValueError):
        # Unresolvable string (embedded NUL, etc.): keep the raw text; resolve_media_source
        # will refuse it rather than raise.
        return (kind, source, value)


def split_media(text: str, *, allow_video: bool = False,
                detect_bare_paths: bool = True) -> Tuple[str, List[MediaItem]]:
    """Return ``(ORIGINAL text UNCHANGED, [MediaItem, ...])`` in document order.

    The parser never edits the text — it only reports spans (see module docstring).
    ``detect_bare_paths=False`` disables the riskiest detector (E17 kill-switch);
    ``allow_video=False`` (the default) leaves video references as text (plan O-8).
    Identical ``(kind, normalized target)`` pairs are reported once, keeping the first
    span (plan O-3): a repeated path must not be uploaded twice, and a dropped duplicate
    keeps its link in the text.
    """
    if not text:
        return text, []

    # Masking preserves offsets and cannot create a new bare-path token (a space is
    # always a token boundary), so the bare pass never double-counts a markdown target.
    masked = list(text)
    candidates: List[Tuple[MediaItem, str]] = []

    for match in _MD_LINK_RE.finditer(text):
        for idx in range(match.start(), match.end()):
            masked[idx] = ' '
        raw_target = match.group(2)
        kind, _source, normalized = classify_target(raw_target)
        if kind is None or (kind == 'video' and not allow_video):
            continue
        candidates.append((
            MediaItem(kind=kind, source=_source, value=raw_target, alt=match.group(1),
                      span=(match.start(), match.end())),
            normalized,
        ))

    if detect_bare_paths:
        for match in _BARE_ABS_RE.finditer(''.join(masked)):
            raw_target = match.group('path') + match.group('ext')
            if '://' in raw_target:
                # A URL fragment, not a path: the drive branch can start INSIDE a scheme,
                # so bare 'http://x.com/img.jpg' matches as 'p://x.com/img.jpg' (an artifact
                # of the plan's drafted regex, §2.1 evidence table). Left alone it would be
                # classified local_path, refused as 'missing', and log a warning per URL.
                continue
            kind, _source, normalized = classify_target(raw_target)
            if kind is None or (kind == 'video' and not allow_video):
                continue
            candidates.append((
                MediaItem(kind=kind, source=_source, value=raw_target, alt='',
                          span=(match.start(), match.end())),
                normalized,
            ))

    candidates.sort(key=lambda pair: pair[0].span[0])

    items: List[MediaItem] = []
    seen = set()
    for item, normalized in candidates:
        key = (item.kind, normalized)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return text, items


def caption_from_alt(alt: str) -> Optional[str]:
    """Return the media caption for a markdown alt label, or None (plan §7.2).

    Generic placeholders (``![image](…)``, ``![audio](…)``) are what state_builder.py
    emits for every attachment, so using them as captions would be pure noise.
    """
    label = (alt or '').strip()
    if not label or label.lower() in _GENERIC_ALTS:
        return None
    return label


def resolve_media_source(item: MediaItem, *, allowed_roots: Optional[Iterable[str]] = None,
                        max_photo_bytes: int, max_other_bytes: int) -> Tuple[Optional[str], object]:
    """Turn a detected item into something PTB can send, or refuse it with a reason.

    Returns one of ``('local', abs_path)``, ``('bytes', data)``, ``('http_url', url)`` or
    ``(None, reason)`` with reason in ``{'missing', 'oversized', 'not_allowed',
    'unreadable', 'bad_uri'}``. Checks run cheap -> expensive (plan §4.2): the allowlist
    gate comes BEFORE any stat, so a hostile path never touches the disk beyond one
    stat, and the size cap is checked BEFORE the file is read.

    Every refusal logs a WARNING naming the resolved path (plan E7 / §14) — never on a
    ``[TG-PUSH]`` line, whose content-free contract is enforced by the sender.
    """
    cap = max_photo_bytes if item.kind == 'image' else max_other_bytes

    if item.source == _SOURCE_DATA_URI:
        match = _DATA_URI_RE.match(item.value.strip())
        if not match:
            return (None, 'bad_uri')
        try:
            # validate=False + the whole PTB error family: a truncated/corrupt payload
            # must degrade to text, never raise (plan §6).
            data = base64.b64decode(match.group('payload'), validate=False)
        except (binascii.Error, ValueError):
            return (None, 'bad_uri')
        if not data:
            return (None, 'unreadable')   # E20: a 0-byte photo is a guaranteed BadRequest
        if len(data) > cap:
            # Cap the DECODED bytes: base64 inflates by 4/3 (plan §6).
            return (None, 'oversized')
        return ('bytes', data)

    if item.source == _SOURCE_HTTP_URL:
        # Pass-through (plan §5/O-7): Telegram fetches it. classify_target already
        # required a media extension after stripping ?/#, so no second gate here.
        return (_SOURCE_HTTP_URL, item.value)

    try:
        path = normalize_local_target(item.value)
    except (OSError, ValueError):
        return (None, 'missing')

    if not _is_path_allowed(path, allowed_roots):
        logger.warning('Telegram outbound media refused (not_allowed): %s (kind=%s)', path, item.kind)
        return (None, 'not_allowed')

    if not os.path.isfile(path):
        return (None, 'missing')
    try:
        size = os.path.getsize(path)
    except OSError:
        return (None, 'missing')
    if size == 0:
        logger.warning('Telegram outbound media refused (unreadable, 0 bytes): %s (kind=%s)', path, item.kind)
        return (None, 'unreadable')
    if not os.access(path, os.R_OK):
        logger.warning('Telegram outbound media refused (unreadable): %s (kind=%s)', path, item.kind)
        return (None, 'unreadable')
    if size > cap:
        logger.warning('Telegram outbound media refused (oversized, %d bytes > %d): %s (kind=%s)',
                       size, cap, path, item.kind)
        return (None, 'oversized')
    return ('local', path)
