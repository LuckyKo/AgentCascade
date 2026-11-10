"""L2 consolidation time-range correctness across multi-day sessions (todo.md:110).

Regression focus: a compression marker's ``.ts`` is its CREATION time — the END of the
window it covers. The pre-fix L2 path preferred ``.ts`` over the marker's stored range,
so every consolidation hop collapsed a multi-day span down to the first marker's
completion instant, and a second hop collapsed it again.

The contract pinned here:
  - markers store their covered window in ``summary_start_ts``/``summary_end_ts``
    (excluded from dumps, so never sent to a provider);
  - ``get_marker_time_range()`` prefers those fields, then the header interval (JSONL
    reload fallback), and only uses ``.ts`` as a degenerate point when no range exists;
  - the L2 header + stored fields span the FIRST consolidated marker's start → the LAST
    consolidated marker's end, across multiple days and multiple hops;
  - the consolidation dataset annotates every summary with its start timestamp.

All tests are self-contained (no LLM / API server).
"""

import threading
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.compression.agent_invoker import _format_summaries_with_spans
from agent_cascade.compression.helpers import (build_consolidation_marker_message, build_marker_message,
                                               get_marker_time_range)
from agent_cascade.llm.schema import SYSTEM, USER, Message
from agent_cascade.prompts.dna import COMPRESSION_MARKER

DAY = 86400.0
BASE = 1757000000.0


def _make_msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)


def _is_marker(msg) -> bool:
    role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
    content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
    return (role == USER and isinstance(content, str)
            and content.startswith(COMPRESSION_MARKER) and '<context_summary>' in content)


def _l1(i: int, start_off: float = 0.0, span: float = 7200.0) -> Message:
    """L1 marker covering [BASE + i*DAY + start_off, +span], stamped like the live path."""
    first = BASE + i * DAY + start_off
    last = first + span
    marker = build_marker_message(f'Summary {i}', first_ts=first, last_ts=last, n_messages=3)
    # Live compression stamps creation time at the END of the window (core.py step 9).
    marker.ts = last + 60.0
    return marker


def _build_history(num_markers: int, prefix: List[Message] | None = None) -> List[Message]:
    """[SYS] + optional prefix markers + N L1 markers (each followed by raw messages)."""
    history: List[Message] = [_make_msg(SYSTEM, 'System')]
    history.extend(prefix or [])
    for i in range(num_markers):
        history.append(_l1(i))
        history.append(_make_msg(USER, f'User-{i}'))
        history.append(_make_msg('assistant', f'Asst-{i}'))
    return history


def _run_consolidation(history: List[Message]):
    """Run the real _consolidate_markers against a mocked pool; return (new_history, invoke)."""
    from agent_cascade.compression.core import _consolidate_markers

    mock_inst = MagicMock()
    mock_inst.conversation = list(history)
    mock_inst._compression_lock = threading.Lock()
    mock_inst.rebuild_conversation = MagicMock()

    mock_pool = MagicMock()
    mock_pool.get_instance.return_value = mock_inst
    mock_pool.get_logger.return_value._consolidate_markers_in_jsonl.return_value = True

    with patch('agent_cascade.agent_pool.AgentPool') as MockAgentPoolClass:
        MockAgentPoolClass.find_all_marker_indices.side_effect = lambda h: [
            i for i, m in enumerate(h) if _is_marker(m)
        ]
        with patch('agent_cascade.settings.COMPRESSION_CONSOLIDATION_THRESHOLD', 5):
            with patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent') as mock_invoke:
                mock_invoke.return_value = ('Consolidated', '')
                _consolidate_markers(mock_pool, 'TestAgent')

    assert mock_inst.rebuild_conversation.called
    return mock_inst.rebuild_conversation.call_args[0][0], mock_invoke


def _l2_markers(history) -> List[Message]:
    return [m for m in history if isinstance(m, Message) and str(m.content).startswith(f'{COMPRESSION_MARKER} (L2,')]


# ── get_marker_time_range precedence ────────────────────────────────────────


class TestMarkerTimeRange:

    def test_prefers_explicit_span_fields(self):
        marker = _l1(0)
        assert get_marker_time_range(marker) == (BASE, BASE + 7200.0)

    def test_header_fallback_when_fields_missing(self):
        # Simulate a JSONL reload: explicit fields are excluded from dumps, header survives.
        marker = _l1(0)
        marker.summary_start_ts = None
        marker.summary_end_ts = None
        start, end = get_marker_time_range(marker)
        assert abs(start - BASE) < 60
        assert abs(end - (BASE + 7200.0)) < 60

    def test_ts_is_never_used_as_start_when_a_range_exists(self):
        marker = _l1(0)
        # .ts (creation/end) is ~2h after the covered start — must not leak into the start.
        assert marker.ts > BASE + 7200.0
        assert get_marker_time_range(marker)[0] == BASE

    def test_ts_is_point_fallback_for_legacy_marker_without_range(self):
        legacy = _make_msg(USER, f'{COMPRESSION_MARKER} (50% summarized) ---\n'
                                 '<context_summary>old</context_summary>')
        legacy.ts = BASE + 123.0
        assert get_marker_time_range(legacy) == (BASE + 123.0, BASE + 123.0)

    def test_no_timestamp_info_returns_nones(self):
        marker = _make_msg(USER, f'{COMPRESSION_MARKER} (50% summarized) ---\n'
                                 '<context_summary>old</context_summary>')
        assert get_marker_time_range(marker) == (None, None)

    def test_builders_stamp_span_but_never_dump_it(self):
        l1 = build_marker_message('s', first_ts=BASE, last_ts=BASE + DAY, n_messages=2)
        l2 = build_consolidation_marker_message('s', 3, first_ts=BASE, last_ts=BASE + DAY)
        for marker in (l1, l2):
            assert marker.summary_start_ts == BASE
            assert marker.summary_end_ts == BASE + DAY
            dumped = marker.model_dump()
            assert 'summary_start_ts' not in dumped
            assert 'summary_end_ts' not in dumped
            assert 'ts' not in dumped


