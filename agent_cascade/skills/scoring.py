"""Pure skill-scoring functions (research §5/§7). No I/O, no side effects, no globals read.

These operate only on a plain metrics snapshot (``n``, ``avg``, ``L``) plus the scalar
activity-age ``A`` (user-turns since the skill last had a chance to be used, research §15).
They are deliberately pure so BOTH the read-only preview path and the real rebalance pass
can reuse them without locks, I/O, or shared state.

Formula (research §5), all factors bounded in [0,1]:

    score = u(n) · [ q̂(n,avg) / 10 ] · w(L,n) · r(A)
      q̂   = (n·avg + K_q·q0) / (n + K_q)     # shrinkage quality, [0,10]; q̂=q0 when n=0
      u   = 1 − exp(−n / n_half)              # saturating usage/popularity
      w   = exp(−max(0, L−n) / g_half)        # load-waste penalty (w=1 when L<=n)
      r   = r_floor + (1−r_floor)·exp(−A/τ)   # bounded activity-recency tiebreaker

The time-stable core ``S = u·(q̂/10)·w`` is neutral at 0.5; the full ``score = S·r`` applies
bounded recency on top (a minor tiebreaker — it can only reduce, never increase, the score).
"""

import math

CLASS_BAD = 'BAD'
CLASS_PROTECTED = 'PROTECTED'
CLASS_USEFUL = 'USEFUL'
CLASS_USELESS = 'USELESS'
CLASS_UNPROVEN = 'UNPROVEN'

# Eviction-ordering ordinals (research §17): lower evicts first. PROTECTED has NO ordinal —
# it is excluded from the eviction candidate set entirely (immune to both the absolute gate
# and the count-cap until its fair window expires), so it is not a member of the sorted set.
CLASS_ORDINAL = {CLASS_BAD: 0, CLASS_USELESS: 1, CLASS_UNPROVEN: 2, CLASS_USEFUL: 3}


def _shrinkage_qhat(n: int, avg, q0: float, kq: float) -> float:
    """Posterior-mean / empirical-Bayes quality estimate in [0,10]; q̂=q0 when n=0 (research §4.2).

    ``avg`` may be ``None`` when ``n == 0`` (no ratings recorded); the pure prior ``q0`` is used.
    """
    a = avg if (avg is not None and n > 0) else q0
    return (n * a + kq * q0) / (n + kq)


def skill_score(n: int, avg, L: int, A: int, *,
                q0=5.0, kq=5, n_half=8, g_half=20, r_floor=0.5, tau_turns=200) -> dict:
    """Pure bounded score in [0,1] (research §5).

    Returns every factor for display/reproducibility: ``{'score', 'S', 'qhat', 'u', 'w', 'r'}``
    where ``S`` is the time-stable core and ``score = S * r``.
    """
    qhat = _shrinkage_qhat(n, avg, q0, kq)
    u = 1.0 - math.exp(-n / n_half)                      # saturating usage, [0,1]
    w = math.exp(-max(0, L - n) / g_half)                # load-waste penalty, [0,1] (w=1 when L<=n)
    r = r_floor + (1.0 - r_floor) * math.exp(-A / tau_turns)  # bounded recency, [r_floor,1]
    S = u * (qhat / 10.0) * w                            # time-stable core, neutral at 0.5
    score = S * r                                        # full score; recency is a minor tiebreaker
    return {'score': score, 'S': S, 'qhat': qhat, 'u': u, 'w': w, 'r': r}


def skill_classify(n: int, avg, A: int, *,
                   q0=5.0, kq=5, dq=0.5, n_min=5, fair_window_turns=50) -> str:
    """Pure quality-gated classification (research §7).

    Precedence: BAD > PROTECTED > {USEFUL, USELESS} > UNPROVEN. A demonstrably harmful skill
    (n>=n_min and clearly below baseline) is evicted even if young; the fair window shields only
    the unproven from retirement. Keys off ``q̂``, ``n`` and ``A`` — NOT off the raw score.
    """
    qhat = _shrinkage_qhat(n, avg, q0, kq)
    if qhat <= q0 - dq and n >= n_min:
        return CLASS_BAD                                  # clear harm + confident; evicted even if young
    if A < fair_window_turns:
        return CLASS_PROTECTED                            # <N user-turns since last chance (unless BAD)
    if qhat >= q0 + dq and n >= n_min:
        return CLASS_USEFUL                               # above baseline + meaningfully used
    if abs(qhat - q0) <= dq:
        return CLASS_USELESS                              # neutral quality, not earning its keep
    return CLASS_UNPROVEN                                 # monitor / keep (exploration)


def eviction_rank_key(class_name: str, score: float, name: str):
    """``(class_ordinal, score, name.lower())`` — research §17 ordering guarantee.

    BAD(0) < USELESS(1) < UNPROVEN(2) < USEFUL(3); lower evicts first. The ordinal dominates the
    lexicographic comparison, so a BAD skill always sorts before any USELESS one regardless of
    score; ``score`` only breaks ties within a class and ``name.lower()`` is the final stable tiebreak.

    PROTECTED has no ordinal (excluded from the eviction candidate set) — passing it raises
    ``KeyError`` by design, so a caller that forgets to filter PROTECTED fails loudly instead of
    silently mis-sorting.
    """
    return (CLASS_ORDINAL[class_name], score, name.lower())
