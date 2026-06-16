"""Structured result from compress_context()."""
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class CompressResult:
    """Structured result from compress_context()."""
    success: bool
    summary_text: str | None          # The raw summary (before template wrapping)
    marker_message: dict | None       # The final USER message with COMPRESSION_MARKER
    messages_discarded: int           # How many messages were trimmed
    tail_count: int                  # Messages remaining after the marker
    error: str | None                # Error message if success is False
    mode: str                        # "auto" or "manual"


@dataclass
class CompressionPreparation:
    """
    Result of _prepare_compression() phase.

    Using a dataclass instead of a tuple makes the code self-documenting
    and easier to extend without breaking changes.
    """
    early_exit: Optional[CompressResult]  # If not None, compression should exit early
    discard_count: int                    # Number of messages to discard
    active_start_idx: int                 # Start index of active set
    target_messages: list                 # Messages to send to compression agent
    existing_summary: Optional[str]       # Previous summary for compounding