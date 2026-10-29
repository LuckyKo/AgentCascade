"""E2E reproduction of the security-advisor shared-sequential-slot deadlock.

GOAL (debugging, not fixing): drive the REAL `SecurityAdvisorHandler._execute_check`
slot-yield logic against a REAL `SlotPool` (`_shared_sequential_slot_`, capacity 1) and
capture full DEBUG logs so we can see EXACTLY which of the three yield paths fires:

  * `[SECURITY_SLOT_YIELD]`                    — normal yield (caller._slot_release not None)
  * `[SECURITY_SLOT_YIELD] LEAKED PERMIT DETECTED` — force-release fallback fired
  * `[SECURITY_SLOT_YIELD_SKIPPED]`            — NEITHER fired (this is the bug signature)

Design:
  - REAL `APIRouter` + conc=0 endpoint → REAL shared `_shared_sequential_slot_` SlotPool.
  - REAL `AgentPool` wired to that router (so `pool._acquire_slot` / `get_instance` are real).
  - A REAL caller instance holds the only permit (simulates a caller blocked in
    request_user_approval while still holding its slot — the production scenario).
  - We patch ONLY `ExecutionEngine.run` (to avoid a real LLM call) and `_create_system_agent`
    (to return a lightweight instance whose slot state we can inspect). The yield/force-release/
    skip logic, exec_lock, and the pool/slot machinery all run UNMOCKED.

Each test PASSES if the security check completes without deadlocking (the correct yield path
fires and the Security agent does NOT time out waiting for the shared slot), and FAILS with a
clear diagnostic report of which path fired + pool-holder state otherwise.
"""
# Isolate this standalone run's logs/telemetry from the production workspace.
# Must be set BEFORE any agent_cascade import (instance_id reads it at call time).
import os as _os

_os.environ.setdefault('AGENT_CASCADE_INSTANCE_ID', f"e2e_{_os.getpid()}")

import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.security_handler import SecurityAdvisorHandler

# ── Real slot-pool harness (no server, no LLM) ───────────────────────────────


def _build_real_router(tmp_path):
    """Real APIRouter with a single conc=0 endpoint → real shared sequential SlotPool."""
    from agent_cascade.api_router import APIEndpoint, APIRouter

    llm_cfg = {
        'model': 'mock',
        'api_base': 'http://127.0.0.1:9/v1',
        'model_server': 'http://127.0.0.1:9/v1',
        'api_key': 'EMPTY',
    }
    router = APIRouter(default_llm_cfg=llm_cfg, config_dir=str(tmp_path))
    with router._lock:
        router.endpoints.clear()
        router.agent_priorities.clear()
        router._agent_types_with_priorities.clear()
    ep = APIEndpoint(id='ep0',
                     name='conc0',
                     api_base=llm_cfg['api_base'],
                     model='mock',
                     concurrency_limit=0,
                     enabled=True)
    router.add_endpoint(ep)
    router.default_llm_cfg = ep.to_llm_cfg()
    return router


def _build_pool(router):
    """Real AgentPool wired to the real router (real _acquire_slot / get_instance)."""
    from agent_cascade.agent_pool import AgentPool

    llm_cfg = {
        'model': 'mock',
        'api_base': 'http://127.0.0.1:9/v1',
        'model_server': 'http://127.0.0.1:9/v1',
        'api_key': 'EMPTY'
    }
    return AgentPool(llm_cfg, agents_dir=str(router._config_dir), api_router=router)


def _make_sec_instance(sec_name, conversation_text='I will analyze this request.'):
    """Lightweight stand-in for engine._create_system_agent's Security instance.

    Real attribute storage (so the yield/reacquire code can read/write it) but a
    dummy conversation so extract_instance_output() yields nothing → ambiguous verdict.
    ``conversation_text`` lets a test drive a clear [YES]/[NO] verdict instead.
    """
    inst = MagicMock(name=sec_name)
    inst.instance_name = sec_name
    inst.agent_class = 'Security'
    inst._state_lock = threading.RLock()
    inst.conversation = [{'role': 'assistant', 'content': conversation_text}]
    return inst


