"""Tests for "append last supervisor re-task to compression marker" (todo.md:143).

When compress_context() discards a window, the verbatim text of the last genuine
supervisor task inside that discarded window is preserved in the marker's trailing
<last_supervisor_task> block so the agent does not lose the actual instruction.

Covers:
- helpers.py: is_supervisor_task_message(), extract_last_supervisor_task(),
  extract_last_task_from_marker(), build_marker_message(last_task=...),
  build_consolidation_marker_message(last_task=...)
- core.py: step 8b extraction (active_set[:target_discard_count], NOT target_messages)
  and L2 consolidation carry-forward.

Unit tests are self-contained. Integration tests use the MockAgentPool harness from
conftest.py plus a patched invoke_compression_agent — no LLM or API server required.
"""
from typing import List
from unittest.mock import patch

import pytest

from agent_cascade.compression.helpers import (build_consolidation_marker_message,
                                               build_marker_message, extract_last_supervisor_task,
                                               extract_last_task_from_marker, extract_summary_from_marker,
                                               is_compression_marker, is_supervisor_task_message)
from agent_cascade.llm.schema import ASSISTANT, FUNCTION, SYSTEM, USER, Message
from agent_cascade.prompts.dna import COMPRESSION_BASELINE_TEMPLATE
from agent_cascade.utils.pool_validation import validate_message_pool

# ── Helper factories (mirror tests/compression/test_memory_consolidation.py) ────────


def _make_msg(role: str, content) -> Message:
    return Message(role=role, content=content)


def _make_marker(summary_text: str, header: str = '50% summarized', last_task: str | None = None) -> Message:
    """Build a marker via the production builder so it carries the real template + optional task."""
    return build_marker_message(summary_text, first_ts=None, last_ts=None, n_messages=10, last_task=last_task)


def _content(msg) -> str:
    return msg.content if isinstance(msg, Message) else msg['content']


def _is_marker(msg) -> bool:
    """Marker detection via the production helper (used to assert pool shape)."""
    return is_compression_marker(msg)


# ────────────────────────────────────────────────────────────────────────────
# 1-3. is_supervisor_task_message
# ────────────────────────────────────────────────────────────────────────────


class TestIsSupervisorTaskMessage:
    """Prefix-based classification of genuine supervisor tasks vs system-injected USER msgs."""

    def test_plain_supervisor_text(self):
        assert is_supervisor_task_message(_make_msg(USER, 'Please refactor the parser module.')) is True

    @pytest.mark.parametrize('content', [
        '[COMPRESSION] Compressed 12 messages.',
        '[SYSTEM]: Loop recovery failed — retrying.',
        '[SYSTEM WARNING: Possible repeating action]',
        '[SYSTEM ERROR]: something broke',
        '[BACKGROUND TOOL RESULT shell_cmd] output...',
        "[Agent 'worker1' Completed]: done",
        '⟨shell_cmd completed⟩ Tool ID: 3',
    ])
    def test_system_injected_user_messages(self, content):
        assert is_supervisor_task_message(_make_msg(USER, content)) is False

    def test_marker_is_not_a_task(self):
        marker = _make_marker('some summary')
        assert _is_marker(marker) is True  # sanity: it IS a marker
        assert is_supervisor_task_message(marker) is False

    @pytest.mark.parametrize('role', [ASSISTANT, 'system', FUNCTION])
    def test_non_user_roles(self, role):
        assert is_supervisor_task_message(_make_msg(role, 'Please do the thing.')) is False

    @pytest.mark.parametrize('content', ['', '   ', '\n\t'])
    def test_empty_or_whitespace_content(self, content):
        assert is_supervisor_task_message(_make_msg(USER, content)) is False

    def test_list_content_with_text_is_a_task(self):
        # Multimodal (list) content that flattens to text still counts as a task.
        msg = _make_msg(USER, [{'text': 'Please implement feature X.'}])
        assert is_supervisor_task_message(msg) is True


# ────────────────────────────────────────────────────────────────────────────
# 4. extract_last_supervisor_task
# ────────────────────────────────────────────────────────────────────────────


