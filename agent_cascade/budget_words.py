"""Centralised wording for turn / token budget messages (cost/token-budget Slice 2).

One source of truth for the warning / allocation / limit-notice text so that:

* **turns mode** strings stay byte-for-byte identical to the pre-Slice-2 sites
  (``engine/run_loop.py`` and ``lifecycle_manager.py``), and
* **tokens mode** adds the ``N tok / $X`` rendering driven by the generated-token
  accumulator, with the ``$`` shown **only when the serving endpoint is priced**.

Cost *math* lives in ``cost.py``; this module only **formats** a caller-supplied
``cost`` (``None`` → no ``$``). Keeping the wording here means the turns-mode
regression tests keep passing while both modes share one code path.
"""

from typing import Optional


def _cost_suffix(cost: Optional[float]) -> str:
    """Render `` / $X`` when priced, otherwise empty."""
    return f" / ${cost:.2f}" if cost is not None else ''


def budget_message(
    kind: str,
    unit: str,
    *,
    remaining: int = 0,
    total: int = 0,
    past: int = 0,
    cost: Optional[float] = None,
) -> str:
    """Build one budget warning message.

    ``kind`` ∈ {``halfway``, ``approaching``, ``final_soft``, ``final_hard``,
    ``over_limit``}. ``unit`` ∈ {``turns`` (default, byte-identical wording),
    ``tokens``}. In tokens mode the ``$`` overlay is appended when ``cost`` is
    supplied (priced endpoint); ``cost=None`` renders tokens only.
    """
    if unit == 'tokens':
        cs = _cost_suffix(cost)
        if kind == 'halfway':
            return (f"[SYSTEM WARNING: Halfway through your token budget. "
                    f"You have {remaining} tok{cs} remaining out of {total} tok total. "
                    f"Assess your progress and plan remaining steps.]")
        if kind == 'approaching':
            return (f"[SYSTEM WARNING: Token budget approaching. "
                    f"You have {remaining} tok{cs} remaining out of {total} tok total. "
                    f"Plan your remaining steps carefully.]")
        if kind == 'final_soft':
            return ('[SYSTEM WARNING: Final token budget. You are at the end of your token '
                    'budget; tools stay enabled and the run will continue past the limit. '
                    'Wrap up and deliver your results now.]')
        if kind == 'final_hard':
            return ('[SYSTEM WARNING: Final token budget. Your token budget is exhausted and '
                    'tools have been disabled. Wrap up and deliver your results now.]')
        if kind == 'over_limit':
            return (f"[SYSTEM WARNING: You are over the token budget, please wrap up. "
                    f"{past} tok past the {total} tok budget.]")
        raise ValueError(f"budget_message: unknown kind={kind!r}")

    # ── turns mode — byte-for-byte identical to the pre-Slice-2 sites ──────────
    if kind == 'halfway':
        return (f"[SYSTEM WARNING: Halfway through your turn budget. "
                f"You have {remaining} turn(s) remaining out of {total} total. "
                f"Assess your progress and plan remaining steps.]")
    if kind == 'approaching':
        return (f"[SYSTEM WARNING: Turn limit approaching. "
                f"You have {remaining} turn(s) remaining out of {total} total. "
                f"Plan your remaining steps carefully.]")
    if kind == 'final_soft':
        return ('[SYSTEM WARNING: Final turn. You have 1 turn left before the '
                'soft turn limit; tools stay enabled and the run will continue '
                'past the limit. Wrap up and deliver your results now.]')
    if kind == 'final_hard':
        return ('[SYSTEM WARNING: Final turn. You have 1 turn left to complete your task and tools have been disabled. '
                'Wrap up and deliver your results now.]')
    if kind == 'over_limit':
        return (f"[SYSTEM WARNING: You are over the turn limit, please wrap up. "
                f"{past} turn(s) past the {total}-turn limit.]")
    raise ValueError(f"budget_message: unknown kind={kind!r}")


def budget_allocation_notice(total: int, unit: str = 'turns', cost: Optional[float] = None) -> str:
    """Child-spawn notice announcing the budget this task gets (``lifecycle_manager``)."""
    if unit == 'tokens':
        return f"\n\nYour token budget for this task: {total:,} tok{_cost_suffix(cost)}."
    return f"\n\nYour turn budget for this task: {total} turns."


def budget_limit_notice(unit: str = 'turns') -> str:
    """Notice appended to the last assistant message when the budget is exhausted."""
    if unit == 'tokens':
        return '\n\n[Token budget reached — results may be incomplete. Continue if needed.]'
    return '\n\n[Turn limit reached — results may be incomplete. Continue if needed.]'
