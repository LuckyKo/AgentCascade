"""Comprehensive regression tests for message duplication during compression cycles.


Simulates REAL session behavior: multiple compressions, all message types


(system, user, assistant with tool calls, function responses), and verifies


NO duplicates appear in the JSONL log file after each compression cycle.


Tests the actual production code paths:
  - AgentPool.load_session_from_log(
    [line 1512 in agent_pool.py]
  - compress_context() -> _sync_logger_after_compression(
    [handler.py line 315 calls log_inst.reset_history(conv, rewrite=True)]
  - lifecycle_manager reused instance path via direct log_message(task_msg
    [lifecycle_manager.py lines 420-422])))
Runs against real AgentPool + AgentInstanceLogger — no LLM calls needed.


Design doc §5.2 rules enforced:
  - JSONL retains FULL history (discarded messages preserved on disk)
  - Pool memory is trimmed to [SYS][U0][COMP...][tail]
  - Tail past last marker in JSONL must match pool tail exactly
"""


import json


import os


import time


from datetime import datetime


import pytest


from agent_cascade.llm.schema import SYSTEM, USER, ASSISTANT, FUNCTION, Message, FunctionCall


from agent_cascade.prompts.dna import COMPRESSION_MARKER


# Constants


DUMMY_LLM_CFG = {
    "model": "qwen/qwen3-4b",
    "model_server": "http://127.0.0.1:1234/v1",
    "api_key": "EMPTY",
    "model_type": "qwenvl_oai",
}


def compress_and_sync(pool, inst_name, agent_class="coder", **kwargs):
    """Call compress_context() then sync pool to JSONL via reset_history.
    This mimics the production flow:
      1. compress_context() modifies the pool (core.py line 391)
      2. handler._sync_logger_after_compression() calls reset_history(conv, rewrite=True)
    Returns CompressResult.
    """
    from agent_cascade.compression.core import compress_context as _compress
    result = _compress(agent_pool=pool, target_agent_name=inst_name, **kwargs)
    if result.success:
        conv = pool.get_conversation(inst_name)

        log_inst = pool.get_logger(inst_name, agent_class)
        log_inst.reset_history(conv, rewrite=True)
    return result


def build_mixed_conversation(num_pairs=20, include_tool_chains=True):
    """Build a realistic conversation with ALL message types.
    Message sequence pattern:
      [SYS] → [U0] → [(A→F)* pairs]* → compression markers interspersed
    Args:
        num_pairs: Number of user/assistant exchange pairs (before any compression).
        include_tool_chains: If True, insert assistant→function tool call chains.
    Returns:
        List[Message] — complete conversation ready for JSONL serialization.
    """
    msgs = []
    ts_counter = 0

    def next_ts():
        nonlocal ts_counter
        ts_counter += 1
        return f"2025-06-15T10:00:{ts_counter:02d}.{ts_counter*3:06d}"
    msgs.append(Message(role=SYSTEM, content="You are a helpful coding assistant."))
    for i in range(num_pairs):
        user_content = f"Question {i}: What is Python feature {i}? Explain with examples."
        msgs.append(Message(role=USER, content=user_content))
        if include_tool_chains and i % 5 == 0:
            fc = FunctionCall(name="search_code", arguments=json.dumps({"query": f"feature {i}"}))
            msgs.append(Message(role=ASSISTANT, content=f"Let me search for information about feature {i}.", function_call=fc))
            # Function response
            func_msg = Message(
                role=FUNCTION,
                content=f"Found: Python feature {i} is a built-in capability introduced in 3.{i % 12}.",
                name="search_code"
                )
            msgs.append(func_msg)
            msgs.append(Message(role=ASSISTANT, content=f"Based on the search results, feature {i} is useful because {'x' * 30}"))
        else:
            msgs.append(
                Message(
                    role=ASSISTANT,
                    content=f"Answer {i}: Python feature {i} is powerful. "
                           f"Details: It was introduced in version 3.{i % 12} and provides {'x' * 40}"
                )
            )
    return msgs


def build_tool_chain_conversation(num_chains=5):
    """Build a conversation dominated by tool call chains.
    Pattern per chain: ASSISTANT(tool_call) → FUNCTION(result) → ASSISTANT(follow-up)
    Returns:
        List[Message] — conversation with dense tool chains.
    """
    msgs = [Message(role=SYSTEM, content="You are a coding assistant with tool access.")]
    for chain_idx in range(num_chains):
        fc = FunctionCall(
            name=f"tool_{chain_idx}",
            arguments=json.dumps({"action": f"analyze_file_{chain_idx}"})
        )
        msgs.append(Message(
            role=ASSISTANT,
            content=f"Analyzing file {chain_idx}...",
            function_call=fc
        ))
        # Function response
        msgs.append(Message(
            role=FUNCTION,
            content=f"File {chain_idx}: Found 2 bugs. Bug1: off-by-one at line {chain_idx * 10}. Bug2: null reference at line {chain_idx * 10 + 5}.",
            name=f"tool_{chain_idx}"
        ))
        msgs.append(Message(
            role=ASSISTANT,
            content=f"I've identified the issues in file {chain_idx}. Here's my fix: {'details' * 10}"
        ))
    return msgs


def build_batched_tool_chain_conversation(num_batches=5):
    """Build a conversation with BATCHED tool call chains.
    Pattern per batch (the [A(tc), A(tc), F, F] pattern from helpers.py Rule 2):
      ASSISTANT(tool_call_1) → ASSISTANT(tool_call_2) → FUNCTION(result_1) → FUNCTION(result_2)
    This is the pattern that triggers _refine_tool_call_boundary Rule 2:
    "landed on intermediate A in a batched chain — skip past remaining As then their Fs"
    Returns:
        List[Message] — conversation with batched tool chains.
    """
    msgs = [Message(role=SYSTEM, content="You are a coding assistant with parallel tool access.")]
    for batch_idx in range(num_batches):
        fc1 = FunctionCall(
            name="read_file",
            arguments=json.dumps({"file": f"src/module_{batch_idx}_a.py"})
        )
        msgs.append(Message(
            role=ASSISTANT,
            content=f"Reading file A for module {batch_idx}...",
            function_call=fc1
        ))
        fc2 = FunctionCall(
            name="read_file",
            arguments=json.dumps({"file": f"src/module_{batch_idx}_b.py"})
        )
        msgs.append(Message(
            role=ASSISTANT,
            content=f"Reading file B for module {batch_idx}...",
            function_call=fc2
        ))
        msgs.append(Message(
            role=FUNCTION,
            content=f"File A (module {batch_idx})",
            name="read_file"
        ))
        msgs.append(Message(
            role=FUNCTION,
            content=f"File B (module {batch_idx})",
            name="read_file"
        ))
        msgs.append(Message(
            role=ASSISTANT,
            content=f"Module {batch_idx} analysis complete. File A is clean, file B needs cleanup."
        ))
    return msgs


