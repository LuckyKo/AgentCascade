"""PTB Application wiring for the v1 Telegram bridge.

Receiving path (per incoming text message from the allowlisted user):
    1. Auth gate — non-allowlisted users are ignored (no reply, no AC call).
    2. Inject the task into AC via /api/message (handshake cached in ACClient).
    3. Reply once with a short ack ("🏃 Started").
    4. Spawn a waiter coroutine that polls /api/status until generation ends,
       then sends the root agent's final assistant message (chunked if >4096).
       A task timeout is NON-FATAL: it sends a "⏳ Still working" notice and keeps
       polling (one notice per task-timeout interval) until AC finishes or the
       outer ceiling (TG_TASK_WAIT_CEILING_SEC) is hit — so late completions still
       get their reply delivered.

Sending path helpers: ``chunk_text`` splits text into <=4096-char parts on line
boundaries; ``send_chunked`` sends them sequentially and honors 429 retry_after.
"""

import asyncio
from typing import List, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter

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
from .waiter import WaiterResult, wait_for_completion, fetch_final_message

# Telegram's hard limit is 4096 chars/message. Chunk at exactly that so we only
# split when a single reply genuinely exceeds the limit (per v1 spec). The value
# lives in settings (TG_MAX_MESSAGE_LEN) so it's tunable/overridable in one place.
CHUNK_SIZE = TG_MAX_MESSAGE_LEN


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


async def send_chunked(bot, chat_id: int, text: str) -> None:
    """Send ``text`` to a chat as one or more messages (each <=4096), honoring 429."""
    for part in chunk_text(text):
        await _send_one(bot, chat_id, part)


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


async def _send_one(bot, chat_id: int, text: str) -> None:
    """Send a single message, retrying on Telegram 429 per its retry_after."""
    backoff = TG_SEND_RETRY_BACKOFF_BASE_SEC
    while True:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return
        except RetryAfter as e:
            wait = _retry_after_seconds(e, backoff)
            logger.warning('Telegram 429; sleeping %.1fs before retry', wait)
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, TG_SEND_RETRY_BACKOFF_CAP_SEC)
        except BadRequest as e:
            # E.g. message too long despite chunking (shouldn't happen). Surface it.
            logger.error('Telegram send failed (BadRequest): %s', e)
            raise


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
            try:
                final_text = await fetch_final_message(ac)
            except Exception as e:
                logger.error('waiter failed to read final message: %s', e)
                await _safe_send(bot, chat_id, "⚠️ AC finished but I couldn't read its reply.")
                return

            if not final_text.strip():
                await _safe_send(bot, chat_id, '✅ Done (AC produced no text reply).')
                return

            await send_chunked(bot, chat_id, final_text)
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


async def _safe_send(bot, chat_id: int, text: str) -> None:
    try:
        await _send_one(bot, chat_id, text)
    except Exception as e:
        logger.error('failed to send notification to Telegram: %s', e)


async def on_message(update: Update, context) -> None:  # noqa: ANN001 (PTB callback sig)
    """Message handler: auth gate -> inject -> ack -> spawn waiter."""
    cfg: BridgeConfig = context.bot_data['config']
    ac: ACClient = context.bot_data['ac_client']
    user_id = update.effective_user.id if update.effective_user else None

    # Auth gate: ignore non-allowlisted users entirely (no reply, no AC call).
    if not cfg.is_allowed(user_id):
        logger.warning('Ignoring message from non-allowlisted user id=%s', user_id)
        return

    text = (update.message.text or '').strip()
    if not text:
        return  # empty / whitespace-only -> ignore

    chat_id = update.effective_chat.id

    # System-command interception (Phase 2): registered slash-commands are handled
    # locally / via AC REST endpoints and answered directly. They NEVER reach the
    # agent — this early return guarantees ac.inject_message is not called for them,
    # so they are never logged as agent messages. Unregistered '/...' commands and
    # plain text both fall through to inject_message below (so AC-side slash commands
    # like /compress x reach the agent).
    if text.startswith('/'):
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

    ``allowed_updates`` is restricted to plain text messages (v1 is direct 1:1).
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
    # NOTE: plain filters.TEXT (NOT `~filters.COMMAND`) — Phase 2 intercepts slash-
    # commands inside on_message via the COMMANDS registry. Excluding COMMAND here
    # would make /stop & co. unreachable dead code.
    app.add_handler(MessageHandler(filters.TEXT, on_message))
    return app
