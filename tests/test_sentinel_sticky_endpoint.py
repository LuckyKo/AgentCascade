"""BUG_0277 — per-instance STICKY endpoint for pure-sentinel agents.

A "pure sentinel" agent (agent_priorities[type] == [USE_LAST_ENDPOINT], no real
Tier-1 endpoints — e.g. Security / admin / Generalist) resolves its model in the
Tier-1.5 block from the GLOBAL last-active endpoint. When other agents (e.g. the
27b orchestrator and the flash researcher) alternate, that global marker bounces,
so the sentinel's resolved model flaps on EVERY call. Each transition moves the
KV-cache key and forces a full re-prefill — the entire `model_change` waste class
in the BUG_0277 telemetry (12 events, 78k tokens, 31% of the 252k total).

The fix makes Tier-1.5 STICKY per-instance: if THIS instance already committed to
an endpoint (`self._instance_committed_endpoint[instance_name]`, set after a real
successful call), pin to it instead of the flapping global, provided that endpoint
is still enabled. Only fall back to the global last-active when there is no valid
committed endpoint (brand-new instance, or the committed endpoint was disabled /
removed) — so a legitimate re-resolve is never defeated.

Only router internals are exercised (get_endpoint_chain does not probe); the lazy
sanity probe is disabled for parity with tests/test_use_last_endpoint.py.
"""

import pytest

from agent_cascade.api_router_pkg.normalization import normalize_api_base
from agent_cascade.api_router_pkg.router import USE_LAST_ENDPOINT
# Reuse the shared router fixture + helper from conftest (isolated config dir, FAST policy).
from tests.conftest import _add_endpoint


@pytest.fixture(autouse=True)
def _disable_sanity_probe():
    """Disable the lazy per-endpoint sanity probe so fake endpoints aren't pruned.

    get_endpoint_chain does not issue HTTP, but keep parity so any future call in
    these tests stays hermetic. Mirrors tests/test_use_last_endpoint.py.
    """
    import agent_cascade.api_router_pkg.router as router_mod
    orig = router_mod.SANITY_PROBE_ENABLED
    router_mod.SANITY_PROBE_ENABLED = False
    try:
        yield
    finally:
        router_mod.SANITY_PROBE_ENABLED = orig


def _commit(router, instance_name, api_base, model):
    """Populate the committed-endpoint marker exactly as call_with_fallback does:
    value shape (normalize_api_base(api_base), model)."""
    with router._lock:
        router._instance_committed_endpoint[instance_name] = (normalize_api_base(api_base), model)


class TestSentinelStickyEndpoint:

    def test_S1_sticky_committed_wins_over_flapping_global(self, router):
        """Core (fail-before-fix): a warm pure-sentinel instance that committed to
        model-27b must resolve to 27b even when the GLOBAL last-active points at flash.

        Pre-fix, Tier 1.5 read only the global marker → resolved model-27b? No: it
        resolved to flash (the global), so this assertion failed. Post-fix the pinned
        committed endpoint wins and the model stays stable.
        """
        _add_endpoint(router, 'flash', 'http://flash-api', model='model-flash')
        _add_endpoint(router, 'big', 'http://27b-api', model='model-27b')
        router.set_agent_priorities('security', [USE_LAST_ENDPOINT])

        inst = 'Security_guard'
        _commit(router, inst, 'http://27b-api', 'model-27b')  # this instance's last success
        with router._lock:
            router._last_active_endpoint = 'ep_flash'  # another agent just used flash → marker flapped

        chain = router.get_endpoint_chain('security', instance_name=inst)
        bases = [c['api_base'] for c in chain]
        assert bases == ['http://27b-api', 'http://default-api'], \
            f"sticky instance must stay on its committed endpoint, got {bases}"
        assert chain[0]['model'] == 'model-27b', \
            f"resolved model must match the committed endpoint, got {chain[0]['model']}"

    def test_S2_no_committed_endpoint_uses_global_last_active(self, router):
        """Preserve existing behavior: a brand-new pure-sentinel instance (no committed
        marker) still inherits the global last-active → [last-active, default].

        First call resolves via last-active, and the successful call commits it, so the
        very next call sticks (S1). This locks the inheritance behavior we must NOT lose.
        """
        _add_endpoint(router, 'c', 'http://c-api', model='model-c')
        router.set_agent_priorities('security', [USE_LAST_ENDPOINT])
        with router._lock:
            router._last_active_endpoint = 'ep_c'  # no committed marker set

        chain = router.get_endpoint_chain('security', instance_name='brand_new_worker')
        bases = [c['api_base'] for c in chain]
        assert bases == ['http://c-api', 'http://default-api'], \
            f"no committed endpoint must fall back to global last-active, got {bases}"

    def test_S3_disabled_committed_endpoint_falls_back_to_global(self, router):
        """Sticky must NOT defeat a legitimate re-resolve: if the committed endpoint has
        been DISABLED, fall back to the global last-active (not a dead endpoint).
        """
        _add_endpoint(router, 'flash', 'http://flash-api', model='model-flash')
        _add_endpoint(router, 'dead', 'http://dead-api', model='model-dead', enabled=False)
        router.set_agent_priorities('security', [USE_LAST_ENDPOINT])

        inst = 'Security_guard'
        _commit(router, inst, 'http://dead-api', 'model-dead')  # committed to a now-disabled endpoint
        with router._lock:
            router._last_active_endpoint = 'ep_flash'  # global last-active is a live endpoint

        chain = router.get_endpoint_chain('security', instance_name=inst)
        bases = [c['api_base'] for c in chain]
        assert bases == ['http://flash-api', 'http://default-api'], \
            f"committed-to-disabled must re-resolve via global last-active, got {bases}"

    def test_S4_committed_endpoint_equal_to_default_dedups(self, router):
        """Respect the existing Tier-1.5 dedup: if the committed endpoint IS the Tier-4
        default, pin it as [default] only — no duplicate, and no flap to the global.

        The global last-active is deliberately set to a different (flappy) endpoint; if
        the default-pin did not suppress the global path, we'd wrongly get [flappy, default].
        """
        _add_endpoint(router, 'flappy', 'http://flappy-api', model='model-flappy')
        router.set_agent_priorities('security', [USE_LAST_ENDPOINT])

        inst = 'Security_guard'
        # Committed to the fixture's own default endpoint (http://default-api / default-model).
        _commit(router, inst, 'http://default-api', 'default-model')
        with router._lock:
            router._last_active_endpoint = 'ep_flappy'  # global marker flapped elsewhere

        chain = router.get_endpoint_chain('security', instance_name=inst)
        bases = [c['api_base'] for c in chain]
        assert bases == ['http://default-api'], \
            f"committed==default must dedup to [default], got {bases}"