def _run_execute_check(handler, ap, rid, caller_agent, *, auto_apply=False, verdict=None,
                      reacquire_side_effect=None, engine_holder=None):
    """Run the REAL _execute_check with only engine.run / _create_system_agent patched.

    ``auto_apply`` and ``verdict`` default to the original behavior (no auto-apply,
    ambiguous response). Pass ``auto_apply=True, verdict='[YES] ...'`` to drive the
    auto-approve path so operation_manager.user_approve fires (the verdict event).
    ``reacquire_side_effect`` forces the shared reacquire helper to raise (failure semantics);
    ``engine_holder`` receives the engine instance BEFORE the call so a test can still inspect
    it when _execute_check itself raises.
    """
    engine_instance = MagicMock()
    run_text = verdict if verdict is not None else 'I will analyze this request.'
    # Security "completes" immediately with the requested response.
    engine_instance.run.return_value = iter([(run_text, False)])
    engine_instance._create_system_agent.side_effect = lambda **kw: _make_sec_instance(
        kw.get('instance_name', 'Security'), conversation_text=run_text)
    engine_instance._telemetry.return_value = None
    # t151b: the security path now re-acquires via the shared helper reacquire_after_slot_yield
    # (which internally wraps reacquire_for + restores any KV saved by save_before_slot_yield).
    if reacquire_side_effect is not None:
        engine_instance.reacquire_after_slot_yield.side_effect = reacquire_side_effect
    else:
        engine_instance.reacquire_after_slot_yield.return_value = True

    mock_engine_cls = MagicMock(return_value=engine_instance)
    if engine_holder is not None:
        engine_holder.append(engine_instance)

    with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 5):
        with patch('agent_cascade.execution_engine.ExecutionEngine', mock_engine_cls):
            handler._execute_check(
                ap=ap,
                sec_inst=None,
                rid=rid,
                auto_apply=auto_apply,
                instance_name='Maine',
                caller_agent=caller_agent,
                prompt_template='Analyze {tool_name}: {description} args={arguments}',
                timeout_seconds=3600,
                warning_seconds=2400,
            )
    return engine_instance


# ── Log capture + reporting ──────────────────────────────────────────────────


def _capture_logs():
    """Capture DEBUG+ from the relevant loggers for the duration of a check."""
    records = []
    lock = threading.Lock()

    class _Capture(logging.Handler):

        def emit(self, record):
            with lock:
                try:
                    records.append(record)
                except Exception:
                    pass

    handler = _Capture(level=logging.DEBUG)
    # The app logger is "agent_cascade_logger" (a TOP-LEVEL name, NOT a child of the
    # "agent_cascade" package), so we must attach to it directly. We also capture the
    # "agent_cascade" package for any module-level loggers (api_router, slot_queue).
    logger_names = [
        'agent_cascade_logger',  # the real app logger (security_handler logs here)
        'agent_cascade',  # package parent (api_router / slot_queue module loggers)
    ]
    targets = []
    for name in logger_names:
        lg = logging.getLogger(name)
        old_level = lg.level
        lg.setLevel(logging.DEBUG)
        lg.addHandler(handler)
        targets.append((lg, old_level))
    return records, handler, targets


def _restore_logs(handler, targets):
    for lg, old_level in targets:
        lg.removeHandler(handler)
        lg.setLevel(old_level)


def _find(records, needle):
    return [r for r in records if needle in (r.getMessage() if hasattr(r, 'getMessage') else str(r))]


def _diagnostic_report(records, shared_pool, caller_agent):
    """Build a human-readable report of which yield path fired and pool state."""
    normal_yield = _find(records, '[SECURITY_SLOT_YIELD] Releasing slot')
    leaked_permit = _find(records, 'LEAKED PERMIT DETECTED')
    skipped = _find(records, 'SECURITY_SLOT_YIELD_SKIPPED')
    acquire_timeout = _find(records, 'waiting for endpoint slot')
    force_release_failed = _find(records, 'Force-release check failed')

    holders_now = list(shared_pool._running.keys()) if shared_pool else []

    report = []
    report.append('── SECURITY SLOT YIELD PATH REPORT ────────────────────────')
    report.append(f"  [1] Normal yield ([SECURITY_SLOT_YIELD] Releasing slot): "
                  f"{'FIRED' if normal_yield else 'NOT fired'} ({len(normal_yield)}x)")
    report.append(f"  [2] Force-release fallback (LEAKED PERMIT DETECTED):     "
                  f"{'FIRED' if leaked_permit else 'NOT fired'} ({len(leaked_permit)}x)")
    report.append(f"  [3] Skip-logging ([SECURITY_SLOT_YIELD_SKIPPED]):        "
                  f"{'FIRED' if skipped else 'NOT fired'} ({len(skipped)}x)")
    report.append(f"  Security slot-acquire timeout (waiting for endpoint slot): "
                  f"{'YES' if acquire_timeout else 'no'}")
    report.append(f"  Force-release check failed:                              "
                  f"{'YES' if force_release_failed else 'no'}")
    report.append(f"  Pool holders at end ({caller_agent} should be released):   {holders_now}")
    if skipped:
        report.append(f"  SKIP diagnostic text: {skipped[0].getMessage()[:300]}")
    if acquire_timeout:
        report.append(f"  TIMEOUT text: {acquire_timeout[0].getMessage()[:300]}")
    report.append('───────────────────────────────────────────────────────────')
    return '\n'.join(report)


