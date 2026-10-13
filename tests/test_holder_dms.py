"""Holder-side dead-man's-switch tests (plan t220_holder_dms_PLAN §6).

The DMS terminates a conc=0 slot holder that has made NO turn-progress
(inst.last_activity) for >= the window while NOT in a legitimately-bounded
operation. These tests are revert-proof: each asserts the corrected behaviour and
would FAIL if the corresponding production guard were removed.

All timing tests use a SMALL window (2s) so the suite stays fast; the production
window is 300s. The holder is granted on the main thread (fast path, capacity
available); the DMS pass is invoked directly (no background thread) so the test
is deterministic.

Revert-proof mapping (plan §6):
  1  Hang-2 retry loop  -> terminated        (FAILS if keyed on _last_llm_activity)
  1b TTFB in-flight LLM -> NOT terminated    (FAILS if llm_active exclusion removed)
  1c bounded shell_cmd  -> NOT terminated    (FAILS if marker exclusion removed)
  1d shell_cmd past bound -> terminated      (FAILS if bound check removed)
  2  healthy slow stream -> NOT terminated   (FAILS if age check removed)
  3  approval-gated      -> NOT terminated   (FAILS if approval exclusion removed)
  4  zero waiters        -> terminated       (closes the symmetric gap)
  5  unresolvable name   -> NOT terminated   (fail-safe: skip all)
  6  operation_manager None -> terminated    (exclusion inactive, never silent)
  7  slot_queue key      -> keyed on last_activity, isolated from _last_llm_activity
"""

import time
import unittest

from agent_cascade.slot_queue import SlotPool, _holder_activity_context
from agent_cascade.pool.idle_manager import IdleManager

WINDOW = 2.0  # test window; production is 300s


# ──────────────────────────────────────────────────────────────────────────────
# Test doubles
# ──────────────────────────────────────────────────────────────────────────────

class _State:
    def __init__(self, name):
        self.name = name


class _FakeInst:
    """Minimal stand-in for an AgentInstance: only the attributes the DMS reads."""

    def __init__(self, name, agent_class='coder', last_activity=0.0,
                 llm_activity=0.0, llm_active=False, state_name='RUNNING'):
        self.instance_name = name
        self.agent_class = agent_class
        self.last_activity = last_activity
        self._last_llm_activity = llm_activity
        self._llm_call_active = llm_active
        self.state = _State(state_name)
        self._in_blocking_tool = None


class _FakeOM:
    def __init__(self, approvals):
        self._approvals = approvals

    def list_pending_approvals(self):
        return self._approvals


class _FakeScheduler:
    def __init__(self, pools):
        self._pools = pools


class _FakeApiRouter:
    def __init__(self, scheduler):
        self.scheduler = scheduler


class _FakeSettings:
    def __init__(self, window):
        self.holder_dms_window_s = window


class _MockPool:
    """Stand-in for AgentPool exposing exactly the attributes _run_holder_dms touches."""

    def __init__(self, instances, pools, window=WINDOW, approvals=None,
                 halted=None, om_present=True):
        self.instances = instances
        self.settings = _FakeSettings(window)
        self.api_router = _FakeApiRouter(_FakeScheduler(pools))
        self.operation_manager = _FakeOM(approvals or []) if om_present else None
        self._halted = set(halted or [])
        self.terminated = []

    def get_instance(self, name):
        return self.instances.get(name)

    def is_instance_halted(self, name):
        return name in self._halted

    def terminate_instance(self, name):
        self.terminated.append(name)


def _build(inst, name='H', window=WINDOW, approvals=None, halted=None, om_present=True):
    """Grant a holder on a real conc=1 SlotPool and wire a mock AgentPool to it."""
    slot_pool = SlotPool(key='dms', capacity=1)
    release = slot_pool.acquire(instance_name=name, agent_class='t')  # fast path grant
    pool = _MockPool({name: inst}, {'dms': slot_pool}, window=window,
                     approvals=approvals, halted=halted, om_present=om_present)
    im = IdleManager(pool)
    return im, pool, slot_pool, release


def _old():
    return time.monotonic() - 10.0  # well past the 2s test window


def _recent():
    return time.monotonic()


# ──────────────────────────────────────────────────────────────────────────────
# 1 — Hang-2: LLM retry loop (advances _last_llm_activity, NOT last_activity)
# ──────────────────────────────────────────────────────────────────────────────

class TestRetryLoopWedge:
    def test_retry_loop_holder_is_terminated(self):
        """THE load-bearing test. A holder stuck between LLM attempts advances
        _last_llm_activity on every attempt but never last_activity. Keying the DMS
        on last_activity terminates it; keying on _last_llm_activity (the existing
        last_activity_age_s) would make the DMS a NO-OP here. Reverts to a no-op if
        the signal source is wrong."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_recent(), llm_active=False)
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == ['H'], f"expected termination, got {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────
# 1b — TTFB: single in-flight LLM call (no chunks) must NOT be preempted
# ──────────────────────────────────────────────────────────────────────────────

class TestTTFBExclusion:
    def test_in_flight_llm_call_not_terminated(self):
        """A holder mid an LLM call (no chunks yet, flat last_activity) is owned by
        the stream watchdog, not the DMS. Fails if the llm_active exclusion is removed."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_recent(), llm_active=True)
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == [], f"TTFB holder wrongly terminated: {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────
# 1c / 1d — Decision D: bounded blocking tool marker
# ──────────────────────────────────────────────────────────────────────────────

