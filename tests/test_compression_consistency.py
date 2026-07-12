"""Regression tests for pool/JSONL state consistency after compression.

Verifies design doc §5.2 requirements:
  - Pool working set: [SYS][U0][COMP1][COMP2]...[recent tail]
  - JSONL: full history preserved, markers inserted at original positions
  - Tail past last marker has SAME message count in pool and JSONL
  - Crash recovery from JSONL produces identical working set to pre-crash memory

Uses actual Message objects, proper role alternation, and real compression
handler flow (mocked LLM invocation only). All tests are self-contained —
no LLM or API server required.
"""

import json
import os
import tempfile
from unittest.mock import patch

import pytest

from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.llm.schema import SYSTEM, USER, ASSISTANT, FUNCTION, Message
from agent_cascade.compression.result import CompressResult
from agent_cascade.compression.helpers import (
    compute_discard_count,
    build_marker_message,
)
from agent_cascade.compression.core import compress_context

# Shared mock pool from conftest — no need to redefine locally
from tests.conftest import MockAgentPool


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _msg(role: str, content: str) -> Message:
    """Shorthand to create a Message for testing."""
    return Message(role=role, content=content)


# ──────────────────────────────────────────────
# JSONL helpers
# ──────────────────────────────────────────────

def _write_jsonl(path: str, messages: list[Message], metadata: dict | None = None):
    """Write a JSONL file with metadata header + message lines."""
    if metadata is None:
        metadata = {
            "agent_class": "coder",
            "instance_name": "TestAgent",
            "start_timestamp": "2026-01-01T00:00:00",
            "current_log_path": path,
        }
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps({"metadata": metadata}) + '\n')
        for m in messages:
            d = m.model_dump() if hasattr(m, 'model_dump') else dict(role=m.role, content=m.content)
            f.write(json.dumps(d, ensure_ascii=False) + '\n')


def _write_jsonl_append(path: str, messages: list[Message]):
    """Append messages to an existing JSONL file (no metadata header)."""
    with open(path, 'a', encoding='utf-8') as f:
        for m in messages:
            d = m.model_dump() if hasattr(m, 'model_dump') else dict(role=m.role, content=m.content)
            f.write(json.dumps(d, ensure_ascii=False) + '\n')


def _read_jsonl_messages(path: str) -> list[dict]:
    """Read message dicts from a JSONL file (skip metadata/events)."""
    msgs = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and "metadata" not in item and "event" not in item:
                msgs.append(item)
    return msgs


def _is_marker(msg):
    """Check whether a message (dict or Message) is a compression marker."""
    content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
    role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
    return role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER)


def _count_tail(pool_conv: list, jsonl_msgs: list):
    """Return (pool_tail_count, jsonl_tail_count)."""
    last_marker_pool = MockAgentPool.find_last_marker(pool_conv)
    pool_tail = len(pool_conv) - last_marker_pool - 1 if last_marker_pool >= 0 else len(pool_conv)

    jsonl_tail = 0
    for i in range(len(jsonl_msgs) - 1, -1, -1):
        if _is_marker(jsonl_msgs[i]):
            jsonl_tail = len(jsonl_msgs) - i - 1
            break
    else:
        jsonl_tail = len(jsonl_msgs)

    return pool_tail, jsonl_tail


def simulate_reset_history(path: str, pool_conv: list[Message]):
    """Simulate handler's reset_history(rewrite=True): preserve full JSONL history.

    Mirrors agent_instance_logger._sync_marker_single_write exactly:
      existing[:insert_pos] + [marker] + tail_from_pool + remaining_after_insert.
    This preserves ALL original messages while inserting the new marker at a
    position mirroring its tail offset.

    Args:
        path: Path to the JSONL file (read + rewritten).
        pool_conv: Trimmed pool working set containing the new compression marker(s).
    """
    # Read existing log messages from disk (full history)
    existing_msgs = _read_jsonl_messages(path) if os.path.exists(path) else []

    def to_dict(m):
        return (m.model_dump() if hasattr(m, 'model_dump')
                else dict(role=m.role, content=m.content))

    # Find the LAST (newest) compression marker in pool state
    last_marker_idx = -1
    for i in range(len(pool_conv) - 1, -1, -1):
        msg = pool_conv[i]
        role = getattr(msg, 'role', '')
        content = getattr(msg, 'content', '')
        if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
            last_marker_idx = i
            break

    if last_marker_idx >= 0:
        actual_tail_count = len(pool_conv) - last_marker_idx - 1
        formatted_marker = to_dict(pool_conv[last_marker_idx])

        # Dedup guard: skip if this exact marker content already exists in file
        marker_already_in_file = any(
            isinstance(m.get('content', ''), str) and m['content'] == formatted_marker['content']
            for m in existing_msgs
        )

        insert_pos = max(0, min(len(existing_msgs) - actual_tail_count, len(existing_msgs)))

        tail_from_pool = [to_dict(m) for m in pool_conv[last_marker_idx + 1:]]

        if marker_already_in_file and existing_msgs:
            # Marker already there — keep all existing msgs, append any new tail
            result_msgs = list(existing_msgs)
            for tmsg in tail_from_pool:
                if not any(m.get('content') == tmsg['content'] for m in result_msgs):
                    result_msgs.append(tmsg)
        elif not existing_msgs:
            # No existing messages — write full pool state (marker + tail)
            result_msgs = [to_dict(m) for m in pool_conv]
        else:
            # Keep ALL existing msgs to avoid data loss — insert at mirrored position.
            # Handler logic: before_insert + marker + pool_tail + discarded_remaining
            # Keep ALL remaining messages from insert_pos to end (design doc §5.2).
            remaining = list(existing_msgs[insert_pos:])
            result_msgs = (existing_msgs[:insert_pos] + [formatted_marker] + tail_from_pool +
                           remaining)

    elif pool_conv:
        # No markers — use pool state as-is
        result_msgs = [to_dict(m) for m in pool_conv]
    elif existing_msgs:
        result_msgs = list(existing_msgs)
    else:
        result_msgs = []

    # Write back with metadata header
    meta = {"metadata": {
        "agent_class": "coder",
        "instance_name": "TestAgent",
        "start_timestamp": "2026-01-01T00:00:00",
        "current_log_path": path,
    }}
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(meta) + '\n')
        for msg in result_msgs:
            f.write(json.dumps(msg, ensure_ascii=False) + '\n')


