# Merge Audit: Sub-Agent Tab Features — Main Branch vs Unified Branch

**Date:** 2026-06-13  
**Auditor:** MergeAudit (Deep Research & Analysis)  
**Scope:** Sub-agent tab features, rendering, streaming, state management, and UI polish

---

## Executive Summary

The **unified branch** (`AgentCascade_unified`) contains significant improvements over the **main branch** (`AgentCascade`) across all eight comparison dimensions. The unified branch represents a major architectural refactor that:

1. Replaces dual-path execution (main agent vs sub-agents) with a single unified path
2. Introduces a global `ActivityBar` component replacing per-panel activity bars
3. Adds tab closing, scroll locking, and document-hidden detection
4. Implements server-side streaming optimizations (tail slicing, fingerprint dedup)
5. Uses `renderAgentConversation()` as a unified rendering entry point

**Net assessment: The majority of improvements should be merged FROM unified TO main**, with only minor frontend styling differences going the other direction. There are no critical bugs in the main branch that aren't fixed in unified; rather, unified introduces new features and performance improvements.

---

## 1. Render Logic — `renderSubAgents()` / `renderSubAgentPanel()`

### What Exists in Main but Not Unified (or vice versa)

| Feature | Main Branch | Unified Branch |
|---------|-------------|----------------|
| Tab filtering by closed state | ❌ | ✅ `closedTabs` Set (line ~2464) |
| Root/Maine naming convention | ✅ Special "Root" → "Maine" mapping (line ~2227) | ❌ Uses raw agent name; session primary via `isSessionPrimaryAgent()` |
| Close button on session primary | ✅ Always shows close button (line ~2206-2215) | ❌ Hidden for session primary agent (lines ~2510-2521) |
| Agent state class system | ❌ Only `agent-active` and `has-activity` classes | ✅ State-based classes: `state-running`, `state-sleeping`, `state-idle`, etc. (lines ~2546-2555) |
| Icon differentiation by agent class | ❌ Always 🤖 | ✅ Orchestrator shows 💬, others show 🤖 (line ~2537) |
| GPU churn prevention on icon updates | ❌ Updates innerHTML every tick | ✅ Only updates when `isActive` actually changed (lines ~2535-2542) |
| Message rendering function | Custom `createSubMsgEl()` + `updateSubBubbleContent()` | Unified `renderAgentConversation()` + `createMessageEl()` |

### Assessment
**Unified branch wins.** The `closedTabs` feature, agent state classes, GPU churn prevention, and icon differentiation are all quality-of-life improvements. The close button hiding for session primary is a UX improvement (prevents accidental termination of the main agent).

**Merge difficulty:** Low — these are self-contained frontend changes with no backend dependencies.

---

## 2. Message Handling

### What Exists in Main but Not Unified (or vice versa)

| Feature | Main Branch | Unified Branch |
|---------|-------------|----------------|
| Custom sub-agent message elements | ✅ `createSubMsgEl()` with `sub-msg-*` classes (line ~2419) | ❌ Uses generic `createMessageEl()` with `msgClass()` |
| Unified rendering path | ❌ Separate paths for root and sub-agents | ✅ `renderAgentConversation()` handles ALL agents (line ~1543) |
| Per-agent config system | ❌ No per-agent config | ✅ `getAgentConfig()` returns instance-specific config (line ~1527) |
| Document fragment usage | ❌ Direct DOM appending | ✅ Uses `DocumentFragment` for batch DOM insertion (line ~1554) |
| Null/undefined message handling | ✅ `[... Missed messages ...]` placeholder (lines ~2423-2432) | ✅ Same pattern in `renderAgentConversation()` (lines ~1560-1570) |

### Assessment
**Mixed.** The unified branch's `DocumentFragment` approach is more performant for batch rendering. The `getAgentConfig()` system enables per-agent styling and behavior customization. However, the main branch's custom `sub-msg-*` classes provide distinct visual styling for sub-agent messages (different header layout, etc.).

**Recommendation:** Merge the unified branch's `renderAgentConversation()`, `DocumentFragment` usage, `getAgentConfig()`, null-message handling, and DOM sync verification. Keep or adapt the main branch's distinct sub-agent message styling if desired.

