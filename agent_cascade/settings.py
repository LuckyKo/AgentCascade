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
from typing import Dict, List, Literal

from agent_cascade.constants import DEFAULT_SECURITY_DISABLED_TOOLS

# Settings for LLMs
DEFAULT_MAX_INPUT_TOKENS: int = int(
    os.getenv('AGENT_CASCADE_DEFAULT_MAX_INPUT_TOKENS',
              65000))  # The LLM will truncate the input messages if they exceed this limit

# Settings for agents
MAX_LLM_CALL_PER_RUN: int = int(os.getenv('AGENT_CASCADE_MAX_LLM_CALL_PER_RUN', 250))
DEFAULT_MAX_TURNS: int = int(os.getenv('AGENT_CASCADE_DEFAULT_MAX_TURNS',
                                       250))  # Default turn limit per agent execution
# ── Soft turn limit ──────────────────────────────────────────────────────────
# When enabled, a run continues past max_turns (with budget warnings + a wrap-up
# nudge every SOFT_TURN_LIMIT_WARNING_INTERVAL turns); this ceiling is the hard stop.
SOFT_TURN_LIMIT: bool = False
SOFT_TURN_LIMIT_WARNING_INTERVAL: int = 5
SOFT_TURN_LIMIT_ABSOLUTE_MAX: int = int(
    os.getenv('AGENT_CASCADE_SOFT_TURN_LIMIT_ABSOLUTE_MAX',
              1000))  # Hard ceiling for soft mode (effectuated turns per run)
# ── Token budget (cost/token-budget feature, Slice 2) ─────────────────────────
# Optional, ADDITIVE per-instance cap on GENERATED tokens (completion-only), a
# second mode of the turn budget. 0 (default) = disabled: turns-mode stays
# byte-for-byte unchanged and no token logic runs. When > 0, the run-loop budget
# warnings are driven by the cumulative generated-token accumulator (telemetry
# ``get_instance_generated_tokens``) instead of the turn count, so re-sent prompt
# context never spends the budget. The hard-cut toggle (``soft_turn_limit``) is
# reused — no new toggle. The ``$`` shown alongside is only rendered when the
# serving endpoint is priced (Slice 1 ``cost.py``).
DEFAULT_TOKEN_BUDGET: int = int(os.getenv('AGENT_CASCADE_DEFAULT_TOKEN_BUDGET', 0))
SECURITY_AGENT_MAX_TURNS: int = int(os.getenv('AGENT_CASCADE_SECURITY_AGENT_MAX_TURNS',
                                              10))  # Turn limit for system-launched Security advisor
# Skill Advisor: single semantic-match decision (read skill list + emit JSON verdict) — tight budget.
SKILL_ADVISOR_MAX_TURNS: int = int(
    os.getenv('AGENT_CASCADE_SKILL_ADVISOR_MAX_TURNS',
              10))  # Turn limit for the system-launched Skill Advisor (semantic skill matcher)
# Skill Advisor pre-filter cap: max candidate skills passed into the advisor prompt.
# Coarse keyword pre-filter (SkillMatcher) ranks skills against task+context; when fewer than
# this many match, ALL skills are passed instead (safe fallback — no regression). Raise if the
# corpus grows well past ~100 and token budget demands a tighter cap.
SKILL_ADVISOR_MAX_CANDIDATES: int = int(
    os.getenv('AGENT_CASCADE_SKILL_ADVISOR_MAX_CANDIDATES',
              20))  # Top-N skill candidates in the advisor prompt (keyword pre-filter)

# ── Debug-verbosity switches (BUG_0291) ──────────────────────────────────────────
# Extra per-module slot/scheduler tracing, OFF by default ('0'/unset = off). A normal
# acquire+release already emits one structured line per transition; set
# AGENT_CASCADE_DEBUG_SLOTS=1 to additionally emit the redundant per-module duplicates
# (EndpointScheduler.acquire internals, SLOT_REACQUIRED, router recovery,
# last_released_endpoint, SECURITY_SLOT_*), the per-turn [MEMORY_HINT] submit line, and
# the toggle-off [STATE_SAVE_SKIP]/[STATE_RESTORE_SKIP] repeats. Deliberately NOT derived
# from AGENT_CASCADE_DEBUG — a routine DEBUG run stays quiet (same rationale as
# AGENT_CASCADE_DEBUG_HTTP; see log.py). Gated sites read settings.DEBUG_SLOTS so tests can
# monkeypatch it.
DEBUG_SLOTS: bool = os.getenv('AGENT_CASCADE_DEBUG_SLOTS', '0').strip().lower() in ('1', 'true')

# ── Post-compression MCP tool unload ──────────────────────────────────────────
# After a successful compression, drop discovered MCP (deferred) tools that were not
# used: the keep-set is every MCP tool SUCCESSFULLY invoked since the last compression (agent_instance._mcp_tools_used)
# ∪ every discovered MCP tool named in the post-compression summary text; everything
# else is unloaded (no longer offered) and _mcp_tools_used is cleared (window boundary). The unload is announced as ONE marker queued onto the next tool result
# so it survives session reload (see pool/session_io re-derive). The
# deferred-tool token win is an optimization, not a correctness fix, so this ships
# dark: set AGENT_CASCADE_UNLOAD_UNUSED_TOOLS_POST_COMPRESSION=1 to enable. Off
# (default) ⇒ byte-identical behavior to append-only discovery (no marker queued,
# re-derive unchanged). Gated sites read settings.UNLOAD_UNUSED_TOOLS_POST_COMPRESSION
# so tests can monkeypatch it.
UNLOAD_UNUSED_TOOLS_POST_COMPRESSION: bool = os.getenv(
    'AGENT_CASCADE_UNLOAD_UNUSED_TOOLS_POST_COMPRESSION', '0'
).strip().lower() in ('1', 'true')

# ── Lazy / on-use MCP server connect (BUG_0310 follow-up) ────────────────────
# Default ON. Boot PARKS every enabled MCP server — it registers stub tool objects carrying
# real schema (read from config/mcp_tools_cache.json) into every template's function_map and
# connects ZERO of them, removing the synchronous `mcp_connect` boot phase (warm median
# ≈3.7 s; see [[bug0310-boot-baselines-mcp-is-hot-skill-scan-is-not]]). A parked server
# connects lazily on first use of one of its tools (tool_search unlock + a dispatcher
# pre-flight backstop) under a per-server single-flight guard. If ALL of a server's tools
# become disabled for every agent it is turned off (disconnected + suppressed); re-enabling
# restores connectability. Set AGENT_CASCADE_MCP_LAZY_CONNECT=0 to restore the exact previous
# eager connect-at-boot path (kill-switch). Tests patch this module attribute. See
# [[ac-mcp-lazy-connect-gotchas]].
MCP_LAZY_CONNECT: bool = os.getenv('AGENT_CASCADE_MCP_LAZY_CONNECT', '1').strip().lower() not in ('0', 'false', '')
# Per-step wall-clock cap (connect handshake AND tools/list, applied to each) for the lazy
# on-use connect. Default 30.0 — matches the value connect_server hardcoded before it was
# parameterized, so enabling lazy mode does not silently make connects more likely to time
# out. The dispatcher's DMS `_in_blocking_tool` bound around a connect is derived from THIS
# value (mcp_manager.lazy_connect_dms_bound = 2× this + teardown margin), so a legitimately
# slow connect is never DMS-terminated in its tail.
MCP_LAZY_CONNECT_TIMEOUT_S: float = float(os.getenv('AGENT_CASCADE_MCP_LAZY_CONNECT_TIMEOUT_S', 30.0))
# When the tool-metadata cache is cold/stale for a parked server, boot spawns ONE bounded
# one-shot daemon thread that does connect→tools/list→disconnect to populate
# config/mcp_tools_cache.json off the boot thread (so the synchronous `mcp_connect` phase
# still measures ≈0). Set AGENT_CASCADE_MCP_LAZY_COLD_INTERROGATE=0 to suppress it — e.g.
# the BUG_0310 boot probe (plans/bug0310_probe.py), which must not spawn a real chrome-devtools
# child to measure the stub-registration-only phase. Off ⇒ a cold cache simply stays cold;
# tools surface once metadata is cached (next boot with the flag on).
MCP_LAZY_COLD_INTERROGATE: bool = os.getenv('AGENT_CASCADE_MCP_LAZY_COLD_INTERROGATE', '1').strip().lower() not in ('0', 'false', '')

# ── AFK nudge mode (basic | admin) ─────────────────────────────────────────────
# 'basic' keeps the current AFK behavior (auto-reject + canned idle nudge). 'admin'
# spawns a read-only Admin agent that reviews the idle session and hands back the
# continuation message. Admin is a per-call fresh spawn (like call_agent children) —
# it never seeds or shares the fixed Security warm-reuse name.
def _normalize_afk_mode(value) -> str:
    """Normalize an AFK mode value to 'basic' | 'admin'; non-string/unknown -> 'basic'."""
    v = value.strip().lower() if isinstance(value, str) else ''
    return 'admin' if v == 'admin' else 'basic'


# Public alias: the canonical normalizer (runtime_state re-exports it for its consumers).
normalize_afk_mode = _normalize_afk_mode

AFK_MODE_DEFAULT: str = _normalize_afk_mode(os.getenv('AGENT_CASCADE_AFK_MODE', 'basic'))
AFK_ADMIN_MAX_TURNS: int = int(
    os.getenv('AGENT_CASCADE_AFK_ADMIN_MAX_TURNS', 30))  # Turn budget for the AFK Admin agent
AFK_ADMIN_FIRST_YIELD_TIMEOUT: float = float(
    os.getenv('AGENT_CASCADE_AFK_ADMIN_FIRST_YIELD_TIMEOUT', 45.0))  # First-yield guard (s)
# Fixed instance_name of the session-continuing AFK Admin call_agent child. Re-called on every
# AFK window so the child's session (and KV) persists across windows; the reaper provides the
# fresh view once it goes idle past AGENT_IDLE_TIMEOUT. Must match the name the root passes in
# its AFK trigger text — any drift on re-call is hard-rejected by the call_agent identity guard.
AFK_ADMIN_INSTANCE_NAME: str = (
    os.getenv('AGENT_CASCADE_AFK_ADMIN_INSTANCE_NAME') or 'AFK_Admin')

# ── Security agent warm reuse ──────────────────────────────────────────────────
# Reuse a single warm Security instance across security checks so the LLM prompt prefix
# stays byte-identical and hits the server-side KV cache, instead of spawning a fresh
# Security_<rid> per check. Default ON; flip AGENT_CASCADE_SECURITY_REUSE=0 to reproduce
# today's fresh-spawn-per-check behavior exactly (the mandatory env kill-switch).
SECURITY_REUSE_ENABLED: bool = os.getenv('AGENT_CASCADE_SECURITY_REUSE',
                                         '1').strip().lower() not in ('0', 'false', 'off', 'no')
# Fixed instance name for the shell_cmd / tool-approval path (security_handler.py). A fixed
# name is REQUIRED for the cache win — a per-rid name changes the prompt bytes every check.
SECURITY_REUSE_APPROVAL_NAME: str = os.getenv('AGENT_CASCADE_SECURITY_REUSE_APPROVAL_NAME', 'Security_guard')
# Fixed instance name for the skill-advisor path (advisor_runner.py). Deliberately distinct
# from the approval name: that path holds no lock, so it must not share one warm object.
SECURITY_REUSE_SKILL_ADVISOR_NAME: str = os.getenv('AGENT_CASCADE_SECURITY_REUSE_SKILL_ADVISOR_NAME', 'Security_skilladvisor')

# ── Compressor agent warm reuse ───────────────────────────────────────────────
# BUG_0330: reuse one warm Compressor (+ one warm CompressorReview) across compressions/
# consolidations/reviews so the stable conversation[0] prefix hits the server-side KV cache
# instead of a fresh Compressor_<n> / CompressionReviewer_<n> per op. Default ON; set
# AGENT_CASCADE_COMPRESSOR_REUSE=0 (or false/off/no) to fall back to the legacy per-op
# fresh-name behavior (byte-identical to pre-feature: the warm names are never created).
COMPRESSOR_REUSE_ENABLED: bool = os.getenv('AGENT_CASCADE_COMPRESSOR_REUSE',
                                           '1').strip().lower() not in ('0', 'false', 'off', 'no')
# Fixed instance name for the warm Compressor (L1 compression + L2 consolidation share it: same
# soul, same prefix; they already share the Compressor_<n> name space today). A fixed name is
# REQUIRED for the cache win — a per-op name changes the prompt bytes every op.
COMPRESSOR_WARM_NAME: str = os.getenv('AGENT_CASCADE_COMPRESSOR_WARM_NAME', 'Compressor_warm')
# Fixed name for the warm compaction reviewer. Deliberately distinct from the summarizer warm
# name: the reviewer runs the Compressor class with a different task prompt and config
# (tool-less, max_turns=1), and a distinct claim keeps a held summarizer from blocking a review.
COMPRESSOR_REVIEWER_WARM_NAME: str = os.getenv(
    'AGENT_CASCADE_COMPRESSOR_REVIEWER_WARM_NAME', 'CompressorReviewer_warm')
# Insurance cap (defense-in-depth): N > 0 retires + re-seeds the warm instances after N
# successful reuses. Default 0 = off, matching Security's uncapped warm behavior. Revisit only
# if telemetry shows a prompt-DNA drift signal (start at N=100 then).
COMPRESSOR_REUSE_MAX_REUSES: int = int(os.getenv('AGENT_CASCADE_COMPRESSOR_REUSE_MAX_REUSES', 0))
# Max seconds an L2 consolidation waits for a HELD Compressor_warm claim — e.g. one held by a
# concurrent in-flight L1 base compression — before falling back to a fresh Compressor_<n>,
# instead of immediately abandoning KV-cache reuse. Covers only the transient "another op is
# mid-run" case; a miss on a FREE claim (instance ineligible / empty-after-release) is never waited on.
# 0 disables the wait entirely (immediate fresh-spawn fallback).
COMPRESSOR_REUSE_CLAIM_WAIT_SECONDS: float = float(
    os.getenv('AGENT_CASCADE_COMPRESSOR_REUSE_CLAIM_WAIT', '60'))

