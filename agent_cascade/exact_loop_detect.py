"""Tier 1 loop detection — exact multi-period matcher (two-tier redesign).

Replaces the legacy :func:`agent_cascade.loop_detection.detect_loop` (removed
2026-08): a contiguous, byte-for-byte period matcher over a larger window with
a quality-improved FUNCTION feature. Wired in as the rollback tier of
``_pre_llm_checks`` (see plan §4.3); the fuzzy ``detect_tool_loop`` is Tier 2
(warning-first, optional escalation).

Provenance / design authority:
    Plan:      N:\\work\\WD\\AgentWorkspace\\plans\\loop_detector_exact_redesign_PLAN.md
    Research:  N:\\work\\WD\\AgentWorkspace\\reports\\loop_detector_exact_redesign_research.md
    Lesson:    .agent_lessons/loop_detector_two_tier_redesign.md

Per-message feature (plan §2, compared byte-for-byte):
    ASSISTANT + function_call : ``{role}|fc|{name}|{raw_args}``   — args raw
    FUNCTION                  : ``{role}|{name}|{stripped_output}``
                                stripped = _normalize_output (wrapper noise)
                                + [TOOL RESPONSE TRUNCATED] marker
    ASSISTANT prose           : ``{role}|{combined[:3000]}``      — raw
    USER                      : ``{role}|{raw_content}``          — raw
    SYSTEM                    : excluded from the feature window

Guards (carried over from the old detector): skip L==1 FUNCTION/USER periods,
skip all-FUNCTION periods, contiguity required.

pop_count (plan §4.1, CRITICAL — critical-bug lesson: compute from actual
message indices, prose included in the popped span):
    ``pop_count = len(messages) - abs_idx[i + L]``
where ``i`` is the feature position where the FIRST repetition starts and
``abs_idx`` maps feature positions to ABSOLUTE indices into the full message
list (SYSTEM-filtered). Popping that many messages keeps exactly ONE
occurrence of the period.

Complexity note (plan §3): runs on every LLM call — O(W) regex passes over ≤60
messages plus a tail-first slice scan bounded by ~26k short-circuiting element
comparations. Microseconds in practice; no KMP/Z-algorithm at this size.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple, Union

from agent_cascade.llm.schema import ASSISTANT, FUNCTION, ROLE, SYSTEM, USER, Message
from agent_cascade.tool_loop_detect import _as_dict, _normalize_output, _text_of

# ── Tunables (plan §1) ───────────────────────────────────────────────────────

#: Window size in non-system messages. 2× the legacy window (40); covers
#: multi-message periods with margin (survey: legitimate periods ≤12).
EXACT_WINDOW = 60

#: Maximum period length L. Survey max legitimate period = 12; higher values
#: increase deterministic re-read false-positive surface (plan §6 item 2 —
#: the L=12 boundary case is an ACCEPTED residual, not guarded against).
EXACT_MAX_PERIOD = 12

#: Minimum window size before matching starts (kept from the old detector's
#: len(messages) < 6 guard; also bounds the worst-case search).
EXACT_MIN_WINDOW = 6

#: Max chars kept from prose/USER text features (bounds feature size; pinned
#: test_long_content_truncation depends on identical long text still matching).
_FEATURE_TEXT_LIMIT = 3000

#: [TOOL RESPONSE TRUNCATED: NN%] markers — the percentage is per-call system
#: noise. Normalized to a single token in FUNCTION features (moved from the
#: old loop_detection.py; required for pinned test t9, which the plain
#: _normalize_output does not cover).
_TOOL_RESP_TRUNC_RE = re.compile(r'\[TOOL RESPONSE TRUNCATED.*?\]', re.DOTALL)


def _repeats_for_period(L: int) -> int:
    """K schedule (identical to the old detector → zero behavioral divergence
    on all 67 pinned scenarios): K=3 for L<5, K=2 for L>=5."""
    return 3 if L < 5 else 2


# ── Feature extraction (plan §2) ─────────────────────────────────────────────

def _fc_feature(role: str, fc) -> Optional[str]:
    """Feature for a message carrying a function_call.

    Args are used EXACTLY as stored (string stays string; non-string args →
    json.dumps — same rule as ``tool_loop_detect._fc_info``). No volatile-key
    stripping, no normalization: ``justification`` is signal here by design.
    Returns None if the call has no name/args to key on (rare; such a message
    then falls through to its prose feature).
    """
    if isinstance(fc, dict):
        name = fc.get('name') or ''
        args = fc.get('arguments', '')
    else:
        name = getattr(fc, 'name', '') or ''
        args = getattr(fc, 'arguments', '') or ''
    if not name and not args:
        return None
    if not isinstance(args, str):
        try:
            args = json.dumps(args)
        except (TypeError, ValueError):
            args = str(args)
    return f"{role}|fc|{name}|{args}"


def _get_feature(m) -> str:
    """Extract the byte-comparison feature string for one message."""
    d = _as_dict(m)
    role = d.get(ROLE) or ''
    content = _text_of(d.get('content', ''))

    # function_call takes precedence over content (matches old behavior;
    # pinned test_function_call_feature: same FC + different prose still loops).
    fc = d.get('function_call')
    if fc:
        feat = _fc_feature(role, fc)
        if feat is not None:
            return feat

    if role == FUNCTION:
        stripped = _TOOL_RESP_TRUNC_RE.sub('[TOOL RESPONSE TRUNCATED]', content)
        stripped = _normalize_output(stripped)
        tool_name = d.get('name') or ''
        return f"{role}|{tool_name}|{stripped}"

    # Prose (USER / ASSISTANT without FC): raw, capped. reasoning+content are
    # combined when both present and content is not a leaked <think> block —
    # same rule as the old detector (pinned t11/t11b).
    reasoning = d.get('reasoning_content') or d.get('thought') or ''
    if isinstance(reasoning, list):
        reasoning = " ".join(_text_of(item) for item in reasoning)
    else:
        reasoning = str(reasoning or '')
    if reasoning and not content.startswith('<think'):
        combined = f"{reasoning}\n{content}"
    else:
        combined = content or reasoning
    return f"{role}|{combined[:_FEATURE_TEXT_LIMIT]}"


# ── Window construction (plan §3) ────────────────────────────────────────────

def _build_window(
    messages: List[Union[dict, Message]],
) -> Tuple[List[str], List[int]]:
    """Build the feature window + absolute-index map.

    Walks ``messages`` from the end, skips SYSTEM-role entries, and collects
    up to EXACT_WINDOW non-SYSTEM messages. For each collected message records
    BOTH its feature AND its index into the ORIGINAL unfiltered ``messages``
    list (SYSTEM entries are skipped but NOT reindexed). CRITICAL for pop_count:
    the map holds real positions in the full list, so interleaved prose/USER/
    SYSTEM messages inside the repeated span are included in the popped range.

    Returns (features, abs_idx) oldest→newest.
    """
    start = max(0, len(messages) - EXACT_WINDOW)
    feats: List[str] = []
    abs_idx: List[int] = []
    for i in range(start, len(messages)):
        m = messages[i]
        role = m.get(ROLE) if isinstance(m, dict) else getattr(m, 'role', '')
        if role == SYSTEM:
            continue
        feats.append(_get_feature(m))
        abs_idx.append(i)
    return feats, abs_idx


# ── Public API (plan §2 return contract — identical shape to old detect_loop)

def detect_exact_loop(
    messages: List[Union[dict, Message]],
) -> Optional[Tuple[str, int]]:
    """Detect an exact repeating period at the tail of the recent history.

    Finds a contiguous period P (length L in 1..EXACT_MAX_PERIOD) repeated K
    times (K=3 for L<5, K=2 for L>=5) at the END of the EXACT_WINDOW-message
    feature window. Tail-first scan: starts are checked from the newest
    position backwards and the first (most recent) match wins — identical
    semantics to the legacy detector.

    Args:
        messages: Full conversation history (list of dicts or Message objects).

    Returns:
        ``(reason, pop_count)`` if a loop is detected, else ``None``.
        ``pop_count = len(messages) - abs_idx[i + L]`` — the number of
        messages to remove from the end so that exactly ONE occurrence of the
        period remains (the first one).
    """
    if len(messages) < EXACT_MIN_WINDOW:
        return None

    feats, abs_idx = _build_window(messages)
    n = len(feats)
    if n < EXACT_MIN_WINDOW:
        return None

    for L in range(1, min(EXACT_MAX_PERIOD, n // 2) + 1):
        K = _repeats_for_period(L)
        if n < L * K:
            continue

        # Tail-anchored search: newest start position first (most recent loop
        # wins), exactly like the old detector.
        for i in range(n - L * K, -1, -1):
            pattern = feats[i:i + L]
            is_loop = all(
                feats[i + k * L:i + (k + 1) * L] == pattern
                for k in range(1, K)
            )
            if not is_loop:
                continue

            # Guards (carried over from the old detector):
            roles = [f.split('|', 1)[0] for f in pattern]
            if L == 1 and roles[0] in (FUNCTION, USER):
                continue
            if all(r == FUNCTION for r in roles):
                continue

            # Canonical pop formula (plan §4.1): pop from the start of the
            # SECOND repetition onward — keep exactly one occurrence.
            pop_count = len(messages) - abs_idx[i + L]
            reason = (
                f"exact loop: sequence ({', '.join(roles)}) repeated {K} times "
                f"(period={L})"
            )
            return reason, pop_count

    return None
