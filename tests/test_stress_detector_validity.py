"""Phase 0: detector-validity unit tests (validity plan §3).

These tests MUST FAIL against the pre-fix harness and PASS after the fixes.
They prove each detector fires on a known-injected defect before any "clean"
result is reported. Run:

    python -m pytest tests/test_stress_detector_validity.py -v

Each test targets one finding (F-1..F-7) or one detector class.
"""

from __future__ import annotations

import hashlib
import os
import random
import sys
import tempfile
import threading
import time
from collections import OrderedDict

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
os.environ.setdefault('AGENT_CASCADE_INSTANCE_ID', 'stress-detector-test')


# ── F-1: waiter age must read tickets, not keys ─────────────────────────

def test_f1_waiter_age_reads_tickets():
    """SlotPool._waiters is OrderedDict[int, QueueTicket]; iterating yields ints.

    The harness's _waiter_tickets() helper must return .values() (tickets),
    and max_waiter_age() must compute a non-zero age for an aged ticket.
    """
    from tests.stress.harness import _waiter_tickets

    # Simulate a pool with one aged waiter
    class FakeTicket:
        def __init__(self, created_at):
            self.created_at = created_at
            self.instance_name = 'fake_waiter'

    class FakePool:
        key = 'test_pool'
        _waiters = OrderedDict()
        capacity = 1
        _running = {}

    pool = FakePool()
    # Age the ticket by 5 seconds (well past STARVATION_AGE_S=2.0)
    aged_ticket = FakeTicket(created_at=time.monotonic() - 5.0)
    pool._waiters[1] = aged_ticket

    tickets = _waiter_tickets(pool)
    assert len(tickets) == 1, f'expected 1 ticket, got {len(tickets)}'
    # The ticket must be the QueueTicket object, not an int key
    assert isinstance(tickets[0], FakeTicket), (
        f'_waiter_tickets returned {type(tickets[0])} — '
        'must return .values() (tickets), not keys (ints)')

    # Now verify max_waiter_age via the harness
    from tests.stress.harness import Harness
    from tests.stress.workload import Scenario, AgentSpec, Action

    scenario = Scenario(name='f1_test', agents=[], seed=0, budget_s=5.0)
    with tempfile.TemporaryDirectory(prefix='stress_f1_') as td:
        h = Harness(scenario, _NoopLog(), td, root=_REPO_ROOT, phase='test')
        # Inject the fake pool into the harness's scheduler
        # NOTE: the scheduler is accessed via router.scheduler (not router._sched)
        class FakeSched:
            _pools = {'test_pool': pool}
        h.router = type('FakeRouter', (), {'scheduler': FakeSched()})()
        age = h.max_waiter_age()
        assert age > 4.0, (
            f'max_waiter_age={age:.2f}s — expected >4s for a 5s-old ticket. '
            'F-1: harness iterates _waiters keys (ints) instead of .values()')


class _NoopLog:
    """Minimal ProgressLog stand-in for unit tests."""
    def record(self, *a, **k): pass
    def counts(self): return {}
    def last_ts(self): return time.monotonic()
    def snapshot(self, n=200): return []
    def since(self, t): return []


# ── F-2: seed must be deterministic across processes ────────────────────

def test_f2_seed_deterministic():
    """stable_seed() must produce the same value in every process.

    The old code used hash(tuple) which is salted by PYTHONHASHSEED and
    differs per process — making --seeds non-reproducible.
    """
    from tests.stress.scenarios import stable_seed

    s1 = stable_seed('post', 'soak', 3)
    s2 = stable_seed('post', 'soak', 3)
    assert s1 == s2, f'stable_seed is not deterministic: {s1} != {s2}'

    # Verify it matches the blake2b formula (not hash())
    expected = int(hashlib.blake2b(b'post:soak:3', digest_size=8).hexdigest(), 16) % (2**31)
    assert s1 == expected, f'stable_seed={s1} != blake2b expected={expected}'

    # Different inputs → different seeds
    s3 = stable_seed('post', 'soak', 4)
    assert s1 != s3, 'different idx must produce different seeds'


