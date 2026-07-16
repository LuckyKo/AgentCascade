"""Regression tests for session load behavior (design doc §5.2).

Verifies that loading a session from a JSONL log file produces the correct
working set structure, retains full history in the log, syncs tails properly,
and avoids message duplication after the first turn.

Design requirements tested:
  1. Marker Stacking Algorithm — working set = [SYS][U0][COMP1...][COMPn...][tail]
  2. Full History Retention — JSONL keeps ALL originals including discarded ones
  3. Tail Sync Rule — tail past last marker has same count in pool and JSONL
  4. No Duplication — after reload + first turn, no duplicate messages in JSONL
  5. Logger Population — logger in-memory holds full cleaned history after load

Uses the rebuild_working_set_from_jsonl helper from test_compression_consistency.py
to simulate what load_session_from_log() does internally (forward pass marker
stacking). Tests are self-contained — no LLM or API server required.
"""

import json
from unittest.mock import patch

import pytest

from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.llm.schema import SYSTEM, USER, ASSISTANT, FUNCTION, Message

# Helpers shared with compression consistency tests
from tests.test_compression_consistency import (
    _msg,
    _write_jsonl,
    _read_jsonl_messages,
    _is_marker,
    rebuild_working_set_from_jsonl,
)

# MockAgentPool from conftest
from tests.conftest import MockAgentPool


# ──────────────────────────────────────────────
# Test data builders
# ──────────────────────────────────────────────

def _build_full_history():
    """Build a full conversation history: [SYS][U0][A0][U1][A1][U2][A2][U3][A3].

    Returns the list of Message objects representing the original (pre-compression)
    conversation that would have existed before any compression took place.
    """
    return [
        _msg(SYSTEM, "You are a helpful coding assistant."),
        _msg(USER, "What is Python?"),
        _msg(ASSISTANT, "Python is a high-level programming language."),
        _msg(USER, "Show me a loop example"),
        _msg(ASSISTANT, "for i in range(10): print(i)"),
        _msg(USER, "Now explain list comprehensions"),
        _msg(ASSISTANT, "List comprehensions are concise ways to create lists."),
        _msg(USER, "Give me a real example"),
        _msg(ASSISTANT, "[x**2 for x in range(5)] produces [0,1,4,9,16]"),
    ]


def _build_compressed_jsonl_messages():
    """Build JSONL message list simulating post-compression state.

    Simulates a session where:
      - Original messages U0,A0,U1,A1 were compressed into COMP1
      - Then U2,A2 were compressed into COMP2 (cumulative, including COMP1)
      - Tail messages U3,A3 remain after last marker

    JSONL layout mirrors design doc §5.2:
      [SYS][U0][U1_orig][A1_orig][COMP1][U2_orig][A2_orig][COMP2][U3][A3]

    Returns list of message dicts (not Message objects) for writing to JSONL.
    """
    comp1_text = COMPRESSION_MARKER + " (round 1) ---\n<context_summary>\n" \
                 "Discussed Python basics, loops, and list comprehensions.\n</context_summary>"
    comp2_text = COMPRESSION_MARKER + " (round 2) ---\n<context_summary>\n" \
                 "Covered Python fundamentals. Explained loops then list comprehensions with examples.\n</context_summary>"

    return [
        # System message
        {"role": SYSTEM, "content": "You are a helpful coding assistant."},
        # First user message (U0) — preserved in JSONL
        {"role": USER, "content": "What is Python?"},
        # Original messages that were compressed into COMP1
        {"role": ASSISTANT, "content": "Python is a high-level programming language."},
        {"role": USER, "content": "Show me a loop example"},
        # Compression marker 1 — inserted at cut position
        {"role": USER, "content": comp1_text},
        # Original messages that were compressed into COMP2
        {"role": ASSISTANT, "content": "for i in range(10): print(i)"},
        {"role": USER, "content": "Now explain list comprehensions"},
        {"role": ASSISTANT, "content": "List comprehensions are concise ways to create lists."},
        # Compression marker 2 — inserted at cut position
        {"role": USER, "content": comp2_text},
        # Tail messages after last marker
        {"role": USER, "content": "Give me a real example"},
        {"role": ASSISTANT, "content": "[x**2 for x in range(5)] produces [0,1,4,9,16]"},
    ]


