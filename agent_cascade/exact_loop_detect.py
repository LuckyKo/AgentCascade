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
    ASSISTANT + function_call : ``{role}|fc|{name}|{raw_args}|{combined[:3000]}``
                                — args raw; combined = reasoning+content capped at
                                _FEATURE_TEXT_LIMIT using the SAME combination rule as
                                the prose branch (so a genuinely-distinct turn that only
                                differs in its reasoning/content produces a DISTINCT
                                feature and is NOT flagged as a loop). When the FC message
                                carries no content/reasoning of its OWN, combined falls
                                back to the text of the most recent preceding non-SYSTEM
                                ASSISTANT-without-FC sibling (which, in the real engine, is
                                the immediately-preceding reasoning/prose message) — because
                                oai.py splits a reasoning+tool-call turn into TWO assistant
                                messages (the bare FC message follows the reasoning/prose
                                message). Only when there is NO own-text AND no such sibling
                                does combined stay empty → the feature is byte-identical to
                                the legacy bare-FC form ``{role}|fc|{name}|{raw_args}``.
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

Complexity (plan §3): runs per LLM call — O(W) regex passes over ≤60 messages
plus a bounded tail-first scan; microseconds in practice, no KMP needed.
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


def _combined_text(d: dict, content: str) -> str:
    """Combine a message's reasoning_content/thought with its content.

    Shared by the FC and prose branches so both apply the SAME rule (pinned
    t11/t11b): when reasoning is present AND content is not a leaked `` block,
    combine as ``reasoning\\ncontent``; otherwise use whichever is non-empty.
    """
    reasoning = d.get('reasoning_content') or d.get('thought') or ''
    if isinstance(reasoning, list):
        reasoning = " ".join(_text_of(item) for item in reasoning)
    else:
        reasoning = str(reasoning or '')
    if reasoning and not content.startswith('<think'):
        return f"{reasoning}\n{content}"
    return content or reasoning


def _get_feature(m, sibling_text: str = '') -> str:
    """Extract the byte-comparison feature string for one message.

    ``sibling_text`` (optional) is the capped combined text of the immediately
    preceding non-SYSTEM ASSISTANT-without-FC message — the reasoning/prose
    sibling that the engine (oai.py) emits as a SEPARATE message right before a
    bare tool-call message. Used only to disambiguate an otherwise-bare FC
    feature; ignored when the FC message carries its own text.
    """
    d = _as_dict(m)
    role = d.get(ROLE) or ''
    content = _text_of(d.get('content', ''))

    # function_call takes precedence over content, but the assistant's own
    # content/reasoning are STILL folded into the feature (capped at
    # _FEATURE_TEXT_LIMIT) so a genuinely-distinct turn — same tool call but
    # different reasoning — produces a DISTINCT feature and is NOT flagged as a
    # loop. A truly identical call with identical surrounding text still matches.
    fc = d.get('function_call')
    if fc:
        feat = _fc_feature(role, fc)
        if feat is not None:
            combined = _combined_text(d, content)[:_FEATURE_TEXT_LIMIT]
            if not combined:
                # The engine (oai.py) splits a reasoning+tool-call turn into TWO
                # assistant messages: a bare FC message (content='', no
                # reasoning) preceded by the sibling reasoning/prose message.
                # Fold in that sibling's text so distinct turns produce distinct
                # features instead of an identical bare-FC feature (false loop).
                combined = sibling_text[:_FEATURE_TEXT_LIMIT]
            # Bare FC with NO own-text AND no sibling stays byte-identical to the
            # legacy ``{role}|fc|{name}|{args}`` form so bare-FC matching is preserved.
            return f"{feat}|{combined}" if combined else feat

    if role == FUNCTION:
        stripped = _TOOL_RESP_TRUNC_RE.sub('[TOOL RESPONSE TRUNCATED]', content)
        stripped = _normalize_output(stripped)
        tool_name = d.get('name') or ''
        return f"{role}|{tool_name}|{stripped}"

    # Prose (USER / ASSISTANT without FC): raw, capped. reasoning+content are
    # combined via the shared rule above (pinned t11/t11b).
    combined = _combined_text(d, content)
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
    # Precompute each message's own combined text once (used both for the FC
    # own-text branch and to feed a preceding sibling into a bare FC feature).
    pre: List[Tuple[dict, str]] = []
    for m in messages[start:]:
        d = _as_dict(m)
        pre.append((d, _combined_text(d, _text_of(d.get('content', '')))))
    feats: List[str] = []
    abs_idx: List[int] = []
    prev_sib: str = ''  # combined text of the last collected ASSISTANT-without-FC msg
    for j, (d, own) in enumerate(pre):
        role = d.get(ROLE) or ''
        if role == SYSTEM:
            continue
        sibling = prev_sib if d.get('function_call') else ''
        feats.append(_get_feature(messages[start + j], sibling))
        abs_idx.append(start + j)
        # A bare FC message (no own text) does NOT refresh the sibling pointer,
        # so a later FC can still borrow the reasoning that preceded it.
        if role == ASSISTANT and not d.get('function_call'):
            prev_sib = own
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
