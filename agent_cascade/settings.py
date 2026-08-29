# Copyright 2023 The Qwen team, Alibaba Group. All rights reserved.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal

# Settings for LLMs
DEFAULT_MAX_INPUT_TOKENS: int = int(os.getenv(
    'QWEN_AGENT_DEFAULT_MAX_INPUT_TOKENS', 65000))  # The LLM will truncate the input messages if they exceed this limit

# Settings for agents
MAX_LLM_CALL_PER_RUN: int = int(os.getenv('QWEN_AGENT_MAX_LLM_CALL_PER_RUN', 250))
DEFAULT_MAX_TURNS: int = int(os.getenv('QWEN_AGENT_DEFAULT_MAX_TURNS', 250))  # Default turn limit per agent execution
SECURITY_AGENT_MAX_TURNS: int = int(os.getenv('QWEN_AGENT_SECURITY_AGENT_MAX_TURNS', 20))  # Turn limit for system-launched Security advisor (replaces brutal wall-clock timeout)
MAX_AUTO_CONTINUE_ATTEMPTS: int = 5  # Max consecutive auto-continue attempts per episode before giving up (each attempt consumes one real turn)
# Reasoning-only soft "continue" (pure-resend) attempts before falling back to a full retry.
# Soft continues share the MAX_AUTO_CONTINUE_ATTEMPTS budget with full retries, so an episode is
# bounded at exactly min(N, cap) soft + (cap - min(N, cap)) full attempts.
REASONING_ONLY_CONTINUE_ATTEMPTS: int = int(os.getenv('QWEN_AGENT_REASONING_ONLY_CONTINUE_ATTEMPTS', 2))
# Deferred escape hatch for the soft-continue path. When False (default), a reasoning-only soft
# continue is a PURE RESEND: no new message is appended and nothing is rolled back — the LLM is
# simply re-called on the same history with the reasoning message still in place. When True, an
# escalating USER nudge ("stop thinking / produce output") is injected on each soft continue so the
# model gets an explicit instruction instead of a bare resend. Kept behind a flag so it can be
# enabled later without re-architecting.
SOFT_CONTINUE_NUDGE_ENABLED: bool = os.getenv('QWEN_AGENT_SOFT_CONTINUE_NUDGE', '0') == '1'


def _resolve_default_workspace() -> str:
    """Resolve DEFAULT_WORKSPACE at import time.

    Priority:
    1. QWEN_AGENT_DEFAULT_WORKSPACE env var (if set)
    2. Docker mount point /workspace (only if running inside a Docker container)
    3. Sibling AgentWorkspace directory relative to project root (e.g., ../AgentWorkspace from agent_cascade/)
    4. workspace/ under project root

    This ensures images/logs go to the correct workspace regardless of CWD when server starts.
    """
    env_val = os.getenv('QWEN_AGENT_DEFAULT_WORKSPACE')
    if env_val:
        return os.path.abspath(env_val)

    # Docker deployment: /workspace is mounted as AgentWorkspace
    # Only use this if we're actually inside a Docker container (/.dockerenv marker)
    if os.path.exists('/.dockerenv') and Path('/workspace').exists():
        return str(Path('/workspace').resolve())

    # Determine project root: agent_cascade/ is one level below project root
    project_root = Path(__file__).resolve().parent.parent

    # Prefer sibling AgentWorkspace directory (e.g., N:\work\WD\AgentWorkspace)
    sibling_ws = project_root.parent / 'AgentWorkspace'
    if sibling_ws.exists():
        return str(sibling_ws.resolve())

    # Check for workspace/ under project root
    local_ws = project_root / 'workspace'
    if local_ws.exists():
        return str(local_ws.resolve())

    # Ultimate fallback: workspace/ under project root (always valid, will be created)
    return str((project_root / 'workspace').resolve())


# Settings for tools
DEFAULT_WORKSPACE: str = _resolve_default_workspace()
DEFAULT_TOOL_RESULT_MAX_CHARS: int = int(os.getenv('QWEN_AGENT_TOOL_RESULT_MAX_CHARS', 25000))
DEFAULT_READ_FILE_MAX_LINES: int = int(os.getenv('QWEN_AGENT_READ_FILE_MAX_LINES', 150))
DEFAULT_HEURISTIC_MATCH_THRESHOLD: float = float(os.getenv('QWEN_AGENT_HEURISTIC_MATCH_THRESHOLD', 0.90))

