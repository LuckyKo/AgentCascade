"""Validate that AC's token estimation matches llama.cpp's actual reported counts.

This test compares:
- AC's total token count (via get_message_stats() per message)
- llama.cpp's reported usage.prompt_tokens from API response

The difference should be within 5% tolerance after the CHAT_TEMPLATE_TOKEN_OVERHEAD fix.
"""

import json
import sys
import os
from typing import List

# Path mapping for different environments
for p in ['/extra_rw_0', '/workspace']:
    if os.path.isdir(p):
        sys.path.insert(0, p)
        break

from agent_cascade.llm.schema import Message
from agent_cascade.utils.utils import get_message_stats
from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
from agent_cascade.settings import CHAT_TEMPLATE_TOKEN_OVERHEAD


def build_realistic_conversation() -> List[Message]:
    """Build a realistic multi-turn conversation similar to AC workload."""
    return [
        Message(role='system', content=(
            "You are a practical senior software engineer. "
            "Tone: Direct, practical, concise. Prefer solutions over explanations."
        )),
        Message(role='user', content="Need to validate that token estimation fix actually makes AC's counts match llama.cpp within acceptable tolerance."),
        Message(role='assistant', content=(
            "I'll create and run a token estimation validation test. "
            "Let me start by checking the existing codebase structure."
        )),
        Message(role='user', content="Can you also check if the overhead constant is configurable via environment variable?"),
        Message(role='assistant', content=(
            "Yes, CHAT_TEMPLATE_TOKEN_OVERHEAD defaults to 5 and can be overridden with:\n"
            "QWEN_AGENT_CHAT_TEMPLATE_TOKEN_OVERHEAD=<value>\n\n"
            "This accounts for llama.cpp chat template wrapper tokens per message."
        )),
        Message(role='user', content="Write a test that compares our count vs what llama.cpp actually reports in usage.prompt_tokens"),
        Message(role='assistant', content=(
            "Here's the approach:\n"
            "1. Build a conversation with ~25 messages\n"
            "2. Sum get_message_stats() for each message to get AC's estimate\n"
            "3. Send to llama.cpp API and read usage.prompt_tokens\n"
            "4. Assert difference < 5%\n\n"
            "Writing test script now..."
        )),
        Message(role='user', content="Also include a fallback that works without llama.cpp running, using the Qwen tokenizer with chat template applied."),
        Message(role='assistant', content=(
            "Good idea. The fallback will:\n"
            "- Apply Qwen's chat template format manually\n"
            "- Count tokens on the templated output\n"
            "- Compare against AC's estimate\n\n"
            "This gives us a deterministic check without needing the server."
        )),
        Message(role='user', content="Make sure to handle edge cases like empty messages and function calls."),
        Message(role='assistant', content=(
            "Will include:\n"
            "- Empty message test\n"
            "- Function call message test\n"
            "- Mixed length messages (short + long)\n"
            "- Verify each path adds CHAT_TEMPLATE_TOKEN_OVERHEAD correctly."
        )),
        Message(role='user', content="Run it and report the actual numbers."),
        Message(role='assistant', content="Running now..."),
        Message(role='user', content="What's the error rate before vs after the fix?"),
        Message(role='assistant', content=(
            "Before fix: ~37% underestimation (raw tokens only)\n"
            "After fix: ~4% error with CHAT_TEMPLATE_TOKEN_OVERHEAD=5\n\n"
            "The overhead accounts for role tags, newlines, and template markers."
        )),
        Message(role='user', content="Can we tune the constant further to get closer?"),
        Message(role='assistant', content=(
            "Yes, but it's model-dependent. For Qwen3 models with llama.cpp:\n"
            "- Each message gets: '▌{role}\\n{content}▌\\n'\n"
            "- That's roughly 4-6 tokens of overhead per message\n"
            "- Value of 5 is a good average.\n\n"
            "We can adjust if systematic bias appears in production."
        )),
        Message(role='user', content="Show me the final comparison table."),
        Message(role='assistant', content=(
            "| Method                    | Tokens   | Error vs llama.cpp |\n"
            "|---------------------------|----------|--------------------|\n"
            "| Raw content only          | ~850     | ~37% under         |\n"
            "| With overhead (+5/msg)    | ~945     | ~4%                |\n"
            "| llama.cpp actual          | 985      | baseline           |\n\n"
            "Within acceptable tolerance for context window budgeting."
        )),
        Message(role='user', content="Good enough. Ship it."),
        Message(role='assistant', content="Done. Test script created at tests/test_token_estimation.py"),
    ]


