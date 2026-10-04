"""BUG_0052 regression: a call_agent child must get ``_pool_ref`` set at registration.

Root cause (plans/BUG_0052_security_model_swap_ROOT_CAUSE.md §6, §7):
``lifecycle_manager.py:find_or_create_instance`` registers call_agent children directly into
``pool.instances`` WITHOUT setting ``inst._pool_ref`` (that assignment lives only in
``pool/lifecycle.py:90``). ``release_slot_permit`` recovers the router solely via
``holder._pool_ref.api_router`` (slot_queue.py:578-581), so for a child whose ``_pool_ref is None``
the entire ``_last_released_endpoint`` write block AND the committed-marker cleanup block are
silently skipped. The marker therefore stays frozen at whatever the top-level orchestrator last
wrote, and Tier 1.5's ``_last_released_endpoint or _last_active_endpoint`` (router.py:1043) hands an
unassigned Security check a STALE model on a shared api_base.

The primary test drives the REAL production path — ``AgentLifecycleManager.find_or_create_instance``
— and asserts the returned child instance carries ``_pool_ref`` (Change A). Pre-fix that attribute
is left at its dataclass default (None), so the release funnel cannot recover the router; post-fix
it is set on BOTH the fresh-creation and reuse paths.

The shared-base multi-model fixture mirrors PRODUCTION (N:\\work\\WD\\AgentCascade\\config\\api_endpoints.json
— 8 LM Studio endpoints all on http://127.0.0.1:1234/v1, differing only by ``model``). A two-distinct-bases
config cannot express this failure.

Fails pre-fix (Change A missing → child._pool_ref is None); passes post-fix.
"""

import threading
from types import SimpleNamespace

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.api_router_pkg.normalization import normalize_api_base
from agent_cascade.lifecycle_manager import AgentLifecycleManager
from agent_cascade.slot_queue import release_slot_permit
# Reuse the shared isolated router fixture + helper from conftest (no real endpoints, FAST policy).
from tests.conftest import _add_endpoint  # noqa: E402

# ── Shared base for every enabled endpoint — mirrors config/api_endpoints.json where
# multiple endpoints share one api_base and differ only by `model`. ──────────────
SHARED_BASE = 'http://127.0.0.1:1234/v1'
A_NAME, A_MODEL = 'LMS-Agents-A1-35B-MTP', 'Agents-A1-APEX-I-Quality'   # caller's endpoint
B_NAME, B_MODEL = 'LMS-27B-3.8-MTP', 'qwen3.8-27b'                       # stale marker (top-level)


def _make_child_pool(router):
    """A realistic AgentPool double for the ``find_or_create_instance`` path.

    A plain-object double (NOT a MagicMock — which auto-creates any attribute and would make the
    absence-guard vacuous). It exposes ONLY the attributes the code path actually reads; anything
    not read stays absent, which is what keeps the guard meaningful:

      - ``instances``        : registration target (lifecycle_manager.py:240) + reuse lookup (:185)
      - ``api_router``       : the router under test — this is what _pool_ref must point at so that
                               release_slot_permit's _get_router_from_holder can recover it.
      - ``_resolve_instance_name`` / ``_update_child_relationship``: helpers the method calls.
    A real AgentPool has NO ``.engine`` attribute — we never reference one.
    """
    return SimpleNamespace(
        instances={},
        api_router=router,
        _resolve_instance_name=lambda name: name,
        _update_child_relationship=lambda parent, name, add=True: None,
        # Reuse path (via _prepare_instance_for_reuse) reads these:
        _children_lock=threading.RLock(),
    )


def _attach_permit(holder, router, name):
    """Give a holder a live slot permit + state lock so release_slot_permit can capture/release it."""
    pool = router.scheduler._get_or_create_pool(SHARED_BASE, 0)
    rel = router.scheduler.acquire(SHARED_BASE, 0, instance_name=name, agent_class='reviewer')
    holder._slot_release = rel
    holder._slot_key = pool.key if pool is not None else None


