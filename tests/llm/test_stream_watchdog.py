"""Tests for the BUG_0026 stream watchdog (reader-thread + bounded queue in watch_stream).

Covers the failure modes that tests/test_streaming_timeout.py cannot express with plain
generators: a mid-next() stall (the reader thread blocks inside next(), like a stalled
socket read), reader-exception propagation, TTFB exemption under the new design, and the
full-path integration (llm.chat → _chat_stream → real watch_stream → ModelServiceError).

Hermetic: no network. The fakes block on a threading.Event that is never set; the blocking
happens inside watch_stream's daemon reader thread, so the test process still exits
promptly. Whole file runs in < 30s.
"""

import threading
import time

import httpx
import pytest

from agent_cascade.llm.base import ModelServiceError
from agent_cascade.retry_policy import classify_error
from agent_cascade.utils.streaming import watch_stream


# ---------------------------------------------------------------------------
# Hermetic fakes
# ---------------------------------------------------------------------------


class _StallingIter:
    """Yields `n` items, then blocks on a threading.Event that is never set.

    Mimics next() blocked in a socket read (e.g. httpx iter_bytes on a stalled
    connection). The block happens inside watch_stream's daemon reader thread.
    """

    def __init__(self, n, delay=0.0):
        self.n = n
        self.delay = delay
        self._never_set = threading.Event()

    def __iter__(self):
        for i in range(self.n):
            if self.delay:
                time.sleep(self.delay)
            yield f'chunk{i}'
        self._never_set.wait()  # blocks forever, like a stalled socket


class _RaisingIter:
    """Yields `n` items, then raises `exc` (mimics a mid-stream transport error)."""

    def __init__(self, n, exc):
        self.n = n
        self.exc = exc

    def __iter__(self):
        for i in range(self.n):
            yield f'chunk{i}'
        raise self.exc


class _SlowFirstIter:
    """Sleeps `delay` before the first item, then yields one item and ends."""

    def __init__(self, delay, n=1):
        self.delay = delay
        self.n = n

    def __iter__(self):
        time.sleep(self.delay)
        for i in range(self.n):
            yield f'chunk{i}'


class _EndlessSlowIter:
    """Yields one item every `interval` seconds, forever."""

    def __init__(self, interval):
        self.interval = interval
        self._stop = threading.Event()

    def __iter__(self):
        i = 0
        while not self._stop.is_set():
            yield f'chunk{i}'
            i += 1
            self._stop.wait(self.interval)


# ---------------------------------------------------------------------------
# watch_stream unit tests (new failure modes)
# ---------------------------------------------------------------------------


def test_stall_after_n_chunks_trips_silence_watchdog():
    """2 items then indefinite block -> silence watchdog trips with stream_stalled."""
    start = time.monotonic()
    with pytest.raises(RuntimeError, match='stream_stalled'):
        list(watch_stream(_StallingIter(2), max_silence_seconds=0.5, max_total_seconds=30.0))
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f'watchdog took too long: {elapsed:.1f}s'


def test_never_yielding_stream_trips_total_watchdog():
    """0 items, blocks forever -> total watchdog trips (message names the total limit)."""
    start = time.monotonic()
    with pytest.raises(RuntimeError, match='stream_stalled.*total'):
        list(watch_stream(_StallingIter(0), max_silence_seconds=1e9, max_total_seconds=0.5))
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f'watchdog took too long: {elapsed:.1f}s'


def test_healthy_stream_passes_through_unchanged():
    """5 fast items -> all yielded in order, no exception, reader thread exits (no leak)."""
    # Snapshot threads BEFORE this test's watch_stream call so we only check for NEW leaks.
    # Other tests may have left daemon reader threads alive (bounded by HTTP_READ_TIMEOUT).
    before = set(t.ident for t in threading.enumerate() if t.name == 'watch_stream-reader')
    result = list(watch_stream(iter(range(5)), max_silence_seconds=10.0, max_total_seconds=60.0))
    assert result == [0, 1, 2, 3, 4]
    # The reader thread is a daemon that exits after the sentinel; give it a moment to die.
    time.sleep(0.2)
    after = set(t.ident for t in threading.enumerate() if t.name == 'watch_stream-reader')
    new_leaks = after - before
    assert not new_leaks, f'this test leaked reader thread(s): {new_leaks}'


