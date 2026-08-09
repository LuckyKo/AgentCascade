# Investigation Report: Thinking Blocks Inside Tool Argument JSON Strings

**Investigating agent**: researcher · `investigate_thinking_in_tool_args`
**Date**: 2026-08-09
**Status**: Conclusion reached — current sanitizer is defensive code for a defect class
that is *structurally impossible via the anchored regexes*, and the codebase's own
history + tests + log data show the tags are **legitimate content** when they appear
inside reachable tool arguments. The sanitizer should be narrowed/failed-closed, not
silently stripped.

---

## Executive Summary

`strip_thinking_blocks()` in `agent_cascade/tools/base.py:139-179` (applied to every
string tool argument before JSON-schema validation) and the unanchored
`_normalize_thinking_blocks()` in `execution_engine.py:276-297` (applied to raw
`function_call.arguments`) both attempt to remove thinking tags that "leaked" into tool
argument JSON strings.

**Finding: In practice, thinking tags NEVER end up inside reachable tool-argument JSON
strings in a way that warrants silent stripping.** Every path that could put a tag
inside an argument value is either:

1. **Prevented by the anchored design** — `strip_thinking_blocks` (utils/thinking_block.py)
   and `json_loads` (utils/utils.py:506-584) only strip *start-anchored* tags; tags
   inside JSON string values are deliberately preserved (proven by
   `tests/test_json_robustness.py` Test 2/3/6 from commit f6c272c).
2. **Legitimate content** — tags inside `new_content`/`content`/`command` args are real
   user data (code being written to a file, text being edited) and were the *cause of the
   original data-corruption bug* that the anchorage was created to fix
   (`.agent_lessons/think_tag_fix.md`, commit f6c272c).
3. **A corrupted call from a misbehaving model** — where the correct action is to fail
   closed (broken-json detection exists: `_is_incomplete_state()` in
   execution_engine.py:390-458), not silently retry with mangled data.

Zero of 421 agent-session JSONL logs examined contain a real occurrence of a thinking
tag inside parsed tool-call arguments (all 7 regex hits were the coder's own test code
referencing `strip_thinking_blocks` string literals).

**Recommendation: Treat `strip_thinking_blocks()` on tool args as handling a
non-reproducible edge case, and *fail closed instead of silently stripping* —
or at minimum narrow it to only the `_call_tool` entry point and only the
start-of-string (argument-level, not value-level) positions. Remove the unanchored
`_normalize_thinking_blocks` on `arguments` which demonstrably corrupts JSON string
values.**

---

## Key Findings

### 1. How thinking tags enter the LLM output pipeline

Three distinct model behaviors place thinking tags **in the assistant message**, not in
tool args:

- **(a) Native function-calling mode (default)**: `llm/function_calling.py:28-36` —
  `fncall_prompt_type` defaults to `'native'`; tools are passed via the OpenAI `tools`
  parameter, the SDK parses `tool_calls` into structured `function_call` objects. Tags
  are returned in `reasoning_content`, which is **explicitly NOT scanned** for tool
  calls (`execution_engine.py:5471-5474`).
- **(b) Legacy XML mode (`fncall_prompt_type='nous'`)**: `nous_fncall_prompt.py:206-216`
  explicitly checks `' thinking' in item_text` and routes thought text into
  `new_content` — deeming tags in content to be *thinking content, not function args*.
  The `<tool_call>` XML → `FunctionCall(arguments=json.dumps(fn['arguments']))`
  conversion (nous_fncall_prompt.py:373-386) parses only the JSON portion after the
  tags are split off. **There is no code path that copies a thinking block into an
  `arguments` value.**
- **(c) Content-embedded tool calls** (`_extract_tool_calls_from_text`,
  execution_engine.py:323-358): regexes capture `✿ARGS✿` / `<parameter>` blocks;
  nothing in this path injects thinking tags into the captured args.

### 2. The extraction step cannot produce tags inside parsed JSON values

- `BaseChatModel._sanitize_fn_args` (llm/base.py:647-677) requires `arguments` to parse
  as a JSON object via `json_loads`, else defaults to `'{}'`. A tag inside a parsed
  JSON **value** survives because `json_loads` only strips start-anchored tags.
- `_verify_json_format_args` (tools/base.py:146-179) parses with `json_loads` (which
  itself strips only start-anchored tags), then runs `strip_thinking_blocks(v)` on each
  **string value** after parsing. Because the sanitizer is start-anchored, a tag inside
  a value is untouched (verified empirically below).

