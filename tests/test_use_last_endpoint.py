"""TODO 90 — "Use Last Endpoint" selector entry + global-endpoint default fallback.

Feature contract tests for the reserved ``USE_LAST_ENDPOINT`` sentinel that rides
``agent_priorities[type]`` to make the global last-active (Tier 1.5) fallback an EXPLICIT
per-agent opt-in. Scope B (confirmed):

  * Sentinel selected          → [last-active, global default]   (today's unassigned behavior, explicit).
  * No assignment, no sentinel → [global default] only          (the behavior change — skips Tier 1.5).
  * Assigned (own endpoints)   → unchanged                       (own-first, auto Tier 1.5, default last).

Only router internals are exercised; no HTTP calls (get_endpoint_chain does not probe).
The lazy sanity probe is disabled so fake endpoints are never pruned. See
.bug_tracker/TODO_90_plan.md §4 (cases R1–R7) and
.agent_lessons/selector-save-paths-sentinel-strip-trap.md.
"""

import logging

import pytest

from agent_cascade.api_router import APIEndpoint
from agent_cascade.api_router_pkg.router import USE_LAST_ENDPOINT
# Reuse the shared router fixture + helpers from conftest (isolated config dir, FAST policy).
from tests.conftest import _add_endpoint


@pytest.fixture(autouse=True)
def _disable_sanity_probe():
    """Disable the lazy per-endpoint sanity probe so fake endpoints aren't pruned.

    Mirrors tests/test_e2e_fallback_chain.py. get_endpoint_chain does not issue HTTP,
    but keep parity so any future call in these tests stays hermetic.
    """
    import agent_cascade.api_router_pkg.router as router_mod
    orig = router_mod.SANITY_PROBE_ENABLED
    router_mod.SANITY_PROBE_ENABLED = False
    try:
        yield
    finally:
        router_mod.SANITY_PROBE_ENABLED = orig


def _ep_dict(name, api_base, model, enabled=True):
    """APIEndpoint dict for from_dict() payloads (stable id = ep_<name>)."""
    return APIEndpoint(
        id=f"ep_{name}",
        name=name,
        api_base=api_base,
        model=model,
        enabled=enabled,
    ).to_dict()


