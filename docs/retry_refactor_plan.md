# API Retry/Fallback Refactoring Plan

**Status:** Proposed (v4 — clean break, no backwards compat)  
**Author:** planner_retry_refactor → updater_retry_plan → updater_retry_plan_final  
**Date:** 2026-07-28  
**Working Dir:** N:\work\WD\AgentCascade

**Changes in v4:** Clean break with new naming and behavior — removed all backwards compatibility baggage (no aliases, no deprecation warnings, no preserving old config formats). Fixed off-by-one bug in pseudocode (`retry_count <= _loop_max` → `retry_count < _loop_max`). Added concrete dual counter example. Aligned terminology (`max_attempts` → `retry_max_attempts` everywhere). Added per-agent budget isolation principle. Removed deprecated function retention language and deprecation timeline. Updated LLM_MAX_RETRIES handling (ignored, no warning). Updated custom backoff handling (centralized policy only). Fixed error message in Phase 5 pseudocode.

---

## Problem Summary

Three retry layers with overlapping responsibilities and a hidden coupling:

| Layer | Location | Default Retries | Backoff Cap | Current Role |
|-------|----------|-----------------|-------------|--------------|
| L1 (LLM base) | `llm/base.py` | 2 (`max_retries=2` in BaseChatModel init) | 300s | Retry individual HTTP calls; catches only `ModelServiceError` |
| L2 (API router) | `api_router.py` | 2 per endpoint | 30s | Multi-endpoint failover chain; retries each endpoint then moves to next |
| L3 (Execution engine) | `execution_engine.py` | 2 (`loop_max_retries`) | 5s | Outer loop with error classification, UI feedback, telemetry |

**Hidden coupling:** L2's `APIEndpoint.max_retries` is passed into `generate_cfg['max_retries']` via `to_llm_cfg()`, which sets `BaseChatModel.max_retries`. So endpoint-level retry config controls BOTH L2 and L1 behavior simultaneously.

**Key issues:**
- Worst-case total attempts: L3(3 iterations) × endpoints(N) × L2(3 attempts/endpoint) × L1(3 attempts/call). With even 2 endpoints, that's up to 54 HTTP calls for one LLM request. In practice fewer because L1 only catches `ModelServiceError`, but still excessive and hard to reason about.
- Three separate exponential backoff implementations with conflicting caps (300s / 30s / 5s)
- `_execute_llm_call_with_retry` is ~500 lines doing too much
- Error classification scattered: L1 (`_raise_or_delay`) vs L3 (`_classify_llm_error`)

---

## 1. Proposed Architecture

### Design Goal: Two Layers, Clear Responsibilities

**Layer A — Execution Engine (Outer):** Retry orchestration with UI feedback and telemetry  
**Layer B — API Router (Inner):** Endpoint selection + per-endpoint retry + failover chain

| Concern | Layer A (Execution Engine) | Layer B (API Router) |
|---------|---------------------------|----------------------|
| **Retry loop** | YES — outer budget enforcement | YES — per-endpoint only |
| **Error classification** | YES — single source of truth | NO — pass errors through |
| **Backoff timing** | YES — between full failover attempts | YES — between retries on same endpoint |
| **UI retry messages** | YES | NO |
| **Telemetry hooks** | YES | NO (router logs only) |
| **Endpoint cursor management** | NO | YES |
| **Rate limiting** | NO | YES |
| **Inner-loop detection** | YES | NO — just passes `CharacterRunDetected`/`MaxTokenExceeded` through |
| **Image captioning** | YES (before first attempt) | NO |

### Flow Diagram

```
ExecutionEngine._execute_llm_call_with_retry()    [Layer A]
│   └─ retry loop: budget = pool.settings.retry_max_attempts (default 3)
│       ├─ classify error → fatal? → fail immediately
│       ├─ UI message + telemetry
│       ├─ backoff sleep (engine-level)
│       │
│       └─ api_router.call_with_fallback()        [Layer B]
│           ├─ for each endpoint in chain:
│           │   ├─ per-endpoint retry loop: budget = endpoint.max_retries (default 1)
│           │   │   ├─ rate limit check
│           │   │   ├─ execute call
│           │   │   ├─ if CharacterRunDetected/MaxTokenExceeded → skip to next endpoint immediately
│           │   │   ├─ if rate limit hit → skip to next endpoint immediately
│           │   │   └─ other errors → retry with backoff (router-level)
│           │   └─ advance cursor on character-run detection
│           └─ raise RuntimeError("All endpoints exhausted") if chain fails
│
└─ yield final messages or error message
```

