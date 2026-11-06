"""BUG_0244 — bounded teardown + halt-observable dispatch guards (plan §7).

The BUG_0244 incident wedged the ROOT orchestrator inside a sync tool dispatch on a
non-halt-aware primitive for 7m14s. The dig (plan §1–§5) proved:

  * teardown is cooperative and does NO thread join (I1) — a regression-guarded invariant;
  * the LLM HTTP path is client-side bounded, but the 300s read timeout sat exactly AT the
    300s holder-DMS window (zero margin) — fixed in §6.2 (read → 120s);
  * the root orchestrator is invisible to BOTH DMS nets by construction (coverage gap, §5).

These tests encode the three invariants as executable facts. Each guard is proven
discriminating: §7c flips RED at the old 300s read timeout, §7d ships a mutation self-test
that proves its AST walker can actually go red, and §7f pins the scope discriminators so a
future root-watchdog change is FORCED to update them (see the xfail → BUG_0245).
"""

import ast
import inspect
import os
import time

from agent_cascade import settings as ac_settings
from agent_cascade.pool.idle_manager import IdleManager
from agent_cascade.slot_queue import SlotPool
from agent_cascade.tools.code_interpreter import CODE_EXECUTION_TIMEOUT

# Reuse the holder-DMS seams from the existing suite. Underscore-prefixed helpers are not
# collected by pytest, so importing them from a sibling test module is safe and matches the
# established `from tests.<mod> import ...` convention used across this tree.
from tests.test_holder_dms import _FakeInst, _MockPool, _build, _old, WINDOW


# ──────────────────────────────────────────────────────────────────────────────────
# §6.1 / §7d — Invariant I1: teardown is cooperative; NO unbounded thread join.
#
# The child's thread is never joined on the terminate path (plan §1.3). A bare join would
# re-open the BUG_0244 failure class: a halt cannot interrupt a thread that is itself blocked
# joining a wedged one. The one legitimate join in the teardown path is already bounded
# (pool/lifecycle.py: `thread.join(timeout=join_timeout)`). Forbid any new unbounded join.
# ──────────────────────────────────────────────────────────────────────────────────

_PKG_DIR = os.path.dirname(os.path.abspath(ac_settings.__file__))
# Paths (relative to the agent_cascade package dir) whose teardown/termination path must stay
# join-free. New teardown code added elsewhere is out of this guard's declared scope on purpose.
_TEARDOWN_FILES = ('child_runner.py', 'pool/lifecycle.py', 'tool_dispatcher.py')


def _iter_join_calls(tree):
    """Yield every `.join(...)` attribute-call node in an AST.

    Both thread joins (`t.join()`) and string joins (`'\\n'.join(parts)`) parse to an
    Attribute call with attr='join', but str.join REQUIRES a single positional iterable, so
    `join()` with NO positional arg and NO kwargs is necessarily a bare *thread* join — that is
    exactly the shape we forbid. See _is_bounded_join.
    """
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'join'):
            yield node


def _is_bounded_join(node) -> bool:
    """A join is bounded if a timeout is supplied as `timeout=`, as a positional arg (join's
    first positional IS the timeout), or smuggled through dynamic `**kwargs`. Fail-open toward
    'bounded' for dynamic kwargs (the house never calls `thread.join(**opts)`) so the guard only
    flags the provably-unbounded bare `.join()`."""
    if node.args:
        return True
    for kw in node.keywords:
        if kw.arg == 'timeout' or kw.arg is None:  # timeout=... or **dynamic
            return True
    return False


class TestNoUnboundedJoin:
    def test_no_bare_thread_join_in_teardown_path(self):
        """No bare `.join()` (no timeout= / no positional arg) may exist in the teardown path.
        Fails if a future change introduces an unbounded join — which re-opens BUG_0244."""
        violations = []
        for rel in _TEARDOWN_FILES:
            path = os.path.join(_PKG_DIR, rel)
            assert os.path.isfile(path), f"guard target missing (renamed?): {path}"
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), filename=path)
            for node in _iter_join_calls(tree):
                if not _is_bounded_join(node):
                    violations.append(f"{rel}:{node.lineno}")
        assert not violations, (
            f"bare `.join()` (no timeout= / no positional arg) in teardown path: {violations}. "
            'If a join is genuinely needed, use a daemon thread + `join(timeout='
            'settings.dismiss_thread_join_timeout)`; an unbounded join re-opens BUG_0244.')

    def test_guard_actually_detects_a_bare_join(self):
        """Mutation-proof (non-vacuity, per the guard-test lesson): the SAME walker used above
        MUST flag a bare `.join()` and accept bounded ones — otherwise the guard is dead code.
        Done with an in-memory snippet so no production source is mutated."""
        tree = ast.parse('t.join()\nt.join(timeout=1.0)\nu.join(2.0)\nt.join(**opts)\n')
        bad = [n.lineno for n in _iter_join_calls(tree) if not _is_bounded_join(n)]
        assert bad == [1], f"walker failed to isolate the bare join (expected [1], got {bad})"


