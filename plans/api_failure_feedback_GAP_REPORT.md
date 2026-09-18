# GAP Report: API endpoint failure feedback vs. non-API Python module crashes

**Date:** 2026-09-15
**Author:** api-failure-research (investigation)
**Scope:** `agent_cascade/` only
**Companion docs:** `api_failure_feedback_plan.md` (design), `api_failure_feedback_plan_CODE_REVIEW.md` (review, REV 2 = PASS)

---

## 0. Version / git note (advisor requested)

- **No git tags exist** in this repo (`git tag -l` → empty). "v0.1.38" is a *version number*, not a tag.
- The API-endpoint failure work is commit **`847bbcb`** "Improve feedback on API endpoint failures (console + agent messages)", shipped via the bump commit **`55da1c3`** "chore(version): bump to 0.1.38".
- Current HEAD = **`2cf42e6`** (0.1.41). After the 0.1.38 bump there are only version bumps + two unrelated fixes (`11e059e` turn-limit notice, `3b96e57` skill names in prompts) + a lint sweep (`a8da0cc`). **No further error-handling work has landed.**
- `847bbcb` touched: `router.py`, `engine/core.py`, `engine/llm_call.py`, **NEW** `error_reporting.py` (536 lines), **NEW** `tests/test_error_reporting.py` (585 lines) + 3 plan/review docs.

---

## 1. Executive Summary

The **API-endpoint failure** feedback path is **fully implemented, tested, and reviewed (PASS)** since 0.1.38. It gives one compact line per incident + a deduped DEBUG traceback for connection / HTTP / model-load / rate-limit failures, plus human-readable terminal and RETRYING messages.

The **GAP** is everything that is *not* an API endpoint failure: **non-API Python module crashes** — a tool function raising an unhandled exception, a dispatcher/plumbing exception, a top-level run-thread crash, or a module import failure. These are **not** routed through `error_reporting.py` at all. Current handling is ad-hoc and inconsistent:

- **Tool function crash** → the *full* `traceback.format_tb()` stack is dumped into a `logger.warning` **and** returned as the tool result (fed straight back into the LLM context).
- **Dispatcher / plumbing crash** → a single opaque `Error: {e}` line, no traceback.
- **Top-level run-thread crash** → a single `logger.error(...{e})` line, **no traceback at all** — a genuine engine bug leaves only one cryptic line.

This is exactly the "better feedback for straight-up Python modules crashing in the log" the user is asking for: today it is *too verbose* in one place (full stacks into log + LLM) and *too vague* in another (one line, no stack), and `error_reporting.py` — the module that already solves the presentation problem for endpoints — has no generic-crash counterpart.

---

## 2. What is ALREADY implemented (the API-endpoint path)

All from `847bbcb`, verified in source:

| Piece | Location | Status |
|---|---|---|
| Leaf helper module `format_endpoint_error`, `classify_endpoint_failure`, `summarize_exhaustion`, `build_terminal_message`, `TracebackDedup` (+ `TB_DEDUP` singleton) | `agent_cascade/error_reporting.py` (536 lines) | ✅ stdlib-only leaf; lazy `ModelServiceError` import; duck-typed openai/httpx |
| Layer-1 per-attempt: compact one-liner WARNING + deduped DEBUG traceback | `api_router_pkg/router.py:2262` (`format_endpoint_error`), `:2269-2270` (`TB_DEDUP.should_log_full_tb` + `traceback.format_exc()`) | ✅ |
| Structured `.endpoint_failures` attr on terminal RuntimeError (no string parsing) | `router.py:2317` (`exc.endpoint_failures = all_errors`) | ✅ |
| Layer-2 retry log + terminal message (reads `.endpoint_failures`) | `engine/llm_call.py:1133-1141` (`build_terminal_message`, `summarize_exhaustion`, `TB_DEDUP`), `:1177`, `:1197` | ✅ |
| Transient `[RETRYING]` message with classified reason | `engine/core.py:1517-1520` (`classify_endpoint_failure` import in `_make_retrying_message`) | ✅ |
| Tests (27 → 29 with `now` regression tests) | `tests/test_error_reporting.py` | ✅ |
| Code review | `api_failure_feedback_plan_CODE_REVIEW.md` — **REV 2 = PASS** (critical `now=0.0` dedup bug fixed; full suite 2849 passed) | ✅ |

**Key design philosophy (reusable for the gap):** *"One incident = one compact log block. Root-cause line first; full traceback demoted to DEBUG and emitted at most once per (key) within a rolling window."* The API path achieves this; the tool-crash path does not.

---

