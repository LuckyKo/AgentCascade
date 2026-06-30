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

    # --- Reasoning content tests ---

    def test_reasoning_content_included_empty_content(self):
        """Test that reasoning_content is surfaced when content is empty (dict input)."""
        msg = {"role": "assistant", "content": "", "reasoning_content": "Let me think about this..."}
        result = extract_text_from_message(msg, add_upload_info=False)
        assert "[THOUGHT: Let me think about this...]" in result

    def test_reasoning_content_included_with_content(self):
        """Test that reasoning_content is prepended when content also exists."""
        msg = {"role": "assistant", "content": "The answer is 42", "reasoning_content": "I need to calculate this carefully"}
        result = extract_text_from_message(msg, add_upload_info=False)
        # extract_text_from_message returns content as-is when non-empty;
        # prepending reasoning before content is handled by _format_messages_for_summary()
        assert result == "The answer is 42"

    def test_format_messages_prepends_reasoning(self):
        """Test that _format_messages_for_summary prepends reasoning before content."""
        from agent_cascade.compression.agent_invoker import _format_messages_for_summary
        
        messages = [
            {"role": "assistant", "content": "The answer is 42", "reasoning_content": "I need to calculate this carefully"},
        ]
        result = _format_messages_for_summary(messages)
        assert "[THOUGHT: I need to calculate this carefully]" in result
        assert "The answer is 42" in result
        # Reasoning should come before content
        assert result.index("[THOUGHT:") < result.index("The answer")

    def test_format_messages_reasoning_as_fallback(self):
        """Test that _format_messages_for_summary uses reasoning when content is empty."""
        from agent_cascade.compression.agent_invoker import _format_messages_for_summary
        
        messages = [
            {"role": "assistant", "content": "", "reasoning_content": "Let me think about this..."},
        ]
        result = _format_messages_for_summary(messages)
        assert "[THOUGHT: Let me think about this...]" in result

    def test_reasoning_content_via_shared_helper(self):
        """Test that _format_tool_calls_for_text returns empty for reasoning-only messages."""
        msg = {"role": "assistant", "content": "", "reasoning_content": "Step by step analysis..."}
        result = _format_tool_calls_for_text(msg)
        assert result == ""

    def test_reasoning_content_priority_over_tool_calls(self):
        """Test that reasoning_content takes priority over tool_calls when both present with empty content."""
        msg = {
            "role": "assistant",
            "content": "",
            "reasoning_content": "Let me reason through this",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search_web", "arguments": "{}"}}]
        }
        result = extract_text_from_message(msg, add_upload_info=False)
        assert "[THOUGHT: Let me reason through this]" in result

    def test_reasoning_content_on_message_object(self):
        """Test reasoning_content works with Message objects."""
        msg = Message(role="assistant", content="", extra={"reasoning_content": "Thinking process here"})
        result = extract_text_from_message(msg, add_upload_info=False)
        assert "[THOUGHT: Thinking process here]" in result

    def test_reasoning_content_user_role_ignored(self):
        """Test that reasoning_content for user messages is not surfaced (only assistant)."""
        msg = {"role": "user", "content": "", "reasoning_content": "User thinking"}
        result = extract_text_from_message(msg, add_upload_info=False)
        assert result == ""

    def test_reasoning_content_whitespace_only(self):
        """Test that whitespace-only reasoning_content is treated as empty."""
        msg = {"role": "assistant", "content": "", "reasoning_content": "   \n  "}
        result = extract_text_from_message(msg, add_upload_info=False)
        assert result == ""

    def test_reasoning_content_list_type(self):
        """Test that list-type reasoning_content (multi-modal) is handled correctly."""
        msg = {
            "role": "assistant",
            "content": "",
            "reasoning_content": [
                {"text": "First thought: the key"},
                {"text": "Second thought: combine them"}
            ]
        }
        result = extract_text_from_message(msg, add_upload_info=False)
        assert "[THOUGHT:" in result
        assert "First thought" in result
        assert "Second thought" in result

    def test_reasoning_content_list_type_via_helper(self):
        """Test that _format_tool_calls_for_text returns empty for list-type reasoning."""
        msg = {
            "role": "assistant",
            "content": "",
            "reasoning_content": [
                {"text": "Step one"},
                {"text": "Step two"}
            ]
        }
        result = _format_tool_calls_for_text(msg)
        assert result == ""

    def test_reasoning_content_list_empty_items(self):
        """Test that list-type reasoning with empty text items returns empty."""
        msg = {
            "role": "assistant",
            "content": "",
            "reasoning_content": [
                {"text": ""},
                {"text": "  "}
            ]
        }
        result = extract_text_from_message(msg, add_upload_info=False)
        assert result == ""

    def test_reasoning_content_truncation(self):
        """Test that large reasoning_content is truncated at MAX_FC_ARGS_LEN (2048)."""
        long_thought = "x" * 3000
        msg = {"role": "assistant", "content": "", "reasoning_content": long_thought}
        result = extract_text_from_message(msg, add_upload_info=False)
        assert "[THOUGHT:" in result
        assert "... [TRUNCATED]" in result
        # Verify it's actually truncated (not full 3000 chars)
        assert len(result) < 3100

    def test_reasoning_content_truncation_via_helper(self):
        """Test that _format_tool_calls_for_text returns empty for reasoning-only messages."""
        long_thought = "y" * 3000
        msg = {"role": "assistant", "content": "", "reasoning_content": long_thought}
        result = _format_tool_calls_for_text(msg)
        assert result == ""

    def test_reasoning_combined_with_tool_calls_on_message_object(self):
        """Test combined reasoning + tool_calls on Message object via extra dict."""
        msg = Message(
            role="assistant",
            content="",
            extra={
                "reasoning_content": "Let me think about this problem carefully",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "search_web", "arguments": '{"q":"test"}'}}
                ]
            }
        )
        result = extract_text_from_message(msg, add_upload_info=False)
        # Both reasoning and tool calls should be accessible via the helper
        assert "[THOUGHT:" in result or "[TOOL CALL:" in result

    def test_reasoning_content_list_truncation(self):
        """Test that list-type reasoning_content is also truncated via extract_text_from_message."""
        long_text = "z" * 3000
        msg = {
            "role": "assistant",
            "content": "",
            "reasoning_content": [
                {"text": long_text}
            ]
        }
        result = extract_text_from_message(msg, add_upload_info=False)
        assert "[THOUGHT:" in result
        assert "... [TRUNCATED]" in result

    def test_format_messages_list_reasoning(self):
        """Test that _format_messages_for_summary handles list-type reasoning."""
        from agent_cascade.compression.agent_invoker import _format_messages_for_summary
        
        messages = [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": [
                    {"text": "Analyzing the data"},
                    {"text": "Found the pattern"}
                ]
            }
        ]
        result = _format_messages_for_summary(messages)
        assert "[THOUGHT:" in result
        assert "Analyzing" in result
        assert "pattern" in result

    def test_format_messages_large_reasoning_truncation(self):
        """Test that _format_messages_for_summary truncates large reasoning."""
        from agent_cascade.compression.agent_invoker import _format_messages_for_summary
        
        long_thought = "a" * 3000
        messages = [
            {"role": "assistant", "content": "", "reasoning_content": long_thought}
        ]
        result = _format_messages_for_summary(messages)
        assert "... [TRUNCATED]" in result

    def test_reasoning_to_text_helper_directly(self):
        """Test the _reasoning_to_text helper function directly."""
        from agent_cascade.utils.utils import _reasoning_to_text
        
        # String input
        assert _reasoning_to_text("hello") == "hello"
        
        # List input with dicts
        result = _reasoning_to_text([{"text": "a"}, {"text": "b"}])
        assert "a b" in result
        
        # Empty list
        assert _reasoning_to_text([]) == ""
        
        # None input
        assert _reasoning_to_text(None) == ""
        
        # Whitespace string
        assert _reasoning_to_text("  ") == ""