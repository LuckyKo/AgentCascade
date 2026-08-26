"""Priority-swap cursor fix tests (Part 1 of the probe/cursor quick-fix plan).

Production bug: ``set_agent_priorities`` and ``update_endpoint`` mutate the priority
chain / endpoint set WITHOUT clearing ``_instance_endpoint_position``. Only
``from_dict`` cleared it. The per-instance cursor is a POSITIONAL index into the
tier chain, so after a live reorder it points at the WRONG endpoint (or out of
range if the new chain is shorter) → agent keeps retrying the same failing
endpoint / never reaches default → hang + model-loader flood.

Fix under test: both methods now clear ALL instance cursors under ``self._lock``
after mutation (mirroring the from_dict FIX-2a reset).

These tests use the REAL router code — no mocks of the cursor logic. The fixture
and helpers mirror tests/test_cursor_rotation_fallback_chain.py so the same real
APIRouter construction path is exercised.
"""

import os
from unittest.mock import patch

import pytest

from agent_cascade.api_router import APIRouter, APIEndpoint
from agent_cascade.retry_policy import RetryPolicy


# ============================================================================
# Fixtures and helpers (mirror test_cursor_rotation_fallback_chain.py)
# ============================================================================

FAST_RETRY_POLICY = RetryPolicy(
    retry_max_attempts=3,
    base_delay=0.01,
    max_delay=0.05,
    jitter_factor=0.0,
    endpoint_max_retries=1,
)


@pytest.fixture
def router(tmp_path_factory):
    """Create an isolated APIRouter instance with its own config dir."""
    test_config_dir = str(tmp_path_factory.mktemp("priority_swap_cursor_test"))

    with patch.dict(os.environ, {"AGENT_CASCADE_TEST_CONFIG_DIR": test_config_dir}):
        r = APIRouter(default_llm_cfg={
            'api_base': 'http://default-api',
            'model': 'default-model',
            'max_tokens': 2048,
        }, policy=FAST_RETRY_POLICY)
        r._pool = None
        yield r


def _add_endpoint(router, name, api_base, model='test-model', enabled=True):
    """Helper to add an endpoint to the router."""
    ep = APIEndpoint(
        id=f"ep_{name}",
        name=name,
        api_base=api_base,
        model=model,
        enabled=enabled,
    )
    router.add_endpoint(ep)


def _setup_three_endpoints(router):
    """Three real endpoints + 'coder' priorities [a, b, c]."""
    _add_endpoint(router, "ep_a", "http://a-api")
    _add_endpoint(router, "ep_b", "http://b-api")
    _add_endpoint(router, "ep_c", "http://c-api")
    router.set_agent_priorities("coder", ["ep_ep_a", "ep_ep_b", "ep_ep_c"])


# ============================================================================
# Test A — set_agent_priorities reorder clears the cursor
# ============================================================================

class TestSetAgentPrioritiesClearsCursor:
    """A live priority reorder must reset per-instance cursors so the next chain
    uses the NEW priority order, not a stale positional rotation."""

    def test_reorder_resets_cursor_and_head_is_new_top(self, router):
        _setup_three_endpoints(router)

        # Advance cursor to position 2 — under the OLD order [a, b, c] the next
        # chain would rotate to start at ep_c.
        router.advance_instance_endpoint("worker1")
        router.advance_instance_endpoint("worker1")
        assert router._instance_endpoint_position.get("worker1", 0) == 2

        # Sanity: with the old order and cursor=2, the chain head is ep_c.
        chain_old = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain_old[0]['api_base'] == 'http://c-api'

        # Live reorder via the REAL entry point: new priority [c, a, b].
        router.set_agent_priorities("coder", ["ep_ep_c", "ep_ep_a", "ep_ep_b"])

        # Cursor must be cleared for that instance.
        assert "worker1" not in router._instance_endpoint_position, \
            f"Cursor should be cleared after set_agent_priorities, got {router._instance_endpoint_position}"

        # Chain must reflect the NEW priority order with head = new top priority (ep_c),
        # NOT a stale positional rotation of the new chain.
        chain_new = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain_new[0]['api_base'] == 'http://c-api', \
            f"Expected new head ep_c, got {chain_new[0].get('api_base')}"
        tier_bases = [cfg['api_base'] for cfg in chain_new[:-1]]  # exclude Tier-4 default
        assert tier_bases == ['http://c-api', 'http://a-api', 'http://b-api'], \
            f"Expected fresh new order, got {tier_bases}"

    def test_reorder_clears_cursors_of_all_instances(self, router):
        """set_agent_priorities clears ALL cursors (clear-all semantics, like from_dict)."""
        _setup_three_endpoints(router)

        router.advance_instance_endpoint("worker1")
        router.advance_instance_endpoint("worker2")
        assert router._instance_endpoint_position == {"worker1": 1, "worker2": 1}

        router.set_agent_priorities("coder", ["ep_ep_b", "ep_ep_a", "ep_ep_c"])

        assert router._instance_endpoint_position == {}, \
            f"All cursors must be cleared, got {router._instance_endpoint_position}"

    def test_reorder_to_shorter_chain_no_stale_index(self, router):
        """Reordering to a SHORTER chain: the stale index would be out of range / wrong.
        After the fix the head is the new top priority."""
        _setup_three_endpoints(router)

        # Cursor at position 2 (valid for the 3-endpoint tier list).
        router.advance_instance_endpoint("worker1")
        router.advance_instance_endpoint("worker1")
        assert router._instance_endpoint_position.get("worker1", 0) == 2

        # New chain has only ONE tier endpoint — stale index 2 is meaningless.
        router.set_agent_priorities("coder", ["ep_ep_b"])

        assert "worker1" not in router._instance_endpoint_position
        chain = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain[0]['api_base'] == 'http://b-api', \
            f"Expected new head ep_b, got {chain[0].get('api_base')}"

    def test_no_cursors_present_is_safe_noop(self, router):
        """set_agent_priorities with no live cursors still applies the config and
        leaves the cursor store empty (idempotent)."""
        _setup_three_endpoints(router)

        router.set_agent_priorities("coder", ["ep_ep_b", "ep_ep_a"])

        assert router._instance_endpoint_position == {}
        chain = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain[0]['api_base'] == 'http://b-api'


