"""BUG_0309 — post-L2-compaction data-integrity validation gate tests.

Covers the CompressionReviewer validation gate added to the L2 consolidation hop:
- _parse_compaction_verdict (scalar [PASS]/[LOSS: ...] parsing, fail-open on ambiguity)
- _validate_and_reconsolidate (bounded retry, best-so-far, fail-open on every error class)
- _consolidate_markers integration (gate wired between Phase 2 and Phase 3; knob disables it)

All tests are self-contained — no LLM or API server required. The reviewer + consolidation
invokers are patched at agent_cascade.compression.agent_invoker so the gate's lazy imports
resolve to the mocks.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.compression.agent_invoker import _parse_compaction_verdict
from agent_cascade.compression.core import _consolidate_markers, _validate_and_reconsolidate
from agent_cascade.settings import COMPRESSION_REVIEW_MAX_RETRIES

# Reuse the marker-history helpers from the known-green consolidation suite.
from tests.compression.test_memory_consolidation import _build_history_with_markers, _is_compression_marker

# ──────────────────────────────────────────────────────────────────────────────
# 1. Verdict parser (pure function)
# ──────────────────────────────────────────────────────────────────────────────


class TestParseCompactionVerdict:
    """Scalar [PASS]/[LOSS: items] parsing with the documented fallback ladder."""

    @pytest.mark.parametrize('raw', [
        '[PASS]',
        '**[PASS]**',
        'Reviewed. Nothing important lost.\n[PASS]',
        '\n\n[PASS]\n',
        "I think it's fine.",            # no token → ambiguous → PASS
        'no verdict here at all',        # no token → PASS
        '[LOSS:]',                       # malformed LOSS → ambiguous → PASS
        '[LOSS',                         # truncated → ambiguous → PASS
        'I considered [LOSS but decided it was fine',  # inline mention w/o bracket → PASS
    ])
    def test_pass_and_ambiguous_resolve_to_pass(self, raw):
        is_pass, items = _parse_compaction_verdict(raw)
        assert is_pass is True, f"expected PASS for {raw!r}, got {items}"
        assert items == []

    def test_simple_loss(self):
        is_pass, items = _parse_compaction_verdict('[LOSS: BUG_0309, core.py:300]')
        assert is_pass is False
        assert items == ['BUG_0309', 'core.py:300']

    def test_loss_markdown_bold(self):
        is_pass, items = _parse_compaction_verdict('**[LOSS: open task, last_supervisor_task]**')
        assert is_pass is False
        assert items == ['open task', 'last_supervisor_task']

    def test_loss_single_line(self):
        is_pass, items = _parse_compaction_verdict('[LOSS: BUG_0123]')
        assert is_pass is False and items == ['BUG_0123']

    def test_loss_items_stripped_and_empty_dropped(self):
        is_pass, items = _parse_compaction_verdict('[LOSS:  a ,  , b ]')
        assert is_pass is False
        assert items == ['a', 'b']

    def test_loss_surrounded_by_prose(self):
        raw = 'The summary dropped the carried task.\n[LOSS: carried task]\nDone.'
        is_pass, items = _parse_compaction_verdict(raw)
        assert is_pass is False
        assert items == ['carried task']

    def test_last_occurrence_loss_wins(self):
        # Verdict line is last; an earlier [PASS] mention must not win.
        is_pass, items = _parse_compaction_verdict('[PASS] but then found one loss\n[LOSS: BUG_999]')
        assert is_pass is False
        assert items == ['BUG_999']

    def test_last_occurrence_pass_wins(self):
        is_pass, items = _parse_compaction_verdict('[LOSS: stale mention]\nfinal verdict [PASS]')
        assert is_pass is True
        assert items == []


# ──────────────────────────────────────────────────────────────────────────────
# 2. Gate logic (_validate_and_reconsolidate) — direct unit tests
#
# The gate calls invoke_consolidation_agent / invoke_compaction_reviewer (lazily imported from
# agent_cascade.compression.agent_invoker). We patch those module symbols. _review is called with
# keyword args (agent_pool, incoming_summaries, outgoing_summary, caller_name); the reviewer mock
# inspects outgoing_summary to model per-candidate verdicts.
# ──────────────────────────────────────────────────────────────────────────────

_KW = dict(
    agent_pool=MagicMock(stopped=False),
    target_agent_name='Target',
    summaries_to_consolidate=['S1', 'S2'],
    summary_spans=[(None, None), (None, None)],
    initial_summary='INITIAL',
)


def _always(verdicts):
    """Reviewer side_effect: consume verdicts in order; repeat the last once exhausted.

    Each call returns (is_pass, items) regardless of candidate.
    """

    def fn(**kwargs):
        if verdicts:
            v = verdicts.pop(0) if len(verdicts) > 1 else verdicts[0]
        else:
            v = (True, [])
        return v

    return fn


def _verdict_list(*tuples):
    return list(tuples)


def _run_gate(review_side_effect, *, cons_return='CONSOLIDATED', retries=2, enabled=True, pool=None, mock_cons=None, mock_rev=None):
    pool = pool if pool is not None else MagicMock(stopped=False)
    with patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', enabled), \
            patch('agent_cascade.compression.core.COMPRESSION_REVIEW_MAX_RETRIES', retries), \
            patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                  return_value=(cons_return, '')) as mc, \
            patch('agent_cascade.compression.agent_invoker.invoke_compaction_reviewer',
                  side_effect=review_side_effect) as mr:
        result = _validate_and_reconsolidate(**{**_KW, 'agent_pool': pool})
    return result, mc, mr


class TestGateDirect:

    def test_pass_no_retry(self):
        result, mc, mr = _run_gate(_always([(True, [])]))
        assert result == 'INITIAL'
        assert mr.call_count == 1
        assert mc.call_count == 0  # consolidation retried only on LOSS

    def test_ambiguous_verdict_no_retry(self):
        # Parser-level ambiguity is resolved to PASS upstream; here the reviewer returns PASS.
        result, mc, mr = _run_gate(_always([(True, [])]))
        assert result == 'INITIAL' and mc.call_count == 0 and mr.call_count == 1

    def test_loss_then_pass_reconsolidates_with_flagged_item(self):
        seen_preserve = []

        def cons_side_effect(**kwargs):
            seen_preserve.append(kwargs.get('preserve_items'))
            return ('ROUND2', '')

        with patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', True), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEW_MAX_RETRIES', 2), \
                patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                      side_effect=cons_side_effect) as mc, \
                patch('agent_cascade.compression.agent_invoker.invoke_compaction_reviewer',
                      side_effect=_always([(False, ['BUG_0309']), (True, [])])) as mr:
            result = _validate_and_reconsolidate(**_KW)

        assert result == 'ROUND2'            # round-2 summary accepted after PASS
        assert mc.call_count == 1            # exactly one retry
        assert mr.call_count == 2            # reviewed INITIAL, then ROUND2
        assert seen_preserve[0] == ['BUG_0309']  # the flagged item is pinned on retry

    def test_exhaustion_is_bounded_returns_best_and_logs(self, caplog):
        pool = MagicMock(stopped=False)
        with caplog.at_level('ERROR'), patch.object(pool, 'stopped', False):
            result, mc, mr = _run_gate(_always([(False, ['x'])]), pool=pool, retries=2)
        # Gate itself calls consolidation once per retry = retries times (Phase-2 call is outside).
        assert mc.call_count == 2
        assert mr.call_count == 3  # INITIAL + 2 retries
        # Still returns a (best-so-far) summary, never raises.
        assert isinstance(result, str) and result
        assert any('exhaust' in r.message.lower() for r in caplog.records)

    def test_reviewer_raises_fails_open_no_reconsolidation(self, caplog):

        def boom(**kwargs):
            raise RuntimeError('reviewer infra down')

        with caplog.at_level('ERROR'):
            result, mc, mr = _run_gate(boom)
        assert result == 'INITIAL'
        assert mc.call_count == 0  # first-review failure short-circuits to fail-open
        assert any('fail-open' in r.message for r in caplog.records)

    def test_reviewer_raises_mid_retry_keeps_best(self):
        pool = MagicMock(stopped=False)

        def review(**kwargs):
            cand = kwargs.get('outgoing_summary')
            if cand == 'INITIAL':
                return (False, ['a', 'b'])
            # reviewer dies when asked to review the retry candidate → keep best-so-far (INITIAL).
            raise RuntimeError('reviewer crashed mid-retry')

        result, mc, mr = _run_gate(review, pool=pool, retries=2)
        assert result == 'INITIAL'
        assert mr.call_count == 2  # INITIAL reviewed ok, ROUND2 review crashed

    def test_best_so_far_fewest_items_wins(self):
        def review(**kwargs):
            cand = kwargs.get('outgoing_summary')
            counts = {'INITIAL': 3, 'R1': 5, 'R2': 1}
            return (False, ['i%d' % n for n in range(counts[cand])])

        seq = iter(['R1', 'R2'])  # two retries return distinct summaries
        with patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', True), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEW_MAX_RETRIES', 2), \
                patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                      side_effect=lambda **k: (next(seq), '')), \
                patch('agent_cascade.compression.agent_invoker.invoke_compaction_reviewer',
                      side_effect=review):
            result = _validate_and_reconsolidate(**_KW)

        assert result == 'R2'  # fewest flagged items (1) beats initial (3) and R1 (5)

    def test_ties_resolve_to_latest(self):
        def review(**kwargs):
            return (False, ['one'])  # every attempt flags exactly one item → all ties

        seq = iter(['R1', 'R2'])
        with patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', True), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEW_MAX_RETRIES', 2), \
                patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                      side_effect=lambda **k: (next(seq), '')), \
                patch('agent_cascade.compression.agent_invoker.invoke_compaction_reviewer',
                      side_effect=review):
            result = _validate_and_reconsolidate(**_KW)
        assert result == 'R2'  # latest attempt wins ties

    def test_disabled_never_invokes_reviewer(self):
        result, mc, mr = _run_gate(_always([(False, ['x'])]), enabled=False)
        assert result == 'INITIAL'
        assert mr.call_count == 0  # knob OFF → reviewer never invoked (byte-identical path)
        assert mc.call_count == 0

    def test_pool_stopped_breaks_loop(self, caplog):
        pool = MagicMock(stopped=True)  # stopped before the loop → no re-consolidation
        with caplog.at_level('INFO'):
            result, mc, mr = _run_gate(_always([(False, ['x'])]), pool=pool, retries=2)
        assert result == 'INITIAL'
        assert mc.call_count == 0  # loop broke on first iteration due to stopped pool
        # A pool stop is NOT a data-loss event: it must not emit the loss "exhausted" ERROR log.
        assert not [r for r in caplog.records if r.levelname == 'ERROR']

    def test_exhaustion_records_telemetry(self):
        retries = COMPRESSION_REVIEW_MAX_RETRIES  # attempts == retries + 1 (1 initial review + N re-consolidations)
        pool = MagicMock(stopped=False)
        tel = MagicMock()
        fake_engine = MagicMock()
        fake_engine._telemetry.return_value = tel
        with patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                   return_value=('C', '')), \
                patch('agent_cascade.compression.agent_invoker.invoke_compaction_reviewer',
                      side_effect=_always([(False, ['x'])])), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', True), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEW_MAX_RETRIES', retries), \
                patch('agent_cascade.execution_engine.ExecutionEngine', return_value=fake_engine):
            _validate_and_reconsolidate(**{**_KW, 'agent_pool': pool})
        tel.record_compaction_validation_loss.assert_called_once()
        args, kwargs = tel.record_compaction_validation_loss.call_args
        # (instance_name, attempts, missing_items_count)
        assert args[0] == 'Target'
        assert args[1] == retries + 1  # bounded: 1 initial review + COMPRESSION_REVIEW_MAX_RETRIES re-consolidations
        assert args[2] == 1


# ──────────────────────────────────────────────────────────────────────────────
# 3. _consolidate_markers integration — the gate is wired between Phase 2 and Phase 3
#
# Mirrors the known-green harness in test_memory_consolidation.py (patch AgentPool.find_all_marker_indices,
# threshold, invoke_consolidation_agent) and adds the reviewer patch. mock_pool.stopped is forced
# False so the retry loop is not short-circuited by MagicMock's auto-truthy attributes.
# ──────────────────────────────────────────────────────────────────────────────


def _integration_run(review_side_effect, inv_con_side_effect, *, retries=2, enabled=True, num_markers=8):
    history = _build_history_with_markers(num_markers=num_markers, msgs_between=2)
    mock_inst = MagicMock()
    mock_inst.conversation = list(history)
    mock_inst._compression_lock = threading.Lock()
    mock_inst.rebuild_conversation = MagicMock()

    mock_pool = MagicMock()
    mock_pool.get_instance.return_value = mock_inst
    mock_pool.get_logger.return_value._consolidate_markers_in_jsonl.return_value = True
    mock_pool.stopped = False  # explicit: let the gate retry loop run

    with patch('agent_cascade.agent_pool.AgentPool') as MockAP:
        MockAP.find_all_marker_indices.side_effect = lambda h: [
            i for i, m in enumerate(h) if _is_compression_marker(m)
        ]
        with patch('agent_cascade.settings.COMPRESSION_CONSOLIDATION_THRESHOLD', 5), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', enabled), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEW_MAX_RETRIES', retries), \
                patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                      side_effect=inv_con_side_effect) as mc, \
                patch('agent_cascade.compression.agent_invoker.invoke_compaction_reviewer',
                      side_effect=review_side_effect) as mr:
            _consolidate_markers(mock_pool, 'TestAgent')
    return mock_inst, mc, mr


class TestConsolidateMarkersIntegration:

    def test_pass_splices_first_summary_single_consolidation(self):
        mock_inst, mc, mr = _integration_run(
            _always([(True, [])]),
            lambda **k: ('FINAL', ''),
        )
        assert mc.call_count == 1          # no retry on PASS
        assert mr.call_count == 1          # reviewer ran once
        assert mock_inst.rebuild_conversation.called
        new_history = mock_inst.rebuild_conversation.call_args[0][0]
        assert any('FINAL' in (m.content or '') if isinstance(m.content, str) else False
                   for m in new_history)  # the accepted summary was spliced

    def test_exhaustion_calls_consolidation_n_plus_one(self, caplog):
        mock_inst, mc, mr = _integration_run(
            _always([(False, ['x'])]),
            lambda **k: ('BEST', ''),
            retries=2,
        )
        assert mc.call_count == 3  # Phase-2 (1) + retries (2) = N+1, bounded
        assert mr.call_count == 3  # reviewed each candidate once
        assert mock_inst.rebuild_conversation.called  # best-so-far still spliced
        assert any('exhaust' in r.message.lower() for r in caplog.records)

    def test_knob_off_never_invokes_reviewer(self):
        mock_inst, mc, mr = _integration_run(
            _always([(False, ['x'])]),
            lambda **k: ('FINAL', ''),
            enabled=False,
        )
        assert mr.call_count == 0          # reviewer never invoked
        assert mc.call_count == 1          # identical to pre-gate behavior
        assert mock_inst.rebuild_conversation.called


# ──────────────────────────────────────────────────────────────────────────────
# 4. Wiring guarantees (load-bearing, not just gate logic):
#    - the reviewer is offered ZERO tools through the real offered-set resolver
#    - the consolidation retry tail is appended only when preserve_items is passed
# ──────────────────────────────────────────────────────────────────────────────


class TestReviewerToolLess:
    """_configure_reviewer_instance's disabled_tools override must yield an empty offered set."""

    def test_offered_tool_set_is_empty_through_real_resolver(self):
        from agent_cascade.compression.agent_invoker import _configure_reviewer_instance
        from agent_cascade.engine.helpers import order_offered_tool_names
        from agent_cascade.tools.base import TOOL_REGISTRY

        rv = MagicMock()
        _configure_reviewer_instance(rv)
        disabled = set(rv._generate_cfg_override['disabled_tools'])
        assert '__all_mcp_tools__' in disabled  # sentinel present (folded into deferred tools)

        # func_map covering every known tool plus one fake MCP (deferred) tool.
        fake_mcp = 'mcp:zzz_bug0309_test'
        func_map = {name: MagicMock() for name in TOOL_REGISTRY}
        func_map[fake_mcp] = MagicMock()
        is_mcp = patch('agent_cascade.tools.mcp_manager.MCPManager.is_mcp_tool',
                       side_effect=lambda name: name == fake_mcp)

        # Primary guarantee: the reviewer's own state (no discovered tools) offers NOTHING.
        with is_mcp:
            assert order_offered_tool_names(func_map, disabled, instance=None) == []

        # Defense-in-depth: even if a deferred MCP tool were discovered, the sentinel fold blocks
        # it; removing the sentinel re-opens it — proving the sentinel (not just the static union)
        # is load-bearing.
        disc_inst = MagicMock(_discovered_mcp_tools=[fake_mcp], _unloaded_mcp_tools=[])
        with is_mcp:
            assert order_offered_tool_names(func_map, disabled, instance=disc_inst) == []
        with is_mcp:
            offered_without_sentinel = order_offered_tool_names(
                func_map, disabled - {'__all_mcp_tools__'}, instance=disc_inst)
        assert fake_mcp in offered_without_sentinel


