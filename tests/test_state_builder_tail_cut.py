"""Unit tests for additive/delta streaming tail cut (phase 1).

Pins the backend delta logic in ``state_builder``:

* ``_safe_tail_start_index`` — R6 tool-pair integrity rule (the trickiest part;
  written FIRST so it pins the rule before the rest is trusted).
* The tail cut in ``_serialize_instance`` (flag ON -> bounded tail, absolute indices).
* Flag OFF / ``streaming=False`` regression guards (byte-identical full send).
* ``history_count`` invariant and dedup invariant.
* ``prefix_shrank`` detection in ``_serialize_instances_incremental`` (compression/rollback
  shrink -> forced full frame).

Self-contained: no network, no live API, no real AgentPool — lightweight fakes with an
RLock, a conversation list, and ``_streaming_responses``, mirroring the conventions in
test_state_builder.py. The feature flag is toggled by patching the module-level constant
(the env var is read at import time).
"""

import threading
from unittest.mock import MagicMock, patch

from agent_cascade.api_integration_pkg import state_builder as sb
from agent_cascade.agent_instance import AgentState


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _msg(role, content="", **kw):
    """Build a plain-dict message (serialize_message handles dicts natively)."""
    m = {"role": role, "content": content}
    m.update(kw)
    return m


def _make_inst(conversation, streaming_responses=None, name="Maine"):
    """Lightweight AgentInstance stub with the attrs ``_serialize_instance`` touches."""
    inst = MagicMock()
    inst.instance_name = name
    inst.agent_class = "coder"
    inst.parent_instance = None
    # Must be a real AgentState enum member: _serialize_instance reads `inst.state.name`.
    inst.state = AgentState.RUNNING
    inst.conversation = list(conversation)
    inst._streaming_responses = list(streaming_responses or [])
    inst._state_lock = threading.RLock()
    inst._compression_lock = threading.RLock()
    return inst


def _make_pool(inst):
    pool = MagicMock()
    pool.is_instance_halted.return_value = False
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)
    return pool


def _serialize(inst, pool, streaming=True, responses=None):
    """Drive ``_serialize_instance`` with a controlled flag value."""
    saved = sb.STREAM_DELTA_ENABLED
    try:
        sb.STREAM_DELTA_ENABLED = True  # default ON for these tests; individual tests flip it
        return sb._serialize_instance(
            inst, pool, include_messages=True, streaming=streaming,
            streaming_responses=responses if responses is not None else inst._streaming_responses,
        )
    finally:
        sb.STREAM_DELTA_ENABLED = saved


# ---------------------------------------------------------------------------
# 1. _safe_tail_start_index — tool-pair integrity table (pins the rule)
# ---------------------------------------------------------------------------

def test_safe_tail_plain_end():
    # [..., user, assistant] plain end -> cut to last TAIL_COMMITTED(1) msg
    msgs = [_msg("user", "q"), _msg("assistant", "a")]
    assert sb._safe_tail_start_index(msgs) == 1


def test_safe_tail_pair_at_end():
    # [..., user, assistant(tool_calls), tool] -> widen to whole last chain
    msgs = [
        _msg("user", "q"),
        _msg("assistant", "", tool_calls=[{"id": "1", "function": {}}]),
        _msg("tool", "result"),
    ]
    assert sb._safe_tail_start_index(msgs) == 1


def test_safe_tail_chain_of_four():
    # [..., user, asst(tc), tool, asst(tc), tool] (chain of 4 after a user) -> whole chain
    msgs = [
        _msg("user", "q"),
        _msg("assistant", "", tool_calls=[{"id": "1"}]),
        _msg("tool", "r1"),
        _msg("assistant", "", tool_calls=[{"id": "2"}]),
        _msg("tool", "r2"),
    ]
    assert sb._safe_tail_start_index(msgs) == 1


def test_safe_tail_plain_then_chain():
    # [..., asst(plain), user, asst(tc), tool] (len 4): backward scan stops at the plain
    # assistant (index 0) -> boundary=1; desired cut c=len-1=3 > boundary -> widen to whole
    # last chain. NOTE: the plan's §1.2 table row for this shape is internally inconsistent
    # (it labels len=5 / start_idx=3 but the shape shown has len 4); the code's R6-safe rule
    # yields start_idx=2 here, and no call/response pair is split either way.
    msgs = [
        _msg("assistant", "a0"),
        _msg("user", "q"),
        _msg("assistant", "", tool_calls=[{"id": "1"}]),
        _msg("tool", "r1"),
    ]
    assert sb._safe_tail_start_index(msgs) == 2


def test_safe_tail_unbroken_chain_full_send():
    # One unbroken 20-msg chain from index 0 -> full send (start_idx 0)
    msgs = []
    for i in range(10):
        msgs.append(_msg("assistant", "", tool_calls=[{"id": str(i)}]))
        msgs.append(_msg("tool", f"r{i}"))
    assert len(msgs) == 20
    assert sb._safe_tail_start_index(msgs) == 0


def test_safe_tail_single_message():
    assert sb._safe_tail_start_index([_msg("user", "q")]) == 0


