"""Integration tests for session load duplication fix.

Tests the ACTUAL production code paths (not just helpers):
  - AgentPool.load_session_from_log() with large sessions (63+ messages)
  - Reused instance path via lifecycle_manager logic
  - Direct log_message() appends after rewrite_log_with_history()
  - Multiple reload stress test

Runs against real AgentPool + AgentInstanceLogger — no LLM calls needed.
"""

import json
import os
import tempfile
from datetime import datetime

import pytest

from agent_cascade.llm.schema import SYSTEM, USER, ASSISTANT, Message
from agent_cascade.prompts.dna import COMPRESSION_MARKER


# ──────────────────────────────────────────────
# Fixtures & helpers
# ──────────────────────────────────────────────

DUMMY_LLM_CFG = {
    "model": "qwen/qwen3-4b",
    "model_server": "http://127.0.0.1:1234/v1",
    "api_key": "EMPTY",
    "model_type": "qwenvl_oai",
}


def build_large_session(num_pairs=32):
    """Build a realistic session with ~64 messages."""
    msgs = [Message(role=SYSTEM, content="You are a helpful coding assistant.")]
    for i in range(num_pairs):
        msgs.append(Message(role=USER, content=f"Question {i}: What is Python feature {i}?"))
        msgs.append(
            Message(role=ASSISTANT,
                    content=f"Answer {i}: Python feature {i} is powerful. Details: {'x' * 50}")
        )
    return msgs


def build_compressed_session(num_rounds=4, tail_pairs=3):
    """Build a session that went through multiple compression rounds."""
    msgs = [Message(role=SYSTEM, content="You are a helpful coding assistant.")]

    for i in range(10):
        msgs.append(Message(role=USER, content=f"Q{i}: Hello {i}"))
        msgs.append(Message(role=ASSISTANT, content=f"A{i}: Hi there {i}"))

    comp1 = f"{COMPRESSION_MARKER} (round 1) ---\n<context_summary>First 10 exchanges.</context_summary>"
    msgs.append(Message(role=USER, content=comp1))

    for i in range(10, 18):
        msgs.append(Message(role=USER, content=f"Q{i}: Question {i}"))
        msgs.append(Message(role=ASSISTANT, content=f"A{i}: Answer {i}"))

    comp2 = f"{COMPRESSION_MARKER} (round 2) ---\n<context_summary>Exchanges 10-18.</context_summary>"
    msgs.append(Message(role=USER, content=comp2))

    for i in range(18, 24):
        msgs.append(Message(role=USER, content=f"Q{i}: Query {i}"))
        msgs.append(Message(role=ASSISTANT, content=f"A{i}: Response {i}"))

    comp3 = f"{COMPRESSION_MARKER} (round 3) ---\n<context_summary>Queries 18-24.</context_summary>"
    msgs.append(Message(role=USER, content=comp3))

    for i in range(24, 24 + tail_pairs):
        msgs.append(Message(role=USER, content=f"Q{i}: Recent question {i}"))
        msgs.append(Message(role=ASSISTANT, content=f"A{i}: Recent answer {i}"))

    return msgs


def write_jsonl(path, messages, metadata=None):
    """Write a JSONL file with metadata header + message lines."""
    if metadata is None:
        metadata = {
            "agent_class": "coder",
            "instance_name": "TestAgent",
            "start_timestamp": datetime.now().isoformat(),
            "current_log_path": path,
        }
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps({"metadata": metadata}) + '\n')
        for m in messages:
            d = m.model_dump() if hasattr(m, 'model_dump') else dict(role=m.role, content=m.content)
            f.write(json.dumps(d, ensure_ascii=False) + '\n')


def read_jsonl_messages(path):
    """Read message dicts from a JSONL file (skip metadata/events)."""
    msgs = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict) and "metadata" not in item and "event" not in item:
                    msgs.append(item)
            except json.JSONDecodeError:
                continue
    return msgs


def check_duplicates(msgs):
    """Return list of duplicate (role, content_prefix) tuples."""
    counts = {}
    for m in msgs:
        c = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        r = m.get("role", "") if isinstance(m, dict) else getattr(m, "role", "")
        key = (r, c[:80])
        counts[key] = counts.get(key, 0) + 1
    return {k: v for k, v in counts.items() if v > 1}


# ──────────────────────────────────────────────
# Test 1: Large session load (65 msgs), verify no dups
# ──────────────────────────────────────────────

class TestLargeSessionLoad:
    """Verify loading a large session via AgentPool produces no duplicates."""

    def test_65_message_load(self, tmp_path):
        import uuid
        log_path = str(tmp_path / "large_session.jsonl")
        full_msgs = build_large_session(num_pairs=32)  # 65 messages
        write_jsonl(log_path, full_msgs)

        from agent_cascade.agent_pool import AgentPool
        pool = AgentPool(DUMMY_LLM_CFG)

        inst_name = f"LargeTest_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        loaded_msgs = read_jsonl_messages(log_inst.log_path)

        dups = check_duplicates(loaded_msgs)
        assert not dups, f"Duplicates found after large session load: {dups}"

        # With no compression markers, WS == full history
        inst = pool.get_instance(inst_name)
        ws_size = len(inst.conversation) if inst else 0
        assert ws_size == len(loaded_msgs) == len(full_msgs), \
            f"Size mismatch: WS={ws_size}, JSONL={len(loaded_msgs)}, expected={len(full_msgs)}"


