# Investigation Report: edit_file delete_and_insert "wrong line removed" (line 870, api_integration.py)

**Investigating agent**: researcher · `investigate_edit_file_bug`
**Date**: 2026-08-09
**Status**: Root cause confirmed — the range logic is correct; the incident is a mix of
a user error (empty `new_content` on a "replace" intent) and a REAL sanitizer bug that
corrupts indented multi-line `new_content` in `delete_and_insert` mode.

---

## Executive Summary

The report that `delete_and_insert` mode "removes the wrong line" for range `"870:870"`
is **not** a range-parsing bug. On the first flagged attempt the tool deleted exactly
line 870, which is what `"870:870"` + empty `new_content` means (delete-only). The
coder's actual intent was *replace* line 870, so the tool did what the arguments asked,
not what the agent intended.

A **second**, real defect made the incident worse: the universal tool-argument
sanitizer (`strip_thinking_blocks`, which ends with `return data.strip()`) strips the
leading indentation from the **first line of `new_content`** for every tool call. When
the coder retried with an indented replacement, the first inserted line landed
flush-left, mangling the Python indentation. This is an active bug that can corrupt any
whitespace-sensitive multi-line string argument of any tool.

The `delete_and_insert` mode itself passes all 20 unit-test scenarios.

---

## Key Findings

### 1. What line was actually removed vs. what should have been removed

| Item | Value |
|---|---|
| Requested range | `"870:870"` (single line) |
| Actual line removed (per unified diff in tool result) | Line 870: `'shell_char_limit', 'code_char_limit', 'list_dir_char_limit'):` |
| Line that "should" have been removed | **Same line 870.** The tool removed exactly the requested line. |
| Why it looked wrong | The coder's stated intent was "Replace line 870 to add max_images_for_llm" but `new_content` was `""` → delete-only. No other line could have been removed. |

Evidence: `coder_img_limit_impl_20260809_033719.jsonl` entry 9 shows
`OK: Edited ... lines 870-870 (d&i, deleted 1 lines, -1 +0 = -1net)` with
diff `@@ -867,7 +867,6 @@` removing `- 'shell_char_limit', ...` (todo.md lines 99-120 mirror this).

### 2. The `delete_and_insert` implementation is correct

- `agent_cascade/operation_manager/file_operations.py:577-637` `_parse_range()`:
  `"870:870"` → `(start-1, end)` = `(869, 870)` → Python slice `file_lines[869:870]`
  → exactly one line deleted. End is **exclusive**; the docstring/report wording
  "lines N:N deletes N" is consistent.
- Splice logic at lines 970-1006 (`before = file_lines[:start_idx]; after =
  file_lines[end_idx:]`) is straightforward and correct.
- **Tests pass**: `python -m pytest tests/tools/test_edit_file_modes.py -q` →
  `5 passed in 5.05s` (includes `test_delete_and_insert_mode` with 20 scenarios
  covering delete-only, insert-only, CRLF, clamping, negative indices, empty files).
- History: commit `68e1a6a` (2026-07-30) added the dedicated `range` param and fixed
  a REAL previous bug (negative-index end semantics + single-number insert).
  `delete_and_insert_feature.md` documents the earlier single-number-deletes-a-line
  bug that IS fixed.

### 3. REAL BUG: tool-arg sanitizer strips leading whitespace from multi-line strings

Chain:
1. `agent_cascade/tools/base.py:163-167` — `_verify_json_format_args()` runs
   `strip_thinking_blocks(v)` on **every string tool argument**.
2. `agent_cascade/utils/thinking_block.py:55-91` — `strip_thinking_blocks()` ends with
   **`return data.strip()`** (line 91), stripping leading/trailing whitespace from the
   whole string.
3. Effect on edit_file `delete_and_insert`: `new_content` lines are spliced verbatim
   (file_operations.py:990-1001), so a first line that was indented to align with
   Python continuation lines loses its indent.

Reproduction (actual repo code, exact JSON from log entry 32):
```
raw new_content first line:  "                     'shell_char_limit', ..."   (21 leading spaces)
after strip_thinking_blocks: "'shell_char_limit', ..."                        (0 leading spaces)
```
→ The coder's retry produced `+'shell_char_limit', 'code_char_limit', ...` flush-left
in the diff, and the coder said "the indentation got messed up" and "delete_and_insert
mode has issues with indentation handling."

