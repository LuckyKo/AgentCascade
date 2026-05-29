# Tab Unification — Lessons & Progress Tracker

## Phase 1 Status: Foundation Layer (Steps A1-A2) ✅ | Step B ✅ | Step C ✅ | Steps D-E ✅

### Steps A1-A2: CSS Helpers + Config Factories ✅
Added 7 new functions at the end of `app.js` (lines 3415-3509):

#### CSS Class Helpers (Step A2)
| Function | Purpose | Key Discovery |
|----------|---------|---------------|
| `msgClass(role, isRoot)` | Returns message wrapper classes | **CHANGED**: Now returns the SAME class for both root and sub-agent: `'message msg-{role}'`. Differentiation via `data-agent-type` attribute. |
| `headerClass(isRoot)` | Returns header class | **CHANGED**: Always returns `'msg-header'` |
| `contentClass(isRoot)` | Returns content class | **CHANGED**: Always returns `'msg-content'` |
| `nameLabelClass(isRoot)` | Returns name label class | **CHANGED**: Always returns `'msg-name'` |
| `roleName(role, isRoot, msg)` | Returns display name | Chat: "You"/"Assistant"/"Tool Result". Sub-agent: "📤 Task"/"Agent"/tool name. **Important**: accepts optional `msg` param to handle `function` role (needs `msg.name`) and custom assistant names. |

#### Config Factories (Step A1)
| Function | Purpose | Key Discovery |
|----------|---------|---------------|
| `getRootAgentConfig()` | Creates AgentRenderConfig for root chat agent | Uses global state (state.messages, state.generating, etc.) |
| `getSubAgentConfig(name)` | Creates AgentRenderConfig for a sub-agent | **Must use `??` nullish coalescing** — server fields like `total_tokens`, `max_tokens`, `is_halted` can be undefined if not yet sent. |

### Key Architectural Discoveries

1. **CSS class structure is now unified**: Both root and sub-agent messages use `.message msg-{role}`. The `data-agent-type="root"` / `data-agent-type="sub"` attribute differentiates them in CSS. This eliminates the dual-class system entirely.

2. **Sub-agent data fields are optional**: `agentData.total_tokens`, `agentData.max_tokens`, etc. may be undefined when the agent first appears. Always use `??` defaults.

3. **The existing code already has partial unification**: `startEdit()`, `deleteMessage()` accept an optional `instanceName` parameter and route to the correct message array. The shared helpers (renderThinkingBlock, renderToolCall, renderToolResult, setInnerHtmlWithState) are already unified.

4. **Reasoning content dedup bug fixed**: Merging `updateSubBubbleContent` into `updateBubbleContent` gave sub-agents the reasoning_content dedup logic that was missing.

5. **config.isGenerating enables sub-agent streaming**: The merged `updateBubbleContent` checks `config.isGenerating !== undefined ? config.isGenerating : state.generating`. This means sub-agent streaming (where `state.generating` is false but the sub-agent is active) works correctly.

6. **No separate 'stream' WebSocket message type exists**: All streaming flows through `stream_update`. There's no distinction between root and sub-agent streaming at the protocol level — just different state objects being updated.

7. **State tracking pattern mismatch**: Root uses global variables (`lastRenderedCount`, `isAutoScrollLocked`), while sub-agents use per-panel DOM attributes (`dataset.lastRenderedCount`, `dataset.contentKey`). Functionally equivalent but architecturally inconsistent.

8. **"Maine" is hardcoded in 4 places** (lines 43, 3279, 3316, 3390) as the default session/agent name. This needs to be parameterized.

### Data Flow Reference

```
state.messages                    → root chat messages
state.subAgents[name].messages    → sub-agent messages
state.generating                  → is root agent generating?
state.subAgents[name].active      → is sub-agent generating?
state.activeSubTab                → null/'chat'/'sub-{name}' (controls lazy rendering)
```

### Completed Steps (Phase 1B-E)
- **Step B**: Merge `updateBubbleContent` + `updateSubBubbleContent` → single function with config param ✅ COMPLETE
- **Step C**: CSS class unification — all helpers return same classes, differentiation via data-agent-type ✅ COMPLETE
- **Step D**: Incremental append in renderMessages uses renderAgentConversation ✅ COMPLETE
- **Step E**: Cleanup dead code (createSubMsgEl, updateSubBubbleContent removed) ✅ COMPLETE

### What Changed in This Phase
1. `msgClass()`, `headerClass()`, `contentClass()`, `nameLabelClass()` — all return same classes regardless of isRoot
2. `updateBubbleContent()` — now accepts `config.isGenerating` for sub-agent streaming; uses unified data attributes (prevContent/prevReasoning)
3. `renderMessages()` incremental append — now uses `renderAgentConversation('root', ...)` instead of direct `createMessageEl` calls
4. `renderAgentConversation()` — removed inline style application (margin-left, border-left); CSS `[data-agent-type="sub"]` handles this
5. `finishEdit()`/`cancelEdit()` — unified dispatch via single `updateBubbleContent(bubble, msgs[index], config)` call
6. Removed: `createSubMsgEl()`, `updateSubBubbleContent()` (deprecated)
7. CSS: all `.msg-X, .sub-msg-X` pairs collapsed to just `.msg-X`; sub-agent overrides in `[data-agent-type="sub"]`

## Phase 2 Status: Naming Convention Unification (Not Started)

### Remaining Differences Identified in Audit

| # | Difference | Root Chat | Sub-Agent | Risk |
|---|-----------|-----------|-----------|------|
| 1 | Visual hierarchy CSS | No left border | `border-left: 2px solid` accent | Low (CSS only) |
| 2 | Panel ID naming | `panelChat` | `panelSub-{name}` | Medium (DOM selector) |
| 3 | Tab ID naming | `'chat'` | `'sub-{name}'` | Medium (JS logic) |
| 4 | '_root' suffix | Not present | N/A (uses agent name directly) | Medium |
| 5 | Chat tab closable | No close button | Has × terminate button | Low (UI only) |
| 6 | Hardcoded "Maine" | 4 references | N/A | Low |
| 7 | State tracking vars | Global (`lastRenderedCount`) | Per-panel DOM (`dataset.*`) | Medium |

### Key Decision Points for Phase 2
- Should the root agent be named `{sessionName}_root` (e.g., `Maine_root`) to match sub-agent naming pattern?
- Should the chat tab be closable/terminatable like sub-agent tabs?
- Should visual hierarchy CSS be fully unified (no left border on any messages) or kept as a subtle distinction?