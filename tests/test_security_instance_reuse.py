"""Tests for Security agent instance reuse (BUG_0029 Phase 3).

Covers plan §7.2 cases A–H:
  A. Claim registry (try_claim / release_claim / claim_holder semantics)
  B. Eligibility predicate — table-driven; every failing row returns None, the clean row reuses
  C. The reset itself — conversation[0] object identity, byte-identical content, frozen stays True,
     every §6 field at its reset value, token counters reset
  D. Integration (security_handler) — two sequential checks reuse the same warm instance with a
     preserved prefix; wedged instance falls back to fresh spawn and still completes; claim is
     released in finally even on exception; SECURITY_REUSE_ENABLED=False reproduces today's path
  E. Integration (advisor_runner) — same two-branch behavior for the skill-advisor name
  F. Concurrency — two threads racing the same warm instance: exactly one reuses, no interleaving
  G. Cache-behavior proof — system message byte-identical across a reuse and _state_label cleared
     so _setup_turn takes the [STATE_RESTORE_SKIP] keeping-warm-KV-cache branch

The engine-level tests build a REAL AgentInstance + a minimal fake pool and drive the real
ExecutionEngine._acquire_reusable_system_agent (no LLM). The integration tests patch
ExecutionEngine at its source module, mirroring test_security_handler_deadlock_fixes.py.
"""
import sys
import os
import threading
import time
from unittest.mock import MagicMock, patch

# Ensure the project root is importable regardless of how pytest is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_cascade.security_reuse import (  # noqa: E402
    try_claim, release_claim, claim_holder, _clear_all)
from agent_cascade.agent_instance import AgentInstance, AgentState  # noqa: E402
from agent_cascade.llm.schema import Message, SYSTEM, USER  # noqa: E402
from agent_cascade.engine.helpers import _with_run_identity  # noqa: E402

REUSE_NAME = 'Security_guard'
FALLBACK_INSTANCE_NAME = 'Security_op_fallback1234'  # per-call fresh-spawn name used across tests


# ── Fixtures / helpers ────────────────────────────────────────────────────────


def _make_warm_instance(name=REUSE_NAME):
    """A clean, eligible warm Security instance (IDLE, frozen, [system] conversation)."""
    inst = AgentInstance(
        instance_name=name,
        agent_class='Security',
        conversation=[Message(role=SYSTEM, content='SYS_PROMPT_PREFIX')],
        created_at=time.monotonic(),
        last_activity=time.monotonic() - 60.0,  # well past the idle epsilon
        latest_marker_index=-1,
    )
    inst._system_prompt_frozen = True
    return inst


def _make_pool(inst=None):
    """Minimal fake AgentPool exposing exactly what _acquire_reusable_system_agent touches.

    Pass an instance to pre-seed ``pool.instances`` under its name (the reuse-hit path); pass None
    for a REAL empty dict (the seed-on-miss path — a MagicMock auto-attr .get() would return truthy
    and short-circuit Gate 2)."""
    pool = MagicMock()
    pool.instances = {inst.instance_name: inst} if inst else {}
    pool.message_queues = {}
    exec_obj = MagicMock()
    exec_obj._state_lock = threading.RLock()
    exec_obj.active_stack = []
    pool._execution = exec_obj
    pool.is_instance_halted.return_value = False
    pool.is_instance_terminated.return_value = False
    # NOTE: this fake pool deliberately does NOT expose a `.lifecycle` attribute — a REAL AgentPool
    # has none (the AgentLifecycleManager lives on the ENGINE, not the pool). The reuse path must
    # therefore reach it via engine.lifecycle; stubbing pool.lifecycle here would mask that bug.
    return pool


def _make_engine(pool):
    """A real ExecutionEngine wired to a fake pool, with UI/stream side-effects mocked out."""
    from agent_cascade.execution_engine import ExecutionEngine
    engine = ExecutionEngine(pool)
    # Isolate the reuse path from WebUI/stream plumbing — those are covered elsewhere.
    engine._update_webui_state = MagicMock()
    engine.stream_publisher = MagicMock()
    return engine


def _acquire(engine, task='do a check', caller='Maine', rid='r1'):
    """Convenience: clear the claim registry, then run one acquire."""
    _clear_all()
    return engine._acquire_reusable_system_agent(
        agent_class='Security', instance_name=REUSE_NAME, task=task, caller=caller, rid=rid)


# ── A. Claim registry ────────────────────────────────────────────────────────


class TestClaimRegistry:
    def test_first_claim_succeeds_second_fails(self):
        _clear_all()
        assert try_claim(REUSE_NAME, 'r1') is True
        assert try_claim(REUSE_NAME, 'r2') is False  # already owned
        assert claim_holder(REUSE_NAME) == 'r1'

    def test_release_by_wrong_rid_is_noop(self):
        _clear_all()
        try_claim(REUSE_NAME, 'r1')
        release_claim(REUSE_NAME, 'r2')  # wrong rid → must NOT free it
        assert claim_holder(REUSE_NAME) == 'r1'
        release_claim(REUSE_NAME, 'r1')  # correct rid → frees it
        assert claim_holder(REUSE_NAME) is None

    def test_release_allows_reclaim(self):
        _clear_all()
        try_claim(REUSE_NAME, 'r1')
        release_claim(REUSE_NAME, 'r1')
        assert claim_holder(REUSE_NAME) is None
        assert try_claim(REUSE_NAME, 'r2') is True  # free again after release
        assert claim_holder(REUSE_NAME) == 'r2'


# ── B. Eligibility predicate (table-driven) ──────────────────────────────────


class TestEligibilityPredicate:
    def test_clean_instance_is_reused(self):
        inst = _make_warm_instance()
        engine = _make_engine(_make_pool(inst))
        got = _acquire(engine)
        assert got is not None, 'a clean warm instance must be reusable'
        result_inst, was_reused, name = got
        assert result_inst is inst and was_reused is True
        assert name == REUSE_NAME

    def test_running_state_falls_back(self):
        inst = _make_warm_instance()
        with inst._state_lock:
            inst.state = AgentState.RUNNING
        engine = _make_engine(_make_pool(inst))
        assert _acquire(engine) is None

    def test_terminated_state_falls_back(self):
        inst = _make_warm_instance()
        with inst._state_lock:
            inst.state = AgentState.TERMINATED
        engine = _make_engine(_make_pool(inst))
        assert _acquire(engine) is None

    def test_slot_held_falls_back(self):
        inst = _make_warm_instance()
        inst._slot_release = lambda: None  # leaked permit
        inst._slot_key = 'some_endpoint'
        engine = _make_engine(_make_pool(inst))
        assert _acquire(engine) is None

    def test_llm_active_falls_back(self):
        inst = _make_warm_instance()
        inst._llm_call_active = True
        engine = _make_engine(_make_pool(inst))
        assert _acquire(engine) is None

    def test_recent_llm_activity_falls_back(self):
        inst = _make_warm_instance()
        inst._last_llm_activity = time.monotonic()  # just now → within epsilon
        engine = _make_engine(_make_pool(inst))
        assert _acquire(engine) is None

    def test_halted_falls_back(self):
        inst = _make_warm_instance()
        pool = _make_pool(inst)
        pool.is_instance_halted.return_value = True
        engine = _make_engine(pool)
        assert _acquire(engine) is None

    def test_terminated_flag_falls_back(self):
        inst = _make_warm_instance()
        inst.is_terminated = True
        engine = _make_engine(_make_pool(inst))
        assert _acquire(engine) is None

    def test_empty_conversation_falls_back(self):
        inst = _make_warm_instance()
        inst.conversation = []  # no system message to preserve
        engine = _make_engine(_make_pool(inst))
        assert _acquire(engine) is None

    def test_non_system_head_falls_back(self):
        inst = _make_warm_instance()
        inst.conversation = [Message(role=USER, content='hi')]
        engine = _make_engine(_make_pool(inst))
        assert _acquire(engine) is None

    def test_message_queue_pending_falls_back(self):
        inst = _make_warm_instance()
        pool = _make_pool(inst)
        pool.message_queues = {inst.instance_name: ['queued msg']}
        engine = _make_engine(pool)
        assert _acquire(engine) is None

    def test_active_stack_falls_back(self):
        inst = _make_warm_instance()
        pool = _make_pool(inst)
        pool._execution.active_stack = [(inst.instance_name, 0)]  # a live run
        engine = _make_engine(pool)
        assert _acquire(engine) is None

    def test_lock_held_returns_promptly(self):
        """The compression lock held by ANOTHER thread must NOT block — non-blocking acquire.

        _compression_lock is an RLock, so holding it on the SAME thread would re-enter (the gate
        would wrongly pass). We therefore hold it from a background thread to model a live
        compressor/rollback that owns the lock while we try to reuse.
        """
        inst = _make_warm_instance()
        engine = _make_engine(_make_pool(inst))
        released = threading.Event()

        def holder():
            inst._compression_lock.acquire()
            time.sleep(2.0)  # hold long enough for the acquire under test to run and finish
            inst._compression_lock.release()
            released.set()

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            # Spin until the holder actually owns the lock (its RLock is held by another thread).
            if inst._compression_lock.acquire(blocking=False):
                inst._compression_lock.release()
                continue  # not yet held — keep waiting
            break
        start = time.monotonic()
        got = _acquire(engine)
        elapsed = time.monotonic() - start
        t.join(timeout=5)
        assert got is None, 'a lock-held instance must fall back to fresh spawn'
        assert elapsed < 0.5, f'acquire must return promptly (non-blocking), took {elapsed:.3f}s'

    def test_no_warm_instance_seeds(self):
        """BUG_0029 Phase 3: an empty slot now SEEDS (create-or-reuse), it no longer returns None.
        The old premise ("no warm instance → fall back to fresh spawn") is exactly the bootstrap gap
        this fix closes — the fixed name is created here so the NEXT check can reuse it."""
        pool = MagicMock()
        pool.instances = {}  # nothing under the reuse name → the seed branch must fire
        engine = _make_engine(pool)
        got = _acquire(engine)
        assert isinstance(got, tuple) and len(got) == 3, f'expected a seed 3-tuple, got {got!r}'
        _, is_reuse, name = got
        assert is_reuse is False, 'a seed must not be reported as a cache hit'
        assert name == REUSE_NAME, 'the seed must create under the FIXED reuse name'

    def test_wrong_agent_class_falls_back(self):
        inst = _make_warm_instance()
        inst.agent_class = 'coder'  # not Security
        engine = _make_engine(_make_pool(inst))
        assert _acquire(engine) is None

    def test_claim_already_held_falls_back(self):
        inst = _make_warm_instance()
        engine = _make_engine(_make_pool(inst))
        try_claim(REUSE_NAME, 'someone_else')  # another check owns it
        try:
            got = engine._acquire_reusable_system_agent(
                agent_class='Security', instance_name=REUSE_NAME, task='x', caller='Maine', rid='me')
        finally:
            release_claim(REUSE_NAME, 'someone_else')
        assert got is None

    def test_state_not_idle_logs_gate_diagnostic(self, caplog):
        """A predicate-driven fallback must leave a greppable DEBUG line naming the gate that fired.

        Representative case (Gate 3, state != IDLE). core.py logs through the SHARED module logger
        (`from agent_cascade.log import logger` → name 'agent_cascade_logger'), NOT getLogger(__name__),
        so a bare caplog fixture would drop these DEBUG records — we must raise THAT logger's level.
        """
        import logging
        from agent_cascade.log import logger as app_logger

        inst = _make_warm_instance()
        with inst._state_lock:
            inst.state = AgentState.RUNNING  # non-IDLE → Gate 3 must fire
        engine = _make_engine(_make_pool(inst))

        caplog.set_level(logging.DEBUG, logger=app_logger.name)
        try:
            assert _acquire(engine) is None  # still falls back (no behavior change)
        finally:
            caplog.set_level(logging.NOTSET, logger=app_logger.name)

        gate_lines = [r for r in caplog.records if 'gate=state_not_idle' in r.getMessage()]
        assert gate_lines, (
            f'expected a DEBUG line with gate=state_not_idle, got: '
            f'{[r.getMessage() for r in caplog.records]}')
        # The uniform format carries the instance name and rid so it is machine-parseable.
        assert REUSE_NAME in gate_lines[-1].getMessage()


# ── B2. Claim released on EVERY failure path (regression guard) ───────────────
# The critical bug this guards: _acquire_reusable_system_agent takes the claim up front and only
# handed it to the caller's finally on SUCCESS. Every early-return / exception path used to leak the
# claim, wedging the warm instance for the whole process after the first eligibility miss.


