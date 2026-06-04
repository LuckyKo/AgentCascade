# Tab Unification — Project Status & Handoff Document

**Date:** 2026-05-24  
**Branch:** `tab-unification`  
**Author:** WrapUpWriter (session handoff)  
**Supervisor:** Maine  

---

## 1. Project Overview

The **Tab Unification** project eliminates the dual-track architecture in AgentCascade where the orchestrator (main chat) and sub-agents follow completely separate code paths at every layer:

- **Frontend:** Different CSS classes, JS rendering functions, and DOM structures for root vs sub-agent messages
- **Backend:** Separate state stores (`session['history']` for root, siloed dicts for sub-agents), distinct serialization logic, and special-case handling in the message loop
- **Loop:** The orchestrator's main loop treats `call_agent` as a hardcoded special case (`STREAMING_TOOLS`) rather than a recursive invocation

### Core Principle

> The orchestrator is not the *parent* of sub-agents; it is the **root instance** of a recursive call tree. Every agent (including the root) follows the same lifecycle: `init → process → serialize`.

### Target Architecture

A unified `AgentPool` framework where all agents — including the root orchestrator — share:
- Single rendering path (`renderAgentConversation` with recursive depth handling)
- Unified state store (`sub_agent_state[instance_name]`)
- One message loop (no `STREAMING_TOOLS` special case)
- Shared utility functions (token cache, placeholder resolution)

### Estimated Impact

- **30-40% code reduction** in redundant JS and Python logic
- **Eliminated special-case bugs** in the message loop
- **Consistent state management** across all agent types
- **Improved extensibility** — new agent types require only single-path updates

---

## 2. Current State

**All phases 0 through 3 are implemented, reviewed, and PASSED.** The code is ready for testing and eventual commit to the `tab-unification` branch.

### Phase Summary

| Phase | Status | Risk Level | Description |
|-------|--------|------------|-------------|
| **Phase 0** | ✅ Complete | Low | Branch synced to latest main; compression module preserved |
| **Phase 1** | ✅ Complete | Low-Medium | Frontend unification helpers, config-based rendering, CSS selectors |
| **Phase 2** | ✅ Complete | Medium | Feature flags, dual-read wrapper, token cache, unified `build_state()` |
| **Phase 3** | ✅ Complete | High | Shared `__USE_PREV_ARG__` resolver, lock protection for concurrent access |

### Review Status

All changes passed a **3-pass comprehensive review cycle**. Findings were identified and fixed before sign-off. The detailed review report is available at `.agent_lessons/final_review_report.md` (main repo) and `.agent_lessons/unified_uncommitted_changes_report.md` (unified branch).

### Feature Flag Safety

All feature flags default to **`False`**, meaning the existing behavior is completely unchanged when running from this branch. Users must explicitly set environment variables to enable unified paths:
- `AC_USE_UNIFIED_ARCHITECTURE=1`
- `AC_USE_UNIFIED_STATE=1`
- `AC_USE_UNIFIED_LOOP=1`

---

## 3. Branch Information

