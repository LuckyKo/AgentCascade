"""Integration tests for call_agent sync vs async path selection using real infrastructure.

Tests the decision logic in ToolDispatcher.handle_call_agent that chooses between
sync and async execution paths based on endpoint concurrency settings and slot states.

Uses real APIRouter, EndpointScheduler, SlotPool, and ToolDispatcher — not mocks —
to verify that sync/async selection actually produces correct execution behavior.

No LLM or network connections required. Uses isolated APIRouter instances.
"""

import os
import threading
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.api_router import APIRouter, APIEndpoint
from agent_cascade.tool_dispatcher import ToolDispatcher


# ============================================================================
# Fixtures and helpers
# ============================================================================

@pytest.fixture
def router_with_endpoints(tmp_path_factory):
    """APIRouter with multiple endpoints for testing various sync/async scenarios."""
    test_config_dir = str(tmp_path_factory.mktemp("call_agent_test"))

    with patch.dict(os.environ, {"AGENT_CASCADE_TEST_CONFIG_DIR": test_config_dir}):
        r = APIRouter(default_llm_cfg={
            'api_base': 'http://default-api',
            'model': 'default-model',
            'max_tokens': 2048,
        })

        # Sequential endpoint (concurrency=1) — default for most agents
        seq_ep = APIEndpoint(
            id="ep_sequential",
            name="Sequential",
            api_base='http://sequential-api',
            model='seq-model',
            enabled=True,
            concurrency_limit=1,
        )
        r.add_endpoint(seq_ep)

        # Parallel endpoint (concurrency=3) — for high-concurrency agents
        parallel_ep = APIEndpoint(
            id="ep_parallel",
            name="Parallel",
            api_base='http://parallel-api',
            model='par-model',
            enabled=True,
            concurrency_limit=3,
        )
        r.add_endpoint(parallel_ep)

        # Unlimited endpoint (concurrency=-1) — no slot management needed
        unlimited_ep = APIEndpoint(
            id="ep_unlimited",
            name="Unlimited",
            api_base='http://unlimited-api',
            model='unlim-model',
            enabled=True,
            concurrency_limit=-1,
        )
        r.add_endpoint(unlimited_ep)

        # Zero-concurrency endpoint (concurrency=0) — shared sequential slot
        zero_ep = APIEndpoint(
            id="ep_zero",
            name="ZeroConcurrency",
            api_base='http://zero-api',
            model='zero-model',
            enabled=True,
            concurrency_limit=0,
        )
        r.add_endpoint(zero_ep)

        yield r


def _make_mock_instance(
    instance_name: str = "caller1",
    agent_class: str = "coder",
    slot_release: Optional[callable] = None,
    state="RUNNING",
    nest_depth: int = 0,
):
    """Minimal mock AgentInstance with configurable slot state."""
    inst = MagicMock()
    inst.instance_name = instance_name
    inst.agent_class = agent_class
    inst._state_lock = threading.RLock()
    inst.state = MagicMock(name=f"{instance_name}_state")
    inst.state.name = state
    inst._slot_release = slot_release
    inst._nest_depth = nest_depth
    return inst


def _make_mock_pool(router: APIRouter, caller_instance=None, max_nesting_depth=10):
    """Minimal mock AgentPool with real router for sync/async decision testing."""
    pool = MagicMock()
    pool.api_router = router
    pool.settings = MagicMock()
    pool.settings.max_nesting_depth = max_nesting_depth

    if caller_instance is not None:
        pool.get_instance.return_value = caller_instance
    else:
        pool.get_instance.return_value = None

    # Track which path was taken
    pool.register_async_call = MagicMock()

    pool.instance_conversations = {}
    pool.instance_classes = {}

    return pool


def _create_dispatcher(pool):
    """Create ToolDispatcher with mocked engine."""
    mock_engine = MagicMock()
    mock_engine._release_slot = MagicMock()
    dispatcher = ToolDispatcher(pool)
    dispatcher.set_engine(mock_engine)
    return dispatcher


# ============================================================================
# Test 1: Sequential child (concurrency=0) forces SYNC path
# ============================================================================

class TestSequentialChildForcesSync:
    """When child agent has concurrency=0, the call takes SYNC path regardless of caller state."""

    def test_sync_when_child_concurrency_zero_no_caller_slot(self, router_with_endpoints):
        """Child with concurrency=0 forces SYNC even when caller holds no slot.
        
        This is the key Sequential Endpoint Guard behavior: conc=0 means only one agent
        can use the shared sequential slot at a time, so we must run inline to avoid
        blocking the entire pool.
        """
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="researcher",
            slot_release=None,  # No slot held
        )

        router_with_endpoints.set_agent_priorities("researcher", ["ep_unlimited"])
        router_with_endpoints.set_agent_priorities("security", ["ep_zero"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "security", "task": "test"},
            messages=[],
            instance=caller,
        )

        # SYNC path: register_async_call should NOT be called
        pool.register_async_call.assert_not_called()
        assert "launched asynchronously" not in result.lower(), \
            f"Expected sync for conc=0 child but got async: {result}"

    def test_sync_when_child_concurrency_zero_with_caller_slot(self, router_with_endpoints):
        """Child with concurrency=0 forces SYNC even when caller holds a slot."""
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=lambda: None,  # Holds a slot
        )

        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])
        router_with_endpoints.set_agent_priorities("security", ["ep_zero"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "security", "task": "test"},
            messages=[],
            instance=caller,
        )

        pool.register_async_call.assert_not_called()
        assert "launched asynchronously" not in result.lower(), \
            f"Expected sync for conc=0 child but got async: {result}"