# ── System-launched advisor toolset ───────────────────────────────────────────
# Disabled tools for SYSTEM-LAUNCHED advisors (the Security guard and the Skill Advisor).
# Deliberately separate from the UI per-agent disabled_tools map: the UI setting governs
# user- and agent-called sub-agents, while system advisors use this fixed set so a UI edit
# cannot widen or narrow their capabilities.
#
# The default is the 18-tool set the Security guard was actually running with (mirrors the
# live config/pool_settings.json "Security" entry, 2026-11-16): the seed
# (DEFAULT_SECURITY_DISABLED_TOOLS - {'shell_cmd'}) with scan_skills dropped and
# image_gen / tool_search / send_message / media_gen / call_cli_agent / view_image added.
# System agents keep read-only shell via restricted_shell=True, so shell_cmd stays out.
# Comma-separated tool names; empty string disables nothing (0 = no restrictions).
_ADVISOR_DISABLED_TOOLS_DEFAULT = ','.join(sorted(
    (DEFAULT_SECURITY_DISABLED_TOOLS - {'shell_cmd', 'scan_skills'})
    | {'image_gen', 'tool_search', 'send_message', 'media_gen', 'call_cli_agent', 'view_image'}
))
ADVISOR_AGENT_DISABLED_TOOLS: frozenset[str] = frozenset(
    t.strip() for t in os.getenv('AGENT_CASCADE_ADVISOR_DISABLED_TOOLS',
                                 _ADVISOR_DISABLED_TOOLS_DEFAULT).split(',') if t.strip())
if not ADVISOR_AGENT_DISABLED_TOOLS:
    # An empty set is a deliberate escape hatch, so we do not refuse to start — but say it loudly:
    # the SYSTEM-launched Security guard / Skill Advisor would run with no tool restrictions at all.
    from agent_cascade.log import logger as _advisor_toolset_logger
    _advisor_toolset_logger.warning(
        '[ADVISOR-TOOLSET] AGENT_CASCADE_ADVISOR_DISABLED_TOOLS resolved to an EMPTY set — the '
        'system-launched Security guard / Skill Advisor runs UNGUARDED, with destructive tools '
        '(write_file, edit_file, delete_file, code_interpreter, call_cli_agent, ...) ENABLED. '
        'Unset the env var to restore the default protection: %s', _ADVISOR_DISABLED_TOOLS_DEFAULT)
# ── Security approval cache ───────────────────────────────────────────────────
# Caches Security-guard verdicts keyed by tool-call signature (tool_name + sha1 of
# canonical tool_args) so repeated identical guard reviews collapse into one LLM turn.
# Default ON; flip AGENT_CASCADE_SECURITY_APPROVAL_CACHE=0 to reproduce today's
# review-every-time behavior exactly (the mandatory env kill-switch).
SECURITY_APPROVAL_CACHE_ENABLED: bool = os.getenv('AGENT_CASCADE_SECURITY_APPROVAL_CACHE',
                                                 '1').strip().lower() not in ('0', 'false', 'off', 'no')
# TTL (seconds) a cached verdict stays authoritative before the guard re-reviews it.
# This is a staleness bound, NOT a disable toggle — the kill-switch is
# AGENT_CASCADE_SECURITY_APPROVAL_CACHE above. A single LLM verdict governs all
# repeats of a signature for this window, so a fluke verdict (unlucky [NO] on a
# safe command, [YES] on a borderline one) is amplified across repeats; the TTL
# is the bound on that exposure.
SECURITY_APPROVAL_CACHE_TTL_SECONDS: float = float(os.getenv(
    'AGENT_CASCADE_SECURITY_APPROVAL_CACHE_TTL', 300))
# Compressor: its tool restrictions come from UI config (seeded by the one-time class-defaults
# migration, which disables nearly all of its tools) — it only performs compression internally.
# A single turn forces no tool calls, guaranteeing a pure one-shot summary. Bump only if the
# Compressor is ever given tools back.
COMPRESSOR_AGENT_MAX_TURNS: int = int(os.getenv('AGENT_CASCADE_COMPRESSOR_AGENT_MAX_TURNS',
                                                1))  # Turn limit for the system-launched Compressor agent
MAX_AUTO_CONTINUE_ATTEMPTS: int = 5  # Max consecutive auto-continue attempts per episode before giving up (each attempt consumes one real turn)
# Reasoning-only soft "continue" (pure-resend) attempts before falling back to a full retry.
# Soft continues share the MAX_AUTO_CONTINUE_ATTEMPTS budget with full retries, so an episode is
# bounded at exactly min(N, cap) soft + (cap - min(N, cap)) full attempts.
REASONING_ONLY_CONTINUE_ATTEMPTS: int = int(os.getenv('AGENT_CASCADE_REASONING_ONLY_CONTINUE_ATTEMPTS', 2))
# Deferred escape hatch for the soft-continue path. When False (default), a reasoning-only soft
# continue is a PURE RESEND: no new message is appended and nothing is rolled back — the LLM is
# simply re-called on the same history with the reasoning message still in place. When True, an
# escalating USER nudge ("stop thinking / produce output") is injected on each soft continue so the
# model gets an explicit instruction instead of a bare resend. Kept behind a flag so it can be
# enabled later without re-architecting.
SOFT_CONTINUE_NUDGE_ENABLED: bool = os.getenv('AGENT_CASCADE_SOFT_CONTINUE_NUDGE', '0') == '1'

# Trust boundary (OWASP LLM01, indirect prompt injection): when ON, untrusted tool
# outputs are wrapped in <tag> delimiters and a standing system-prompt line tells the
# model to treat delimited content as data, never instructions. Default OFF — no
# behavior change unless AGENT_CASCADE_UNTRUSTED_TOOL_OUTPUT=1.
UNTRUSTED_TOOL_OUTPUT_WRAPPER: bool = os.getenv('AGENT_CASCADE_UNTRUSTED_TOOL_OUTPUT', '0') == '1'
# Single source of truth for the trust-boundary delimiter tag, shared by the
# tool-output wrapper (engine/tool_execution.py) and the system-prompt line
# (engine/turn_setup.py).
UNTRUSTED_TOOL_OUTPUT_TAG: str = 'untrusted_tool_output'

# ── Memory vault write screen (OWASP LLM01, persistent indirect prompt injection) ──
# Screen writes into .agent_lessons/ and admin_suggestion_box/ for instruction-injection
# patterns before they hit disk. This is the only PERSISTENT injection vector in AC — a
# poisoned vault note is re-loaded as context by every future agent. Default ON: it rejects
# only obviously malicious directives (pattern list + zero-false-positive validation live in
# _INJECTION_PATTERNS in operation_manager/file_operations.py). Disable with
# AGENT_CASCADE_MEMORY_VAULT_WRITE_SCREEN=0.
MEMORY_VAULT_WRITE_SCREEN: bool = os.getenv('AGENT_CASCADE_MEMORY_VAULT_WRITE_SCREEN', '1') == '1'


def _resolve_default_workspace() -> str:
    """Resolve DEFAULT_WORKSPACE at import time.

    Priority:
    1. AGENT_CASCADE_DEFAULT_WORKSPACE env var (if set)
    2. Docker mount point /workspace (only if running inside a Docker container)
    3. Sibling AgentWorkspace directory relative to project root (e.g., ../AgentWorkspace from agent_cascade/)
    4. workspace/ under project root

    This ensures images/logs go to the correct workspace regardless of CWD when server starts.
    """
    env_val = os.getenv('AGENT_CASCADE_DEFAULT_WORKSPACE')
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
# Global tool-result safety net: any tool result over this trigger is cut to
# DEFAULT_WILD_READ_TRUNCATION_CHARS with a spillover pointer + warning. Default 25000 chars.
DEFAULT_TOOL_RESULT_MAX_CHARS: int = int(os.getenv('AGENT_CASCADE_TOOL_RESULT_MAX_CHARS', 25000))
# Truncation CUT for wild reads / over-limit results (when the trip threshold above is
# exceeded). Content beyond this window is probably useless, so the agent gets just enough
# to see what's there. Default 4000 chars.
DEFAULT_WILD_READ_TRUNCATION_CHARS: int = int(os.getenv('AGENT_CASCADE_WILD_READ_TRUNCATION_CHARS', 4000))
DEFAULT_READ_FILE_MAX_LINES: int = int(os.getenv('AGENT_CASCADE_READ_FILE_MAX_LINES', 120))  # DEPRECATED — not used for no-limit reads
# read_file default CHAR truncation for a NO-LIMIT read (limit is None): the body is cut to
# this many rendered chars at a whole-line boundary (last line kept complete, never chopped).
# A bounded read (explicit limit/range) is NOT char-truncated — the requested range wins.
# Reads incrementally to ~this size so memory is O(window), not O(file). UI + env:
# read_file_max_chars / AGENT_CASCADE_READ_FILE_MAX_CHARS. -1/0 DISABLES (falls back to the
# 25k global net only).
DEFAULT_READ_FILE_MAX_CHARS: int = int(os.getenv('AGENT_CASCADE_READ_FILE_MAX_CHARS', 4000))
# Wall-clock ceiling (seconds) for a single grep search before it aborts with a
# "narrow your search" error. Kept low so a pathological wide search can't hang the
# agent for tens of seconds; ripgrep searches normally finish well under this.
DEFAULT_GREP_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_GREP_TIMEOUT', 5.0))
# ripgrep is told not to STREAM files larger than this as text. With `--text`
# (required so NUL-byte files are not silently skipped) a multi-GB model artifact is read
# byte-by-byte, blows the grep timeout and reports a misleading "scope too broad" error.
# Such files are instead skipped by the streaming search and only their FIRST
# GREP_LARGE_FILE_HEAD_KB KiB are scanned (enough for JSON/model headers), with the skip
# reported in the result. Set to 0 to disable the cap (old, unbounded behaviour).
GREP_MAX_FILE_SIZE_MB: int = int(os.getenv('AGENT_CASCADE_GREP_MAX_FILE_SIZE_MB', 100))
GREP_LARGE_FILE_HEAD_KB: int = int(os.getenv('AGENT_CASCADE_GREP_LARGE_FILE_HEAD_KB', 64))
# Safety caps for the header scan of over-size files: never scan more than this many files
# or spend more than this many seconds walking/stat-ing the scope to find them.
GREP_LARGE_FILE_SCAN_LIMIT: int = int(os.getenv('AGENT_CASCADE_GREP_LARGE_FILE_SCAN_LIMIT', 50))
GREP_LARGE_FILE_SCAN_BUDGET_S: float = float(
    os.getenv('AGENT_CASCADE_GREP_LARGE_FILE_SCAN_BUDGET_S', 0.75))
# One matched line is not necessarily one line of text — a JSONL record (or any
# minified log) can be 5-500 KB on a single line. The char budget is applied to rendered
# entries, so without a per-line cap a handful of JSONL matches consume the whole budget and
# dump megabytes into the spillover file. Each rendered content column is abbreviated to
# this many characters (0 disables the cap); the omitted length is disclosed on the line so
# the agent can re-target with read_logs/read_file. Kept generous enough that ordinary code
# lines are byte-identical to before.
GREP_MAX_LINE_CHARS: int = int(os.getenv('AGENT_CASCADE_GREP_MAX_LINE_CHARS', 500))

DEFAULT_HEURISTIC_MATCH_THRESHOLD: float = float(os.getenv('AGENT_CASCADE_HEURISTIC_MATCH_THRESHOLD', 0.90))
# Wall-clock ceiling (seconds) for the CPU-bound document-parse step in web_extractor /
# SimpleDocParser. The network download is already bounded by _HTTP_FETCH_TIMEOUT
# (utils.py); this bounds pdfminer/pdfplumber etc., which can run for minutes on a
# large/corrupt PDF. On timeout the parse is abandoned in a worker thread and a clean
# DocParserError is returned to the agent.
# PDF gets the longest budget (pdfminer on a 100-page PDF can take 30-90s); fast
# text-based types (txt/html/csv/tsv/md) fall back to the default below.
WEB_EXTRACTOR_PARSE_TIMEOUT_SECONDS: float = float(
    os.getenv('AGENT_CASCADE_WEB_EXTRACTOR_PARSE_TIMEOUT', 30.0))  # default for fast types
WEB_EXTRACTOR_PARSE_TIMEOUT_BY_TYPE: dict = {
    'pdf': 120.0,
    'docx': 60.0,
    'pptx': 60.0,
    'xlsx': 60.0,
    'xls': 60.0,
}
# Pool size for the shared parse-timeout worker executor (module-level in
# simple_doc_parser.py). One hung parse occupies one worker; the rest stay available.
WEB_EXTRACTOR_PARSE_WORKERS: int = int(os.getenv('AGENT_CASCADE_WEB_EXTRACTOR_PARSE_WORKERS', 4))

# Settings for RAG
DEFAULT_MAX_REF_TOKEN: int = int(os.getenv('AGENT_CASCADE_DEFAULT_MAX_REF_TOKEN',
                                           20000))  # The window size reserved for RAG materials
DEFAULT_PARSER_PAGE_SIZE: int = int(os.getenv('AGENT_CASCADE_DEFAULT_PARSER_PAGE_SIZE',
                                              500))  # Max tokens per chunk when doing RAG
DEFAULT_RAG_KEYGEN_STRATEGY: Literal['None', 'GenKeyword', 'SplitQueryThenGenKeyword', 'GenKeywordWithKnowledge',
                                     'SplitQueryThenGenKeywordWithKnowledge'] = os.getenv(
                                         'AGENT_CASCADE_DEFAULT_RAG_KEYGEN_STRATEGY', 'GenKeyword')