def test_safe_tail_legacy_function_call():
    # legacy function_call + function response -> widen to whole chain
    msgs = [
        _msg("user", "q"),
        _msg("assistant", "", function_call={"name": "f", "arguments": "{}"}),
        _msg("function", "result"),
    ]
    assert sb._safe_tail_start_index(msgs) == 1


def test_safe_tail_empty_and_misconfig():
    assert sb._safe_tail_start_index([]) == 0
    # TAIL_COMMITTED <= 0 -> always full send (misconfig guard)
    with patch.object(sb, "TAIL_COMMITTED", 0):
        assert sb._safe_tail_start_index([_msg("user", "q"), _msg("assistant", "a")]) == 0


# ---------------------------------------------------------------------------
# 2. Tail cut on: flag ON -> bounded tail + absolute indices + history_count
# ---------------------------------------------------------------------------

def test_tail_cut_flag_on():
    committed = [_msg("user" if i % 2 == 0 else "assistant", f"m{i}") for i in range(50)]
    partial = _msg("assistant", "streaming...")
    inst = _make_inst(committed, streaming_responses=[partial])
    pool = _make_pool(inst)

    result = _serialize(inst, pool, streaming=True)

    # last committed + 1 streaming partial
    assert len(result["messages"]) == 2, f"expected tail of 2, got {len(result['messages'])}"
    # TOTAL history count (committed + streaming), unaffected by the cut
    assert result["history_count"] == 51
    # first sent message carries ABSOLUTE index 49 (the last committed message)
    assert result["messages"][0]["index"] == 49
    assert result["messages"][1]["index"] == 50
    assert result["is_partial"] is True


# ---------------------------------------------------------------------------
# 3. Flag OFF -> full send (regression guard, byte-identical to legacy)
# ---------------------------------------------------------------------------

def test_flag_off_full_send():
    committed = [_msg("user" if i % 2 == 0 else "assistant", f"m{i}") for i in range(50)]
    partial = _msg("assistant", "streaming...")
    inst = _make_inst(committed, streaming_responses=[partial])
    pool = _make_pool(inst)

    saved = sb.STREAM_DELTA_ENABLED
    try:
        sb.STREAM_DELTA_ENABLED = False
        result = sb._serialize_instance(
            inst, pool, include_messages=True, streaming=True,
            streaming_responses=inst._streaming_responses,
        )
    finally:
        sb.STREAM_DELTA_ENABLED = saved

    # full conversation + 1 partial, indices start at 0 (legacy behavior)
    assert len(result["messages"]) == 51
    assert result["history_count"] == 51
    assert result["messages"][0]["index"] == 0


# ---------------------------------------------------------------------------
# 4. streaming=False (force_full / connect-time) -> full send regardless of flag
# ---------------------------------------------------------------------------

def test_streaming_false_full_send_flag_on():
    committed = [_msg("user" if i % 2 == 0 else "assistant", f"m{i}") for i in range(50)]
    partial = _msg("assistant", "streaming...")
    inst = _make_inst(committed, streaming_responses=[partial])
    pool = _make_pool(inst)

    result = _serialize(inst, pool, streaming=False)  # flag ON by default here

    # no tail cut even with a live partial in flight
    assert len(result["messages"]) == 51
    assert result["history_count"] == 51
    assert result["messages"][0]["index"] == 0
    # is_partial still reflects the live _streaming_responses (independent of `streaming`)
    assert result["is_partial"] is True


# ---------------------------------------------------------------------------
# 5. history_count invariant + absolute indices across all shapes
# ---------------------------------------------------------------------------

def test_history_count_invariant_and_absolute_indices():
    shapes = [
        [_msg("user", "q"), _msg("assistant", "a")],
        [_msg("user", "q"), _msg("assistant", "", tool_calls=[{"id": "1"}]), _msg("tool", "r")],
        [_msg("assistant", "a0"), _msg("user", "q"), _msg("assistant", "", tool_calls=[{"id": "1"}]), _msg("tool", "r")],
    ]
    for committed in shapes:
        partial = _msg("assistant", "streaming...")
        inst = _make_inst(committed, streaming_responses=[partial])
        pool = _make_pool(inst)
        result = _serialize(inst, pool, streaming=True)

        # invariant: history_count == len(committed) + num_streaming (regardless of tail size)
        assert result["history_count"] == len(committed) + 1
        # absolute index check: every sent message's index == its position in the FULL
        # conversation. start_idx = history_count - messages.length; a message at tail
        # position `pos` sits at full position start_idx + pos (the streaming partial is NOT
        # committed, so it does not shift the committed messages' absolute indices).
        start_idx = result["history_count"] - len(result["messages"])
        for pos, m in enumerate(result["messages"]):
            assert m["index"] == start_idx + pos, f"index {m['index']} != full position {start_idx + pos}"


# ---------------------------------------------------------------------------
# 6. Dedup invariant: a partial identical to the last committed msg is not double-appended
#    (relies on the last committed message being inside the tail — documented in §1.4)
# ---------------------------------------------------------------------------