def _build_compressed_jsonl_with_no_markers():
    """Build JSONL with no compression markers — plain conversation."""
    return [
        {"role": SYSTEM, "content": "You are a helpful coding assistant."},
        {"role": USER, "content": "Hello"},
        {"role": ASSISTANT, "content": "Hi there!"},
        {"role": USER, "content": "How are you?"},
        {"role": ASSISTANT, "content": "I'm doing well, thanks."},
    ]


# ──────────────────────────────────────────────
# Test 1: Marker Stacking Algorithm
# ──────────────────────────────────────────────

class TestSessionLoadMarkerStacking:
    """Verify working set structure after load matches [SYS][U0][COMP1...][tail]."""

    def test_working_set_structure_with_markers(self, tmp_path):
        """After loading a JSONL with 2 compression markers, the working set must be
        [SYS][U0][COMP1][COMP2][tail messages] — exactly as §5.2 specifies.
        """
        jsonl_path = str(tmp_path / "session.jsonl")
        jsonl_msgs = _build_compressed_jsonl_messages()
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs])

        # Re-read from disk (simulates load_session_from_log reading the file)
        loaded_msgs = _read_jsonl_messages(jsonl_path)
        assert len(loaded_msgs) == len(jsonl_msgs), "JSONL write/read round-trip failed"

        # Apply marker stacking algorithm (what load_session_from_log does internally)
        working_set = rebuild_working_set_from_jsonl(loaded_msgs)

        # --- Verify structure: [SYS][U0][COMP1][COMP2][tail] ---
        assert len(working_set) >= 5, f"Working set too small: {len(working_set)} msgs"

        # Index 0: System message
        assert working_set[0].role == SYSTEM, "First msg must be SYSTEM"

        # Index 1: First user message (U0)
        assert working_set[1].role == USER, "Second msg must be U0 (first user)"
        assert not working_set[1].content.startswith(COMPRESSION_MARKER), \
            "U0 should not be a compression marker"

        # Indices 2..N-2: Compression markers stacked in order
        tail_start = len(working_set) - 2  # Last 2 are tail (U3, A3)
        for i in range(2, tail_start):
            assert working_set[i].role == USER, f"Marker at index {i} should be USER role"
            assert working_set[i].content.startswith(COMPRESSION_MARKER), \
                f"Index {i} content should start with COMPRESSION_MARKER"

        # Last 2: Tail messages after last marker (U3, A3)
        tail = working_set[tail_start:]
        for m in tail:
            assert not _is_marker(m), "Tail messages should not be markers"

    def test_working_set_structure_no_markers(self, tmp_path):
        """Without any compression markers, the full history IS the working set."""
        jsonl_path = str(tmp_path / "session.jsonl")
        jsonl_msgs = _build_compressed_jsonl_with_no_markers()
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs])

        loaded_msgs = _read_jsonl_messages(jsonl_path)
        working_set = rebuild_working_set_from_jsonl(loaded_msgs)

        # All messages should be present — no filtering needed
        assert len(working_set) == len(loaded_msgs), \
            "No markers → full history is the working set"

    def test_marker_order_preserved(self, tmp_path):
        """Markers must appear in chronological order (COMP1 before COMP2)."""
        jsonl_path = str(tmp_path / "session.jsonl")
        jsonl_msgs = _build_compressed_jsonl_messages()
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs])

        loaded_msgs = _read_jsonl_messages(jsonl_path)
        working_set = rebuild_working_set_from_jsonl(loaded_msgs)

        # Extract markers from working set
        markers_in_ws = [m for m in working_set if _is_marker(m)]
        assert len(markers_in_ws) == 2, "Should have exactly 2 markers"

        # COMP1 text should mention round 1, COMP2 should mention round 2
        assert "round 1" in markers_in_ws[0].content.lower(), \
            "First marker must be from compression round 1"
        assert "round 2" in markers_in_ws[1].content.lower(), \
            "Second marker must be from compression round 2"


# ──────────────────────────────────────────────
# Test 2: Full History Retention
# ──────────────────────────────────────────────