DEFAULT_RAG_SEARCHERS: List[str] = ast.literal_eval(
    os.getenv('AGENT_CASCADE_DEFAULT_RAG_SEARCHERS',
              "['keyword_search', 'front_page_search']"))  # Sub-searchers for hybrid retrieval

# Settings for compression
DEFAULT_COMPRESSION_COOLDOWN_SECONDS: float = float(
    os.getenv('AGENT_CASCADE_DEFAULT_COMPRESSION_COOLDOWN_SECONDS',
              2.0))  # Minimum seconds between forced compressions to prevent thrashing
DEFAULT_COMPRESSION_MAX_ATTEMPTS: int = int(
    os.getenv('AGENT_CASCADE_COMPRESSION_MAX_ATTEMPTS',
              100))  # Safety net: max forced compressions before terminating
COMPRESSION_FORCE_THRESHOLD: float = float(os.getenv('AGENT_CASCADE_COMPRESSION_FORCE_THRESHOLD',
                                                     96.0))  # Force compress at X% token usage
COMPRESSION_WARNING_THRESHOLD: float = float(os.getenv('AGENT_CASCADE_COMPRESSION_WARNING_THRESHOLD',
                                                       90.0))  # Warn at X% token usage
COMPRESSION_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_COMPRESSION_TIMEOUT',
                                             120.0))  # Max seconds for compression to complete
DEFAULT_COMPRESSION_PROACTIVE_THRESHOLD: float = float(
    os.getenv('AGENT_CASCADE_DEFAULT_COMPRESSION_PROACTIVE_THRESHOLD',
              95.0))  # Proactive compress at X% usage (post-tool, async drain checks)
DEFAULT_COMPRESSION_CONTEXT_RESERVE_TOKENS: int = int(
    os.getenv('AGENT_CASCADE_COMPRESSION_CONTEXT_RESERVE_TOKENS',
              3000))  # Tokens reserved for LLM call overhead (system prompt, function schemas, reasoning)
COMPRESSION_OVERFLOW_TOLERANCE_PCT: float = float(
    os.getenv('AGENT_CASCADE_COMPRESSION_OVERFLOW_TOLERANCE_PCT',
              3.0))  # Tolerance margin for overflow detection before raising exception
# Recount threshold: when delta token estimates are unavailable, force a full recount
# if cached usage already exceeds this fraction of the allocated max (cache may be stale).
COMPRESSION_RECOUNT_THRESHOLD: float = float(
    os.getenv('AGENT_CASCADE_COMPRESSION_RECOUNT_THRESHOLD',
              0.85))  # Force full recount at X fraction of allocated max when cache invalidated
# Immutable env-var seed only — used at import time to initialize
# runtime_state.state.compression_fraction. The LIVE mutable value lives on the state singleton;
# this name is NOT a runtime setting and must not be read or mutated by consumers.
COMPRESSION_DEFAULT_FRACTION_SEED: float = float(os.getenv('AGENT_CASCADE_COMPRESSION_DEFAULT_FRACTION',
                                                           0.7))  # Default fraction of history to discard (70%)
COMPRESSION_MIN_USAGE_PCT: float = float(os.getenv('AGENT_CASCADE_COMPRESSION_MIN_USAGE_PCT',
                                                    50.0))  # Refuse agent-triggered compression below X% context usage
COMPRESSION_MIN_FRACTION: float = float(os.getenv('AGENT_CASCADE_COMPRESSION_MIN_FRACTION',
                                                  0.1))  # Minimum allowed compression fraction
COMPRESSION_MAX_FRACTION: float = float(os.getenv('AGENT_CASCADE_COMPRESSION_MAX_FRACTION',
                                                  0.9))  # Maximum allowed compression fraction
COMPRESSION_SECURITY_CHECK_TIMEOUT: float = float(os.getenv(
    'AGENT_CASCADE_COMPRESSION_SECURITY_CHECK_TIMEOUT', 120.0))  # Max seconds for security advisor during compression
COMPRESSION_MAX_RETRIES: int = int(
    os.getenv('AGENT_CASCADE_COMPRESSION_MAX_RETRIES',
              5))  # Max retry attempts for compression agent invocation on marker validation failure

# Hierarchical memory consolidation settings
COMPRESSION_CONSOLIDATION_THRESHOLD: int = int(os.getenv('AGENT_CASCADE_COMPRESSION_CONSOLIDATION_THRESHOLD',
                                                         8))  # Markers at which to trigger consolidation
COMPRESSION_MAX_CONSOLIDATION_TOKENS: int = int(os.getenv('AGENT_CASCADE_COMPRESSION_MAX_CONSOLIDATION_TOKENS',
                                                          100000))  # Max tokens for consolidation input before aborting

# Post-consolidation data-integrity validation gate (L2 hop only). The reviewer is a separate,
# tool-less Compressor instance (no dedicated soul). Advisory-on-failure: a validation failure never
# blocks compaction nor turns a successful compaction into a failure.
# COMPRESSION_REVIEWER_ENABLED=False disables the gate entirely, making consolidation byte-identical
# to the pre-gate behavior.
COMPRESSION_REVIEWER_ENABLED: bool = os.getenv('AGENT_CASCADE_COMPRESSION_REVIEWER_ENABLED',
                                               'true').lower() in ('1', 'true', 'yes',
                                                   'on')  # Run the compaction-reviewer gate on each L2 consolidation hop (default ON)
COMPRESSION_REVIEW_MAX_RETRIES: int = int(
    os.getenv('AGENT_CASCADE_COMPRESSION_REVIEW_MAX_RETRIES',
              2))  # Extra consolidation attempts on a LOSS verdict (re-pays 2 LLM calls each; small bound on purpose)

# The compaction reviewer is a single-shot diff-check: it must never iterate. Its max_turns is
# deliberately NOT tied to COMPRESSOR_AGENT_MAX_TURNS so a future bump of the Compressor budget
# cannot silently grant the reviewer multi-turn tool loops. Keep this at 1.
COMPACTION_REVIEWER_MAX_TURNS: int = int(os.getenv('AGENT_CASCADE_COMPACTION_REVIEWER_MAX_TURNS', 1))  # Single-shot reviewer turn budget (decoupled from the Compressor).

# Compression agent invocation timeout (5 minutes for large compression/consolidation tasks)
COMPRESSION_AGENT_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_COMPRESSION_AGENT_TIMEOUT', 300.0))

# Hard cap on the <last_supervisor_task> block appended to a compression marker.
# Bounds marker growth and its tokens_after accounting; tasks over this are truncated with an
# explicit "[truncated]" suffix so the agent knows the text is partial.
COMPRESSION_MAX_LAST_TASK_CHARS: int = int(os.getenv('AGENT_CASCADE_COMPRESSION_MAX_LAST_TASK_CHARS', 4000))

# Settings for agent pool
AGENT_IDLE_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_IDLE_TIMEOUT',
                                            1600.0))  # Auto-dismiss regular agents after X seconds inactivity
SYSTEM_AGENT_IDLE_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_SYSTEM_AGENT_IDLE_TIMEOUT',
                                                    300.0))  # Auto-dismiss Compressor/Security after X seconds inactivity (5 min)
AGENT_IDLE_CHECK_INTERVAL: float = float(os.getenv('AGENT_CASCADE_IDLE_CHECK_INTERVAL', 60.0))  # Check every N seconds
# Holder-side dead-man's-switch window: a conc=0 slot holder with NO turn-progress
# (last_activity) for >= this many seconds while NOT in a legitimately-bounded operation
# (approval / media-gen / in-bounded blocking tool / in-flight LLM call) is terminated by
# the idle-checker to free the wedged slot. Default 300s; must stay >= the largest
# un-excluded tool bound.
HOLDER_DMS_WINDOW_S: float = float(os.getenv('AGENT_CASCADE_HOLDER_DMS_WINDOW_S', 300.0))
# Fallback DMS bound for a synchronous tool that declares NO finite bound (BaseTool.dms_bound()
# returns None). The dispatch boundary carries this as the marker bound so an unbounded marker can
# never shield a holder forever. Default 900s == the media/image fallback
# ceiling; must stay >= any legitimate sync tool's max bound or genuine long ops get killed.
HOLDER_DMS_MAX_BOUND_S: float = float(os.getenv('AGENT_CASCADE_HOLDER_DMS_MAX_BOUND_S', 900.0))
# Max outputs from ONE media_gen batch call (len(prompts) × variations), BUG_0350. Guards GPU
# time, the holder-DMS shield (dms_bound scales with batch size) and (for vision agents)
# context growth from N images in one tool result. 0 = off (uncapped). Env-only knob (no UI).
MEDIA_GEN_MAX_BATCH: int = int(os.getenv('AGENT_CASCADE_MEDIA_GEN_MAX_BATCH', 8))
AGENT_MAX_AUTO_ROLLBACKS: int = int(os.getenv('AGENT_CASCADE_MAX_AUTO_ROLLBACKS', 5))  # Max loop recovery retries
# ── Two-tier loop detection ──
# Tier 1 — exact matcher (exact_loop_detect.py). Rollback still respects auto_rollback_on_loop / max_auto_rollbacks.
LOOP_EXACT_ROLLBACK_ENABLED: bool = os.getenv('AGENT_CASCADE_LOOP_EXACT_ROLLBACK',
                                              '1') == '1'  # Tier 1 runs and rolls back on exact hits
# Tier 2 — fuzzy detector (tool_loop_detect.py), warning-first: ONE advisory per run, no destructive path.
LOOP_FUZZY_WARNING_ENABLED: bool = os.getenv('AGENT_CASCADE_LOOP_FUZZY_WARNING',
                                             '1') == '1'  # Tier 2 runs and may inject the advisory
# Tier 2 escalation: fuzzy loop persisting FUZZY_ESCALATION_TURNS turns after the warning → full rollback. Off by default.
TOOL_LOOP_FUZZY_ROLLBACK_ENABLED: bool = os.getenv('AGENT_CASCADE_TOOL_LOOP_FUZZY_ROLLBACK',
                                                   '0') == '1'  # Escalation off by default
# DEPRECATED — legacy kill switch: can DISABLE the fuzzy tier (LOOP_FUZZY_WARNING_ENABLED AND this) but never enable it.
TOOL_LOOP_DETECTION_ENABLED: bool = os.getenv('AGENT_CASCADE_TOOL_LOOP_DETECTION',
                                              '1') == '1'  # Legacy kill switch for the fuzzy tier
# Tier 2 (fuzzy) similarity floor: minimum difflib SequenceMatcher.ratio() between
# consecutive near-duplicate cores for a Layer 2 run to chain. Applies to all tools.
TOOL_LOOP_SIM_THRESHOLD: float = float(os.getenv('AGENT_CASCADE_TOOL_LOOP_SIM_THRESHOLD',
                                                 '0.95'))  # Default 0.95 (unchanged behavior unless opted in)
AGENT_MAX_NESTING_DEPTH: int = int(os.getenv('AGENT_CASCADE_MAX_NESTING_DEPTH', 10))  # Max depth of nested agent calls
AGENT_MAX_WORKERS: int = int(os.getenv('AGENT_CASCADE_MAX_WORKERS', 3))  # ThreadPoolExecutor workers
# DEPRECATED — unused; kept for backward compatibility.
AGENT_SLEEPING_TIMEOUT: float = float(
    os.getenv('AGENT_CASCADE_SLEEPING_TIMEOUT',
              300.0))  # DEPRECATED — unused
AGENT_SLEEPING_WAKEUP_INTERVAL: float = float(os.getenv('AGENT_CASCADE_SLEEPING_WAKEUP_INTERVAL',
                                                        5.0))  # Wakeup log interval while SLEEPING
# Conservative estimate used for compression template overhead estimation.
# Counts system prompt overhead and structural tokens, so a higher divisor
# (more chars per token) yields safer/more conservative estimates.
CHARS_PER_TOKEN_ESTIMATE: float = float(os.getenv('AGENT_CASCADE_CHARS_PER_TOKEN_ESTIMATE', 5.0))

# Settings for forget_last tool
DEFAULT_FORGET_LAST_TRUNCATE_MAX_CHARS: int = int(os.getenv(
    'AGENT_CASCADE_FORGET_LAST_TRUNCATE_MAX_CHARS', 100))  # Maximum characters to keep when truncating tool responses
DEFAULT_FORGET_LAST_MIN_CHAR_LIMIT: int = int(
    os.getenv('AGENT_CASCADE_FORGET_LAST_MIN_CHAR_LIMIT',
              200))  # Skip truncation for responses ≤ this size (too small to benefit from truncation)

# Settings for endpoint scheduling
ENDPOINT_SLOT_ACQUIRE_TIMEOUT: int = int(os.getenv('AGENT_CASCADE_ENDPOINT_SLOT_ACQUIRE_TIMEOUT',
                                                   30))  # Timeout in seconds for acquiring endpoint scheduling slots

# Bounded FAST re-acquire window (post-yield fast path); on timeout the instance
# re-enters FIFO at tail (unbounded).
REACQUIRE_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_REACQUIRE_TIMEOUT', 30.0))

# Bounded TAIL re-acquire after a post-yield fast-path miss. Deliberately a
# SEPARATE, SHORT, LITERAL bound: this call does NOT pass `pool=`, so the holder-activity
# resolver is absent and the dead-man's-switch window can never reset here. Without an
# explicit cap the wait would inherit the user's window and then the derived 6x hard cap —
# i.e. a Security check could hold its worker for up to 6x the user's setting. Passed as
# BOTH timeout and hard_cap, which makes the sliding logic inert (the two deadlines
# coincide and the cap fires first) — a plain fixed wait.
POST_YIELD_REACQUIRE_TIMEOUT: float = 120.0


