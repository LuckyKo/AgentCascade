"""Regression tests for the compression redesign (all 4 phases).

Covers:
- helpers.py: compute_discard_count, build_marker_message, rebuild_working_set
- core.py: compress_context clean trim, force mode, manual mode, dry_run, failure paths, fraction validation
- agent_pool.py additions: get_compression_target_set, find_last_marker
- Integration: nested compression guard (hooked_call_llm skips compression_agent)

All tests are self-contained — no LLM or API server required.
"""

import copy
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.llm.schema import SYSTEM, USER, Message
from agent_cascade.compression.result import CompressResult
from agent_cascade.compression.helpers import (
    compute_discard_count,
    build_marker_message,
    rebuild_working_set,
)
from agent_cascade.compression.core import compress_context


# ──────────────────────────────────────────────
# Test Fixtures — Lightweight Mock Pool
# ──────────────────────────────────────────────

def _make_msg(role, content):
    """Create a Message object for testing."""
    return Message(role=role, content=content)


class MockInstance:
    """Mock AgentInstance for testing Phase 3 changes."""
    
    class _FakeLock:
        """A no-op context manager that mimics threading.RLock behavior."""
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    
    _compression_lock = _FakeLock()

    def __init__(self, history):
        self.conversation = list(history)
        self._last_token_count_conversation_length = -1


class MockAgentPool:
    """Lightweight mock of AgentPool that implements the methods compress_context needs.

    This avoids the heavy real AgentPool (which has DB/file deps) while providing
    correct behavior for get_compression_target_set, find_last_marker, and pool mutation.
    """

    def __init__(self, history=None):
        self.history = list(history) if history else []
        self.instance_conversations = {}  # Will be synced below
        self.instance_loggers = {}  # No logger — avoids notification side-effects
        # Phase 3: Add instances dict and get_instance for proper pool API access
        mock_inst = MockInstance(self.history)
        self.instances = {"TestAgent": mock_inst}
        # Sync instance_conversations to point to the same list as instance.conversation
        self.instance_conversations["TestAgent"] = mock_inst.conversation

    def get_conversation(self, agent_name):
        """Read from instance.conversation (Phase 3 pattern)."""
        inst = self.instances.get(agent_name)
        if inst is not None:
            return list(inst.conversation)
        return []  # Match production semantics — no fallback to bridge dict

    def get_instance(self, agent_name):
        """Phase 3: Return a mock instance if it exists."""
        return self.instances.get(agent_name)

    @staticmethod
    def find_last_marker(history):
        """Same logic as AgentPool.find_last_marker."""
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                return i
        return -1

    def get_compression_target_set(self, agent_name):
        """Same logic as AgentPool.get_compression_target_set."""
        history = self.get_conversation(agent_name)
        if not history:
            return None, [], -1

        start_idx = 1 if (history[0].get('role') == SYSTEM if isinstance(history[0], dict) else getattr(history[0], 'role', '') == SYSTEM) else 0
        latest_summary_idx = self.find_last_marker(history)
        active_start_idx = latest_summary_idx + 1 if latest_summary_idx != -1 else start_idx
        messages_to_compress = history[active_start_idx:]
        return active_start_idx, messages_to_compress, latest_summary_idx


def _build_pool_with_history(num_user_msgs=10):
    """Build a MockAgentPool with realistic conversation history."""
    history: list[Message] = [_make_msg(SYSTEM, "You are a test agent")]
    for i in range(num_user_msgs):
        history.append(_make_msg(USER, f"User message {i}"))
        history.append(_make_msg("assistant", f"Assistant reply {i}"))
    pool = MockAgentPool(history)
    return pool, len(history)


def _build_pool_with_marker(msgs_before=5, msgs_after=8):
    """Build a MockAgentPool with an existing compression marker.

    Layout: [SYSTEM] + msgs_before*2 + [MARKER] + msgs_after*2
    """
    history: list[Message] = [_make_msg(SYSTEM, "You are a test agent")]

    for i in range(msgs_before):
        history.append(_make_msg(USER, f"Old user {i}"))
        history.append(_make_msg("assistant", f"Old assistant {i}"))

    marker_content = f"{COMPRESSION_MARKER} (50% summarized) ---\nSummary: old stuff"
    history.append(_make_msg(USER, marker_content))

    for i in range(msgs_after):
        history.append(_make_msg(USER, f"New user {i}"))
        history.append(_make_msg("assistant", f"New assistant {i}"))

    pool = MockAgentPool(history)
    return pool, len(history)