# ── Shared test fixture ──────────────────────────────────────────────────────


@pytest.fixture
def slot_harness(tmp_path, request):
    """Build the real router/pool, patch QUEUE_WAIT_TIMEOUT short, yield caller + permit.

    Each test gets its OWN config dir (derived from the node id) so pytest-xdist's
    parallel workers don't overwrite each other's api_endpoints.json.
    """
    import os as _os

    import agent_cascade.api_router_pkg.scheduler as _ar_mod
    import agent_cascade.slot_queue as _sq_mod

    cfg_dir = tmp_path / request.node.name.replace('/', '_')
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # Save prior value so teardown can restore it (previously set without a restore — env leak).
    old_cfg_dir = _os.environ.get('AGENT_CASCADE_TEST_CONFIG_DIR')
    _os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = str(cfg_dir)

    # Shorten the shared-slot acquire timeout (module constants, captured at import time).
    old_sq = _sq_mod.QUEUE_WAIT_TIMEOUT
    old_ar = _ar_mod.QUEUE_WAIT_TIMEOUT
    _sq_mod.QUEUE_WAIT_TIMEOUT = 5
    _ar_mod.QUEUE_WAIT_TIMEOUT = 5

    router = _build_real_router(cfg_dir)
    pool = _build_pool(router)

    # ALSO shorten the new PoolSettings field. Since the dead-man's switch, the acquire
    # path resolves its window from pool.settings.slot_queue_timeout_seconds FIRST and
    # only falls back to the module constants above, so patching the constants alone is
    # now a no-op — the Security waiter would sit on the real 300s window and this suite
    # would hang instead of exercising the deadlock detection. Derived hard cap: 60s.
    old_slot_window = getattr(pool.settings, 'slot_queue_timeout_seconds', None)
    pool.settings.slot_queue_timeout_seconds = 5

    # The real AgentPool has operation_manager=None. Give it a minimal one so
    # _execute_check's prompt-building + result handling don't crash on None.
    om = MagicMock()
    om.base_dir = str(cfg_dir)
    om.extra_work_folders_ro = []
    om.extra_work_folders_rw = []
    om.enable_timeout = True
    om.approval_timeout_seconds = 180
    pool.operation_manager = om

    # _cleanup needs pool._execution._state_lock; ensure it exists on the real pool.
    if getattr(pool, '_execution', None) is None:
        pool._execution = MagicMock()
    pool._execution._state_lock = threading.Lock()
    pool.instance_state = {}

    # The shared pool is created lazily on first acquire — trigger it now.
    shared = router.scheduler._get_or_create_pool('http://127.0.0.1:9/v1', 0)
    assert shared is not None, 'Shared sequential SlotPool was not created (conc=0 not in effect?)'

    # Real caller instance holding the ONLY permit on the shared slot.
    import time as _t

    from agent_cascade.agent_instance import AgentInstance
    caller = AgentInstance(
        instance_name='caller',
        agent_class='coder',
        conversation=[],
        created_at=_t.monotonic(),
        last_activity=_t.monotonic(),
        latest_marker_index=0,
    )
    pool.instances['caller'] = caller

    release_cb = pool._acquire_slot('coder', 'caller')
    assert release_cb is not None, 'conc=0 endpoint should return a real release callback'
    # Bind the permit to the instance so the NORMAL yield path can find it.
    caller._slot_release = release_cb
    caller._slot_key = '_shared_sequential_slot_'

    assert 'caller' in shared._running, f"Caller did not acquire the shared slot: {list(shared._running)}"

    app = type('App', (), {})()
    session = {'session_name': 'Maine', 'generate_cfg': {}}
    handler = SecurityAdvisorHandler(pool, session, app, MagicMock(), lambda *a, **k: None)

    yield {
        'router': router,
        'pool': pool,
        'shared': shared,
        'caller': caller,
        'app': app,
        'session': session,
        'handler': handler,
        'release_cb': release_cb,
    }

    # Cleanup: release the permit + restore constants.
    try:
        release_cb()
    except Exception:
        pass
    _sq_mod.QUEUE_WAIT_TIMEOUT = old_sq
    _ar_mod.QUEUE_WAIT_TIMEOUT = old_ar
    # Restore the slot-queue activity window — a leaked 5s would silently shorten every
    # later acquire against this pool.
    if old_slot_window is not None:
        try:
            pool.settings.slot_queue_timeout_seconds = old_slot_window
        except Exception:
            pass
    # Restore the config-dir env var to its prior value (or drop it if it was unset).
    if old_cfg_dir is None:
        _os.environ.pop('AGENT_CASCADE_TEST_CONFIG_DIR', None)
    else:
        _os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = old_cfg_dir


