"""Regression tests for ``BaseChatModel._sanitize_fn_args``.

The sanitizer is a compatibility shim for malformed historical tool calls. In
particular, an LLM can emit a truncated first chunk (e.g. ``arguments == '{'``);
the engine persists that assistant message plus the invalid-JSON tool result.
Every later request re-sanitizes the same history entry, so an invalid-JSON
warning would repeat forever. Invalid JSON must therefore stay at debug level,
while a *valid* JSON value of the wrong shape still deserves a warning.
"""

from unittest.mock import patch

from agent_cascade.llm.base import BaseChatModel
from agent_cascade.llm.schema import ASSISTANT, FUNCTION, Message


def test_truncated_object_falls_back_to_empty_object_without_warning():
    with patch('agent_cascade.llm.base.logger') as mock_logger:
        result = BaseChatModel._sanitize_fn_args('{')

    assert result == '{}'
    mock_logger.warning.assert_not_called()
    mock_logger.debug.assert_called_once()


def test_valid_object_is_passed_through_unchanged():
    args = '{"command":"__wait","tool_id":4}'

    with patch('agent_cascade.llm.base.logger') as mock_logger:
        result = BaseChatModel._sanitize_fn_args(args)

    assert result == args
    mock_logger.warning.assert_not_called()
    mock_logger.debug.assert_not_called()


def test_valid_non_object_still_warns():
    with patch('agent_cascade.llm.base.logger') as mock_logger:
        result = BaseChatModel._sanitize_fn_args('[1, 2]')

    assert result == '{}'
    mock_logger.warning.assert_called_once()
    assert mock_logger.warning.call_args.args[1] == 'list'


def test_empty_arguments_are_debug_only():
    with patch('agent_cascade.llm.base.logger') as mock_logger:
        result = BaseChatModel._sanitize_fn_args('')

    assert result == '{}'
    mock_logger.warning.assert_not_called()
    mock_logger.debug.assert_called_once()


def test_real_malformed_history_pair_converts_to_valid_openai_arguments():
    """The exact shape found in the field: assistant args ``{`` + invalid-JSON result."""
    messages = [
        Message(role=ASSISTANT,
                content='',
                function_call={'name': 'shell_cmd', 'arguments': '{'},
                extra={'function_id': 'call_7fc2bfd77f5e47a79bc79bf9'}),
        Message(role=FUNCTION,
                name='shell_cmd',
                content="Error: Invalid JSON arguments for tool 'shell_cmd'.",
                extra={'function_id': 'call_7fc2bfd77f5e47a79bc79bf9'}),
    ]

    with patch('agent_cascade.llm.base.logger') as mock_logger:
        converted = BaseChatModel._conv_agent_cascade_messages_to_oai(messages)

    assert converted[0]['tool_calls'][0] == {
        'id': 'call_7fc2bfd77f5e47a79bc79bf9',
        'type': 'function',
        'function': {
            'name': 'shell_cmd',
            'arguments': '{}',
        },
    }
    assert converted[1]['role'] == 'tool'
    assert converted[1]['tool_call_id'] == 'call_7fc2bfd77f5e47a79bc79bf9'
    mock_logger.warning.assert_not_called()