class TestClaimReleasedOnFailure:
    def _acquire_and_check(self, inst, rid='r-fail'):
        """Run one acquire against a (possibly ineligible) instance; return the result."""
        _clear_all()
        engine = _make_engine(_make_pool(inst))
        got = engine._acquire_reusable_system_agent(
            agent_class='Security', instance_name=REUSE_NAME, task='x', caller='Maine', rid=rid)
        # The regression assertion: whatever the outcome, if we did NOT hand off a successful reuse
        # the claim must already be released (no leak). A successful reuse is the ONLY case where the
        # claim stays held (the caller's finally owns it from here on).
        if got is None:
            assert claim_holder(REUSE_NAME) is None, (
                f'claim leaked after a failed acquire (holder={claim_holder(REUSE_NAME)!r}) — '
                'the warm instance would be wedged for the process lifetime')
        return got

    def test_no_warm_instance_releases_claim(self):
        """BUG_0029 Phase 3: an empty slot now SEEDS, and a SUCCESSFUL seed holds the claim (so the
        naive "got is None" assertion no longer holds). The no-leak intent is preserved by making the
        seed FAIL — _create_system_agent raises → acquire returns None AND the finally releases the
        claim. That is exactly the regression this class guards."""
        pool = MagicMock()
        pool.instances = {}  # nothing under the reuse name → the seed branch fires
        _clear_all()
        engine = _make_engine(pool)
        engine._create_system_agent = MagicMock(side_effect=RuntimeError('boom: seed failed'))
        got = engine._acquire_reusable_system_agent(
            agent_class='Security', instance_name=REUSE_NAME, task='x', caller='Maine', rid='r1')
        assert got is None, 'a failed seed must fall back (None), not hand off a broken instance'
        assert claim_holder(REUSE_NAME) is None, 'seed failure → claim must be released (no leak)'

    def test_wrong_agent_class_releases_claim(self):
        inst = _make_warm_instance()
        inst.agent_class = 'coder'
        got = self._acquire_and_check(inst)
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_running_state_releases_claim(self):
        inst = _make_warm_instance()
        with inst._state_lock:
            inst.state = AgentState.RUNNING
        got = self._acquire_and_check(inst)
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_terminated_state_releases_claim(self):
        inst = _make_warm_instance()
        with inst._state_lock:
            inst.state = AgentState.TERMINATED
        got = self._acquire_and_check(inst)
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_slot_held_releases_claim(self):
        inst = _make_warm_instance()
        inst._slot_release = lambda: None
        inst._slot_key = 'some_endpoint'
        got = self._acquire_and_check(inst)
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_llm_active_releases_claim(self):
        inst = _make_warm_instance()
        inst._llm_call_active = True
        got = self._acquire_and_check(inst)
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_halted_releases_claim(self):
        inst = _make_warm_instance()
        pool = _make_pool(inst)
        pool.is_instance_halted.return_value = True
        _clear_all()
        engine = _make_engine(pool)
        got = engine._acquire_reusable_system_agent(
            agent_class='Security', instance_name=REUSE_NAME, task='x', caller='Maine', rid='r1')
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_empty_conversation_releases_claim(self):
        inst = _make_warm_instance()
        inst.conversation = []
        got = self._acquire_and_check(inst)
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_non_system_head_releases_claim(self):
        inst = _make_warm_instance()
        inst.conversation = [Message(role=USER, content='hi')]
        got = self._acquire_and_check(inst)
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_message_queue_pending_releases_claim(self):
        inst = _make_warm_instance()
        pool = _make_pool(inst)
        pool.message_queues = {inst.instance_name: ['queued msg']}
        _clear_all()
        engine = _make_engine(pool)
        got = engine._acquire_reusable_system_agent(
            agent_class='Security', instance_name=REUSE_NAME, task='x', caller='Maine', rid='r1')
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_active_stack_releases_claim(self):
        inst = _make_warm_instance()
        pool = _make_pool(inst)
        pool._execution.active_stack = [(inst.instance_name, 0)]
        _clear_all()
        engine = _make_engine(pool)
        got = engine._acquire_reusable_system_agent(
            agent_class='Security', instance_name=REUSE_NAME, task='x', caller='Maine', rid='r1')
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_exception_path_releases_claim(self):
        """A mid-acquire exception (after the claim was taken) must also release it.

        We force the exception via pool.active_stack_append (a MagicMock on the fake pool), which
        is called in the body AFTER Gate 2 passes and AFTER the claim is taken — so this exercises
        the except-Exception branch, not an early-return gate.
        """
        inst = _make_warm_instance()
        pool = _make_pool(inst)
        pool.active_stack_append = MagicMock(side_effect=RuntimeError('boom'))
        _clear_all()
        engine = _make_engine(pool)
        got = engine._acquire_reusable_system_agent(
            agent_class='Security', instance_name=REUSE_NAME, task='x', caller='Maine', rid='r1')
        assert got is None, 'an exception inside acquire must fall back to fresh spawn'
        assert claim_holder(REUSE_NAME) is None, 'exception path must release the claim (no leak)'

    def test_success_path_leaves_claim_held_for_caller(self):
        """The ONLY case where the claim stays held: a successful reuse hands it to the caller."""
        inst = _make_warm_instance()
        got = self._acquire_and_check(inst)
        assert got is not None
        # We do NOT release here — the caller's finally owns it. So the holder must still be us.
        assert claim_holder(REUSE_NAME) == 'r-fail', (
            'a successful reuse must leave the claim held for the caller to release in its finally')


# ── B3. Bootstrap fix: seed-on-miss + create-or-reuse contract (plan §5.1) ────
# THE behavioral proof that the v2 BUILD's dead feature is restored: an empty slot under the fixed
# reuse name now SEEDS, and the NEXT check REUSES the same object. The `_seed` stand-in registers the
# instance in pool.instances exactly as find_or_create_instance really does.


def _seeded_engine(pool):
    """A real engine whose _create_system_agent is a FAITHFUL stand-in: it builds an eligible
    instance AND registers it in pool.instances (what find_or_create_instance really does)."""
    engine = _make_engine(pool)

    def _seed(**kw):
        inst = _make_warm_instance(kw['instance_name'])
        pool.instances[kw['instance_name']] = inst
        return inst

    engine._create_system_agent = MagicMock(side_effect=_seed)
    return engine


class TestBootstrapSeedOnMiss:
    """plan §5.1 — the decisive behavioral tests (these FAIL on the shipped BUILD)."""

    def test_first_check_seeds_then_second_reuses(self):
        """THE bootstrap regression test: check #1 seeds under the fixed name; check #2 reuses the
        SAME object with no new spawn. Fails before the fix (the warm name is never created)."""
        pool = _make_pool()
        engine = _seeded_engine(pool)
        _clear_all()

        # Check #1 — nothing under the fixed name → must SEED
        g1 = _acquire(engine, rid='r1')
        assert g1 is not None
        inst1, is_reuse1, name1 = g1
        assert is_reuse1 is False, 'a seed must not be reported as a cache hit'
        assert name1 == REUSE_NAME
        assert pool.instances[REUSE_NAME] is inst1
        assert engine._create_system_agent.call_count == 1
        assert engine._create_system_agent.call_args.kwargs['instance_name'] == REUSE_NAME
        assert claim_holder(REUSE_NAME) == 'r1'  # held for the caller's finally

        release_claim(REUSE_NAME, 'r1')  # simulate the caller's finally

        # Check #2 — the seeded instance exists and is eligible → must REUSE the SAME object
        g2 = _acquire(engine, rid='r2')
        inst2, is_reuse2, name2 = g2
        assert is_reuse2 is True
        assert name2 == REUSE_NAME
        assert inst2 is inst1  # same object, not a re-create
        assert engine._create_system_agent.call_count == 1  # NO new spawn on #2

    def test_ineligible_existing_instance_falls_back_and_is_not_clobbered(self):
        """An existing-but-ineligible (unfrozen) instance must NOT be seeded-over or replaced."""
        inst = _make_warm_instance(REUSE_NAME)
        inst._system_prompt_frozen = False  # ineligible at Gate 8b
        pool = _make_pool(inst)
        engine = _seeded_engine(pool)
        got = _acquire(engine, rid='r1')
        assert got is None, 'an ineligible instance must fall back, not seed'
        assert pool.instances[REUSE_NAME] is inst, 'the existing instance must be left untouched'
        engine._create_system_agent.assert_not_called()

    def test_seed_freezes_prompt_for_stable_class(self):
        """The seed must freeze a stable-class system prompt at creation time, or the NEXT check
        fails Gate 8b and the seed is wasted (plan §2 finding)."""
        pool = _make_pool()
        engine = _seeded_engine(pool)
        g1 = _acquire(engine, rid='r1')
        inst1, _, _name = g1
        assert inst1._system_prompt_frozen is True

    def test_system_prompt_byte_identical_seed_then_reuse(self):
        """conversation[0] must be byte-identical between the seeded instance and the reused one.

        Capture the content BEFORE check #2's acquire so a future reset-block regression that mutates
        conversation[0] in place is caught (asserting inst1 vs inst2 alone is tautological — they are
        the same object)."""
        pool = _make_pool()
        engine = _seeded_engine(pool)
        g1 = _acquire(engine, rid='r1')
        inst1, _, _ = g1
        conv0_before = inst1.conversation[0].content  # capture BEFORE the reuse acquire
        release_claim(REUSE_NAME, 'r1')
        g2 = _acquire(engine, rid='r2')
        inst2, _, _ = g2
        assert inst2 is inst1  # same object reused (sanity)
        assert inst2.conversation[0].content == conv0_before, (
            'a reuse must not mutate conversation[0] — the KV-cache prefix must stay byte-identical')

    def test_advisor_cleanup_targets_actual_name_on_reuse(self):
        """Step-4 regression: the advisor's finally must clean up the ACTUAL instance name (the fixed
        reuse name on a hit), not the per-call fallback name — otherwise the real warm instance is
        left active and on the stack, wedging Gate 11 so advisor reuse stays dead."""
        from agent_cascade.settings import SECURITY_REUSE_SKILL_ADVISOR_NAME as ADV_NAME
        pool = _make_integration_pool()
        warm = _make_warm_instance(ADV_NAME)

        engine_instance = MagicMock()
        # Real 3-tuple: a REUSE hit under the fixed advisor name.
        engine_instance._acquire_reusable_system_agent.return_value = (warm, True, ADV_NAME)
        engine_instance.run.return_value = iter([(' [VERDICT] APPROVE', False)])

        from agent_cascade.advisor_runner import run_lightweight_advisor
        cleanup_spy = MagicMock()
        with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)), \
             patch('agent_cascade.advisor_runner._cleanup_advisor_instance', cleanup_spy):
            result = run_lightweight_advisor(
                pool=pool, agent_class='Security', instance_name=FALLBACK_INSTANCE_NAME,
                task='advise me', caller='Maine')

        assert result.ok is True
        # THE assertion: cleanup targeted the fixed reuse name, not the per-call fallback name.
        # _cleanup_advisor_instance(pool, name) → args[0]=pool, args[1]=name.
        assert cleanup_spy.call_count == 1
        cleaned_name = cleanup_spy.call_args.args[1]
        assert cleaned_name == ADV_NAME, (
            f'advisor must clean up the ACTUAL instance name ({ADV_NAME!r}), got {cleaned_name!r}')


# ── B4. Regression: reuse must NOT depend on a pool.lifecycle attribute ──────
# A real AgentPool has NO `.lifecycle` attribute — the AgentLifecycleManager lives on the ENGINE
# (self.lifecycle, set in ExecutionEngine.__init__). An earlier draft of _acquire_reusable_system_agent
# called self.pool.lifecycle._prepare_instance_for_reuse(...), which raised AttributeError that the
# method's own best-effort except swallowed → every check fell back to a fresh spawn. The test
# fixture masked it by stubbing pool.lifecycle; this test asserts the fixture never re-adds it and
# that reuse still succeeds against a realistic (lifecycle-free) pool.


class TestNoPoolLifecycleAttributeRequired:
    def test_reuse_does_not_require_pool_lifecycle_attribute(self):
        """Regression guard for core.py's self.pool.lifecycle._prepare_instance_for_reuse bug.

        A real AgentPool has NO `.lifecycle` attribute (the AgentLifecycleManager lives on the
        ENGINE). We build a REALISTIC pool — a plain object exposing only the attributes the reuse
        path actually reads, and NOTHING else — so it genuinely lacks `.lifecycle`. A MagicMock would
        auto-create the attribute on access and mask the bug (exactly what the old fixture did), so
        we deliberately avoid it here. Seeding via one acquire then re-acquiring must return the SAME
        warm object with a byte-identical conversation[0] prefix, proving the reuse path reaches the
        lifecycle manager through engine.lifecycle. On pre-fix code this fails: acquire #2 raises
        AttributeError at self.pool.lifecycle._prepare_instance_for_reuse, which the method's
        best-effort except swallows → it returns None (fresh spawn)."""
        from types import SimpleNamespace

        inst = _make_warm_instance()
        sys_before = inst.conversation[0]

        # A realistic pool: only the attributes _acquire_reusable_system_agent actually reads.
        # NO `.lifecycle` — a real AgentPool never has one (it lives on the engine).
        pool = SimpleNamespace(
            instances={inst.instance_name: inst},
            message_queues={},
            is_instance_halted=lambda name: False,
            is_instance_terminated=lambda name: False,
            _execution=SimpleNamespace(_state_lock=threading.RLock(), active_stack=[]),
            active_stack_append=lambda name, depth=0: None,
            # Real-pool attributes the reuse mutation block touches (via engine.lifecycle):
            # _children_lock guards reading the old parent; _update_child_relationship drops the
            # stale link. A real AgentPool has BOTH — and still no `.lifecycle`.
            _children_lock=threading.RLock(),
            _update_child_relationship=lambda parent, name, add=True: None,
        )

        # THE guard: the pool must genuinely lack the attribute the buggy code asked for. (On a
        # MagicMock this would be vacuous — auto-attrs make hasattr() always True.)
        assert not hasattr(pool, 'lifecycle'), (
            'the pool must NOT expose .lifecycle — a real AgentPool has none; '
            'a pool that does means this test no longer models production')

        engine = _make_engine(pool)
        _clear_all()

        # Seed / first acquire: returns the warm instance under the fixed reuse name.
        g1 = _acquire(engine, rid='r-seed')
        assert g1 is not None, 'the first acquire must succeed against a lifecycle-free pool'
        inst1, _is_reuse1, name1 = g1
        assert inst1 is inst and name1 == REUSE_NAME
        release_claim(REUSE_NAME, 'r-seed')  # model the caller's finally

        # Second acquire: must REUSE the same object (not fall back to a fresh spawn).
        g2 = _acquire(engine, rid='r-reuse')
        assert g2 is not None, (
            'the second acquire must reuse the warm instance — a None here means the '
            'pool.lifecycle AttributeError was swallowed and it fell back to a fresh spawn')
        inst2, is_reuse2, name2 = g2
        assert is_reuse2 is True
        assert name2 == REUSE_NAME
        assert inst2 is inst1, 'the reused instance must be the SAME object, not a re-create'
        # Byte-identical KV-cache prefix: conversation[0] is the identical Message object.
        assert inst2.conversation[0] is sys_before
        assert inst2.conversation[0].content == sys_before.content