### Total Retry Budget

**Definition:** "Total attempts" = total number of times `call_with_fallback()` is invoked by the execution engine. Each invocation walks the endpoint chain until one succeeds or all are exhausted.

- **Default: 3 outer attempts** (execution engine loop iterations)
- **Per-agent budget isolation:** Each agent instance has its own independent retry budget. When a parent delegates to a sub-agent, the sub-agent's LLM calls consume from its own budget, not the parent's. Parallel sub-agents each get their full budget. This prevents one agent's failures from starving others and simplifies reasoning about retry behavior in nested/parallel execution scenarios.
- Per-attempt behavior: API router walks the endpoint chain; each endpoint gets 1 retry before moving to next
- Example with 2 endpoints, default policy:
  - Attempt 1: Endpoint A (try + 1 retry if needed), then Endpoint B (try + 1 retry)
  - Attempt 2: Same chain again (cursor may have advanced due to inner-loop detection)
  - Attempt 3: Same chain again → give up after this
- **Maximum HTTP calls in worst case:** engine_attempts × endpoints × (1 + endpoint_max_retries). With defaults: 3 × 5 × 2 = 30. Still high but bounded and intentional. Users with many endpoints should reduce `retry_max_attempts`.
- **Configurable** via UI settings: `retry_max_attempts` in [1, 6], `endpoint_max_retries` in [0, 2]

---

## 2. Retry Policy Design

### Centralized Configuration

Create `agent_cascade/retry_policy.py` — single source of truth for all retry behavior:

```python
# agent_cascade/retry_policy.py

from dataclasses import dataclass
from typing import Type

@dataclass(frozen=True)
class RetryPolicy:
    """Centralized retry configuration for LLM calls.
    
    Applied at the execution engine level (outer loop). The API router
    uses per-endpoint settings that are constrained by this policy.
    """
    # Total retry budget across all layers and endpoints
    retry_max_attempts: int = 3        # Range [1, 6] — total outer attempts
    
    # Backoff parameters (used by both engine and router)
    base_delay: float = 1.0            # Seconds
    max_delay: float = 8.0             # Cap for exponential backoff
                                     # Rationale: Current L2 cap is 30s but L3 uses 5s via LLM_RETRY_MAX_BACKOFF.
                                     # 8s is a compromise — long enough to give transient failures time to clear,
                                     # short enough to avoid users staring at retries for minutes. With 3 engine
                                     # attempts and exponential growth (1→2→4→8), total retry wait maxes at ~15s.
    jitter_factor: float = 0.1         # ±10% randomization
    
    # Per-endpoint retry limit (used by API router inner loop)
    endpoint_max_retries: int = 1      # Retries per endpoint before failover
    
    # Error classification
    fatal_patterns: tuple = (...)      # Non-retryable error substrings
    retryable_patterns: tuple = (...)  # Transient error substrings


# Predefined policies for different scenarios

POLICY_DEFAULT = RetryPolicy(
    retry_max_attempts=3,
    base_delay=1.0,
    max_delay=8.0,
    endpoint_max_retries=1,
)

POLICY_AGGRESSIVE = RetryPolicy(
    retry_max_attempts=5,
    base_delay=0.5,
    max_delay=4.0,
    endpoint_max_retries=1,
)

POLICY_CONSERVATIVE = RetryPolicy(
    retry_max_attempts=2,
    base_delay=2.0,
    max_delay=10.0,
    endpoint_max_retries=0,  # No per-endpoint retries; failover immediately
)
```

### Error Classification (Single Source of Truth)

Move error classification to `retry_policy.py`:

```python
def classify_error(error: Exception) -> str:
    """Classify an error as 'fatal', 'retryable', or 'unknown'.
    
    Returns:
        'fatal' — do not retry (auth, quota, config errors)
        'retryable' — transient, safe to retry (network, timeout, 5xx)
        'unknown' — default to retryable for safety
    """
```

This replaces:
- `_classify_llm_error` in `execution_engine.py`
- Error handling logic in `_raise_or_delay` in `llm/base.py`

### Backoff Calculation (Reusable Utility)

```python
def calculate_backoff(attempt: int, policy: RetryPolicy) -> float:
    """Calculate backoff delay with exponential growth and jitter."""
    raw = policy.base_delay * (2 ** (attempt - 1))
    jitter = random.uniform(-policy.jitter_factor, policy.jitter_factor) * raw
    return min(max(raw + jitter, 0.1), policy.max_delay)
```

