"""T1.5 / BUG_0052-sibling regression: a SESSION-RESTORED instance must carry ``_pool_ref``.

Two failure modes guarded here (see plans/t15-security-endpoint-fix_PLAN.md):
  1. ``load_session_from_log`` installed the restored AgentInstance without setting ``_pool_ref``,
     so ``release_slot_permit``'s router recovery returned None and silently skipped the single
     ``_last_released_endpoint`` write + committed/held cleanup — leaving the global marker stale,
     so Tier 1.5 handed the next endpointless agent a foreign model on a shared api_base.

The primary test drives the REAL production path (``SessionIOMixin.load_session_from_log``) with a
minimal pool double; the release test uses a real router + a session-restored holder. Fails pre-fix
(restored ``_pool_ref`` is None), passes post-fix. The shared-base multi-model fixture mirrors
production: LM Studio endpoints all on one api_base, differing only by ``model``.
"""

import json
import os
import threading
from types import SimpleNamespace

import pytest

from agent_cascade.api_router_pkg.normalization import normalize_api_base
from agent_cascade.pool.session_io import SessionIOMixin
from agent_cascade.slot_queue import release_slot_permit
# Reuse the shared isolated router fixture + helper from conftest (no real endpoints, FAST policy).
from tests.conftest import _add_endpoint  # noqa: E402

# ── Shared base for every enabled endpoint — mirrors config/api_endpoints.json where
# multiple endpoints share one api_base and differ only by `model`. ──────────────
SHARED_BASE = 'http://127.0.0.1:1234/v1'
A_NAME, A_MODEL = 'LMS-Agents-A1-35B-MTP', 'Agents-A1-APEX-I-Quality'   # stale marker (top-level)
B_NAME, B_MODEL = 'LMS-27B-3.8-MTP', 'qwen3.8-27b'                       # caller's endpoint

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _repo_root_strays():
    """Return the set of orchestrator_*.jsonl files in the repo root (the leak location).

    PROJECT_ROOT is the AgentCascade repo root (NOT the workspace). A real AgentInstanceLogger
    whose log_dir resolves to the process CWD writes orchestrator_Maine_<ts>.jsonl here.
    """
    import glob
    return set(glob.glob(os.path.join(PROJECT_ROOT, 'orchestrator_*.jsonl')))


@pytest.fixture(autouse=True)
def _no_stray_orchestrator_logs():
    """Regression guard: catch a stray orchestrator_*.jsonl leaking into the repo root if the
    session-restore path (``SessionIOMixin.load_session_from_log``) ever constructs a real
    AgentInstanceLogger with a CWD-resolving log_dir. Snapshots the repo-root globs before/after
    each test so a pre-existing (stale) stray does not false-fail, but a NEW leak does.
    """
    before = _repo_root_strays()
    yield
    new = _repo_root_strays() - before
    assert not new, (
        f"Test leaked {len(new)} new orchestrator log(s) into repo root {PROJECT_ROOT}: "
        f"{sorted(os.path.basename(p) for p in new)}"
    )


class _FakeInstanceLogger:
    """Lightweight stand-in for AgentInstanceLogger so load_session_from_log's post-load logger
    setup constructs NO real logger and writes NO file to CWD.

    The restore path only ever calls ``rewrite_log_with_history`` on the instance and stores it in
    ``_logger._loggers``; it never reads any real attribute. ``copy_session_file`` is intentionally
    NOT provided — it is only reached when the log metadata carries an existing ``current_log_path``,
    which these tests never do.
    """

    def __init__(self, *args, **kwargs):
        pass

    def rewrite_log_with_history(self, *args, **kwargs):
        # No-op: the real method would write the JSONL; we must not touch the filesystem.
        return None