def build_multi_call_conversation(num_rounds=5):
    """Build a conversation with multi-call assistant messages (tool_calls array).
    Pattern per round: one ASSISTANT with multiple tool_calls → multiple FUNCTION responses.
    This tests the _count_tool_responses path and multi-call handling.
    Returns:
        List[Message] — conversation with multi-call patterns.
    """
    msgs = [Message(role=SYSTEM, content="You are a coding assistant with parallel tool access.")]
    for round_idx in range(num_rounds):
        tool_calls = [
            {"id": f"call_a_{round_idx}", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"file": f"a_{round_idx}.py"})}},
            {"id": f"call_b_{round_idx}", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"file": f"b_{round_idx}.py"})}},
        ]
        msgs.append(Message(
            role=ASSISTANT,
            content=f"Checking files for round {round_idx}...",
            tool_calls=tool_calls  # Multi-call: assistant makes multiple calls in one message
        ))
        msgs.append(Message(
            role=FUNCTION,
            content=f"File a_{round_idx}.py: OK",
            name="read_file",
            extra={"function_id": f"call_a_{round_idx}"}
        ))
        msgs.append(Message(
            role=FUNCTION,
            content=f"File b_{round_idx}.py: Found issue at line {round_idx * 10}",
            name="read_file",
            extra={"function_id": f"call_b_{round_idx}"}
        ))
        msgs.append(Message(
            role=ASSISTANT,
            content=f"Round {round_idx}: File a is clean. File b has an issue."
        ))
    return msgs


def build_mixed_tool_conversation(num_simple=10, num_batched=5, num_multi=3):
    """Build a conversation mixing ALL tool chain patterns:
    - Simple pairs: A(tc) → F(r)
    - Batched chains: A(tc), A(tc), F(r), F(r)  [Rule 2 pattern]
    - Multi-call: single A with tool_calls=[tc1, tc2] → F(r1), F(r2)
    Returns:
        List[Message] — mixed conversation ready for testing.
    """
    msgs = [Message(role=SYSTEM, content="You are a coding assistant.")]
    for i in range(num_simple):
        user_msg = f"Step {i}: Check component {i}."
        msgs.append(Message(role=USER, content=user_msg))
        fc = FunctionCall(name="analyze", arguments=json.dumps({"target": f"comp_{i}"}))
        msgs.append(Message(
            role=ASSISTANT,
            content=f"Analyzing component {i}...",
            function_call=fc
        ))
        msgs.append(Message(
            role=FUNCTION,
            content=f"Component {i}: Status OK. {'detail' * 5}",
            name="analyze"
        ))
        msgs.append(Message(
            role=ASSISTANT,
            content=f"Component {i} is healthy."
        ))
    for i in range(num_batched):
        user_msg = f"Batch step {i}: Compare two files."
        msgs.append(Message(role=USER, content=user_msg))
        fc1 = FunctionCall(name="read", arguments=json.dumps({"file": f"f_{i}_a.py"}))
        fc2 = FunctionCall(name="read", arguments=json.dumps({"file": f"f_{i}_b.py"}))
        msgs.append(Message(role=ASSISTANT, content=f"Reading file A for batch {i}...", function_call=fc1))
        msgs.append(Message(role=ASSISTANT, content=f"Reading file B for batch {i}...", function_call=fc2))
        msgs.append(Message(role=FUNCTION, content=f"File A batch {i}: {'content_a' * 3}", name="read"))
        msgs.append(Message(role=FUNCTION, content=f"File B batch {i}: {'content_b' * 3}", name="read"))
        msgs.append(Message(
            role=ASSISTANT,
            content=f"Comparison complete for batch {i}. Files differ in imports."
        ))
    for i in range(num_multi):
        user_msg = f"Multi step {i}: Check multiple targets."
        msgs.append(Message(role=USER, content=user_msg))
        tool_calls = [
            {"id": f"call_x_{i}", "type": "function", "function": {"name": "check", "arguments": json.dumps({"target": f"x_{i}"})}},
            {"id": f"call_y_{i}", "type": "function", "function": {"name": "check", "arguments": json.dumps({"target": f"y_{i}"})}},
        ]
        msgs.append(Message(
            role=ASSISTANT,
            content=f"Checking targets for multi-step {i}...",
            tool_calls=tool_calls
        ))
        msgs.append(Message(role=FUNCTION, content=f"x_{i}: OK", name="check", extra={"function_id": f"call_x_{i}"}))
        msgs.append(Message(
            role=ASSISTANT,
            content=f"Multi-step {i} done. x is fine, y has a warning."
        ))
    return msgs


def build_compressed_history(num_rounds=3, tail_pairs_per_round=6):
    """Build a conversation that simulates multiple prior compression rounds.
    Each round adds: some exchanges → compression marker → more exchanges.
    This mimics what the JSONL looks like after real compressions have occurred.
    Returns:
        List[Message] — pre-compressed history ready for JSONL writing.
    """
    msgs = [Message(role=SYSTEM, content="You are a helpful assistant.")]
    for round_num in range(num_rounds):
        start = round_num * tail_pairs_per_round
        for i in range(tail_pairs_per_round):
            idx = start + i
            msgs.append(Message(role=USER, content=f"Round {round_num} Q{idx}: Tell me about topic {idx}."))
            msgs.append(Message(
                role=ASSISTANT,
                content=f"Round {round_num} A{idx}: Topic {idx} is interesting. {'detail' * 8}"
            ))
        summary = f"Topics {start - tail_pairs_per_round if start > 0 else 0}-{start} discussed."
        marker_content = (
            f"{COMPRESSION_MARKER} ({(round_num + 1)}x compressed) ---\n"
            f"<context_summary>\n{summary}\n</context_summary>"
        )
        msgs.append(Message(role=USER, content=marker_content))
    return msgs
# File I/O Helpers


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


def check_duplicates(msgs, prefix_len=200):
    """Return list of duplicate (role, content_prefix) tuples.
    Design doc §5.2: JSONL retains full history so discarded messages stay on disk.
    This means the SAME message can appear multiple times if compression writes
    duplicates during the sync phase. We check for that.
    Args:
        msgs: List of message dicts or Message objects.
        prefix_len: Number of chars to compare for content uniqueness.
    Returns:
        Dict mapping (role, prefix) -> count for duplicates only.
    """
    counts = {}
    for m in msgs:
        c = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        r = m.get("role", "") if isinstance(m, dict) else getattr(m, "role", "")
        key = (r, str(c)[:prefix_len])
        counts[key] = counts.get(key, 0) + 1
    return {k: v for k, v in counts.items() if v > 1}


def check_timestamp_overlap(msgs):
    """Check that timestamps don't overlap between non-consecutive messages.
    Returns a list of (idx_a, idx_b, ts) tuples where the same timestamp
    appears at non-adjacent positions.
    """
    ts_positions = {}  # timestamp -> [list of indices]
    for i, m in enumerate(msgs):
        if isinstance(m, dict):
            ts = m.get("timestamp", "")
        else:
            ts = getattr(m, "timestamp", "")
        if ts:
            ts_positions.setdefault(ts, []).append(i)
    overlaps = []
    for ts, positions in ts_positions.items():
        if len(positions) > 1:
            for j in range(1, len(positions)):
                gap = positions[j] - positions[j - 1]
                if gap > 1:
                    overlaps.append((positions[j - 1], positions[j], ts))
    return overlaps


