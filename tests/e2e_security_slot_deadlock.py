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
_os.environ.setdefault("AGENT_CASCADE_INSTANCE_ID", f"e2e_{_os.getpid()}")

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
        "model": "mock",
        "api_base": "http://127.0.0.1:9/v1",
        "model_server": "http://127.0.0.1:9/v1",
        "api_key": "EMPTY",
    }
    router = APIRouter(default_llm_cfg=llm_cfg, config_dir=str(tmp_path))
    with router._lock:
        router.endpoints.clear()
        router.agent_priorities.clear()
        router._agent_types_with_priorities.clear()
    ep = APIEndpoint(id="ep0", name="conc0", api_base=llm_cfg["api_base"],
                     model="mock", concurrency_limit=0, enabled=True)
    router.add_endpoint(ep)
    router.default_llm_cfg = ep.to_llm_cfg()
    return router


def _build_pool(router):
    """Real AgentPool wired to the real router (real _acquire_slot / get_instance)."""
    from agent_cascade.agent_pool import AgentPool

    llm_cfg = {"model": "mock", "api_base": "http://127.0.0.1:9/v1",
               "model_server": "http://127.0.0.1:9/v1", "api_key": "EMPTY"}
    return AgentPool(llm_cfg, agents_dir=str(router._config_dir), api_router=router)


def _make_sec_instance(sec_name):
    """Lightweight stand-in for engine._create_system_agent's Security instance.

    Real attribute storage (so the yield/reacquire code can read/write it) but a
    dummy conversation so extract_instance_output() yields nothing → ambiguous verdict.
    """
    inst = MagicMock(name=sec_name)
    inst.instance_name = sec_name
    inst.agent_class = "Security"
    inst._state_lock = threading.RLock()
    inst.conversation = [{"role": "assistant", "content": "I will analyze this request."}]
    return inst


