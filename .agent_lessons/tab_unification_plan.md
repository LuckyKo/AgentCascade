# Plan of Action: Unification of Chat and Sub-Agent Systems

**Date:** May 20, 2026  
**Status:** SUPERSEDED — See [tab_unification_plan_v4.md](tab_unification_plan_v4.md) for the current approved plan (v4.0, reviewed and approved May 23, 2026)  
**Project:** AgentCascade Refactor - Tab & Logic Unification (TODO item #20)

---

**Note:** This is the original v1.0 plan. It has been superseded by v4.0 after three rounds of review that identified significant gaps in actionability, concurrency strategy, and rollback planning. The approved plan with full implementation details is at [tab_unification_plan_v4.md](tab_unification_plan_v4.md). Supporting documents:
- [Analysis Report](tab_unification_analysis_report.md) — Current state assessment of main vs unified branches
- [Parallel Merge Protocol](parallel_merge_protocol.md) — Detailed concurrency pseudocode for Phase 3b

---

## Branch Setup

- **Git branch:** `tab-unification` — all work for this project lives on this branch.
- **Frontend sandbox:** `web_ui_unified/` — a copy of `web_ui/` used for frontend development. The original `web_ui/` remains untouched as the live version until all phases are complete and tested. See [`web_ui_unified/README.md`](web_ui_unified/README.md) for details on switching between versions.
- **Merge strategy:** Once validated, `web_ui_unified/` replaces `web_ui/` via a merge commit before merging to main.

---

## 1. Executive Summary
The goal is to eliminate the dual-track architecture currently separating the "orchestrator" (main chat) from "sub-agents." Currently, these two paths diverge at every layer: UI rendering uses different CSS/JS functions, backend serialization uses separate data structures, and the message loop treats orchestrator calls as special cases while sub-agents are treated as children.

This unification will treat the orchestrator as simply another agent instance within a unified `AgentPool` framework. By merging these paths, we reduce code duplication by approximately 40%, eliminate "special case" bugs in the message loop, and create a consistent state management pattern across all agents.

**Core Principle:** The orchestrator is not the *parent* of sub-agents; it is the *root instance* of a recursive call tree. Every agent (including the root) should follow the same lifecycle: `init` → `process` → `serialize`.

---

## 2. Phase 1: Frontend Unification (Low Risk, High Visibility)
The UI currently maintains two parallel rendering paths. We will merge these into a single recursive rendering system.

### 2.1 JS Logic Consolidation (`web_ui/app.js`)
- [ ] **Merge Rendering Functions:** Replace `renderMessages()` and `renderSubAgentPanel()` with a single `renderAgentConversation(instanceName, messages)` function.
- [ ] **Unify Message Element Creation:** Create a single `createMessageEl(msg, isRoot)` helper. The `isRoot` flag will conditionally apply the `.msg-` vs `.sub-msg-` prefix if legacy CSS support is required during transition.
- [ ] **Shared State Management:** Move all message-related state (scroll position, active tab, pending requests) into a single `UIState` object.
- [ ] **Recursive Rendering:** Implement a recursive call to `renderAgentConversation` for nested sub-agent messages, allowing the UI to naturally represent the call tree depth.

### 2.2 CSS Consolidation (`web_ui/styles.css`)
- [ ] **Class Unification:** Merge `.msg-container`, `.msg-user`, `.msg-bot` with their `.sub-msg-` counterparts.
- [ ] **Visual Hierarchy:** Use indentation or border-left indicators for nested sub-agent messages rather than entirely different class sets.
- [ ] **Remove Redundancy:** Delete duplicate styles for avatars, timestamps, and action buttons.

### 2.3 HTML Structural Changes (`web_ui/index.html`)
- [ ] **Dynamic Container:** Replace the static `#chat-container` and dynamic sub-agent panels with a single container that dynamically renders based on the `active_stack`.
- [ ] **Instance-Based Tabs:** Update tab logic to treat the orchestrator as `instance: root` and sub-agents as `instance: name`.

**Risk:** High visibility of changes. Any regression will be immediately apparent to users.  
**Mitigation:** Implement a "legacy mode" toggle in the UI to switch back to dual-path rendering during testing.

---

## 3. Phase 2: Backend Data Serialization Unification (`api_server.py`)
Currently, orchestrator state is global (`session['history']`) while sub-agents are siloed. We will move all agent state into a unified structure.

### 3.1 State Structure Migration
- [ ] **Unified Store:** Move `session['history']` into `sub_agent_state['root']['messages']`.
- [ ] **Refactor `build_state()`:** Rewrite to iterate through all instances in `sub_agent_state` rather than having a special case for the root.
- [ ] **Refactor `get_sub_agent_state()`:** Make it generic: `get_agent_state(instance_name)`.

### 3.2 Token Cache Unification
- [ ] **Consolidate Caching:** Replace `_cached_hist_stats` and `_sa_stats_{name}` with a single `AgentTokenCache` class that tracks usage per instance ID.
- [ ] **Unified Cleanup:** Implement a single cleanup routine that purges caches based on a global TTL or when an instance is deleted.

### 3.3 API Endpoint Updates
- [ ] Update all endpoints that currently distinguish between "main chat" and "sub-agent" requests to use a common `instance_id` parameter.

**Risk:** Breaking changes to the API contract could disrupt existing frontend logic.  
**Mitigation:** Maintain backward compatibility for one release cycle by having the old endpoints proxy to the new unified logic.

---

## 4. Phase 3: Message Loop & Instance Lifecycle Unification (High Risk)
This is the core architectural shift. The orchestrator must become a first-class citizen of the `AgentPool`.

### 4.1 Agent Pool Integration (`agent_pool.py`)
- [ ] **Orchestrator Registration:** Modify `AgentPool` to initialize the orchestrator as a standard instance upon startup, rather than having it exist outside the pool.
- [ ] **Unified Lifecycle:** Ensure `orchestrator` instance follows the same `init` → `process` → `destroy` lifecycle as sub-agents.

### 4.2 Message Loop Refactoring (`agent_orchestrator.py`)
- [ ] **Remove Special Interception:** Eliminate the `STREAMING_TOOLS` special case for `call_agent`. Instead, treat `call_agent` as a standard tool that triggers a recursive call to the same loop logic.
- [ ] **Unify Recovery Logic:** Move the sub-agent retry/compression monkey-patches into the base `Agent` class or a shared mixin. The orchestrator should use the same recovery paths.
- [ ] **Standardize Call Tree:** Ensure all agent calls (root → child → grandchild) use the same `instance_id` tracking and state propagation mechanism.

### 4.3 Logic De-duplication
- [ ] Remove redundant "orchestrator-only" logic in `api_server.py` that handles session history separately from sub-agent history.
- [ ] Centralize all "continue/stop" signals into a single event bus that all agents listen to.

**Risk:** This modifies the fundamental execution flow. A bug here could cause infinite loops or state corruption across all agents.  
**Mitigation:** Implement exhaustive unit tests for the recursive call chain before deploying to production. Use a "dry run" mode where changes are logged but not executed.

---

## 5. Testing Strategy
Testing must be performed in layers, mirroring the phase approach:

1. **Unit Tests (Post-Phase 1):** Verify `renderAgentConversation` correctly handles various nesting depths and message types.
2. **Integration Tests (Post-Phase 2):** Ensure `get_agent_state('root')` returns the same data as the old `build_state()`.
3. **End-to-End Tests (Post-Phase 3):** 
   - Trigger a chain of sub-agent calls (Root → A → B → C) and verify state is preserved across all levels.
   - Test "edit/delete" on the root instance vs a sub-agent instance to ensure identical behavior.
   - Verify token counting and cache eviction works uniformly across the call tree.

---

## 6. Rollback Plan
Given the depth of changes, a phased rollback is necessary:

1. **Phase 1 Rollback:** Revert `app.js` and `styles.css` to previous versions. The backend remains unchanged, so no data loss occurs.
2. **Phase 2 Rollback:** Restore `api_server.py` state handling logic. This requires a data migration script to move unified state back into the split structure.
3. **Phase 3 Rollback:** Re-introduce the `STREAMING_TOOLS` interception and separate `AgentPool` logic. This is the most complex rollback and should only be done if critical system failures occur.

**Emergency Kill Switch:** Implement a feature flag `USE_UNIFIED_ARCHITECTURE = False` that can be toggled via environment variable to revert to legacy behavior without code redeployment.

---

## 7. Summary of Expected Outcomes
- **Code Reduction:** Estimated 30-40% reduction in redundant JS and Python logic.
- **Performance:** Improved state loading times by eliminating duplicate serialization paths.
- **Maintainability:** A single point of failure for message rendering and state management, rather than two parallel systems.
- **Extensibility:** New agent types can be added without needing to update separate orchestrator/sub-agent logic paths.

---

## Files Involved

| File | Size | Role in Refactor |
|------|------|-----------------|
| `web_ui/app.js` | ~132KB | Primary JS — merge rendering functions |
| `web_ui/styles.css` | ~47KB | CSS — consolidate .msg-* and .sub-msg-* systems |
| `web_ui/index.html` | ~33KB | HTML — remove static chat panel, use dynamic injection |
| `api_server.py` | ~134KB | Backend — unify state serialization |
| `agent_orchestrator.py` | ~107KB | Message loop — remove special-case logic |
| `agent_pool.py` | ~46KB | Lifecycle — register orchestrator as first-class instance |