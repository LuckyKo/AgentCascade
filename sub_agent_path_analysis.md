# Sub-Agent Code Path Analysis

## Executive Summary

The sub-agent path in `execution_engine.py` is **NOT redundant** and should NOT be unified with the main agent path. However, there is a **significant asymmetry in system message construction** between the two paths that could contribute to current soul-loading problems.

---

## 1. Architecture Overview: Unified vs. Original

### Original AgentCascade (Two Separate Paths)

The original codebase had **two completely independent execution paths**:

| Aspect | Main Agent Path | Sub-Agent Path |
|--------|-----------------|----------------|
| Entry point | `api_server.run_agent_thread()` (line 871) | `agent_orchestrator._stream_sub_agent_call()` (line 1796) |
| LLM execution | `agent_runner.run()` via `_run()` method | `agent.run()` with monkey-patched hooks |
| History source | `session['history']` | `agent_pool.instance_conversations[name]` |
| State management | Session dictionary + pool | Pool only |
| Compression handling | Built into `_run()` | Monkey-patched via `hooked_call_llm` (line 2064) |

These were **fundamentally different code paths** — not just parameterized differently, but entirely separate functions with separate loops.

### Unified AgentCascade (Single Path)

The unified version explicitly states its design goal in the docstring:

> "Stateless execution coordinator that drives ALL agent instances through a single unified loop. Replaces both api_server.run_agent_thread() and the old sub-agent execution path — eliminating the structural duality."

**ALL agents now go through `ExecutionEngine.run()`** (line 49). There is no separate execution loop for any agent type.

---

## 2. Where Paths Diverge: Line-by-Line Analysis

### Divergence Point 1: System Message Injection (`execution_engine.py` line 143)

```python
# _setup_turn() method, lines 141-231
if instance.parent_instance is None and len(conv) > 0:
    # Only ROOT agents (no parent) get these injections:
    # 1. Identity update: "You are [instance]." (line 154)
    # 2. Session Metadata section (lines 158-182)
    # 3. Available Resources injection (lines 190-217)
    #    - Lists available agent types
    #    - Lists enabled tools
    #    - Includes "CURRENT AVAILABLE RESOURCES" header block
    # 4. Argument Reuse instructions (lines 220-225)
```

**What sub-agents miss:** They never get the `--- CURRENT AVAILABLE RESOURCES ---` block that lists available agent types and enabled tools. This is intentional — sub-agents already know their own capabilities from their system message — but it creates an asymmetry in what each agent type knows about the environment.

### Divergence Point 2: System Message Construction

#### Main Agent (`api_integration.py` line 32-94, `run_agent_unified.py` line 91-96):
```python
# The system message is passed IN from outside:
create_main_agent_instance(
    pool=pool,
    instance_name=instance_name,
    system_message_content=system_message_content,  # ← From soul.md or history
)
```

The main agent's `system_message_content` comes from one of these sources:
1. **Primary:** Extracted from `history_for_agent` (which was loaded via soul_loader → `create_agent_from_soul`)
2. **Fallback:** Extracted from existing pool instance's first SYSTEM message
3. **Last resort:** From `agent_runner.base_system_message` or `system_message` attribute

#### Sub-Agent (`execution_engine.py` lines 1092-1207):
```python
def _create_and_run_agent(self, agent_class, instance_name, args, caller):
    # System message is BUILT INTERNALLY:
    sys_content = getattr(template, 'base_system_message',
                          getattr(template, 'system_message', ''))
    lines = sys_content.strip().split('\n') if sys_content else []

    # Only modifications:
    # 1. Identity line: "You are {instance_name}." (line 1132)
    # 2. Session Metadata with supervisor name (lines 1135-1141)
    # NO available resources injection
    # NO argument reuse instructions

    sys_msg = Message(role=SYSTEM, content="\n".join(lines))
```

Key observation: Sub-agents get `template.base_system_message` which was loaded from the agent's `*_soul.md` file via `create_agent_from_soul()`. This means **the soul IS loaded for sub-agents** — they get the full personality prompt from their soul file, just with identity and supervisor metadata prepended.

### Divergence Point 3: Agent Instance Properties

| Property | Main Agent | Sub-Agent |
|----------|-----------|-----------|
| `parent_instance` | `None` | `caller` (the agent that called them) |
| `agent_class` | `'Orchestrator'` | From template (e.g., 'coder', 'researcher') |
| `max_turns` | Set via UI config | Propagated from caller's template config (line 1237) |
| `disabled_tools` | Set via UI config | Propagated from caller (lines 1252-1270) |

---

## 3. Detailed Comparison: Original vs. Unified Sub-Agent Path

### Original `_stream_sub_agent_call()` (agent_orchestrator.py line 1796):

```python
# Key features of the original sub-agent path:
1. Built system message with supervisor, working_dir, log_path, extra_paths
2. Created conversation: [system_message] for new instances
3. Appended user task message
4. Monkey-patched agent._call_llm for compression injection hooks
5. Ran via `agent.run()` (the old OrchestratorAgent loop)
6. Managed sub_agent_state dict for WebUI visibility
7. Handled active_stack tracking
```

### Unified `_create_and_run_agent()` (execution_engine.py line 1092):