### 3. When tags DO appear in the raw arguments string (pre-parse), it's a different bug

The one real "contamination" path is **a model emitting a thinking block *before* the
JSON object in the raw arguments string**, e.g.:

```
"arguments": " thinking\nReasoning\n response\n{\"path\": \"x\"}"
```

This is handled exactly once at parse time — `json_loads` strips start-anchored tags
(utils/utils.py:513-533) so the JSON parses cleanly. **No runtime corruption occurs in
this path.**

### 4. The two sanitizers that strip from *inside* values are the only danger

- `execution_engine._normalize_thinking_blocks` (execution_engine.py:276-297) uses
  **UNANCHORED regexes** (`<think.*?</think>` with `re.DOTALL`) and is applied to the
  **whole raw arguments string** via `_normalize_tool_arguments`
  (execution_engine.py:361-373) in `_normalize_turn_output` (execution_engine.py:3783-3787).
  **Empirical test (this investigation):** a tag inside a JSON string value —
  `{"content":" next"}` — is **destroyed**,
  producing `{"content":" next"}`. This is the exact data-corruption class that
  `.agent_lessons/think_tag_fix.md` says the anchored regexes were created to prevent.
  It runs on every turn (`_normalize_turn_output` is called at execution_engine.py:4346
  before tool dispatch), so **every tool argument passes through an unanchored
  tag-stripper**.
- `tools/base.py:163-167` (`strip_thinking_blocks(v)` per string value) and its
  `file_ops.py:766-804` override (which already special-cases `new_content`) are
  start-anchored, so they only strip tags that are at the **start of a value**. This
  means a legitimate value that itself *begins* with a tag-like pattern (e.g. code
  starting `<think...`) would still be corrupted — less common than the unanchored
  case, but the same class of bug. Reviewer correction: the report's original "cannot
  corrupt values" phrasing was too strong.

### 5. Historical evidence: this class of bug already happened and was fixed by anchoring

- Commit `f6c272c` (2026-05-20) "prevent LLM server crashes from corrupted function_call
  arguments": added anchored regexes + `_verify_json_format_args` sanitization,
  **and** `tests/test_json_robustness.py` which asserts (Test 2, 3, 6) that tags inside
  JSON values are **preserved**, not stripped.
- `.agent_lessons/think_tag_fix.md` (same commit): "patterns lacked start-of-string
  anchors (`^`), causing them to strip tags and their content from *anywhere* in the
  text. This led to **data corruption and crashes when tag-like patterns appeared inside
  tool arguments** (e.g., code being written to a file...)". I.e., the system already
  learned that unanchored stripping corrupts tool args. Yet commit `573b236` later
  re-introduced an **unanchored** `_normalize_thinking_blocks` applied to
  `function_call.arguments`.
- The very recent `edit_file` whitespace bug (`investigation_report_edit_file_delete_and_insert.md`,
  `.agent_lessons/edit_file_whitespace_bug_fix.md`) shows the *anchored* sanitizer's
  residual `.strip()` already corrupted `new_content`, and the fix was to skip
  sanitization for `new_content` entirely (file_ops.py:795-796) — same conclusion:
  tags in code content are legitimate.

### 6. Log evidence: no real occurrences found

Scanned 421 agent-session JSONL logs across `AgentWorkspace\logs` and `AgentCascade\logs`
for `function_call.arguments` / `tool_calls[].function.arguments` containing
`<think>`, `<thought>`, `[THINK]`, `<|channel>thought`:

- **7 raw hits — all false positives** (coder's own test code / string literals in
  `coder_fix_strip_whitespace_bug_20260809_233023.jsonl` calling
  `strip_thinking_blocks`; no real tag-in-arg data).
- `console.log.2` jsonschema-validation tracebacks at lines 7724/12699/12795 are
  unrelated validation failures (not thinking-tag corruption).

---

## How the pipeline currently handles it (defense-in-depth audit)