---

## 3. Implementation Steps

### Phase 1: Foundation — Retry Policy Module (Low Risk)

**Files:**
- `agent_cascade/retry_policy.py` (NEW)
- `agent_cascade/execution_engine.py`
- `agent_cascade/settings.py`

**Changes:**
1. Create `retry_policy.py` with `RetryPolicy` dataclass, `classify_error()`, `calculate_backoff()`
2. Move error classification patterns from `execution_engine.py._classify_llm_error()` into `retry_policy.py`
3. Update `Settings` dataclass in `settings.py` to include retry policy fields:
   - `retry_max_attempts: int = 3` (new name, replaces old `loop_max_retries`)
   - `retry_base_delay: float = 1.0`
   - `retry_max_delay: float = 8.0`
4. Add config handler in `config_handlers.py` for new settings

**Testing:** Unit tests for `classify_error()` and `calculate_backoff()`. No runtime behavior changes yet.

---

### Phase 2: Decouple and Disable Layer 1 Retries (Medium Risk)

**Files:**
- `agent_cascade/llm/base.py`
- `agent_cascade/api_router.py`
- `agent_cascade/settings.py`

**Changes:**
1. **Break the coupling:** Remove `'max_retries'` from `APIEndpoint.to_llm_cfg()` output. Endpoint retry config should only control L2 (API router), not leak into L1.
2. **Bypass L1 retry wrappers in `chat()`** (CRITICAL — do NOT just set max_retries=0):
   - In `BaseChatModel.chat()`, replace the wrapper calls with direct calls:
     ```python
     # Before (lines 358-361):
     if stream and delta_stream:
         output = _call_model_service()
     elif stream and (not delta_stream):
         output = retry_model_service_iterator(_call_model_service, max_retries=self.max_retries)
     else:
         output = retry_model_service(_call_model_service, max_retries=self.max_retries)
     
     # After:
     if stream:
         output = _call_model_service()  # Direct call — no wrapper
     else:
         output = _call_model_service()  # Same for non-streaming
     ```
   - **Why not just set max_retries=0?** The wrapper `retry_model_service_iterator()` catches generic `Exception` and re-wraps it as `ModelServiceError`, corrupting error types. This would break execution engine's `_classify_llm_error()` which relies on original exception types to decide retry vs fatal.
3. **Bypass L1 retry wrappers in `raw_chat()`** (CRITICAL for DashScope users):
   - In `BaseChatModel.raw_chat()`, replace the wrapper call with direct call:
     ```python
     # Before (lines 549-552):
     if stream:
         def _chat_stream_with_retry():
             return self._chat_stream(messages=messages, delta_stream=False, generate_cfg=generate_cfg)
         
         output_iter = retry_model_service_iterator(_chat_stream_with_retry, max_retries=self.max_retries)
     
     # After:
     if stream:
         output_iter = self._chat_stream(messages=messages, delta_stream=False, generate_cfg=generate_cfg)
     ```
   - **Impact:** This affects DashScope models which use `use_raw_api=True` by default (~50%+ of production users). Without this fix, they'd experience the same error-corruption bug.
4. Set `self.max_retries = 0` defensively (no-op now, but documents intent).
5. **Clean up:** Remove `retry_model_service()`, `retry_model_service_iterator()`, `_raise_or_delay()` — they are no longer called after Phase 2 bypasses them in chat() and raw_chat().
6. **Update env var docs:** Note that `LLM_MAX_RETRIES` is ignored; retry logic lives in execution engine + API router. Runtime behavior: `config_handlers.py` silently ignores `LLM_MAX_RETRIES` — no warning logged, no configuration applied.

**Rationale:** 
- L1 catches only `ModelServiceError` with a 300s backoff cap — too aggressive when L2/L3 exist above it
- The coupling (endpoint.max_retries → L1) makes behavior confusing: changing endpoint retry count silently affects two layers
- API router already handles per-endpoint retries with smarter failover; L1 adds no unique value

**Risk mitigation:** 
- Bypass wrappers entirely in BOTH `chat()` and `raw_chat()` rather than relying on max_retries=0 semantics (which corrupt error types)
- Remove unused wrapper functions after confirming no external callers exist (Phase 0 audit step)
- Add a comment explaining why L1 is disabled so future devs don't re-enable it accidentally