class TestUseLastEndpoint:

    def test_R1_no_assignment_no_sentinel_falls_to_global_default(self, router):
        """R1: live last-active marker but NO assignment and NO sentinel → [default] only.

        The behavior change: a bare marker is no longer auto-inherited by unassigned agents.
        """
        _add_endpoint(router, 'c', 'http://c-api', model='model-c')
        with router._lock:
            router._last_active_endpoint = 'ep_c'

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        assert [c['api_base'] for c in chain] == ['http://default-api']

    def test_R2_sentinel_opts_into_last_active(self, router):
        """R2: sentinel + live marker → [last-active, default] (restores old unassigned behavior)."""
        _add_endpoint(router, 'c', 'http://c-api', model='model-c')
        with router._lock:
            router._last_active_endpoint = 'ep_c'
        router.set_agent_priorities('security', [USE_LAST_ENDPOINT])

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        assert [c['api_base'] for c in chain] == ['http://c-api', 'http://default-api']
        assert chain[0]['model'] == 'model-c'

    def test_R3_sentinel_survives_live_save_path(self, router):
        """R3 (critical gotcha): set_agent_priorities must NOT strip the reserved sentinel.

        Without the whitelist at set_agent_priorities the live REST/WS save path would drop
        the sentinel (while from_dict preserves it) → feature works only via bulk save.
        """
        router.set_agent_priorities('security', [USE_LAST_ENDPOINT])
        # The public accessor normalizes the key (stored as PascalCase 'Security') and is exactly
        # what the UI/save path reads back — returning the sentinel proves the save path kept it.
        assert router.get_agent_priorities('security') == [USE_LAST_ENDPOINT]

    def test_R4_sentinel_survives_from_dict_and_drives_chain(self, router):
        """R4: sentinel survives bulk/from_dict load and still opts the agent into Tier 1.5."""
        router.from_dict({
            'endpoints': [_ep_dict('c', 'http://c-api', 'model-c')],
            'agent_priorities': {'security': [USE_LAST_ENDPOINT]},
        })
        assert router.get_agent_priorities('security') == [USE_LAST_ENDPOINT]

        with router._lock:
            router._last_active_endpoint = 'ep_c'
        chain = router.get_endpoint_chain('security', instance_name='worker1')
        assert [c['api_base'] for c in chain] == ['http://c-api', 'http://default-api']

    def test_R5_sentinel_with_stale_marker_degrades_to_default_and_warns(self, router, caplog):
        """R5: sentinel set, marker points at a REMOVED endpoint → [default] + WARN."""
        router.set_agent_priorities('security', [USE_LAST_ENDPOINT])
        # Marker references an endpoint that does not exist (stale).
        with router._lock:
            router._last_active_endpoint = 'ep_gone'

        with caplog.at_level(logging.WARNING, logger='agent_cascade_logger'):
            chain = router.get_endpoint_chain('security', instance_name='worker1')

        assert [c['api_base'] for c in chain] == ['http://default-api']
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and 'stale endpoint ID' in r.getMessage()
        ]
        assert len(warnings) == 1, \
            f"expected one stale-ID WARNING, got {len(warnings)}: {[r.getMessage() for r in warnings]}"

    def test_R6_assigned_agent_unchanged(self, router):
        """R6 (back-compat): assigned agent keeps own-first + auto Tier 1.5 before default."""
        _add_endpoint(router, 'own', 'http://own-api', model='model-own')
        _add_endpoint(router, 'c', 'http://c-api', model='model-c')
        router.set_agent_priorities('coder', ['ep_own'])
        with router._lock:
            router._last_active_endpoint = 'ep_c'

        chain = router.get_endpoint_chain('coder', instance_name='w1')
        bases = [c['api_base'] for c in chain]
        # Tier-1 own first, Tier-1.5 (c) inserted before the Tier-4 default; default last.
        assert bases[0] == 'http://own-api', f"expected own endpoint first, got {bases}"
        assert bases[-1] == 'http://default-api', f"expected default last, got {bases}"
        assert bases[1] == 'http://c-api', f"expected last-active inserted after Tier-1, got {bases}"

    def test_R7_sentinel_marker_equals_default_dedup(self, router):
        """R7: sentinel set but last-active resolves to the Tier-4 default → [default] (no duplicate)."""
        # Marker endpoint shares the fixture default's (api_base, model) → Tier 1.5 must skip it.
        _add_endpoint(router, 'dflt', 'http://default-api', model='default-model')
        router.set_agent_priorities('security', [USE_LAST_ENDPOINT])
        with router._lock:
            router._last_active_endpoint = 'ep_dflt'

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        bases = [c['api_base'] for c in chain]
        assert bases == ['http://default-api'], f"expected single default (dedup), got {bases}"

    def test_R8_no_sentinel_stale_marker_does_not_warn(self, router, caplog):
        """R8: no sentinel + no own endpoints + STALE marker → [default] and NO Tier-1.5 WARN.

        Locks that the whole Tier-1.5 block (incl. its stale-ID WARN) is gated on the opt-in: a
        non-opted-in unassigned agent skips the block entirely, so no WARN is emitted (contrast R5).
        """
        # Marker references a removed endpoint, but 'security' did NOT opt in.
        with router._lock:
            router._last_active_endpoint = 'ep_gone'

        with caplog.at_level(logging.WARNING, logger='agent_cascade_logger'):
            chain = router.get_endpoint_chain('security', instance_name='worker1')

        assert [c['api_base'] for c in chain] == ['http://default-api']
        stale_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and 'stale endpoint ID' in r.getMessage()
        ]
        assert not stale_warnings, \
            f"non-opted-in agent must not hit Tier-1.5, but saw: {[r.getMessage() for r in stale_warnings]}"

    def test_R9_sentinel_only_agent_not_tier3_eligible(self, router):
        """R9 (follow-up #7): a sentinel-only agent must NOT become Tier-3 (last-successful) eligible.

        'Use Last Endpoint' must equal the no-assigned-slot behavior — never [.., last-successful, ..].
        Setup deliberately OMITS the last-active marker so endpoint_configs is EMPTY at the Tier-3
        check (T1.5 skipped), making Tier-3 the decisive branch: pre-fix a sentinel-only agent resolves
        to [last-successful, default]; post-fix it stays [default]. This is the genuine fail-before-fix.
        """
        _add_endpoint(router, 'ls', 'http://ls-api', model='model-ls')  # enabled real endpoint
        with router._lock:
            router._last_successful_endpoint_cfg = {
                'api_base': 'http://ls-api', 'model': 'model-ls', 'max_input_tokens': 1024,
            }
        # No _last_active_endpoint / _last_released_endpoint → Tier 1.5 does not append.
        router.set_agent_priorities('security', [USE_LAST_ENDPOINT])

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        bases = [c['api_base'] for c in chain]
        assert bases == ['http://default-api'], \
            f"sentinel-only agent must not be Tier-3 eligible, got {bases}"

    def test_R10_sentinel_last_active_excludes_last_successful(self, router):
        """R10 (follow-up #7 intent): sentinel + live last-active → [last-active, default]; Tier-3 absent.

        With a live last-active the chain leads with it (endpoint_configs non-empty → Tier-3 not
        consulted). A valid _last_successful_endpoint_cfg must NOT appear. Documents the confirmed
        [last-active, default] shape end-to-end.
        """
        _add_endpoint(router, 'la', 'http://la-api', model='model-la')
        _add_endpoint(router, 'ls', 'http://ls-api', model='model-ls')
        with router._lock:
            router._last_active_endpoint = 'ep_la'
            router._last_successful_endpoint_cfg = {
                'api_base': 'http://ls-api', 'model': 'model-ls', 'max_input_tokens': 1024,
            }
        router.set_agent_priorities('security', [USE_LAST_ENDPOINT])

        chain = router.get_endpoint_chain('security', instance_name='worker1')
        bases = [c['api_base'] for c in chain]
        assert bases == ['http://la-api', 'http://default-api'], \
            f"expected [last-active, default] with Tier-3 excluded, got {bases}"

    def test_R11_remove_last_real_endpoint_drops_tier3_eligibility(self, router):
        """R11 (invariant close): remove_endpoint must prune Tier-3 eligibility when the list becomes
        sentinel-only. A `[real_ep, sentinel]` list is Tier-3 eligible; after deleting the only real
        endpoint the list is `[sentinel]` and the type must leave _agent_types_with_priorities.
        """
        canon = router._normalize_agent_type('security')  # how set_agent_priorities keys the entry
        _add_endpoint(router, 'real', 'http://real-api', model='model-real')
        router.set_agent_priorities('security', ['ep_real', USE_LAST_ENDPOINT])
        assert canon in router._agent_types_with_priorities, \
            'a list with a real endpoint must be Tier-3 eligible'

        router.remove_endpoint('ep_real')

        assert router.get_agent_priorities('security') == [USE_LAST_ENDPOINT], \
            'priority list should remain, now sentinel-only'
        assert canon not in router._agent_types_with_priorities, \
            'sentinel-only list must not be Tier-3 eligible after the last real endpoint is removed'
