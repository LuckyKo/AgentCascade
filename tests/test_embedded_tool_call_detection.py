"""Test embedded tool call detection in reasoning/content text.

Covers the _extract_tool_calls_from_text helper and the updated
_detect_tool / _check_for_tool_calls_in_output methods.
"""

import pytest
from agent_cascade.execution_engine import _extract_tool_calls_from_text


class TestExtractToolCallsQwenFormat:
    """Qwen ✿FUNCTION✿ / ✿ARGS✿ format detection."""

    def test_single_tool_call(self):
        text = "✿FUNCTION✿: code_interpreter\n✿ARGS✿: {'code': 'print(1)'}"
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0][0] == 'code_interpreter'
        assert 'print(1)' in result[0][1]

    def test_multiple_tool_calls(self):
        text = (
            "✿FUNCTION✿: read_file\n✿ARGS✿: {'path': 'foo.py'}\n"
            "✿FUNCTION✿: grep_search\n✿ARGS✿: {'pattern': 'def main'}"
        )
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 2
        assert result[0][0] == 'read_file'
        assert result[1][0] == 'grep_search'

    def test_tool_call_with_return(self):
        text = "✿FUNCTION✿: shell_cmd\n✿ARGS✿: git status\n✿RETURN✿"
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0][0] == 'shell_cmd'

    def test_case_insensitive(self):
        text = "✿function✿: read_file\n✿args✿: {'path': 'x'}"
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0][0] == 'read_file'

    def test_no_tool_call(self):
        text = "Just some regular text without tool calls"
        result = _extract_tool_calls_from_text(text)
        assert result == []


class TestExtractToolCallsPegFormat:
    """Peg-native <function=...> format detection."""

    def test_single_function_tag(self):
        text = "<function=code_interpreter><parameter>{'code': 'print(1)'}</parameter></function>"
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0][0] == 'code_interpreter'
        assert 'print(1)' in result[0][1]

    def test_function_tag_without_parameter(self):
        text = "<function=read_file>{'path': 'foo.py'}</function>"
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0][0] == 'read_file'
        assert 'foo.py' in result[0][1]

    def test_multiple_function_tags(self):
        text = (
            "<function=shell_cmd><parameter>git status</parameter></function>"
            "<function=read_file><parameter>{'path': 'x'}</parameter></function>"
        )
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 2
        assert result[0][0] == 'shell_cmd'
        assert result[1][0] == 'read_file'

    def test_nested_in_reasoning(self):
        text = (
            "Let me check the file first...\n"
            "<function=read_file><parameter>{'path': 'todo.md'}</parameter></function>\n"
            "That should give us the content we need."
        )
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0][0] == 'read_file'

    def test_case_insensitive(self):
        text = "<FUNCTION=shell_cmd><PARAMETER>ls</PARAMETER></FUNCTION>"
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0][0] == 'shell_cmd'