Scope: **affects every tool argument** that is a whitespace-sensitive multi-line string
(write_file `content`, shell_cmd `command`, etc.), not just edit_file. Single-line
args are unaffected unless they legitimately need leading/trailing whitespace.

### 4. Test gap

`tests/tools/test_edit_file_modes.py` calls `operation_manager.edit_file()` **directly**,
bypassing the tool wrapper (`tools/custom/file_ops.py::EditFile.call()` →
`BaseTool._verify_json_format_args()`), so the sanitizer corruption is invisible to the
existing suite. No tests exercise the sanitizer on whitespace-significant args.

---

## Root-Cause Verdict

1. **"Wrong line removed" (the reported symptom)**: NOT a tool bug. Range
   `870:870` + `new_content=""` correctly deletes line 870. The intent/args mismatch
   (replace vs delete) caused the false impression.
2. **Mangled indentation on retry (the real defect)**: `strip_thinking_blocks()`'s
   unconditional `.strip()` in `tools/base.py`'s universal arg sanitization,
   stripping leading whitespace from the first line of `new_content`. Confirmed by
   code reading + live reproduction + matching diff in the session log.

---

## Supporting Evidence

- Session log: `N:\work\WD\AgentWorkspace\logs\coder_img_limit_impl_20260809_033719.jsonl`
  entries 8-9 (first d&i attempt: exact diff, backup path
  `api_integration.py.1786236685.bak`), entries 32-36 (second attempt: sanitizer
  corruption visible in diff `+'shell_char_limit'...`), entry 37 ("This is getting
  messy with delete_and_insert"), entry 40 user: "i think that mode is bugged...".
- TODO entry: `todo.md` lines 97-120 (the exact reported repro).
- Implementation: `file_operations.py:577-637` (`_parse_range`), `970-1006`
  (d&i splice), `1104-1115` (result message).
- Sanitizer: `tools/base.py:139-179` (`_verify_json_format_args`), import line 22;
  `utils/thinking_block.py:55-91`.
- History: `git log` — `68e1a6a` (range param), `f6c272c` (sanitizer added),
  `baff97fc` (last change to line 870 area — the refactor that introduced
  `POOL_SETTINGS_TO_BROADCAST`).
- Tests: `tests/tools/test_edit_file_modes.py:227-466`; run result `5 passed`.

---

## Confidence Level

**High** (root cause for the mangled-indentation real bug: confirmed by code + live
reproduction of the exact logged call; the "wrong line" symptom: confirmed as
intent/args mismatch with the exact diff evidence).

## Open Questions / Remaining Unknowns

- Whether other in-flight agents have already been bitten by the same sanitizer
  (e.g., write_file content corruption). Worth a quick broad scan of logs for
  "indentation got messed up" style complaints.
- Whether the `.strip()` was load-bearing for any other flow (it was introduced
  intentionally for thinking-tag cleanup; removing it needs care).

## Recommended Next Actions

1. **Fix the sanitizer scoping** in `tools/base.py:163-167`: only call
   `strip_thinking_blocks` when the string actually contains a thinking/thought tag
   (the fast-path check already exists inside the function but the final
   unconditional `.strip()` still runs), or make `strip_thinking_blocks` preserve
   body whitespace (strip only tag-adjacent whitespace).
2. **Add a tool-level regression test**: call `EditFile.call()` with a real JSON
   string containing indented multi-line `new_content` (+ `delete_and_insert`
   range) and assert leading indentation is preserved in the file.
3. **Optionally harden `delete_and_insert` UX**: when `new_content` is empty but the
   justification/old_content suggests replacement, the tool could warn, but this is
   secondary — the primary fix is #1.
4. Re-run the full edit_file test suite after the fix.

## Deliverables

- This report: `N:\work\WD\AgentCascade\investigation_report_edit_file_delete_and_insert.md`
- Memory/lesson saved: `N:\work\WD\AgentCascade\.agent_lessons\edit_file_delete_and_insert_strip_whitespace_bug.md`