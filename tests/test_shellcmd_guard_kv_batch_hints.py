"""shell_cmd parallel-batch guard KV hints — GUARD-HANDLER level.

Drives the REAL SecurityAdvisorHandler._execute_check against the REAL save/reacquire chain
(only the HTTP layer faked) so we assert the user-visible /state/save + /state/load issued by
the GUARD itself (not the engine net). This is the counterpart to
tests/test_shell_cmd_parallel_batch_guard_state_deferral.py (engine level).

The batch hints come from operation_manager._get_approval_kv_batch_hints(rid); we drive that
path with a side_effect table (a bare MagicMock would crash the tuple unpack in _execute_check).
Mirror of tests/e2e_security_slot_deadlock.py::_bind_real_restore_chain + _record_state_http.
"""

import os as _os
import threading

_os.environ.setdefault('AGENT_CASCADE_INSTANCE_ID', f"e2e_{_os.getpid()}")

from dataclasses import fields as _dc_fields
from types import SimpleNamespace

from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.security_handler import SecurityAdvisorHandler
from agent_cascade.operation_manager.approval import ApprovalMixin, PendingApproval

# Reuse the real slot-pool harness helpers from the e2e suite (same convention as tests.conftest).
from tests.e2e_security_slot_deadlock import (
    _build_pool,
    _build_real_router,
    _bind_real_restore_chain,
    _make_sec_instance,
    _record_state_http,
)

_ENDPOINT_CFG = {'api_base': 'http://127.0.0.1:9/v1', 'model': 'mock', 'state_save_enabled': True}


@pytest.fixture
def guard_harness(tmp_path, request):
    import agent_cascade.api_router_pkg.scheduler as _ar_mod
    import agent_cascade.slot_queue as _sq_mod

    cfg_dir = tmp_path / request.node.name.replace('/', '_')
    cfg_dir.mkdir(parents=True, exist_ok=True)
    old_cfg_dir = _os.environ.get('AGENT_CASCADE_TEST_CONFIG_DIR')
    _os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = str(cfg_dir)
    old_sq = _sq_mod.QUEUE_WAIT_TIMEOUT
    old_ar = _ar_mod.QUEUE_WAIT_TIMEOUT
    _sq_mod.QUEUE_WAIT_TIMEOUT = 5
    _ar_mod.QUEUE_WAIT_TIMEOUT = 5

    router = _build_real_router(cfg_dir)
    pool = _build_pool(router)
    old_window = getattr(pool.settings, 'slot_queue_timeout_seconds', None)
    pool.settings.slot_queue_timeout_seconds = 5

    om = MagicMock()
    om.base_dir = str(cfg_dir)
    om.extra_work_folders_ro = []
    om.extra_work_folders_rw = []
    om.enable_timeout = True
    om.approval_timeout_seconds = 180
    # The guard reads hints through this; a MagicMock alone would return a MagicMock and crash the
    # `(skip, defer) = fn(rid)` tuple unpack in _execute_check. Drive it from a mutable HINTS table.
    om._hints = {}
    om._get_approval_kv_batch_hints.side_effect = lambda rid: om._hints.get(rid, (False, False))
    pool.operation_manager = om

    if getattr(pool, '_execution', None) is None:
        pool._execution = MagicMock()
    pool._execution._state_lock = threading.Lock()
    pool.instance_state = {}

    shared = router.scheduler._get_or_create_pool('http://127.0.0.1:9/v1', 0)
    assert shared is not None

    import time as _t
    from agent_cascade.agent_instance import AgentInstance
    caller = AgentInstance(instance_name='caller', agent_class='coder', conversation=[],
                           created_at=_t.monotonic(), last_activity=_t.monotonic(), latest_marker_index=0)
    pool.instances['caller'] = caller
    release_cb = pool._acquire_slot('coder', 'caller')
    caller._slot_release = release_cb
    caller._slot_key = '_shared_sequential_slot_'
    assert 'caller' in shared._running

    app = type('App', (), {})()
    session = {'session_name': 'Maine', 'generate_cfg': {}}
    handler = SecurityAdvisorHandler(pool, session, app, MagicMock(), lambda *a, **k: None)

    yield {'pool': pool, 'om': om, 'caller': caller, 'handler': handler, 'release_cb': release_cb,
           'shared': shared}

    try:
        release_cb()
    except Exception:
        pass
    _sq_mod.QUEUE_WAIT_TIMEOUT = old_sq
    _ar_mod.QUEUE_WAIT_TIMEOUT = old_ar
    if old_window is not None:
        try:
            pool.settings.slot_queue_timeout_seconds = old_window
        except Exception:
            pass
    if old_cfg_dir is None:
        _os.environ.pop('AGENT_CASCADE_TEST_CONFIG_DIR', None)
    else:
        _os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = old_cfg_dir