# Settings for RAG
DEFAULT_MAX_REF_TOKEN: int = int(os.getenv('QWEN_AGENT_DEFAULT_MAX_REF_TOKEN',
                                           20000))  # The window size reserved for RAG materials
DEFAULT_PARSER_PAGE_SIZE: int = int(os.getenv('QWEN_AGENT_DEFAULT_PARSER_PAGE_SIZE',
                                               500))  # Max tokens per chunk when doing RAG
DEFAULT_RAG_KEYGEN_STRATEGY: Literal['None', 'GenKeyword', 'SplitQueryThenGenKeyword', 'GenKeywordWithKnowledge',
                                     'SplitQueryThenGenKeywordWithKnowledge'] = os.getenv(
                                         'QWEN_AGENT_DEFAULT_RAG_KEYGEN_STRATEGY', 'GenKeyword')
DEFAULT_RAG_SEARCHERS: List[str] = ast.literal_eval(
    os.getenv('QWEN_AGENT_DEFAULT_RAG_SEARCHERS',
              "['keyword_search', 'front_page_search']"))  # Sub-searchers for hybrid retrieval

# Settings for compression (Feature 020)
DEFAULT_COMPRESSION_COOLDOWN_SECONDS: float = float(os.getenv(
    'QWEN_AGENT_DEFAULT_COMPRESSION_COOLDOWN_SECONDS', 2.0))  # Minimum seconds between forced compressions to prevent thrashing
DEFAULT_COMPRESSION_MAX_ATTEMPTS: int = int(os.getenv(
    'QWEN_AGENT_COMPRESSION_MAX_ATTEMPTS', 100))  # Safety net max forced compressions before terminating (true overfeeding detected in core.py)
COMPRESSION_FORCE_THRESHOLD: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_FORCE_THRESHOLD', 96.0))  # Force compress at X% token usage
COMPRESSION_WARNING_THRESHOLD: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_WARNING_THRESHOLD', 90.0))  # Warn at X% token usage
COMPRESSION_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_TIMEOUT', 120.0))  # Max seconds for compression to complete
DEFAULT_COMPRESSION_PROACTIVE_THRESHOLD: float = float(os.getenv(
    'QWEN_AGENT_DEFAULT_COMPRESSION_PROACTIVE_THRESHOLD', 95.0))  # Proactive compress at X% usage (post-tool, async drain checks)
DEFAULT_COMPRESSION_CONTEXT_RESERVE_TOKENS: int = int(os.getenv(
    'QWEN_AGENT_COMPRESSION_CONTEXT_RESERVE_TOKENS', 3000))  # Tokens reserved for LLM call overhead (system prompt, function schemas, reasoning)
COMPRESSION_OVERFLOW_TOLERANCE_PCT: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_OVERFLOW_TOLERANCE_PCT', 3.0))  # Tolerance margin for overflow detection before raising exception
# Recount threshold: when delta token estimates are unavailable, force a full recount
# if cached usage already exceeds this fraction of the allocated max (cache may be stale).
COMPRESSION_RECOUNT_THRESHOLD: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_RECOUNT_THRESHOLD', 0.85))  # Force full recount at X fraction of allocated max when cache invalidated
COMPRESSION_DEFAULT_FRACTION: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_DEFAULT_FRACTION', 0.7))  # Default fraction of history to discard (70%)
COMPRESSION_MIN_FRACTION: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_MIN_FRACTION', 0.1))  # Minimum allowed compression fraction
COMPRESSION_MAX_FRACTION: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_MAX_FRACTION', 0.9))  # Maximum allowed compression fraction
COMPRESSION_SECURITY_CHECK_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_SECURITY_CHECK_TIMEOUT', 120.0))  # Max seconds for security advisor during compression
COMPRESSION_MAX_RETRIES: int = int(os.getenv(
    'QWEN_AGENT_COMPRESSION_MAX_RETRIES', 5))  # Max retry attempts for compression agent invocation on marker validation failure

