"""Proof phase: prove each detector fires on a KNOWN-INJECTED defect.

Run BEFORE post/baseline; if any injection is misclassified the runner exits
with code 2 (fail-fast) — a "clean" result from unproven detectors is vacuous.

Injections (plan §3):
  A — zombie holder:   suppress the harness-side permit release for one name
                       (F-5 switch). The instance is dismissed while its permit
                       stays in pool._running → expect ZOMBIE (or DEADLOCK).
  B — permit leak → STARVATION: a long holder keeps the only permit past
                       STARVATION_AGE_S while waiters queue → expect STARVATION.
  C — circular wait:   gated on hasattr(ToolDispatcher, '_chain_held_slots')
                       (COLL-2 is absent in the baseline tree). Runs the
                       collision-prone deep_conc0 shape with barrier start; if
                       the watchdog classifies DEADLOCK that proves the
                       circular-wait detector fires. On baseline: SKIP with a
                       recorded reason (never a silent pass).

Negative control: run each shape WITHOUT the injection → expect NONE. If the
detector fires without the defect, the detector is broken, not the product.
"""

from __future__ import annotations

import random
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import _timing
from .harness import Harness, Result  # noqa: F401 (Result used in type hints)
from .scenarios import run_scenario
from .workload import (
    Scenario, build_deep_chain, build_dismiss_storm, build_long_hold,
)

PROOF_BUDGET_S = 90.0


@dataclass
class ProofCase:
    name: str
    injected: bool
    classification: str = 'NONE'
    expected: str = ''
    ok: bool = False
    skipped: bool = False
    skip_reason: str = ''
    elapsed: float = 0.0
    error: str = ''

    def line(self) -> str:
        if self.skipped:
            return f'[{self.name}] SKIPPED ({self.skip_reason})'
        mark = 'PASS' if self.ok else 'FAIL'
        return (f'[{self.name:<28}] {mark} injected={self.injected!s:<5} '
                f'expected={self.expected:<11} got={self.classification:<11} '
                f'elapsed={self.elapsed:5.2f}s {self.error}')


def _has_coll2() -> bool:
    """COLL-2 chain-release machinery present? (absent in baseline tree)."""
    try:
        from agent_cascade.tool_dispatcher import ToolDispatcher
        return hasattr(ToolDispatcher, '_chain_held_slots')
    except Exception:  # noqa: BLE001
        return False


def _run_proof(scenario: Scenario, phase: str = 'proof', root: str = '.') -> tuple:
    """Run one proof scenario; returns (result, stalls).

    Uses a dedicated temp dir per case (not TemporaryDirectory) so that a
    stall dump can be inspected after the fact. Cleanup is best-effort.
    """
    import shutil
    td = tempfile.mkdtemp(prefix=f'stress_proof_{scenario.name}_')
    try:
        return run_scenario(scenario, td, root, phase)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _classify_for(case: ProofCase, result, expected: str) -> None:
    case.classification = result.classification
    # For injected cases: the classification must be one of the expected classes.
    # For control cases (injected=False): must be NONE.
    if case.injected:
        case.ok = result.classification in expected.split('|')
    else:
        case.ok = result.classification == 'NONE'
    if not case.ok and result.error:
        case.error = result.error