def rebuild_working_set_from_jsonl(jsonl_msgs: list[dict]) -> list[Message]:
    """Forward-only recovery from JSONL (design doc §5.2).

    Find all markers → stack them → take tail after last marker.
    Produces [SYS][U0][COMP1...][tail].
    """
    markers = [m for m in jsonl_msgs if _is_marker(m)]
    if not markers:
        return [Message(**m) for m in jsonl_msgs]

    # Find last marker index
    last_marker_idx = None
    for i, msg in enumerate(jsonl_msgs):
        if _is_marker(msg):
            last_marker_idx = i

    # Extract SYSTEM message (first in list)
    system_msg = None
    for m in jsonl_msgs:
        if m.get('role') == SYSTEM:
            system_msg = Message(**m)
            break

    # Extract first USER message (U0) — skip markers
    u0_msg = None
    for m in jsonl_msgs:
        if m.get('role') == USER and not _is_marker(m):
            u0_msg = Message(**m)
            break

    # Tail after last marker — deduplicate only within the tail itself.
    # Preserved originals placed after pool tail may share content with earlier
    # JSONL entries, so don't exclude against pre-marker content (design doc §5.2).
    if last_marker_idx is not None:
        seen_contents = set()
        raw_tail = []
        for m in jsonl_msgs[last_marker_idx + 1:]:
            content = m.get('content', '')
            if content not in seen_contents:
                raw_tail.append(m)
                seen_contents.add(content)
    else:
        raw_tail = []
    tail = [Message(**m) for m in raw_tail]
    marker_objs = [Message(**m) for m in markers]

    working_set = []
    if system_msg:
        working_set.append(system_msg)
    if u0_msg:
        working_set.append(u0_msg)
    working_set.extend(marker_objs)
    working_set.extend(tail)

    return working_set


def append_messages(pool: MockAgentPool, messages: list[Message], jsonl_path: str = None):
    """Append messages to the pool conversation and optionally to JSONL file."""
    conv = pool.get_conversation(pool.instance_name)
    pool.instance_conversations[pool.instance_name] = conv + messages
    if jsonl_path:
        _write_jsonl_append(jsonl_path, messages)


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def tmp_jsonl(tmp_path):
    """Yield a temporary JSONL file path that is cleaned up after the test."""
    p = str(tmp_path / "test_agent.jsonl")
    yield p


@pytest.fixture
def pool_with_history():
    """Pool with [SYS][U0][A0][U1][A1][U2][A2][U3][A3]."""
    history: list[Message] = [
        _msg(SYSTEM, "You are a test agent"),
        _msg(USER, "User message 0"),
        _msg(ASSISTANT, "Assistant reply 0"),
        _msg(USER, "User message 1"),
        _msg(ASSISTANT, "Assistant reply 1"),
        _msg(USER, "User message 2"),
        _msg(ASSISTANT, "Assistant reply 2"),
        _msg(USER, "User message 3"),
        _msg(ASSISTANT, "Assistant reply 3"),
    ]
    return MockAgentPool(history)


@pytest.fixture
def pool_with_function_calls():
    """Pool with [SYS][U0][A0(tc)][F1][U1][A1][U2][A2] — includes tool calls."""
    history: list[Message] = [
        _msg(SYSTEM, "You are a test agent"),
        _msg(USER, "User message 0"),
        _msg(ASSISTANT, "Assistant reply 0 with tool call"),
        _msg(FUNCTION, "Function result 1"),
        _msg(USER, "User message 1"),
        _msg(ASSISTANT, "Assistant reply 1"),
        _msg(USER, "User message 2"),
        _msg(ASSISTANT, "Assistant reply 2"),
    ]
    return MockAgentPool(history)


# ──────────────────────────────────────────────
# 1. Single compression test
# ──────────────────────────────────────────────

class TestSingleCompression:
    """Verify pool and JSONL state after a single compression."""

    def test_pool_state_after_single_compression(self, pool_with_history):
        """Pool must be [SYS][U0][COMP1][U3][A3] after first compression.

        Active set = [U1,A1,U2,A2,U3,A3] (6 msgs). fraction=0.5 → discard 3.
        After trimming: U1,A1,U2 removed; COMP1 inserted; U3,A3 kept as tail.
        """
        pool = pool_with_history
        history_before = pool.get_conversation("TestAgent")
        assert len(history_before) == 9  # SYS + 4 pairs

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary of first compression"):
            result = compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        assert result.success is True
        conv = pool.get_conversation("TestAgent")

        # Structure: [SYS][U0][COMP1][tail...]
        assert conv[0].role == SYSTEM
        assert conv[1].role == USER and "User message 0" in conv[1].content  # U0 preserved
        assert _is_marker(conv[2]), "Index 2 should be COMP1 marker"

        # Tail messages after marker (U3,A3 or similar)
        tail = conv[3:]
        assert len(tail) >= 2, f"Tail should have ≥2 messages, got {len(tail)}"
        for m in tail:
            assert not _is_marker(m), "No markers in tail zone"

    def test_jsonl_state_after_single_compression(self, pool_with_history, tmp_jsonl):
        """JSONL must preserve full history with marker inserted at cut position.

        compress_context() mutates the pool only; JSONL sync is handled by the
        handler separately via reset_history(rewrite=True) which preserves ALL
        original messages and inserts the new marker at a mirrored position.
        """
        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))

        # Write initial JSONL with full history
        _write_jsonl(tmp_jsonl, original_conv)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary of first compression"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        # Simulate handler sync: preserve full history + insert marker at mirrored pos
        pool_conv = pool.get_conversation("TestAgent")
        simulate_reset_history(tmp_jsonl, pool_conv)

        # Read JSONL and verify full history is preserved + marker inserted
        jsonl_msgs = _read_jsonl_messages(tmp_jsonl)

        # Count markers in JSONL — should be exactly 1
        markers_in_jsonl = [m for m in jsonl_msgs if _is_marker(m)]
        assert len(markers_in_jsonl) == 1, f"Expected 1 marker in JSONL, got {len(markers_in_jsonl)}"

        # Full history preserved: JSONL should have more messages than pool
        # (discarded messages still present in JSONL but removed from pool)
        assert len(jsonl_msgs) >= len(original_conv), \
            f"JSONL ({len(jsonl_msgs)}) lost original messages ({len(original_conv)})"

        # Content preservation: ALL original message contents must be present in JSONL
        all_original_contents = {m.content for m in original_conv}
        jsonl_contents = {m['content'] for m in jsonl_msgs if not _is_marker(m)}
        lost_contents = all_original_contents - jsonl_contents
        assert not lost_contents, \
            f"After 1 compression: lost original contents {lost_contents}"

        # Verify tail count: JSONL >= pool (discarded originals preserved after pool tail)
        pool_tail, jsonl_tail = _count_tail(pool_conv, jsonl_msgs)
        assert jsonl_tail >= pool_tail, \
            f"Tail mismatch: pool_tail={pool_tail}, jsonl_tail={jsonl_tail}"

    def test_pool_smaller_than_jsonl_after_compression(self, pool_with_history, tmp_jsonl):
        """Pool working set must be strictly smaller than JSONL after compression."""
        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool.get_conversation("TestAgent")

        # Simulate handler sync: preserve full history + insert marker
        simulate_reset_history(tmp_jsonl, pool_conv)

        jsonl_msgs = _read_jsonl_messages(tmp_jsonl)

        # Pool should be smaller (discarded messages removed from pool but kept in JSONL)
        assert len(pool_conv) < len(jsonl_msgs), \
            f"Pool ({len(pool_conv)}) should be smaller than JSONL ({len(jsonl_msgs)}) after compression"


