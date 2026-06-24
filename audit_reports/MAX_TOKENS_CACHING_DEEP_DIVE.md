# max_tokens Caching Deep Dive — Complete Data Flow Analysis

**Date:** 2026-06-24  
**Author:** MaxTokensInvestigation (Deep Research Specialist)  
**Severity:** Critical  
**Branch:** tab-unification  
**Related Reports:** `MAX_TOKENS_CACHE_STALENESS_BUG_RESEARCH.md` (2026-06-22)

---

## 1. Executive Summary

The `_max_tokens_cache` module-level cache described in the prior audit report has **already been removed** from the codebase. However, the stale max_tokens bug **persists** through a different mechanism: **`_allocated_max_input_tokens` on `AgentInstance` acts as an uninvalidated cache that short-circuits the endpoint-specific resolution chain.**

### Root Cause

In `_resolve_max_tokens` (api_integration.py, lines 848-853), **Step 2b** checks `instance._allocated_max_input_tokens` and returns it **immediately** if > 0. This value was set during the **last LLM call** and may correspond to a **different endpoint** than the one currently assigned to the agent. Since this check short-circuits before the router limit is checked, agents always return the max_tokens from their **last LLM call's endpoint**, not the **currently assigned endpoint**.

### Impact

- When endpoints are changed at runtime (failover, reconfiguration, priority changes), the UI and compression checks report stale max_tokens values
- Agents with different endpoint assignments may report each other's token limits
- Compression triggers fire at incorrect thresholds

---

## 2. Previous Fix Status: `_max_tokens_cache` Already Removed

### 2.1 Prior Audit Report Findings (2026-06-22)

The previous report (`MAX_TOKENS_CACHE_STALENESS_BUG_RESEARCH.md`) identified a module-level `_max_tokens_cache: Dict[str, int] = {}` in `api_integration.py` that cached `_get_max_tokens_for_instance` results per instance name. The cache was only invalidated during `AgentPool.reset()`.

### 2.2 Current Status: Cache Completely Removed

**Confirmed by grep:** `_max_tokens_cache` returns **no matches** in the entire codebase. The cache has been fully removed.

The current code in `api_integration.py` uses **direct calls** without caching:

```python
# build_state_from_pool (line 464-466)
max_tokens = _get_max_tokens_for_instance(pool, instance)

# build_stream_update_from_pool (line 649-651)
max_tokens = _get_max_tokens_for_instance(pool, instance)

# _serialize_instance (line 1173-1174)
max_tokens = _get_max_tokens_for_instance(pool, inst)
```

### 2.3 What Remains Cleared During Reset

`_clear_performance_caches()` (api_integration.py, lines 57-62) clears:
- `_token_stats_cache` (line 60)
- `_cached_instance_data` (line 61)
- `_stream_token_stats_cache` (line 62)

**`_max_tokens_cache` is NOT in this list** — confirming it was removed.

---

## 3. The Persistent Bug: `_allocated_max_input_tokens` as an Uninvalidated Cache

### 3.1 The Resolution Chain

`_resolve_max_tokens` (api_integration.py, lines 795-888) follows this priority order:

| Step | Source | Code Location | Behavior |
|------|--------|---------------|----------|
| 1 | API Router | Lines 833-840 | `pool.api_router.get_effective_max_tokens(agent_class)` |
| 2 | Per-instance override | Lines 843-846 | `instance._generate_cfg_override.get('max_input_tokens')` |
| **2b** | **Instance allocated max** | **Lines 848-853** | **`instance._allocated_max_input_tokens`** |
| 3 | Runtime-detected LLM limit | Lines 855-864 | `llm.generate_cfg['max_input_tokens']` |
| 4 | Template static config | Lines 866-878 | `llm.cfg['generate_cfg']['max_input_tokens']` |
| 5 | Default | Line 888 | `DEFAULT_MAX_INPUT_TOKENS` |

### 3.2 The Critical Short-Circuit

**Step 2b (lines 848-853):**

```python
# ── Step 2b: Instance's allocated max_input_tokens (Feature 006) ──
# Check ground-truth value from last LLM call for consistent tool truncation thresholds
if instance and hasattr(instance, '_allocated_max_input_tokens'):
    allocated = instance._allocated_max_input_tokens
    if allocated > 0:
        return allocated  # ← SHORT-CIRCUITS: never reaches Step 1's router check
```