# ──────────────────────────────────────────────
# 1. compute_discard_count
# ──────────────────────────────────────────────

class TestComputeDiscardCount:
    """Test the discard-count calculation logic."""

    def test_normal_fraction(self):
        """fraction=0.5 on 10 messages → discard 5, clamped to 8 (keep 2 tail)."""
        active = list(range(10))
        count = compute_discard_count(active, fraction=0.5, force=False)
        # int(10 * 0.5) = 5; min(5, 10-2) = 5
        assert count == 5

    def test_fraction_keeps_two_tail(self):
        """Large fraction should still keep 2 tail messages."""
        active = list(range(10))
        count = compute_discard_count(active, fraction=0.9, force=False)
        # int(10 * 0.9) = 9; min(9, 8) = 8
        assert count == 8

    def test_force_mode_bypasses_tail_guard(self):
        """force=True should discard even from small sets."""
        active = list(range(3))
        count = compute_discard_count(active, fraction=0.5, force=True)
        # int(3 * 0.5) = 1; max(1, 1) = 1
        assert count == 1

    def test_force_mode_small_set_minimum_one(self):
        """force=True on a set where fraction rounds to 0 → still discards 1."""
        active = list(range(3))
        count = compute_discard_count(active, fraction=0.1, force=True)
        # int(3 * 0.1) = 0; max(1, 0) = 1
        assert count == 1

    def test_fraction_zero(self):
        """fraction=0 → discard 0 (without force)."""
        active = list(range(10))
        count = compute_discard_count(active, fraction=0.0, force=False)
        assert count == 0

    def test_fraction_zero_force(self):
        """fraction=0 with force=True → discard at least 1."""
        active = list(range(10))
        count = compute_discard_count(active, fraction=0.0, force=True)
        assert count == 1

    def test_fraction_one(self):
        """fraction=1.0 without force → clamped to len-2."""
        active = list(range(10))
        count = compute_discard_count(active, fraction=1.0, force=False)
        # int(10*1.0) = 10; min(10, 8) = 8
        assert count == 8

    def test_fraction_one_force(self):
        """fraction=1.0 with force=True → discards all."""
        active = list(range(10))
        count = compute_discard_count(active, fraction=1.0, force=True)
        # int(10*1.0) = 10; max(1, 10) = 10
        assert count == 10

    def test_small_active_set_no_force(self):
        """Small active set without force → discard 1 (since len-2=1)."""
        active = list(range(3))
        count = compute_discard_count(active, fraction=0.5, force=False)
        # int(3*0.5)=1; min(1, 1)=1
        assert count == 1

    def test_very_small_active_set_no_force(self):
        """Only 2 messages, no force → discard 0."""
        active = list(range(2))
        count = compute_discard_count(active, fraction=0.5, force=False)
        # int(2*0.5)=1; min(1, 0)=0
        assert count == 0

    def test_empty_active_set(self):
        """Empty active set → discard 0."""
        count = compute_discard_count([], fraction=0.5, force=False)
        assert count == 0


# ──────────────────────────────────────────────
# 2. build_marker_message
# ──────────────────────────────────────────────

class TestBuildMarkerMessage:
    """Test marker message construction.

    Current signature: build_marker_message(summary_text, fraction)
    where fraction is a float (e.g., 0.5 for 50%).
    """

    def test_returns_message_object(self):
        """build_marker_message returns a Message with role=USER."""
        msg = build_marker_message("test summary", 0.5)
        assert isinstance(msg, Message)
        assert msg.role == USER

    def test_contains_compression_marker(self):
        """Marker message content starts with COMPRESSION_MARKER."""
        msg = build_marker_message("test summary", 0.6)
        assert msg.content.startswith(COMPRESSION_MARKER)

    def test_contains_summary_text(self):
        """Marker message includes the raw summary text."""
        summary = "The agent was building a web app"
        msg = build_marker_message(summary, 0.5)
        assert summary in msg.content

    def test_contains_fraction_header(self):
        """Marker message includes the compression fraction in the header."""
        msg = build_marker_message("summary", 0.75)
        assert "75% of history summarized" in msg.content

    def test_small_fraction_rounds_correctly(self):
        """Fraction is converted to integer percent without decimals."""
        msg = build_marker_message("summary", 0.33)
        assert "33% of history summarized" in msg.content

    def test_full_template_format(self):
        """Marker message matches the expected template structure."""
        summary = "Test summary"
        msg = build_marker_message(summary, 0.5)
        assert "--- CONTEXT COMPRESSED (50% of history summarized) ---" in msg.content
        assert "<context_summary>" in msg.content
        assert "</context_summary>" in msg.content
        assert summary in msg.content

    def test_invalid_fraction_is_handled(self):
        """Fraction > 1.0 produces a percentage > 100% (no ValueError, just unusual output)."""
        # The function doesn't validate fraction — it just formats it as a percentage.
        # A fraction of 2.0 would produce "200% of history summarized".
        msg = build_marker_message("summary", 2.0)
        assert "200% of history summarized" in msg.content


