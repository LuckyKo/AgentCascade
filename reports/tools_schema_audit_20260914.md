# Tools Schema Audit — 2026-09-14

**Auditor:** ponytail (schema_auditor)
**Scope:** `agent_cascade/prompts/dna.py` TOOL_METADATA, AVAILABLE_TOOLS, XML_CONTENT_FIELDS
**Method:** Static analysis + consumer cross-reference. No files modified.

---

## Summary

The schema is **structurally sound** — the two-format system (simple-string vs structured-dict) works and all consumers correctly read from TOOL_METADATA. The main issues are:

1. **Type inference gaps** (HIGH): ~20 params across 9 tools get emitted as `type: string` in the JSON Schema when they're actually int/bool. This is semantically wrong for LLM function-calling APIs and can cause the model to emit `"5"` instead of `5`.
2. **Duplication** (MED): 3 tool descriptions repeat param-level text verbatim, inflating prompt tokens by ~600 chars total.
3. **Typo** (LOW): `call_agent.context` says "usefull".
4. **Dead XML_CONTENT_FIELDS entries** (LOW): 2 of 9 fields (`old_string`, `new_string`) no longer match any tool param name.
5. **Registry drift** (INFO): `forget_last` has a factory branch in `agent_factory.py:103` but is NOT in AVAILABLE_TOOLS — the branch is unreachable dead code.

**Top 5 by impact:**
| # | Item | Impact |
|---|------|--------|
| 1 | Type inference for int/bool params | LLM correctness (wrong JSON types) |
| 2 | system_info description/help duplication | Token waste in every prompt |
| 3 | view_image description/path duplication | Token waste |
| 4 | shell_cmd execution_mode duplication | Token waste |
| 5 | XML_CONTENT_FIELDS dead entries | Maintenance confusion |

---

## Findings Table

### F1 — Type Inference Gaps (HIGH / correctness)

**Location:** `dna.py` lines 203–658 (all simple-string params)
**Mechanism:** `_agent_instance_proxy.py:32` — any non-dict param value becomes `{'type': 'string', 'description': <value>}`.

**Critical nuance:** Most tools that have their own class-level `parameters` dict (file_ops.py, shell_cmd.py, code_interpreter.py, compression_tools.py, read_logs.py, web_extractor.py, etc.) define **correct types locally** and only pull the *description text* from TOOL_METADATA. The type-inference gap ONLY affects tools registered via `_AgentInstanceFunctionProxy` — which is currently just `call_agent` and `dismiss_agent`.

However, if any new tool is added to AVAILABLE_TOOLS without a dedicated class (or if the proxy pattern expands), the gap becomes live. Also, the emitted schema for `call_agent`/`dismiss_agent` is already correct because they use structured dicts.

**Verdict:** The type-inference gap is **latent, not active**. No currently-registered tool emits a wrong type to the LLM. The risk is future regression if someone adds a tool via the proxy path with simple-string params.

**Affected params (if/when exposed via proxy):**
| Tool | Param | Real type | Emitted as |
|------|-------|-----------|------------|
| read_file | start_line, limit | integer | string |
| list_dir | recursive, max_depth, show_summary, max_entries, files_only, dirs_only | boolean/integer | string |
| grep | context, ignore_vcs, smart_case | integer/boolean | string |
| code_interpreter | fresh, fix_paths | boolean | string |
| shell_cmd | timeout, heartbeat_interval | integer | string |
| compress_context | fraction, force | number/boolean | string |
| forget_last | count | integer | string |
| image_gen | width, height, seed | integer | string |
| web_extractor | extract_images | boolean | string |

**Recommendation:** Add a `# NOTE` comment in dna.py near TOOL_METADATA documenting that simple-string params are description-only and the type must be defined in the consuming tool class. Optionally, add a `'_types'` hint dict per tool for documentation purposes (not consumed by the converter). **Do NOT convert to structured dicts** — this would break all 12+ consumer files that do `TOOL_METADATA[...]['parameters'][...]` expecting a string.

**Risk class:** SAFE (comment-only) vs RISKY (value-type change breaks consumers).

