# Tool Assignment Research Findings

## Executive Summary

This report documents the current tool assignment flow in AgentCascade, identifies gaps preventing real-time updates, and provides recommendations for refactoring to enable immediate application of UI-configured disabled tools.

---

## 1. Current Data Flow Diagram

```
UI (Frontend)
    │
    ├───► Sends 'update_config' WebSocket message with generate_cfg.disabled_tools
    │       (per-agent dict: {"agent_name": ["tool1", "tool2"], ...})
    │
    ▼
WsMessageHandler.handle_update_config() [ws_handlers.py:643-654]
    ├─ Stores data['generate_cfg'] in self.session['generate_cfg']
    ├─ Creates ConfigUpdateRouter and calls router.apply(ui_cfg)
    └─ Calls await self._broadcast() to notify UI
        │
        └──► Router applies registered handlers (none for 'disabled_tools')
            └──► Config stored in session only, NOT applied to instances

───────────────────────────────────────────────────────────────────────

When an agent turn begins (user message or API call):

ui_cfg = session.get('generate_cfg', {})  // from api_integration.py:692
    │
    ▼
_apply_ui_config(pool, instance_name, ui_cfg) [api_integration.py:904-905]
    ├─ Sanitizes numeric values and filters non-LLM keys
    ├─ Validates disabled_tools against TOOL_REGISTRY
    ├─ Creates instance._generate_cfg_override with disabled_tools included
    └─ Applies other settings (max_turns, auto_continue, etc.)

───────────────────────────────────────────────────────────────────────

During each LLM call:

_get_active_functions_from_template(template, instance) [execution_engine.py:69-132]
    ├─ Reads instance._generate_cfg_override (Layer 1 - highest priority)
    ├─ Reads template.llm.generate_cfg (Layer 2)
    ├─ Calls resolve_disabled_tools_for_agent() in utils/disabled_tools.py
    ├─ Merges with agent-class defaults (Security/Compressor defense-in-depth)
    └─ Filters template.function_map to return active tool schemas

───────────────────────────────────────────────────────────────────────

Sub-agent creation (via call_agent):

LifecycleManager.propagate_settings() [lifecycle_manager.py:456-583]
    ├─ Reads caller's _generate_cfg_override or template config
    ├─ Resolves caller's full disabled set via resolve_disabled_tools_for_agent()
    ├─ Extracts child-specific entries from caller's per-agent dict
    ├─ Merges parent and child disabled sets
    └─ Sets child instance._generate_cfg_override

───────────────────────────────────────────────────────────────────────

System prompt building:

_build_resources_block() [execution_engine.py:236-291]
    ├─ Calls _get_active_functions_from_template(template, instance)
    ├─ Lists enabled tools in system message
    └─ Includes available agent types if call_agent is enabled

```

---

## 2. All Touch Points Where Tool Assignment is Read/Updated

### A. Storage & Validation

1. **UI Layer** - Frontend stores per-agent assignments in `agentDisabledTools` (localStorage)
2. **WebSocket Handler** - `_validate_disabled_tools()` [ws_handlers.py:26-39]
   - Validates disabled_tools against TOOL_REGISTRY before storing in session
   - Handles both dict and list formats
3. **Session Storage** - `self.session['generate_cfg']` [ws_handlers.py:180]
   - Central per-connection store for UI configuration

### B. Configuration Application

4. **ConfigUpdateRouter** [config_handlers.py:204-232]
   - Dispatches config keys to registered handlers
   - **No handler exists for 'disabled_tools'** (key is not in LLM_CONFIG_KEYS)
5. **_apply_ui_config()** [api_integration.py:1475-1594]
   - **Primary entry point** for applying UI config to instances
   - Validates and stores disabled_tools in `instance._generate_cfg_override`
   - Called from:
     - `execute_agent_turn()` [api_integration.py:905]
     - `run_agent_thread_unified()` [run_agent_unified.py:111]

### c. Runtime Resolution (Reading Tool Assignments)

6. **_get_active_functions_from_template()** [execution_engine.py:69-132]
   - **Main function determining active tools per LLM call**
   - Reads `instance._generate_cfg_override` and `template.llm.generate_cfg`
   - Delegates to centralized resolver: `resolve_disabled_tools_for_agent()`

7. **_build_resources_block()** [execution_engine.py:236-291]
   - Builds system prompt section listing enabled tools
   - Calls `_get_active_functions_from_template()` to get active functions

### d. Sub-Agent Propagation

8. **propagate_settings()** [lifecycle_manager.py:456-583]
   - Propagates disabled_tools from parent to child instances
   - Resolves both parent's full set and child-specific entries
   - Merges using `merge_disabled_tools()` (union)

### e. Centralized Resolver

9. **resolve_disabled_tools_for_agent()** [utils/disabled_tools.py:68-129]
   - Single source of truth for disabled tool resolution
   - Layers: instance override → template config → agent-class defaults
   - Handles dict lookups by agent name, slugified name, or agent type

---

## 3. Gaps Identified (Where Real-Time Updates Are Missing)

### Gap #1: Configuration Update Path Does Not Apply Changes

**Location**: `WsMessageHandler.handle_update_config()` [ws_handlers.py:643-654]

```python
async def handle_update_config(self, data: dict) -> None:
    if 'generate_cfg' in data:
        self.session['generate_cfg'] = data['generate_cfg']  # Stores only
        ui_cfg = data['generate_cfg']
        router = ConfigUpdateRouter(self.agent_pool, self.agents)
        await router.apply(ui_cfg)  # No handler for disabled_tools
    await self._broadcast()
```