# ──────────────────────────────────────────────
# Test 2: Compressed session load + append via log_message()
# ──────────────────────────────────────────────

class TestCompressedSessionAppend:
    """Verify no duplicates after loading compressed session and appending."""

    def test_load_then_append(self, tmp_path):
        import uuid
        log_path = str(tmp_path / "compressed_session.jsonl")
        full_msgs = build_compressed_session(num_rounds=3, tail_pairs=5)
        write_jsonl(log_path, full_msgs)

        from agent_cascade.agent_pool import AgentPool
        pool = AgentPool(DUMMY_LLM_CFG)

        inst_name = f"CompressTest_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        loaded_msgs = read_jsonl_messages(log_inst.log_path)

        # Check for duplicates immediately after load
        dups_after_load = check_duplicates(loaded_msgs)
        assert not dups_after_load, f"Duplicates found right after load: {dups_after_load}"

        # Simulate reused instance path: direct log_message() for new messages
        new_task = Message(role=USER, content="New task after session load")
        new_response = Message(role=ASSISTANT, content="I'll handle that task.")

        log_inst.log_message(new_task)
        log_inst.log_message(new_response)

        # Also append to working set (what lifecycle_manager does)
        inst = pool.get_instance(inst_name)
        if inst:
            inst.append_message(new_task)
            inst.append_message(new_response)

        final_msgs = read_jsonl_messages(log_inst.log_path)
        dups_after_append = check_duplicates(final_msgs)
        assert not dups_after_append, f"Duplicates after append: {dups_after_append}"

        # Verify new messages are at the end
        assert final_msgs[-1]["content"] == "I'll handle that task."
        assert len(final_msgs) == len(loaded_msgs) + 2


# ──────────────────────────────────────────────
# Test 3: Reused instance path simulation
# ──────────────────────────────────────────────

class TestReusedInstancePath:
    """Simulate lifecycle_manager.py reused instance flow."""

    def test_reuse_no_dups(self, tmp_path):
        orig_log = str(tmp_path / "original.jsonl")
        msgs = build_compressed_session(num_rounds=4, tail_pairs=3)
        write_jsonl(orig_log, msgs)

        from agent_cascade.agent_pool import AgentPool
        pool = AgentPool(DUMMY_LLM_CFG)

        status = pool.load_session_from_log(orig_log, target_instance="Worker")
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger("Worker", "coder")

        # FIX path (lifecycle_manager.py line 420-422): direct log_message()
        task_msg = Message(role=USER, content="Please analyze this code.")
        response_msg = Message(role=ASSISTANT, content="Here's my analysis...")

        log_inst.log_message(task_msg)
        log_inst.log_message(response_msg)

        # Verify JSONL integrity
        jsonl_msgs = read_jsonl_messages(log_inst.log_path)

        seen = set()
        dup_list = []
        for m in jsonl_msgs:
            key = f"{m['role']}:{hash(m['content'])}"
            if key in seen:
                dup_list.append((m['role'], m['content'][:60]))
            seen.add(key)

        assert not dup_list, f"Content duplicates: {dup_list[:5]}"

        # System message appears exactly once
        sys_msgs = [m for m in jsonl_msgs if m['role'] == SYSTEM]
        assert len(sys_msgs) == 1, f"System msg count: {len(sys_msgs)} (expected 1)"

        # Tail messages at the end
        assert jsonl_msgs[-2]['content'] == "Please analyze this code."
        assert jsonl_msgs[-1]['content'] == "Here's my analysis..."


# ──────────────────────────────────────────────
# Test 4: Multiple reloads (stress test)
# ──────────────────────────────────────────────

class TestMultipleReloads:
    """Reload the same session multiple times to catch accumulation bugs."""

    def test_five_reloads(self, tmp_path):
        log_path = str(tmp_path / "stress.jsonl")
        msgs = build_large_session(num_pairs=25)  # 51 messages
        write_jsonl(log_path, msgs)

        from agent_cascade.agent_pool import AgentPool

        for round_num in range(5):
            pool = AgentPool(DUMMY_LLM_CFG)
            status = pool.load_session_from_log(log_path, target_instance=f"Agent_{round_num}")

            assert not status.startswith("Error"), f"Round {round_num}: Load failed: {status}"

            log_inst = pool.get_logger(f"Agent_{round_num}", "coder")
            loaded = read_jsonl_messages(log_inst.log_path)

            dups = check_duplicates(loaded)
            assert not dups, f"Round {round_num}: Found duplicates!"

            # Each reload should produce same count (no accumulation)
            assert len(loaded) == len(msgs), \
                f"Round {round_num}: Expected {len(msgs)}, got {len(loaded)}"


# ──────────────────────────────────────────────
# Test 5: File sync flag verification
# ──────────────────────────────────────────────

class TestFileSyncFlag:
    """Verify rewrite_log_with_history sets _file_history_synced correctly."""

    def test_sync_flag_set(self, tmp_path):
        log_path = str(tmp_path / "sync_test.jsonl")
        msgs = build_compressed_session(num_rounds=3, tail_pairs=2)
        write_jsonl(log_path, msgs)

        from agent_cascade.agent_pool import AgentPool
        pool = AgentPool(DUMMY_LLM_CFG)

        status = pool.load_session_from_log(log_path, target_instance="SyncTest")
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger("SyncTest", "coder")
        assert log_inst._file_history_synced, \
            "_file_history_synced should be True after load_session_from_log"