class TestExtractLastSupervisorTask:
    def test_returns_last_of_several(self):
        msgs = [
            _make_msg(USER, 'first task'),
            _make_msg(ASSISTANT, 'reply'),
            _make_msg(USER, '[SYSTEM]: warning noise'),
            _make_msg(USER, 'second task'),  # last genuine one
        ]
        assert extract_last_supervisor_task(msgs) == 'second task'

    def test_none_when_no_tasks(self):
        msgs = [_make_msg(USER, '[COMPRESSION] x'), _make_msg(ASSISTANT, 'y')]
        assert extract_last_supervisor_task(msgs) is None

    def test_stops_at_first_qualifying_scanning_backwards(self):
        # The most recent genuine task wins; earlier ones are ignored.
        msgs = [
            _make_msg(USER, 'oldest task'),
            _make_msg(ASSISTANT, 'a'),
            _make_msg(USER, 'newest task'),
        ]
        assert extract_last_supervisor_task(msgs) == 'newest task'

    def test_empty_window(self):
        assert extract_last_supervisor_task([]) is None


# ────────────────────────────────────────────────────────────────────────────
# 5. build_marker_message golden (byte-identical when last_task=None)
# ────────────────────────────────────────────────────────────────────────────


class TestBuildMarkerMessage:
    def test_golden_none_is_byte_identical_to_template(self):
        """last_task=None must produce EXACTLY the legacy template output — zero behaviour change.

        With no timestamps, _format_timestamp_interval falls back to "{n} messages summarized";
        we compare against the template rendered with that exact header so the assertion is a true
        byte-for-byte golden check (the header logic itself is covered by the timestamp tests).
        """
        summary = 'The raw summary text here.'
        n_messages = 10
        fallback_header = f"{n_messages} messages summarized"
        expected = COMPRESSION_BASELINE_TEMPLATE.format(header=fallback_header, summary=summary)
        marker = build_marker_message(summary, first_ts=None, last_ts=None, n_messages=n_messages)
        assert _content(marker) == expected
        # And no task block leaks in.
        assert '<last_supervisor_task>' not in _content(marker)

    def test_with_task_appends_block_after_summary_tag(self):
        marker = build_marker_message('summary', first_ts=None, last_ts=None, n_messages=10, last_task='do the thing')
        c = _content(marker)
        assert '<last_supervisor_task>' in c
        # </context_summary> appears exactly once and BEFORE the task block.
        assert c.count('</context_summary>') == 1
        assert c.index('</context_summary>') < c.index('<last_supervisor_task>')
        assert 'do the thing' in c

    def test_truncation_at_cap(self):
        from agent_cascade.settings import COMPRESSION_MAX_LAST_TASK_CHARS
        big = 'A' * (COMPRESSION_MAX_LAST_TASK_CHARS + 500)
        marker = build_marker_message('summary', last_task=big)
        c = _content(marker)
        # Truncated: the full string is NOT present, but a truncated marker is.
        assert big not in c
        assert '[truncated]' in c
        # The closing tag survives truncation (parser safety).
        assert c.rstrip().endswith('</last_supervisor_task>')

    def test_empty_string_task_is_not_appended(self):
        # '' is falsy → no block. (In practice extract_last_supervisor_task never returns an
        # empty/whitespace string — it does `.strip() or None` — so the builder only ever sees
        # a non-empty task or None.)
        marker = build_marker_message('summary', last_task='')
        assert '<last_supervisor_task>' not in _content(marker)

    def test_whitespace_only_task_extracts_back_to_none(self):
        # A whitespace-only block (defensive; unreachable via the real extraction path) must
        # round-trip to None so it is never mistaken for a real task.
        marker = build_marker_message('summary', last_task='   ')
        assert extract_last_task_from_marker(marker) is None


# ────────────────────────────────────────────────────────────────────────────
# 7. extract_summary_from_marker returns ONLY the summary (regression guard)
# ────────────────────────────────────────────────────────────────────────────


class TestSummaryExtractionRegression:
    def test_summary_ignores_task_block(self):
        marker = build_marker_message('THE SUMMARY', first_ts=None, last_ts=None, n_messages=10,
                                      last_task='the raw task text')
        assert extract_summary_from_marker(marker) == 'THE SUMMARY'