def _ap(rid):
    return {'request_id': rid, 'tool_name': 'shell_cmd', 'description': 'echo hi',
            'tool_args': {'command': 'echo hi'}, 'agent_name': 'caller'}


def _drive(h, rid, *, skip=False, defer=False, toggle=True, safe_mode=False):
    """One REAL _execute_check with the REAL save/reacquire chain bound. Returns the ordered
    HTTP events [('save'|'load', label), ...]. The batch hints are read via the om accessor."""
    import agent_cascade.state_ops as state_ops

    pool, caller, handler = h['pool'], h['caller'], h['handler']
    caller._safe_mode_kv = safe_mode
    caller._last_endpoint_config = dict(_ENDPOINT_CFG)
    h['om']._hints[rid] = (skip, defer)

    events = []
    pool.operation_manager.user_approve.side_effect = lambda *a, **k: (events.append(('approve', None)), 'Approved')[1]

    eng = MagicMock()
    _bind_real_restore_chain(eng, pool, toggle=toggle)
    eng.run.return_value = iter([('[YES] safe to proceed', False)])
    eng._create_system_agent.side_effect = lambda **kw: _make_sec_instance(
        kw.get('instance_name', 'Security'), conversation_text='[YES] safe to proceed')
    eng._telemetry.return_value = None

    with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 5):
        with patch('agent_cascade.execution_engine.ExecutionEngine', MagicMock(return_value=eng)):
            with patch.object(state_ops.httpx, 'post') as post_mock:
                _record_state_http(post_mock, events)
                handler._execute_check(
                    ap=_ap(rid), sec_inst=None, rid=rid, auto_apply=True, instance_name='Maine',
                    caller_agent='caller', prompt_template='Analyze {tool_name}: {description} args={arguments}',
                    timeout_seconds=3600, warning_seconds=2400)
    return events


def _count(events, tag):
    return [lbl for t, lbl in events if t == tag]


def test_skip_save_issues_no_state_save(guard_harness):
    # Non-first guard: skip the save (reuses the batch's earlier save) => the guard issues no /state/save.
    events = _drive(guard_harness, 'rid_skip', skip=True, defer=False)
    assert _count(events, 'save') == [], f"kv_skip_save=True must issue NO /state/save, got {events}"


def test_no_skip_save_issues_one_state_save(guard_harness):
    events = _drive(guard_harness, 'rid_save', skip=False, defer=False)
    assert _count(events, 'save') == ['caller'], f"kv_skip_save=False must issue ONE /state/save, got {events}"


def test_defer_restore_keeps_label_pending_no_load(guard_harness):
    h = guard_harness
    caller = h['caller']
    events = _drive(h, 'rid_defer', skip=False, defer=True)
    assert _count(events, 'load') == [], f"kv_defer_restore=True must issue NO /state/load from the guard, got {events}"
    assert caller._state_label is not None, "defer must LEAVE the caller's _state_label pending for the last guard"


def test_middle_guard_skip_and_defer_keeps_label_pending_no_load(guard_harness):
    # UNTESTED MIDDLE CELL of the (skip, defer) x safe matrix — the (skip=True, defer=True, safe=False)
    # combination is a non-first, non-last guard of a n>=3 batch: it reuses guard1's save (skip) AND
    # keeps guard1's label alive (defer). First and last are covered above; only the middle is skipped,
    # and n<=2 batches are correct by construction, so a middle-guard bug is invisible to those.
    # Pre-arm the label guard1's save would have left, then assert THIS guard issues no /state/load and
    # preserves it. Under the old `defer = defer and not safe and not batch_skip_save` formula,
    # batch_skip_save=True forced defer OFF here -> a full mid-batch /state/load that CLEARED guard1's
    # label -> both assertions below fail red (spec-conformance, not a plumbing-call check).
    h = guard_harness
    caller = h['caller']
    caller._state_label = 'caller'  # stands in for the first guard's already-armed pending label
    events = _drive(h, 'rid_mid', skip=True, defer=True, safe_mode=False)
    assert _count(events, 'load') == [], (
        f"a middle guard (skip+defer, not safe) must issue NO /state/load, got {events}")
    assert _count(events, 'save') == [], (
        f"a middle guard (skip=True) must issue NO /state/save, got {events}")
    assert caller._state_label is not None, (
        "a middle guard's defer must LEAVE guard1's _state_label pending for the last guard to consume")