## 3. Current log output — actual code

### 3.1 ✅ API endpoint path (already good)

`agent_cascade/api_router_pkg/router.py` (per-attempt, Layer 1):
```python
summary = format_endpoint_error(e)                       # one compact line
error_msg = (f"Endpoint '{endpoint_name}' @ {endpoint_base} "
             f"attempt {attempt+1}/{max_retries+1}: {summary}")
logger.warning(f"[APIRouter] {error_msg}")              # visible at INFO
all_errors.append(error_msg)
if TB_DEDUP.should_log_full_tb(TB_DEDUP.get_tb_key(e)):  # once per key per window
    logger.debug(f"[APIRouter] {error_msg}\nTraceback:\n{traceback.format_exc()}")
```
Terminal error carries structured data: `exc.endpoint_failures = all_errors` (`router.py:2317`), consumed by `build_terminal_message` in `llm_call.py:1133-1135`.

### 3.2 🔴 Tool function crash — full stack into log AND LLM

`agent_cascade/agent.py:264-282` (base `Agent._call_tool`, funnel for every template; `fncall_agent.py:122` → `super()._call_tool`):
```python
tool_result = tool.call(tool_args, **kwargs)
except (ToolServiceError, DocParserError) as ex:
    error_message = str(ex)
    logger.warning(f'Tool `{tool_name}` reported a service error:\n{error_message}')
    return error_message
except Exception as ex:
    exception_type = type(ex).__name__
    exception_message = str(ex)
    traceback_info = ''.join(traceback.format_tb(ex.__traceback__))   # ← FULL multi-frame stack
    error_message = f'An error occurred when calling tool `{tool_name}`:\n' \
                    f'{exception_type}: {exception_message}\n' \
                    f'Traceback:\n{traceback_info}'
    logger.warning(error_message)      # full stack in console.log
    return error_message              # full stack returned → becomes the FUNCTION message → fed to LLM
```
Consequences: (a) every crash re-logs the entire stack (no dedup); (b) the whole stack is injected into the model's context; (c) the returned string is *also* logged again via `_append_and_log` → `log_inst.log_message(fn_msg)` (`engine/core.py:217/221`) and may be cached (`_cache_tool_output`, `tool_execution.py:217`). So the full stack can appear **≥2–3×** per incident.

### 3.3 🟠 Dispatcher / plumbing crash — single line, no stack

`agent_cascade/engine/tool_execution.py:211-215` (wraps `tool_dispatcher.execute_tool`):
```python
except Exception as e:
    logger.error(f"Tool {tool_name} failed for {inst_name}: {e}")
    tool_result = f"Error: {e}"
```
Contrast with 3.2: a crash *inside* the tool gets a full stack; a crash *in the plumbing* gets one terse line. Inconsistent.

### 3.4 🔴 Top-level run-thread crash — one line, NO traceback

`agent_cascade/run_agent_unified.py:266-272`:
```python
except Exception as e:
    logger.error(f"run_agent_thread_unified failed for {instance_name}: {e}")
    error_msg = Message(role=ASSISTANT, content=f"[SYSTEM ERROR: {e}]")
```
A genuine engine bug (TypeError, AttributeError, …) surfaces as a single `str(e)` line with **no stack** — the hardest case to debug gets the least information.

### 3.5 Existing compact-traceback primitive (unused here)

`agent_cascade/utils/utils.py:183`:
```python
def print_traceback(is_error: bool = True):
    tb = ''.join(traceback.format_exception(*sys.exc_info(), limit=3))   # only 3 frames
    (logger.error if is_error else logger.warning)(tb)
```
A 3-frame compact pattern already exists and is used in a handful of spots (`llm/base.py:169`, `tools/base.py:226`, several agents) — but **not** in the tool-crash path.

### 3.6 Module import crashes (startup robustness, lower priority)

Tool modules are imported at load time (`agent.py:26` `from agent_cascade.tools import TOOL_REGISTRY, ...`; `agent_factory.py` imports each tool; `@register_tool` decorators register on import). **Optional** deps are handled lazily inside functions (`except ImportError` in `code_interpreter.py`, `image_gen.py`, `mcp_manager.py`, `screen_capture.py`, `syntax_check.py`, etc. → helpful install hints). But a **hard** module-level import failure (missing hard dependency, or a syntax error in a tool module) propagates up and crashes startup with no per-tool graceful surfacing.

---

## 4. Specific gaps (non-API Python module crashes)

