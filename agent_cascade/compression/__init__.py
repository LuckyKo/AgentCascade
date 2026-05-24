"""
Compression system — unified compress_context() with clean-trim model.

All compression triggers (forced, agent-triggered, manual) go through a single
compress_context() function that mutates the pool and notifies the logger.
Discarded messages are actually deleted from the pool (clean trim).
"""
from agent_cascade.compression.result import CompressResult
from agent_cascade.compression.core import compress_context
from agent_cascade.compression.helpers import rebuild_working_set

__all__ = ['CompressResult', 'compress_context', 'rebuild_working_set']