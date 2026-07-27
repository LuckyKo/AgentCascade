# Current Retry Math — Baseline Documentation

This document captures the EXACT retry behavior of AgentCascade BEFORE the retry refactoring.
Use this as a regression reference: after refactoring, equivalent scenarios must produce
identical or better outcomes.

**Generated:** 2026-07-28  
**Version:** v4 (pre-refactor baseline)  
**Related Plan:** docs/retry_refactor_plan.md

---

## Architecture Overview — Three Retry Layers

```
Layer 3: execution_engine._execute_llm_call_with_retry()   [MAX_RETRIES=1, error classification]
         ↓ calls
Layer 2: APIRouter.call_with_fallback()                    [endpoint chain + per-endpoint retries]
         ↓ calls
Layer 1: BaseChatModel.retry_model_service_iterator()      [raw HTTP retry with exponential backoff]
```

### Critical Coupling (Current State)

**endpoint.max_retries is passed to LLM via `to_llm_cfg()`**, so changing an endpoint's
retry count affects BOTH Layer 2 AND Layer 1 simultaneously. This is intentional but
problematic — the retry refactoring will decouple these concerns.

Example: `APIEndpoint(max_retries=3)` → `llm_cfg["max_retries"]=3` → both layers use 3.

---

## Exact Default Values Per Layer

### Layer 1 (BaseChatModel.retry_model_service_iterator)

| Parameter | Default Source | Value |
|-----------|---------------|-------|
| max_retries | cfg.get("max_retries", 2) | **2** |
| base_retry_delay | cfg.get("base_retry_delay", 1.0) | **1.0s** |
| max_retry_delay | cfg.get("max_retry_delay", 30.0) | **30.0s** |

**Behavior:**
- Catches `Exception` (bare except — catches EVERYTHING including KeyboardInterrupt, SystemExit)
- Wraps all caught exceptions as `ModelServiceError(message=str(e))`
- Exponential backoff: `delay = min(base_retry_delay * 2^attempt, max_retry_delay)`
- Total attempts = max_retries + 1

### Layer 2 (APIRouter.call_with_fallback)

| Parameter | Default Source | Value |
|-----------|---------------|-------|
| max_retries per endpoint | endpoint.max_retries (if using endpoint), else hardcoded 2 | **2** |
| base_retry_delay | endpoint.base_retry_delay or hardcoded 1.0 | **1.0s** |
| max_retry_delay | endpoint.max_retry_delay or hardcoded 30.0 | **30.0s** |

**Behavior:**
- Iterates through endpoint chain: agent-specific → last successful → default fallback
- Per-endpoint: retries up to max_retries times before moving to next endpoint
- Exponential backoff same formula as L1
- On CharacterRunDetected/MaxTokenExceeded: advances cursor, skips remaining retries on current endpoint

### Layer 3 (execution_engine._execute_llm_call_with_retry)

| Parameter | Default Source | Value |
|-----------|---------------|-------|
| MAX_RETRIES | hardcoded constant | **1** |
| Error classification | is_transient_error() | CharacterRunDetected, MaxTokenExceeded, rate limits |

**Behavior:**
- Wraps APIRouter.call_with_fallback() calls
- On transient errors: retries entire endpoint chain (up to MAX_RETRIES=1)
- On non-transient errors: fails immediately
- Provides error classification context for caller

---

## Maximum HTTP Calls by Scenario

### Single Endpoint, No Failover

Configuration: 1 endpoint with max_retries=N

| N | L1 attempts per call | L2 attempts (endpoints tried) | Total HTTP calls |
|---|---------------------|------------------------------|-----------------|
| 0 | 1 | 1 endpoint × 1 attempt = 1 | **1** |
| 1 | 2 | 1 endpoint × 2 attempts = 2 | **2** |
| 2 | 3 | 1 endpoint × 3 attempts = 3 | **3** |

Formula: `total = (max_retries + 1)` — because L1 and L2 share the same max_retries value.

### Two-Endpoint Chain, First Fails Completely

Configuration: Endpoint A (max_retries=NA), Endpoint B (max_retries=NB)

| NA | NB | Calls on A | Calls on B | Total |
|----|----|-----------|-----------|-------|
| 0 | 2 | 1 | up to 3 | **4** |
| 1 | 2 | 2 | up to 3 | **5** |
| 2 | 2 | 3 | up to 3 | **6** |

Formula: `total = (NA + 1) + (NB + 1)` when A exhausts and B succeeds.

### Full Chain Exhaustion (All Endpoints Fail)

Configuration: Agent-specific endpoints [E1, E2] + default fallback

| E1.max_retries | E2.max_retries | Default.max_retries | Total HTTP calls |
|---------------|---------------|---------------------|-----------------|
| 0 | 0 | 2 | **4** |
| 1 | 1 | 2 | **6** |
| 2 | 2 | 2 | **9** |