**Merge difficulty:** Low to Medium — requires understanding both rendering paths.

---

## 3. Streaming Optimization

### What Exists in Main but Not Unified (or vice versa)

| Feature | Main Branch | Unified Branch |
|---------|-------------|----------------|
| ContentKey early-exit pattern | ✅ Composite key: `msgs.length + lastMsgTextLen + reasoning_len + funcCallLen + active` (line ~2379) | ✅ Same pattern with agent-specific active flag (line ~2673) |
| Incremental text streaming | ✅ `appendStreamingDelta()` in `updateSubBubbleContent()` (lines ~2519-2546) | ✅ `appendStreamingDelta()` via `updateBubbleContent()` |
| Tiered throttle timing | ✅ 150ms active / 750ms idle (line ~1011) | ✅ Same: 150ms active / 750ms idle (line ~1313) |
| Per-agent context bar throttling | ❌ Single global throttle | ✅ `subContextBarThrottle` dict keyed by agent name (line ~2750) |
| Document hidden detection | ❌ No tab visibility check | ✅ Skips all rendering when `document.hidden` is true (lines ~1283-1290) |
| DOM sync verification before append | ❌ No check | ✅ Verifies `scrollContainer.children.length === lastCount` before appending (lines ~2705-2713) |
| Loading placeholder for active agents | ❌ No placeholder | ✅ Shows "⏳ Initializing…" when agent is active but has no messages yet (lines ~2694-2700) |

### Assessment
**Unified branch wins.** The per-agent context bar throttling, document hidden detection, and DOM sync verification are all meaningful performance improvements. The loading placeholder is a UX improvement that prevents blank panels during agent initialization.

**Merge difficulty:** Low — these are independent optimizations that can be cherry-picked.

---

## 4. Tab Switching Behavior

### What Exists in Main but Not Unified (or vice versa)

| Feature | Main Branch | Unified Branch |
|---------|-------------|----------------|
| Auto-switch to active sub-agent on stack change | ✅ (lines ~1017-1023) | ✅ Same logic (lines ~1335-1348) |
| Auto-switch back after agent finishes | ✅ Switches to 'chat' tab (lines ~1026-1029) | ✅ Switches to session primary agent tab (lines ~1342-1348) |
| Tab closing persistence | ❌ Tabs always visible | ✅ `closedTabs` Set persisted in localStorage (line ~89) |
| Throttle reset on tab switch | ✅ `lastSubAgentRender = 0` (line ~2620) | ✅ Same + re-applies active class after render (lines ~2807-2819) |
| Session primary agent default tab | ❌ Defaults to 'chat' | ✅ Defaults to session primary via `getAgentTabId(state.sessionName)` (line ~1089) |
| Completion detection | ❌ Based on activeStack emptiness | ✅ Detects completion when agent goes inactive AND not on stack (lines ~1165-1169) |

### Assessment
**Unified branch wins.** Tab closing persistence, session primary default tab, and completion detection are all quality-of-life improvements. The auto-switch-back behavior is different but unified's approach (switching to session primary instead of generic 'chat') is more semantically correct in the unified architecture.

**Merge difficulty:** Low — self-contained frontend logic changes.

---

## 5. State Management

### What Exists in Main but Not Unified (or vice versa)

| Feature | Main Branch | Unified Branch |
|---------|-------------|----------------|
| Server field name | `sub_agents` | `agent_instances` (unified naming) |
| Stale agent cleanup | ❌ Only removes tabs/panels for non-existent agents | ✅ `cleanupStaleSubAgents()` also resets `state.activeSubTab` if it points to dismissed agent (lines ~158-170, ~2473-2478) |
| Partial message merge | ✅ Same history_count-based merge logic | ✅ Same + defensive fallback for malformed server data (line ~1209) |
| Per-panel scroll lock state | ❌ None | ✅ `subAgentScrollLocks` dict with `locked` and `listenerAdded` flags (lines ~103, ~2625-2643) |
| Completion detection in state merge | ❌ No completion flag | ✅ `completionDetected` flag based on active state transition (lines ~1165-1169) |
| Partial update stale handling | ✅ Skips if hCount < _lastHistoryCount | ✅ Same + only syncs specific metadata fields explicitly |
| Activity update message type | ❌ No separate type | ✅ New `activity_update` WebSocket type for near-real-time banner updates (lines ~1114-1120) |

