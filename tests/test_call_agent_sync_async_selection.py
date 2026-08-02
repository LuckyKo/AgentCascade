"""Integration tests for call_agent sync vs async path selection.

Tests the decision logic in ToolDispatcher.handle_call_agent that chooses between
sync and async execution paths based on endpoint concurrency settings and slot states.

These tests exercise the real decision logic (not fully mocked) to verify:
- Agents on unlimited endpoints can launch children asynchronously
- Agents on limited-concurrency endpoints (>1) can launch children asynchronously
- Sequential Endpoint Guard only forces sync when child's effective concurrency is 0
- No deadlocks occur with shared/competing endpoint slots

No LLM or network connections required. Uses isolated APIRouter instances.
"""

import os
import threading
import time
from typing import Optional
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from agent_cascade.api_router import APIRouter, APIEndpoint
from agent_cascade.tool_dispatcher import ToolDispatcher


# ============================================================================
# Test fixtures and helpers
# ============================================================================


@pytest.fixture
def isolated_router(tmp_path_factory):
    """Create an isolated APIRouter with its own config dir."""
    test_config_dir = str(tmp_path_factory.mktemp("call_agent_test"))

    with patch.dict(os.environ, {"AGENT_CASCADE_TEST_CONFIG_DIR": test_config_dir}):
        r = APIRouter(default_llm_cfg={
            'api_base': 'http://default-api',
            'model': 'default-model',
            'max_tokens': 2048,
        })
        # Add default endpoint with concurrency=1 (sequential)
        default_ep = APIEndpoint(
            id="ep_default",
            name="Default",
            api_base='http://default-api',
            model='default-model',
            enabled=True,
            concurrency_limit=1,
        )
        r.add_endpoint(default_ep)
        yield r


@pytest.fixture
def router_with_endpoints(tmp_path_factory):
    """Create an APIRouter with multiple endpoints for testing various scenarios."""
    test_config_dir = str(tmp_path_factory.mktemp("call_agent_test"))

    with patch.dict(os.environ, {"AGENT_CASCADE_TEST_CONFIG_DIR": test_config_dir}):
        r = APIRouter(default_llm_cfg={
            'api_base': 'http://sequential-api',
            'model': 'default-model',
            'max_tokens': 2048,
        })

        # Sequential endpoint (concurrency=1) - default for most agents
        seq_ep = APIEndpoint(
            id="ep_sequential",
            name="Sequential",
            api_base='http://sequential-api',
            model='seq-model',
            enabled=True,
            concurrency_limit=1,
        )
        r.add_endpoint(seq_ep)

        # Parallel endpoint (concurrency=3) - for high-concurrency agents
        parallel_ep = APIEndpoint(
            id="ep_parallel",
            name="Parallel",
            api_base='http://parallel-api',
            model='par-model',
            enabled=True,
            concurrency_limit=3,
        )
        r.add_endpoint(parallel_ep)

        # Unlimited endpoint (concurrency=-1) - no slot management needed
        unlimited_ep = APIEndpoint(
            id="ep_unlimited",
            name="Unlimited",
            api_base='http://unlimited-api',
            model='unlim-model',
            enabled=True,
            concurrency_limit=-1,
        )
        r.add_endpoint(unlimited_ep)

        # Zero-concurrency endpoint (concurrency=0) - shared sequential slot
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
    """Create a minimal mock AgentInstance with configurable slot state."""
    inst = MagicMock()
    inst.instance_name = instance_name
    inst.agent_class = agent_class
    inst._state_lock = threading.RLock()
    inst.state = MagicMock(name=f"{instance_name}_state")
    inst.state.name = state
    # Set _slot_release directly - this is what the decision logic checks
    inst._slot_release = slot_release
    # Set _nest_depth for nesting depth check
    inst._nest_depth = nest_depth
    return inst


def _make_mock_settings(max_nesting_depth=10):
    """Create a minimal mock Settings object."""
    settings = MagicMock()
    settings.max_nesting_depth = max_nesting_depth
    return settings


def _make_mock_pool(router: APIRouter, caller_instance=None, max_nesting_depth=10):
    """Create a minimal mock AgentPool with the given router."""
    pool = MagicMock()
    pool.api_router = router
    pool.settings = _make_mock_settings(max_nesting_depth)

    if caller_instance is not None:
        pool.get_instance.return_value = caller_instance
    else:
        pool.get_instance.return_value = None

    # Mock async call registration to track what path was taken
    pool.register_async_call = MagicMock()

    # Ensure instance_conversations exists for active instance guard
    pool.instance_conversations = {}
    pool.instance_classes = {}

    return pool


