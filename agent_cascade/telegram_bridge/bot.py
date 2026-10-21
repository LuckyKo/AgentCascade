"""PTB Application wiring for the v1 Telegram bridge.

Receiving path (per incoming text message from the allowlisted user):
    1. Auth gate — non-allowlisted users are ignored (no reply, no AC call).
    2. Inject the task into AC via /api/message (handshake cached in ACClient).
    3. Reply once with a short ack ("🏃 Started").
    4. Spawn a waiter coroutine that polls /api/status until generation ends, then
       exits quietly: final-answer delivery is push-based at run end (the engine's
       pre-reflection hook and the post-run push in run_agent_unified deliver it).
       A task timeout is NON-FATAL: it sends a "⏳ Still working" notice and keeps
       polling (one notice per task-timeout interval) until AC finishes or the
       outer ceiling (TG_TASK_WAIT_CEILING_SEC) is hit.

Sending path helpers: ``chunk_text`` splits text into <=4096-char parts on line
boundaries; ``_safe_send`` chunks long messages and honors 429 retry_after. When a
message references image/audio files, ``_safe_send`` also delivers them as real
Telegram media (todo.md:102) via ``media_outbound`` + ``_send_media_aware``; text-only
messages take the pre-existing code path verbatim.
"""

import asyncio
import base64
import hashlib
import os
from typing import List, NamedTuple, Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import MessageLimit, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from agent_cascade import settings
from agent_cascade.log import logger
from agent_cascade.settings import (
    TG_MAX_MESSAGE_LEN,
    TG_OFFLINE_AFTER_SEC,
    TG_SEND_RETRY_BACKOFF_BASE_SEC,
    TG_SEND_RETRY_BACKOFF_CAP_SEC,
    TG_TASK_WAIT_CEILING_SEC,
)

from .ac_client import ACClient, ACError
from .commands import dispatch_command
from .config import BridgeConfig
# media_outbound is a pure parser + resolver (no PTB import) — see its module docstring.
from .media_outbound import (
    MediaItem,
    caption_from_alt,
    resolve_media_source,
    split_media,
)
from .waiter import WaiterResult, wait_for_completion

# Telegram's hard limit is 4096 chars/message. Chunk at exactly that so we only
# split when a single reply genuinely exceeds the limit (per v1 spec). The value
# lives in settings (TG_MAX_MESSAGE_LEN) so it's tunable/overridable in one place.
CHUNK_SIZE = TG_MAX_MESSAGE_LEN

# Max send attempts on transient Telegram network errors (stale connection after
# idle) before surfacing. 429 retries stay unbounded (per Telegram's retry_after),
# so this bound applies ONLY to the NetworkError path below. At defaults
# (base=1.0, cap=30.0) the backoff sleeps are 1+2+4+8 = ~15s of recovery window.
_SEND_ONE_MAX_ATTEMPTS = 5

# Inbound idempotency: Telegram update ids already processed by on_message. A
# redelivered update (PTB long-poll reconnect, sleep-wake, NAT expiry) must not
# start a second AC run — the per-run push model would deliver the answer N times.
# Keys are heterogeneous (BUG_0023): bare ints for globally-unique ``update_id``,
# ``(chat_id, message_id)`` tuples for the per-chat-scoped ``message_id`` fallback
# so two different chats cannot collide on the same numeric id.
_SEEN_TG_IDS: set = set()
_SEEN_TG_IDS_MAX = 1000

# Max file size a bot can fetch via the hosted Bot API getFile (20 MB). Larger
# inbound photos/documents are rejected with a notice instead of downloaded —
# matches core.telegram.org/bots/api ("bots can download files of up to 20MB").
_TG_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


def chunk_text(text: str, limit: int = CHUNK_SIZE) -> List[str]:
    """Split ``text`` into parts each <= ``limit`` chars, preferring line breaks.

    Guarantees: every part has length <= limit, and ''.join(parts) == text
    (no characters are dropped or added). Empty input -> []. A single line longer
    than the limit is hard-split.
    """
    text = '' if text is None else str(text)
    if len(text) <= limit:
        return [text] if text else []

    parts: List[str] = []
    remaining = text
    while len(remaining) > limit:
        # Find the last newline within the window; split there to keep lines whole.
        cut = remaining.rfind('\n', 0, limit + 1)
        if cut <= 0:
            cut = limit  # no line boundary in range -> hard split
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        parts.append(remaining)
    return parts