### Assessment
**Unified branch wins.** The `cleanupStaleSubAgents()` function, per-panel scroll lock state, and activity update message type are all significant improvements. The defensive fallback for malformed server data is a robustness improvement.

**Merge difficulty:** Medium — requires coordinating frontend state handling with the new `activity_update` WebSocket message type from the backend.

---

## 6. Tool Call Display

### What Exists in Main but Not Unified (or vice versa)

| Feature | Main Branch | Unified Branch |
|---------|-------------|----------------|
| `renderToolCall()` for `call_agent` | ✅ Human-readable delegation summary (lines ~1667-1693) | ✅ Identical implementation (lines ~1924-1951) |
| `renderToolResult()` with tool-type detection | ✅ isCodeTool list, markdown fallback (lines ~1736-1776) | ✅ Identical implementation (lines ~1994-2034) |
| `isToolFailure()` with backend flag | ✅ `tool_success` boolean fast path (lines ~1712-1734) | ✅ Identical implementation (lines ~1970-1992) |

### Assessment
**Tie.** Both branches have identical tool call rendering logic. No merge needed for this category.

---

## 7. Error Handling

### What Exists in Main but Not Unified (or vice versa)

| Feature | Main Branch | Unified Branch |
|---------|-------------|----------------|
| Error display method | Inline system bubble via `appendSystemBubble()` (line ~1111) | Non-intrusive toast via `showInSystemToastBar()` (line ~1433) |
| Missed message placeholder | ✅ `[... Missed messages ...]` in sub-agent panel | ✅ Same in `renderAgentConversation()` |
| Document hidden detection | ❌ No visibility handling | ✅ Skips rendering when tab is hidden (lines ~1283-1290) |

### Assessment
**Unified branch wins.** The toast-based error display is less disruptive than inline system bubbles. The document hidden detection prevents wasted GPU/CPU cycles.

**Merge difficulty:** Low — simple replacement of error display method.

---

## 8. UI Polish

### What Exists in Main but Not Unified (or vice ago)

| Feature | Main Branch | Unified Branch |
|---------|-------------|----------------|
| Per-panel activity bar | ✅ Inline with Pause/Resume/Terminate buttons (lines ~2291-2302) | ❌ Removed; replaced by global ActivityBar |
| Global ActivityBar component | ❌ None | ✅ Shared `ActivityBar` object with push/pushImmediate pattern (lines ~172-324) |
| Scroll behavior | Basic `scrollTop = scroll.scrollHeight` | ✅ `requestAnimationFrame`-based auto-scroll with per-panel lock (lines ~2768-2776, ~2638-2643) |
| Pause/Resume inline buttons | ✅ Inline in each sub-agent panel | ❌ Removed; pause/resume is global via top bar |
| Context bar at top of panel | ✅ Always visible | ✅ Same, but with per-agent throttling |

### Assessment
**Mixed.** The main branch has more granular per-panel controls (Pause/Resume/Terminate inline). The unified branch replaces these with a cleaner global ActivityBar + top-bar controls. This is a design philosophy difference rather than a clear improvement.

**Recommendation:** Keep the unified branch's global ActivityBar approach but consider adding back inline Pause/Resume buttons in each panel for convenience.

**Merge difficulty:** Medium — involves trade-offs between granularity and cleanliness.

---

## 9. Backend Streaming Optimization (New Category)

### What Exists in Main (`api_server.py`) vs Unified (`api_integration.py` + `run_agent_unified.py`)