@pytest.fixture(autouse=True)
def _no_real_instance_logger(monkeypatch):
    """Route load_session_from_log's AgentInstanceLogger construction to _FakeInstanceLogger.

    ``load_session_from_log`` imports AgentInstanceLogger function-locally (at call time), so patching
    the module attribute is picked up. Without this, the pool double's log_dir='.' resolves to the
    process CWD (repo root) and a real logger writes a stray orchestrator_Maine_<ts>.jsonl there
    (see _no_stray_orchestrator_logs).
    """
    import agent_cascade.logger.agent_instance_logger as ail
    monkeypatch.setattr(ail, 'AgentInstanceLogger', _FakeInstanceLogger)


class _PoolDouble(SessionIOMixin):
    """A minimal AgentPool double that runs the REAL load_session_from_log.

    A plain-object double (NOT a MagicMock — which auto-creates any attribute and would make the
    absence-guard vacuous). It exposes ONLY the attributes load_session_from_log reads:

      - ``instances``            : registration target + dismissal lookup
      - ``_execution._state_lock``: the lock the install block runs under (RLock)
      - ``_logger``              : a logger double whose _lock/_loggers/close are no-ops
      - ``instance_summaries``   : dict sink for compression summaries

    It carries NO api_router here on purpose — the point of test 1 is that the RESTORED instance's
    ``_pool_ref`` points at THIS pool object (the same `self` load_session_from_log runs on), exactly
    as Change 1 wires it. Test 2 then uses a separate double that ALSO exposes api_router so the full
    release funnel can recover the router end-to-end.
    """

    def __init__(self, api_router=None):
        self.instances = {}
        self._instances_version = 0
        self.instance_summaries = {}
        self._execution = SimpleNamespace(_state_lock=threading.RLock())
        # workspace_dir=None → the real _parse_json_input falls back to DEFAULT_WORKSPACE for any
        # relative path; our JSONL input is parsed line-by-line (Strategy 2) so no file I/O occurs.
        self._logger = SimpleNamespace(
            _lock=threading.RLock(),
            _loggers={},
            log_dir='.',
            workspace_dir=None,
        )
        if api_router is not None:
            self.api_router = api_router

    # Minimal stand-ins for the helpers load_session_from_log calls (real ones live on other mixins).
    def _dismiss_all_instances(self, exclude=None):
        # No-op in the double: we start with an empty pool, so there is nothing to dismiss.
        return 0

    def _resolve_instance_name(self, name):
        # Minimal stand-in for the real name resolver (strips/normalises; here: identity).
        return str(name).strip() if name else 'RecoveredSession'


def _make_pool(router):
    """A pool double that exposes api_router so release_slot_permit can recover it end-to-end."""
    return _PoolDouble(api_router=router)


def _attach_permit(holder, router, name):
    """Give a holder a live slot permit + state lock so release_slot_permit can capture/release it."""
    pool = router.scheduler._get_or_create_pool(SHARED_BASE, 0)
    rel = router.scheduler.acquire(SHARED_BASE, 0, instance_name=name, agent_class='orchestrator')
    holder._slot_release = rel
    holder._slot_key = pool.key if pool is not None else None


def _sample_log(instance_name):
    """A minimal valid JSONL session log: one system + one user message (→ non-empty working set)."""
    lines = [
        json.dumps({'role': 'system', 'content': 'You are a test orchestrator.'}),
        json.dumps({'role': 'user', 'content': 'Do the thing.',
                    'metadata': {'instance_name': instance_name, 'agent_class': 'Orchestrator'}}),
    ]
    return '\n'.join(lines)