def test_safe_mode_forces_save_and_clears_label(guard_harness):
    h = guard_harness
    caller = h['caller']
    # TRAP §4.4: safe mode suppresses kv_skip_save (save forced ON) and forces defer OFF, then
    # skip_kv_restore=True CLEARS the label (no load from the guard — the tool restores by label later).
    events = _drive(h, 'rid_safe', skip=True, defer=True, safe_mode=True)
    assert _count(events, 'save') == ['caller'], f"safe mode must FORCE the save despite kv_skip_save, got {events}"
    assert _count(events, 'load') == [], f"safe mode skips the advisor load, got {events}"
    assert caller._state_label is None, 'safe mode must CLEAR the pending label (skip_kv_restore wins over defer)'


def test_single_guard_control_one_save_one_load(guard_harness):
    # No hints (single/non-batched guard) -> full save + full load, exactly as today.
    events = _drive(guard_harness, 'rid_control', skip=False, defer=False)
    assert _count(events, 'save') == ['caller']
    assert _count(events, 'load') == ['caller']
    assert guard_harness['caller']._state_label is None


def test_toggle_off_issues_zero_ops(guard_harness):
    # state_kv_save_enabled OFF and no safe-mode override -> the engine seams no-op: zero save/load.
    events = _drive(guard_harness, 'rid_off', skip=False, defer=False, toggle=False)
    assert _count(events, 'save') == [], f"toggle off must issue no save, got {events}"
    assert _count(events, 'load') == [], f"toggle off must issue no load, got {events}"


# Two-guard accumulation (first guard saves+defers, last guard skips-save + does the single full
# restore -> EXACTLY 1 save + 1 load across the batch) is proven end-to-end at the ENGINE level in
# tests/test_shell_cmd_parallel_batch_guard_state_deferral.py::test_multi_guard_batch_saves_once_and_loads_once.
# It is intentionally NOT re-proven by driving _execute_check twice here: the real handler caches/
# consumes per-rid state (active-check registry, Security-instance template) and does not faithfully
# re-enter the engine loop for a second rid on the same caller, so a handler-level double-drive would
# only measure harness carryover, not production. The per-guard EFFECTS (skip-save, defer-keep-label,
# safe-mode override, control save+load) — which ARE faithful single-call assertions — cover this layer.


def test_list_pending_approvals_does_not_leak_kv_hints():
    # The kv_* hints are INTERNAL (private accessor only); list_pending_approvals is the WebUI payload
    # and must not carry them. Bind the real method onto a minimal object; introspect required fields.
    om = SimpleNamespace(pending={}, _lock=threading.RLock())
    kw = {}
    for f in _dc_fields(PendingApproval):
        if f.default is not _dc_missing(f) or f.default_factory is not _dc_mf_missing(f):
            continue
        ann = str(f.type)
        if 'dict' in ann:
            kw[f.name] = {}
        elif 'list' in ann:
            kw[f.name] = []
        elif 'int' in ann:
            kw[f.name] = 0
        else:
            kw[f.name] = 'x'
    rec = PendingApproval(**kw)
    rec.kv_skip_save = True
    rec.kv_defer_restore = True
    om.pending['rid1'] = rec
    items = ApprovalMixin.list_pending_approvals(om)
    assert items, 'list_pending_approvals must return the record'
    for it in items:
        assert 'kv_skip_save' not in it, f"kv_skip_save leaked into WebUI payload: {sorted(it)}"
        assert 'kv_defer_restore' not in it, f"kv_defer_restore leaked into WebUI payload: {sorted(it)}"


def _dc_missing(f):
    import dataclasses
    return dataclasses.MISSING


def _dc_mf_missing(f):
    import dataclasses
    return dataclasses.MISSING


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
