# Revised Plan of Action: Unification of Chat and Sub-Agent Systems

**Date:** May 23, 2026
**Status:** Draft / For Review
**Project:** AgentCascade Refactor - Tab & Logic Unification (TODO item #20)
**Version:** 2.0 (Revised from v1.0 based on Analysis Report and Reviewer Feedback)

---

## 1. Executive Summary

The goal is to eliminate the dual-track architecture currently separating the "orchestrator" (main chat) from "sub-agents." Currently, these two paths diverge at every layer: UI rendering uses different CSS/JS functions, backend serialization uses separate data structures, and the message loop treats orchestrator calls as special cases while sub-agents are treated as children.

This unification will treat the orchestrator as simply another agent instance within a unified `AgentPool` framework. By merging these paths, we reduce code duplication by approximately 40%, eliminate "special case" bugs in the message loop, and create a consistent state management pattern across all agents.

**Core Principle:** The orchestrator is not the *parent* of sub-agents; it is the *root instance* of a recursive call tree. Every agent (including the root) should follow the same lifecycle: `init` → `process` → `serialize`.

**Critical Constraint:** This plan must be executed as a phased migration with feature flags to avoid breaking the 30+ commits of new functionality on the main branch since the unified branch forked. A "big bang" merge is not viable.

---

## 2. Phase 0: Reconciliation & Setup (Immediate)

Before any unification work begins, the divergence between the `main` branch and the `tab-unification` branch must be resolved to prevent catastrophic merge conflicts.

### 2.1 Operation Manager Reconciliation
*   **Task:** Compare `operation_manager.py` in main (91KB) vs unified (44KB).
*   **Action:** Identify and merge missing functionality from main into the unification branch. The ~47KB discrepancy represents significant logic (grep, file operations, shell commands) that must be preserved.
*   **Verification:** Run existing operation manager tests against the merged version to ensure no regressions.

### 2.2 Branch Synchronization & Conflict Resolution Plan
*   **Task:** Rebase `tab-unification` onto `main` to incorporate recent changes.
*   **Action:** 
    *   Address conflicts in `api_server.py` (specifically Security Advisor auto-launch logic, lines 2218-2295).
    *   Address conflicts in `agent_orchestrator.py` (ParallelAgentManager, `__USE_PREV_ARG__` implementation).
    *   Address conflicts in `api_router.py` (multi-endpoint failover logic).
*   **Risk:** High conflict density (est. 400+ lines of overlapping changes).
*   **Mitigation:** Use a dedicated merge session with the original authors of the divergent features if possible.

### 2.3 Feature Flag Infrastructure
*   **Task:** Implement a global configuration toggle for the unification process.
*   **Action:** Add `USE_UNIFIED_ARCHITECTURE = False` to environment variables/config. All subsequent changes will be wrapped in this flag, allowing instant rollback to legacy behavior.

---

## 3. Phase 1: Frontend Unification (Low Risk, High Visibility)

The UI currently maintains two parallel rendering paths. We will merge these into a single recursive rendering system. The `tab-unification` branch has ~60% of this work complete; we will finish and merge it first.

### 3.1 JS Logic Consolidation (`web_ui/app.js`)
*   **Task:** Replace `renderMessages()` and `renderSubAgentPanel()` with a single `renderAgentConversation(instanceName, messages)` function.
*   **Action:** 
    *   Implement recursive calls to `renderAgentConversation` for nested sub-agent messages, allowing the UI to naturally represent the call tree depth.
    *   Move all message-related state (scroll position, active tab, pending requests) into a single `UIState` object.
*   **Current Progress:** Unified branch already has `createMessageEl(msg, index, config)` and `updateBubbleContent()`. These must be fully integrated into the recursive loop.

### 3.2 CSS Unification (`web_ui/styles.css`)
*   **Task:** Merge `.msg-container`, `.msg-user`, `.msg-bot` with their `.sub-msg-` counterparts.
*   **Action:** 
    *   Use **data attributes** (e.g., `data-agent-type="root"` vs `data-agent-type="sub"`) on message elements to apply specific styles via CSS selectors, avoiding class proliferation.
    *   Implement indentation or border-left indicators for nested sub-agent messages to maintain visual hierarchy without separate class sets.
    *   Delete duplicate styles for avatars, timestamps, and action buttons.

### 3.3 HTML Structural Changes (`web_ui/index.html`)
*   **Task:** Replace static `#chat-container` and dynamic sub-agent panels with a single container.
*   **Action:** Create a dynamic container that renders based on the `active_stack`. Update tab logic to treat the orchestrator as `instance: root` and sub-agents as `instance: name`.

### 3.4 Testing & Validation
*   **Task:** Verify rendering across all agent types and nesting depths.
*   **Action:** Test with a chain of 3+ sub-agent calls (Root → A → B → C) to ensure recursive rendering handles depth correctly.
*   **Verification:** Ensure the "Security Advisor" auto-launch messages render correctly within the unified flow.

---

## 4. Phase 2: Backend State Unification (Medium Risk)

Currently, orchestrator state is global (`session['history']`) while sub-agents are siloed. We will move all agent state into a unified structure using a **dual-read mode** for safe migration.

### 4.1 Dual-Read Implementation
*   **Task:** Implement a transition period where the system can read from both old and new state stores.
*   **Action:** 
    *   Introduce `USE_UNIFIED_STATE` feature flag.
    *   Modify `build_state()` to check this flag: if true, read from unified store; if false, fallback to legacy `session['history']` for root and `sub_agent_state` for sub-agents.
    *   Update all 100+ references to `session['history']` to use a wrapper function that respects the flag.

### 4.2 State Structure Migration
*   **Task:** Move `session['history']` into `sub_agent_state['root']['messages']`.
*   **Action:** 
    *   Refactor `build_state()` to iterate through all instances in `sub_agent_state` rather than having a special case for the root.
    *   Refactor `get_sub_agent_state()` into a generic `get_agent_state(instance_name)`.
    *   **Critical:** The Security Advisor auto-launch logic (lines 2218-2295 in main) must be updated to create its sub-agent state entry within the unified structure.

### 4.3 Token Cache Unification
*   **Task:** Consolidate `_cached_hist_stats` and `_sa_stats_{name}` into a single `AgentTokenCache` class.
*   **Action:** 
    *   Create a class that tracks usage per instance ID.
    *   Implement a unified cleanup routine that purges caches based on a global TTL or when an instance is deleted.
    *   **Parallel Consideration:** Ensure cache invalidation for one agent does not affect others (crucial for ParallelAgentManager).

### 4.4 API Endpoint Updates
*   **Task:** Update all endpoints that currently distinguish between "main chat" and "sub-agent" requests.
*   **Action:** Use a common `instance_id` parameter. Maintain backward compatibility for one release cycle by having old endpoints proxy to the new unified logic.

---

## 5. Phase 3: Message Loop & Instance Lifecycle Unification (High Risk)

This is the core architectural shift. The orchestrator must become a first-class citizen of the `AgentPool`. This phase is split into three sub-phases to manage complexity.

### 5.1 Sequential Path Unification
*   **Task:** Remove `STREAMING_TOOLS` special case and treat `call_agent` as a recursive loop invocation.
*   **Action:** 
    *   Modify `agent_orchestrator.py` to remove the hardcoded `STREAMING_TOOLS` set (line 410).
    *   Update the main loop (line 1383) to handle `call_agent` by invoking the same logic used for any other tool, which then triggers a recursive call to the unified loop.
    *   Move sub-agent retry/compression monkey-patches into a shared mixin or base `Agent` class.

### 5.2 Parallel Path Unification
*   **Task:** Integrate `ParallelAgentManager` into the unified flow.
*   **Action:** 
    *   Ensure the unified loop can dispatch to `parallel_manager.submit_task` for agents marked as parallel-capable.
    *   Handle the return of parallel results through the same recursive chain as sequential results.
    *   **Concurrency Check:** Implement thread-safe state updates when multiple parallel agents write to the unified `sub_agent_state`.

### 5.3 Fallback & Edge Case Handling
*   **Task:** Unify fallback logic and remove legacy markers.
*   **Action:** 
    *   Remove `STREAMING_TOOLS` entirely from the codebase.
    *   Ensure "continue with agent" (fallback when concurrency limits are hit) uses the same unified path as standard sequential calls.
    *   Update `__USE_PREV_ARG__` replacement logic to work across all dispatch paths.

---

## 6. Phase 4: Cleanup & Finalization (Low Risk)

Once all phases are verified, remove the safety nets and legacy baggage.

### 6.1 Feature Flag Removal
*   **Task:** Remove `USE_UNIFIED_ARCHITECTURE`, `USE_UNIFIED_STATE`, and any other temporary flags.
*   **Action:** Hard-code the unified paths as the primary behavior.

### 6.2 Code & Asset Deletion
*   **Task:** Delete redundant logic and styles.
*   **Action:** 
    *   Remove `renderSubAgentPanel()` and all `.sub-msg-*` CSS classes.
    *   Delete `_cached_hist_stats` and `_sa_stats_{name}` from `api_server.py`.
    *   Clean up `agent_pool.py` by removing the "Maine" special case in the idle checker.

### 6.3 Final Validation
*   **Task:** End-to-end test of the unified system.
*   **Action:** Run a full suite of tests covering:
    *   Deeply nested agent calls (Root → A → B → C).
    *   Parallel execution with concurrency limits.
    *   Security Advisor auto-launch and subsequent state management.
    *   Token cache eviction across multiple instances.
    *   Grep spillover pre-computation in a unified context.

---

## 7. Rollback Plan

Given the depth of changes, a phased rollback is required:

1.  **Frontend Rollback:** Revert `app.js` and `styles.css` to previous versions. This is safe as it doesn't affect backend state.
2.  **Backend State Rollback:** Restore `api_server.py` state handling logic. This requires a data migration script to move unified state back into the split structure (`session['history']` vs `sub_agent_state`).
3.  **Message Loop Rollback:** Re-introduce `STREAMING_TOOLS` interception and separate `AgentPool` logic. This is the most complex rollback and should only be done if critical system failures occur.

**Emergency Kill Switch:** The `USE_UNIFIED_ARCHITECTURE = False` environment variable can be toggled to revert to legacy behavior without code redeployment, provided all phases are wrapped in this flag.

---

## 8. Summary of Expected Outcomes

*   **Code Reduction:** Estimated 30-40% reduction in redundant JS and Python logic.
*   **Performance:** Improved state loading times by eliminating duplicate serialization paths.
*   **Maintainability:** A single point of failure for message rendering and state management, rather than two parallel systems.
*   **Extensibility:** New agent types can be added without needing to update separate orchestrator/sub-agent logic paths.
*   **Consistency:** Unified token counting, cache eviction, and error handling across all agents.

---

## 9. Files Involved & Impact Analysis

| File | Size (Main) | Role in Refactor | Change Type | Risk |
| :--- | :--- | :--- | :--- | :--- |
| `operation_manager.py` | ~91KB | Logic reconciliation | Merge/Restore | High |
| `api_server.py` | ~159KB | State unification & dual-read | Heavy Edit | Medium |
| `agent_orchestrator.py` | ~126KB | Loop unification (Seq/Par/Fallback) | Heavy Edit | High |
| `agent_pool.py` | ~53KB | Orchestrator registration | Moderate Edit | Low |
| `api_router.py` | ~30KB | Endpoint updates & failover logic | Moderate Edit | Medium |
| `web_ui/app.js` | ~152KB | Recursive rendering merge | Heavy Edit | Medium |
| `web_ui/styles.css` | ~47KB | CSS consolidation via data-attrs | Moderate Edit | Low |
| `web_ui/index.html` | ~33KB | Dynamic container switch | Light Edit | Low |

---

## 10. Timeline Estimate (Revised)

*   **Phase 0 (Reconciliation):** 2-3 days (high conflict resolution effort).
*   **Phase 1 (Frontend):** 3-5 days (merging existing unified branch work + recursive implementation).
*   **Phase 2 (Backend State):** 4-6 days (dual-read implementation, 100+ reference updates, Security Advisor integration).
*   **Phase 3 (Message Loop):** 5-8 days (handling three dispatch paths, parallel logic, and fallback cases).
*   **Phase 4 (Cleanup):** 2-3 days.
*   **Total Estimated Effort:** **16-23 business days** (approximately 3-5 weeks).

*Note: This timeline reflects a ~2x increase over original estimates to account for the 30+ commit divergence and new complexity (ParallelAgentManager, Security Advisor, etc.) identified in the analysis report.*