class TestSessionRestorePoolRef:

    def test_load_session_from_log_sets_pool_ref(self, router):
        """T1.5 Change 1 (PRIMARY): a session-restored instance must carry ``_pool_ref``.

        Pre-fix, load_session_from_log installs the fresh AgentInstance without setting _pool_ref →
        it stays None → release_slot_permit cannot recover the router → the _last_released_endpoint
        write and committed/held cleanup are silently skipped (the incident). Post-fix, _pool_ref is
        set to the pool (the same `self` the method runs on), mirroring lifecycle.py:90.
        """
        pool = _make_pool(router)
        log_text = _sample_log('Maine')

        status = pool.load_session_from_log(
            log_text, target_instance='Maine', clear_sub_agents_before_load=False)

        assert not str(status).startswith('Error'), f"session load failed: {status}"
        # The restored instance was installed into the pool:
        inst = pool.instances.get('Maine')
        assert inst is not None, 'load_session_from_log did not install an instance named Maine'
        # THE FIX (Change 1): _pool_ref must be set so release_slot_permit can recover the router.
        assert inst._pool_ref is not None, \
            'T1.5: load_session_from_log did not set _pool_ref on the restored instance — ' \
            'release_slot_permit will silently skip the _last_released_endpoint write'
        # And it must point at THIS pool (the same object the method ran on):
        assert inst._pool_ref is pool, \
            'T1.5: restored instance _pool_ref does not point at the pool that installed it'
        # ...and that pool exposes the router under test (what _get_router_from_holder reads).
        assert getattr(inst._pool_ref, 'api_router', None) is router, \
            'T1.5: restored instance _pool_ref.api_router is not the expected router'


class TestSessionRestoreReleaseLastReleasedEndpoint:

    def test_session_restored_release_no_recovery_warning(self, router, caplog):
        """T1.5: releasing a session-restored holder emits the marker DEBUG and ZERO recovery WARNINGs.

        The incident's signature was a double "router recovery failed on release" WARNING per release
        (slot_queue.py). Post-fix, a session-restored holder carries _pool_ref, so router recovery
        succeeds and the release writes exactly one ``[SLOT] {name}: last_released_endpoint → {id}``
        DEBUG with NO "router recovery failed" WARNING.
        """
        import logging

        _add_endpoint(router, 'a', SHARED_BASE, model=A_MODEL, concurrency_limit=0)
        _add_endpoint(router, 'b', SHARED_BASE, model=B_MODEL, concurrency_limit=0)
        A_ID = router._endpoint_id_for_key(normalize_api_base(SHARED_BASE), A_MODEL)
        B_ID = router._endpoint_id_for_key(normalize_api_base(SHARED_BASE), B_MODEL)

        # Stale-A global marker (the incident's precondition).
        with router._lock:
            router._last_released_endpoint = A_ID

        pool = _make_pool(router)
        status = pool.load_session_from_log(_sample_log('Maine'),
                                            target_instance='Maine',
                                            clear_sub_agents_before_load=False)
        assert not str(status).startswith('Error'), f"session load failed: {status}"
        holder = pool.instances.get('Maine')
        assert getattr(holder._pool_ref, 'api_router', None) is router
        _attach_permit(holder, router, 'Maine')

        # Maine committed B (the endpoint it ran on).
        with router._lock:
            router._instance_committed_endpoint['Maine'] = \
                (normalize_api_base(SHARED_BASE), B_MODEL)

        with caplog.at_level(logging.DEBUG, logger='agent_cascade_logger'):
            ok = release_slot_permit(holder, 'Maine', action='drop-exit')
        assert ok is True, 'release_slot_permit should have captured and released a live permit'

        # THE marker must be refreshed to Maine's endpoint (B), not stay stale (A).
        assert router._last_released_endpoint == B_ID, \
            f"T1.5: session-restored release did not refresh _last_released_endpoint — " \
            f"expected B id {B_ID}, got {router._last_released_endpoint!r} (stale A = {A_ID})"

        # Exactly ONE marker-write DEBUG line for Maine, and ZERO "router recovery failed" WARNINGs.
        marker_writes = [r for r in caplog.records
                         if 'Maine' in r.getMessage()
                         and 'last_released_endpoint →' in r.getMessage()]
        assert len(marker_writes) == 1, \
            f"T1.5: expected exactly one last_released_endpoint DEBUG for Maine, " \
            f"got {len(marker_writes)}: {[r.getMessage() for r in marker_writes]}"

        recovery_warnings = [r for r in caplog.records
                             if r.levelno == logging.WARNING
                             and 'router recovery failed' in r.getMessage()]
        assert not recovery_warnings, \
            f"T1.5: session-restored release still logged a router-recovery WARNING " \
            f"(the incident's signature): {[r.getMessage() for r in recovery_warnings]}"