# ── B5. Epsilon gate (_REUSE_IDLE_EPSILON) — REAL gate, no mocking of acquire ─
# BUG_0029 Phase 3: _REUSE_IDLE_EPSILON was lowered 5.0 → 1.0 in core.py to SHRINK the "too recent"
# blocking band so a warm Security instance is reusable sooner after its last LLM call settles.
# Gate 5b semantics (core.py ~4284): reuse is BLOCKED while `age < epsilon` (instance recently
# active — give it a moment to settle) and ALLOWED once `age >= epsilon` (idle long enough).
# These tests drive the REAL ExecutionEngine._acquire_reusable_system_agent (NOT mocked) and stamp
# _last_llm_activity with a fixed delta, then assert the gate's decision. Gaps are expressed
# RELATIVE to the constant (imported from core.py, never hardcoded) so the test stays correct if
# epsilon is tuned again:
#   * gap = epsilon * 0.3 / 0.9  → still inside the blocking band (age < epsilon) → MUST fall back
#   * gap = epsilon * 2.0        → settled (age >= epsilon)                       → MUST reuse


def _make_realistic_pool(inst):
    """A plain-object pool exposing ONLY what _acquire_reusable_system_agent's SUCCESS path reads.

    Deliberately NOT a MagicMock: auto-attrs would mask a regression that reaches for an attribute
    the real AgentPool lacks (see TestNoPoolLifecycleAttributeRequired). Includes the mutation-block
    hooks (active_stack_append, _children_lock, _update_child_relationship) so the full reuse path —
    past Gate 5b all the way to the return — runs against something realistic.
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        instances={inst.instance_name: inst},
        message_queues={},
        is_instance_halted=lambda name: False,
        is_instance_terminated=lambda name: False,
        _execution=SimpleNamespace(_state_lock=threading.RLock(), active_stack=[]),
        active_stack_append=lambda name, depth=0: None,
        _children_lock=threading.RLock(),
        _update_child_relationship=lambda parent, name, add=True: None,
    )


def _acquire_with_llm_age(inst, age_s):
    """Stamp _last_llm_activity to `age_s` seconds ago and drive the REAL acquire once.

    Returns whatever _acquire_reusable_system_agent returned (a 3-tuple on reuse/seed, None on
    fallback). The claim is released afterwards only if a successful hand-off occurred, mirroring
    the caller's finally so the registry never wedges between tests.
    """
    inst._last_llm_activity = time.monotonic() - age_s
    engine = _make_engine(_make_realistic_pool(inst))
    got = _acquire(engine, rid='r-eps')
    if got is not None:
        release_claim(REUSE_NAME, 'r-eps')  # model the caller's finally after a successful hand-off
    return got


class TestEpsilonGateReal:
    """The REAL Gate 5b (_REUSE_IDLE_EPSILON) exercised end-to-end — no mocking of acquire."""

    def test_recent_activity_within_epsilon_falls_back(self):
        """A recently-active instance (age = 0.3 * epsilon, inside the blocking band) must FALL BACK
        to fresh spawn. This is the "too recent" guard still working — a disabled/bypassed gate would
        wrongly reuse here."""
        from agent_cascade.engine.core import _REUSE_IDLE_EPSILON
        inst = _make_warm_instance()
        got = _acquire_with_llm_age(inst, age_s=_REUSE_IDLE_EPSILON * 0.3)
        assert got is None, (
            f'a gap of {_REUSE_IDLE_EPSILON * 0.3:.3f}s (< epsilon={_REUSE_IDLE_EPSILON}) must fall '
            'back — Gate 5b blocks a recently-active instance; a non-None here means the guard is gone')

    def test_recent_activity_near_epsilon_boundary_falls_back(self):
        """A gap just under epsilon (0.9 * epsilon) is STILL inside the blocking band → must FALL BACK.
        Pins the boundary direction: the gate only releases reuse at age >= epsilon, not earlier."""
        from agent_cascade.engine.core import _REUSE_IDLE_EPSILON
        inst = _make_warm_instance()
        got = _acquire_with_llm_age(inst, age_s=_REUSE_IDLE_EPSILON * 0.9)
        assert got is None, (
            f'a gap of {_REUSE_IDLE_EPSILON * 0.9:.3f}s (< epsilon={_REUSE_IDLE_EPSILON}) must still '
            'fall back — the boundary is age >= epsilon, not a smaller cutoff')

    def test_settled_instance_beyond_epsilon_reuses(self):
        """A settled instance (age = 2.0 * epsilon, past the blocking band) must REUSE the SAME warm
        object with is_reuse=True. This is the regression guard: if epsilon were reverted to a larger
        value or an async-teardown regression kept stamping _last_llm_activity too recently, this would
        fall back instead of reusing."""
        from agent_cascade.engine.core import _REUSE_IDLE_EPSILON
        inst = _make_warm_instance()
        got = _acquire_with_llm_age(inst, age_s=_REUSE_IDLE_EPSILON * 2.0)
        assert got is not None, (
            f'a gap of {_REUSE_IDLE_EPSILON * 2.0:.3f}s (>= epsilon={_REUSE_IDLE_EPSILON}) must reuse '
            'the warm instance — a None here means Gate 5b is over-blocking a settled instance')
        result_inst, was_reused, name = got
        assert result_inst is inst and was_reused is True
        assert name == REUSE_NAME


# ── C. The reset itself ───────────────────────────────────────────────────────


class TestResetSemantics:
    def _dirty_instance(self):
        """A warm instance with every §6 field dirtied so we can prove each is reset."""
        inst = _make_warm_instance()
        # Append a prior task + assistant turn so the conversation is non-trivial.
        inst.append_message(Message(role=USER, content='old task'))
        inst.append_message(Message(role='assistant', content='old answer'))
        # Dirty every field the reset must clear:
        inst.compression_summary = 'stale summary'
        inst.latest_marker_index = 7
        inst.max_turns = 25
        inst._generate_cfg_override = {'disabled_tools': ['x']}
        inst._current_turn = 3
        inst._turn_consumed = True
        inst._loop_rollback_count = 2
        inst._compression_suspended_at = 123.0
        inst._suppress_loop_detection_next_turn = True
        # NOTE: is_terminated is deliberately left False here — it is an ELIGIBILITY gate (plan
        # §3.4 item 7), so a terminated instance must fall back to fresh spawn, not be reused.
        # The dedicated test_terminated_flag_falls_back covers that path. We still assert the
        # reset assigns is_terminated=False (a no-op here) in test_every_six_field_reset.
        inst.sleeping_since = time.monotonic()
        inst._continue_saved_msg = Message(role='assistant', content='saved')
        inst._tool_warnings = ['stale warning']
        inst._cache_notifications = ['stale notif']
        inst._state_label = 'stale-label'
        inst._last_endpoint_config = {'api_base': 'x'}
        inst._memories_read = {'mem1'}
        inst._recently_hinted = {'mem1': 5}
        inst._last_memory_hint_turn = 4
        inst._recently_skill_hinted = {'sk1': 5}
        inst._last_skill_hint_turn = 4
        inst._auto_skill_task_output = 'stale'
        inst._auto_skill_proposed = True
        inst._auto_skill_dirty_stop = True
        inst._tg_first_pushed = True
        inst._tg_final_pushed_phase = 'pre'
        inst._tg_first_pushed_text = 'stale text'
        # Token counters (rebuild_conversation resets these):
        inst._cached_token_count = 999
        inst._last_actual_token_count = 888
        return inst

    def test_conversation0_object_identity_preserved(self):
        """conversation[0] must be the SAME object after reuse (the KV-cache win)."""
        inst = self._dirty_instance()
        sys_before = inst.conversation[0]
        engine = _make_engine(_make_pool(inst))
        got = _acquire(engine)
        assert got is not None
        assert inst.conversation[0] is sys_before, 'conversation[0] object identity must be preserved'

    def test_system_content_byte_identical(self):
        inst = self._dirty_instance()
        before = inst.conversation[0].content
        engine = _make_engine(_make_pool(inst))
        _acquire(engine)
        assert inst.conversation[0].content == before, 'system prompt bytes must be unchanged'

    def test_frozen_stays_true(self):
        inst = self._dirty_instance()
        engine = _make_engine(_make_pool(inst))
        _acquire(engine)
        assert inst._system_prompt_frozen is True, '_system_prompt_frozen must stay True (no reset_conversation)'

    def test_every_six_field_reset(self):
        inst = self._dirty_instance()
        engine = _make_engine(_make_pool(inst))
        got = _acquire(engine)
        assert got is not None
        assert inst.compression_summary is None
        assert inst.latest_marker_index == -1
        assert inst.max_turns is None
        assert inst._generate_cfg_override is None
        assert inst._current_turn == 0
        assert inst._turn_consumed is False
        assert inst._loop_rollback_count == 0
        assert inst._compression_suspended_at == 0.0
        assert inst._suppress_loop_detection_next_turn is False
        assert inst.is_terminated is False
        assert inst.sleeping_since is None
        assert inst._continue_saved_msg is None
        assert inst._tool_warnings == []
        assert inst._cache_notifications == []
        assert inst._state_label is None
        assert inst._last_endpoint_config is None
        assert inst._memories_read == set()
        assert inst._recently_hinted == {}
        assert inst._last_memory_hint_turn == -1
        assert inst._recently_skill_hinted == {}
        assert inst._last_skill_hint_turn == -1
        assert inst._auto_skill_task_output is None
        assert inst._auto_skill_proposed is False
        assert inst._auto_skill_dirty_stop is False
        assert inst._tg_first_pushed is False
        assert inst._tg_final_pushed_phase is None
        assert inst._tg_first_pushed_text is None

    def test_token_counters_reset(self):
        inst = self._dirty_instance()
        engine = _make_engine(_make_pool(inst))
        _acquire(engine)
        assert inst._cached_token_count == 0
        assert inst._last_actual_token_count == 0
        assert inst._last_token_count_conversation_length == -1

    def test_new_task_appended(self):
        inst = self._dirty_instance()
        engine = _make_engine(_make_pool(inst))
        _acquire(engine, task='fresh check', caller='Maine')
        # conversation is now [system, new_user_task] — the old turns are gone.
        assert len(inst.conversation) == 2
        assert inst.conversation[0].role == SYSTEM
        assert inst.conversation[1].role == USER
        assert 'fresh check' in inst.conversation[1].content

    def test_ownership_repointed(self):
        inst = self._dirty_instance()
        engine = _make_engine(_make_pool(inst))
        _acquire(engine, caller='Maine')
        assert inst.parent_instance == 'Maine'
        assert inst._nest_depth == 0
        assert inst.restricted_shell is True


# ── D. Integration: security_handler ─────────────────────────────────────────


def _make_handler(pool):
    from agent_cascade.security_handler import SecurityAdvisorHandler
    app = type('App', (), {})()
    session = {'session_name': 'Maine', 'generate_cfg': {}}
    send_queue = MagicMock()
    return SecurityAdvisorHandler(pool, session, app, send_queue, lambda: None)


def _make_integration_pool():
    """Minimal pool mirroring test_security_handler_deadlock_fixes._make_minimal_pool."""
    from agent_cascade.runtime_state import state
    pool = MagicMock()
    pool.stopped = False
    pool.operation_manager.base_dir = '/tmp/test'
    pool.operation_manager.extra_work_folders_ro = []
    pool.operation_manager.extra_work_folders_rw = []
    pool.operation_manager.enable_timeout = True
    pool.operation_manager.approval_timeout_seconds = 180
    pool.instance_state = {}
    pool._execution = MagicMock()
    pool._execution._state_lock = threading.Lock()
    # A Security template with an LLM so the cfg block doesn't warn.
    template = MagicMock()
    template.llm = MagicMock()
    template.llm.generate_cfg = {}
    pool.get_template.return_value = template
    return pool


def _make_ap(rid):
    return {
        'request_id': rid,
        'tool_name': 'shell_cmd',
        'description': 'test',
        'tool_args': {},
        'agent_name': 'Maine',
    }


class TestSecurityHandlerIntegration:
    def _run_check(self, handler, pool, engine_instance, rid):
        ap = _make_ap(rid)
        with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)):
            handler._execute_check(
                ap=ap, sec_inst=None, rid=rid, auto_apply=True, instance_name='Maine',
                caller_agent='Maine', prompt_template='Test {tool_name}',
                timeout_seconds=3600, warning_seconds=2400)

    def test_two_sequential_checks_reuse_same_instance(self):
        """Two checks → the same warm instance is reused and its prefix is preserved."""
        pool = _make_integration_pool()
        handler = _make_handler(pool)
        warm = _make_warm_instance(REUSE_NAME)

        engine_instance = MagicMock()
        # First call: reuse succeeds. Second call (same warm instance): also reuse.
        engine_instance._acquire_reusable_system_agent.side_effect = lambda **kw: (warm, True, REUSE_NAME)
        engine_instance.run.return_value = iter([(' [YES] safe', False)])

        with patch('agent_cascade.settings.SECURITY_REUSE_ENABLED', True):
            self._run_check(handler, pool, engine_instance, 'rid_A')
            self._run_check(handler, pool, engine_instance, 'rid_B')

        assert engine_instance._acquire_reusable_system_agent.call_count == 2
        # Both runs must have executed against the SAME warm instance.
        run_instances = [c.args[0] for c in engine_instance.run.call_args_list]
        assert all(i is warm for i in run_instances), 'both checks must reuse the same warm instance'
        # Prefix preserved: the system message object is unchanged across both checks.
        assert warm.conversation[0].content == 'SYS_PROMPT_PREFIX'

    def test_wedged_instance_falls_back_to_fresh_spawn(self):
        """When acquire returns None (wedged), the legacy fresh-spawn path still completes."""
        pool = _make_integration_pool()
        handler = _make_handler(pool)
        fresh_inst = MagicMock()
        fresh_inst.conversation = []

        engine_instance = MagicMock()
        engine_instance._acquire_reusable_system_agent.return_value = None  # wedged → fall back
        engine_instance._create_system_agent.return_value = fresh_inst
        engine_instance.run.return_value = iter([(' [YES] safe', False)])

        with patch('agent_cascade.settings.SECURITY_REUSE_ENABLED', True):
            self._run_check(handler, pool, engine_instance, 'rid_wedged')

        # Fallback must have spawned a fresh instance and run it. (The [YES]-safe verdict does not
        # route through user_approve, so we don't assert on that — the two calls above already prove
        # the legacy fresh-spawn path ran to completion without an exception escaping _execute_check.)
        engine_instance._create_system_agent.assert_called_once()
        assert engine_instance.run.call_args.args[0] is fresh_inst

    def test_claim_released_in_finally_on_exception(self):
        """Even when engine.run raises mid-loop, the reuse claim must be released in finally."""
        from agent_cascade.security_reuse import claim_holder, release_claim
        pool = _make_integration_pool()
        handler = _make_handler(pool)
        warm = _make_warm_instance(REUSE_NAME)

        # A generator that yields one token then raises — the raise lands INSIDE the run loop,
        # so it propagates out of the handler and the finally must release the claim.
        # Note: engine.run(...) is invoked with a positional arg, so _boom must accept *args.
        def _boom(*_a):
            yield (' [YES] safe', False)
            raise RuntimeError('simulated LLM crash')

        engine_instance = MagicMock()
        engine_instance._acquire_reusable_system_agent.return_value = (warm, True, REUSE_NAME)
        engine_instance.run.side_effect = _boom

        # The mocked acquire does NOT run the real try_claim, so we claim manually with the SAME
        # rid the handler passes ('rid_exc'). This models "the check owns the warm instance" and
        # lets us assert the handler's finally (or except-RuntimeError path) releases it.
        _clear_all()
        assert try_claim(REUSE_NAME, 'rid_exc') is True

        with patch('agent_cascade.settings.SECURITY_REUSE_ENABLED', True):
            try:
                self._run_check(handler, pool, engine_instance, 'rid_exc')
            except Exception:
                pass  # the exception is expected; we only care about claim release

        # The handler's finally (or its except-RuntimeError re-raise path) must have released it.
        assert claim_holder(REUSE_NAME) is None, 'claim must be released in finally even on exception'

    def test_disabled_reproduces_today_behavior(self):
        """SECURITY_REUSE_ENABLED=False → acquire never called; legacy per-rid spawn used."""
        pool = _make_integration_pool()
        handler = _make_handler(pool)
        fresh_inst = MagicMock()
        fresh_inst.conversation = []

        engine_instance = MagicMock()
        engine_instance._create_system_agent.return_value = fresh_inst
        engine_instance.run.return_value = iter([(' [YES] safe', False)])

        with patch('agent_cascade.settings.SECURITY_REUSE_ENABLED', False):
            self._run_check(handler, pool, engine_instance, 'rid_off')

        engine_instance._acquire_reusable_system_agent.assert_not_called()
        # Legacy path uses the per-rid name Security_<rid>.
        call_kwargs = engine_instance._create_system_agent.call_args.kwargs
        assert call_kwargs['instance_name'] == f'Security_rid_off'


# ── E. Integration: advisor_runner ────────────────────────────────────────────


class TestAdvisorRunnerIntegration:
    def _run_advisor(self, pool, engine_instance):
        from agent_cascade.advisor_runner import run_lightweight_advisor
        return run_lightweight_advisor(
            pool=pool, agent_class='Security', instance_name=FALLBACK_INSTANCE_NAME,
            task='advise me', caller='Maine')

    def test_reuse_path_uses_warm_instance(self):
        from agent_cascade.settings import SECURITY_REUSE_SKILL_ADVISOR_NAME
        pool = _make_integration_pool()
        warm = _make_warm_instance(SECURITY_REUSE_SKILL_ADVISOR_NAME)

        engine_instance = MagicMock()
        engine_instance._acquire_reusable_system_agent.return_value = (warm, True, SECURITY_REUSE_SKILL_ADVISOR_NAME)
        engine_instance.run.return_value = iter([(' [VERDICT] APPROVE', False)])

        with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)):
            result = self._run_advisor(pool, engine_instance)

        assert result.ok is True
        # The reuse name was passed to acquire.
        kw = engine_instance._acquire_reusable_system_agent.call_args.kwargs
        assert kw['instance_name'] == SECURITY_REUSE_SKILL_ADVISOR_NAME
        # Fresh spawn must NOT have happened.
        engine_instance._create_system_agent.assert_not_called()

    def test_fallback_path_spawns_fresh(self):
        pool = _make_integration_pool()
        fresh_inst = MagicMock()
        fresh_inst.conversation = []

        engine_instance = MagicMock()
        engine_instance._acquire_reusable_system_agent.return_value = None  # wedged
        engine_instance._create_system_agent.return_value = fresh_inst
        engine_instance.run.return_value = iter([(' [VERDICT] APPROVE', False)])

        with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)):
            result = self._run_advisor(pool, engine_instance)

        assert result.ok is True
        engine_instance._create_system_agent.assert_called_once()
        # Fallback uses the per-call instance_name passed in.
        kw = engine_instance._create_system_agent.call_args.kwargs
        assert kw['instance_name'] == FALLBACK_INSTANCE_NAME

    # ── T1: THE regression test (fails before Change A, passes after) ──────────
    def test_broadcast_uses_actual_name_on_reuse_hit(self):
        """On a reuse hit the advisor must broadcast under the REAL instance name (the fixed reuse
        name), not the per-call fallback — otherwise build_stream_update_from_pool can't resolve it
        and zero frames are enqueued for the whole run (the streaming-freeze bug)."""
        import asyncio
        from agent_cascade.settings import SECURITY_REUSE_SKILL_ADVISOR_NAME
        pool = _make_integration_pool()
        # broadcast_stream_update resolves ws_queue/ws_loop from the pool and bails early if either
        # is missing — so give it a live loop + queue to reach build_stream_update_from_pool.
        loop = asyncio.new_event_loop()
        pool._ws_send_queue = asyncio.Queue(maxsize=10)
        pool._ws_loop = loop
        warm = _make_warm_instance(SECURITY_REUSE_SKILL_ADVISOR_NAME)

        engine_instance = MagicMock()
        engine_instance._acquire_reusable_system_agent.return_value = (warm, True, SECURITY_REUSE_SKILL_ADVISOR_NAME)
        engine_instance.run.return_value = iter([(' [VERDICT] APPROVE', False)])

        try:
            with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)), \
                 patch('agent_cascade.api_integration_pkg.streaming.build_stream_update_from_pool') as bsu:
                self._run_advisor(pool, engine_instance)
        finally:
            loop.close()

        assert bsu.called, 'build_stream_update_from_pool must be invoked at least once during the run'
        # The broadcast must target the real reuse name so pool.get_instance() resolves it.
        assert bsu.call_args.kwargs['instance_name'] == SECURITY_REUSE_SKILL_ADVISOR_NAME
        assert bsu.call_args.kwargs['instance_name'] != FALLBACK_INSTANCE_NAME

    # ── T2: fallback path unchanged ────────────────────────────────────────────
    def test_broadcast_uses_fallback_name_on_fresh_spawn(self):
        """On a fresh-spawn fallback (acquire returns None) the broadcast must still use the per-call
        instance_name — Change A must not alter the legacy path."""
        import asyncio
        pool = _make_integration_pool()
        loop = asyncio.new_event_loop()
        pool._ws_send_queue = asyncio.Queue(maxsize=10)
        pool._ws_loop = loop
        fresh_inst = MagicMock()
        fresh_inst.conversation = []

        engine_instance = MagicMock()
        engine_instance._acquire_reusable_system_agent.return_value = None  # wedged → fresh spawn
        engine_instance._create_system_agent.return_value = fresh_inst
        engine_instance.run.return_value = iter([(' [VERDICT] APPROVE', False)])

        try:
            with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)), \
                 patch('agent_cascade.api_integration_pkg.streaming.build_stream_update_from_pool') as bsu:
                self._run_advisor(pool, engine_instance)
        finally:
            loop.close()

        assert bsu.called
        assert bsu.call_args.kwargs['instance_name'] == FALLBACK_INSTANCE_NAME

    # ── T3: end-to-end frame actually builds (no mocks for the builder) ────────
    def test_reuse_hit_builds_real_frame_and_enqueues(self):
        """With NO mock on the builder, a reuse hit must produce a real stream_update dict and
        enqueue it — proving frames flow for the whole run instead of being silently dropped.

        (The pre-fix bug: broadcast used the per-call fallback name → pool.get_instance(fallback) →
        None → build returned None → nothing enqueued. With Change A the reuse name resolves.)"""
        import asyncio
        from agent_cascade.settings import SECURITY_REUSE_SKILL_ADVISOR_NAME

        warm = _make_warm_instance(SECURITY_REUSE_SKILL_ADVISOR_NAME)
        # A pool whose get_instance resolves the reuse name to the warm instance but NOT the
        # per-call fallback (mirrors production: only Security_skilladvisor exists in the pool).
        pool = MagicMock()
        pool.stopped = False
        pool.instance_state = {}
        pool.get_template.return_value = None  # advisor falls back to minimal cfg — fine here
        pool._execution = MagicMock()
        pool._execution._state_lock = threading.Lock()

        def _get_instance(name):
            return warm if name == SECURITY_REUSE_SKILL_ADVISOR_NAME else None

        pool.get_instance.side_effect = _get_instance

        # A live event loop + send queue so broadcast_stream_update's run_coroutine_threadsafe works.
        # (asyncio.Queue(loop=...) was removed in 3.10+; bind the loop explicitly instead.)
        loop = asyncio.new_event_loop()
        ws_queue = asyncio.Queue(maxsize=10)
        try:
            ws_queue._loop = loop
        except Exception:
            pass
        pool._ws_send_queue = ws_queue
        pool._ws_loop = loop

        engine_instance = MagicMock()
        engine_instance._acquire_reusable_system_agent.return_value = (warm, True, SECURITY_REUSE_SKILL_ADVISOR_NAME)
        engine_instance.run.return_value = iter([(' [VERDICT] APPROVE', False)])

        # The builder is REAL here. The only non-defensive call in _serialize_instance against a
        # MagicMock pool is _get_max_tokens_for_instance (its sibling calls are try/except-guarded),
        # so patch it to return an int and let the rest of the real build + enqueue path run.
        import agent_cascade.api_integration_pkg.state_builder as sb

        # The advisor acquires the instance via the ENGINE (not pool.get_instance), so the only way a
        # frame reaches the WS queue is if broadcast_stream_update resolves the REAL reuse name through
        # build_stream_update_from_pool → pool.get_instance(reuse_name). Before Change A it used the
        # per-call fallback name, which get_instance returns None for → no frame. So "a frame landed"
        # is the end-to-end proof that Change A took effect.
        try:
            with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)), \
                 patch.object(sb, '_get_max_tokens_for_instance', return_value=8192):
                self._run_advisor(pool, engine_instance)

            # A stream_update frame was actually dispatched to the WS queue. Drain the pending
            # put_nowait tasks (scheduled via run_coroutine_threadsafe) and confirm at least one
            # event landed in the queue.
            async def _drain():
                while ws_queue.qsize() < 1:
                    await asyncio.sleep(0)
                return True

            loop.run_until_complete(asyncio.wait_for(_drain(), timeout=5.0))
            assert ws_queue.qsize() >= 1, \
                'at least one stream_update frame must be enqueued (proves the reuse name resolved)'
        finally:
            try:
                loop.close()
            except Exception:
                pass

    # ── T4: _serialize_instances_incremental isolation (Change B) ──────────────
    def test_serialize_isolation_bad_instance_does_not_abort_frame(self):
        """One malformed instance whose serialization raises must NOT abort the whole frame — the
        healthy instance's entry is still returned and no exception escapes. Without Change B the
        per-instance body has no try/except, so a single bad instance would propagate and drop every
        other agent from the frame."""
        import logging
        import agent_cascade.api_integration_pkg.state_builder as sb
        from agent_cascade.api_integration_pkg.state_builder import _serialize_instances_incremental

        # Healthy: a REAL warm instance (real locks + real [system] conversation) so its serialization
        # completes cleanly.
        healthy = _make_warm_instance('healthy_inst')

        # Bad: a MagicMock whose per-instance body raises at its very first step — acquiring
        # inst._compression_lock (state_builder.py:238). That `with` is the FIRST line inside Change B's
        # try block, so the raise propagates straight to the per-instance except and the bad instance is
        # skipped. A plain MagicMock would otherwise serialize "cleanly" (auto-attrs) and wrongly land in
        # the result.
        bad = MagicMock()
        bad.instance_name = 'bad_inst'
        # Make `with inst._compression_lock:` raise: set __enter__.side_effect (not the mock's own
        # side_effect, which only fires on call() not context-manager entry).
        bad._compression_lock.__enter__.side_effect = RuntimeError('simulated serialization crash')

        pool = MagicMock()
        pool.instances = {'bad_inst': bad, 'healthy_inst': healthy}

        # Capture the WARNING Change B emits when it skips the bad instance.
        records = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r)
        sb_logger = logging.getLogger('agent_cascade_logger')
        old_level = sb_logger.level
        sb_logger.addHandler(handler)
        try:
            # _get_max_tokens_for_instance does an int comparison against pool settings; with a
            # MagicMock pool that raises. Patch it so the HEALTHY instance serializes to completion and
            # only the BAD one trips Change B's isolation (via slice_history_for_llm above).
            with patch.object(sb, '_get_max_tokens_for_instance', return_value=8192):
                result = _serialize_instances_incremental(pool, instance_name='healthy_inst', force_full=False)
        finally:
            sb_logger.removeHandler(handler)
            sb_logger.setLevel(old_level)

        # The healthy instance was serialized; the bad one was skipped without raising.
        assert 'healthy_inst' in result
        assert 'bad_inst' not in result
        # Change B must have logged a WARNING naming the skipped (bad) instance — proof the isolation
        # path ran rather than silently swallowing the error.
        skip_msgs = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
        assert any('bad_inst' in m for m in skip_msgs), \
            f'expected a WARNING naming bad_inst, got: {skip_msgs}'

    # ── T5: _streaming_responses cleared on acquire (Change C) ────────────────
    def test_streaming_responses_cleared_on_acquire(self):
        """A warm instance seeded with a stale partial must have _streaming_responses == [] after a
        successful reuse, and its conversation reset to [system, new_task] (len 2)."""
        inst = _make_warm_instance()
        inst._streaming_responses = [Message(role='assistant', content='old')]
        engine = _make_engine(_make_pool(inst))
        got = _acquire(engine)
        assert got is not None, 'acquire must reuse the warm instance for this test'
        assert inst._streaming_responses == [], '_streaming_responses must be cleared on reuse acquire'
        assert len(inst.conversation) == 2

    # ── T6: regression guard for the approval path ────────────────────────────
    def test_approval_path_broadcasts_under_reuse_name(self):
        """The approval (security_handler) path already broadcasts under sec_state_key = the fixed reuse
        name. Guard that a reuse hit keeps streaming under SECURITY_REUSE_APPROVAL_NAME, not the
        per-rid fallback — mirrors the T1 contract for the sibling path."""
        import asyncio
        from agent_cascade.security_handler import SecurityAdvisorHandler
        from agent_cascade.settings import SECURITY_REUSE_APPROVAL_NAME
        pool = _make_integration_pool()
        loop = asyncio.new_event_loop()
        real_queue = asyncio.Queue(maxsize=10)
        pool._ws_send_queue = real_queue
        pool._ws_loop = loop
        # Build the handler with a REAL send queue (not _make_handler's MagicMock): _handle_verdict calls
        # run_coroutine_threadsafe(self.send_queue.put(...)), which needs a real coroutine, not a MagicMock.
        app = type('App', (), {})()
        session = {'session_name': 'Maine', 'generate_cfg': {}}
        handler = SecurityAdvisorHandler(pool, session, app, real_queue, lambda: None)
        warm = _make_warm_instance(SECURITY_REUSE_APPROVAL_NAME)

        engine_instance = MagicMock()
        engine_instance._acquire_reusable_system_agent.return_value = (warm, True, SECURITY_REUSE_APPROVAL_NAME)

        # The real engine.run commits the turn's assistant output to the conversation before returning;
        # extract_instance_output then reads messages[-1].content. Mirror that so the handler parses a
        # real [YES] verdict (otherwise it sees only the system prompt → ambiguous → _handle_ambiguous).
        def _run(inst):
            inst.append_message(Message(role='assistant', content='[YES] safe'))
            yield (' [YES] safe', False)

        engine_instance.run.side_effect = lambda inst: _run(inst)

        # _run_check lives on TestSecurityHandlerIntegration; invoke its method directly.
        run_check = TestSecurityHandlerIntegration._run_check

        try:
            with patch('agent_cascade.settings.SECURITY_REUSE_ENABLED', True), \
                 patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)), \
                 patch('agent_cascade.api_integration_pkg.streaming.build_stream_update_from_pool') as bsu:
                run_check(self, handler, pool, engine_instance, 'rid_t6')
        finally:
            loop.close()

        # The approval path must broadcast under the fixed reuse name (sec_state_key), never the fallback.
        assert bsu.called, 'the approval path must broadcast stream updates'
        names = [c.kwargs.get('instance_name') for c in bsu.call_args_list]
        assert SECURITY_REUSE_APPROVAL_NAME in names, f'expected {SECURITY_REUSE_APPROVAL_NAME} in broadcast names, got {names}'

    def test_engine_ctor_error_not_masked_by_unbound_reuse_name(self):
        """B8 regression: if ExecutionEngine(pool) raises BEFORE reuse_name is assigned inside the
        try, the finally must NOT raise UnboundLocalError (which would mask the real error). The
        real engine-ctor error must be what lands in result.error_msg.
        """
        from agent_cascade.security_reuse import _clear_all
        pool = _make_integration_pool()
        _clear_all()

        sentinel = RuntimeError('engine ctor exploded')
        with patch('agent_cascade.execution_engine.ExecutionEngine', side_effect=sentinel):
            result = self._run_advisor(pool, None)

        # The real error must be recorded — NOT an UnboundLocalError from the finally.
        assert result.was_error is True
        assert 'engine ctor exploded' in (result.error_msg or '')
        assert 'reuse_name' not in (result.error_msg or ''), \
            f'UnboundLocalError masked the real error: {result.error_msg!r}'

    def test_claim_released_in_finally_on_exception(self):
        """The advisor's finally must release its claim even when engine.run raises mid-loop.

        We do NOT patch stdlib `time` (advisor_runner.time IS the stdlib module — patching it is a
        process-wide hazard under xdist). Instead we spy on release_claim and assert it was invoked
        for the advisor reuse name with a rid matching the runner's own adv_rid = f'adv_{...}'.
        """
        from agent_cascade.security_reuse import claim_holder, _clear_all
        from agent_cascade.settings import SECURITY_REUSE_SKILL_ADVISOR_NAME
        pool = _make_integration_pool()
        warm = _make_warm_instance(SECURITY_REUSE_SKILL_ADVISOR_NAME)

        def _boom(*_a):
            yield (' [VERDICT] APPROVE', False)
            raise RuntimeError('crash')

        engine_instance = MagicMock()
        engine_instance._acquire_reusable_system_agent.return_value = (warm, True, SECURITY_REUSE_SKILL_ADVISOR_NAME)
        engine_instance.run.side_effect = _boom

        _clear_all()
        release_spy = MagicMock(wraps=release_claim)  # real behavior + records args
        with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)), \
             patch('agent_cascade.security_reuse.release_claim', release_spy):
            result = self._run_advisor(pool, engine_instance)

        assert result.was_error is True
        # The finally must have called release_claim for the advisor reuse name with a rid that
        # matches the runner's own adv_rid derivation. This proves the release actually happened —
        # not just that the claim happens to be free (which would be vacuous with a mocked acquire).
        assert release_spy.call_count == 1, f'expected exactly one release_claim call, got {release_spy.call_count}'
        name_arg, rid_arg = release_spy.call_args.args
        assert name_arg == SECURITY_REUSE_SKILL_ADVISOR_NAME
        assert isinstance(rid_arg, str) and rid_arg.startswith('adv_'), f'unexpected rid {rid_arg!r}'
        # Belt-and-braces: the registry is actually clean too (no leaked owner).
        assert claim_holder(SECURITY_REUSE_SKILL_ADVISOR_NAME) is None

    # ── Fresh-spawn fallback cleanup: remove_instance discriminator proof ────────
    # BUG: a fresh-spawn fallback (effective_name != reuse_name) was marked inactive by
    # _cleanup_advisor_instance but never popped from pool.instances, so its Security_op_* UI tab
    # lingered forever. The fix removes it in the finally — but ONLY for the fresh-spawn path; a
    # warm hit / seed uses the FIXED reuse name and must persist for the next check to reuse it.
    #
    # CRITICAL fixture caveat: _make_integration_pool returns a MagicMock with pool.instance_state={}
    # but NO real .instances dict and NO real remove_instance — pool.remove_instance(name) on that
    # mock is an auto-spec call that mutates nothing, so asserting on pool.instances membership is
    # VACUOUS. Option A (below): give each test a REAL dict + a spy whose side_effect actually pops,
    # so "the name is gone from pool.instances" is meaningful.

    def _wire_remove_spy(self, pool, seed_name=None, seed_inst=None):
        """Give the MagicMock pool a REAL .instances dict and a remove_instance spy that pops.

        Returns (remove_spy, cleanup_spy, release_spy). The side_effect actually mutates the real
        dict so membership assertions are meaningful; wrapping release_claim keeps its real no-op-on-None
        behavior while recording args.
        """
        from agent_cascade.security_reuse import _clear_all
        pool.instances = {seed_name: seed_inst} if seed_name else {}
        remove_spy = MagicMock(side_effect=lambda n: pool.instances.pop(n, None))
        pool.remove_instance = remove_spy
        cleanup_spy = MagicMock()
        release_spy = MagicMock(wraps=release_claim)
        _clear_all()  # isolate the claim registry per test
        return remove_spy, cleanup_spy, release_spy

    def test_fresh_spawn_fallback_removes_itself(self):
        """T1 (POSITIVE discriminator half): a fresh-spawn fallback (name != fixed reuse name) is
        popped from pool.instances in the finally — its Security_op_* tab no longer lingers."""
        from agent_cascade.settings import SECURITY_REUSE_SKILL_ADVISOR_NAME as ADV_NAME
        FALLBACK = FALLBACK_INSTANCE_NAME  # the per-call instance_name _run_advisor passes in
        pool = _make_integration_pool()
        fresh_inst = MagicMock()
        fresh_inst.conversation = []

        engine_instance = MagicMock()
        # Real 3-tuple: a FRESH-SPAWN fallback — was_reused=False, actual_name=FALLBACK != ADV_NAME.
        engine_instance._acquire_reusable_system_agent.return_value = (fresh_inst, False, FALLBACK)
        engine_instance.run.return_value = iter([(' [VERDICT] APPROVE', False)])

        remove_spy, cleanup_spy, release_spy = self._wire_remove_spy(pool, FALLBACK, fresh_inst)
        with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)), \
             patch('agent_cascade.advisor_runner._cleanup_advisor_instance', cleanup_spy), \
             patch('agent_cascade.security_reuse.release_claim', release_spy):
            result = self._run_advisor(pool, engine_instance)

        assert result.ok is True
        # THE assertion: the fallback name was removed from the pool (and actually popped).
        remove_spy.assert_called_once_with(FALLBACK)
        assert FALLBACK not in pool.instances, 'fresh-spawn corpse must be popped from pool.instances'
        # Regression guards: existing teardown still ran with the correct actual_name + claim released.
        # _cleanup_advisor_instance(pool, name) — the NAME is args[1], not args[0].
        assert cleanup_spy.call_count == 1 and cleanup_spy.call_args.args[1] == FALLBACK
        assert release_spy.call_count >= 1

    def test_warm_hit_does_not_remove(self):
        """T2: a warm hit (was_reused=True, name == fixed reuse name) must NOT be removed — ZERO
        calls to remove_instance. Asserting not-called-with-ADV_NAME would be vacuous; assert_not_called
        is what proves the guard keys on effective_name != reuse_name, not was_reused."""
        from agent_cascade.settings import SECURITY_REUSE_SKILL_ADVISOR_NAME as ADV_NAME
        pool = _make_integration_pool()
        warm = _make_warm_instance(ADV_NAME)

        engine_instance = MagicMock()
        # Real 3-tuple: a WARM HIT — was_reused=True, actual_name=ADV_NAME.
        engine_instance._acquire_reusable_system_agent.return_value = (warm, True, ADV_NAME)
        engine_instance.run.return_value = iter([(' [VERDICT] APPROVE', False)])

        remove_spy, cleanup_spy, release_spy = self._wire_remove_spy(pool, ADV_NAME, warm)
        with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)), \
             patch('agent_cascade.advisor_runner._cleanup_advisor_instance', cleanup_spy), \
             patch('agent_cascade.security_reuse.release_claim', release_spy):
            result = self._run_advisor(pool, engine_instance)

        assert result.ok is True
        # THE assertion: ZERO removals — the warm instance must survive for reuse.
        remove_spy.assert_not_called()
        assert ADV_NAME in pool.instances, 'warm instance must persist in pool.instances'
        # Regression guards: existing teardown still ran with the correct actual_name + claim released.
        assert cleanup_spy.call_count == 1 and cleanup_spy.call_args.args[1] == ADV_NAME
        assert release_spy.call_count >= 1

    def test_seed_on_miss_does_not_remove(self):
        """T3 (CRITICAL discriminator half): a seed-on-miss has was_reused=False BUT name == fixed reuse
        name — it must NOT be removed. This is the case that separates 'fresh spawn' from 'seed': both
        have was_reused=False, so only the name-equality discriminator distinguishes them. T2+T3 together
        prove the guard is NOT keyed on was_reused."""
        from agent_cascade.settings import SECURITY_REUSE_SKILL_ADVISOR_NAME as ADV_NAME
        pool = _make_integration_pool()
        seeded = _make_warm_instance(ADV_NAME)

        engine_instance = MagicMock()
        # Real 3-tuple: a SEED-ON-MISS — was_reused=False, actual_name=ADV_NAME (== reuse name).
        engine_instance._acquire_reusable_system_agent.return_value = (seeded, False, ADV_NAME)
        engine_instance.run.return_value = iter([(' [VERDICT] APPROVE', False)])

        remove_spy, cleanup_spy, release_spy = self._wire_remove_spy(pool, ADV_NAME, seeded)
        with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)), \
             patch('agent_cascade.advisor_runner._cleanup_advisor_instance', cleanup_spy), \
             patch('agent_cascade.security_reuse.release_claim', release_spy):
            result = self._run_advisor(pool, engine_instance)

        assert result.ok is True
        # THE assertion: ZERO removals even though was_reused=False — the seeded first-warm persists.
        remove_spy.assert_not_called()
        assert ADV_NAME in pool.instances, 'seeded warm instance must persist in pool.instances'
        # Regression guards: existing teardown still ran with the correct actual_name + claim released.
        assert cleanup_spy.call_count == 1 and cleanup_spy.call_args.args[1] == ADV_NAME
        assert release_spy.call_count >= 1

    def test_kill_switch_off_removes_every_run(self):
        """T4: SECURITY_REUSE_ENABLED=False → reuse_name=None, so EVERY run is a fresh spawn under its
        per-call name and must self-remove (there is no warm instance to preserve). Covers the
        `reuse_name is None` branch of the guard. Mirrors test_fallback_path_spawns_fresh."""
        FALLBACK = FALLBACK_INSTANCE_NAME  # the per-call instance_name _run_advisor passes in
        pool = _make_integration_pool()
        fresh_inst = MagicMock()
        fresh_inst.conversation = []

        engine_instance = MagicMock()
        # Kill-switch OFF: acquire is never called; the legacy fresh-spawn path creates a fresh
        # instance under the per-call name. Pin it so we can assert on the exact removed name.
        engine_instance._create_system_agent.return_value = fresh_inst
        engine_instance.run.return_value = iter([(' [VERDICT] APPROVE', False)])

        remove_spy, cleanup_spy, release_spy = self._wire_remove_spy(pool, FALLBACK, fresh_inst)
        with patch('agent_cascade.settings.SECURITY_REUSE_ENABLED', False), \
             patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=engine_instance)), \
             patch('agent_cascade.advisor_runner._cleanup_advisor_instance', cleanup_spy), \
             patch('agent_cascade.security_reuse.release_claim', release_spy):
            result = self._run_advisor(pool, engine_instance)

        assert result.ok is True
        # THE assertion: the per-call name was removed (reuse_name=None → effective_name != None).
        remove_spy.assert_called_once_with(FALLBACK)
        assert FALLBACK not in pool.instances, 'fresh-spawn corpse must be popped from pool.instances'
        # Regression guards: existing teardown still ran with the correct actual_name + claim released.
        assert cleanup_spy.call_count == 1 and cleanup_spy.call_args.args[1] == FALLBACK
        assert release_spy.call_count >= 1


# ── F. Concurrency ────────────────────────────────────────────────────────────


class TestConcurrency:
    def test_no_reuse_while_claim_held(self):
        """While a claim is held, NO thread can reuse — both concurrent checks fall back.

        This models the production invariant that the claim is held across a check's ENTIRE run
        (taken in _acquire, released only in the caller's finally after the LLM call). We pre-claim
        the name so BOTH threads lose; this is deterministic (no reliance on thread-startup overlap,
        which CPython/Windows does not guarantee and which would let both win a *non-race*).
        """
        inst = _make_warm_instance()
        pool = _make_pool(inst)
        engine = _make_engine(pool)
        _clear_all()

        results = {}
        errors = []
        barrier = threading.Barrier(2)

        def worker(rid):
            try:
                got = engine._acquire_reusable_system_agent(
                    agent_class='Security', instance_name=REUSE_NAME, task=f't{rid}', caller='Maine', rid=rid)
                results[rid] = got  # expected None (the claim is held by someone else)
            except Exception as e:  # noqa: BLE001
                errors.append(f'{rid}: {e}')

        def gated(fn, rid):
            barrier.wait()
            fn(rid)

        _clear_all()
        try_claim(REUSE_NAME, 'holder')  # an external owner holds it across the run
        t1 = threading.Thread(target=gated, args=(worker, 'rA'))
        t2 = threading.Thread(target=gated, args=(worker, 'rB'))
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        assert not errors, f'unexpected errors: {errors}'
        # Neither thread could reuse while the claim is held → both fall back to fresh spawn.
        assert results.get('rA') is None, 'a concurrent check must fall back while another owns it'
        assert results.get('rB') is None, 'a concurrent check must fall back while another owns it'

    def test_one_thread_wins_the_other_falls_back(self):
        """The real "exactly one owner" invariant: two threads race to acquire with NO pre-claim →
        exactly ONE gets (inst, True) and the OTHER gets None.

        Deterministic: try_claim is atomic under the registry RLock, so whichever thread reaches it
        first wins and the other MUST get None — regardless of whether CPython/Windows actually lets
        them overlap or runs them serially. To model the production lifecycle faithfully (the claim is
        held across a check's ENTIRE run, released only in the caller's finally AFTER both racing
        checks have tried to acquire), we use a second barrier: every thread blocks after its acquire
        until BOTH acquires are done, and only then releases. This prevents the winner from freeing
        the claim before the loser has raced — which is exactly what production does not do.
        """
        inst = _make_warm_instance()
        pool = _make_pool(inst)
        engine = _make_engine(pool)

        results = {}
        errors = []
        start_barrier = threading.Barrier(2)   # both threads ready before racing
        release_barrier = threading.Barrier(2)  # hold claims until BOTH acquires complete

        def gated(rid):
            start_barrier.wait()
            try:
                got = engine._acquire_reusable_system_agent(
                    agent_class='Security', instance_name=REUSE_NAME, task=f't{rid}', caller='Maine', rid=rid)
                results[rid] = got  # winner: (inst, True); loser: None
            except Exception as e:  # noqa: BLE001
                errors.append(f'{rid}: {e}')
            release_barrier.wait()  # hold the claim until the other thread has also tried to acquire
            # Model the caller's finally: whoever won holds the claim across its run, then releases.
            # BUG_0029 Phase 3: a winner is now a 3-tuple (inst, is_reuse, name), not a 2-tuple.
            if isinstance(results.get(rid), tuple) and len(results[rid]) == 3:
                release_claim(REUSE_NAME, rid)

        _clear_all()
        t1 = threading.Thread(target=gated, args=('rA',))
        t2 = threading.Thread(target=gated, args=('rB',))
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        assert not errors, f'unexpected errors: {errors}'
        a, b = results.get('rA'), results.get('rB')
        # Exactly one winner (a 3-tuple), exactly one loser (None).
        winners = [r for r in (a, b) if isinstance(r, tuple) and len(r) == 3]
        losers = [r for r in (a, b) if r is None]
        assert len(winners) == 1, f'expected exactly one winner, got {len(winners)}: a={a!r} b={b!r}'
        assert len(losers) == 1, f'expected exactly one loser (None), got {len(losers)}: a={a!r} b={b!r}'
        # The winner must be the SAME warm object (reuse, not a fresh spawn).
        win_inst, was_reused, win_name = winners[0]
        assert was_reused is True
        assert win_inst is inst, 'the winning acquire must return the shared warm instance'
        # After both threads release, no claim may leak.
        assert claim_holder(REUSE_NAME) is None, 'no leaked owner after both acquires complete'

    def test_no_interleaved_conversation(self):
        """A losing thread must NOT have mutated the warm instance's conversation."""
        inst = _make_warm_instance()
        pool = _make_pool(inst)
        engine = _make_engine(pool)
        _clear_all()

        # Pre-claim so BOTH threads lose → neither may mutate.
        try_claim(REUSE_NAME, 'holder')
        barrier = threading.Barrier(2)
        done = []

        def worker(rid):
            barrier.wait()
            got = engine._acquire_reusable_system_agent(
                agent_class='Security', instance_name=REUSE_NAME, task=f'task_{rid}', caller='Maine', rid=rid)
            done.append((rid, got))

        t1 = threading.Thread(target=worker, args=('rA',))
        t2 = threading.Thread(target=worker, args=('rB',))
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        assert all(got is None for _, got in done), 'both must fall back when claim is held'
        # The warm instance's conversation must be untouched (still just the system message).
        assert len(inst.conversation) == 1
        assert inst.conversation[0].role == SYSTEM


# ── G. Cache-behavior proof ───────────────────────────────────────────────────


class TestCacheBehavior:
    def test_system_message_byte_identical_across_reuse(self):
        """Across a reuse the system message bytes are identical (the KV-cache prefix)."""
        inst = _make_warm_instance()
        engine = _make_engine(_make_pool(inst))
        before_bytes = inst.conversation[0].content.encode('utf-8')
        got = _acquire(engine, task='first check')
        assert got is not None
        mid_bytes = inst.conversation[0].content.encode('utf-8')
        # Simulate a second reuse on the same warm instance.
        release_claim(REUSE_NAME, 'r1')
        got2 = engine._acquire_reusable_system_agent(
            agent_class='Security', instance_name=REUSE_NAME, task='second check', caller='Maine', rid='r2')
        assert got2 is not None
        after_bytes = inst.conversation[0].content.encode('utf-8')
        assert before_bytes == mid_bytes == after_bytes

    def test_state_label_cleared_for_warm_kv_branch(self):
        """_state_label must be cleared so _setup_turn takes the [STATE_RESTORE_SKIP] branch.

        The engine's warm-KV-cache branch (core.py ~1541) is entered when _slot_release is None
        OR there is no saved state label. After a reuse both hold: _slot_release is None and
        _state_label is None — so the next turn keeps the warm KV cache instead of restoring.
        """
        inst = self._dirty_state_for_kv()
        engine = _make_engine(_make_pool(inst))
        got = _acquire(engine)
        assert got is not None
        # The exact condition the engine checks for the [STATE_RESTORE_SKIP] branch:
        warm_kv_branch = (inst._slot_release is None) or (inst._state_label is None)
        assert inst._state_label is None, '_state_label must be cleared after reuse'
        assert inst._slot_release is None, 'a reused instance holds no leaked slot permit'
        assert warm_kv_branch is True

    @staticmethod
    def _dirty_state_for_kv():
        inst = _make_warm_instance()
        inst.append_message(Message(role=USER, content='old task'))
        inst._state_label = 'stale-label'  # would force a state restore if not cleared
        return inst


# ════════════════════════════════════════════════════════════════════════════
#  V2 METADATA-FIX TESTS (plan §8, T1–T7)
#
#  These prove the v2 delta: conversation[0] is byte-identical across checks whose
#  caller differs (the exact regression that made v1 dead code), by suppressing the two
#  volatile run-identity lines for stable classes and relocating them into the Security
#  task message only.
# ════════════════════════════════════════════════════════════════════════════


def _seeded_system_prompt(pool, inst):
    """The exact bytes that would be injected as conversation[0]'s metadata block for `inst`.

    Drives the REAL _build_session_metadata (the single source of truth for what a stable
    class's system prompt contains), so this is the authoritative check on suppression.
    """
    from agent_cascade.engine.helpers import _build_session_metadata
    return _build_session_metadata(pool, inst)


def _real_system_prompt_content(pool, name=REUSE_NAME):
    """The GENUINE conversation[0] bytes a fresh Security instance would carry (plan §8 T1).

    Reproduces the real creation path: lifecycle.build_system_message (template identity line) +
    _inject_metadata_into_message (which embeds the REAL _build_session_metadata block). This is
    what makes the byte-identity test meaningful — it exercises the actual metadata builder, so it
    would FAIL if a volatile run-identity line were ever re-introduced for a stable class.
    """
    from agent_cascade.lifecycle_manager import AgentLifecycleManager, _inject_metadata_into_message
    from agent_cascade.agent_instance import AgentInstance

    # build_system_message reads template.base_system_message; the fake pool's get_template returns
    # a MagicMock (truthy) whose .base_system_message is not a str, so stub it with a real string.
    # llm.generate_cfg MUST be a real dict: propagate_settings copies it into conversation[0] and a
    # MagicMock would leak its repr (with a memory address) into the prompt — nondeterministic bytes.
    template = MagicMock()
    template.base_system_message = 'You are a security reviewer.\nCheck the command for safety.'
    template.system_message = template.base_system_message
    template.llm.generate_cfg = {}
    pool.get_template.return_value = template

    # A throwaway instance carrying only what _build_session_metadata reads (agent_class, name, parent).
    inst = AgentInstance(instance_name=name, agent_class='Security',
                         conversation=[], created_at=time.monotonic(), last_activity=time.monotonic(),
                         latest_marker_index=-1)
    sys_msg = AgentLifecycleManager(pool).build_system_message('Security', name)
    _inject_metadata_into_message(sys_msg, pool, inst)
    return sys_msg.content


def run_security_check(caller='Maine', rid='op_aaa'):
    """T1 harness: drive the REAL acquire path for one Security check and return (instance, was_reused).

    A fresh warm instance is seeded into a fake pool, then engine._acquire_reusable_system_agent is
    invoked directly — the real reuse path. Because the caller owns the claim across its run (it is
    only released in the caller's finally), we release it after each check so the NEXT check can
    reuse the same warm object. A fresh-spawn-only harness would pass trivially; this one proves the
    second check actually reuses the first check's instance (was_reused is True).

    conversation[0] is seeded with the GENUINE system-prompt bytes (build_system_message +
    _inject_metadata_into_message → real _build_session_metadata), NOT a placeholder — so the
    byte-identity assertion exercises the actual metadata builder and would fail if a volatile line
    were re-introduced for a stable class.
    """
    inst = _make_warm_instance(REUSE_NAME)
    pool = _make_pool(inst)
    # Stub the workspace config as REAL values: _build_session_metadata does str(om.base_dir) and
    # str() of a MagicMock yields a memory-address repr, which would make conversation[0] differ per
    # process. In production these are real path strings (deterministic), so we mirror that here.
    pool.operation_manager.base_dir = '/tmp/reuse'
    pool.operation_manager.extra_work_folders_ro = []
    pool.operation_manager.extra_work_folders_rw = []
    # Seed conversation[0] with the real builder's output (the acquire path keeps this object as-is).
    inst.conversation[0].content = _real_system_prompt_content(pool, REUSE_NAME)
    engine = _make_engine(pool)
    _clear_all()
    got = engine._acquire_reusable_system_agent(
        agent_class='Security', instance_name=REUSE_NAME, task=f'check {rid}', caller=caller, rid=rid)
    assert isinstance(got, tuple) and len(got) == 3, f'acquire must return (inst, True, name), got {got!r}'
    inst_out, was_reused, _name = got
    # Model the caller's finally: release so a subsequent check can reuse the same warm object.
    release_claim(REUSE_NAME, rid)
    return inst_out, was_reused


class TestT1ByteIdentityAcrossCallers:
    """THE decisive regression test (plan §8 T1). v1 shipped as dead code because every test used a
    single fixed caller, so cross-caller volatility in '- Supervisor: {name} ({log})' was invisible.
    """

    def test_system_prompt_byte_identical_across_different_callers(self):
        a_inst, a_reused = run_security_check(caller='Maine', rid='op_aaa')
        b_inst, b_reused = run_security_check(caller='Orchestrator', rid='op_bbb')
        # The second check MUST have reused the warm instance (not two independent fresh spawns).
        assert a_reused is True and b_reused is True
        # Assert on CONTENT BYTES, not a substring — this is what v1's tests failed to do.
        assert a_inst.conversation[0].content == b_inst.conversation[0].content

    def test_seeded_prompt_has_no_caller_volatility(self):
        """Direct proof: the metadata block for a stable class contains neither the supervisor name
        nor the own-log-path line, so it cannot differ across callers."""
        inst = _make_warm_instance(REUSE_NAME)
        pool = _make_pool(inst)
        block = _seeded_system_prompt(pool, inst)
        assert '- Supervisor:' not in block, 'supervisor line must be suppressed for stable classes'
        assert '- Your log Path:' not in block, 'own-log-path line must be suppressed for stable classes'
        # The caller name must not leak into the prompt at all.
        assert 'Maine' not in block and 'Orchestrator' not in block

    def test_property_sweep_callers_and_rids(self):
        """Sweep ≥5 caller names × ≥2 request ids — every check's conversation[0] must be byte-identical."""
        ref = None
        for i, caller in enumerate(['Maine', 'Orchestrator', 'Ponytail', 'Researcher', 'Coder']):
            for rid in (f'op_{i}_a', f'op_{i}_b'):
                inst, reused = run_security_check(caller=caller, rid=rid)
                assert reused is True
                if ref is None:
                    ref = inst.conversation[0].content
                else:
                    assert inst.conversation[0].content == ref, (
                        f'conversation[0] drifted for caller={caller} rid={rid}')

    def test_harness_exercises_real_builder_and_detects_volatility(self):
        """Self-check that the T1 harness is meaningful (plan §8 T1 'quick self-check').

        Two properties:
          (a) The seeded conversation[0] is the GENUINE builder output — it contains a real
              '## Session Metadata' block with a System line, NOT the old placeholder. If this
              regresses to a placeholder, test_system_prompt_byte_identical... passes trivially and
              proves nothing.
          (b) The harness WOULD fail if a volatile caller-specific line were present: manually
              appending one makes the two callers' prompts differ. This proves the byte-identity
              assertion is sensitive to exactly the regression v1 shipped with.
        """
        # (a) real builder output, not a placeholder
        inst = _make_warm_instance(REUSE_NAME)
        pool = _make_pool(inst)
        real = _real_system_prompt_content(pool, REUSE_NAME)
        assert '## Session Metadata' in real, 'seeded prompt must be the real builder output'
        assert '- System:' in real, 'real builder output carries a System line'
        assert real != 'SYS_PROMPT_PREFIX', 'harness must not seed a placeholder'

        # (b) appending a volatile caller-specific line makes the prompts differ → assertion would fail
        volatiled_a = real + '\n- Supervisor: Maine (maine.jsonl)'
        volatiled_b = real + '\n- Supervisor: Orchestrator (orchestrator.jsonl)'
        assert volatiled_a != volatiled_b, (
            'the byte-identity assertion must be able to detect a re-introduced volatile line')


class TestT2SuppressionCorrectness:
    """plan §8 T2 — suppression must be exactly two lines and nothing more."""

    def _pool_with_workspace(self, agent_class):
        inst = AgentInstance(
            instance_name=f'{agent_class}_t2',
            agent_class=agent_class,
            conversation=[Message(role=SYSTEM, content='x')],
            created_at=time.monotonic(), last_activity=time.monotonic(),
            latest_marker_index=-1)
        pool = _make_pool(inst)
        # A real operation_manager so Working Dir / Extra Paths resolve to known values.
        om = MagicMock()
        om.base_dir = '/tmp/t2'
        om.extra_work_folders_ro = ['/ro/a']
        om.extra_work_folders_rw = ['/rw/b']
        pool.operation_manager = om
        return inst, pool

    def test_stable_classes_suppress_both_identity_lines(self):
        for agent_class in ('Security', 'Compressor'):
            inst, pool = self._pool_with_workspace(agent_class)
            block = _seeded_system_prompt(pool, inst)
            assert '- Supervisor:' not in block
            assert '- Your log Path:' not in block

    def test_non_stable_class_keeps_both_identity_lines(self):
        """Guards the existing test_session_metadata_fix.py behaviour from regressing the other way."""
        inst, pool = self._pool_with_workspace('orchestrator')
        block = _seeded_system_prompt(pool, inst)
        assert '- Supervisor:' in block
        assert '- Your log Path:' in block

    def test_stable_class_retains_non_identity_lines(self):
        """Suppression must not over-reach: System / Working Dir / Extra Paths stay for stable classes."""
        inst, pool = self._pool_with_workspace('Security')
        block = _seeded_system_prompt(pool, inst)
        assert '- System:' in block
        assert '- Working Dir: /tmp/t2' in block
        assert '- Extra Paths (Read-Only): /ro/a' in block
        assert '- Extra Paths (Read-Write): /rw/b' in block


class TestT3RelocationCorrectness:
    """plan §8 T3 — the run identity is an inline "(log path …)" on the Context line in conversation[1], for Security only."""

    def test_security_task_message_contains_run_identity(self):
        """BUG_0029 Phase 3: the run identity is now an INLINE "(log path …)" on the Context line,
        replacing the v2 appended "## Run Identity" block. The supervisor's FULL log path is still
        carried in conversation[1]; Security's own log path is still omitted."""
        inst = _make_warm_instance(REUSE_NAME)
        pool = _make_pool(inst)
        # Give the caller a REAL, stable log path so we can assert on its exact bytes (a MagicMock
        # auto-attr would yield a per-process memory-address repr).
        sup_log = 'N:/work/WD/AgentWorkspace/logs/orchestrator_Maine_20261001.jsonl'
        pool.instance_loggers.get.return_value.log_path = sup_log
        engine = _make_engine(pool)
        got = _acquire(engine, task='check', caller='Maine')
        assert got is not None
        task_msg = inst.conversation[1]
        # The v2 appended block is GONE.
        assert '## Run Identity' not in task_msg.content
        # The inline rewrite carries the supervisor's FULL log path, anchored right after the
        # "This is a message from Maine." sentence (the exact build_task_message shape).
        assert '(log path ' in task_msg.content
        assert sup_log in task_msg.content
        assert task_msg.content.startswith('Context: This is a message from Maine. (log path ')
        # Security is short/non-compressive — it never reads back its own history, so the self
        # log-path line is still omitted entirely (in both conversation[0] and conversation[1]).
        assert '- Your log Path:' not in task_msg.content
        assert '- Your log Path:' not in inst.conversation[0].content

    def test_compressor_task_message_has_no_run_identity(self):
        """Compressor gets NO relocation — the call site gates _with_run_identity on Security only.

        Both task sites (fresh spawn in _create_system_agent and reuse in _acquire_reusable_system_agent)
        must carry that gate, so a Compressor's conversation[1] never gains a ## Run Identity block.
        """
        import inspect
        from agent_cascade.engine import core as core_mod
        for method_name in ('_create_system_agent', '_acquire_reusable_system_agent'):
            src = inspect.getsource(getattr(core_mod.ExecutionEngine, method_name))
            assert "agent_class == 'Security'" in src, (
                f'{method_name} must gate the ## Run Identity relocation on Security only')

    def test_compressor_fresh_spawn_task_message_has_no_run_identity(self):
        """Behavioral (plan §8 T3): drive the REAL fresh-spawn path for a Compressor and assert its
        conversation[1] carries NO '## Run Identity' block — the gate must be Security-only in practice,
        not just in source. A Compressor's run-identity is never relocated anywhere."""
        from agent_cascade.agent_instance import AgentInstance

        # Seed an empty warm slot so _make_pool's fake pool exposes a real instances dict + lifecycle.
        seed = AgentInstance(instance_name='Compressor', agent_class='Compressor',
                             conversation=[], created_at=time.monotonic(), last_activity=time.monotonic(),
                             latest_marker_index=-1)
        pool = _make_pool(seed)
        # find_or_create_instance calls pool._resolve_instance_name; stub it to return the name verbatim.
        pool._resolve_instance_name.side_effect = lambda n: n
        engine = _make_engine(pool)

        inst = engine._create_system_agent(
            agent_class='Compressor', instance_name='Compressor', task='compress this log', caller='Maine')
        assert inst is not None and inst.agent_class == 'Compressor'
        # conversation[1] is the task message — it must NOT have gained a run-identity block.
        assert len(inst.conversation) >= 2, 'fresh spawn must produce [system, task]'
        assert '## Run Identity' not in inst.conversation[1].content, (
            "Compressor's task message must NOT contain the relocated ## Run Identity block")
        # And it must not have leaked into conversation[0] either.
        assert '## Run Identity' not in inst.conversation[0].content


# ── T3b. Inline-context rewrite (BUG_0029 Phase 3, plan §5.2) ────────────────
# The v2 appended "## Run Identity" block is replaced by an inline "(log path …)" on the Context
# line, anchored on the EXACT sentence build_task_message emits. These tests exercise _with_run_identity
# directly (the pure builder), so they are deterministic and independent of the acquire path.


def _ri_pool(log_path):
    """A minimal pool whose supervisor log path resolves to a known string."""
    pool = MagicMock()
    pool.instance_loggers.get.return_value.log_path = log_path
    return pool


class TestT3InlineContextRewrite:
    """plan §5.2 — the inline "(log path …)" rewrite, not an appended block."""

    def test_security_context_line_carries_full_log_path(self):
        sup_log = 'N:/work/WD/AgentWorkspace/logs/orchestrator_Maine_20261001.jsonl'
        pool = _ri_pool(sup_log)
        msg = Message(role=USER, content=(
            'Context: This is a message from Maine.\n\nTask: do a thing\n\nPlease help with this task.'))
        out = _with_run_identity(pool, msg, caller='Maine')
        assert '(log path ' in out.content
        assert sup_log in out.content  # FULL path, not a basename
        assert '## Run Identity' not in out.content  # block is GONE
        assert out.content.startswith('Context: This is a message from Maine. (log path ')

    def test_with_run_identity_marker_missing_returns_unchanged(self):
        """Unrecognised content → returned unchanged, never mangled (best-effort)."""
        pool = _ri_pool('N:/logs/some.jsonl')
        msg = Message(role=USER, content='Context: something else entirely\n\nTask: t')
        out = _with_run_identity(pool, msg, caller='Maine')
        assert out.content == msg.content  # byte-identical, no insertion

    def test_with_run_identity_handles_dotted_caller_name(self):
        """Regression for the v1 period-landing bug: 'John.Doe' must insert AFTER 'John.Doe.', not
        after 'John.' (which produced '...from John. (log path …)Doe.')."""
        caller = 'John.Doe'
        sup_log = 'N:/logs/orchestrator_2026.jsonl'
        pool = _ri_pool(sup_log)
        raw = 'Context: This is a message from John.Doe.\n\nTask: t\n\nPlease help with this task.'
        out = _with_run_identity(pool, Message(role=USER, content=raw), caller=caller)
        assert out.content == (
            f'Context: This is a message from John.Doe. (log path {sup_log})\n\nTask: t\n\n'
            'Please help with this task.')

    def test_with_run_identity_non_empty_context_shape(self):
        """Both build_task_message shapes must get the inline path (lifecycle_manager.py:330-331)."""
        sup_log = 'N:/logs/orchestrator_2026.jsonl'
        pool = _ri_pool(sup_log)
        raw = ('Context: This is a message from Maine.\nExtra tool args here.\n\nTask: t\n\n'
               'Please help with this task.')
        out = _with_run_identity(pool, Message(role=USER, content=raw), caller='Maine')
        assert out.content.startswith('Context: This is a message from Maine. (log path ')
        # The context body join is preserved verbatim.
        assert '\nExtra tool args here.\n\nTask: t' in out.content
        assert '## Run Identity' not in out.content

    def test_compressor_task_message_has_no_log_path(self):
        """Compressor never routes through _with_run_identity → its task text has no '(log path'.
        Drive the REAL fresh-spawn path for a Compressor and assert the inline path is absent."""
        from agent_cascade.agent_instance import AgentInstance
        sup_log = 'N:/logs/orchestrator_2026.jsonl'
        seed = AgentInstance(instance_name='Compressor', agent_class='Compressor',
                             conversation=[], created_at=time.monotonic(), last_activity=time.monotonic(),
                             latest_marker_index=-1)
        pool = _make_pool(seed)
        pool.instance_loggers.get.return_value.log_path = sup_log
        pool._resolve_instance_name.side_effect = lambda n: n
        engine = _make_engine(pool)

        inst = engine._create_system_agent(
            agent_class='Compressor', instance_name='Compressor', task='compress this log', caller='Maine')
        assert inst is not None and inst.agent_class == 'Compressor'
        assert '(log path ' not in inst.conversation[1].content, (
            "Compressor's task message must NOT carry the Security-only inline log path")

    def test_build_task_message_generic_output_unchanged(self):
        """The generic builder is byte-unaffected: build_task_message still yields the exact old string
        for a non-Security caller (no '(log path' injected here — that's _with_run_identity's job)."""
        from agent_cascade.lifecycle_manager import AgentLifecycleManager
        pool = MagicMock()
        lifecycle = AgentLifecycleManager(pool)
        msg = lifecycle.build_task_message(
            {'task': 'do a thing', 'context': ''}, caller='Maine')
        assert msg.content == (
            'Context: This is a message from Maine.\n\nTask: do a thing\n\nPlease help with this task.')
        assert '(log path ' not in msg.content


class TestT4ParserIsolation:
    """plan §8 T4 — a leading "## Run Identity" block must not break verdict parsing or .format() paths."""

    def test_verdict_parse_ignores_run_identity_block(self):
        """A task body containing a (synthetic legacy) "## Run Identity" block still yields a parseable [VERDICT]."""
        from agent_cascade.advisor_runner import _VERDICT_RE
        # Synthetic block mirrors the current shape: a single "<caller>'s log file:" line with a
        # full path (the self log-path line was removed for Security). Verdict parsing must still
        # find [VERDICT] when this block precedes it.
        final = ('## Run Identity\n- Maine\'s log file: /logs/orch.jsonl\n\n[VERDICT] APPROVE')
        m = _VERDICT_RE.search(final)
        assert m is not None, 'verdict regex must still match when the run-identity block precedes it'
        assert m.group(1).upper() == 'APPROVE'

    def test_task_body_with_braces_does_not_raise_in_format(self):
        """A task body containing literal { } must not raise in the .format()-adjacent path.

        SECURITY_ADVISOR_PROMPT is a template with named placeholders; a brace-laden description is
        passed as an ARGUMENT value, so it is substituted verbatim and never re-interpreted as a
        placeholder (which would raise KeyError/IndexError).
        """
        from agent_cascade.prompts.dna import SECURITY_ADVISOR_PROMPT
        try:
            out = SECURITY_ADVISOR_PROMPT.format(
                tool_name='shell_cmd', description='run {rm -rf /} with braces',
                arguments='{}', os_info='Windows', workspace_info='/tmp')
            assert 'run {rm -rf /} with braces' in out
        except (KeyError, IndexError) as e:  # noqa: BLE001
            raise AssertionError(f'.format() raised on brace-laden description: {e}')


class TestT5ClaimReleaseOnFailure:
    """plan §8 T5 — one test per failure path; the registry flag must be cleared in all of them.

    The claim is taken inside _acquire_reusable_system_agent and released in its finally on every
    non-success exit (flag pattern). Each case here forces a distinct failure AFTER the claim is
    held and asserts no owner leaks.
    """

    def _acquire(self, inst, **kw):
        pool = _make_pool(inst)
        engine = _make_engine(pool)
        defaults = dict(agent_class='Security', instance_name=REUSE_NAME, task='x', caller='Maine', rid='r1')
        defaults.update(kw)
        _clear_all()
        return engine._acquire_reusable_system_agent(**defaults), pool

    def test_frozen_false_releases_claim(self):
        """Eligibility gate 8b (V2): an unfrozen instance falls back and releases the claim."""
        inst = _make_warm_instance()
        inst._system_prompt_frozen = False
        got, _ = self._acquire(inst)
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_running_state_releases_claim(self):
        inst = _make_warm_instance()
        with inst._state_lock:
            inst.state = AgentState.RUNNING
        got, _ = self._acquire(inst)
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_slot_held_releases_claim(self):
        inst = _make_warm_instance()
        inst._slot_release = lambda: None  # leaked permit → not quiescent
        got, _ = self._acquire(inst)
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_lock_held_releases_claim(self):
        """Compression lock already held by another run → non-blocking acquire fails → release.

        _compression_lock is an RLock (re-entrant), so the same thread could re-acquire it and mask
        the "held" state. Swap in a plain non-reentrant Lock to model another thread holding it.
        """
        inst = _make_warm_instance()
        real_lock = inst._compression_lock
        held = threading.Lock()
        held.acquire()  # simulate a live compressor on another thread holding it
        inst._compression_lock = held
        try:
            got, _ = self._acquire(inst)
        finally:
            inst._compression_lock = real_lock
            held.release()
        assert got is None
        assert claim_holder(REUSE_NAME) is None

    def test_exception_during_reset_releases_claim(self):
        """An exception raised AFTER the claim is held (mid-reset) must still release it."""
        inst = _make_warm_instance()
        pool = _make_pool(inst)
        engine = _make_engine(pool)
        # Force the exception in the reset block: make rebuild_conversation raise.
        with patch.object(AgentInstance, 'rebuild_conversation', side_effect=RuntimeError('reset boom')):
            got, _ = self._acquire(inst)
        assert got is None
        assert claim_holder(REUSE_NAME) is None, 'exception path must release the claim (no leak)'


class TestT6BootstrapGapRegression:
    """plan §8 T6 — bootstrap-gap bugfixes: seed-only-into-empty-slot, claim-gated create-or-reuse,
    3-tuple return unpacking."""

    def test_acquire_returns_three_tuple(self):
        """BUG_0029 Phase 3: acquire returns a 3-tuple (inst, is_reuse, name) so the real instance
        name can be threaded to the caller's cleanup/telemetry (the v2 2-tuple could not)."""
        inst = _make_warm_instance()
        got = _acquire(_make_engine(_make_pool(inst)))
        assert isinstance(got, tuple) and len(got) == 3, f'expected a 3-tuple, got {got!r}'
        result_inst, was_reused, name = got
        assert result_inst is inst and was_reused is True
        assert name == REUSE_NAME

    def test_acquire_security_agent_two_branch(self):
        """acquire_security_agent: reuse hit → (inst, True, name); miss → fresh spawn, (inst, False, fallback)."""
        from agent_cascade.security_reuse import acquire_security_agent
        warm = _make_warm_instance(REUSE_NAME)
        engine = MagicMock()
        # Reuse hit: a real 3-tuple. The name is the fixed reuse name the caller passed in.
        engine._acquire_reusable_system_agent.return_value = (warm, True, REUSE_NAME)
        inst, reused, name = acquire_security_agent(
            engine, 'Security', REUSE_NAME, f'Security_{REUSE_NAME}', 't', 'Maine', 'r1')
        assert inst is warm and reused is True
        assert name == REUSE_NAME
        # Reuse miss → fresh spawn.
        fresh = MagicMock()
        engine._acquire_reusable_system_agent.return_value = None
        engine._create_system_agent.return_value = fresh
        inst2, reused2, name2 = acquire_security_agent(
            engine, 'Security', REUSE_NAME, f'Security_{REUSE_NAME}', 't', 'Maine', 'r1')
        assert inst2 is fresh and reused2 is False
        assert name2 == f'Security_{REUSE_NAME}'
        engine._create_system_agent.assert_called_once()

    def test_acquire_security_agent_ignores_magicmock(self):
        """A MagicMock return (un-stubbed test mock) must be treated as a miss, not a hit."""
        from agent_cascade.security_reuse import acquire_security_agent
        fresh = MagicMock()
        engine = MagicMock()
        engine._acquire_reusable_system_agent.return_value = MagicMock()  # auto-spec stand-in
        engine._create_system_agent.return_value = fresh
        inst, reused, name = acquire_security_agent(
            engine, 'Security', REUSE_NAME, f'Security_{REUSE_NAME}', 't', 'Maine', 'r1')
        assert inst is fresh and reused is False
        assert name == f'Security_{REUSE_NAME}'


class TestT7KillSwitch:
    """plan §8 T7 — with the kill-switch off, behaviour is byte-identical to pre-feature HEAD."""

    def test_disabled_never_calls_acquire(self):
        """SECURITY_REUSE_ENABLED=False → acquire never called; legacy per-rid spawn used."""
        pool = _make_integration_pool()
        handler = _make_handler(pool)
        fresh_inst = MagicMock()
        fresh_inst.conversation = []
        engine_instance = MagicMock()
        engine_instance._create_system_agent.return_value = fresh_inst
        engine_instance.run.return_value = iter([(' [YES] safe', False)])

        with patch('agent_cascade.settings.SECURITY_REUSE_ENABLED', False):
            TestSecurityHandlerIntegration()._run_check(handler, pool, engine_instance, 'rid_kill')

        engine_instance._acquire_reusable_system_agent.assert_not_called()
        call_kwargs = engine_instance._create_system_agent.call_args.kwargs
        assert call_kwargs['instance_name'] == f'Security_rid_kill'


# ── E2E: real ExecutionEngine, real acquire logic (plan §5.3a) ────────────────


def _make_pool_empty():
    """A fake pool with a REAL empty instances dict, plus everything _execute_check and
    _acquire_reusable_system_agent both touch. Extends _make_integration_pool (handler-side needs)
    with _make_pool's real-engine wiring. `pool.instances = {}` is the load-bearing line — a MagicMock
    auto-attr .get() returns truthy and would short-circuit Gate 2."""
    pool = _make_integration_pool()
    pool.instances = {}  # REAL empty dict, NOT a MagicMock attribute
    pool.message_queues = {}
    pool._execution = MagicMock()
    pool._execution._state_lock = threading.RLock()  # RLock, not Lock (Gate 11)
    pool._execution.active_stack = []
    pool.is_instance_halted.return_value = False
    pool.is_instance_terminated.return_value = False
    from agent_cascade.lifecycle_manager import AgentLifecycleManager
    pool.lifecycle = AgentLifecycleManager(pool)  # real builder → real Message objects
    return pool


class TestReuseEndToEnd:
    """THE decisive e2e proof (plan §5.3a): two sequential shell_cmd checks, REAL engine.
    Check #1 SEEDS Security_reuse via the real seed branch; check #2 REUSES the same object.
    Patches the FACTORY (ExecutionEngine(self.agent_pool)), NOT _acquire_reusable_system_agent — that
    is what keeps the acquire logic real."""

    def _one_check(self, handler, pool, engine, rid):
        ap = _make_ap(rid)
        with patch('agent_cascade.execution_engine.ExecutionEngine',
                   MagicMock(return_value=engine)), \
             patch('agent_cascade.settings.SECURITY_REUSE_ENABLED', True):
            handler._execute_check(
                ap=ap, sec_inst=None, rid=rid, auto_apply=True, instance_name='Maine',
                caller_agent='Maine', prompt_template='Test {tool_name}',
                timeout_seconds=3600, warning_seconds=2400)

    def test_two_shell_cmd_checks_seed_then_reuse(self):
        from agent_cascade.security_reuse import _clear_all, claim_holder
        _clear_all()

        pool = _make_pool_empty()
        handler = _make_handler(pool)
        engine = _make_engine(pool)  # REAL ExecutionEngine

        # Leaf stub #1: the create leaf. A real _create_system_agent would need an LLM/slots/WebUI;
        # we replace only this, and make it FAITHFUL by registering the instance in pool.instances —
        # which is what find_or_create_instance really does. Without the registration,
        # check #2 would see an empty slot and miss the very reuse we are proving.
        def _fake_create(**kw):
            name = kw['instance_name']
            inst = _make_warm_instance(name)
            inst.restricted_shell = True  # _create_system_agent sets this for system agents
            pool.instances[name] = inst
            return inst
        engine._create_system_agent = MagicMock(side_effect=_fake_create)

        # Leaf stubs #2 + #3: run the agent / slot bookkeeping, no live LLM. Fresh iterator per call
        # so check #2 cannot hit an exhausted generator.
        engine.run = MagicMock(side_effect=lambda *a, **k: iter([(' [YES] safe', False)]))
        engine.reacquire_for = MagicMock(return_value=True)
        engine._telemetry = MagicMock(return_value=None)

        # Check #1: pool.instances is EMPTY → the real acquire must take the SEED branch.
        self._one_check(handler, pool, engine, 'op_rid_A')

        assert engine._create_system_agent.call_count == 1, 'check #1 must seed exactly once'
        assert engine._create_system_agent.call_args.kwargs['instance_name'] == REUSE_NAME, (
            'the seed must create under the FIXED reuse name, not a per-rid name')
        seeded = pool.instances[REUSE_NAME]
        # _cleanup must not remove the seeded instance — it survives for check #2.
        assert seeded is not None and seeded.instance_name == REUSE_NAME
        assert not any(k.startswith('Security_op_') for k in pool.instances), (
            f'check #1 leaked a per-rid instance: {sorted(pool.instances)}')

        # Check #2: the real acquire must find REUSE_NAME, pass eligibility, and REUSE it.
        self._one_check(handler, pool, engine, 'op_rid_B')

        assert engine._create_system_agent.call_count == 1, (
            'check #2 must NOT create anything — the seed is not being reused')
        assert pool.instances[REUSE_NAME] is seeded, 'check #2 must reuse the SAME object'
        # THE assertion: engine.run was handed the same instance object both times.
        ran = [c.args[0] for c in engine.run.call_args_list]
        assert len(ran) == 2 and ran[0] is seeded and ran[1] is seeded, (
            f'check #2 did not run the seeded instance: {ran}')
        # No per-rid key appeared on check #2 (and none at all).
        assert not any(k.startswith('Security_op_') for k in pool.instances), (
            f'per-rid spawn detected: {sorted(pool.instances)}')
        assert claim_holder(REUSE_NAME) is None, 'the finally must release the claim between checks'


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
