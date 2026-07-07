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
from typing import List, Literal

# Settings for LLMs
DEFAULT_MAX_INPUT_TOKENS: int = int(os.getenv(
    'QWEN_AGENT_DEFAULT_MAX_INPUT_TOKENS', 65000))  # The LLM will truncate the input messages if they exceed this limit

# Settings for agents
MAX_LLM_CALL_PER_RUN: int = int(os.getenv('QWEN_AGENT_MAX_LLM_CALL_PER_RUN', 250))

# Settings for tools
DEFAULT_WORKSPACE: str = os.path.abspath(os.getenv('QWEN_AGENT_DEFAULT_WORKSPACE', 'workspace/'))
DEFAULT_TOOL_RESULT_MAX_CHARS: int = int(os.getenv('QWEN_AGENT_TOOL_RESULT_MAX_CHARS', 10000))
DEFAULT_READ_FILE_MAX_LINES: int = int(os.getenv('QWEN_AGENT_READ_FILE_MAX_LINES', 1000))
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
    'QWEN_AGENT_COMPRESSION_FORCE_THRESHOLD', 95.0))  # Force compress at X% token usage
COMPRESSION_WARNING_THRESHOLD: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_WARNING_THRESHOLD', 90.0))  # Warn at X% token usage
COMPRESSION_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_TIMEOUT', 120.0))  # Max seconds for compression to complete
COMPRESSION_DEFAULT_FRACTION: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_DEFAULT_FRACTION', 0.7))  # Default fraction of history to discard (70%)
COMPRESSION_MIN_FRACTION: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_MIN_FRACTION', 0.1))  # Minimum allowed compression fraction
COMPRESSION_MAX_FRACTION: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_MAX_FRACTION', 0.9))  # Maximum allowed compression fraction
COMPRESSION_SECURITY_CHECK_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_COMPRESSION_SECURITY_CHECK_TIMEOUT', 120.0))  # Max seconds for security advisor during compression
# Settings for agent pool
AGENT_IDLE_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_IDLE_TIMEOUT', 900.0))  # Auto-dismiss after X seconds inactivity
AGENT_IDLE_CHECK_INTERVAL: float = float(os.getenv(
    'QWEN_AGENT_IDLE_CHECK_INTERVAL', 60.0))  # Check every N seconds
AGENT_MAX_AUTO_ROLLBACKS: int = int(os.getenv(
    'QWEN_AGENT_MAX_AUTO_ROLLBACKS', 3))  # Max loop recovery retries
AGENT_MAX_NESTING_DEPTH: int = int(os.getenv(
    'QWEN_AGENT_MAX_NESTING_DEPTH', 10))  # Max depth of nested agent calls
AGENT_MAX_WORKERS: int = int(os.getenv(
    'QWEN_AGENT_MAX_WORKERS', 10))  # ThreadPoolExecutor workers
AGENT_SLEEPING_TIMEOUT: float = float(os.getenv(
    'QWEN_AGENT_SLEEPING_TIMEOUT', 300.0))  # Max seconds for background tools
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

# Settings for endpoint scheduling
ENDPOINT_SLOT_ACQUIRE_TIMEOUT: int = int(os.getenv(
    'QWEN_AGENT_ENDPOINT_SLOT_ACQUIRE_TIMEOUT', 30))  # Timeout in seconds for acquiring endpoint scheduling slots

# Settings for token estimation
# Aggressive estimate used for telemetry and output estimation.
# Based on typical English text (~4 chars/token), this is more optimistic
# than CHARS_PER_TOKEN_ESTIMATE (5.0) which accounts for system prompt overhead.
TOKEN_ESTIMATE_CHAR_DIVISOR: float = float(os.getenv(
    'QWEN_AGENT_TOKEN_ESTIMATE_CHAR_DIVISOR', 5.0))
IMAGE_TOKEN_ESTIMATE: int = int(os.getenv(
    'QWEN_AGENT_IMAGE_TOKEN_ESTIMATE', 255))  # Estimated tokens per image in message counting
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

    All fields have defaults matching current production values. Override by
    constructing a custom instance and passing it to ``InnerLoopDetector``.
    """

    # Memory bounds
    max_counter_entries: int = 200          # Max entries per Counter before pruning
    max_tokens: int = 1000                 # Max tokens in the sliding window

    # Activation thresholds
    default_min_chars: int = 4000          # Min chars to accumulate before full detection
    default_batch_interval: int = 1        # Run heavy checks every N-th feed call

    # Structural parameters (passed to InnerLoopDetector constructor)
    ngram_size: int = 64                   # Token window size for n-gram repetition
    block_size: int = 128                  # Token window size for block repetition
    entropy_window: int = 128             # Token window for Shannon entropy calculation
    char_run_limit: int = 70              # Max consecutive identical chars before alert
    score_threshold: int = 200            # Cumulative score to trigger loop detection

    # Detection thresholds (hardcoded in detection logic)
    sentence_repetition_threshold: int = 7   # Sentence count to flag repetition
    ngram_repetition_threshold: int = 5      # N-gram count to flag repetition
    block_repetition_threshold: int = 4      # Block count to flag repetition
    entropy_threshold: float = 2.0          # Shannon entropy below which a loop is suspected

    # Scoring
    score_decay_rate: float = 0.97         # Multiplicative decay per feed cycle