# ──────────────────────────────────────────────────────────────────────────────────
# §6.2 / §7c — Invariant I2 margin: a non-streaming idle HTTP read must sit WELL UNDER the
# 300s holder-DMS window. This is the load-bearing RED/GREEN test.
# ──────────────────────────────────────────────────────────────────────────────────


class TestToolBoundMargin:
    def test_http_read_timeout_has_margin_under_holder_dms_window(self):
        """RED/GREEN for §6.2.

        A non-streaming idle HTTP read is silent on BOTH holder-DMS timestamps, so it is one of
        the bounds the 300s window is sized around. At the old 300s default it sat EXACTLY AT the
        window (zero margin) — a wedged read parked a conc=0 holder right at the kill edge.
        Pre-fix this is RED (300.0 !< 300.0); after §6.2 (120.0) it is GREEN (120.0 < 300.0 → 2.5x)."""
        assert ac_settings.HTTP_READ_TIMEOUT < ac_settings.HOLDER_DMS_WINDOW_S, (
            f"HTTP_READ_TIMEOUT={ac_settings.HTTP_READ_TIMEOUT} is not strictly under "
            f"HOLDER_DMS_WINDOW_S={ac_settings.HOLDER_DMS_WINDOW_S}; a non-streaming idle read this "
            'long holds a conc=0 holder silent at the DMS kill edge with zero margin (BUG_0244 §6.2).')

    def test_http_read_timeout_does_not_exceed_house_longest_tool_bound(self):
        """§6.2 rationale: the 300s DMS margin assumes no bound exceeds the house longest tool
        bound (CODE_EXECUTION_TIMEOUT=120s, dead-mans-switch memory). The read backstop honours it."""
        assert ac_settings.HTTP_READ_TIMEOUT <= CODE_EXECUTION_TIMEOUT, (
            f"HTTP_READ_TIMEOUT={ac_settings.HTTP_READ_TIMEOUT} exceeds the longest house tool bound "
            f"CODE_EXECUTION_TIMEOUT={CODE_EXECUTION_TIMEOUT} the 300s DMS margin is sized around.")

    def test_grep_like_unbounded_sync_tool_past_cap_is_terminated(self):
        """§7c mechanical half (I3 for tools that declare NO finite dms_bound): a plain sync tool
        ('grep'-like) falls back to the settings cap; once past that carried bound with no
        turn-progress the holder IS terminated — an un-declared tool is never an escape hatch.
        (The dispatch carry side already proves the cap fallback in
        TestDispatchBoundary.test_unbounded_tool_uses_settings_cap.)"""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        # 3-tuple marker exactly as the dispatch boundary writes it for an un-bounded tool: cap=900.
        inst._in_blocking_tool = ('grep', time.monotonic() - 1000.0, 900.0)  # age 1000 > cap 900
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == ['H'], f"past-cap un-bounded sync tool not terminated: {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────────
# §6.4 / §5 / acceptance #6 — ROOT/orchestrator DMS blind spot (the BUG_0244 victim), CLOSED.
#
# Phase 1 (this file, BUG_0244) recorded the gap: the root orchestrator is never in pool._running
# and is not a slot waiter, so the waiter- and holder-side DMS nets could not see it. Phase 2
# (BUG_0245) extended the holder-DMS scan with a root pass (idle_manager._is_wedged_root) over
# pool.instances, reusing the existing _in_blocking_tool marker / _marker_shield_state / join-free
# terminate_instance machinery — no new timer, no new per-instance state, no new settings constant.
# The former xfail(strict) now passes as a regression test; the scope-discriminator pin below stays
# green because the root pass additionally requires a PAST-bound marker (guard #7), so the documented
# holder scope (capacity==1 / holder-registry-only) is unchanged.
# Closed by: .bug_tracker/BUG_0245_root_orchestrator_invisible_to_all_dms_*.md
# ──────────────────────────────────────────────────────────────────────────────────


