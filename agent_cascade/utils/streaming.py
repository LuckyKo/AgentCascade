"""Streaming timeout utilities for AgentCascade."""

import queue
import threading
import time
from typing import Iterator, TypeVar

T = TypeVar('T')

_SENTINEL = object()


class _Err:
    """Wrapper so a reader-thread exception is distinguishable from a stream item."""
    __slots__ = ('exc',)

    def __init__(self, exc):
        self.exc = exc


class StreamStalledError(RuntimeError):
    """Raised by :func:`watch_stream` when the silence or total-duration guard trips.

    BUG_0028: a dedicated subclass so backends can catch ONLY the watchdog timeout
    (and classify it retryable) instead of matching on the broad ``RuntimeError``
    type, which would also wrap unrelated programming errors raised inside
    stream processing and mask them behind a generic retryable wrapper.
    """


def watch_stream(
    stream: Iterator[T],
    max_silence_seconds: float,
    max_total_seconds: float,
    error_message_prefix: str = '',
) -> Iterator[T]:
    """Wrap a streaming iterator with enforceable silence and total-duration timeout guards.

    A daemon reader thread pulls items from ``stream`` into a bounded queue (maxsize=1);
    the consumer loop enforces deadlines on each ``queue.get()`` so the guards are
    authoritative even when the underlying transport blocks inside ``next()`` (e.g. an
    httpx socket read or a server that heartbeats without producing events).

    Precedence, evaluated per wait:
      1. ``max_total_seconds`` — always active from stream start; wins whenever the
         remaining total budget is the smaller of the two.
      2. ``max_silence_seconds`` — active only AFTER the first item has been received.
         The pre-first-item (TTFB) window is governed by the total limit alone, so slow
         reasoning models can still take a long time to produce their first token.

    Residual risk (documented): on a watchdog trip or forwarded exception the reader may
    still be blocked inside the underlying iterator's ``next()`` until the transport-level
    ``AGENT_CASCADE_HTTP_READ_TIMEOUT`` fires (or the server closes the connection) — a
    blocked read cannot be interrupted from here. It can NO LONGER be parked indefinitely
    in ``q.put`` once the consumer stops draining: ``_closed`` is set and the reader
    abandons its pending put (BUG_0318 D1). Such daemon threads never block process exit.

    Args:
        stream: The underlying iterator to wrap.
        max_silence_seconds: Max seconds between consecutive items before raising.
        max_total_seconds: Max total duration of the stream before raising.
        error_message_prefix: Optional prefix for error messages (e.g., backend name).

    Raises:
        StreamStalledError: On silence timeout or total timeout. Caller should wrap in
            ModelServiceError if needed. (Subclass of RuntimeError for backwards
            compatibility with existing broad catches — BUG_0028.)
        Any exception raised by the underlying iterator is re-raised unchanged
            (original type preserved) so existing ``except`` arms keep firing.
    """
    prefix = f"{error_message_prefix}: " if error_message_prefix else ''
    q: queue.Queue = queue.Queue(maxsize=1)
    # _closed is set by the consumer (generator finally) the moment it stops draining —
    # normal end, watchdog trip, forwarded error, or generator close/GC. The reader polls
    # it so an ORPHANED reader never parks forever on a full queue: the documented BUG_0026
    # "residual risk" (thread held until the socket read-timeout) is cut short (BUG_0318 D1).
    # threading.Event.set() is idempotent + thread-safe, so there is no double-stop hazard.
    _closed = threading.Event()
    _PUT_POLL = 0.1  # max block before re-checking _closed on a backpressured put

    def _put(item) -> None:
        # Backpressure put that yields to abandonment: delivers as soon as the queue has
        # room (immediate for a draining consumer → zero added latency on the hot path),
        # and gives up once the consumer has stopped, so the daemon reader can exit.
        while not _closed.is_set():
            try:
                q.put(item, timeout=_PUT_POLL)
                return
            except queue.Full:
                continue

    def _reader():
        try:
            for item in stream:
                _put(item)
                if _closed.is_set():       # consumer stopped while we still had items
                    return
        except BaseException as exc:              # noqa: BLE001 - must forward everything
            _put(_Err(exc))
        finally:
            _put(_SENTINEL)                       # skipped only if consumer already gone

    threading.Thread(target=_reader, daemon=True, name='watch_stream-reader').start()

    stream_start = time.monotonic()
    last_item_time = None
    try:
        while True:
            now = time.monotonic()
            total_left = max_total_seconds - (now - stream_start)
            # Before the first item only the TOTAL limit applies (TTFB exemption).
            budget = total_left if last_item_time is None else min(max_silence_seconds, total_left)
            try:
                item = q.get(timeout=max(budget, 0.01))
            except queue.Empty:
                is_total = last_item_time is None or total_left <= max_silence_seconds
                kind = 'total' if is_total else 'silence'
                limit = max_total_seconds if is_total else max_silence_seconds
                silence = (now - last_item_time) if last_item_time is not None else (now - stream_start)
                # Drain any pending error BEFORE raising the stall: the reader may have put an
                # _Err into the queue moments before the timeout fired (e.g. a transport error
                # arriving at the same instant the watchdog budget expired). Without this, the
                # original exception is silently lost and the caller sees a generic retryable
                # stream_stalled instead of the real cause.
                try:
                    pending = q.get_nowait()
                    if isinstance(pending, _Err):
                        raise pending.exc
                except queue.Empty:
                    pass
                raise StreamStalledError(
                    f"{prefix}stream_stalled: no data for {silence:.1f}s ({kind} limit={limit:.1f}s)")
            # Dispatch order is load-bearing: _Err BEFORE _SENTINEL. The reader's finally
            # enqueues the sentinel AFTER an error, so checking the sentinel first would
            # report a clean end-of-stream on a failed stream.
            if isinstance(item, _Err):
                raise item.exc
            if item is _SENTINEL:
                return
            last_item_time = time.monotonic()
            yield item
    finally:
        # Any exit path (normal end, watchdog trip, forwarded error, generator close/GC)
        # signals the reader to stop its backpressured put so it can exit promptly.
        _closed.set()
