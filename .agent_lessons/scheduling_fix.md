# Scheduling Fix - Phase 1, 2 & 3 Lessons

## What Was Fixed
The AgentCascade system had a bug where parallel agents (especially Security Advisor) landed on the same non-concurrent API endpoint, causing model trashing. Three root causes:
1. Default endpoint concurrency was never read in `call_with_fallback()`
2. `get_concurrency_limit()` returned -1 for agents without custom endpoints — even though their inherited default endpoint might have `concurrency_limit=0`
3. The semaphore protected individual API calls, not entire agent lifecycles — allowing interleaving between agents on the same endpoint

## Changes Made

### Phase 1: Fix call_with_fallback (api_router.py)
- Removed `if not is_default:` guard so default fallback endpoints also read their actual `concurrency_limit` and `max_retries` from endpoint config (was stuck at 0)

### Phase 2: Add get_effective_concurrency() (api_router.py + agent_orchestrator.py)
- New method resolves real endpoint concurrency including default fallback
- Returns 0 (conservative/sequential) if default config has api_base but no matching endpoint found
- Only returns -1 when there's truly no config at all
- Replaced `get_concurrency_limit()` with `get_effective_concurrency()` in parallel launch check

### Phase 3: EndpointScheduler for lifecycle-aware serialization (api_router.py + agent_orchestrator.py)

**New class `EndpointScheduler`** — uses `threading.Semaphore` per endpoint for race-free capacity control:
- `acquire(api_base, concurrency_limit)` — blocks atomically if at capacity, returns cleanup callback. Returns None for unlimited (-1). For concurrency=0, max_active=1 (strictly sequential)
- `release()` callback — decrements active_count and releases the semaphore
- `count_active(api_base)` — returns current active count
- `get_status()` — diagnostic method returning all endpoint states

**Integration in `ParallelAgentManager.submit_task()`:**
- Before deepcopy of history, resolves actual api_base and concurrency_limit using `get_effective_concurrency()` and `get_llm_config()`
- Calls `router.scheduler.acquire(api_base, concurrency_limit)` — blocks if endpoint is at capacity
- The `endpoint_release` callback is captured by closure in `task_wrapper` and called in the `finally` block when agent completes

**CRITICAL DESIGN DECISION — Semaphore vs Manual Queue:**
The initial implementation used a manual `queue.Queue()` with separate lock acquisitions for check-and-increment. This had a **TOCTOU race condition** (Finding 14) that allowed agents to bypass concurrency limits. The fix was to use `threading.Semaphore` which provides atomic blocking — no window between "check capacity" and "enter critical section".

### Thread Safety Fixes
- Added `with self._lock:` around all `self.endpoints.values()` and `self.agent_priorities` iterations in `call_with_fallback()`, `get_endpoint_chain()`, `get_agent_priorities()`, `list_endpoints()`
- Applied consistent `defaults = self.default_llm_cfg or {}` pattern across all methods

### Key Design Decisions
- **Conservative default**: When default config exists but no matching endpoint is found, return 0 (sequential) instead of -1 (unlimited). This prevents unexpected parallel launches on unknown endpoints.
- **Backward compatibility**: `get_concurrency_limit()` still exists and delegates to the new method, so external callers aren't broken.
- **Semaphore-based scheduling**: Standard library primitive with atomic blocking — no manual queue management needed.

## What Was NOT Fixed (Out of Scope)
- The `_waiting_agents` / `_sem_lock` race condition in `execute_with_sem()` — this is a pre-existing design issue affecting UI display only, not correctness. Track separately.
- Phase 4 from the original plan (per-call semaphore cleanup) — future work item.

## Testing Notes
- Verify that `get_effective_concurrency('security_advisor')` returns the default endpoint's actual concurrency_limit, not -1
- Test with concurrent endpoint CRUD operations to confirm no RuntimeError crashes
- Test: configure default endpoint with concurrency_limit=0, launch 3 Security Advisor agents in parallel via `parallel_launch=true` → only one runs at a time, strictly sequential