class TestPreserveItemsPrompt:
    """invoke_consolidation_agent appends the 'MUST retain verbatim' tail only on retry."""

    @staticmethod
    def _capture_prompt(preserve_items):
        from agent_cascade.compression import agent_invoker as ai

        captured = {}

        def fake_create(**kw):
            captured['task'] = kw['task']
            return MagicMock()

        fake_engine = MagicMock()
        fake_engine._create_system_agent.side_effect = fake_create
        pool = MagicMock()
        pool.get_agent.return_value = MagicMock()  # _ensure_compressor_loaded short-circuits

        with patch.object(ai, '_configure_compressor_instance'), \
                patch.object(ai, '_execute_compressor_and_extract_summary', return_value=('S', '')), \
                patch('agent_cascade.execution_engine.ExecutionEngine', return_value=fake_engine):
            ai.invoke_consolidation_agent(
                agent_pool=pool, marker_summaries=['A', 'B'], caller_name='X',
                preserve_items=preserve_items)
        return captured['task']

    def test_no_preserve_items_has_no_retain_tail(self):
        assert 'MUST retain them verbatim' not in self._capture_prompt(None)

    def test_preserve_items_appends_retain_tail(self):
        prompt = self._capture_prompt(['BUG_0309', 'core.py:300'])
        assert 'MUST retain them verbatim' in prompt
        assert 'BUG_0309' in prompt and 'core.py:300' in prompt

    def test_none_and_empty_are_byte_identical(self):
        # No-retry path must stay byte-identical whether preserve_items is None or empty.
        assert self._capture_prompt(None) == self._capture_prompt([])
