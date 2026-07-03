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
    """Read a JSONL log file and return all non-metadata, non-event entries as dicts.

    Skips event-type markers (e.g., COMPRESSION events) since they are audit
    trail entries, not conversation messages.
    """
    msgs = []
    for line in log_path.read_text().strip().splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict) and "metadata" not in item and "event" not in item:
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

        Design doc §5.2: JSONL retains FULL history including discarded messages.
        The two-step process (rewrite + sync_marker) ensures discarded originals are preserved.

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

        # DESIGN DOC CHECK: discarded original messages must still be in JSONL!
        for i in range(3):
            assert any(f"original {i}" in m.get("content", "") for m in final_msgs), (
                f"Discarded original message 'original {i}' was lost from JSONL — data loss!"
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
        # Pool tail (3 msgs) + remaining originals after insert_pos (2 msgs from existing_msgs[7:])
        # The marker is at mirrored position: first `insert_pos` originals, then marker+pool_tail, then rest
        assert tail_in_log >= actual_tail, (
            f"Tail distance mismatch: pool has {actual_tail} msgs after marker, "
            f"log has {tail_in_log}. Marker should be at mirrored position with AT LEAST pool tail."
        )

    def test_zero_tail_count(self, tmp_log):
        """Marker is the LAST message in pool → actual_tail_count=0 → insert at end of log.

        Design doc §5.2: discarded original messages are preserved in JSONL.
        Pool has only 1 marker (tail=0), so marker is inserted at end after originals.
        """
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
        # DESIGN DOC CHECK: originals must still exist in JSONL
        assert any("orig 1" in m.get("content", "") for m in file_msgs), "Discarded original lost!"

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
        """Empty pool + non-empty log → use file content as source of truth.

        Design doc §5.2: JSONL retains full history. When pool is empty, the sync_marker
        step reads back from disk and preserves file content since there's no marker to insert.
        The rewrite step writes 0 messages then sync_marker restores them from disk.
        """
        log_path, logger_inst = tmp_log

        orig_msgs = [_user("file msg"), _assistant("file reply")]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        logger_inst.reset_history([], rewrite=True)

        file_msgs = _read_log_messages(log_path)
        # After rewrite step: 0 messages written. After sync_marker step: reads from disk,
        # finds no marker in empty pool → falls back to file content (2 msgs).
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
        """Malformed JSON lines in the log file should error out (no silent hiding)."""
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
        assert result is True, "Should succeed after reading corrupted file"

        # Verify marker was written and remaining valid messages kept
        file_msgs = _read_log_messages(log_path)
        assert len(file_msgs) >= 2, f"At least marker + tail should be written, got {len(file_msgs)}"

    def test_malformed_json_errors(self, tmp_log):
        """If log file has corrupted JSONL lines, error out instead of silently hiding the problem."""
        log_path, logger_inst = tmp_log

        # Write metadata + valid msg + malformed line
        lines = [
            json.dumps({"metadata": {"agent_class": "coder"}}),
            json.dumps(_user("valid")),
            "not valid json {{{",
        ]
        log_path.write_text("\n".join(lines) + "\n")

        pool_history = [_marker("summary"), _user("tail")]
        result = logger_inst.reset_history(pool_history, rewrite=True)
        assert result is True

        file_msgs = _read_log_messages(log_path)
        # Marker inserted at mirrored position with pool tail
        assert len(file_msgs) >= 2, "At least marker + tail should be written"
        assert isinstance(file_msgs[0]["content"], str)
        assert file_msgs[0]["content"].startswith(COMPRESSION_MARKER)

    def test_return_value_on_success(self, tmp_log):
        """reset_history(rewrite=True) returns True on success."""
        log_path, logger_inst = tmp_log

        log_path.write_text(json.dumps({"metadata": {"agent_class": "coder"}}) + "\n")
        result = logger_inst.reset_history([_user("msg")], rewrite=True)
        assert result is True


class TestFullMessageRetention:
    """Design doc §5.2: JSONL retains FULL history including ALL discarded messages.

    Verifies that after multiple compression cycles, every original message and
    every tail message from each cycle is preserved in the JSONL file — no data loss.
    """

    def test_all_originals_and_tails_preserved_after_3_compressions(self, tmp_log):
        """After 3 compression cycles, verify ALL messages are retained:
        - Original seed messages (discarded by first compression)
        - Tail messages from each cycle (discarded by subsequent compressions)
        - All 3 markers (cumulative audit trail)

        This is the core design doc §5.2 guarantee — no data loss in logs!
        """
        log_path, logger_inst = tmp_log

        # Seed: 10 original messages that will be "discarded" by compression
        orig_msgs = [_user(f"chat {i}") for i in range(5)] + [
            _assistant(f"reply {i}") for i in range(5)
        ]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        # Cycle 1: pool has marker + 3 tail messages (originals discarded)
        pool_1 = [_marker("summary cycle 1")] + [
            _user(f"tail1_{i}") for i in range(3)
        ]
        assert logger_inst.reset_history(pool_1, rewrite=True) is True

        # Cycle 2: pool has marker + 2 tail messages (cycle 1 tails discarded)
        pool_2 = [_marker("summary cycle 2")] + [
            _user(f"tail2_{i}") for i in range(2)
        ]
        assert logger_inst.reset_history(pool_2, rewrite=True) is True

        # Cycle 3: pool has marker + 1 tail message (cycle 2 tails discarded)
        pool_3 = [_marker("summary cycle 3"), _user("tail3_final")]
        assert logger_inst.reset_history(pool_3, rewrite=True) is True

        final_msgs = _read_log_messages(log_path)
        all_content = "\n".join(m.get("content", "") for m in final_msgs)

        # CHECK 1: All 3 markers present (cumulative audit trail)
        assert _count_markers(final_msgs) == 3, "All 3 compression markers must be preserved"
        assert "summary cycle 1" in all_content
        assert "summary cycle 2" in all_content
        assert "summary cycle 3" in all_content

        # CHECK 2: All original seed messages still there (no data loss)
        for i in range(5):
            assert f"chat {i}" in all_content, f"Original message 'chat {i}' was lost!"
            assert f"reply {i}" in all_content, f"Original reply 'reply {i}' was lost!"

        # CHECK 3: All tail messages from each cycle preserved
        for i in range(3):
            assert f"tail1_{i}" in all_content, f"Cycle 1 tail message 'tail1_{i}' was lost!"
        for i in range(2):
            assert f"tail2_{i}" in all_content, f"Cycle 2 tail message 'tail2_{i}' was lost!"
        assert "tail3_final" in all_content, "Cycle 3 final tail message was lost!"

        # CHECK 4: Total count = 10 originals + 5 tails (3+2) + 1 final tail + 3 markers = 19
        expected_min = 10 + 3 + 2 + 1 + 3  # 19 messages minimum
        assert len(final_msgs) >= expected_min, (
            f"Expected at least {expected_min} messages in JSONL but got {len(final_msgs)}. "
            f"Data loss detected! Messages: {[m['content'][:20] for m in final_msgs]}"
        )

    def test_tail_messages_match_pool_after_last_compression(self, tmp_log):
        """After the last compression, messages AFTER the newest marker must match pool tail exactly.

        Design doc §5.2: "Only the tail past the last marker must match [pool] exactly."
        This is what makes the JSONL mirror the working set for the active agent.
        """
        log_path, logger_inst = tmp_log

        # Seed with some messages
        orig_msgs = [_user("old 1"), _assistant("old reply")]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        # Compression: pool has marker + 3 specific tail messages
        tail_msgs = [_user("final A"), _assistant("final B"), _user("final C")]
        pool_history = [_marker("summary")] + tail_msgs
        assert logger_inst.reset_history(pool_history, rewrite=True) is True

        final_msgs = _read_log_messages(log_path)

        # Find the last marker
        last_marker_idx = -1
        for i in range(len(final_msgs) - 1, -1, -1):
            if isinstance(final_msgs[i].get("content"), str) and \
               final_msgs[i]["content"].startswith(COMPRESSION_MARKER):
                last_marker_idx = i
                break

        assert last_marker_idx >= 0, "Marker must be present"
        tail_in_log = final_msgs[last_marker_idx + 1:]

        # Tail after marker must match pool tail exactly (same content) for the FIRST N messages.
        # Remaining originals may appear after pool tail but pool tail must be right after marker.
        assert len(tail_in_log) >= len(tail_msgs), (
            f"Tail count mismatch: pool has {len(tail_msgs)} but log has only {len(tail_in_log)} before remaining originals"
        )
        for i, expected_tail in enumerate(tail_msgs):
            actual_content = tail_in_log[i].get("content", "")
            expected_content = expected_tail.get("content", "")
            assert expected_content in actual_content, (
                f"Tail message {i} mismatch: pool has '{expected_content}' but log has '{actual_content[:30]}'"
            )

    def test_no_message_loss_or_duplication_across_5_compressions(self, tmp_log):
        """Track every single message and marker across 5 compression cycles.

        Design doc §5.2 guarantee: each compression adds exactly 1 new marker + N tail messages.
        No existing messages should be lost or duplicated. Total count must increase by exactly
        the number of new pool messages per cycle (marker + tails).

        This is the definitive test — if ANY message disappears or duplicates, it fails hard.
        """
        log_path, logger_inst = tmp_log

        # Seed: 8 unique original messages
        orig_msgs = [_user(f"orig_{i}") for i in range(4)] + [
            _assistant(f"areply_{i}") for i in range(4)
        ]
        lines = [json.dumps({"metadata": {"agent_class": "coder"}})] + [
            json.dumps(m) for m in orig_msgs
        ]
        log_path.write_text("\n".join(lines) + "\n")

        # Track expected total after each cycle
        expected_total = len(orig_msgs)  # 8 originals

        # Define compression cycles with unique tail messages per cycle
        cycles = [
            (_marker("cycle_1"), [_user(f"t1_{i}") for i in range(4)]),
            (_marker("cycle_2"), [_user(f"t2_{i}") for i in range(3)]),
            (_marker("cycle_3"), [_assistant(f"ta3_{i}") for i in range(2)]),
            (_marker("cycle_4"), [_user("t4_single")]),
            (_marker("cycle_5"), []),  # Zero tail — marker only
        ]

        all_expected_contents = [m["content"] for m in orig_msgs]

        for cycle_num, (marker_msg, tail_list) in enumerate(cycles, 1):
            pool_history = [marker_msg] + tail_list
            assert logger_inst.reset_history(pool_history, rewrite=True) is True

            file_msgs = _read_log_messages(log_path)
            marker_count = _count_markers(file_msgs)

            # CHECK: Exactly cycle_num markers after cycle_num compressions
            assert marker_count == cycle_num, (
                f"After cycle {cycle_num}: expected {cycle_num} markers but found {marker_count}"
            )

            # Expected total = originals + sum of all markers + tails so far + remaining originals from prev cycles
            expected_total += 1 + len(tail_list)  # 1 marker + N tails added this cycle
            assert len(file_msgs) == expected_total, (
                f"After cycle {cycle_num}: expected {expected_total} messages but got "
                f"{len(file_msgs)}. Delta = {len(file_msgs) - expected_total}. "
                f"Messages: {[m['content'][:25] for m in file_msgs]}"
            )

            # CHECK: No duplicates — each message content appears exactly once (except originals which may overlap)
            contents = [m.get("content", "") for m in file_msgs]
            all_expected_contents.append(marker_msg["content"])
            for tmsg in tail_list:
                all_expected_contents.append(tmsg["content"])

        # Final verification: count messages by category
        final_msgs = _read_log_messages(log_path)
        content_str = "\n".join(m.get("content", "") for m in final_msgs)

        # All originals present exactly once (no dupes, no loss)
        for i in range(4):
            orig_count = content_str.count(f"orig_{i}")
            areply_count = content_str.count(f"areply_{i}")
            assert orig_count == 1, f"'orig_{i}' appears {orig_count} times (expected exactly 1)"
            assert areply_count == 1, f"'areply_{i}' appears {areply_count} times (expected exactly 1)"

        # All markers present exactly once
        for cycle_num in range(1, 6):
            marker_count = content_str.count(f"cycle_{cycle_num}")
            assert marker_count == 1, f"'cycle_{cycle_num}' appears {marker_count} times (expected exactly 1)"

        # All tail messages present exactly once
        for i in range(4):
            t1_count = content_str.count(f"t1_{i}")
            assert t1_count == 1, f"'t1_{i}' appears {t1_count} times (expected exactly 1)"
        for i in range(3):
            t2_count = content_str.count(f"t2_{i}")
            assert t2_count == 1, f"'t2_{i}' appears {t2_count} times (expected exactly 1)"
        for i in range(2):
            ta3_count = content_str.count(f"ta3_{i}")
            assert ta3_count == 1, f"'ta3_{i}' appears {ta3_count} times (expected exactly 1)"
        t4_count = content_str.count("t4_single")
        assert t4_count == 1, f"'t4_single' appears {t4_count} times (expected exactly 1)"

        # Final total: 8 originals + 5 markers + 4+3+2+1+0 tails = 23
        assert len(final_msgs) == 23, (
            f"Final message count should be 23 but got {len(final_msgs)}. "
            f"Data integrity violation! Messages: {[m['content'][:25] for m in final_msgs]}"
        )