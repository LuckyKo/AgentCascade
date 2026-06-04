# Compression Bug Trace Report

## Issue
Sub-agent calls to `compress_context` fail with "Not enough messages to compress" even though the sub-agent has many messages.

## Key Finding: Kwargs Flows Correctly
After thorough tracing, `agent_instance_name` IS correctly passed through the entire chain:
orchestrator → agent.run() → _run() → _call_tool() → tool.call(). kwargs is NOT lost.

**Both orchestrator and sub-agents are OrchestratorAgent instances**, so the sub-agent uses the 
orchestrator's custom `_run()` method (NOT fncall_agent's). Tool calls go through lines 1505-1534,
which explicitly sets `agent_instance_name` in call_kwargs if missing.

## Three Root Causes

### Bug 1: Stale Conversation View During Sub-Agent Turns (PRIMARY)

When a sub-agent calls `compress_context`, the tool reads the conversation from the pool via 
`get_compression_target_set(target_agent_name)`. This returns `conv` from 
`instance_conversations[instance_name]`.

**But `conv` only contains messages committed from PREVIOUS turns.** The current turn's messages 
(LLM output, intermediate tool calls/results) are in local lists (`working_history`, `llm_messages`)
that haven't been written back to the pool yet.

Messages are only committed at line 2222: `conv.extend(final_resp)` — after the entire sub-agent 
turn completes. The working history is a slice made at line 2099:
```python
working_history = self.agent_pool.slice_history_for_llm(conv)
```

**Impact**: If the sub-agent has been running for many turns, `conv` is still large enough. But if 
it's a fresh sub-agent (first or second turn) or if previous compressions already trimmed `conv`, 
then `active_set` could be small → "Not enough messages to compress".

### Bug 2: Shared Agent Object — `self.agent_name` Fallback is Wrong

In `agent_factory.py` line 86-88:
```python
compress_tool = CompressContext()
compress_tool.agent_pool = agent_pool
compress_tool.agent_name = agent_name  # "coder" - the CLASS name, not instance name
```

All instances of the same agent class share the SAME CompressContext tool object (because agents 
are cached in the pool). The fallback `self.agent_name` resolves to `"coder"` instead of the 
instance name. If kwargs somehow doesn't carry `agent_instance_name`, it would look up 
`instance_conversations["coder"]` which likely has very few or no messages.

### Bug 3: `agent_obj.instance_name` is Never Set

The resolution chain at compression_tools.py line 78 tries 
`getattr(agent_obj, 'instance_name', None)` — but this attribute is never set on the agent object. 
It always returns None.

## Data Flow Trace (kwargs path — WORKS)

### Step 1: Orchestrator → Sub-agent run() ✅
```python
# agent_orchestrator.py:2107
for resp in agent.run(working_history, agent_instance_name=instance_name):
```
`agent_instance_name="CompressionBugTracer"` is passed correctly.

### Step 2: agent.run() → _run() ✅
```python
# agent.py:130
for rsp in self._run(messages=new_messages, **kwargs):
```
kwargs (including `agent_instance_name`) flows through.

### Step 3: OrchestratorAgent._run() tool handling ✅
```python
# agent_orchestrator.py:1505-1507
call_kwargs = kwargs.copy()
if 'agent_instance_name' not in call_kwargs:
    call_kwargs['agent_instance_name'] = self.session_name
```
`self.session_name` was set at line 935 to `instance_name`.

### Step 4: agent._call_tool() → tool.call() ✅
```python
# agent.py:246-248
if 'agent_obj' not in kwargs:
    kwargs['agent_obj'] = self
tool_result = tool.call(tool_args, **kwargs)
```
kwargs contains both `agent_instance_name` AND `agent_obj`.

### Step 5: CompressContext.call() — agent_name resolution ✅ (kwargs path works)
```python
# compression_tools.py:76-81
agent_name = (
    kwargs.get('agent_instance_name') or           # "CompressionBugTracer" ✅
    getattr(agent_obj, 'instance_name', None) or   # None ❌ (never set)
    self.agent_name or                             # "coder" ❌ (class name)
    'orchestrator'                                 # fallback
)
```

The kwargs path works. But the BUG is that compress_context reads from the pool's `conv`, which 
is stale during a turn (Bug 1). And the fallbacks are broken (Bugs 2 & 3).

## Recommended Fix

### Fix Bug 3 (easy, defense in depth):
In `_stream_sub_agent_call`, after getting the agent from the pool (around line 1815):
```python
agent = self.agent_pool.get_agent(agent_class)
agent.instance_name = instance_name  # So getattr fallback works
```

### Fix Bug 2 (defense in depth):
Same location:
```python
if 'compress_context' in agent.function_map:
    agent.function_map['compress_context'].agent_name = instance_name
```

### Fix Bug 1 (structural, most important):
Either:
- **Option A**: Pass the current working set to compress_context instead of reading from pool
- **Option B**: Commit messages to conv incrementally during the turn, not just at the end
- **Option C**: Have compress_context read from a passed-in messages list rather than the pool

## Files Involved
- `agent_orchestrator.py` — line 2107 (passes agent_instance_name), line 1815 (get_agent), 
  line 2099 (slice_history_for_llm creates stale copy), line 2222 (commit at end of turn)
- `agent_orchestrator.py` — line 934-935 (sets session_name), line 1505-1534 (orchestrator tool handling)
- `compression_tools.py` — line 76-81 (agent_name resolution)
- `agent_factory.py` — line 88 (sets compress_tool.agent_name to class name)
- `compression/core.py` — line 132-141 ("Not enough messages" error), line 79-81 (get_compression_target_set)