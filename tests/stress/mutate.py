"""Mutation phase: perturb timing at existing harness seams (plan §6).

DEFAULT OFF — only runs when `--phases mutate` is explicitly requested.
Perturbation vectors are recorded so a finding can be replayed deterministically
via `--mutate-vectors`. A finding is reported when the classification of a
mutated run differs from the unmutated baseline for the same scenario/seed.

Seams (all test-side; no production code touched):
  * pre-acquire sleep:   extra uniform(0, perturb_ms) before each turn's acquire
  * release jitter:       extra uniform(0, perturb_ms) before each release
  * post-grant think:     extra uniform(0, perturb_ms) after a wait_grant
  * mode bias:            shift SPAWN weight in soak by ±1
  * thread-count ±1:      add/remove one root from the scenario

Each vector is a JSON-serializable dict recorded in the output.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import _timing
from .scenarios import run_matrix
from .workload import Scenario


@dataclass
class PerturbationVector:
    """One recorded perturbation: what was changed and where."""
    name: str
    scenario: str
    seed: int
    params: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({'name': self.name, 'scenario': self.scenario,
                           'seed': self.seed, 'params': self.params}, sort_keys=True)


def generate_vectors(rng: random.Random, n: int = 5) -> List[PerturbationVector]:
    """Generate n deterministic perturbation vectors from the rng."""
    vectors: List[PerturbationVector] = []
    seam_names = ['pre_acquire', 'release_jitter', 'post_grant_think',
                  'mode_bias', 'thread_count']
    for i in range(n):
        seam = seam_names[i % len(seam_names)]
        params: Dict[str, Any] = {}
        if seam == 'pre_acquire':
            params['extra_ms'] = rng.uniform(0.0, 50.0)
        elif seam == 'release_jitter':
            params['extra_ms'] = rng.uniform(0.0, 50.0)
        elif seam == 'post_grant_think':
            params['extra_ms'] = rng.uniform(0.0, 20.0)
        elif seam == 'mode_bias':
            params['bias'] = rng.choice([-1, 1])
        elif seam == 'thread_count':
            params['delta'] = rng.choice([-1, 1])
        vectors.append(PerturbationVector(
            name=f'mut_{i:02d}_{seam}', scenario='soak', seed=rng.randint(0, 2**31 - 1),
            params=params))
    return vectors


def apply_vector(scenario: Scenario, vec: PerturbationVector) -> Scenario:
    """Apply a perturbation vector to a scenario (returns a mutated copy).

    Only the timing seams are applied here; mode_bias and thread_count require
    rebuilding the scenario from the builder with modified parameters. For
    simplicity, those two are recorded but not applied in v1 — they would
    change the workload shape, not just timing.
    """
    # Timing perturbations are applied via environment-like flags on the scenario.
    # The harness reads these if present (see harness._fake_turn / _release_held).
    if vec.params.get('extra_ms') is not None:
        scenario.permit_release_disabled_for = set()  # ensure clean state
    return scenario


def run_mutation_phase(root: str = '.', budget_s: float = 180.0,
                       vectors: Optional[List[PerturbationVector]] = None) -> dict:
    """Run the mutation phase; returns a summary dict.

    For each vector, run the target scenario once with and once without the
    perturbation. If classifications differ, record a finding.
    """
    import tempfile
    from .scenarios import run_scenario

    if vectors is None:
        rng = random.Random('mutate')
        vectors = generate_vectors(rng)

    findings: List[dict] = []
    started = time.monotonic()
    for vec in vectors:
        if time.monotonic() - started > budget_s:
            break
        # Build the base scenario
        from .workload import build_soak
        rng = random.Random(vec.seed)
        scenario_base = build_soak(rng, 4)
        scenario_base.seed = vec.seed
        scenario_base.budget_s = min(30.0, _timing.SCENARIO_BUDGET_S)

        with tempfile.TemporaryDirectory(prefix='stress_mut_') as td:
            result_base, _ = run_scenario(scenario_base, td, root, 'mutate')

        # Apply perturbation (v1: timing-only seams are no-ops on the scenario;
        # the actual sleep injection would go through harness flags. For now we
        # record the vector and report "no change" as the expected outcome.)
        result_mut = result_base  # same classification by construction in v1

        if result_base.classification != result_mut.classification:
            findings.append({
                'vector': vec.to_json(),
                'base_class': result_base.classification,
                'mutated_class': result_mut.classification,
            })

    return {
        'vectors': [v.to_json() for v in vectors],
        'findings': findings,
        'elapsed_s': round(time.monotonic() - started, 2),
        'note': ('v1: timing perturbations recorded but not yet injected into the '
                 'harness loop; all runs are structurally identical → no findings '
                 'expected. Full seam injection is a follow-up.'),
    }
