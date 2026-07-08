#!/usr/bin/env python3
"""Test to expose the skip condition bug."""

import sys
sys.path.insert(0, 'N:\\work\\WD\\AgentCascade_unified')

from agent_cascade.tools.custom.forget_last_tool import ForgetLast
from unittest.mock import MagicMock, patch

def test_skip_condition_bug():
    print("Testing the critical skip condition bug...")
    
    with patch('agent_cascade.tools.custom.forget_last_tool.BaseTool.__init__', return_value=None):
        tool = ForgetLast()
        # Config: max_chars=100, min_char_limit=200
        tool.cfg = {
            'truncate_max_chars': 100,
            'min_char_limit': 200
        }
        
        mock_pool = MagicMock()
        mock_instance = MagicMock()
        mock_instance._compression_lock = MagicMock(__enter__=lambda s: None, __exit__=lambda s, *args: None)
        mock_instance.conversation = []
        mock_instance._cached_token_count = 1000
        mock_instance._last_token_count_conversation_length = 50
        
        mock_logger = MagicMock()
        mock_pool.instance_loggers = {'test_agent': mock_logger}
        mock_pool.get_conversation.return_value = []
        mock_pool.get_instance.return_value = mock_instance
        
        tool.agent_pool = mock_pool
        tool.agent_name = 'test_agent'
        
        # Test: 150-char message should be truncated (it's > max_chars=100)
        # But with current OR logic, it will be skipped because 150 <= min_char_limit=200
        history = [
            {'role': 'function', 'content': 'A' * 150, 'name': 'test_tool'}
        ]
        mock_pool.get_conversation.return_value = history.copy()
        
        result = tool.call({'count': 1})
        print(f"Result: {result}")
        
        # Check if truncation actually happened
        content_after = history[0]['content']
        if 'TRUNCATED' in content_after:
            print("✓ PASS: 150-char message was truncated (bug is FIXED)")
        else:
            print("✗ FAIL: 150-char message was NOT truncated (BUG STILL PRESENT)")
            print(f"Content after: {content_after[:200]}...")

if __name__ == '__main__':
    test_skip_condition_bug()