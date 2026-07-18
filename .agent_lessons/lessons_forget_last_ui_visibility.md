# forget_last Tool UI Visibility - Lessons Learned

**Date:** 2026-06-16  
**Topic:** How tools are displayed in the Agent Cascade WebUI  
**Related Files:** `agent_factory.py`, `api_server.py`, `web_ui/app.js`, `forget_last_tool.py`

---

## Key Finding: Dynamic Tool Discovery

The Agent Cascade WebUI uses **dynamic tool discovery** rather than hardcoded lists. This means:

### ✅ Tools Appear Automatically If:

1. The tool is registered via `@register_tool('tool_name', ...)` decorator
2. The tool instance is added to `agent.function_map['tool_name']`
3. No additional UI configuration needed!

### Data Flow:

```
Tool Registration → Agent Integration → API Response → Frontend Rendering
     ↓                    ↓                  ↓                 ↓
@register_tool    function_map[key]   list(keys)       .map() render
```

---

## Architecture Details

### 1. Tool Registration Layer

**File:** `agent_cascade/tools/<tool_name>.py`

```python
from agent_cascade.tools import register_tool, BaseTool

@register_tool('tool_name', allow_overwrite=True)
class MyTool(BaseTool):
    """Tool description"""
    def execute(self, ...):
        # Implementation
        pass
```

**Purpose:** Registers the tool in `TOOL_REGISTRY` for discovery and instantiation.

---

### 2. Agent Integration Layer

**File:** `agent_factory.py`

```python
from agent_cascade.tools.custom.some_tool import SomeTool

def create_agent_from_soul(...):
    # ... agent creation code
    
    # Add tool to agent's function_map
    some_tool = SomeTool()
    some_tool.agent_pool = agent_pool
    some_tool.agent_name = agent_name
    agent.function_map['some_tool'] = some_tool
    
    return agent
```

**Purpose:** Instantiates tools and adds them to the agent's `function_map` dictionary.

**Key Point:** ALL keys in `function_map` are automatically sent to the frontend!

---

### 3. API Response Layer

**File:** `api_server.py` (line ~770)

```python
'agents': [
    {
        'name': getattr(a, 'name', f'Agent-{i}'),
        'index': i,
        'agent_type': getattr(a, 'agent_type', 'orchestrator').lower(),
        'description': getattr(a, 'description', ''),
        'tools': list(a.function_map.keys()) if hasattr(a, 'function_map') else [],
        'default_tools': getattr(a, 'default_tools', list(a.function_map.keys()) if ...)
    }
    for i, a in enumerate(agents)
]
```

**Purpose:** Serializes agent information including ALL tools to JSON for frontend consumption.

**Key Line:** `'tools': list(a.function_map.keys())` - This is the magic! It dynamically captures all registered tools.

---

### 4. Frontend Rendering Layer

**File:** `web_ui/app.js` (lines ~2678-2711)

```javascript
function renderToolsForSelectedAgent() {
  const agent = state.agents.find(a => a.index === idx);
  
  if (!agent || !agent.tools || agent.tools.length === 0) {
    settingToolsList.innerHTML = '<div>No tools available...</div>';
    return;
  }
  
  // Render ALL tools from the API response
  settingToolsList.innerHTML = agent.tools.map(toolName => `
    <label class="setting-field toggle-field">
      <span>${escapeHtml(toolName)}</span>
      <input type="checkbox" 
             class="tool-toggle" 
             data-agent="${escapeHtml(agent.name)}" 
             data-tool="${escapeHtml(toolName)}" 
             ${!disabled.includes(toolName) ? 'checked' : ''} />
    </label>
  `).join('');
  
  // Add event listeners for toggle functionality
  settingToolsList.querySelectorAll('.tool-toggle').forEach(chk => {
    chk.addEventListener('change', (e) => {
      // Handle tool enable/disable
    });
  });
}
```

**Purpose:** Dynamically renders checkboxes for ALL tools received from the API.

