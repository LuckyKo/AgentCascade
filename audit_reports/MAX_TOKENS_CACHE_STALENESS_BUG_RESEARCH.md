# `_max_tokens_cache` Staleness Bug — Deep Research Report

**Date:** 2026-06-22  
**Author:** TokenCacheFixResearcher  
**Severity:** Critical  
**Impact:** Compression trigger calculations and UI state display use stale `max_input_tokens` values when API endpoints change at runtime (failover, endpoint reconfiguration, priority changes).

---

## 1. Executive Summary

The `_max_tokens_cache` in `api_integration.py` caches `max_input_tokens` values per instance name. The cache is populated on first read and **never invalidated** when the API router's endpoint configuration changes at runtime. This means:

- When an endpoint is updated (e.g., `max_input_tokens` changed from 4096 to 8192), the cached value remains stale.
- The stale value propagates to **UI state** (via `build_state_from_pool`, `build_stream_update_from_pool`, `_serialize_instance`) and **compression checks** (via `execution_engine._check_and_trigger_compression`).
- Compression triggers can fire at wrong thresholds, causing premature or missed compression events.

---

## 2. Cache Definition and Invalidation Points

### 2.1 Definition (api_integration.py, lines 40-43)

```python
# BUG31 Fix #2: Cache _get_max_tokens_for_instance result per instance name.
# The max tokens value never changes during a session, so caching avoids expensive lookups.
# Key: instance_name, Value: max_input_tokens (int)
_max_tokens_cache: Dict[str, int] = {}
```

### 2.2 Invalidation Points

The **only** invalidation is `_clear_performance_caches()` (line 62-68), which clears ALL caches. This is **only** called during `AgentPool.reset()` (agent_pool.py, line 722-723), i.e., "New Session".

```python
def _clear_performance_caches():
    """Clear all module-level performance caches. Called during session reset."""
    global _token_stats_cache, _max_tokens_cache, _cached_instance_data, _stream_token_stats_cache
    _token_stats_cache.clear()
    _max_tokens_cache.clear()
    _cached_instance_data.clear()
    _stream_token_stats_cache.clear()
```

**Critical Finding:** There is NO invalidation when:
- Endpoints are added/updated/deleted via `/api/endpoints` (api_server.py lines 1147-1207)
- Agent priorities are changed via `set_agent_priorities` (api_server.py lines 1203-1205, 1966-1971)
- The API router's `default_llm_cfg` is modified

---

## 3. All Three Cache Usage Locations

### 3.1 Location 1: `build_state_from_pool` (api_integration.py, lines 470-474)

```python
# Get max tokens via module-level helper (avoids creating ExecutionEngine instance)
# BUG31 Fix #2: Cache result per instance to avoid expensive repeated lookups
if instance_name not in _max_tokens_cache:
    _max_tokens_cache[instance_name] = _get_max_tokens_for_instance(pool, instance)
max_tokens = _max_tokens_cache[instance_name]
```

This feeds into the full state snapshot sent to the frontend (line 540+), which includes `max_tokens` in the response.

### 3.2 Location 2: `build_stream_update_from_pool` (api_integration.py, lines 657-661)

```python
# Get max tokens via module-level helper (avoids creating ExecutionEngine instance)
# BUG31 Fix #2: Cache result per instance to avoid expensive repeated lookups
if instance_name not in _max_tokens_cache:
    _max_tokens_cache[instance_name] = _get_max_tokens_for_instance(pool, instance)
max_tokens = _max_tokens_cache[instance_name]
```

This feeds into the streaming update sent to the frontend during generation.

### 3.3 Location 3: `_serialize_instance` (api_integration.py, lines 1183-1186)

```python
# BUG31 Fix #2: Cache max_tokens per instance name to avoid expensive repeated lookups
if inst.instance_name not in _max_tokens_cache:
    _max_tokens_cache[inst.instance_name] = _get_max_tokens_for_instance(pool, inst)
max_tokens = _max_tokens_cache[inst.instance_name]
```