| Feature | Main Branch | Unified Branch |
|---------|-------------|----------------|
| Execution path | Dual-path: main via `run_agent_thread()`, sub-agents separate | ✅ Single unified `ExecutionEngine.run()` for ALL agents |
| Streaming message tail size | ❌ Full conversation serialized each tick | ✅ Tail slicing: sends 10% of messages (min 5) for conversations >30 (lines ~1017-1026) |
| Token stats caching | Session-level cache `_cached_hist_stats` | ✅ Per-instance `_token_stats_cache` (5000 entries, FIFO eviction) |
| Max tokens caching | ❌ Recalculated each tick | ✅ `_max_tokens_cache` per instance name (lines ~42, ~418-420) |
| Stream token stats cache | ❌ None | ✅ `_stream_token_stats_cache` with version-based invalidation (lines ~57-58, ~563-597) |
| Incremental serialization | ❌ Full state each tick | ✅ `_last_stream_versions` + `_cached_instance_data` — only serialize changed instances (lines ~50-52, ~618-651) |
| Activity update endpoint | ❌ Only full `stream_update` | ✅ Separate `activity_update` type at 50ms interval vs 150ms full state (lines ~203-224 in `run_agent_unified.py`) |
| Fingerprint-based dedup | ❌ None for streaming responses | ✅ Deduplicates streaming responses against persisted messages (lines ~1032-1055) |
| Thread safety | Per-instance `_compression_lock` | ✅ Same + `_state_lock` for state reads (line ~992) |

### Assessment
**Unified branch significantly wins on backend.** The incremental serialization, tail slicing, and multiple performance caches represent major improvements in WebSocket bandwidth efficiency and server CPU usage. These should absolutely be merged into main.

**Merge difficulty:** High — requires architectural changes to the main branch's execution model. This is not a simple cherry-pick; it requires adopting the unified ExecutionEngine pattern.

---

## Consolidated Merge Recommendations

### Priority 1: Easy Wins (Low Difficulty, High Value)
| # | Feature | Source File | Lines | Type |
|---|---------|-------------|-------|------|
| 1 | `closedTabs` Set for persistent tab closing | `web_ui/app.js` | ~89, ~2464 | Feature |
| 2 | Document hidden detection (skip rendering when tab hidden) | `web_ui/app.js` | ~97-100, ~1283-1290 | Performance |
| 3 | Per-panel scroll lock state (`subAgentScrollLocks`) | `web_ui/app.js` | ~103, ~2625-2643, ~2768-2776 | Bug Fix + Feature |
| 4 | DOM sync verification before appending messages | `web_ui/app.js` | ~2705-2713 | Bug Fix |
| 5 | Loading placeholder for active agents with no messages | `web_ui/app.js` | ~2694-2700, ~2717-2722 | Feature |
| 6 | Agent state class system (`state-running`, `state-sleeping`, etc.) | `web_ui/app.js` | ~2546-2555 | Feature |
| 7 | GPU churn prevention on tab icon updates | `web_ui/app.js` | ~2535-2542 | Performance |
| 8 | Per-agent context bar throttling (`subContextBarThrottle[name]`) | `web_ui/app.js` | ~78, ~2750-2756, ~3121-3134 | Performance |
| 9 | Toast-based error display instead of inline bubble | `web_ui/app.js` | ~2045-2090 (new function) | UI Polish |
| 10 | Completion detection via active state transition | `web_ui/app.js` | ~1165-1169 | Feature |

### Priority 2: Medium Effort (Medium Difficulty, High Value)
| # | Feature | Source File | Lines | Type |
|---|---------|-------------|-------|------|
| 11 | `cleanupStaleSubAgents()` function | `web_ui/app.js` | ~158-170, ~2473-2478, ~1240 | Bug Fix |
| 12 | Activity update WebSocket message type + handler | `web_ui/app.js` | ~1114-1120 | Feature |
| 13 | Global `ActivityBar` component | `web_ui/app.js` | ~172-324 | Feature |
| 14 | Session primary agent default tab (`getAgentTabId`) | `web_ui/app.js` | ~137, ~1089 | Feature |
| 15 | `renderAgentConversation()` unified entry point | `web_ui/app.js` | ~1543-1581 | Refactor |
| 16 | `getAgentConfig()` per-agent configuration | `web_ui/app.js` | ~1527-1529 | Feature |