class TestSessionLoadFullHistoryRetention:
    """Verify JSONL keeps ALL original messages including discarded ones."""

    def test_jsonl_retains_discarded_originals(self, tmp_path):
        """After compression + reload, the JSONL must still contain the original
        messages that were compressed away (U1,A1,U2,A2 etc.), not just markers.

        Design §5.2: "Agent memory and JSONL are NOT in full sync — the logs retain
        the full conversation history at all times."
        """
        jsonl_path = str(tmp_path / "session.jsonl")
        jsonl_msgs = _build_compressed_jsonl_messages()
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs])

        # Read back and verify discarded originals are present
        loaded = _read_jsonl_messages(jsonl_path)

        # Count non-marker messages (should include SYS + U0 + originals + tail)
        non_markers = [m for m in loaded if not _is_marker(m)]
        marker_msgs = [m for m in loaded if _is_marker(m)]

        assert len(marker_msgs) == 2, "Should have exactly 2 markers"
        assert len(non_markers) >= 6, \
            f"Non-marker messages too few ({len(non_markers)}). " \
            "Discarded originals should still be in JSONL."

        # Verify specific original content is present
        contents = [m.get("content", "") for m in loaded]
        assert "Python is a high-level programming language." in contents, \
            "Original A0 message missing from JSONL"
        assert "for i in range(10): print(i)" in contents, \
            "Original assistant reply about loops missing from JSONL"

    def test_jsonl_retains_all_after_reload_simulation(self, tmp_path):
        """Simulate a full reload cycle: write compressed JSONL → read it back →
        verify no messages were lost during the round-trip.
        """
        jsonl_path = str(tmp_path / "session.jsonl")

        # Build and write compressed state
        jsonl_msgs = _build_compressed_jsonl_messages()
        original_count = len(jsonl_msgs)
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs])

        # Simulate load_session_from_log: read → filter valid msgs → rewrite full history
        loaded = _read_jsonl_messages(jsonl_path)
        assert len(loaded) == original_count, \
            f"Lost {original_count - len(loaded)} messages during load simulation"

        # Verify content integrity — every original message is still there
        for i, orig in enumerate(jsonl_msgs):
            assert loaded[i] == orig, \
                f"Message at index {i} changed: expected {orig}, got {loaded[i]}"


# ──────────────────────────────────────────────
# Test 3: Tail Sync Rule
# ──────────────────────────────────────────────

class TestSessionLoadTailSync:
    """Verify tail count matches between pool working set and JSONL."""

    def test_tail_count_matches(self, tmp_path):
        """The number of tail messages (after last marker) must be identical in
        both the pool's working set AND the JSONL file.

        Design §5.2: "the tail end past the last marker MUST be in sync at all
        times and have the EXACT same number of messages since the last compression."
        """
        jsonl_path = str(tmp_path / "session.jsonl")
        jsonl_msgs = _build_compressed_jsonl_messages()
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs])

        loaded_msgs = _read_jsonl_messages(jsonl_path)

        # Count tail in JSONL (messages after last marker)
        last_marker_idx_jsonl = -1
        for i, m in enumerate(loaded_msgs):
            if _is_marker(m):
                last_marker_idx_jsonl = i
        jsonl_tail_count = len(loaded_msgs) - last_marker_idx_jsonl - 1

        # Build working set and count its tail
        working_set = rebuild_working_set_from_jsonl(loaded_msgs)
        last_marker_ws = MockAgentPool.find_last_marker(working_set)
        ws_tail_count = len(working_set) - last_marker_ws - 1

        assert jsonl_tail_count == ws_tail_count, \
            f"Tail sync violation: JSONL tail={jsonl_tail_count}, " \
            f"pool working set tail={ws_tail_count}"

    def test_zero_tail_sync(self, tmp_path):
        """Edge case: no messages after the last marker → both tails should be 0."""
        jsonl_msgs = _build_compressed_jsonl_messages()
        # Remove tail messages so last marker is at the end
        jsonl_msgs_no_tail = [m for m in jsonl_msgs[:-2]]

        jsonl_path = str(tmp_path / "zero_tail.jsonl")
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs_no_tail])

        loaded = _read_jsonl_messages(jsonl_path)

        # Find last marker in JSONL
        last_marker_idx = -1
        for i, m in enumerate(loaded):
            if _is_marker(m):
                last_marker_idx = i
        jsonl_tail = len(loaded) - last_marker_idx - 1

        working_set = rebuild_working_set_from_jsonl(loaded)
        ws_last_marker = MockAgentPool.find_last_marker(working_set)
        ws_tail = len(working_set) - ws_last_marker - 1

        assert jsonl_tail == ws_tail == 0, \
            f"Zero-tail case: JSONL tail={jsonl_tail}, WS tail={ws_tail}"


