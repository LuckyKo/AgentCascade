"""
Loop Detection — Standalone module for the AgentCascade Architecture Rewrite.

Per DESIGN_REWRITE.md §7.1, loop detection is extracted into a dedicated
module so it can be shared across ExecutionEngine, api_server streaming, and
any future consumers without circular dependencies.

The algorithm: extract identifying features from recent messages and check for
repeated patterns of length L repeating K times. Returns (reason, pop_count) if
a loop is found, else None.
"""

import re
from typing import List, Optional, Tuple, Union

from agent_cascade.llm.schema import (
    ASSISTANT, CONTENT, FUNCTION, ROLE, SYSTEM, USER, Message,
)

# Regex to normalize tool response truncation markers for consistent comparison
_TOOL_TRUNCATED_RE = re.compile(
    r'\[TOOL RESPONSE TRUNCATED.*?\]', re.DOTALL
)

class LoopDetectedError(Exception):
    """Raised when a repetitive loop is detected in agent turns."""
    def __init__(self, reason, agent_name=None, pop_count=None, turn_pop_count=0, resp_snapshot=None):
        self.reason = reason
        self.agent_name = agent_name
        self.pop_count = pop_count
        self.turn_pop_count = turn_pop_count
        self.resp_snapshot = resp_snapshot or []
        super().__init__(f"Loop detected for {agent_name or 'agent'}: {reason}")


def detect_loop(
    messages: List[Union[dict, Message]],
) -> Optional[Tuple[str, int]]:
    """Detect if the agent is stuck in a repetitive loop.

    Works by extracting identifying features from recent messages and checking
    for repeated patterns of length L repeating K times.

    Args:
        messages: Full conversation history (or active set + responses).

    Returns:
        (reason, pop_count) if loop detected, else None.
        pop_count = number of messages to remove from the end to break the loop.

    Example:
        info = detect_loop(messages)
        if info:
            reason, pop_count = info
            logger.warning(f"Loop: {reason}, rolling back {pop_count} msgs")
    """
    if len(messages) < 6:
        return None

    # ── Feature extraction ──────────────────────────────────────────────
    def get_feature(m):
        """Extract a feature string from a message for loop comparison."""
        if hasattr(m, 'model_dump'):
            m = m.model_dump()
        elif not isinstance(m, dict):
            m = {
                ROLE: getattr(m, 'role', ''),
                CONTENT: getattr(m, 'content', ''),
                'reasoning_content': getattr(m, 'reasoning_content', getattr(m, 'thought', '')),
                'function_call': getattr(m, 'function_call', None),
            }

        role = m.get(ROLE)
        content = m.get(CONTENT, '')
        # Handle multimodal content (list of items with 'text' keys)
        if isinstance(content, list):
            text_parts = [
                item.get('text', '') for item in content
                if isinstance(item, dict) and item.get('type') == 'text'
            ]
            content = " ".join(text_parts)
        content = str(content)

        reasoning = str(m.get('reasoning_content', '') or m.get('thought', ''))

        # Combine reasoning and content for better loop detection
        if reasoning and not content.startswith('<think'):
            text_feature = f"{reasoning}\n{content}"
        else:
            text_feature = content or reasoning

        # Normalize truncation markers so they don't break pattern matching
        text_feature = _TOOL_TRUNCATED_RE.sub('[TOOL RESPONSE TRUNCATED]', text_feature)

        fc = m.get('function_call')
        if fc:
            name = fc.get('name') if isinstance(fc, dict) else getattr(fc, 'name', '')
            args = fc.get('arguments') if isinstance(fc, dict) else getattr(fc, 'arguments', '')
            return f"{role}:{name}:{args}"

        # For plain messages, use first 3000 chars to distinguish long reasoning
        return f"{role}:{text_feature[:3000]}"

    # ── Build feature window (last 40 non-system messages) ──────────────
    window = messages[-40:]
    features: List[str] = []
    feature_to_window_idx: List[int] = []

    for i, m in enumerate(window):
        role = m.get(ROLE) if isinstance(m, dict) else getattr(m, 'role', '')
        if role != SYSTEM:
            features.append(get_feature(m))
            feature_to_window_idx.append(i)

    if len(features) < 4:
        return None

    # ── Pattern matching: look for length-L patterns repeating K times ───
    for L in range(1, 21):
        # Require more repetitions for shorter patterns to avoid false positives
        K = 3 if L < 5 else 2

        if len(features) < L * K:
            continue

        for i in range(len(features) - (L * K), -1, -1):
            pattern = features[i : i + L]
            is_loop = True

            for k in range(1, K):
                if features[i + k * L : i + (k + 1) * L] != pattern:
                    is_loop = False
                    break

            if is_loop and features[-L:] == pattern:
                roles = [p.split(':')[0] for p in pattern]

                # Skip false positives: single-function or single-user patterns
                # (these are usually parallel tool responses, not agent loops)
                if L == 1 and roles[0] in (FUNCTION, USER):
                    continue

                # Skip FUNCTION-only sequences with no ASSISTANT messages interspersed.
                # A real agent loop always involves the agent making decisions:
                # ASSISTANT→FUNCTION→ASSISTANT→FUNCTION. If the pattern contains only
                # FUNCTION role messages consecutively (no assistant decisions between them),
                # it's likely from parallel tool execution / batch overflow, not an agent loop.
                if roles and all(role == FUNCTION for role in roles):
                    continue

                # Calculate pop_count: messages from end that belong to the loop
                second_rep_window_idx = feature_to_window_idx[i + L]
                pop_count = len(window) - second_rep_window_idx

                reason = (
                    f"Detected repeated sequence loop "
                    f"({', '.join(roles)} repeating {K} times)"
                )
                return reason, pop_count

    return None