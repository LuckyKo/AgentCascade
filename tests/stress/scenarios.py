"""Phase 1 scenario matrix + verdict logic (plan §4/§5).

Runs a list of Scenarios through the Harness, with a Watchdog armed for each.
Every scenario result is retained; nothing is aborted on a stall — the stall
is recorded, its threads are abandoned (daemon), and the next scenario starts
with a FRESH pool/router (so one deadlock cannot cascade into the matrix).

Validity-plan changes:
  * STARVATION_AGE_S / NO_PROGRESS_S come from _timing.py (small defaults,
    env-overridable) instead of hardcoded 8.0/10.0.
  * F-2: `--seeds` no longer re-derives the seed with salted hash(); the
    builder's deterministic rng IS the workload definition, and the recorded
    seed is a stable blake2b digest of (phase, name, idx) so `repro_cmd`
    reproduces the exact workload.
  * F-4: blocked_flags passed to the Watchdog counts only acquire:/reacquire:
    values — async_body: markers no longer inflate the DEADLOCK predicate.
  * F-6: the dead TIMEOUT-reassignment branch is gone; a stalled run keeps the
    watchdog's classification, and the post-hoc STARVATION backstop remains.
  * `expected` (ScenarioSpec.expected) is enforced: mismatches are recorded on
    the Result + Verdict as findings (not aborts — only the proof phase
    hard-aborts).
  * is_clean() returns False when the verdict was truncated (complete=False).
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import _timing
from .harness import Harness, Result
from .watchdog import ProgressLog, StallReport, Watchdog
from .workload import (
    Scenario, build_async_fanout, build_deep_chain, build_dismiss_storm,
    build_grandparent_dismiss, build_long_hold, build_soak, build_sticky_churn,
)

# Starvation threshold: a waiter older than this while the system is still
# making progress is a fairness problem, not a liveness failure.
# (validity plan §0: default lowered 8.0 -> 2.0, env-overridable.)
STARVATION_AGE_S = _timing.STARVATION_AGE_S
NO_PROGRESS_S = _timing.NO_PROGRESS_S


def stable_seed(phase: str, name: str, idx: int) -> int:
    """F-2 fix: deterministic recorded seed (stable across processes).

    `hash()` of a tuple containing str is salted by PYTHONHASHSEED and differs
    per process — the old code made `--seeds` non-reproducible. blake2b is not.
    """
    return int(hashlib.blake2b(f'{phase}:{name}:{idx}'.encode(), digest_size=8)
               .hexdigest(), 16) % (2**31)


@dataclass
class ScenarioSpec:
    name: str
    seeds: int
    build: Callable[[random.Random, int], Scenario]
    expected: str = 'NONE'


# (b) deep_conc0 is listed first: it is the scenario that actually drives the
# COLL-2 ancestor-collision path (grandparent chain on a conc=0 pool).
# Validity plan §3.5: soak 25->40, dismiss_storm 20->25, new long_hold (6).
SCENARIOS: List[ScenarioSpec] = [
    # deep_conc0 on a conc=0 pool can produce brief starvation (waiter > 2.0s)
    # when multiple chains queue simultaneously. Fairness finding, not liveness.
    ScenarioSpec('deep_conc0', 10,
                 lambda rng, i: build_deep_chain(rng, 'stress_a', 'stress_b', depth=4),
                 expected='NONE|STARVATION'),
    # soak with 8 concurrent roots on a conc=0 pool can legitimately produce
    # brief starvation (waiter age > 2.0s) during peak contention. This is a
    # FAIRNESS finding, not a liveness failure. Accept NONE or STARVATION.
    ScenarioSpec('soak', 40, lambda rng, i: build_soak(rng, 6), expected='NONE|STARVATION'),
    # dismiss_storm idx 0 is the ZOMBIE-designated seed (F-5 switch); the other
    # seeds must stay NONE. Per-seed expectation is enforced in run_matrix.
    ScenarioSpec('dismiss_storm', 25, lambda rng, i: build_dismiss_storm(rng, 3)),
    ScenarioSpec('async_fanout', 10, lambda rng, i: build_async_fanout(rng, 4)),
    ScenarioSpec('sticky_churn', 10, lambda rng, i: build_sticky_churn(rng, 5)),
    ScenarioSpec('grandparent_dismiss', 5, lambda rng, i: build_grandparent_dismiss(rng, 3)),
    ScenarioSpec('long_hold', 6, lambda rng, i: build_long_hold(rng)),
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
    expected_mismatches: List[str] = field(default_factory=list)

    @property
    def expected_ok(self) -> bool:
        return not self.expected_mismatches

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
        for m in self.expected_mismatches:
            lines.append(f'  EXPECTED-MISMATCH: {m}')
        return '\n'.join(lines)

    def is_clean(self) -> bool:
        # A truncated phase cannot be called clean — it only ran part of the matrix.
        if not self.complete:
            return False
        return all(r.classification == 'NONE' for r in self.results) and not self.stalls


def run_scenario(scenario: Scenario, tmp_dir: str, root: str, phase: str,
                 no_progress_s: float = NO_PROGRESS_S,
                 waiter_starve_s: float = STARVATION_AGE_S) -> tuple:
    """Run one scenario to completion (or until the watchdog declares a stall)."""
    log = ProgressLog()
    harness = Harness(scenario, log, tmp_dir, root=root, phase=phase)
    seen: List[StallReport] = []

    def _blocked_flags():
        # F-4 fix: only acquire:/reacquire: values mean "blocked on a slot
        # wait". async_body: is a lifetime marker for async workers and must
        # not inflate the DEADLOCK predicate.
        return {tid: f'{what}@{tid}'
                for tid, what in harness.flags.registry.items()
                if what.startswith(('acquire:', 'reacquire:'))}

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
        blocked_flags=_blocked_flags,
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
        # F-6 fix: keep the watchdog's classification as-is. (The old code had
        # a no-op branch reassigning 'TIMEOUT' to itself; the "old waiter =>
        # STARVATION" intent is covered by the post-hoc check below and by the
        # watchdog's own STARVATION branch.)
        result.classification = report.classification
        result.dump = report.render()
    elif result.error:
        result.classification = 'UNEXPLAINED'

    # Post-hoc fairness check: did any waiter exceed the starvation threshold
    # even though the run completed (or stalled as TIMEOUT)?
    if result.classification in ('NONE', 'TIMEOUT') and result.max_waiter_age_s > waiter_starve_s:
        result.classification = 'STARVATION'

    harness.teardown()
    return result, seen


def run_matrix(phase: str,
               root: str,
               only: Optional[str] = None,
               seeds: Optional[int] = None,
               budget_s: float = _timing.PHASE_BUDGET_S,
               tmp_factory: Optional[Callable[[str], str]] = None,
               on_result: Optional[Callable[[Result], None]] = None,
               repro_key: Optional[str] = None) -> Verdict:
    """Run the scenario matrix for one phase under a wall-clock budget.

    A budget overrun skips the REMAINDER of the current phase and labels the
    verdict provisional — it never silently truncates a phase.

    `repro_key` (F-2 reviewer fix): if set, run ONLY the workload identified
    by that key (format: 'phase:name:idx'). This reconstructs the exact RNG
    stream for a specific scenario instance, making repro_cmd() truly
    reproducible. Takes precedence over `only` and `seeds`.
    """
    import tempfile

    verdict = Verdict(phase=phase, budget_s=budget_s)
    started = time.monotonic()

    # F-2: --repro-key takes precedence — run exactly one workload.
    if repro_key is not None:
        parts = repro_key.split(':')
        if len(parts) != 3:
            verdict.note = f'invalid repro_key format: {repro_key!r} (expected phase:name:idx)'
            return verdict
        r_phase, r_name, r_idx_str = parts
        try:
            r_idx = int(r_idx_str)
        except ValueError:
            verdict.note = f'invalid repro_key idx: {r_idx_str!r}'
            return verdict
        spec = next((s for s in SCENARIOS if s.name == r_name), None)
        if spec is None:
            verdict.note = f'repro_key references unknown scenario: {r_name!r}'
            return verdict
        rng = random.Random(f'{phase}:{spec.name}:{r_idx}')
        scenario = spec.build(rng, r_idx)
        scenario.seed = stable_seed(phase, spec.name, r_idx)
        scenario.set_repro_key(phase, r_idx)
        if spec.name == 'dismiss_storm' and r_idx == 0:
            scenario.permit_release_disabled_for = {'c0'}
        tmp_dir = (tmp_factory(spec.name) if tmp_factory
                   else tempfile.mkdtemp(prefix=f'stress_{phase}_{spec.name}_{r_idx}_'))
        result, stalls = run_scenario(scenario, tmp_dir, root, phase)
        verdict.results.append(result)
        verdict.stalls.extend(stalls)
        if on_result is not None:
            on_result(result)
        verdict.elapsed = time.monotonic() - started
        return verdict

    specs = [s for s in SCENARIOS if only is None or s.name == only]

    for spec in specs:
        for idx in range(seeds if seeds is not None else spec.seeds):
            if time.monotonic() - started > budget_s:
                verdict.complete = False
                verdict.note = (f'PROVISIONAL: budget {budget_s}s exhausted after '
                                f'{len(verdict.results)} scenarios')
                return verdict
            rng = random.Random(f'{phase}:{spec.name}:{idx}')   # deterministic workload
            scenario = spec.build(rng, idx)
            # F-2 fix: stable digest + repro_key. The rng above already fully
            # determines the workload; repro_key is what repro_cmd prints.
            scenario.seed = stable_seed(phase, spec.name, idx)
            scenario.set_repro_key(phase, idx)

            # F-5 exposure (validity plan §3.4): exactly one designated seed per
            # phase runs dismiss_storm with the release-suppression switch so
            # the production zombie state is exercised once, not 25 times.
            if spec.name == 'dismiss_storm' and idx == 0:
                scenario.permit_release_disabled_for = {'c0'}

            tmp_dir = (tmp_factory(spec.name) if tmp_factory
                       else tempfile.mkdtemp(prefix=f'stress_{phase}_{spec.name}_{idx}_'))
            result, stalls = run_scenario(scenario, tmp_dir, root, phase)

            # `expected` enforcement: a mismatch is a FINDING, not an abort.
            # `expected` can be a pipe-separated set (e.g. 'NONE|STARVATION').
            expected = spec.expected
            if spec.name == 'dismiss_storm' and idx == 0:
                expected = 'ZOMBIE|DEADLOCK|STARVATION'  # designated zombie seed (F-5 switch on)
            elif spec.name == 'long_hold':
                expected = 'STARVATION'
            expected_set = set(expected.split('|'))
            if result.classification not in expected_set:
                verdict.expected_mismatches.append(
                    f'{spec.name}[{idx}] expected={expected} got={result.classification}')

            verdict.results.append(result)
            verdict.stalls.extend(stalls)
            if on_result is not None:
                on_result(result)

    verdict.elapsed = time.monotonic() - started
    return verdict