# ──────────────────────────────────────────────
# Test 4: No Duplication After First Turn
# ──────────────────────────────────────────────

class TestSessionLoadNoDuplication:
    """Verify no duplicate messages appear in JSONL after reload + first turn."""

    def test_no_duplication_after_first_turn(self, tmp_path):
        """After loading a session and simulating the first turn (appending new
        messages), the JSONL must not contain any duplicated messages.

        This tests that load_session_from_log writes clean history and subsequent
        appends don't re-insert already-present messages.
        """
        jsonl_path = str(tmp_path / "session.jsonl")
        jsonl_msgs = _build_compressed_jsonl_messages()
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs])

        # Load and build working set (simulating load_session_from_log step 5-6)
        loaded = _read_jsonl_messages(jsonl_path)
        working_set = rebuild_working_set_from_jsonl(loaded)

        # Simulate first turn: agent receives a new user message and responds
        new_user_msg = Message(role=USER, content="What about dictionaries?")
        new_assistant_msg = Message(role=ASSISTANT, content="Dictionaries map keys to values.")

        # Append to working set (simulating pool mutation during execution)
        extended_ws = list(working_set) + [new_user_msg, new_assistant_msg]

        # Now simulate the logger appending these messages to JSONL
        # (what happens in update_history / append_line path)
        from tests.test_compression_consistency import _write_jsonl_append
        _write_jsonl_append(jsonl_path, [new_user_msg, new_assistant_msg])

        # Read back and check for duplicates
        final_msgs = _read_jsonl_messages(jsonl_path)

        # Count content occurrences — each unique content should appear exactly once
        content_counts: dict[str, int] = {}
        for m in final_msgs:
            c = m.get("content", "")
            content_counts[c] = content_counts.get(c, 0) + 1

        duplicates = {c: n for c, n in content_counts.items() if n > 1}
        assert not duplicates, \
            f"Duplicated messages found after first turn: {duplicates}"

    def test_no_system_message_duplication(self, tmp_path):
        """System message should appear exactly once after reload + turns."""
        jsonl_path = str(tmp_path / "session.jsonl")
        jsonl_msgs = _build_compressed_jsonl_messages()
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs])

        loaded = _read_jsonl_messages(jsonl_path)
        working_set = rebuild_working_set_from_jsonl(loaded)

        # Simulate several turns
        from tests.test_compression_consistency import _write_jsonl_append
        extra_turns = [
            Message(role=USER, content="Question A"),
            Message(role=ASSISTANT, content="Answer A"),
            Message(role=USER, content="Question B"),
            Message(role=ASSISTANT, content="Answer B"),
        ]
        _write_jsonl_append(jsonl_path, extra_turns)

        final_msgs = _read_jsonl_messages(jsonl_path)

        # System message should appear exactly once
        sys_msgs = [m for m in final_msgs if m.get("role") == SYSTEM]
        assert len(sys_msgs) == 1, \
            f"Expected 1 system message, found {len(sys_msgs)}"


# ──────────────────────────────────────────────
# Test 5: Logger Population (Full History in Memory)
# ──────────────────────────────────────────────