# ── Test 1: normal yield path (caller holds a live _slot_release) ────────────


def test_normal_yield_path_completes(slot_harness):
    """Caller holds a LIVE _slot_release → the normal yield path must fire, the Security
    agent acquires the freed slot and completes, then the caller's slot is reacquired."""
    h = slot_harness
    shared, caller = h['shared'], h['caller']
    ap = {
        'request_id': 'rid_normal',
        'tool_name': 'shell_cmd',
        'description': 'echo hi',
        'tool_args': {
            'command': 'echo hi'
        },
        'agent_name': 'caller',
    }

    records, handler_log, targets = _capture_logs()
    try:
        engine_instance = _run_execute_check(h['handler'], ap, 'rid_normal', 'caller')
    finally:
        _restore_logs(handler_log, targets)

    normal_yield = _find(records, '[SECURITY_SLOT_YIELD] Releasing slot')
    leaked = _find(records, 'LEAKED PERMIT DETECTED')
    skipped = _find(records, 'SECURITY_SLOT_YIELD_SKIPPED')
    acquire_timeout = _find(records, 'waiting for endpoint slot')

    report = _diagnostic_report(records, shared, 'caller')
    print('\n' + report)

    assert normal_yield, (
        f"[NORMAL YIELD PATH NOT FIRED] Expected the caller's live _slot_release to be yielded.\n{report}")
    assert not leaked, f"Force-release fallback should NOT fire when a live callback exists.\n{report}"
    assert not skipped, f"Skip path should NOT fire when a slot was yielded.\n{report}"
    assert not acquire_timeout, (f"[DEADLOCK] Security agent timed out waiting for the shared slot — normal yield did "
                                 f"not free it in time.\n{report}")
    # The normal yield path fired (verified above) and the Security agent did NOT time out
    # waiting for the slot — i.e. the caller's permit was freed in time for it to proceed.
    # After the check, the caller is expected to hold the permit again (yield → run Security
    # → reacquire is in-order). BUG_0155: the reacquire now happens BEFORE the verdict is
    # delivered, not in the finally. t151b: via the shared helper (wraps reacquire_for + KV restore).
    assert engine_instance.reacquire_after_slot_yield.called, (
        '_yielded_slot should be True so the reacquire runs before the verdict is delivered.\n' + report)
    # Reacquire must have targeted the caller.
    assert any(c.args and c.args[0] is caller for c in engine_instance.reacquire_after_slot_yield.call_args_list), (
        'reacquire_after_slot_yield should be called with the caller instance to restore its slot.\n' + report)


# ── Test 2: force-release fallback path (leaked permit) ──────────────────────


def test_force_release_fallback_path(slot_harness):
    """Caller's _slot_release is None (cleared without releasing — the leaked state) but the
    pool STILL holds the caller's permit. The force-release fallback must detect + release it,
    so the Security agent can acquire and complete."""
    h = slot_harness
    shared, caller = h['shared'], h['caller']

    # Simulate the leaked/stale state: callback cleared, but the pool still shows a holder.
    caller._slot_release = None
    caller._slot_key = '_shared_sequential_slot_'
    assert 'caller' in shared._running, f"Precondition: caller must hold the permit: {list(shared._running)}"

    ap = {
        'request_id': 'rid_leak',
        'tool_name': 'shell_cmd',
        'description': 'echo hi',
        'tool_args': {
            'command': 'echo hi'
        },
        'agent_name': 'caller',
    }

    records, handler_log, targets = _capture_logs()
    try:
        engine_instance = _run_execute_check(h['handler'], ap, 'rid_leak', 'caller')
    finally:
        _restore_logs(handler_log, targets)

    normal_yield = _find(records, '[SECURITY_SLOT_YIELD] Releasing slot')
    leaked = _find(records, 'LEAKED PERMIT DETECTED')
    _find(records, 'SECURITY_SLOT_YIELD_SKIPPED')
    acquire_timeout = _find(records, 'waiting for endpoint slot')

    report = _diagnostic_report(records, shared, 'caller')
    print('\n' + report)

    # ── THE BUG SIGNATURE: if the force-release fallback does NOT fire and the check
    # times out waiting for the slot, this is the production deadlock. ────────────────
    assert leaked or normal_yield, (
        f"[DEADLOCK REPRODUCED] Neither the normal yield NOR the force-release fallback fired "
        f"for a caller that still holds the shared permit. The Security agent had nothing to "
        f"yield and blocked on _shared_sequential_slot_.\n{report}")
    assert not acquire_timeout, (
        f"[DEADLOCK] Security agent timed out waiting for the shared slot — the force-release "
        f"fallback did not free the leaked permit.\n{report}\n\nFULL LOG:\n" + '\n'.join(
            r.getMessage() for r in records if 'SECURITY_SLOT' in r.getMessage() or 'endpoint slot' in r.getMessage()))
    # After a successful force-release, the caller's permit must be gone from the pool.
    assert 'caller' not in shared._running, (
        f"Force-release fallback should remove the leaked holder from _running: {list(shared._running)}\n{report}")
    # Reacquire must run (the caller's slot is restored before the verdict is delivered). t151b: via the shared helper.
    assert engine_instance.reacquire_after_slot_yield.called, (
        '_yielded_slot should be True after force-release so the reacquire runs before the verdict is delivered.\n' + report)


