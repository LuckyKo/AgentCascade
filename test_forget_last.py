#!/usr/bin/env python3
"""Comprehensive test of forget_last tool edge cases and correctness."""

import sys
sys.path.insert(0, 'N:\\work\\WD\\AgentCascade_unified')

from agent_cascade.tools.custom.forget_last_tool import ForgetLast
from unittest.mock import MagicMock, patch

def create_message(role, content, name=None):
    """Create a message dict for testing."""
    return {'role': role, 'content': content, 'name': name} if name else {'role': role, 'content': content}

def test_edge_cases():
    print("=" * 60)
    print("Testing forget_last tool edge cases")
    print("=" * 60)
    
    # Setup mock agent pool and instance
    with patch('agent_cascade.tools.custom.forget_last_tool.BaseTool.__init__', return_value=None):
        tool = ForgetLast()
        tool.cfg = {
            'truncate_max_chars': 100,
            'min_char_limit': 200
        }
        
        # Mock agent pool and instance
        mock_pool = MagicMock()
        mock_instance = MagicMock()
        mock_instance._compression_lock = MagicMock(__enter__=lambda s: None, __exit__=lambda s, *args: None)
        mock_instance.conversation = []
        mock_instance._cached_token_count = 1000
        mock_instance._last_token_count_conversation_length = 50
        
        # Mock instance loggers
        mock_logger = MagicMock()
        mock_pool.instance_loggers = {'test_agent': mock_logger}
        mock_pool.get_conversation.return_value = []
        mock_pool.get_instance.return_value = mock_instance
        
        tool.agent_pool = mock_pool
        tool.agent_name = 'test_agent'
        
        # Test cases
        test_cases = [
            {
                'name': 'Empty justification',
                'params': {'count': 1},
                'history': [
                    create_message('function', 'A' * 300, name='test_tool'),
                ],
                'expected_marker_contains': 'TRUNCATED',
                'expected_not_contains': 'Truncated:',  # Should use default message when no justification
            },
            {
                'name': 'Whitespace-only justification',
                'params': {'count': 1, 'justification': '   \n\t   '},
                'history': [
                    create_message('function', 'B' * 300, name='test_tool'),
                ],
                'expected_marker_contains': 'TRUNCATED',
                'expected_not_contains': '[TRUNCATED] Truncated:',  # Should treat as empty and use default
            },
            {
                'name': 'Very long justification (150 chars)',
                'params': {'count': 1, 'justification': 'A' * 150},
                'history': [
                    create_message('function', 'C' * 300, name='test_tool'),
                ],
                'expected_marker_contains': '[TRUNCATED] Truncated:',
                'expected_content_length_le': 100 + len(" ... [TRUNCATED] Truncated: A" * 97 + '...' + '. ~X chars freed.'),
            },
            {
                'name': 'count=1, single message',
                'params': {'count': 1},
                'history': [
                    create_message('function', 'D' * 300, name='test_tool'),
                ],
                'expected_truncated_count': 1,
            },
            {
                'name': 'No messages (empty history)',
                'params': {'count': 1},
                'history': [],
                'expected_return_contains': 'Error: No conversation history found',
            },
            {
                'name': 'All messages already short (≤200 chars)',
                'params': {'count': 1},
                'history': [
                    create_message('function', 'E' * 150, name='test_tool'),
                ],
                'expected_return_contains': "Nothing to truncate",
            },
            {
                'name': 'Mixed lengths - some short, some long',
                'params': {'count': 2},
                'history': [
                    create_message('function', 'F' * 150, name='tool1'),  # Short, should skip
                    create_message('function', 'G' * 300, name='tool2'),  # Long, should truncate
                ],
                'expected_truncated_count': 1,
            },
            {
                'name': 'Non-function messages in history',
                'params': {'count': 1},
                'history': [
                    create_message('user', 'Hello'),
                    create_message('assistant', 'Hi there'),
                    create_message('function', 'X' * 300, name='test_tool'),
                    create_message('user', 'Thanks'),
                ],
                'expected_truncated_count': 1,
            },
            {
                'name': 'count exceeds available function messages',
                'params': {'count': 10},
                'history': [
                    create_message('function', 'Y' * 300, name='tool1'),
                    create_message('function', 'Z' * 300, name='tool2'),
                ],
                'expected_truncated_count': 2,
            },
            {
                'name': 'Justification with special characters',
                'params': {'count': 1, 'justification': "Test: \"quoted\" & 'apostrophe' < >"},
                'history': [
                    create_message('function', 'W' * 300, name='test_tool'),
                ],
                'expected_marker_contains': '[TRUNCATED] Truncated:',
            },
            {
                'name': 'content is not string (should be converted)',
                'params': {'count': 1},
                'history': [
                    {'role': 'function', 'content': 12345, 'name': 'test_tool'},  # Non-string content
                ],
                'expected_truncated_count': 1,
                'should_not_crash': True,
            },
        ]
        
        for test in test_cases:
            print(f"\nTest: {test['name']}")
            
            # Reset mock
            mock_logger.reset_mock()
            mock_instance.conversation = [msg.copy() if isinstance(msg, dict) else msg for msg in test['history']]
            mock_pool.get_conversation.return_value = mock_instance.conversation.copy()
            
            try:
                result = tool.call(test['params'])
                
                # Verify expectations
                if 'expected_return_contains' in test:
                    assert test['expected_return_contains'] in result, f"Expected '{test['expected_return_contains']}' in result: {result}"
                    print(f"  ✓ Return contains expected text")
                
                if 'expected_truncated_count' in test:
                    # Check actual truncation count from log or return
                    assert str(test['expected_truncated_count']) in result, f"Expected truncated count {test['expected_truncated_count']} in result: {result}"
                    print(f"  ✓ Truncated correct number of messages")
                
                if 'expected_marker_contains' in test:
                    # Find the message that should have been truncated
                    for msg in mock_instance.conversation:
                        content = str(msg.get('content', ''))
                        if 'TRUNCATED' in content:
                            assert test['expected_marker_contains'] in content, f"Expected '{test['expected_marker_contains']}' in content: {content}"
                            print(f"  ✓ Truncation marker correct")
                            break
                    else:
                        raise AssertionError("No message contains TRUNCATED marker")
                
                if 'expected_not_contains' in test:
                    for msg in mock_instance.conversation:
                        content = str(msg.get('content', ''))
                        if 'TRUNCATED' in content:
                            assert test['expected_not_contains'] not in content, f"Unexpected '{test['expected_not_contains']}' in content: {content}"
                            print(f"  ✓ Does not contain unexpected text")
                            break
                
                if 'should_not_crash' in test and test['should_not_crash']:
                    print(f"  ✓ Did not crash with non-string content")
                    
            except Exception as e:
                print(f"  ✗ FAILED: {e}")
                raise

if __name__ == '__main__':
    try:
        test_edge_cases()
        print("\n" + "=" * 60)
        print("ALL TESTS PASSED")
        print("=" * 60)
    except Exception as e:
        print(f"\nTEST FAILED: {e}")
        sys.exit(1)