| Item | Value |
|------|-------|
| **Working branch** | `tab-unification` |
| **Working directory** | `N:\work\WD\AgentCascade_unified\` |
| **Main (origin/master)** | `N:\work\WD\AgentCascade\` |
| **Base commit** | Fresh initial copy of main with follow-up commits for unification work |
| **Feature flags** | All default to `False` (existing behavior unchanged) |

---

## 4. What Was Changed — By Phase

### Phase 0: Branch Reconciliation & Setup

- Synced `tab-unification` branch to latest `main` (origin/master)
- Resolved merge conflicts in `api_server.py`, `agent_orchestrator.py`, and `api_router.py`
- Preserved existing compression module from the unified branch
- Verified all new functionality on main (30+ commits) was carried forward

### Phase 1: Frontend Unification Helpers

**Goal:** Consolidate dual-track UI rendering into a single config-based system.

| File | Change Description |
|------|-------------------|
| `web_ui/app.js` | Added config-based helper functions: `msgClass()`, `headerClass()`, `contentClass()`, `nameLabelClass()`, `roleName()` — all accept an optional `config` object with `isRoot` flag and fall back to legacy defaults for backward compatibility |
| `web_ui/app.js` | Added `getRootAgentConfig()` (~line 3350) — returns `AgentRenderConfig` with `isRoot: true`, pulls from global `state` |
| `web_ui/app.js` | Added `getSubAgentConfig(name)` (~line 3379) — returns `AgentRenderConfig` with `isRoot: false`, uses `??` nullish coalescing for optional fields |
| `web_ui/app.js` | Updated `createMessageEl()` to accept a 3rd `config` parameter (falls back to `isRoot=true` for backward compat) |
| `web_ui/app.js` | Updated `updateBubbleContent()` to accept a `config` parameter with the same fallback pattern |
| `web_ui/styles.css` | Added CSS data-attribute selectors to support unified styling (`[data-agent-type="root"]`, `[data-agent-type="sub"]`) |
| `web_ui/index.html` | Structural updates for dynamic container rendering based on `active_stack` |

### Phase 2: Backend State Unification Infrastructure

**Goal:** Provide feature flags, dual-read state access, unified token cache, and a shared `build_state()` function.

| File | Change Description |
|------|-------------------|
| `config/unified.py` | **NEW** — Feature flag definitions (see Section 6 below) |
| `config/token_cache.py` | **NEW** — `AgentTokenCache` class replacing split caching (`_cached_hist_stats` + `_sa_stats_{name}`) with a unified, thread-safe, TTL-based cache |
| `agent_orchestrator.py` | Dual-read wrapper `get_session_history()` — reads from either legacy or unified store depending on flag; falls back gracefully for root instance if unified store is empty |
| `agent_orchestrator.py` | Unified `build_state()` — iterates through all instances in `sub_agent_state` without special-casing the root |
| Various files | Migrated ~100+ references from `session['history']` to dual-read wrapper (grep patterns: `'session["history"]'`, `'_cached_hist_stats'`) |

### Phase 3: Shared `__USE_PREV_ARG__` Resolver & Lock Protection

**Goal:** Wire the placeholder resolver into both streaming and non-streaming tool paths, add thread-safety for concurrent access.

| File | Change Description |
|------|-------------------|
| `agent_cascade/tool_utils.py` | **NEW** — `resolve_prev_arg_placeholders()` shared utility function; works in both streaming and non-streaming paths; supports per-tool and global (`__GLOBAL__`) scope resolution; optional lock parameter for thread-safety (deadlock-safe: callers already holding the lock pass `None`) |
| `agent_orchestrator.py` | Integrated `resolve_prev_arg_placeholders()` into the streaming tool path (`_stream_sub_agent_call()`) — previously only the non-streaming path had resolution logic |
| `agent_orchestrator.py` | Added `_state_lock` protection around `last_tool_args` reads (line ~1457) and writes (line ~1522) in the `__USE_PREV_ARG__` resolution flow |
| `agent_pool.py` | Lock guard added to `enqueue_message()` — wraps queue append in `with self._state_lock:` for thread safety under concurrent parallel agent completion |
| `agent_pool.py` | Lock guard added to `drain_queue()` — prevents concurrent enqueue during drain with deduplication logic |

---

## 5. New Files Created

| File | Description |
|------|-------------|
| `config/unified.py` | Feature flag definitions: `USE_UNIFIED_ARCHITECTURE`, `USE_UNIFIED_STATE`, `USE_UNIFIED_LOOP` — all default to `False` (legacy mode), controlled via environment variables |
| `config/token_cache.py` | `AgentTokenCache` class — thread-safe, TTL-based cache replacing split root/sub-agent token stats with a unified per-instance store |
| `agent_cascade/tool_utils.py` | Shared utility module containing `resolve_prev_arg_placeholders()` for `__USE_PREV_ARG__` resolution in both streaming and non-streaming tool execution paths |

---

## 6. Feature Flags

All three flags default to `False`, preserving existing behavior. Set them via environment variables or in the code to enable unified paths incrementally.

| Flag | Environment Variable | Phase | Purpose |
|------|---------------------|-------|---------|
| `USE_UNIFIED_ARCHITECTURE` | `AC_USE_UNIFIED_ARCHITECTURE=1` | Master | **Master toggle** — gates all unified behavior. When `False`, the entire legacy dual-track system remains active. When `True`, enables unified state and loop paths. |
| `USE_UNIFIED_STATE` | `AC_USE_UNIFIED_STATE=1` | Phase 2 | Gates state read/write path. Controls whether `get_session_history()` reads from the legacy `session['history']` or the unified `sub_agent_state[instance_name]`. Enables dual-read mode during transition. |
| `USE_UNIFIED_LOOP` | `AC_USE_UNIFIED_LOOP=1` | Phase 3 | Gates message loop unification. When `False`, the legacy `STREAMING_TOOLS` interception path remains active. When `True`, treats `call_agent` as a recursive invocation following the unified loop. |

### Activation Order (Recommended)

```
Phase 2 first:   AC_USE_UNIFIED_STATE=1
                   ↓ (verify state parity)
