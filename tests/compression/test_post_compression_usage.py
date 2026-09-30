"""FIX 3 (todo.md:114) — post-compression context-usage readout is authoritative.

Before the fix, the four user-facing feedback sites each reported a different
numerator (stale ``result.tokens_after`` for S1/S2/S3, a char-4 heuristic for
S4) against the wrong denominator (``instance._allocated_max_input_tokens``, a
per-call cache of the PREVIOUS request's budget). After the fix all four route
through ``compression.core.measure_context_usage`` — the same math as the
min-usage guard and the engine's forced-path warning:

  numerator   = sum(get_message_stats(m)['tokens'] for m in full conv)
                + estimate_functions_tokens(active functions)
  denominator = _resolve_max_tokens - compression_context_reserve_tokens
                (floored to max when that would go <= 0)

T1/T3/T4 fail before the fix (proven via a clean worktree at the parent commit);
T7 is the gate that ``tests/compression/test_min_usage_guard.py`` passes
unmodified after the ``_estimate_usage_pct`` → ``measure_context_usage`` delegation.
"""
import re
import threading
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.compression.core import _estimate_usage_pct, measure_context_usage
from agent_cascade.compression.handler import CompressionHandler
from agent_cascade.compression.result import CompressResult
from agent_cascade.llm.schema import SYSTEM, USER, Message
from agent_cascade.utils.utils import get_message_stats
from tests.conftest import MockAgentPool

NAME = 'TestAgent'
RESERVE = 3000
MAX_TOKENS = 10_000
EFFECTIVE_LIMIT = MAX_TOKENS - RESERVE  # 7000
TOOLS = 2000

FEEDBACK_RE = re.compile(r'Context: (\d+)/(\d+) tokens \(([\d.]+)% used\)')


def _make_msg(role, content):
    return Message(role=role, content=content)


class _SettingsStub:
    """Minimal settings object (MockAgentPool has no settings)."""

    def __init__(self, reserve_tokens: int = RESERVE):
        self.compression_context_reserve_tokens = reserve_tokens
        self.compression_min_usage_pct = 50.0


def _build_history() -> List[Message]:
    """SYSTEM + a pre-existing <context_summary> marker block (before the active
    slice) + an active tail — so the full conversation is strictly larger than
    the working set the old numerators were based on."""
    history = [_make_msg(SYSTEM, 'You are a test agent')]
    # Pre-marker block: summary of an earlier compression.
    history.append(_make_msg(USER, '<context_summary> Earlier conversation summarized here. ' * 4))
    for i in range(3):
        body = f"User question number {i} about the weather and planning " * 3
        history.append(_make_msg(USER, body))
        history.append(_make_msg('assistant', f"Assistant reply number {i} with details " * 3))
    return history


def _make_pool(reserve_tokens: int = RESERVE) -> MockAgentPool:
    """Copy of the test_min_usage_guard.py pool pattern (do not share across modules),
    plus the small extra surface the handler/tool success paths touch."""
    pool = MockAgentPool(_build_history())
    pool.settings = _SettingsStub(reserve_tokens=reserve_tokens)
    inst = pool.instances[NAME]
    inst.agent_class = 'coder'
    inst.instance_name = NAME
    inst._allocated_max_input_tokens = 1234  # deliberately wrong — must never appear in output
    inst._compression_lock = threading.RLock()
    inst.parent_instance = None                    # stream push path
    inst._force_compress_count = 0                 # S3 log line
    # Extra surface beyond test_min_usage_guard's needs:
    pool.get_instance = lambda name: pool.instances.get(name)       # S1 tool path
    pool.slice_history_for_llm = lambda conv: list(conv)            # S2 rebuild path
    pool.halt_all_instances = lambda except_instances=None: None    # S3 forced path
    pool.resume_all_instances = lambda *a, **k: None                # S3 finally block
    pool.get_template = lambda name: object()                       # tool-schema resolution in measure_context_usage
    return pool


class _EngineStubForT2:
    """Minimal engine stand-in for T2: self._get_max_tokens + self.pool (bound-method access)."""

    def __init__(self, pool):
        self.pool = pool

    def _get_max_tokens(self, instance):
        return MAX_TOKENS


def _true_pair(pool) -> tuple:
    """Authoritative pair over the pool's CURRENT conversation (re-counted each call).

    The handler paths append a notification to instance.conversation after computing the
    readout, so callers must invoke this BEFORE running the path under test.
    """
    conv = pool.get_conversation(NAME)
    return sum(get_message_stats(m)['tokens'] for m in conv) + TOOLS, EFFECTIVE_LIMIT