# HTTP timeout for state save/restore POSTs to the autoloader. A restore may
# cold-start a multi-GB model first; override with AGENT_CASCADE_STATE_HTTP_TIMEOUT
# for rigs with slower cold-starts.
STATE_HTTP_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_STATE_HTTP_TIMEOUT', 120.0))

# Per-endpoint reasoning effort values (UI pulldown → LLM API `reasoning_effort`).
# "none" means the param is NOT sent (model uses default behavior).
# "xhigh" maps to "high" at the API level (future-proofing for extended levels).
REASONING_EFFORT_VALUES: tuple = ('none', 'low', 'medium', 'high', 'xhigh')

# Settings for the Telegram bridge (agent_cascade/telegram_bridge/)
# NOTE on env-var naming: TG_TASK_TIMEOUT_SEC and TG_POLL_INTERVAL_SEC intentionally
# read the SAME unprefixed user-facing env vars that config.py has always used —
# live deployments set those exact names, so introducing AGENT_CASCADE_-prefixed
# aliases for them would silently create two knobs for one setting. The newer
# knobs (offline cap, HTTP timeout, 429 backoff, message length) had no env var
# before and follow the standard AGENT_CASCADE_ prefix convention.
TG_TASK_TIMEOUT_SEC: int = int(os.getenv('TG_TASK_TIMEOUT_SEC', 28800))  # Max seconds to wait per task (8h — AC runs can be long); env: TG_TASK_TIMEOUT_SEC
TG_TASK_WAIT_CEILING_SEC: float = float(os.getenv('AGENT_CASCADE_TG_TASK_WAIT_CEILING_SEC', 86400))  # Outer hard ceiling for how long the waiter keeps polling after task-timeout 'still working' notices before giving up entirely (24h)
TG_POLL_INTERVAL_SEC: float = float(os.getenv('TG_POLL_INTERVAL_SEC', 2.5))  # /api/status poll cadence; env: TG_POLL_INTERVAL_SEC
TG_OFFLINE_AFTER_SEC: float = float(os.getenv('AGENT_CASCADE_TG_OFFLINE_AFTER_SEC', 30.0))  # Surface "AC offline" after this many seconds unreachable (capped at task timeout)
TG_HTTP_REQUEST_TIMEOUT_SEC: float = float(os.getenv('AGENT_CASCADE_TG_HTTP_REQUEST_TIMEOUT_SEC', 30.0))  # Per-request httpx timeout to the AC REST API
TG_SEND_RETRY_BACKOFF_BASE_SEC: float = float(os.getenv('AGENT_CASCADE_TG_SEND_RETRY_BACKOFF_BASE_SEC', 1.0))  # Initial delay for Telegram 429 retries (doubles each attempt)
TG_SEND_RETRY_BACKOFF_CAP_SEC: float = float(os.getenv('AGENT_CASCADE_TG_SEND_RETRY_BACKOFF_CAP_SEC', 30.0))  # Ceiling for any single 429 retry delay
TG_MAX_MESSAGE_LEN: int = int(os.getenv('AGENT_CASCADE_TG_MAX_MESSAGE_LEN', 4096))  # Telegram hard limit of chars per message (chunking boundary)

# Outbound media delivery.
# TG_OUTBOUND_MEDIA is the master switch: False restores the text-only behaviour exactly.
# Ship enabled — the feature is invisible otherwise. Names follow the AGENT_CASCADE_
# prefix convention of the newer knobs above (constant == env name minus the prefix).
TG_OUTBOUND_MEDIA: bool = os.getenv('AGENT_CASCADE_TG_OUTBOUND_MEDIA', 'true').lower() in ('1', 'true', 'yes', 'on')  # Send image/audio references as real Telegram photo/audio messages
TG_MEDIA_DETECT_BARE_PATHS: bool = os.getenv('AGENT_CASCADE_TG_MEDIA_DETECT_BARE_PATHS', 'true').lower() in ('1', 'true', 'yes', 'on')  # Also detect bare absolute media paths (the media_gen/view_image output format); kill-switch for false positives from quoted logs
TG_MEDIA_MAX_PHOTO_BYTES: int = int(os.getenv('AGENT_CASCADE_TG_MEDIA_MAX_PHOTO_BYTES', 10 * 1024 * 1024))  # Bot API multipart photo upload limit (PTB send_photo: "photo must be at most 10MB")
TG_MEDIA_MAX_OTHER_BYTES: int = int(os.getenv('AGENT_CASCADE_TG_MEDIA_MAX_OTHER_BYTES', 50 * 1024 * 1024))  # Bot API multipart limit for audio/other uploads
TG_MEDIA_ALLOWED_ROOTS: str = os.getenv('AGENT_CASCADE_TG_MEDIA_ALLOWED_ROOTS', '')  # os.pathsep-separated extra directories allowed for outbound media (appended to the /api/file roots in path_security)
TG_MEDIA_SEND_VIDEO: bool = os.getenv('AGENT_CASCADE_TG_MEDIA_SEND_VIDEO', 'false').lower() in ('1', 'true', 'yes', 'on')  # Stretch goal, off: video references stay text unless explicitly enabled


# Settings for endpoint cooldown (time-based skip of failed endpoints)
def _parse_endpoint_cooldown():
    """Parse AGENT_CASCADE_ENDPOINT_COOLDOWN with safe defaults."""
    try:
        val = int(os.getenv('AGENT_CASCADE_ENDPOINT_COOLDOWN', '60'))
        return max(0, val)  # Clamp to 0 if negative
    except (ValueError, TypeError):
        return 60


ENDPOINT_COOLDOWN_SECONDS: int = _parse_endpoint_cooldown()  # Seconds to skip a failed endpoint before retrying
ENDPOINT_FAILURE_CLEANUP_HOURS: int = int(os.getenv('AGENT_CASCADE_ENDPOINT_FAILURE_CLEANUP_HOURS',
                                                    24))  # Remove failure records older than this many hours

# Absolute wall-clock deadline for a single LLM call.
# Caps the total time (all retries included) that _execute_llm_call_with_retry may
# spend before aborting with a [SYSTEM ERROR]. Prevents unbounded retry churn when
# backoff is misconfigured or an endpoint never recovers. Set to 0 to disable.
LLM_CALL_DEADLINE_SECONDS: int = int(
    os.getenv('AGENT_CASCADE_LLM_CALL_DEADLINE_SECONDS',
              2400))  # Wall-clock deadline (seconds) for one LLM call, all retries included

# Lightweight pre-allocation API sanity probe.
# Before the router allocates an endpoint to a real call, it checks reachability
# and auth via a fast GET /models request (no model loading or GPU thrashing).
# Probes fire once per connection-establishment: an instance that already holds a
# committed endpoint (a real call succeeded on it) is NOT re-probed — see
# _instance_committed_endpoint in router.py. Set SANITY_PROBE_ENABLED to False to disable.
SANITY_PROBE_ENABLED: bool = os.getenv('AGENT_CASCADE_SANITY_PROBE_ENABLED',
                                       'true').lower() in ('1', 'true', 'yes', 'on'
                                                          )  # Master toggle for the pre-allocation sanity probe
SANITY_PROBE_TIMEOUT_SECONDS: float = float(os.getenv(
    'AGENT_CASCADE_SANITY_PROBE_TIMEOUT_SECONDS', 10.0))  # HTTP READ timeout (seconds) for the lightweight probe GET
    # request. Sized for a BUSY local server (may be mid-stream on another agent's request),
    # not an idle probe. Default 10.0s.

# Endpoint blacklist for deterministic failures.
# An endpoint that fails ENDPOINT_DETERMINISTIC_FAILURE_THRESHOLD consecutive times
# with a DETERMINISTIC client error (one that will recur on every attempt, e.g. an
# HTTP 400 "not supported") is blacklisted for ENDPOINT_BLACKLIST_SECONDS — longer
# than the 60s cooldown because deterministic errors don't self-resolve quickly, but
# short enough (5 min) to re-try a recovered endpoint without waiting an hour.
# Non-deterministic failures (network, timeout, 5xx) reset the counter instead.
ENDPOINT_DETERMINISTIC_FAILURE_THRESHOLD: int = int(
    os.getenv('AGENT_CASCADE_ENDPOINT_DETERMINISTIC_FAILURE_THRESHOLD',
              3))  # Consecutive deterministic failures before blacklisting an endpoint
ENDPOINT_BLACKLIST_SECONDS: int = int(
    os.getenv('AGENT_CASCADE_ENDPOINT_BLACKLIST_SECONDS',
              300))  # Blacklist duration (seconds) for a persistently-failing endpoint

# Cap on SLEEPING duration.
# Max seconds an agent may remain in SLEEPING state waiting for background tools
# (async agent calls / async shells). On expiry the agent is forced to COMPLETING
# with an error message so a hung background tool can no longer hold it forever.
# Set to 0 to disable the cap (unbounded wait).
AGENT_SLEEPING_MAX_WAIT_SECONDS: int = int(
    os.getenv('AGENT_CASCADE_AGENT_SLEEPING_MAX_WAIT_SECONDS',
              3600))  # Max seconds in SLEEPING before forcing COMPLETING with error

# Settings for API router circuit breaker
BREAKER_BASE_WINDOW_SECONDS: float = float(
    os.getenv('AGENT_CASCADE_BREAKER_BASE_WINDOW_SECONDS',
              20.0))  # Initial open-state window for the per-server circuit breaker (seconds)
BREAKER_MAX_WINDOW_SECONDS: float = float(
    os.getenv('AGENT_CASCADE_BREAKER_MAX_WINDOW_SECONDS',
              120.0))  # Cap for exponential window growth on repeated failed probes (seconds)
BREAKER_WINDOW_GROWTH: float = float(os.getenv('AGENT_CASCADE_BREAKER_WINDOW_GROWTH',
                                               2.0))  # Window multiplier applied on each repeated failed probe
SERVER_BUSY_WAIT_CAP_SECONDS: float = float(
    os.getenv('AGENT_CASCADE_SERVER_BUSY_WAIT_CAP_SECONDS',
              30.0))  # Per-call cap for the D1 fail-fast wait when the whole endpoint chain is breaker-gated (seconds)
SERVER_BUSY_RETRY_DELAY_SECONDS: float = float(
    os.getenv('AGENT_CASCADE_SERVER_BUSY_RETRY_DELAY_SECONDS',
              0.0))  # Minimum seconds to wait before the next retry on a busy-loading 503, so the per-endpoint budget spans a real model load. 0 = generic backoff (Retry-After is still honoured when present). Bounded by LLM_CALL_DEADLINE_SECONDS

# Settings for token estimation
# Aggressive estimate used for telemetry and output estimation.
# Based on typical English text (~4 chars/token).
TOKEN_ESTIMATE_CHAR_DIVISOR: float = float(os.getenv('AGENT_CASCADE_TOKEN_ESTIMATE_CHAR_DIVISOR', 4.0))
IMAGE_TOKEN_ESTIMATE: int = int(os.getenv('AGENT_CASCADE_IMAGE_TOKEN_ESTIMATE',
                                          255))  # Fallback tokens per image when dimensions are unreadable
# Dimension-aware image token estimation (Qwen3-VL dynamic resolution). The flat IMAGE_TOKEN_ESTIMATE
# above is only a fallback for images whose pixel dimensions cannot be read (remote URL, missing
# file, malformed data URI). For readable images the token cost is computed from the sent pixel
# dimensions. Defaults target Qwen3-VL (16px patches merged 2x2 -> 32px cells); use
# AGENT_CASCADE_IMAGE_TOKEN_PATCH_SIZE=28 for Qwen2.5-VL.
IMAGE_TOKEN_PATCH_SIZE: int = int(os.getenv('AGENT_CASCADE_IMAGE_TOKEN_PATCH_SIZE',
                                            32))  # Vision cell size in px (patch_size * merge_size)
IMAGE_TOKEN_MIN: int = int(os.getenv('AGENT_CASCADE_IMAGE_TOKEN_MIN',
                                     11))  # Observed floor for tiny images (vision markers + padding)
IMAGE_MAX_SHORT_SIDE: int = int(os.getenv('AGENT_CASCADE_IMAGE_MAX_SHORT_SIDE',
                                          1080))  # Short-side cap applied before sending (single source of truth)
CHAT_TEMPLATE_TOKEN_OVERHEAD: int = int(
    os.getenv('AGENT_CASCADE_CHAT_TEMPLATE_TOKEN_OVERHEAD',
              8))  # Overhead per message from llama.cpp chat template (bos, role tags, newlines, etc.)
MESSAGE_TOKEN_ESTIMATE: int = int(os.getenv('AGENT_CASCADE_MESSAGE_TOKEN_ESTIMATE',
                                            500))  # Estimated tokens per message during compression
CONTEXT_RESERVATION_RATIO: float = float(os.getenv('AGENT_CASCADE_CONTEXT_RESERVATION_RATIO',
                                                   0.9))  # Reserve 90% for input, 10% for output during compression

# Settings for LLM retry/backoff
DEFAULT_MAX_TOKENS: int = int(os.getenv('AGENT_CASCADE_DEFAULT_MAX_TOKENS', 128000))  # Default max tokens for LLM calls
LLM_MAX_RETRIES: int = int(os.getenv('AGENT_CASCADE_LLM_MAX_RETRIES', 1))  # Max retries for LLM calls
LLM_RETRY_BASE_DELAY: float = float(os.getenv('AGENT_CASCADE_LLM_RETRY_BASE_DELAY',
                                              1.0))  # Base delay in seconds for retry backoff
LLM_RETRY_MAX_BACKOFF: float = float(os.getenv('AGENT_CASCADE_LLM_RETRY_MAX_BACKOFF',
                                               5.0))  # Maximum backoff cap in seconds