# ── Test 3: skip path (no slot to yield at all) — diagnostic only ────────────


def test_skip_path_logs_diagnostics(slot_harness):
    """Neither a live callback NOR a leaked permit exists. The skip path must log a clear
    diagnostic with pool-holder info. This is NOT the deadlock (the slot is free), so the
    check should complete; we only verify the diagnostic is emitted for debuggability."""
    h = slot_harness
    shared, caller = h['shared'], h['caller']

    # No slot to yield: clear the callback AND release the permit so the pool is empty.
    caller._slot_release = None
    caller._slot_key = None
    h['release_cb']()  # actually free the permit → pool empty
    assert 'caller' not in shared._running, f"Precondition: pool must be empty: {list(shared._running)}"

    ap = {
        'request_id': 'rid_skip',
        'tool_name': 'shell_cmd',
        'description': 'echo hi',
        'tool_args': {
            'command': 'echo hi'
        },
        'agent_name': 'caller',
    }

    records, handler_log, targets = _capture_logs()
    try:
        _run_execute_check(h['handler'], ap, 'rid_skip', 'caller')
    finally:
        _restore_logs(handler_log, targets)

    skipped = _find(records, 'SECURITY_SLOT_YIELD_SKIPPED')
    acquire_timeout = _find(records, 'waiting for endpoint slot')
    report = _diagnostic_report(records, shared, 'caller')
    print('\n' + report)

    assert skipped, (f"[SKIP PATH NOT FIRED] Expected a [SECURITY_SLOT_YIELD_SKIPPED] diagnostic when there "
                     f"is no slot to yield.\n{report}")
    # The skip diagnostic must include pool-holder info.
    assert any('Pool holders:' in r.getMessage() for r in skipped), (
        f"Skip diagnostic should include pool-holder info: {[r.getMessage()[:200] for r in skipped]}")
    # Slot is free, so the Security agent should NOT deadlock.
    assert not acquire_timeout, (
        f"[UNEXPECTED DEADLOCK] Slot was free but the Security agent still timed out.\n{report}")


# ── Test 4: the safe-mode window must defer the advisor's KV restore ──────────
#
# The finally-block call site reads the caller's _safe_mode_kv flag (armed by shell_cmd
# safe mode before the approval window) and passes skip_kv_restore to the shared helper.
# The helper itself is unit-tested in tests/test_state_restore_slot_gate.py::TestSafeModeRestoreDeferral;
# here we only pin the WIRING, so the engine stays a MagicMock.


def test_safe_mode_window_passes_skip_kv_restore_true(slot_harness):
    """Caller armed _safe_mode_kv (a shell_cmd safe window) → the finally must pass
    skip_kv_restore=True so the advisor re-acquires its slot WITHOUT restoring the
    parent's KV inside the approval window (that restore raced the tool's unload and
    cold-reloaded a multi-GB model mid-command)."""
    h = slot_harness
    caller = h['caller']
    ap = {
        'request_id': 'rid_safe_mode',
        'tool_name': 'shell_cmd',
        'description': 'echo hi',
        'tool_args': {
            'command': 'echo hi'
        },
        'agent_name': 'caller',
    }

    # shell_cmd sets this BEFORE the approval window and clears it only in its finally.
    caller._safe_mode_kv = True

    records, handler_log, targets = _capture_logs()
    try:
        engine_instance = _run_execute_check(h['handler'], ap, 'rid_safe_mode', 'caller')
    finally:
        _restore_logs(handler_log, targets)

    report = _diagnostic_report(records, h['shared'], 'caller')
    print('\n' + report)

    calls = engine_instance.reacquire_after_slot_yield.call_args_list
    assert calls, f'reacquire_after_slot_yield must still run before the verdict is delivered.\n{report}'
    targeted = [c for c in calls if c.args and c.args[0] is caller]
    assert targeted, f'reacquire must target the caller instance.\n{report}'
    assert all((c.kwargs or {}).get('skip_kv_restore') is True for c in targeted), (
        'a safe-mode window must defer the advisor KV restore: '
        f'{[dict(c.kwargs or {}) for c in targeted]}\n{report}')