| # | Gap | Where | Symptom |
|---|---|---|---|
| G1 | Full `format_tb` stack logged at WARNING **and** returned as tool result | `agent.py:274-282` | Log spam + LLM context pollution; no dedup |
| G2 | No compact root-cause line for tool crashes (no `error_reporting` equivalent) | `agent.py` | First line buried in a stack |
| G3 | Inconsistent presentation: tool crash = verbose, dispatcher crash = terse | `agent.py` vs `tool_execution.py:211` | Two different shapes for "a tool failed" |
| G4 | Top-level handler logs only `str(e)`, **no traceback** | `run_agent_unified.py:268` | Real engine bug → one cryptic line |
| G5 | Crash string double/triple-logged (WARNING + per-instance `log_message` + cache) | `agent.py` + `core.py:217/221` | Redundant full stacks in ≥2 files |
| G6 | `error_reporting.py` is endpoint-specific; no generic-crash helper | `error_reporting.py` | Can't reuse the dedup/summary machinery for module crashes |
| G7 | Hard module import failure at startup not gracefully surfaced | tool import path | Startup crash, no per-tool hint |

---

## 5. Recommendations (evidence-based, zero control-flow change — presentation only)

**R1 — Compact root-cause line for tool crashes (primary).** In `Agent._call_tool` (`agent.py:274-282`), replace the full `format_tb` dump with a single-line summary at WARNING (`{Type}: {msg}`) and demote the full traceback to **DEBUG**, gated by the existing `TB_DEDUP.should_log_full_tb(TB_DEDUP.get_tb_key(e))`. Mirror the API path exactly (router.py:2262-2270 pattern).

**R2 — Keep full stacks out of the LLM context.** The returned tool result should carry the compact one-liner (or a bounded excerpt, e.g. last 1–2 frames + root cause), *not* the entire stack. Full stack → DEBUG log only. This protects the context window and keeps the model usable.

**R3 — Consistent shape across the two tool-crash layers.** Make `tool_execution.py:211-215` present the same compact line + deduped DEBUG traceback as `agent.py`, so "a tool failed" looks identical whether the crash is in the tool or the plumbing.

**R4 — Add a (deduped) traceback to the top-level handler.** `run_agent_unified.py:268` should log a compact line + a traceback (full at DEBUG, deduped) so a genuine unhandled engine crash is diagnosable. This fixes the *too-vague* end.

**R5 — Single canonical logging point.** Decide where the crash detail is logged once (e.g. only in `_call_tool`, or only in `_append_and_log`) to remove the G5 redundancy.

**R6 — Reuse, don't reinvent (optional).** Add a small `format_crash(e)` / `classify_crash(e)` helper to `error_reporting.py` (preserving its leaf-module, stdlib-only constraint) or a sibling `crash_reporting.py`, and reuse `TracebackDedup` for both endpoint and module crashes. One dedup + one formatting contract for all failure presentation.

**R7 — (Lower priority) Startup import surfacing.** Wrap tool-module imports so a hard import failure logs a per-tool "tool `X` unavailable: {ImportError}" at startup instead of crashing the whole app.

---

## 6. Confidence

- **Confirmed:** all snippets in §3 read directly from source; `error_reporting.py` exists and is endpoint-only; review REV 2 = PASS; no git tags; `847bbcb` is the API-failure commit; no later error-handling commits.
- **High:** G1–G5 (tool/dispatcher/top-level crash handling) — directly inspected.
- **Moderate:** G7 (startup import surfacing) — tool load path verified, but the exact startup crash propagation not traced end-to-end.
- **Open (see §7):** whether the FUNCTION tool-result message is *streamed to the UI* (only the auto-denied path is confirmed to `response.append`), and the product decision on how many frames, if any, belong in the LLM-facing tool result.

## 7. Open questions

1. Is the FUNCTION tool-result message (carrying the full stack from G1) streamed to the web UI, or only sent to the LLM + per-instance log? (affects severity of G1)
2. Should the LLM-facing tool result carry *zero* frames, the root-cause line, or a short bounded excerpt? (product decision)
3. Do we want a shared `crash_reporting` helper (R6) or keep `error_reporting.py` strictly endpoint-scoped?

## 8. Suggested next actions

1. Prototype R1+R2 in `agent.py:_call_tool` (compact WARNING + deduped DEBUG, bounded LLM excerpt); add a regression test asserting the returned tool result does **not** contain `format_tb` output.
2. Add a (deduped) traceback to `run_agent_unified.py:268` (R4).
3. Reconcile `tool_execution.py:211` with the new shape (R3).
4. If R6 adopted, extend `error_reporting.py` (leaf constraint) and reuse `TB_DEDUP` for both paths.