def _retry_after_seconds(exc, fallback: float) -> float:
    """Extract the 429 retry-after delay from a PTB ``RetryAfter`` as seconds.

    Handles both the current int/float form and the upcoming ``timedelta`` form
    (PTB >= v22.2 deprecates the int; a future major will switch to timedelta).
    Falls back to ``fallback`` if the attribute is missing or unparseable, so an
    unexpected shape never crashes the send loop.
    """
    val = getattr(exc, 'retry_after', None)
    if val is None:
        return fallback
    try:
        # timedelta has .total_seconds(); int/float go through float().
        total = getattr(val, 'total_seconds', None)
        return float(total()) if callable(total) else float(val)
    except (TypeError, ValueError):
        return fallback


def _content_fingerprint(payload: str) -> str:
    """Content fingerprint (first 8 hex chars of sha1) for log correlation.

    Used ONLY in log lines, so repeated retries/deliveries of the SAME payload are
    correlatable across lines. Never stored, and the payload itself is never logged
    (the content-free [TG-PUSH] contract, tests/test_tg_dup_stale_snapshot.py).
    """
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:8]


async def _attempt_send(call, label: str, fingerprint: str) -> None:
    """Run one PTB send call under the shared retry policy (plan §2.2, Option B).

    ``call`` is a zero-argument callable returning the coroutine of a single send
    attempt (``send_message``, ``send_photo``, ``send_audio``, ...); ``label`` only
    decorates the BadRequest log ('send' keeps the text-path line byte-identical);
    ``fingerprint`` is the content hash for the network-retry line.

    Policy (identical for text and media, so neither path can drift):
      * 429 (``RetryAfter``) -> sleep its retry_after; unbounded, per Telegram's pacing.
      * ``BadRequest`` -> re-raise immediately; a bad request will not heal by retrying.
        PTB hierarchy note: BOTH ``TimedOut`` and ``BadRequest`` subclass ``NetworkError``,
        so the ``except BadRequest`` branch MUST precede the ``except NetworkError`` branch
        or it would be swallowed and retried (tests/test_telegram_bridge.py:1912).
      * ``NetworkError``/``TimedOut`` -> exponential backoff, bounded to
        ``_SEND_ONE_MAX_ATTEMPTS`` total attempts, then re-raise
        (tests/test_telegram_bridge.py:1896).
    """
    backoff = TG_SEND_RETRY_BACKOFF_BASE_SEC
    net_attempts = 0
    while True:
        try:
            await call()
            return
        except RetryAfter as e:
            wait = _retry_after_seconds(e, backoff)
            logger.warning('Telegram 429; sleeping %.1fs before retry', wait)
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, TG_SEND_RETRY_BACKOFF_CAP_SEC)
        except BadRequest as e:
            # E.g. message too long despite chunking (shouldn't happen). Surface it.
            logger.error('Telegram %s failed (BadRequest): %s', label, e)
            raise
        except NetworkError as e:
            # Transient failure from a stale/dropped Telegram connection (covers
            # TimedOut and plain NetworkError). Retry with exponential backoff, but
            # bound the attempts so a persistently-dead connection surfaces.
            net_attempts += 1
            if net_attempts >= _SEND_ONE_MAX_ATTEMPTS:
                logger.error('Telegram send failed after %d network retries: %s',
                             net_attempts, e)
                raise
            logger.warning(
                'Telegram network error (attempt %d/%d); sleeping %.1fs before retry; content=%s',
                net_attempts, _SEND_ONE_MAX_ATTEMPTS, backoff, fingerprint)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, TG_SEND_RETRY_BACKOFF_CAP_SEC)


async def _send_one(bot, chat_id: int, text: str) -> None:
    """Send a single text message (signature unchanged by the Option-B extraction).

    The 429/bounded-network-retry/no-retry-on-BadRequest contract now lives in
    ``_attempt_send``, which media sends share (see its docstring).
    """
    await _attempt_send(lambda: bot.send_message(chat_id=chat_id, text=text),
                       'send', _content_fingerprint(text))


