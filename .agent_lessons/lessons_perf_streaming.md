# Performance Lessons: Streaming GPU/CPU Usage

## Key Findings (2026-05-24)

### Root Cause of High GPU During Tool Execution
1. `updateControls()` called on every ~150ms stream_update, destroying/recreating DOM with innerHTML
2. No Page Visibility API handling — renders even when tab is hidden
3. Server sends events during tool execution because `has_tool_event` triggers immediate send

### Fixes Implemented (2026-05-24)

#### Fix 1: Throttle updateControls() + Change Detection (Critical)
- Added `lastControlsUpdate` throttle (~1Hz) in stream_update handler
- Added `_lastIsGenerating` change detection inside updateControls() — innerHTML/classList only fires on state transition
- New throttle timestamps added to genStats init and resetGenStats

#### Fix 2: Page Visibility API (Critical)
- Added `documentHidden` global + visibilitychange listener
- stream_update handler returns early when tab is hidden, still updating scalar stats and controls minimally

#### Fix 3: Skip renderMessages() During Tool Execution (High)
- After content key check passes, detect tool-only execution (function_call with no content/reasoning_content)
- Skip full re-render when only function_call arguments grew

#### Fix 4: Throttle updateTelemetryPanel() (Medium)
- Added `lastTelemetryUpdate` throttle (~2s) — telemetry is session-level data that doesn't need frequent updates
- Stores pending telemetry in state for future use

#### Fix 5: Change Detection in renderSubAgents() (Medium)
- Added `data-is-active` tracking on tab buttons
- Only update icon innerHTML when active state actually changed

#### Fix 6: Cache Activity Preview (Medium)
- Added `_lastActivityPreviewKey` and `_lastActivityPreview` caching in updateMainActivityBar()
- Avoids calling getActivityPreview() repeatedly during tool execution when only args grow

#### Fix 7: Remove box-shadow from CSS Animation (Low)
- Removed `box-shadow: 0 0 6px var(--accent)` from .sub-tab-pulse (triggers GPU filter compositing)
- Added `will-change: opacity, transform` hint for browser optimization
- Moved glow effect to parent via `.main-tab.agent-active .tab-icon-container { filter: drop-shadow(...) }`

### What Works Well
- Content key deduplication (line 1162) prevents redundant renders when nothing changed
- Throttled rendering at ~300ms for chat, ~150ms for sub-agents with new content
- requestAnimationFrame batching for scroll operations

### Patterns to Follow
- Always throttle DOM updates during streaming — users can't perceive >10fps text updates
- Use `innerHTML` only when content actually changed (compare strings first)
- Check tab visibility before expensive render passes
- Buffer telemetry data and update at ~2Hz instead of on every receipt

### Backend Behavior
- Server sends stream_update every 150ms OR immediately on tool events
- During tool execution (write_file etc), `has_tool_event` is true → frequent updates
- Sub-agent state changes are already change-detected before computing full state