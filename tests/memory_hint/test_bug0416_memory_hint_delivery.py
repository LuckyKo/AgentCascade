"""BUG_0416 — memory-hint delivery-loss fix: two opt-in levers + surfacing counters.

Unit-level, deterministic (no full run-loop harness — the memory-matching gate is
environment-dependent, so these target the two LEVER methods directly):

  * Lever 1 (MEMORY_HINT_INLINE_WAIT_MS): the post-Phase-4 peek takes a bounded
    ``Event.wait`` so a worker that finishes within the window is claimed SAME TURN.
    ``wait_ms == 0`` (default) skips the wait → byte-identical to the historical peek.
  * Lever 2 (MEMORY_HINT_SURFACE_ON_TEXT_TURN): the end-of-run flush drains undrained
    ``_tool_warnings`` into a SYSTEM-role ``[SYSTEM NOTIFICATIONS]`` note the model can
    see (the dominant loss is a hint queued on a text-only final turn). OFF → inert.
  * Dedup: a byte-identical second ``_queue_tool_warning`` is dropped (bool return).

Both new settings default OFF ⇒ byte-identical to today.
"""
import threading
import time
from types import SimpleNamespace

from agent_cascade.llm.schema import Message, SYSTEM
from agent_cascade.memory_hint.handle import HintHandle
from agent_cascade.engine.memory_hint import MemoryHintMixin
from agent_cascade.compression.handler import CompressionHandler
from agent_cascade.operation_manager.path_security import _queue_tool_warning
import agent_cascade.settings as S


def _make_inst(warnings=None):
    return SimpleNamespace(
        instance_name='w',
        conversation=[],
        _tool_warnings=list(warnings or []),
        _compression_lock=threading.RLock(),
        _cached_messages=[],
        _cached_llm_messages=[],
        _pending_hint_job=None,
    )


def _make_probe_pool(inst):
    mgr = SimpleNamespace(_inline_delivered=0, _deferred_delivered=0,
                          _hint_drained_into_result=0)
    pool = SimpleNamespace(memory_hint_manager=mgr, get_instance=lambda name: inst)
    return pool, mgr


def _peek_probe(inst):
    """A bare MemoryHintMixin instance with ``.pool`` bound (no __init__ needed — the
    peek method only reads ``self.pool`` and the module-level ``settings``)."""
    probe = MemoryHintMixin.__new__(MemoryHintMixin)
    probe.pool, _ = _make_probe_pool(inst)
    return probe


class TestBug0416InlineWait:
    """Lever 1: bounded micro-wait (MEMORY_HINT_INLINE_WAIT_MS) lets the worker win the peek."""

    def test_wait_zero_abandons_not_ready(self, monkeypatch):
        """wait_ms=0 (default) → the peek does NOT wait → a not-ready handle is abandoned
        (deferred), never claimed. Byte-identical to the historical non-blocking peek."""
        monkeypatch.setattr(S, 'MEMORY_HINT_INLINE', True)
        monkeypatch.setattr(S, 'MEMORY_HINT_INLINE_WAIT_MS', 0)
        inst = _make_inst()
        h = HintHandle('w', 1)
        inst._pending_hint_job = h
        probe = _peek_probe(inst)
        probe._maybe_attach_inline_memory_hint(inst, [], [])
        assert h.abandoned is True, 'a not-ready handle with wait_ms=0 must be abandoned'
        assert h.owner is None, 'no inline claim may occur without a wait'
        assert probe.pool.memory_hint_manager._inline_delivered == 0

    def test_wait_positive_claims_ready_same_turn(self, monkeypatch):
        """wait_ms >= worker latency → the bounded wait lets the worker finalize within the
        window → the hint is claimed INLINE (the Lever-1 fix). Fails on pre-change code
        (no bounded wait existed, so the peek always saw a not-ready handle)."""
        monkeypatch.setattr(S, 'MEMORY_HINT_INLINE', True)
        monkeypatch.setattr(S, 'MEMORY_HINT_INLINE_WAIT_MS', 1000)
        inst = _make_inst()
        h = HintHandle('w', 1)
        h.text = '[MEMORY HINT] compression-debug.md'
        inst._pending_hint_job = h

        def _worker():
            time.sleep(0.05)          # finalize the hint 50ms after the peek starts
            h.ready.set()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        probe = _peek_probe(inst)
        probe._maybe_attach_inline_memory_hint(inst, [], [])
        t.join(timeout=2.0)
        assert h.owner == 'inline', 'the bounded wait must let the worker win the claim'
        assert probe.pool.memory_hint_manager._inline_delivered == 1
        assert any('[MEMORY HINT]' in w for w in inst._tool_warnings), \
            'the claimed hint must land on the instance notification queue'


class TestBug0416NextTurnSurfacing:
    """Lever 2 (B1): the end-of-run flush surfaces undrained hints as a SYSTEM note."""

    def _handler(self, inst):
        pool, mgr = _make_probe_pool(inst)
        handler = CompressionHandler.__new__(CompressionHandler)
        handler.pool = pool
        return handler, mgr

    def test_flush_surfaces_system_note(self):
        """A queued hint is drained into a SYSTEM-role [SYSTEM NOTIFICATIONS] note on the
        conversation (the model-visible surface). THE regression guard: fails pre-change
        (no flush method existed), passes post-change."""
        inst = _make_inst(warnings=['[MEMORY HINT] compression-debug.md'])
        handler, mgr = self._handler(inst)
        assert handler._drain_tool_warnings_into_system_note(inst) is True
        assert len(inst.conversation) == 1
        note = inst.conversation[0]
        assert isinstance(note, Message) and note.role == SYSTEM, \
            'the surfaced note must be SYSTEM-role (R1: never USER)'
        assert '[MEMORY HINT]' in note.content
        assert inst._tool_warnings == [], 'the flush must drain the pending queue'
        assert mgr._hint_drained_into_result == 1, \
            'the surfacing counter must prove a hint reached model context'
        assert inst._cached_messages == [] and inst._cached_llm_messages == [], \
            'build caches must be cleared so the next run sees the note'

    def test_flush_noop_when_queue_empty(self):
        """An empty queue appends nothing and bumps no counter (byte-identical to today)."""
        inst = _make_inst(warnings=[])
        handler, mgr = self._handler(inst)
        assert handler._drain_tool_warnings_into_system_note(inst) is False
        assert inst.conversation == []
        assert mgr._hint_drained_into_result == 0


class TestBug0416Dedup:
    """_queue_tool_warning dedup + bool return (pin the documented drop)."""

    def test_dedup_collision_same_text(self):
        inst = _make_inst()
        pool, _ = _make_probe_pool(inst)
        assert _queue_tool_warning(pool, 'w', 'dup warning') is True, 'first entry must land'
        assert _queue_tool_warning(pool, 'w', 'dup warning') is False, \
            'a byte-identical second entry must be dropped (dedup)'
        assert inst._tool_warnings == ['dup warning']