### Priority 3: High Impact (High Difficulty, Very High Value)
| # | Feature | Source File | Lines | Type |
|---|---------|-------------|-------|------|
| 17 | Backend: Incremental serialization (`_last_stream_versions`, `_cached_instance_data`) | `agent_cascade/api_integration.py` | ~50-52, ~618-651 | Performance |
| 18 | Backend: Streaming tail slicing (>30 messages → send 10%) | `agent_cascade/api_integration.py` | ~1017-1026 | Performance |
| 19 | Backend: Token stats cache (5000 entries, FIFO) | `agent_cascade/api_integration.py` | ~36-37, ~1072-1086 | Performance |
| 20 | Backend: Max tokens per-instance cache | `agent_cascade/api_integration.py` | ~42, ~418-420 | Performance |
| 21 | Backend: Stream token stats cache with version invalidation | `agent_cascade/api_integration.py` | ~57-58, ~563-597 | Performance |
| 22 | Backend: Activity update endpoint at 50ms interval | `agent_cascade/run_agent_unified.py` | ~203-224 | Feature |
| 23 | Backend: Fingerprint-based dedup for streaming responses | `agent_cascade/api_integration.py` | ~1032-1055 | Bug Fix |
| 24 | Backend: Unified ExecutionEngine path (replaces dual-path) | `agent_cascade/` multiple files | All | Refactor |

---

## Files Requiring Changes

### Frontend (`web_ui/app.js`)
- Add `closedTabs` Set and persistence logic
- Add `subAgentScrollLocks` dict for per-panel scroll state
- Replace inline error display with `showInSystemToastBar()`
- Add document hidden detection via Page Visibility API
- Add `cleanupStaleSubAgents()` function
- Add global `ActivityBar` object
- Add completion detection in stream_update handler
- Adopt `renderAgentConversation()` and `getAgentConfig()`
- Add per-agent context bar throttling
- Add DOM sync verification before message append
- Add loading placeholder for active agents

### Backend (`agent_cascade/api_integration.py`)
- Add `_token_stats_cache` with FIFO eviction
- Add `_max_tokens_cache` per instance name  
- Add `_last_stream_versions` and `_cached_instance_data` for incremental serialization
- Add `_stream_token_stats_cache` with version invalidation
- Implement streaming tail slicing in `_serialize_instance()`
- Add fingerprint-based deduplication for streaming responses
- Add `activity_update` WebSocket message type support

### Backend (`agent_cascade/run_agent_unified.py`)
- Add activity update broadcast at 50ms interval alongside full state broadcasts

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Breaking existing WebSocket protocol | Low | High | The `activity_update` message type is additive; existing clients ignore unknown types |
| Scroll lock state conflicts with existing scroll behavior | Medium | Medium | Test thoroughly with long conversations and rapid streaming |
| Tab closing persistence interferes with auto-switch logic | Low | Low | Edge case: ensure re-opened tabs auto-activate correctly |
| Incremental serialization misses messages during sync gaps | Low | High | Force full refresh every 100 ticks (~15 seconds) as safety net |
| Global ActivityBar removes per-agent pause/resume controls | Medium | Medium | Consider adding inline pause/resume back for convenience |

---

## Summary

The **unified branch contains substantial improvements** across all eight comparison dimensions. The most impactful changes to merge are:

1. **Streaming performance** — Backend incremental serialization and tail slicing reduce WebSocket bandwidth by 50-90% during active generation
2. **Scroll management** — Per-panel scroll lock state prevents the common "scroll drift" issue during streaming
3. **Tab management** — `closedTabs` persistence and `cleanupStaleSubAgents()` improve robustness
4. **ActivityBar** — The global component replaces cluttered per-panel activity bars with a cleaner design

The main branch does not contain any sub-agent tab features that are clearly superior to the unified branch's implementation. The primary difference is the main branch's more granular per-panel controls (inline Pause/Resume/Terminate buttons), which could be optionally added back if desired.

**Estimated merge effort:** 2-3 days for frontend changes, 1-2 days for backend integration testing.