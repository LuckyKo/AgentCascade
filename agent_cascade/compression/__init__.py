"""
Compression system — unified compress_context() with clean-trim model.

All compression triggers (forced, agent-triggered, manual) go through a single
compress_context() function that mutates the pool and notifies the logger.
Discarded messages are actually deleted from the pool (clean trim).
"""
from agent_cascade.compression.result import CompressResult, CompressionPreparation
from agent_cascade.compression.core import compress_context
from agent_cascade.compression.helpers import rebuild_working_set, get_role, get_content, count_active_tokens
from agent_cascade.compression.constants import (
    DEFAULT_COMPRESSION_FRACTION,
    FORCE_COMPRESSION_FRACTION,
    FORCE_COMPRESSION_THRESHOLD,
    MIN_MESSAGES_TO_COMPRESS,
    MIN_TOKENS_TO_COMPRESS,
    MAX_COMPRESSION_RETRIES,
)

__all__ = [
    # Core functionality
    'CompressResult',
    'CompressionPreparation',
    'compress_context',
    'rebuild_working_set',
    # Utilities
    'get_role',
    'get_content',
    'count_active_tokens',
    # Constants (most commonly used)
    'DEFAULT_COMPRESSION_FRACTION',
    'FORCE_COMPRESSION_FRACTION',
    'FORCE_COMPRESSION_THRESHOLD',
    'MIN_MESSAGES_TO_COMPRESS',
    'MIN_TOKENS_TO_COMPRESS',
    'MAX_COMPRESSION_RETRIES',
]