def test_plain_security_check_keeps_advisor_restore(slot_harness):
    """No _safe_mode_kv (an ordinary security check) → skip_kv_restore must be False,
    i.e. the advisor's restore stays exactly as before."""
    h = slot_harness
    caller = h['caller']
    ap = {
        'request_id': 'rid_plain',
        'tool_name': 'shell_cmd',
        'description': 'echo hi',
        'tool_args': {
            'command': 'echo hi'
        },
        'agent_name': 'caller',
    }

    records, handler_log, targets = _capture_logs()
    try:
        engine_instance = _run_execute_check(h['handler'], ap, 'rid_plain', 'caller')
    finally:
        _restore_logs(handler_log, targets)

    report = _diagnostic_report(records, h['shared'], 'caller')
    print('\n' + report)

    calls = engine_instance.reacquire_after_slot_yield.call_args_list
    assert calls, f'reacquire_after_slot_yield must still run before the verdict is delivered.\n{report}'
    targeted = [c for c in calls if c.args and c.args[0] is caller]
    assert targeted, f'reacquire must target the caller instance.\n{report}'
    assert all((c.kwargs or {}).get('skip_kv_restore') is False for c in targeted), (
        'outside a safe-mode window the advisor restore must stay enabled: '
        f'{[dict(c.kwargs or {}) for c in targeted]}\n{report}')


# ── BUG_0155: the /state/load must be issued BEFORE the verdict is delivered ─
#
# These tests drive the REAL _execute_check. The ordering + negative tests bind the REAL
# save/reacquire/restore chain (only the HTTP layer is faked) so a "skip" that still
# issued a state/load cannot pass — the same recording idiom as
# tests/test_state_restore_slot_gate.py::TestSafeModeRestoreDeferral. The failure-semantics
# tests use the MagicMock engine and force the shared helper to raise.


def _make_ap(rid):
    return {
        'request_id': rid,
        'tool_name': 'shell_cmd',
        'description': 'echo hi',
        'tool_args': {
            'command': 'echo hi'
        },
        'agent_name': 'caller',
    }


def _bind_real_restore_chain(engine_instance, pool, *, toggle=True):
    """Bind the REAL save + reacquire/restore chain onto the stub engine so the test
    exercises the production code path (only the HTTP layer is faked). The engine's
    slot resolution reads pool.api_router only, so a MagicMock pool carrying the real
    router is enough — no AgentPool internals needed."""
    from agent_cascade.engine.core import ExecutionEngine

    engine_instance.pool = MagicMock()
    engine_instance.pool.api_router = pool.api_router
    engine_instance._get_pool_setting.return_value = toggle  # state_kv_save_enabled toggle
    # Real save side (issues /state/save + sets the consume-once label). yield_caller_slot
    # invokes save_fn(caller_inst, release_reason) with two positional args.
    engine_instance.save_before_slot_yield = (
        lambda instance, inst_name, reason='': ExecutionEngine.save_before_slot_yield(
            engine_instance, instance, inst_name, reason))
    # Real reacquire/restore (issues /state/load from the pending label onto the held endpoint).
    engine_instance.reacquire_after_slot_yield = (
        lambda instance, inst_name, context='', *, tolerate_failure=False, respect_toggle=True,
        skip_kv_restore=False, defer_kv_restore=False: ExecutionEngine.reacquire_after_slot_yield(
            engine_instance, instance, inst_name, context, tolerate_failure=tolerate_failure,
            respect_toggle=respect_toggle, skip_kv_restore=skip_kv_restore, defer_kv_restore=defer_kv_restore))
    engine_instance._restore_held_slot_state = (
        lambda instance, inst_name, label=None: ExecutionEngine._restore_held_slot_state(
            engine_instance, instance, inst_name, label))
    engine_instance._clear_orphaned_state_label = ExecutionEngine._clear_orphaned_state_label

    # Fake the bounded-FIFO acquire: grant the caller its slot without touching the scheduler.
    def _fake_reacquire(instance, holder_name, context='reacquire'):
        instance._slot_release = lambda: None
        instance._slot_key = '_shared_sequential_slot_'
        return True

    engine_instance.reacquire_for = _fake_reacquire


