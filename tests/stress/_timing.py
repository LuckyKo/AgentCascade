"""Configurable timing constants for the stress harness (validity plan §0).

All watchdog thresholds, starvation threshold, proof hold durations and phase
budgets live here so a full valid run finishes in minutes. Every value can be
overridden via environment variable (``STRESS_<NAME>``) or CLI flag in
``run_stress.py`` — the user directive is small defaults first:

    STARVATION_AGE_S = 2.0   (was 8.0) — waiter older than this while progress
                                       still flows is a fairness failure.
    NO_PROGRESS_S    = 3.0   (was 10.0) — idle window before the watchdog fires.
    SCENARIO_BUDGET_S = 45.0 (was 60.0) — per-scenario wall clock.
    PHASE_BUDGET_S   = 120.0 (was 900.0) — per-phase wall clock default.

The harness's own ``QUEUE_WAIT_TIMEOUT`` (harness.py:37) MUST stay greater than
NO_PROGRESS_S so a thread stuck in SlotPool.acquire is classified by the
watchdog BEFORE the acquire timeout fires and masks it as a benign event.
25.0 > 3.0 ✔ — keep that invariant if you tune these.
"""

from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(f'STRESS_{name}', default))
    except (TypeError, ValueError):
        return default


# ── Watchdog / classification thresholds ────────────────────────────────
STARVATION_AGE_S = _f('STARVATION_AGE_S', 2.0)      # F-3: starvation threshold
NO_PROGRESS_S = _f('NO_PROGRESS_S', 3.0)             # watchdog idle window
SCENARIO_BUDGET_S = _f('SCENARIO_BUDGET_S', 45.0)    # per-scenario budget
PHASE_BUDGET_S = _f('PHASE_BUDGET_S', 120.0)         # per-phase budget default

# ── Proof-phase hold durations (structural defects need no long waits) ──
PROOF_HOLD_MS = _f('PROOF_HOLD_MS', 4000.0)          # zombie/starvation holder hold
PROOF_WAITER_HOLD_S = _f('PROOF_WAITER_HOLD_S', 8.0)  # keep waiter alive past threshold