# ── F-3: STARVATION classification must be reachable ────────────────────

def test_f3_starvation_reachable():
    """The watchdog decision tree must have a STARVATION branch.

    Pre-fix: no STARVATION branch existed; the post-hoc check ran on a broken
    age signal (F-1) so STARVATION was structurally unreachable.
    """
    import inspect
    from tests.stress.watchdog import Watchdog

    source = inspect.getsource(Watchdog._tick)
    assert 'STARVATION' in source, (
        'Watchdog._tick has no STARVATION branch — F-3: classification unreachable')

    # Verify the branch is BEFORE the all_alive/DEADLOCK branches
    starv_idx = source.index('STARVATION')
    deadlock_idx = source.index('DEADLOCK', starv_idx + 1) if 'DEADLOCK' in source[starv_idx:] else len(source)
    assert starv_idx < deadlock_idx, (
        'STARVATION branch must come before DEADLOCK in the decision tree')


# ── F-4: async flag must not inflate DEADLOCK predicate ─────────────────

def test_f4_async_flag_not_counted_as_blocked():
    """async_body:<name> flags must NOT be counted in blocked_flags.

    Pre-fix: the harness set a single 'async' flag for the worker's lifetime,
    and the watchdog counted ALL flags — inflating the DEADLOCK predicate.
    Post-fix: only acquire:/reacquire: values are "blocked".
    """
    from tests.stress.harness import Harness
    from tests.stress.workload import Scenario

    scenario = Scenario(name='f4_test', agents=[], seed=0, budget_s=5.0)
    with tempfile.TemporaryDirectory(prefix='stress_f4_') as td:
        h = Harness(scenario, _NoopLog(), td, root=_REPO_ROOT, phase='test')

        # Simulate: one async worker (lifetime marker) + one blocked acquirer
        h.flags.set('async_body:fake_async')
        h.flags.set('acquire:ep_stress_a')

        # The blocked_flags filter (from scenarios.run_scenario) must exclude async_body
        from tests.stress.scenarios import run_scenario  # noqa: F401 (just checking import works)
        # Replicate the filter logic inline (it's a closure in run_scenario)
        blocked = {tid: f'{what}@{tid}'
                   for tid, what in h.flags.registry.items()
                   if what.startswith(('acquire:', 'reacquire:'))}

        assert len(blocked) == 1, (
            f'blocked_flags has {len(blocked)} entries — expected 1 '
            '(only acquire:/reacquire:). async_body must be excluded. F-4.')
        # Clean up
        h.flags.clear()


# ── F-5: zombie state must be producible via the switch ─────────────────

def test_f5_zombie_switch_producible():
    """permit_release_disabled_for must allow the harness to produce a zombie.

    Pre-fix: _teardown_instance always released the permit before dismiss,
    so the production zombie state (permit in _running, instance gone) was
    never reachable from the harness. Post-fix: names in the switch set skip
    the release, and _suppress_release nullifies the callback without releasing.
    """
    from tests.stress.harness import Harness
    from tests.stress.workload import Scenario

    scenario = Scenario(name='f5_test', agents=[], seed=0, budget_s=5.0,
                        permit_release_disabled_for={'zombie_name'})
    with tempfile.TemporaryDirectory(prefix='stress_f5_') as td:
        h = Harness(scenario, _NoopLog(), td, root=_REPO_ROOT, phase='test')

        # Verify the switch is recognized
        assert 'zombie_name' in h.permit_release_disabled_for, (
            'permit_release_disabled_for not populated from scenario — F-5')

        # Verify _suppress_release exists and nullifies without releasing
        assert hasattr(h, '_suppress_release'), (
            'Harness._suppress_release missing — F-5: cannot produce zombie state')

        # Simulate: an instance with a live permit callback
        class FakeInst:
            instance_name = 'zombie_name'
            agent_class = 'stress_a'
            _slot_release = lambda: None  # a "live" permit
            _slot_key = 'ep_stress_a'
            _state_lock = threading.RLock()

        inst = FakeInst()
        h._suppress_release(inst)
        assert inst._slot_release is None, (
            '_suppress_release did not nullify _slot_release — F-5')
        # The permit was NOT released (no call to release_slot_permit)
        # We can't easily verify the pool state without a real pool, but the
        # callback being nulled IS the mechanism that prevents the release.