def _run_execute_check(handler, ap, rid, caller_agent):
    """Run the REAL _execute_check with only engine.run / _create_system_agent patched."""
    from agent_cascade.execution_engine import ExecutionEngine

    engine_instance = MagicMock()
    # Security "completes" immediately with an ambiguous (non-[YES]/[NO]) response.
    engine_instance.run.return_value = iter([("I will analyze this request.", False)])
    engine_instance._create_system_agent.side_effect = lambda **kw: _make_sec_instance(
        kw.get("instance_name", "Security")
    )
    engine_instance._telemetry.return_value = None
    engine_instance.reacquire_for.return_value = True

    mock_engine_cls = MagicMock(return_value=engine_instance)

    with patch("agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS", 5):
        with patch("agent_cascade.execution_engine.ExecutionEngine", mock_engine_cls):
            handler._execute_check(
                ap=ap, sec_inst=None, rid=rid, auto_apply=False,
                instance_name="Maine", caller_agent=caller_agent,
                prompt_template="Analyze {tool_name}: {description} args={arguments}",
                timeout_seconds=3600, warning_seconds=2400,
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
        "agent_cascade_logger",  # the real app logger (security_handler logs here)
        "agent_cascade",         # package parent (api_router / slot_queue module loggers)
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
    return [r for r in records if needle in (r.getMessage() if hasattr(r, "getMessage") else str(r))]


def _diagnostic_report(records, shared_pool, caller_agent):
    """Build a human-readable report of which yield path fired and pool state."""
    normal_yield = _find(records, "[SECURITY_SLOT_YIELD] Releasing slot")
    leaked_permit = _find(records, "LEAKED PERMIT DETECTED")
    skipped = _find(records, "SECURITY_SLOT_YIELD_SKIPPED")
    acquire_timeout = _find(records, "waiting for endpoint slot")
    force_release_failed = _find(records, "Force-release check failed")

    holders_now = list(shared_pool._running.keys()) if shared_pool else []

    report = []
    report.append("── SECURITY SLOT YIELD PATH REPORT ────────────────────────")
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
    report.append("───────────────────────────────────────────────────────────")
    return "\n".join(report)


# ── Shared test fixture ──────────────────────────────────────────────────────

@pytest.fixture
def slot_harness(tmp_path, request):
    """Build the real router/pool, patch QUEUE_WAIT_TIMEOUT short, yield caller + permit.

    Each test gets its OWN config dir (derived from the node id) so pytest-xdist's
    parallel workers don't overwrite each other's api_endpoints.json.
    """
    import os as _os
    import agent_cascade.slot_queue as _sq_mod
    import agent_cascade.api_router as _ar_mod

    cfg_dir = tmp_path / request.node.name.replace("/", "_")
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = str(cfg_dir)

    # Shorten the shared-slot acquire timeout (module constants, captured at import time).
    old_sq = _sq_mod.QUEUE_WAIT_TIMEOUT
    old_ar = _ar_mod.QUEUE_WAIT_TIMEOUT
    _sq_mod.QUEUE_WAIT_TIMEOUT = 5
    _ar_mod.QUEUE_WAIT_TIMEOUT = 5

    router = _build_real_router(cfg_dir)
    pool = _build_pool(router)

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
    if getattr(pool, "_execution", None) is None:
        pool._execution = MagicMock()
    pool._execution._state_lock = threading.Lock()
    pool.instance_state = {}

    # The shared pool is created lazily on first acquire — trigger it now.
    shared = router.scheduler._get_or_create_pool("http://127.0.0.1:9/v1", 0)
    assert shared is not None, "Shared sequential SlotPool was not created (conc=0 not in effect?)"

    # Real caller instance holding the ONLY permit on the shared slot.
    from agent_cascade.agent_instance import AgentInstance
    import time as _t
    caller = AgentInstance(
        instance_name="caller", agent_class="coder", conversation=[],
        created_at=_t.monotonic(), last_activity=_t.monotonic(), latest_marker_index=0,
    )
    pool.instances["caller"] = caller

    release_cb = pool._acquire_slot("coder", "caller")
    assert release_cb is not None, "conc=0 endpoint should return a real release callback"
    # Bind the permit to the instance so the NORMAL yield path can find it.
    caller._slot_release = release_cb
    caller._slot_key = "_shared_sequential_slot_"

    assert "caller" in shared._running, f"Caller did not acquire the shared slot: {list(shared._running)}"

    app = type("App", (), {})()
    session = {"session_name": "Maine", "generate_cfg": {}}
    handler = SecurityAdvisorHandler(pool, session, app, MagicMock(), lambda *a, **k: None)

    yield {
        "router": router, "pool": pool, "shared": shared, "caller": caller,
        "app": app, "session": session, "handler": handler, "release_cb": release_cb,
    }

    # Cleanup: release the permit + restore constants.
    try:
        release_cb()
    except Exception:
        pass
    _sq_mod.QUEUE_WAIT_TIMEOUT = old_sq
    _ar_mod.QUEUE_WAIT_TIMEOUT = old_ar


# ── Test 1: normal yield path (caller holds a live _slot_release) ────────────

def test_normal_yield_path_completes(slot_harness):
    """Caller holds a LIVE _slot_release → the normal yield path must fire, the Security
    agent acquires the freed slot and completes, then the caller's slot is reacquired."""
    h = slot_harness
    shared, caller = h["shared"], h["caller"]
    ap = {
        "request_id": "rid_normal", "tool_name": "shell_cmd",
        "description": "echo hi", "tool_args": {"command": "echo hi"}, "agent_name": "caller",
    }

    records, handler_log, targets = _capture_logs()
    try:
        engine_instance = _run_execute_check(h["handler"], ap, "rid_normal", "caller")
    finally:
        _restore_logs(handler_log, targets)

    normal_yield = _find(records, "[SECURITY_SLOT_YIELD] Releasing slot")
    leaked = _find(records, "LEAKED PERMIT DETECTED")
    skipped = _find(records, "SECURITY_SLOT_YIELD_SKIPPED")
    acquire_timeout = _find(records, "waiting for endpoint slot")

    report = _diagnostic_report(records, shared, "caller")
    print("\n" + report)

    assert normal_yield, (
        f"[NORMAL YIELD PATH NOT FIRED] Expected the caller's live _slot_release to be yielded.\n{report}"
    )
    assert not leaked, f"Force-release fallback should NOT fire when a live callback exists.\n{report}"
    assert not skipped, f"Skip path should NOT fire when a slot was yielded.\n{report}"
    assert not acquire_timeout, (
        f"[DEADLOCK] Security agent timed out waiting for the shared slot — normal yield did "
        f"not free it in time.\n{report}"
    )
    # The normal yield path fired (verified above) and the Security agent did NOT time out
    # waiting for the slot — i.e. the caller's permit was freed in time for it to proceed.
    # After the check, the finally-block RE-ACQUIRES the caller's slot, so the caller is
    # expected to hold the permit again (yield → run Security → reacquire is in-order).
    assert engine_instance.reacquire_for.called, (
        "_yielded_slot should be True so the finally-block reacquire runs.\n" + report
    )
    # Reacquire must have targeted the caller.
    assert any(c.args and c.args[0] is caller for c in engine_instance.reacquire_for.call_args_list), (
        "reacquire_for should be called with the caller instance to restore its slot.\n" + report
    )


# ── Test 2: force-release fallback path (leaked permit) ──────────────────────

def test_force_release_fallback_path(slot_harness):
    """Caller's _slot_release is None (cleared without releasing — the leaked state) but the
    pool STILL holds the caller's permit. The force-release fallback must detect + release it,
    so the Security agent can acquire and complete."""
    h = slot_harness
    shared, caller = h["shared"], h["caller"]

    # Simulate the leaked/stale state: callback cleared, but the pool still shows a holder.
    caller._slot_release = None
    caller._slot_key = "_shared_sequential_slot_"
    assert "caller" in shared._running, f"Precondition: caller must hold the permit: {list(shared._running)}"

    ap = {
        "request_id": "rid_leak", "tool_name": "shell_cmd",
        "description": "echo hi", "tool_args": {"command": "echo hi"}, "agent_name": "caller",
    }

    records, handler_log, targets = _capture_logs()
    try:
        engine_instance = _run_execute_check(h["handler"], ap, "rid_leak", "caller")
    finally:
        _restore_logs(handler_log, targets)

    normal_yield = _find(records, "[SECURITY_SLOT_YIELD] Releasing slot")
    leaked = _find(records, "LEAKED PERMIT DETECTED")
    skipped = _find(records, "SECURITY_SLOT_YIELD_SKIPPED")
    acquire_timeout = _find(records, "waiting for endpoint slot")

    report = _diagnostic_report(records, shared, "caller")
    print("\n" + report)

    # ── THE BUG SIGNATURE: if the force-release fallback does NOT fire and the check
    # times out waiting for the slot, this is the production deadlock. ────────────────
    assert leaked or normal_yield, (
        f"[DEADLOCK REPRODUCED] Neither the normal yield NOR the force-release fallback fired "
        f"for a caller that still holds the shared permit. The Security agent had nothing to "
        f"yield and blocked on _shared_sequential_slot_.\n{report}"
    )
    assert not acquire_timeout, (
        f"[DEADLOCK] Security agent timed out waiting for the shared slot — the force-release "
        f"fallback did not free the leaked permit.\n{report}\n\nFULL LOG:\n"
        + "\n".join(r.getMessage() for r in records if "SECURITY_SLOT" in r.getMessage() or "endpoint slot" in r.getMessage())
    )
    # After a successful force-release, the caller's permit must be gone from the pool.
    assert "caller" not in shared._running, (
        f"Force-release fallback should remove the leaked holder from _running: {list(shared._running)}\n{report}"
    )
    # Reacquire must run (the finally block restores the caller's slot).
    assert engine_instance.reacquire_for.called, (
        "_yielded_slot should be True after force-release so the reacquire runs.\n" + report
    )


# ── Test 3: skip path (no slot to yield at all) — diagnostic only ────────────

def test_skip_path_logs_diagnostics(slot_harness):
    """Neither a live callback NOR a leaked permit exists. The skip path must log a clear
    diagnostic with pool-holder info. This is NOT the deadlock (the slot is free), so the
    check should complete; we only verify the diagnostic is emitted for debuggability."""
    h = slot_harness
    shared, caller = h["shared"], h["caller"]

    # No slot to yield: clear the callback AND release the permit so the pool is empty.
    caller._slot_release = None
    caller._slot_key = None
    h["release_cb"]()  # actually free the permit → pool empty
    assert "caller" not in shared._running, f"Precondition: pool must be empty: {list(shared._running)}"

    ap = {
        "request_id": "rid_skip", "tool_name": "shell_cmd",
        "description": "echo hi", "tool_args": {"command": "echo hi"}, "agent_name": "caller",
    }

    records, handler_log, targets = _capture_logs()
    try:
        engine_instance = _run_execute_check(h["handler"], ap, "rid_skip", "caller")
    finally:
        _restore_logs(handler_log, targets)

    skipped = _find(records, "SECURITY_SLOT_YIELD_SKIPPED")
    acquire_timeout = _find(records, "waiting for endpoint slot")
    report = _diagnostic_report(records, shared, "caller")
    print("\n" + report)

    assert skipped, (
        f"[SKIP PATH NOT FIRED] Expected a [SECURITY_SLOT_YIELD_SKIPPED] diagnostic when there "
        f"is no slot to yield.\n{report}"
    )
    # The skip diagnostic must include pool-holder info.
    assert any("Pool holders:" in r.getMessage() for r in skipped), (
        f"Skip diagnostic should include pool-holder info: {[r.getMessage()[:200] for r in skipped]}"
    )
    # Slot is free, so the Security agent should NOT deadlock.
    assert not acquire_timeout, (
        f"[UNEXPECTED DEADLOCK] Slot was free but the Security agent still timed out.\n{report}"
    )
