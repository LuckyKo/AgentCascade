"""Constants for the compression system.

All magic numbers and configurable thresholds should be defined here
to ensure consistency across the codebase and easy tuning.
"""

# ── Compression Fractions ──
DEFAULT_COMPRESSION_FRACTION = 0.5  # Default: compress 50% of active history
FORCE_COMPRESSION_FRACTION = 0.7    # Fraction used for forced compression (may differ from default)
MIN_COMPRESSION_FRACTION = 0.3      # Minimum fraction allowed
MAX_COMPRESSION_FRACTION = 1.0      # Maximum fraction (compress all)

# ── Context Window Thresholds ──
FORCE_COMPRESSION_THRESHOLD = 95.0   # Trigger forced compression at >95% context usage
WARNING_COMPRESSION_THRESHOLD = 85.0  # Log warning at >85% (if implemented)

# ── Compression Guards ──
MIN_MESSAGES_TO_COMPRESS = 3         # Minimum messages before compression (unless force=True)
MIN_TOKENS_TO_COMPRESS = 200         # Minimum tokens before compression (unless force=True)
KEEP_TAIL_MESSAGES = 2               # Always keep last N messages when not forcing
MAX_COMPRESSION_RETRIES = 3          # Max consecutive forced compression failures before skipping

# ── Compression Agent Configuration ──
COMPRESSION_AGENT_TIMEOUT = 300      # 5-minute timeout for large compression tasks
ESTIMATED_TOKENS_PER_MESSAGE = 500   # Estimate for context window calculations
COMPRESSION_INPUT_FRACTION = 0.9     # Reserve 90% of agent's context for input messages

# ── Summary Processing ──
SUMMARY_PREFIXES_TO_STRIP = (         # Conversational filler to remove from summaries
    "here is a summary",
    "here is the summary",
    "summary:",
    "in summary,",
    "here's a summary",
    "**summary**:",
)


# Validation assertions — catch misconfiguration at import time
assert 0.0 < DEFAULT_COMPRESSION_FRACTION <= 1.0, "Default fraction must be in (0, 1]"
assert 0.0 < MIN_COMPRESSION_FRACTION < DEFAULT_COMPRESSION_FRACTION, "Min must be less than default"
assert 80.0 < FORCE_COMPRESSION_THRESHOLD < 100.0, "Threshold must be between 80% and 100%"


__all__ = [
    'DEFAULT_COMPRESSION_FRACTION',
    'FORCE_COMPRESSION_FRACTION',
    'MIN_COMPRESSION_FRACTION',
    'MAX_COMPRESSION_FRACTION',
    'FORCE_COMPRESSION_THRESHOLD',
    'WARNING_COMPRESSION_THRESHOLD',
    'MIN_MESSAGES_TO_COMPRESS',
    'MIN_TOKENS_TO_COMPRESS',
    'KEEP_TAIL_MESSAGES',
    'MAX_COMPRESSION_RETRIES',
    'COMPRESSION_AGENT_TIMEOUT',
    'ESTIMATED_TOKENS_PER_MESSAGE',
    'COMPRESSION_INPUT_FRACTION',
    'SUMMARY_PREFIXES_TO_STRIP',
]