```python
# Key features of the unified sub-agent path:
1. Builds system message from template's base_system_message + identity + metadata
2. Creates AgentInstance with parent_instance=caller
3. Multimodal image propagation from caller's conversation (lines 1151-1198)
4. Runs via self.run(inst) → ExecutionEngine.run() — unified loop
5. Manages instance_state for WebUI visibility
6. Handles active_stack tracking
7. Propagates settings (max_turns, disabled_tools) from caller
```

**The unified path is strictly SUPERIOR** to the original:
- Uses the proper `ExecutionEngine` loop instead of monkey-patching
- Has better multimodal image propagation
- More consistent state management via `pool.instances`
- Cleaner separation of concerns (engine handles execution, factory handles creation)

---

## 4. Assessment: Is the Sub-Agent Path Needed?

### Answer: YES — It is needed and NOT redundant

The sub-agent path serves essential functions that CANNOT be handled by the main agent path:

1. **Instance Creation**: The main agent doesn't need instance creation (it's pre-created). Sub-agents must be dynamically created with proper parent tracking, class assignment, and conversation initialization.

2. **System Message Tailoring**: Each sub-agent needs its system message to reflect:
   - Its own identity (`"You are {instance_name}."`)
   - Its supervisor (the caller agent)
   - Its agent_class-specific capabilities (from the template's soul.md)

3. **Context Inheritance**: Sub-agents need multimodal content propagation from their caller's conversation.

4. **Settings Propagation**: Settings like `max_turns` and `disabled_tools` must flow from supervisor to sub-agent.

### What CAN Be Unified

The **execution loop** is already unified — both main and sub-agents use `ExecutionEngine.run()`. The divergence is only in:
- How the instance is created
- How the initial system message is constructed
- Whether extra metadata (available resources) is injected into the system message

---

## 5. Potential Soul Loading Issue

### The Asymmetry

| Aspect | Main Agent | Sub-Agent |
|--------|-----------|-----------|
| System prompt source | soul.md loaded at startup, passed as `system_message_content` | Template's `base_system_message` from agent_class lookup |
| Dynamic metadata injection | Via `_setup_turn()` (lines 143-231) | At creation time in `_create_and_run_agent()` (lines 1130-1143) |
| Available resources block | **YES** — injected into conv[0] for root agents | **NO** — not injected for sub-agents |
| Argument reuse instructions | **YES** — appended to system message | **NO** — not included |

### Could This Cause Current Soul Loading Problems?

**Possibly.** If the issue is that sub-agents are not receiving their correct soul.md content, the problem would be in `_create_and_run_agent()` lines 1126-1127:

```python
sys_content = getattr(template, 'base_system_message',
                      getattr(template, 'system_message', ''))
```

This relies on `template.base_system_message` being correctly set when the agent template was loaded. If the template was loaded with an incorrect or missing soul file, ALL sub-agents of that class would inherit the wrong system message.

**Check points:**
1. Verify `pool.templates[agent_class].base_system_message` contains the correct soul content
2. Compare with how the main agent's `system_message_content` is extracted from history/soul
3. Ensure no stale template caching is serving outdated system messages

---

## 6. Recommendations

### Short-Term (No Code Changes)
1. Verify that `pool.templates[agent_class].base_system_message` is correctly set for each agent class
2. Check if the main agent's soul loading path differs in how it loads/validates soul.md files
3. Compare the actual content of system messages between a main agent and a sub-agent at runtime

### Medium-Term (If Unification Makes Sense)
1. Consider moving the system message construction logic from `_create_and_run_agent()` to a shared method that both paths can use
2. Add a `system_message_injector` phase in `ExecutionEngine._setup_turn()` that handles metadata injection for ALL agents, not just root agents
3. This would make the divergence explicit and testable

### Long-Term
1. The current architecture (one execution loop, different instance creation) is sound — don't force unification where it doesn't add value
2. Focus on ensuring consistent system message loading between main and sub-agent paths if that's the actual bug

---

## 7. Key File Locations

| File | Relevant Lines | Purpose |
|------|---------------|---------|
| `execution_engine.py` | 49-118 | Unified `run()` loop for ALL agents |
| `execution_engine.py` | 124-239 | `_setup_turn()` — system message injection (lines 141-231) |
| `execution_engine.py` | 1092-1351 | `_create_and_run_agent()` — sub-agent creation |
| `execution_engine.py` | 754-819 | `_handle_call_agent()` — tool dispatcher for sub-agents |
| `api_integration.py` | 32-94 | `create_main_agent_instance()` — main agent creation |
| `run_agent_unified.py` | 37-230 | `run_agent_thread_unified()` — main agent entry point |
| `soul_loader.py` | 146-203 | `create_agent_from_soul()` — creates agents with system prompts |
| `agent_factory.py` | 169-229 | `load_agent()` — loads templates from soul.md files |

---

## 8. Conclusion

The sub-agent path is **needed and correctly separated** from the main agent path. The divergence in system message injection (lines 143-231 of execution_engine.py) is intentional and architecturally sound — root agents need to know about available resources, while sub-agents only need their own identity and supervisor info.

However, if there's a soul loading bug, it likely resides in:
1. **Template loading**: `agent_factory.load_agent()` not correctly setting `base_system_message`
2. **System message content**: The template's `base_system_message` may be stale or incorrect
3. **No issue with the execution path itself** — both paths use the same `ExecutionEngine.run()` loop

The structural separation is correct; any bug would be in data (system message content), not in the code path logic.