"""Unit tests for BUG_0049 fix.

1. extract_instance_output extracts earlier prose / turn limit notice when conversation
   ends with a tool-call-only message or function message.
2. _validate_call_agent_args reports precise error message when instance_name or
   agent_class is missing.
"""

from agent_cascade.compression.helpers import extract_instance_output
from agent_cascade.tool_dispatcher import ToolDispatcher


def test_extract_instance_output_turn_exhaustion_tool_call_tail():
    """Verify extract_instance_output returns prose text & notice when last msg is tool call."""
    messages = [
        {'role': 'user', 'content': 'Do task'},
        {
            'role': 'assistant',
            'content': [
                {'text': 'Completed step 1 analysis.\n\n[Turn limit reached — results may be incomplete. Continue if needed.]'}
            ]
        },
        {
            'role': 'assistant',
            'content': [
                {
                    'text': '',
                    'function_call': {'name': 'code_interpreter', 'arguments': '{"code": "print(1)"}'}
                }
            ]
        }
    ]

    result = extract_instance_output(messages, 'test_agent')
    assert 'Completed step 1 analysis.' in result
    assert '[Turn limit reached — results may be incomplete. Continue if needed.]' in result
    assert '<tool_call>' not in result


def test_extract_instance_output_function_tail_with_earlier_prose():
    """Verify extract_instance_output returns earlier prose when tail is a function message."""
    messages = [
        {'role': 'user', 'content': 'Do task'},
        {
            'role': 'assistant',
            'content': 'Partial work finished prior to function execution.'
        },
        {
            'role': 'function',
            'name': 'read_file',
            'content': 'File contents...'
        }
    ]

    result = extract_instance_output(messages, 'test_agent')
    assert result == 'Partial work finished prior to function execution.'


def test_call_agent_validation_error_messages():
    """Verify ToolDispatcher._validate_call_agent_args returns precise missing parameter errors."""
    td = ToolDispatcher(pool=None)

    # Missing both
    _, _, err_both = td._validate_call_agent_args({}, 'caller')
    assert err_both == 'Error: call_agent requires instance_name and agent_class.'

    # Missing instance_name only
    _, _, err_no_inst = td._validate_call_agent_args({'agent_class': 'coder'}, 'caller')
    assert err_no_inst == 'Error: call_agent requires instance_name.'

    # Missing agent_class only
    _, _, err_no_cls = td._validate_call_agent_args({'instance_name': 'child_1'}, 'caller')
    assert err_no_cls == 'Error: call_agent requires agent_class.'

    # Both present
    inst, cls, err_ok = td._validate_call_agent_args({'instance_name': 'child_1', 'agent_class': 'coder'}, 'caller')
    assert err_ok is None
    assert inst == 'child_1'
    assert cls == 'coder'