def test_dedup_partial_matches_last_committed():
    # last committed assistant message has content "DONE"; the in-flight partial equals it
    committed = [_msg("user", "q"), _msg("assistant", "DONE")]
    partial = _msg("assistant", "DONE")  # same fingerprint as committed[1]
    inst = _make_inst(committed, streaming_responses=[partial])
    pool = _make_pool(inst)

    result = _serialize(inst, pool, streaming=True)

    # tail = [committed assistant "DONE"] ; the identical partial is deduped (num_streaming=0)
    assert len(result["messages"]) == 1
    assert result["history_count"] == 2  # committed only; deduped partial not counted


# ---------------------------------------------------------------------------
# 7. prefix_shrank detection in _serialize_instances_incremental
# ---------------------------------------------------------------------------

def test_prefix_shrink_forces_full_frame():
    """When the conversation shrinks (compression/rollback), the frame is forced full."""
    # Two committed messages; a streaming partial in flight.
    inst = _make_inst([_msg("user", "q"), _msg("assistant", "a")],
                      streaming_responses=[_msg("assistant", "partial")])
    pool = MagicMock()
    pool.instances = {"Maine": inst}
    pool.is_instance_halted.return_value = False
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)

    saved_versions = dict(sb._cache_mgr.stream_versions)
    saved_flag = sb.STREAM_DELTA_ENABLED
    try:
        with sb._cache_mgr._lock:
            sb._cache_mgr.stream_versions.clear()
            sb.STREAM_DELTA_ENABLED = True

            # Frame 1: active instance, streaming -> delta tail. committed=[user,assistant],
            # TAIL_COMMITTED=1 => start_idx=1 => [committed assistant(idx 1), partial(idx 2)].
            r1 = sb._serialize_instances_incremental(pool, "Maine", force_full=False)
            assert len(r1["Maine"]["messages"]) == 2
            assert r1["Maine"]["messages"][0]["index"] == 1  # tail (not full)

            # Simulate compression/rollback: shrink the conversation to a single message.
            with inst._compression_lock:
                inst.conversation = [_msg("user", "q")]

            # Frame 2: current_version[0] (1) < prev_version[0] (2) -> prefix_shrank -> FULL frame
            # (no tail cut): [committed user(idx 0), partial(idx 1)]. Distinguished from the
            # delta tail by the first message index being 0.
            r2 = sb._serialize_instances_incremental(pool, "Maine", force_full=False)
            assert len(r2["Maine"]["messages"]) == 2
            assert r2["Maine"]["messages"][0]["index"] == 0  # full send (prefix shrank)
    finally:
        with sb._cache_mgr._lock:
            sb.STREAM_DELTA_ENABLED = saved_flag
            sb._cache_mgr.stream_versions.clear()
            sb._cache_mgr.stream_versions.update(saved_versions)


def test_prefix_shrink_noop_when_growing():
    """A growing conversation does NOT trigger prefix_shrank (delta tail still applies)."""
    inst = _make_inst([_msg("user", "q")], streaming_responses=[_msg("assistant", "partial")])
    pool = MagicMock()
    pool.instances = {"Maine": inst}
    pool.is_instance_halted.return_value = False
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)

    saved_versions = dict(sb._cache_mgr.stream_versions)
    try:
        with sb._cache_mgr._lock:
            sb._cache_mgr.stream_versions.clear()
            sb.STREAM_DELTA_ENABLED = True

            r1 = sb._serialize_instances_incremental(pool, "Maine", force_full=False)

            # Grow the conversation (new committed message) — not a shrink.
            with inst._compression_lock:
                inst.conversation.append(_msg("assistant", "a"))

            r2 = sb._serialize_instances_incremental(pool, "Maine", force_full=False)
            # Delta tail still applies (no shrink): last committed + partial.
            assert len(r2["Maine"]["messages"]) == 2
    finally:
        with sb._cache_mgr._lock:
            sb._cache_mgr.stream_versions.clear()
            sb._cache_mgr.stream_versions.update(saved_versions)


def test_streaming_true_no_responses_sends_full():
    """CRITICAL: streaming=True but empty stream_responses => is_partial=False.

    The tail cut must NOT apply — a non-partial frame with only a tail would cause
    the frontend to replace the entire message list with just the tail (UI corruption).
    With no active stream, we must send the full history.
    """
    msgs = [_msg("user", "q"), _msg("assistant", "a1"), _msg("user", "q2")]
    inst = _make_inst(msgs, streaming_responses=[])  # NO streaming responses
    pool = MagicMock()
    pool.instances = {"Maine": inst}
    pool.is_instance_halted.return_value = False
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)

    saved_flag = sb.STREAM_DELTA_ENABLED
    try:
        with patch.object(sb, "STREAM_DELTA_ENABLED", True):
            result = sb._serialize_instance(
                inst, pool, include_messages=True,
                streaming=True,  # not force_full
                streaming_responses=[],  # but no active stream!
            )
            # Must send ALL messages (full), not just the tail.
            assert len(result["messages"]) == 3, \
                f"Expected full send (3 msgs) when no streaming responses, got {len(result['messages'])}"
            assert result["is_partial"] is False
    finally:
        sb.STREAM_DELTA_ENABLED = saved_flag