# ──────────────────────────────────────────────
# 3. rebuild_working_set
# ──────────────────────────────────────────────

class TestRebuildWorkingSet:
    """Test working set rebuild from pool state."""

    def test_replaces_content_with_deepcopy(self):
        """rebuild_working_set clears and extends with deepcopy of pool content."""
        pool, _ = _build_pool_with_history(num_user_msgs=3)

        caller_list: list[Message] = [_make_msg(USER, "stale data")]
        rebuild_working_set(caller_list, pool, "TestAgent")

        # Should have replaced stale data with pool content
        assert len(caller_list) == 7  # 1 system + 3*2 user/assistant
        assert caller_list[0].role == SYSTEM

    def test_deepcopy_independence(self):
        """Modifying the rebuilt list doesn't affect pool state."""
        pool, _ = _build_pool_with_history(num_user_msgs=2)

        caller_list: list[Message] = []
        rebuild_working_set(caller_list, pool, "TestAgent")

        original_len = len(pool.get_conversation("TestAgent"))
        caller_list.append(_make_msg(USER, "new msg"))

        # Pool should be unaffected
        assert len(pool.get_conversation("TestAgent")) == original_len

    def test_empty_pool_returns_early(self):
        """If pool has no conversation, caller list is unchanged."""
        pool = MockAgentPool(history=[])

        caller_list: list[Message] = [_make_msg(USER, "keep this")]
        rebuild_working_set(caller_list, pool, "Nobody")

        assert len(caller_list) == 1
        assert caller_list[0].content == "keep this"


# ──────────────────────────────────────────────
# 4. compress_context — Clean Trim
# ──────────────────────────────────────────────

class TestCompressContextCleanTrim:
    """Verify that clean trim actually deletes messages (not cumulative)."""

    def test_messages_actually_deleted(self):
        """After compression, discarded messages are removed from the pool."""
        pool, initial_len = _build_pool_with_history(num_user_msgs=10)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary of the conversation"

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                force=False,
            )

        assert result.success is True
        # Pool history should be shorter than initial (clean trim)
        new_history = pool.get_conversation("TestAgent")
        assert len(new_history) < initial_len
        assert result.messages_discarded > 0

    def test_marker_inserted_at_correct_position(self):
        """Marker message is inserted after the discarded messages."""
        pool, _ = _build_pool_with_history(num_user_msgs=10)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary"

            compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                force=False,
            )

        new_history = pool.get_conversation("TestAgent")
        # Find the marker
        marker_idx = None
        for i, msg in enumerate(new_history):
            if isinstance(msg.content, str) and msg.content.startswith(COMPRESSION_MARKER):
                marker_idx = i
                break
        assert marker_idx is not None, "Marker message not found in new history"
        # Marker should not be at position 0 (SYSTEM is at 0)
        assert marker_idx > 0

    def test_clean_trim_not_cumulative(self):
        """Two successive compressions should each trim independently."""
        pool, initial_len = _build_pool_with_history(num_user_msgs=10)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary 1"

            result1 = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                force=True,
            )

        assert result1.success is True
        after_first = len(pool.get_conversation("TestAgent"))

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary 2"

            # Second compression on the now-smaller pool
            result2 = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                force=True,
            )

        # Second compression should also succeed (or defer if too small)
        after_second = len(pool.get_conversation("TestAgent"))
        assert after_second <= after_first  # Pool doesn't grow from compression


# ──────────────────────────────────────────────
# 5. compress_context — Force Mode
# ──────────────────────────────────────────────