def count_tokens_ac(messages: List[Message]) -> int:
    """Count tokens using AC's get_message_stats() per message."""
    return sum(get_message_stats(m)['tokens'] for m in messages)


def count_tokens_templated(messages: List[Message]) -> int:
    """Count tokens by applying Qwen chat template manually (fallback when llama.cpp not available)."""
    # Qwen chat template format used by llama.cpp:
    # For each message: ▌{role}\n{content}▌\n
    # Final suffix for generation: ▌assistant\n
    parts = []
    for m in messages:
        parts.append(f"▌{m.role}\n{m.content}▌\n")
    parts.append("▌assistant\n")  # Generation prompt suffix
    templated = ''.join(parts)
    return qwen_count(templated)


def count_tokens_llama_cpp(messages: List[Message], api_base: str, model: str) -> int:
    """Send conversation to llama.cpp API and return reported usage.prompt_tokens."""
    import urllib.request
    import urllib.error

    # Convert messages to OpenAI-compatible format
    openai_messages = [{"role": m.role, "content": m.content} for m in messages]

    payload = {
        "model": model,
        "messages": openai_messages,
        "max_tokens": 1,  # Minimal generation to get prompt token count
        "temperature": 0,
    }

    url = f"{api_base.rstrip('/')}/chat/completions"
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            return result.get("usage", {}).get("prompt_tokens")
    except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
        raise ConnectionError(f"llama.cpp API call failed: {e}")


def run_test():
    """Main test: compare AC's token estimation against llama.cpp."""
    messages = build_realistic_conversation()

    print("=" * 70)
    print("Token Estimation Validation Test")
    print("=" * 70)
    print(f"Messages: {len(messages)}")
    print(f"CHAT_TEMPLATE_TOKEN_OVERHEAD: {CHAT_TEMPLATE_TOKEN_OVERHEAD}")
    print()

    # AC's estimate
    ac_tokens = count_tokens_ac(messages)
    print(f"AC estimate (get_message_stats):     {ac_tokens} tokens")

    # Fallback: templated count (deterministic, no server needed)
    templated_tokens = count_tokens_templated(messages)
    print(f"Templated count (Qwen tokenizer):    {templated_tokens} tokens")

    # Try llama.cpp API
    api_base = "http://127.0.0.1:1234/v1"
    model = "qwen3.6-35b-a3b"  # Default model from config/api_endpoints.json
    llama_cpp_tokens = None

    print(f"\nAttempting llama.cpp API ({api_base})...")
    try:
        llama_cpp_tokens = count_tokens_llama_cpp(messages, api_base, model)
        print(f"llama.cpp reported prompt_tokens: {llama_cpp_tokens} tokens")
    except ConnectionError as e:
        print(f"llama.cpp not available (expected in test env): {e}")
        print("Using templated count as reference instead.")

    # Compare AC vs reference
    reference_tokens = llama_cpp_tokens if llama_cpp_tokens is not None else templated_tokens

    if reference_tokens > 0:
        error_pct = abs(ac_tokens - reference_tokens) / reference_tokens * 100
        print()
        print("-" * 70)
        print("Comparison Results:")
        print(f"  AC estimate:              {ac_tokens}")
        print(f"  Reference ({'llama.cpp' if llama_cpp_tokens else 'templated'}): {reference_tokens}")
        print(f"  Difference:               {ac_tokens - reference_tokens:+d} tokens")
        print(f"  Error rate:               {error_pct:.1f}%")
        print()

        # Tolerance check
        tolerance = 5.0
        if error_pct <= tolerance:
            print(f"PASS: Error {error_pct:.1f}% is within {tolerance}% tolerance.")
        else:
            print(f"FAIL: Error {error_pct:.1f}% exceeds {tolerance}% tolerance.")
            print("  Consider adjusting CHAT_TEMPLATE_TOKEN_OVERHEAD.")
    else:
        print("SKIP: Reference token count is zero, cannot compare.")

    print("=" * 70)


if __name__ == '__main__':
    run_test()