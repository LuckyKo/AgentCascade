"""Regression tests for the KV state-restore-on-agent-return bug (todo.md line 216).

Diagnosis: reports/state_restore_on_return_DIAG.md.

Root cause: in ``slot_yield_utils.yield_caller_slot`` the caller's KV state was only
persisted on Path 1 (live ``_slot_release``) and Path 2 (leaked permit), both of which
return True. Path 3 ("no slot to yield") returned False WITHOUT saving, so whenever a
caller was already slotless the whole save/restore pair was a silent no-op — producing
the observed "Fail to load state on agent return" with zero error logs.

Fix under test: Path 3 now performs the same best-effort KV save as Paths 1/2 before
returning False (see ``slot_yield_utils.py``). This file locks that in and proves the
existing save paths were not broken.

Run serially to avoid xdist state-inheritance flakes:
    python -m pytest tests/test_slot_yield_path3_kv_save.py -n 0 -q
"""

import inspect
from unittest.mock import MagicMock

from agent_cascade.slot_yield_utils import yield_caller_slot
from tests.slot_test_helpers import make_mock_instance, make_mock_pool


# ============================================================================
# Fakes
# ============================================================================


def _engine():
    """Mock ExecutionEngine: a spy on ``_release_slot`` (Path 1) + save_fn stand-in."""
    engine = MagicMock()
    engine._release_slot = MagicMock()
    return engine


def _router(needs_slot=True, holder=None):
    """Mock api_router wired for the pool-holder inspection in Paths 2/3.

    ``holder`` (a mock with ``instance_name``) is placed in the scheduler pool's
    ``_running`` dict so Path 2 can find a leaked permit; leave it None for Path 3.
    """
    router = MagicMock()
    router.get_agent_slot_info.return_value = (
        {'needs_slot': needs_slot, 'api_base': 'http://x', 'concurrency_limit': 1}
        if needs_slot else None
    )
    pool = MagicMock()
    pool._running = {holder.instance_name: holder} if holder is not None else {}

    def _release(h):
        # Mirror the real scheduler pool: remove the holder so Path 2's post-release
        # verification (`_leaked_holder.instance_name not in _running`) passes.
        pool._running.pop(getattr(h, 'instance_name', None), None)

    pool.release.side_effect = _release
    router.scheduler._get_or_create_pool.return_value = pool
    return router


def _pool(router, caller):
    """Mock AgentPool exposing the caller for ``describe_pool_holders`` diagnostics."""
    return make_mock_pool(router, [caller])


# ============================================================================
# Path 3 — the regression guard (no slot to yield → KV still saved)
# ============================================================================


class TestPath3KvSave:

    def test_path3_saves_kv_and_returns_false(self):
        """Regression: a slotless caller (no _slot_release, not a pool holder) must
        STILL have its KV persisted before Path 3 returns False. Before the fix this
        save never happened — that is exactly the bug."""
        caller = make_mock_instance('caller1', slot_release=None)
        router = _router(needs_slot=True, holder=None)   # not a holder → Path 3
        pool = _pool(router, caller)
        engine = _engine()
        save_fn = MagicMock(return_value=True)

        result = yield_caller_slot(
            pool, engine, caller, 'caller1',
            log_prefix='TEST_SLOT_YIELD', release_reason='before_security_check',
            before_action='Security check', save_fn=save_fn,
        )

        # Return semantics UNCHANGED: Path 3 still returns False (caller must not
        # reacquire a slot it never yielded).
        assert result is False
        # The fix: KV was saved exactly once, with (caller_inst, release_reason).
        save_fn.assert_called_once_with(caller, 'before_security_check')
        # No release happened on the skip path.
        engine._release_slot.assert_not_called()

    def test_path3_no_save_when_save_fn_is_none(self):
        """Legacy behavior preserved: when no save_fn is injected, Path 3 does not
        attempt a save (and still returns False)."""
        caller = make_mock_instance('caller1', slot_release=None)
        pool = _pool(_router(needs_slot=True, holder=None), caller)

        result = yield_caller_slot(
            pool, _engine(), caller, 'caller1',
            log_prefix='TEST_SLOT_YIELD', release_reason='before_security_check',
            before_action='Security check', save_fn=None,
        )

        assert result is False

    def test_path3_save_failure_is_swallowed(self):
        """A raising save_fn must NOT propagate (same best-effort discipline as
        Paths 1/2) — Path 3 still returns False."""
        caller = make_mock_instance('caller1', slot_release=None)
        pool = _pool(_router(needs_slot=True, holder=None), caller)
        save_fn = MagicMock(side_effect=RuntimeError('disk full'))

        result = yield_caller_slot(
            pool, _engine(), caller, 'caller1',
            log_prefix='TEST_SLOT_YIELD', release_reason='before_security_check',
            before_action='Security check', save_fn=save_fn,
        )

        assert result is False
        save_fn.assert_called_once()


