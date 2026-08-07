"""Streaming timeout utilities for AgentCascade."""

import time
from typing import Any, Iterator, TypeVar

T = TypeVar('T')


def watch_stream(
    stream: Iterator[T],
    max_silence_seconds: float,
    max_total_seconds: float,
    error_message_prefix: str = "",
) -> Iterator[T]:
    """Wrap a streaming iterator with silence and total-duration timeout guards.

    Skips the silence check on the first item (slow reasoning models may take >120s
    to produce their first token). The total timeout applies from stream start.

    Args:
        stream: The underlying iterator to wrap.
        max_silence_seconds: Max seconds between consecutive items before raising.
        max_total_seconds: Max total duration of the stream before raising.
        error_message_prefix: Optional prefix for error messages (e.g., backend name).

    Raises:
        RuntimeError: On silence timeout or total timeout. Caller should wrap in
            ModelServiceError if needed.
    """
    prefix = f"{error_message_prefix}: " if error_message_prefix else ""
    stream_start = time.monotonic()
    first_item = True
    last_item_time: float | None = None

    for item in stream:
        now = time.monotonic()

        # Silence check only after first item received (slow reasoning models may take >120s)
        if not first_item and last_item_time is not None:
            elapsed_silence = now - last_item_time
            if elapsed_silence > max_silence_seconds:
                raise RuntimeError(
                    f"{prefix}stream_stalled: no data for {elapsed_silence:.1f}s "
                    f"(silence limit={max_silence_seconds:.0f}s)"
                )

        # Total timeout applies from stream start
        elapsed_total = now - stream_start
        if elapsed_total > max_total_seconds:
            raise RuntimeError(
                f"{prefix}stream_stalled: exceeded total limit of {elapsed_total:.1f}s "
                f"(max={max_total_seconds:.0f}s)"
            )

        first_item = False
        last_item_time = now
        yield item