Formula: `total = Σ(endpoint.max_retries + 1)` for all endpoints in chain.

### With Layer 3 (execution_engine) Added

Layer 3 adds MAX_RETRIES=1 retry of the ENTIRE chain on transient errors:

| Scenario | Without L3 | With L3 (transient error) |
|----------|-----------|--------------------------|
| Single endpoint, max_retries=2 | 3 calls | Up to **6** calls (chain retried once) |
| Two endpoints [1,2] + default[2] | 6 calls | Up to **12** calls |

---

## Error Flow Examples

### Example 1: ConnectionError on First Attempt

```
ConnectionError → L1 catches as Exception → wraps as ModelServiceError
→ L1 retries (up to max_retries times with backoff)
→ If all L1 retries exhausted → raises ModelServiceError("Maximum number of retries exceeded")
→ L2 catches ModelServiceError → retries endpoint (up to endpoint.max_retries times)
→ If all L2 retries exhausted → moves to next endpoint in chain
```

**Bug:** Original ConnectionError type is lost — wrapped as ModelServiceError by L1.

### Example 2: CharacterRunDetected

```
CharacterRunDetected → L1 catches as Exception → wraps as ModelServiceError
→ L1 retries (up to max_retries times) ← BUG: should not retry, this is deterministic!
→ If all L1 retries exhausted → raises ModelServiceError
→ L2 catches ModelServiceError → does NOT recognize CharacterRunDetected (type lost)
→ L2 retries normally instead of advancing cursor
```

**Bug:** CharacterRunDetected is treated as a transient network error by L1, causing
unnecessary retries before the type information is lost entirely. Phase 2 will fix this.

### Example 3: MaxTokenExceeded

Same bug as CharacterRunDetected — wrapped by L1, loses special handling.

### Example 4: Rate Limit (HTTP 429)

```
RateLimitError → L1 catches → wraps as ModelServiceError
→ L1 retries with exponential backoff
→ Eventually succeeds or exhausts retries
```

**Note:** Rate limits should ideally trigger endpoint advance, but current code treats
them like any other transient error.

---

## Endpoint Configuration Audit Results

From `config/api_endpoints.json` (15 endpoints total):

| Setting | Distribution |
|---------|-------------|
| max_retries=2 | 11 endpoints (73%) |
| max_retries=1 | 4 endpoints (27%) — Opencode, Opencode2, Player2, OpenRouter |
| base_retry_delay=1 | 14 endpoints (93%) |
| base_retry_delay=3 | 1 endpoint (7%) — zai-org/GLM-5.2:novita |
| max_retry_delay=30 | 15 endpoints (100%) |

**Pattern:** External/throttled services use max_retries=1 to avoid rate-limiting issues.

---

## Direct LLM Calls Outside Execution Engine Audit

Found **6 locations** calling `.chat()` directly outside execution_engine.py:

### HIGH Risk
1. **api_router.py:1317** — Image captioning creates separate LLM, calls `.chat()` without going through call_with_fallback's endpoint fallback chain.

### MODERATE Risk
2. **agent.py:181** — Agent._chat_with_functions() called by agent subclasses; bypasses outer retry layers when agents used directly.
3. **tools/image_gen.py:53** — ImageGen tool creates own LLM instance, independent of main agent's retry path.
4. **workstation_server.py:206** — Standalone server component with no outer retry layer.

### LOW Risk (acceptable)
5. **llm/base.py:176,740** — BaseChatModel utility methods (quick_chat, etc.) — intentional helper patterns.
6. **test_agent.py:181** — Test file only.

---

## Performance Baselines

Measured from tests/test_retry_baseline.py (local mock, no network):

| Scenario | Latency | Notes |
|----------|---------|-------|
| Zero retries (success on first) | <0.5s | Pure Python overhead |
| One retry (fail then succeed) | ~1.0s | Includes 0.05s backoff |
| Max retries exhausted (2 retries + default fallback) | ~3.1s | Includes cumulative backoff delays |

With real network and defaults (base_retry_delay=1.0, max_retry_delay=30.0):
- Single retry: ~1-2 seconds additional latency
- Full exhaustion of 3-attempt endpoint: up to 60+ seconds if hitting max_retry_delay caps

---

## Known Bugs Documented by This Baseline

1. **Error type corruption:** L1 wraps ALL exceptions as ModelServiceError, losing original types.
2. **Inner-loop detection bypassed:** CharacterRunDetected/MaxTokenExceeded caught by L1 before L2 can handle them specially.
3. **Retry coupling:** endpoint.max_retries controls both L1 and L2 simultaneously via to_llm_cfg().
4. **Bare except in L1:** Catches KeyboardInterrupt/SystemExit — could prevent clean shutdowns.

These bugs are the primary motivation for the retry refactoring (Phase 2+).