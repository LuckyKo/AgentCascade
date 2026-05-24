# Revised Plan of Action: Unification of Chat and Sub-Agent Systems

**Date:** May 23, 2026
**Status:** Draft / For Review
**Project:** AgentCascade Refactor - Tab & Logic Unification (TODO item #20)
**Version:** 4.0 (Final Revision based on Technical Review)

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
*   **Action:** 
    *   Use `diff -rq n:\work\WD\AgentCascade\operation_manager.py n:\work\WD\AgentCascade_unified\operation_manager.py` to identify all differences.
    *   Categorize differences: 
        *   **Critical:** File/grep/shell operations present in main but missing in unified (must be restored).
        *   **Incremental:** Optimizations, refactors, or bug fixes in main (merge if applicable).
        *   **Conflicts:** Overlapping changes requiring manual resolution.
    *   Produce a diff summary document listing all categories before merging.
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
*   **Action:** Create `config/unified.py` at the repository root level (same directory as `api_server.py`) with exactly these constants:
    ```python
    import os

    # Master toggle - gates all unified behavior. Defaults to False (legacy mode).
    USE_UNIFIED_ARCHITECTURE = os.environ.get('AC_USE_UNIFIED', '0') == '1'
    
    # Gates state read/write path.
    USE_UNIFIED_STATE = os.environ.get('AC_USE_UNIFIED_STATE', '0') == '1'
    
    # Gates message loop unification.
    USE_UNIFIED_LOOP = os.environ.get('AC_USE_UNIFIED_LOOP', '0') == '1'
    ```
*   **Implementation Note:** Access via `from config.unified import USE_UNIFIED_STATE` etc. This allows runtime toggling via environment variables without code redeployment.
*   **Verification:** Ensure all newly introduced logic in subsequent phases is wrapped in these flags.

---

## 3. Phase 1: Frontend Unification (Low Risk, High Visibility)

The UI currently maintains two parallel rendering paths. We will merge these into a single recursive rendering system. The `tab-unification` branch has ~60% of this work complete; we will finish and merge it first.

### 3.1 JS Logic Consolidation (`web_ui/app.js`)
*   **Task:** Replace `renderMessages()` and `renderSubAgentPanel()` with a single `renderAgentConversation(instanceName, messages, depth = 0)` function.
*   **Action:** 
    *   Implement recursive calls to `renderAgentConversation` for nested sub-agent messages, using the `depth` parameter to control indentation level.
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
    *   Introduce `USE_UNIFIED_STATE` feature flag (see Section 2.3).
    *   Implement wrapper function:
        ```python
        def get_session_history(session, instance_name='root', use_unified=None):
            # Priority check: explicit argument > global flag > default (False)
            effective_unified = use_unified if use_unified is not None else USE_UNIFIED_STATE
            
            if effective_unified:
                store = agent_pool.sub_agent_state.get(instance_name, {})
                msgs = store.get('messages', [])
                # Edge case: unified store not populated yet, fall back to legacy for root only
                if not msgs and instance_name == 'root':
                    msgs = list(session.get('history', []))
                return msgs
            else:
                if instance_name == 'root':
                    return list(session.get('history', []))
                else:
                    return agent_pool.get_sub_agent_state(instance_name) or []
        ```
    *   **Migration Priority Order:**
        1.  `build_state()` and streaming handlers (most frequently called/critical path).
        2.  State read/write operations (edit, retry, rollback functions).
        3.  Token counting and cache operations.
        4.  API endpoints and WebSocket handlers.
    *   **Grep Patterns for Migration:** Search for `'session\["history"\]'`, `'session\[.history.\]'`, and `'_cached_hist_stats'` to identify all references.
    *   **Testing Requirement:** Each migrated reference must be tested with both flag values to ensure parity.

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
*   **Implementation Note:** See the mapping table below for specific endpoint changes.

| Old Endpoint | New Unified Endpoint | Proxy Behavior |
| :--- | :--- | :--- |
| `/api/history` | `/api/instance/root/history` | Maps to `get_agent_state('root')` |
| `/api/sub_agent/history` | `/api/instance/{id}/history` | Maps to `get_agent_state(id)` |
| `/api/clear_history` | `/api/instance/root/clear` | Clears `sub_agent_state['root']` |
| `/api/sub_agent/clear` | `/api/instance/{id}/clear` | Clears `sub_agent_state[id]` |

---

## 5. Phase 3: Message Loop & Instance Lifecycle Unification (High Risk)

This is the core architectural shift. The orchestrator must become a first-class citizen of the `AgentPool`. This phase is split into three sub-phases to manage complexity. All changes in this phase are gated by the `USE_UNIFIED_LOOP` feature flag (see Section 2.3) — when False, the legacy `STREAMING_TOOLS` interception path remains active.

### 5.1 Sequential Path Unification
*   **Task:** Remove `STREAMING_TOOLS` special case and treat `call_agent` as a recursive loop invocation.
*   **Action:** 
    *   Modify `agent_orchestrator.py` to remove the hardcoded `STREAMING_TOOLS` set (line 410). When `USE_UNIFIED_LOOP = True`, this set is ignored.
    *   Update the main loop (line 1383) to handle `call_agent` by invoking the same logic used for any other tool, which then triggers a recursive call to the unified loop.
    *   Move sub-agent retry/compression monkey-patches into a shared mixin or base `Agent` class.

### 5.2 Parallel Path Unification
*   **Task:** Integrate `ParallelAgentManager` into the unified flow.
*   **Action:** 
    *   Ensure the unified loop can dispatch to `parallel_manager.submit_task` for agents marked as parallel-capable.
    *   Handle the return of parallel results through the same recursive chain as sequential results.
    *   **Concurrency Strategy (detailed in [parallel_merge_protocol.md](parallel_merge_protocol.md)):** 
        *   Leverage existing `copy.deepcopy` isolation in `ParallelAgentManager.submit_task()` for task-level isolation.
        *   Add `_state_lock` to `enqueue_message()` and `drain_queue()` in AgentPool to serialize concurrent message queue operations.
        *   Create new `_merge_result_into_state()` method on AgentPool that acquires `_state_lock` before writing to `sub_agent_state`. Replace all three existing direct writes (agent_orchestrator.py lines 1963, 2174, 2219) with calls to this method.
        *   Guard all `sub_agent_state` reads in the main loop with `_state_lock` + `copy.deepcopy` snapshot pattern (proposed `_collect_ui_state()` function).
        *   **Lock Ordering:** `_state_lock` → `_halt_lock` → `_activity_lock`. Never hold an inner lock while acquiring an outer one.
        *   **Parallel Write Pattern:** Each parallel agent gets its own isolated buffer for results; merge into the unified store under lock only after completion.
    *   **Verification:** See implementation checklist in [parallel_merge_protocol.md](parallel_merge_protocol.md) — 8 specific items covering all mutation points.

### 5.3 Fallback & Edge Case Handling
*   **Task:** Unify fallback logic and remove legacy markers.
*   **Action:** 
    *   Remove `STREAMING_TOOLS` entirely from the codebase.
    *   Ensure "continue with agent" (fallback when concurrency limits are hit) uses the same unified path as standard sequential calls.
    *   **Bug Fix - `__USE_PREV_ARG__`:** 
        *   **Current Issue:** Placeholder resolution is implemented in the non-streaming path only (lines 1433-1476 in `agent_orchestrator.py`), causing streaming tools to fail when using it.
        *   **Implementation:** Create a shared utility function:
            ```python
            def _resolve_prev_arg_placeholders(tool_args, agent_pool):
                """Resolves __USE_PREV_ARG__ placeholders from the last tool call."""
                if not tool_args or '__USE_PREV_ARG__' not in tool_args:
                    return tool_args
                
                # Use a lock for reading last_tool_args to ensure thread safety
                with agent_pool._state_lock: 
                    prev_args = agent_pool.last_tool_args
                
                resolved_args = tool_args.copy()
                for key, value in tool_args.items():
                    if value == '__USE_PREV_ARG__':
                        resolved_args[key] = prev_args.get(key)
                return resolved_args
            ```
        *   **Integration:** Call this function at the start of `_stream_sub_agent_call()` (for streaming paths) AND in the original non-streaming path.
        *   **Verification:** Test with a tool that uses `__USE_PREV_ARG__` in both streaming and non-streaming modes.

---

## 6. Phase 4: Cleanup & Finalization (Low Risk)

Once all phases are verified, remove the safety nets and legacy baggage.

### 6.1 Feature Flag Removal
*   **Task:** Remove `USE_UNIFIED_ARCHITECTURE`, `USE_UNIFIED_STATE`, `USE_UNIFIED_LOOP`, and any other temporary flags.
*   **Action:** Hard-code the unified paths as the primary behavior. Delete `config/unified.py`.

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

### 7.1 Backend State Rollback Script Specification
If a rollback is triggered, the following data transformation must be applied to restore the split state:
*   **Algorithm:**
    1.  Read current unified state store (`sub_agent_state`).
    2.  Extract `messages` from `sub_agent_state['root']` and write to `session['history']`.
    3.  Leave all other `sub_agent_state[instance_id]` entries unchanged (they already exist in the legacy format).
    4.  If unified store used a different schema, apply the reverse mapping of the migration logic.
*   **Pseudocode:**
    ```python
    def rollback_state(session, sub_agent_state):
        # Restore root history
        root_msgs = sub_agent_state.get('root', {}).get('messages', [])
        session['history'] = root_msgs
        # Remove root from sub_agent_state to avoid duplication
        sub_agent_state.pop('root', None)
    ```

### 7.2 General Rollback Sequence
1.  **Frontend Rollback:** Revert `app.js` and `styles.css` to previous versions. This is safe as it doesn't affect backend state.
2.  **Backend State Rollback:** Execute the script defined in 7.1.
3.  **Message Loop Rollback:** Re-introduce `STREAMING_TOOLS` interception and separate `AgentPool` logic. This is the most complex rollback and should only be done if critical system failures occur.

**Rollback Dependency Chain:** 
Frontend $\rightarrow$ Backend State $\rightarrow$ Message Loop. 
*Phase 3 cannot be rolled back without first rolling back Phase 2.*

**Emergency Kill Switch:** The `AC_USE_UNIFIED=0` environment variable (which sets `USE_UNIFIED_ARCHITECTURE = False`) can be toggled to revert to legacy behavior without code redeployment, **provided all paths in the codebase check this flag**. If any path bypasses the flag, a full rollback is required.

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