"""BUG_0291 debug-log-noise cleanup regression guards.

These tests pin the *log-noise* behaviour that BUG_0291 introduced:
  - item 4: sanity-probe skip lines log once per (endpoint, reason), not per call.
  - item 8: toggle-off state save/restore skip lines log once per process.
  - item 3: [CALL_AGENT_DEBUG] scheduler acquire detail is gated behind settings.DEBUG_SLOTS
            (default OFF) and reappears when the knob is enabled.
  - item 6: run_loop no longer emits the ENTRY / [TURN_START] debug lines, but keeps the
            canonical EXIT line.

caplog target: the real emitting logger is `agent_cascade_logger` (agent_cascade/log.py),
which has level NOTSET and propagate=True, so `caplog.at_level(DEBUG)` on the ROOT captures
it. We set the root level (no logger= kwarg) to avoid the child-logger-explicit-level trap.
"""

import logging
import re
import threading

from agent_cascade import settings
from agent_cascade.api_router_pkg.router import APIRouter
from agent_cascade.api_router_pkg.scheduler import EndpointScheduler
from agent_cascade.engine import slot_lifecycle


# ── item 4: sanity-probe skip logs once per (endpoint, reason) ───────────────

def _bare_router():
    """APIRouter instance exercising only _log_sanity_skip (no real config/network)."""
    r = APIRouter.__new__(APIRouter)
    r._lock = threading.RLock()
    r._sanity_probe_logged = {}
    return r


def test_item4_sanity_probe_skip_logs_once_per_endpoint_reason(caplog):
    r = _bare_router()
    ep = ('http://base', 'model-x')
    with caplog.at_level(logging.DEBUG):
        caplog.clear()
        r._log_sanity_skip(ep, 'breaker-open', '[SANITY] skipping probe (model=%s)', 'model-x')
        r._log_sanity_skip(ep, 'breaker-open', '[SANITY] skipping probe (model=%s)', 'model-x')
        first_pass = [rec for rec in caplog.records
                      if 'skipping probe (model=model-x)' in rec.getMessage()]
        # Same (endpoint, reason) twice -> exactly one record.
        assert len(first_pass) == 1, f"expected 1 record for repeat skip, got {len(first_pass)}"

        # Different reason for the SAME endpoint -> one additional record.
        r._log_sanity_skip(ep, 'live-connection', '[SANITY] skipping probe (model=%s)', 'model-x')
        after_reason_change = [rec for rec in caplog.records
                               if 'skipping probe (model=model-x)' in rec.getMessage()]
        assert len(after_reason_change) == 2, (
            f"expected 2 records after reason change, got {len(after_reason_change)}")


# ── item 8: toggle-off state save/restore skip logs once per process ─────────

def test_item8_state_skip_logs_once_across_many_calls(caplog):
    # Snapshot the process-global memo (save/restore keys are module-level) so this test is
    # deterministic regardless of test order AND leaves no state leaked to later tests/production.
    saved_memo = set(slot_lifecycle._STATE_SKIP_LOGGED)
    slot_lifecycle._STATE_SKIP_LOGGED.clear()
    try:
        with caplog.at_level(logging.DEBUG):
            caplog.clear()
            for _ in range(5):
                slot_lifecycle._log_state_skip_once('save', '[STATE_SAVE_SKIP] %s (%s)', 'inst', 'slot-yield')
            for _ in range(3):
                slot_lifecycle._log_state_skip_once('restore', '[STATE_RESTORE_SKIP] %s (%s)', 'inst', 'slot-yield')
            save_records = [rec for rec in caplog.records if '[STATE_SAVE_SKIP]' in rec.getMessage()]
            restore_records = [rec for rec in caplog.records if '[STATE_RESTORE_SKIP]' in rec.getMessage()]
        assert len(save_records) == 1, f"expected exactly 1 save-skip record, got {len(save_records)}"
        assert len(restore_records) == 1, f"expected exactly 1 restore-skip record, got {len(restore_records)}"
    finally:
        # Restore the original contents so the memo is not left dirty on failure or success.
        slot_lifecycle._STATE_SKIP_LOGGED.clear()
        slot_lifecycle._STATE_SKIP_LOGGED.update(saved_memo)


# ── item 3: scheduler acquire-detail gated behind DEBUG_SLOTS (default OFF) ──

GATED_DETAIL = '[CALL_AGENT_DEBUG] EndpointScheduler.acquire'

def test_item3_scheduler_acquire_detail_gated_by_default(caplog, monkeypatch):
    """Unlimited-acquire early-return: the [CALL_AGENT_DEBUG] detail line is emitted ONLY
    when settings.DEBUG_SLOTS is on; with the knob off (default) it is absent."""
    scheduler = EndpointScheduler()
    monkeypatch.setattr(settings, 'DEBUG_SLOTS', False)
    with caplog.at_level(logging.DEBUG):
        caplog.clear()
        release = scheduler.acquire('http://x/v1', -1, instance_name='i', agent_class='c')
        assert release is None  # unlimited path returns None
        assert not [rec for rec in caplog.records if GATED_DETAIL in rec.getMessage()], (
            'gated scheduler detail must be absent with DEBUG_SLOTS off')

        monkeypatch.setattr(settings, 'DEBUG_SLOTS', True)
        caplog.clear()
        release = scheduler.acquire('http://x/v1', -1, instance_name='i', agent_class='c')
        assert release is None
        gated = [rec for rec in caplog.records if GATED_DETAIL in rec.getMessage()]
        assert gated, 'gated scheduler detail must reappear with DEBUG_SLOTS on'


# ── item 6: run_loop ENTRY / [TURN_START] debug removed, canonical EXIT kept ─

def test_item6_run_loop_entry_and_turn_start_debug_removed():
    """Static guard: the deleted ENTRY/TURN_START debug strings must not reappear, while the
    lossless canonical EXIT line is still emitted."""
    src = re.sub(r'[\'"]', '', _run_loop_source())
    assert 'engine.run() ENTRY' not in src, 'deleted ENTRY debug line reappeared'
    assert '[TURN_START]' not in src, 'deleted TURN_START debug line reappeared'
    assert 'EXIT - %s' in src, 'canonical EXIT line must remain'


def _run_loop_source():
    import inspect
    from agent_cascade.engine import run_loop
    return inspect.getsource(run_loop)
