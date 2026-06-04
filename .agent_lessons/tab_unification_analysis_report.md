# Tab Unification Plan — Current Viability Analysis

**Date:** May 23, 2026  
**Analyst:** PlanAnalyzer  
**Source Files Examined:** Main branch at `n:\work\WD\AgentCascade\` (current HEAD)  
**Unified Branch Reference:** `n:\work\WD\AgentCascade_unified\`, branch `tab-unification` (uncommitted changes present)

---

## Executive Summary

The tab unification plan remains **architecturally sound but requires significant revision** to account for substantial code growth and new complexity on the main branch since the unified branch was forked. The frontend unification work started on the unified branch provides a good template, but the backend dual-track architecture is now even more entrenched than when the plan was written.

---

## 1. Branch Divergence Analysis

### File Size Growth (Main vs Unified)

| File | Main Size | Unified Size | Delta | Significance |
|------|-----------|-------------|-------|--------------|
| `api_server.py` | 159,258 B | 134,675 B | +24,583 | Moderate — new endpoints, Security Advisor auto-launch |
| `agent_pool.py` | 52,793 B | 46,352 B | +6,441 | Low-Moderate — idle checker refinements |
| `agent_orchestrator.py` | 126,214 B | 106,938 B | +19,276 | Moderate — ParallelAgentManager, grep spillover |
| `web_ui/app.js` | 151,713 B | 133,086 B | +18,627 | Moderate — new features (AFK, context bar) |
| `api_router.py` | 30,449 B | 20,201 B | +10,248 | **High** — multi-endpoint failover logic added |
| `operation_manager.py` | 91,050 B | 43,868 B | **+47,182** | **Very High** — nearly doubled in size |

### Git Commit Gap

The main branch has advanced from commit `9c6a92a` to `f291d51` (approximately 30+ commits) since the unified branch was created. The unified branch's frontend unification work cannot be directly merged because:
- Main branch `api_server.py` has diverged with new endpoints and Security Advisor auto-launch logic
- `operation_manager.py` grew by 47KB — almost entirely absent from unified branch
- `api_router.py` added multi-endpoint failover (10KB of new complexity)

---

## 2. Per-File Current State Assessment

### 2.1 `api_server.py` (Main: 159KB, Unified: 135KB)

**Dual-Track Patterns Still Present:**
- **`session['history']`** remains the authoritative store for orchestrator messages (line 474, 496, 654+)
- **`agent_pool.sub_agent_state`** remains siloed for sub-agents (line 507-631)
- **`build_state()`** (line 652) still has a special case: it reads `session['history']` directly, while sub-agents come from `get_sub_agent_state()` — no unified iteration
- **Token caching is split**: `_cached_hist_stats` for main vs `_sa_stats_{name}` for each sub-agent (lines 672-704, 548-585)
- **Sync logic** between `session['history']` and `agent_pool.instance_conversations` has grown with Security Advisor auto-launch (lines 2227-2295)

**New Complexity Since Plan Written:**
1. **Security Advisor Auto-Launch**: New code block (lines 2218-2239, 2527-2544) that dynamically creates sub-agent state for security analysis — adds another special case to the sync logic
2. **`build_stream_update()`** grew with telemetry and sub-agent staleness handling (lines 756-830)
3. **Show active-only mode**: New `show_active_only` UI config that affects both main and sub-agent rendering (line 667)

**Impact on Plan:** The plan's Phase 2 (state structure migration) is now more complex because:
- `session['history']` is referenced in 100+ locations — not just `build_state()` but also streaming handlers, rollback logic, edit/retry operations
- The Security Advisor auto-launch creates a sub-agent state entry dynamically within the main generation loop, blurring the line between "orchestrator" and "sub-agent" flows

**Viability: MEDIUM** — The core concept (move history into unified store) is still valid, but the migration surface has grown significantly.

---

### 2.2 `agent_pool.py` (Main: 53KB, Unified: 46KB)

**Dual-Track Patterns Still Present:**
- **Orchestrator ("Maine") is explicitly excluded from auto-dismissal** at line 726-727: `if instance_name == 'Maine': return False`
- **No orchestrator registration**: The OrchestratorAgent exists outside the AgentPool, created separately in `api_server.py` and passed as a dependency
- **`sub_agent_state`** is separate from `instance_conversations` — two parallel data structures for the same concept
- **`clear_conversation()`** (line 665) explicitly pops both `instance_conversations[name]` AND `sub_agent_state[name]`

**New Complexity Since Plan Written:**
1. **Idle checker thread** with auto-dismissal logic (lines 753-848) — orchestrator is the only agent excluded
2. **Recovery from logs** via `load_session_from_log()` (line 854+) — creates new sub_agent_state entries dynamically
3. **History cleanup** in `_cleanup_history()` (line 1007+) — echo detection, compression gap handling

**Impact on Plan:** The plan's Phase 3 (orchestrator registration) must now account for:
- The idle checker's special-casing of "Maine"
- Recovery logic that creates instances dynamically from log files
- The auto-dismissal exclusion being the only place where orchestrator identity ("Maine") is hardcoded

**Viability: MEDIUM-HIGH** — Orchestrator registration is straightforward, but must handle idle checker and recovery edge cases.

---

### 2.3 `agent_orchestrator.py` (Main: 126KB, Unified: 107KB)

**Dual-Track Patterns Still Present:**
- **`STREAMING_TOOLS = {'call_agent', 'continue_with_agent'}`** at line 410 — still a hardcoded set of special-case tools
- **Special interception at line 1383**: `if tool_name in self.STREAMING_TOOLS:` — this is the core branching point between normal tools and sub-agent execution
- **`_stream_sub_agent_call()`** (line 1735+) is a dedicated generator method, completely separate from the main `_run()` loop

**New Complexity Since Plan Written:**
1. **ParallelAgentManager** (line 266) — new class for parallel sub-agent execution with concurrency limits per agent class
2. **`__USE_PREV_ARG__` placeholder replacement** (lines 1452-1478) — adds caching logic in `last_tool_args`
3. **Grep spillover pre-computation** (line 537+) — new tool behavior specific to grep
4. **Turn limit handling** with `_count_message_tokens` and `_get_history_tokens` — token counting logic duplicated from base.py
5. **Compression warning injection** (`_inject_compression_warning`, line 753) — orchestrator-specific

**Impact on Plan:** The plan's Phase 3 (message loop refactoring) is now more complex:
- Parallel execution adds a third code path: sequential → parallel → fallback sequential
- The `STREAMING_TOOLS` set must account for parallel vs sequential dispatch logic
- Removing the special case requires understanding all three dispatch paths

**Viability: MEDIUM** — The core idea (treat call_agent as recursive loop invocation) is correct, but the parallel execution layer adds significant indirection.

---

### 2.4 `web_ui/app.js` (Main: 152KB, Unified: 133KB)

**Dual-Track Patterns Still Present:**
- **`renderMessages()`** (line 1123) — renders main chat with `createMessageEl(msg, index)` 
- **`renderSubAgentPanel()`** (line 2232) — renders sub-agent panels with `createSubMsgEl(msg, index, instanceName, isGenerating)`
- **`createMessageEl()`** (line 1300) — hardcoded `msg-*` CSS classes, "You"/"Assistant"/"Tool Result" labels
- **`createSubMsgEl()`** (line 2375) — hardcoded `sub-msg-*` CSS classes, "Task"/"Agent"/"result" labels

**New Complexity Since Plan Written:**
1. **AFK auto-send** feature (lines 1080-1119) — new state tracking
2. **Context bar with token/word counts** — shared between main and sub-agent rendering but with different implementations
3. **Activity bar updates** (`updateMainActivityBar` at line 1237) — orchestrator-specific
4. **Lazy rendering** — both paths have independent lazy-render logic with content-key deduplication

**Unified Branch Progress:** The unified branch has made significant frontend progress:
- **CSS class helper functions** (lines 3288-3303): `msgClass()`, `headerClass()`, `contentClass()`, `nameLabelClass()` — abstract the `.msg-*` vs `.sub-msg-*` distinction
- **`roleName()` function** (line 3313) — unified label resolution ("You" vs "Task", "Assistant" vs "Agent")
- **`createMessageEl(msg, index, config)`** (line 1155) — accepts a `config` object with `isRoot` flag, falling back to legacy behavior
- **`updateBubbleContent(bubble, msg, config)`** (line 1234) — unified content rendering using config

**Critical Gap:** The unified branch has the helper functions and updated `createMessageEl`, but **still uses `renderMessages()` and `renderSubAgentPanel()` as separate entry points**. The full recursive rendering (`renderAgentConversation`) from the plan has not been implemented.

**Impact on Plan:** The frontend unification is ~60% complete on the unified branch:
- ✅ CSS class abstraction layer exists
- ✅ Single `createMessageEl` with config parameter
- ❌ Still two separate rendering entry points (`renderMessages` + `renderSubAgentPanel`)
- ❌ No recursive rendering for nested sub-agent trees

**Viability: HIGH** — The unified branch's frontend work is the most mature part of the effort. The remaining work (merging entry points, implementing recursive rendering) is well-scoped.

---

## 3. Plan Viability Rating

| Phase | Original Viability | Current Viability | Reason |
|-------|-------------------|-------------------|--------|
| **Phase 1: Frontend Unification** | High | **Medium-High** | Unified branch has ~60% done; needs to complete entry point merge and recursive rendering |
| **Phase 2: Backend State Unification** | Medium | **Low-Medium** | Dual-track is more entrenched; Security Advisor auto-launch adds complexity; 100+ references to `session['history']` |
| **Phase 3: Message Loop Unification** | Low-Medium | **Low** | ParallelAgentManager, __USE_PREV_ARG__, grep spillover all add branching logic that must be preserved |

### Overall Plan Viability: **LOW-MEDIUM**

The plan's core architectural vision remains correct, but the execution surface has grown significantly. The main branch is no longer a simple superset of where the unified branch started — it has accumulated 30+ commits of new features (Security Advisor auto-launch, parallel agent management, multi-endpoint failover, grep spillover, AFK, context bar) that must be preserved during unification.

---

## 4. Specific Revisions Needed to the Plan

### Revision 1: Phased Rollout Strategy Must Change

**Current plan:** Frontend → Backend State → Message Loop (top-down).  
**Revised approach:** The frontend work on the unified branch should be **merged into main first** (since it's partially done and lowest risk), then backend state unification can proceed with a working UI. This reverses the original phase order for practical reasons.

### Revision 2: Backend State Migration Must Be Incremental

**Current plan:** Move `session['history']` into `sub_agent_state['root']['messages']` in one pass.  
**Revised approach:** Implement a **dual-read mode** where `build_state()` can read from either the old or new store, controlled by a feature flag. This allows testing without breaking existing API consumers.

### Revision 3: Message Loop Refactoring Must Handle Parallel Execution

**Current plan:** Remove `STREAMING_TOOLS` special case and treat `call_agent` as recursive loop invocation.  
**Revised approach:** The unification must account for **three dispatch paths**:
1. Sequential streaming (`yield from _stream_sub_agent_call`)
2. Parallel execution (`parallel_manager.submit_task`)
3. Sequential fallback (when concurrency limit reached)

The unified message loop must support all three, not just sequential recursion.

### Revision 4: OperationManager Integration Must Be Addressed

**New finding:** `operation_manager.py` grew by 47KB on main but is only 44KB on unified — the unified branch may have reverted or excluded this module's changes. The plan should explicitly address how operation manager (grep, file operations, shell commands) integrates with the unified agent model.

### Revision 5: API Router Must Be Part of State Unification

**New finding:** `api_router.py` added multi-endpoint failover logic (10KB growth). The plan should consider whether token limits and concurrency limits should be part of the unified per-instance state, or remain at the router level.

---

## 5. New Risks Identified

### Risk 1: Merge Conflict Explosion
The unified branch's frontend changes will likely conflict with main in `app.js` at multiple points (edit handling, rendering functions, state management). Estimated conflict density: **high** — over 400 lines of overlapping changes.

### Risk 2: Security Advisor Auto-Launch Dependency
The Security Advisor auto-launch (main branch) creates sub-agent state entries dynamically during orchestrator generation. Any state unification that moves history into a unified store must handle this dynamic instantiation without breaking the auto-launch flow.

### Risk 3: Parallel Agent State Synchronization
When agents run in parallel via `ParallelAgentManager`, their states are independent but share the same `agent_pool`. The unified state model must handle concurrent writes to instance conversations without race conditions.

### Risk 4: Token Cache Invalidation Complexity
The plan proposes consolidating `_cached_hist_stats` and `_sa_stats_{name}` into a single `AgentTokenCache`. With parallel agents, cache invalidation becomes more complex — truncating one agent's history must not affect another agent's cached stats.

### Risk 5: OperationManager Size Discrepancy
The 47KB gap in `operation_manager.py` suggests the unified branch may be missing critical file operation functionality. Before merging, this module must be reconciled to ensure no capabilities are lost.

---

## 6. Recommended Action Plan

### Step 1: Reconcile operation_manager.py (Priority: Critical)
- Compare main vs unified `operation_manager.py` line-by-line
- Identify which features were dropped on unified branch
- Merge missing functionality before any unification work

### Step 2: Merge Unified Frontend Progress into Main (Priority: High)
- Cherry-pick or merge the CSS class helpers (`msgClass`, `headerClass`, etc.)
- Merge updated `createMessageEl` with config parameter
- This gives a working unified UI foundation before backend changes

### Step 3: Backend State Unification with Dual-Read Mode (Priority: Medium)
- Add feature flag `USE_UNIFIED_STATE` 
- Implement `get_agent_state(instance_name)` that works for both old and new stores
- Gradually migrate `build_state()` to use unified store

### Step 4: Orchestrator Registration in AgentPool (Priority: Medium)
- Register orchestrator as first-class instance named "Maine"
- Update idle checker to handle root instance differently (no auto-dismissal, but same lifecycle otherwise)
- Remove explicit `"Maine"` string comparisons where possible

### Step 5: Message Loop Unification (Priority: Low — highest risk)
- Only after frontend and state are unified
- Handle three dispatch paths (sequential, parallel, fallback)
- Use feature flag for gradual rollout

---

## 7. Summary

The tab unification plan's **architectural vision remains valid** — the orchestrator should be treated as a first-class agent instance in a unified pool. However, the **execution complexity has grown significantly** since the plan was written:

- **30+ commits** of new features on main branch
- **operation_manager.py nearly doubled** (91KB vs 44KB)
- **Parallel agent execution** adds a third dispatch path
- **Security Advisor auto-launch** creates dynamic sub-agent state during generation
- **Multi-endpoint API router** adds token/concurrency management complexity

The unified branch's **frontend progress is the most valuable asset** — the CSS class abstraction layer and unified `createMessageEl` with config parameter provide a solid foundation. This work should be prioritized for merge into main before backend unification begins.

The plan should be revised to:
1. Reverse phase order (frontend first, backend second)
2. Use incremental migration with feature flags
3. Explicitly address parallel execution and Security Advisor auto-launch
4. Reconcile operation_manager.py discrepancies before merging