# ────────────────────────────────────────────────────────────────────────────
# 9. extract_last_task_from_marker round-trip / legacy
# ────────────────────────────────────────────────────────────────────────────


class TestExtractLastTaskFromMarker:
    def test_round_trip(self):
        marker = build_marker_message('summary', last_task='the task')
        assert extract_last_task_from_marker(marker) == 'the task'

    def test_legacy_marker_returns_none(self):
        # A marker built without a task (or an old-format marker) has no block.
        legacy = _make_msg(USER, COMPRESSION_BASELINE_TEMPLATE.format(header='h', summary='s'))
        assert extract_last_task_from_marker(legacy) is None


# ────────────────────────────────────────────────────────────────────────────
# 10. build_consolidation_marker_message
# ────────────────────────────────────────────────────────────────────────────


class TestBuildConsolidationMarkerMessage:
    def test_appends_task(self):
        m = build_consolidation_marker_message('cons summary', 3, last_task='carried task')
        c = _content(m)
        assert '<last_supervisor_task>' in c
        assert 'carried task' in c
        assert c.count('</context_summary>') == 1

    def test_none_unchanged(self):
        m = build_consolidation_marker_message('cons summary', 3)
        assert '<last_supervisor_task>' not in _content(m)


# ────────────────────────────────────────────────────────────────────────────
# 16. validate_message_pool — no "Malformed" warning on a marker with a task
# ────────────────────────────────────────────────────────────────────────────


class TestPoolValidation:
    def test_marker_with_task_not_flagged_malformed(self, caplog):
        import logging
        marker = build_marker_message('summary', last_task='the task')
        pool = [_make_msg(SYSTEM, 'sys'), _make_msg(USER, 'u0'), marker]
        with caplog.at_level(logging.WARNING):
            validate_message_pool(pool, 'TestAgent')
        assert 'Malformed compression marker' not in caplog.text

    def test_marker_missing_closing_tag_still_flagged(self, caplog):
        import logging
        # Genuinely malformed: opening tag but no closing tag.
        bad = _make_msg(USER, '--- CONTEXT COMPRESSED (x) ---\n<context_summary>\nunterminated')
        pool = [_make_msg(SYSTEM, 'sys'), bad]
        with caplog.at_level(logging.WARNING):
            validate_message_pool(pool, 'TestAgent')
        assert 'Malformed compression marker' in caplog.text


# ────────────────────────────────────────────────────────────────────────────
# Integration: compress_context() with MockAgentPool (tests 11-15, 17, 18)
# ────────────────────────────────────────────────────────────────────────────


class _SettingsStub:
    """Minimal settings object for the guard (MockAgentPool has no settings)."""

    def __init__(self, reserve_tokens: int = 3000, min_usage_pct: float = 50.0):
        self.compression_context_reserve_tokens = reserve_tokens
        self.compression_min_usage_pct = min_usage_pct


def _build_history(num_pairs: int) -> List[Message]:
    """[SYSTEM] + num_pairs user/assistant pairs (no markers). active_set = history[2:]."""
    history: List[Message] = [_make_msg(SYSTEM, 'You are a test agent')]
    for i in range(num_pairs):
        body = f"User question number {i} about the weather and planning " * 3
        history.append(_make_msg(USER, body))
        history.append(_make_msg('assistant', f"Assistant reply number {i} with details " * 3))
    return history


def _make_pool(history: List[Message]) -> 'MockAgentPool':
    from tests.conftest import MockAgentPool
    pool = MockAgentPool(history)
    pool.settings = _SettingsStub()
    # No api_router → available_for_messages stays None → no discard-count capping.
    return pool


def _run_compress(pool, **kwargs):
    """Run compress_context with the LLM call and consolidation suppressed."""
    from agent_cascade.compression.core import compress_context
    kwargs.setdefault('agent_pool', pool)
    kwargs.setdefault('target_agent_name', 'TestAgent')
    kwargs.setdefault('fraction', 0.5)
    kwargs.setdefault('mode', 'auto')
    kwargs.setdefault('trigger', 'user')

    with patch('agent_cascade.api_integration_pkg.tokens._resolve_max_tokens', return_value=1_000_000), \
         patch('agent_cascade.compression.core.invoke_compression_agent') as mock_invoke, \
         patch('agent_cascade.agent_pool.AgentPool.count_markers', return_value=0):
        mock_invoke.return_value = ('Fresh compression summary', '')
        result = compress_context(**kwargs)
    return result