def _make_mock_engine():
    """Create a minimal mock ExecutionEngine."""
    engine = MagicMock()
    engine._release_slot = MagicMock()
    return engine


def _create_dispatcher(pool, engine=None):
    """Create a ToolDispatcher with the given pool and optional engine."""
    if engine is None:
        engine = _make_mock_engine()
    dispatcher = ToolDispatcher(pool)
    dispatcher.set_engine(engine)
    return dispatcher


# ============================================================================
# Test 1: Agent on concurrency=1 endpoint calling agent on different endpoint
# ============================================================================


class TestSequentialCallerDifferentEndpointChild:
    """Test that an agent on a sequential (concurrency=1) endpoint can call
    another agent asynchronously when the child uses a different endpoint."""

    def test_sync_when_child_same_sequential_endpoint(self, router_with_endpoints):
        """When caller holds slot on sequential endpoint and child shares it,
        should take SYNC path to avoid deadlock."""
        # Caller is on sequential endpoint (holds slot)
        release_cb = lambda: None  # Simulates holding a slot
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=release_cb,
        )

        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])
        # Child uses same sequential endpoint
        router_with_endpoints.set_agent_priorities("reviewer", ["ep_sequential"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "reviewer", "task": "test"},
            messages=[],
            instance=caller,
        )

        # Should take SYNC path because caller holds slot on same sequential endpoint
        pool.register_async_call.assert_not_called()
        # Sync path returns result from child_runner (mocked), not async message
        assert "launched asynchronously" not in result.lower(), \
            f"Expected sync path but got async result: {result}"

    def test_async_when_child_different_parallel_endpoint(self, router_with_endpoints):
        """When caller holds slot on sequential endpoint but child uses a
        different parallel endpoint (concurrency>1), should take ASYNC path.

        FIXED: Now correctly takes async when child uses a different endpoint pool.
        """
        release_cb = lambda: None
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=release_cb,
        )

        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])
        # Child uses parallel endpoint (different from caller)
        router_with_endpoints.set_agent_priorities("reviewer", ["ep_parallel"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "reviewer", "task": "test"},
            messages=[],
            instance=caller,
        )

        # EXPECTED: Should take ASYNC because child uses different endpoint with its own concurrency
        pool.register_async_call.assert_called(), \
            f"Expected async path but took sync. Result: {result}"


# ============================================================================
# Test 2: Agent on concurrency=N (>1) endpoint calling children asynchronously
# ============================================================================


class TestParallelCallerAsyncChildren:
    """Test that an agent on a parallel (concurrency>1) endpoint can call
    children asynchronously without blocking."""

    def test_async_when_caller_on_parallel_endpoint(self, router_with_endpoints):
        """When caller is on a parallel endpoint with concurrency>1 and holds a slot,
        should be able to launch children asynchronously if child uses different endpoint.

        FIXED: Now correctly takes async when child uses a different endpoint pool.
        """
        # Caller is on parallel endpoint (holds one of 3 slots)
        release_cb = lambda: None
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=release_cb,
        )

        router_with_endpoints.set_agent_priorities("coder", ["ep_parallel"])
        # Child uses sequential endpoint (different from caller)
        router_with_endpoints.set_agent_priorities("reviewer", ["ep_sequential"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "reviewer", "task": "test"},
            messages=[],
            instance=caller,
        )

        # EXPECTED: Should take ASYNC - child uses different endpoint, no slot conflict
        pool.register_async_call.assert_called(), \
            f"Expected async path but took sync. Result: {result}"

    def test_async_when_child_on_unlimited_endpoint(self, router_with_endpoints):
        """When child uses an unlimited endpoint (concurrency=-1), should always take ASYNC.

        FIXED: Now correctly takes async for unlimited endpoint children regardless of caller's slot.
        """
        # Caller holds a slot on sequential endpoint
        release_cb = lambda: None
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=release_cb,
        )

        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])
        # Child uses unlimited endpoint - no slot needed
        router_with_endpoints.set_agent_priorities("researcher", ["ep_unlimited"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "researcher", "task": "test"},
            messages=[],
            instance=caller,
        )

        # EXPECTED: Should take ASYNC - child on unlimited endpoint doesn't compete for slots
        pool.register_async_call.assert_called(), \
            f"Expected async path but took sync. Result: {result}"