# Hierarchical memory consolidation settings
COMPRESSION_CONSOLIDATION_THRESHOLD: int = int(os.getenv(
    'QWEN_AGENT_COMPRESSION_CONSOLIDATION_THRESHOLD', 8))  # Markers at which to trigger consolidation
COMPRESSION_MAX_CONSOLIDATION_TOKENS: int = int(os.getenv(
    'QWEN_AGENT_COMPRESSION_MAX_CONSOLIDATION_TOKENS', 100000))  # Max tokens for consolidation input before aborting

# Compression agent invocation timeout (5 minutes for large compression/consolidation tasks)
COMPRESSION_AGENT_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_AGENT_TIMEOUT', 300.0))

# Settings for agent pool
AGENT_IDLE_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_IDLE_TIMEOUT', 1600.0))  # Auto-dismiss regular agents after X seconds inactivity
SYSTEM_AGENT_IDLE_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_SYSTEM_AGENT_IDLE_TIMEOUT', 60.0))  # Auto-dismiss Compressor/Security after X seconds inactivity
AGENT_IDLE_CHECK_INTERVAL: float = float(os.getenv(
    'QWEN_AGENT_IDLE_CHECK_INTERVAL', 60.0))  # Check every N seconds
AGENT_MAX_AUTO_ROLLBACKS: int = int(os.getenv(
    'QWEN_AGENT_MAX_AUTO_ROLLBACKS', 5))  # Max loop recovery retries
# ── Two-tier loop detection (2026-08 redesign; plan: plans/loop_detector_exact_redesign_PLAN.md §5.3) ──
# Tier 1 — exact matcher (exact_loop_detect.py). Rollback still respects auto_rollback_on_loop / max_auto_rollbacks.
LOOP_EXACT_ROLLBACK_ENABLED: bool = os.getenv(
    'QWEN_AGENT_LOOP_EXACT_ROLLBACK', '1') == '1'  # Tier 1 runs and rolls back on exact hits
# Tier 2 — fuzzy detector (tool_loop_detect.py), warning-first: ONE advisory per run, no destructive path.
LOOP_FUZZY_WARNING_ENABLED: bool = os.getenv(
    'QWEN_AGENT_LOOP_FUZZY_WARNING', '1') == '1'  # Tier 2 runs and may inject the advisory
# Tier 2 escalation: fuzzy loop persisting FUZZY_ESCALATION_TURNS turns after the warning → full rollback. Off by default.
TOOL_LOOP_FUZZY_ROLLBACK_ENABLED: bool = os.getenv(
    'QWEN_AGENT_TOOL_LOOP_FUZZY_ROLLBACK', '0') == '1'  # Escalation off by default
# DEPRECATED (2026-08): legacy kill switch only — can DISABLE Tier 2 (LOOP_FUZZY_WARNING_ENABLED AND this flag) but never enable it. Removed in a later cleanup release.
TOOL_LOOP_DETECTION_ENABLED: bool = os.getenv(
    'QWEN_AGENT_TOOL_LOOP_DETECTION', '1') == '1'  # Legacy kill switch for the fuzzy tier
AGENT_MAX_NESTING_DEPTH: int = int(os.getenv(
    'QWEN_AGENT_MAX_NESTING_DEPTH', 10))  # Max depth of nested agent calls
AGENT_MAX_WORKERS: int = int(os.getenv(
    'QWEN_AGENT_MAX_WORKERS', 3))  # ThreadPoolExecutor workers
# DEPRECATED (2026-08): AGENT_SLEEPING_TIMEOUT is no longer used.
# Timeout-to-IDLE transition removed; agents stay SLEEPING until woken by messages or completed.
AGENT_SLEEPING_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_SLEEPING_TIMEOUT', 300.0))  # DEPRECATED (2026-08): Formerly max seconds before SLEEPING→IDLE transition; now unused.
AGENT_SLEEPING_WAKEUP_INTERVAL: float = float(os.getenv(
    'QWEN_AGENT_SLEEPING_WAKEUP_INTERVAL', 5.0))  # Wakeup log interval while SLEEPING
