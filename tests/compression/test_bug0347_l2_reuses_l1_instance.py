"""Structural warm-instance reuse (L1 compression -> L2 consolidation).

Targets three invariants the prior fix did NOT cover:

1. The bounded wait (compressor_reuse.acquire_warm_consolidation) must fire on a
   TRANSIENT reject even when the warm claim is FREE. The old predicate
   ("claim_holder is not None") only waited on a HELD claim, so a miss whose reason
   was a still-reading warm instance (reader-in-flight) fresh-spawned a duplicate
   Compressor even though the warm slot was seconds from ready. The fix makes the wait
   reason-driven: retry on transient reasons, stop early on a permanent reason, stop at
   the deadline. These tests drive a REAL warm instance whose reader Event is unset
   (transient), so the warm claim is FREE on every acquire — exactly the case the old
   predicate gave up on.

2. WarmCompressorSession.begin_task primes a held instance with a new append-only task
   without releasing the claim, and refuses (degrades) once the claim is lost.

3. _consolidate_markers, when handed a threaded l1_session, uses it directly (no fresh
   warm acquire) and does NOT release it — the caller (compress_context) owns the single
   release, otherwise the claim would be dropped mid-consolidation / double-released.

No LLM server needed: transport is patched at the boundary.
"""
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_cascade.compressor_reuse as compressor_reuse  # noqa: E402
from agent_cascade.agent_instance import AgentInstance  # noqa: E402
from agent_cascade.compression.agent_invoker import COMPRESSOR_WARM_NAME  # noqa: E402
from agent_cascade.llm.schema import Message, SYSTEM  # noqa: E402
from agent_cascade.reuse_reason import is_transient  # noqa: E402


def _warm_compressor(name=COMPRESSOR_WARM_NAME):
    conversation = [Message(role=SYSTEM, content='COMPRESSOR_SYS_PREFIX')]
    inst = AgentInstance(
        instance_name=name,
        agent_class='Compressor',
        conversation=conversation,
        created_at=time.monotonic(),
        last_activity=time.monotonic() - 60.0,
        latest_marker_index=-1,
    )
    inst._system_prompt_frozen = True
    return inst


def _fake_pool(inst):
    """Minimal fake pool exposing exactly what the real reuse gate touches (mirrors
    tests/test_compressor_instance_reuse.py::_make_pool)."""
    pool = MagicMock()
    pool.instances = {inst.instance_name: inst} if inst else {}
    pool.message_queues = {}
    exec_obj = MagicMock()
    exec_obj._state_lock = threading.RLock()
    exec_obj.active_stack = []
    pool._execution = exec_obj
    pool.is_instance_halted.return_value = False
    pool.is_instance_terminated.return_value = False
    return pool


def _complete_after(delay, event):
    """Background helper: the warm instance's reader drains after ``delay`` (the in-flight
    condition resolves), making the warm slot eligible for a re-acquire."""
    time.sleep(delay)
    event.set()