def test_reader_exception_propagates_with_original_type():
    """Iterator raises httpx.ReadTimeout after 1 item -> consumer sees the original type."""
    items = []
    with pytest.raises(httpx.ReadTimeout):
        for item in watch_stream(_RaisingIter(1, httpx.ReadTimeout('timed out')),
                                 max_silence_seconds=10.0, max_total_seconds=60.0):
            items.append(item)
    assert items == ['chunk0']


def test_silence_limit_not_applied_before_first_item():
    """0.6s delay before first item with max_silence=0.5 but max_total=30 -> no error (TTFB exemption)."""
    result = list(watch_stream(_SlowFirstIter(0.6), max_silence_seconds=0.5, max_total_seconds=30.0))
    assert result == ['chunk0']


def test_total_limit_wins_when_smaller():
    """Item every 0.1s forever with max_silence=1e9, max_total=0.5 -> trips on TOTAL, not silence."""
    stream = _EndlessSlowIter(0.1)
    start = time.monotonic()
    with pytest.raises(RuntimeError, match='stream_stalled.*total'):
        list(watch_stream(stream, max_silence_seconds=1e9, max_total_seconds=0.5))
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f'watchdog took too long: {elapsed:.1f}s'
    stream._stop.set()
    time.sleep(0.1)  # let the (daemon) reader thread unwind before the next test


def test_slow_consumer_does_not_buffer_whole_response():
    """maxsize=1 backpressure: a slow consumer still receives all items in order."""
    def gen():
        for i in range(5):
            yield f'chunk{i}'

    def slow_consumer(it):
        out = []
        for item in it:
            time.sleep(0.05)  # consume slower than the reader produces
            out.append(item)
        return out

    result = slow_consumer(watch_stream(gen(), max_silence_seconds=10.0, max_total_seconds=60.0))
    assert result == [f'chunk{i}' for i in range(5)]


# ---------------------------------------------------------------------------
# Full-path integration: llm.chat → _chat_stream → watch_stream (real watchdog)
# ---------------------------------------------------------------------------
# oai.py:566 imports STREAM_MAX_SILENCE_SECONDS / STREAM_MAX_TOTAL_SECONDS INSIDE
# the function body, so we must patch the agent_cascade.settings module attributes
# (not names in agent_cascade.llm.oai, which don't exist there). The base.py wrapper
# chain (_postprocess_messages_iterator → _format_and_cache →
# _convert_messages_iterator_to_target_type) is pure pass-through generators and
# cannot starve the watchdog.


class _SSE:
    """Mimics an httpx SSE event: .data and .json()."""

    def __init__(self, data):
        self.data = data

    def json(self):
        return {'choices': [{'delta': {'content': 'ok'}, 'finish_reason': None}]}


class _Client:
    @staticmethod
    def _process_response_data(data, cast_to=None, response=None):
        class _Delta:
            content = 'ok'
            tool_calls = None

        class _Choice:
            delta = _Delta()
            finish_reason = None

        class _Chunk:
            choices = [_Choice()]
            model = None
            usage = None

        return _Chunk()


class _StallingResp:
    """Yields `chunks` SSE events, then blocks forever (mimics a stalled OpenAI stream)."""

    def __init__(self, chunks=1):
        self.chunks = chunks
        self._never = threading.Event()

    def _iter_events(self):
        for _ in range(self.chunks):
            yield _SSE('c')
        self._never.wait()  # blocks forever, like a stalled socket read

    def close(self):
        pass

    _client = _Client()
    _cast_to = None
    response = None


@pytest.fixture
def tight_stream_limits(monkeypatch):
    """Patch the settings module (oai.py:566 imports these INSIDE _chat_stream)."""
    from agent_cascade import settings
    monkeypatch.setattr(settings, 'STREAM_MAX_SILENCE_SECONDS', 1.0)
    monkeypatch.setattr(settings, 'STREAM_MAX_TOTAL_SECONDS', 3.0)