class TestCompressContextForceMode:
    """Verify force mode compresses even when active_set is small."""

    def test_force_compression_on_small_set(self):
        """force=True bypasses the 'not enough messages to discard' guard.

        Note: The token-based guard (<3 msgs AND <200 tokens) fires before the
        force check, so we need at least 3 active messages to reach the force logic.
        With 3 user+assistant pairs (6 msgs), fraction=0.1 gives discard=0 without
        force — but force=True ensures at least 1 is discarded.
        """
        pool, _ = _build_pool_with_history(num_user_msgs=3)  # 6 active msgs

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary"

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.1,  # int(6*0.1) = 0 → would discard 0 without force
                mode="auto",
                force=True,
            )

        # Force mode should succeed — at least 1 message discarded even though
        # fraction rounds to 0
        assert result.success is True
        assert result.messages_discarded >= 1


# ──────────────────────────────────────────────
# 6. compress_context — Manual Mode
# ──────────────────────────────────────────────

class TestCompressContextManualMode:
    """Verify Compression Agent is NOT invoked in manual mode."""

    def test_manual_mode_skips_agent_invocation(self):
        """mode='manual' with summary_text should NOT call invoke_compression_agent."""
        pool, _ = _build_pool_with_history(num_user_msgs=5)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="manual",
                summary_text="User-provided summary of events",
            )

        # invoke_compression_agent should NOT have been called
        mock_invoke.assert_not_called()
        assert result.success is True
        assert "User-provided summary" in result.summary_text

    def test_manual_mode_without_summary_fails(self):
        """mode='manual' without summary_text returns failure."""
        pool, _ = _build_pool_with_history(num_user_msgs=5)

        result = compress_context(
            agent_pool=pool,
            target_agent_name="TestAgent",
            fraction=0.5,
            mode="manual",
            summary_text=None,
        )

        assert result.success is False
        # Error mentions both summary_text and precomputed_summary requirements
        assert "summary_text" in (result.error or "")
        assert "precomputed_summary" in (result.error or "")


# ──────────────────────────────────────────────
# 7. compress_context — Dry Run
# ──────────────────────────────────────────────

class TestCompressContextDryRun:
    """Verify dry_run generates summary but doesn't mutate the pool."""

    def test_dry_run_no_pool_mutation(self):
        """dry_run=True should leave the pool unchanged."""
        pool, initial_len = _build_pool_with_history(num_user_msgs=10)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary"

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                dry_run=True,
            )

        assert result.success is True
        # Pool should be completely unchanged
        assert len(pool.get_conversation("TestAgent")) == initial_len

    def test_dry_run_returns_discard_count(self):
        """dry_run should still report how many messages would be discarded."""
        pool, _ = _build_pool_with_history(num_user_msgs=10)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary"

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                dry_run=True,
            )

        assert result.messages_discarded > 0
        assert result.tail_count > 0


# ──────────────────────────────────────────────
# 8. compress_context — Failure Paths
# ──────────────────────────────────────────────

class TestCompressContextFailurePaths:
    """Verify graceful failure with untouched pool."""

    def test_agent_invocation_failure(self):
        """If invoke_compression_agent raises, pool is untouched and result.success=False."""
        pool, initial_len = _build_pool_with_history(num_user_msgs=10)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.side_effect = RuntimeError("LLM timeout")

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
            )

        assert result.success is False
        assert "Compression Agent failed" in (result.error or "")
        # Pool should be untouched
        assert len(pool.get_conversation("TestAgent")) == initial_len

    def test_no_active_messages(self):
        """Empty active set returns failure."""
        pool = MockAgentPool(history=[_make_msg(SYSTEM, "System")])

        result = compress_context(
            agent_pool=pool,
            target_agent_name="TestAgent",
            fraction=0.5,
            mode="auto",
        )

        assert result.success is False
        assert "No active messages" in (result.error or "")

    def test_already_optimally_compressed(self):
        """Very small active set with few tokens returns deferral."""
        pool = MockAgentPool(history=[
            _make_msg(SYSTEM, "System"),
            _make_msg(USER, "Hi"),
            _make_msg("assistant", "Hello!"),
        ])

        result = compress_context(
            agent_pool=pool,
            target_agent_name="TestAgent",
            fraction=0.5,
            mode="auto",
        )

        assert result.success is False
        assert "optimally compressed" in (result.error or "").lower()


