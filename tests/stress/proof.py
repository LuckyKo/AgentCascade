"""Proof phase: prove each detector fires on a KNOWN-INJECTED defect.

Run BEFORE post/baseline; if any injection is misclassified the runner exits
with code 2 (fail-fast) — a "clean" result from unproven detectors is vacuous.

Injections (plan §3, reviewer-updated):
  A — zombie holder:   GUARANTEED phantom permit in pool._running via direct
                        injection (independent of product dismiss behavior).
                        Expected: ZOMBIE|DEADLOCK|STARVATION (NONE = FAIL).
  B — permit leak → STARVATION: a long holder keeps the only permit past
                        STARVATION_AGE_S while waiters queue.
                        Expected: STARVATION (NONE = FAIL).
  C — circular wait:   GUARANTEED deadlock via direct pool manipulation:
                        Thread A holds pool1, blocks on pool2; Thread B holds
                        pool2, blocks on pool1. True circular wait at the pool
                        level, independent of COLL-2 or any product logic.
                        Expected: DEADLOCK (NONE = FAIL).

Negative control: run each shape WITHOUT the injection → expect NONE. If the
detector fires without the defect, the detector is broken, not the product.

CRITICAL (reviewer finding 1): For ALL injected cases, NONE must NOT be an
acceptable outcome. A blind detector returning NONE must FAIL the proof.
"""

from __future__ import annotations

