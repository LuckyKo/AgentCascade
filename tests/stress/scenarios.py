"""Phase 1 scenario matrix + verdict logic (plan §4/§5).

Runs a list of Scenarios through the Harness, with a Watchdog armed for each.
Every scenario result is retained; nothing is aborted on a stall — the stall
is recorded, its threads are abandoned (daemon), and the next scenario starts
with a FRESH pool/router (so one deadlock cannot cascade into the matrix).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .harness import Harness, Result
from .watchdog import ProgressLog, StallReport, Watchdog
from .workload import (
    Scenario, build_async_fanout, build_deep_chain, build_dismiss_storm,
    build_grandparent_dismiss, build_soak, build_sticky_churn,
)

# Starvation threshold: a waiter older than this while the system is still
# making progress is a fairness problem, not a liveness failure.
STARVATION_AGE_S = 8.0


@dataclass
class ScenarioSpec:
    name: str
    seeds: int
    build: Callable[[random.Random, int], Scenario]
    expected: str = 'NONE'


# (b) deep_conc0 is listed first: it is the scenario that actually drives the
# COLL-2 ancestor-collision path (grandparent chain on a conc=0 pool).
SCENARIOS: List[ScenarioSpec] = [
    ScenarioSpec('deep_conc0', 10,
                 lambda rng, i: build_deep_chain(rng, 'stress_a', 'stress_b', depth=4)),
    ScenarioSpec('soak', 25, lambda rng, i: build_soak(rng, 6)),
    ScenarioSpec('dismiss_storm', 20, lambda rng, i: build_dismiss_storm(rng, 3)),
    ScenarioSpec('async_fanout', 10, lambda rng, i: build_async_fanout(rng, 4)),
    ScenarioSpec('sticky_churn', 10, lambda rng, i: build_sticky_churn(rng, 5)),
    ScenarioSpec('grandparent_dismiss', 5, lambda rng, i: build_grandparent_dismiss(rng, 3)),
]


@dataclass
class Verdict:
    phase: str
    results: List[Result] = field(default_factory=list)
    stalls: List[StallReport] = field(default_factory=list)
    elapsed: float = 0.0
    budget_s: float = 0.0
    complete: bool = True
    note: str = ''

    def counts(self) -> dict:
        out = {}
        for r in self.results:
            out[r.classification] = out.get(r.classification, 0) + 1
        return out

    def summary(self) -> str:
        lines = [f'phase={self.phase} ran={len(self.results)} counts={self.counts()} '
                 f'elapsed={self.elapsed:.1f}s complete={self.complete} {self.note}']
        for r in self.results:
            lines.append('  ' + r.line())
        return '\n'.join(lines)

    def is_clean(self) -> bool:
        return all(r.classification == 'NONE' for r in self.results) and not self.stalls


def run_scenario(scenario: Scenario, tmp_dir: str, root: str, phase: str,
                 no_progress_s: float = 10.0,
                 waiter_starve_s: float = STARVATION_AGE_S) -> tuple:
    """Run one scenario to completion (or until the watchdog declares a stall)."""
    log = ProgressLog()
    harness = Harness(scenario, log, tmp_dir, root=root, phase=phase)
    seen: List[StallReport] = []

    wd = Watchdog(
        log=log,
        budget_s=scenario.budget_s,
        no_progress_s=no_progress_s,
        waiter_starve_s=waiter_starve_s,
        scenario=scenario.name,
        seed=scenario.seed,
        phase=phase,
        seed_repro=scenario.repro_cmd(phase, root),
        on_stall=seen.append,
        collect=harness.collect,
        blocked_flags=lambda: {tid: f'{harness.flags.registry.get(tid, '')}@{tid}'
                               for tid in harness.flags.registry},
        all_alive=harness.all_workers_done,
        thread_stacks=harness.thread_stacks,
    )

    harness.setup()
    wd.start()
    started = time.monotonic()
    try:
        harness._launch_roots()
        harness._join_all()
    except Exception as exc:  # noqa: BLE001
        harness._fail('run', exc)
    finally:
        elapsed = time.monotonic() - started
        wd.stop()
        harness._stop.set()

    result = Result(scenario=scenario.name, seed=scenario.seed, elapsed=elapsed,
                    turns=harness._turns, counts=log.counts(),
                    error='; '.join(harness._errors[:5]))
    result.max_waiter_age_s = wd.max_waiter_age_s

    if seen:
        report = seen[0]
        result.classification = report.classification
        result.dump = report.render()
        # A pure perf blowout with progress still flowing is TIMEOUT, and a
        # waiter that is merely old is STARVATION — both are recorded.
        if report.classification == 'TIMEOUT' and wd.max_waiter_age_s > waiter_starve_s:
            result.classification = 'TIMEOUT'
    elif result.error:
        result.classification = 'UNEXPLAINED'

    # Post-hoc fairness check: did any waiter exceed the starvation threshold
    # even though the run completed?
    if result.classification == 'NONE' and result.max_waiter_age_s > waiter_starve_s:
        result.classification = 'STARVATION'

    harness.teardown()
    return result, seen


def run_matrix(phase: str,
               root: str,
               only: Optional[str] = None,
               seeds: Optional[int] = None,
               budget_s: float = 900.0,
               tmp_factory: Optional[Callable[[str], str]] = None,
               on_result: Optional[Callable[[Result], None]] = None) -> Verdict:
    """Run the scenario matrix for one phase under a wall-clock budget.

    A budget overrun skips the REMAINDER of the current phase and labels the
    verdict provisional — it never silently truncates a phase.
    """
    import tempfile

    verdict = Verdict(phase=phase, budget_s=budget_s)
    started = time.monotonic()
    specs = [s for s in SCENARIOS if only is None or s.name == only]

    for spec in specs:
        for idx in range(seeds if seeds is not None else spec.seeds):
            if time.monotonic() - started > budget_s:
                verdict.complete = False
                verdict.note = (f'PROVISIONAL: budget {budget_s}s exhausted after '
                                f'{len(verdict.results)} scenarios')
                return verdict
            rng = random.Random(f'{phase}:{spec.name}:{idx}')
            scenario = spec.build(rng, idx)
            if seeds is not None:
                scenario.seed = abs(hash((spec.name, idx, phase))) % (2**31)
            tmp_dir = (tmp_factory(spec.name) if tmp_factory
                       else tempfile.mkdtemp(prefix=f'stress_{phase}_{spec.name}_{idx}_'))
            result, stalls = run_scenario(scenario, tmp_dir, root, phase)
            verdict.results.append(result)
            verdict.stalls.extend(stalls)
            if on_result is not None:
                on_result(result)

    verdict.elapsed = time.monotonic() - started
    return verdict