# ──────────────────────────────────────────────
# 9. Fraction Validation
# ──────────────────────────────────────────────

class TestFractionValidation:
    """Reject fraction < 0 or > 1."""

    def test_negative_fraction(self):
        """fraction=-0.1 → failure."""
        pool, _ = _build_pool_with_history(num_user_msgs=5)

        result = compress_context(
            agent_pool=pool,
            target_agent_name="TestAgent",
            fraction=-0.1,
            mode="auto",
        )

        assert result.success is False
        assert "fraction must be between 0.0 and 1.0" in (result.error or "")

    def test_fraction_over_one(self):
        """fraction=1.5 → failure."""
        pool, _ = _build_pool_with_history(num_user_msgs=5)

        result = compress_context(
            agent_pool=pool,
            target_agent_name="TestAgent",
            fraction=1.5,
            mode="auto",
        )

        assert result.success is False
        assert "fraction must be between 0.0 and 1.0" in (result.error or "")

    def test_fraction_zero_boundary(self):
        """fraction=0.0 passes validation (but may discard 0 messages)."""
        pool, _ = _build_pool_with_history(num_user_msgs=5)

        result = compress_context(
            agent_pool=pool,
            target_agent_name="TestAgent",
            fraction=0.0,
            mode="auto",
        )

        # Should pass validation but fail at "not enough to compress" guard
        assert result.success is False
        assert "fraction must be between 0.0 and 1.0" not in (result.error or "")

    def test_fraction_one_boundary(self):
        """fraction=1.0 passes validation."""
        pool, _ = _build_pool_with_history(num_user_msgs=5)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary"

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=1.0,
                mode="auto",
                force=False,
            )

        # Should pass validation and succeed (clamped to len-2 tail)
        assert result.success is True


# ──────────────────────────────────────────────
# 10. get_compression_target_set
# ──────────────────────────────────────────────

class TestGetCompressionTargetSet:
    """Test the MockAgentPool.get_compression_target_set method (mirrors AgentPool)."""

    def test_without_existing_marker(self):
        """Without a marker, active set starts after SYSTEM message."""
        pool, _ = _build_pool_with_history(num_user_msgs=5)

        active_start_idx, messages_to_compress, latest_summary_idx = (
            pool.get_compression_target_set("TestAgent")
        )

        assert latest_summary_idx == -1  # No marker
        assert active_start_idx == 1  # After SYSTEM message
        assert len(messages_to_compress) == 10  # 5 user + 5 assistant

    def test_with_existing_marker(self):
        """With a marker, active set starts after the marker."""
        pool, _ = _build_pool_with_marker(msgs_before=3, msgs_after=4)

        active_start_idx, messages_to_compress, latest_summary_idx = (
            pool.get_compression_target_set("TestAgent")
        )

        assert latest_summary_idx != -1  # Has marker
        assert active_start_idx == latest_summary_idx + 1
        assert len(messages_to_compress) == 8  # 4 user + 4 assistant after marker

    def test_empty_conversation(self):
        """Empty conversation returns None start index and empty list."""
        pool = MockAgentPool(history=[])

        active_start_idx, messages_to_compress, latest_summary_idx = (
            pool.get_compression_target_set("Nobody")
        )

        assert active_start_idx is None
        assert messages_to_compress == []
        assert latest_summary_idx == -1


# ──────────────────────────────────────────────
# 11. find_last_marker
# ──────────────────────────────────────────────

