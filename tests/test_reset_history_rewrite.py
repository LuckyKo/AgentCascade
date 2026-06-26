"""Tests for AgentInstanceLogger.reset_history(rewrite=True) — compression marker preservation.

Verifies that sequential compressions correctly preserve previous markers and insert new ones
at the mirrored position (tail distance from end matches pool conversation tail distance).

Covers:
 - Previous markers are preserved across multiple rewrite cycles (cumulative audit trail)
 - New marker inserted at correct mirrored position based on pool tail count
 - "No markers" fallback branch — uses pool state or file content as source of truth
 - Three compression cycles produce three distinct markers in the log

All tests are self-contained — no LLM or API server required.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest

from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger


# ──────────────────────────────────────────────
# Helper factories — dict-based messages
# ──────────────────────────────────────────────

def _user(content: str) -> dict:
    """Create a USER role message (dict format)."""
    return {"role": "user", "content": content}


def _assistant(content: str) -> dict:
    """Create an ASSISTANT role message."""
    return {"role": "assistant", "content": content}


def _marker(summary: str = "summarized context") -> dict:
    """Create a compression marker USER message using the standard template.

    Content starts with COMPRESSION_MARKER so reset_history can detect it.
    """
    return {
        "role": "user",
        "content": f"{COMPRESSION_MARKER} (header) ---\n<context_summary>\n{summary}\n</context_summary>",
    }


# ──────────────────────────────────────────────
# Fixture: create a logger backed by a temp file
# ──────────────────────────────────────────────

@pytest.fixture
def tmp_log(tmp_path):
    """Return (log_path, AgentInstanceLogger) for a fresh temp log file.

    The logger is created with an externally-provided log_path so that _initial_save()
    skips writing metadata on its own — we control the exact file content ourselves.
    """
    log_file = tmp_path / "test_logger.jsonl"
    logger_inst = AgentInstanceLogger(
        agent_class="coder",
        instance_name="test_worker",
        log_dir=str(tmp_path),
        log_path=str(log_file),
    )
    return log_file, logger_inst


# ──────────────────────────────────────────────
# Helper: read log file and return list of message dicts (skip metadata line)
# ──────────────────────────────────────────────

def _read_log_messages(log_path: Path) -> list:
    """Read a JSONL log file and return all non-metadata entries as dicts."""
    msgs = []
    for line in log_path.read_text().strip().splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict) and "metadata" not in item:
            msgs.append(item)
    return msgs


# ──────────────────────────────────────────────
# Helper: count markers in a list of message dicts
# ──────────────────────────────────────────────

def _count_markers(msgs: list) -> int:
    """Count how many messages contain COMPRESSION_MARKER in their content."""
    return sum(1 for m in msgs if isinstance(m.get("content"), str) and m["content"].startswith(COMPRESSION_MARKER))


# ──────────────────────────────────────────────
# Helper: verify _format_message added timestamps to written messages
# ──────────────────────────────────────────────

def _assert_has_timestamps(msgs: list):
    """Assert every message in the log has a timestamp (added by _format_message)."""
    for i, m in enumerate(msgs):
        assert "timestamp" in m, f"Message at index {i} missing 'timestamp' field from _format_message"


# ──────────────────────────────────────────────
# 1. Previous compression markers are preserved (parameterized)
# ──────────────────────────────────────────────

class TestPreviousMarkersPreserved:
    """After sequential forced compressions, the log should contain ALL markers cumulatively."""

    @pytest.mark.parametrize("num_cycles", [2, 3])
    def test_cumulative_markers(self, tmp_log, num_cycles):
        """Each reset_history(rewrite=True) adds one new marker while preserving old ones.

        Parameterized for 2 and 3 cycles to cover both the original test_two_compressions
        and the three-cycle scenario in a single parameterized test (eliminates redundancy).
        """
        log_path, logger_inst = tmp_log

        # Seed the log file with pre-existing messages (simulating original conversation)
        orig_msgs = [_user(f"original {i}") for i in range(3)] + [
            _assistant(f"orig reply {i}") for i in range(2)
        ]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        # Run sequential compression cycles
        for cycle in range(1, num_cycles + 1):
            pool_history = [_marker(f"summary round {cycle}"), _user(f"msg{cycle}")]
            result = logger_inst.reset_history(pool_history, rewrite=True)

            file_msgs = _read_log_messages(log_path)
            assert _count_markers(file_msgs) == cycle, (
                f"After cycle {cycle}, expected {cycle} markers but got {_count_markers(file_msgs)}"
            )
            # Verify return value is True on success
            assert result is True, "reset_history should return True on success"

        # Final verification: all marker summaries are preserved in the file
        final_msgs = _read_log_messages(log_path)
        all_content = "\n".join(m.get("content", "") for m in final_msgs)
        for cycle in range(1, num_cycles + 1):
            assert f"summary round {cycle}" in all_content, (
                f"Marker summary from cycle {cycle} should be preserved in the log"
            )


# ──────────────────────────────────────────────
# 2. New marker inserted at correct mirrored position
# ──────────────────────────────────────────────

class TestMarkerMirroredPosition:
    """The new marker should be placed so that tail distance from end matches pool state."""

    def test_marker_position_mirrors_pool_tail(self, tmp_log):
        """If pool has 3 messages after the marker, log should also have ~3 msgs after it."""
        log_path, logger_inst = tmp_log

        # Seed log with 10 original messages
        orig_msgs = [_user(f"orig {i}") for i in range(5)] + [_assistant(f"reply {i}") for i in range(5)]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        # Pool history: marker + 3 tail messages (tail_count = 3)
        pool_history = [_marker("summary"), _user("t1"), _assistant("r1"), _user("t2")]
        assert len(pool_history) == 4
        actual_tail = len(pool_history) - 1  # 3 messages after marker

        result = logger_inst.reset_history(pool_history, rewrite=True)
        assert result is True, "reset_history should return True on success"

        file_msgs = _read_log_messages(log_path)

        # Find the LAST (newest) marker in the log
        last_marker_idx = -1
        for i in range(len(file_msgs) - 1, -1, -1):
            if isinstance(file_msgs[i].get("content"), str) and file_msgs[i]["content"].startswith(COMPRESSION_MARKER):
                last_marker_idx = i
                break

        assert last_marker_idx >= 0, "Marker should be present in the log"
        tail_in_log = len(file_msgs) - last_marker_idx - 1
        assert tail_in_log == actual_tail, (
            f"Tail distance mismatch: pool has {actual_tail} msgs after marker, "
            f"log has {tail_in_log}. Marker should be at mirrored position."
        )

    def test_zero_tail_count(self, tmp_log):
        """Marker is the LAST message in pool → actual_tail_count=0 → insert at end of log."""
        log_path, logger_inst = tmp_log

        orig_msgs = [_user("orig 1"), _assistant("reply 1")]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        # Pool: only the marker, no tail messages → actual_tail_count = 0
        pool_history = [_marker("summary")]
        logger_inst.reset_history(pool_history, rewrite=True)

        file_msgs = _read_log_messages(log_path)
        assert len(file_msgs) == 3, "2 original msgs + 1 new marker"
        # Marker should be the LAST message (inserted at end since tail=0)
        assert isinstance(file_msgs[-1]["content"], str), "Last msg should be the marker"
        assert file_msgs[-1]["content"].startswith(COMPRESSION_MARKER)

    def test_tail_larger_than_file_clamping(self, tmp_log):
        """Pool tail > total existing msgs → insert_pos clamped to 0 (marker at front).

        Lines 481-482: insert_pos = max(0, min(insert_pos, len(existing_msgs)))
        """
        log_path, logger_inst = tmp_log

        # Only 2 messages in the file
        orig_msgs = [_user("orig")]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        # Pool: marker + 5 tail messages → actual_tail_count = 5 > len(existing_msgs)=1
        pool_history = [_marker("summary")] + [_user(f"tail {i}") for i in range(5)]
        logger_inst.reset_history(pool_history, rewrite=True)

        file_msgs = _read_log_messages(log_path)
        # Marker should be at position 0 (clamped), followed by the original message
        assert isinstance(file_msgs[0]["content"], str), "First msg should be the marker"
        assert file_msgs[0]["content"].startswith(COMPRESSION_MARKER), "Marker clamped to front"

    def test_empty_file_with_marker(self, tmp_log):
        """File has no messages → marker inserted at position 0."""
        log_path, logger_inst = tmp_log

        # Write only metadata — no message content in file
        log_path.write_text(json.dumps({"metadata": {"agent_class": "coder"}}) + "\n")

        pool_history = [_marker("summary"), _user("tail msg")]
        logger_inst.reset_history(pool_history, rewrite=True)

        file_msgs = _read_log_messages(log_path)
        # With 1 original msg (0 existing), tail_count=1 → insert_pos = max(0, 0-1) = 0
        assert len(file_msgs) >= 1
        # Marker should be present somewhere in the log
        assert _count_markers(file_msgs) == 1


# ──────────────────────────────────────────────
# 3. "No markers" fallback branch
# ──────────────────────────────────────────────

class TestNoMarkersFallback:
    """When new_history has no compression markers, the correct fallback is used."""

    def test_no_marker_uses_pool_state(self, tmp_log):
        """Pool history without markers → log content matches pool state.

        Also verifies _format_message adds timestamps to written messages.
        """
        log_path, logger_inst = tmp_log

        # Seed log with different messages than pool (to verify pool wins)
        orig_msgs = [_user("file original 1"), _assistant("file reply")]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        # Pool has no markers — just plain messages
        pool_history = [_user("pool msg 1"), _assistant("pool reply 1")]

        result = logger_inst.reset_history(pool_history, rewrite=True)
        assert result is True

        file_msgs = _read_log_messages(log_path)
        assert len(file_msgs) == 2, "Log should have exactly the pool's messages"
        assert file_msgs[0]["content"] == "pool msg 1", "First message from pool state"
        assert file_msgs[1]["content"] == "pool reply 1", "Second message from pool state"

        # Verify _format_message added timestamps to all written messages
        _assert_has_timestamps(file_msgs)

    def test_no_marker_empty_pool_falls_back_to_file(self, tmp_log):
        """Empty pool + non-empty log → use file content as source of truth."""
        log_path, logger_inst = tmp_log

        orig_msgs = [_user("file msg"), _assistant("file reply")]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        logger_inst.reset_history([], rewrite=True)

        file_msgs = _read_log_messages(log_path)
        assert len(file_msgs) == 2, "File content should be preserved when pool is empty"
        assert file_msgs[0]["content"] == "file msg"
        _assert_has_timestamps(file_msgs)

    def test_both_pool_and_file_empty(self, tmp_log):
        """Both pool and file empty → log has zero messages."""
        log_path, logger_inst = tmp_log

        # Write only metadata line (no message content)
        log_path.write_text(json.dumps({"metadata": {"agent_class": "coder"}}) + "\n")

        logger_inst.reset_history([], rewrite=True)

        file_msgs = _read_log_messages(log_path)
        assert len(file_msgs) == 0, "Both empty → no messages"


# ──────────────────────────────────────────────
# 4. Internal state verification after reset_history
# ──────────────────────────────────────────────

class TestInternalStateAfterReset:
    """Verify internal tracking is updated correctly by reset_history(rewrite=True)."""

    def test_data_history_updated(self, tmp_log):
        """self.data['history'] should reflect pool state after rewrite."""
        log_path, logger_inst = tmp_log

        orig_msgs = [_user("orig")]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        pool_history = [_marker("summary"), _user("new msg")]
        logger_inst.reset_history(pool_history, rewrite=True)

        # Internal history should match pool state (not file content which has more msgs)
        assert len(logger_inst.data["history"]) == 2, (
            "data['history'] should reflect compressed pool size"
        )
        assert logger_inst.data["history"][0]["content"].startswith(COMPRESSION_MARKER)

    def test_file_history_synced_flag_set(self, tmp_log):
        """_file_history_synced flag should be True after rewrite=True."""
        log_path, logger_inst = tmp_log

        orig_msgs = [_user("orig")]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        logger_inst.reset_history([_marker("s"), _user("m")], rewrite=True)

        assert logger_inst._file_history_synced is True, (
            "_file_history_synced should be set to True after rewrite to prevent redundant file reloads"
        )


# ──────────────────────────────────────────────
# 5. Exception and edge case handling
# ──────────────────────────────────────────────

class TestExceptionHandling:
    """Verify graceful error handling in reset_history(rewrite=True)."""

    def test_malformed_json_in_log(self, tmp_log):
        """Malformed JSON lines in the log file should be skipped gracefully."""
        log_path, logger_inst = tmp_log

        # Write valid metadata + 2 messages + a malformed line
        lines = [
            json.dumps({"metadata": {"agent_class": "coder"}}),
            json.dumps(_user("valid msg")),
            "not valid json {{{",  # Malformed line
            json.dumps(_assistant("reply")),
        ]
        log_path.write_text("\n".join(lines) + "\n")

        pool_history = [_marker("summary"), _user("tail")]
        result = logger_inst.reset_history(pool_history, rewrite=True)
        assert result is True

        file_msgs = _read_log_messages(log_path)
        # Should have: 2 original valid msgs (skipping malformed) + 1 new marker = 3
        assert len(file_msgs) == 3, "Malformed JSON should be skipped"

    def test_file_not_exists(self, tmp_log):
        """If log file doesn't exist, existing_msgs is empty — marker inserted at position 0.

        With pool = [marker, user_msg] and no existing msgs:
          actual_tail_count = 1, insert_pos = max(0, 0 - 1) = 0
          result = [] + [marker] + [] = [marker] (only the marker is written since
          there are no existing messages to mirror against).
        """
        log_path, logger_inst = tmp_log

        # Delete the log file entirely
        if log_path.exists():
            log_path.unlink()

        pool_history = [_marker("summary"), _user("msg")]
        result = logger_inst.reset_history(pool_history, rewrite=True)
        assert result is True

        file_msgs = _read_log_messages(log_path)
        # With no existing msgs and marker present: only the marker is written (inserted at pos 0 into empty list)
        assert len(file_msgs) == 1, "Only marker should be written when file has no messages"
        assert isinstance(file_msgs[0]["content"], str)
        assert file_msgs[0]["content"].startswith(COMPRESSION_MARKER)

    def test_return_value_on_success(self, tmp_log):
        """reset_history(rewrite=True) returns True on success."""
        log_path, logger_inst = tmp_log

        log_path.write_text(json.dumps({"metadata": {"agent_class": "coder"}}) + "\n")
        result = logger_inst.reset_history([_user("msg")], rewrite=True)
        assert result is True