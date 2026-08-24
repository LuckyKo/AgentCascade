"""Regression tests for the per-instance logger file-handle leak on dismissal.

Root cause (todo.md line 138): ``AgentPool.remove_instance()`` was supposed to close
the per-instance logger's cached file handle, but it computed the logger key from
``self.instance_classes`` *after* popping the instance out of ``self.instances``.
Since ``instance_classes`` is a property derived from ``self.instances``, it no longer
contained the instance and returned ``''`` — so the key became ``(name, '')`` instead
of ``(name, '<agent_class>')``, ``_loggers.pop()`` returned ``None``, and ``close()``
was silently skipped. The logger object (and its open ``_file_handle``) leaked.

This test is RED before the fix (handle still open / logger not removed) and GREEN after.
It uses the same mocked-AgentPool fixture pattern as test_dismiss_termination.py, so it
is fast and deterministic with no LLM calls.
"""

import time
from unittest.mock import patch, MagicMock

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState


# ---------------------------------------------------------------------------
# Fixture: build a minimal AgentPool without hitting the filesystem/LLM
# (same pattern as test_dismiss_termination.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_pool(tmp_path):
    """Create an AgentPool with mocked dependencies so it can be instantiated.

    Uses a real tmp workspace dir so LoggerManager can create a genuine log
    directory and open a real file handle (no LLM involved).
    """
    with patch('agent_cascade.operation_manager.OperationManager') as mock_op_mgr, \
         patch('agent_cascade.telemetry.TelemetryCollector') as mock_telem, \
         patch('agent_cascade.api_router.APIRouter') as mock_router:

        op_mgr = MagicMock()
        op_mgr.base_dir = MagicMock()
        op_mgr.base_dir.__str__ = lambda self: str(tmp_path)
        op_mgr.extra_work_folders_ro = []
        op_mgr.extra_work_folders_rw = []
        mock_op_mgr.return_value = op_mgr

        router = MagicMock()
        router.get_effective_concurrency.return_value = 3
        mock_router.return_value = router

        from agent_cascade.agent_pool import AgentPool
        pool = AgentPool(
            llm_cfg={'max_parallel_agents': 2},
            agents_dir=str(tmp_path / 'fake_agents'),
            workspace_dir=str(tmp_path),
        )
        if hasattr(pool, 'settings'):
            pool.settings.idle_timeout_seconds = 60.0
            pool.settings.idle_check_interval = 30.0
        pool.start()
        pool._idle.stop()
        return pool


def make_instance(name: str, agent_class: str = "coder", state: AgentState = AgentState.IDLE):
    """Create a minimal AgentInstance for testing."""
    return AgentInstance(
        instance_name=name,
        agent_class=agent_class,
        conversation=[],
        state=state,
        max_turns=None,
        parent_instance=None,
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        compression_summary=None,
        latest_marker_index=-1,
    )


def _force_open_handle(pool, name: str, agent_class: str):
    """Create the per-instance logger and write one message so ``_file_handle`` is open.

    Returns the logger instance so the test can inspect its ``_file_handle``.
    """
    log_inst = pool.get_logger(name, agent_class)
    # A real write forces _ensure_file() to open the cached handle.
    log_inst.log_message({"role": "user", "content": "hello"})
    assert log_inst._file_handle is not None, \
        "precondition: logger file handle should be open after a write"
    assert not log_inst._file_handle.closed, \
        "precondition: logger file handle should be open (not closed) before dismissal"
    return log_inst


def _handle_is_closed(log_inst) -> bool:
    """True if the cached handle is gone or actually closed."""
    return log_inst._file_handle is None or log_inst._file_handle.closed


# ===========================================================================
# remove_instance() closes the logger's file handle / removes the entry
# ===========================================================================

class TestRemoveInstanceClosesLogger:
    """remove_instance() must close the cached file handle and drop the logger entry."""

    def test_remove_instance_closes_file_handle(self, agent_pool):
        inst = make_instance("w1", agent_class="coder")
        agent_pool.instances["w1"] = inst
        log_inst = _force_open_handle(agent_pool, "w1", "coder")

        agent_pool.remove_instance("w1")

        # The previously-open handle must now be closed (or nulled).
        assert _handle_is_closed(log_inst), \
            "remove_instance() leaked the logger file handle — it is still open"

    def test_remove_instance_removes_logger_entry(self, agent_pool):
        inst = make_instance("w2", agent_class="coder")
        agent_pool.instances["w2"] = inst
        _force_open_handle(agent_pool, "w2", "coder")

        # Sanity: the entry exists before removal.
        assert ("w2", "coder") in agent_pool._logger._loggers

        agent_pool.remove_instance("w2")

        # The (name, agent_class) key must be gone from the cache.
        assert ("w2", "coder") not in agent_pool._logger._loggers, \
            "remove_instance() did not remove the logger entry for the instance"

    def test_remove_instance_with_mixed_case_agent_class(self, agent_pool):
        """agent_class normalization must match get_logger's (strip + lower)."""
        inst = make_instance("w3", agent_class="Coder")
        agent_pool.instances["w3"] = inst
        log_inst = _force_open_handle(agent_pool, "w3", "Coder")

        # get_logger normalizes to 'coder', so the cached key uses the lowercase form.
        assert ("w3", "coder") in agent_pool._logger._loggers

        agent_pool.remove_instance("w3")

        assert _handle_is_closed(log_inst)
        assert ("w3", "coder") not in agent_pool._logger._loggers


# ===========================================================================
# Full dismiss_instance() path (routes through remove_instance)
# ===========================================================================

class TestDismissInstanceClosesLogger:
    """dismiss_instance() must also close the logger handle via remove_instance()."""

    def test_dismiss_idle_instance_closes_file_handle(self, agent_pool):
        inst = make_instance("busy", agent_class="coder", state=AgentState.IDLE)
        agent_pool.instances["busy"] = inst
        log_inst = _force_open_handle(agent_pool, "busy", "coder")

        agent_pool.dismiss_instance("busy")

        assert _handle_is_closed(log_inst), \
            "dismiss_instance() leaked the logger file handle — it is still open"
        assert ("busy", "coder") not in agent_pool._logger._loggers


# ===========================================================================
# The stale-logger hazard: re-creating the same name must NOT reuse a leaked handle
# ===========================================================================

class TestRecreateAfterDismiss:
    """After dismissal, re-creating the same instance name must get a FRESH logger."""

    def test_recreate_same_name_gets_fresh_logger(self, agent_pool):
        inst = make_instance("w1", agent_class="coder")
        agent_pool.instances["w1"] = inst
        old_log = _force_open_handle(agent_pool, "w1", "coder")

        agent_pool.remove_instance("w1")

        # Re-create the same instance name and obtain its logger again.
        inst2 = make_instance("w1", agent_class="coder")
        agent_pool.instances["w1"] = inst2
        new_log = agent_pool.get_logger("w1", "coder")

        # The stale (leaked) handle must have been closed so the two loggers can't
        # both be writing to the same JSONL file.
        assert _handle_is_closed(old_log), \
            "stale logger's file handle is still open — re-creation would double-write"
        # And it should be a distinct, fresh logger object (not the stale one).
        assert new_log is not old_log