# Conservative estimate used for compression template overhead estimation.
# Counts system prompt overhead and structural tokens, so a higher divisor
# (more chars per token) yields safer/more conservative estimates.
CHARS_PER_TOKEN_ESTIMATE: float = float(os.getenv(
    'QWEN_AGENT_CHARS_PER_TOKEN_ESTIMATE', 5.0))

# Settings for forget_last tool (Feature 021)
DEFAULT_FORGET_LAST_TRUNCATE_MAX_CHARS: int = int(os.getenv(
    'QWEN_AGENT_FORGET_LAST_TRUNCATE_MAX_CHARS', 100))  # Maximum characters to keep when truncating tool responses
DEFAULT_FORGET_LAST_MIN_CHAR_LIMIT: int = int(os.getenv(
    'QWEN_AGENT_FORGET_LAST_MIN_CHAR_LIMIT', 200))  # Skip truncation for responses ≤ this size (too small to benefit from truncation)

# Settings for endpoint scheduling
ENDPOINT_SLOT_ACQUIRE_TIMEOUT: int = int(os.getenv(
    'QWEN_AGENT_ENDPOINT_SLOT_ACQUIRE_TIMEOUT', 30))  # Timeout in seconds for acquiring endpoint scheduling slots

# Settings for endpoint cooldown (time-based skip of failed endpoints)
def _parse_endpoint_cooldown():
    """Parse AGENT_CASCADE_ENDPOINT_COOLDOWN with safe defaults."""
    try:
        val = int(os.getenv('AGENT_CASCADE_ENDPOINT_COOLDOWN', '60'))
        return max(0, val)  # Clamp to 0 if negative
    except (ValueError, TypeError):
        return 60

ENDPOINT_COOLDOWN_SECONDS: int = _parse_endpoint_cooldown()  # Seconds to skip a failed endpoint before retrying
ENDPOINT_FAILURE_CLEANUP_HOURS: int = int(os.getenv(
    'AGENT_CASCADE_ENDPOINT_FAILURE_CLEANUP_HOURS', 24))  # Remove failure records older than this many hours

# Phase 1: Fix A2 — absolute wall-clock deadline for a single LLM call.
# Caps the total time (all retries included) that _execute_llm_call_with_retry may
# spend before aborting with a [SYSTEM ERROR]. Prevents unbounded retry churn when
# backoff is misconfigured or an endpoint never recovers. Set to 0 to disable.
LLM_CALL_DEADLINE_SECONDS: int = int(os.getenv(
    'QWEN_AGENT_LLM_CALL_DEADLINE_SECONDS', 900))  # Wall-clock deadline (seconds) for one LLM call, all retries included

# Phase 1: Fix D — lightweight pre-allocation API sanity probe.
# Before the router allocates an endpoint to a real call, it checks reachability
# and auth via a fast GET /models request (no model loading or GPU thrashing).
# Probes fire once per connection-establishment: an instance that already holds a
# committed endpoint (a real call succeeded on it) is NOT re-probed — see
# _instance_committed_endpoint in router.py. Set SANITY_PROBE_ENABLED to False to disable.
SANITY_PROBE_ENABLED: bool = os.getenv(
    'QWEN_AGENT_SANITY_PROBE_ENABLED', 'true').lower() in ('1', 'true', 'yes', 'on')  # Master toggle for the pre-allocation sanity probe
SANITY_PROBE_TIMEOUT_SECONDS: float = float(os.getenv(
    'QWEN_AGENT_SANITY_PROBE_TIMEOUT_SECONDS', 5.0))  # HTTP timeout (seconds) for the lightweight probe GET request

# Phase 2: Fix B1 — endpoint blacklist for deterministic failures.
# An endpoint that fails ENDPOINT_DETERMINISTIC_FAILURE_THRESHOLD consecutive times
# with a DETERMINISTIC client error (one that will recur on every attempt, e.g. an
# HTTP 400 "not supported") is blacklisted for ENDPOINT_BLACKLIST_SECONDS — much
# longer than the 60s cooldown because deterministic errors don't self-resolve.
# Non-deterministic failures (network, timeout, 5xx) reset the counter instead.
ENDPOINT_DETERMINISTIC_FAILURE_THRESHOLD: int = int(os.getenv(
    'QWEN_AGENT_ENDPOINT_DETERMINISTIC_FAILURE_THRESHOLD', 3))  # Consecutive deterministic failures before blacklisting an endpoint
