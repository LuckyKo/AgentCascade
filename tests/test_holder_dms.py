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
from unittest.mock import MagicMock, patch

from agent_cascade.slot_queue import SlotPool, _holder_activity_context
from agent_cascade.pool.idle_manager import IdleManager
from agent_cascade.llm.schema import Message

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
    def __init__(self, window, cap=900.0):
        self.holder_dms_window_s = window
        self.holder_dms_max_bound_s = cap


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
        """A malformed marker with NO carried bound AND no registry bound (e.g. a legacy
        2-tuple code_interpreter marker) must NOT shield a genuine wedge — otherwise a stale
        marker is a permanent escape hatch. The boundary always writes a 3-tuple, so a 2-tuple
        here represents a malformed/legacy marker the read side must still fail-safe on."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('code_interpreter', time.monotonic() - 10.0)  # 2-tuple, no carried bound
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == ['H'], f"unbounded (no carried bound) marker wrongly shielded: {pool.terminated}"

    def test_code_interpreter_carried_bound_within_bound_not_terminated(self):
        """t292: a code_interpreter marker carrying its real (config-driven) bound within that
        bound is a legitimately-bounded long tool and must NOT be terminated. This is the exact
        case the old allowlist missed — code_interpreter is now covered by the dispatch boundary."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        # 3-tuple: carried bound 150s (e.g. exec 120 + iopub tail 30); age 10s < 150s.
        inst._in_blocking_tool = ('code_interpreter', time.monotonic() - 10.0, 150.0)
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == [], f"code_interpreter within carried bound wrongly terminated: {pool.terminated}"

    def test_code_interpreter_past_carried_bound_is_terminated(self):
        """Symmetric case: a code_interpreter marker whose age exceeds its carried bound is a
        genuine wedge and IS terminated (the carried bound is authoritative, not infinite)."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('code_interpreter', time.monotonic() - 200.0, 150.0)  # age 200 > bound 150
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == ['H'], f"code_interpreter past carried bound not terminated: {pool.terminated}"


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


# ──────────────────────────────────────────────────────────────────────────────
# Real-AgentInstance regression (the gap that let the missing-field bug ship)
# ──────────────────────────────────────────────────────────────────────────────
# Every DMS test above uses _FakeInst, which manually sets self._in_blocking_tool
# in its constructor. That is exactly why the production AttributeError — caused by
# _in_blocking_tool never being declared on the real AgentInstance dataclass — was
# not caught: the fakes always had the attribute, the real instances did not. These
# tests drive a REAL AgentInstance through the same _build/_run_holder_dms path to
# prove the attribute exists and the shell_cmd write/clear pattern works without
# AttributeError. Revert-proof: FAIL if the field is removed from agent_instance.py.

def _make_real_inst(name='H', last_activity=0.0, running=True):
    """A REAL AgentInstance (not a fake) with only the DMS-relevant state set.

    ``running`` defaults True because the DMS predicate only acts on RUNNING holders
    (idle_manager.py:316); a fresh dataclass instance is IDLE and would be skipped.
    """
    from agent_cascade.agent_instance import AgentInstance, AgentState
    now = time.monotonic()
    inst = AgentInstance(
        instance_name=name,
        agent_class='coder',
        conversation=[],
        created_at=now,
        last_activity=last_activity or now,
        latest_marker_index=0,
        state=AgentState.RUNNING if running else AgentState.IDLE,
    )
    # The DMS reads these getattr-guarded; set them to the "stuck holder" baseline.
    inst._last_llm_activity = last_activity or now
    inst._llm_call_active = False
    return inst


class TestRealAgentInstanceMarker:
    """The DMS marker must exist on a real AgentInstance, not just the test fakes."""

    def test_field_declared_with_none_default(self):
        """A fresh real instance carries _in_blocking_tool defaulting to None. Fails if
        the field is removed from the dataclass (the exact production AttributeError)."""
        inst = _make_real_inst()
        assert hasattr(inst, '_in_blocking_tool'), '_in_blocking_tool must be declared on AgentInstance'
        assert inst._in_blocking_tool is None

    def test_shell_write_clear_roundtrip_on_real_instance(self):
        """The exact set/clear pattern from operation_manager/shell.py (lines 641 & 785)
        works on a real instance without AttributeError. Line 785 is the finally-clear
        that crashed in production."""
        inst = _make_real_inst()
        inst._in_blocking_tool = ('shell_cmd', time.monotonic(), float(120.0))  # shell.py:641
        assert inst._in_blocking_tool[0] == 'shell_cmd'
        assert inst._in_blocking_tool[2] == 120.0
        inst._in_blocking_tool = None  # shell.py:785 — the line that raised AttributeError
        assert inst._in_blocking_tool is None

    def test_real_holder_with_live_marker_not_terminated(self):
        """End-to-end through the real DMS pass: a REAL AgentInstance holder with a live
        in-bound marker is excluded from termination. Uses _build (real SlotPool + mock
        AgentPool) exactly like the fake-based tests, but with a real instance."""
        inst = _make_real_inst('H', last_activity=_old())  # far past the window
        inst._in_blocking_tool = ('shell_cmd', time.monotonic(), float(120.0))  # actively bounded
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == [], f"real holder with live marker wrongly terminated: {pool.terminated}"

    def test_real_wedged_holder_is_terminated(self):
        """The symmetric real-instance case: a REAL AgentInstance stuck past the window with
        NO marker is terminated — proving the real instance flows through the DMS predicate
        (not just fakes)."""
        inst = _make_real_inst('H', last_activity=_old())  # far past the window, no marker
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == ['H'], f"real wedged holder not terminated: {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────
# t292 — Dispatch-boundary single-writer marker (ToolDispatcher.execute_tool)
# ──────────────────────────────────────────────────────────────────────────────
# Drive the REAL ToolDispatcher.execute_tool with minimal pool/engine/template
# doubles to prove the boundary is the SINGLE writer: it sets a 3-tuple marker
# carrying the tool's dms_bound() (or the settings cap when the tool declares no
# finite bound), and clears it in finally on EVERY exit path (success or exception).
# Revert-proof: FAIL if the boundary's set/finally is removed.

class _FakeTool:
    def __init__(self, name, bound=None, raise_on_bound=False):
        self.name = name
        self._bound = bound
        self._raise = raise_on_bound

    def dms_bound(self, tool_args, instance):
        if self._raise:
            raise RuntimeError('dms_bound exploded')
        return self._bound


class _FakeTemplate:
    def __init__(self, function_map, on_call=None, raise_call=False):
        self.function_map = function_map
        self._on_call = on_call
        self._raise_call = raise_call

    def _call_tool(self, tool_name, tool_args, **kwargs):
        if self._on_call:
            self._on_call(tool_name, tool_args, kwargs)
        if self._raise_call:
            raise RuntimeError('tool exploded')
        return 'ok'


class _FakeCompressionHandler:
    def __init__(self, on_call=None):
        self._on_call = on_call

    def handle_compress_tool(self, resolved, instance, name):
        if self._on_call:
            self._on_call(instance)
        return 'compressed'


class _FakeEngine:
    def __init__(self, compression_handler=None):
        self.compression_handler = compression_handler or _FakeCompressionHandler()
        self.cached = []

    def _resolve_placeholders(self, tool_args, instance_name, tool_name):
        return tool_args  # pass-through (no placeholders in these tests)

    def _cache_tool_args(self, instance_name, tool_name, resolved):
        self.cached.append((instance_name, tool_name))


class _BoundaryPool:
    def __init__(self, instance, template, cap=900.0):
        self._instance = instance
        self._template = template
        self.settings = _FakeSettings(WINDOW, cap)
        self.mark_activity_calls = []

    def get_instance(self, name):
        return self._instance if name == self._instance.instance_name else None

    def get_template(self, agent_class):
        return self._template

    def _mark_activity(self, instance_name):
        # Mirror the real AgentPool: bump last_activity. The dispatch boundary re-stamps here
        # after a sync tool returns (t293-followup DMS fix), so this must exist for the fake.
        self.mark_activity_calls.append(instance_name)
        inst = self.get_instance(instance_name)
        if inst is not None:
            inst.last_activity = time.monotonic()


def _build_dispatcher(inst, tool_obj, on_call=None, raise_call=False, cap=900.0, compression_handler=None):
    from agent_cascade.tool_dispatcher import ToolDispatcher
    fmap = {tool_obj.name: tool_obj} if tool_obj is not None else {}
    template = _FakeTemplate(fmap, on_call=on_call, raise_call=raise_call)
    pool = _BoundaryPool(inst, template, cap=cap)
    d = ToolDispatcher(pool)
    d.set_engine(_FakeEngine(compression_handler=compression_handler))
    return d, pool, template


class TestDispatchBoundary:
    def test_sync_tool_marker_set_with_dms_bound(self):
        """The boundary sets a 3-tuple marker carrying the tool's dms_bound() BEFORE the call."""
        inst = _make_real_inst('H', last_activity=_recent())
        seen = {}

        def on_call(tool_name, tool_args, kwargs):
            seen['marker'] = inst._in_blocking_tool

        tool = _FakeTool('mytool', bound=500.0)
        d, _, _ = _build_dispatcher(inst, tool, on_call=on_call)
        result = d.execute_tool(inst, 'mytool', {'a': 1}, [])
        assert result == 'ok'
        m = seen.get('marker')
        assert isinstance(m, tuple) and len(m) == 3, f"expected 3-tuple marker, got {m}"
        assert m[0] == 'mytool'
        assert m[2] == 500.0, f"carried bound must be the tool's dms_bound(), got {m[2]}"

    def test_sync_tool_marker_cleared_after_call(self):
        """The boundary clears the marker in finally after a successful call."""
        inst = _make_real_inst('H', last_activity=_recent())
        tool = _FakeTool('mytool', bound=500.0)
        d, _, _ = _build_dispatcher(inst, tool)
        d.execute_tool(inst, 'mytool', {'a': 1}, [])
        assert inst._in_blocking_tool is None, 'marker not cleared after call'

    def test_sync_tool_marker_cleared_on_exception(self):
        """(c) The boundary clears the marker even when the tool raises (finally)."""
        inst = _make_real_inst('H', last_activity=_recent())
        tool = _FakeTool('mytool', bound=500.0)
        d, _, _ = _build_dispatcher(inst, tool, raise_call=True)
        try:
            d.execute_tool(inst, 'mytool', {'a': 1}, [])
        except RuntimeError:
            pass
        assert inst._in_blocking_tool is None, 'marker not cleared after exception'

    def test_unbounded_tool_uses_settings_cap(self):
        """A tool whose dms_bound() returns None carries the settings cap (never infinite)."""
        inst = _make_real_inst('H', last_activity=_recent())
        seen = {}

        def on_call(tool_name, tool_args, kwargs):
            seen['marker'] = inst._in_blocking_tool

        tool = _FakeTool('mystery', bound=None)
        d, _, _ = _build_dispatcher(inst, tool, on_call=on_call, cap=777.0)
        d.execute_tool(inst, 'mystery', {'a': 1}, [])
        assert seen['marker'][2] == 777.0, f"unbounded tool must carry the cap, got {seen['marker'][2]}"

    def test_dms_bound_exception_falls_back_to_cap(self):
        """If dms_bound() raises, the boundary uses the cap and the tool call still runs."""
        inst = _make_real_inst('H', last_activity=_recent())
        seen = {}

        def on_call(tool_name, tool_args, kwargs):
            seen['marker'] = inst._in_blocking_tool

        tool = _FakeTool('flaky', bound=None, raise_on_bound=True)
        d, _, _ = _build_dispatcher(inst, tool, on_call=on_call, cap=650.0)
        result = d.execute_tool(inst, 'flaky', {'a': 1}, [])
        assert result == 'ok'
        assert seen['marker'][2] == 650.0

    def test_compress_context_marker_set_and_cleared(self):
        """(f) The compress_context branch sets a marker with the cap and clears it in finally."""
        inst = _make_real_inst('H', last_activity=_recent())
        seen = {}

        def on_call(instance):
            seen['marker'] = instance._in_blocking_tool

        chandler = _FakeCompressionHandler(on_call=on_call)
        d, _, _ = _build_dispatcher(inst, None, cap=888.0, compression_handler=chandler)
        result = d.execute_tool(inst, 'compress_context', {'a': 1}, [])
        assert result == 'compressed'
        m = seen.get('marker')
        assert isinstance(m, tuple) and m[0] == 'compress_context' and m[2] == 888.0, f"compress marker wrong: {m}"
        assert inst._in_blocking_tool is None, 'compress marker not cleared'