# Settings for streaming timeouts (Layer 1-3 defense against stuck streams).
# Enforced by the watchdog reader thread in utils/streaming.py::watch_stream, so these
# values are authoritative even when the endpoint heartbeats without producing events
# (the transport-level AGENT_CASCADE_HTTP_READ_TIMEOUT below is only an idle-byte backstop).
STREAM_MAX_SILENCE_SECONDS: float = float(os.getenv(
    'AGENT_CASCADE_STREAM_MAX_SILENCE_SECONDS', 180.0))  # Max seconds between chunks before considering stream stalled
STREAM_MAX_TOTAL_SECONDS: float = float(os.getenv('AGENT_CASCADE_STREAM_MAX_TOTAL_SECONDS',
                                                  900.0))  # Max total duration of a streaming response
# Additive/delta streaming: when enabled (default ON), partial (streaming) frames send only a
# small safe tail instead of the full committed history; force_full / connect-time frames stay full.
# Set AGENT_CASCADE_STREAM_DELTA=0 to disable and revert to full-send legacy behavior.
STREAM_DELTA_ENABLED: bool = os.getenv('AGENT_CASCADE_STREAM_DELTA',
                                       '1').strip().lower() not in ('0', 'false', 'off', 'no')

# Stack-swap / fleet-snapshot prerequisite (BUG_0323 S1, refined by BUG_0362). When
# enabled, a root's identity is its clean display name (Maine); a second live stack
# sharing that name gets a numeric suffix (Maine_2) so two stacks never collide on
# KV labels or log paths. Birth stamps (Maine_20261115_143022) are legacy-only —
# preserved verbatim when restoring old sessions, never minted. ON by default; set
# AGENT_CASCADE_ENABLE_STACK_SWAP=false to restore the legacy plain-identity behavior
# (byte-identical to pre-BUG_0323).
_ENABLE_STACK_SWAP_ENV = 'AGENT_CASCADE_ENABLE_STACK_SWAP'
ENABLE_STACK_SWAP_FROM_ENV: bool = _ENABLE_STACK_SWAP_ENV in os.environ
ENABLE_STACK_SWAP: bool = os.getenv(_ENABLE_STACK_SWAP_ENV,
                                    'true').strip().lower() in ('true', '1', 'yes')

# Restore the last active saved session at server start. On by default (user decision).
# Off = fall back to the legacy "load newest log" boot path; saves still happen, and the
# newest log is the live one (there is no log rotation), so nothing is lost either way.
STACK_RESTORE_ON_BOOT: bool = True

# Automatic session saves (restart, shutdown, root run end). Park saves on new/swap are
# inherent and unaffected. False = only explicit/park saves.
SNAPSHOT_AUTOSAVE_ENABLED: bool = os.getenv('AGENT_CASCADE_SNAPSHOT_AUTOSAVE',
                                            'true').strip().lower() in ('true', '1', 'yes')

# Per-instance grace window (seconds) granted to an in-flight turn when the pool
# parks the active stack for a swap. The turn is given this much time to reach its
# next turn boundary and stop cooperatively (work preserved); if it is still wedged
# when the window expires it is force-halted, its in-flight background tools are
# killed, and its turn discarded at the last coherent point, then the swap proceeds.
# Supersedes the old drain-rejection timeout: a parked stack is never rejected for
# exceeding it. Only read when ENABLE_STACK_SWAP is on.
def _parse_snapshot_grace() -> int:
    """Defensively parse AGENT_CASCADE_SNAPSHOT_GRACE_SECONDS; fall back to 10 on bad values."""
    raw = os.getenv('AGENT_CASCADE_SNAPSHOT_GRACE_SECONDS', '10').strip()
    try:
        value = int(raw)
    except ValueError:
        return 10
    return value if value >= 0 else 10


SNAPSHOT_GRACE_SECONDS: int = _parse_snapshot_grace()


def _parse_snapshot_retention() -> int:
    """Defensively parse AGENT_CASCADE_SNAPSHOT_RETENTION_COUNT; fall back to 50.

    A non-positive value would mean "prune everything", which is never intended, so
    any parsed value below 1 clamps to 1 (always keep the newest snapshot).
    """
    raw = os.getenv('AGENT_CASCADE_SNAPSHOT_RETENTION_COUNT', '50').strip()
    try:
        value = int(raw)
    except ValueError:
        return 50
    return value if value >= 1 else 1


# Number of most-recent snapshots retained per stack before pruning deletes the
# oldest ones (OQ1). Only read when ENABLE_STACK_SWAP is on.
SNAPSHOT_RETENTION_COUNT: int = _parse_snapshot_retention()


def _parse_corrupt_snapshot_retention() -> int:
    """Defensively parse AGENT_CASCADE_CORRUPT_SNAPSHOT_RETENTION_COUNT; fall back to 20.

    Bounds how many of the most-recent quarantined ("corrupt") snapshot envelopes are
    kept per ``snapshots/_corrupt/`` directory; older envelopes (and their ``.reason.txt``
    sidecars) are pruned beyond this. Unlike live-snapshot retention, a value of 0 means
    OFF (keep every quarantined envelope, i.e. unbounded) — never clamped up to 1 — so a
    deliberate "retain all forensic evidence" setting is honored. Negative values are
    treated as 0 (off).
    """
    raw = os.getenv('AGENT_CASCADE_CORRUPT_SNAPSHOT_RETENTION_COUNT', '20').strip()
    try:
        value = int(raw)
    except ValueError:
        return 20
    return value if value >= 0 else 0


# Maximum number of most-recent quarantined snapshot envelopes retained per
# ``snapshots/_corrupt/`` directory before older ones (and their ``.reason.txt``
# sidecars) are deleted. Units: files. Default 20. ``0`` = off (keep all). Only read
# when ENABLE_STACK_SWAP is on.
CORRUPT_SNAPSHOT_RETENTION_COUNT: int = _parse_corrupt_snapshot_retention()


def _parse_stream_tail() -> int:
    """Defensively parse AGENT_CASCADE_STREAM_TAIL; fall back to 1 on bad values.

    settings.py has no logger and uses plain module-level env parsing, so a bad value
    falls back silently rather than crashing at import time.
    """
    raw = os.getenv('AGENT_CASCADE_STREAM_TAIL', '1')
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return 1


STREAM_DELTA_TAIL_COMMITTED: int = _parse_stream_tail()


def _parse_stream_force_full_interval() -> float:
    """Defensively parse AGENT_CASCADE_STREAM_FORCE_FULL_INTERVAL; fall back to 60.0 on bad values.

    Seconds between periodic full (force_full) state frames per instance. The full frame is a
    self-healing safety net that re-syncs the UI if individual delta frames were dropped while
    the WS send queue was full. Falls back silently (settings.py has no logger).
    """
    raw = os.getenv('AGENT_CASCADE_STREAM_FORCE_FULL_INTERVAL', '30')
    try:
        return float(str(raw).strip())
    except (ValueError, TypeError):
        return 60.0


STREAM_FORCE_FULL_INTERVAL: float = _parse_stream_force_full_interval()

# Dismiss thread join timeout (seconds to wait for agent thread to stop cooperatively)
DISMISS_THREAD_JOIN_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_DISMISS_THREAD_JOIN_TIMEOUT', 2.0))

# HTTP client timeouts (passed to httpx)
# read is a per-chunk idle backstop, silent on BOTH holder-DMS timestamps, so it must
# sit well under HOLDER_DMS_WINDOW_S (300s). Default 120s (the longest tool bound,
# CODE_EXECUTION_TIMEOUT=120 → 2.5x margin). Streaming is unaffected: the 180s stream-silence
# watchdog (utils/streaming.py) is authoritative and fires first. Env-overridable as usual.
HTTP_READ_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_HTTP_READ_TIMEOUT',
                                           120.0))  # Idle read backstop; strictly under HOLDER_DMS_WINDOW_S
HTTP_CONNECT_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_HTTP_CONNECT_TIMEOUT',
                                              10.0))  # Timeout for establishing TCP connection
HTTP_WRITE_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_HTTP_WRITE_TIMEOUT',
                                            60.0))  # Timeout for sending request body
HTTP_POOL_TIMEOUT: float = float(os.getenv('AGENT_CASCADE_HTTP_POOL_TIMEOUT',
                                           30.0))  # Timeout waiting for connection from pool

# Settings for telemetry
DEFAULT_RECENT_EVENT_COUNT: int = int(os.getenv('AGENT_CASCADE_DEFAULT_RECENT_EVENT_COUNT',
                                                50))  # Default recent events count
MAX_EVENTS_IN_MEMORY: int = int(os.getenv('AGENT_CASCADE_MAX_EVENTS_IN_MEMORY',
                                          5000))  # Max events in memory before trimming
# Events between periodic telemetry_stats checkpoints (BUG_0400 (a)) — bounds the
# aggregates lost to an unhandled hard-kill (SIGKILL/taskkill/OOM), where no graceful
# shutdown or stack-park write can run. Counts written events, not wall-clock time.
# Default 50; 0 disables the periodic checkpoint (graceful/park checkpoints still run).
TELEMETRY_CHECKPOINT_EVENT_INTERVAL: int = int(os.getenv('AGENT_CASCADE_TELEMETRY_CHECKPOINT_EVENT_INTERVAL',
                                                         50))  # Events per periodic telemetry checkpoint; 0 disables

# Optional $-cost map for cache-waste telemetry (borrow-audit #7). Empty by default:
# cache waste is always reported in tokens (provider-agnostic). Populate to ALSO
# surface llm_wasted_cost. Shape: {model_name: {'input_per_m': float, 'cache_read_per_m': float}}
# Env-overridable (Python dict literal, same pattern as DEFAULT_RAG_SEARCHERS).
MODEL_PRICING: Dict[str, Dict[str, float]] = ast.literal_eval(
    os.getenv('AGENT_CASCADE_MODEL_PRICING', '{}'))

# Settings for LM Studio
LM_STUDIO_KEEPALIVE_SECONDS: float = float(os.getenv('AGENT_CASCADE_LM_STUDIO_KEEPALIVE',
                                                     3.0))  # Keepalive expiry in seconds