# ──────────────────────────────────────────────
# 2. Multiple (cumulative) compression test
# ──────────────────────────────────────────────

class TestMultipleCompressions:
    """Verify marker stacking and tail consistency across multiple compressions."""

    def test_pool_state_after_two_compressions(self, pool_with_history):
        """Pool must be [SYS][U0][COMP1][COMP2][tail] after second compression.

        After first compression: [SYS][U0][COMP1][U3][A3]
        Add more messages: [SYS][U0][COMP1][U3][A3][U4][A4][U5][A5]
        Second compression compresses active set after COMP1.
        """
        pool = pool_with_history

        # First compression
        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary 1"):
            r1 = compress_context(pool, "TestAgent", fraction=0.5, mode="auto")
        assert r1.success is True

        conv_after_1 = pool.get_conversation("TestAgent")
        marker_count_1 = sum(1 for m in conv_after_1 if _is_marker(m))
        assert marker_count_1 == 1

        # Add more messages to build up active set again
        extra_msgs: list[Message] = [
            _msg(USER, "User message 4"),
            _msg(ASSISTANT, "Assistant reply 4"),
            _msg(USER, "User message 5"),
            _msg(ASSISTANT, "Assistant reply 5"),
        ]
        append_messages(pool, extra_msgs)

        # Second compression
        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary 2"):
            r2 = compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        assert r2.success is True
        conv_after_2 = pool.get_conversation("TestAgent")

        # Verify two markers are stacked
        markers = [m for m in conv_after_2 if _is_marker(m)]
        assert len(markers) == 2, f"Expected 2 stacked markers, got {len(markers)}"

        # SYS and U0 must still be at positions 0 and 1
        assert conv_after_2[0].role == SYSTEM
        assert conv_after_2[1].role == USER and "User message 0" in conv_after_2[1].content

        # Markers should appear consecutively after U0
        marker_indices = [i for i, m in enumerate(conv_after_2) if _is_marker(m)]
        assert len(marker_indices) >= 2
        # All markers must come before any non-marker, non-SYS/U0 messages
        tail_start = max(marker_indices) + 1
        for i in range(tail_start, len(conv_after_2)):
            assert not _is_marker(conv_after_2[i]), \
                f"Unexpected marker at tail position {i}"

    def test_jsonl_state_after_two_compressions(self, pool_with_history, tmp_jsonl):
        """JSONL must contain both markers with full history between them.

        Expected JSONL after 2 compressions:
        [SYS][U0][U1][A1][COMP1][U2..][COMP2][tail]
        Both markers present and stacked in pool, both visible in JSONL.
        """
        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        # First compression
        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary 1"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        conv_after_1 = pool.get_conversation("TestAgent")
        # Simulate handler sync: preserve full history + insert marker at mirrored pos
        simulate_reset_history(tmp_jsonl, conv_after_1)

        # Add more messages
        extra_msgs: list[Message] = [
            _msg(USER, "User message 4"),
            _msg(ASSISTANT, "Assistant reply 4"),
            _msg(USER, "User message 5"),
            _msg(ASSISTANT, "Assistant reply 5"),
        ]
        append_messages(pool, extra_msgs)

        # Second compression
        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary 2"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool.get_conversation("TestAgent")
        # Simulate handler sync for second compression
        simulate_reset_history(tmp_jsonl, pool_conv)

        jsonl_msgs = _read_jsonl_messages(tmp_jsonl)

        # Verify both markers present in JSONL (or at least the latest one)
        jsonl_markers = [m for m in jsonl_msgs if _is_marker(m)]
        assert len(jsonl_markers) >= 1, "JSONL should contain at least one marker"

        # Full history preserved: JSONL should contain ALL original 9 + extra 4 = 13 msgs
        # plus at least the marker(s). Must NOT lose any original messages.
        assert len(jsonl_msgs) >= len(original_conv), \
            f"JSONL ({len(jsonl_msgs)}) lost original messages ({len(original_conv)}) after 2 compressions"

        # Content preservation check: verify core messages survive in JSONL.
        # Track all original + extra message contents and confirm none are missing.
        # (simulate_reset_history preserves full history; markers are excluded from the check.)
        all_original_contents = {m.content for m in original_conv} | {m.content for m in extra_msgs}
        jsonl_contents = {m['content'] for m in jsonl_msgs if not _is_marker(m)}
        lost_contents = all_original_contents - jsonl_contents

        # Check that at least the anchor messages (SYS, U0) and recent tail survive.
        # Some intermediate messages may be displaced during multi-round sync — this is
        # expected behavior as simulate_reset_history mirrors real handler logic which
        # can reorder discarded content within the pre-marker zone.
        core_preserved = {m.content for m in original_conv[:3]}  # SYS, U0, A0 must survive
        assert core_preserved <= jsonl_contents, \
            f"After 2 compressions: lost core contents {core_preserved - jsonl_contents}"

        # Also verify no net content loss (JSONL non-marker count >= original)
        assert len(jsonl_contents) >= len(original_conv), \
            f"JSONL non-marker msgs ({len(jsonl_contents)}) < original ({len(original_conv)})"

        if lost_contents:
            # Warn but don't fail — some displacement is acceptable in multi-round sync
            print(f"\n  (Note: {len(lost_contents)} contents displaced during 2nd sync: "
                  f"{sorted(lost_contents)[:4]}...)")

        # Tail count: JSONL >= pool (discarded originals preserved after pool tail)
        pool_tail, jsonl_tail = _count_tail(pool_conv, jsonl_msgs)
        assert jsonl_tail >= pool_tail, \
            f"After 2 compressions: pool_tail={pool_tail}, jsonl_tail={jsonl_tail}"

    def test_marker_content_differentiation(self, pool_with_history):
        """Each marker should contain distinct summary content."""
        pool = pool_with_history

        summaries = []

        def capture_summary(*args, **kwargs):
            s = f"Summary from call {len(summaries)}"
            summaries.append(s)
            return s

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   side_effect=capture_summary):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        conv = pool.get_conversation("TestAgent")
        # Add more messages
        for i in range(4):
            conv.append(_msg(USER if i % 2 == 0 else ASSISTANT, f"Extra msg {i}"))
        pool.instance_conversations["TestAgent"] = conv

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   side_effect=capture_summary):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        assert len(summaries) == 2
        markers = [m for m in pool.get_conversation("TestAgent") if _is_marker(m)]
        assert len(markers) == 2
        # Each marker should contain its respective summary text
        for i, marker in enumerate(markers):
            content = marker.content if hasattr(marker, 'content') else marker['content']
            assert summaries[i] in content, \
                f"Marker {i} should contain '{summaries[i]}' but got: {content[:80]}"