# ============================================================================
# Contrast — Paths 1 & 2 still save (proves the fix did not break existing paths)
# ============================================================================


class TestExistingSavePathsUnchanged:

    def test_path1_live_release_saves_and_returns_true(self):
        """Path 1 (live _slot_release): saves KV, releases via engine._release_slot,
        returns True."""
        caller = make_mock_instance('caller1', slot_release=lambda: None)
        pool = _pool(_router(), caller)
        engine = _engine()
        save_fn = MagicMock(return_value=True)

        result = yield_caller_slot(
            pool, engine, caller, 'caller1',
            log_prefix='TEST_SLOT_YIELD', release_reason='before_security_check',
            before_action='Security check', save_fn=save_fn,
        )

        assert result is True
        save_fn.assert_called_once_with(caller, 'before_security_check')
        engine._release_slot.assert_called_once()

    def test_path2_leaked_holder_saves_and_returns_true(self):
        """Path 2 (leaked permit: _slot_release cleared but pool still holds the
        caller): saves KV before force-release and returns True."""
        caller = make_mock_instance('caller1', slot_release=None)
        holder = MagicMock()
        holder.instance_name = 'caller1'
        router = _router(needs_slot=True, holder=holder)  # pool shows a holder → Path 2
        pool = _pool(router, caller)
        engine = _engine()
        save_fn = MagicMock(return_value=True)

        result = yield_caller_slot(
            pool, engine, caller, 'caller1',
            log_prefix='TEST_SLOT_YIELD', release_reason='before_compression',
            before_action='compression', save_fn=save_fn,
        )

        assert result is True
        save_fn.assert_called_once_with(caller, 'before_compression')
        # Force-release went through the scheduler pool, not engine._release_slot.
        router.scheduler._get_or_create_pool.return_value.release.assert_called_once_with(holder)


# ============================================================================
# Coverage lock — all three production callers route their SAVE through the helper
# ============================================================================


class TestProductionCallersUseSharedHelper:

    def test_security_handler_routes_save_through_helper(self):
        """security_handler passes ``save_fn=engine.save_before_slot_yield`` into the
        shared yield_caller_slot, so the single Path-3 fix covers its save."""
        from agent_cascade import security_handler
        src = inspect.getsource(security_handler)
        assert 'yield_caller_slot(' in src, 'security_handler must use the shared helper'
        assert 'save_fn=engine.save_before_slot_yield' in src

    def test_compression_invoker_routes_save_through_helper(self):
        """compression/agent_invoker passes ``save_fn=engine.save_before_slot_yield``
        into the shared yield_caller_slot, so the single Path-3 fix covers its save."""
        from agent_cascade.compression import agent_invoker
        src = inspect.getsource(agent_invoker)
        assert 'yield_caller_slot(' in src, 'compression invoker must use the shared helper'
        assert 'save_fn=engine.save_before_slot_yield' in src

    def test_tool_dispatcher_is_a_separate_unconditional_path(self):
        """DISCREPANCY NOTE (do not patch): tool_dispatcher's sync-child path does NOT
        route through yield_caller_slot. It releases directly via
        ``engine._release_slot(...)`` gated on ``_slot_release is not None``, and
        re-acquires UNCONDITIONALLY in its finally block — a separate, already-
        unconditional release path with no caller-side KV save. The single Path-3 fix
        therefore covers only security_handler + compression/agent_invoker; this test
        documents that tool_dispatcher is intentionally NOT part of the shared helper."""
        from agent_cascade import tool_dispatcher
        src = inspect.getsource(tool_dispatcher)
        assert 'yield_caller_slot' not in src, (
            'tool_dispatcher must NOT route through yield_caller_slot — it uses its own '
            'direct _release_slot + unconditional reacquire path'
        )
        # It releases directly and re-acquires unconditionally.
        assert '_release_slot(' in src
        assert 'reacquire_after_slot_yield(' in src