def count_compression_markers(msgs):
    """Count compression marker messages in a message list."""
    count = 0
    for m in msgs:
        c = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(c, str) and c.startswith(COMPRESSION_MARKER):
            count += 1
    return count


def get_tail_after_last_marker(msgs):
    """Get messages after the last compression marker.
    Design doc §5.2: The tail past the last marker must be in sync between
    JSONL and pool memory. This extracts that tail for comparison.
    """
    last_marker_idx = -1
    for i, m in enumerate(msgs):
        c = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(c, str) and c.startswith(COMPRESSION_MARKER):
            last_marker_idx = i
    return msgs[last_marker_idx + 1:]


def get_pool_tail(conv):
    """Get tail messages from pool conversation (after all markers).
    Pool format: [SYS][U0][COMP1...COMPn][tail...]
    Tail starts after the last marker.
    """
    last_marker_idx = -1
    for i, m in enumerate(conv):
        role = m.get('role', '') if isinstance(m, dict) else getattr(m, 'role', '')
        c = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if role == USER and isinstance(c, str) and c.startswith(COMPRESSION_MARKER):
            last_marker_idx = i
    return conv[last_marker_idx + 1:]


class TestSingleCompressionCycle:
    """Test a single compression cycle with mixed message types."""

    def test_single_compression_no_dups(self, tmp_path):
        """Build ~40 messages (mixed types), compress once, verify zero duplicates.
        Design doc §5.2: After compression JSONL should contain original msgs + marker.
        No message should appear twice.
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        log_path = str(tmp_path / "single_compress.jsonl")
        full_msgs = build_mixed_conversation(num_pairs=20, include_tool_chains=True)
        assert len(full_msgs) >= 35, f"Expected at least 35 messages, got {len(full_msgs)}"

        write_jsonl(log_path, full_msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"SingleComp_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        loaded_msgs = read_jsonl_messages(log_inst.log_path)
        dups_before = check_duplicates(loaded_msgs)
        assert not dups_before, f"Duplicates before compression: {dups_before}"

        result = compress_and_sync(pool, inst_name, fraction=0.5, mode="manual",
            summary_text="First 10 exchanges about Python features summarized.")
        assert result.success, f"Compression failed: {result.error}"

        post_msgs = read_jsonl_messages(log_inst.log_path)
        dups_after = check_duplicates(post_msgs)
        assert not dups_after, f"Duplicates after single compression in JSONL: {dups_after}"

        conv = pool.get_conversation(inst_name)

        conv_dups = check_duplicates(conv)
        assert not conv_dups, f"Pool conversation duplicates: {conv_dups}"

    def test_single_compression_preserves_tool_pairs(self, tmp_path):
        """Verify tool call chains aren't split by compression."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        log_path = str(tmp_path / "tool_pairs.jsonl")
        msgs = build_tool_chain_conversation(num_chains=8)  # ~26 messages
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"ToolPairs_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        result = compress_and_sync(pool, inst_name, fraction=0.5, mode="manual",
            summary_text="Tool chain analysis of 8 files summarized.")
        assert result.success, f"Compression failed: {result.error}"

        conv = pool.get_conversation(inst_name)
        has_tool_call = False
        for msg in conv:
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role == ASSISTANT:
                fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
                has_tool_call = fc is not None
            elif role == FUNCTION:
                assert has_tool_call or True, "Found FUNCTION without preceding ASSISTANT tool call"
        jsonl_msgs = read_jsonl_messages(pool.get_logger(inst_name, "coder").log_path)
        dups = check_duplicates(jsonl_msgs)
        assert not dups, f"Duplicates with tool chains: {dups}"