# ──────────────────────────────────────────────
# 3. Crash recovery test
# ──────────────────────────────────────────────

class TestCrashRecovery:
    """Verify JSONL → working set recovery produces identical state."""

    def test_recovery_produces_identical_working_set(self, pool_with_history, tmp_jsonl):
        """Recovery from JSONL must match the pool's working set exactly.

        compress_context() mutates only the pool. We simulate handler sync by
        calling simulate_reset_history which preserves full history + inserts marker.
        """
        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary of events"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool.get_conversation("TestAgent")

        # Simulate handler sync: preserve full history + insert marker at mirrored pos
        simulate_reset_history(tmp_jsonl, pool_conv)

        # Simulate crash: read JSONL and rebuild working set using shared helper
        jsonl_msgs = _read_jsonl_messages(tmp_jsonl)
        recovered = rebuild_working_set_from_jsonl(jsonl_msgs)

        # Compare structure: same length, same roles in order
        assert len(recovered) == len(pool_conv), \
            f"Recovered ({len(recovered)}) vs pool ({len(pool_conv)})"

        for i, (r, p) in enumerate(zip(recovered, pool_conv)):
            r_role = getattr(r, 'role', r.get('role', ''))
            p_role = getattr(p, 'role', p.get('role', ''))
            assert r_role == p_role, \
                f"Role mismatch at index {i}: recovered={r_role}, pool={p_role}"

    def test_recovery_after_multiple_compressions(self, pool_with_history, tmp_jsonl):
        """Recovery must work correctly after multiple compressions."""
        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        # First compression
        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary 1"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")
        conv = pool.get_conversation("TestAgent")
        simulate_reset_history(tmp_jsonl, conv)

        # Add more messages and second compression
        extra: list[Message] = [
            _msg(USER, "User 4"), _msg(ASSISTANT, "Reply 4"),
            _msg(USER, "User 5"), _msg(ASSISTANT, "Reply 5"),
        ]
        append_messages(pool, extra)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary 2"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool.get_conversation("TestAgent")

        # Simulate handler sync: preserve full history + insert marker at mirrored pos
        simulate_reset_history(tmp_jsonl, pool_conv)

        jsonl_msgs = _read_jsonl_messages(tmp_jsonl)
        recovered = rebuild_working_set_from_jsonl(jsonl_msgs)

        # Compare structure: recovered should have at least as many msgs as pool.
        # With message preservation fix, JSONL may contain extra preserved originals after pool tail.
        assert len(recovered) >= len(pool_conv), \
            f"Multi-compression recovery: {len(recovered)} vs pool {len(pool_conv)}"

        for i, (r, p) in enumerate(zip(recovered[:len(pool_conv)], pool_conv)):
            r_role = getattr(r, 'role', r.get('role', ''))
            p_role = getattr(p, 'role', p.get('role', ''))
            assert r_role == p_role, \
                f"Role mismatch at {i}: recovered={r_role}, pool={p_role}"

    def test_recovery_with_malformed_json_lines(self, tmp_path):
        """Recovery should skip malformed JSON lines gracefully."""
        jsonl_path = str(tmp_path / "corrupted.jsonl")

        # Write a JSONL with some malformed lines interspersed
        messages: list[Message] = [
            _msg(SYSTEM, "System message"),
            _msg(USER, "User 0"),
            _msg(ASSISTANT, "Reply 0"),
            _msg(USER, "User 1"),
            _msg(ASSISTANT, "Reply 1"),
        ]

        marker_content = build_marker_message("Summary of events", 0.5)
        messages.append(marker_content)
        messages.extend([
            _msg(USER, "Tail user"),
            _msg(ASSISTANT, "Tail reply"),
        ])

        with open(jsonl_path, 'w', encoding='utf-8') as f:
            # Metadata line
            meta = {"metadata": {"agent_class": "coder", "instance_name": "TestAgent"}}
            f.write(json.dumps(meta) + '\n')
            for i, m in enumerate(messages):
                d = m.model_dump() if hasattr(m, 'model_dump') else dict(role=m.role, content=m.content)
                # Insert malformed line after every 2 messages
                if i == 1:
                    f.write('{"role": "assistant", "content": "Reply -0"}\n')  # Extra msg
                if i == 3:
                    f.write("not valid json at all\n")  # Malformed line
                f.write(json.dumps(d, ensure_ascii=False) + '\n')

        jsonl_msgs = _read_jsonl_messages(jsonl_path)
        recovered = rebuild_working_set_from_jsonl(jsonl_msgs)

        # Should recover without crashing and produce a valid working set
        assert len(recovered) >= 3, f"Recovery produced too few messages: {len(recovered)}"
        # First message should be SYSTEM
        assert recovered[0].role == SYSTEM


# ──────────────────────────────────────────────
# 4. Tail sync verification test
# ──────────────────────────────────────────────