# ──────────────────────────────────────────────────────────────────────────────
# t292 — DMS read side: a generic sync tool carrying a finite bound is excluded
# within its bound and terminated past it (the boundary's marker contract).
# ──────────────────────────────────────────────────────────────────────────────

class TestSyncToolDMS:
    def test_long_sync_tool_within_bound_not_terminated(self):
        """(a) A generic sync tool (no registry bound) carrying a finite bound within that
        bound is a legitimately-bounded long op — NOT terminated. This is the generalized
        case the old 3-tool allowlist could not express."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('some_generic_tool', time.monotonic() - 10.0, 500.0)  # age 10 < 500
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == [], f"generic sync tool within carried bound wrongly terminated: {pool.terminated}"

    def test_sync_tool_past_cap_terminated(self):
        """(b) A marker whose age exceeds its carried bound (the cap) with no progress is a
        genuine wedge — terminated. Proves the carried bound is finite, not an escape hatch."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('some_generic_tool', time.monotonic() - 600.0, 500.0)  # age 600 > 500
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == ['H'], f"past-cap generic sync tool not terminated: {pool.terminated}"

    def test_sleeping_holder_excluded_by_state(self):
        """(d) An async tool parks the holder in SLEEPING; the state guard excludes it
        regardless of any marker. The boundary must never mark a SLEEPING holder."""
        inst = _FakeInst('H', last_activity=_old(), llm_active=False, state_name='SLEEPING')
        inst._in_blocking_tool = ('shell_cmd', time.monotonic() - 4000.0, 3600.0)  # past bound, but SLEEPING
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == [], f"SLEEPING holder wrongly terminated: {pool.terminated}"

    def test_compress_context_within_bound_not_terminated(self):
        """(f) A compress_context marker carrying the cap within that bound is excluded — the
        compress gap (same defect class) is closed."""
        inst = _FakeInst('H', last_activity=_old(), llm_activity=_old(), llm_active=False)
        inst._in_blocking_tool = ('compress_context', time.monotonic() - 10.0, 900.0)  # age 10 < 900
        im, pool, _, _ = _build(inst)
        im._run_holder_dms()
        assert pool.terminated == [], f"compress_context within bound wrongly terminated: {pool.terminated}"