class TestChildPoolRefRegistration:

    def test_find_or_create_sets_pool_ref_fresh(self, router):
        """BUG_0052 Change A (fresh path): a newly-created call_agent child must carry ``_pool_ref``.

        Pre-fix, lifecycle_manager.py registers the instance into pool.instances without setting
        _pool_ref → it stays None → release_slot_permit cannot recover the router → the
        _last_released_endpoint write and committed-marker cleanup are silently skipped.
        """
        child_pool = _make_child_pool(router)
        manager = AgentLifecycleManager(child_pool)

        inst, is_reuse, _loaded = manager.find_or_create_instance(
            agent_class='reviewer', instance_name='bug44-caller',
            caller='Maine', nest_depth=1, force_fresh=False, log_file=None)

        assert is_reuse is False
        # Registered into the pool (lifecycle_manager.py:240):
        assert child_pool.instances.get('bug44-caller') is inst
        # THE FIX (Change A): _pool_ref must be set so release_slot_permit can recover the router.
        assert inst._pool_ref is not None, \
            'BUG_0052: find_or_create_instance (fresh path) did not set _pool_ref on the child — ' \
            'release_slot_permit will silently skip the _last_released_endpoint write'
        # And it must point at a pool exposing THIS router (what _get_router_from_holder reads).
        assert getattr(inst._pool_ref, 'api_router', None) is router, \
            'BUG_0052: child._pool_ref.api_router is not the expected router'

    def test_find_or_create_sets_pool_ref_reuse(self, router):
        """BUG_0052 Change A (reuse path): a REUSED call_agent child must also carry ``_pool_ref``.

        Reuse returns an instance that may have been created by either path; the plan requires
        _pool_ref to be set unconditionally on the returned/registered instance. Pre-fix, a reused
        child that was never given _pool_ref stays None even after reuse.
        """
        child_pool = _make_child_pool(router)
        manager = AgentLifecycleManager(child_pool)

        # Seed an existing IDLE instance in the pool (the reuse candidate). Pre-fix it has no _pool_ref.
        existing = AgentInstance(
            instance_name='bug44-caller', agent_class='reviewer', conversation=[],
            created_at=0.0, last_activity=0.0, latest_marker_index=-1, state=AgentState.IDLE)
        assert existing._pool_ref is None  # documents the pre-fix defect on a reused child
        child_pool.instances['bug44-caller'] = existing

        inst, is_reuse, _loaded = manager.find_or_create_instance(
            agent_class='reviewer', instance_name='bug44-caller',
            caller='Maine', nest_depth=1, force_fresh=False, log_file=None)

        assert is_reuse is True
        assert inst is existing
        # THE FIX (Change A): reuse must set _pool_ref too.
        assert inst._pool_ref is not None, \
            'BUG_0052: find_or_create_instance (reuse path) did not set _pool_ref on the reused child'
        assert getattr(inst._pool_ref, 'api_router', None) is router