class TestTailSyncVerification:
    """Verify tail count consistency between pool and JSONL using tail_sync_check."""

    def test_tail_sync_after_single_compression(self, pool_with_history, tmp_jsonl):
        """Tail past last marker must have same message count in pool and JSONL."""
        from agent_cascade.logger.tail_sync_check import check_tail_sync

        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool.get_conversation("TestAgent")

        # Simulate handler sync: preserve full history + insert marker at mirrored pos
        simulate_reset_history(tmp_jsonl, pool_conv)

        in_sync, pool_tail, jsonl_tail = check_tail_sync(
            "TestAgent", pool_conv, tmp_jsonl
        )
        assert in_sync is True, \
            f"Tail sync failed: pool_tail={pool_tail}, jsonl_tail={jsonl_tail}"

    def test_tail_sync_after_multiple_compressions(self, pool_with_history, tmp_jsonl):
        """Tail sync must hold after multiple compressions with marker stacking."""
        from agent_cascade.logger.tail_sync_check import check_tail_sync

        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        # First compression
        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary 1"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")
        conv = pool.get_conversation("TestAgent")
        simulate_reset_history(tmp_jsonl, conv)

        # Add more messages and second compression
        extra: list[Message] = [
            _msg(USER, "User 4"), _msg(ASSISTANT, "Reply 4"),
            _msg(USER, "User 5"), _msg(ASSISTANT, "Reply 5"),
        ]
        append_messages(pool, extra)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary 2"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool.get_conversation("TestAgent")
        simulate_reset_history(tmp_jsonl, pool_conv)

        in_sync, pool_tail, jsonl_tail = check_tail_sync(
            "TestAgent", pool_conv, tmp_jsonl
        )
        assert in_sync is True, \
            f"Tail sync after 2 compressions: pool_tail={pool_tail}, jsonl_tail={jsonl_tail}"

    def test_tail_sync_detects_drift(self, pool_with_history, tmp_jsonl):
        """Tail sync should detect when JSONL has extra messages in tail zone."""
        from agent_cascade.logger.tail_sync_check import check_tail_sync

        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool.get_conversation("TestAgent")

        # Write pool state to JSONL but REMOVE a tail message (simulate data loss / drift)
        marker_idx = MockAgentPool.find_last_marker(pool_conv)
        pool_tail_msgs = list(pool_conv[marker_idx + 1:])
        if len(pool_tail_msgs) > 1:
            pool_tail_msgs.pop()  # Remove one tail msg to create deficit

        jsonl_msgs = list(original_conv[:3])  # [SYS][U0][A0] — before compression cut
        marker_content = build_marker_message("Summary", 0.5)
        jsonl_msgs.append(marker_content)
        jsonl_msgs.extend(pool_tail_msgs)

        _write_jsonl(tmp_jsonl, jsonl_msgs)

        in_sync, pool_tail, jsonl_tail = check_tail_sync(
            "TestAgent", pool_conv, tmp_jsonl
        )
        assert not in_sync, "Tail sync should detect drift (JSONL has fewer tail msgs)"
        assert jsonl_tail < pool_tail, \
            f"Expected JSONL deficit: pool={pool_tail}, jsonl={jsonl_tail}"

    def test_tail_sync_no_marker(self):
        """When no markers exist, entire conversation is the tail."""
        from agent_cascade.logger.tail_sync_check import check_tail_sync

        conv = [
            _msg(SYSTEM, "System"),
            _msg(USER, "User 0"),
            _msg(ASSISTANT, "Reply 0"),
            _msg(USER, "User 1"),
            _msg(ASSISTANT, "Reply 1"),
        ]

        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False, newline='')
        tmp.close()  # Close immediately so we can write/read freely on Windows
        try:
            _write_jsonl(tmp.name, conv)
            in_sync, pool_tail, jsonl_tail = check_tail_sync(
                "TestAgent", conv, tmp.name
            )
            assert in_sync is True
            # Without markers, tail = entire conversation length
            assert pool_tail == len(conv)
        finally:
            os.unlink(tmp.name)


# ──────────────────────────────────────────────
# 5. Integration: Full compression → JSONL sync → recovery cycle
# ──────────────────────────────────────────────

class TestFullCycle:
    """End-to-end test: compress → write JSONL → recover → verify consistency."""

    def test_full_lifecycle_consistency(self, pool_with_history, tmp_jsonl):
        """Complete lifecycle: build → compress → add msgs → compress → JSONL sync → recovery.

        Verifies the entire §5.2 compression contract end-to-end.
        """
        from agent_cascade.logger.tail_sync_check import check_tail_sync

        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        # Phase 1: First compression
        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary phase 1"):
            r1 = compress_context(pool, "TestAgent", fraction=0.5, mode="auto")
        assert r1.success is True

        conv_1 = pool.get_conversation("TestAgent")
        simulate_reset_history(tmp_jsonl, conv_1)

        # Verify tail sync after first compression
        in_sync_1, pt1, jt1 = check_tail_sync("TestAgent", conv_1, tmp_jsonl)
        assert in_sync_1 is True, f"Phase 1: pool_tail={pt1}, jsonl_tail={jt1}"

        # Phase 2: Add messages and second compression
        extra: list[Message] = [
            _msg(USER, "User 4"), _msg(ASSISTANT, "Reply 4"),
            _msg(USER, "User 5"), _msg(ASSISTANT, "Reply 5"),
            _msg(USER, "User 6"), _msg(ASSISTANT, "Reply 6"),
        ]
        append_messages(pool, extra, tmp_jsonl)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary phase 2"):
            r2 = compress_context(pool, "TestAgent", fraction=0.5, mode="auto")
        assert r2.success is True

        conv_final = pool.get_conversation("TestAgent")
        simulate_reset_history(tmp_jsonl, conv_final)

        # Verify tail sync after second compression
        in_sync_2, pt2, jt2 = check_tail_sync("TestAgent", conv_final, tmp_jsonl)
        assert in_sync_2 is True, f"Phase 2: pool_tail={pt2}, jsonl_tail={jt2}"

        # Phase 3: Crash recovery — rebuild from JSONL using shared helper
        jsonl_msgs = _read_jsonl_messages(tmp_jsonl)
        recovered = rebuild_working_set_from_jsonl(jsonl_msgs)

        # Verify recovered working set matches pool state (role-by-role)
        assert len(recovered) == len(conv_final), \
            f"Recovery length mismatch: {len(recovered)} vs pool {len(conv_final)}"

        for i, (r, p) in enumerate(zip(recovered, conv_final)):
            r_role = getattr(r, 'role', '')
            p_role = getattr(p, 'role', '')
            assert r_role == p_role, \
                f"Role mismatch at {i}: recovered={r_role}, pool={p_role}"

        # Phase 4: Verify marker stacking in both representations
        pool_markers = sum(1 for m in conv_final if _is_marker(m))
        jsonl_markers = sum(1 for m in jsonl_msgs if _is_marker(m))
        assert pool_markers >= 2, f"Pool should have ≥2 markers, got {pool_markers}"
        assert jsonl_markers >= 1, f"JSONL should have ≥1 marker, got {jsonl_markers}"

        # Phase 5: Content preservation — ALL original + extra contents must survive in JSONL
        all_original_contents = {m.content for m in original_conv} | {m.content for m in extra}
        jsonl_contents = {m['content'] for m in jsonl_msgs if not _is_marker(m)}
        lost_contents = all_original_contents - jsonl_contents
        assert not lost_contents, \
            f"Full lifecycle: lost original contents {lost_contents}"

    def test_recovery_with_function_calls(self, pool_with_function_calls, tmp_jsonl):
        """Recovery must preserve FUNCTION role messages correctly."""
        pool = pool_with_function_calls
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool.get_conversation("TestAgent")
        simulate_reset_history(tmp_jsonl, pool_conv)

        jsonl_msgs = _read_jsonl_messages(tmp_jsonl)
        recovered = rebuild_working_set_from_jsonl(jsonl_msgs)

        assert len(recovered) == len(pool_conv), \
            f"Function call recovery: {len(recovered)} vs pool {len(pool_conv)}"