def _record_state_http(post_mock, seq):
    """Record every state/save + state/load POST as (tag, label) into ``seq``."""
    def _post(url, *args, json=None, **kwargs):
        if isinstance(url, str) and '/state/' in url:
            tag = 'save' if '/state/save' in url else 'load'
            seq.append((tag, (json or {}).get('label')))
        resp = MagicMock()
        resp.status_code = 200
        return resp

    post_mock.side_effect = _post


def _drive_real_restore(h, ap, rid, *, auto_apply=True, verdict='[YES] safe to proceed',
                       toggle=True, safe_mode=False, arm_endpoint=True):
    """Drive the REAL _execute_check with the REAL save/reacquire chain bound. Returns the
    ordered event log: ('save'|'load', label) from HTTP and ('approve', None) from the verdict."""
    import agent_cascade.state_ops as state_ops

    pool, caller = h['pool'], h['caller']
    if safe_mode:
        caller._safe_mode_kv = True
    if arm_endpoint:
        # The caller's held endpoint declares state-save capability (mirrors a real autoloader).
        caller._last_endpoint_config = {'api_base': 'http://127.0.0.1:9/v1',
                                        'model': 'mock',
                                        'state_save_enabled': True}

    events = []
    if auto_apply:
        pool.operation_manager.user_approve.side_effect = (
            lambda *a, **k: (events.append(('approve', None)), 'Approved: rid')[1])

    engine_instance = MagicMock()
    _bind_real_restore_chain(engine_instance, pool, toggle=toggle)
    engine_instance.run.return_value = iter([(verdict, False)])
    engine_instance._create_system_agent.side_effect = lambda **kw: _make_sec_instance(
        kw.get('instance_name', 'Security'), conversation_text=verdict)
    engine_instance._telemetry.return_value = None

    mock_engine_cls = MagicMock(return_value=engine_instance)
    with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 5):
        with patch('agent_cascade.execution_engine.ExecutionEngine', mock_engine_cls):
            with patch.object(state_ops.httpx, 'post') as post_mock:
                _record_state_http(post_mock, events)
                h['handler']._execute_check(
                    ap=ap, sec_inst=None, rid=rid, auto_apply=auto_apply,
                    instance_name='Maine', caller_agent='caller',
                    prompt_template='Analyze {tool_name}: {description} args={arguments}',
                    timeout_seconds=3600, warning_seconds=2400,
                )
    return events


def test_restore_is_issued_before_the_verdict_is_delivered(slot_harness):
    """BUG_0155 PRIMARY (red/green): exactly one /state/load with the save's label must be
    issued BEFORE operation_manager.user_approve unblocks the caller.

    Against the PRE-FIX (late) code the load is issued in the finally — i.e. AFTER user_approve
    — so events.index('load') > events.index('approve') and this FAILS. After the fix the
    early call issues the load before the verdict is delivered, so it PASSES.
    """
    h = slot_harness
    events = _drive_real_restore(h, _make_ap('rid_order'), 'rid_order')

    saves = [lbl for tag, lbl in events if tag == 'save']
    loads = [lbl for tag, lbl in events if tag == 'load']
    assert len(saves) == 1 and saves[0] == 'caller', (
        f"exactly one /state/save with the caller label expected: {events}")
    assert len(loads) == 1 and loads[0] == 'caller', (
        f"exactly one /state/load with the save's label expected: {events}")
    assert loads[0] == saves[0], f'the load label must equal the save label: {events}'

    load_i = next(i for i, (tag, _) in enumerate(events) if tag == 'load')
    approve_i = next(i for i, (tag, _) in enumerate(events) if tag == 'approve')
    assert load_i < approve_i, (
        f'[BUG_0155] the /state/load must be issued BEFORE the verdict is delivered; got order: {events}')


def test_negative_toggle_off_issues_zero_loads(slot_harness):
    """state_kv_save_enabled OFF (and no safe-mode override) → ZERO state/load requests, and
    the verdict is still delivered (user_approve exactly once). Mirrors the
    TestSafeModeRestoreDeferral skip-path assertion."""
    h = slot_harness
    events = _drive_real_restore(h, _make_ap('rid_toggle_off'), 'rid_toggle_off', toggle=False)

    assert [lbl for tag, lbl in events if tag == 'load'] == [], (
        f'toggle off must issue no state/load: {events}')
    assert [t for t, _ in events if t == 'approve'].count('approve') == 1, (
        f'the verdict must still be delivered once: {events}')


