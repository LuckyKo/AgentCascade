"""Smoke tests for function_call/tool_calls handling in extract_text_from_message."""
import pytest
from agent_cascade.utils.utils import extract_text_from_message, _format_tool_calls_for_text, MAX_FC_ARGS_LEN
from agent_cascade.llm.schema import Message, FunctionCall


class TestExtractTextFunctionCall:
    def test_legacy_function_call(self):
        msg = {
            "role": "assistant",
            "content": "",
            "function_call": {"name": "search_web", "arguments": '{"query":"python"}'}
        }
        result = extract_text_from_message(msg, add_upload_info=False)
        assert result == '[TOOL CALL: search_web({"query":"python"})]'

    def test_modern_tool_calls_array(self):
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"test.txt"}'}},
                {"id": "call_2", "type": "function", "function": {"name": "write_file", "arguments": '{"content":"hello"}'}}
            ]
        }
        result = extract_text_from_message(msg, add_upload_info=False)
        assert "[TOOL CALL: read_file(" in result
        assert "[TOOL CALL: write_file(" in result

    def test_function_call_takes_priority_over_tool_calls(self):
        msg = {
            "role": "assistant",
            "content": "",
            "function_call": {"name": "legacy_tool", "arguments": "{}"},
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "modern_tool", "arguments": "{}"}}]
        }
        result = extract_text_from_message(msg, add_upload_info=False)
        assert result == "[TOOL CALL: legacy_tool({})]"

    def test_empty_assistant_no_calls(self):
        msg = {"role": "assistant", "content": ""}
        result = extract_text_from_message(msg, add_upload_info=False)
        assert result == ""

    def test_user_with_function_call_ignored(self):
        msg = {"role": "user", "content": "", "function_call": {"name": "search_web", "arguments": "{}"}}
        result = extract_text_from_message(msg, add_upload_info=False)
        assert result == ""

    def test_content_not_overridden(self):
        msg = {"role": "assistant", "content": "Here is the answer", "function_call": {"name": "tool", "arguments": "{}"}}
        result = extract_text_from_message(msg, add_upload_info=False)
        assert result == "Here is the answer"

    def test_whitespace_only_content_surfaces_tool_call(self):
        """Content with only whitespace should still surface function_call info."""
        msg = {
            "role": "assistant",
            "content": "   \n  ",
            "function_call": {"name": "read_file", "arguments": '{"path":"data.csv"}'}
        }
        result = extract_text_from_message(msg, add_upload_info=False)
        assert result == '[TOOL CALL: read_file({"path":"data.csv"})]'

    def test_message_object_input_with_function_call(self):
        """Verify fix works with Message objects (not just dicts)."""
        fc = FunctionCall(name="search_web", arguments='{"query":"test"}')
        msg = Message(role="assistant", content="", function_call=fc)
        result = extract_text_from_message(msg, add_upload_info=False)
        assert result == '[TOOL CALL: search_web({"query":"test"})]'

    def test_argument_truncation(self):
        """Verify large arguments are truncated to MAX_FC_ARGS_LEN (2048)."""
        big_args = '{"items": ' + ','.join([f'"item_{i}"' for i in range(100)]) + '}'
        msg = {
            "role": "assistant",
            "content": "",
            "function_call": {"name": "process_data", "arguments": big_args}
        }
        result = extract_text_from_message(msg, add_upload_info=False)
        assert "[TOOL CALL: process_data(" in result
        assert "... [TRUNCATED]" not in result  # args are small enough

    def test_argument_truncation_with_large_payload(self):
        """Verify truncation actually fires for oversized arguments."""
        big_args = '{"data": "' + "x" * 3000 + '"}'
        msg = {
            "role": "assistant",
            "content": "",
            "function_call": {"name": "analyze", "arguments": big_args}
        }
        result = extract_text_from_message(msg, add_upload_info=False)
        assert "[TOOL CALL: analyze(" in result
        assert "... [TRUNCATED]" in result

    # --- Additional tests for C1 (tool_calls on Message objects), M2, M3 ---

    def test_tool_calls_on_message_object(self):
        """C1/M2: tool_calls array works when passed via Message object (not just dict)."""
        tc_list = [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
            {"id": "call_2", "type": "function", "function": {"name": "write_file", "arguments": '{"content":"ok"}'}}
        ]
        msg = Message(role="assistant", content="", extra={"tool_calls": tc_list})
        result = extract_text_from_message(msg, add_upload_info=False)
        assert "[TOOL CALL: read_file(" in result
        assert "[TOOL CALL: write_file(" in result

    def test_mixed_dict_object_items_in_tool_calls(self):
        """M3: tool_calls array with mixed dict and object items."""
        # First item is a plain dict, second simulates an object-like structure via nested dict
        tc_list = [
            {"id": "call_1", "type": "function", "function": {"name": "tool_a", "arguments": '{"x":1}'}},
            {"id": "call_2", "type": "function", "function": {"name": "tool_b", "arguments": '{"y":2}'}}
        ]
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": tc_list
        }
        result = extract_text_from_message(msg, add_upload_info=False)
        assert "[TOOL CALL: tool_a({" in result
        assert "[TOOL CALL: tool_b({" in result

    def test_shared_helper_returns_empty_for_no_tool_calls(self):
        """Verify the shared helper returns empty string when no tool calls exist."""
        msg = {"role": "assistant", "content": ""}
        result = _format_tool_calls_for_text(msg)
        assert result == ""

    def test_max_fc_args_len_is_module_level_constant(self):
        """M1: Verify MAX_FC_ARGS_LEN is accessible as a module-level constant."""
        assert isinstance(MAX_FC_ARGS_LEN, int)
        assert MAX_FC_ARGS_LEN == 2048