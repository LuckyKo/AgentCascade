"""Structured result from compress_context()."""
from dataclasses import dataclass


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