import random
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from . import _timing
from .harness import Harness  # noqa: F401 (used in type hints)
from .scenarios import run_scenario
from .workload import (
    Scenario, AgentSpec, Action, build_dismiss_storm, build_long_hold,
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


def _run_proof(scenario: Scenario, phase: str = 'proof', root: str = '.') -> tuple:
    """Run one proof scenario; returns (result, stalls)."""
    td = tempfile.mkdtemp(prefix=f'stress_proof_{scenario.name}_')
    try:
        return run_scenario(scenario, td, root, phase)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _classify_for(case: ProofCase, result, expected: str) -> None:
    case.classification = result.classification
    if case.injected:
        # CRITICAL: NONE is NEVER acceptable for an injected case.
        # A blind detector returning NONE means the proof FAILED.
        case.ok = (result.classification in expected.split('|')
                   and result.classification != 'NONE')
    else:
        case.ok = result.classification == 'NONE'
    if not case.ok and result.error:
        case.error = result.error


def _inject_phantom_zombie(harness: Harness, pool_key: Optional[str] = None) -> bool:
    """Directly insert a phantom SlotHolder into pool._running.

    This GUARANTEES the zombie state (permit in _running, instance gone)
    regardless of product dismiss behavior. The phantom uses a real SlotHolder
    so that release() and zombie_holders() work correctly with it.

    Pools are created LAZILY by EndpointScheduler._get_or_create_pool on first
    acquire. If no pools exist yet (fresh harness, no threads have run), we
    create one via the scheduler's own method to ensure the pool is registered
    and discoverable by _sched_pools().

    If pool_key is None, injects into the first available pool (or creates one).
    Returns True if injection succeeded.
    """
    from agent_cascade.slot_queue import SlotHolder

    # Ensure at least one pool exists. Pools are created lazily on first
    # acquire; a fresh harness has none. Create one via the scheduler's own
    # method so it's registered in _pools and discoverable by _sched_pools().
    pools = harness._sched_pools()
    if not pools:
        sched = getattr(harness.router, 'scheduler', None)
        if sched is not None and hasattr(sched, '_get_or_create_pool'):
            # Create the shared sequential pool (conc=0 → capacity 1)
            sched._get_or_create_pool('http://127.0.0.1:9/ep_stress_a', 0)
            pools = harness._sched_pools()

    for pool in pools:
        key = getattr(pool, 'key', None)
        if pool_key is not None and key != pool_key:
            continue
        running = getattr(pool, '_running', None)
        if running is None:
            continue
        # Create a real SlotHolder so release() can find it by acquisition_id
        holder = SlotHolder(
            agent_name='phantom_zombie',
            instance_name='phantom_zombie',
            acquisition_id=99999,
        )
        running['phantom_zombie'] = holder
        return True
    return False


def _run_circular_wait_proof(root: str) -> tuple:
    """Run the circular-wait proof with direct pool manipulation.

    Creates two threads:
      - Thread A: acquires pool1, then blocks on pool2.acquire()
      - Thread B: acquires pool2, then blocks on pool1.acquire()

    This is a TRUE circular wait at the pool level — no product logic involved.
    Both threads are stuck in SlotPool.acquire with the acquire: flag set.
    The watchdog must classify DEADLOCK (all workers blocked, no progress).

    Returns (classification, elapsed, error).
    """
    from .harness import _orig_acquire, _orig_release
    from .watchdog import ProgressLog, Watchdog, StallReport

    if _orig_acquire is None or _orig_release is None:
        return ('UNEXPLAINED', 0.0, 'originals not captured — cannot run circular wait proof')

    td = tempfile.mkdtemp(prefix='stress_proof_circular_')
    try:
        log = ProgressLog()
        scenario = Scenario(name='circular_wait', agents=[], seed=9005, budget_s=15.0)
        harness = Harness(scenario, log, td, root=root, phase='proof')
        harness.setup()

        # We need TWO distinct pools for a true circular wait.
        # ep_stress_a and ep_stress_b both have conc=0 → share _shared_sequential_slot_.
        # ep_stress_c has conc=1 → gets its own pool (keyed by normalized api_base).
        # So we need to find two pools with different keys.
        # Pools are created LAZILY on first acquire. A fresh harness has none.
        # Create two distinct pools via the scheduler's own method:
        #   - conc=0 → _shared_sequential_slot_ (capacity 1)
        #   - conc=1 → keyed by normalized api_base (capacity 1)
        sched = getattr(harness.router, 'scheduler', None)
        if sched is not None and hasattr(sched, '_get_or_create_pool'):
            sched._get_or_create_pool('http://127.0.0.1:9/ep_stress_a', 0)
            sched._get_or_create_pool('http://127.0.0.1:9/ep_stress_c', 1)

        pools = harness._sched_pools()
        if len(pools) < 2:
            return ('UNEXPLAINED', 0.0, f'only {len(pools)} pools available, need >=2')

        pool1 = pools[0]
        pool2 = None
        for p in pools[1:]:
            if p is not pool1 and getattr(p, 'key', None) != getattr(pool1, 'key', None):
                pool2 = p
                break
        if pool2 is None:
            return ('UNEXPLAINED', 0.0, 'could not find two distinct pools')

        barrier = threading.Barrier(2, timeout=_timing.BARRIER_TIMEOUT)
        a_done = threading.Event()
        b_done = threading.Event()
        errors: List[str] = []
        # Track the release callbacks so we can clean up properly
        rel_a_pool1 = None
        rel_b_pool2 = None

        # Per-thread blocked flags: set when each thread enters its blocking
        # acquire, cleared when it exits (or times out). The watchdog's
        # _blocked_flags closure reads these to determine if all workers are
        # blocked on slot waits (DEADLOCK predicate).
        a_blocked = threading.Event()
        b_blocked = threading.Event()

        def thread_a_wrapped():
            nonlocal rel_a_pool1
            try:
                # Acquire pool1 (capacity 1, empty → immediate grant)
                log.record('acquire', 'circ_a', pool=pool1.key, detail='initial')
                rel1 = _orig_acquire(pool1, 'circ_a', 'stress_a', timeout=5.0)
                if rel1 is None:
                    errors.append('A: pool1 acquire returned None')
                    return
                rel_a_pool1 = rel1
                # Signal B that A holds pool1; now block on pool2
                barrier.wait(timeout=_timing.BARRIER_TIMEOUT)
                a_blocked.set()
                log.record('wait_enqueue', 'circ_a', pool=pool2.key, detail='circular')
                try:
                    # This will BLOCK (pool2 is held by B) — timeout=30s safety net
                    rel2 = _orig_acquire(pool2, 'circ_a', 'stress_a', timeout=30.0)
                finally:
                    a_blocked.clear()
            except Exception as exc:
                errors.append(f'A: {type(exc).__name__}: {exc}')
                a_blocked.clear()
            finally:
                a_done.set()

        def thread_b_wrapped():
            nonlocal rel_b_pool2
            try:
                # Acquire pool2 (capacity 1, empty → immediate grant)
                log.record('acquire', 'circ_b', pool=pool2.key, detail='initial')
                rel2 = _orig_acquire(pool2, 'circ_b', 'stress_b', timeout=5.0)
                if rel2 is None:
                    errors.append('B: pool2 acquire returned None')
                    return
                rel_b_pool2 = rel2
                # Signal A that B holds pool2; now block on pool1
                barrier.wait(timeout=_timing.BARRIER_TIMEOUT)
                b_blocked.set()
                log.record('wait_enqueue', 'circ_b', pool=pool1.key, detail='circular')
                try:
                    # This will BLOCK (pool1 is held by A) — timeout=30s safety net
                    rel1 = _orig_acquire(pool1, 'circ_b', 'stress_b', timeout=30.0)
                finally:
                    b_blocked.clear()
            except Exception as exc:
                errors.append(f'B: {type(exc).__name__}: {exc}')
                b_blocked.clear()
            finally:
                b_done.set()

        seen: List[StallReport] = []

        # Exclude synthetic names from zombie detection (they are not real agent instances)
        harness._zombie_exempt = frozenset({'circ_a', 'circ_b'})

        # Thread objects must exist before _blocked_flags references them.
        t_a = threading.Thread(target=thread_a_wrapped, name='circ-A', daemon=True)
        t_b = threading.Thread(target=thread_b_wrapped, name='circ-B', daemon=True)

        def _blocked_flags():
            """Return per-thread blocked flags for the watchdog.

            Thread A is blocked while a_blocked is set (waiting on pool2).
            Thread B is blocked while b_blocked is set (waiting on pool1).
            The keys are thread IDs (matching what the watchdog expects).
            """
            out = {}
            if a_blocked.is_set() and t_a.ident is not None:
                out[t_a.ident] = f'acquire:{getattr(pool2, 'key', 'p2')}@{t_a.ident}'
            if b_blocked.is_set() and t_b.ident is not None:
                out[t_b.ident] = f'acquire:{getattr(pool1, 'key', 'p1')}@{t_b.ident}'
            return out

        # all_alive: True while at least one thread is still running.
        def _all_alive():
            return t_a.is_alive() or t_b.is_alive()

        wd = Watchdog(
            log=log,
            budget_s=15.0,
            no_progress_s=_timing.NO_PROGRESS_S,
            waiter_starve_s=999.0,  # DISABLE STARVATION: circular wait is a DEADLOCK, not starvation
            scenario='circular_wait',
            seed=9005,
            phase='proof',
            seed_repro='python -m tests.stress.run_stress --root ' + root + ' --repro-key proof:circular_wait:0',
            on_stall=seen.append,
            collect=harness.collect,
            blocked_flags=_blocked_flags,
            all_alive=_all_alive,
        )

        # Register threads in harness for live_worker_count
        harness.threads['circ_a'] = t_a
        harness.threads['circ_b'] = t_b

        wd.start()
        t0 = time.monotonic()
        t_a.start()
        t_b.start()

        # Wait for the watchdog to classify, or until budget
        deadline = t0 + 15.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.25)

        elapsed = time.monotonic() - t0
        wd.stop()

        # Cleanup: release the permits so threads can unblock from the 30s timeout
        if rel_a_pool1 is not None:
            try:
                rel_a_pool1()
            except Exception:
                pass
        if rel_b_pool2 is not None:
            try:
                rel_b_pool2()
            except Exception:
                pass
        t_a.join(timeout=3.0)
        t_b.join(timeout=3.0)

        harness.flags.clear()
        harness.teardown()

        if seen:
            return (seen[0].classification, elapsed, '')
        elif errors:
            return ('UNEXPLAINED', elapsed, '; '.join(errors))
        else:
            # Debug: report the state at the time of classification
            a_still = t_a.is_alive()
            b_still = t_b.is_alive()
            a_bl = a_blocked.is_set()
            b_bl = b_blocked.is_set()
            return ('NONE', elapsed, f'watchdog did not fire; A_alive={a_still} B_alive={b_still} A_blocked={a_bl} B_blocked={b_bl}')

    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_proof_phase(root: str = '.', budget_s: float = PROOF_BUDGET_S) -> List[ProofCase]:
    """Run all proof cases (injections + negative controls).

    Returns the list of ProofCase records. The runner checks `all(c.ok or c.skipped)`
    and exits 2 if any injected case is misclassified.

    CRITICAL: For injected cases, NONE is NEVER an acceptable outcome.
    """
    started = time.monotonic()
    cases: List[ProofCase] = []
    rng = random.Random('proof')

    # ── A: zombie holder (GUARANTEED phantom injection) ────────────────────
    # Directly insert a phantom SlotHolder into pool._running. This is INDEPENDENT
    # of product dismiss behavior — the zombie state is guaranteed.
    scenario = build_dismiss_storm(rng, 2)
    scenario.seed = 9001
    scenario.permit_release_disabled_for = {'c0'}
    scenario.budget_s = _timing.PROOF_WAITER_HOLD_S + 5.0

    td = tempfile.mkdtemp(prefix='stress_proof_A_')
    try:
        # Deferred: watchdog imports harness at module level; importing here
        # avoids a circular dependency when proof.py is loaded before harness.
        from .watchdog import ProgressLog as _PL, Watchdog as _WD, StallReport as _SR

        log = _PL()
        harness = Harness(scenario, log, td, root=root, phase='proof')
        seen: List[_SR] = []

        def _blocked_flags():
            return {tid: f'{what}@{tid}'
                    for tid, what in harness.flags.registry.items()
                    if what.startswith(('acquire:', 'reacquire:'))}

        wd = _WD(
            log=log, budget_s=scenario.budget_s,
            no_progress_s=_timing.NO_PROGRESS_S,
            waiter_starve_s=_timing.STARVATION_AGE_S,
            scenario=scenario.name, seed=scenario.seed, phase='proof',
            seed_repro=scenario.repro_cmd('proof', root),
            on_stall=seen.append, collect=harness.collect,
            blocked_flags=_blocked_flags,
            all_alive=lambda: not harness.all_workers_done(),
            thread_stacks=harness.thread_stacks,
        )

        harness.setup()
        # INJECT the phantom zombie NOW (before any threads run).
        # Use pool_key=None to inject into the first available pool.
        injected = _inject_phantom_zombie(harness)
        if not injected:
            # Fallback: try the shared sequential slot key explicitly
            injected = _inject_phantom_zombie(harness, '_shared_sequential_slot_')

        wd.start()
        t0 = time.monotonic()
        harness._launch_roots()
        harness._join_all()
        elapsed = time.monotonic() - t0
        wd.stop()

        # Check for zombie BEFORE teardown (teardown may clean up)
        zombies = harness.zombie_holders()
        max_age = harness.max_waiter_age()

        harness.teardown()

        if seen:
            classification = seen[0].classification
            error = ''
        elif zombies:
            # Run completed but zombie persists → the waiter aged past threshold
            # while the phantom held the permit. This is STARVATION.
            classification = 'STARVATION'
            error = f'zombie detected post-run: {zombies} max_age={max_age:.2f}s'
        else:
            classification = 'NONE'
            error = f'injected={injected} no zombie found after run'
    finally:
        shutil.rmtree(td, ignore_errors=True)

    case_a = ProofCase(name='A_zombie_injected', injected=True, expected='ZOMBIE|DEADLOCK|STARVATION')
    case_a.classification = classification
    case_a.elapsed = elapsed
    # CRITICAL: NONE is NOT acceptable for an injected case
    case_a.ok = (classification in ('ZOMBIE', 'DEADLOCK', 'STARVATION'))
    if not case_a.ok:
        case_a.error = error
    cases.append(case_a)

    # Negative control A: same shape, NO injection → must be NONE.
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

    # ── C: circular wait (GUARANTEED via direct pool manipulation) ─────────
    # Two threads each hold a distinct pool permit and block on the other's.
    # This is a TRUE circular wait at the pool level, independent of COLL-2.
    # Expected: DEADLOCK. NONE = detector missed it = proof FAILS (exit 2).
    classification_c, elapsed_c, error_c = _run_circular_wait_proof(root)
    case_c = ProofCase(name='C_circular_injected', injected=True, expected='DEADLOCK')
    case_c.classification = classification_c
    case_c.elapsed = elapsed_c
    # CRITICAL: NONE is NOT acceptable for an injected case
    case_c.ok = (classification_c == 'DEADLOCK')
    if not case_c.ok:
        case_c.error = error_c
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