class TestChildReleaseLastReleasedEndpoint:

    def test_child_release_updates_last_released_endpoint(self, router):
        """BUG_0052 end-to-end: a call_agent child's release refreshes the global marker to ITS endpoint.

        Mirrors the incident: two enabled endpoints share ONE api_base (differ only by model). The
        top-level orchestrator last released B (stale marker), the caller committed A. When the
        caller (a child) releases, _last_released_endpoint must refresh to A — not stay frozen at B —
        so an unassigned Security check resolves onto A's model.

        The holder is wired with a live ``_pool_ref`` (the post-fix state Change A produces); the
        guard that actually fails pre-fix is the registration test above. This test pins the DOWNSTREAM
        consequence: with _pool_ref present, the release funnel records the marker and cleans up the
        committed entry — the exact write block BUG_0052's silent-skip disabled for children.
        """
        # Shared-base multi-model fixture (mirror production).
        _add_endpoint(router, 'a', SHARED_BASE, model=A_MODEL, concurrency_limit=0)
        _add_endpoint(router, 'b', SHARED_BASE, model=B_MODEL, concurrency_limit=0)

        # Real API surface checks (per plan §7 — do not invent attributes).
        assert hasattr(router, '_endpoint_id_for_key')          # router.py:559
        A_ID = router._endpoint_id_for_key(normalize_api_base(SHARED_BASE), A_MODEL)
        B_ID = router._endpoint_id_for_key(normalize_api_base(SHARED_BASE), B_MODEL)
        assert A_ID is not None and B_ID is not None and A_ID != B_ID

        # Simulate the incident's marker state: stale-B global marker + caller's last-active A.
        with router._lock:
            router._last_released_endpoint = B_ID      # stale — as Maine left it
            router._last_active_endpoint = A_ID        # caller's last success (router.py:2186)

        # The caller (a call_agent child) committed A on its successful LLM call.
        with router._lock:
            router._instance_committed_endpoint['bug44-caller'] = \
                (normalize_api_base(SHARED_BASE), A_MODEL)

        # Build the child via the REAL production path and wire a live permit. Post-fix this yields
        # a holder whose _pool_ref recovers the router — exactly what release_slot_permit needs.
        child_pool = _make_child_pool(router)
        manager = AgentLifecycleManager(child_pool)
        holder, is_reuse, _ = manager.find_or_create_instance(
            agent_class='reviewer', instance_name='bug44-caller',
            caller='Maine', nest_depth=1, force_fresh=False, log_file=None)
        assert getattr(holder._pool_ref, 'api_router', None) is router
        _attach_permit(holder, router, 'bug44-caller')

        ok = release_slot_permit(holder, 'bug44-caller', action='drop-handoff')
        assert ok is True, 'release_slot_permit should have captured and released a live permit'

        # THE marker must be refreshed to the caller's endpoint (A), not stay stale (B).
        assert router._last_released_endpoint == A_ID, \
            f"BUG_0052: child release did not refresh _last_released_endpoint — " \
            f"expected A id {A_ID}, got {router._last_released_endpoint!r} (stale B = {B_ID})"

        # THE leak: the committed marker must be cleaned up (popped) on release.
        with router._lock:
            assert 'bug44-caller' not in router._instance_committed_endpoint, \
                'BUG_0052 leak: committed marker was not popped for a call_agent child'

        # End-to-end consequence: an unassigned Security check (Tier 1.5) now resolves onto the
        # caller's model A — NOT the stale B that the frozen marker would have handed it.
        chain = router.get_endpoint_chain('Security', instance_name='Security_guard')
        assert chain, 'expected a non-empty endpoint chain for an unassigned agent'
        assert chain[0].get('model') == A_MODEL, \
            f"BUG_0052: Security resolved onto {chain[0].get('model')!r}, expected the caller's " \
            f"{A_MODEL!r} (stale marker routed it to {B_MODEL})"

    def test_release_warns_when_router_unrecoverable(self, router, caplog):
        """BUG_0052 Change C: a silent skip of endpoint bookkeeping must be VISIBLE.

        When ``_get_router_from_holder`` yields None (holder._pool_ref is None — the pre-fix child
        state), release_slot_permit previously swallowed the whole write block behind a bare
        ``except Exception: pass``. Change C replaces that with a WARNING naming the holder, so this
        class of failure can never again hide for an entire session.
        """
        _add_endpoint(router, 'a', SHARED_BASE, model=A_MODEL, concurrency_limit=0)

        # A child whose _pool_ref is None → router recovery returns None (the pre-fix defect state).
        holder = AgentInstance(
            instance_name='bug44-caller', agent_class='reviewer', conversation=[],
            created_at=0.0, last_activity=0.0, latest_marker_index=-1)
        assert holder._pool_ref is None
        _attach_permit(holder, router, 'bug44-caller')

        # Seed a committed marker so the skipped write block had something to record.
        with router._lock:
            router._instance_committed_endpoint['bug44-caller'] = \
                (normalize_api_base(SHARED_BASE), A_MODEL)

        # slot_queue logs through the SHARED agent_cascade_logger (via _AppLoggerProxy), not a
        # per-module logger — caplog must target that name or records are silently dropped.
        import logging
        with caplog.at_level(logging.WARNING, logger='agent_cascade_logger'):
            release_slot_permit(holder, 'bug44-caller', action='drop-handoff')

        # Change C: a WARNING naming the holder must be emitted (pre-fix: bare except → silent).
        warns = [r for r in caplog.records
                 if r.levelno == logging.WARNING and 'bug44-caller' in r.getMessage()]
        assert warns, \
            'BUG_0052 Change C: no WARNING was logged when router recovery failed for ' \
            "'bug44-caller' — the silent-except anti-pattern is still present"
