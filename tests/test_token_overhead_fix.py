"""Test that token estimation now accounts for chat template overhead.

This demonstrates the fix for the ~10% underestimation bug (todo.md line 60).

Before fix: get_message_stats() counted only raw content tokens.
After fix:  get_message_stats() adds CHAT_TEMPLATE_TOKEN_OVERHEAD per message
            to account for llama.cpp's chat template wrapper tokens.
"""

import sys
from pathlib import Path

repo_root = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(repo_root))

from agent_cascade.llm.schema import Message
from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
from agent_cascade.utils.utils import get_message_stats
from agent_cascade.settings import CHAT_TEMPLATE_TOKEN_OVERHEAD


def test_single_message_overhead():
    """Verify that a single message includes the chat template overhead."""
    msg = Message(role='user', content='Hello, how are you?')
    
    # Raw content tokens (what we counted before)
    raw_tokens = qwen_count('Hello, how are you?')
    
    # What get_message_stats now returns
    stats = get_message_stats(msg)
    
    print(f"Single message test:")
    print(f"  Raw content tokens:          {raw_tokens}")
    print(f"  Chat template overhead:      {CHAT_TEMPLATE_TOKEN_OVERHEAD}")
    print(f"  get_message_stats(tokens):   {stats['tokens']}")
    print(f"  Expected (raw + overhead):   {raw_tokens + CHAT_TEMPLATE_TOKEN_OVERHEAD}")
    
    assert stats['tokens'] == raw_tokens + CHAT_TEMPLATE_TOKEN_OVERHEAD, \
        f"Mismatch: got {stats['tokens']}, expected {raw_tokens + CHAT_TEMPLATE_TOKEN_OVERHEAD}"
    print("  PASS\n")


def test_conversation_underestimation_comparison():
    """Compare old vs new estimation for a realistic conversation."""
    
    # Build a sample conversation similar to what AC sees in practice
    messages = [
        Message(role='system', content='You are a helpful assistant.'),
        Message(role='user', content='Can you help me write a Python script?'),
        Message(role='assistant', content='Sure! What kind of script do you need?'),
        Message(role='user', content='I need something to parse log files and extract errors.'),
        Message(role='assistant', content='Here is a basic example that reads a log file...'),
    ]
    
    # Old approach: sum raw content tokens only
    old_total = sum(qwen_count(m.content) for m in messages)
    
    # New approach: use get_message_stats which includes overhead
    new_total = sum(get_message_stats(m)['tokens'] for m in messages)
    
    # What llama.cpp would actually receive (simulated with chat template)
    # Qwen template wraps each message with {role}\n{content}</think>\n
    templated_parts = []
    for m in messages:
        templated = f"▌{m.role}\n{m.content}▌\n"
        templated_parts.append(templated)
    templated_text = ''.join(templated_parts) + "▌assistant\n"  # Generation prompt suffix
    llama_cpp_tokens = qwen_count(templated_text)
    
    print(f"Conversation test ({len(messages)} messages):")
    print(f"  Old estimate (raw only):       {old_total} tokens")
    print(f"  New estimate (with overhead):  {new_total} tokens")
    print(f"  Simulated llama.cpp count:     {llama_cpp_tokens} tokens")
    
    old_error = abs(old_total - llama_cpp_tokens) / llama_cpp_tokens * 100
    new_error = abs(new_total - llama_cpp_tokens) / llama_cpp_tokens * 100
    
    print(f"  Old error rate:                {old_error:.1f}%")
    print(f"  New error rate:                {new_error:.1f}%")
    print()
    
    # The new estimate should be closer to llama.cpp's actual count
    assert new_error < old_error, \
        f"New estimate ({new_error:.1f}% error) should be better than old ({old_error:.1f}% error)"
    print("  PASS: New estimate is closer to actual llama.cpp token count\n")


def test_overhead_constant_configurable():
    """Verify the overhead constant can be adjusted via environment variable."""
    # This is just a sanity check that the constant exists and has expected default
    assert CHAT_TEMPLATE_TOKEN_OVERHEAD == 5, \
        f"Default overhead should be 5, got {CHAT_TEMPLATE_TOKEN_OVERHEAD}"
    print(f"Configurable overhead constant: CHAT_TEMPLATE_TOKEN_OVERHEAD = {CHAT_TEMPLATE_TOKEN_OVERHEAD}")
    print("  PASS\n")


if __name__ == '__main__':
    print("=" * 60)
    print("Token Estimation Fix Verification")
    print("=" * 60 + "\n")
    
    test_single_message_overhead()
    test_conversation_underestimation_comparison()
    test_overhead_constant_configurable()
    
    print("=" * 60)
    print("All tests passed!")
    print("=" * 60)