This serializes each instance's data including `max_tokens` (line 1196). Called from both `build_state_from_pool` (line 498) and `build_stream_update_from_pool` (lines 703, 711).

---

## 4. The `_resolve_max_tokens` Resolution Chain

The cached value comes from `_resolve_max_tokens` (api_integration.py, lines 805-898), which follows this priority order:

1. **API Router** (line 848): `pool.api_router.get_effective_max_tokens(agent_class)` — reads from endpoint chain
2. **Per-instance override** (line 856): `instance._generate_cfg_override.get('max_input_tokens')`
3. **Instance's allocated max** (line 863): `instance._allocated_max_input_tokens` (Feature 006)
4. **Runtime-detected LLM limit** (line 874): `llm.generate_cfg['max_input_tokens']`
5. **Template static config** (line 888): `llm.cfg['generate_cfg']['max_input_tokens']`
6. **Default** (line 898): `DEFAULT_MAX_INPUT_TOKENS`

**Key insight:** Step 1 (API Router) is the **primary resolution path** for most configurations. When the router's endpoint config changes, the resolved value changes — but the cache still returns the old value.

### 4.1 `get_effective_max_tokens` (api_router.py, lines 661-691)

```python
def get_effective_max_tokens(self, agent_type: str) -> int:
    ep_limit = 0
    general_limit = 0
    with self._lock:
        defaults = self.default_llm_cfg or {}
        general_limit = defaults.get('max_input_tokens', 0)
        normalized_agent_type = self._normalize_agent_type(agent_type)
        for eid in self.agent_priorities.get(normalized_agent_type, []):
            ep = self.endpoints.get(eid)
            if ep and ep.enabled:
                ep_limit = ep.max_input_tokens
                break
    if ep_limit > 0:
        return ep_limit
    if general_limit > 0:
        return general_limit
    return 0
```

This reads live from `self.endpoints` and `self.agent_priorities` — both mutable at runtime.

---

## 5. How Endpoint Changes Happen at Runtime

### 5.1 API Endpoints (api_server.py)

| Endpoint | Method | Effect on Router |
|----------|--------|-----------------|
| `/api/endpoints` | POST | `add_endpoint()` — adds new endpoint |
| `/api/endpoints/{id}` | PUT | `update_endpoint()` — modifies existing |
| `/api/endpoints/{id}` | DELETE | `remove_endpoint()` — removes endpoint |
| `/api/endpoints/priorities` | POST | `set_agent_priorities()` — changes priority order |
| WebSocket `update_api_priorities` | — | `set_agent_priorities()` |

After each change, `build_state()` is called and broadcast to the frontend. However, `_max_tokens_cache` is NOT invalidated.

### 5.2 `call_with_fallback` (api_router.py, lines 821-977)

When an LLM call is made, `get_endpoint_chain()` is called to build the chain of endpoints to try. This can result in different endpoints being used for the same agent type at different times (failover). The `max_input_tokens` of the selected endpoint determines the actual limit.

---

## 6. Impact on Compression Checks

### 6.1 `_check_and_trigger_compression` (execution_engine.py, lines 1128-1181)

```python
def _check_and_trigger_compression(self, instance, messages, llm_messages, response=None) -> bool:
    max_tokens = self._get_max_tokens(instance)  # ← Calls _resolve_max_tokens
    # ...
    actual_tokens = instance._last_actual_token_count
    allocated_max = instance._allocated_max_input_tokens
    
    if actual_tokens > 0 and allocated_max > 0:
        current_tokens = actual_tokens
        max_tokens_for_check = allocated_max
    else:
        current_tokens = self._count_history_tokens(messages, instance)
        max_tokens_for_check = max_tokens  # ← Falls back to _resolve_max_tokens
    
    usage_pct = (current_tokens / max_tokens_for_check * 100) if max_tokens_for_check > 0 else 0
    
    if usage_pct > self.pool.settings.compression_force_threshold:
        return self._force_compression(...)
    
    if usage_pct > self.pool.settings.compression_warning_threshold:
        self._inject_compression_warning(...)
```