def test_full_path_stall_after_chunk_fails_fast(monkeypatch, tight_stream_limits):
    """Real watch_stream (unpatched): 1 chunk then stall -> ModelServiceError in < 8s.

    Exercises the full path: llm.chat → _chat_stream → watch_stream (daemon reader
    thread) → RuntimeError('stream_stalled...') → except RuntimeError → ModelServiceError.
    """
    from agent_cascade.llm.oai import TextChatAtOAI

    llm = TextChatAtOAI({'api_base': 'http://127.0.0.1:9/v1', 'model': 'my-alias'})
    llm._chat_complete_create = lambda **kw: _StallingResp(1)
    start = time.monotonic()
    with pytest.raises(ModelServiceError, match='stream_stalled'):
        list(llm.chat(messages=[{'role': 'user', 'content': 'hi'}],
                      stream=True, delta_stream=False, extra_generate_cfg={}))
    assert time.monotonic() - start < 8.0


def test_full_path_stall_before_first_item_hits_total_limit(monkeypatch, tight_stream_limits):
    """Real watch_stream: 0 chunks (stall before first item) -> total limit message.

    Covers the TTFB exemption through the full path: before the first item only
    STREAM_MAX_TOTAL_SECONDS applies, so the error names 'total'.
    """
    from agent_cascade.llm.oai import TextChatAtOAI

    llm = TextChatAtOAI({'api_base': 'http://127.0.0.1:9/v1', 'model': 'my-alias'})
    llm._chat_complete_create = lambda **kw: _StallingResp(0)
    start = time.monotonic()
    with pytest.raises(ModelServiceError, match=r'stream_stalled.*total'):
        list(llm.chat(messages=[{'role': 'user', 'content': 'hi'}],
                      stream=True, delta_stream=False, extra_generate_cfg={}))
    assert time.monotonic() - start < 8.0


def test_full_path_watchdog_error_is_retryable(monkeypatch, tight_stream_limits):
    """The ModelServiceError from a watchdog trip classifies as 'retryable'."""
    from agent_cascade.llm.oai import TextChatAtOAI

    llm = TextChatAtOAI({'api_base': 'http://127.0.0.1:9/v1', 'model': 'my-alias'})
    llm._chat_complete_create = lambda **kw: _StallingResp(1)
    with pytest.raises(ModelServiceError) as exc_info:
        list(llm.chat(messages=[{'role': 'user', 'content': 'hi'}],
                      stream=True, delta_stream=False, extra_generate_cfg={}))
    assert classify_error(exc_info.value) == 'retryable'


def test_full_path_non_stall_runtimeerror_not_masked_as_retryable(monkeypatch):
    """BUG_0028: an unrelated RuntimeError raised inside _chat_stream must propagate with
    its natural type — NOT be wrapped in a generic retryable ModelServiceError.

    Pre-fix, oai.py's broad ``except RuntimeError`` arm caught ANY RuntimeError in the
    stream body (not just the watchdog's timeout) and wrapped it as a retryable
    ModelServiceError — masking real programming errors behind a pointless retry.
    Post-fix only StreamStalledError is wrapped; this plain RuntimeError propagates
    unmasked with its original type. FAILS pre-fix (ModelServiceError raised, which is
    not a RuntimeError subclass and would escape pytest.raises(RuntimeError)).
    """
    from agent_cascade.llm.oai import TextChatAtOAI

    llm = TextChatAtOAI({'api_base': 'http://127.0.0.1:9/v1', 'model': 'my-alias'})

    def _boom(**kw):
        raise RuntimeError('simulated chunk-processing bug')

    llm._chat_complete_create = _boom
    with pytest.raises(RuntimeError, match='simulated chunk-processing bug') as exc_info:
        list(llm.chat(messages=[{'role': 'user', 'content': 'hi'}],
                      stream=True, delta_stream=False, extra_generate_cfg={}))
    # Must be the ORIGINAL RuntimeError — not a ModelServiceError wrapper.
    assert type(exc_info.value) is RuntimeError, \
        f'unrelated RuntimeError was masked as {type(exc_info.value).__name__} (BUG_0028)'