class TestRootWatchdogGap:
    def test_wedged_root_orchestrator_is_rescued_by_a_dms(self):
        """GREEN since BUG_0245 phase-2 — the former xfail(strict) now asserts the rescue holds.

        The BUG_0244 victim (Maine, the root orchestrator) wedged in sync tool dispatch for 7m14s
        and NO safety net observed it: not a slot waiter, not in pool._running. We model that exact
        position — aged turn-progress, past the cap in an un-bounded sync tool, with the root NEVER
        granted a slot (it owns instances; it is not a scheduler slot holder), so the HOLDER pass
        no-ops for it and the ROOT pass in _run_holder_dms collects it via _marker_shield_state ==
        'past' and abandons it through the same join-free terminate_instance the holder pass uses.

        Gate note: the fail-safe early returns (window<=0 disable, pause guard, unresolvable-approval)
        sit above the pass split and gate the ROOT pass too — the root pass is not unconditionally
        always-on (plan §4.1)."""
        inst = _FakeInst('root-orchestrator', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('grep', time.monotonic() - 1000.0, 900.0)
        # No slot granted → pool._running is empty → the holder pass cannot see the root; exactly
        # mirroring the root's real out-of-registry position. Rescue happens via the ROOT pass.
        slot_pool = SlotPool(key='root', capacity=1)
        pool = _MockPool({'root-orchestrator': inst}, {'root': slot_pool}, window=WINDOW)
        im = IdleManager(pool)
        im._run_holder_dms()
        assert 'root-orchestrator' in pool.terminated, (
            'BUG_0245: a wedged root orchestrator must be rescued by the root pass in _run_holder_dms.')

    def test_holder_dms_scope_discriminators_are_explicit(self):
        """Pin (GREEN today; BREAKS if anyone broadens the holder-DMS scope).

        Encodes the §5 scope map as an executable fact so a future root-watchdog change is FORCED
        to update it instead of silently altering what the DMS can see. Asserts:
          (a) capacity>1 pools are out of scope (holders[0] would be arbitrary) — functional, and
          (b) the documented root-invisible invariant is still written verbatim in the predicate."""
        # (a) functional: aged holder on a capacity>1 pool is skipped (out of scope).
        inst = _FakeInst('H', last_activity=_old(), llm_active=False)
        wide = SlotPool(key='wide2', capacity=2)
        wide.acquire(instance_name='H', agent_class='t')
        pool = _MockPool({'H': inst}, {'wide2': wide}, window=WINDOW)
        IdleManager(pool)._run_holder_dms()
        assert pool.terminated == [], 'capacity>1 pool must remain out of holder-DMS scope'

        # (b) the root-invisible invariant is still present verbatim in the predicate source.
        src = inspect.getsource(IdleManager._evaluate_wedged_holder)
        assert 'the holder registry never yields one' in src, (
            "BUG_0245: the root/orchestrator 'holder registry never yields one' invariant was removed "
            'or renamed — if a root watchdog was added, update this pin and BUG_0245 together.')
        src_run = inspect.getsource(IdleManager._run_holder_dms)
        assert 'capacity != 1' in src_run, (
            'BUG_0245: the capacity!=1 scope-skip was removed; reconcile against the holder-DMS scope contract.')
        # NOTE (BUG_0245): _run_holder_dms now ALSO scans pool.instances for wedged ROOTS
        # (IdleManager._is_wedged_root). That pass is marker-gated (requires a PAST-bound
        # _in_blocking_tool marker) and root-gated (parent_instance is None), so a capacity>1 aged
        # holder WITHOUT a marker stays out of scope — the `terminated == []` assertion above holds.

    def test_root_within_declared_bound_is_not_disturbed(self):
        """Per-bound discriminator (BUG_0245 §6.2): a root whose in-flight tool declares a long
        bound (carried bound = MAX_SHELL_TIMEOUT=3600 ≫ the 2s test window) is SHIELDED, never
        preempted — even with aged turn-progress (last_activity). This is the ticket's
        'shell_cmd timeout=900 must not be disturbed': the root pass honours the declared bound."""
        inst = _FakeInst('root-orchestrator', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('shell_cmd', time.monotonic(), 3600.0)  # fresh marker, bound ≫ age
        pool = _MockPool({'root-orchestrator': inst}, {}, window=WINDOW)  # no slot → not a holder
        IdleManager(pool)._run_holder_dms()
        assert pool.terminated == [], f"root within declared bound wrongly preempted: {pool.terminated}"

    def test_wedged_root_is_not_re_terminated_after_the_marker_leaks(self):
        """Idempotency (BUG_0245 §6.5): the wedged thread's finally never runs, so its marker LEAKS
        after abandon. Guard #2 (state != RUNNING) must stop a later tick re-terminating it — no
        terminate/re-terminate loop on the abandoned, never-joined parked thread. _MockPool.terminate_instance
        transitions state to TERMINATED, mirroring lifecycle.terminate_instance."""
        inst = _FakeInst('root-orchestrator', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('grep', time.monotonic() - 1000.0, 900.0)  # deliberately left leaked
        pool = _MockPool({'root-orchestrator': inst}, {}, window=WINDOW)
        im = IdleManager(pool)
        im._run_holder_dms()
        assert pool.terminated == ['root-orchestrator'], f"first pass: {pool.terminated}"
        # Marker NOT cleared (the wedged thread's finally never ran); state is now TERMINATED.
        im._run_holder_dms()
        assert pool.terminated == ['root-orchestrator'], (
            f"leaked marker re-terminated an already-TERMINATED root: {pool.terminated}")

    def test_wedged_root_is_rescued_when_scheduler_is_absent(self):
        """BUG_0245 §4.1: the root pass must NOT inherit the old `scheduler is None` early return —
        a wedged root owns no scheduler slot, so it is rescued even with no pool registry present."""
        inst = _FakeInst('root-orchestrator', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('grep', time.monotonic() - 1000.0, 900.0)
        pool = _MockPool({'root-orchestrator': inst}, {}, window=WINDOW)
        pool.api_router = None  # no router → scheduler None → holder pass skipped; root pass still runs
        IdleManager(pool)._run_holder_dms()
        assert 'root-orchestrator' in pool.terminated, (
            f"root not rescued with no scheduler: {pool.terminated}")