---

### F2 — system_info: description duplicates help param (MED / compactness)

**Location:** `dna.py:377–386`

The tool description (line 383) says:
> "Optionally pass `help="<section>"` to fetch a targeted section of AgentCascade system knowledge (e.g. REST API reference) instead of normal system info; valid sections are listed in the error you get if you pass an unknown value. Use `help="telemetry"` for a live dump of current session telemetry."

The `help` param description (line 386) says:
> "Optional. Fetch a help section about the AgentCascade system instead of normal system info. Valid sections are listed in the error you get if you pass an unknown value (e.g., 'rest_api', 'websocket', 'parallel_instances'). Use 'telemetry' for a live dump of current session telemetry. Leave empty/omit for normal system information."

**Overlap:** ~250 chars repeated verbatim between description and param.

**Proposed fix (safe, text-only):**
- Tool description: remove the help sentence entirely. Replace with: `"Pass 'help' param to fetch a specific AgentCascade documentation section instead of system info."` (~90 chars saved)
- Keep full detail in the `help` param description (it's where the LLM looks for param-specific guidance).

**Savings:** ~160 chars.

---

### F3 — view_image: description duplicates path param (MED / compactness)

**Location:** `dna.py:219–231`

The tool description (lines 224–227) lists all three special paths with full explanations:
> "Special paths: \"__screen_capture\" captures all monitors combined; \"__screen_capture:N\" captures physical monitor N by 0-based index (0=first monitor, 1=second, etc.); \"__window_capture:PID\" captures a specific window by process ID."

The `path` param (line 230) repeats the same info:
> "Special directives: \"__screen_capture\" for full screen capture (all monitors); \"__screen_capture:N\" to capture physical monitor N by 0-based index; \"__window_capture:PID\" to capture a specific window by its process ID."

**Overlap:** ~200 chars.

**Proposed fix (safe, text-only):**
- Tool description: replace the special-paths sentence with: `"Supports special paths for screen/window capture — see 'path' param for details."` (~180 chars saved)
- Keep full detail in the `path` param.

**Savings:** ~180 chars.

---

### F4 — shell_cmd: execution_mode param duplicates description (MED / compactness)

**Location:** `dna.py:363–371`

Tool description line 363:
> "**Execution mode:** \"auto\" (default) = background if timeout>60s, else blocking; \"sync\" = always blocking; \"async\" = always background."

Param `execution_mode` line 371:
> "\"auto\" (default) = background if timeout>60s else blocking; \"sync\" = always blocking; \"async\" = always background. null ≡ auto."

**Overlap:** ~120 chars nearly verbatim.

**Proposed fix (safe, text-only):**
- Tool description: shorten to `"Execution modes: auto/sync/async — see execution_mode param."` (~100 chars saved)
- Keep full detail in the param (it has the extra `null ≡ auto` note).

**Savings:** ~100 chars.

---

### F5 — call_agent.context typo (LOW / correctness)

**Location:** `dna.py:482`

Current: `"Optional background context for the agent instance, usefull for auto skill allocator to match relevant skills."`

Fix: `"usefull"` → `"useful"`. One-character fix.

---

### F6 — XML_CONTENT_FIELDS dead entries (LOW / dead-code)

**Location:** `dna.py:69–80`

Current set:
```python
XML_CONTENT_FIELDS = {
    'content',        # write_file param ✓
    'old_content',    # edit_file param ✓
    'new_content',    # edit_file param ✓
    'old_string',     # ✗ NO tool uses this name (edit_file uses old_content)
    'new_string',     # ✗ NO tool uses this name (edit_file uses new_content)
    'full_content',   # ✗ NO tool uses this name (write_file uses content)
    'code',           # code_interpreter param ✓
    'command',        # shell_cmd param ✓
    'justification',  # write_file/edit_file/delete_file/shell_cmd ✓
    'summary'         # compress_context? No — it's summary_text. ✗
}
```

**Dead entries:** `old_string`, `new_string`, `full_content`, `summary` (4 of 10).

**Consumer:** Only `nous_fncall_prompt.py:67` uses this set, and only in the legacy nous path. The default is 'native' fncall type.

**Recommendation:** Remove the 4 dead entries. Keep the set for the 6 live fields. Add a comment noting this is legacy-only (nous path). If the nous path is ever removed, delete the entire set + XML_MIN_LENGTH.

---

### F7 — Registry drift: forget_last factory branch unreachable (LOW / dead-code)

**Location:** `agent_factory.py:103–105`

`forget_last` is NOT in `AVAILABLE_TOOLS` (dna.py:12–55), so the `elif tool_name == 'forget_last':` branch at agent_factory.py:103 is **unreachable dead code**. It's listed in the "hidden" comment (dna.py:64) and in constants.py disabled lists, but no code path ever iterates it into AVAILABLE_TOOLS.

**Verdict:** Intentional — the tool exists for potential future enablement or manual registration. The factory branch is a forward-looking stub. Not a bug, but worth a `# TODO: add to AVAILABLE_TOOLS when ready` comment or removal if it's truly dead.

**Recommendation:** No action needed. Optionally add a comment at agent_factory.py:103 noting it's intentionally excluded from AVAILABLE_TOOLS.

---

### F8 — Inconsistent `required` usage (LOW / consistency)

**Observation:** Only 3 tools declare an explicit `required` list in TOOL_METADATA:
- `call_agent`: `['agent_class', 'instance_name', 'task']` (line 513)
- `dismiss_agent`: `[]` (line 530)
- `load_skill`: `['skill_names']` (line 656)

All other tools omit `required`, so the converter defaults to **all params required** (line 40 of _agent_instance_proxy.py). This means e.g. `read_file` emits `required: ['path', 'start_line', 'limit']` — but start_line and limit are clearly optional!

**However:** This only matters for proxy-registered tools (call_agent, dismiss_agent). All other tools define their own `parameters` dict in their class with correct `required` lists. So the emitted schema is correct for all currently-active tools.

**Verdict:** Latent issue, same as F1. No active bug. If the proxy pattern expands, add explicit `required` lists to those entries.

---

### F9 — Inconsistent "Optional:" prefix style (LOW / consistency)

Some params start with "Optional:" or "Optional." (e.g., read_file.start_line line 214, code_map.force_as line 586), while others embed optionality in the sentence (e.g., list_dir.recursive "When true..." line 285). This is a style inconsistency but has zero functional impact.

**Recommendation:** Low priority. If touching these strings for other reasons, normalize to: params with defaults don't need "Optional:" prefix — the `default:` clause already implies optionality.

---

### F10 — call_agent.load_skill / load_skill.skill_names oneOf schema (INFO)

**Location:** `dna.py:493–511` and `dna.py:641–654`

Both use `oneOf` with array/string/null variants. This is valid JSON Schema and works with OpenAI-compatible APIs. The `call_agent.load_skill` version includes a `type: null` branch (line 506) which some older API implementations may not handle well, but modern OpenAI/Anthropic APIs accept it.

**Simplification opportunity:** The `load_skill.skill_names` (lines 641–654) does NOT include a null branch — it's just string|array. This is fine since `required: ['skill_names']` makes it mandatory anyway.

**Verdict:** No action needed. Schemas are valid and functional.

---

### F11 — code_interpreter description missing space (LOW / correctness)

**Location:** `dna.py:350–351`

```python
'Use system_info to find exact path mappings for extra workspaces.'
'Missing packages can be installed as the container will be reused in follow up queries.'
```

These two adjacent string literals concatenate without a space: `"...workspaces.Missing packages..."`. Python implicit string concatenation doesn't add a separator.

**Fix:** Add a trailing space to line 350 or leading space to line 351.

---

### F12 — list_dir.min_size/max_size are strings, not numbers (INFO)

**Location:** `dna.py:292–293`

These accept human-readable sizes like `'1.5KB'` — so `type: string` is actually **correct** for these two params. They're NOT type-inference gaps. The file_ops.py consumer correctly declares them as `type: 'string'` (lines 1064, 1068).

---

## Safe-to-Do vs Needs-Review

### SAFE — pure text compaction (no consumer impact)
These changes only modify the *description text* within TOOL_METADATA. All consumers read `TOOL_METADATA[...]['parameters'][...]` as a string and pass it through — changing the string content is invisible to them.

| Change | File:Line | Savings |
|--------|-----------|---------|
| F2: system_info description trim | dna.py:383 | ~160 chars |
| F3: view_image description trim | dna.py:224–227 | ~180 chars |
| F4: shell_cmd description trim | dna.py:363 | ~100 chars |
| F5: "usefull" → "useful" | dna.py:482 | 0 (typo fix) |
| F11: missing space | dna.py:350–351 | 0 (bug fix) |

**Total safe savings: ~440 chars** (roughly 110 tokens).

### NEEDS REVIEW — structural changes (consumer impact)
These would change the *value type* of a param from string to dict, or rename/remove keys. Each requires updating all consumer files.

| Change | Risk | Consumers affected |
|--------|------|-------------------|
| Convert any simple-string param to structured dict | HIGH | 12+ files do `TOOL_METADATA[...]['parameters'][name]` expecting a string; would get a dict → `.description` attribute error or type confusion |
| Remove XML_CONTENT_FIELDS dead entries | LOW | Only nous_fncall_prompt.py (legacy path) |
| Add explicit `required` to more TOOL_METADATA entries | MED | No consumer reads `required` directly, but changes emitted schema for proxy tools |

**Recommendation:** Do NOT convert params to structured dicts. The dual-format system works; the consumers are the constraint. If type correctness is ever needed at the metadata level, add a separate `_type_hints` dict that's documentation-only.

---

## Consumer Map (for reference)

Files that read `TOOL_METADATA[...]['parameters'][...]` as a **string**:
| File | Tools consumed |
|------|---------------|
| tools/custom/file_ops.py | read_file, view_image, write_file, edit_file, list_dir |
| tools/custom/shell_cmd.py | shell_cmd |
| tools/code_interpreter.py | code_interpreter (code param only) |
| tools/doc_parser.py | doc_parser |
| tools/web_extractor.py | web_extractor |
| tools/web_search.py | web_search |
| tools/retrieval.py | retrieval |
| tools/image_gen.py | image_gen (description only, params hardcoded) |
| tools/custom/calculation.py | calculate |
| tools/custom/code_map.py | code_map |
| tools/custom/read_logs.py | read_logs |
| tools/custom/compression_tools.py | compress_context (fraction, mode, summary_text) |

Files that use `_build_schema_from_metadata` (proxy path):
| File | Tools |
|------|-------|
| tools/_agent_instance_proxy.py | call_agent, dismiss_agent |

---

## Quantified Token Cost

Current TOOL_METADATA dict is ~42 KB of Python source. The *emitted* JSON Schema (what goes into the LLM prompt) is smaller — roughly 8–10 KB for all 30 tools combined. The safe compactions above save ~440 chars (~110 tokens) from the emitted schema, which is injected into **every agent's system prompt on every turn**.

At ~5 agents running concurrently with ~20 turns each, that's roughly **11,000 tokens saved per session** from the safe changes alone.

---

## Recommended Action Plan (for coder)

**Phase 1 — Safe text fixes (no consumer risk):**
1. Fix "usefull" → "useful" (F5)
2. Fix missing space in code_interpreter description (F11)
3. Trim system_info description (F2)
4. Trim view_image description (F3)
5. Trim shell_cmd execution_mode duplication (F4)

**Phase 2 — Dead code cleanup (low risk):**
6. Remove 4 dead entries from XML_CONTENT_FIELDS (F6)
7. Add comment to agent_factory.py:103 re: forget_last being intentionally hidden (F7)

**Phase 3 — Documentation (no functional change):**
8. Add a NOTE comment in dna.py explaining the two-format system and that simple-string params are description-only (F1/F8)

**Do NOT do:** Convert any param from string to dict. The consumer constraint makes this a 12-file refactor for zero active benefit (all live tools already emit correct types via their class-level schemas).