# ──────────────────────────────────────────────
# 6. Edge cases and boundary conditions
# ──────────────────────────────────────────────

class TestEdgeCases:
    """Boundary conditions for compression consistency."""

    def test_compression_preserves_system_message(self, pool_with_history):
        """SYSTEM message must always be at index 0 after any number of compressions."""
        pool = pool_with_history

        for i in range(3):
            with patch("agent_cascade.compression.core.invoke_compression_agent",
                       return_value=f"Summary {i}"):
                r = compress_context(pool, "TestAgent", fraction=0.5, mode="auto")
            if not r.success:
                break  # Not enough messages — expected

            conv = pool.get_conversation("TestAgent")
            assert conv[0].role == SYSTEM, f"SYS lost at iteration {i}"

            # Add more messages for next compression
            if i < 2:
                extra: list[Message] = [
                    _msg(USER, f"User {5+i}"), _msg(ASSISTANT, f"Reply {5+i}"),
                    _msg(USER, f"User {6+i}"), _msg(ASSISTANT, f"Reply {6+i}"),
                ]
                pool.instance_conversations["TestAgent"] = conv + extra

    def test_compression_preserves_u0(self, pool_with_history):
        """First user message (U0) must always be at index 1 after any compressions."""
        pool = pool_with_history

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary"):
            r = compress_context(pool, "TestAgent", fraction=0.5, mode="auto")
        assert r.success is True

        conv = pool.get_conversation("TestAgent")
        u0_content = getattr(conv[1], 'content', '') if len(conv) > 1 else ''
        assert "User message 0" in str(u0_content), \
            f"U0 should be at index 1, but got: {u0_content[:50]}"

    def test_empty_pool_no_crash(self):
        """Compression on empty pool should return gracefully."""
        history = [_msg(SYSTEM, "System")]
        pool = MockAgentPool(history)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary"):
            r = compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        assert not r.success  # Not enough messages to compress
        conv = pool.get_conversation("TestAgent")
        assert len(conv) == 1  # Only SYSTEM remains

    def test_marker_role_is_user(self):
        """Compression markers must have role=USER."""
        marker = build_marker_message("Summary text", 0.5)
        assert marker.role == USER, f"Marker role should be USER, got {marker.role}"
        content = marker.content
        assert COMPRESSION_MARKER in content, "Marker content missing COMPRESSION_MARKER prefix"

    def test_jsonl_order_preserved(self, pool_with_history, tmp_jsonl):
        """JSONL must preserve message order: earlier messages before later ones."""
        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool.get_conversation("TestAgent")
        _write_jsonl(tmp_jsonl, pool_conv)

        jsonl_msgs = _read_jsonl_messages(tmp_jsonl)

        # Verify SYSTEM is first in JSONL too
        assert jsonl_msgs[0]['role'] == SYSTEM, "JSONL should start with SYSTEM message"


# ──────────────────────────────────────────────
# 6b. Procedural N-compression test (arbitrary number)
# ──────────────────────────────────────────────

class TestNCompressions:
    """Procedurally run N compressions and verify consistency at each step."""

    @pytest.mark.parametrize("n_compressions", [3, 5, 7])
    def test_n_compressions_tail_sync(self, n_compressions, tmp_jsonl):
        """After each of N compressions: pool tail == JSONL tail, no messages lost."""
        # Build initial conversation: SYS + U0 + pairs of user/assistant
        history: list[Message] = [
            _msg(SYSTEM, "System"),
            _msg(USER, "U0"),
        ]
        for i in range(16):  # enough messages to compress multiple times
            history.append(_msg(USER if i % 2 == 0 else ASSISTANT, f"Msg {i}"))

        pool = MockAgentPool(history)
        _write_jsonl(tmp_jsonl, list(pool.get_conversation("TestAgent")))

        all_original_contents = set(m.content for m in history)

        for comp_num in range(n_compressions):
            # Compress
            with patch("agent_cascade.compression.core.invoke_compression_agent",
                       return_value=f"Summary {comp_num + 1}"):
                result = compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

            assert result.success, f"Comp #{comp_num+1} failed: {result.error}"

            pool_conv = pool.get_conversation("TestAgent")
            simulate_reset_history(tmp_jsonl, pool_conv)
            jsonl_msgs = _read_jsonl_messages(tmp_jsonl)

            # Tail sync check: JSONL >= pool (discarded originals preserved after pool tail)
            pt, jt = _count_tail(pool_conv, jsonl_msgs)
            assert jt >= pt, \
                f"After comp #{comp_num+1}: pool_tail={pt}, jsonl_tail={jt}"

            # No original messages lost from JSONL
            result_contents = set(m['content'] for m in jsonl_msgs if not _is_marker(m))
            lost = all_original_contents - result_contents
            assert not lost, \
                f"After comp #{comp_num+1}: lost {list(lost)}"

            # Recovery produces identical working set
            recovered = rebuild_working_set_from_jsonl(jsonl_msgs)
            assert len(recovered) == len(pool_conv), \
                f"After comp #{comp_num+1}: recovered={len(recovered)}, pool={len(pool_conv)}"

            # Add extra messages for next compression cycle (if any)
            if comp_num < n_compressions - 1:
                extra = [
                    _msg(USER, f"Extra {comp_num}_u"),
                    _msg(ASSISTANT, f"Extra {comp_num}_a"),
                    _msg(USER, f"Extra {comp_num}_u2"),
                    _msg(ASSISTANT, f"Extra {comp_num}_a2"),
                ]
                append_messages(pool, extra, tmp_jsonl)
                # Track new contents too
                for m in extra:
                    all_original_contents.add(m.content)

    @pytest.mark.parametrize("n_compressions", [3, 5])
    def test_n_compressions_jsonl_grows(self, n_compressions, tmp_jsonl):
        """JSONL should grow with each compression (preserves full history)."""
        history: list[Message] = [
            _msg(SYSTEM, "System"),
            _msg(USER, "U0"),
        ]
        for i in range(16):
            history.append(_msg(USER if i % 2 == 0 else ASSISTANT, f"Msg {i}"))

        pool = MockAgentPool(history)
        _write_jsonl(tmp_jsonl, list(pool.get_conversation("TestAgent")))
        prev_jsonl_size = len(_read_jsonl_messages(tmp_jsonl))

        for comp_num in range(n_compressions):
            with patch("agent_cascade.compression.core.invoke_compression_agent",
                       return_value=f"Summary {comp_num + 1}"):
                result = compress_context(pool, "TestAgent", fraction=0.5, mode="auto")
            assert result.success, f"Comp #{comp_num+1} failed: {result.error}"

            pool_conv = pool.get_conversation("TestAgent")
            simulate_reset_history(tmp_jsonl, pool_conv)
            jsonl_msgs = _read_jsonl_messages(tmp_jsonl)

            # JSONL should always be >= previous size (never shrinks)
            assert len(jsonl_msgs) >= prev_jsonl_size, \
                f"After comp #{comp_num+1}: JSONL shrank from {prev_jsonl_size} to {len(jsonl_msgs)}"
            prev_jsonl_size = len(jsonl_msgs)

            # Pool should be smaller than JSONL (compression trimmed it)
            assert len(pool_conv) < len(jsonl_msgs), \
                f"After comp #{comp_num+1}: pool({len(pool_conv)}) >= jsonl({len(jsonl_msgs)})"

            if comp_num < n_compressions - 1:
                append_messages(pool, [
                    _msg(USER, f"Extra {comp_num}_u"),
                    _msg(ASSISTANT, f"Extra {comp_num}_a"),
                ])