async def _send_chunks(bot, chat_id: int, text: str) -> int:
    """Send ``text`` through the unchanged chunking path; return the chunk count sent.

    One implementation for both the text-only path (``_safe_send``) and the media
    caption path (``_send_media_aware``), so the two cannot drift. Empty text sends
    nothing and returns 0 (an empty caption is never sent, §8 E13).
    """
    n_chunks = 0
    for part in chunk_text(text):
        await _send_one(bot, chat_id, part)
        n_chunks += 1
    return n_chunks


def _fmt_elapsed(seconds: float) -> str:
    """Render an elapsed duration for the "still working" notice.

    Pure function (unit-testable): <60s -> "45s", <1h -> "42m", else "1h 05m".
    Negative/zero values render as "0s".
    """
    secs = max(0, int(seconds))
    if secs < 60:
        return f'{secs}s'
    minutes, rem = divmod(secs, 60)
    if minutes < 60:
        return f'{minutes}m'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h {minutes:02d}m'


async def _run_waiter(ac: ACClient, bot, chat_id: int, cfg: BridgeConfig) -> None:
    """Wait for the current AC run to finish, then deliver the final message.

    A task timeout is NON-FATAL: it sends a "⏳ Still working" notice (one per
    ``task_timeout_sec`` interval) and keeps polling until AC finishes or the
    outer ceiling (``task_wait_ceiling_sec``, default TG_TASK_WAIT_CEILING_SEC)
    is reached. This way late completions still get their reply delivered.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    next_notify_at = start + cfg.task_timeout_sec
    # Surface "AC appears offline" a bit sooner than the full task timeout so the
    # user isn't left hanging if AC is down (still bounded by the task timeout).
    # Re-evaluated relative to each wait_for_completion call, so it naturally
    # re-arms per round: a transient blip won't kill us, sustained unreachability will.
    offline_after = min(TG_OFFLINE_AFTER_SEC, cfg.task_timeout_sec)

    while True:
        try:
            result = await wait_for_completion(
                ac, poll_interval=cfg.poll_interval_sec,
                timeout=max(0.1, next_notify_at - loop.time()),
                offline_after=offline_after,
            )
        except Exception as e:
            logger.error('waiter could not reach AC: %s', e)
            await _safe_send(bot, chat_id, '⚠️ Could not reach AC. Try again later.')
            return

        if result.status == WaiterResult.OFFLINE:
            await _safe_send(bot, chat_id, '⚠️ AC appears offline — I stopped waiting.')
            return

        if result.status == WaiterResult.FINISHED:
            # Final-answer delivery is now push-based at run end (see plans/tg-bridge-push-model_PLAN.md).
            # The waiter only provides progress/offline notices; it no longer fetches or sends the reply,
            # which is what caused N waiters to send the same answer N times.
            logger.debug('waiter: run finished; final answer delivered via push')
            return

        # result.status == TIMEOUT -> a "still working" checkpoint. If we've hit the
        # outer ceiling, stop here (the "gave up" message is the final notice — no
        # redundant "Still working" ping right before it). Otherwise notify and keep waiting.
        elapsed = loop.time() - start
        if elapsed >= cfg.task_wait_ceiling_sec:
            await _safe_send(
                bot, chat_id,
                '⌛ Reached the max wait ceiling; stopping here. Check the AC session for the result.',
            )
            return
        await _safe_send(
            bot, chat_id,
            f'⏳ Still working ({_fmt_elapsed(elapsed)}) — I\'ll keep waiting '
            'and send the reply when it\'s done.',
        )
        next_notify_at += cfg.task_timeout_sec  # next ping one task-timeout later


async def _send_one_media(bot, chat_id: int, kind: str, prepared, caption: Optional[str],
                          *, fingerprint: str) -> None:
    """Send one prepared media item through the matching PTB method.

    Mapping (plan §7.1): image -> ``send_photo`` (also for .gif/.webp; Telegram may
    restyle those as animations — the v1 fidelity caveat), audio -> ``send_audio``
    (NOT ``send_voice``: sendVoice needs audio/ogg <= 1MB while our producers emit
    mp3/wav/flac), video -> ``send_video`` (only when TG_MEDIA_SEND_VIDEO is on).

    ``prepared`` is whatever ``resolve_media_source`` returned — a path string, decoded
    bytes, or an http URL; all three are valid PTB ``FileInput`` values (PTB 22.8).
    Retries come from the shared ``_attempt_send``, so a permanently-invalid file
    cannot loop forever. The caption is truncated to Telegram's own limit (E25): an
    over-long alt would otherwise BadRequest and degrade a perfectly-sendable file.
    """
    cap_text = caption[:MessageLimit.CAPTION_LENGTH] if caption else None
    if kind == 'image':
        await _attempt_send(lambda: bot.send_photo(chat_id=chat_id, photo=prepared,
                                                  caption=cap_text), 'photo send', fingerprint)
    elif kind == 'audio':
        await _attempt_send(lambda: bot.send_audio(chat_id=chat_id, audio=prepared,
                                                  caption=cap_text), 'audio send', fingerprint)
    elif kind == 'video':
        await _attempt_send(lambda: bot.send_video(chat_id=chat_id, video=prepared,
                                                  caption=cap_text), 'video send', fingerprint)
    else:
        # Unreachable by construction (resolve/classify only yield the three kinds above).
        # Raising keeps the caller's E10 fallback + counters honest: a silent None would
        # count an unsent item as sent.
        raise ValueError(f'unknown media kind {kind!r}')


def _media_allowed_roots(extra_roots: Optional[List[str]] = None) -> List[str]:
    """Allowed roots for outbound media, read at CALL time.

    settings.TG_MEDIA_ALLOWED_ROOTS is the user-facing escape hatch (os.pathsep-separated)
    for media living outside the built-in [media_dir, DEFAULT_WORKSPACE] roots; ``extra_roots``
    carries the session's live extra work folders (supervisor N-1), which are mutated at
    runtime by config_handlers.py and must therefore never be cached.
    """
    roots = [r for r in (settings.TG_MEDIA_ALLOWED_ROOTS or '').split(os.pathsep) if r.strip()]
    if extra_roots:
        roots.extend(str(r) for r in extra_roots)
    return roots


class MediaSendResult(NamedTuple):
    """What ``_send_media_aware`` delivered, for ``_safe_send``'s [TG-PUSH] line.

    Named instead of a positional 4-tuple: these values ARE the log line's format
    arguments, so a field cannot be silently swapped with its neighbour.
    """
    n_chunks: int
    n_media: int
    n_fallback: int
    caption_text: str


async def _send_media_aware(bot, chat_id: int, text: str, items: List[MediaItem],
                            *, allowed_roots: Optional[List[str]] = None) -> MediaSendResult:
    """Deliver a media-bearing message: text first, then one send per item.

    Returns a ``MediaSendResult`` (chunk / media / text-fallback counts plus the delivered
    caption text) so ``_safe_send`` can log its [TG-PUSH] line in the existing format plus
    ', media=N'.

    Order (plan §7.3): the link-free remainder goes through the UNCHANGED chunking
    path first (a human sends "look at this: <photo>"), then items in document order.
    No album: mixed types and per-item captions rule ``send_media_group`` out for v1.

    Excision (plan §3/§12d): a span is removed ONLY for items that RESOLVED, and
    spans are excised in DESCENDING start-offset order so earlier offsets stay valid.
    Resolution therefore happens BEFORE the text send: an item that could not be
    resolved (missing / oversized / outside the allowlist / unreadable) keeps its
    original link inside the caption text, with no re-insertion afterwards.
    A payload that Telegram itself REJECTS (E10: wrong content type, photo geometry,
    dead URL) is a different case — it resolved, so its span is already gone from the
    caption; it is therefore re-sent as its own TEXT message (the ORIGINAL span) after
    the media loop, so the user keeps the link they would have got before this feature.
    Only a failure that could NOT happen to a plain text message gets that fallback
    (see the ``isinstance`` guard in the loop).
    """
    # Constant for one call, so computed once instead of per item. _media_allowed_roots
    # is still read HERE and never cached at import: the session's extra work folders
    # change at runtime (supervisor N-1).
    roots = _media_allowed_roots(allowed_roots)
    max_photo_bytes = settings.TG_MEDIA_MAX_PHOTO_BYTES
    max_other_bytes = settings.TG_MEDIA_MAX_OTHER_BYTES

    deliverable = []
    for item in items:
        outcome, payload = resolve_media_source(
            item, allowed_roots=roots,
            max_photo_bytes=max_photo_bytes, max_other_bytes=max_other_bytes)
        if outcome is None:
            # payload is the refusal reason here. The path (if any) was named by
            # resolve_media_source's own WARNING; the [TG-PUSH] line stays content-free.
            logger.warning('[TG-PUSH] media item not sent (%s, %s); its link stays in the text',
                           item.kind, payload)
            continue
        deliverable.append((item, payload))

    caption_text = text
    for item, _payload in sorted(deliverable, key=lambda pair: pair[0].span[0], reverse=True):
        start, end = item.span
        caption_text = caption_text[:start] + caption_text[end:]

    n_chunks = await _send_chunks(bot, chat_id, caption_text)   # the EXCISED text — never the raw text (that would re-send the links)

    n_sent = 0
    n_fallback = 0
    for item, payload in deliverable:
        try:
            await _send_one_media(bot, chat_id, item.kind, payload,
                                  caption_from_alt(item.alt),
                                  fingerprint=_content_fingerprint(
                                      f'{item.kind}:{item.source}:{item.value}'))
            n_sent += 1
        except Exception as e:
            # Per-ITEM isolation (plan §14): one item's failure must not stop the OTHER
            # items in the same message. The guarantee is scoped to items, not absolute:
            # a failing text CHUNK above aborts this loop (the message itself is down).
            logger.warning('Telegram media send failed for a %s (%s) item: %s',
                          item.kind, item.source, e)
            # §8 E10 fallback: re-send the ORIGINAL span so the link is not lost — but
            # only when a text send could actually succeed. PTB 22.8 hierarchy:
            # BadRequest is a permanent CONTENT rejection and, confusingly, IS a
            # NetworkError subclass — hence the explicit carve-out, it is exactly the
            # "Telegram rejected this payload" case E10 describes. Every OTHER TelegramError
            # is skipped: Forbidden / Conflict / InvalidToken / ChatMigrated etc. are
            # permanent and are NOT NetworkError, so a text message would be rejected the
            # same way (the old "not a NetworkError" test paid a doomed re-send plus a
            # second WARNING for a blocked or kicked bot); TimedOut is transient but its
            # bounded retry was already paid by _attempt_send, so a text re-send buys a
            # SECOND backoff chain, not a second chance. RetryAfter never reaches here —
            # _attempt_send loops on 429 until it heals. Only a non-Telegram exception
            # (bad FileInput, TypeError, ...) is worth re-sending as text.
            if not (isinstance(e, TelegramError) and not isinstance(e, BadRequest)):
                start, end = item.span
                try:
                    await _send_one(bot, chat_id, text[start:end])
                    n_fallback += 1
                except Exception as e2:
                    logger.warning('Telegram text fallback for a failed %s item also failed: %s',
                                   item.kind, e2)
    return MediaSendResult(n_chunks=n_chunks, n_media=n_sent, n_fallback=n_fallback,
                          caption_text=caption_text)


def _delivery_suffix(instance_name: Optional[str], run_generation: Optional[int]) -> str:
    """', inst=…, gen=…' for the [TG-PUSH] line — only when BOTH are provided.

    Callers that pass nothing keep their previous log shape (tg-dup v3 F3a contract,
    tests/test_tg_dup_stale_snapshot.py:168-169).
    """
    if instance_name is not None and run_generation is not None:
        return f', inst={instance_name}, gen={run_generation}'
    return ''


async def _safe_send(bot, chat_id: int, text: str,
                     instance_name: Optional[str] = None,
                     run_generation: Optional[int] = None, *,
                     allowed_roots: Optional[List[str]] = None) -> None:
    """Send ``text`` to Telegram safely: chunked, retried, never raising.

    Media-aware since todo.md:102: image/audio references in the text are delivered as
    real Telegram messages. The feature-flag gate lives HERE and only here, and the
    no-media case (the text-only call sites) takes the pre-existing path verbatim —
    ``split_media`` never edits the text, so byte-identical sends and logs are the
    contract, not an aspiration (plan §2.3, E1/E22).

    ``allowed_roots`` is the optional per-call extra-root list (supervisor N-1).
    """
    try:
        items = []
        if settings.TG_OUTBOUND_MEDIA:
            # The parser is only paid for when the feature is on: with the flag off the
            # text-only call sites keep the pre-change cost AND the pre-change failure
            # surface (a parser exception cannot suppress a send that predates the feature).
            _original, items = split_media(text,
                                           allow_video=settings.TG_MEDIA_SEND_VIDEO,
                                           detect_bare_paths=settings.TG_MEDIA_DETECT_BARE_PATHS)
        if not items:
            # chunk long answers (short messages are byte-identical: [text])
            n_chunks = await _send_chunks(bot, chat_id, text)
            # Success-side observability (tg-dup-delivery plan §4.2, extended by tg-dup v3 F3b):
            # chunk count and char length ONLY — no message content, so a push can never leak
            # into logs. The sha1[:8] fingerprint (same construction as the retry WARNING above)
            # makes a stale cross-run replay diagnosable; inst/gen appear only when BOTH are
            # provided, keeping P3/P4/bridge-internal lines unchanged in shape.
            fingerprint = _content_fingerprint(text)
            suffix = _delivery_suffix(instance_name, run_generation)
            logger.info('[TG-PUSH] delivered %d chunk(s), %d chars, fp=%s%s',
                        n_chunks, len(text), fingerprint, suffix)
            return

        n_chunks, n_media, n_fallback, caption_text = await _send_media_aware(
            bot, chat_id, text, items, allowed_roots=allowed_roots)
        # Same line format as the text path, plus ', media=N' (plan §12 item g). fp/chars
        # describe the caption text actually delivered — never message content, never paths.
        # ', fallback=M' appears ONLY when Telegram rejected a resolved media item and
        # its original link was re-sent as text (E10) — the normal media line stays exactly
        # as the plan specifies.
        fingerprint = _content_fingerprint(caption_text)
        suffix = _delivery_suffix(instance_name, run_generation)
        fallback_suffix = f', fallback={n_fallback}' if n_fallback else ''
        logger.info('[TG-PUSH] delivered %d chunk(s), %d chars, fp=%s, media=%d%s%s',
                    n_chunks, len(caption_text), fingerprint, n_media, fallback_suffix, suffix)
    except Exception as e:
        logger.error('failed to send notification to Telegram: %s', e)


async def on_polling_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ANN001 (PTB callback sig)
    """Error handler for polling-side exceptions forwarded by ``run_polling``.

    PTB's updater retries transient network errors (stale connection, a 502 from
    Telegram's edge, etc.) internally via its own ``network_retry_loop`` BEFORE the
    error reaches this callback — so a ``NetworkError``/``TimedOut`` here is almost
    always an already-recovered blip. Without a registered handler, PTB falls back to
    ``process_error``'s catch-all and logs a full WARNING + multi-line traceback for
    what is expected, self-healing noise. We log one concise line (DEBUG for the
    transient network class, WARNING otherwise) and swallow it: nothing to do, and
    if a failure is persistent enough to actually break ``run_polling``, the
    supervisor's bounded-restart loop catches that separately.
    """
    err = context.error
    if isinstance(err, (NetworkError, RetryAfter)):
        # Transient / already-retried-by-PTB — keep it quiet.
        logger.debug('Telegram polling transient error (self-healed by PTB retry): %s', err)
    else:
        logger.warning('Telegram polling error: %r', err)


def _pick_image_file(update: Update) -> Tuple[Optional[object], Optional[str]]:
    """Return (file_obj, mime) for an inbound image, or (None, None).

    Photos: PTB exposes ``message.photo`` as a list of ``PhotoSize`` in ascending
    resolution — take the largest (last). Arbitrary files (incl. images sent as
    files) arrive as ``message.document``; only ``image/*`` mime types qualify.
    Anything else (video, non-image docs, plain text) is not an image we can
    forward, so returns (None, None) and the caller falls back to text handling.
    """
    photo = getattr(update.message, 'photo', None)
    if photo:
        return photo[-1], 'image/jpeg'
    doc = getattr(update.message, 'document', None)
    if doc and str(getattr(doc, 'mime_type', '')).startswith('image/'):
        return doc, doc.mime_type  # guard already proved mime_type is a non-empty image/* value
    return None, None


async def _compose_media_text(update: Update, context, chat_id, file_obj, mime: str) -> Optional[str]:
    """Download an inbound image and compose the markdown data-URI the AC multimodal
    parser expects, prefixed with the user's caption if present.

    Returns the composed string, or ``None`` when the image was already handled-and-
    rejected (too large / download failure) — those send their own notice via
    ``_safe_send`` and return ``None`` so the caller can stop. Kept separate from
    ``on_message`` so the handler stays about flow, not byte-munging.
    """
    if getattr(file_obj, 'file_size', 0) > _TG_MAX_DOWNLOAD_BYTES:
        await _safe_send(context.bot, chat_id,
                         f"⚠️ Image too large (max {_TG_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB).")
        return None
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        data = await tg_file.download_as_bytearray()
    except (TelegramError, asyncio.TimeoutError) as e:  # whole PTB error family (NetworkError/BadRequest/RetryAfter/Forbidden/Conflict/... all subclass TelegramError) so a failed download never crashes the handler or silently drops the image
        logger.error('get_file/download failed for inbound image: %s', e)
        await _safe_send(context.bot, chat_id, '⚠️ Could not download image.')
        return None
    b64 = base64.b64encode(bytes(data)).decode('ascii')
    image_md = f"![image](data:{mime};base64,{b64})"
    caption = (getattr(update.message, 'caption', None) or '').strip()
    return f"{caption}\n{image_md}" if caption else image_md


async def on_message(update: Update, context) -> None:  # noqa: ANN001 (PTB callback sig)
    """Message handler: auth gate -> inject -> ack -> spawn waiter."""
    cfg: BridgeConfig = context.bot_data['config']
    ac: ACClient = context.bot_data['ac_client']
    user_id = update.effective_user.id if update.effective_user else None

    # Auth gate: ignore non-allowlisted users entirely (no reply, no AC call).
    if not cfg.is_allowed(user_id):
        logger.warning('Ignoring message from non-allowlisted user id=%s', user_id)
        return

    chat_id = update.effective_chat.id

    # Media branch: forward inbound photos / image documents as a markdown data-URI
    # that the existing multimodal pipeline (_parse_multimodal_content on the AC
    # side) already understands. This runs BEFORE the empty-text early-return so a
    # photo with no caption is NOT dropped; the composed string then flows through
    # the unchanged dedup / slash-command / inject / ack / waiter path below.
    text = (update.message.text or '').strip()
    file_obj, mime = _pick_image_file(update)
    is_image = file_obj is not None
    if is_image:
        composed = await _compose_media_text(update, context, chat_id, file_obj, mime)
        if composed is None:
            return  # helper already sent its own notice (too-large / download-failure)
        text = composed
    elif not text:
        return  # empty / whitespace-only, no media -> ignore

    context.bot_data['last_chat_id'] = chat_id

    # Idempotency on the inbound event. A redelivered update (PTB long-poll
    # reconnect, sleep-wake, NAT expiry) must not start a second AC run, or the
    # per-run push model delivers the answer N times.
    tg_id = getattr(update, 'update_id', None)
    key_kind = 'update_id'
    if tg_id is None:
        # Fallback key: message_id is unique PER CHAT, not globally (BUG_0023),
        # so namespace it by chat_id to keep it a valid dedup key in multi-user
        # setups. update_id stays a bare int — it IS globally unique per bot.
        _msg = getattr(update, 'effective_message', None)
        tg_id = getattr(_msg, 'message_id', None) if _msg is not None else None
        key_kind = 'message_id'

    if tg_id is None:
        # No usable key — proceed rather than risk dropping a real message.
        logger.debug('TG update has no update_id/message_id; skipping inbound dedup')
    elif key_kind == 'message_id':
        _key = (chat_id, tg_id)
        if _key in _SEEN_TG_IDS:
            # WARNING (not DEBUG): a false-positive drop must be auditable from
            # production logs — the id + chat + key kind say which message vanished.
            logger.warning('ignoring duplicate Telegram event id=%s (message_id fallback, chat_id=%s)', tg_id, chat_id)
            return
        _SEEN_TG_IDS.add(_key)
    else:
        if tg_id in _SEEN_TG_IDS:
            logger.warning('ignoring duplicate Telegram event id=%s (update_id)', tg_id)
            return
        _SEEN_TG_IDS.add(tg_id)
    if len(_SEEN_TG_IDS) > _SEEN_TG_IDS_MAX:   # Bounded size; clear all on overflow to prevent unbounded growth.
        _SEEN_TG_IDS.clear()

    # System-command interception (Phase 2): registered slash-commands are handled
    # locally / via AC REST endpoints and answered directly. They NEVER reach the
    # agent — this early return guarantees ac.inject_message is not called for them,
    # so they are never logged as agent messages. Unregistered '/...' commands and
    # plain text both fall through to inject_message below (so AC-side slash commands
    # like /compress x reach the agent). The `is_image` guard means an inbound
    # image is never treated as a command — a photo captioned '/...' must forward the
    # image, not dispatch a command whose args would be polluted by the data-URI.
    if not is_image and text.startswith('/'):
        reply = await dispatch_command(text, ac, cfg)
        if reply is not None:
            await _safe_send(context.bot, chat_id, reply)
            return

    try:
        result = await ac.inject_message(text, target=cfg.target_agent)
    except ACError as e:
        logger.error('inject_message failed: %s', e)
        await _safe_send(context.bot, chat_id, '⚠️ Could not reach AC. Is it running?')
        return

    target = result.get('target', cfg.target_agent) if isinstance(result, dict) else cfg.target_agent
    await _safe_send(context.bot, chat_id, f"🏃 Started → {target}. I'll reply when done.")

    # Fire-and-forget waiter; tracked so we can cancel it on shutdown.
    task = asyncio.create_task(_run_waiter(ac, context.bot, chat_id, cfg))
    context.bot_data.setdefault('waiters', set()).add(task)
    task.add_done_callback(context.bot_data['waiters'].discard)


def build_application(cfg: BridgeConfig, ac: ACClient):
    """Build the PTB Application wired to our handler.

    ``allowed_updates`` (set at the polling entry point, not here) is ``['message']``,
    which per the Bot API already delivers "any kind - text, photo, sticker, etc." —
    so photo/document updates reach this process. The handler filter is widened to
    ``TEXT | PHOTO | Document.ALL`` so ``on_message`` can forward inbound images as a
    markdown data-URI into the existing multimodal pipeline; TEXT stays included so
    slash-commands remain reachable via the COMMANDS registry.
    The built-in rate limiter is left at its default (PTB enables it automatically).

    A post_shutdown coroutine is registered (via the builder) so that on SIGINT/
    SIGTERM any outstanding waiters are cancelled and the AC client is closed. PTB
    22.x exposes this as a single ``post_shutdown`` coroutine on the builder — there
    is no ``add_post_shutdown_task`` method in this version.
    """
    from telegram.ext import ApplicationBuilder, MessageHandler, filters

    async def _post_shutdown(_app):
        for task in list(_app.bot_data.get('waiters', ()) or ()):
            task.cancel()
        await ac.close()
        logger.info('Telegram bridge shut down cleanly')

    app = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data['config'] = cfg
    app.bot_data['ac_client'] = ac
    # NOTE: TEXT | PHOTO | Document.ALL (NOT `~filters.COMMAND`) — Phase 2 intercepts
    # slash-commands inside on_message via the COMMANDS registry, so TEXT must stay
    # included (excluding COMMAND would make /stop & co. unreachable dead code). PHOTO
    # and Document.ALL are added so inbound photos / image files reach on_message too;
    # on_message composes them into a markdown data-URI and reuses the same flow.
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL, on_message))
    # Register a polling error handler so transient network errors (e.g. a 502 from
    # Telegram's edge) log one quiet line instead of PTB's default WARNING + full
    # traceback ("No error handlers are registered"). See on_polling_error.
    app.add_error_handler(on_polling_error)
    return app
