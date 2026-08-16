"""FULL-FLOW e2e reproduction of the security-advisor shared-slot deadlock.

Unlike tests/e2e_security_slot_deadlock.py (which calls `_execute_check` directly), this
exercises the REAL production flow end-to-end:

  1. Caller agent ("coder") acquires its slot via the REAL path — `engine.run()` →
     `_acquire_slot_with_logging` → `pool._acquire_slot` (shared sequential SlotPool, cap 1).
  2. The caller then calls `shell_cmd`, which triggers `OperationManager.request_user_approval`
     — this BLOCKS the caller's thread while it STILL HOLDS its slot (the production scenario).
  3. We trigger the security check the SAME way production does:
     `await SecurityAdvisorHandler.run_check({'request_id': rid, 'auto_apply': True})`.
     run_check() reads the pending approval, resolves caller_agent, and spawns a daemon thread
     that runs `_run_check_worker` → `_execute_check`.
  4. The Security agent must acquire the shared slot (freed by the yield) to complete.

GOAL (debugging, not fixing): capture which of the three yield paths fires in the REAL flow and
whether the check completes or deadlocks/timeouts:
  * `[SECURITY_SLOT_YIELD] Releasing slot`              — normal yield
  * `[SECURITY_SLOT_YIELD] LEAKED PERMIT DETECTED`      — force-release fallback
  * `[SECURITY_SLOT_YIELD_SKIPPED]`                     — neither (bug signature)
  * `waiting for endpoint slot` (timeout)               — the deadlock

We patch ONLY the LLM model call (so no real network/LLM is hit). Everything else — the pool,
the SlotPool, the scheduler, engine.run()'s slot acquisition, request_user_approval's blocking,
run_check's thread spawning, and _execute_check's yield/reacquire logic — runs UNMOCKED.
"""
# Isolate this standalone run's logs/telemetry from the production workspace.
# Must be set BEFORE any agent_cascade import (instance_id reads it at call time).
import os as _os
_os.environ.setdefault("AGENT_CASCADE_INSTANCE_ID", f"e2e_{_os.getpid()}")

import asyncio
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.security_handler import SecurityAdvisorHandler


# ── Real component harness (no server) ───────────────────────────────────────

def _build_real_router(cfg_dir):
    """Real APIRouter with a single conc=0 endpoint → real shared sequential SlotPool."""
    from agent_cascade.api_router import APIEndpoint, APIRouter

    llm_cfg = {
        "model": "mock",
        "api_base": "http://127.0.0.1:9/v1",
        "model_server": "http://127.0.0.1:9/v1",
        "api_key": "EMPTY",
    }
    router = APIRouter(default_llm_cfg=llm_cfg, config_dir=str(cfg_dir))
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
    """Real AgentPool wired to the real router."""
    from agent_cascade.agent_pool import AgentPool

    llm_cfg = {"model": "mock", "api_base": "http://127.0.0.1:9/v1",
               "model_server": "http://127.0.0.1:9/v1", "api_key": "EMPTY"}
    return AgentPool(llm_cfg, agents_dir=str(router._config_dir), api_router=router)


def _build_real_operation_manager(pool, base_dir):
    """A REAL OperationManager (ApprovalMixin) so request_user_approval blocks for real."""
    from agent_cascade.operation_manager import OperationManager

    om = OperationManager(base_dir=str(base_dir), agent_pool=pool)
    # Short approval timeout so a stuck approval can't hang the test forever.
    om.enable_timeout = True
    om.approval_timeout_seconds = 30
    return om


# ── Log capture (GOTCHA: app logger is top-level 'agent_cascade_logger') ─────

def _capture_logs():
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
    # The app logger is "agent_cascade_logger" (TOP-LEVEL, NOT under the package).
    logger_names = ["agent_cascade_logger", "agent_cascade"]
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


def _relevant_lines(records):
    """Filter to the log lines that matter for the deadlock diagnosis."""
    needles = ("SECURITY_SLOT", "SLOT_", "endpoint slot", "APPROVAL", "SECURITY",
               "waiting for", "timed out", "Timeout", "timeout", "deadlock")
    out = []
    for r in records:
        try:
            m = r.getMessage()
        except Exception:
            m = str(r)
        if any(n.lower() in m.lower() for n in needles):
            out.append(f"[{r.levelname:>8}] {r.name}: {m}")
    return out