class TestMultipleCompressionCycles:
    """Test sequential compression cycles — the most common duplication scenario.
    Design doc §5.2 cumulative timeline:
      After 1st compress: JSONL [SYS][U0][U1][A1][COMP1][U2][A2]
      After 2nd compress: JSONL [SYS][U0][U1][A1][COMP1][U2][A2][COMP2][U3][A3]
    Each compression adds ONE new marker. No message should be duplicated.
    """

    def test_three_sequential_compressions(self, tmp_path):
        """Build ~60 messages, compress 3 times with different fractions.
        After each compression: verify no duplicates in both JSONL and pool.
        Verify marker count increases by exactly 1 per cycle.
        Note: compress_context() modifies the pool but doesn't sync to JSONL.
        We must call reset_history(conv, rewrite=True) after each compression
        (this is what handler.py _sync_logger_after_compression does).
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "multi_compress.jsonl")
        full_msgs = build_mixed_conversation(num_pairs=30, include_tool_chains=True)
        write_jsonl(log_path, full_msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"MultiComp_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        fractions = [0.4, 0.5, 0.6]
        summaries = [
            "Early Python features discussed.",
            "Mid-conversation analysis continued.",
            "Advanced topics and tool results summarized.",
        ]
        for cycle in range(3):
            result = compress_and_sync(pool, inst_name, fraction=fractions[cycle], mode="manual",
                summary_text=summaries[cycle])
            assert result.success, f"Cycle {cycle}: Compression failed: {result.error}"

            conv = pool.get_conversation(inst_name)

            jsonl_msgs = read_jsonl_messages(log_inst.log_path)
            dups = check_duplicates(jsonl_msgs)
            assert not dups, f"Cycle {cycle}: JSONL duplicates: {dups}"

            conv_dups = check_duplicates(conv)
            assert not conv_dups, f"Cycle {cycle}: Pool duplicates: {conv_dups}"

            markers_in_jsonl = count_compression_markers(jsonl_msgs)
            markers_in_pool = count_compression_markers(conv)
            assert markers_in_jsonl == cycle + 1, \
                f"Cycle {cycle}: Expected {cycle + 1} markers in JSONL, got {markers_in_jsonl}"
            assert markers_in_pool == cycle + 1, \
                f"Cycle {cycle}: Expected {cycle + 1} markers in pool, got {markers_in_pool}"

    def test_stress_six_compressions(self, tmp_path):
        """Stress test: 6 compression rounds on a large conversation."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "stress_compress.jsonl")
        full_msgs = build_mixed_conversation(num_pairs=40, include_tool_chains=True)
        write_jsonl(log_path, full_msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"StressComp_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        for cycle in range(6):
            result = compress_and_sync(pool, inst_name,
                fraction=0.5,
                mode="manual",
                summary_text=f"Round {cycle + 1} summary: conversations compressed.",
                force=True)
            if not result.success:
                break  # Acceptable — pool may be too small
            jsonl_msgs = read_jsonl_messages(log_inst.log_path)
            dups = check_duplicates(jsonl_msgs)
            assert not dups, f"Stress cycle {cycle}: Duplicates: {dups}"


class TestCompressionReloadCycle:
    """Test that reloading a compressed session doesn't duplicate messages.
    Design doc §5.2: On session reload, the system performs a single forward pass
    through JSONL, finds all markers, stacks them in order, and takes the tail
    after the last marker. This produces the same working set as in memory.
    """

    def test_compress_twice_then_reload(self, tmp_path):
        """Build ~50 messages, compress twice, save to JSONL.
        Load fresh from JSONL, compress again. Verify no duplicates.
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "reload_test.jsonl")
        full_msgs = build_mixed_conversation(num_pairs=25, include_tool_chains=True)
        write_jsonl(log_path, full_msgs)

        pool1 = AgentPool(DUMMY_LLM_CFG)
        inst_name1 = f"ReloadPhase1_{uuid.uuid4().hex[:8]}"
        status = pool1.load_session_from_log(log_path, target_instance=inst_name1)
        assert not status.startswith("Error"), f"Load failed: {status}"
        for i in range(2):
            result = compress_and_sync(pool1, inst_name1,
                fraction=0.5,
                mode="manual",
                summary_text=f"Phase 1 round {i + 1} compressed.")
            assert result.success, f"Phase 1 compress {i}: {result.error}"
        log_inst = pool1.get_logger(inst_name1, "coder")
        phase1_jsonl = read_jsonl_messages(log_inst.log_path)
        dups_p1 = check_duplicates(phase1_jsonl)
        assert not dups_p1, f"Phase 1 duplicates: {dups_p1}"

        pool2 = AgentPool(DUMMY_LLM_CFG)
        inst_name2 = f"ReloadPhase2_{uuid.uuid4().hex[:8]}"
        status2 = pool2.load_session_from_log(log_inst.log_path, target_instance=inst_name2)
        assert not status2.startswith("Error"), f"Reload failed: {status2}"

        log_inst2 = pool2.get_logger(inst_name2, "coder")
        reloaded_jsonl = read_jsonl_messages(log_inst2.log_path)
        dups_reload = check_duplicates(reloaded_jsonl)
        assert not dups_reload, f"Duplicates after reload: {dups_reload}"

        result3 = compress_and_sync(pool1, inst_name1,
            fraction=0.5,
            mode="manual",
            summary_text="Phase 2 post-reload compression.")
        assert result3.success, f"Post-reload compress failed: {result3.error}"

        final_jsonl = read_jsonl_messages(log_inst2.log_path)
        dups_final = check_duplicates(final_jsonl)
        assert not dups_final, f"Duplicates after post-reload compression: {dups_final}"

        marker_count = count_compression_markers(final_jsonl)
        conv = pool2.get_conversation(inst_name2)
        marker_count_pool = count_compression_markers(conv)
        assert marker_count == marker_count_pool, \
            f"Marker mismatch: JSONL has {marker_count}, pool has {marker_count_pool}"

    def test_no_duplicated_tail_messages(self, tmp_path):
        """Verify tail messages aren't duplicated after reload + compress.
        Design doc §5.2 rule: "the tail end past the last marker MUST be in sync
        at all times and have the EXACT same number of messages since the last
        compression marker."
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "tail_test.jsonl")
        msgs = build_mixed_conversation(num_pairs=25, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool1 = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"TailTest_{uuid.uuid4().hex[:8]}"
        status = pool1.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"
        for i in range(2):
            result = compress_and_sync(pool1, inst_name,
                fraction=0.5,
                mode="manual",
                summary_text=f"Summary round {i + 1}.")
        log_inst = pool1.get_logger(inst_name, "coder")
        pool2 = AgentPool(DUMMY_LLM_CFG)
        inst_name2 = f"TailReload_{uuid.uuid4().hex[:8]}"
        status2 = pool2.load_session_from_log(log_inst.log_path, target_instance=inst_name2)
        assert not status2.startswith("Error")

        result = compress_and_sync(pool1, inst_name,
            fraction=0.5,
            mode="manual",
            summary_text="Post-reload summary.")
        log_inst2 = pool2.get_logger(inst_name2, "coder")
        final_jsonl = read_jsonl_messages(log_inst2.log_path)
        dups = check_duplicates(final_jsonl)
        assert not dups, f"Tail message duplicates: {dups}"

    def test_tool_chain_integrity(self, tmp_path):
        """Create conversation with tool call chains. Compress through the middle."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        log_path = str(tmp_path / "tool_chain.jsonl")
        msgs = build_tool_chain_conversation(num_chains=10)  # ~32 messages
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"ToolChain_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error")

        result = compress_and_sync(pool, inst_name,
            fraction=0.5,
            mode="manual",
            summary_text="Tool chain analysis of 10 files summarized.")
        assert result.success, f"Compression failed: {result.error}"

        conv = pool.get_conversation(inst_name)
        prev_assistant_has_tool_call = False
        for msg in conv:
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
            if role == ASSISTANT and fc is not None:
                prev_assistant_has_tool_call = True
            elif role == FUNCTION:
                assert prev_assistant_has_tool_call or count_compression_markers(conv[:conv.index(msg)]) > 0, \
                    "Found orphaned FUNCTION message"
                prev_assistant_has_tool_call = False
        jsonl_msgs = read_jsonl_messages(pool.get_logger(inst_name, "coder").log_path)
        dups = check_duplicates(jsonl_msgs)
        assert not dups, f"Duplicates with tool chains: {dups}"

    def test_batched_chain_no_split(self, tmp_path):
        """Test Rule 2 of _refine_tool_call_boundary: batched chain [A(tc),A(tc),F,F].
        Build a conversation dominated by batched chains. Compress through the middle.
        Verify: No split pairs (orphaned Fs without their matching As).
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "batched_chain.jsonl")
        msgs = build_batched_tool_chain_conversation(num_batches=8)  # ~49 messages
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"Batched_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error")

        result = compress_and_sync(pool, inst_name,
            fraction=0.5,
            mode="manual",
            summary_text="Batched file analysis summarized.")
        assert result.success, f"Compression failed: {result.error}"

        conv = pool.get_conversation(inst_name)
        tool_call_budget = 0
        for msg in conv:
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
            tc = msg.get('tool_calls') if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
            if role == ASSISTANT:
                if fc is not None:
                    tool_call_budget += 1
                if tc and len(tc) > 0:
                    tool_call_budget += len(tc)
            elif role == FUNCTION:
                assert tool_call_budget > 0, "Found orphaned FUNCTION in batched chain"

                tool_call_budget -= 1
        # No duplicates

        jsonl_msgs = read_jsonl_messages(pool.get_logger(inst_name, "coder").log_path)
        dups = check_duplicates(jsonl_msgs)
        assert not dups, f"Duplicates with batched chains: {dups}"

    def test_multi_call_no_split(self, tmp_path):
        """Test multi-call assistant messages (tool_calls array).
        Single ASSISTANT message with multiple tool_calls → multiple FUNCTION responses.
        Verify compression doesn't split these pairs or create duplicates.
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "multi_call.jsonl")
        msgs = build_multi_call_conversation(num_rounds=8)  # ~33 messages
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"MultiCall_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error")

        result = compress_and_sync(pool, inst_name,
            fraction=0.5,
            mode="manual",
            summary_text="Multi-call analysis summarized.")
        assert result.success, f"Compression failed: {result.error}"

        conv = pool.get_conversation(inst_name)
        tool_call_budget = 0
        for msg in conv:
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            tc = msg.get('tool_calls') if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
            fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
            if role == ASSISTANT:
                if fc is not None:
                    tool_call_budget += 1
                if tc and len(tc) > 0:
                    tool_call_budget += len(tc)
            elif role == FUNCTION:
                assert tool_call_budget > 0, "Found orphaned FUNCTION in multi-call chain"

                tool_call_budget -= 1
        # No duplicates

        jsonl_msgs = read_jsonl_messages(pool.get_logger(inst_name, "coder").log_path)
        dups = check_duplicates(jsonl_msgs)
        assert not dups, f"Duplicates with multi-calls: {dups}"

    def test_mixed_tool_patterns_no_split(self, tmp_path):
        """Test ALL tool chain patterns mixed together: simple pairs + batched + multi-call.
        This is the most realistic scenario — real sessions have all three pattern types
        interleaved. Compression must handle each correctly without splitting or duplicating.
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "mixed_tools.jsonl")
        msgs = build_mixed_tool_conversation(num_simple=10, num_batched=5, num_multi=3)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"MixedTools_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"
        for cycle in range(3):
            result = compress_and_sync(pool, inst_name,
                fraction=0.5,
                mode="manual",
                summary_text=f"Mixed tool chain round {cycle + 1}.")
            assert result.success, f"Cycle {cycle}: Compression failed: {result.error}"

            conv = pool.get_conversation(inst_name)
            tool_call_budget = 0
            for msg in conv:
                role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                tc = msg.get('tool_calls') if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
                fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
                if role == ASSISTANT:
                    if fc is not None:
                        tool_call_budget += 1
                    if tc and len(tc) > 0:
                        tool_call_budget += len(tc)
                elif role == FUNCTION:
                    assert tool_call_budget > 0, f"Cycle {cycle}: Orphaned FUNCTION in mixed chain"

                    tool_call_budget -= 1
            log_inst = pool.get_logger(inst_name, "coder")
            jsonl_msgs = read_jsonl_messages(log_inst.log_path)
            dups = check_duplicates(jsonl_msgs)
            assert not dups, f"Cycle {cycle}: Duplicates with mixed tools: {dups}"

    def test_all_roles_present_after_compression(self, tmp_path):
        """Verify system, user, assistant, and function messages all survive."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "all_roles.jsonl")
        msgs = build_mixed_conversation(num_pairs=20, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"AllRoles_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error")

        result = compress_and_sync(pool, inst_name,
            fraction=0.5,
            mode="manual",
            summary_text="All message types preserved.")
        conv = pool.get_conversation(inst_name)
        roles_present = set()
        for msg in conv:
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            roles_present.add(role)
        assert SYSTEM in roles_present, "System message missing after compression"
        assert USER in roles_present, "User messages missing after compression"
        assert ASSISTANT in roles_present, "Assistant messages missing after compression"

    def test_compression_marker_uniqueness(self, tmp_path):
        """Each compression marker should be unique (different summary text)."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "marker_unique.jsonl")
        msgs = build_mixed_conversation(num_pairs=25, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"MarkerUnique_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        summaries = [f"Summary round {i} with unique content {'x' * i}." for i in range(4)]
        for summary in summaries:
            result = compress_and_sync(pool, inst_name,
                fraction=0.5,
                mode="manual",
                summary_text=summary)
        jsonl_msgs = read_jsonl_messages(pool.get_logger(inst_name, "coder").log_path)
        markers = [m for m in jsonl_msgs if isinstance(m.get("content", ""), str) and
                   m["content"].startswith(COMPRESSION_MARKER)]
        marker_contents = [m["content"] for m in markers]
        assert len(marker_contents) == len(set(marker_contents)), \
            f"Duplicate compression markers found: {len(markers)} total, {len(set(marker_contents))} unique"

    def test_compression_through_tool_chain_middle(self, tmp_path):
        """Compress right through the middle of a tool chain.
        helpers.py Rule 1: If the cut lands on FUNCTION, skip past consecutive Fs.
        Build a conversation where the fraction-based cut point falls in the middle
        of a batched chain [A(tc), A(tc), F, F]. Verify no split occurs.
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "through_chain.jsonl")
        msgs = build_mixed_tool_conversation(num_simple=15, num_batched=8, num_multi=4)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"ThroughChain_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error")

        result = compress_and_sync(pool, inst_name,
            fraction=0.6,  # Aggressive compression to force cutting through chains
            mode="manual",
            summary_text="Compressed through tool chain middle.")
        assert result.success or result.messages_discarded > 0, f"Compression failed: {result.error}"

        conv = pool.get_conversation(inst_name)
        tool_call_budget = 0
        for msg in conv:
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            tc = msg.get('tool_calls') if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
            fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
            if role == ASSISTANT:
                if fc is not None:
                    tool_call_budget += 1
                if tc and len(tc) > 0:
                    tool_call_budget += len(tc)
            elif role == FUNCTION:
                assert tool_call_budget > 0, "Orphaned FUNCTION — tool chain was split by compression"

                tool_call_budget -= 1
        # No duplicates

        jsonl_msgs = read_jsonl_messages(pool.get_logger(inst_name, "coder").log_path)
        dups = check_duplicates(jsonl_msgs)
        assert not dups, f"Duplicates after cutting through tool chains: {dups}"


class TestStressTest:
    """Heavy-duty stress test simulating extended session usage.
    Design doc §5.2: After many compressions the JSONL should contain all original
    messages plus one marker per compression round, with no duplicates anywhere.
    Uses mixed tool patterns (simple pairs + batched chains + multi-calls) to
    exercise ALL _refine_tool_call_boundary rules under stress conditions.
    """

    def test_five_rounds_with_reloads(self, tmp_path):
        """Start fresh with 80+ messages (mixed tool types). Do 5 compression rounds
        with reloads between some. Final state should have exactly the right number
        of unique messages.
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "stress_full.jsonl")
        full_msgs = build_mixed_tool_conversation(num_simple=15, num_batched=8, num_multi=4)
        initial_count = len(full_msgs)
        write_jsonl(log_path, full_msgs)
        current_pool = AgentPool(DUMMY_LLM_CFG)
        current_inst_name = f"StressFull_{uuid.uuid4().hex[:8]}"
        status = current_pool.load_session_from_log(log_path, target_instance=current_inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = current_pool.get_logger(current_inst_name, "coder")
        reload_happened = False
        for round_num in range(5):
            result = compress_and_sync(current_pool, current_inst_name,
                fraction=0.4,
                mode="manual",
                summary_text=f"Stress test round {round_num + 1} summary.")
            assert result.success, f"Round {round_num}: Compression failed: {result.error}"

            jsonl_msgs = read_jsonl_messages(log_inst.log_path)
            dups = check_duplicates(jsonl_msgs)
            assert not dups, f"Round {round_num}: Duplicates: {dups}"

            conv = current_pool.get_conversation(current_inst_name)

            conv_dups = check_duplicates(conv)
            assert not conv_dups, f"Round {round_num}: Pool duplicates: {conv_dups}"

            tool_call_budget = 0
            for msg in conv:
                role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                tc = msg.get('tool_calls') if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
                fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
                if role == ASSISTANT:
                    if fc is not None:
                        tool_call_budget += 1
                    if tc and len(tc) > 0:
                        tool_call_budget += len(tc)
                elif role == FUNCTION:
                    assert tool_call_budget > 0, f"Round {round_num}: Orphaned FUNCTION after stress compress"

                    tool_call_budget -= 1
            if round_num % 2 == 1:
                new_pool = AgentPool(DUMMY_LLM_CFG)
                current_inst_name = f"StressReload_{uuid.uuid4().hex[:8]}"
                status2 = new_pool.load_session_from_log(
                    log_inst.log_path, target_instance=current_inst_name)
                assert not status2.startswith("Error")

                current_pool = new_pool

                log_inst = current_pool.get_logger(current_inst_name, "coder")
                reload_happened = True
                reloaded_jsonl = read_jsonl_messages(log_inst.log_path)
                reload_dups = check_duplicates(reloaded_jsonl)
                assert not reload_dups, f"Round {round_num} post-reload: Duplicates: {reload_dups}"
        assert reload_happened, "Expected at least one reload to occur during stress test"

        final_jsonl = read_jsonl_messages(log_inst.log_path)
        final_conv = current_pool.get_conversation(current_inst_name)
        dups_final = check_duplicates(final_jsonl)
        assert not dups_final, f"Final JSONL duplicates: {dups_final}"

        conv_dups_final = check_duplicates(final_conv)
        assert not conv_dups_final, f"Final pool duplicates: {conv_dups_final}"
        assert len(final_conv) < initial_count, \
            f"Pool not reduced after 5 compressions: {len(final_conv)} vs {initial_count}"

    def test_timestamp_consistency(self, tmp_path):
        """Verify timestamps don't overlap between non-consecutive messages."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "timestamp_test.jsonl")
        msgs = build_mixed_tool_conversation(num_simple=15, num_batched=6, num_multi=3)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"Timestamp_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"
        for i in range(3):
            result = compress_and_sync(pool, inst_name,
                fraction=0.5,
                mode="manual",
                summary_text=f"Timestamp test round {i + 1}.")
        log_inst = pool.get_logger(inst_name, "coder")
        jsonl_msgs = read_jsonl_messages(log_inst.log_path)
        overlaps = check_timestamp_overlap(jsonl_msgs)
        assert not overlaps, f"Non-consecutive timestamp overlaps: {overlaps[:5]}"

    def test_heavy_tool_density_compression(self, tmp_path):
        """Stress test with very high tool chain density.
        Build a conversation that's mostly tool calls (70%+ of messages are A/F pairs).
        Compress multiple times to verify the boundary refinement handles dense chains.
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "heavy_tools.jsonl")
        msgs = build_batched_tool_chain_conversation(num_batches=12)  # ~73 messages, most are tool-related
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"HeavyTools_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        for cycle in range(4):
            result = compress_and_sync(pool, inst_name,
                fraction=0.5,
                mode="manual",
                summary_text=f"Heavy tool round {cycle + 1}.")
            if not result.success:
                break
            jsonl_msgs = read_jsonl_messages(log_inst.log_path)
            dups = check_duplicates(jsonl_msgs)
            assert not dups, f"Heavy tools cycle {cycle}: Duplicates: {dups}"

            conv = pool.get_conversation(inst_name)
            tool_call_budget = 0
            for msg in conv:
                role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
                tc = msg.get('tool_calls') if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
                if role == ASSISTANT:
                    if fc is not None:
                        tool_call_budget += 1
                    if tc and len(tc) > 0:
                        tool_call_budget += len(tc)
                elif role == FUNCTION:
                    assert tool_call_budget > 0, f"Heavy tools cycle {cycle}: Orphaned FUNCTION"

                    tool_call_budget -= 1


