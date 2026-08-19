"""Facade: preserves the historical import surface for ``execution_engine``.

The real implementations now live in the :mod:`agent_cascade.engine` sub-package
(Phase 1 of the module-split refactor). This module is a thin compatibility shim:
it re-exports every symbol that production code (and tests) import from
``agent_cascade.execution_engine`` so that no import site needs to change.

Moved method bodies are NOT re-exported as free functions — they live on
``ExecutionEngine`` via the mixins in ``engine/llm_call.py``,
``engine/compression_exec.py`` and ``engine/tool_execution.py``. Only module-level
names (helpers, constants) and the class itself are re-exported here.

NOTE (v3): mock.patch targets for internal helpers are NOT bound here. Tests that
patch internal helpers point at the TRUE home sub-module (see plan §2.1 / §11).
The facade only re-exports names that PRODUCTION code imports from this path.
"""

from agent_cascade.engine.core import ExecutionEngine  # noqa: F401
from agent_cascade.engine.compression_exec import (    # noqa: F401
    FALLBACK_COMPRESSION_MAX_ROUNDS,
    FALLBACK_COMPRESSION_INITIAL_FRACTION,
    FALLBACK_COMPRESSION_MIN_SLICE_FRACTION,
    _COMPRESSOR_WINDOW_SAFETY_FACTOR,
)
from agent_cascade.engine.helpers import (             # noqa: F401
    SleepAction,
    _get_active_functions_from_template,
    _make_token_count_callback,
    _make_usage_callback,
    _invalidate_token_cache,
    _normalize_gemma_thought_tags,
    _normalize_thinking_blocks,
    _extract_tool_calls_from_text,
    _check_message_truncation,
    _is_incomplete_state,
    _build_resources_block,
    _build_skills_block,
    _inject_skills_to_system_message,
    _inject_self_augmentation_skill,
    _get_supervisor_log_filename,
    _build_session_metadata,
    _replace_section,
    _replace_resources_block,
)

__all__ = [
    "ExecutionEngine",
    # Fallback-compression constants (production import surface preserved)
    "FALLBACK_COMPRESSION_MAX_ROUNDS",
    "FALLBACK_COMPRESSION_INITIAL_FRACTION",
    "FALLBACK_COMPRESSION_MIN_SLICE_FRACTION",
    "_COMPRESSOR_WINDOW_SAFETY_FACTOR",
    # Module-level helpers (re-exported so historical import paths keep working)
    "SleepAction",
    "_get_active_functions_from_template",
    "_make_token_count_callback",
    "_make_usage_callback",
    "_invalidate_token_cache",
    "_normalize_gemma_thought_tags",
    "_normalize_thinking_blocks",
    "_extract_tool_calls_from_text",
    "_check_message_truncation",
    "_is_incomplete_state",
    "_build_resources_block",
    "_build_skills_block",
    "_inject_skills_to_system_message",
    "_inject_self_augmentation_skill",
    "_get_supervisor_log_filename",
    "_build_session_metadata",
    "_replace_section",
    "_replace_resources_block",
]