# ──────────────────────────────────────────────────────────────────────────────
# t293-followup — post-return activity re-stamp (the "killed after a long sync op" bug)
#
# Root cause: a conc=0 holder whose last_activity went stale during a LONG synchronous
# operation is terminated by the DMS in the gap AFTER the op returns and BEFORE its next
# LLM activity — because the bounded-tool marker is already cleared and nothing re-stamps
# last_activity. The fix re-stamps last_activity at the three "emerged from a long sync op"
# boundaries:
#   (1) the generic sync-tool dispatch boundary  (tool_dispatcher.execute_tool finally),
#   (2) the compress_context branch              (tool_dispatcher.execute_tool finally), and
#   (3) after a successful slot re-acquire following a sync child / system-agent yield
#       (engine.core.reacquire_after_slot_yield).
# All tests below are revert-proof: each fails if its corresponding re-stamp is removed.
# ──────────────────────────────────────────────────────────────────────────────

class TestPostReturnRestampReacquire:
    """(3) reacquire_after_slot_yield re-stamps last_activity on a SUCCESSFUL re-acquire so a
    healthy caller that just returned from a long sync child is not wedged-dead by the DMS."""

    def _engine_and_instance(self):
        from agent_cascade.agent_instance import AgentInstance
        from agent_cascade.engine.core import ExecutionEngine
        inst = AgentInstance(
            instance_name='P', agent_class='orchestrator',
            conversation=[Message(role='system', content='sys')],
            created_at=time.monotonic(), last_activity=time.monotonic() - 1000.0,  # stale on purpose
            latest_marker_index=0, parent_instance=None,
        )
        pool = MagicMock()
        pool.stopped = False
        pool._run_generation = 1
        # Wire a REAL _mark_activity (bump the instance) so we can assert on the timestamp,
        # not just that the mock was called.
        pool._mark_activity.side_effect = lambda name: setattr(
            inst, 'last_activity', time.monotonic()) if name == inst.instance_name else None
        engine = ExecutionEngine(pool)
        engine._my_generation = 1
        return inst, engine, pool

    def test_successful_reacquire_restamps_activity(self):
        inst, engine, pool = self._engine_and_instance()
        stale_before = inst.last_activity  # ~1000s old
        with patch.object(engine, 'reacquire_for', return_value=True), \
             patch('agent_cascade.engine.core.ExecutionEngine._get_pool_setting', return_value=False):
            ok = engine.reacquire_after_slot_yield(inst, 'P', context='sync child')
        assert ok is True
        # The re-stamp must have advanced last_activity to ~now (well past the stale value).
        pool._mark_activity.assert_called_once_with('P')
        assert inst.last_activity > stale_before + 500.0, (
            f"last_activity not re-stamped after successful re-acquire "
            f"(stale={stale_before:.1f}, now={inst.last_activity:.1f})")

    def test_failed_reacquire_does_not_restamp(self):
        """A caller that FAILS to re-acquire (no router) must NOT be marked active — it is not
        holding a slot, so the DMS/other paths handle it; stamping would be wrong."""
        inst, engine, pool = self._engine_and_instance()
        stale_before = inst.last_activity
        with patch.object(engine, 'reacquire_for', return_value=False):
            ok = engine.reacquire_after_slot_yield(inst, 'P', context='sync child')
        assert ok is False
        pool._mark_activity.assert_not_called()
        assert inst.last_activity == stale_before, (
            'last_activity must NOT change when re-acquire fails')