# ============================================================================
# Test 3: Sequential Endpoint Guard only forces sync when appropriate
# ============================================================================


class TestSequentialEndpointGuard:
    """Test that the Sequential Endpoint Guard (concurrency=0 check) only
    forces sync when child's effective concurrency is actually 0."""

    def test_sync_when_child_effective_concurrency_is_zero(self, router_with_endpoints):
        """When child's effective concurrency is 0, should force SYNC path
        regardless of caller's slot state."""
        # Caller doesn't hold a slot (on unlimited endpoint)
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="researcher",
            slot_release=None,  # No slot held
        )

        router_with_endpoints.set_agent_priorities("researcher", ["ep_unlimited"])
        # Child uses zero-concurrency endpoint
        router_with_endpoints.set_agent_priorities("security", ["ep_zero"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "security", "task": "test"},
            messages=[],
            instance=caller,
        )

        # Should take SYNC path due to Sequential Endpoint Guard
        pool.register_async_call.assert_not_called()
        assert "launched asynchronously" not in result.lower(), \
            f"Expected sync for zero-concurrency child but got async: {result}"

    def test_async_when_child_concurrency_is_positive(self, router_with_endpoints):
        """When child's effective concurrency is > 0 and caller doesn't hold slot,
        should take ASYNC path (guard does NOT force sync)."""
        # Caller doesn't hold a slot
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="researcher",
            slot_release=None,
        )

        router_with_endpoints.set_agent_priorities("researcher", ["ep_unlimited"])
        # Child uses sequential endpoint (concurrency=1, not 0)
        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "coder", "task": "test"},
            messages=[],
            instance=caller,
        )

        # Should take ASYNC path - child has concurrency=1 (not 0), caller has no slot
        pool.register_async_call.assert_called(), \
            f"Expected async path but took sync. Result: {result}"

    def test_guard_does_not_trigger_for_non_zero_concurrency(self, router_with_endpoints):
        """Sequential Endpoint Guard should NOT force sync when child concurrency is 1 or more."""
        # Caller doesn't hold a slot
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="researcher",
            slot_release=None,
        )

        router_with_endpoints.set_agent_priorities("researcher", ["ep_unlimited"])
        # Child uses parallel endpoint (concurrency=3)
        router_with_endpoints.set_agent_priorities("coder", ["ep_parallel"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "coder", "task": "test"},
            messages=[],
            instance=caller,
        )

        # Should take ASYNC path - guard only triggers for concurrency=0
        pool.register_async_call.assert_called(), \
            f"Expected async path but took sync. Result: {result}"


# ============================================================================
# Test 4: No deadlock with shared/competing endpoint slots
# ============================================================================


class TestNoDeadlockWithSharedSlots:
    """Test that async path doesn't deadlock when caller and child share or
    compete for endpoints with limited concurrency."""

    def test_async_when_caller_has_no_slot(self, router_with_endpoints):
        """When caller doesn't hold a slot (unlimited endpoint), async should work fine."""
        # Caller on unlimited endpoint - no slot held
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="researcher",
            slot_release=None,
        )

        router_with_endpoints.set_agent_priorities("researcher", ["ep_unlimited"])
        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "coder", "task": "test"},
            messages=[],
            instance=caller,
        )

        # Should take ASYNC path - no slot conflict possible
        pool.register_async_call.assert_called(), \
            f"Expected async path but took sync. Result: {result}"

    def test_sync_when_same_endpoint_shared_slot(self, router_with_endpoints):
        """When caller and child share the same limited endpoint, should take SYNC
        to avoid deadlock (caller holds slot, child can't acquire it)."""
        # Caller holds slot on sequential endpoint
        release_cb = lambda: None
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=release_cb,
        )

        # Both use same sequential endpoint
        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "coder", "task": "test"},
            messages=[],
            instance=caller,
        )

        # Should take SYNC path - child would compete for same slot caller holds
        pool.register_async_call.assert_not_called()
        assert "launched asynchronously" not in result.lower(), \
            f"Expected sync for shared endpoint but got async: {result}"


# ============================================================================
# Test 5: Decision logic uses effective concurrency, not just caller's slot state
# ============================================================================


