"""Standalone CLI for the slot-pool liveness stress harness.

    python -m tests.stress.run_stress --phases proof,post
    python -m tests.stress.run_stress --phases post,baseline --root N:\\work\\WD\\AgentCascade_baseline
    python -m tests.stress.run_stress --phases proof,post --only deep_conc0 --seeds 2
    python -m tests.stress.run_stress --phases mutate

Phase order (validity plan): proof → post → baseline → mutate.
The proof phase runs FIRST and aborts the rest on misclassification (exit 2).
Skipping proof prints a warning and stamps validated=false in the output.

Exit codes:
  0 — all phases clean (or only expected findings)
  1 — a post/baseline phase produced an unexpected classification
  2 — proof phase failed (a detector did not fire on its injected defect)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List

PHASES = ('proof', 'post', 'baseline', 'mutate')


def _default_root() -> str:
    return os.getcwd()


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='tests.stress.run_stress',
                                 description=__doc__.splitlines()[0])
    ap.add_argument('--root', default=_default_root(),
                    help='AgentCascade checkout to import agent_cascade from '
                         '(default: cwd). For --phases baseline this must be the '
                         'pre-COLL-2 worktree (fa77772b).')
    ap.add_argument('--phases', default='proof,post',
                    help="comma-separated subset of 'proof,post,baseline,mutate' "
                         '(default: proof,post)')
    ap.add_argument('--only', default=None,
                    help='run only this scenario name (default: the full matrix)')
    ap.add_argument('--seeds', type=int, default=None,
                    help='seeds per scenario (default: the plan\'s per-scenario count)')
    ap.add_argument('--repro-key', default=None,
                    help='reproduce a specific workload by its key (format: phase:name:idx). '
                         'Takes precedence over --only and --seeds. '
                         'Printed by repro_cmd() in stall reports.')
    ap.add_argument('--budget', type=float, default=None,
                    help='wall-clock budget in seconds for the whole phase '
                         '(default: 120 from _timing.PHASE_BUDGET_S)')
    ap.add_argument('--starvation-age', type=float, default=None,
                    help='override STARVATION_AGE_S (default: 2.0)')
    ap.add_argument('--no-progress', type=float, default=None,
                    help='override NO_PROGRESS_S (default: 3.0)')
    ap.add_argument('--proof-hold-ms', type=float, default=None,
                    help='override PROOF_HOLD_MS (default: 1500)')
    ap.add_argument('--mutate-vectors', default=None,
                    help='JSON file with recorded perturbation vectors for replay')
    ap.add_argument('--out', default=None,
                    help='write per-result JSON lines to this path')
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    if root not in sys.path:
        sys.path.insert(0, root)

    # Import AFTER sys.path is seeded so `--root` actually selects the tree.
    from tests.stress import _timing as timing_mod
    from tests.stress.scenarios import run_matrix
    import tests.stress.harness as harness_mod

    # Apply CLI overrides to timing module (before any scenario runs).
    if args.starvation_age is not None:
        timing_mod.STARVATION_AGE_S = args.starvation_age
    if args.no_progress is not None:
        timing_mod.NO_PROGRESS_S = args.no_progress
    if args.proof_hold_ms is not None:
        timing_mod.PROOF_HOLD_MS = args.proof_hold_ms
    budget = args.budget if args.budget is not None else timing_mod.PHASE_BUDGET_S

    print(f'[stress] agent_cascade from: {harness_mod.__file__}')
    print(f'[stress] thresholds: STARVATION_AGE={timing_mod.STARVATION_AGE_S}s '
          f'NO_PROGRESS={timing_mod.NO_PROGRESS_S}s BUDGET={budget}s')

    phases = [p.strip() for p in args.phases.split(',') if p.strip()]
    bad = [p for p in phases if p not in PHASES]
    if bad:
        ap.error(f'unknown phase(s): {bad}; valid: {list(PHASES)}')

    # Validity stamp: proof must run before post/baseline.
    validated = False
    if 'proof' not in phases and any(p in ('post', 'baseline') for p in phases):
        print('[stress] WARNING: proof phase skipped — results are NOT validated '
              '(detectors unproven). Stamping validated=false.')

    sink = open(args.out, 'w', encoding='utf-8') if args.out else None
    worst = 0
    try:
        for phase in phases:
            phase_root = root
            if phase == 'baseline' and phase_root == _default_root():
                print('[stress] WARNING: --phases baseline without --root will run the '
                      'BASELINE PHASE against the CURRENT tree. Point --root at the '
                      'pre-COLL-2 worktree (fa77772b).')

            if phase == 'proof':
                from tests.stress.proof import run_proof_phase, proof_summary, proof_failed
                print(f'\n===== PHASE proof (root={phase_root}) =====')
                t0 = time.monotonic()
                cases = run_proof_phase(root=phase_root, budget_s=budget)
                elapsed = time.monotonic() - t0
                print(proof_summary(cases))
                print(f'[proof] wall time: {elapsed:.1f}s')
                if proof_failed(cases):
                    print('[stress] FATAL: proof phase failed — detectors unproven. '
                          'Aborting remaining phases.')
                    return 2
                validated = True

            elif phase == 'mutate':
                from tests.stress.mutate import run_mutation_phase, PerturbationVector
                print(f'\n===== PHASE mutate (root={phase_root}) =====')
                t0 = time.monotonic()
                vectors = None
                if args.mutate_vectors:
                    with open(args.mutate_vectors, 'r', encoding='utf-8') as f:
                        raw = json.load(f)
                    vectors = [PerturbationVector(**v) for v in raw]
                summary = run_mutation_phase(root=phase_root, budget_s=budget,
                                             vectors=vectors)
                elapsed = time.monotonic() - t0
                print(f"[mutate] {summary['note']}")
                print(f"[mutate] vectors={len(summary['vectors'])} "
                      f"findings={len(summary['findings'])} wall={elapsed:.1f}s")
                for f in summary['findings']:
                    print(f'  FINDING: {json.dumps(f)}')

            else:
                phase_root = root
                if phase == 'baseline':
                    # Baseline worktree: use the baseline tree's own tests/stress.
                    # The harness is importable from both trees (lazy imports).
                    pass
                print(f'\n===== PHASE {phase} (root={phase_root}) =====')
                t0 = time.monotonic()
                verdict = run_matrix(phase, phase_root, only=args.only, seeds=args.seeds,
                                     budget_s=budget,
                                     on_result=lambda r: _emit(r, sink),
                                     repro_key=args.repro_key)
                elapsed = time.monotonic() - t0
                print(verdict.summary())
                print(f'[{phase}] wall time: {elapsed:.1f}s')
                for stall in verdict.stalls:
                    print(stall.render())
                if not verdict.is_clean():
                    worst = max(worst, 1)
    finally:
        if sink is not None:
            sink.close()

    # Stamp validated flag in output.
    if sink is not None and args.out:
        try:
            with open(args.out, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'validated': validated}) + '\n')
        except Exception:  # noqa: BLE001
            pass

    return worst


def _emit(result, sink) -> None:
    line = result.line()
    print(line, flush=True)
    if result.dump:
        print(result.dump, flush=True)
    if sink is not None:
        sink.write(json.dumps({
            'scenario': result.scenario, 'seed': result.seed,
            'classification': result.classification, 'turns': result.turns,
            'elapsed_s': round(result.elapsed, 3),
            'max_waiter_age_s': round(result.max_waiter_age_s, 3),
            'counts': result.counts, 'error': result.error,
        }) + '\n')
        sink.flush()


if __name__ == '__main__':
    raise SystemExit(main())