| Layer | Code | Behavior |
|---|---|---|
| `json_loads` | utils/utils.py:506-584 | Strips **start-anchored** tags only (safe) |
| `_verify_json_format_args` | tools/base.py:139-179 | Strips tags from parsed string **values** (start-anchored; can't corrupt values, but can drop leading legit tags in edge cases; EditFile overrides for `new_content`) |
| `_normalize_tool_arguments` | execution_engine.py:361-373 → 276-297 | **UNANCHORED** strip on raw `arguments` string — **corrupts JSON values; this is the actual problem** |
| `_sanitize_fn_args` | llm/base.py:647-677 | Defaults invalid args to `'{}'` — fail-closed at transport level |
| `_is_incomplete_state` | execution_engine.py:390-458 | Detects broken-json tool calls → dump/retry (fail-closed exists) |

---

## Verdict / Recommendation

**Q: Is this a real problem we need to handle, or a corrupted call we should reject?**

**A: Both — but the current implementation handles the wrong side of it.**

1. **FAIL CLOSED for genuinely corrupted calls.** When a model emits a tag inside the
   raw arguments string such that the resulting JSON is malformed, the system already
   has the correct machinery: `_sanitize_fn_args` defaults to `'{}'` with a warning, and
   `_is_incomplete_state` ("broken-json") triggers a dump-and-retry. **Silent stripping
   hides this and executes a tool with data the model never sent** — exactly the 
   "corrupted call" case the question asks about. Do NOT silently strip here.

2. **DO NOT strip tags that are legitimately inside values.** Every reachable real case
   (code content, edit_file `new_content`, shell commands) is legitimate data. The
   tests (`test_json_robustness.py`) and the lesson (`think_tag_fix.md`) codify this.

3. **Remove or anchor `_normalize_thinking_blocks` on arguments.** The unanchored strip
   in execution_engine.py:294-296 demonstrably destroys JSON string values and was
   previously identified as the root cause of data corruption (f6c272c lesson). It is
   applied on every turn before dispatch. **This is the code path that should be fixed.**

4. **Concrete minimal action:**
   - In `_normalize_tool_arguments`, do NOT strip tags from `arguments`. Either remove
     the call (keep reasoning_content/content normalization only) or, if keeping,
     anchor it to the *start of the arguments string only* (pre-JSON), never inside.
   - In `tools/base.py:163-167`, restrict sanitization to the **raw argument string
     before parsing** (start-anchored, i.e., `json_loads` already does this) and remove
     per-value stripping, or make it fail-closed (raise `ValueError` when a tag is
     detected inside a parsed value instead of silently deleting content).
   - Keep `EditFile._verify_json_format_args` override (skip `new_content`) and extend
     the same "content-bearing args are legitimate" principle to `write_file`,
     `shell_cmd`, `call_agent` task text.

---

## Confidence Levels

- **Confirmed**: `strip_thinking_blocks` is start-anchored; it cannot strip tags inside
  JSON values (empirically verified, and codified in tests+lesson).
- **Confirmed**: The unanchored `_normalize_thinking_blocks` applied to
  `function_call.arguments` corrupts JSON string values (empirically verified).
- **High**: No real tag-in-arg occurrences in 421 session logs (0 true positives).
- **High**: Fail-closed machinery (`_sanitize_fn_args`, `_is_incomplete_state`) exists
  and is the correct handling for genuinely corrupted calls.
- **Moderate**: Exact frequency of the "model emits tag before arguments" raw-string
  case is unknown (logs only show parsed args; raw pre-parse strings aren't logged
  consistently).

## Open Questions

1. Is there historical evidence (pre-May-2026, error.txt/commits) of a model actually
   emitting a thinking block *inside* an `arguments` JSON value? The current test suite
   and lessons suggest the original May-2026 corruption was from **unanchored stripping
   of legitimate content**, not from models emitting tags.
2. Which backend/model configurations use `fncall_prompt_type='nous'` vs `'native'`?
   The legacy XML parser is the only path where raw text (with possible tags) is
   converted to `arguments`; if `native` is always used, the whole strip-inside-value
   concern is for `qwen`/`nous` legacy modes only.

## Suggested Next Actions

1. **Fix first**: anchor/remove `_normalize_thinking_blocks` in
   `_normalize_tool_arguments` (execution_engine.py:361-373) — this is an
   actively-corrupting path on every turn.
2. **Narrow `tools/base.py`**: remove per-value `strip_thinking_blocks`; rely on
   `json_loads` start-anchored stripping for the raw string, and fail-closed
   (raise/error) if tags remain inside parsed values.
3. **Add a regression test**: `BaseTool._verify_json_format_args` receiving
   `{"new_content": "  <think>literal code</think>rest"}` must preserve the value
   (mirroring `test_delete_and_insert_preserves_whitespace_via_tool_pipeline`).
4. **Log raw pre-parse arguments** at debug level so future corrupted-call cases are
   visible and distinguishable from legitimate content.
5. Report to Maine: the sanitizer in tools/base.py handles an edge case that the
   anchoring design already makes unreachable; the real risk is the unanchored strip in
   execution_engine.py.