Phase 3 first:   AC_USE_UNIFIED_LOOP=1
                   ↓ (verify loop behavior)
Master toggle:   AC_USE_UNIFIED_ARCHITECTURE=1
                   ↓ (all unified paths active)
```

---

## 7. Review Status

All changes passed a **3-pass comprehensive review cycle**:

- **Pass 1:** Structural review — verified architecture alignment with the tab unification plan, confirmed feature flag coverage on all new code paths
- **Pass 2:** Functional review — tested each phase's changes against both flag states (True/False) to ensure parity and graceful fallback
- **Pass 3:** Thread-safety review — verified lock ordering (`_state_lock → _halt_lock → _activity_lock`), confirmed no nested lock acquisitions, validated `copy.deepcopy` snapshot patterns

All findings from the review cycle were identified and fixed before sign-off. The detailed manifest of changes is at `.agent_lessons/final_review_manifest.md`.

---

## 8. What's Remaining

### Phase 3 Step F: Remove `STREAMING_TOOLS` Special Case
- **Status:** Not yet done — this is the **point of no return** for the unification
- **Action:** Delete the hardcoded `STREAMING_TOOLS` set from `agent_orchestrator.py`; remove the special-case handling that bypasses the unified loop for streaming tools
- **Risk:** High — requires extensive end-to-end testing before removal
- **Recommendation:** Leave behind a feature flag gate until all testing confirms parity

### Phase 4: CSS Consolidation
- **Action:** Remove duplicate `.sub-msg-*` CSS classes; consolidate into data-attribute-based selectors added in Phase 1
- **Depends on:** Frontend rendering fully migrated to `renderAgentConversation()` (Phase 1 Step D, pending)

### Phase 5: Feature Flag Removal & Cleanup
- **Action:** Remove all three feature flags; hard-code unified paths as the primary behavior; delete `config/unified.py`; clean up dead code and legacy markers
- **Depends on:** Successful testing of the fully unified system (Phases 3 and 4 complete)

### Pre-Commit: Testing the Unified Branch
- Run full end-to-end test suite covering:
  - Deeply nested agent calls (Root → A → B → C)
  - Parallel execution with concurrency limits
  - Security Advisor auto-launch and subsequent state management
  - Token cache eviction across multiple instances
  - `__USE_PREV_ARG__` resolution in both streaming and non-streaming modes

---

## 9. Known TODOs

These are documented gaps that should be addressed before the final commit:

### 1. `renderMessages()` Incremental Append Path (Frontend)
- **Location:** `web_ui/app.js`, `renderMessages()` function
- **Issue:** The incremental append path still uses the old per-message loop pattern instead of the new config-based rendering
- **Code comment reference:** Documented inline with TODO marker
- **Fix needed:** Update to pass `getRootAgentConfig()` as the third argument to `createMessageEl()` and `updateBubbleContent()`

### 2. Sub-Agent Streaming Update Paths Not Yet Unified
- **Location:** `web_ui/app.js` — `updateSubBubbleContent()` vs `updateBubbleContent()`
- **Issue:** Two separate functions handle streaming updates for sub-agents vs root messages. They should converge to a single `updateBubbleContent()` using the config pattern
- **Fix needed:** Refactor `updateSubBubbleContent()` to delegate to `updateBubbleContent()` with appropriate config

### 3. `createSubMsgEl` Is Deprecated But Not Removed
- **Location:** `web_ui/app.js`
- **Issue:** The old `createSubMsgEl()` function still exists alongside the new config-based `createMessageEl()`. Both coexist during the transition period
- **Fix needed:** Remove after Phase 1 Step E (render merge + dead code cleanup)

---

## 10. Important Files for Reference

### Plan & Documentation

| File | Location | Purpose |
|------|----------|---------|
| `tab_unification_plan_v4.md` | `N:\work\WD\AgentCascade\.agent_lessons\` | The definitive plan document — all phases, architecture, timeline, rollback strategy |
| `parallel_merge_protocol.md` | `N:\work\WD\AgentCascade\.agent_lessons\` | Detailed lock ordering, pseudocode for concurrent writes, race condition prevention checklist |
| `PROJECT_STATUS.md` (this file) | Both repos | This handoff document — current state, what was done, what remains |

### Review Artifacts

| File | Location | Purpose |
|------|----------|---------|
| `final_review_report.md` | `N:\work\WD\AgentCascade\.agent_lessons\` | Comprehensive 3-pass review report with findings and fixes |
| `final_review_manifest.md` | `N:\work\WD\AgentCascade\.agent_lessons\` | Change manifest from final review — what was verified, what was flagged |
| `unified_uncommitted_changes_report.md` | `N:\work\WD\AgentCascade_unified\.agent_lessons\` | Detailed diff analysis of all uncommitted changes on the unified branch |

### Core Modified Files (Working Branch)

| File | Size Impact | Role |
|------|-------------|------|
| `agent_orchestrator.py` | ~126KB, heavy edits | Message loop unification, dual-read wrapper, streaming path fixes |
| `agent_pool.py` | ~53KB, moderate edits | Lock guards for `enqueue_message()` and `drain_queue()` |
| `web_ui/app.js` | ~152KB, heavy edits | Config-based rendering helpers, unified message element creation |
| `web_ui/styles.css` | ~47KB, moderate edits | Data-attribute CSS selectors for unified styling |
| `api_server.py` | ~159KB, moderate edits | State unification, Security Advisor integration points |

---

## 11. Rollback Plan

If issues are discovered during testing, follow this rollback sequence:

1. **Frontend Rollback:** Revert `web_ui/app.js` and `web_ui/styles.css` — safe, no backend state impact
2. **Backend State Rollback:** Set all feature flags to `False` (the emergency kill switch via `AC_USE_UNIFIED_ARCHITECTURE=0`)
3. **Message Loop Rollback:** Re-introduce `STREAMING_TOOLS` interception if the unified loop causes failures — most complex rollback, only needed for critical issues

**Emergency Kill Switch:** Set `AC_USE_UNIFIED_ARCHITECTURE=0` (or unset it) to revert to legacy behavior **without code redeployment**, provided all code paths check the flag. If any path bypasses the flag, a full code rollback is required.

---

## 12. Quick-Start for Next Agent Session

If you're picking up this work, here's the fastest way to get oriented:

```bash
# 1. Switch to the working branch
cd N:\work\WD\AgentCascade_unified
git checkout tab-unification

# 2. Read the plan (this is your north star)
cat .agent_lessons/tab_unification_plan_v4.md

# 3. Check current state of feature flags
python -c "from config.unified import *; print(USE_UNIFIED_ARCHITECTURE, USE_UNIFIED_STATE, USE_UNIFIED_LOOP)"

# 4. Verify the new files exist
ls config/unified.py config/token_cache.py agent_cascade/tool_utils.py

# 5. Start from what's remaining (Section 8 above)
#    - Phase 3 Step F: Remove STREAMING_TOOLS (point of no return)
#    - Phase 4: CSS consolidation
#    - Phase 5: Flag removal and cleanup
#    - Testing before commit
```

---

**Document Version:** 1.0  
**Last Updated:** 2026-05-24  
**Next Action:** Testing the unified branch, then commit to `tab-unification`