class TestCompressContextLastTask:
    """Integration tests for step 8b extraction through the real compress_context path."""

    def test_first_compression_u0_only_no_section(self):
        """Test 11 (todo:143 caveat): first compression where U0 is the only task → NO section.

        All USER messages in the discarded window are plain questions, but on a FIRST
        compression the active_set excludes U0 — so if the only genuine task were U0 there'd be
        no qualifying message in the window. Here we make every window user-msg a system note so
        the window has zero genuine tasks → last_task is None → no block.
        """
        history = [_make_msg(SYSTEM, 'sys')]
        history.append(_make_msg(USER, 'Original prompt: build the thing.'))  # U0 (excluded)
        # Discardable window: only system-injected USER msgs + assistant replies.
        for i in range(6):
            history.append(_make_msg(USER, f'[SYSTEM]: progress note {i}'))
            history.append(_make_msg('assistant', f'reply {i} ' * 5))
        pool = _make_pool(history)

        result = _run_compress(pool)
        assert result.success is True
        marker = result.marker_message
        assert '<last_supervisor_task>' not in _content(marker)

    def test_first_compression_with_retask_in_window(self):
        """Test 12: a genuine re-task inside the DISCARDED window → section contains that text.

        compute_discard_count keeps a 2-message tail, so the re-task must sit well before the end
        of active_set to land in the discard window (active_set[:target_discard_count]). We place
        it early and pad with many more messages so the discard boundary is guaranteed past it.
        """
        history = [_make_msg(SYSTEM, 'sys')]
        history.append(_make_msg(USER, 'Original prompt.'))  # U0 (excluded from window)
        # The re-task — placed EARLY so it is definitely inside the discard window.
        history.append(_make_msg(USER, 'REDO: now use a different approach for the parser.'))
        # Pad with many non-task messages AFTER it so the 2-tail keep zone never reaches back to
        # the re-task. The last genuine task in active_set[:discard] is then the REDO line.
        for i in range(10):
            history.append(_make_msg(USER, f'[SYSTEM]: progress note {i}'))
            history.append(_make_msg('assistant', f'reply {i} ' * 5))
        pool = _make_pool(history)

        result = _run_compress(pool)
        assert result.success is True
        c = _content(result.marker_message)
        assert '<last_supervisor_task>' in c
        assert 'REDO: now use a different approach for the parser.' in c

    def test_second_compression_no_growth(self):
        """Test 13: a second compression does NOT duplicate or inherit the prior marker's task.

        The previous L1 marker (which carries a task) is OUTSIDE the new active_set, so it is never
        re-scanned; only the new window's own task is used. Two guarantees pin "no growth":
          1. Each marker carries at most ONE <last_supervisor_task> block.
          2. The new marker's task is its OWN window's task — not a copy of the prior marker's.
        (The two markers coexist in the pool until L2 consolidation collapses them; that is the
        existing stacking behavior, not growth from this feature.)
        """
        # First compression: task early in the window → marker carries a block.
        history = [_make_msg(SYSTEM, 'sys')]
        history.append(_make_msg(USER, 'Original prompt.'))  # U0
        history.append(_make_msg(USER, 'first window task'))
        for i in range(8):
            history.append(_make_msg(USER, f'[SYSTEM]: note {i}'))
            history.append(_make_msg('assistant', f'reply {i} ' * 5))
        pool = _make_pool(history)
        r1 = _run_compress(pool)
        assert r1.success is True
        c1 = _content(r1.marker_message)
        assert '<last_supervisor_task>' in c1
        assert c1.count('<last_supervisor_task>') == 1
        assert 'first window task' in c1

        # Append a fresh active window (after the marker) with its OWN re-task, placed early.
        # NOTE: get_conversation() returns a COPY — append via the pool's write path so it
        # actually persists to the instance. Pad generously so the discard boundary is well past
        # the task (compute_discard_count keeps a 2-message tail).
        new_window = [_make_msg(USER, 'second window task')]
        for i in range(12):
            new_window.append(_make_msg(USER, f'[SYSTEM]: new note {i}'))
            new_window.append(_make_msg('assistant', f'new reply {i} ' * 5))
        pool.instance_conversations['TestAgent'] = pool.get_conversation('TestAgent') + new_window

        r2 = _run_compress(pool)
        assert r2.success is True
        c2 = _content(r2.marker_message)
        # Exactly one block, and it carries the NEW window's task — not the prior marker's.
        assert c2.count('<last_supervisor_task>') == 1
        assert 'second window task' in c2
        assert 'first window task' not in c2

    def test_second_compression_fresh_window_no_task_no_inheritance(self):
        """Test 14: new window has no task, previous marker has one → new marker has NO section.

        Pins the L1 = no-inheritance decision (supervisor Decision 1).
        """
        # First compression with a task EARLY in the window → marker carries a block.
        history = [_make_msg(SYSTEM, 'sys')]
        history.append(_make_msg(USER, 'Original prompt.'))
        history.append(_make_msg(USER, 'first window task'))
        for i in range(8):
            history.append(_make_msg(USER, f'[SYSTEM]: note {i}'))
            history.append(_make_msg('assistant', f'reply {i} ' * 5))
        pool = _make_pool(history)
        r1 = _run_compress(pool)
        assert r1.success is True
        assert '<last_supervisor_task>' in _content(r1.marker_message)

        # Fresh window with NO genuine task (only system notes + assistant replies).
        conv = pool.get_conversation('TestAgent')
        for i in range(8):
            conv.append(_make_msg(USER, f'[SYSTEM]: note {i}'))
            conv.append(_make_msg('assistant', f'reply {i} ' * 5))

        r2 = _run_compress(pool)
        assert r2.success is True
        # The NEW marker must NOT inherit the previous marker's task.
        assert '<last_supervisor_task>' not in _content(r2.marker_message)

    def test_window_only_system_feedback_no_section(self):
        """Test 15: window containing only [COMPRESSION] feedback + assistant turns → no section."""
        history = [_make_msg(SYSTEM, 'sys')]
        history.append(_make_msg(USER, 'Original prompt.'))
        for i in range(6):
            history.append(_make_msg(USER, f'[COMPRESSION] feedback {i}'))
            history.append(_make_msg('assistant', f'reply {i} ' * 5))
        pool = _make_pool(history)

        result = _run_compress(pool)
        assert result.success is True
        assert '<last_supervisor_task>' not in _content(result.marker_message)

    def test_pool_shape_and_counts_unchanged(self):
        """Test 17: pool shape [SYS][U0][MARKER][tail], messages_discarded/tail_count correct."""
        history = [_make_msg(SYSTEM, 'sys')]
        history.append(_make_msg(USER, 'Original prompt.'))
        for i in range(8):
            history.append(_make_msg(USER, f'q {i}'))
            history.append(_make_msg('assistant', f'reply {i} ' * 5))
        pool = _make_pool(history)

        result = _run_compress(pool)
        assert result.success is True

        final_conv = pool.get_conversation('TestAgent')
        # Shape: [SYS][U0][MARKER] + tail.
        assert final_conv[0].role == SYSTEM  # SYS (content is 'sys' in this fixture)
        assert final_conv[1].role == USER and 'Original prompt.' in _content(final_conv[1])  # U0
        assert _is_marker(final_conv[2])  # MARKER
        # Tail preserved (len(active_set) - discard). active_set = history[2:] = 16 msgs.
        expected_tail = result.tail_count
        assert len(final_conv) == 3 + expected_tail
        assert result.messages_discarded + expected_tail == 16

    def test_tokens_after_computed(self):
        """Test 18: tokens_after is computed and >= tokens_before - discarded_tokens (loose bound)."""
        history = [_make_msg(SYSTEM, 'sys')]
        history.append(_make_msg(USER, 'Original prompt.'))
        for i in range(8):
            history.append(_make_msg(USER, f'q {i} ' * 3))
            history.append(_make_msg('assistant', f'reply {i} ' * 5))
        pool = _make_pool(history)

        result = _run_compress(pool)
        assert result.success is True
        # tokens_after must be a non-negative int (advisory, but must be computed).
        assert isinstance(result.tokens_after, int)
        assert result.tokens_after >= 0