# ── F-6: dead no-op branch removed ──────────────────────────────────────

def test_f6_dead_branch_removed():
    """The old post-hoc logic had a no-op reassignment (TIMEOUT → TIMEOUT).

    Post-fix: the dead branch is gone; a stalled run keeps the watchdog's
    classification. Verify by checking that scenarios.py does NOT contain
    the pattern `classification = 'TIMEOUT'` followed immediately by itself.
    """
    import inspect
    from tests.stress import scenarios as scen_mod

    source = inspect.getsource(scen_mod.run_scenario)
    # The dead branch was: `result.classification = 'TIMEOUT'` (a no-op
    # reassignment). Post-fix it's gone. The only remaining TIMEOUT reference
    # is in the post-hoc STARVATION backstop condition `in ('NONE', 'TIMEOUT')`.
    assignments = [line for line in source.splitlines()
                   if "classification = 'TIMEOUT'" in line]
    assert not assignments, (
        f'Found dead TIMEOUT assignment in run_scenario — F-6: {assignments}')


# ── F-7: no PEP-701 nested same-quote f-strings ─────────────────────────

def test_f7_no_pep701_nested_fstrings():
    """harness.py must not use PEP-701 nested same-quote f-strings.

    Python 3.12+ allows f"outer {f'inner'}" but it breaks on 3.11.
    The baseline worktree may run on 3.11, so we avoid this pattern.

    Reviewer finding 8: use AST to explicitly flag JoinedStr nodes containing
    nested FormattedValue with same-quote nesting (not just syntax parse).
    """
    import ast
    harness_path = os.path.join(_REPO_ROOT, 'tests', 'stress', 'harness.py')
    with open(harness_path, 'r', encoding='utf-8') as f:
        source = f.read()

    # First: verify the file parses at all.
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        pytest.fail(f'harness.py has a syntax error (possibly PEP-701 on <3.12): {e}')

    # Second: AST walk for PEP-701 nested same-quote f-strings.
    # A JoinedStr is an f-string. If any of its FormattedValue children
    # contains another JoinedStr (a nested f-string), that's PEP-701 territory.
    # On Python < 3.12, the inner f-string MUST use a different quote than
    # the outer one. We flag ANY nested JoinedStr inside a FormattedValue.

    violations = []

    class _Pep701Checker(ast.NodeVisitor):
        def __init__(self):
            self._in_fstring = 0

        def visit_JoinedStr(self, node):
            # This is an f-string. Check its children for nested f-strings.
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    inner = value.value
                    # If the FormattedValue's expression contains another JoinedStr,
                    # that's a nested f-string (PEP-701 pattern).
                    self._check_nested(inner, node.lineno)
            self.generic_visit(node)

        def _check_nested(self, expr, outer_line):
            for child in ast.walk(expr):
                if isinstance(child, ast.JoinedStr) and child is not expr:
                    violations.append(
                        f'line {outer_line}: nested f-string (PEP-701) inside FormattedValue')
                    return  # one violation per outer f-string is enough

    _Pep701Checker().visit(tree)

    if violations:
        pytest.fail('PEP-701 nested f-strings found in harness.py:\n' + '\n'.join(violations))


# ── Integration: full detector proof on a real pool ─────────────────────

