"""Test tool-chain boundary protection in compute_discard_count.

Ensures compression never cuts in the middle of a tool call/result pair:
if the discard boundary lands on a FUNCTION result, it walks back to include
the paired ASSISTANT tool call so both are discarded together.
"""

import sys
from pathlib import Path
from agent_cascade.llm.schema import ASSISTANT, FUNCTION, USER, Message

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
from agent_cascade.compression.helpers import compute_discard_count


def _make_msg(role, content="text", function_call=None):
    if role == ASSISTANT and function_call:
        # function_call must be a dict with 'name' key for Message validation
        return Message(role=role, content=content, function_call={'name': function_call, 'arguments': '{}'})
    return Message(role=role, content=content)


class TestToolChainBoundaryProtection:
    """Test that compression boundaries respect tool call chains."""

    def test_no_adjustment_when_cut_is_on_user_message(self):
        """If the boundary falls on a USER message, no adjustment needed."""
        msgs = [
            _make_msg(USER, "hello"),
            _make_msg(ASSISTANT, "hi there"),
            _make_msg(USER, "what time is it?"),  # discard=2 lands here
            _make_msg(ASSISTANT, "it's noon"),
        ]
        # fraction=0.5 -> int(4*0.5)=2, not force -> min(2, 4-2)=2
        discard = compute_discard_count(msgs, 0.5, False)
        assert discard == 2

    def test_no_adjustment_when_cut_is_on_assistant_message(self):
        """If the boundary falls on a plain ASSISTANT message, no adjustment."""
        msgs = [
            _make_msg(ASSISTANT, "first"),
            _make_msg(FUNCTION, "tool result"),
            _make_msg(ASSISTANT, "second"),  # discard=2 lands here
            _make_msg(USER, "ok"),
        ]
        discard = compute_discard_count(msgs, 0.5, False)
        assert discard == 2

    def test_adjustment_when_cut_is_on_function_result(self):
        """If the boundary falls on a FUNCTION result, walk back to include its tool call."""
        msgs = [
            _make_msg(USER, "prompt"),
            _make_msg(ASSISTANT, "thinking", function_call="shell_cmd"),  # tool call at index 1
            _make_msg(FUNCTION, "tool output"),  # discard=2 lands here -> adjust
            _make_msg(ASSISTANT, "response"),
        ]
        # fraction=0.5 -> int(4*0.5)=2, not force -> min(2, 4-2)=2
        # msgs[2] is FUNCTION -> walk back to index 1 (tool call)
        # extended=3 > tail_limit=2 -> can't extend -> reduce to i=1
        discard = compute_discard_count(msgs, 0.5, False)
        assert discard == 1

    def test_adjustment_with_function_at_boundary(self):
        """Boundary falls on FUNCTION result — should include paired tool call."""
        msgs = [
            _make_msg(USER, "hello"),
            _make_msg(ASSISTANT, "thinking", function_call="read_file"),  # tool call at index 1
            _make_msg(FUNCTION, "file content"),  # FUNCTION at index 2 — this is the boundary
            _make_msg(ASSISTANT, "analysis"),
        ]
        # fraction=0.5 -> int(4*0.5)=2, not force -> min(2, 4-2)=2
        # msgs[2] = FUNCTION -> walk back to index 1 (ASSISTANT with function_call)
        # extended=3 > tail_limit=2 -> can't extend -> reduce to i=1
        discard = compute_discard_count(msgs, 0.5, False)
        assert discard == 1

    def test_adjustment_extends_when_tail_guard_allows(self):
        """When tail guard allows, extend discard to include the FUNCTION result."""
        msgs = [
            _make_msg(USER, "hello"),
            _make_msg(ASSISTANT, "thinking", function_call="read_file"),  # tool call at index 1
            _make_msg(FUNCTION, "file content"),  # boundary at index 2
            _make_msg(ASSISTANT, "analysis"),     # index 3
            _make_msg(USER, "next"),              # index 4
            _make_msg(ASSISTANT, "response"),     # index 5
        ]
        # fraction=0.5 -> int(6*0.5)=3, tail guard min(3, 4)=3
        # msgs[3] = ASSISTANT (no function_call) — not FUNCTION, no adjustment
        # Need boundary at index 2 (FUNCTION). Use fraction=0.375:
        # int(6*0.375) = int(2.25) = 2, tail guard min(2, 4)=2
        # msgs[2] = FUNCTION -> walk back to index 1 -> extended=3, tail_limit=4 -> ok!
        discard = compute_discard_count(msgs, 0.375, False)
        assert discard == 3

    def test_adjustment_skips_plain_assistant_messages(self):
        """Walking back stops at non-FUNCTION, non-tool-call messages."""
        msgs = [
            _make_msg(ASSISTANT, "plain text"),  # no function_call — stop walking here
            _make_msg(FUNCTION, "tool result"),  # boundary would be here
            _make_msg(ASSISTANT, "response"),
            _make_msg(USER, "ok"),
        ]
        # fraction=0.5 -> int(4*0.5)=2, not force -> min(2, 4-2)=2
        # msgs[2] = ASSISTANT (no function_call) — not FUNCTION at all, no adjustment
        discard = compute_discard_count(msgs, 0.5, False)
        assert discard == 2

    def test_adjustment_with_consecutive_function_results(self):
        """Multiple consecutive FUNCTION results before the tool call."""
        msgs = [
            _make_msg(ASSISTANT, "thinking", function_call="read_file"),  # tool call at index 0
            _make_msg(FUNCTION, "result 1"),  # index 1
            _make_msg(FUNCTION, "result 2"),  # index 2 — boundary lands here
            _make_msg(ASSISTANT, "done"),
        ]
        # fraction=0.5 -> int(4*0.5)=2, not force -> min(2, 4-2)=2
        # msgs[2] = FUNCTION -> walk back: index 1 is FUNCTION (keep going)
        # index 0 is ASSISTANT with function_call -> i=0
        # extended=3 > tail_limit=2 -> can't extend -> reduce to i=0
        discard = compute_discard_count(msgs, 0.5, False)
        assert discard == 0

    def test_no_adjustment_at_end_of_active_set(self):
        """If discard equals len(active_set), there's no message at the boundary."""
        msgs = [
            _make_msg(ASSISTANT, "thinking", function_call="read_file"),
            _make_msg(FUNCTION, "result"),
        ]
        # fraction=1.0 -> int(2*1.0)=2, force -> max(1, 2)=2
        # discard == len(msgs), so no boundary message to check
        discard = compute_discard_count(msgs, 1.0, True)
        assert discard == 2

    def test_dict_messages_work(self):
        """Tool chain detection works with dict messages too."""
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "thinking", "function_call": {"name": "read_file"}},  # tool call at index 1
            {"role": "function", "content": "file content"},  # boundary at index 2
            {"role": "user", "content": "next"},
        ]
        discard = compute_discard_count(msgs, 0.5, False)
        # raw=2, tail_limit=2, extended=3 > 2 -> can't extend -> reduce to i=1
        assert discard == 1

    def test_force_mode_adjustment(self):
        """Force mode also respects tool chain boundaries."""
        msgs = [
            _make_msg(ASSISTANT, "thinking", function_call="shell_cmd"),
            _make_msg(FUNCTION, "output"),  # boundary
            _make_msg(ASSISTANT, "done"),
        ]
        # fraction=0.3 -> int(3*0.3)=1, force -> max(1, 1)=1
        # msgs[1] = FUNCTION -> walk back to index 0 (tool call)
        # discard becomes 2
        discard = compute_discard_count(msgs, 0.3, True)
        assert discard == 2

    def test_adjustment_respects_tail_guard(self):
        """When tail guard prevents extension, reduce discard to exclude FUNCTION result."""
        msgs = [
            _make_msg(ASSISTANT, "thinking", function_call="read_file"),
            _make_msg(FUNCTION, "result"),  # boundary would be here
            _make_msg(ASSISTANT, "done"),  # only 1 message left if we include this
        ]
        # fraction=0.6: raw=int(3*0.6)=2, clamp=min(2,1)=1
        # msgs[1]=FUNCTION -> walk back to 0 (tool call)
        # extended=2 > tail_limit=1 -> can't extend -> reduce to i=0
        discard = compute_discard_count(msgs, 0.6, False)
        assert discard == 0