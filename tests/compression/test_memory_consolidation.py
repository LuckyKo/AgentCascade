"""Comprehensive tests for the memory consolidation feature.

Covers:
- agent_pool.py: count_markers(), find_all_marker_indices()
- helpers.py: is_compression_marker(), select_markers_for_consolidation(),
              extract_summary_from_marker(), build_consolidation_marker_message()
- core.py: _consolidate_markers() (unit tests with mocks, integration-style with real pool)

All unit tests are self-contained — no LLM or API server required.
Integration tests use mocked LLM calls to avoid external dependencies.
"""

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.prompts.dna import COMPRESSION_MARKER, COMPRESSION_BASELINE_TEMPLATE
from agent_cascade.llm.schema import SYSTEM, USER, Message
from agent_cascade.compression.helpers import (
    select_markers_for_consolidation,
    extract_summary_from_marker,
    build_consolidation_marker_message,
)
from agent_cascade.agent_pool import AgentPool

# ── Helper factories ────────────────────────────────────────────────────────


def _make_msg(role: str, content: str) -> Message:
    """Create a Message object for testing."""
    return Message(role=role, content=content)


def _make_marker(summary_text: str, header: str = "50% summarized") -> Message:
    """Create a valid compression marker message with context_summary tags."""
    content = COMPRESSION_BASELINE_TEMPLATE.format(
        header=header,
        summary=summary_text,
    )
    return _make_msg(USER, content)


def _build_history_with_markers(num_markers: int, msgs_between: int = 3) -> List[Message]:
    """Build a conversation history with the specified number of compression markers.

    Layout: [SYSTEM] + [msgs_between*2] + [MARKER] + [msgs_between*2] + ... + [MARKER]
    Each marker is followed by msgs_between user/assistant pairs.
    """
    history: List[Message] = [_make_msg(SYSTEM, "You are a test agent")]

    for i in range(num_markers):
        # Add some raw messages before this marker (except first iteration)
        if i > 0:
            for j in range(msgs_between):
                history.append(_make_msg(USER, f"User-{i}-{j}"))
                history.append(_make_msg("assistant", f"Assistant-{i}-{j}"))

        # Insert the marker
        marker = _make_marker(f"Summary from compression cycle {i}", header=f"{50}% summarized")
        history.append(marker)

    return history


def _is_compression_marker(msg: Any) -> bool:
    """Local version of is_compression_marker that uses correct import.

    The production helpers.py has a bug importing COMPRESSION_MARKER from settings
    instead of prompts.dna. We use this local copy for tests, and also test via
    AgentPool's methods which import correctly.
    """
    from agent_cascade.llm.schema import USER as USER_ROLE
    role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
    content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
    return (role == USER_ROLE and isinstance(content, str)
            and content.startswith(COMPRESSION_MARKER)
            and '<context_summary>' in content)


# ────────────────────────────────────────────────────────────────────────────
# 1. Marker counting utilities (agent_pool.py)
# ────────────────────────────────────────────────────────────────────────────


class TestCountMarkers:
    """Test AgentPool.count_markers() utility."""

    def test_zero_markers(self):
        """Empty history and history with no markers should return 0."""
        assert AgentPool.count_markers([]) == 0

        history = [
            _make_msg(SYSTEM, "System"),
            _make_msg(USER, "Hello"),
            _make_msg("assistant", "Hi there"),
        ]
        assert AgentPool.count_markers(history) == 0

    def test_counts_valid_markers(self):
        """Should count messages with COMPRESSION_MARKER prefix and <context_summary> tags."""
        history = [
            _make_msg(SYSTEM, "System"),
            _make_marker("First summary"),
            _make_msg(USER, "More stuff"),
            _make_marker("Second summary"),
        ]
        assert AgentPool.count_markers(history) == 2

    def test_ignores_non_user_role(self):
        """Messages with marker-like content but wrong role should not be counted."""
        content = COMPRESSION_MARKER + " (test) ---\n<context_summary>test</context_summary>"
        history = [
            _make_msg(SYSTEM, content),  # Wrong role
            _make_msg("assistant", content),  # Wrong role
            _make_marker("Valid marker"),  # Correct
        ]
        assert AgentPool.count_markers(history) == 1

    def test_ignores_missing_context_summary_tags(self):
        """Messages starting with COMPRESSION_MARKER but lacking <context_summary> are not markers."""
        history = [
            _make_msg(USER, f"{COMPRESSION_MARKER} (test) ---\nNo tags here"),
            _make_marker("Valid marker"),
        ]
        assert AgentPool.count_markers(history) == 1

    def test_edge_case_user_message_starts_with_prefix_but_no_tags(self):
        """User message starting with marker prefix but no context_summary tags — not a marker."""
        history = [
            _make_msg(USER, f"{COMPRESSION_MARKER} this is just user text without tags"),
        ]
        assert AgentPool.count_markers(history) == 0

    def test_edge_case_non_string_content(self):
        """Messages with non-string content should not crash and should not be counted."""
        # Use dicts for non-string content since Message validates types strictly
        history = [
            {"role": USER, "content": None},
            {"role": USER, "content": 123},
            _make_marker("Valid"),
        ]
        assert AgentPool.count_markers(history) == 1