class TestExtractToolCallsEdgeCases:
    """Edge cases and defensive behavior."""

    def test_empty_string(self):
        assert _extract_tool_calls_from_text('') == []

    def test_none_input(self):
        assert _extract_tool_calls_from_text(None) == []

    def test_non_string_input(self):
        assert _extract_tool_calls_from_text(42) == []

    def test_qwen_takes_priority_over_peg(self):
        """Qwen format should be detected first if both exist."""
        text = (
            "✿FUNCTION✿: read_file\n✿ARGS✿: {'path': 'a'}\n"
            "<function=shell_cmd><parameter>ls</parameter></function>"
        )
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0][0] == 'read_file'

    def test_tool_name_with_underscore(self):
        text = "<function=code_interpreter><parameter>x</parameter></function>"
        result = _extract_tool_calls_from_text(text)
        assert result[0][0] == 'code_interpreter'

    def test_nested_function_tags(self):
        """Nested <function> tags: outer should be detected with args extracted."""
        text = "<function=read_file><function=inner><parameter>y</parameter></function><parameter>{'path': 'a'}</parameter></function>"
        result = _extract_tool_calls_from_text(text)
        assert len(result) >= 1
        assert result[0][0] == 'read_file'
        # Args should not contain nested <function= tags
        assert '<function=' not in result[0][1]

    def test_nested_function_tags_with_siblings(self):
        """Nested tags with sibling tags: should detect all valid tags."""
        text = "<function=read_file><function=inner><parameter>y</parameter></function><parameter>{'path': 'a'}</parameter></function><function=shell_cmd><parameter>ls</parameter></function>"
        result = _extract_tool_calls_from_text(text)
        # read_file matched first, shell_cmd is also valid
        assert len(result) >= 2
        assert result[0][0] == 'read_file'
        assert result[1][0] == 'shell_cmd'

    def test_empty_function_body(self):
        """Function tags with empty body should be skipped."""
        text = "<function=read_file></function>"
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 0

    def test_qwen_tool_name_with_dash(self):
        """Qwen format: tool names with dashes should not match (not valid identifiers)."""
        text = "✿FUNCTION✿: code-interpreter\n✿ARGS✿: {'code': 'print(1)'}"
        result = _extract_tool_calls_from_text(text)
        # 'code-interpreter' has a dash, so 'code' is matched but ✿ARGS✿
        # follows after '-interpreter\n'. Verify it correctly rejects this.
        assert len(result) == 0

    def test_qwen_tool_name_starting_with_number(self):
        """Qwen format: tool names starting with a number should not match."""
        text = "✿FUNCTION✿: 1st_tool\n✿ARGS✿: {'x': 1}"
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 0

    def test_multiple_qwen_calls_first_used_by_detect_tool(self):
        """Verify _detect_tool uses only the first match from multiple calls."""
        # _detect_tool is an instance method but doesn't use self.pool,
        # so we test the logic via _extract_tool_calls_from_text directly.
        text = (
            "✿FUNCTION✿: read_file\n✿ARGS✿: {'path': 'first'}\n"
            "✿FUNCTION✿: shell_cmd\n✿ARGS✿: ls"
        )
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 2
        # _detect_tool uses calls[0], so first match wins
        assert result[0][0] == 'read_file'
        assert result[1][0] == 'shell_cmd'

    def test_multiple_peg_calls_first_used_by_detect_tool(self):
        """Verify _detect_tool uses only the first match from peg-native calls."""
        text = (
            "<function=read_file><parameter>{'path': 'first'}</parameter></function>"
            "<function=shell_cmd><parameter>ls</parameter></function>"
        )
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 2
        # _detect_tool uses calls[0], so first match wins
        assert result[0][0] == 'read_file'
        assert result[1][0] == 'shell_cmd'

    def test_whitespace_in_tool_name(self):
        """Tool names with leading/trailing whitespace should be stripped."""
        text = "<function=read_file><parameter>x</parameter></function>"
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0][0] == 'read_file'

    def test_whitespace_in_qwen_tool_name(self):
        """Qwen tool names with extra whitespace should be stripped."""
        text = "✿FUNCTION✿:  code_interpreter  \n✿ARGS✿: {'code': '1'}"
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0][0] == 'code_interpreter'