**Testing:** 
- Verify streaming still works without L1 retry wrapper in both `chat()` and `raw_chat()` paths
- Verify errors propagate correctly to L2 (API router) and L3 (execution engine) with ORIGINAL exception types preserved
- Verify endpoint-level `max_retries` config still controls L2 behavior after uncoupling
- **Specific test:** Create endpoint with `max_retries=3`, simulate transient failures, verify exactly 3 L2 retries occur with correct error types reaching execution engine's `_classify_llm_error()`
- **Specific test for raw_chat:** Use a DashScope model with `use_raw_api=True`, simulate failure, verify error type preserved and L2 retry kicks in correctly

---

### Phase 3: Simplify API Router Retry (Medium Risk)

**Files:**
- `agent_cascade/api_router.py`
- `agent_cascade/retry_policy.py`

**Changes:**
1. Import `calculate_backoff()` from `retry_policy.py`; use it instead of inline backoff in `call_with_fallback()`
2. Reduce default `max_retries` in `APIEndpoint` dataclass from 2 to 1 (single retry per endpoint before failover)
3. **Use centralized backoff policy:** Remove `base_retry_delay` and `max_retry_delay` fields from `APIEndpoint`. All endpoints use the centralized policy values (`retry_policy.base_delay`, `retry_policy.max_delay`). This simplifies configuration and ensures consistent behavior across endpoints.
4. Clean up `call_with_fallback()`: ensure it passes all errors through to the execution engine for classification, only making local decisions for rate limits and character-run detection

**Testing:** 
- Verify failover chain still works; verify per-endpoint retry count is respected; verify rate limit skip behavior unchanged
- Verify backoff timing matches centralized policy values across all endpoints

---

### Phase 4: Refactor Execution Engine Retry Loop (High Risk)

**Files:**
- `agent_cascade/execution_engine.py`

**Changes:**

Phase 4 is split into sub-phases, each independently testable:

#### 4a: Extract Error Classification and Backoff
1. Replace inline error classification with call to `retry_policy.classify_error()`
2. Replace inline backoff calculation with `retry_policy.calculate_backoff()`
3. No behavioral change — just replace the code with calls to shared utilities

#### 4b: Extract Inner-Loop Detection Handling
4. Extract the inner-loop detection + `_abort_stream` logic into `_handle_inner_loop_detection(instance, event, text, ...)` 
5. This method handles: aborting stream, incrementing counters, yielding UI signal, checking loop budget exhaustion
6. **Dual counter semantics:** Keep separate `retry_count` (general errors) and `loop_retry_count` (inner-loop detection). Both counters share the same budget from `retry_max_attempts`. They are NOT independent budgets — they track different error categories against a single pool:
   - `retry_count`: incremented for general transient errors (network, timeout, 5xx)
   - `loop_retry_count`: incremented specifically for inner-loop detection events (CharacterRunDetected, MaxTokenExceeded)
   - **Budget check:** The while loop condition is `while retry_count < _loop_max` where `_loop_max = retry_max_attempts`. Both counters increment the SAME logical budget — when EITHER type of error causes a retry, it consumes one attempt from the shared pool. The dual counter exists purely for observability (telemetry distinguishes "retried due to transient error" vs "retried due to inner-loop detection") and because inner-loop retries trigger additional side effects (endpoint cursor advancement).
   - **Mapping:** `retry_max_attempts = 3` means up to 3 total retry attempts regardless of error type. If all 3 are consumed by general errors, loop_retry_count is irrelevant; if all 3 are consumed by inner-loop detection, same result. The two counters together must not exceed `retry_max_attempts`.
   - **Concrete example with mixed errors (retry_max_attempts = 3):**
     - Attempt 1: Transient network error → `retry_count` becomes 1, `loop_retry_count` stays 0 → retry allowed (1 < 3)
     - Attempt 2: Inner-loop detection → `retry_count` becomes 2, `loop_retry_count` becomes 1 → retry allowed (2 < 3)
     - Attempt 3: Transient timeout → `retry_count` becomes 3, `loop_retry_count` stays 1 → loop exits (3 is not < 3), raise "Max attempts exceeded"
     - Result: 3 total attempts consumed; counters show 2 general errors + 1 inner-loop detection. Budget exhausted.

#### 4c: Extract Telemetry Spans
7. Wrap telemetry calls in helper `_record_telemetry_event(inst_name, event_type, **kwargs)` to reduce inline noise
8. No behavioral change — same events recorded at same points

#### 4d: Rename and Wire Settings
9. Rename `_loop_max` → `_max_attempts` and source from `pool.settings.retry_max_attempts`

**Key constraint:** Do NOT change the retry loop semantics — same number of retries, same error handling decisions, same dual-counter behavior, just cleaner code structure through extraction.