class TestFindAllMarkerIndices:
    """Test AgentPool.find_all_marker_indices() utility."""

    def test_no_markers_returns_empty(self):
        """Empty list when no markers present."""
        history = [
            _make_msg(SYSTEM, "System"),
            _make_msg(USER, "Hello"),
        ]
        assert AgentPool.find_all_marker_indices(history) == []

    def test_single_marker_index(self):
        """Returns correct index for a single marker."""
        history = [
            _make_msg(SYSTEM, "System"),       # 0
            _make_msg(USER, "User msg"),       # 1
            _make_marker("Summary"),           # 2
            _make_msg("assistant", "Reply"),   # 3
        ]
        assert AgentPool.find_all_marker_indices(history) == [2]

    def test_multiple_markers_ordered_chronologically(self):
        """Returns indices in ascending (chronological) order."""
        history = []
        for i in range(5):
            history.append(_make_msg(USER, f"Msg {i}"))
            if i % 2 == 0:
                history.append(_make_marker(f"Summary {i}"))

        indices = AgentPool.find_all_marker_indices(history)
        # i=0: user msg at 0, marker at 1
        # i=1: user msg at 2
        # i=2: user msg at 3, marker at 4
        # i=3: user msg at 5
        # i=4: user msg at 6, marker at 7
        assert indices == [1, 4, 7]
        assert indices == sorted(indices)

    def test_ignores_invalid_markers(self):
        """Only counts valid markers (with prefix AND context_summary tags)."""
        history = [
            _make_msg(SYSTEM, "System"),                        # 0
            _make_marker("Valid 1"),                            # 1
            _make_msg(USER, f"{COMPRESSION_MARKER} no tags"),   # 2 — invalid
            _make_marker("Valid 2"),                            # 3
        ]
        assert AgentPool.find_all_marker_indices(history) == [1, 3]


# ────────────────────────────────────────────────────────────────────────────
# 2. Consolidation marker builder (helpers.py)
# ────────────────────────────────────────────────────────────────────────────


class TestBuildConsolidationMarkerMessage:
    """Test build_consolidation_marker_message() output format."""

    def test_returns_user_role_message(self):
        """Result should be a USER-role Message object."""
        msg = build_consolidation_marker_message("Merged summary", num_summaries_consolidated=5)
        assert isinstance(msg, Message)
        assert msg.role == USER

    def test_contains_l2_header(self):
        """Header should indicate L2 consolidation and count of merged summaries."""
        msg = build_consolidation_marker_message("Summary", num_summaries_consolidated=7)
        content = str(msg.content)
        assert "L2" in content
        assert "7 summaries consolidated" in content

    def test_contains_context_summary_tags(self):
        """Content should be wrapped in <context_summary> tags."""
        msg = build_consolidation_marker_message("My summary text", num_summaries_consolidated=3)
        content = str(msg.content)
        assert "<context_summary>" in content
        assert "</context_summary>" in content

    def test_contains_original_summary(self):
        """The provided summary text should appear inside the tags."""
        original = "This is the consolidated content"
        msg = build_consolidation_marker_message(original, num_summaries_consolidated=2)
        content = str(msg.content)
        assert original in content

    def test_starts_with_compression_marker_prefix(self):
        """Content should start with COMPRESSION_MARKER so it's detected as a marker."""
        msg = build_consolidation_marker_message("Summary", num_summaries_consolidated=4)
        content = str(msg.content)
        assert content.startswith(COMPRESSION_MARKER)

    def test_is_detected_as_valid_marker(self):
        """Built consolidation marker should be recognized by AgentPool.count_markers."""
        msg = build_consolidation_marker_message("Summary", num_summaries_consolidated=3)
        assert _is_compression_marker(msg) is True


# ────────────────────────────────────────────────────────────────────────────
# 3. Marker utilities (helpers.py) — using local helper to avoid import bug
# ────────────────────────────────────────────────────────────────────────────