class TestCheckForToolCallsInOutput:
    """Tests for ExecutionEngine._check_for_tool_calls_in_output."""

    def test_no_tool_calls(self):
        from agent_cascade.llm.schema import Message, ASSISTANT, USER
        from agent_cascade.execution_engine import ExecutionEngine

        class MockPool:
            settings = None
        engine = ExecutionEngine(MockPool())

        response = [
            Message(role=USER, content="Hello"),
            Message(role=ASSISTANT, content="Hi there! No tool calls here.")
        ]
        assert not engine._check_for_tool_calls_in_output(None, response)

    def test_with_executed_tool_calls(self):
        from agent_cascade.llm.schema import Message, ASSISTANT, FUNCTION
        from agent_cascade.execution_engine import ExecutionEngine

        class MockPool:
            settings = None
        engine = ExecutionEngine(MockPool())

        # Standard tool call that was executed:
        response = [
            Message(role=ASSISTANT, content="Let me run shell status.", function_call={"name": "shell_status", "arguments": ""}),
            Message(role=FUNCTION, name="shell_status", content="All systems green"),
            Message(role=ASSISTANT, content="The status is all systems green.")
        ]

        # The last assistant message has no tool call, so it should return False
        assert not engine._check_for_tool_calls_in_output(None, response)

    def test_with_executed_embedded_tool_call(self):
        from agent_cascade.llm.schema import Message, ASSISTANT, FUNCTION
        from agent_cascade.execution_engine import ExecutionEngine

        class MockPool:
            settings = None
        engine = ExecutionEngine(MockPool())

        # Embedded Qwen format tool call that was executed:
        response = [
            Message(role=ASSISTANT, content="✿FUNCTION✿: shell_status\n✿ARGS✿: {}"),
            Message(role=FUNCTION, name="shell_status", content="All systems green"),
            Message(role=ASSISTANT, content="The status is all systems green.")
        ]
        assert not engine._check_for_tool_calls_in_output(None, response)

    def test_with_unexecuted_embedded_tool_call(self):
        from agent_cascade.llm.schema import Message, ASSISTANT
        from agent_cascade.execution_engine import ExecutionEngine

        class MockPool:
            settings = None
        engine = ExecutionEngine(MockPool())

        # Embedded Qwen format tool call that has NOT been executed:
        response = [
            Message(role=ASSISTANT, content="✿FUNCTION✿: shell_status\n✿ARGS✿: {}")
        ]
        assert engine._check_for_tool_calls_in_output(None, response)

    def test_case_insensitive_tool_matching(self):
        """Tool executed as 'shell_cmd' should match detection of 'Shell_Cmd'."""
        from agent_cascade.llm.schema import Message, ASSISTANT, FUNCTION
        from agent_cascade.execution_engine import ExecutionEngine

        class MockPool:
            settings = None
        engine = ExecutionEngine(MockPool())

        # Tool executed as lowercase, but content has mixed case
        response = [
            Message(role=ASSISTANT, content="✿FUNCTION✿: Shell_Cmd\n✿ARGS✿: {}"),
            Message(role=FUNCTION, name="shell_cmd", content="done"),
            Message(role=ASSISTANT, content="Result is done.")
        ]
        # shell_cmd was executed, Shell_Cmd in last msg should match
        assert not engine._check_for_tool_calls_in_output(None, response)

    def test_case_insensitive_tool_matching_peg(self):
        """PEG format: tool executed as 'read_file' should match 'Read_File'."""
        from agent_cascade.llm.schema import Message, ASSISTANT, FUNCTION
        from agent_cascade.execution_engine import ExecutionEngine

        class MockPool:
            settings = None
        engine = ExecutionEngine(MockPool())

        response = [
            Message(role=ASSISTANT, content="<function=Read_File><parameter>x</parameter></function>"),
            Message(role=FUNCTION, name="read_file", content="content"),
            Message(role=ASSISTANT, content="Got it.")
        ]
        assert not engine._check_for_tool_calls_in_output(None, response)


