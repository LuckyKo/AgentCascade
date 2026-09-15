# Plan: Better Feedback on API Endpoint Failures (todo.md line 219)

**Status:** DRAFT v3 — REV 2 review addressed: counter-based dedup pruning (perf fix #1)
**Scope:** `agent_cascade/` only. No behavior changes to retry/failover logic — presentation of failures only.

## 1. Problem Statement

When an LLM API endpoint fails (connection refused, WinError 10055, 502 "llama-server unreachable",
503 model-load failure, etc.), the user gets poor feedback in both channels:

### Console / `logs/console.log`
- **Layer 1** (`api_router_pkg/router.py:2374-2380`, per-attempt): logs a WARNING with
  `traceback.format_exc()` — a full multi-frame stack through httpx → httpcore → openai SDK →
  oai.py → router.py (20–60 lines) for *every single attempt*. The one line that matters
  (`httpcore.ConnectError: [WinError 10055] ...`) is buried at the bottom of ~4 noise frames.
- **Layer 2** (`engine/llm_call.py:1218-1221`, per-retry): logs `Error: {e}` where `{e}` is the
  terminal `RuntimeError("All API endpoints exhausted for agent type 'X'.\n" + all_errors)` —
  i.e. the FULL concatenated stack dumps from every endpoint/attempt get re-printed into the log
  a second time, once per outer retry (up to 3×).
- Net effect: one bad server can produce hundreds of lines of repeated tracebacks per incident.

### Agent / UI feedback
- **Transient** (`engine/core.py:1561-1564`): `[RETRYING] Connection lost, retrying (n/N) in Xs...`
  — hardcoded "Connection lost" regardless of the actual cause (could be 502, rate limit,
  model-load failure). No endpoint identity.
- **Terminal** (`engine/llm_call.py:1164`): `LLM call failed after N retry attempts — {first line
  of str(e)}` = literally "All API endpoints exhausted for agent type 'security'." with no idea
  WHICH endpoints failed, WHY (connection refused vs 502 vs 503), or what the user can do.

## 2. Design Goals

1. **One incident = one compact log block.** Root-cause line first; full traceback demoted to
   DEBUG and emitted at most once per (endpoint, model) within a rolling window.
2. **No duplicate stack dumps.** The terminal "exhausted" error must never re-print raw
   tracebacks into WARNING/ERROR lines.
3. **Agent-facing messages are human-readable and actionable**: what failed, where, why
   (classified), what is being done / what the user can do.
4. **Zero behavior change** to retry counts, backoff, failover ordering, breakers, cooldowns.
5. **No new config knobs.** Keep it minimal.

## 3. Changes

### 3.1 New helper module: `agent_cascade/error_reporting.py` (new file)

```python
def format_endpoint_error(e: Exception) -> str:
    """Compact single-line root-cause summary for an endpoint failure.

    Walks the exception chain (e.exception / __cause__ / .body) and returns ONE line, e.g.:
      - "connection error to http://127.0.0.1:1234/v1 (WinError 10055: socket buffer full)"
      - "HTTP 502 from http://127.0.0.1:1234/v1: llama-server unreachable"
      - "HTTP 503 from http://127.0.0.1:1234/v1: Failed to load model 'Agents-A1-...'"
      - "timeout after 30s (httpx.ReadTimeout)"

    Rules:
      * ModelServiceError: prefer .code + .message; if .exception present, descend into it.
      * openai.APIStatusError / httpx.HTTPStatusError: 'HTTP {status} from {url}: {body excerpt ≤120 chars}'
        (body = first 120 chars of str(ex.response.text) or ex.body JSON value, if available).
      * httpx.ConnectError / httpcore errors: include the inner OSError errno text when present.
      * Fallback: f'{type(e).__name__}: {str(e)[:160]}'
    """

def classify_endpoint_failure(e: Exception) -> str:
    """Human-facing short category label for UI messages.

    Returns one of: 'connection refused/unreachable', 'network timeout', 'server error (HTTP xxx)',
    'rate limited (HTTP 429)', 'model load failure', 'authentication failure', 'unknown error'.
    Reuses retry_policy patterns where sensible but returns user-facing strings, not internal codes.
    """

class TracebackDedup:
    """Rolling-window dedup for full tracebacks.

    get_tb_key(e) -> str        # (normalized endpoint base, model, root exception type+msg hash)
    should_log_full_tb(key, now=0.0) -> bool   # True at most once per key per WINDOW_SECONDS (default 60)

    Thread-safety: a single `threading.Lock` guards the dict; it is held for the ENTIRE
    check-and-update in should_log_full_tb (atomic read-compare-write). The lock is never
    held while logging (release before any logger call — callers log outside the method).
    No nested locking: this class takes no other locks, so deadlock with router._lock is
    impossible (router code calls it without holding _lock at the log site).

    Bounded memory (review fix #1, REV 2 perf fix): pruning is COUNTER-BASED, not per-call —
    every PRUNE_EVERY_NTH_CALLS-th invocation (default 100) removes entries whose last-seen
    timestamp is older than PRUNE_AFTER (default 3600s). At worst one O(n) sweep per 100 calls;
    during a high-rate outage (hundreds of attempts/sec) that is ≤ a few dozen dict scans/sec,
    negligible vs the HTTP round-trips being retried. The counter itself is an int guarded by
    the same lock. Dict size stays bounded by distinct keys active within the prune horizon.
    """
```

- Module-level singleton `TB_DEDUP = TracebackDedup()`.
- Endpoint identity for keys comes from `ModelServiceError` context where available; otherwise
  hash of root exception class + first 80 chars of message.

**Import audit (review fix #4) — hard constraint:** `error_reporting.py` is a leaf module.
It imports ONLY:
- standard library (`re`, `hashlib`, `threading`, `time`, `logging`, `typing`)
- `agent_cascade.llm.base` → `ModelServiceError` (lazy, inside functions — base.py imports no
  router/engine modules, verified)
- `agent_cascade.exceptions` → nothing currently needed; allowed if required

It MUST NOT import from `agent_cascade.api_router_pkg`, `agent_cascade.engine.*`, or
`agent_cascade.api_integration_pkg`. All openai/httpx exception types are handled by
duck-typing (`getattr(ex, 'response', None)`, `isinstance` against names resolved lazily via
`importlib` is NOT needed — walk the `__cause__`/`exception` chain and match on class NAME
strings plus attributes, so openai/httpx are never imported at module level).

### 3.2 Layer 1 — per-attempt logging (`api_router_pkg/router.py`, `call_with_fallback` except block, ~L2374-2380)

Replace:
```python
tb_str = traceback.format_exc()
error_msg = (f"Endpoint '{endpoint_name}' @ {endpoint_base} "
             f"attempt {attempt+1}/{max_retries+1}: {e}\nTraceback: {tb_str}")
logger.warning(f"[APIRouter] {error_msg}")
all_errors.append(error_msg)
```
with:
```python
summary = format_endpoint_error(e)
error_msg = (f"Endpoint '{endpoint_name}' @ {endpoint_base} "
             f"attempt {attempt+1}/{max_retries+1}: {summary}")
logger.warning(f"[APIRouter] {error_msg}")
all_errors.append(error_msg)

# Full traceback at DEBUG, deduped per endpoint+root-cause (once per 60s window).
if TB_DEDUP.should_log_full_tb(TB_DEDUP.get_tb_key(e)):
    logger.debug(f"[APIRouter] {error_msg}\nTraceback:\n{traceback.format_exc()}")
```

Notes:
- `all_errors` now carries compact lines only → the terminal RuntimeError (3.4) becomes readable.
- Keep `logger.warning` level for the one-liner (visible at default INFO).
- Do NOT change the backoff/retry/failover code around this block.

### 3.3 Layer 2 — outer retry logging (`engine/llm_call.py`, ~L1218-1221)

The `Error: {e}` there prints the whole concatenated dump. Replace with a compact digest:
```python
logger.warning(
    f"[ENDPOINT_RETRY] LLM call failed for {inst_name}, retry {retry_count}/{_max_attempts}. "
    f"Retrying in {backoff:.1f}s{endpoint_str}... {summarize_exhaustion(e)}"
)
```
where `summarize_exhaustion(e)` (in error_reporting.py):
- If `str(e)` starts with "All API endpoints exhausted": return
  `"all endpoints failed: " + "; ".join(first-line-of-each-error, max 3, else '...')`.
- Else: `format_endpoint_error(e)`.

Also the terminal path at L1165 (`logger.error(... : {e})`) — same treatment: log the compact
digest, and (deduped) full traceback via `TB_DEDUP` since this is the user-visible failure point.

### 3.4 Terminal error message surfaced to agent/UI (`engine/llm_call.py`, ~L1150-1168)

Current: `[SYSTEM ERROR: LLM call failed after N retry attempts — All API endpoints exhausted for agent type 'security'.]`

New (built from the same compact digest):
```
[SYSTEM ERROR: LLM unavailable after 3 retries.
 Endpoints tried:
   • qwen3.8-27b @ http://127.0.0.1:1234/v1 — connection refused/unreachable (WinError 10055)
   • whatever_is_on @ http://localhost:1234/v1 — server error (HTTP 502): llama-server unreachable
 Check that the LLM server is running and reachable.]
```

Implementation (review fix #3 — no string parsing): the router's terminal `RuntimeError`
carries structured data instead of relying on str() parsing. In router.py, when building the
terminal exception, attach the compact per-endpoint list as an attribute:

```python
exc = RuntimeError(
    f"All API endpoints exhausted for agent type '{agent_type}'.\n" + '\n'.join(all_errors)
)
exc.endpoint_failures = all_errors  # List[str], one compact line per endpoint/attempt
raise exc
```

(`RuntimeError` accepts arbitrary attributes — same pattern already used by
`FallbackCompressionRequired(original_error=...)` consumers.) The llm_call terminal path then
reads `getattr(e, 'endpoint_failures', None)`; if absent (e.g. non-router errors), it falls
back to the first line of `str(e)`. No delimiter parsing anywhere.

The message lists up to 5 endpoint lines, truncated with "…and N more". Append one action hint based on dominant category:
- connection/unreachable → "Check that the LLM server is running and reachable."
- model load failure / 503 → "The model may still be loading — it will retry automatically."
- rate limited → "Rate limit hit — requests will be throttled."
- other → no hint.

Keep `display_msg` (first line) for the log; the multi-line version goes in the yielded Message.

### 3.5 Transient RETRYING message (`engine/core.py:1541-1564`)

`_make_retrying_message()` currently has no access to the exception. Add optional param:
```python
def _make_retrying_message(self, instance, attempt, max_retries, delay, error=None) -> Message:
    reason = classify_endpoint_failure(error) if error is not None else 'Connection lost'
    return Message(role=ASSISTANT,
                   content=f"[RETRYING] {reason} — retrying ({attempt}/{max_retries}) in {delay:.1f}s...")
```
Caller in llm_call.py:1224 passes `error=e`. When `error is None` (any other/legacy caller) the
message text stays EXACTLY as today ("[RETRYING] Connection lost, retrying ...") — review fix #2.
Note the wording changes for the llm_call call site itself (it will now say e.g.
"[RETRYING] server error (HTTP 502) — retrying..."); reviewer verified no test asserts on the
old exact string, so this is an intentional improvement, not a regression.

### 3.6 Out of scope (explicit)

- No changes to `retry_policy.py`, breakers, cooldowns, sanity probes, or failover order.
- No new settings/config entries.
- Web UI rendering of these messages is unchanged (they're plain assistant-role Messages).
- The deprecated `retry_model_service*` / `_raise_or_delay` paths in llm/base.py are untouched.

## 4. Testing Plan

New file `tests/test_error_reporting.py`:
1. `format_endpoint_error` — unit cases: ModelServiceError(502 body), ModelServiceError(503 model load),
   OpenAI APIConnectionError wrapping httpx.ConnectError(WinError 10055), httpx.ReadTimeout,
   APIStatusError with .body dict, bare RuntimeError fallback. Assert single-line, ≤ ~200 chars,
   contains status code / errno where applicable.
   Edge cases (review fix #5): `None` input → safe fallback string (no exception); message >160
   chars → truncated; deeply nested `__cause__` chain (3+ levels) → root cause still found;
   non-Exception object passed by mistake → safe fallback.
2. `classify_endpoint_failure` — mapping table assertions for each category, incl. the edge cases above.
3. `TracebackDedup` — first call True, immediate second False, after window expiry True again;
   different keys independent; thread-safety smoke (8 threads hammering same key);
   **pruning test (review fix #1)**: seed entries with old timestamps, call should_log_full_tb,
   assert stale entries removed from the internal dict (size stays bounded).
4. Router layer-1: monkeypatch logger + fake endpoint chain failing with a canned ConnectError →
   assert WARNING line is single-line compact and DEBUG traceback appears exactly once for repeated
   identical failures within the window; also assert `exc.endpoint_failures` attribute exists on
   the terminal RuntimeError (review fix #3).
5. llm_call terminal path: canned exhausted RuntimeError WITH `.endpoint_failures` list → yielded
   Message contains per-endpoint lines + action hint; log line does NOT contain "Traceback".
   Also test the fallback path where `.endpoint_failures` is absent.

Regression (review fix #7): run existing `tests/test_sticky_slot_assignment.py`,
`tests/test_retry_baseline.py` (asserts on "All API endpoints exhausted" substring — must still pass),
any router retry tests, and the FULL suite (baseline 2610 passed / 4 skipped) before commit.

## 5. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Tests assert on old log/message formats | Grep tests for "All API endpoints exhausted" / "[RETRYING] Connection lost" before implementing; update assertions only where the NEW format is strictly more informative. Reviewer must verify no test weakened. |
| `str(e)` parsing of the exhausted error is fragile | The compact lines are produced by our own code (3.2) — stable contract. Add a unit test pinning the line format. |
| Dedup window hides a genuinely new traceback variant | Key includes root-exception type + message hash, not just endpoint — a different failure mode still logs its full TB once per window. |
| Behavior drift in retry logic | 3.2/3.3 touch only the logging statements inside existing except blocks; reviewer must diff-verify no control-flow change. |

## 6. Files Touched (summary)

| File | Change |
|---|---|
| `agent_cascade/error_reporting.py` | NEW — helpers + dedup (leaf module, import-audited) |
| `agent_cascade/api_router_pkg/router.py` | Layer-1 logging (~L2374-2380) + `.endpoint_failures` attr on terminal RuntimeError (~L2433) |
| `agent_cascade/engine/llm_call.py` | Layer-2 log, terminal message (reads `.endpoint_failures`), RETRYING call site |
| `agent_cascade/engine/core.py` | `_make_retrying_message` optional error param |
| `tests/test_error_reporting.py` | NEW — unit + integration tests |

## 7. Review Disposition (REV 2)

- #1 dedup pruning → fixed in §3.1 (counter-based: sweep every 100th call, PRUNE_AFTER=3600s; REV 2 perf fix) + test §4.3
- #2 fallback text → fixed in §3.5 (`'Connection lost'` preserved for error=None)
- #3 parser fragility → fixed in §3.4 (structured `.endpoint_failures` attribute, no parsing)
- #4 circular imports → fixed in §3.1 import audit (leaf module, stdlib + lazy llm.base only)
- #5 edge cases → fixed in §4.1/§4.2 test list
- #6 thread-safety details → fixed in §3.1 (single lock, atomic check-and-update, no nested locks)
- #7 test coverage → fixed in §4 (pruning test, full-suite regression incl. test_retry_baseline.py)
- #8/#9/#10 (nice-to-haves: config-reload reset, dedup telemetry, more hint categories) → DEFERRED, out of scope for this change