class TestBoundedBlockingTool:
    def test_approved_shell_cmd_within_bound_not_terminated(self):
        """Approval already granted & popped; holder mid a sync shell_cmd with a fresh
        marker (age < MAX_SHELL_TIMEOUT=3600). Fails if the marker exclusion is removed."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('shell_cmd', time.monotonic() - 10.0)
        im, pool, _, _ = _build(inst, approvals=[])  # approval already popped -> none pending
        im._run_holder_dms()
        assert pool.terminated == [], f"bounded shell_cmd wrongly terminated: {pool.terminated}"

    def test_shell_cmd_past_bound_is_terminated(self):
        """A hung shell_cmd whose marker age exceeds MAX_SHELL_TIMEOUT is a genuine
        wedge. Fails if the bound comparison is removed (marker would shield forever)."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('shell_cmd', time.monotonic() - 4000.0)  # > 3600s bound
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == ['H'], f"past-bound shell_cmd not terminated: {pool.terminated}"

    def test_media_gen_marker_not_terminated(self):
        """Long media-gen renders are excluded like approval-gated (plan §3.4). The marker
        is now a 3-tuple carrying the resolved (config-driven) bound, which the DMS prefers
        over the registry fallback."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('media_gen', time.monotonic() - 10.0, 900.0)  # 3-tuple: carried bound
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == [], f"media_gen holder wrongly terminated: {pool.terminated}"

    def test_media_gen_carried_bound_past_is_terminated(self):
        """The CARRIED bound (not the registry) is authoritative. A media_gen marker whose
        age exceeds its carried bound (20s) but is still < the 900s registry fallback is a
        genuine wedge. Fails if the predicate ignores the carried bound and uses the
        registry default instead (which would shield it forever)."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('media_gen', time.monotonic() - 30.0, 20.0)  # age 30s > carried 20s
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == ['H'], f"past carried-bound media_gen not terminated: {pool.terminated}"

    def test_media_gen_two_tuple_falls_back_to_registry(self):
        """A legacy 2-tuple marker (no carried bound) falls back to the registry bound
        (900s), so a fresh media_gen marker is still excluded. Guards the 2-tuple compat
        path that shell_cmd's pre-TASK-2 markers relied on."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('media_gen', time.monotonic() - 10.0)  # 2-tuple
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == [], f"2-tuple media_gen marker not excluded: {pool.terminated}"

    def test_unbounded_tool_marker_does_not_shield(self):
        """A marker for a tool with NO registered bound (real bound < window) must NOT
        shield a genuine wedge — otherwise a stale marker is a permanent escape hatch."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('code_interpreter', time.monotonic() - 10.0)
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == ['H'], f"unbounded marker wrongly shielded: {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────
# 2 — Healthy holder keeps advancing last_activity -> never preempted
# ──────────────────────────────────────────────────────────────────────────────

class TestHealthyHolder:
    def test_recent_turn_progress_not_terminated(self):
        """A legitimately slow but progressing holder (recent last_activity) is not a
        wedge. Fails if the age check is removed."""
        inst = _FakeInst('H', last_activity=_recent(), llm_activity=_recent(), llm_active=False)
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == [], f"healthy holder wrongly terminated: {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────
# 3 — Approval-gated holder within the window -> not preempted
# ──────────────────────────────────────────────────────────────────────────────

class TestApprovalGated:
    def test_pending_approval_holder_not_terminated(self):
        """A holder awaiting user approval is not a wedge. Fails if the approval
        exclusion is removed."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        im, pool, _, _ = _build(inst, approvals=[{'agent_name': 'H'}])
        im._run_holder_dms()
        assert pool.terminated == [], f"approval-gated holder wrongly terminated: {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────
# 4 — Zero waiters: a wedged holder with NO waiters is still terminated
# ──────────────────────────────────────────────────────────────────────────────

class TestZeroWaiters:
    def test_wedged_holder_terminated_without_waiters(self):
        """The waiter-side DMS only fires when someone is blocked. The holder-side DMS
        runs on the always-on timer regardless of waiters — closing the symmetric gap.
        No waiter is ever started here; the holder is still terminated."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        im, pool, slot_pool, _ = _build(inst)
        assert len(slot_pool._waiters) == 0, 'test precondition: no waiters'
        im._run_holder_dms()
        assert pool.terminated == ['H'], f"zero-waiter wedge not terminated: {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────
# 5 — Unresolvable approval name -> skip ALL terminations this tick (fail-safe)
# ──────────────────────────────────────────────────────────────────────────────

