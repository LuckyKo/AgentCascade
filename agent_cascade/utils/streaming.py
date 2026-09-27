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

    Residual risk (documented, accepted): on a watchdog trip or a forwarded exception,
    the reader thread may remain blocked in its socket read until the transport-level
    ``AGENT_CASCADE_HTTP_READ_TIMEOUT`` fires (or the server closes the connection).
    It may also be parked in a blocking ``q.put`` if the consumer stops draining (backpressure).
    Either way the thread is a daemon, so it never blocks process exit; the leak is bounded by
    concurrent stalled streams.

    Args:
        stream: The underlying iterator to wrap.
        max_silence_seconds: Max seconds between consecutive items before raising.
        max_total_seconds: Max total duration of the stream before raising.
        error_message_prefix: Optional prefix for error messages (e.g., backend name).

    Raises:
        RuntimeError: On silence timeout or total timeout. Caller should wrap in
            ModelServiceError if needed.
        Any exception raised by the underlying iterator is re-raised unchanged
            (original type preserved) so existing ``except`` arms keep firing.
    """
    prefix = f"{error_message_prefix}: " if error_message_prefix else ''
    q: queue.Queue = queue.Queue(maxsize=1)

    def _reader():
        try:
            for item in stream:
                q.put(item)                       # blocking put = backpressure
        except BaseException as exc:              # noqa: BLE001 - must forward everything
            q.put(_Err(exc))
        finally:
            q.put(_SENTINEL)                      # blocking: ALWAYS delivered, never dropped

    threading.Thread(target=_reader, daemon=True, name='watch_stream-reader').start()

    stream_start = time.monotonic()
    last_item_time = None
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
            raise RuntimeError(
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