class TestGate5TransientBoundedWait:
    """Regression: a FREE-claim transient reject must still be bounded-waited.

    Pre-fix, the wait was gated on ``claim_holder(name) is not None``. The real reuse gate
    releases its OWN claim on every internal miss, so a reader-in-flight miss leaves the claim
    FREE -> the old predicate decided "not transient" -> immediate fresh-spawn. Post-fix the
    decision is reason-driven and fires."""

    def setup_method(self):
        compressor_reuse._clear_all()

    def teardown_method(self):
        compressor_reuse._clear_all()

    def test_free_claim_transient_reject_bounded_wait_fires(self):
        # Real warm instance with its reader Event present-and-unset -> instance_llm_in_flight
        # True -> transient LIVE_LLM_STREAM miss on a FREE claim. The reader completes shortly;
        # the waiter must re-acquire the warm slot instead of fresh-spawning.
        warm = _warm_compressor()
        reader_done = threading.Event()
        warm._stream_reader_done = reader_done            # present-and-unset => in-flight
        pool = _fake_pool(warm)
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None  # free claim

        t = threading.Thread(target=_complete_after, args=(0.2, reader_done), daemon=True)
        t.start()
        with patch.object(compressor_reuse, 'COMPRESSOR_REUSE_ENABLED', True), \
                patch.object(compressor_reuse, 'COMPRESSOR_REUSE_CLAIM_WAIT_SECONDS', 3.0):
            t0 = time.monotonic()
            sess = compressor_reuse.acquire_warm_consolidation(
                pool, ['S1', 'S2'], 'Caller', marker_time_ranges=[(0.0, 1.0), (0.0, 1.0)])
            elapsed = time.monotonic() - t0
        t.join(timeout=2.0)

        assert sess is not None, 'a transient free-claim miss must be bounded-waited, not fresh-spawned'
        assert sess.actual_name == COMPRESSOR_WARM_NAME
        assert reader_done.is_set()                       # waited until the reader drained
        assert elapsed >= 0.15                             # the wait actually fired (not immediate)
        sess.release()
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None  # claim clean after success

    def test_free_claim_permanent_reject_stops_early(self):
        # A permanent reason must NOT consume the window: bail immediately, never re-acquire.
        warm = _warm_compressor()
        warm.agent_class = 'Admin'                          # not our class -> permanent
        pool = _fake_pool(warm)
        with patch.object(compressor_reuse, 'COMPRESSOR_REUSE_ENABLED', True), \
                patch.object(compressor_reuse, 'COMPRESSOR_REUSE_CLAIM_WAIT_SECONDS', 5.0):
            t0 = time.monotonic()
            sess = compressor_reuse.acquire_warm_consolidation(pool, ['S1'], 'Caller')
            elapsed = time.monotonic() - t0
        assert sess is None
        assert elapsed < 1.0                                # far under the 5s window

    def test_zero_window_byte_identity_free_claim(self):
        # Window 0 disables the wait entirely: even a transient reason returns immediately,
        # byte-identical to the pre-fix path.
        warm = _warm_compressor()
        warm._stream_reader_done = threading.Event()       # present-and-unset => in-flight
        pool = _fake_pool(warm)
        with patch.object(compressor_reuse, 'COMPRESSOR_REUSE_ENABLED', True), \
                patch.object(compressor_reuse, 'COMPRESSOR_REUSE_CLAIM_WAIT_SECONDS', 0.0):
            t0 = time.monotonic()
            sess = compressor_reuse.acquire_warm_consolidation(pool, ['S1'], 'Caller')
            elapsed = time.monotonic() - t0
        assert sess is None
        assert elapsed < 0.2                                # zero window -> no sleep

    def test_flag_off_never_waits(self):
        # Kill switch: reuse disabled -> acquire returns None with no engine touch, no wait.
        warm = _warm_compressor()
        warm._stream_reader_done = threading.Event()
        pool = _fake_pool(warm)
        with patch.object(compressor_reuse, 'COMPRESSOR_REUSE_ENABLED', False), \
                patch.object(compressor_reuse, 'COMPRESSOR_REUSE_CLAIM_WAIT_SECONDS', 5.0):
            t0 = time.monotonic()
            sess = compressor_reuse.acquire_warm_consolidation(pool, ['S1'], 'Caller')
            elapsed = time.monotonic() - t0
        assert sess is None
        assert elapsed < 0.2


