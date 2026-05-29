# Unify Root Tab and Sub-Agent Tabs - COMPLETE ✅

## COMPLETED (All items done, reviewer PASS)

### State Changes
- [x] Root messages routed through subAgents[{sessionName}_root] instead of state.messages
- [x] Removed state.messages from state object entirely
- [x] Removed genStats fields lastChatRender, lastChatContentKey (per-panel now)

### WebSocket Handler Changes  
- [x] 'state'/'done' handler routes root through subAgents
- [x] 'stream_update' handler routes root through subAgents
- [x] Root panel cache invalidated on fresh generation start
- [x] Default active tab set to root on initial load

### Rendering Pipeline Unification
- [x] Removed renderMessages() and fullRender() — root rendered via renderSubAgents/renderSubAgentPanel
- [x] renderSubAgents includes root agent in the names list (only if initialized)
- [x] renderSubAgentPanel handles root: system msg filtering, active state from generating, global token counts
- [x] Removed duplicate/buggy active-class block
- [x] switchMainTab unified — no special case for 'chat'

### Helper Functions
- [x] getRootAgentName() → returns '{sessionName}_root'
- [x] isRootAgentName(name) → checks 'root', '_root' suffix, or exact root name
- [x] getRootTabId() → returns 'sub-{sessionName}_root'  
- [x] getRootPanelId() → returns 'panelSub-{sessionName}_root'
- [x] getRootAgentConfig → {isRoot:true, instanceName} — consistent with purpose

### Message Element Unification
- [x] createMessageEl: all agents use same config path (getSubAgentConfig)
- [x] Removed unused data-agent-type attribute
- [x] All agents get data-instance-name for accent color styling
- [x] isGenerating check uses subAgents[instanceName] for all agents

### Edit/Delete Unification
- [x] startEdit/finishEdit/cancelEdit/deleteMessage use subAgents path for all agents
- [x] Container selector always uses panelSub-{name} (no fallback)

### Controls and Stats
- [x] updateControls uses subAgents[rootName] instead of state.messages
- [x] sendMessage async injection routes to root via getRootTabId()
- [x] resetGenStats/updateGenStats use root from subAgents, exclude root from sub-agent loop
- [x] Root tab pulse indicator updated dynamically (no mainTabChat ref)

### CSS Unification
- [x] border-left and muted names apply to ALL agents via .message (not just [data-agent-type="sub"])
- [x] Root agent gets accent color via [data-instance-name$="_root"]
- [x] Generic fallback applies to all agents (.message .msg-header)

### HTML Changes
- [x] Static HTML for Chat tab removed from index.html
- [x] Only dynamic containers remain (mainTabBar, mainTabPanels)

### Cleanup
- [x] Removed isAutoScrollLocked global variable — per-panel locks used
- [x] Removed scrollToBottom() function — each panel handles its own scrolling
- [x] Updated appendSystemBubble to use root panel's scroll container
- [x] Removed lastRenderedCount/lastLastContent globals — per-panel dataset used
- [x] Terminating root agent no longer tries to switch to destroyed tab

## Reviewer Status: PASS ✅