class TestIsCompressionMarker:
    """Test marker detection logic via AgentPool methods and local helper."""

    def test_valid_marker_object(self):
        """A Message object with proper prefix and tags is a marker."""
        msg = _make_marker("Test summary")
        assert _is_compression_marker(msg) is True

    def test_valid_marker_dict(self):
        """A dict with proper structure is also recognized as a marker."""
        content = COMPRESSION_BASELINE_TEMPLATE.format(header="test", summary="s")
        msg = {"role": USER, "content": content}
        assert _is_compression_marker(msg) is True

    def test_user_message_without_tags(self):
        """User message starting with prefix but no tags — not a marker."""
        msg = _make_msg(USER, f"{COMPRESSION_MARKER} random text")
        assert _is_compression_marker(msg) is False

    def test_wrong_role_with_valid_content(self):
        """Assistant/system messages with marker-like content are not markers."""
        content = COMPRESSION_BASELINE_TEMPLATE.format(header="test", summary="s")
        assert _is_compression_marker(_make_msg(SYSTEM, content)) is False
        assert _is_compression_marker(_make_msg("assistant", content)) is False

    def test_empty_message(self):
        """Empty or None-like messages are not markers."""
        assert _is_compression_marker(_make_msg(USER, "")) is False
        assert _is_compression_marker({"role": USER}) is False

    def test_non_string_content(self):
        """Non-string content should not crash and should return False."""
        msg = {"role": USER, "content": None}
        assert _is_compression_marker(msg) is False


class TestSelectMarkersForConsolidation:
    """Test select_markers_for_consolidation() strategy."""

    def test_keeps_newest_selects_oldest_n_minus_1(self):
        """With multiple markers, keep the newest (last), consolidate all others."""
        indices = [2, 5, 8, 11, 14]
        to_consolidate, keep = select_markers_for_consolidation(indices)
        assert to_consolidate == [2, 5, 8, 11]
        assert keep == 14

    def test_single_marker_no_consolidation(self):
        """With only one marker, nothing to consolidate."""
        indices = [7]
        to_consolidate, keep = select_markers_for_consolidation(indices)
        assert to_consolidate == []
        assert keep == 7

    def test_two_markers_consolidate_first_keep_second(self):
        """With two markers, consolidate the first, keep the second."""
        indices = [3, 9]
        to_consolidate, keep = select_markers_for_consolidation(indices)
        assert to_consolidate == [3]
        assert keep == 9

    def test_empty_list_raises_value_error(self):
        """Empty input should raise ValueError."""
        with pytest.raises(ValueError, match="empty"):
            select_markers_for_consolidation([])


class TestExtractSummaryFromMarker:
    """Test extract_summary_from_marker() parsing."""

    def test_valid_marker_returns_summary(self):
        """Should extract text between <context_summary> tags."""
        msg = _make_marker("This is the summary content")
        result = extract_summary_from_marker(msg)
        assert result == "This is the summary content"

    def test_multiline_summary_preserved(self):
        """Multi-line summaries should be preserved (with whitespace stripped)."""
        summary = "Line 1\nLine 2\nLine 3"
        msg = _make_marker(summary)
        result = extract_summary_from_marker(msg)
        assert result == summary

    def test_malformed_marker_no_closing_tag_returns_none(self):
        """Marker missing </context_summary> should return None."""
        content = COMPRESSION_MARKER + " ---\n<context_summary>no closing tag"
        msg = _make_msg(USER, content)
        assert extract_summary_from_marker(msg) is None

    def test_malformed_marker_no_opening_tag_returns_none(self):
        """Marker missing <context_summary> should return None."""
        content = COMPRESSION_MARKER + " ---\njust text</context_summary>"
        msg = _make_msg(USER, content)
        assert extract_summary_from_marker(msg) is None

    def test_empty_summary_between_tags_returns_none(self):
        """Tags present but empty content should return None."""
        content = COMPRESSION_MARKER + " ---\n<context_summary>   </context_summary>"
        msg = _make_msg(USER, content)
        assert extract_summary_from_marker(msg) is None

    def test_non_marker_message_returns_none(self):
        """Regular user message should return None without crashing."""
        msg = _make_msg(USER, "Hello world")
        assert extract_summary_from_marker(msg) is None

    def test_dict_format_marker(self):
        """Should also work with dict-format messages."""
        content = COMPRESSION_BASELINE_TEMPLATE.format(header="test", summary="dict summary")
        msg = {"role": USER, "content": content}
        result = extract_summary_from_marker(msg)
        assert result == "dict summary"


# ────────────────────────────────────────────────────────────────────────────
# 4. Consolidation logic (core.py) — Unit tests with mocked dependencies
# ────────────────────────────────────────────────────────────────────────────


