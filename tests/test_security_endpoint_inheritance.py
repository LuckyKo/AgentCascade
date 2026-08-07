"""Unit tests for Security agent endpoint inheritance deadlock fix (TODO #90).

Verifies that the Security handler resolves the caller from ap['agent_name']
instead of always using the session name, so Security inherits the caller's
endpoint chain rather than slot 0. This prevents deadlock when an async child
holding a different slot triggers a Security check while the sync parent holds
slot 0.

All tests are self-contained — no LLM or API server required.
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

# ──────────────────────────────────────────────
# Test Helpers — lightweight mock objects
# ──────────────────────────────────────────────


def _make_mock_instance(
    instance_name="Maine",
    agent_class="orchestrator",
):
    """Create a minimal AgentInstance mock."""
    inst = MagicMock()
    inst.instance_name = instance_name
    inst.agent_class = agent_class
    inst._state_lock = threading.RLock()
    inst._slot_release = None  # Not holding any slot by default
    inst.conversation = []
    return inst


async def _mock_broadcast(data):
    """Dummy broadcast function for SecurityAdvisorHandler."""
    pass


def _make_handler(pool, session=None):
    """Create a SecurityAdvisorHandler with all required dependencies mocked.

    Args:
        pool: AgentPool mock (from _make_mock_pool).
        session: Optional session dict; defaults to {'session_name': 'Maine'}.
    Returns:
        SecurityAdvisorHandler instance ready for testing.
    """
    from agent_cascade.security_handler import SecurityAdvisorHandler

    if session is None:
        session = {'session_name': 'Maine'}
    app_state = MagicMock()
    send_queue = MagicMock()

    return SecurityAdvisorHandler(pool, session, app_state, send_queue, _mock_broadcast)


def _make_mock_pool(instances=None):
    """Create a minimal AgentPool mock.

    Always includes 'Maine' (orchestrator) by default for fallback behavior tests.

    Args:
        instances: Dict of instance_name -> instance mock, or list of (name, class) tuples.
                   If None, creates pool with only Maine.
    """
    pool = MagicMock()
    # Always start with Maine as the orchestrator
    if isinstance(instances, dict):
        pool.instances = instances.copy()
    else:
        pool.instances = {}
        for item in (instances or []):
            if isinstance(item, tuple):
                name, cls = item
                pool.instances[name] = _make_mock_instance(name, cls)
            else:
                pool.instances[item] = _make_mock_instance(item)

    # Ensure Maine exists
    if "Maine" not in pool.instances:
        pool.instances["Maine"] = _make_mock_instance("Maine", "orchestrator")

    def get_instance(name):
        return pool.instances.get(name, None)

    pool.get_instance = get_instance
    pool.stopped = False

    # Mock operation_manager
    op_mgr = MagicMock()
    op_mgr.enable_timeout = True
    op_mgr.approval_timeout_seconds = 180
    op_mgr.base_dir = "/workspace"
    op_mgr.extra_work_folders_ro = []
    op_mgr.extra_work_folders_rw = []
    pool.operation_manager = op_mgr

    return pool


def _make_approval(
    request_id="test_req_001",
    agent_name="Maine",
    tool_name="shell_cmd",
    description="Test command",
    tool_args=None,
):
    """Create a minimal approval dict matching list_pending_approvals() output."""
    return {
        'request_id': request_id,
        'agent_name': agent_name,
        'tool_name': tool_name,
        'tool_args': tool_args or {},
        'description': description,
        'justification': '',
        'timestamp': 0.0,
    }


# ──────────────────────────────────────────────
# Tests: Caller Resolution Logic (async run_check)
# ──────────────────────────────────────────────


class TestCallerResolutionFromApproval:
    """Test that caller_agent is resolved from ap['agent_name'] correctly."""

    @pytest.mark.asyncio
    async def test_primary_regression_async_child_triggers_security(self):
        """Security inherits caller's endpoint, not slot 0.

        Scenario: async child B triggers security check while parent A holds slot 0.
        Assert: Security's parent_instance == B (not Maine), so it uses B's endpoint.
        """
        # Setup: pool with orchestrator (Maine) in slot 0, async child B in slot 1
        pool = _make_mock_pool()
        child_b = _make_mock_instance("async_child_B", "coder")
        child_b._slot_release = MagicMock()  # B holds a slot
        pool.instances["async_child_B"] = child_b

        handler = _make_handler(pool)

        # Approval from async_child_B (the real tool requester)
        approval = _make_approval(agent_name="async_child_B", tool_name="shell_cmd")
        pool.operation_manager.list_pending_approvals.return_value = [approval]

        with patch.object(handler, '_run_check_worker') as mock_worker:
            # Await the async run_check
            await handler.run_check({'request_id': approval['request_id'], 'auto_apply': True})

            # Verify _run_check_worker was called with correct caller_agent
            assert mock_worker.called
            call_args = mock_worker.call_args
            # args: (ap, sec_inst, rid, auto_apply, instance_name, caller_agent, prompt_template, timeout, warning)
            ap_arg = call_args[0][0]
            instance_name_arg = call_args[0][4]
            caller_agent_arg = call_args[0][5]

            # The approval's agent_name should be used as caller_agent
            assert ap_arg['agent_name'] == 'async_child_B'
            assert instance_name_arg == 'Maine', "instance_name (session) should still be Maine"
            assert caller_agent_arg == 'async_child_B', \
                "caller_agent must be the async child, not the session name"

    @pytest.mark.asyncio
    async def test_fallback_when_agent_name_missing(self):
        """ap without 'agent_name' key falls back to session name."""
        pool = _make_mock_pool()
        handler = _make_handler(pool)

        # Approval missing agent_name key entirely
        approval = {
            'request_id': 'test_req_no_agent',
            'tool_name': 'shell_cmd',
            'tool_args': {},
            'description': 'Test',
            'justification': '',
            'timestamp': 0.0,
        }
        pool.operation_manager.list_pending_approvals.return_value = [approval]

        with patch.object(handler, '_run_check_worker') as mock_worker:
            await handler.run_check({'request_id': approval['request_id'], 'auto_apply': True})

            assert mock_worker.called
            caller_agent_arg = mock_worker.call_args[0][5]
            assert caller_agent_arg == 'Maine', \
                "Missing agent_name should fall back to session name"

    @pytest.mark.asyncio
    async def test_fallback_when_agent_name_none(self):
        """ap['agent_name'] is None → falls back to session name."""
        pool = _make_mock_pool()
        handler = _make_handler(pool)

        approval = _make_approval(agent_name=None)
        pool.operation_manager.list_pending_approvals.return_value = [approval]

        with patch.object(handler, '_run_check_worker') as mock_worker:
            await handler.run_check({'request_id': approval['request_id'], 'auto_apply': True})

            assert mock_worker.called
            caller_agent_arg = mock_worker.call_args[0][5]
            assert caller_agent_arg == 'Maine', \
                "None agent_name should fall back to session name"

    @pytest.mark.asyncio
    async def test_fallback_when_agent_name_not_in_pool(self):
        """ap['agent_name'] points to non-existent instance → falls back gracefully."""
        pool = _make_mock_pool()  # Only Maine exists
        handler = _make_handler(pool)

        # Approval references an instance that doesn't exist in the pool
        approval = _make_approval(agent_name="ghost_worker_99")
        pool.operation_manager.list_pending_approvals.return_value = [approval]

        with patch.object(handler, '_run_check_worker') as mock_worker:
            await handler.run_check({'request_id': approval['request_id'], 'auto_apply': True})

            assert mock_worker.called
            caller_agent_arg = mock_worker.call_args[0][5]
            assert caller_agent_arg == 'Maine', \
                "Non-existent agent_name should fall back to session name"

    @pytest.mark.asyncio
    async def test_caller_is_orchestrator_itself(self):
        """When the caller IS Maine/orchestrator, no regression — still works."""
        pool = _make_mock_pool()  # Only Maine exists
        handler = _make_handler(pool)

        # Approval from Maine itself (root agent sync path)
        approval = _make_approval(agent_name="Maine")
        pool.operation_manager.list_pending_approvals.return_value = [approval]

        with patch.object(handler, '_run_check_worker') as mock_worker:
            await handler.run_check({'request_id': approval['request_id'], 'auto_apply': True})

            assert mock_worker.called
            caller_agent_arg = mock_worker.call_args[0][5]
            instance_name_arg = mock_worker.call_args[0][4]
            assert caller_agent_arg == 'Maine', \
                "Orchestrator-as-caller should resolve to Maine"
            assert instance_name_arg == 'Maine'


# ──────────────────────────────────────────────
# Tests: Security Instance Creation with Correct Parent
# ──────────────────────────────────────────────


class TestSecurityInstanceParentAssignment:
    """Test that _execute_check creates Security instance with correct caller as parent."""

    def test_security_parent_is_caller_not_session(self):
        """Security's parent_instance == caller_agent, not session name.

        This is the core fix — Security inherits from the true caller so it
        uses their endpoint chain instead of slot 0.
        """
        pool = _make_mock_pool()
        child_b = _make_mock_instance("async_child_B", "coder")
        pool.instances["async_child_B"] = child_b

        handler = _make_handler(pool)

        approval = _make_approval(agent_name="async_child_B")
        rid = approval['request_id']

        created_caller_args = []

        def mock_create_system_agent(agent_class, instance_name, task, caller, context=""):
            """Capture the caller arg passed to _create_system_agent."""
            created_caller_args.append(caller)
            sec_inst = MagicMock()
            sec_inst.instance_name = instance_name
            sec_inst.agent_class = agent_class
            sec_inst.conversation = []
            return sec_inst

        with patch('agent_cascade.execution_engine.ExecutionEngine') as MockEngine:
            mock_engine = MagicMock()
            MockEngine.return_value = mock_engine
            mock_engine._create_system_agent = mock_create_system_agent
            # Make engine.run yield one response so _execute_check completes
            mock_engine.run.return_value = iter([('Analysis complete.\n\n[YES] Safe to proceed.', False)])
            # Mock telemetry
            mock_engine._telemetry.return_value = None

            # Run the check synchronously (call _run_check_worker directly)
            handler._run_check_worker(
                ap=approval,
                sec_inst=None,
                rid=rid,
                auto_apply=True,
                instance_name='Maine',
                caller_agent='async_child_B',  # The resolved true caller
                prompt_template="[SECURITY] Check: {tool_name}\n{description}",
                timeout_seconds=180,
                warning_seconds=120,
            )

        assert len(created_caller_args) == 1
        assert created_caller_args[0] == 'async_child_B', \
            "Security's caller (parent) must be the async child, not Maine"

    def test_security_parent_fallback_to_session(self):
        """When caller_agent falls back to session name, Security parent is session."""
        pool = _make_mock_pool()  # Only Maine
        handler = _make_handler(pool)

        approval = _make_approval(agent_name="Maine")
        rid = approval['request_id']

        created_caller_args = []

        def mock_create_system_agent(agent_class, instance_name, task, caller, context=""):
            created_caller_args.append(caller)
            sec_inst = MagicMock()
            sec_inst.instance_name = instance_name
            sec_inst.agent_class = agent_class
            sec_inst.conversation = []
            return sec_inst

        with patch('agent_cascade.execution_engine.ExecutionEngine') as MockEngine:
            mock_engine = MagicMock()
            MockEngine.return_value = mock_engine
            mock_engine._create_system_agent = mock_create_system_agent
            mock_engine.run.return_value = iter([('Analysis complete.\n\n[YES] Safe.', False)])
            mock_engine._telemetry.return_value = None

            handler._run_check_worker(
                ap=approval,
                sec_inst=None,
                rid=rid,
                auto_apply=True,
                instance_name='Maine',
                caller_agent='Maine',  # Orchestrator is the caller
                prompt_template="[SECURITY] Check: {tool_name}\n{description}",
                timeout_seconds=180,
                warning_seconds=120,
            )

        assert len(created_caller_args) == 1
        assert created_caller_args[0] == 'Maine'


# ──────────────────────────────────────────────
# Tests: Signature Compatibility
# ──────────────────────────────────────────────


class TestSignatureCompatibility:
    """Test that _run_check_worker and _execute_check signatures work correctly."""

    def test_run_check_worker_signature(self):
        """_run_check_worker accepts caller_agent parameter in correct position."""
        pool = _make_mock_pool()
        handler = _make_handler(pool)

        approval = _make_approval(agent_name="worker1")
        rid = approval['request_id']

        with patch.object(handler, '_execute_check') as mock_execute:
            # Call with all required positional args matching the new signature
            handler._run_check_worker(
                ap=approval,
                sec_inst=None,
                rid=rid,
                auto_apply=True,
                instance_name='Maine',
                caller_agent='worker1',
                prompt_template="Test prompt",
                timeout_seconds=180,
                warning_seconds=120,
            )

            # Verify _execute_check was called with matching signature
            assert mock_execute.called
            call_args = mock_execute.call_args[0]
            assert len(call_args) == 9  # Same number of args as _run_check_worker receives
            assert call_args[0] is approval
            assert call_args[1] is None
            assert call_args[2] == rid
            assert call_args[3] is True
            assert call_args[4] == 'Maine'      # instance_name
            assert call_args[5] == 'worker1'    # caller_agent

    def test_execute_check_signature(self):
        """_execute_check accepts caller_agent parameter in correct position."""
        pool = _make_mock_pool()
        handler = _make_handler(pool)

        approval = _make_approval(agent_name="worker1")
        rid = approval['request_id']

        created_caller_args = []

        def mock_create_system_agent(agent_class, instance_name, task, caller, context=""):
            created_caller_args.append(caller)
            sec_inst = MagicMock()
            sec_inst.instance_name = instance_name
            sec_inst.agent_class = agent_class
            sec_inst.conversation = []
            return sec_inst

        with patch('agent_cascade.execution_engine.ExecutionEngine') as MockEngine:
            mock_engine = MagicMock()
            MockEngine.return_value = mock_engine
            mock_engine._create_system_agent = mock_create_system_agent
            mock_engine.run.return_value = iter([('Analysis complete.\n\n[YES] Safe.', False)])
            mock_engine._telemetry.return_value = None

            # Direct call to _execute_check with new signature
            handler._execute_check(
                ap=approval,
                sec_inst=None,
                rid=rid,
                auto_apply=True,
                instance_name='Maine',
                caller_agent='worker1',  # New parameter
                prompt_template="[SECURITY] Check: {tool_name}\n{description}",
                timeout_seconds=180,
                warning_seconds=120,
            )

        assert len(created_caller_args) == 1
        assert created_caller_args[0] == 'worker1', \
            "caller_agent must be passed through to _create_system_agent"


# ──────────────────────────────────────────────
# Tests: Slot-Bypass Logging Uses Correct Caller
# ──────────────────────────────────────────────


class TestSlotBypassLogging:
    """Test that slot-bypass logging reports the true caller, not session name."""

    def test_slot_bypass_log_reports_true_caller(self):
        """[SECURITY_SLOT_BYPASS] log line uses caller_agent, not session name."""
        pool = _make_mock_pool()
        child_b = _make_mock_instance("async_child_B", "coder")
        pool.instances["async_child_B"] = child_b

        handler = _make_handler(pool)

        approval = _make_approval(agent_name="async_child_B")
        rid = approval['request_id']

        def mock_create_system_agent(agent_class, instance_name, task, caller, context=""):
            sec_inst = MagicMock()
            sec_inst.instance_name = instance_name
            sec_inst.agent_class = agent_class
            sec_inst.conversation = []
            return sec_inst

        with patch('agent_cascade.execution_engine.ExecutionEngine') as MockEngine:
            mock_engine = MagicMock()
            MockEngine.return_value = mock_engine
            mock_engine._create_system_agent = mock_create_system_agent
            mock_engine.run.return_value = iter([('Analysis complete.\n\n[YES] Safe.', False)])
            mock_engine._telemetry.return_value = None

            with patch('agent_cascade.log.logger') as mock_logger:
                handler._run_check_worker(
                    ap=approval,
                    sec_inst=None,
                    rid=rid,
                    auto_apply=True,
                    instance_name='Maine',
                    caller_agent='async_child_B',
                    prompt_template="[SECURITY] Check: {tool_name}\n{description}",
                    timeout_seconds=180,
                    warning_seconds=120,
                )

                # Find the [SECURITY_SLOT_BYPASS] debug call
                bypass_calls = [c for c in mock_logger.debug.call_args_list
                                if 'SECURITY_SLOT_BYPASS' in str(c)]
                assert len(bypass_calls) >= 1, \
                    "Expected at least one [SECURITY_SLOT_BYPASS] log call"

                # Verify the log message contains the true caller name
                bypass_msg = str(bypass_calls[0])
                assert 'caller=async_child_B' in bypass_msg, \
                    f"Slot bypass log should report true caller, got: {bypass_msg}"