ENDPOINT_BLACKLIST_SECONDS: int = int(os.getenv(
    'QWEN_AGENT_ENDPOINT_BLACKLIST_SECONDS', 7200))  # Blacklist duration (seconds) for a persistently-failing endpoint

# Phase 3: Fix A1 — cap on SLEEPING duration.
# Max seconds an agent may remain in SLEEPING state waiting for background tools
# (async agent calls / async shells). On expiry the agent is forced to COMPLETING
# with an error message so a hung background tool can no longer hold it forever.
# This replaces the deprecated AGENT_SLEEPING_TIMEOUT above for control flow — that
# constant's timeout-to-IDLE transition was removed in 2026-08 and it is now unused;
# the deprecated block is left intact for backward compatibility. Set to 0 to disable
# the cap (legacy unbounded-wait behavior).
AGENT_SLEEPING_MAX_WAIT_SECONDS: int = int(os.getenv(
    'QWEN_AGENT_AGENT_SLEEPING_MAX_WAIT_SECONDS', 3600))  # Max seconds in SLEEPING before forcing COMPLETING with error

# Settings for API router circuit breaker
BREAKER_BASE_WINDOW_SECONDS: float = float(os.getenv(
    'QWEN_AGENT_BREAKER_BASE_WINDOW_SECONDS', 20.0))  # Initial open-state window for the per-server circuit breaker (seconds)
BREAKER_MAX_WINDOW_SECONDS: float = float(os.getenv(
    'QWEN_AGENT_BREAKER_MAX_WINDOW_SECONDS', 120.0))  # Cap for exponential window growth on repeated failed probes (seconds)
BREAKER_WINDOW_GROWTH: float = float(os.getenv(
    'QWEN_AGENT_BREAKER_WINDOW_GROWTH', 2.0))  # Window multiplier applied on each repeated failed probe
SERVER_BUSY_WAIT_CAP_SECONDS: float = float(os.getenv(
    'QWEN_AGENT_SERVER_BUSY_WAIT_CAP_SECONDS', 30.0))  # Per-call cap for the D1 fail-fast wait when the whole endpoint chain is breaker-gated (seconds)

# Settings for token estimation
# Aggressive estimate used for telemetry and output estimation.
# Based on typical English text (~4 chars/token).
TOKEN_ESTIMATE_CHAR_DIVISOR: float = float(os.getenv(
    'QWEN_AGENT_TOKEN_ESTIMATE_CHAR_DIVISOR', 4.0))
IMAGE_TOKEN_ESTIMATE: int = int(os.getenv(
    'QWEN_AGENT_IMAGE_TOKEN_ESTIMATE', 255))  # Estimated tokens per image in message counting
CHAT_TEMPLATE_TOKEN_OVERHEAD: int = int(os.getenv(
    'QWEN_AGENT_CHAT_TEMPLATE_TOKEN_OVERHEAD', 8))  # Overhead per message from llama.cpp chat template (bos, role tags, newlines, etc.)
MESSAGE_TOKEN_ESTIMATE: int = int(os.getenv(
    'QWEN_AGENT_MESSAGE_TOKEN_ESTIMATE', 500))  # Estimated tokens per message during compression
CONTEXT_RESERVATION_RATIO: float = float(os.getenv(
    'QWEN_AGENT_CONTEXT_RESERVATION_RATIO', 0.9))  # Reserve 90% for input, 10% for output during compression

# Settings for LLM retry/backoff
DEFAULT_MAX_TOKENS: int = int(os.getenv(
    'QWEN_AGENT_DEFAULT_MAX_TOKENS', 128000))  # Default max tokens for LLM calls
LLM_MAX_RETRIES: int = int(os.getenv(
    'QWEN_AGENT_LLM_MAX_RETRIES', 1))  # Max retries for LLM calls
LLM_RETRY_BASE_DELAY: float = float(os.getenv(
    'QWEN_AGENT_LLM_RETRY_BASE_DELAY', 1.0))  # Base delay in seconds for retry backoff
LLM_RETRY_MAX_BACKOFF: float = float(os.getenv(
    'QWEN_AGENT_LLM_RETRY_MAX_BACKOFF', 5.0))  # Maximum backoff cap in seconds