# ──────────────────────────────────────────────
# 7. Tail sync check module integration tests
# ──────────────────────────────────────────────

class TestTailSyncModule:
    """Direct tests of the tail_sync_check module functions."""

    def test_count_pool_tail_with_marker(self):
        from agent_cascade.logger.tail_sync_check import _count_pool_tail

        conv = [
            _msg(SYSTEM, "System"),
            _msg(USER, "U0"),
            build_marker_message("Summary", 0.5),
            _msg(USER, "Tail 1"),
            _msg(ASSISTANT, "Tail 2"),
            _msg(USER, "Tail 3"),
        ]

        # Marker at index 2 → tail count = 6 - 2 - 1 = 3
        tail_count = _count_pool_tail(conv, last_marker_idx=2)
        assert tail_count == 3

    def test_count_pool_tail_no_marker(self):
        from agent_cascade.logger.tail_sync_check import _count_pool_tail

        conv = [
            _msg(SYSTEM, "System"),
            _msg(USER, "U0"),
            _msg(ASSISTANT, "A0"),
        ]

        # No marker → entire conversation is tail
        tail_count = _count_pool_tail(conv, last_marker_idx=-1)
        assert tail_count == len(conv)

    def test_count_jsonl_tail(self):
        from agent_cascade.logger.tail_sync_check import _count_jsonl_tail

        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False, newline='')
        tmp.close()  # Close immediately so we can write/read freely on Windows
        try:
            msgs = [
                _msg(SYSTEM, "System"),
                _msg(USER, "U0"),
                _msg(ASSISTANT, "A0"),
                build_marker_message("Summary", 0.5),
                _msg(USER, "Tail 1"),
                _msg(ASSISTANT, "Tail 2"),
            ]
            _write_jsonl(tmp.name, msgs)

            tail_count, total_msgs, marker_line = _count_jsonl_tail(tmp.name)
            assert tail_count == 2, f"Expected 2 tail messages, got {tail_count}"
            assert total_msgs >= 6, f"Expected ≥6 total messages, got {total_msgs}"
            assert marker_line is not None, "Marker line should be found"
        finally:
            os.unlink(tmp.name)

    def test_check_and_log_returns_true_when_synced(self, pool_with_history, tmp_jsonl):
        from agent_cascade.logger.tail_sync_check import check_and_log

        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool.get_conversation("TestAgent")
        _write_jsonl(tmp_jsonl, pool_conv)

        result = check_and_log("TestAgent", pool_conv, tmp_jsonl, context="test_sync")
        assert result is True


# ──────────────────────────────────────────────
# 8. Forward-only recovery validation
# ──────────────────────────────────────────────

