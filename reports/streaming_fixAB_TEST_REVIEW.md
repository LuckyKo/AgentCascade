# Independent Review — Streaming Fix A/B Integration Tests

**File reviewed:** `tests/test_streaming_broadcast_fixes.py` (5 async tests)
**Verdict:** ✅ **PASS**
**Reviewer:** reviewer agent (`stream_fixAB_test_review`)

## Scope
Verify the two committed streaming-stall fixes:
- **Fix A** (c09976d): `api_server.broadcast()` sends to all WS clients concurrently via
  `asyncio.gather`, each send bounded by `WS_SEND_TIMEOUT=5.0s`; slow/wedged conns closed +
  discarded after the gather; per-client FIFO preserved.
- **Fix B** (33bf869): `ws_handlers.WsMessageHandler._broadcast()` runs `build_state_fn(...)`
  via `await loop.run_in_executor(None, ...)`.

## 1. Fix A testability trade-off — ACCEPTED ✅
`api_server.broadcast()` is a closure nested inside `create_app()`; its private globals
(`ws_connections`, `_send_queue`) are not module-level and unreachable from tests without a
production hook. The two (later three) Fix A tests therefore exercise a **faithful extraction**
of the exact core (`_build_broadcast_core` in the test file).

Line-by-line comparison vs production `api_server.py` (~616-667):
| Production | Test extraction | Divergence? |
|---|---|---|
| `json.dumps(data, ensure_ascii=False, default=str)` | same | none |
| `snapshot = frozenset(ws_connections)` | same | none |
| `_send_with_timeout` + `wait_for(..., timeout=WS_SEND_TIMEOUT)` | same (via `timeout_ref()` → real module global) | equivalent |
| `return conn, None` / `except: close(); return conn, e` | same | none |
| `gather(*[...], return_exceptions=True)` | same | none |
| post-gather `isinstance(result, Exception): continue`; `err is not None: discard(conn)` | same | none |
| `logger.debug(...)` before discard | **missing** | non-functional (observability only) |

**Judgment:** extraction is faithful. Assertions genuinely FAIL if production regressed to
sequential sends or dropped the per-send timeout/reap:
- `test_fix_a_slow_client_does_not_stall_healthy` asserts total broadcast `< 2.0s` (bounded by
  the lowered 0.2s timeout, not the 5s slow sleep) — a sequential impl would take ~5s and fail.
- `test_fix_a_per_client_fifo_preserved` asserts the reaped slow client never receives frame 2.

**Residual limitation (acknowledged):** this approach cannot catch a future *structural* refactor
of the literal closure (e.g. moving broadcast logic out of `create_app`). Ideal follow-up if
literal-function coverage is wanted: a small production hook exposing `broadcast`/`ws_connections`
on `app.state`. Not required for this change.

## 2. Fix B thread-identity assertions — SOUND ✅
- Captures the event-loop thread via `threading.current_thread()` before `_broadcast()`, records
  the thread inside `fake_build_state`, asserts they differ. Correct proof of off-loop execution.
- No false-pass risk: `loop.run_in_executor(None, ...)` uses a ThreadPoolExecutor; the main
  (event-loop) thread is never a worker thread, so identity comparison is robust and not
  timing-dependent.
- `_make_handler` constructs `WsMessageHandler` correctly against its real signature.

## 3. Test quality & edge cases 🟡 minor
Covered: slow-vs-healthy concurrency, per-client FIFO, off-loop execution, concurrent burst,
(empty connections no-op added post-review). Suggested-but-deferred (lower value / flakier):
all-clients-slow, timeout-exactly-at-boundary. Timing assertions use generous margins
(0.2s timeout vs 2.0s limit) — safe; thread-identity checks deterministic — safe.

## 4. Conventions & style ✅
`sys.path.insert(0, str(PROJECT_ROOT))` matches project convention; `asyncio.run()` in sync tests;
clear helper scoping; no dead code or import issues.

## Required changes
None critical. (Post-review: the empty-connections no-op test was added per suggestion → 5 tests.)

## Result
`python -m pytest tests/test_streaming_broadcast_fixes.py -v` → **5 passed**.