# Settings for streaming timeouts (Layer 1-3 defense against stuck streams)
STREAM_MAX_SILENCE_SECONDS: float = float(os.getenv(
    'QWEN_AGENT_STREAM_MAX_SILENCE_SECONDS', 180.0))  # Max seconds between chunks before considering stream stalled
STREAM_MAX_TOTAL_SECONDS: float = float(os.getenv(
    'QWEN_AGENT_STREAM_MAX_TOTAL_SECONDS', 900.0))  # Max total duration of a streaming response

# Dismiss thread join timeout (seconds to wait for agent thread to stop cooperatively)
DISMISS_THREAD_JOIN_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_DISMISS_THREAD_JOIN_TIMEOUT', 2.0))

# HTTP client timeouts (passed to httpx)
HTTP_READ_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_HTTP_READ_TIMEOUT', 300.0))  # Timeout for reading a single chunk from server
HTTP_CONNECT_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_HTTP_CONNECT_TIMEOUT', 10.0))  # Timeout for establishing TCP connection
HTTP_WRITE_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_HTTP_WRITE_TIMEOUT', 60.0))  # Timeout for sending request body
HTTP_POOL_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_HTTP_POOL_TIMEOUT', 30.0))  # Timeout waiting for connection from pool

# Settings for telemetry
SYSTEM_PROMPT_HASH_MAX_CHARS: int = int(os.getenv(
    'QWEN_AGENT_SYSTEM_PROMPT_HASH_MAX_CHARS', 2000))  # Max chars for system prompt before hashing
DEFAULT_RECENT_EVENT_COUNT: int = int(os.getenv(
    'QWEN_AGENT_DEFAULT_RECENT_EVENT_COUNT', 50))  # Default recent events count
MAX_EVENTS_IN_MEMORY: int = int(os.getenv(
    'QWEN_AGENT_MAX_EVENTS_IN_MEMORY', 5000))  # Max events in memory before trimming

# Settings for LM Studio
LM_STUDIO_KEEPALIVE_SECONDS: float = float(os.getenv(
    'QWEN_AGENT_LM_STUDIO_KEEPALIVE', 3.0))  # Keepalive expiry in seconds