**Goal:** Reduce `_execute_llm_call_with_retry` from ~500 lines to ~250-300 lines through method extraction only (no logic changes).

**Testing after each sub-phase (Golden Master approach):**
Before starting Phase 4, capture "golden master" baselines from current code: exact retry counts, error messages, endpoint advance behavior, and telemetry events for key scenarios. After each sub-phase, run the same scenarios and assert outputs match exactly — any deviation is a regression until proven intentional.

Golden master scenarios to capture:
- Transient error on attempt 1 → success on attempt 2: record retry count, backoff delays (approximate), UI messages, telemetry events
- Fatal error on attempt 1: record that no retry occurred, exact error message format
- Inner-loop detection with 3 endpoints: record which endpoint cursor advances to, retry consumed from which counter
- Full exhaustion after N attempts: record final error message, total HTTP calls made

Sub-phase verification:
- 4a: All existing tests pass; golden master scenarios produce identical classification results and backoff behavior
- 4b: Golden master scenarios for inner-loop detection match exactly (same endpoint advances, same counter decrements)
- 4c: Golden master telemetry events match exactly (same event types, same sequence, same payloads)
- 4d: Golden master retry counts match; settings-driven retry count works with new `retry_max_attempts` field

**Full integration tests after Phase 4:**
- Successful call on first attempt
- Retry after transient error
- Fail after max attempts exceeded  
- Fatal error fails immediately without retry
- Inner-loop detection triggers endpoint advance and retry
- UI retry messages appear correctly with accurate counts

---

### Phase 5: Wire Policy into API Router (Medium Risk)

**Files:**
- `agent_cascade/api_router.py`
- `agent_cascade/execution_engine.py`
- `agent_cascade/agent_pool.py` (or wherever policy is resolved)

**Changes:**
1. Pass `RetryPolicy` instance to `APIRouter.__init__()` or resolve from pool settings on demand
2. Use policy's `endpoint_max_retries` as the default for endpoints that don't specify their own
3. **Endpoint's explicit max_retries always takes precedence over policy default.** If an endpoint has `max_retries` explicitly set (non-zero), use that value. If the endpoint doesn't set `max_retries`, fall back to `policy.endpoint_max_retries`. This ensures user-configured endpoint settings are honored while new endpoints get sensible defaults from the global policy.

**Budget Enforcement Location:**
The outer retry budget is enforced in `execution_engine.py` inside `_execute_llm_call_with_retry()`. The API router does NOT enforce total budget — it only handles per-endpoint retries within each invocation. Here is the pseudocode showing where and how:

```python
# execution_engine.py - _execute_llm_call_with_retry() [Layer A]

def _execute_llm_call_with_retry(self, instance, llm_messages, template, active_functions):
    retry_count = 0              # General transient error retries
    loop_retry_count = 0         # Inner-loop detection retries
    _loop_max = pool.settings.retry_max_attempts  # Budget from policy (default: 3)

    while retry_count < _loop_max:        # <-- OUTER RETRY LOOP LIVES HERE
        try:
            # Call API router — it walks endpoint chain, handles per-endpoint retries
            gen = self._execute_llm_call(instance, template, llm_messages, active_functions)
            
            for event in gen:
                if is_inner_loop_detected(event):
                    raise CharacterRunDetected(...)  # Let outer loop handle retry
                yield event
            
            return  # Success — exit retry loop
        
        except FatalError:
            raise          # No retry for fatal errors
        
        except (CharacterRunDetected, MaxTokenExceeded) as e:
            # Inner-loop detection: advance endpoint cursor, consume budget
            loop_retry_count += 1
            retry_count += 1         # Both counters increment shared budget
            if retry_count >= _loop_max:
                raise RuntimeError("Max attempts exceeded")
            yield RETRY_MESSAGE(...)
            sleep(calculate_backoff(retry_count, policy))
            continue               # Retry with advanced cursor
        
        except TransientError as e:
            retry_count += 1
            if retry_count >= _loop_max:
                raise RuntimeError("Max attempts exceeded after {} attempts".format(_loop_max))
            yield RETRY_MESSAGE(...)
            sleep(calculate_backoff(retry_count, policy))
            continue               # Retry from top of loop
    
    raise RuntimeError("Unexpected exhaustion")
```