# ============================================================================
# Test B — update_endpoint clears the cursor
# ============================================================================

class TestUpdateEndpointClearsCursor:
    """Any endpoint mutation (enable/disable, api_base change, ...) invalidates
    positional cursors; update_endpoint must clear them."""

    def test_disable_endpoint_clears_cursor(self, router):
        _setup_three_endpoints(router)

        router.advance_instance_endpoint("worker1")
        assert router._instance_endpoint_position.get("worker1", 0) == 1

        # Disable the head endpoint — it drops out of the tier chain entirely.
        ok = router.update_endpoint("ep_ep_a", {"enabled": False})
        assert ok is True

        assert "worker1" not in router._instance_endpoint_position, \
            f"Cursor should be cleared after update_endpoint, got {router._instance_endpoint_position}"

        # Head must now be the new top priority (ep_b), not a stale rotation.
        chain = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain[0]['api_base'] == 'http://b-api', \
            f"Expected head ep_b after disabling ep_a, got {chain[0].get('api_base')}"

    def test_api_base_change_clears_cursor(self, router):
        _setup_three_endpoints(router)

        router.advance_instance_endpoint("worker1")
        router.advance_instance_endpoint("worker1")
        assert router._instance_endpoint_position.get("worker1", 0) == 2

        # Change the head endpoint's api_base — same positional slot, different target.
        ok = router.update_endpoint("ep_ep_a", {"api_base": "http://a-api-v2"})
        assert ok is True

        assert "worker1" not in router._instance_endpoint_position
        chain = router.get_endpoint_chain("coder", instance_name="worker1")
        # Cursor reset → unrotated order → head is the (updated) ep_a.
        assert chain[0]['api_base'] == 'http://a-api-v2', \
            f"Expected updated head, got {chain[0].get('api_base')}"

    def test_update_nonexistent_endpoint_does_not_clear(self, router):
        """update_endpoint returns False for unknown IDs and must NOT touch cursors."""
        _setup_three_endpoints(router)

        router.advance_instance_endpoint("worker1")
        assert router._instance_endpoint_position.get("worker1", 0) == 1

        ok = router.update_endpoint("ep_nonexistent", {"enabled": False})
        assert ok is False
        # Early-return path — cursor untouched.
        assert router._instance_endpoint_position.get("worker1", 0) == 1


# ============================================================================
# Test C — regression guard: normal rotation still works without config changes
# ============================================================================

class TestNormalRotationUnchanged:
    """Without any config change, advancing the cursor must still rotate the chain
    exactly as before (the fix must not break normal kick-to-next-endpoint)."""

    def test_rotation_still_works(self, router):
        _setup_three_endpoints(router)

        # Cursor 0 → head a
        chain1 = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain1[0]['api_base'] == 'http://a-api'

        router.advance_instance_endpoint("worker1")
        chain2 = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain2[0]['api_base'] == 'http://b-api', \
            f"Expected rotation to ep_b, got {chain2[0].get('api_base')}"

        router.advance_instance_endpoint("worker1")
        chain3 = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain3[0]['api_base'] == 'http://c-api', \
            f"Expected rotation to ep_c, got {chain3[0].get('api_base')}"

    def test_wrap_around_still_works(self, router):
        _setup_three_endpoints(router)

        # Advance past the tier count — wraps (4 % 3 = 1 → head b).
        for _ in range(4):
            router.advance_instance_endpoint("worker1")

        chain = router.get_endpoint_chain("coder", instance_name="worker1")
        assert chain[0]['api_base'] == 'http://b-api', \
            f"Expected wrapped head ep_b, got {chain[0].get('api_base')}"

    def test_other_instances_unaffected_by_rotation(self, router):
        _setup_three_endpoints(router)

        router.advance_instance_endpoint("worker1")

        chain = router.get_endpoint_chain("coder", instance_name="worker2")
        assert chain[0]['api_base'] == 'http://a-api', \
            "Advancing one instance must not rotate another's chain"