def _stub_engine():
    """Engine stub with the minimal surface the handler paths touch.

    ``_append_and_log`` is implemented against instance.conversation so tests can
    read the appended notification back (the real one also writes a JSONL log).
    """
    eng = MagicMock()
    eng._telemetry.return_value = None  # skip telemetry recording

    def _append_and_log(instance, msg, **kwargs):
        with instance._compression_lock:
            instance.conversation.append(msg)

    eng._append_and_log.side_effect = _append_and_log
    return eng


def _ok_result(tokens_after: int = 500) -> CompressResult:
    return CompressResult(success=True, summary_text='s', marker_message=None,
                          messages_discarded=3, tail_count=2, error=None, mode='auto',
                          tokens_before=100, tokens_after=tokens_after)


def _context_clause(msg: str):
    m = FEEDBACK_RE.search(msg)
    assert m, f"no context clause in feedback: {msg!r}"
    return int(m.group(1)), int(m.group(2)), float(m.group(3))


def _pin_patches():
    """Pins for the authoritative math (shared by all tests)."""
    return [
        patch('agent_cascade.api_integration_pkg.tokens._resolve_max_tokens', return_value=MAX_TOKENS),
        patch('agent_cascade.compression.core._get_active_functions_from_template',
              return_value=[{'name': 'f'}] * 50),
        patch('agent_cascade.compression.core.estimate_functions_tokens', return_value=TOOLS),
    ]


# ── T1 — the pin: S2 wiring reports the authoritative pair (fails pre-fix) ───

def test_t1_s2_reports_authoritative_pair():
    pool = _make_pool()
    inst = pool.instances[NAME]
    handler = CompressionHandler(pool)
    handler.set_engine(_stub_engine())

    true_tokens, _ = _true_pair(pool)
    wrong = true_tokens + 5000  # deliberately far off the true count
    with _pin_patches()[0], _pin_patches()[1], _pin_patches()[2], \
         patch('agent_cascade.compression.core.compress_context', return_value=_ok_result(wrong)):
        out = handler.handle_compress_tool({'fraction': 0.5}, inst, NAME)

    x, y, pct = _context_clause(out)
    assert y == EFFECTIVE_LIMIT, f"denominator must be the reserve-adjusted limit, got {y}"
    assert x == true_tokens, f"numerator must be the real tokenizer count, got {x} (true {true_tokens})"
    assert abs(pct - x / EFFECTIVE_LIMIT * 100) < 0.05, "pair must be self-consistent with the formatter's rounding"
    assert x != wrong, 'reported tokens must NOT be result.tokens_after (anti-regression)'


# ── T2 — cross-check against the engine helpers ───────────────────────────────

def test_t2_matches_engine_helpers():
    from agent_cascade.engine.core import ExecutionEngine
    pool = _make_pool()
    inst = pool.instances[NAME]
    conv = pool.get_conversation(NAME)

    with _pin_patches()[0], _pin_patches()[1], _pin_patches()[2]:
        pair = measure_context_usage(pool, NAME, conv)
        # Engine-side: the same math through the engine's own helpers. The mock
        # instance has no template machinery, so functions=[] on the engine side —
        # compare against messages-only + our pinned tool tokens accordingly.
        eng_msg_tokens = ExecutionEngine._count_history_tokens(None, conv)
        # _get_effective_limit is a bound method (self=engine): it reads self.pool.settings.
        # Call it through a minimal engine stand-in whose .pool IS the mock pool.
        eng_limit = ExecutionEngine._get_effective_limit(_EngineStubForT2(pool), inst)

    assert pair is not None
    x, y = pair
    assert y == eng_limit, f"denominator must match engine effective limit: {y} vs {eng_limit}"
    # Numerator tolerance ±2% (same get_message_stats path; tool-schema resolution differs).
    expected = eng_msg_tokens + TOOLS
    assert abs(x - expected) <= max(10, expected * 0.02), f"{x} vs expected {expected}"


# ── T3 — all four in-scope sites agree (fails pre-fix) ────────────────────────