Key points:
- **Line `while retry_count < _loop_max`:** This is the budget enforcement gate. `_loop_max` comes directly from `pool.settings.retry_max_attempts`. With default value 3, this allows exactly 3 iterations (retry_count = 0, 1, 2). When retry_count reaches 3, the loop exits and "Max attempts exceeded" is raised.
- **Each loop iteration calls `_execute_llm_call()` → `api_router.call_with_fallback()`:** The router walks its endpoint chain once per outer loop iteration.
- **Router does NOT know about total budget:** It only knows per-endpoint retry limits (`endpoint.max_retries` or `policy.endpoint_max_retries`). Budget enforcement is exclusively the engine's responsibility.

**Testing:** Verify total attempt count stays within budget across multi-endpoint chains (engine loop limit × endpoint chain traversal).

---

### Phase 6: UI/Config Updates (Low Risk)

**Files:**
- `agent_cascade/api_integration.py`
- `agent_cascade/api_server.py`
- Web UI settings panel (if applicable)

**Changes:**
1. Update settings API to expose new retry policy fields (`retry_max_attempts`, `endpoint_max_retries`, `retry_base_delay`, `retry_max_delay`)
2. Use new field name `retry_max_attempts` in UI labels (no backwards compat for old config)
3. Add validation: `retry_max_attempts` must be in [1, 6]
4. Add validation: `endpoint_max_retries` must be in [0, 2]. Value 0 means no per-endpoint retries (failover immediately); value 2 is the maximum to prevent excessive attempts on a single failing endpoint before failover.
5. Document the new retry budget concept clearly

---

### Phase 0: Audit Current Behavior (MANDATORY, Low Risk)

**Goal:** Establish a baseline before changing anything. Without this, we cannot prove no regressions.

**Actions:**
1. Write integration tests that measure actual retry counts under various failure scenarios:
   - Single endpoint failure → count total HTTP calls made
   - Multi-endpoint chain with first endpoint failing → verify failover behavior  
   - Inner-loop detection → verify endpoint advance and retry count
   - **DashScope/raw_chat path:** Verify error types preserved through `raw_chat()` streaming wrapper
2. Document the exact retry math for current code (for reference during migration)
3. Check existing endpoint configs: how many endpoints have custom `max_retries` values? (Note: `base_retry_delay` and `max_retry_delay` will be removed in Phase 3 — all endpoints use centralized policy.)
4. **Audit all direct LLM implementation calls:** Search entire codebase for any direct calls to `retry_model_service*` functions outside of `BaseChatModel.chat()` and `BaseChatModel.raw_chat()`. Use grep: `grep -rn "retry_model_service" agent_cascade/`. Any callers found must be updated or accounted for in Phase 2.
5. **Performance baseline benchmark:** Measure latency with current retry configuration using a controlled test (e.g., simulate transient failures that trigger retries, measure total wall-clock time). Record: average latency for successful calls with 0 retries, 1 retry, and max retries. This provides a comparison point after refactoring to verify we haven't inadvertently increased wait times.
6. **Enumerate all BaseChatModel subclasses:** Search codebase for all classes inheriting from `BaseChatModel` (grep: `grep -rn "class.*BaseChatModel" agent_cascade/`). For each subclass, verify which API path it uses: `chat()` or `raw_chat()`. Document findings to ensure Phase 2 changes cover all code paths. Pay special attention to non-OpenAI providers (DashScope, Ollama, local models) as they may use different paths.
7. **Audit all direct `.chat(` and `.raw_chat(` calls outside execution_engine.py:** Search entire codebase for direct invocations of these methods on BaseChatModel instances (grep: `grep -rn "\.chat(" agent_cascade/` and `grep -rn "\.raw_chat(" agent_cascade/`). For each caller found outside `execution_engine.py`, verify that it routes through the execution engine's retry path rather than calling LLM implementations directly. Any direct calls bypassing the execution engine must be flagged for refactoring or documented as intentional exceptions (e.g., internal tooling, health checks). This ensures Phase 2 changes to `chat()` and `raw_chat()` do not break callers that depend on specific error-handling behavior.

**Output:** A "current behavior" test suite that MUST pass before AND after refactoring. This is our regression safety net. No production changes proceed without Phase 0 tests in CI.

---

### Phase Dependencies

```
Phase 0 (Audit) — MANDATORY, must pass before any production changes
    ↓
Phase 1 (Foundation)
    ↓
Phase 2 (Decouple L1) ──→ Phase 3 (Simplify Router)
    ↓                          ↓
Phase 4a-4d (Refactor Engine) ←── Phase 5 (Wire Policy)
    ↓
Phase 6 (UI/Config)
```

