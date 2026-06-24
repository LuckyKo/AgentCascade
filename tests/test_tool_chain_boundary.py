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


def _make_msg(role, content="text", function_call=None, extra=None):
    """Create a test message with optional function_call and extra dict.

    When function_call is provided but no extra dict is given, an extra dict
    is auto-created containing a synthetic function_id derived from the call name.
    This ensures proper ID-based matching between ASSISTANT→FUNCTION pairs.
    """
    if role == ASSISTANT and function_call:
        # function_call must be a dict with 'name' key for Message validation
        fc = {'name': function_call, 'arguments': '{}'}
        if extra is None:
            # Auto-generate function_id so FUNCTION results can match via ID
            extra = {'function_id': f"call_{function_call}"}
        return Message(role=role, content=content, function_call=fc, extra=extra)
    return Message(role=role, content=content, extra=extra or None)


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
        """If the boundary falls on a FUNCTION result, walk forward to include its pair."""
        msgs = [
            _make_msg(USER, "prompt"),
            _make_msg(ASSISTANT, "thinking", function_call="shell_cmd"),  # tool call at index 1, extra={'function_id': 'call_shell_cmd'}
            _make_msg(FUNCTION, "tool output", extra={'function_id': 'call_shell_cmd'}),  # matches ASSISTANT above
            _make_msg(ASSISTANT, "response"),
        ]
        # fraction=0.5 -> int(4*0.5)=2, not force -> min(2, 4-2)=2
        # msgs[2] is FUNCTION with matching function_id -> post-validation finds split
        # (A at pos 1 discarded, F at pos 2 kept) -> advances discard to 3 which exceeds keep zone
        # returns -1 (no valid compression without splitting the pair)
        discard = compute_discard_count(msgs, 0.5, False)
        assert discard == -1

    def test_adjustment_with_function_at_boundary(self):
        """Boundary falls on FUNCTION result — should include paired tool call."""
        msgs = [
            _make_msg(USER, "hello"),
            _make_msg(ASSISTANT, "thinking", function_call="read_file"),  # tool call at index 1
            _make_msg(FUNCTION, "file content"),  # FUNCTION at index 2 — this is the boundary
            _make_msg(ASSISTANT, "analysis"),
        ]
        # fraction=0.5 -> int(4*0.5)=2, not force -> min(2, 4-2)=2
        discard = compute_discard_count(msgs, 0.5, False)
        assert discard == 2

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
        discard = compute_discard_count(msgs, 0.5, False)
        assert discard == 2

    def test_no_adjustment_at_end_of_active_set(self):
        """If discard equals len(active_set), there's no message at the boundary."""
        msgs = [
            _make_msg(ASSISTANT, "thinking", function_call="read_file"),
            _make_msg(FUNCTION, "result"),
        ]
        # fraction=1.0 -> int(2*1.0)=2, force -> max(1, 2)=2
        # refinement advances past F to pos 2 (end), returns min(2, 1+1)=2 but clamp=1
        discard = compute_discard_count(msgs, 1.0, True)
        assert discard == 1

    def test_dict_messages_work(self):
        """Tool chain detection works with dict messages too."""
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "thinking", "function_call": {"name": "read_file"}},  # tool call at index 1
            {"role": "function", "content": "file content"},  # boundary at index 2
            {"role": "user", "content": "next"},
        ]
        discard = compute_discard_count(msgs, 0.5, False)
        assert discard == 2

    def test_force_mode_adjustment(self):
        """Force mode also respects tool chain boundaries."""
        msgs = [
            _make_msg(ASSISTANT, "thinking", function_call="shell_cmd"),
            _make_msg(FUNCTION, "output", extra={'function_id': 'call_shell_cmd'}),  # boundary, matches ASSISTANT above
            _make_msg(ASSISTANT, "done"),
        ]
        discard = compute_discard_count(msgs, 0.3, True)
        assert discard == 2

    def test_adjustment_respects_tail_guard(self):
        """When tail guard prevents extension, reduce discard to exclude FUNCTION result."""
        msgs = [
            _make_msg(ASSISTANT, "thinking", function_call="read_file"),
            _make_msg(FUNCTION, "result"),  # boundary would be here
            _make_msg(ASSISTANT, "done"),  # only 1 message left if we include this
        ]
        discard = compute_discard_count(msgs, 0.6, False)
        assert discard == 1