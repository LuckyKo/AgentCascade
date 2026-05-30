# Lessons Learned — AgentCascade Bug Investigation

## Critical Pattern: Dict vs Message Object Type Mismatch

**The Lesson:** After forced compression, the AgentPool returns dict messages. The LLM's `chat()` method tracks input type and returns the same type — if all inputs are dicts, output is dicts. This causes AttributeError throughout the codebase where `.name`, `.extra`, `.function_call` are accessed on what should be Message objects but are actually dicts.

**How to fix:** Always use isinstance-aware access patterns:
```python
# BAD (crashes after forced compression):
msg.extra.get('key')
msg.function_call.name
msg.content

# GOOD:
if isinstance(msg, dict):
    extra = msg.get('extra', {})
else:
    extra = getattr(msg, 'extra', None) or {}
```

## Critical Pattern: Always Use slice_history_for_llm After Compression

**The Lesson:** After compression runs (either forced or via tool call), always sync messages from the pool using `slice_history_for_llm()`, NOT `get_conversation()`. The latter returns the full cumulative history including old compressed messages, which defeats the purpose of compression.

## Architecture Notes

- AgentPool stores messages as **dicts**
- LLM chat method converts dicts→Message internally and returns based on input type
- `_return_message_type` in base.py determines output format
- All message consumers should be resilient to both types

## Quick Reference: Files That Need Dict-Safe Handling

Any file that processes LLM output messages needs dict safety:
1. `agent_cascade/agent.py` — run() loop, _detect_tool()
2. `agent_cascade/agents/fncall_agent.py` — _run() tool detection
3. `agent_orchestrator.py` — truncation detection, tool result processing, sub-agent hooks
