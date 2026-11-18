"""Streaming timeout utilities for AgentCascade."""

import contextlib
import contextvars
import queue
import threading
import time
from typing import Iterator, Optional, TypeVar

T = TypeVar('T')

_SENTINEL = object()

# BUG_0344 (Fix A): per-call absolute wall-clock deadline, in ``time.monotonic()``
# seconds, that a caller may impose on the stream TOTAL-duration guard. A caller
# that knows its own remaining budget (the compressor's COMPRESSION_AGENT_TIMEOUT
# window) sets this so watch_stream's existing no-chunk-required total guard
# (:func:`watch_stream`) fires INSIDE that window while the reader is still blocked
# awaiting the FIRST token — the one case neither the yield-gated caller timeout
# (compression/agent_invoker) nor the yield-gated engine watchdog (engine/llm_call)
# can cover. The deadline only ever LOWERS the effective ``max_total_seconds``
# (never raises it); when unset the guard behaves exactly as before. A ContextVar,
# not a parameter, so the budget threads down through the lazy generator chain
# (engine.run -> llm_call -> backend ``_chat_stream``) WITHOUT editing every backend
# call site. Readers that start on this thread (the consumer side of watch_stream,
# which is where this value is read) see it; the daemon reader thread does not need
# it. Reset is guaranteed by :func:`stream_total_deadline`.
_STREAM_TOTAL_DEADLINE: 'contextvars.ContextVar[Optional[float]]' = contextvars.ContextVar(
    'stream_total_deadline', default=None)

# BUG_0344 (Fix B1): the reader thread's completion signal. ``watch_stream`` starts a
# daemon reader that pulls from the backend iterator; when that reader's finally runs the
# underlying iterator has truly ended — i.e. a parked-in-next() orphan can no longer
# overlap the endpoint. ``watch_stream`` sets this ContextVar to a fresh Event on the
# CONSUMER thread (so the run-loop's same-thread finally can read it) and the reader's
# finally sets the Event. The deferred drop-exit release waits on this Event instead of a
# blind sleep, and it is attached to the instance so the cross-thread reuse gate sees the
# same signal. ``reset_stream_reader_done`` scopes it per run.
_STREAM_READER_DONE: 'contextvars.ContextVar[Optional[threading.Event]]' = contextvars.ContextVar(
    'stream_reader_done', default=None)


def current_stream_reader_done() -> Optional[threading.Event]:
    """The completion Event of the most recent stream created on this thread, or None."""
    return _STREAM_READER_DONE.get()


def reset_stream_reader_done() -> None:
    """Clear the per-thread reader-done signal; call at the start of each run."""
    _STREAM_READER_DONE.set(None)


def stream_reader_in_flight(reader_done: Optional[threading.Event]) -> bool:
    """True while a published reader-completion Event is present and not yet set.

    Single definition of "the most recent LLM stream's daemon reader is still reading the
    endpoint", shared by the run-loop defer decision (``_should_defer_drop_exit``) and the
    cross-thread reuse gate (Gate 4 / Gate 5 in ``system_agent``). ``None`` (no stream opened
    this run) and a set Event (reader ended) are both False, so genuine leaked permits and
    normally-quiesced exits stay inert — only a live, abandoned, still-parked reader is True.
    """
    return reader_done is not None and not reader_done.is_set()


def instance_llm_in_flight(inst) -> bool:
    """Single authoritative predicate: is this instance's last LLM stream still live?

    The reader-completion Event (``inst._stream_reader_done``) wins whenever it is present:
    an unset Event means a daemon reader is still parked on the endpoint (the instance must
    not be reused/reclaimed); a set Event means the reader finished, which overrides a stale
    ``_llm_call_active`` (that flag can stick True forever on an abandoned stream, so it must
    never be the sole authority once a completed reader contradicts it). Only when NO reader
    Event was published this run do we fall back to ``_llm_call_active`` as defence in depth.

    Every reuse-gate read of "is the endpoint busy" goes through this predicate (Gate 4's
    reclaim-skip and Gate 5's reject in ``system_agent``) so the three raw signals
    (``_stream_reader_done``, ``_llm_call_active``) are never read ad hoc at a gate. The
    deferred drop-exit release owner is the only writer that clears them.
    """
    reader_done = getattr(inst, '_stream_reader_done', None)
    if reader_done is not None:
        return not reader_done.is_set()
    return bool(getattr(inst, '_llm_call_active', False))


@contextlib.contextmanager
def stream_total_deadline(deadline_monotonic: Optional[float]):
    """Scope an absolute ``time.monotonic()`` deadline onto the current call path.

    Inside the ``with`` block, :func:`watch_stream` clamps its ``max_total_seconds``
    down to the time left until ``deadline_monotonic``. Pass ``None`` to disable (an
    already-expired ``deadline_monotonic`` floors to a prompt trip inside watch_stream —
    it does not disable the guard). The value is reset on exit, so a raised stream or
    timeout can never leak the deadline onto a later stream sharing the thread.
    """
    if deadline_monotonic is None:
        yield
        return
    token = _STREAM_TOTAL_DEADLINE.set(deadline_monotonic)
    try:
        yield
    finally:
        _STREAM_TOTAL_DEADLINE.reset(token)


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
    # BUG_0344 (Fix A): a caller on this thread may have scoped an absolute
    # monotonic deadline (see stream_total_deadline). Clamp the total guard DOWN to
    # the time left so a first-token stall is cut off inside the caller's budget
    # instead of waiting out the raw max_total_seconds default. Never raises the
    # limit; inert when no deadline is set. An already-expired deadline floors to a
    # prompt trip rather than disabling the guard.
    _deadline = _STREAM_TOTAL_DEADLINE.get()
    if _deadline is not None:
        _left = _deadline - time.monotonic()
        if _left < max_total_seconds:
            max_total_seconds = max(_left, 0.01)
    q: queue.Queue = queue.Queue(maxsize=1)
    # _closed is set by the consumer (generator finally) the moment it stops draining —
    # normal end, watchdog trip, forwarded error, or generator close/GC. The reader polls
    # it so an ORPHANED reader never parks forever on a full queue: the documented BUG_0026
    # "residual risk" (thread held until the socket read-timeout) is cut short (BUG_0318 D1).
    # threading.Event.set() is idempotent + thread-safe, so there is no double-stop hazard.
    _closed = threading.Event()
    _PUT_POLL = 0.1  # max block before re-checking _closed on a backpressured put
    # BUG_0344 (Fix B1): set by the reader's finally when the underlying iterator has
    # truly ended. The deferred drop-exit release waits on this instead of blind-sleeping.
    reader_done = threading.Event()

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
            # BUG_0344 (Fix B1): signal that the daemon reader has finished unwinding —
            # the underlying iterator has ended, so a previously parked read can no
            # longer overlap the endpoint. Idempotent + thread-safe. This is the event the
            # deferred drop-exit release waits on (NOT _llm_call_active, which llm_call's
            # retry handler clears when Fix A raises StreamStalledError).
            reader_done.set()

    # BUG_0344 (Fix B1): publish the reader-completion Event on this (consumer) thread
    # BEFORE starting the reader, so the run-loop's same-thread finally can read it.
    _STREAM_READER_DONE.set(reader_done)
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