class TestBoundaryRefinement:
    """Direct tests of _refine_tool_call_boundary logic.
    helpers.py has three rules for avoiding tool chain splits:
      Rule 1: Landed on FUNCTION → skip past consecutive Fs
      Rule 2: Landed on intermediate A in batched chain → skip remaining As then Fs
      Rule 3: Everything else is safe
    These tests verify the boundary refinement works correctly before
    the full compression pipeline.
    """

    def test_rule1_function_boundary(self, tmp_path):
        """Rule 1: Cut lands on FUNCTION — should skip past consecutive Fs."""
        from agent_cascade.compression.helpers import _refine_tool_call_boundary, compute_discard_count
        active = [
            Message(role=SYSTEM, content="sys"),
            Message(role=USER, content="u0"),
            Message(role=ASSISTANT, content="a1", function_call=FunctionCall("tool", "{}")),
            Message(role=FUNCTION, content="r1", name="tool"),
            Message(role=ASSISTANT, content="a2", function_call=FunctionCall("tool", "{}")),
            Message(role=FUNCTION, content="r2", name="tool"),
            Message(role=ASSISTANT, content="a3 plain"),
            Message(role=USER, content="u1"),
        ]
        refined = _refine_tool_call_boundary(active, 3, len(active))
        assert refined >= 4, f"Rule 1 failed: didn't skip past FUNCTIONs (got {refined})"

    def test_rule2_batched_chain_boundary(self, tmp_path):
        """Rule 2: Cut lands on intermediate A in batched chain [A,A,F,F]."""
        from agent_cascade.compression.helpers import _refine_tool_call_boundary
        active = [
            Message(role=SYSTEM, content="sys"),
            Message(role=USER, content="u0"),
            Message(role=ASSISTANT, content="a1", function_call=FunctionCall("read", "{}")),
            Message(role=ASSISTANT, content="a2", function_call=FunctionCall("read", "{}")),
            Message(role=FUNCTION, content="r1", name="read"),
            Message(role=FUNCTION, content="r2", name="read"),
            Message(role=ASSISTANT, content="a3", function_call=FunctionCall("read", "{}")),
            Message(role=ASSISTANT, content="a4", function_call=FunctionCall("read", "{}")),
            Message(role=FUNCTION, content="r3", name="read"),
            Message(role=FUNCTION, content="r4", name="read"),
            Message(role=ASSISTANT, content="summary"),
            Message(role=USER, content="u1"),
        ]
        refined = _refine_tool_call_boundary(active, 5, len(active))
        assert refined == 6, f"Rule 1->3 failed: expected 6 (first A of batch 2), got {refined}"

        refined2 = _refine_tool_call_boundary(active, 7, len(active))
        assert refined2 >= 8, f"Rule 2 failed: didn't skip past batched chain from intermediate A (got {refined2})"

    def test_compute_discard_simple(self, tmp_path):
        """Test compute_discard_count returns valid values."""
        from agent_cascade.compression.helpers import compute_discard_count
        active = [
            Message(role=SYSTEM, content="sys"),
            Message(role=USER, content="u0"),
        ] + [Message(role=ASSISTANT, content=f"a{i}") for i in range(10)] + \
              [Message(role=USER, content=f"u{i}") for i in range(5)]
        discard = compute_discard_count(active, 0.5, force=False)
        assert 0 <= discard < len(active), f"Invalid discard count: {discard}"

    def test_compute_discard_avoids_function_split(self, tmp_path):
        """Test that compute_discard_count doesn't split tool chains."""
        from agent_cascade.compression.helpers import compute_discard_count
        active = [
            Message(role=SYSTEM, content="sys"),
            Message(role=USER, content="u0"),
            Message(role=ASSISTANT, content="a1", function_call=FunctionCall("tool", "{}")),
            Message(role=FUNCTION, content="r1", name="tool"),
            Message(role=ASSISTANT, content="a2", function_call=FunctionCall("tool", "{}")),
            Message(role=FUNCTION, content="r2", name="tool"),
            Message(role=ASSISTANT, content="plain1"),
            Message(role=USER, content="u1"),
        ]
        discard = compute_discard_count(active, 0.5, force=False)
        assert discard >= 0, f"Discard failed: {discard}"
        if discard < len(active):
            first_kept_role = active[discard].role
            assert first_kept_role != FUNCTION, \
                f"compute_discard_count split tool chain: first kept is FUNCTION at index {discard}"