# ── Inner-loop detection settings (Feature: loop detection tuning) ─────────────
@dataclass
class InnerLoopSettings:
    """Tunable parameters for the inner-loop repetition detector.

    After Phase 3 cleanup, only char_run and max_chars guards plus the
    two-phase semantic loop detector remain active. All scoring-based fields
    below are DEPRECATED and kept only for backward compatibility with existing
    code that constructs InnerLoopSettings with explicit values.

    Active detection modes:
    - Character run detection (char_run_enabled, char_run_limit)
    - Max chars guard (default_max_chars)
    - Two-phase semantic loop detector (configured via two_phase_loop_detect.py settings)

    All fields have defaults matching current production values. Override by
    constructing a custom instance and passing it to ``InnerLoopDetector``.
    """

    # ── Active settings ────────────────────────────────────────────────

    # Character run detection (last line of defense against degenerate output)
    char_run_enabled: bool = os.getenv('QWEN_AGENT_LOOP_CHAR_RUN', '1') != '0'
    char_run_limit: int = 129              # Max consecutive identical chars before alert

    # Activation thresholds
    default_min_chars: int = 4000          # Min chars to accumulate before full detection (kept for compatibility)
    default_max_chars: int = 40960         # Hard character limit — force-trigger detection if exceeded (~8K tokens)

    # Max chars guard toggle — controls the hard character limit detection guard.
    loop_max_chars_enabled: bool = True    # Enable max chars hard limit guard

    # Two-phase semantic loop detection settings
    loop_two_phase_enabled: bool = True    # Enable two-phase semantic loop detection
    loop_suspicion_threshold: int = 7      # N-gram occurrence count to trigger suspicion [5-15]
    loop_confirm_required: int = 5         # Exact matches required for confirmation [2-6]
    loop_cooldown_feeds: int = 50          # Feeds to suppress after failed confirmation [10-200]

    # ── Deprecated settings (scoring-based modes removed in Phase 3) ────
    # These fields are no longer used by InnerLoopDetector but kept for backward
    # compatibility with code that passes explicit values when constructing settings.

    # Memory bounds (deprecated — scoring counters removed)
    max_counter_entries: int = 200          # DEPRECATED: was max entries per Counter before pruning
    max_tokens: int = 1000                 # DEPRECATED: was max tokens in the sliding window

    # Activation thresholds (deprecated)
    default_batch_interval: int = 1        # DEPRECATED: was run heavy checks every N-th feed call

    # Structural parameters (deprecated — scoring modes removed)
    ngram_size: int = 64                   # DEPRECATED: was token window size for n-gram repetition scoring
    block_size: int = 128                  # DEPRECATED: was token window size for block repetition scoring
    entropy_window: int = 128             # DEPRECATED: was token window for Shannon entropy calculation

    # Scoring system (deprecated — entirely removed)
    score_threshold: int = 350            # DEPRECATED: was cumulative score to trigger loop detection
    score_decay_rate: float = 0.97         # DEPRECATED: was multiplicative decay per feed cycle
    max_score: int = 500                   # DEPRECATED: was hard cap to prevent unbounded score growth

    # Detection thresholds (deprecated — scoring modes removed)
    sentence_repetition_threshold: int = 15  # DEPRECATED: was sentence count to flag repetition
    ngram_repetition_threshold: int = 7      # DEPRECATED: was n-gram count to flag repetition
    block_repetition_threshold: int = 6      # DEPRECATED: was block count to flag repetition
    entropy_threshold: float = 2.0          # DEPRECATED: was Shannon entropy below which a loop is suspected

    # Per-mode toggles for removed modes (deprecated)
    sentence_rep_enabled: bool = os.getenv('QWEN_AGENT_LOOP_SENTENCE_REP', '1') != '0'   # DEPRECATED: sentence scoring removed
    ngram_rep_enabled: bool = os.getenv('QWEN_AGENT_LOOP_NGRAM_REP', '1') != '0'          # DEPRECATED: n-gram scoring removed
    block_rep_enabled: bool = os.getenv('QWEN_AGENT_LOOP_BLOCK_REP', '1') != '0'          # DEPRECATED: block scoring removed
    entropy_collapse_enabled: bool = os.getenv('QWEN_AGENT_LOOP_ENTROPY', '1') != '0'     # DEPRECATED: entropy detection removed

# ── Code interpreter settings (Feature: CI session sharing) ────────────────
CI_EXECUTION_TIMEOUT: int = int(os.getenv('M6_CODE_INTERPRETER_EXEC_TIMEOUT', '120'))   # Per-call execution timeout (seconds)
CI_WATCHDOG_TIMEOUT: int = int(os.getenv('M6_CODE_INTERPRETER_WATCHDOG_TIMEOUT', '300'))  # Kernel inactivity watchdog timeout (seconds)
CI_STALE_CONTAINER_TTL: int = int(os.getenv('M6_CODE_INTERPRETER_STALE_TTL', '1200'))      # Stale container cleanup TTL (seconds)
CI_MIN_EXECUTION_TIMEOUT: int = 1    # Minimum per-call execution timeout (seconds)
CI_MIN_WATCHDOG_TIMEOUT: int = 30     # Minimum watchdog timeout (seconds)
CI_MIN_STALE_CONTAINER_TTL: int = 30  # Minimum stale container TTL (seconds)

# ── Cache pool settings (Feature: USE_CACHED_ENTRY_N) ────────────────────────
CACHE_POOL_ENABLED: bool = False              # Toggle cache pool on/off (default: disabled)
CACHE_POOL_SIZE: int = int(os.getenv('QWEN_AGENT_CACHE_POOL_SIZE', '50'))          # Rolling buffer entries per instance
CACHE_THRESHOLD_CHARS: int = int(os.getenv('QWEN_AGENT_CACHE_THRESHOLD_CHARS', '1000'))  # Min chars for output & granular arg caching