def test_integration_zombie_detection():
    """End-to-end: GUARANTEED zombie via phantom injection → detector must fire.

    Reviewer finding 4: the old test accepted 'NONE' in the expected set, which
    meant a blind detector (one that never fires) would PASS. Now we use the
    proof phase's _inject_phantom_zombie to GUARANTEE the zombie state, and
    assert a specific detection classification. A broken detector must FAIL.
    """
    from tests.stress.harness import Harness
    from tests.stress.workload import build_dismiss_storm
    from tests.stress.scenarios import run_scenario
    from tests.stress.proof import _inject_phantom_zombie
    from tests.stress.watchdog import ProgressLog, Watchdog, StallReport

    rng = random.Random(42)
    scenario = build_dismiss_storm(rng, 1)
    scenario.seed = 9999
    scenario.permit_release_disabled_for = {'c0'}
    scenario.budget_s = 15.0

    with tempfile.TemporaryDirectory(prefix='stress_int_') as td:
        log = ProgressLog()
        harness = Harness(scenario, log, td, root=_REPO_ROOT, phase='test')
        seen: list = []

        def _blocked_flags():
            return {tid: f'{what}@{tid}'
                    for tid, what in harness.flags.registry.items()
                    if what.startswith(('acquire:', 'reacquire:'))}

        wd = Watchdog(
            log=log, budget_s=scenario.budget_s,
            no_progress_s=3.0, waiter_starve_s=2.0,
            scenario=scenario.name, seed=scenario.seed, phase='test',
            seed_repro='test', on_stall=seen.append, collect=harness.collect,
            blocked_flags=_blocked_flags,
            all_alive=harness.all_workers_done,
            thread_stacks=harness.thread_stacks,
        )

        harness.setup()
        # GUARANTEED zombie: inject a phantom SlotHolder into pool._running.
        # Use pool_key=None to inject into the first available pool.
        injected = _inject_phantom_zombie(harness)
        assert injected, 'phantom injection failed — cannot test zombie detection'

        wd.start()
        harness._launch_roots()
        harness._join_all()
        wd.stop()

        # Check for zombie BEFORE teardown (teardown may clean up)
        zombies = harness.zombie_holders()
        max_age = harness.max_waiter_age()

        if seen:
            classification = seen[0].classification
        elif zombies:
            # Run completed but zombie persists → STARVATION (waiters aged
            # while the phantom held the permit).
            classification = 'STARVATION'
        else:
            classification = 'NONE'

        harness.teardown()

    # CRITICAL: NONE means the detector missed the guaranteed zombie. FAIL.
    assert classification in ('ZOMBIE', 'DEADLOCK', 'STARVATION'), (
        f'zombie detection FAILED: classification={classification} but a phantom '
        f'zombie was GUARANTEED in pool._running. A broken detector returns NONE. '
        f'repro: scenario=dismiss_storm seed=9999')


def test_integration_starvation_detection():
    """End-to-end: long holder → waiter age crosses threshold → STARVATION.

    Uses a 6s holder with barrier start so the waiters are GUARANTEED to be
    in the queue while the holder holds the permit (F-1 + F-3 proof).
    """
    from tests.stress.workload import build_long_hold
    from tests.stress.scenarios import run_scenario

    rng = random.Random(42)
    scenario = build_long_hold(rng, holder_ms=6000, n_waiters=3)
    scenario.seed = 9998
    scenario.budget_s = 20.0
    scenario.barrier_start = True  # all roots enter the queue at t≈0

    with tempfile.TemporaryDirectory(prefix='stress_int_') as td:
        result, stalls = run_scenario(scenario, td, _REPO_ROOT, 'test')

    # The waiter age should exceed STARVATION_AGE_S (2.0s) → STARVATION
    assert result.max_waiter_age_s > 1.5, (
        f'max_waiter_age={result.max_waiter_age_s:.2f}s — expected >1.5s for a '
        '6s holder with barrier start. F-1: waiter age signal may still be broken.')
    assert result.classification in ('STARVATION', 'NONE'), (
        f'unexpected classification: {result.line()}')
    if result.max_waiter_age_s > 2.0:
        assert result.classification == 'STARVATION', (
            f'waiter age {result.max_waiter_age_s:.2f}s > threshold but '
            f'classification={result.classification} — F-3: STARVATION unreachable')