class TestBeginTask:
    """begin_task primes a held instance for the next phase without releasing the claim."""

    def setup_method(self):
        compressor_reuse._clear_all()

    def teardown_method(self):
        compressor_reuse._clear_all()

    def _session(self, continue_return=True):
        warm = _warm_compressor()
        rid = 'bt_rid'
        assert compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, rid)
        engine = MagicMock()
        engine._continue_reusable_system_agent_turn.return_value = continue_return
        return compressor_reuse.WarmCompressorSession(
            warm, COMPRESSOR_WARM_NAME, rid, engine, _fake_pool(warm), 'Caller'), warm, engine

    def test_begin_task_primes_without_releasing_claim(self):
        session, warm, engine = self._session(continue_return=True)
        ok = session.begin_task('CONSOLIDATE THIS', 'Caller')
        assert ok is True
        assert engine._continue_reusable_system_agent_turn.call_count == 1
        # begin_task delegates the append to the engine turn; it must NOT release the claim.
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) == 'bt_rid'
        session.release()
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None

    def test_begin_task_refuses_when_claim_lost(self):
        # The claim-ownership / IDLE guard lives INSIDE _continue_reusable_system_agent_turn,
        # which returns None when we no longer own the claim or the instance is not IDLE. Mirror
        # that here (the warm slot was released externally -> the engine refuses the append).
        warm = _warm_compressor()
        rid = 'bt_rid'
        assert compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, rid)
        engine = MagicMock()
        engine._continue_reusable_system_agent_turn.return_value = None  # engine refuses
        session = compressor_reuse.WarmCompressorSession(
            warm, COMPRESSOR_WARM_NAME, rid, engine, _fake_pool(warm), 'Caller')
        compressor_reuse.release_claim(COMPRESSOR_WARM_NAME, rid)        # claim lost
        ok = session.begin_task('CONSOLIDATE THIS', 'Caller')
        assert ok is False

    def test_begin_task_degrades_on_engine_failure(self):
        session, warm, engine = self._session(continue_return=None)  # engine error -> None
        ok = session.begin_task('CONSOLIDATE THIS', 'Caller')
        assert ok is False

    def test_begin_task_refuses_after_self_release(self):
        # A session already released must not attempt any append (short-circuits on _released).
        session, warm, engine = self._session(continue_return=True)
        session.release()
        ok = session.begin_task('CONSOLIDATE THIS', 'Caller')
        assert ok is False
        engine._continue_reusable_system_agent_turn.assert_not_called()

    def test_transient_predicate_classifies_transient_reasons(self):
        # Sanity: the transient predicate used by the wait treats a reader-in-flight reason as
        # worth waiting on; guard against accidentally enumerating it as permanent.
        from agent_cascade.reuse_reason import ReuseRejectReason
        assert is_transient(ReuseRejectReason.LIVE_LLM_STREAM) is True
        assert is_transient(ReuseRejectReason.CLAIM_HELD) is True
        assert is_transient(ReuseRejectReason.CLASS_MISMATCH) is False
        assert is_transient(None) is False  # fail-closed on unknown


class TestConsolidateMarkersThreading:
    """The threaded l1_session is owned by compress_context: _consolidate_markers must
    neither re-acquire a fresh warm session nor release the threaded one."""

    def setup_method(self):
        compressor_reuse._clear_all()

    def teardown_method(self):
        compressor_reuse._clear_all()

    def test_threaded_session_skips_fresh_acquire_and_is_not_released(self):
        import agent_cascade.compression.core as _core
        from agent_cascade.agent_pool import AgentPool as _AgentPool
        from agent_cascade import settings as _settings

        pool = MagicMock()
        pool.stopped = False
        warm = _warm_compressor()
        rid = 'thread_rid'
        assert compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, rid)
        engine = MagicMock()
        engine._continue_reusable_system_agent_turn.return_value = True  # begin_task succeeds
        l1_session = compressor_reuse.WarmCompressorSession(
            warm, COMPRESSOR_WARM_NAME, rid, engine, pool, 'Caller')

        # Real target instance with enough history that the patched marker indices are valid.
        inst = _warm_compressor(name='target')
        for _ in range(10):
            inst.append_message(Message(role=SYSTEM, content='m'))
        pool.get_instance.return_value = inst
        real_release = compressor_reuse.release_claim

        with patch.object(compressor_reuse, 'acquire_warm_consolidation') as acq, \
                patch.object(_settings, 'COMPRESSION_CONSOLIDATION_THRESHOLD', 1), \
                patch.object(_core, 'select_markers_for_consolidation',
                             return_value=([2, 5], 10)), \
                patch.object(_core, 'extract_summary_from_marker', return_value='S1'), \
                patch.object(_core, 'get_marker_time_range', return_value=(0.0, 1.0)), \
                patch.object(_AgentPool, 'find_all_marker_indices', return_value=[2, 5, 8]), \
                patch.object(_core, '_validate_and_reconsolidate',
                             return_value=('CONSOLIDATED', '')), \
                patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                      return_value=('CONSOLIDATED', '')), \
                patch.object(compressor_reuse, 'release_claim', side_effect=real_release) as rel:
            _core._consolidate_markers(pool, 'target', l1_session=l1_session)

        acq.assert_not_called()                                # no fresh warm spawn (threaded)
        # The threaded session was primed once (begin_task -> engine continue), then left held.
        assert engine._continue_reusable_system_agent_turn.call_count == 1
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) == rid  # caller owns release
        rel.assert_not_called()                                # _consolidate_markers did not release it
        # The single owner (compress_context) releases exactly once.
        l1_session.release()
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None


