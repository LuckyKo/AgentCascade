"""Standalone CLI for the slot-pool liveness stress harness.

    python -m tests.stress.run_stress --phases post
    python -m tests.stress.run_stress --phases baseline --root N:\\work\\WD\\AgentCascade_baseline
    python -m tests.stress.run_stress --phases post --only deep_conc0 --seeds 2

Phase 1 (`post`) runs against the current tree. Phase 2 (`baseline`) runs the
IDENTICAL matrix against the pre-COLL-2 worktree (d78f2347~1 == fa77772b); it
must be a SEPARATE checkout, which the runner does not create.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

PHASES = ('post', 'baseline')


def _default_root() -> str:
    return os.getcwd()


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='tests.stress.run_stress',
                                 description=__doc__.splitlines()[0])
    ap.add_argument('--root', default=_default_root(),
                    help='AgentCascade checkout to import agent_cascade from '
                         '(default: cwd). For --phases baseline this must be the '
                         'pre-COLL-2 worktree.')
    ap.add_argument('--phases', default='post',
                    help="comma-separated subset of 'post,baseline' (default: post)")
    ap.add_argument('--only', default=None,
                    help='run only this scenario name (default: the full matrix)')
    ap.add_argument('--seeds', type=int, default=None,
                    help='seeds per scenario (default: the plan\'s per-scenario count)')
    ap.add_argument('--budget', type=float, default=900.0,
                    help='wall-clock budget in seconds for the whole phase (default: 900)')
    ap.add_argument('--out', default=None,
                    help='write per-result JSON lines to this path')
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    if root not in sys.path:
        sys.path.insert(0, root)

    # Import AFTER sys.path is seeded so `--root` actually selects the tree.
    from tests.stress.scenarios import run_matrix
    import tests.stress.harness as harness_mod
    print(f'[stress] agent_cascade from: {harness_mod.__file__}')

    phases = [p.strip() for p in args.phases.split(',') if p.strip()]
    bad = [p for p in phases if p not in PHASES]
    if bad:
        ap.error(f'unknown phase(s): {bad}; valid: {list(PHASES)}')

    sink = open(args.out, 'w', encoding='utf-8') if args.out else None
    worst = 0
    try:
        for phase in phases:
            phase_root = root
            if phase == 'baseline' and phase_root == _default_root():
                print('[stress] WARNING: --phases baseline without --root will run the '
                      'BASELINE PHASE against the CURRENT tree. Point --root at the '
                      'pre-COLL-2 worktree (fa77772b).')
            print(f'\n===== PHASE {phase} (root={phase_root}) =====')
            verdict = run_matrix(phase, phase_root, only=args.only, seeds=args.seeds,
                                 budget_s=args.budget,
                                 on_result=lambda r: _emit(r, sink))
            print(verdict.summary())
            for stall in verdict.stalls:
                print(stall.render())
            worst = max(worst, 0 if verdict.is_clean() else 1)
    finally:
        if sink is not None:
            sink.close()
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
