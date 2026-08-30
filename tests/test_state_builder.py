"""Regression tests for the state-builder approval-field contract.

Background (see reports/approval_banner_disappears_INVESTIGATION.md):

    The user-approval banner intermittently appeared then disappeared on its own
    until a page refresh. Root cause: BOTH ``build_state_from_pool`` and
    ``build_stream_update_from_pool`` emitted an ``approvals`` field in every
    payload. A stream tick / full-state snapshot that was *built* before a pending
    approval was registered carried ``approvals: []``; when it was *delivered*
    after the dedicated ``{'type': 'approvals', ...}`` broadcast (which correctly
    included the new approval), the client's unconditional overwrite clobbered
    ``state.approvals`` back to ``[]`` and hid the banner.

The chosen fix (Option A) removes the ``approvals`` field from STREAM-UPDATE
payloads only, leaving it in full-state payloads (initial load / refresh still need
it). The dedicated ``approvals`` WS message (broadcast by ``_approval_loop``) is the
sole source of approval show/clear during active generation.

This module pins that invariant server-side:

    * ``build_stream_update_from_pool()`` output MUST NOT contain an ``approvals`` key.
    * ``build_state_from_pool()`` output MUST contain an ``approvals`` key.

Self-contained: no network, no live API, no real AgentPool construction — the pool is
a MagicMock, mirroring the conventions in test_refactor_name_resolution.py.
"""

import threading
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Shared fake-pool builder
# ---------------------------------------------------------------------------

def _make_fake_pool(pending_approvals):
    """Build a MagicMock AgentPool sufficient to drive both state builders.

    ``pending_approvals`` is returned by ``operation_manager.list_pending_approvals()``
    so that ``_get_approvals(pool)`` surfaces a controlled value (we pass a non-empty
    list to make the full-state assertion meaningful).
    """
    instance = MagicMock()
    instance.conversation = []                # empty -> no cached version match
    instance._streaming_responses = None
    instance._compression_lock = threading.Lock()
    instance.compression_summary = ""

    pool = MagicMock()
    pool.get_instance.return_value = instance
    pool.instances = {}                        # dict(pool.instances) must work
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.stopped = False
    pool.is_paused.return_value = False

    om = MagicMock()
    om.list_pending_approvals.return_value = pending_approvals
    om.extra_work_folders_ro = []
    om.extra_work_folders_rw = []
    om.base_dir = "/tmp/fake-workspace"
    pool.operation_manager = om

    return pool


def _build_stream_update(pool):
    """Drive build_stream_update_from_pool far enough to reach its return dict.

    A fresh (uncached) instance forces the "recompute" branch, which lazily imports and
    calls ``_calc_stream_token_stats`` from streaming.py; we stand in for it with a
    sentinel so no real token math runs. Cache state is saved/restored around the call.
    """
    from agent_cascade.api_integration_pkg import state_builder, streaming

    saved_stats = dict(state_builder._cache_mgr.stream_token_stats)
    saved_versions = dict(state_builder._cache_mgr.stream_versions)
    try:
        with state_builder._cache_mgr._lock:
            state_builder._cache_mgr.stream_token_stats.clear()
            state_builder._cache_mgr.stream_versions.clear()

        def sentinel(pool_, name, conv, sr, resp):
            return ({'tokens': 0, 'words': 0}, {'tokens': 0, 'words': 0})

        with patch.object(streaming, '_calc_stream_token_stats', sentinel):
            result = state_builder.build_stream_update_from_pool(pool, "Maine")
    finally:
        with state_builder._cache_mgr._lock:
            state_builder._cache_mgr.stream_token_stats.clear()
            state_builder._cache_mgr.stream_versions.clear()
            state_builder._cache_mgr.stream_token_stats.update(saved_stats)
            state_builder._cache_mgr.stream_versions.update(saved_versions)

    return result


def _build_full_state(pool):
    """Drive build_state_from_pool to its return dict (no streaming cache involved)."""
    from agent_cascade.api_integration_pkg import state_builder

    return state_builder.build_state_from_pool(pool, "Maine", generating=True)


# ---------------------------------------------------------------------------
# Invariant: stream updates must NOT carry approvals; full state MUST.
# ---------------------------------------------------------------------------

def test_stream_update_omits_approvals_key():
    """build_stream_update_from_pool() output must NOT contain an 'approvals' key.

    Regression: a stale stream tick (built before an approval was registered) used to
    carry ``approvals: []`` and, delivered after the dedicated approvals broadcast,
    clobbered live approval state on the client — hiding the banner.
    """
    pool = _make_fake_pool(pending_approvals=[{'request_id': 'r1', 'tool': 'shell_cmd'}])
    result = _build_stream_update(pool)

    assert isinstance(result, dict), "expected a stream-update dict, got %r" % type(result)
    assert 'approvals' not in result, (
        "stream update must NOT include an 'approvals' key — approvals are delivered "
        "exclusively via the dedicated {'type':'approvals'} WS message from _approval_loop; "
        "including them here allows stale ticks to clobber live approval state."
    )


def test_full_state_includes_approvals_key():
    """build_state_from_pool() output MUST contain an 'approvals' key.

    Initial load / page refresh still needs the current pending-approval snapshot, so
    full-state payloads keep the field even though stream updates no longer carry it.
    """
    sentinel = [{'request_id': 'r1', 'tool': 'shell_cmd'}]
    pool = _make_fake_pool(pending_approvals=sentinel)
    result = _build_full_state(pool)

    assert isinstance(result, dict), "expected a full-state dict, got %r" % type(result)
    assert 'approvals' in result, (
        "full state MUST include an 'approvals' key — initial load / refresh relies on it."
    )
    # The value must be the live snapshot surfaced by _get_approvals(pool).
    assert result['approvals'] == sentinel


def test_stream_and_full_state_diverge_on_approvals():
    """The two builders diverge exactly on the 'approvals' key (the Option A contract).

    This is the sharpest statement of the invariant: same pool, one payload has the key
    and the other does not. It guards against someone "fixing" the stream update to
    re-add approvals, or accidentally dropping them from full state.
    """
    sentinel = [{'request_id': 'r1', 'tool': 'shell_cmd'}]
    pool = _make_fake_pool(pending_approvals=sentinel)

    stream_update = _build_stream_update(pool)
    full_state = _build_full_state(pool)

    assert isinstance(stream_update, dict) and isinstance(full_state, dict)
    assert 'approvals' not in stream_update
    assert 'approvals' in full_state