class TestCompressContextSingleClaimRelease:
    """End-to-end (compress_context) regression for the load-bearing invariant:
    the single `finally` in compress_context MUST release the threaded warm claim on every
    exit, including the step-14-skip and step-14-exception cases that no helper-level test
    covers. This is the top-severity wedge: without it the warm Compressor claim leaks and
    every subsequent reuse is bounded-waited then fresh-spawned for the session's lifetime.

    Step 8 is patched at its return boundary to model the case it actually produces when the
    warm path served: a held WarmCompressorSession handed off as l1_session (keep_warm_session
    is True whenever dry_run is False). The LLM boundary for step 8/14 is never real — patched.

    Also folded here: when L1 reused the warm instance, step 14 (real
    _consolidate_markers threading) must NOT trigger any fresh Compressor spawn —
    `acquire_warm_consolidation` and the engine's `_create_system_agent` are spied and asserted
    to stay untouched."""

    _RID = 'cc_rid'

    def setup_method(self):
        compressor_reuse._clear_all()

    def teardown_method(self):
        compressor_reuse._clear_all()

    @staticmethod
    def _history():
        """[SYSTEM] + U0 + MARKER + 8 active — enough to drive a clean compress_context trim."""
        from agent_cascade.llm.schema import USER
        from agent_cascade.prompts.dna import COMPRESSION_MARKER
        msgs = [Message(role=SYSTEM, content='You are a test agent'),
                Message(role=USER, content='U0 initial prompt')]
        marker = f'{COMPRESSION_MARKER} (50% summarized) ---\nSummary: old stuff'
        msgs.append(Message(role=USER, content=marker))
        for i in range(4):
            msgs.append(Message(role=USER, content=f'Active user {i}'))
            msgs.append(Message(role='assistant', content=f'Active assistant {i}'))
        return msgs

    def _target_inst(self):
        inst = _warm_compressor(name='TestAgent')
        for _ in range(10):
            inst.append_message(Message(role=SYSTEM, content='m'))
        return inst

    def _run(self, *, step14, retry=False):
        """Drive the real compress_context. step14 ∈ {'run', 'skip', 'raise'} selects which
        exit path is taken between step 8's hand-off and step 14. Returns (result, spies)."""
        import agent_cascade.compression.core as _core
        from agent_cascade.agent_pool import AgentPool as _AgentPool
        from agent_cascade import settings as _settings
        from tests.conftest import MockAgentPool

        pool = MockAgentPool(self._history())
        target_inst = self._target_inst()
        pool.get_instance = MagicMock(return_value=target_inst)

        # A live session holding the real claim — exactly what step 8 hands off when the warm
        # path served. For retry=True, model step 8's internal re-claim churn: an OLD rid is
        # taken and dropped, the NEW rid (on the session) survives to be released by the finally.
        if retry:
            assert compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, 'old_rid')
            compressor_reuse.release_claim(COMPRESSOR_WARM_NAME, 'old_rid')
        assert compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, self._RID)
        engine = MagicMock()
        engine._continue_reusable_system_agent_turn.return_value = True   # begin_task succeeds
        l1_session = compressor_reuse.WarmCompressorSession(
            target_inst, COMPRESSOR_WARM_NAME, self._RID, engine, pool, 'Caller')

        def fake_invoke(*a, **kw):
            # keep_warm_session=(not dry_run) -> step 8 yields the 3-tuple (summary, caption, session).
            return ('GENERATED SUMMARY', '', l1_session)

        # Force step 14's marker-count guard so 'run'/'raise' consolidate and 'skip' does not.
        marker_count = 3 if step14 != 'skip' else 0

        import contextlib

        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(_core, 'invoke_compression_agent', side_effect=fake_invoke))
        stack.enter_context(patch.object(_AgentPool, 'count_markers', return_value=marker_count))
        stack.enter_context(patch.object(_AgentPool, 'find_all_marker_indices', return_value=[2, 5, 8]))
        stack.enter_context(patch.object(_settings, 'COMPRESSION_CONSOLIDATION_THRESHOLD', 1))
        stack.enter_context(patch.object(
            _core, 'select_markers_for_consolidation', return_value=([2, 5], 8)))
        stack.enter_context(patch.object(_core, 'extract_summary_from_marker', return_value='S1'))
        stack.enter_context(patch.object(_core, 'get_marker_time_range', return_value=(0.0, 1.0)))
        stack.enter_context(patch.object(_core, '_validate_and_reconsolidate', return_value='CONSOLIDATED'))
        stack.enter_context(patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                                  return_value=('CONSOLIDATED', '')))
        # 'raise' overrides step 14 with a hard failure; 'run'/'skip' leave the real function.
        if step14 == 'raise':
            stack.enter_context(patch.object(_core, '_consolidate_markers',
                                             side_effect=RuntimeError('boom during consolidation')))
        acq = stack.enter_context(patch('agent_cascade.compressor_reuse.acquire_warm_consolidation'))
        engine_cls = stack.enter_context(patch('agent_cascade.execution_engine.ExecutionEngine'))
        # Spy the release the single finally performs; delegate to the real release so the
        # claim actually clears (asserting the real registry state, not just the spy count).
        real_release = compressor_reuse.release_claim
        rel = stack.enter_context(patch.object(compressor_reuse, 'release_claim', side_effect=real_release))
        # The patched engine the consolidation path would build — spy for fresh spawns.
        engine_cls.return_value._create_system_agent = MagicMock(return_value=None)

        with stack:
            result = _core.compress_context(
                agent_pool=pool, target_agent_name='TestAgent',
                fraction=0.5, mode='auto', force=False, trigger='user')
        return result, {
            'acq': acq,
            'rel': rel,
            'create': engine_cls.return_value._create_system_agent,
            'continue': engine._continue_reusable_system_agent_turn,
        }

    def _assert_released_once(self, rel):
        assert rel.call_count == 1, (
            f'compress_context must release the threaded claim EXACTLY once '
            f'(single-release finally) — got {rel.call_count}')
        assert rel.call_args.args[:2] == (COMPRESSOR_WARM_NAME, self._RID)

    def test_claim_released_when_step14_ran(self):
        result, spies = self._run(step14='run')
        assert result.success is True
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None, \
            'claim leaked after a normal consolidation — wedge invariant broken'
        self._assert_released_once(spies['rel'])

    def test_claim_released_when_step14_skipped(self):
        # marker_count < threshold -> step 14 never invoked -> the finally alone releases.
        result, spies = self._run(step14='skip')
        assert result.success is True
        spies['continue'].assert_not_called()              # threading never reached
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None, \
            'claim leaked when consolidation was skipped (marker_count < threshold)'
        self._assert_released_once(spies['rel'])

    def test_claim_released_after_l1_retry(self):
        # Step 8 re-took the claim mid-retry (NEW rid on the session); the finally still
        # releases exactly that one rid — no orphan of the old rid, no leak of the new.
        result, spies = self._run(step14='run', retry=True)
        assert result.success is True
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None, \
            'claim leaked after an L1 retry handed off the re-taken session'
        self._assert_released_once(spies['rel'])

    def test_claim_released_when_step14_raises(self):
        # _consolidate_markers raises -> step-14 try/except swallows it (non-fatal) -> the
        # outer finally still releases the threaded claim exactly once.
        result, spies = self._run(step14='raise')
        assert result.success is True, 'a step-14 failure must not fail the compression itself'
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None, \
            'claim leaked when step 14 raised — the wedge case'
        self._assert_released_once(spies['rel'])

    def test_step14_reuses_l1_instance_no_fresh_spawn(self):
        # With the L1 session threaded, step 14 must not fresh-spawn a Compressor —
        # the warm-acquire and the engine's _create_system_agent stay untouched.
        result, spies = self._run(step14='run')
        assert result.success is True
        assert spies['acq'].call_count == 0, \
            'threaded l1_session must skip acquire_warm_consolidation (no fresh warm acquire)'
        assert spies['create'].call_count == 0, \
            'threaded L2 must not fresh-spawn a Compressor_* via _create_system_agent'
        # Proves the threading path actually executed: primed once, never re-acquired.
        spies['continue'].assert_called_once()


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