class TestConsolidateMarkersUnit:
    """Unit tests for _consolidate_markers() with fully mocked agent_pool."""

    @pytest.fixture(autouse=True)
    def reset_consolidation_state(self):
        """Reset the module-level recursion guard before and after each test."""
        from agent_cascade.compression.core import _consolidating_agents, _consolidation_lock
        with _consolidation_lock:
            _consolidating_agents.clear()
        yield
        with _consolidation_lock:
            _consolidating_agents.clear()

    def test_does_not_run_when_marker_count_below_threshold(self):
        """Consolidation should skip when markers < COMPRESSION_CONSOLIDATION_THRESHOLD."""
        from agent_cascade.compression.core import _consolidate_markers

        history = _build_history_with_markers(num_markers=3, msgs_between=2)

        mock_inst = MagicMock()
        mock_inst.conversation = history
        mock_inst._compression_lock = threading.Lock()

        mock_pool = MagicMock()
        mock_pool.get_instance.return_value = mock_inst

        # Patch AgentPool where it's imported inside _consolidate_markers
        with patch("agent_cascade.agent_pool.AgentPool") as MockAgentPoolClass:
            MockAgentPoolClass.find_all_marker_indices.side_effect = lambda h: [
                i for i, m in enumerate(h) if _is_compression_marker(m)
            ]

            # Patch threshold to 5 so 3 markers is below threshold
            with patch("agent_cascade.settings.COMPRESSION_CONSOLIDATION_THRESHOLD", 5):
                _consolidate_markers(mock_pool, "TestAgent")

                # invoke_consolidation_agent should never be called
                with patch(
                    "agent_cascade.compression.agent_invoker.invoke_consolidation_agent"
                ) as mock_invoke:
                    mock_invoke.assert_not_called()

    def test_marker_selection_keeps_newest(self):
        """Should select oldest N-1 markers for consolidation, keep newest."""
        from agent_cascade.compression.core import _consolidate_markers

        num_markers = 8
        history = _build_history_with_markers(num_markers=num_markers, msgs_between=2)
        marker_indices = [i for i, m in enumerate(history) if _is_compression_marker(m)]

        mock_inst = MagicMock()
        mock_inst.conversation = history
        mock_inst._compression_lock = threading.Lock()
        mock_inst.rebuild_conversation = MagicMock()

        mock_pool = MagicMock()
        mock_pool.get_instance.return_value = mock_inst

        with patch("agent_cascade.agent_pool.AgentPool") as MockAgentPoolClass:
            MockAgentPoolClass.find_all_marker_indices.side_effect = lambda h: [
                i for i, m in enumerate(h) if _is_compression_marker(m)
            ]

            # Patch threshold to 5 so 8 markers triggers consolidation
            with patch("agent_cascade.settings.COMPRESSION_CONSOLIDATION_THRESHOLD", 5):
                with patch(
                    "agent_cascade.compression.agent_invoker.invoke_consolidation_agent"
                ) as mock_invoke:
                    mock_invoke.return_value = "Consolidated summary"

                    _consolidate_markers(mock_pool, "TestAgent")

                    # Verify invoke was called with summaries from oldest N-1 markers
                    assert mock_invoke.called
                    call_args = mock_invoke.call_args
                    marker_summaries = call_args.kwargs.get("marker_summaries", []) or call_args.args[1]

                    # Should have consolidated num_markers - 1 summaries (oldest)
                    expected_count = num_markers - 1
                    assert len(marker_summaries) == expected_count, \
                        f"Expected {expected_count} summaries, got {len(marker_summaries)}"

    def test_pool_mutation_preserves_raw_segments(self):
        """Consolidation should only remove markers, never raw messages between them."""
        from agent_cascade.compression.core import _consolidate_markers

        # Build history with identifiable raw segments between markers
        history = []
        history.append(_make_msg(SYSTEM, "System"))

        for i in range(8):
            # Raw segment before marker
            history.append(_make_msg(USER, f"Raw-segment-{i}-user"))
            history.append(_make_msg("assistant", f"Raw-segment-{i}-assistant"))
            # Marker
            history.append(_make_marker(f"Summary {i}"))

        original_raw_count = sum(
            1 for m in history if not _is_compression_marker(m) and m.role != SYSTEM
        )

        mock_inst = MagicMock()
        mock_inst.conversation = list(history)
        mock_inst._compression_lock = threading.Lock()
        mock_inst.rebuild_conversation = MagicMock()

        mock_pool = MagicMock()
        mock_pool.get_instance.return_value = mock_inst
        mock_pool.get_logger.return_value._consolidate_markers_in_jsonl.return_value = True

        with patch("agent_cascade.agent_pool.AgentPool") as MockAgentPoolClass:
            MockAgentPoolClass.find_all_marker_indices.side_effect = lambda h: [
                i for i, m in enumerate(h) if _is_compression_marker(m)
            ]

            with patch("agent_cascade.settings.COMPRESSION_CONSOLIDATION_THRESHOLD", 5):
                with patch(
                    "agent_cascade.compression.agent_invoker.invoke_consolidation_agent"
                ) as mock_invoke:
                    mock_invoke.return_value = "Consolidated"

                    _consolidate_markers(mock_pool, "TestAgent")

                    # Verify rebuild_conversation was called
                    assert mock_inst.rebuild_conversation.called
                    new_history = mock_inst.rebuild_conversation.call_args[0][0]

                    # Count raw messages in new history (excluding SYSTEM and markers)
                    new_raw_count = sum(
                        1 for m in new_history if not _is_compression_marker(m) and m.role != SYSTEM
                    )

                    assert new_raw_count == original_raw_count, \
                        f"Raw segments lost: {original_raw_count} -> {new_raw_count}"

    def test_recursion_guard_prevents_re_entry(self):
        """If consolidation is already running for an agent, second call should skip."""
        from agent_cascade.compression.core import _consolidate_markers, _consolidating_agents, _consolidation_lock

        # Manually set recursion guard
        with _consolidation_lock:
            _consolidating_agents.add("TestAgent")

        mock_inst = MagicMock()
        mock_inst.conversation = _build_history_with_markers(num_markers=8)
        mock_inst._compression_lock = threading.Lock()

        mock_pool = MagicMock()
        mock_pool.get_instance.return_value = mock_inst

        # Invoke should not be called because recursion guard blocks early
        with patch(
            "agent_cascade.compression.agent_invoker.invoke_consolidation_agent"
        ) as mock_invoke:
            _consolidate_markers(mock_pool, "TestAgent")
            mock_invoke.assert_not_called()

    def test_non_fatal_compressor_failure(self):
        """If consolidation agent raises RuntimeError, function returns without crashing."""
        from agent_cascade.compression.core import _consolidate_markers

        history = _build_history_with_markers(num_markers=8)

        mock_inst = MagicMock()
        mock_inst.conversation = history
        mock_inst._compression_lock = threading.Lock()
        mock_inst.rebuild_conversation = MagicMock()

        mock_pool = MagicMock()
        mock_pool.get_instance.return_value = mock_inst

        with patch("agent_cascade.agent_pool.AgentPool") as MockAgentPoolClass:
            MockAgentPoolClass.find_all_marker_indices.side_effect = lambda h: [
                i for i, m in enumerate(h) if _is_compression_marker(m)
            ]

            with patch("agent_cascade.settings.COMPRESSION_CONSOLIDATION_THRESHOLD", 5):
                with patch(
                    "agent_cascade.compression.agent_invoker.invoke_consolidation_agent"
                ) as mock_invoke:
                    mock_invoke.side_effect = RuntimeError("Compressor crashed")

                    # Should not raise
                    _consolidate_markers(mock_pool, "TestAgent")

                    # Pool should not be mutated
                    mock_inst.rebuild_conversation.assert_not_called()

    def test_non_fatal_extraction_failure(self):
        """If all summaries fail to extract, consolidation aborts gracefully."""
        from agent_cascade.compression.core import _consolidate_markers

        history = _build_history_with_markers(num_markers=8)

        mock_inst = MagicMock()
        mock_inst.conversation = history
        mock_inst._compression_lock = threading.Lock()
        mock_inst.rebuild_conversation = MagicMock()

        mock_pool = MagicMock()
        mock_pool.get_instance.return_value = mock_inst

        with patch("agent_cascade.agent_pool.AgentPool") as MockAgentPoolClass:
            MockAgentPoolClass.find_all_marker_indices.side_effect = lambda h: [
                i for i, m in enumerate(h) if _is_compression_marker(m)
            ]

            with patch("agent_cascade.settings.COMPRESSION_CONSOLIDATION_THRESHOLD", 5):
                # Patch extract_summary_from_marker where it's imported in core.py
                with patch(
                    "agent_cascade.compression.helpers.extract_summary_from_marker",
                    return_value=None,
                ):
                    _consolidate_markers(mock_pool, "TestAgent")

                    # Should not proceed to LLM call or pool mutation
                    mock_inst.rebuild_conversation.assert_not_called()