### 6.2 Staleness Impact Scenarios

| Scenario | Cached Value | Actual Value | Impact |
|----------|-------------|-------------|--------|
| Endpoint max increased (4096→8192) | 4096 | 8192 | **Premature compression** — triggers at ~50% of real limit |
| Endpoint max decreased (8192→4096) | 8192 | 4096 | **Missed compression** — allows context to overflow |
| Priority changed (Endpoint A→B) | A.max_tokens | B.max_tokens | Wrong limit entirely |
| Endpoint deleted | Old endpoint's value | New endpoint's value | Wrong limit entirely |

### 6.3 `_get_max_tokens` in ExecutionEngine (execution_engine.py, lines 2983-2990)

```python
def _get_max_tokens(self, instance: AgentInstance) -> int:
    from agent_cascade.api_integration import _resolve_max_tokens
    return _resolve_max_tokens(self.pool, instance)
```

This is **not cached** — it calls `_resolve_max_tokens` directly every time. So compression checks in the execution engine are **not affected** by the staleness bug. The bug only affects the **UI state** (build_state_from_pool, build_stream_update_from_pool, _serialize_instance).

---

## 7. Existing Endpoint Tracking Mechanisms

### 7.1 `_config_version` (agent_pool.py, lines 302-305, 1315-1321)

```python
self._config_version = 0  # Pool-level counter

def notify_config_changed(self):
    self._config_version += 1
```

This is incremented when workspace dir, templates, or extra folders change. It is **NOT** incremented when API endpoints change. It is used by ExecutionEngine to detect if system prompts need rebuilding.

### 7.2 `_last_successful_endpoint_cfg` (api_router.py, lines 503-506, 784-804)

```python
self._last_successful_endpoint_cfg: Optional[Dict[str, Any]] = None
```

This tracks the last successfully used endpoint config for fallback. It is updated on successful LLM calls (line 973). Not a version tracker.

### 7.3 `agent_priorities` (api_router.py, lines 583-601)

Mutable dict mapping agent_type → list of endpoint IDs. Changed via `set_agent_priorities()`.

### 7.4 `endpoints` (api_router.py, line 491)

Mutable dict mapping endpoint_id → APIEndpoint. Changed via `add_endpoint()`, `update_endpoint()`, `remove_endpoint()`.

---

## 8. Is `_resolve_max_tokens` Expensive?

Let me analyze the cost:

1. **Step 1 (API Router):** `get_effective_max_tokens()` — O(n) where n = number of endpoints in priority list (typically 1-3). Under lock.
2. **Step 2 (Per-instance override):** O(1) dict lookup.
3. **Step 2b (Allocated max):** O(1) attribute read.
4. **Step 3 (Runtime LLM limit):** Template lookup + nested dict access. O(1).
5. **Step 4 (Template static config):** Template lookup + nested dict access. O(1).

**Total cost:** ~5-10 dictionary lookups + 1 lock acquisition. This is **very cheap** — likely sub-microsecond. The original comment claiming "expensive lookups" is **misleading**.

---

## 9. Fix Approach Analysis

### Option A: Remove Caching Entirely

**Pros:**
- Zero staleness risk
- Simplest fix
- `_resolve_max_tokens` is cheap enough

**Cons:**
- Slightly more work on every state build (but negligible)
- Removes the optimization that was added in BUG31 Fix #2

**Verdict:** ✅ **Strong candidate.** The cost savings are negligible and the staleness risk is eliminated.

### Option B: Add Cache Invalidation on Endpoint Change

**Pros:**
- Preserves optimization
- Targeted fix

**Cons:**
- Requires hooking into ALL endpoint change paths (add, update, delete, priority change, default_llm_cfg change)
- Risk of missing a code path
- Adds complexity

**Implementation:** Add a `_notify_endpoint_config_changed()` method to APIRouter that calls `_invalidate_max_tokens_cache()`.

**Verdict:** ⚠️ Viable but fragile — requires careful integration at every mutation point.

### Option C: Per-Endpoint Cache with Validation

**Pros:**
- Preserves optimization with validation
- Cache key includes endpoint identity