def _run_s4(pool):
    """Drive handle_compress_command (/compress) end-to-end with a stubbed tool."""
    inst = pool.instances[NAME]
    handler = CompressionHandler(pool)
    handler.set_engine(_stub_engine())
    compress_tool_mock = MagicMock(call=lambda *a, **k: 'ok')
    with _pin_patches()[0], _pin_patches()[1], _pin_patches()[2], \
         patch.object(handler, 'detect_and_parse_compress_command', return_value=0.5), \
         patch.object(pool, 'get_template', create=True,
                      return_value=MagicMock(function_map={'compress_context': compress_tool_mock})), \
         patch.object(handler, '_sync_logger_after_compression'), \
         patch('agent_cascade.compression.handler.validate_message_pool', return_value=True):
        ok = handler.handle_compress_command(inst, [Message(role=USER, content='x')], [])
    assert ok is True
    return inst.conversation[-1].content


# ── T3 — all four in-scope sites agree (fails pre-fix) ────────────────────────

def test_t3_all_four_sites_agree():
    from agent_cascade.tools.custom.compression_tools import CompressContext

    # S1 — the compress_context tool.
    pool1 = _make_pool()
    inst1 = pool1.instances[NAME]
    tool = CompressContext(agent_pool=pool1, agent_name=NAME)
    with _pin_patches()[0], _pin_patches()[1], _pin_patches()[2], \
         patch('agent_cascade.tools.custom.compression_tools.compress_context',
               return_value=_ok_result(987654)):
        out_s1 = tool.call('{"fraction": 0.5}')

    # S2 — handle_compress_tool (same shape as T1).
    pool2 = _make_pool()
    inst2 = pool2.instances[NAME]
    handler2 = CompressionHandler(pool2)
    handler2.set_engine(_stub_engine())
    with _pin_patches()[0], _pin_patches()[1], _pin_patches()[2], \
         patch('agent_cascade.compression.core.compress_context', return_value=_ok_result(987654)):
        out_s2 = handler2.handle_compress_tool({'fraction': 0.5}, inst2, NAME)

    # S3 — forced compression (execute_force_compression).
    pool3 = _make_pool()
    inst3 = pool3.instances[NAME]
    handler3 = CompressionHandler(pool3)
    handler3.set_engine(_stub_engine())
    with _pin_patches()[0], _pin_patches()[1], _pin_patches()[2], \
         patch('agent_cascade.compression.core.compress_context', return_value=_ok_result(987654)), \
         patch.object(handler3, '_sync_logger_after_compression'), \
         patch('agent_cascade.compression.handler.validate_message_pool', return_value=True):
        ok = handler3.execute_force_compression(inst3, [Message(role=USER, content='x')], [], 90.0)
    assert ok is True
    out_s3 = inst3.conversation[-1].content

    # S4 — /compress command (handle_compress_command).
    pool4 = _make_pool()
    out_s4 = _run_s4(pool4)

    pairs = {}
    for label, out in (('S1', out_s1), ('S2', out_s2), ('S3', out_s3), ('S4', out_s4)):
        m = FEEDBACK_RE.search(out)
        assert m, f"{label}: no context clause: {out!r}"
        pairs[label] = (int(m.group(1)), int(m.group(2)))

    x, y = pairs['S1']
    true_x, true_y = _true_pair(pool1)
    for label, (px, py) in pairs.items():
        assert (px, py) == (x, y), f"{label} reports {px}/{py}, S1 reports {x}/{y}"
    assert (x, y) == (true_x, true_y), f"S1 reports {x}/{y}, expected authoritative {true_x}/{true_y}"


# ── T4 — denominator uses the reserve; _allocated_max_input_tokens ignored ───

def test_t4_denominator_uses_reserve():
    pool = _make_pool(reserve_tokens=RESERVE)
    inst = pool.instances[NAME]
    handler = CompressionHandler(pool)
    handler.set_engine(_stub_engine())
    with _pin_patches()[0], _pin_patches()[1], _pin_patches()[2], \
         patch('agent_cascade.compression.core.compress_context', return_value=_ok_result()):
        out = handler.handle_compress_tool({'fraction': 0.5}, inst, NAME)
    _, y, _ = _context_clause(out)
    assert y == EFFECTIVE_LIMIT, f"reserve must be subtracted: expected {EFFECTIVE_LIMIT}, got {y}"
    assert '1234' not in out, 'instance._allocated_max_input_tokens must never appear in the output'

    # Contrast case: reserve 0 → full max.
    pool0 = _make_pool(reserve_tokens=0)
    inst0 = pool0.instances[NAME]
    handler0 = CompressionHandler(pool0)
    handler0.set_engine(_stub_engine())
    with _pin_patches()[0], _pin_patches()[1], _pin_patches()[2], \
         patch('agent_cascade.compression.core.compress_context', return_value=_ok_result()):
        out0 = handler0.handle_compress_tool({'fraction': 0.5}, inst0, NAME)
    _, y0, _ = _context_clause(out0)
    assert y0 == MAX_TOKENS, f"reserve=0 must give the full max: expected {MAX_TOKENS}, got {y0}"