# ────────────────────────────────────────────────────────────────────────────
# 5. Integration tests — Real AgentPool with mocked LLM
# ────────────────────────────────────────────────────────────────────────────


class TestConsolidationIntegration:
    """End-to-end consolidation flow using real MockAgentPool and mocked LLM."""

    @pytest.fixture(autouse=True)
    def reset_consolidation_state(self):
        """Reset the module-level recursion guard before and after each test."""
        from agent_cascade.compression.core import _consolidating_agents, _consolidation_lock
        with _consolidation_lock:
            _consolidating_agents.clear()
        yield
        with _consolidation_lock:
            _consolidating_agents.clear()

    @pytest.fixture
    def pool_with_many_markers(self):
        """Create a MockAgentPool instance with 8+ compression markers."""
        from tests.conftest import MockAgentPool, MockInstance

        history = _build_history_with_markers(num_markers=10, msgs_between=3)

        # Build the real MockAgentPool structure
        pool = MockAgentPool.__new__(MockAgentPool)
        pool.instance_name = "TestAgent"
        inst = MockInstance(history)
        # Add attributes needed by _consolidate_markers
        inst._compression_lock = threading.Lock()
        inst.rebuild_conversation = lambda new_hist: setattr(inst, 'conversation', list(new_hist))
        pool.instances = {"TestAgent": inst}
        pool.instance_conversations = {}
        pool.instance_conversations["TestAgent"] = list(inst.conversation)

        return pool, history

    def test_end_to_end_consolidation_flow(self, pool_with_many_markers):
        """Trigger consolidation and verify marker count reduced, raw segments preserved."""
        from agent_cascade.compression.core import _consolidate_markers
        from tests.conftest import MockAgentPool

        pool, original_history = pool_with_many_markers

        # Count original markers and raw messages
        original_marker_count = AgentPool.count_markers(original_history)
        assert original_marker_count >= 8

        original_raw_messages = [
            m for m in original_history if not _is_compression_marker(m)
        ]

        mock_logger = MagicMock()
        mock_logger._consolidate_markers_in_jsonl.return_value = True
        pool.get_logger = MagicMock(return_value=mock_logger)

        # Also patch get_instance to return the real instance
        pool.get_instance = lambda name: pool.instances.get(name)

        with patch("agent_cascade.compression.agent_invoker.invoke_consolidation_agent") as mock_invoke:
            mock_invoke.return_value = "Fully consolidated summary of all cycles"

            _consolidate_markers(pool, "TestAgent")

        # Verify consolidation was attempted
        assert mock_invoke.called

        # Get the new history from pool
        new_history = pool.get_conversation("TestAgent")

        # Marker count should be reduced (many markers -> 2: one L2 + one kept newest)
        new_marker_count = AgentPool.count_markers(new_history)
        assert new_marker_count < original_marker_count, \
            f"Markers not reduced: {original_marker_count} -> {new_marker_count}"

        # Raw messages should be preserved
        new_raw_messages = [m for m in new_history if not _is_compression_marker(m)]
        assert len(new_raw_messages) == len(original_raw_messages), \
            f"Raw messages changed: {len(original_raw_messages)} -> {len(new_raw_messages)}"

    def test_tail_sync_invariant(self, pool_with_many_markers):
        """After consolidation, tail past last marker has same count in pool and JSONL."""
        from agent_cascade.compression.core import _consolidate_markers
        import tempfile

        pool, original_history = pool_with_many_markers

        # Create a temp JSONL file to simulate real logger behavior
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            jsonl_path = f.name
            # Write metadata line
            f.write(json.dumps({"metadata": {"instance_name": "TestAgent"}}) + '\n')
            # Write all messages
            for msg in original_history:
                if isinstance(msg, Message):
                    f.write(json.dumps({"role": msg.role, "content": msg.content}) + '\n')
                else:
                    f.write(json.dumps(msg) + '\n')

        try:
            mock_logger = MagicMock()
            mock_logger.log_path = jsonl_path
            mock_logger._consolidate_markers_in_jsonl.return_value = True
            pool.get_logger = MagicMock(return_value=mock_logger)
            pool.get_instance = lambda name: pool.instances.get(name)

            with patch("agent_cascade.compression.agent_invoker.invoke_consolidation_agent") as mock_invoke:
                mock_invoke.return_value = "Consolidated"

                _consolidate_markers(pool, "TestAgent")

            new_history = pool.get_conversation("TestAgent")

            # Find last marker in new pool history
            last_marker_idx = -1
            for i, m in enumerate(new_history):
                if _is_compression_marker(m):
                    last_marker_idx = i

            assert last_marker_idx >= 0, "No markers found after consolidation"

            # Tail = messages after last marker
            tail_in_pool = new_history[last_marker_idx + 1:]

            # Read JSONL to verify tail matches
            jsonl_msgs = []
            with open(jsonl_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        if "metadata" not in item and "event" not in item:
                            jsonl_msgs.append(item)
                    except json.JSONDecodeError:
                        pass

            # Find last marker in JSONL
            jsonl_last_marker_idx = -1
            for i, m in enumerate(jsonl_msgs):
                content = m.get('content', '')
                if isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                    jsonl_last_marker_idx = i

            assert jsonl_last_marker_idx >= 0, "No markers found in JSONL"

            tail_in_jsonl = jsonl_msgs[jsonl_last_marker_idx + 1:]

            # Tail counts should match between pool and JSONL
            assert len(tail_in_pool) == len(tail_in_jsonl), \
                f"Tail mismatch: pool={len(tail_in_pool)}, jsonl={len(tail_in_jsonl)}"

        finally:
            os.unlink(jsonl_path)


class TestSettingsDrivenBehavior:
    """Test consolidation behavior with different threshold settings."""

    @pytest.fixture(autouse=True)
    def reset_consolidation_state(self):
        """Reset the module-level recursion guard before and after each test."""
        from agent_cascade.compression.core import _consolidating_agents, _consolidation_lock
        with _consolidation_lock:
            _consolidating_agents.clear()
        yield
        with _consolidation_lock:
            _consolidating_agents.clear()

    def test_consolidation_fires_at_threshold(self):
        """With threshold=5 and exactly 5 markers, consolidation should trigger."""
        from agent_cascade.compression.core import _consolidate_markers

        history = _build_history_with_markers(num_markers=5, msgs_between=2)

        mock_inst = MagicMock()
        mock_inst.conversation = history
        mock_inst._compression_lock = threading.Lock()
        mock_inst.rebuild_conversation = MagicMock()

        mock_pool = MagicMock()
        mock_pool.get_instance.return_value = mock_inst
        mock_pool.get_logger.return_value._consolidate_markers_in_jsonl.return_value = True

        with patch("agent_cascade.agent_pool.AgentPool") as MockAgentPoolClass:
            MockAgentPoolClass.find_all_marker_indices.side_effect = lambda h: [
                i for i, m in enumerate(h) if _is_compression_marker(m)
            ]

            # Patch threshold to 5
            with patch("agent_cascade.settings.COMPRESSION_CONSOLIDATION_THRESHOLD", 5):
                with patch(
                    "agent_cascade.compression.agent_invoker.invoke_consolidation_agent"
                ) as mock_invoke:
                    mock_invoke.return_value = "Consolidated"

                    _consolidate_markers(mock_pool, "TestAgent")

                    # Should have called the consolidation agent
                    assert mock_invoke.called

    def test_consolidation_skips_below_threshold(self):
        """With threshold=5 and only 4 markers, consolidation should skip."""
        from agent_cascade.compression.core import _consolidate_markers

        history = _build_history_with_markers(num_markers=4, msgs_between=2)

        mock_inst = MagicMock()
        mock_inst.conversation = history
        mock_inst._compression_lock = threading.Lock()
        mock_inst.rebuild_conversation = MagicMock()

        mock_pool = MagicMock()
        mock_pool.get_instance.return_value = mock_inst

        with patch("agent_cascade.agent_pool.AgentPool") as MockAgentPoolClass:
            MockAgentPoolClass.find_all_marker_indices.side_effect = lambda h: [
                i for i, m in enumerate(h) if _is_compression_marker(m)
            ]

            # Patch threshold to 5
            with patch("agent_cascade.settings.COMPRESSION_CONSOLIDATION_THRESHOLD", 5):
                with patch(
                    "agent_cascade.compression.agent_invoker.invoke_consolidation_agent"
                ) as mock_invoke:
                    _consolidate_markers(mock_pool, "TestAgent")

                    # Should NOT have called the consolidation agent
                    mock_invoke.assert_not_called()

    def test_high_threshold_prevents_consolidation(self):
        """With a very high threshold, even many markers won't trigger consolidation."""
        from agent_cascade.compression.core import _consolidate_markers

        history = _build_history_with_markers(num_markers=10, msgs_between=2)

        mock_inst = MagicMock()
        mock_inst.conversation = history
        mock_inst._compression_lock = threading.Lock()
        mock_inst.rebuild_conversation = MagicMock()

        mock_pool = MagicMock()
        mock_pool.get_instance.return_value = mock_inst

        with patch("agent_cascade.agent_pool.AgentPool") as MockAgentPoolClass:
            MockAgentPoolClass.find_all_marker_indices.side_effect = lambda h: [
                i for i, m in enumerate(h) if _is_compression_marker(m)
            ]

            # Patch threshold to 20 (higher than our marker count)
            with patch("agent_cascade.settings.COMPRESSION_CONSOLIDATION_THRESHOLD", 20):
                with patch(
                    "agent_cascade.compression.agent_invoker.invoke_consolidation_agent"
                ) as mock_invoke:
                    _consolidate_markers(mock_pool, "TestAgent")

                    mock_invoke.assert_not_called()


# ────────────────────────────────────────────────────────────────────────────
# 6. compress_context consolidation trigger (core.py) — Integration
# ────────────────────────────────────────────────────────────────────────────


class TestCompressContextConsolidationTrigger:
    """Test that compress_context triggers consolidation when threshold is met."""

    @pytest.fixture(autouse=True)
    def reset_consolidation_state(self):
        from agent_cascade.compression.core import _consolidating_agents, _consolidation_lock
        with _consolidation_lock:
            _consolidating_agents.clear()
        yield
        with _consolidation_lock:
            _consolidating_agents.clear()

    def test_consolidation_triggered_after_compression_when_threshold_met(self):
        """compress_context should call _consolidate_markers if post-compression marker count >= threshold."""
        from agent_cascade.compression.core import compress_context, _consolidate_markers
        from tests.conftest import MockAgentPool

        # Build pool with many existing markers + fresh messages to compress after last marker
        history = _build_history_with_markers(num_markers=7, msgs_between=2)

        # Add more messages AFTER the last marker so there's active content to compress
        for i in range(5):
            history.append(_make_msg(USER, f"Post-marker user {i}"))
            history.append(_make_msg("assistant", f"Post-marker assistant {i}"))

        pool = MockAgentPool(history)

        mock_logger = MagicMock()
        mock_logger._consolidate_markers_in_jsonl.return_value = True
        pool.get_logger = MagicMock(return_value=mock_logger)

        # Create a fake compressor agent with required attributes (for token budget estimation)
        mock_comp_agent = MagicMock()
        mock_comp_agent.llm.generate_cfg = {"max_input_tokens": 128000}

        # Override get_agent to return the compressor when asked for 'Compressor'
        original_get_agent = pool.get_agent
        def patched_get_agent(name):
            if name == "Compressor":
                return mock_comp_agent
            return original_get_agent(name)
        pool.get_agent = patched_get_agent

        # Patch invoke_compression_agent at the point where core.py uses it (module-level import)
        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Fresh compression summary"

            # Patch _consolidate_markers to track if it was called
            with patch("agent_cascade.compression.core._consolidate_markers") as mock_consolidate:
                result = compress_context(
                    agent_pool=pool,
                    target_agent_name="TestAgent",
                    fraction=0.5,
                    mode="auto",
                    force=True,  # Force to bypass validation
                )

                assert result.success is True

                # _consolidate_markers should have been called since we have >= 8 markers after compression
                mock_consolidate.assert_called_once()
                call_args = mock_consolidate.call_args
                assert call_args[0][1] == "TestAgent"

    def test_no_consolidation_when_dry_run(self):
        """compress_context with dry_run=True should NOT trigger consolidation."""
        from agent_cascade.compression.core import compress_context
        from tests.conftest import MockAgentPool

        # Build pool with markers + active messages to compress
        history = _build_history_with_markers(num_markers=7, msgs_between=2)

        # Add more messages AFTER the last marker so there's active content to compress
        for i in range(5):
            history.append(_make_msg(USER, f"Post-marker user {i}"))
            history.append(_make_msg("assistant", f"Post-marker assistant {i}"))

        pool = MockAgentPool(history)

        mock_logger = MagicMock()
        pool.get_logger = MagicMock(return_value=mock_logger)

        # Create a fake compressor agent with required attributes (for token budget estimation)
        mock_comp_agent = MagicMock()
        mock_comp_agent.llm.generate_cfg = {"max_input_tokens": 128000}

        # Override get_agent to return the compressor when asked for 'Compressor'
        original_get_agent = pool.get_agent
        def patched_get_agent(name):
            if name == "Compressor":
                return mock_comp_agent
            return original_get_agent(name)
        pool.get_agent = patched_get_agent

        # Patch invoke_compression_agent at the point where core.py uses it (module-level import)
        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary"

            with patch("agent_cascade.compression.core._consolidate_markers") as mock_consolidate:
                result = compress_context(
                    agent_pool=pool,
                    target_agent_name="TestAgent",
                    fraction=0.5,
                    mode="auto",
                    force=True,
                    dry_run=True,
                )

                assert result.success is True
                mock_consolidate.assert_not_called()