**Cons:**
- More complex cache key management
- Need to track which endpoint is "active" per instance

**Verdict:** ❌ Overly complex for the marginal benefit.

### Option D: Hybrid — Cache with Validation Check

**Pros:**
- Preserves optimization
- Validates on each access
- Self-healing

**Cons:**
- Still has a brief window of staleness between endpoint change and next validation

**Verdict:** ⚠️ Acceptable but adds unnecessary complexity.

---

## 10. Recommendation

### **Recommended Fix: Option A (Remove Caching) + Option B (Hybrid Invalidation)**

**Primary fix:** Remove the `_max_tokens_cache` entirely. The `_resolve_max_tokens` function is cheap enough that caching provides negligible benefit while introducing a critical staleness bug.

**Secondary safeguard:** Add a `_config_version`-style counter to the APIRouter that increments on any endpoint/priority change. This can be used for future cache invalidation if needed.

### Implementation Plan

#### Step 1: Remove `_max_tokens_cache` usage (api_integration.py)

Replace all three cache patterns:
```python
# BEFORE (lines 472-474, 659-661, 1184-1186)
if instance_name not in _max_tokens_cache:
    _max_tokens_cache[instance_name] = _get_max_tokens_for_instance(pool, instance)
max_tokens = _max_tokens_cache[instance_name]

# AFTER
max_tokens = _get_max_tokens_for_instance(pool, instance)
```

#### Step 2: Remove cache definition and clear function (api_integration.py)

- Remove `_max_tokens_cache: Dict[str, int] = {}` (line 43)
- Remove `_max_tokens_cache.clear()` from `_clear_performance_caches()` (line 66)
- Remove `global _max_tokens_cache` from `_clear_performance_caches()` (line 64)

#### Step 3: Add APIRouter config version (api_router.py)

Add to `APIRouter.__init__`:
```python
self._endpoint_config_version = 0
```

Increment in all mutation methods:
- `add_endpoint()` (line 531)
- `remove_endpoint()` (line 557)
- `update_endpoint()` (line 569)
- `set_agent_priorities()` (line 601)

Add a method:
```python
def get_endpoint_config_version(self) -> int:
    with self._lock:
        return self._endpoint_config_version
```

#### Step 4: (Optional) Add invalidation hook

If caching is re-added in the future, the `_endpoint_config_version` provides a clean invalidation mechanism.

---

## 11. Files to Modify

| File | Lines | Changes |
|------|-------|---------|
| `agent_cascade/api_integration.py` | 43 | Remove `_max_tokens_cache` definition |
| `agent_cascade/api_integration.py` | 64-66 | Remove `_max_tokens_cache` from `_clear_performance_caches()` |
| `agent_cascade/api_integration.py` | 472-474 | Remove cache pattern in `build_state_from_pool` |
| `agent_cascade/api_integration.py` | 659-661 | Remove cache pattern in `build_stream_update_from_pool` |
| `agent_cascade/api_integration.py` | 1184-1186 | Remove cache pattern in `_serialize_instance` |
| `agent_cascade/api_router.py` | ~496 | Add `_endpoint_config_version = 0` |
| `agent_cascade/api_router.py` | 531, 557, 569, 601 | Increment `_endpoint_config_version` |
| `agent_cascade/api_router.py` | +10 | Add `get_endpoint_config_version()` method |

---

## 12. Testing Recommendations

1. **Unit test:** Verify `_resolve_max_tokens` returns correct values after endpoint changes
2. **Integration test:** Simulate endpoint update → verify `build_state_from_pool` returns updated `max_tokens`
3. **Compression test:** Verify compression triggers at correct thresholds after endpoint changes
4. **Perf test:** Verify no measurable performance regression from removing cache

---

## 13. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Performance regression | Very Low | Low | `_resolve_max_tokens` is sub-microsecond |
| Missing invalidation path | N/A (no invalidation needed) | N/A | — |
| Breaking changes | Low | Low | Only internal cache removed; public API unchanged |

---

*End of Research Report*