def test_negative_safe_mode_window_issues_zero_loads(slot_harness):
    """Caller armed _safe_mode_kv (a shell_cmd safe window) → the early call passes
    skip_kv_restore=True, so the slot is re-acquired but ZERO state/load fires here (the
    tool owns the restore by stable label after the command)."""
    h = slot_harness
    events = _drive_real_restore(h, _make_ap('rid_safe'), 'rid_safe', toggle=True, safe_mode=True)

    assert [lbl for tag, lbl in events if tag == 'load'] == [], (
        f'a safe-mode window must defer the advisor KV restore: {events}')
    assert [t for t, _ in events if t == 'approve'].count('approve') == 1, (
        f'the verdict must still be delivered once: {events}')


def test_negative_no_slot_held_issues_zero_loads(slot_harness):
    """Nothing yielded (no live callback, no leaked permit) → neither the early call nor the
    finally runs the reacquire, so ZERO state/load fires and the verdict is still delivered."""
    h = slot_harness
    caller = h['caller']
    # Free the permit so the pool is empty (the skip path).
    caller._slot_release = None
    caller._slot_key = None
    h['release_cb']()

    events = _drive_real_restore(h, _make_ap('rid_noslot'), 'rid_noslot', toggle=True, arm_endpoint=False)

    assert [lbl for tag, lbl in events if tag == 'load'] == [], (
        f'nothing yielded must issue no state/load: {events}')
    assert [t for t, _ in events if t == 'approve'].count('approve') == 1, (
        f'the verdict must still be delivered once: {events}')


def test_verdict_still_delivered_when_early_reacquire_raises_timeout(slot_harness):
    """A hard TimeoutError from the early reacquire must NOT skip the verdict: user_approve
    still runs once, the unchanged finally re-attempts (call_count == 2), and the exception
    still propagates out of _execute_check."""
    h = slot_harness
    approve_calls = []
    h['pool'].operation_manager.user_approve.side_effect = (
        lambda *a, **k: (approve_calls.append(1), 'Approved: rid')[1])

    engine_holder = []
    with pytest.raises(TimeoutError):
        _run_execute_check(h['handler'], _make_ap('rid_fail_to'), 'rid_fail_to', 'caller',
                           auto_apply=True, verdict='[YES] safe to proceed',
                           reacquire_side_effect=TimeoutError('slot acquire timeout'),
                           engine_holder=engine_holder)

    engine_instance = engine_holder[0]
    assert len(approve_calls) == 1, 'the verdict must still be delivered despite the early-reacquire failure'
    assert engine_instance.reacquire_after_slot_yield.call_count == 2, (
        'early attempt + the unchanged finally must both run on a hard failure')


def test_verdict_still_delivered_when_early_reacquire_raises_other(slot_harness):
    """A non-TimeoutError (e.g. a slot-queue RuntimeError re-raised at slot_lifecycle.py:231)
    must also NOT skip the verdict — the reviewer-corrected exception-safety requirement.
    Same shape: user_approve once, finally re-attempts (call_count == 2), exception propagates."""
    h = slot_harness
    approve_calls = []
    h['pool'].operation_manager.user_approve.side_effect = (
        lambda *a, **k: (approve_calls.append(1), 'Approved: rid')[1])

    engine_holder = []
    with pytest.raises(RuntimeError):
        _run_execute_check(h['handler'], _make_ap('rid_fail_rt'), 'rid_fail_rt', 'caller',
                           auto_apply=True, verdict='[YES] safe to proceed',
                           reacquire_side_effect=RuntimeError('slot queue'),
                           engine_holder=engine_holder)

    engine_instance = engine_holder[0]
    assert len(approve_calls) == 1, 'the verdict must still be delivered despite a non-TimeoutError failure'
    assert engine_instance.reacquire_after_slot_yield.call_count == 2, (
        'early attempt + the unchanged finally must both run on a hard failure')


def test_success_path_reacquires_exactly_once(slot_harness):
    """On the normal (no-error) path the caller is re-acquired EXACTLY once — the early call
    clears _yielded_slot so the finally does not repeat it (no double-acquire)."""
    h = slot_harness
    engine_holder = []
    _run_execute_check(h['handler'], _make_ap('rid_once'), 'rid_once', 'caller',
                       auto_apply=True, verdict='[YES] safe to proceed',
                       engine_holder=engine_holder)

    engine_instance = engine_holder[0]
    assert engine_instance.reacquire_after_slot_yield.call_count == 1, (
        'the success path must re-acquire exactly once (early call clears _yielded_slot)')