# ── Inner-loop detection settings ───────────────────────────────────
@dataclass
class InnerLoopSettings:
    """Tunable parameters for the inner-loop repetition detector.

    Only the char_run and max_chars guards plus the
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
    char_run_enabled: bool = os.getenv('AGENT_CASCADE_LOOP_CHAR_RUN', '1') != '0'
    char_run_limit: int = 129  # Max consecutive identical chars before alert

    # Activation thresholds
    default_min_chars: int = 4000  # Min chars to accumulate before full detection (kept for compatibility)
    default_max_chars: int = 40960  # Hard character limit — force-trigger detection if exceeded (~8K tokens)

    # Max chars guard toggle — controls the hard character limit detection guard.
    loop_max_chars_enabled: bool = True  # Enable max chars hard limit guard

    # Two-phase semantic loop detection settings
    loop_two_phase_enabled: bool = True  # Enable two-phase semantic loop detection
    loop_suspicion_threshold: int = 7  # N-gram occurrence count to trigger suspicion [5-15]
    loop_confirm_required: int = 5  # Exact matches required for confirmation [2-6]
    loop_cooldown_feeds: int = 50  # Feeds to suppress after failed confirmation [10-200]

    # ── Deprecated settings (scoring-based modes removed) ────
    # These fields are no longer used by InnerLoopDetector but kept for backward
    # compatibility with code that passes explicit values when constructing settings.

    # Memory bounds (deprecated — scoring counters removed)
    max_counter_entries: int = 200  # DEPRECATED: was max entries per Counter before pruning
    max_tokens: int = 1000  # DEPRECATED: was max tokens in the sliding window

    # Activation thresholds (deprecated)
    default_batch_interval: int = 1  # DEPRECATED: was run heavy checks every N-th feed call

    # Structural parameters (deprecated — scoring modes removed)
    ngram_size: int = 64  # DEPRECATED: was token window size for n-gram repetition scoring
    block_size: int = 128  # DEPRECATED: was token window size for block repetition scoring
    entropy_window: int = 128  # DEPRECATED: was token window for Shannon entropy calculation

    # Scoring system (deprecated — entirely removed)
    score_threshold: int = 350  # DEPRECATED: was cumulative score to trigger loop detection
    score_decay_rate: float = 0.97  # DEPRECATED: was multiplicative decay per feed cycle
    max_score: int = 500  # DEPRECATED: was hard cap to prevent unbounded score growth

    # Detection thresholds (deprecated — scoring modes removed)
    sentence_repetition_threshold: int = 15  # DEPRECATED: was sentence count to flag repetition
    ngram_repetition_threshold: int = 7  # DEPRECATED: was n-gram count to flag repetition
    block_repetition_threshold: int = 6  # DEPRECATED: was block count to flag repetition
    entropy_threshold: float = 2.0  # DEPRECATED: was Shannon entropy below which a loop is suspected

    # Per-mode toggles for removed modes (deprecated)
    sentence_rep_enabled: bool = os.getenv('AGENT_CASCADE_LOOP_SENTENCE_REP',
                                           '1') != '0'  # DEPRECATED: sentence scoring removed
    ngram_rep_enabled: bool = os.getenv('AGENT_CASCADE_LOOP_NGRAM_REP',
                                        '1') != '0'  # DEPRECATED: n-gram scoring removed
    block_rep_enabled: bool = os.getenv('AGENT_CASCADE_LOOP_BLOCK_REP', '1') != '0'  # DEPRECATED: block scoring removed
    entropy_collapse_enabled: bool = os.getenv('AGENT_CASCADE_LOOP_ENTROPY',
                                               '1') != '0'  # DEPRECATED: entropy detection removed


# ── Code interpreter settings ────────────────
CI_EXECUTION_TIMEOUT: int = int(os.getenv('M6_CODE_INTERPRETER_EXEC_TIMEOUT',
                                          '120'))  # Per-call execution timeout (seconds)
CI_WATCHDOG_TIMEOUT: int = int(os.getenv('M6_CODE_INTERPRETER_WATCHDOG_TIMEOUT',
                                         '300'))  # Kernel inactivity watchdog timeout (seconds)
CI_STALE_CONTAINER_TTL: int = int(os.getenv('M6_CODE_INTERPRETER_STALE_TTL',
                                            '1200'))  # Stale container cleanup TTL (seconds)
CI_MIN_EXECUTION_TIMEOUT: int = 1  # Minimum per-call execution timeout (seconds)
CI_MIN_WATCHDOG_TIMEOUT: int = 30  # Minimum watchdog timeout (seconds)
CI_MIN_STALE_CONTAINER_TTL: int = 30  # Minimum stale container TTL (seconds)

# ── External CLI coding-agent (call_cli_agent) settings ──
CLI_AGENT_TIMEOUT: int = int(os.getenv('M6_CLI_AGENT_TIMEOUT', '1800'))  # Hard wall-clock cap per one-shot run (seconds)
CLI_AGENT_MIN_TIMEOUT: int = 1  # Minimum one-shot wall-clock cap (seconds)
MAX_CLI_AGENT_CHILDREN: int = int(os.getenv('M6_MAX_CLI_AGENT_CHILDREN', '2'))  # Concurrent one-shot containers cap
CLI_AGENT_MIN_CHILDREN: int = 1  # Minimum concurrent one-shot cap (never 0)

# Master read-only toggle for inherited extra-paths: when True the runner also mounts the
# agent's ``operation_manager.extra_work_folders_ro`` (read-only) into the CLI container.
# 0/False = workspace-only mount (current behavior, default). RO only — the external CLI is untrusted.
CLI_AGENT_PATHS_RO_DEFAULT: bool = False

#: Shared multi-harness image tag built from ``Dockerfile.cli_agent`` (claude + opencode on
#: PATH). One build / one containment surface; the ``cli_agent_images`` pool map still permits
#: per-harness tags and ``M6_CLI_AGENT_DOCKER_IMAGE`` stays the ops escape hatch.
CLI_AGENT_DEFAULT_IMAGE: str = 'cli_agent:all'

#: Dedicated image tag for antigravity (agy). agy ships a non-npm binary installer so it is
#: intentionally NOT baked into the shared image; built from ``Dockerfile.cli_agent_agy``.
CLI_AGENT_ANTIGRAVITY_IMAGE: str = 'cli_agent_agy'

#: Periodic refresh window in days for a CLI-agent image: an existing image older than this is
#: rebuilt on next use (so npm-installed CLIs + the agy installer pull the latest), re-checked on a
#: cadence so refresh happens within a running process. 0 or negative disables staleness rebuilds
#: (the image is only built when missing).
CLI_AGENT_IMAGE_MAX_AGE_DAYS: int = int(os.getenv('M6_CLI_AGENT_IMAGE_MAX_AGE_DAYS', '7'))

#: Named Docker volume that persists an OAuth-based CLI's account store across one-shot runs
#: (consumed when the adapter declares ``auth_type='oauth'`` + ``auth_volume_target``; agy mounts
#: it at /root/.gemini so a one-time interactive login survives container teardown).
#: '' or '0' = OFF: no volume is mounted and the user supplies credentials another way.
CLI_AGENT_AGY_AUTH_VOLUME: str = os.getenv('M6_CLI_AGENT_AGY_AUTH_VOLUME', 'cli_agent_agy_auth')

#: Minimum spacing between STARTS of one-shot runs for the SAME harness (seconds). Some providers
#: ban an account that receives fast automated bursts, so a burst of call_cli_agent calls is
#: spread out: each caller waits for its own start slot instead of firing back-to-back.
#: 0 = OFF (no spacing — every call starts as soon as the concurrency cap allows).
CLI_AGENT_RATE_LIMIT_SECONDS: float = float(os.getenv('AGENT_CASCADE_CLI_AGENT_RATE_LIMIT_SECONDS', '5'))

#: Stall watchdog (BUG_0343): seconds with NO new stdout/stderr line before an async CLI run is
#: reported as "STALLED" in __status and logged once. Warn only — the run keeps going, so a
#: legitimately silent long step is never killed by this alone. 0 = OFF (no stall verdict).
CLI_AGENT_STALL_WARN_SECONDS: float = float(os.getenv('AGENT_CASCADE_CLI_AGENT_STALL_WARN_SECONDS', '300'))

#: Stall watchdog hard limit: seconds with NO new stdout/stderr line before the async tracker
#: terminates the container, independent of the wall-clock cap (which may be much longer).
#: 0 = OFF (never auto-kill on silence; recovery stays with the wall-clock timeout / __kill).
CLI_AGENT_STALL_KILL_SECONDS: float = float(os.getenv('AGENT_CASCADE_CLI_AGENT_STALL_KILL_SECONDS', '0'))


def cli_agent_default_image(harness: str) -> str:
    """Default Docker image tag for a CLI harness. antigravity resolves to its dedicated
    image (agy is not in the shared image); every other harness shares cli_agent:all."""
    if harness == 'antigravity':
        return CLI_AGENT_ANTIGRAVITY_IMAGE
    return CLI_AGENT_DEFAULT_IMAGE

# ── Cache pool settings ────────────────────────
CACHE_POOL_ENABLED: bool = False  # Toggle cache pool on/off (default: disabled)
CACHE_POOL_SIZE: int = int(os.getenv('AGENT_CASCADE_CACHE_POOL_SIZE', '50'))  # Rolling buffer entries per instance
CACHE_THRESHOLD_CHARS: int = int(os.getenv('AGENT_CASCADE_CACHE_THRESHOLD_CHARS',
                                           '1000'))  # Min chars for output & granular arg caching

# ── Async shell command settings ───────────
MAX_ASYNC_SHELL_PER_AGENT: int = 5  # Max concurrent async shells per agent
# DEPRECATED — async shell truncation uses shell_char_limit from llm_cfg (default 2048, same as sync mode)
ASYNC_SHELL_HEARTBEAT_TRUNCATE_CHARS: int = int(os.getenv('AGENT_CASCADE_ASYNC_SHELL_HEARTBEAT_CHARS',
                                                          '800'))  # noqa: F841
ASYNC_SHELL_DEFAULT_TIMEOUT: int = 3600  # Default timeout for async shells (1 hour)

# ── Async shell stall-watchdog idle floor (BUG_0385) ─────────────────────────────
# The declared `timeout` is the LAST-RESORT top-down tree-kill backstop, NOT the close
# trigger: a wedged-but-alive child (alive, zero new stdout/stderr, internally crashed) must
# close strictly BEFORE the deadline. These knobs feed AsyncShellTracker._stall_*_seconds
# (tracker.py), which drive the already-built poll-loop wedge detector (line-count delta) and
# the stall branch in _track_task that reuses the SAME _kill_process_tree tree-killer as the
# timeout path. They are OWNER-TUNABLE policy knobs; the detector only counts new output
# lines, so any progress resets it — the residual risk (premature kill of a legitimately
# silent-but-working command, e.g. a buffered compile) is bounded by choosing CONSERVATIVE
# fractions of the user's own `timeout`.
#
# The kill floor is derived as min(ASYNC_SHELL_STALL_KILL_DEFAULT,
# ASYNC_SHELL_STALL_KILL_FRACTION * timeout). Because the fraction is < 1, the kill floor
# sits below timeout for any realistic timeout, so enabling the watchdog can only ADVANCE
# closure past today's behavior, never delay it beyond the existing deadline. The `timeout`
# backstop (_track_task -> _kill_process_tree) stays the unconditional ceiling for hangs the
# idle detector cannot see (e.g. an output trickle that never stays quiet long enough to trip).
#: Cap on the idle floor (seconds): never wait longer than this on silence, even for a very
#: large `timeout` — a run silent for this long has almost certainly wedged. The ONLY lever to
#: widen the floor once it is cap-dominated is raising ASYNC_SHELL_STALL_KILL_DEFAULT itself;
#: raising `timeout` widens the floor only while KILL_FRACTION * timeout < KILL_DEFAULT and has
#: NO effect once the KILL_DEFAULT cap dominates. 0 = OFF.
ASYNC_SHELL_STALL_KILL_DEFAULT: float = float(os.getenv('AGENT_CASCADE_ASYNC_SHELL_STALL_KILL_DEFAULT', '600'))
#: Fraction of `task.timeout` the kill floor is capped to (must be < 1.0 so the wedge-close
#: always precedes the deadline): the kill floor is min(KILL_DEFAULT, this * timeout).
ASYNC_SHELL_STALL_KILL_FRACTION: float = float(os.getenv('AGENT_CASCADE_ASYNC_SHELL_STALL_KILL_FRACTION', '0.8'))
#: Fraction of the kill floor at which __status first logs/verdicts a warning (status only,
#: never kills). Kept well below the kill floor so a slow step is reported before it is reaped.
ASYNC_SHELL_STALL_WARN_FRACTION: float = float(os.getenv('AGENT_CASCADE_ASYNC_SHELL_STALL_WARN_FRACTION', '0.5'))

HEARTBEAT_CHECK_INTERVAL: float = 0.5  # How often the tracker thread checks for heartbeats (seconds)
HEARTBEAT_TRUNCATE_FIRST_LINES: int = 5  # Lines kept at start when truncating heartbeat output
HEARTBEAT_TRUNCATE_LAST_LINES: int = 10  # Lines kept at end when truncating heartbeat output
EARLY_OUTPUT_CHECK_TIMEOUT: float = 2.0  # Max seconds to wait for early output/completion after launch
AUTO_ASYNC_TIMEOUT_THRESHOLD: int = 60  # Seconds: timeout above this triggers auto-async mode
DEFAULT_AUTO_ASYNC_HEARTBEAT: int = 30  # Default heartbeat interval for auto-async shells
# shell_cmd AUTO low-RAM guard (see PoolSettings.safe_mode_auto_ram_floor_mb / safe_mode_auto_vram_floor_mb):
# below the RAM floor, AUTO forces safe-mode to avoid OOM-killing the host; VRAM is advisory only.
SAFE_MODE_AUTO_RAM_FLOOR_MB: int = 4096   # MB free system RAM — HARD floor (force safe mode)
SAFE_MODE_AUTO_VRAM_FLOOR_MB: int = 512  # MB free GPU VRAM — ADVISORY floor (warn only)
WAIT_CMD_MAX_TIMEOUT: float = 180.0  # Max seconds __wait will block when heartbeats are configured (3 min)
WAIT_CMD_DEFAULT_TIMEOUT: float = 30.0  # Seconds __wait blocks when no heartbeats are configured (-1)
WAIT_CMD_POLL_INTERVAL: float = 0.5  # Seconds between state polls inside the __wait loop
# Sync shell_cmd Stop watcher: how often a blocked sync command re-checks whether the
# user pressed Stop (pool.stopped) or a newer run generation superseded it. 0 = off
# (a sync command then always runs to its own timeout, even after Stop).
SHELL_STOP_POLL_INTERVAL: float = float(os.getenv('AGENT_CASCADE_SHELL_STOP_POLL_INTERVAL', '0.5'))

# ── Skills system settings ────────────
LOAD_SKILL_AUTO: str = 'AUTO'  # Auto-match relevant skills from task context
LOAD_SKILL_NONE: str = 'NONE'  # No skill loading (saves tokens)
DEFAULT_LOAD_SKILL_MODE: str = os.getenv('AGENT_CASCADE_DEFAULT_LOAD_SKILL',
                                         'AUTO')  # Default load_skill mode: AUTO or NONE
# AUTO Skill Helper sub-mode (only applies when default_load_skill_mode == AUTO):
#   "basic"    — keyword-only matching via resolve_load_skill() (existing behavior, no extra LLM call)
#   "advanced" — invokes the Skill Advisor (Security agent) for semantic matching + delegation validation
#   "none"     — disables system-injected auto-matched skills entirely (no Basic keyword match,
#                no Advanced advisor). Self-Augmentation (global toggle) and caller-explicit
#                load_skill lists are still preserved.
DEFAULT_AUTO_SKILL_MODE: str = os.getenv('AGENT_CASCADE_DEFAULT_AUTO_SKILL_MODE', 'basic')
AUTO_SKILL_MODE_BASIC: str = 'basic'
AUTO_SKILL_MODE_ADVANCED: str = 'advanced'
AUTO_SKILL_MODE_NONE: str = 'none'
# Skill selector mode — which strategy backs SkillManager.match_skills auto-matching:
#   "keyword" (default) — existing keyword matcher only (today's behavior, byte-identical)
#   "api"             — decision-model endpoint (/v1/systemone) picks the best skill,
#                       degrading gracefully to keyword on any failure.
DEFAULT_SKILL_SELECTOR_MODE: str = os.getenv('AGENT_CASCADE_DEFAULT_SKILL_SELECTOR_MODE', 'keyword')
SKILL_SELECTOR_MODE_KEYWORD: str = 'keyword'
SKILL_SELECTOR_MODE_API: str = 'api'
SKILL_MATCH_THRESHOLD: float = float(os.getenv('AGENT_CASCADE_SKILL_MATCH_THRESHOLD',
                                                '0.25'))  # Minimum relevance score for AUTO mode skill loading
MAX_AUTO_SKILLS_PER_CALL: int = 3  # Hard cap on Basic AUTO keyword-matched skills per call (self-augmentation excluded)
# The `## Active Skills` block is assembled ONCE at spawn and never rebuilt (idempotency guard
# in engine/helpers.py), so all pruning happens at assembly time. This bounds that static block
# by skill count; set to 0 to disable. SKILL_MATCH_THRESHOLD (above) is the separate AUTO-mode
# floor that shrinks the tail entering it. This is a hard ceiling on skills in the per-session
# block (self-augmentation COUNTED) — the only knob covering the uncapped explicit-list and
# advisor paths that MAX_AUTO_SKILLS_PER_CALL misses. 0 = off. Env-reversible.
MAX_ACTIVE_SKILLS_PER_SESSION: int = int(os.getenv('AGENT_CASCADE_MAX_ACTIVE_SKILLS_PER_SESSION', 8))
# Minimum interval before the SAME skill may be hinted again. ``memory_hint_cooldown_seconds``
# is a separate MEMORY-re-read knob that a user may legitimately set very low; reusing it for
# skills lets one skill re-appear on nearly every turn, so skill entries get their own floor,
# applied as max(memory_hint_cooldown_seconds, this) — a low memory cooldown can never turn
# skill suggestions into per-turn spam (nor can a high one shorten this guard). Default 1800s (30 min).
SKILL_HINT_ENTRY_COOLDOWN_SECONDS: float = float(
    os.getenv('AGENT_CASCADE_SKILL_HINT_ENTRY_COOLDOWN_SECONDS', 1800.0))
# ── Skill-hint selector-precision lever (memory-hint sub-pipeline only) ──
# A post-Gate-3 ranking layer in memory_hint/manager.py._match_skills_for_hint (Gate 4).
# It reads the persistent load ledger (agents/global/skills-metrics.json) and DEMOTES a
# skill that is hinted often but loaded rarely, and hard-drops one hinted many times yet
# NEVER loaded. It never feeds SKILL_MATCH_THRESHOLD or the AUTO-mode loader — it only
# changes WHICH names the hint block lists and in what order. All four knobs are
# env-reversible; set weight to 0 and N to 0 to disable the layer entirely.
# SKILL_PRECISION_WEIGHT: master toggle for the re-rank — 1 = on (default), 0 = off.
# When off, the candidate order is the raw score-descending order, unchanged.
SKILL_PRECISION_WEIGHT: int = int(os.getenv('AGENT_CASCADE_SKILL_PRECISION_WEIGHT', '1'))
# SKILL_PRECISION_K: Laplace smoothing pseudo-count in
# w(s) = max(FLOOR, min(1, (loads+K)/(hints+K))). Larger K shrinks the swing on thin
# evidence; 1.0 means a single load already lifts a 0-load skill to full weight.
SKILL_PRECISION_K: float = float(os.getenv('AGENT_CASCADE_SKILL_PRECISION_K', '1.0'))
# SKILL_PRECISION_FLOOR: lower bound on w(s), so a heavy-hint / low-load skill is demoted
# but never buried below FLOOR × score — other skills stay promotable (the guardrail for
# rare-but-correct skills that are rarely topical and so never accumulate enough hints).
# Lower floor ⇒ repeatedly-unloaded skills decay out faster (less hint noise). Default 0.10.
SKILL_PRECISION_FLOOR: float = float(os.getenv('AGENT_CASCADE_SKILL_PRECISION_FLOOR', '0.10'))

# ── Skill-hint Thompson-Sampling bandit (self-adapting value ranking) ──────────
# A ranking-only layer that REPLACES Gate-4's precision demote re-rank when enabled,
# at the same slot (memory_hint/manager.py). Default OFF (weight 0.0) ⇒ the existing
# Gate-4 path runs ⇒ deploy-time behaviour is byte-identical to today; rollout is an
# env-var A/B. See plans/skill-hint-self-adapting_DIG.md.
# SKILL_BANDIT_WEIGHT: master toggle + modulation blend in [0,1]. 0.0 = OFF (default);
#   1.0 = full Thompson-Sampling value re-rank; 0<W<1 ramps from raw order to full.
#   The path is taken iff weight > 0.0 (a true predicate, not merely "is the setting set").
#   NOTE: any weight > 0.0 turns on BOTH the value re-rank AND the bounded θ_min admission
#   bar (which arms the fire-rate controller) — a small weight is NOT a "rank-only" mode;
#   the bar still seeds at 0.0 (admit-all) and only drifts once the controller sees turns.
SKILL_BANDIT_WEIGHT: float = float(os.getenv('AGENT_CASCADE_SKILL_BANDIT_WEIGHT', '0.0'))
# SKILL_BANDIT_PRIOR_MASS: prior mass m₀ of the base-rate-centred informed prior
#   Beta(m₀·p̄ + s, m₀·(1−p̄) + f). Small m₀ = aggressive dormancy exploration; large =
#   conservative. p̄ is the running global action-rate learned from the ledger, so a
#   dormant skill's posterior mean is p̄ (eligible) rather than 0.5 (which would mass-spam).
SKILL_BANDIT_PRIOR_MASS: float = float(os.getenv('AGENT_CASCADE_SKILL_BANDIT_PRIOR_MASS', '3.0'))
# SKILL_BANDIT_TARGET_FIRE: outcome target — the fraction of processed skill-hint turns
#   that should render ≥1 skill. The in-memory controller drifts the admission bar θ_min
#   to hold this; it is a target, not a hand-tuned threshold. 0.10 = fire on ~10% of turns.
SKILL_BANDIT_TARGET_FIRE: float = float(os.getenv('AGENT_CASCADE_SKILL_BANDIT_TARGET_FIRE', '0.10'))
# SKILL_BANDIT_GAIN: per-turn controller drift cap (η). Bounds how fast θ_min moves, which
#   damps oscillation alongside the EWMA damping (SKILL_EWMA_ALPHA reused for the fire EWMA).
SKILL_BANDIT_GAIN: float = float(os.getenv('AGENT_CASCADE_SKILL_BANDIT_GAIN', '0.02'))
# SKILL_BANDIT_THETA_MAX: hard ceiling on the admission bar θ_min ∈ [0, θ_max]. Since θ_max
#   < 1 and every skill's posterior mean is centred on p̄ > 0, mass-suppression is
#   structurally impossible: the bar can never reach 1, so a candidate set is never all
#   dropped by the bandit. 0.0 (the seed / safe state) = admit-all = rank-only identity.
SKILL_BANDIT_THETA_MAX: float = float(os.getenv('AGENT_CASCADE_SKILL_BANDIT_THETA_MAX', '0.6'))

# ── Memory-hint Thompson-Sampling bandit (self-adapting value ranking, BUG_0317) ──
# A ranking-only layer applied in _match_memories between the dedup/cooldown result
# and the max_entries cap (memory_hint/manager.py). It multiplies the cosine relevance
# score by the same scale-agnostic base-rate-centred multiplier the skill bandit uses,
# so it COMPOSES with the cosine floor rather than re-gating. Default OFF (weight 0.0)
# ⇒ the block is skipped ⇒ _match_memories returns the matcher's score-descending order
# ⇒ deploy-time behaviour is byte-identical to today; rollout is an env-var A/B. The
# reward is the durable per-lesson reads/hints ratio in memory_stats.json (Q4), NOT the
# session telemetry actioned counter. See plans/bug0317_memory-hint-bandit.md.
# MEMORY_BANDIT_WEIGHT: master toggle + modulation blend in [0,1]. 0.0 = OFF (default);
#   1.0 = full Thompson-Sampling value re-rank; 0<W<1 ramps from raw order to full.
#   The path is taken iff weight > 0.0 (a true predicate). NOTE: any weight > 0.0 turns
#   on BOTH the value re-rank AND the bounded θ_min admission bar (which arms the
#   fire-rate controller), AND the render-time `hints` ledger bump — a small weight is
#   NOT a "rank-only" mode; the bar still seeds at 0.0 (admit-all) and only drifts once
#   the controller sees turns.
MEMORY_BANDIT_WEIGHT: float = float(os.getenv('AGENT_CASCADE_MEMORY_BANDIT_WEIGHT', '0.0'))
# MEMORY_BANDIT_PRIOR_MASS: prior mass m₀ of the base-rate-centred informed prior
#   Beta(m₀·p̄ + s, m₀·(1−p̄) + f), s = min(reads, hints). Small m₀ = aggressive dormancy
#   exploration; large = conservative. p̄ is the learned base rate, so a never-read lesson's
#   posterior mean is p̄ (eligible) rather than 0.5 (which would mass-spam).
MEMORY_BANDIT_PRIOR_MASS: float = float(os.getenv('AGENT_CASCADE_MEMORY_BANDIT_PRIOR_MASS', '3.0'))
# MEMORY_BANDIT_TARGET_FIRE: outcome target — the fraction of processed memory-hint turns
#   that should render ≥1 lesson. The in-memory controller drifts θ_min to hold this; a
#   target, not a hand-tuned threshold. 0.10 = fire on ~10% of turns.
MEMORY_BANDIT_TARGET_FIRE: float = float(os.getenv('AGENT_CASCADE_MEMORY_BANDIT_TARGET_FIRE', '0.10'))
# MEMORY_BANDIT_GAIN: per-turn controller drift cap (η). Bounds how fast θ_min moves,
#   damping oscillation alongside the EWMA damping (EWMA_ALPHA reused for the fire EWMA).
MEMORY_BANDIT_GAIN: float = float(os.getenv('AGENT_CASCADE_MEMORY_BANDIT_GAIN', '0.02'))
# MEMORY_BANDIT_THETA_MAX: hard ceiling on the admission bar θ_min ∈ [0, θ_max]. Since θ_max
#   < 1 and every lesson's posterior mean is centred on p̄ > 0, mass-suppression is
#   structurally impossible: the bar can never reach 1, so a candidate set is never all
#   dropped by the bandit. 0.0 (the seed / safe state) = admit-all = rank-only identity.
MEMORY_BANDIT_THETA_MAX: float = float(os.getenv('AGENT_CASCADE_MEMORY_BANDIT_THETA_MAX', '0.6'))
# MEMORY_BANDIT_M_FLOOR: promote/demote multiplier band floor (mirrors SKILL_PRECISION_FLOOR).
#   The effective multiplier is clamped to [M_FLOOR, 1/M_FLOOR], centre-neutral about p̄. A
#   dedicated knob (memories have no precision-floor setting) avoids coupling memory tuning
#   to the skill's demote floor; default identical (0.35).
MEMORY_BANDIT_M_FLOOR: float = float(os.getenv('AGENT_CASCADE_MEMORY_BANDIT_M_FLOOR', '0.35'))

# Same-turn, opportunistic hint delivery (BUG_0382). OFF by default; the feature
# stays off via llm_cfg 'memory_hint_enabled' regardless. When True AND memory_hint
# enabled, the submit at the Phase-3/4 boundary returns a completion handle and the
# post-Phase-4 peek attaches an already-computed hint to the SAME turn as a SYSTEM
# note. If the worker is not ready at the peek, the hint defers to the next turn
# exactly as today (no block, no regression). NO wait budget: the peek is always
# non-blocking, so there is no delay knob to tune.
MEMORY_HINT_INLINE: bool = os.getenv('AGENT_CASCADE_MEMORY_HINT_INLINE', '0').lower() in ('1', 'true', 'yes')

# Hard-reject similarity gate for propose_skill: a NEW skill (or an UPDATE) whose
# frontmatter text is MORE similar than this ratio to any existing registered skill
# is rejected before approval/registration. Strict `>` comparison — a score of exactly
# 0.95 PASSES (is not rejected); only scores strictly greater than 0.95 are rejected.
# Frontmatter fields only (name + description + triggers); the body is excluded for speed.
SKILL_DUP_SIM_THRESHOLD: float = float(os.getenv('AGENT_CASCADE_SKILL_DUP_SIM_THRESHOLD', '0.8'))
SKILL_CACHE_TTL_SECONDS: float = float(os.getenv('AGENT_CASCADE_SKILL_CACHE_TTL',
                                                 30.0))  # Cache TTL for mtime-based discovery cache

_SKILLS_DISABLED_RAW: str = os.getenv('AGENT_CASCADE_SKILLS_DISABLED', '')
SKILLS_DISABLED: List[str] = [s.strip().lower() for s in _SKILLS_DISABLED_RAW.split(',') if s.strip()
                             ] if _SKILLS_DISABLED_RAW else []

# ── Auto-skill generation settings ──
AUTO_SKILL_ENABLED: bool = False  # Toggle auto-skill generation on/off
AUTO_SKILL_EXTRA_TURNS: int = int(
    os.getenv('AGENT_CASCADE_AUTO_SKILL_EXTRA_TURNS',
              25))  # Fresh turn budget granted at in-loop trigger time (added on top of the exhausted budget)
AUTO_SKILL_MIN_TURNS: int = int(os.getenv('AGENT_CASCADE_AUTO_SKILL_MIN_TURNS',
                                          20))  # Fire reflection when turns effectuated > N (strictly greater)
# Activity-clock wall-clock fallback rate:
# seconds of real time mapped to one "activity turn" when the durable turns counter is
# unavailable (global_activity_turns == 0). Only used as a bounded fallback; the primary
# clock is the persisted cumulative user-turn counter. A larger value ages skills SLOWER
# (more conservative — keeps them PROTECTED longer on a stuck clock).
SKILL_WALLCLOCK_SECONDS_PER_TURN: int = int(
    os.getenv('AGENT_CASCADE_SKILL_WALLCLOCK_SECONDS_PER_TURN', '3600'))  # 1h ~ 1 turn

# ── Skill invalidation (adaptive count-cap) ────────────────────────────────
# Defaults for the startup rebalance pass: raw = round(K × n_valuable); the
# evict threshold is clamped to [MIN_CAP, MAX_CAP]. n_valuable counts
# serve-candidates classified PROTECTED/USEFUL (value-driven, not touched-count).
SKILL_ACTIVE_TARGET_K: float = float(os.getenv('AGENT_CASCADE_SKILL_ACTIVE_TARGET_K', '1.0'))
SKILL_ACTIVE_MIN_CAP: int = int(os.getenv('AGENT_CASCADE_SKILL_ACTIVE_MIN_CAP', '20'))
SKILL_ACTIVE_MAX_CAP: int = int(os.getenv('AGENT_CASCADE_SKILL_ACTIVE_MAX_CAP', '200'))

# ── Task-aware skill cache (LRU working-set + prefetch-probation) ──────────
# When enabled, the active-skill pool is sized by a fixed target and managed by a
# single eviction order (expired probation → remaining probation → demand LRU),
# replacing the multiplier-driven sizing of rebalance_active_skills. Master switch
# skill_cache_active_enabled now defaults ON (full mode: machinery runs and, with
# commit on, mutates the pool). Opt out via the UI "Skill Cache" toggle or
# AGENT_CASCADE_SKILL_CACHE_ACTIVE_ENABLED=0 — then every cache method is a no-op.
# The numeric settings are clamped to [SKILL_ACTIVE_MIN_CAP, SKILL_ACTIVE_MAX_CAP]
# by the cache itself so a stray value can't grow the pool beyond the hard ceiling.
# Master switch for the task-aware skill cache. True (default) = machinery runs: the
# prefetch worker and eviction tick read and (when commit is on) write the pool.
# False = complete no-op: every cache method short-circuits before touching the pool.
# Defaults ON so the cache is live out of the box; opt out via the UI toggle or env var.
SKILL_CACHE_ACTIVE_ENABLED: bool = (
    os.getenv('AGENT_CASCADE_SKILL_CACHE_ACTIVE_ENABLED', '1').strip().lower() in ('1', 'true', 'yes', 'on'))
# Whether the skill cache ACTS on its decisions vs only logs them. Only consulted when
# skill_cache_active_enabled is ON (the methods short-circuit first). True (default) =
# full mode: computed admissions are applied and victims are retired. False = shadow:
# prefetch/evict compute and log their choices but never mutate the active pool.
SKILL_CACHE_COMMIT_ENABLED: bool = (
    os.getenv('AGENT_CASCADE_SKILL_CACHE_COMMIT_ENABLED', '1').strip().lower() in ('1', 'true', 'yes', 'on'))
# Desired steady-state size of the active-skill pool (skills). Operating band 50-80.
SKILL_CACHE_TARGET_SIZE: int = int(os.getenv('AGENT_CASCADE_SKILL_CACHE_TARGET_SIZE', '60'))
# Number of top-ranked inactive skills a root-task-boundary prefetch speculatively
# admits (as probation). Demand recall peaks ~12; noise stays bounded by
# MAX_AUTO_SKILLS_PER_CALL at hint delivery, so a larger K is harmless.
SKILL_CACHE_PREFETCH_TOP_K: int = int(os.getenv('AGENT_CASCADE_SKILL_CACHE_PREFETCH_TOP_K', '12'))
# How long (global activity-turns) a prefetched probation skill may idle before it
# becomes eligible to evict first. Bounds idle-prefetch churn; insensitive to hit-rate.
SKILL_CACHE_PROBATION_TTL_TURNS: int = int(os.getenv('AGENT_CASCADE_SKILL_CACHE_PROBATION_TTL_TURNS', '50'))
# Upper bound on evictions applied in a single cache_evict_tick, so one boundary
# cannot sweep the active set. Bounds churn, independent of how far over target it is.
SKILL_CACHE_MAX_EVICT_PER_TICK: int = int(os.getenv('AGENT_CASCADE_SKILL_CACHE_MAX_EVICT_PER_TICK', '10'))
# Slow idle safety tick (seconds). cache_evict_tick is otherwise reached only at a root task
# boundary, so a pool that stays idle never triggers eviction; this timer drains probation TTL /
# over-target active sets on long-idle pools too (plan §2.4). The tick self-gates on
# skill_cache_active_enabled, so it is a no-op call (zero behavior change) when the cache is off.
SKILL_CACHE_IDLE_TICK_SECONDS: int = int(os.getenv('AGENT_CASCADE_SKILL_CACHE_IDLE_TICK_SECONDS', str(6 * 3600)))

# ── Skill-scoring constants ──────────────────
# Every formula/gate constant in the skill scoring is a named setting here. Each default
# matches the hard-coded fallbacks read by pool/core.py and skills/manager.py
# (_SKILL_SCORE_DEFAULTS), so exposing them changes no behavior at defaults.
SKILL_SCORE_Q0: float = float(os.getenv('AGENT_CASCADE_SKILL_SCORE_Q0', '5.0'))  # neutral quality baseline (5.0)
SKILL_SCORE_KQ: int = int(os.getenv('AGENT_CASCADE_SKILL_SCORE_KQ', '5'))  # shrinkage prior strength
SKILL_SCORE_NHALF: float = float(os.getenv('AGENT_CASCADE_SKILL_SCORE_NHALF', '8'))  # usage saturation
SKILL_SCORE_GHALF: float = float(os.getenv('AGENT_CASCADE_SKILL_SCORE_GHALF', '20'))  # load-waste scale
SKILL_SCORE_RFLOOR: float = float(os.getenv('AGENT_CASCADE_SKILL_SCORE_RFLOOR', '0.5'))  # recency floor; must stay <1
SKILL_SCORE_TAU_TURNS: int = int(os.getenv('AGENT_CASCADE_SKILL_SCORE_TAU_TURNS', '200'))  # activity-recency decay
SKILL_SCORE_DQ: float = float(os.getenv('AGENT_CASCADE_SKILL_SCORE_DQ', '0.5'))  # quality margin / neutral-band width
SKILL_SCORE_NMIN: int = int(os.getenv('AGENT_CASCADE_SKILL_SCORE_NMIN', '5'))  # meaningful-use bar
SKILL_FAIR_WINDOW_TURNS: int = int(os.getenv('AGENT_CASCADE_SKILL_FAIR_WINDOW_TURNS', '50'))  # PROTECTED window
SKILL_MAX_EVICTIONS_PER_PASS: int = int(os.getenv('AGENT_CASCADE_SKILL_MAX_EVICTIONS_PER_PASS', '25'))  # max evictions per pass

# Meta/process skills that must NEVER be evicted by the rebalance pass, even if their metrics
# would classify them BAD (BAD normally overrides the fair-window PROTECTED). This is a STRING
# setting (comma-separated names), NOT one of the clamped numeric SKILL_SCORE_SETTINGS above — it
# is normalized to the canonical lowercase comma-separated string via
# format_skill_always_protected() at load/ingress time (consumers re-parse it). The four defaults
# are the process skills the system itself depends on; a user can add/remove without code changes.
SKILL_ALWAYS_PROTECTED_DEFAULT: str = (
    'self-augmentation,project-memory-writing,bug-tracker-entry-format,skill-creator,'
    'memory-vault-consolidation')


def parse_skill_always_protected(value) -> frozenset:
    """Parse the ``skill_always_protected`` setting into a lowercase name set.

    Accepts a comma-separated string (or an already-parsed list/tuple/set). Empty and whitespace-only
    tokens are ignored; every surviving token is stripped + lowercased so matching against live skill
    names is case-insensitive on both sides. On ``None``/empty/unparseable input the DEFAULT four
    skills are returned, so a missing or blank config always protects the meta-skill set (safe).
    """
    _DEFAULT_SET = frozenset(t.strip().lower() for t in SKILL_ALWAYS_PROTECTED_DEFAULT.split(','))
    if value is None:
        raw = SKILL_ALWAYS_PROTECTED_DEFAULT
    elif isinstance(value, str):
        raw = value
        if raw.strip().lower().startswith(('frozenset(', 'set(')):
            # Legacy poisoned literal (e.g. "frozenset({'a','b'})") — tokenizing it would
            # mint junk "skills" and silently unprotect the meta-skills; the default four are
            # the only safe interpretation of a value that is no longer a name list.
            return _DEFAULT_SET
    elif isinstance(value, (list, tuple, set, frozenset)):
        # An incoming collection (UI may send a list) — normalize the same way; an empty
        # collection falls back to the default so it can never silently unprotect the meta-skills.
        # NOTE: frozenset is NOT a subclass of set, so it must be listed explicitly here.
        result = frozenset(str(v).strip().lower() for v in value if str(v).strip())
        return result or _DEFAULT_SET
    else:
        try:
            raw = str(value)
        except Exception:  # noqa: BLE001 — never let a bad setting break config load
            raw = SKILL_ALWAYS_PROTECTED_DEFAULT
    tokens = [t.strip().lower() for t in raw.split(',')]
    result = frozenset(t for t in tokens if t)
    return result or _DEFAULT_SET


def format_skill_always_protected(value) -> str:
    """Canonical persistable STRING form of ``skill_always_protected``.

    Parses via :func:`parse_skill_always_protected` and re-joins the sorted names
    comma-separated, so the value stored in ``llm_cfg`` (and persisted through
    pool_settings.json / api_endpoints.json) is always JSON-serializable. Consumers
    re-parse the string with ``parse_skill_always_protected`` (order- and
    case-insensitive), so the sorted canonical form is lossless.
    """
    return ','.join(sorted(parse_skill_always_protected(value)))


# ── Single source of truth for the skill-scoring clamp ranges ─
# Each setting's (default, lo, hi, type) lives HERE exactly once. config_handlers.py,
# pool/config_persist.py, pool/core.py and api_server.py all derive their clamps from this
# table via clamp_skill_setting() instead of hardcoding min(max(lo, val), hi) in four places.
SKILL_SCORE_SETTINGS = {
    'skill_score_q0':            {'default': SKILL_SCORE_Q0,             'lo': 0.0,  'hi': 10.0,  'type': float},
    'skill_score_kq':            {'default': SKILL_SCORE_KQ,             'lo': 0,    'hi': 20,    'type': int},
    'skill_score_nhalf':         {'default': SKILL_SCORE_NHALF,          'lo': 1.0,  'hi': 50.0,  'type': float},
    'skill_score_ghalf':         {'default': SKILL_SCORE_GHALF,          'lo': 1.0,  'hi': 100.0, 'type': float},
    # NOTE: the llm_cfg key is deliberately 'skill_score_rflood' (matches pool/core.py's read), but it
    # maps to the r_floor recency factor in the scoring formula — so its clamp must stay < 1.
    'skill_score_rflood':        {'default': SKILL_SCORE_RFLOOR,         'lo': 0.0,  'hi': 0.99,  'type': float},
    'skill_score_tau_turns':     {'default': SKILL_SCORE_TAU_TURNS,      'lo': 10,   'hi': 5000,  'type': int},
    'skill_score_dq':            {'default': SKILL_SCORE_DQ,             'lo': 0.1,  'hi': 3.0,   'type': float},
    'skill_score_nmin':          {'default': SKILL_SCORE_NMIN,           'lo': 1,    'hi': 20,    'type': int},
    'skill_fair_window_turns':   {'default': SKILL_FAIR_WINDOW_TURNS,    'lo': 0,    'hi': 1000,  'type': int},
    'skill_max_evictions_per_pass': {'default': SKILL_MAX_EVICTIONS_PER_PASS, 'lo': 0, 'hi': 1000, 'type': int},
}


def clamp_skill_setting(key: str, value):
    """Clamp one skill-scoring setting to its [lo, hi] range (single source of truth).

    Uses the SKILL_SCORE_SETTINGS table. Returns the typed clamped value (int for int settings,
    float for float settings); on an unparseable/None input it returns the setting's default (already
    within range). Raises KeyError if ``key`` is not in the table. This is the one place where a
    skill-scoring clamp is computed, so every call site (config handlers, persistence restore,
    pool/core.py reads, api_server preview) stays byte-identical. Callers that must *skip* an
    unparseable value (e.g. the persistence restore path) should cast the raw value themselves first.
    """
    spec = SKILL_SCORE_SETTINGS[key]
    cast = spec['type']
    try:
        val = cast(value)
    except (TypeError, ValueError):
        val = cast(spec['default'])
    return min(max(spec['lo'], val), spec['hi'])

AUTO_SKILL_PROMOTION_THRESHOLD: float = 0.3  # Self-match score threshold for auto-promotion
# Applies to NEW skills only: a newly-registered skill is moved straight from the pending
# staging dir into agents/global/skills/. Upgrade proposals (name == existing production skill)
# do NOT use this flag — they become live-serving candidates in agents/global/candidates/ and
# are promoted/discarded by the rating-based decision gate (evaluate_candidates).
AUTO_SKILL_AUTO_PROMOTE: bool = True  # Auto-promote validated NEW skills to agents/global/skills/
CANDIDATE_MIN_RATINGS: int = int(
    os.getenv('AGENT_CASCADE_CANDIDATE_MIN_RATINGS',
              5))  # Ratings a candidate must accumulate before the decision gate compares it with the incumbent
CANDIDATE_EVAL_INTERVAL_SECONDS: float = float(os.getenv(
    'AGENT_CASCADE_CANDIDATE_EVAL_INTERVAL', 30.0))  # Safety-net interval for the candidate evaluation timer
CANDIDATE_MAX_PENDING_DAYS: float = float(os.getenv(
    'AGENT_CASCADE_CANDIDATE_MAX_PENDING_DAYS', 14.0))  # A candidate still short of CANDIDATE_MIN_RATINGS after this
# many days is finalized (promoted) instead of hanging forever: it has already been serving since registration.
# 0 disables the deadline (candidates then wait for ratings only).
AUTO_SKILL_MAX_SIZE_KB: int = 15  # Maximum SKILL.md file size in KB
MIN_SKILL_BODY_LENGTH: int = 100  # Minimum body char count for skill validation
MIN_DESCRIPTION_LENGTH: int = 20  # Minimum description length for skill validation
# Propose_skill's non-fatal unresolved-skill-reference scan (advisory only — it
# never blocks registration or candidate promotion).
SKILL_REF_SCAN_MODE: str = os.getenv('AGENT_CASCADE_SKILL_REF_SCAN_MODE', 'hybrid')  # hybrid | cue | typo | off
SKILL_REF_SUGGEST_CUTOFF: float = 0.6  # difflib similarity floor for "did you mean" suggestions
SKILL_REF_MAX_REPORT: int = 5  # Cap on unresolved references listed per response (+N more)