**Issue**: The config update path stores `disabled_tools` in session but never calls `_apply_ui_config()` to propagate changes to existing agent instances. This means:

- Existing instances retain stale `_generate_cfg_override` until next turn
- Real-time updates are not reflected immediately
- Users must trigger a new message/turn to see updated tool assignments

### Gap #2: Sub-Agent Inheritance Chain Does Not Reflect Live Config

**Location**: `LifecycleManager.propagate_settings()` [lifecycle_manager.py:456-583]

The propagation logic reads from the **caller's current override**, which may be stale if UI config changed after the parent was created. This creates a chain of outdated assignments for nested sub-agents.

**Issue**: If a user updates tool assignments mid-conversation, any newly spawned sub-agents will inherit the parent's old `_generate_cfg_override`, not the fresh session config.

### Gap #3: No Central Config Cache for Real-Time Access

**Location**: Multiple functions read from `instance._generate_cfg_override` directly

The current design requires reading from instance-specific overrides. There is no shared "current UI config" cache that all agents can query in real-time, making it difficult to ensure consistency across multiple concurrent sessions or rapid configuration changes.

### Gap #4: Missing Handler for disabled_tools in ConfigUpdateRouter

**Location**: `config_handlers.py` registers handlers only for specific keys (LLM_CONFIG_KEYS, mcpServers, etc.)

There is no dedicated handler for `disabled_tools`, so the config update router does nothing special with it beyond storing in session. This was likely intentional to avoid duplicate handling, but it contributes to Gap #1.

---

## 4. Recommendations for Refactoring Approach

### Option A: Apply UI Config on Update (Minimal Change)

**Approach**: Call `_apply_ui_config()` for all relevant instances immediately when `handle_update_config` is invoked.

```python
async def handle_update_config(self, data: dict) -> None:
    if 'generate_cfg' in data:
        self.session['generate_cfg'] = data['generate_cfg']
        ui_cfg = data['generate_cfg']
        
        router = ConfigUpdateRouter(self.agent_pool, self.agents)
        await router.apply(ui_cfg)
        
        # NEW: Apply UI config to all active instances for real-time updates
        for instance_name in self.agent_pool.instances:
            _apply_ui_config(self.agent_pool, instance_name, ui_cfg)
        
        await self._broadcast()
```

**Pros**:
- Simple, localized change
- Immediate reflection of UI changes
- No need to modify runtime tool resolution logic

**Cons**:
- May be expensive if many instances exist (though `_apply_ui_config` is lightweight for non-matching names)
- Doesn't address sub-agent inheritance chain issue directly (but parent override update will propagate on next sub-agent call)

### Option B: Real-Time Resolution in _get_active_functions_from_template

**Approach**: Modify `_get_active_functions_from_template()` to also check current session config alongside instance overrides.

```python
def _get_active_functions_from_template(template, instance=None) -> list:
    # ... existing code ...
    
    # NEW: Fetch latest UI config from a shared cache (need global or per-session storage)
    current_ui_config = get_current_session_config()  # Needs implementation
    
    # Merge with override and template
    merged_cfg = {}
    if instance_override:
        merged_cfg.update(instance_override)
    if template_cfg:
        merged_cfg.update(template_cfg)
    if current_ui_config:
        merged_cfg.update(current_ui_config)
    
    disabled = resolve_disabled_tools_for_agent(
        instance_override=merged_cfg,  # Now includes live UI config
        ...
    )
```

**Pros**:
- Most up-to-date configuration always used
- No need to push updates to instances
- Works across concurrent sessions if cache is properly scoped

**Cons**:
- Requires establishing a shared config store (global or session-scoped)
- Changes core resolution logic, potentially affecting performance
- May introduce race conditions without careful synchronization

### Option C: Hybrid Approach (Recommended)

Combine elements of both options:

1. **Store current UI config in agent_pool** (or a dedicated ConfigManager) with thread-safe access
2. **Modify _get_active_functions_from_template()** to merge instance override, template config, AND the live pool config
3. **Remove dependency on instance._generate_cfg_override for disabled_tools** when it conflicts with live config

This provides:
- Real-time updates without per-instance mutations
- Consistent configuration across all agents in the pool
- Backward compatibility with existing overrides for other settings

### Implementation Priority

1. **Fix Gap #1**: Ensure `_apply_ui_config` is called on config update OR implement real-time resolution (Option A or C)
2. **Update propagate_settings()** to read from live config rather than caller's stale override
3. **Add test coverage** for rapid config changes during active conversations
4. **Document behavior** in DESIGN_REWRITE.md and user-facing docs

---

## 5. Key Files Referenced

- `agent_cascade/ws_handlers.py` - WebSocket message handling, update_config handler
- `agent_cascade/api_integration.py` - `_apply_ui_config()` implementation
- `agent_cascade/execution_engine.py` - `_get_active_functions_from_template()`, `_build_resources_block()`
- `agent_cascade/config_handlers.py` - ConfigUpdateRouter and registered handlers
- `agent_cascade/lifecycle_manager.py` - `propagate_settings()` for sub-agent inheritance
- `agent_cascade/utils/disabled_tools.py` - Centralized resolver

---

## 6. Conclusion

The current tool assignment flow is well-architected for static configuration but lacks real-time update capability. The primary gap is that config updates are stored but not applied to running instances. Implementing Option C (hybrid approach) will provide the most robust solution, ensuring UI changes take effect immediately while maintaining clean separation of concerns and thread safety.