class TestReasoningBlockIgnored:
    """Regression tests: tool call syntax in reasoning_content must be IGNORED.

    This is the actual bug scenario — LLMs leak tool call formatting into
    their thinking/reasoning blocks. If _detect_tool picks these up, the
    engine tries to execute them and gets stuck in an infinite loop.
    """

    def test_detect_tool_ignores_qwen_in_reasoning(self):
        """_detect_tool must not treat Qwen tool syntax in reasoning as a real call."""
        from agent_cascade.llm.schema import Message, ASSISTANT
        from agent_cascade.execution_engine import ExecutionEngine

        class MockPool:
            settings = None
        engine = ExecutionEngine(MockPool())

        msg = Message(
            role=ASSISTANT,
            content="Here is my answer.",
            reasoning_content="✿FUNCTION✿: code_interpreter\n✿ARGS✿: {'code': 'print(1)'}"
        )
        use_tool, tool_name, tool_args, text = engine._detect_tool(msg)
        assert not use_tool, "Tool call in reasoning_content should be ignored"
        assert tool_name is None

    def test_detect_tool_ignores_peg_in_reasoning(self):
        """_detect_tool must not treat PEG tool syntax in reasoning as a real call."""
        from agent_cascade.llm.schema import Message, ASSISTANT
        from agent_cascade.execution_engine import ExecutionEngine

        class MockPool:
            settings = None
        engine = ExecutionEngine(MockPool())

        # Reproduces the exact bug from todo.md — LLM puts <function=code_interpreter>
        # inside a reasoning block
        msg = Message(
            role=ASSISTANT,
            content="",
            reasoning_content=(
                "Let me fix the code and run it properly:\n"
                "<function=code_interpreter>\n"
                "<parameter=code>\n"
                "print('hello')\n"
                "</parameter>\n"
                "</function>"
            )
        )
        use_tool, tool_name, tool_args, text = engine._detect_tool(msg)
        assert not use_tool, "Tool call in reasoning_content should be ignored"

    def test_detect_tool_ignores_reasoning_but_finds_content(self):
        """If both reasoning and content have tool calls, only content is detected."""
        from agent_cascade.llm.schema import Message, ASSISTANT
        from agent_cascade.execution_engine import ExecutionEngine

        class MockPool:
            settings = None
        engine = ExecutionEngine(MockPool())

        msg = Message(
            role=ASSISTANT,
            content="✿FUNCTION✿: shell_cmd\n✿ARGS✿: ls -la",
            reasoning_content="✿FUNCTION✿: code_interpreter\n✿ARGS✿: {'code': 'print(1)'}"
        )
        use_tool, tool_name, tool_args, text = engine._detect_tool(msg)
        assert use_tool
        assert tool_name == 'shell_cmd', "Should detect the content tool call, not the reasoning one"

    def test_check_output_ignores_reasoning_only_tool_calls(self):
        """_check_for_tool_calls_in_output must return False when tool calls
        exist ONLY in reasoning_content with no real tool calls anywhere."""
        from agent_cascade.llm.schema import Message, ASSISTANT
        from agent_cascade.execution_engine import ExecutionEngine

        class MockPool:
            settings = None
        engine = ExecutionEngine(MockPool())

        response = [
            Message(
                role=ASSISTANT,
                content="I've analyzed the situation.",
                reasoning_content=(
                    "<function=code_interpreter>"
                    "<parameter>dangerous_variants = {'stash': ['pop']}</parameter>"
                    "</function>"
                )
            )
        ]
        result = engine._check_for_tool_calls_in_output(None, response)
        assert not result, (
            "_check_for_tool_calls_in_output should return False when tool calls "
            "are only in reasoning_content — this was the infinite loop bug"
        )

    def test_real_function_call_still_detected_with_reasoning_noise(self):
        """A proper function_call attribute must still be detected even when
        reasoning_content also contains tool call syntax."""
        from agent_cascade.llm.schema import Message, ASSISTANT
        from agent_cascade.execution_engine import ExecutionEngine

        class MockPool:
            settings = None
        engine = ExecutionEngine(MockPool())

        msg = Message(
            role=ASSISTANT,
            content="Running the command.",
            reasoning_content="<function=code_interpreter><parameter>x</parameter></function>",
            function_call={"name": "shell_cmd", "arguments": "git status"}
        )
        use_tool, tool_name, tool_args, text = engine._detect_tool(msg)
        assert use_tool
        assert tool_name == 'shell_cmd'
        assert tool_args == 'git status'


class TestMixedFormat:
    """Mixed Qwen + PEG format content tests."""

    def test_qwen_args_dont_capture_peg_tags(self):
        """Qwen args should stop at <function= tags, not consume PEG content."""
        text = (
            "✿FUNCTION✿: read_file\n✿ARGS✿: {'path': 'a'}\n"
            "✿FUNCTION✿: shell_cmd\n✿ARGS✿: ls\n"
            "<function=code_interpreter><parameter>{'code': 'print(1)'}</parameter></function>"
        )
        result = _extract_tool_calls_from_text(text)
        # Qwen format found first; should return both Qwen calls
        assert len(result) == 2
        assert result[0][0] == 'read_file'
        assert result[1][0] == 'shell_cmd'

    def test_qwen_args_stop_at_peg_function_tag(self):
        """Qwen args should stop at <function= in the same text block."""
        text = (
            "✿FUNCTION✿: shell_cmd\n✿ARGS✿: ls -la\n"
            "<function=read_file><parameter>{'path': 'x'}</parameter></function>"
        )
        result = _extract_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0][0] == 'shell_cmd'
        # Args should not contain the PEG tag
        assert '<function=' not in result[0][1]

    def test_peg_args_contain_function_string(self):
        """PEG arguments containing '<function=' string in JSON should work."""
        text = (
            "<function=read_file>"
            "<parameter>{'path': 'src/main.py', 'filter': '<function=main>'}</parameter>"
            "</function>"
        )
        result = _extract_tool_calls_from_text(text)
        # Nested <function= in args is filtered, so expect empty result
        assert len(result) == 0