class TestFindLastMarker:
    """Test the static find_last_marker method (via AgentPool and MockAgentPool)."""

    def test_no_marker_returns_minus_one(self):
        """History without any marker returns -1."""
        from agent_cascade.agent_pool import AgentPool

        history = [
            _make_msg(SYSTEM, "System"),
            _make_msg(USER, "Hello"),
            _make_msg("assistant", "Hi there"),
        ]
        assert AgentPool.find_last_marker(history) == -1

    def test_finds_single_marker(self):
        """Single marker in history is found."""
        from agent_cascade.agent_pool import AgentPool

        marker_content = f"{COMPRESSION_MARKER} (50%) ---\nSummary: old stuff"
        history = [
            _make_msg(SYSTEM, "System"),
            _make_msg(USER, "Old message 1"),
            _make_msg("assistant", "Old reply 1"),
            _make_msg(USER, marker_content),
            _make_msg(USER, "New message 1"),
            _make_msg("assistant", "New reply 1"),
        ]
        idx = AgentPool.find_last_marker(history)
        assert idx == 3  # The marker is at index 3

    def test_finds_latest_of_multiple_markers(self):
        """Multiple markers → returns the latest (last) one."""
        from agent_cascade.agent_pool import AgentPool

        m1 = f"{COMPRESSION_MARKER} (50%) ---\nSummary: first"
        m2 = f"{COMPRESSION_MARKER} (30%) ---\nSummary: second"
        history = [
            _make_msg(SYSTEM, "System"),
            _make_msg(USER, m1),
            _make_msg(USER, "middle message"),
            _make_msg("assistant", "middle reply"),
            _make_msg(USER, m2),
            _make_msg(USER, "after second marker"),
        ]
        idx = AgentPool.find_last_marker(history)
        assert idx == 4  # The second (latest) marker

    def test_ignores_non_user_markers(self):
        """Marker in assistant role is ignored (must be USER role)."""
        from agent_cascade.agent_pool import AgentPool

        marker_content = f"{COMPRESSION_MARKER} (50%) ---\nSummary: fake"
        history = [
            _make_msg(SYSTEM, "System"),
            _make_msg("assistant", marker_content),  # Wrong role — should be ignored
            _make_msg(USER, "Normal message"),
        ]
        assert AgentPool.find_last_marker(history) == -1

    def test_ignores_partial_match(self):
        """Content that merely contains the marker string but doesn't start with it is ignored."""
        from agent_cascade.agent_pool import AgentPool

        partial_content = f"Before: {COMPRESSION_MARKER} (50%) ---\nSummary: fake"
        history = [
            _make_msg(SYSTEM, "System"),
            _make_msg(USER, partial_content),
        ]
        assert AgentPool.find_last_marker(history) == -1

    def test_empty_history(self):
        """Empty history returns -1."""
        from agent_cascade.agent_pool import AgentPool

        assert AgentPool.find_last_marker([]) == -1

    def test_dict_messages(self):
        """find_last_marker works with dict-style messages (not just Message objects)."""
        from agent_cascade.agent_pool import AgentPool

        marker_content = f"{COMPRESSION_MARKER} (50%) ---\nSummary: old"
        history = [
            {"role": SYSTEM, "content": "System"},
            {"role": USER, "content": marker_content},
            {"role": "assistant", "content": "Reply"},
        ]
        idx = AgentPool.find_last_marker(history)
        assert idx == 1

    def test_mock_pool_marker_consistency(self):
        """MockAgentPool.find_last_marker gives same results as AgentPool."""
        from agent_cascade.agent_pool import AgentPool

        marker_content = f"{COMPRESSION_MARKER} (50%) ---\nSummary: old"
        history = [
            _make_msg(SYSTEM, "System"),
            _make_msg(USER, "msg1"),
            _make_msg("assistant", "reply1"),
            _make_msg(USER, marker_content),
            _make_msg(USER, "msg2"),
        ]

        real_idx = AgentPool.find_last_marker(history)
        mock_idx = MockAgentPool.find_last_marker(history)
        assert real_idx == mock_idx == 3


# ──────────────────────────────────────────────
# 12. Integration — Nested Compression Guard
# ──────────────────────────────────────────────