class TestPostReturnRestampSyncTool:
    """(1) & (2) The generic sync-tool and compress_context dispatch boundaries re-stamp
    last_activity in their finally blocks, so a holder that held the permit silently through a
    long tool / compression is fresh on return. Uses the existing _build_dispatcher helper
    (real ToolDispatcher + _BoundaryPool fake)."""

    def test_sync_tool_return_restamps_activity(self):
        inst = _make_real_inst('H', last_activity=_old())  # stale on purpose (~10s old > 2s window)
        tool = _FakeTool('code_interpreter', bound=900.0)
        d, pool, _ = _build_dispatcher(inst, tool)
        stale_before = inst.last_activity

        result = d.execute_tool(inst, 'code_interpreter', {'code': 'print(1)'}, [])

        assert result == 'ok'
        # Marker cleared in finally.
        assert inst._in_blocking_tool is None
        # Pool re-stamped activity on return (the fix under test) — exactly once, for this holder.
        assert pool.mark_activity_calls == ['H'], (
            f"expected one _mark_activity('H') on sync tool return, got {pool.mark_activity_calls}")
        # And the timestamp actually advanced past the stale value.
        assert inst.last_activity > stale_before + 5.0, (
            f"last_activity not re-stamped after sync tool return "
            f"(stale={stale_before:.1f}, now={inst.last_activity:.1f})")

    def test_sync_tool_exception_still_restamps(self):
        """The re-stamp lives in the finally block, so it fires even when the tool raises — a holder
        that survived a failing long tool is still fresh on return (not wedged-dead)."""
        inst = _make_real_inst('H', last_activity=_old())
        tool = _FakeTool('code_interpreter', bound=900.0)
        d, pool, _ = _build_dispatcher(inst, tool, raise_call=True)
        stale_before = inst.last_activity

        try:
            d.execute_tool(inst, 'code_interpreter', {'code': 'x'}, [])
        except RuntimeError:
            pass  # the fake raises inside _call_tool; execute_tool propagates it

        assert inst._in_blocking_tool is None  # marker cleared even on exception
        assert pool.mark_activity_calls == ['H'], (
            f"expected re-stamp even on tool exception, got {pool.mark_activity_calls}")
        assert inst.last_activity > stale_before + 5.0

    def test_compress_context_return_restamps_activity(self):
        """(2) A long forced compression is silent the whole time (no LLM activity on the holder),
        so after it returns the marker is cleared and last_activity is stale — the same defect class.
        The compress branch must re-stamp in finally. Revert-proof: fails if that line is removed."""
        inst = _make_real_inst('H', last_activity=_old())  # stale on purpose (~10s old > 2s window)
        chandler = _FakeCompressionHandler()
        d, pool, _ = _build_dispatcher(inst, None, cap=888.0, compression_handler=chandler)
        stale_before = inst.last_activity

        result = d.execute_tool(inst, 'compress_context', {'a': 1}, [])

        assert result == 'compressed'
        assert inst._in_blocking_tool is None  # marker cleared in finally
        # Pool re-stamped activity on return (the fix under test) — exactly once, for this holder.
        assert pool.mark_activity_calls == ['H'], (
            f"expected one _mark_activity('H') after compress_context return, got {pool.mark_activity_calls}")
        assert inst.last_activity > stale_before + 5.0, (
            f"last_activity not re-stamped after compress_context return "
            f"(stale={stale_before:.1f}, now={inst.last_activity:.1f})")


if __name__ == '__main__':
    unittest.main()