This `return` statement **bypasses** Step 1 (API Router). The comment says the value is from the "last LLM call for consistent tool truncation thresholds," but it's also used for **UI state display** and **compression checks**.

### 3.3 Where `_allocated_max_input_tokens` Is Set

**During LLM calls** (execution_engine.py, lines 1633-1638):

```python
# Store allocated max_input_tokens in instance for compression check (ground-truth tracking)
if 'max_input_tokens' in merged_cfg:
    val = merged_cfg['max_input_tokens']
    if isinstance(val, int) and val > 0:
        instance._allocated_max_input_tokens = val
```

This happens in `_execute_llm_call` inside `_do_call`, which receives `llm_cfg` from `api_router.call_with_fallback()`. The `merged_cfg` is built from:
1. Endpoint config (from the router's priority list)
2. Per-instance override (if set)

The value stored is whatever endpoint config was **actually used for that LLM call**.

**Also set by token count callback** (execution_engine.py, lines 179-187):

```python
def _make_token_count_callback(instance):
    def _on_token_count(all_tokens: int, available_token: int, max_tokens: int):
        instance._last_actual_token_count = all_tokens
        if max_tokens > 0:
            instance._allocated_max_input_tokens = max_tokens
    return _on_token_count
```

This callback is invoked by `llm/base.py` after token counting, providing ground-truth values from the LLM API.

### 3.4 Where `_allocated_max_input_tokens` Is **NOT** Reset

| Operation | `_generate_cfg_override` Reset? | `_allocated_max_input_tokens` Reset? |
|-----------|--------------------------------|-------------------------------------|
| `load_session_from_log()` (line 1156) | ✅ Yes | ❌ **No** |
| `reset()` (via dismiss/remove) | ✅ (via remove_instance) | ❌ **No** |
| `remove_instance()` | N/A (instance removed) | N/A |
| Endpoint change at runtime | ❌ No | ❌ **No** |
| Priority change at runtime | ❌ No | ❌ **No** |

**Critical gap:** When endpoints or priorities change at runtime, `_allocated_max_input_tokens` is **never invalidated**. It retains the value from the last LLM call, which may have used a different endpoint.

---

## 4. Flow A — Initial Setup

### 4.1 Where max_tokens Is First Set from API Endpoint Config

**Entry point:** `AgentPool.__init__` (agent_pool.py, lines 219-312)

```python
if api_router is not None:
    self.api_router = api_router
else:
    from agent_cascade.api_router import APIRouter
    self.api_router = APIRouter(
        default_llm_cfg=llm_cfg,
        config_dir=config_dir
    )
```

The `APIRouter` loads persisted config from `config/api_endpoints.json` in its `__init__` (api_router.py, line 522).

### 4.2 How max_tokens Propagates to AgentInstance

**Path 1: UI State Display** (no caching at pool level)

```
build_state_from_pool → _get_max_tokens_for_instance → _resolve_max_tokens
  → pool.api_router.get_effective_max_tokens(agent_class)
    → iterates agent_priorities[agent_class] → reads ep.max_input_tokens
```

**Path 2: LLM Call** (sets `_allocated_max_input_tokens`)

```
_execute_llm_call → call_with_fallback → get_endpoint_chain
  → builds llm_cfg from endpoint → _do_call → merged_cfg
    → instance._allocated_max_input_tokens = merged_cfg['max_input_tokens']
```

### 4.3 Caching at Pool Level

**No pool-level caching of max_tokens exists.** The `_max_tokens_cache` has been removed. Each call to `_get_max_tokens_for_instance` makes a fresh call to `_resolve_max_tokens`.

However, `_allocated_max_input_tokens` on `AgentInstance` **acts as a per-instance cache** that is never invalidated when endpoints change.

---

## 5. Flow B — During Operation

### 5.1 Where max_tokens Is Read During LLM Calls

**In `_execute_llm_call`** (execution_engine.py, lines 1595-1678):

```python
def _execute_llm_call(self, instance, template, messages, active_functions):
    # Dynamic endpoint selection based on agent's actual token requirements
    allocated_tokens = None
    if instance._generate_cfg_override is not None and 'max_input_tokens' in instance._generate_cfg_override:
        val = instance._generate_cfg_override['max_input_tokens']
        if isinstance(val, int) and val > 0:
            allocated_tokens = val
    elif hasattr(llm, 'generate_cfg') and 'max_input_tokens' in llm.generate_cfg:
        val = llm.generate_cfg['max_input_tokens']
        if isinstance(val, int) and val > 0:
            allocated_tokens = val
```

Then in `_do_call`:

```python
def _do_call(llm_cfg: dict) -> Iterator[List[Message]]:
    merged_cfg = dict(llm_cfg)  # Endpoint defaults first
    if instance._generate_cfg_override is not None:
        merged_cfg.update(instance._generate_cfg_override)
    elif hasattr(llm, 'generate_cfg'):
        merged_cfg.update(llm.generate_cfg)
    
    # Store allocated max_input_tokens in instance
    if 'max_input_tokens' in merged_cfg:
        val = merged_cfg['max_input_tokens']
        if isinstance(val, int) and val > 0:
            instance._allocated_max_input_tokens = val
```

The `llm_cfg` comes from `api_router.call_with_fallback()`, which calls `get_endpoint_chain()` to build the chain of endpoints to try.

### 5.2 Where max_tokens Is Read for UI State

**`build_state_from_pool`** (api_integration.py, line 466):
```python
max_tokens = _get_max_tokens_for_instance(pool, instance)
```

**`build_stream_update_from_pool`** (api_integration.py, line 651):
```python
max_tokens = _get_max_tokens_for_instance(pool, instance)
```

**`_serialize_instance`** (api_integration.py, line 1174):
```python
max_tokens = _get_max_tokens_for_instance(pool, inst)
```

All three call `_resolve_max_tokens`, which may return the stale `_allocated_max_input_tokens` value.

### 5.3 Where max_tokens Is Read for Compression

**`_check_and_trigger_compression`** (execution_engine.py, line 1159):
```python
max_tokens = self._get_max_tokens(instance)
```

Which calls `_resolve_max_tokens` (execution_engine.py, lines 3037-3044):
```python
def _get_max_tokens(self, instance: AgentInstance) -> int:
    from agent_cascade.api_integration import _resolve_max_tokens
    return _resolve_max_tokens(self.pool, instance)
```

**Also uses ground-truth values directly** (lines 1164-1165):
```python
actual_tokens = instance._last_actual_token_count
allocated_max = instance._allocated_max_input_tokens
```

When both `actual_tokens > 0` and `allocated_max > 0`, compression uses `allocated_max` directly (line 1170), bypassing `_resolve_max_tokens` entirely.

### 5.4 Multiple Code Paths

| Code Path | Reads max_tokens From | Affected by Staleness? |
|-----------|----------------------|----------------------|
| UI state (`build_state_from_pool`) | `_resolve_max_tokens` | ✅ Yes (via Step 2b) |
| Stream update (`build_stream_update_from_pool`) | `_resolve_max_tokens` | ✅ Yes (via Step 2b) |
| Instance serialization (`_serialize_instance`) | `_resolve_max_tokens` | ✅ Yes (via Step 2b) |
| Compression check (`_check_and_trigger_compression`) | `_resolve_max_tokens` + direct read | ✅ Yes (direct read of `_allocated_max_input_tokens`) |
| LLM call (`_execute_llm_call`) | Endpoint config from router | ❌ No (reads fresh from router) |

---

## 6. Flow C — Session Restore

### 6.1 Session Restore Path

**`load_session_from_log`** (agent_pool.py, lines 939-1252):

```python
def load_session_from_log(self, log_input, target_instance=None, clear_sub_agents_before_load=False):
    # ... parse JSONL, build working_set ...
    
    existing = self.instances.get(instance_name)
    if existing:
        with existing._compression_lock:
            existing.rebuild_conversation(restored_messages)
            existing.agent_class = agent_class
            existing._streaming_responses = []
            existing._last_config_version = -1
            existing._generate_cfg_override = None  # ← Cleared
            existing._last_force_compress_time = 0.0
            existing._force_compress_count = 0
            existing._slot_release = None
            existing._suppress_loop_detection_next_turn = False
            existing.state = AgentState.IDLE
        # NOTE: _allocated_max_input_tokens is NOT cleared here
```

### 6.2 Stale Value Propagation During Restore

When a session is restored:
1. `_generate_cfg_override` is cleared (line 1156)
2. `_allocated_max_input_tokens` is **NOT cleared** (not in the reset list)

This means if the agent previously had an LLM call with endpoint A's max_tokens, and then the session is restored with the agent now assigned to endpoint B, the stale value from endpoint A will be returned by `_resolve_max_tokens` via Step 2b.

### 6.3 Could This Overwrite Correct Endpoint's max_tokens?

**Yes.** The resolution chain order is:
1. Router limit (fresh from endpoint config)
2. Per-instance override (cleared on restore)
3. **`_allocated_max_input_tokens` (NOT cleared on restore)** ← BUG

Since Step 2b returns immediately if `_allocated_max_input_tokens > 0`, the router's fresh endpoint-specific value is never consulted.

---

## 7. Detailed Analysis of Caching Mechanisms

### 7.1 All max_tokens Cache Mechanisms

| Cache | Type | Location | Invalidated On |
|-------|------|----------|---------------|
| `_max_tokens_cache` | Module-level dict | `api_integration.py` | **REMOVED** (was only on reset) |
| `_allocated_max_input_tokens` | Instance attribute | `AgentInstance` | **NEVER** (no invalidation path) |
| `_generate_cfg_override` | Instance attribute | `AgentInstance` | Session restore, reset |
| `_token_stats_cache` | Module-level dict | `api_integration.py` | Reset, conversation change |
| `_stream_token_stats_cache` | Module-level dict | `api_integration.py` | Reset, conversation change |

### 7.2 The `_allocated_max_input_tokens` Cache Problem

This is a **per-instance attribute** (agent_instance.py, line 110):

```python
_allocated_max_input_tokens: int = field(default=0)  # Max input tokens allocated for the last LLM call
```

**Set by:**
1. `_execute_llm_call` → `_do_call` (execution_engine.py, lines 1633-1638)
2. Token count callback (execution_engine.py, lines 179-187)

**Never invalidated by:**
- Endpoint changes at runtime
- Priority changes at runtime
- Session restore
- `AgentPool.reset()` (via `remove_instance` — but root orchestrator survives reset)

### 7.3 Cache Invalidation vs. Where It SHOULD Be Invalidated

| Current Invalidation | Missing Invalidation |
|---------------------|---------------------|
| Instance removal (via `remove_instance`) | Endpoint CRUD operations (`add_endpoint`, `update_endpoint`, `remove_endpoint`) |
| Session restore (for `_generate_cfg_override`) | Priority changes (`set_agent_priorities`) |
| Reset (via dismiss + remove) | `default_llm_cfg` changes |
| | `clear_sub_agents()` |

---

## 8. Race Conditions

### 8.1 Thread Safety Analysis

**`_allocated_max_input_tokens` is written from:**
- `_execute_llm_call` (execution_engine.py) — runs in agent thread
- Token count callback (execution_engine.py) — invoked during LLM call

**`_allocated_max_input_tokens` is read from:**
- `_resolve_max_tokens` (api_integration.py) — called from UI thread (WebSocket handlers)
- `_check_and_trigger_compression` (execution_engine.py) — runs in agent thread

**Potential race:** The UI thread reads `_allocated_max_input_tokens` via `_resolve_max_tokens` while the agent thread is in the middle of an LLM call that updates it. However:
- Python's GIL ensures atomic reads/writes for simple integer assignments
- The value is an `int` (immutable type), so torn reads are not possible
- **No actual race condition exists** for this specific field

### 8.2 Priority List Race Condition

The API router's `agent_priorities` dict is accessed under `self._lock` in both `get_effective_max_tokens` (api_router.py, lines 673-684) and mutation methods (`set_agent_priorities`, `remove_endpoint`). **No race condition** here either.

---

## 9. Compression System and max_tokens

### 9.1 Does Compression Set a Different max_tokens?

**Yes, but not directly.** The compression system uses `_resolve_max_tokens` for its checks, which may return the stale `_allocated_max_input_tokens` value. Additionally, compression uses ground-truth values directly:

```python
# _check_and_trigger_compression (execution_engine.py, lines 1164-1170)
actual_tokens = instance._last_actual_token_count
allocated_max = instance._allocated_max_input_tokens

if actual_tokens > 0 and allocated_max > 0:
    current_tokens = actual_tokens
    max_tokens_for_check = allocated_max  # ← Uses stale value directly
```

### 9.2 Does the Stale Value Persist After Compression Ends?

**Yes.** Compression does not reset `_allocated_max_input_tokens`. After compression:
1. The working set is rebuilt
2. Token caches are invalidated (`_cached_token_count`, `_last_actual_token_count`)
3. `_allocated_max_input_tokens` is **NOT reset**

The stale value persists until the next LLM call, which may use the same or a different endpoint.

### 9.3 Compression Trigger Scenarios with Stale Values

| Scenario | Cached Value | Actual Value | Impact |
|----------|-------------|-------------|--------|
| Endpoint max increased (4096→8192) | 4096 (from `_allocated_max_input_tokens`) | 8192 | **Premature compression** — triggers at ~50% of real limit |
| Endpoint max decreased (8192→4096) | 8192 (from `_allocated_max_input_tokens`) | 4096 | **Missed compression** — allows context to overflow |
| Priority changed (Endpoint A→B) | A.max_tokens | B.max_tokens | **Wrong limit entirely** |
| Endpoint deleted | Old endpoint's value | New endpoint's value | **Wrong limit entirely** |

---

## 10. The `_resolve_max_tokens` Resolution Chain — Detailed Trace

### 10.1 Full Resolution Logic (api_integration.py, lines 795-888)

```python
def _resolve_max_tokens(pool, instance=None):
    # ── Step 1: API Router (per-endpoint priority-based selection) ──
    router_limit = 0
    if pool and hasattr(pool, 'api_router') and pool.api_router:
        try:
            agent_class = instance.agent_class.lower() if instance else 'orchestrator'
            router_limit = pool.api_router.get_effective_max_tokens(agent_class)
        except Exception as e:
            logger.debug(f"API Router lookup failed for {agent_class}: {e}")

    # ── Step 2: Per-instance override (from execution engine propagation) ──
    if instance and hasattr(instance, '_generate_cfg_override') and instance._generate_cfg_override:
        inst_override = instance._generate_cfg_override.get('max_input_tokens')
        if inst_override:
            return int(inst_override)  # ← Short-circuit

    # ── Step 2b: Instance's allocated max_input_tokens (Feature 006) ──
    if instance and hasattr(instance, '_allocated_max_input_tokens'):
        allocated = instance._allocated_max_input_tokens
        if allocated > 0:
            return allocated  # ← BUG: Short-circuits router check!

    # ── Step 3: Runtime-detected LLM limit ──
    # ... (llm.generate_cfg)

    # ── Step 4: Template static config ──
    # ... (llm.cfg)

    # ── Step 5: Resolve priority ──
    if router_limit > 0:
        return router_limit
    # ... (fallbacks)
```

### 10.2 The Bug in Plain English

1. **Step 1** correctly reads the router's endpoint-specific limit
2. **Step 2** checks for per-instance override (cleared on restore)
3. **Step 2b** checks `_allocated_max_input_tokens` — **if set, returns immediately**
4. **Step 1's value is never used** because Step 2b short-circuits

The router's value is only used in the **final fallback** (Step 5), which is never reached when `_allocated_max_input_tokens > 0`.

### 10.3 Why Step 2b Exists

The comment says: "Check ground-truth value from last LLM call for **consistent tool truncation thresholds**."

This was designed for **tool truncation** consistency — ensuring that when tools need to truncate their output, they use the same threshold as the LLM call. However, this value is also returned by `_resolve_max_tokens` for **UI display** and **compression checks**, where the ground-truth value is **incorrect** because it reflects a **different endpoint**.

---

## 11. Fix Recommendations

### 11.1 Recommended Fix: Reorder Resolution Chain

**Primary fix:** Move Step 1 (API Router) to take **absolute priority**, before Step 2b.

The resolution chain should be:
1. **API Router** (per-endpoint priority-based) — **ALWAYS check first**
2. Per-instance override (`_generate_cfg_override`)
3. Runtime-detected LLM limit
4. Template static config
5. Default

**Remove Step 2b from the resolution chain entirely.** The `_allocated_max_input_tokens` value should only be used for **compression checks** (where it's already read directly), not for the general `_resolve_max_tokens` resolution.

### 11.2 Implementation Plan

#### Step 1: Reorder `_resolve_max_tokens` (api_integration.py)

```python
def _resolve_max_tokens(pool, instance=None):
    # ── Step 1: API Router (per-endpoint priority-based) — CHECK FIRST ──
    router_limit = 0
    if pool and hasattr(pool, 'api_router') and pool.api_router:
        try:
            agent_class = instance.agent_class.lower() if instance else 'orchestrator'
            router_limit = pool.api_router.get_effective_max_tokens(agent_class)
        except Exception as e:
            logger.debug(f"API Router lookup failed for {agent_class}: {e}")
    
    # ── Step 1b: Per-instance override (from execution engine propagation) ──
    if instance and hasattr(instance, '_generate_cfg_override') and instance._generate_cfg_override:
        inst_override = instance._generate_cfg_override.get('max_input_tokens')
        if inst_override:
            return int(inst_override)
    
    # ── REMOVED: Step 2b (_allocated_max_input_tokens) ──
    # This value was short-circuiting the router check. It is now only used
    # directly in _check_and_trigger_compression for compression-specific checks.
    
    # ── Step 2: Runtime-detected LLM limit ──
    # ... (existing code)
    
    # ── Step 3: Template static config ──
    # ... (existing code)
    
    # ── Step 4: Resolve priority ──
    if router_limit > 0:
        return router_limit
    # ... (fallbacks)
```

#### Step 2: Ensure Compression Uses Correct Values

In `_check_and_trigger_compression` (execution_engine.py), the direct read of `_allocated_max_input_tokens` is appropriate for compression-specific checks. However, consider also invalidating this value when endpoints change.

#### Step 3: (Optional) Add Endpoint Change Invalidation

For defense-in-depth, add invalidation of `_allocated_max_input_tokens` when:
- `add_endpoint()` is called
- `update_endpoint()` is called
- `remove_endpoint()` is called
- `set_agent_priorities()` is called
- `default_llm_cfg` is modified

This would require hooking into the APIRouter's mutation methods and notifying the pool, which then iterates over instances to clear `_allocated_max_input_tokens`.

### 11.3 Alternative: Hybrid Approach

Keep Step 2b but add a validation check:

```python
# ── Step 2b: Instance's allocated max_input_tokens (Feature 006) ──
if instance and hasattr(instance, '_allocated_max_input_tokens'):
    allocated = instance._allocated_max_input_tokens
    if allocated > 0:
        # Only return if it matches the current router limit
        if router_limit == 0 or router_limit == allocated:
            return allocated
        # Otherwise, fall through to use router_limit
```

This preserves the optimization for unchanged endpoints while fixing the staleness bug.

---

## 12. Files to Modify

| File | Lines | Changes |
|------|-------|---------|
| `agent_cascade/api_integration.py` | 848-853 | Remove or reorder Step 2b in `_resolve_max_tokens` |
| `agent_cascade/api_router.py` | 526-604 | (Optional) Add `_allocated_max_input_tokens` invalidation hooks |
| `agent_cascade/agent_pool.py` | 1140-1167 | (Optional) Clear `_allocated_max_input_tokens` during session restore |

---

## 13. Testing Recommendations

1. **Unit test:** Verify `_resolve_max_tokens` returns the router's value when `_allocated_max_input_tokens` differs
2. **Integration test:** Simulate endpoint update → verify `build_state_from_pool` returns updated `max_tokens`
3. **Compression test:** Verify compression triggers at correct thresholds after endpoint changes
4. **Session restore test:** Verify restored sessions get correct max_tokens from current endpoint config
5. **Multi-agent test:** Verify agents with different endpoint assignments report their own limits, not each other's

---

## 14. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Tool truncation inconsistency | Low | Medium | `_allocated_max_input_tokens` still set during LLM calls; compression reads it directly |
| Performance regression | Very Low | Low | `_resolve_max_tokens` is cheap; removing one dict lookup has negligible impact |
| Breaking changes | Low | Low | Only internal resolution order changed; public API unchanged |
| Compression threshold drift | Medium | High | **Must test compression after fix** — compression reads `_allocated_max_input_tokens` directly |

---

## 15. Summary of Key Findings

1. **The old `_max_tokens_cache` was already removed** — confirmed by grep returning no matches in the codebase.

2. **The stale max_tokens bug persists through `_allocated_max_input_tokens`** — an instance attribute that acts as an uninvalidated cache.

3. **Step 2b in `_resolve_max_tokens` short-circuits the router check** — when `_allocated_max_input_tokens > 0`, the function returns immediately without consulting the router's endpoint-specific limit.

4. **`_allocated_max_input_tokens` is never invalidated** when endpoints, priorities, or `default_llm_cfg` change at runtime.

5. **Session restore does not clear `_allocated_max_input_tokens`** — only `_generate_cfg_override` is cleared, leaving the stale allocation value.

6. **Compression system is doubly affected** — it reads `_allocated_max_input_tokens` both through `_resolve_max_tokens` and directly in `_check_and_trigger_compression`.

7. **The fix is a resolution chain reorder** — move the API Router check to absolute priority, before `_allocated_max_input_tokens`.

---

*End of Investigation Report*