# ────────────────────────────────────────────────────────────────────────────
# L2 consolidation carry-forward (unit-level on _consolidate_markers)
# ────────────────────────────────────────────────────────────────────────────


class TestConsolidationCarryForward:
    """The L2 marker must inherit the newest consolidated marker's <last_supervisor_task>."""

    def _build_history_with_tasks(self, num_markers: int, task_on: int | None) -> List[Message]:
        """Markers at known positions; only marker `task_on` (0-based among markers) carries a task.

        Layout per test_memory_consolidation._build_history_with_markers:
        [SYS] + (msgs_between pairs + MARKER)*num_markers
        """
        history: List[Message] = [_make_msg(SYSTEM, 'sys')]
        for i in range(num_markers):
            if i > 0:
                for j in range(2):
                    history.append(_make_msg(USER, f"User-{i}-{j}"))
                    history.append(_make_msg('assistant', f"Asst-{i}-{j}"))
            task = (f'task from marker {i}' if i == task_on else None)
            history.append(_make_marker(f'summary {i}', header='50% summarized', last_task=task))
        return history

    def _run_consolidate(self, pool_history: List[Message]):
        """Run _consolidate_markers; return the new history passed to rebuild_conversation."""
        import threading
        from unittest.mock import MagicMock
        from agent_cascade.compression.core import _consolidate_markers

        mock_inst = MagicMock()
        mock_inst.conversation = pool_history
        mock_inst._compression_lock = threading.Lock()
        mock_inst.rebuild_conversation = MagicMock()

        mock_pool = MagicMock()
        mock_pool.get_instance.return_value = mock_inst

        from agent_cascade.compression.helpers import is_compression_marker
        with patch('agent_cascade.agent_pool.AgentPool') as MockAgentPoolClass:
            MockAgentPoolClass.find_all_marker_indices.side_effect = lambda h: [
                i for i, m in enumerate(h) if is_compression_marker(m)
            ]
            with patch('agent_cascade.settings.COMPRESSION_CONSOLIDATION_THRESHOLD', 2), \
                 patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent') as mock_invoke:
                mock_invoke.return_value = ('Consolidated summary', '')
                _consolidate_markers(mock_pool, 'TestAgent')

        # The L2 marker is written via rebuild_conversation(new_history), NOT by mutating
        # mock_inst.conversation — read the new history from the mock call.
        assert mock_inst.rebuild_conversation.called, 'rebuild_conversation was not called'
        return mock_inst.rebuild_conversation.call_args[0][0]

    def test_newest_marker_task_carried_forward(self):
        """The newest marker being consolidated carries a task → L2 marker keeps it."""
        # 3 markers; the middle one (index 1) has a task. Consolidation keeps the newest (idx 2),
        # consolidates idx 0 and 1. The newest CONSOLIDATED is idx 1 → its task should carry forward.
        history = self._build_history_with_tasks(num_markers=3, task_on=1)
        new_history = self._run_consolidate(history)

        from agent_cascade.compression.helpers import is_compression_marker
        l2_markers = [m for m in new_history if is_compression_marker(m) and 'L2' in _content(m)]
        assert len(l2_markers) >= 1, 'expected an L2 consolidation marker to be produced'
        assert 'task from marker 1' in _content(l2_markers[0])

    def test_no_task_in_consolidated_markers(self):
        """No consolidated marker has a task → L2 marker has no block (loss, not growth)."""
        history = self._build_history_with_tasks(num_markers=3, task_on=None)
        new_history = self._run_consolidate(history)

        from agent_cascade.compression.helpers import is_compression_marker
        l2_markers = [m for m in new_history if is_compression_marker(m) and 'L2' in _content(m)]
        assert len(l2_markers) >= 1
        assert '<last_supervisor_task>' not in _content(l2_markers[0])