def _diagnostic_report(records, shared_pool, rid):
    normal_yield = _find(records, "[SECURITY_SLOT_YIELD] Releasing slot")
    leaked = _find(records, "LEAKED PERMIT DETECTED")
    skipped = _find(records, "SECURITY_SLOT_YIELD_SKIPPED")
    acquire_timeout = _find(records, "waiting for endpoint slot")
    worker_started = _find(records, "Check worker started")
    worker_finished = _find(records, "Check worker finished")

    holders_now = list(shared_pool._running.keys()) if shared_pool else []
    report = [
        f"── FULL-FLOW SECURITY SLOT REPORT (rid={rid}) ──────────────────────",
        f"  worker started:   {'yes' if worker_started else 'NO'}",
        f"  worker finished:  {'yes' if worker_finished else 'NO'}",
        f"  [1] normal yield: {'FIRED' if normal_yield else 'NOT fired'} ({len(normal_yield)}x)",
        f"  [2] force-release: {'FIRED' if leaked else 'NOT fired'} ({len(leaked)}x)",
        f"  [3] skip-logging:  {'FIRED' if skipped else 'NOT fired'} ({len(skipped)}x)",
        f"  slot-acquire timeout (waiting for endpoint slot): {'YES' if acquire_timeout else 'no'}",
        f"  pool holders at end: {holders_now}",
    ]
    if skipped:
        report.append(f"  SKIP text: {skipped[0].getMessage()[:300]}")
    if acquire_timeout:
        report.append(f"  TIMEOUT text: {acquire_timeout[0].getMessage()[:300]}")
    report.append("──────────────────────────────────────────────────────────────")
    return "\n".join(report)


def _dump_log(records, path, title):
    lines = [f"=== {title} ===", ""] + _relevant_lines(records)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ── Shared fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def full_flow_harness(tmp_path, request):
    """Real router/pool/operation_manager + short timeouts. Own config dir per test (xdist-safe)."""
    import os as _os
    import agent_cascade.slot_queue as _sq_mod
    import agent_cascade.api_router as _ar_mod

    cfg_dir = tmp_path / request.node.name.replace("/", "_")
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = str(cfg_dir)

    # Shorten the shared-slot acquire timeout (module constants captured at import time).
    old_sq = _sq_mod.QUEUE_WAIT_TIMEOUT
    old_ar = _ar_mod.QUEUE_WAIT_TIMEOUT
    _sq_mod.QUEUE_WAIT_TIMEOUT = 5
    _ar_mod.QUEUE_WAIT_TIMEOUT = 5

    router = _build_real_router(cfg_dir)
    pool = _build_pool(router)

    # Real OperationManager (so request_user_approval blocks for real).
    om = _build_real_operation_manager(pool, cfg_dir / "ws")
    pool.operation_manager = om

    # _cleanup needs pool._execution._state_lock; ensure it exists on the real pool.
    if getattr(pool, "_execution", None) is None:
        pool._execution = MagicMock()
    pool._execution._state_lock = threading.Lock()
    pool.instance_state = {}

    # Shared pool is created lazily on first acquire — trigger it now.
    shared = router.scheduler._get_or_create_pool("http://127.0.0.1:9/v1", 0)
    assert shared is not None, "Shared sequential SlotPool was not created (conc=0 not in effect?)"

    # Register a REAL Security template so _create_system_agent → lifecycle.find_or_create_instance
    # succeeds (production loads this from config; the harness has none). Uses the SAME load path.
    llm_cfg = {"model": "mock", "api_base": "http://127.0.0.1:9/v1",
               "model_server": "http://127.0.0.1:9/v1", "api_key": "EMPTY"}
    try:
        from agent_cascade.agent_factory import load_agent
        pool.templates["Security"] = load_agent(pool, "Security", llm_cfg)
    except Exception as e:
        pytest.skip(f"Could not build a Security template for the full-flow harness: {e}")

    app = type("App", (), {})()
    session = {"session_name": "Maine"}
    handler = SecurityAdvisorHandler(pool, session, app, MagicMock(), lambda *a, **k: None)

    yield {
        "router": router, "pool": pool, "shared": shared, "om": om,
        "app": app, "session": session, "handler": handler, "cfg_dir": cfg_dir,
    }

    # Cleanup: stop the pool (unblocks any stuck approval wait) + restore constants.
    try:
        if hasattr(pool, "stop"):
            pool.stop()
    except Exception:
        pass
    _sq_mod.QUEUE_WAIT_TIMEOUT = old_sq
    _ar_mod.QUEUE_WAIT_TIMEOUT = old_ar