# ============================================================================
# Test 2: Unlimited child always takes ASYNC path (no slot conflict possible)
# ============================================================================

class TestUnlimitedChildAlwaysAsync:
    """When child uses concurrency=-1 endpoint, ASYNC is safe regardless of caller's slot."""

    def test_async_when_child_unlimited_caller_holds_slot(self, router_with_endpoints):
        """Caller holds a slot but child on unlimited endpoint → ASYNC (no collision)."""
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=lambda: None,  # Holds a slot
        )

        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])
        router_with_endpoints.set_agent_priorities("researcher", ["ep_unlimited"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "researcher", "task": "test"},
            messages=[],
            instance=caller,
        )

        pool.register_async_call.assert_called()
        assert "launched asynchronously" in result.lower(), \
            f"Expected async for unlimited child but got sync: {result}"

    def test_async_when_child_unlimited_caller_no_slot(self, router_with_endpoints):
        """Caller has no slot, child on unlimited → ASYNC."""
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="researcher",
            slot_release=None,
        )

        router_with_endpoints.set_agent_priorities("researcher", ["ep_unlimited"])
        router_with_endpoints.set_agent_priorities("coder", ["ep_unlimited"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "coder", "task": "test"},
            messages=[],
            instance=caller,
        )

        pool.register_async_call.assert_called()


# ============================================================================
# Test 3: Different slot pools → ASYNC is safe (no collision)
# ============================================================================

class TestDifferentSlotPoolsAsync:
    """When caller and child use different slot pools, ASYNC avoids deadlock."""

    def test_async_when_different_endpoints_caller_holds_slot(self, router_with_endpoints):
        """Caller holds sequential slot, child uses parallel endpoint → ASYNC (different pools)."""
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=lambda: None,
        )

        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])
        router_with_endpoints.set_agent_priorities("reviewer", ["ep_parallel"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "reviewer", "task": "test"},
            messages=[],
            instance=caller,
        )

        pool.register_async_call.assert_called(), \
            f"Expected async (different pools) but took sync. Result: {result}"


# ============================================================================
# Test 4: Same slot pool collision → SYNC required to avoid deadlock
# ============================================================================

class TestSamePoolCollisionSync:
    """When caller and child share the same limited endpoint, SYNC avoids deadlock."""

    def test_sync_when_same_sequential_endpoint(self, router_with_endpoints):
        """Caller holds sequential slot, child uses same endpoint → SYNC (collision)."""
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=lambda: None,
        )

        # Both use the same sequential endpoint
        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "coder", "task": "test"},
            messages=[],
            instance=caller,
        )

        pool.register_async_call.assert_not_called()
        assert "launched asynchronously" not in result.lower(), \
            f"Expected sync for same-pool collision but got async: {result}"


# ============================================================================
# Test 5: Decision uses child's effective concurrency (not just caller state)
# ============================================================================

class TestEffectiveConcurrencyDecision:
    """Verify the decision logic queries and respects the child's effective concurrency."""

    def test_queries_child_effective_concurrency(self, router_with_endpoints):
        """get_effective_concurrency is called for the child agent class during dispatch."""
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=lambda: None,
        )

        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])
        router_with_endpoints.set_agent_priorities("reviewer", ["ep_parallel"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        original_get_eff = router_with_endpoints.get_effective_concurrency
        calls_to_child_class = []

        def tracking_get_eff(agent_type):
            result = original_get_eff(agent_type)
            calls_to_child_class.append(agent_type)
            return result

        with patch.object(router_with_endpoints, 'get_effective_concurrency', side_effect=tracking_get_eff):
            dispatcher.handle_call_agent(
                args={"instance_name": "child1", "agent_class": "reviewer", "task": "test"},
                messages=[],
                instance=caller,
            )

        assert "reviewer" in calls_to_child_class or "Reviewer" in calls_to_child_class, \
            f"Expected get_effective_concurrency for child class 'reviewer'. Calls: {calls_to_child_class}"

    def test_guard_only_triggers_for_concurrency_zero(self, router_with_endpoints):
        """Sequential Endpoint Guard only forces sync for conc=0, not conc=1 or higher."""
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="researcher",
            slot_release=None,  # No slot held
        )

        router_with_endpoints.set_agent_priorities("researcher", ["ep_unlimited"])
        # Child uses conc=1 endpoint (sequential but NOT zero)
        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "coder", "task": "test"},
            messages=[],
            instance=caller,
        )

        # conc=1 is NOT zero → guard does not trigger → ASYNC (caller has no slot)
        pool.register_async_call.assert_called(), \
            f"Expected async for conc=1 child but took sync. Result: {result}"