# ── Consolidation dataset annotations ───────────────────────────────────────


class TestConsolidationDatasetLabels:

    def test_each_summary_is_labelled_with_its_start(self):
        text = _format_summaries_with_spans(
            ['a', 'b'],
            [(BASE, BASE + 7200.0), (BASE + DAY, BASE + DAY + 7200.0)],
        )
        from agent_cascade.compression.helpers import format_marker_timestamp
        assert f'SUMMARY 1 (starts {format_marker_timestamp(BASE)}):\na' in text
        assert f'SUMMARY 2 (starts {format_marker_timestamp(BASE + DAY)}):\nb' in text

    def test_missing_or_partial_ranges_fall_back_to_plain_labels(self):
        text = _format_summaries_with_spans(['a', 'b', 'c'], [None, (None, None)])
        assert 'SUMMARY 1:\na' in text
        assert 'SUMMARY 2:\nb' in text
        assert 'SUMMARY 3:\nc' in text

    def test_none_ranges_keeps_legacy_format(self):
        assert _format_summaries_with_spans(['only']) == 'SUMMARY 1:\nonly'


# ── End-to-end L2 span across days and hops ─────────────────────────────────


class TestL2MultiDaySpan:

    def test_live_creation_ts_does_not_collapse_multiday_span(self):
        new_history, _ = _run_consolidation(_build_history(8))

        l2 = _l2_markers(new_history)
        assert len(l2) == 1
        marker = l2[0]

        # Markers 0..6 consolidated: first start = day 0, last end = day 6 + 2h.
        assert marker.summary_start_ts == BASE
        assert marker.summary_end_ts == BASE + 6 * DAY + 7200.0
        # Header carries the same multi-day span (both dates rendered).
        from agent_cascade.compression.helpers import _parse_marker_timestamps, format_marker_timestamp
        parsed_start, parsed_end = _parse_marker_timestamps(marker)
        assert format_marker_timestamp(BASE) in str(marker.content)
        assert format_marker_timestamp(BASE + 6 * DAY + 7200.0) in str(marker.content)
        assert abs(parsed_start - BASE) < 60
        assert abs(parsed_end - (BASE + 6 * DAY + 7200.0)) < 60

    def test_consolidation_input_receives_per_summary_ranges(self):
        _, invoke = _run_consolidation(_build_history(8))
        kwargs = invoke.call_args.kwargs
        assert kwargs['marker_time_ranges'] == [
            (BASE + i * DAY, BASE + i * DAY + 7200.0) for i in range(7)
        ]

    def test_second_hop_keeps_the_original_start_and_first_prompt_day(self):
        # Hop 1: 8 L1s → L2 covering days 0..6, keeping L1 day 7.
        new_history, _ = _run_consolidation(_build_history(8))
        l2 = _l2_markers(new_history)[0]
        assert l2.summary_start_ts == BASE

        # Hop 2: MORE days arrive after the L2. Add 6 fresh L1s so marker count is 8 again
        # (L2 + kept L1 day 7 + days 8..13). The first consolidated marker is now the L2 itself.
        extra = []
        for i in range(8, 14):
            extra.append(_l1(i))
            extra.append(_make_msg(USER, f'User-{i}'))
            extra.append(_make_msg('assistant', f'Asst-{i}'))
        hop2_history = list(new_history) + extra

        new_history2, invoke2 = _run_consolidation(hop2_history)
        l2_2 = _l2_markers(new_history2)
        assert len(l2_2) == 1
        marker2 = l2_2[0]

        # The pre-fix bug re-anchored the start at the first L1's creation time (day 7) —
        # the L2's covered start (day 0) must survive the hop.
        assert marker2.summary_start_ts == BASE
        # Consolidated set = L2 (days 0..6) + L1s 7..12 → end = day 12 + 2h.
        assert marker2.summary_end_ts == BASE + 12 * DAY + 7200.0
        # And the Compressor's first input summary is still anchored at day 0, not day 7.
        assert invoke2.call_args.kwargs['marker_time_ranges'][0] == (BASE, BASE + 6 * DAY + 7200.0)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