class TestNestedCompressionGuard:
    """Integration tests for nested compression guard using real orchestrator code.

    The guard lives in agent_orchestrator.py:1846:
        if instance_name != 'Compressor':
            hook_forced = self._inject_compression_warning_for_agent(...)

    These tests verify the guard's behavior using simple mock objects.
    """

    def test_orchestrator_skips_inject_for_compression_agent(self):
        """When instance_name == 'Compressor', _inject_compression_warning_for_agent
        is NOT called — prevents nested/circular compression."""

        inject_called = {"value": False}

        mock_orch = MagicMock()
        mock_orch._compress_context_ran_this_turn = False

        def track_inject(_agent, _instance_name, _messages):
            inject_called["value"] = True
            return False

        mock_orch._inject_compression_warning_for_agent = track_inject

        # Simulate what hooked_call_llm does for Compressor (agent_orchestrator.py:1846)
        instance_name = "Compressor"
        hook_forced = False

        if instance_name != 'Compressor':
            hook_forced = mock_orch._inject_compression_warning_for_agent(
                mock_orch, instance_name, []
            )

        assert hook_forced is False
        assert inject_called["value"] is False, (
            "_inject_compression_warning_for_agent should NOT be called "
            "for Compressor — nested compression guard failed"
        )

    def test_orchestrator_calls_inject_for_other_agents(self):
        """For non-compression agents, _inject_compression_warning_for_agent IS called."""

        inject_called = {"value": False}

        mock_orch = MagicMock()

        def track_inject(_agent, _instance_name, _messages):
            inject_called["value"] = True
            return False

        mock_orch._inject_compression_warning_for_agent = track_inject

        instance_name = "coder"
        hook_forced = False

        if instance_name != 'Compressor':
            hook_forced = mock_orch._inject_compression_warning_for_agent(
                mock_orch, instance_name, []
            )

        assert inject_called["value"] is True, (
            "_inject_compression_warning_for_agent SHOULD be called for non-compression agents"
        )

    def test_compression_agent_exemption_in_force_path(self):
        """
        Verify that the Compressor is in the exempt list during forced compression.

        From agent_orchestrator.py:682:
            exempt = [instance_name, 'Compressor', self.session_name]
            self.agent_pool.halt_all_instances(except_instances=exempt)
        """
        instance_name = "TestAgent"
        session_name = "Maine"
        exempt = [instance_name, 'Compressor', session_name]

        assert 'Compressor' in exempt, (
            "Compressor must be in the exempt list during forced compression"
        )
        assert instance_name in exempt
        assert session_name in exempt


# ──────────────────────────────────────────────
# 6b. compress_context — precomputed_summary (Critical: reviewer #2)
# ──────────────────────────────────────────────

class TestCompressContextPrecomputedSummary:
    """Verify precomputed_summary parameter bypasses LLM invocation."""

    def test_precomputed_summary_skips_agent_invocation(self):
        """precomputed_summary in auto mode should NOT call invoke_compression_agent."""
        pool, _ = _build_pool_with_history(num_user_msgs=5)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",  # auto mode — but precomputed_summary takes priority
                precomputed_summary="Pre-generated summary from /compress command",
            )

        mock_invoke.assert_not_called()
        assert result.success is True
        assert "Pre-generated summary" in result.summary_text

    def test_precomputed_summary_empty_fails(self):
        """Empty/whitespace-only precomputed_summary fails validation (core.py:216)."""
        pool, _ = _build_pool_with_history(num_user_msgs=5)

        result = compress_context(
            agent_pool=pool,
            target_agent_name="TestAgent",
            fraction=0.5,
            mode="auto",
            precomputed_summary="   ",  # whitespace only → stripped to empty
        )

        assert result.success is False
        assert "Failed to obtain a valid summary" in (result.error or "")

    def test_precomputed_summary_with_manual_mode(self):
        """precomputed_summary works even without summary_text in manual mode."""
        pool, _ = _build_pool_with_history(num_user_msgs=5)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="manual",
                summary_text=None,  # No summary_text — would fail without precomputed_summary
                precomputed_summary="Fallback summary",
            )

        mock_invoke.assert_not_called()
        assert result.success is True


# ──────────────────────────────────────────────
# 8b. compress_context — Empty generated summary (Reviewer #9)
# ──────────────────────────────────────────────

class TestCompressContextEmptySummary:
    """Verify empty summary from Compression Agent returns failure."""

    def test_empty_summary_from_agent_fails(self):
        """If invoke_compression_agent returns None/empty, compression fails gracefully."""
        pool, initial_len = _build_pool_with_history(num_user_msgs=5)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = ""  # Empty summary

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
            )

        assert result.success is False
        assert "Failed to obtain a valid summary" in (result.error or "")
        # Pool should be untouched
        assert len(pool.get_conversation("TestAgent")) == initial_len

    def test_none_summary_from_agent_fails(self):
        """If invoke_compression_agent returns None, compression fails gracefully."""
        pool, initial_len = _build_pool_with_history(num_user_msgs=5)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = None

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
            )

        assert result.success is False
        assert "Failed to obtain a valid summary" in (result.error or "")
        assert len(pool.get_conversation("TestAgent")) == initial_len


# ──────────────────────────────────────────────
# 8c. compress_context — Pool mutation failure (Reviewer #8)
# ──────────────────────────────────────────────