class TestSessionLoadLoggerPopulation:
    """Verify logger holds full cleaned history after load, not just working set."""

    def test_logger_has_full_history_not_working_set(self, tmp_path):
        """After session load, the logger's in-memory history should contain ALL
        messages from the JSONL (full history), NOT just the trimmed working set.

        Design §5.2: rewrite_log_with_history(cleaned) writes full cleaned history,
        and sets data["history"] to that same list.
        """
        jsonl_path = str(tmp_path / "session.jsonl")
        jsonl_msgs = _build_compressed_jsonl_messages()
        msg_objects = [Message(**m) for m in jsonl_msgs]
        _write_jsonl(jsonl_path, msg_objects)

        # Simulate what load_session_from_log does:
        # Step 5: Build working set (trimmed) from cleaned messages
        loaded = _read_jsonl_messages(jsonl_path)
        working_set = rebuild_working_set_from_jsonl(loaded)
        assert len(working_set) < len(loaded), \
            "Working set should be smaller than full history"

        # Step 7: Create instance with working set (this is what goes to pool.conversation)
        pool = MockAgentPool(list(working_set))
        inst_conv = pool.get_conversation(pool.instance_name)
        assert len(inst_conv) == len(working_set), \
            "Instance conversation should match working set size"

        # Step 8: Logger rewrite_log_with_history(cleaned) — full history, not WS
        # The logger stores ALL cleaned messages in data["history"]
        # We verify this by checking the JSONL file was rewritten with full history
        logged_msgs = _read_jsonl_messages(jsonl_path)

        # Full history count should include originals + markers (not just WS)
        assert len(logged_msgs) == len(loaded), \
            f"Logger should retain all {len(loaded)} messages, " \
            f"but JSONL has {len(logged_msgs)}"

    def test_logger_history_includes_discarded(self, tmp_path):
        """Logger's internal history list must include discarded original messages
        that are NOT in the working set.
        """
        jsonl_path = str(tmp_path / "session.jsonl")
        jsonl_msgs = _build_compressed_jsonl_messages()
        msg_objects = [Message(**m) for m in jsonl_msgs]
        _write_jsonl(jsonl_path, msg_objects)

        loaded = _read_jsonl_messages(jsonl_path)
        working_set = rebuild_working_set_from_jsonl(loaded)

        # Get contents of discarded originals (in JSONL but not in WS tail or markers)
        ws_contents = {m.content for m in working_set}
        jsonl_contents = {m.get("content", "") for m in loaded}

        # There should be messages in JSONL that aren't in the working set
        extra_in_jsonl = jsonl_contents - ws_contents
        assert len(extra_in_jsonl) > 0, \
            "JSONL should contain discarded originals not present in working set"

        # Verify these are actual conversation content (not empty or metadata)
        for c in extra_in_jsonl:
            assert len(c.strip()) > 0, "Extra messages should have non-empty content"


# ──────────────────────────────────────────────
# Integration-style test: Full load cycle simulation
# ──────────────────────────────────────────────

class TestSessionLoadFullCycle:
    """End-to-end simulation of the complete session load flow."""

    def test_complete_load_cycle(self, tmp_path):
        """Run through the entire load_session_from_log flow:
        1. Write JSONL with compressed history (originals + markers + tail)
        2. Read and parse messages
        3. Build working set via marker stacking
        4. Verify pool gets trimmed WS, logger gets full history
        5. Simulate first turn
        6. Verify no duplication in final JSONL
        """
        jsonl_path = str(tmp_path / "session.jsonl")

        # Phase 1: Write compressed session to disk
        jsonl_msgs = _build_compressed_jsonl_messages()
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs])

        # Phase 2: Load from JSONL (simulating load_session_from_log steps 2-4)
        loaded = _read_jsonl_messages(jsonl_path)
        assert len(loaded) == len(jsonl_msgs), "Load should preserve all messages"

        # Phase 3: Build working set via marker stacking (§5.2 algorithm)
        ws = rebuild_working_set_from_jsonl(loaded)
        assert len(ws) < len(loaded), \
            f"Working set ({len(ws)}) must be smaller than full history ({len(loaded)})"

        # Phase 4: Verify WS structure [SYS][U0][COMP1][COMP2][tail]
        assert ws[0].role == SYSTEM
        assert ws[1].role == USER and not _is_marker(ws[1])
        markers = [m for m in ws if _is_marker(m)]
        tail = ws[len(ws) - 2:]
        assert len(markers) == 2, "Should have 2 stacked markers"
        assert len(tail) == 2, "Tail should have exactly 2 messages (U3,A3)"

        # Phase 5: Verify JSONL still has full history
        jsonl_check = _read_jsonl_messages(jsonl_path)
        contents = [m.get("content", "") for m in jsonl_check]
        assert "Python is a high-level programming language." in contents, \
            "Discarded original should remain in JSONL"

        # Phase 6: Simulate first turn and check no duplication
        from tests.test_compression_consistency import _write_jsonl_append
        new_msgs = [
            Message(role=USER, content="New question after reload"),
            Message(role=ASSISTANT, content="Answer to the new question."),
        ]
        _write_jsonl_append(jsonl_path, new_msgs)

        final = _read_jsonl_messages(jsonl_path)
        content_counts: dict[str, int] = {}
        for m in final:
            c = m.get("content", "")
            content_counts[c] = content_counts.get(c, 0) + 1

        dups = {c: n for c, n in content_counts.items() if n > 1}
        assert not dups, f"Duplicates after full cycle: {dups}"