**Key Method:** `agent.tools.map(...)` - Iterates over every tool name and creates UI elements.

---

## Common Misconceptions

### ❌ Myth: "I need to update a hardcoded tool list in the UI"

**Fact:** The UI has NO hardcoded tool lists (except for optional tools in `ALL_BUILTIN_TOOLS`). Core tools appear automatically via dynamic discovery.

### ❌ Myth: "The frontend needs to know about each tool individually"

**Fact:** The frontend is tool-agnostic. It receives an array of tool names and renders them generically.

### ❌ Myth: "I need to update both backend AND frontend when adding a tool"

**Fact:** Only backend changes needed (registration + function_map). Frontend automatically picks it up!

---

## Optional Tools vs Core Tools

### Core Tools (Always Enabled)
- Added to `agent.function_map` in `agent_factory.py`
- Appear in UI with checkbox **checked by default**
- Examples: `read_file`, `write_file`, `forget_last`, `call_agent`

### Optional Tools (Disabled by Default)
- Listed in `ALL_BUILTIN_TOOLS` in `start_multi_agent.py`
- Available but shown as **unchecked** initially
- Examples: `image_gen`, `shell_cmd`, `code_interpreter`

**Note:** A tool can be both core AND in the optional list, but this is uncommon.

---

## Verification Checklist

When adding a new tool, verify:

- [ ] Tool class registered with `@register_tool('name', ...)`
- [ ] Tool imported in `agent_factory.py`
- [ ] Tool instantiated: `tool_instance = ToolClass()`
- [ ] Tool configured (if needed): `tool_instance.agent_pool = agent_pool`
- [ ] Tool added to function_map: `agent.function_map['name'] = tool_instance`
- [ ] (Optional) Add to `ALL_BUILTIN_TOOLS` if should be disabled by default

**No frontend changes required!** ✨

---

## Debugging Tool Visibility Issues

If a tool doesn't appear in the UI:

### 1. Check Registration

```bash
grep -r "@register_tool('tool_name'" agent_cascade/tools/
```

Should find the registration decorator.

### 2. Check Agent Integration

```bash
grep "agent.function_map\['tool_name'\]" agent_factory.py
```

Should find the function_map assignment.

### 3. Check API Response

Browser DevTools → Network tab → Find API response → Inspect `agents[0].tools` array.

The tool name should be in the list.

### 4. Check Frontend State

Browser DevTools → Console:

```javascript
state.agents[0].tools.includes('tool_name')
```

Should return `true`.

---

## Related Investigation

See `FORGET_LAST_UI_VERIFICATION.md` for a complete investigation of the `forget_last` tool visibility, including:

- Full verification script (`verify_forget_last_ui.py`)
- Detailed code analysis with line numbers
- Complete data flow diagram
- All 9 automated checks that passed

---

## Quick Reference

### Files to Modify When Adding Tools

**Backend Only:**
1. `agent_cascade/tools/<new_tool>.py` - Tool implementation + registration
2. `agent_factory.py` - Import and add to function_map
3. (Optional) `start_multi_agent.py` - Add to ALL_BUILTIN_TOOLS if optional

**Frontend:** No changes needed! 🎉

### Key Code Patterns

**Registration:**
```python
@register_tool('tool_name', allow_overwrite=True)
class MyTool(BaseTool):
    pass
```

**Integration:**
```python
from agent_cascade.tools.custom.my_tool import MyTool
# ...
agent.function_map['tool_name'] = MyTool()
```

**API Response (automatic):**
```python
'tools': list(a.function_map.keys())  # Includes 'tool_name' automatically
```

---

## Conclusion

The Agent Cascade tool system is designed for **extensibility without UI changes**. By following the registration pattern and adding tools to `function_map`, they automatically appear in the WebUI with full toggle functionality.

This architecture reduces coupling between backend and frontend, making it easier to add new capabilities without coordinated changes across layers.

**Key Takeaway:** Trust the dynamic discovery system. If it's in `function_map`, it's in the UI! ✅