Phases 2 and 3 can run in parallel after Phase 1. Phase 4 depends on both being complete. Phase 5 can overlap with Phase 4b+. Phase 0 tests must be green throughout all phases.

---

## 4. Migration Strategy

### New Configuration Fields

| Field | Type | Default | Range | Description |
|-------|------|---------|-------|-------------|
| `retry_max_attempts` | int | 3 | [1, 6] | Total outer retry attempts (execution engine budget) |
| `endpoint_max_retries` | int | 1 | [0, 2] | Retries per endpoint before failover |
| `retry_base_delay` | float | 1.0 | > 0 | Base delay for exponential backoff (seconds) |
| `retry_max_delay` | float | 8.0 | > 0 | Maximum backoff cap (seconds) |

### Breaking Changes

This is a clean break — old field names are not supported:

1. **`loop_max_retries` removed:** Use `retry_max_attempts` instead. Old config files using this field will fail to load; users must update their configs.
2. **`LLM_MAX_RETRIES` env var ignored:** Retry logic now lives in execution engine + API router via settings. This env var is silently ignored.
3. **Custom per-endpoint backoff caps removed:** `base_retry_delay` and `max_retry_delay` fields on `APIEndpoint` are removed. All endpoints use centralized policy values.
4. **L1 retry helpers removed:** `retry_model_service()`, `retry_model_service_iterator()`, `_raise_or_delay()` are deleted — they are no longer called after Phase 2 bypasses them.

### Non-Breaking Aspects

- **Function signatures unchanged:** `_execute_llm_call_with_retry()` and `call_with_fallback()` retain their signatures.
- **Endpoint max_retries field retained:** After uncoupling in Phase 2, `APIEndpoint.max_retries` controls ONLY API router retries (not L1). Default changes from 2 to 1. Existing custom values preserved.

### Transition Notes

- **Phase 0 (Audit):** MANDATORY first step — establish baseline tests before any changes
- **Phases 1-3:** User-visible behavior change: fewer total retries (L1 removed), shorter backoff waits. Intentional and desirable.
- **Phase 4:** No user-visible behavior change; cleaner code only
- **Phases 5-6:** Policy enforcement and UI updates

**Release note for Phases 1-3 (intentional behavior change):** 
"Reduced redundant retry attempts across LLM call layers. Previously, up to three nested retry layers could compound into dozens of HTTP calls for a single request. The new design uses two layers with a configurable total budget (default: 3 outer attempts). This is an intentional reduction in total retries — designed to reduce wait times on transient failures without affecting success rates. Users who relied on aggressive retrying can increase `retry_max_attempts` in settings."

**Rationale for explicit language:** Reduced retries may cause some users to see more visible errors if they were depending on the old behavior where extreme retry counts masked intermittent issues. Being upfront avoids confusion and support tickets.

---

## 5. Testing Strategy

### After Each Phase

**Phase 1:**
- [ ] `test_classify_error()` — verify all error patterns classified correctly
- [ ] `test_calculate_backoff()` — verify exponential growth, jitter, cap behavior
- [ ] Existing test suite passes unchanged

**Phase 2:**
- [ ] Streaming LLM calls still work without L1 retry wrapper
- [ ] Errors propagate to API router correctly
- [ ] No regression in successful call paths

**Phase 3:**
- [ ] Failover chain works: endpoint A fails → endpoint B succeeds
- [ ] Per-endpoint retry count respected (1 retry, then failover)
- [ ] Rate limit skip behavior unchanged
- [ ] Character-run detection causes immediate endpoint skip

**Phase 4:**
- [ ] Total retry count matches `retry_max_attempts` setting
- [ ] Fatal errors fail immediately (no retry)
- [ ] Transient errors trigger retries with correct backoff
- [ ] Inner-loop detection + endpoint advance works
- [ ] UI retry messages appear with correct attempt counts
- [ ] Telemetry events recorded correctly

**Phase 5:**
- [ ] Multi-endpoint chain respects total budget (no more than N attempts across all endpoints)
- [ ] Policy overrides work correctly
- [ ] **Sub-agent LLM calls — nested sub-agents (parent → child → grandchild):** Create scenario where parent delegates to child agent, which delegates to grandchild agent. Grandchild's LLM call triggers transient failure → verify retry applies at grandchild level, success propagates back up chain. Also test: grandchild exhausts retries → error propagates through child to parent with correct type preserved.
- [ ] **Sub-agent LLM calls — parallel sub-agent invocations:** Parent agent spawns multiple sub-agents concurrently (e.g., researcher + coder in parallel). One sub-agent's LLM call triggers retry while others succeed. Verify: retry budget is per-agent-instance (not shared across parallel agents), retries don't block other agents, all results aggregate correctly at parent.
- [ ] **Sub-agent LLM calls — error propagation across boundaries:** Sub-agent's LLM call fails with fatal error (e.g., auth failure). Verify: no retries attempted, error propagates to parent with original exception type intact, parent can classify and handle appropriately (e.g., log as config error vs transient), retry budget not consumed for fatal errors at any level.