# ── T5 — fail-soft: measure_context_usage None or raising still renders ──────

@pytest.mark.parametrize('mode', ['none', 'raise'])
def test_t5_fail_soft(mode):
    pool = _make_pool()
    inst = pool.instances[NAME]
    handler = CompressionHandler(pool)
    handler.set_engine(_stub_engine())
    if mode == 'none':
        mcm = patch('agent_cascade.compression.core.measure_context_usage', return_value=None)
    else:
        mcm = patch('agent_cascade.compression.core.measure_context_usage', side_effect=RuntimeError('boom'))
    with _pin_patches()[0], \
         patch('agent_cascade.compression.core.compress_context', return_value=_ok_result(500)), \
         mcm:
        out = handler.handle_compress_tool({'fraction': 0.5}, inst, NAME)
    assert '[COMPRESSION]' in out, f"feedback banner missing ({mode}): {out!r}"
    m = FEEDBACK_RE.search(out)
    assert m is not None, f"fallback context clause must render ({mode}): {out!r}"
    x, y = int(m.group(1)), int(m.group(2))
    # Fallback pair = (result.tokens_after, instance._allocated_max_input_tokens or 0).
    assert (x, y) == (500, 1234), f"fallback must be the old pair ({mode}): got {x}/{y}"


# ── T6 — failure path unchanged: no context clause on a failed compression ───

def test_t6_failure_path_unchanged():
    # S2 failure branch: handle_compress_tool's else-branch returns
    # f"Compression failed: {result.error}" — no context clause by construction.
    fail_result = CompressResult(success=False, summary_text=None, marker_message=None,
                                 messages_discarded=0, tail_count=0, error='boom', mode='auto')
    pool = _make_pool()
    inst = pool.instances[NAME]
    handler = CompressionHandler(pool)
    handler.set_engine(_stub_engine())
    with patch('agent_cascade.compression.core.compress_context', return_value=fail_result):
        out = handler.handle_compress_tool({'fraction': 0.5}, inst, NAME)
    assert 'compression failed: boom' in out.lower() or 'Compression failed: boom' in out
    assert 'tokens (' not in out, 'failure text must not carry a success-style context clause'

    # S3 failure branch: _format_compression_failure reports pre-compression usage only.
    fail_text = CompressionHandler._format_compression_failure('forced', 'boom', 90.0)
    assert 'failed' in fail_text.lower()
    assert 'tokens (' not in fail_text, 'failure text must not carry a success-style context clause'


# ── T7 — min-usage guard unaffected (refactor gate) ──────────────────────────

def test_t7_min_usage_guard_unmodified():
    """The delegation must be behaviour-preserving: _estimate_usage_pct returns the
    same percentage measure_context_usage would, and None on failure."""
    pool = _make_pool()
    conv = pool.get_conversation(NAME)
    with _pin_patches()[0], _pin_patches()[1], _pin_patches()[2]:
        pair = measure_context_usage(pool, NAME, conv)
        pct = _estimate_usage_pct(pool, NAME, conv)
    assert pair is not None and pct is not None
    x, y = pair
    assert abs(pct - x / y * 100) < 1e-9
    # Failure path: unknown instance → both None.
    with _pin_patches()[0]:
        assert measure_context_usage(pool, 'NoSuchAgent', conv) is None
        assert _estimate_usage_pct(pool, 'NoSuchAgent', conv) is None


# ── T8 — notification not self-counted (two /compress runs) ──────────────────

def test_t8_notification_not_self_counted():
    def run_once(pool):
        out = _run_s4(pool)
        m = FEEDBACK_RE.search(out)
        assert m, f"no context clause: {out!r}"
        return int(m.group(1))

    pool = _make_pool()
    inst = pool.instances[NAME]
    x1 = run_once(pool)
    # Second run on the same (now notification-bearing) conversation. The readout is taken
    # BEFORE the new notification is appended, so it must count the FIRST notification only —
    # i.e. delta == exactly that notification's token cost, never more.
    x2 = run_once(pool)
    delta = x2 - x1
    notif_tokens = get_message_stats(inst.conversation[-2])['tokens']  # first notification
    assert delta <= notif_tokens + 5, \
        f"second readout grew by {delta} — the appended notification must not be self-counted (first notif ≈ {notif_tokens})"