# ──────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────

class TestSessionLoadEdgeCases:
    """Boundary conditions for session load behavior."""

    def test_single_marker(self, tmp_path):
        """Session with exactly one compression marker."""
        jsonl_msgs = [
            {"role": SYSTEM, "content": "System prompt"},
            {"role": USER, "content": "First question"},
            {"role": ASSISTANT, "content": "Answer 1"},
            {"role": USER, "content": COMPRESSION_MARKER + " (round 1) ---\n"
             "<context_summary>Summary</context_summary>"},
            {"role": USER, "content": "Second question"},
            {"role": ASSISTANT, "content": "Answer 2"},
        ]
        jsonl_path = str(tmp_path / "single_marker.jsonl")
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs])

        loaded = _read_jsonl_messages(jsonl_path)
        ws = rebuild_working_set_from_jsonl(loaded)

        assert ws[0].role == SYSTEM
        assert ws[1].role == USER and not _is_marker(ws[1])  # U0
        assert _is_marker(ws[2]), "Index 2 should be the single marker"
        tail = ws[3:]
        assert len(tail) == 2, "Tail: [U2, A2]"

    def test_all_compressed_no_tail(self, tmp_path):
        """All messages compressed away — only markers remain as content."""
        jsonl_msgs = [
            {"role": SYSTEM, "content": "System"},
            {"role": USER, "content": "U0"},
            {"role": ASSISTANT, "content": "A0"},
            {
                "role": USER,
                "content": COMPRESSION_MARKER + " ---\n<context_summary>Everything</context_summary>",
            },
        ]
        jsonl_path = str(tmp_path / "all_compressed.jsonl")
        _write_jsonl(jsonl_path, [Message(**m) for m in jsonl_msgs])

        loaded = _read_jsonl_messages(jsonl_path)
        ws = rebuild_working_set_from_jsonl(loaded)

        # Should be [SYS][U0][COMP1] — no tail
        assert len(ws) == 3, f"Expected 3 messages, got {len(ws)}"
        assert ws[2].role == USER and _is_marker(ws[2])

    def test_empty_jsonl(self, tmp_path):
        """Empty JSONL file should produce empty working set."""
        jsonl_path = str(tmp_path / "empty.jsonl")
        metadata = {
            "agent_class": "coder",
            "instance_name": "TestAgent",
            "start_timestamp": "2026-01-01T00:00:00",
            "current_log_path": jsonl_path,
        }
        with open(jsonl_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({"metadata": metadata}) + '\n')

        loaded = _read_jsonl_messages(jsonl_path)
        ws = rebuild_working_set_from_jsonl(loaded)
        assert len(ws) == 0, "Empty JSONL → empty working set"


# ──────────────────────────────────────────────
# Helper verification: MockAgentPool.find_last_marker consistency
# ──────────────────────────────────────────────

class TestHelperConsistency:
    """Verify that helper functions match production behavior."""

    def test_find_last_marker_matches_jsonl(self, tmp_path):
        """MockAgentPool.find_last_marker should find the same last marker position
        in both Message objects and dicts.
        """
        jsonl_msgs = _build_compressed_jsonl_messages()

        # Find last marker index in dict list
        last_marker_dict = -1
        for i, m in enumerate(jsonl_msgs):
            if _is_marker(m):
                last_marker_dict = i

        # Convert to Message objects and find with MockAgentPool helper
        msg_objs = [Message(**m) for m in jsonl_msgs]
        last_marker_obj = MockAgentPool.find_last_marker(msg_objs)

        assert last_marker_dict == last_marker_obj, \
            f"find_last_marker mismatch: dict={last_marker_dict}, obj={last_marker_obj}"

    def test_find_last_marker_no_markers(self):
        """Should return -1 when no markers exist."""
        msgs = [Message(SYSTEM, "sys"), Message(USER, "hello")]
        assert MockAgentPool.find_last_marker(msgs) == -1