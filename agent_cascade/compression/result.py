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
    tokens_before: int = 0           # Token count before compression (BUG 6 fix)
    tokens_after: int = 0            # Token count after compression (BUG 6 fix)