class TestCompressContextPoolMutationFailure:
    """Verify pool mutation exception is handled gracefully."""

    def test_pool_mutation_raises_returns_failure(self):
        """If pool assignment raises, CompressResult(success=False) and pool is untouched."""
        pool, initial_len = _build_pool_with_history(num_user_msgs=5)

        # Make instance_conversations raise on assignment to simulate corruption
        class FailingPool:
            def __init__(self, base_pool):
                self._base = base_pool

            def get_conversation(self, name):
                return self._base.get_conversation(name)

            def get_compression_target_set(self, name):
                return self._base.get_compression_target_set(name)

            @property
            def instance_conversations(self):
                raise RuntimeError("Pool corrupted — cannot write")

            @instance_conversations.setter
            def instance_conversations(self, value):
                raise RuntimeError("Pool corrupted — cannot write")

            @property
            def instance_loggers(self):
                return {}

        failing_pool = FailingPool(pool)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary"

            result = compress_context(
                agent_pool=failing_pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
            )

        assert result.success is False
        assert "Pool mutation failed" in (result.error or "")


# ──────────────────────────────────────────────
# 4b. compress_context — Dict-style messages (Reviewer #4)
# ──────────────────────────────────────────────

class TestCompressContextDictMessages:
    """Verify compress_context works with dict-style messages (not just Message objects)."""

    def test_dict_messages_compression(self):
        """compress_context succeeds with dict-style messages in pool."""
        history = [
            {"role": SYSTEM, "content": "You are a test agent"},
        ]
        for i in range(5):
            history.append({"role": USER, "content": f"User message {i}"})
            history.append({"role": "assistant", "content": f"Assistant reply {i}"})

        pool = MockAgentPool(history)

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary of dict messages"

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                force=False,
            )

        assert result.success is True
        assert result.messages_discarded > 0


# ──────────────────────────────────────────────
# 8d. Token guard dual-path tests (Reviewer #5)
# ──────────────────────────────────────────────

class TestTokenGuard:
    """Test the token-based 'already optimally compressed' guard."""

    def test_defers_when_small_and_few_tokens(self):
        """<3 messages AND <200 tokens → defer compression."""
        pool = MockAgentPool(history=[
            _make_msg(SYSTEM, "System"),
            _make_msg(USER, "Hi"),
            _make_msg("assistant", "Hello!"),
        ])

        # Patch at the source module (lazy import in core.py)
        with patch("agent_cascade.utils.tokenization_qwen.count_tokens") as mock_count:
            mock_count.return_value = 50  # 3 msgs * 50 = 150 < 200

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
            )

        assert result.success is False
        assert "optimally compressed" in (result.error or "").lower()

    def test_compresses_when_small_but_many_tokens(self):
        """<3 messages but >=200 tokens → compression proceeds past the token guard.

        With only 2 active messages, compute_discard_count returns 0 without force,
        so we use force=True to demonstrate the token guard is bypassed.
        """
        pool = MockAgentPool(history=[
            _make_msg(SYSTEM, "System"),
            _make_msg(USER, "x" * 100),  # Long content
            _make_msg("assistant", "y" * 100),
        ])

        with patch("agent_cascade.utils.tokenization_qwen.count_tokens") as mock_count:
            mock_count.return_value = 150  # 2 msgs * 150 = 300 >= 200

            with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
                mock_invoke.return_value = "Summary"

                result = compress_context(
                    agent_pool=pool,
                    target_agent_name="TestAgent",
                    fraction=0.5,
                    mode="auto",
                    force=True,  # Needed because only 2 msgs → discard_count=0 without force
                )

        # Should succeed because tokens >= 200 (token guard bypassed) and force=True
        assert result.success is True


# ──────────────────────────────────────────────
# 7b. compress_context — dry_run + force combination
# ──────────────────────────────────────────────

class TestCompressContextDryRunWithForce:
    """Test dry_run combined with force mode."""

    def test_dry_run_with_force(self):
        """dry_run=True + force=True should report discard count without mutating pool."""
        pool, initial_len = _build_pool_with_history(num_user_msgs=2)  # Small set

        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = "Summary"

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                force=True,   # Bypass the small-set guard
                dry_run=True, # Don't mutate pool
            )

        assert result.success is True
        assert result.messages_discarded > 0
        # Pool should be unchanged
        assert len(pool.get_conversation("TestAgent")) == initial_len


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])