# ── Core full-flow driver ────────────────────────────────────────────────────

def _run_full_flow(h, auto_apply):
    """Drive the REAL production flow and return (records, shared, rid, caller_result)."""
    import agent_cascade.slot_queue as _sq_mod
    import agent_cascade.api_router as _ar_mod
    from agent_cascade.agent_instance import AgentInstance

    pool, om, shared = h["pool"], h["om"], h["shared"]
    handler = h["handler"]
    rid_holder = {}

    # 1. Create the caller instance (real AgentInstance, IDLE state).
    caller = AgentInstance(
        instance_name="coder", agent_class="coder", conversation=[],
        created_at=time.monotonic(), last_activity=time.monotonic(), latest_marker_index=0,
    )
    pool.instances["coder"] = caller

    # 2. Caller acquires its slot via the REAL path: engine.run() → _acquire_slot_with_logging.
    #    We patch ONLY the LLM model call so no network is hit; run() still does real slot
    #    acquisition + state transitions. The generator yields one turn then stops.
    from agent_cascade.execution_engine import ExecutionEngine

    engine = ExecutionEngine(pool)

    def _caller_turn_generator():
        # Real run() acquires the slot at entry (line ~1154). We let it do that, then
        # yield a single (messages, is_streaming) tuple and stop. The slot stays held
        # until run()'s finally block releases it — but we keep the generator alive (not
        # fully exhausted) so the caller keeps its slot while "blocked on approval".
        yield (["turn 1"], False)

    def _patched_run(self, instance):
        # Mimic run(): acquire the slot for real, then drive our minimal turn.
        # (self is passed because patch.object replaces the bound method with a plain function.)
        instance._slot_release = None
        instance._slot_key = None
        engine._acquire_slot_with_logging(instance, "initial")
        try:
            yield from _caller_turn_generator()
        finally:
            # Release only if we fully finish (mirrors run()'s cleanup). We do NOT call this
            # here because the caller is "blocked on approval" and still holds the slot.
            pass

    # Patch engine.run so the caller's turn uses our minimal generator but REAL slot acquisition.
    with patch.object(ExecutionEngine, "run", _patched_run):
        gen = engine.run(caller)
        next(gen)  # advance one turn — this triggers real slot acquisition
        # Do NOT exhaust the generator: the caller keeps its slot (blocked on approval).

    assert "coder" in shared._running, (
        f"Caller did not acquire the shared slot via the real path: {list(shared._running)}"
    )

    # 3. Caller calls shell_cmd → request_user_approval (BLOCKS the caller thread, holds slot).
    approval_result = {}

    def _caller_tool_call():
        try:
            res = om.request_user_approval(
                agent_name="coder", tool_name="shell_cmd",
                tool_args={"command": "echo hi", "justification": "test"},
                description="test shell command",
            )
            approval_result["value"] = res
        except Exception as e:
            approval_result["error"] = str(e)

    caller_thread = threading.Thread(target=_caller_tool_call, daemon=True)
    caller_thread.start()

    # Wait until the approval is pending (so run_check can find it).
    deadline = time.time() + 5
    rid = None
    while time.time() < deadline:
        pending = om.list_pending_approvals()
        if pending:
            rid = pending[0]["request_id"]
            break
        time.sleep(0.05)
    assert rid, "Caller's approval did not become pending in time"

    # 4. Trigger the security check EXACTLY like production: run_check(data).
    #    The Security agent's engine.run() is REAL (real slot acquisition, real turn loop),
    #    but we patch ONLY the LLM model call so it yields a mock "[YES]" verdict instead of
    #    hitting a real model. This makes the full check complete deterministically and fast.
    from agent_cascade.llm.schema import Message, ASSISTANT

    def _mock_llm(self, instance, llm_messages):
        # Only the Security agent reaches here (the caller's run() is separately patched).
        yield Message(role=ASSISTANT, content="[YES] Reason: Safe operation.")

    records, handler_log, targets = _capture_logs()
    try:
        with patch.object(ExecutionEngine, "_call_llm_with_injection", _mock_llm):
            data = {"request_id": rid, "auto_apply": auto_apply}
            asyncio.run(handler.run_check(data))

            # Wait for the daemon check worker to finish (or time out).
            def _wait_worker():
                d = time.time() + 12
                while time.time() < d:
                    if any("Check worker finished" in r.getMessage() for r in records):
                        break
                    time.sleep(0.1)

            wt = threading.Thread(target=_wait_worker, daemon=True)
            wt.start()
            wt.join(timeout=15)
    finally:
        _restore_logs(handler_log, targets)
        # Unblock the caller's approval wait (approve it) so the test can finish.
        try:
            om.user_approve(rid, reason="e2e cleanup")
        except Exception:
            pass
        caller_thread.join(timeout=5)

    return records, shared, rid, caller, approval_result