**Phase 6:**
- [ ] Settings API round-trips correctly for new fields (`retry_max_attempts`, `endpoint_max_retries`, `retry_base_delay`, `retry_max_delay`)
- [ ] Validation rejects out-of-range values

### Performance Regression Threshold

**Post-refactoring latency must be within 10% of baseline for equivalent retry scenarios.** Any regression >10% requires investigation before proceeding. Baseline is established in Phase 0 audit (performance benchmark step). This applies to:
- Successful calls with 0 retries
- Calls requiring 1+ retries due to transient errors
- Full failover chain traversals

Rationale: The refactoring should not introduce measurable overhead. If latency increases beyond 10%, it indicates either a bug in the new code paths or an unintended algorithmic change that must be corrected.

### Key Scenarios to Verify

1. **Happy path:** Single endpoint, succeeds on first try → 0 retries, no backoff
2. **Transient failure recovery:** Network error → retry → success → 1 retry consumed
3. **Full exhaustion:** All attempts fail with transient errors → clear error message after N retries
4. **Fatal error:** Auth failure → immediate failure, no retries wasted
5. **Failover chain:** Endpoint A down → endpoint B succeeds
6. **Inner-loop recovery:** Character run detected → advance cursor → retry on different endpoint → success
7. **Rate limit handling:** Rate limit hit → skip to next endpoint immediately (no retries)
8. **Sub-agent LLM call with retry:** Parent agent delegates to sub-agent; sub-agent's LLM call experiences transient failure → verify retry applies, error propagates correctly to parent

---

## 6. Things NOT Changing

To keep scope bounded, these areas are explicitly OUT of scope:

1. **Streaming behavior:** No changes to how streaming works, chunk delivery, or UI updates beyond retry messages
2. **Endpoint configuration format:** `APIEndpoint` dataclass retains core fields (`max_retries`, etc.); backoff fields (`base_retry_delay`, `max_retry_delay`) are removed in Phase 3
3. **Inner-loop detection algorithm:** Detection logic unchanged; only its integration with retry loop is cleaned up
4. **Telemetry event schema:** Same events, same fields — just called from cleaner code paths
5. **Error message format:** UI-facing messages like `[RETRYING]` and `[SYSTEM ERROR:]` keep their current format
6. **HTTP client layer:** No changes to underlying HTTP libraries or connection pooling
7. **Concurrent agent scheduling:** EndpointScheduler and concurrency limits unchanged
8. **Image captioning flow:** Still happens before first LLM call, same behavior

---

## Risk Assessment

| Phase | Risk Level | Mitigation |
|-------|------------|------------|
| 0 (Audit) | None | Pure test addition; no production code changed |
| 1 (Foundation) | Low | Pure addition; no behavior change |
| 2 (Decouple L1) | Medium | Changes retry behavior (removes L1); audit for external callers before removing unused functions; verify error propagation to L2/L3 |
| 3 (Simplify Router) | Medium | Core failover path; test endpoint chain thoroughly with centralized backoff policy |
| 4a-4d (Refactor Engine) | High | Touches the hot path for every LLM call; split into sub-phases each independently testable; extract only, no logic changes |
| 5 (Wire Policy) | Medium | Cross-cutting change; verify budget enforcement with integration tests |
| 6 (UI/Config) | Low | Surface-level changes; new config fields only |

---

## Success Criteria

After all phases complete:

1. **Single retry policy module** — `retry_policy.py` is the source of truth for backoff and classification
2. **Two clear layers** — execution engine orchestrates, API router fails over; no overlap
3. **Reasonable budget** — default 3 total attempts (configurable 1-6), not 40
4. **Consistent backoff** — one `calculate_backoff()` function used everywhere with centralized policy values
5. **Smaller methods** — `_execute_llm_call_with_retry` reduced from ~500 to ~250 lines via extraction
6. **Clean config** — new field names only (`retry_max_attempts`, `endpoint_max_retries`), no legacy baggage
7. **Tested** — key scenarios covered with integration tests