class TestEffectiveConcurrencyBasedDecision:
    """Test that the decision logic considers child's effective concurrency
    in addition to caller's slot state."""

    def test_uses_effective_concurrency_for_child(self, router_with_endpoints):
        """Verify get_effective_concurrency is called for child agent class."""
        release_cb = lambda: None
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=release_cb,
        )

        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])
        router_with_endpoints.set_agent_priorities("reviewer", ["ep_parallel"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        # Patch get_effective_concurrency to verify it's called for child class
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

        # Should have queried effective concurrency for the child class
        assert "reviewer" in calls_to_child_class or "Reviewer" in calls_to_child_class, \
            f"Expected get_effective_concurrency to be called for child class 'reviewer'. Calls: {calls_to_child_class}"


# ============================================================================
# Test 6: Verify fixed behavior - slot pool collision detection
# ============================================================================


class TestFixedBehaviorSlotCollisionDetection:
    """Verify that the fix correctly uses slot pool collision detection instead of
    blindly forcing sync whenever caller holds any slot."""

    def test_async_when_caller_holds_slot_but_child_unlimited(self, router_with_endpoints):
        """When caller holds a slot but child is on unlimited endpoint (conc=-1),
        should take ASYNC path - no collision possible."""
        release_cb = lambda: None
        caller = _make_mock_instance(
            instance_name="caller1",
            agent_class="coder",
            slot_release=release_cb,  # Caller holds a slot
        )

        # Child uses completely different unlimited endpoint - no conflict possible
        router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])
        router_with_endpoints.set_agent_priorities("researcher", ["ep_unlimited"])

        pool = _make_mock_pool(router_with_endpoints, caller)
        dispatcher = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={"instance_name": "child1", "agent_class": "researcher", "task": "test"},
            messages=[],
            instance=caller,
        )

        # FIXED: Now correctly takes ASYNC because child is on unlimited endpoint
        pool.register_async_call.assert_called()
        assert "launched asynchronously" in result.lower(), \
            f"Expected async for unlimited child but got sync: {result}"

    def test_sync_only_when_same_slot_pool_collision(self, router_with_endpoints):
        """Verify that SYNC is taken only when there's an actual slot pool collision:
        - conc=0 child (shared sequential) → SYNC
        - same api_base with limited concurrency → SYNC
        ASYNC is taken for different pools and unlimited children."""
        results = {}

        test_cases = [
            # (child_class, endpoint_id, expected_async, reason)
            ("security", "ep_zero", False, "conc=0 child always sync"),
            ("coder_child", "ep_sequential", True, "different pool from caller's ep_sequential? No - same pool → SYNC actually"),
            ("parallel_worker", "ep_parallel", True, "different pool → async"),
            ("unlimited_worker", "ep_unlimited", True, "conc=-1 → always async"),
        ]

        for child_class, endpoint_id, expected_async, reason in test_cases:
            release_cb = lambda: None
            caller = _make_mock_instance(
                instance_name="caller1",
                agent_class="coder",
                slot_release=release_cb,
            )

            router_with_endpoints.set_agent_priorities("coder", ["ep_sequential"])
            router_with_endpoints.set_agent_priorities(child_class, [endpoint_id])

            pool = _make_mock_pool(router_with_endpoints, caller)
            dispatcher = _create_dispatcher(pool)

            result = dispatcher.handle_call_agent(
                args={"instance_name": "child1", "agent_class": child_class, "task": "test"},
                messages=[],
                instance=caller,
            )

            is_async = pool.register_async_call.called
            results[child_class] = {
                "endpoint": endpoint_id,
                "took_async_path": is_async,
                "expected_async": expected_async,
                "reason": reason,
            }

        # Verify each case matches expected behavior after fix:
        # conc=0 → sync (shared sequential pool)
        assert not results["security"]["took_async_path"], \
            f"conc=0 child should be SYNC. Results: {results}"
        
        # same endpoint as caller (ep_sequential for both) → SYNC (collision)
        assert not results["coder_child"]["took_async_path"], \
            f"Same pool collision should be SYNC. Results: {results}"
        
        # different parallel endpoint → ASYNC
        assert results["parallel_worker"]["took_async_path"], \
            f"Different pool should be ASYNC. Results: {results}"
        
        # unlimited child → ASYNC
        assert results["unlimited_worker"]["took_async_path"], \
            f"Unlimited child should be ASYNC. Results: {results}"