# ── Async shell command settings (Feature: async shell_cmd) ───────────
MAX_ASYNC_SHELL_PER_AGENT: int = 5            # Max concurrent async shells per agent
# DEPRECATED: Async shell truncation now uses shell_char_limit from llm_cfg (default 2048, same as sync mode)
ASYNC_SHELL_HEARTBEAT_TRUNCATE_CHARS: int = int(os.getenv('QWEN_AGENT_ASYNC_SHELL_HEARTBEAT_CHARS', '800'))  # noqa: F841
ASYNC_SHELL_DEFAULT_TIMEOUT: int = 3600       # Default timeout for async shells (1 hour)
HEARTBEAT_CHECK_INTERVAL: float = 0.5         # How often the tracker thread checks for heartbeats (seconds)
HEARTBEAT_TRUNCATE_FIRST_LINES: int = 5       # Lines kept at start when truncating heartbeat output
HEARTBEAT_TRUNCATE_LAST_LINES: int = 10       # Lines kept at end when truncating heartbeat output
EARLY_OUTPUT_CHECK_TIMEOUT: float = 2.0       # Max seconds to wait for early output/completion after launch
AUTO_ASYNC_TIMEOUT_THRESHOLD: int = 60        # Seconds: timeout above this triggers auto-async mode
DEFAULT_AUTO_ASYNC_HEARTBEAT: int = 30        # Default heartbeat interval for auto-async shells
WAIT_CMD_MAX_TIMEOUT: float = 180.0           # Max seconds __wait will block when heartbeats are configured (3 min)
WAIT_CMD_DEFAULT_TIMEOUT: float = 30.0        # Seconds __wait blocks when no heartbeats are configured (-1)
WAIT_CMD_POLL_INTERVAL: float = 0.5           # Seconds between state polls inside the __wait loop

# ── Skills system settings (Feature: Skills System Phase 1) ────────────
LOAD_SKILL_AUTO: str = "AUTO"     # Auto-match relevant skills from task context
LOAD_SKILL_NONE: str = "NONE"     # No skill loading (saves tokens)
DEFAULT_LOAD_SKILL_MODE: str = os.getenv('QWEN_AGENT_DEFAULT_LOAD_SKILL', 'AUTO')  # Default load_skill mode: AUTO or NONE
SKILL_MATCH_THRESHOLD: float = float(os.getenv('QWEN_AGENT_SKILL_MATCH_THRESHOLD', '0.15'))  # Minimum relevance score for AUTO mode skill loading
SKILL_CACHE_TTL_SECONDS: float = float(os.getenv(
    'QWEN_AGENT_SKILL_CACHE_TTL', 30.0))  # Cache TTL for mtime-based discovery cache

_SKILLS_DISABLED_RAW: str = os.getenv('QWEN_AGENT_SKILLS_DISABLED', '')
SKILLS_DISABLED: List[str] = [
    s.strip().lower() for s in _SKILLS_DISABLED_RAW.split(',') if s.strip()
] if _SKILLS_DISABLED_RAW else []

# ── Auto-skill generation settings (Feature: Auto-Skill Generation Phase 1) ──
AUTO_SKILL_ENABLED: bool = False                          # Toggle auto-skill generation on/off
AUTO_SKILL_EXTRA_TURNS: int = int(os.getenv(
    'QWEN_AGENT_AUTO_SKILL_EXTRA_TURNS', 25))            # Extra turns for auto-skill execution before rollback
AUTO_SKILL_MIN_TOOL_CALLS: int = 5                       # Minimum tool calls before triggering reflection
AUTO_SKILL_PROMOTION_THRESHOLD: float = 0.3              # Self-match score threshold for auto-promotion
AUTO_SKILL_AUTO_PROMOTE: bool = True                     # Auto-promote validated skills to .qwen/skills/
AUTO_SKILL_MAX_SIZE_KB: int = 15                         # Maximum SKILL.md file size in KB
MAX_SKILL_INJECTION_TOKENS: int = 8000                   # Max tokens for skill injection per turn
MAX_SKILLS_PER_CALL: int = 5                             # Max skills to propose per reflection call
AUTO_SKILL_MAX_PER_SESSION: int = 1                      # Max skill proposals per agent instance per session
MIN_SKILL_BODY_LENGTH: int = 100                         # Minimum body char count for skill validation
MIN_DESCRIPTION_LENGTH: int = 20                         # Minimum description length for skill validation