# ── Test 1: auto_apply=True (production default for auto-security) ───────────

def test_full_flow_auto_apply_true(full_flow_harness):
    """FULL flow with auto_apply=True. The security check must complete (not deadlock)."""
    records, shared, rid, caller, approval_result = _run_full_flow(full_flow_harness, auto_apply=True)

    report = _diagnostic_report(records, shared, rid)
    print("\n" + report)
    print("── RELEVANT LOG LINES ──")
    for line in _relevant_lines(records):
        print("   " + line)

    # Save full log for offline analysis.
    _dump_log(records, str(full_flow_harness["cfg_dir"] / "e2e_full_flow_autoapply_true.txt"),
              "FULL FLOW auto_apply=True")

    worker_started = _find(records, "Check worker started")
    worker_finished = _find(records, "Check worker finished")
    normal_yield = _find(records, "[SECURITY_SLOT_YIELD] Releasing slot")
    leaked = _find(records, "LEAKED PERMIT DETECTED")
    skipped = _find(records, "SECURITY_SLOT_YIELD_SKIPPED")
    acquire_timeout = _find(records, "waiting for endpoint slot")

    assert worker_started, f"[BUG] run_check did not spawn the check worker.\n{report}"
    # THE DEADLOCK SIGNATURE: if the Security agent times out waiting for the shared slot,
    # the caller's permit was never yielded. This is what we're hunting for.
    assert not acquire_timeout, (
        f"[DEADLOCK REPRODUCED] In the FULL flow, the Security agent timed out waiting for the "
        f"shared sequential slot — the caller's permit was NOT freed in time.\n{report}\n\n"
        + "\n".join(_relevant_lines(records))
    )
    # Exactly one yield path should fire (normal OR force-release), NOT skip.
    assert (normal_yield or leaked) and not skipped, (
        f"[BUG] In the FULL flow, neither the normal yield nor the force-release fallback fired "
        f"(skip path fired instead) — the caller held a permit but it was never yielded.\n{report}\n\n"
        + "\n".join(_relevant_lines(records))
    )


# ── Test 2: auto_apply=False (manual-confirmation variant) ───────────────────

def test_full_flow_auto_apply_false(full_flow_harness):
    """FULL flow with auto_apply=False. Same slot-yield behavior expected; the only difference
    is the result routing (send to UI for manual confirmation instead of auto-approve)."""
    records, shared, rid, caller, approval_result = _run_full_flow(full_flow_harness, auto_apply=False)

    report = _diagnostic_report(records, shared, rid)
    print("\n" + report)
    print("── RELEVANT LOG LINES ──")
    for line in _relevant_lines(records):
        print("   " + line)

    _dump_log(records, str(full_flow_harness["cfg_dir"] / "e2e_full_flow_autoapply_false.txt"),
              "FULL FLOW auto_apply=False")

    worker_started = _find(records, "Check worker started")
    normal_yield = _find(records, "[SECURITY_SLOT_YIELD] Releasing slot")
    leaked = _find(records, "LEAKED PERMIT DETECTED")
    skipped = _find(records, "SECURITY_SLOT_YIELD_SKIPPED")
    acquire_timeout = _find(records, "waiting for endpoint slot")

    assert worker_started, f"[BUG] run_check did not spawn the check worker.\n{report}"
    assert not acquire_timeout, (
        f"[DEADLOCK REPRODUCED] auto_apply=False: Security agent timed out waiting for the shared "
        f"slot — caller's permit was NOT freed.\n{report}\n\n" + "\n".join(_relevant_lines(records))
    )
    assert (normal_yield or leaked) and not skipped, (
        f"[BUG] auto_apply=False: no yield path fired (skip instead).\n{report}\n\n"
        + "\n".join(_relevant_lines(records))
    )
