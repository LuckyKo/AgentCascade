"""Configurable timing constants for the stress harness (validity plan §0).

All watchdog thresholds, starvation threshold, proof hold durations and phase
budgets live here so a full valid run finishes in minutes. The user directive
is small defaults first.

CLI-flagged constants (``run_stress.py --flag`` AND env ``STRESS_<NAME>``):
    STARVATION_AGE_S = 2.0   (was 8.0) — waiter older than this while progress
                                        still flows is a fairness failure.
    NO_PROGRESS_S    = 3.0   (was 10.0) — idle window before the watchdog fires.
    PROOF_HOLD_MS    = 4000  (was n/a)  — proof-phase holder hold duration.
    PHASE_BUDGET_S   = 120.0 (was 900.0) — per-phase wall clock default.

Env-only constants (``STRESS_<NAME>`` only, no CLI flag):
    SCENARIO_BUDGET_S = 45.0 (was 60.0) — per-scenario wall clock.
    PROOF_WAITER_HOLD_S = 8.0          — keep waiter alive past threshold.
    BARRIER_TIMEOUT   = 5.0            — barrier fall-through seconds.
    QUEUE_WAIT_TIMEOUT = 25.0          — bounded queue wait (see invariant).
    MAX_TURNS         = 20             — depth cap; generous vs nesting depth.
    JOIN_TIMEOUT      = 30.0           — thread join timeout.
    WAIT_SETTLE       = 1.0            — post-join settle delay.

Invariant: ``QUEUE_WAIT_TIMEOUT`` MUST stay greater than ``NO_PROGRESS_S`` so a
thread stuck in SlotPool.acquire is classified by the watchdog BEFORE the
acquire timeout fires and masks it as a benign event. 25.0 > 3.0 ✔ — keep that
invariant if you tune these.
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

# ── Barrier timeout (reviewer finding 5: was hardcoded 10.0 in harness.py) ──
BARRIER_TIMEOUT = _f('BARRIER_TIMEOUT', 5.0)         # seconds; barrier fall-through

# ── Harness internal constants (env-only, no CLI flag) ───────────────────
QUEUE_WAIT_TIMEOUT = _f('QUEUE_WAIT_TIMEOUT', 25.0)  # bounded queue wait → stall surfaces
MAX_TURNS = int(_f('MAX_TURNS', 20))                 # depth cap; generous vs nesting depth
JOIN_TIMEOUT = _f('JOIN_TIMEOUT', 30.0)              # thread join timeout
WAIT_SETTLE = _f('WAIT_SETTLE', 1.0)                 # post-join settle delay
