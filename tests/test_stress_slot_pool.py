"""Pytest entry points for the slot-pool liveness stress harness.

These run a SMALL, bounded subset of the phase-1 matrix. The full matrix
(75 scenarios) is a standalone job:

    python -m tests.stress.run_stress --phases post

A stall here is a REAL result, not a test bug: `test_no_deadlock` fails with
the full dump and the repro seed so the scenario can be re-run standalone.
"""

from __future__ import annotations

import os
import random
import sys
import tempfile

import pytest

# `AGENT_CASCADE_INSTANCE_ID` must be set before `agent_cascade` is imported,
# and the repo root must be importable for `tests.stress.*`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
os.environ.setdefault('AGENT_CASCADE_INSTANCE_ID', 'stress-pytest')

from tests.stress.scenarios import (  # noqa: E402
    SCENARIOS, STARVATION_AGE_S, run_matrix, run_scenario,
)
from tests.stress.workload import (  # noqa: E402
    build_async_fanout, build_deep_chain, build_dismiss_storm,
    build_grandparent_dismiss, build_soak, build_sticky_churn,
)

# Wall-clock budget for the whole pytest module. Generous enough that a clean
# run is never truncated, small enough that a hard hang fails in CI time.
MODULE_BUDGET_S = 600.0


def _run(builder, seed: int, budget_s: float = 60.0):
    """Run one scenario in an isolated temp dir; returns (result, stalls)."""
    scenario = builder(random.Random(seed))
    scenario.budget_s = budget_s
    with tempfile.TemporaryDirectory(prefix='stress_pytest_') as td:
        return run_scenario(scenario, td, _REPO_ROOT, 'post')


def _assert_clean(result, stalls) -> None:
    if result.dump:
        pytest.fail(f'slot-pool stall detected.\n\n{result.dump}')
    assert result.classification == 'NONE', (
        f'{result.line()}\n'
        f'expected NONE, got {result.classification}; '
        f'repro: scenario={result.scenario} seed={result.seed}')
    assert not stalls, f'unexpected stalls recorded: {stalls}'


def test_deep_conc0_liveness():
    """(b) COLL-2's home scenario: concurrent grandparent chains on a conc=0 pool.

    Every level must release its permit before the child acquires (COLL-2) and
    re-acquire afterwards, while a rival chain is queued on the same pool.
    """
    for seed in (11, 12, 13):
        result, stalls = _run(
            lambda r: build_deep_chain(r, 'stress_a', 'stress_b', depth=4), seed)
        _assert_clean(result, stalls)


def test_soak_liveness():
    """(a) Mixed actions, one shared sequential pool."""
    result, stalls = _run(lambda r: build_soak(r, 6), 21)
    _assert_clean(result, stalls)


def test_async_fanout_liveness():
    """(d) Async children go through register_async_call, the second seam."""
    result, stalls = _run(lambda r: build_async_fanout(r, 4), 31)
    _assert_clean(result, stalls)


def test_sticky_churn_liveness():
    """(f) Many agents force sticky-slot sync onto one pool."""
    result, stalls = _run(lambda r: build_sticky_churn(r, 5), 41)
    _assert_clean(result, stalls)


def test_grandparent_dismiss_liveness():
    """(e) Root dismisses a mid-chain node that COLL-2 may still hold."""
    result, stalls = _run(lambda r: build_grandparent_dismiss(r, 3), 51)
    _assert_clean(result, stalls)


def test_dismiss_storm_surfaces_permit_leak():
    """(c) KNOWN-OPEN BUG: dismiss does not release the slot permit.

    A dismissed-then-reused instance name must be able to acquire its permit
    again. If the permit leaks, the pool stays occupied by a `_running` entry
    whose instance is gone (a ZOMBIE), and subsequent acquires time out.

    Expected today: either the reuse succeeds (bug absent) or the run is
    classified ZOMBIE/DEADLOCK — both are a valid, reported outcome. UNEXPLAINED
    is the only classification that means the harness itself is broken.
    """
    result, stalls = _run(lambda r: build_dismiss_storm(r, 3), 61)

    if result.classification == 'UNEXPLAINED':
        pytest.fail(f'harness error (not a product finding):\n{result.line()}\n'
                    + (result.dump or ''))
    if result.dump:
        print(result.dump)
    # A clean run is the desired outcome; ZOMBIE/DEADLOCK are the known bug.
    assert result.classification in ('NONE', 'ZOMBIE', 'DEADLOCK', 'TIMEOUT'), \
        f'unexpected classification: {result.line()}'


def test_waiter_fairness_threshold():
    """No waiter may be starved for longer than the fairness threshold."""
    result, _stalls = _run(lambda r: build_soak(r, 8), 71)
    assert result.max_waiter_age_s <= STARVATION_AGE_S, \
        f'waiter age {result.max_waiter_age_s:.2f}s exceeded {STARVATION_AGE_S}s'


def test_bounded_matrix_smoke():
    """One seed of every scenario, under a wall-clock guard.

    Guards the TIME GUARD itself: if a scenario ever hangs, the budget cuts the
    phase short and the verdict is labelled PROVISIONAL rather than silently
    truncated.
    """
    verdict = run_matrix('post', _REPO_ROOT, seeds=1, budget_s=MODULE_BUDGET_S)
    ran = {r.scenario for r in verdict.results}
    assert ran <= {s.name for s in SCENARIOS}
    assert len(verdict.results) >= 1
    if not verdict.complete:
        pytest.fail(f'matrix did not complete: {verdict.note}')
    for r in verdict.results:
        assert r.classification != 'UNEXPLAINED', f'{r.line()}\n{r.dump}'