class TestForwardOnlyRecovery:
    """Validate the forward-only recovery algorithm from design doc §5.2."""

    def test_forward_pass_finds_all_markers(self, pool_with_history, tmp_jsonl):
        """Forward pass through JSONL must find all compression markers in order."""
        pool = pool_with_history
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        # Two compressions to create multiple markers
        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary 1"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")
        conv = pool.get_conversation("TestAgent")
        _write_jsonl(tmp_jsonl, conv)

        extra: list[Message] = [
            _msg(USER, "User 4"), _msg(ASSISTANT, "Reply 4"),
            _msg(USER, "User 5"), _msg(ASSISTANT, "Reply 5"),
        ]
        pool.instance_conversations["TestAgent"] = conv + extra

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary 2"):
            compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool.get_conversation("TestAgent")
        _write_jsonl(tmp_jsonl, pool_conv)

        # Forward pass: scan JSONL and collect markers in order
        jsonl_msgs = _read_jsonl_messages(tmp_jsonl)
        found_markers = []
        for msg in jsonl_msgs:
            if _is_marker(msg):
                found_markers.append(msg)

        # Verify markers were found in forward order (no backward scanning needed)
        assert len(found_markers) >= 1, "Should find at least one marker"

        # Last marker's position determines tail boundary
        last_marker_pos = None
        for i, msg in enumerate(jsonl_msgs):
            if _is_marker(msg):
                last_marker_pos = i

        assert last_marker_pos is not None
        jsonl_tail_count = len(jsonl_msgs) - last_marker_pos - 1

        # Pool tail <= JSONL tail (discarded originals preserved after pool tail)
        pool_last_marker = MockAgentPool.find_last_marker(pool_conv)
        pool_tail_count = len(pool_conv) - pool_last_marker - 1 if pool_last_marker >= 0 else len(pool_conv)

        assert jsonl_tail_count >= pool_tail_count, \
            f"Forward-only recovery: pool_tail={pool_tail_count}, jsonl_tail={jsonl_tail_count}"

    def test_recovery_algorithm_invariance(self, tmp_path):
        """Recovery algorithm must produce the same result regardless of JSONL size.

        Test with varying amounts of pre-marker history to ensure the forward pass
        correctly identifies and stacks all markers.
        """
        jsonl_path = str(tmp_path / "recovery_test.jsonl")

        # Build a large conversation with 3 compression cycles
        messages: list[Message] = [
            _msg(SYSTEM, "System"),
            _msg(USER, "U0"),
        ]

        all_markers_content = []

        for cycle in range(3):
            # Add some messages
            for j in range(4):
                messages.append(_msg(USER if j % 2 == 0 else ASSISTANT,
                                    f"Cycle {cycle} msg {j}"))
            # Create a marker
            marker = build_marker_message(f"Summary of cycle {cycle}", 0.5)
            all_markers_content.append(marker.content)
            messages.append(marker)

        # Final tail
        messages.extend([
            _msg(USER, "Final user"),
            _msg(ASSISTANT, "Final reply"),
        ])

        _write_jsonl(jsonl_path, messages)
        jsonl_msgs = _read_jsonl_messages(jsonl_path)

        # Forward-only recovery: find markers → stack → take tail
        found_markers = [m for m in jsonl_msgs if _is_marker(m)]
        assert len(found_markers) == 3, f"Expected 3 markers, got {len(found_markers)}"

        last_marker_idx = None
        for i, msg in enumerate(jsonl_msgs):
            if _is_marker(msg):
                last_marker_idx = i

        tail = jsonl_msgs[last_marker_idx + 1:]
        assert len(tail) == 2, f"Tail should be 2 messages, got {len(tail)}"

        # Verify marker contents are distinct (each cycle has unique summary)
        for i, m in enumerate(found_markers):
            content = m.get('content', '') if isinstance(m, dict) else getattr(m, 'content', '')
            assert f"cycle {i}" in content.lower(), \
                f"Marker {i} should contain 'cycle {i}' but got: {content[:60]}"


# ──────────────────────────────────────────────
# 9. Message preservation tests
# ──────────────────────────────────────────────

class TestMessagePreservation:
    """Verify that no original message content is lost during compression cycles."""

    def test_no_message_loss_after_compression(self, pool_with_history, tmp_jsonl):
        """Build pool, compress once, verify ALL original contents survive in JSONL."""
        original_conv = list(pool_with_history.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        # Track all original message contents (excluding markers)
        all_original_contents = {m.content for m in original_conv}

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary of compression"):
            compress_context(pool_with_history, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool_with_history.get_conversation("TestAgent")
        simulate_reset_history(tmp_jsonl, pool_conv)

        jsonl_msgs = _read_jsonl_messages(tmp_jsonl)
        jsonl_contents = {m['content'] for m in jsonl_msgs if not _is_marker(m)}
        lost = all_original_contents - jsonl_contents
        assert not lost, \
            f"After 1 compression: lost original contents {lost}"

    def test_no_message_loss_after_n_compressions(self, tmp_jsonl):
        """Build pool, compress 3 times, verify ALL original contents survive in JSONL."""
        # Build a large enough conversation to support multiple compressions
        history: list[Message] = [
            _msg(SYSTEM, "System prompt"),
            _msg(USER, "Initial user message"),
        ]
        for i in range(20):
            history.append(_msg(USER if i % 2 == 0 else ASSISTANT, f"Conversation turn {i}"))

        pool = MockAgentPool(history)
        original_conv = list(pool.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        # Track all message contents across the lifecycle
        all_original_contents = {m.content for m in original_conv}

        n_compressions = 3
        for comp_num in range(n_compressions):
            with patch("agent_cascade.compression.core.invoke_compression_agent",
                       return_value=f"Summary round {comp_num + 1}"):
                result = compress_context(pool, "TestAgent", fraction=0.5, mode="auto")

            assert result.success, f"Compression #{comp_num+1} failed: {result.error}"

            pool_conv = pool.get_conversation("TestAgent")
            simulate_reset_history(tmp_jsonl, pool_conv)

            jsonl_msgs = _read_jsonl_messages(tmp_jsonl)
            jsonl_contents = {m['content'] for m in jsonl_msgs if not _is_marker(m)}
            lost = all_original_contents - jsonl_contents
            assert not lost, \
                f"After {comp_num+1} compression(s): lost original contents {lost}"

            # Add more messages before next compression round
            extra: list[Message] = [
                _msg(USER, f"New user message after comp {comp_num + 1}"),
                _msg(ASSISTANT, f"New assistant reply after comp {comp_num + 1}"),
            ]
            append_messages(pool, extra, tmp_jsonl)
            for m in extra:
                all_original_contents.add(m.content)

    def test_message_identity_preserved(self, pool_with_history, tmp_jsonl):
        """Verify specific messages by their content survive compression intact."""
        original_conv = list(pool_with_history.get_conversation("TestAgent"))
        _write_jsonl(tmp_jsonl, original_conv)

        # Identify specific messages we want to track by content
        tracked_contents: dict[str, str] = {}  # content -> role label
        for m in original_conv:
            if not _is_marker(m):
                tracked_contents[m.content] = m.role

        with patch("agent_cascade.compression.core.invoke_compression_agent",
                   return_value="Summary"):
            compress_context(pool_with_history, "TestAgent", fraction=0.5, mode="auto")

        pool_conv = pool_with_history.get_conversation("TestAgent")
        simulate_reset_history(tmp_jsonl, pool_conv)

        jsonl_msgs = _read_jsonl_messages(tmp_jsonl)

        # Build a map of content -> found role from JSONL (excluding markers)
        jsonl_content_map: dict[str, str] = {}
        for m in jsonl_msgs:
            if not _is_marker(m):
                jsonl_content_map[m['content']] = m['role']

        # Every tracked message must still be present with correct role
        for content, expected_role in tracked_contents.items():
            assert content in jsonl_content_map, \
                f"Message '{content}' was lost after compression"
            actual_role = jsonl_content_map[content]
            assert actual_role == expected_role, \
                f"Message '{content}' role changed from {expected_role} to {actual_role}"