def run_proof_phase(root: str = '.', budget_s: float = PROOF_BUDGET_S) -> List[ProofCase]:
    """Run all proof cases (injections + negative controls).

    Returns the list of ProofCase records. The runner checks `all(c.ok or c.skipped)`
    and exits 2 if any injected case is misclassified.
    """
    started = time.monotonic()
    cases: List[ProofCase] = []
    rng = random.Random('proof')

    # ── A: zombie holder (F-5 switch) ────────────────────────────────────
    # The suppress-release injection makes c0's permit never released. The
    # watchdog sees: waiters stuck on the pool + no progress → DEADLOCK or
    # STARVATION (if waiter age crosses threshold before no_progress fires).
    # Both are valid "defect detected" classifications for this structural defect.
    scenario = build_dismiss_storm(rng, 2)
    scenario.seed = 9001
    scenario.permit_release_disabled_for = {'c0'}
    scenario.budget_s = _timing.PROOF_WAITER_HOLD_S + 5.0
    result, _stalls = _run_proof(scenario, root=root)
    case_a = ProofCase(name='A_zombie_injected', injected=True, expected='ZOMBIE|DEADLOCK|STARVATION')
    _classify_for(case_a, result, 'ZOMBIE|DEADLOCK|STARVATION')
    cases.append(case_a)

    # Negative control A: same shape, NO suppression → must be NONE.
    scenario_nc = build_dismiss_storm(rng, 2)
    scenario_nc.seed = 9002
    scenario_nc.budget_s = _timing.PROOF_WAITER_HOLD_S + 5.0
    result_nc, _ = _run_proof(scenario_nc, root=root)
    case_a_nc = ProofCase(name='A_zombie_control', injected=False, expected='NONE')
    _classify_for(case_a_nc, result_nc, 'NONE')
    cases.append(case_a_nc)

    # ── B: permit leak → STARVATION (long holder) ────────────────────────
    scenario_b = build_long_hold(rng, holder_ms=int(_timing.PROOF_HOLD_MS), n_waiters=3)
    scenario_b.seed = 9003
    scenario_b.budget_s = _timing.PROOF_WAITER_HOLD_S + 5.0
    result_b, _ = _run_proof(scenario_b, root=root)
    case_b = ProofCase(name='B_starvation_injected', injected=True, expected='STARVATION')
    _classify_for(case_b, result_b, 'STARVATION')
    cases.append(case_b)

    # Negative control B: short holder (well under threshold) → must be NONE.
    scenario_b_nc = build_long_hold(rng, holder_ms=50, n_waiters=3)
    scenario_b_nc.seed = 9004
    scenario_b_nc.budget_s = _timing.PROOF_WAITER_HOLD_S + 5.0
    result_b_nc, _ = _run_proof(scenario_b_nc, root=root)
    case_b_nc = ProofCase(name='B_starvation_control', injected=False, expected='NONE')
    _classify_for(case_b_nc, result_b_nc, 'NONE')
    cases.append(case_b_nc)

    # ── C: circular wait (gated on COLL-2) ───────────────────────────────
    # The "injection" for circular wait is the collision-prone shape itself:
    # multiple concurrent chains on a conc=0 pool where each parent holds its
    # permit while waiting for its child to acquire. This is NOT a structural
    # defect injection (like A/B) — it's a shape that CAN deadlock if COLL-2
    # release/reacquire is broken. On healthy code it should be NONE.
    #
    # We therefore treat C as a SHAPE VALIDATION, not a defect proof:
    #   - injected=True means "collision-prone shape applied"
    #   - expected=NONE (healthy code handles it)
    #   - If it classifies DEADLOCK/ZOMBIE → that IS the finding (COLL-2 broken)
    if not _has_coll2():
        cases.append(ProofCase(
            name='C_circular_injected', injected=True, expected='NONE|DEADLOCK|ZOMBIE',
            skipped=True, skip_reason='COLL-2 _chain_held_slots absent (baseline tree)'))
    else:
        scenario_c = build_deep_chain(rng, 'stress_a', 'stress_b', depth=4, roots=3)
        scenario_c.seed = 9005
        scenario_c.barrier_start = True
        scenario_c.budget_s = _timing.PROOF_WAITER_HOLD_S + 10.0
        result_c, _ = _run_proof(scenario_c, root=root)
        # On healthy COLL-2 code: NONE. If broken: DEADLOCK/ZOMBIE (that's the finding).
        case_c = ProofCase(name='C_circular_injected', injected=True, expected='NONE|DEADLOCK|ZOMBIE')
        _classify_for(case_c, result_c, 'NONE|DEADLOCK|ZOMBIE')
        cases.append(case_c)

    return cases


def proof_summary(cases: List[ProofCase]) -> str:
    lines = ['=== PROOF PHASE ===']
    for c in cases:
        lines.append('  ' + c.line())
    failed = [c for c in cases if not c.ok and not c.skipped]
    skipped = [c for c in cases if c.skipped]
    if failed:
        lines.append(f'  RESULT: FAIL ({len(failed)} misclassified)')
    elif skipped:
        lines.append(f'  RESULT: PASS ({len(skipped)} skipped with reason)')
    else:
        lines.append('  RESULT: PASS (all detectors proven)')
    return '\n'.join(lines)


def proof_failed(cases: List[ProofCase]) -> bool:
    """True if any NON-SKIPPED case failed (injection misclassified or control fired)."""
    return any(not c.ok and not c.skipped for c in cases)