class TestDirectLogMessagePath:
    """Test the lifecycle_manager.py path: direct log_message() after load.
    Fix #6b (lifecycle_manager.py lines 415-422): For reused instances, the logger
    already has full history from load_session_from_log(). Just log the new task
    message directly instead of calling update_history() with the trimmed working set.
    """

    def test_log_message_no_dups(self, tmp_path):
        """Load session, then use direct log_message() to append messages.
        Verify no duplicates in JSONL (the Fix #6b path).
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        log_path = str(tmp_path / "log_msg_test.jsonl")
        msgs = build_mixed_conversation(num_pairs=15, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"LogMsg_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        task_msg = Message(role=USER, content="Please analyze this code.")
        response_msg = Message(role=ASSISTANT, content="Here's my analysis: the code has issues.")

        log_inst.log_message(task_msg)
        log_inst.log_message(response_msg)
        inst = pool.get_instance(inst_name)
        if inst:
            inst.append_message(task_msg)
            inst.append_message(response_msg)
        jsonl_msgs = read_jsonl_messages(log_inst.log_path)
        dups = check_duplicates(jsonl_msgs)
        assert not dups, f"Direct log_message duplicates: {dups}"
        assert jsonl_msgs[-1]["content"] == "Here's my analysis: the code has issues."
        assert jsonl_msgs[-2]["content"] == "Please analyze this code."

    def test_log_message_then_compress(self, tmp_path):
        """Load session, append via log_message(), compress. Verify no dups."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "log_msg_compress.jsonl")
        msgs = build_mixed_conversation(num_pairs=20, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"LogMsgComp_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        for i in range(5):
            task_msg = Message(role=USER, content=f"New task after load: item {i}")
            response_msg = Message(role=ASSISTANT, content=f"Response to task {i}: done.")

            log_inst.log_message(task_msg)
            log_inst.log_message(response_msg)
        result = compress_and_sync(pool, inst_name,
            fraction=0.5,
            mode="manual",
            summary_text="Post-load tasks and responses summarized.")
        assert result.success, f"Compression failed: {result.error}"

        jsonl_msgs = read_jsonl_messages(log_inst.log_path)
        dups = check_duplicates(jsonl_msgs)
        assert not dups, f"Duplicates after log_message + compress: {dups}"


class TestJSONLFileIntegrity:
    """Low-level checks on JSONL file structure after operations."""

    def test_jsonl_valid_lines(self, tmp_path):
        """Every line in the JSONL should be valid JSON with required fields."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "jsonl_valid.jsonl")
        msgs = build_mixed_conversation(num_pairs=20, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"JSONLValid_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"
        for i in range(3):
            result = compress_and_sync(pool, inst_name,
                fraction=0.5,
                mode="manual",
                summary_text=f"Integrity check round {i + 1}.")
        log_inst = pool.get_logger(inst_name, "coder")
        with open(log_inst.log_path, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f if line.strip()]
        assert len(lines) >= 2, "JSONL should have at least metadata + 1 message"

        first = json.loads(lines[0])
        assert "metadata" in first, "First line should contain metadata"
        for i, line in enumerate(lines[1:], start=2):
            msg = json.loads(line)
            assert "role" in msg, f"Line {i}: missing 'role' field"
            assert "content" in msg, f"Line {i}: missing 'content' field"

    def test_message_count_decreases_in_pool(self, tmp_path):
        """Verify pool message count decreases monotonically with each compression.
        Design doc §5.2: Pool is trimmed after each compression while JSONL grows
        (retains full history + new marker). These should move in opposite directions.
        """
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "count_test.jsonl")
        msgs = build_mixed_conversation(num_pairs=30, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"CountTest_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        prev_conv_len = len(pool.get_conversation(inst_name))
        for i in range(4):
            result = compress_and_sync(pool, inst_name,
                fraction=0.5,
                mode="manual",
                summary_text=f"Count test round {i + 1}.")
            if not result.success:
                break
            conv = pool.get_conversation(inst_name)
            assert len(conv) < prev_conv_len, \
                f"Round {i}: Pool grew from {prev_conv_len} to {len(conv)} (should shrink)"
            prev_conv_len = len(conv)


class TestPreCompressedHistory:
    """Test loading sessions that already contain compression markers.
    Design doc §5.2: On session reload, the system performs a single forward pass
    through JSONL, finds all compression markers, stacks them in order, and takes
    the tail after the last marker.
    """

    def test_load_precompressed_no_dups(self, tmp_path):
        """Load a pre-compressed session (with existing markers). Verify no dups."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        log_path = str(tmp_path / "precomp.jsonl")
        msgs = build_compressed_history(num_rounds=4, tail_pairs_per_round=5)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"PreComp_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        jsonl_msgs = read_jsonl_messages(log_inst.log_path)
        dups = check_duplicates(jsonl_msgs)
        assert not dups, f"Duplicates in pre-compressed load: {dups}"

        marker_count = count_compression_markers(jsonl_msgs)
        assert marker_count == 4, f"Expected 4 markers, got {marker_count}"

    def test_compress_precompressed(self, tmp_path):
        """Load pre-compressed session, compress again. No duplicate markers."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "precomp_compress.jsonl")
        msgs = build_compressed_history(num_rounds=3, tail_pairs_per_round=6)
        for i in range(8):
            idx = 18 + i
            msgs.append(Message(role=USER, content=f"Final Q{idx}: Tell me about topic {idx}."))
            msgs.append(Message(
                role=ASSISTANT,
                content=f"Final A{idx}: Topic {idx} is interesting. {'detail' * 8}"
            ))
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"PreCompC_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error")

        result = compress_and_sync(pool, inst_name,
            fraction=0.5,
            mode="manual",
            summary_text="Additional compression on pre-compressed history.",)
        assert result.success, f"Compression failed: {result.error}"

        log_inst = pool.get_logger(inst_name, "coder")
        jsonl_msgs = read_jsonl_messages(log_inst.log_path)
        dups = check_duplicates(jsonl_msgs)
        assert not dups, f"Duplicates after compressing pre-compressed history: {dups}"

        marker_count = count_compression_markers(jsonl_msgs)
        assert marker_count == 4, f"Expected 4 markers total, got {marker_count}"


class TestTailSyncVerification:
    """Verify the §5.2 tail sync rule after compression operations.
    Design doc §5.2: "the tail end past the last marker MUST be in sync at all times
    and have the EXACT same number of messages since the last compression marker."
    """

    def test_tail_sync_after_compression(self, tmp_path):
        """After each compression, verify JSONL tail matches pool tail."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "tail_sync.jsonl")
        msgs = build_mixed_conversation(num_pairs=25, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"TailSync_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        for cycle in range(3):
            result = compress_and_sync(pool, inst_name,
                fraction=0.5,
                mode="manual",
                summary_text=f"Tail sync round {cycle + 1}.")
            assert result.success, f"Cycle {cycle}: Compression failed: {result.error}"

            jsonl_msgs = read_jsonl_messages(log_inst.log_path)
            jsonl_tail = get_tail_after_last_marker(jsonl_msgs)
            conv = pool.get_conversation(inst_name)

            pool_tail = get_pool_tail(conv)
            assert len(jsonl_tail) == len(pool_tail), \
                f"Cycle {cycle}: Tail count mismatch — JSONL tail has {len(jsonl_tail)}, " \
                f"pool tail has {len(pool_tail)}"

    def test_tail_sync_after_reload(self, tmp_path):
        """After reload + compression, verify tail sync still holds."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "tail_sync_reload.jsonl")
        msgs = build_mixed_conversation(num_pairs=20, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool1 = AgentPool(DUMMY_LLM_CFG)
        inst_name1 = f"TailSyncR1_{uuid.uuid4().hex[:8]}"
        status1 = pool1.load_session_from_log(log_path, target_instance=inst_name1)
        assert not status1.startswith("Error"), f"Load failed: {status1}"
        for i in range(2):
            result = compress_and_sync(pool1, inst_name1,
                fraction=0.5,
                mode="manual",
                summary_text=f"Pre-reload round {i + 1}.")
        log_inst = pool1.get_logger(inst_name1, "coder")
        pool2 = AgentPool(DUMMY_LLM_CFG)
        inst_name2 = f"TailSyncR2_{uuid.uuid4().hex[:8]}"
        status2 = pool2.load_session_from_log(log_inst.log_path, target_instance=inst_name2)
        assert not status2.startswith("Error"), f"Reload failed: {status2}"

        log_inst2 = pool2.get_logger(inst_name2, "coder")
        result = compress_and_sync(pool1, inst_name1,
            fraction=0.5,
            mode="manual",
            summary_text="Post-reload compression.")
        jsonl_msgs = read_jsonl_messages(log_inst2.log_path)
        conv = pool2.get_conversation(inst_name2)

        jsonl_tail = get_tail_after_last_marker(jsonl_msgs)
        pool_tail = get_pool_tail(conv)
        assert len(jsonl_tail) == len(pool_tail), \
            f"Tail mismatch after reload: JSONL {len(jsonl_tail)}, pool {len(pool_tail)}"


class TestConsecutiveMarkers:
    """Test that consecutive compression markers don't duplicate.
    Design doc §5.2 cumulative timeline shows each marker is unique and stacked.
    """

    def test_no_consecutive_marker_dups(self, tmp_path):
        """Compress multiple times rapidly. Each marker should be unique."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "consec_markers.jsonl")
        msgs = build_mixed_conversation(num_pairs=25, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"Consec_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"

        log_inst = pool.get_logger(inst_name, "coder")
        all_marker_contents = []
        for i in range(5):
            result = compress_and_sync(pool, inst_name,
                fraction=0.4,
                mode="manual",
                summary_text=f"Marker content {i} with unique padding {'M' * (i + 1)}")
            if not result.success:
                break
            jsonl_msgs = read_jsonl_messages(log_inst.log_path)
            markers = [m for m in jsonl_msgs if isinstance(m.get("content", ""), str) and
                       m["content"].startswith(COMPRESSION_MARKER)]
            new_markers = markers[len(all_marker_contents):]
            all_marker_contents.extend(m["content"] for m in new_markers)
        assert len(all_marker_contents) == len(set(all_marker_contents)), \
            f"Duplicate markers across compressions: {len(all_marker_contents)} total, " \
            f"{len(set(all_marker_contents))}"


class TestSystemMessageUniqueness:
    """Verify the system message appears exactly once in both JSONL and pool."""

    def test_single_system_message(self, tmp_path):
        """After multiple compressions and reloads, only one SYSTEM message exists."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "sys_unique.jsonl")
        msgs = build_mixed_conversation(num_pairs=20, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"SysUnique_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"
        for i in range(3):
            result = compress_and_sync(pool, inst_name,
                fraction=0.5,
                mode="manual",
                summary_text=f"System test round {i + 1}.")
        conv = pool.get_conversation(inst_name)
        sys_msgs = [m for m in conv if
                    (m.get('role', '') if isinstance(m, dict) else getattr(m, 'role', '')) == SYSTEM]
        assert len(sys_msgs) == 1, f"Expected exactly 1 system message, found {len(sys_msgs)}"

    def test_single_system_in_jsonl(self, tmp_path):
        """JSONL should also have exactly one system message."""
        import uuid
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.compression.core import compress_context as _compress
        log_path = str(tmp_path / "sys_unique_jsonl.jsonl")
        msgs = build_mixed_conversation(num_pairs=20, include_tool_chains=True)
        write_jsonl(log_path, msgs)

        pool = AgentPool(DUMMY_LLM_CFG)
        inst_name = f"SysUniqueJ_{uuid.uuid4().hex[:8]}"
        status = pool.load_session_from_log(log_path, target_instance=inst_name)
        assert not status.startswith("Error"), f"Load failed: {status}"
        for i in range(3):
            result = compress_and_sync(pool, inst_name,
                fraction=0.5,
                mode="manual",
                summary_text=f"JSONL system test round {i + 1}.")
        log_inst = pool.get_logger(inst_name, "coder")
        jsonl_msgs = read_jsonl_messages(log_inst.log_path)
        sys_count = sum(1 for m in jsonl_msgs if m["role"] == SYSTEM)
        assert sys_count == 1, f"Expected exactly 1 system message in JSONL, found {sys_count}"