class TestUnresolvableApproval:
    def test_unknown_approval_skips_all(self):
        """If ANY pending approval has an unresolvable agent_name ('unknown'), the DMS
        cannot map it to a holder, so it skips every termination this tick — even for an
        obviously-wedged holder. Fail-safe toward NOT killing."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        im, pool, _, _ = _build(inst, approvals=[{'agent_name': 'unknown'}])
        im._run_holder_dms()
        assert pool.terminated == [], f"unresolvable approval did not skip: {pool.terminated}"

    def test_agent_class_approval_skips_all(self):
        """An approval whose agent_name is an agent_class (the _get_agent_name fallback)
        is likewise unresolvable -> skip all."""
        inst = _FakeInst('H', agent_class='coder', last_activity=_old(), llm_active=False)
        im, pool, _, _ = _build(inst, approvals=[{'agent_name': 'coder'}])
        im._run_holder_dms()
        assert pool.terminated == [], f"agent_class approval did not skip: {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────
# 6 — operation_manager not injected -> exclusion inactive, never silent
# ──────────────────────────────────────────────────────────────────────────────

class TestOperationManagerMissing:
    def test_no_om_still_terminates(self):
        """Guard #1: with no operation_manager the approval exclusion is INACTIVE; the
        DMS still runs (and logs a warning) rather than silently disabling itself."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        im, pool, _, _ = _build(inst, om_present=False)
        im._run_holder_dms()
        assert pool.terminated == ['H'], f"missing-om DMS did not run: {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────
# Extra guards — halted holder, non-RUNNING holder, capacity>1 out of scope
# ──────────────────────────────────────────────────────────────────────────────

class TestOtherExclusions:
    def test_halted_holder_not_terminated(self):
        inst = _FakeInst('H', last_activity=_old(), llm_active=False)
        im, pool, _, _ = _build(inst, halted={'H'})
        im._run_holder_dms()
        assert pool.terminated == [], f"halted holder wrongly terminated: {pool.terminated}"

    def test_non_running_holder_not_terminated(self):
        inst = _FakeInst('H', last_activity=_old(), llm_active=False, state_name='IDLE')
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == [], f"non-RUNNING holder wrongly terminated: {pool.terminated}"

    def test_capacity_gt1_pool_skipped(self):
        """Scope discipline: the holder-side DMS targets conc=0 (capacity==1) only.
        A capacity>1 pool is skipped (holders[0] would be arbitrary)."""
        inst = _FakeInst('H', last_activity=_old(), llm_active=False)
        slot_pool = SlotPool(key='wide', capacity=2)
        slot_pool.acquire(instance_name='H', agent_class='t')
        pool = _MockPool({'H': inst}, {'wide': slot_pool}, window=WINDOW)
        im = IdleManager(pool)
        im._run_holder_dms()
        assert pool.terminated == [], f"capacity>1 pool wrongly preempted: {pool.terminated}"

    def test_window_zero_disables(self):
        inst = _FakeInst('H', last_activity=_old(), llm_active=False)
        im, pool, _, _ = _build(inst, window=0.0)
        im._run_holder_dms()
        assert pool.terminated == [], f"window=0 did not disable DMS: {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────
# 7 — The new slot_queue key is keyed on last_activity, isolated from _last_llm_activity
# ──────────────────────────────────────────────────────────────────────────────

class TestTurnProgressKey:
    def test_key_isolated_from_llm_activity(self):
        """_holder_activity_context must expose turn_progress_age_s keyed on last_activity
        while last_activity_age_s stays keyed on _last_llm_activity. A recent
        _last_llm_activity must NOT reduce turn_progress_age_s."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_recent(), llm_active=False)
        slot_pool = SlotPool(key='k', capacity=1)
        slot_pool.acquire(instance_name='H', agent_class='t')
        now = time.monotonic()
        ctx = _holder_activity_context(slot_pool, now, resolver=lambda n: inst)
        assert ctx['context_available'] is True
        assert ctx['holder_name'] == 'H'
        # New key: keyed on last_activity -> ~10s old.
        assert ctx['turn_progress_age_s'] >= WINDOW, (
            f"turn_progress_age_s not keyed on last_activity: {ctx['turn_progress_age_s']}")
        # Existing key: keyed on _last_llm_activity -> ~0s (recent). Must stay isolated.
        assert ctx['last_activity_age_s'] < WINDOW, (
            f"last_activity_age_s leaked last_activity: {ctx['last_activity_age_s']}")
        # streaming must remain False (llm_active False) — untouched by the new key.
        assert ctx['streaming'] is False

    def test_key_negative_when_never_stamped(self):
        """Never-stamped last_activity -> -1.0 (fail-safe: not aged)."""
        inst = _FakeInst('H', last_activity=0.0, llm_activity=0.0, llm_active=False)
        slot_pool = SlotPool(key='k2', capacity=1)
        slot_pool.acquire(instance_name='H', agent_class='t')
        ctx = _holder_activity_context(slot_pool, time.monotonic(), resolver=lambda n: inst)
        assert ctx['turn_progress_age_s'] == -1.0


if __name__ == '__main__':
    unittest.main()
