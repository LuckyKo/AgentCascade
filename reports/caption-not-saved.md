# Root-Cause Investigation: Session Caption Not Saved to Line-1 Metadata

**Investigator:** caption-save-recon (researcher)
**Mode:** Investigative (read-only)
**Date:** 2026-08-29
**Confidence:** Confirmed (both logs + full code chain traced)

---

## 1. Executive Summary

The compressor **did** produce a correct caption, and the parse/hand-off/`set_caption`/rewrite
machinery is individually sound. The caption is dropped **specifically on the `/compress` command
path**, which splits a single compression into **two** `compress_context` calls:

1. a **dry-run preview** (`dry_run=True`) that *parses* the caption but **skips** `set_caption`
   (guarded by `and not dry_run`) and returns **only the summary body** (`CompressResult` has no
   caption field), and
2. a **precomputed apply** (`precomputed_summary=…`, `dry_run=False`) that re-runs with an **empty**
   `generated_caption` and therefore also skips `set_caption`.

Net effect: `set_caption` is never called with a real caption, so the in-memory metadata stays
`caption: ""`, and the subsequent compression rewrite re-emits line 1 with that empty value.

**Exact drop point (primary):** `agent_cascade/compression/core.py:569` —
`if generated_caption and not dry_run:` suppresses `set_caption` during the dry-run preview; the
caption is then structurally unable to reach the apply step because `CompressResult`
(`agent_cascade/compression/result.py:5-16`) has **no caption field** and the dry-run return
(`agent_cascade/tools/custom/compression_tools.py:148`) forwards only `result.summary_text`.

---

## 2. What the Logs Show

### Log #1 — `compressor_Compressor_1_20260829_101124.jsonl` (compressor's own log)
- **Line 1 metadata:** `caption: ""` (empty — expected; the compressor's *own* session has no caption).
- **Line 4 (final assistant output):** **contains the caption** on the end-marker line:
  ```
  --- END SUMMARY --- CAPTION: Todo 139 fix: Add self-call/identity guards; test failure shows name-variant rejection conflicts with recall logic.
  ```
  Caption text (verbatim):
  `Todo 139 fix: Add self-call/identity guards; test failure shows name-variant rejection conflicts with recall logic.`

**→ The parser source is correct.** Feeding this exact line 4 through a faithful re-implementation
of `_parse_compression_output` (marker `"--- END SUMMARY ---"`, regex
`^\s*CAPTION:\s*(?P<caption>[^\n]*)\s*$`) yields:
- `summary_body` = the clean summary (marker + caption stripped)
- `caption` = `Todo 139 fix: Add self-call/identity guards; test failure shows name-variant rejection conflicts with recall logic.`

### Log #2 — `orchestrator_Maine_20260829_083426.jsonl` (the session that SHOULD carry the caption)
- **Line 1 metadata:** `caption: ""` — **present but empty string** (not missing; the key exists,
  value is `""`). `last_update: 2026-08-29T10:11:24.479407`.
- **Line 200 (decisive):** `{"role": "user", "content": "/compress 0.5", "timestamp": "2026-08-29T10:11:24.480785"}`
- **Line 201:** `[COMPRESSION] manual compression complete. Context: 22396/120000 tokens …` (10:12:58)

**→ The caption is ABSENT (empty string) in the orchestrator's line-1 metadata.**
The compression that produced the caption was a **`/compress` command** (line 200), *not* a
forced/auto (token-threshold) compression. This is the path where the drop occurs.

---

## 3. The Full Chain (file:line), and Where It Breaks

### 3.1 Parse — `agent_cascade/compression/agent_invoker.py` — ✅ WORKS
- `agent_invoker.py:55` `def _parse_compression_output(raw) -> Tuple[str, str]`
- `agent_invoker.py:107-121` — captures the single-line caption tail; returns `(summary_body, caption)`.
- `agent_invoker.py:451` `summary_body, caption = _parse_compression_output(summary)`
- `agent_invoker.py:453` `return summary_body, caption`

### 3.2 Hand-off — `invoke_compression_agent` — ✅ WORKS (caption IS returned up)
- `agent_invoker.py:594-598` retry loop:
  ```python
  summary, caption = _execute_compressor_and_extract_summary(
      agent_pool, engine, comp_instance, comp_state_key, caller_name,
      timeout_label="Compression",
  )
  return summary, caption          # ← caption propagates out
  ```

### 3.3 Store — `agent_cascade/compression/core.py` — ⚠️ PARTIALLY WORKS
- `core.py:527` `generated_caption = ""`
- `core.py:528-532` — `if precomputed_summary:` / `elif mode == "manual":` branches leave
  `generated_caption` as `""` (no caption exists for precomputed/manual paths).
- `core.py:537-542` — `else` branch (auto/forced): `generated_summary, generated_caption = invoke_compression_agent(...)`.
- `core.py:569` **`if generated_caption and not dry_run:`** ← **DROP POINT A**
- `core.py:574` `_log_inst.set_caption(generated_caption)` (only reached when a caption is present **and** not a dry-run).
- `core.py:579-594` `if dry_run:` — early return (no pool/logger mutation).

**So for forced/auto (non-dry-run) compression, `set_caption` IS correctly called (core.py:574),
and the feature works.** The integration test `tests/test_session_caption.py:308-326`
(`invoke_compression_agent → ("Clean summary body", "My session caption")`) confirms this path.

### 3.4 Persist — `agent_cascade/logger/agent_instance_logger.py` — ✅ WORKS
- `agent_instance_logger.py:118-139` `set_caption()` — **in-memory only**, first-meaningful-wins;
  updates `data["metadata"]["caption"]`.
- `agent_instance_logger.py:689` (in `_sync_marker_single_write`, reached via
  `reset_history(rewrite=True)` at `:840-841`, triggered by
  `handler._sync_logger_after_compression` → `handler.py:631`):
  ```python
  lines = [json.dumps({"metadata": self.data["metadata"]}, ensure_ascii=False) + '\n']
  ```
  The rewrite **re-emits line 1 with the full in-memory `data["metadata"]`**, which includes `caption`.
  → The persist mechanism is correct; it faithfully re-writes whatever `caption` value is in memory.

### 3.5 THE ACTUAL BREAK: the `/compress` command path (this incident)

The orchestrator log line 200 (`/compress 0.5`) routes through
`CompressionHandler.handle_compress_command` (`handler.py:1268`):

**Step 2 — preview (dry-run):** `handler.py:1294` → `generate_compression_preview` (`handler.py:1055`)
- `handler.py:1090-1096` calls `compress_tool.call(preview_params, …, dry_run=True)`.
- Inside `compress_context`, `dry_run=True` + no `precomputed_summary` → `else` branch
  (`core.py:537`) → `invoke_compression_agent` **parses the caption** (agent_invoker.py:451/598).
- **But** `core.py:569` `if generated_caption and not dry_run:` is **False** (dry_run=True) →
  **`set_caption` is skipped** (core.py:574 not reached).
- `core.py:579-594` returns early. The tool returns **only the body**:
  - `compression_tools.py:147-148` `if dry_run: return result.summary_text`
  - `CompressResult` (`result.py:5-16`) has **NO caption field** → the parsed caption cannot be
    propagated out of the preview at all.
- `handler.py:1105` `return (summary, 'success')` — `summary` is the **body only** (marker+caption already stripped).

**Step 3 — apply (precomputed, NOT dry-run):** `handler.py:1314` → `apply_approved_compression` (`handler.py:1159`)
- `handler.py:1192-1198` calls `compress_tool.call(apply_params, …, precomputed_summary=summary)`.
- `compress_context` sees `precomputed_summary` set → `core.py:528-530`
  `generated_summary = precomputed_summary.strip()`, and `generated_caption` **remains `""`**
  (core.py:527) — the caption was already stripped in the preview and is not re-parsed.
- `core.py:569` `if generated_caption …` is **False** (caption is `""`) → **`set_caption` skipped again** (core.py:574 not reached).
- This step is *not* a dry-run, but it has **no caption to set**.

**Result:** `set_caption` is never invoked with the real caption. In-memory
`data["metadata"]["caption"]` stays `""`. The post-compression rewrite
(`handler.py:1212` → `reset_history(rewrite=True)` → `agent_instance_logger.py:689`) re-emits
line 1 with `caption: ""` — exactly what log #2 line 1 shows.

---

## 4. Root Cause (2–3 sentences)

The caption feature works for forced/auto compression but is broken for the `/compress` command
path, which splits one compression into a **dry-run preview** followed by a **precomputed apply**.
During the preview, `core.py:569` (`if generated_caption and not dry_run:`) suppresses `set_caption`,
and the dry-run return (`compression_tools.py:148`, `CompressResult` has no caption field) forwards
**only the summary body**, so the parsed caption is discarded before it can reach the apply step.
The apply step then re-runs `compress_context` with `precomputed_summary` and an **empty**
`generated_caption` (core.py:527/528), so `set_caption` is skipped a second time — and the later
line-1 rewrite faithfully re-emits the still-empty `caption: ""`.

---

## 5. Suggested Fix (minimal, precise — RECOMMENDATION ONLY, not implemented)

Goal: the caption parsed in the preview must reach `set_caption` on the **apply** step (which is the
non-dry-run call where the logger is safe to mutate), and then be persisted by the existing rewrite.

**Primary fix — carry the caption from the preview into the apply step:**

1. **`agent_cascade/compression/result.py:5-16`** — add a field to `CompressResult`:
   ```python
   caption: str = ""   # Parsed session caption (from compressor output); "" if absent
   ```

2. **`agent_cascade/compression/core.py`** — populate it on the return, and relax the store guard so
   the *preview* can stash the caption even when dry-run (store happens on apply, not preview):
   - In the `else` branch (core.py:537), `generated_caption` is already set from
     `invoke_compression_agent`.
   - On the dry-run early return (core.py:584-594) and the success return (core.py:719-729), set
     `caption=generated_caption` on the returned `CompressResult`.
   - Keep `core.py:569` guard as-is for the preview (don't mutate the logger in a dry-run) — the
     caption is only *stashed* in the result; the actual `set_caption` still fires on the apply step.

3. **`agent_cascade/tools/custom/compression_tools.py:147-148`** — change the dry-run return to carry
   both values instead of just the body:
   ```python
   if dry_run:
       return result.summary_text, result.caption   # (body, caption)
   ```
   (Update the two callers below to unpack the 2-tuple.)

4. **`agent_cascade/compression/handler.py`**
   - `generate_compression_preview` (handler.py:1090-1105): unpack
     `summary, caption = compress_tool.call(...)` and `return (summary, caption, 'success')`.
   - `handle_compress_command` (handler.py:1294-1314): unpack the 3-tuple and pass `caption` into
     `apply_approved_compression`.
   - `apply_approved_compression` (handler.py:1159-1212): after the apply call succeeds, if a caption
     was carried over, invoke it explicitly (the apply step's own `generated_caption` is empty):
     ```python
     if caption:
         _inst = self.pool.get_instance(inst_name)
         _cls = getattr(_inst, 'agent_class', None) or inst_name
         self.pool.get_logger(inst_name, _cls).set_caption(caption)
     ```
     Place this **before** `_sync_logger_after_compression` (handler.py:1212) so the subsequent
     `reset_history(rewrite=True)` → `_sync_marker_single_write` (agent_instance_logger.py:689)
     re-emits line 1 with the caption already in memory.

**Why this is minimal:** it reuses the existing, already-correct `set_caption` (first-wins) and the
existing rewrite persistence (agent_instance_logger.py:689). No new full-file rewrite is added; the
caption flows through the same `CompressResult` the preview already returns.

**Alternative (smaller blast radius, more rework):** have the apply step re-run the parser on the
raw compressor output instead of accepting `precomputed_summary`. Rejected — the preview has already
consumed/discarded the raw output; re-deriving it would require threading raw text through the same
2-tuple anyway, so option above is strictly simpler.

---

## 6. Open Questions / Limitations

- **Timestamp anomaly (noted, not blocking):** line-1 `last_update` is `10:11:24.479` (1 ms *before*
  the `/compress` command at `10:11:24.480`), yet the compression applied ~10:12:58. This suggests
  line 1 may not be re-stamped on every append (only `log_message`→`update_timestamp` mutates it, and
  the metadata line is rewritten wholesale from the in-memory dict). This does **not** affect the
  caption conclusion (caption is `""` regardless), but the line-1 rewrite timing/stamping behavior is
  worth a separate look.
- The **forced/auto** path was not re-exercised end-to-end in the logs here; the fix above is
  backward-compatible with it (caption already works there per `test_session_caption.py`).
- Whether a caption should *also* be set on the preview (dry-run) call is a product decision; this fix
  defers it to the apply step to avoid mutating the logger during a no-op preview.

---

## 7. Evidence Index (file:line)

| Step | Location | Behavior |
|------|----------|----------|
| Parse | `agent_invoker.py:55`, `:107-121`, `:451`, `:453` | ✅ returns `(body, caption)` |
| Hand-off | `agent_invoker.py:594-598` | ✅ `return summary, caption` |
| Store (guard) | `core.py:569` | ⚠️ `and not dry_run` skips `set_caption` in preview |
| Store (call) | `core.py:574` | `set_caption(generated_caption)` — only reached when caption present & not dry-run |
| Dry-run return | `compression_tools.py:147-148` | ⚠️ returns only `result.summary_text` |
| Result type | `result.py:5-16` | ⚠️ **no caption field** |
| Preview | `handler.py:1055`, `:1090-1096`, `:1105` | dry_run=True, body only |
| Apply | `handler.py:1159`, `:1192-1198`, `:1212` | precomputed_summary, empty caption |
| Persist | `agent_instance_logger.py:118-139`, `:689`, `:840-841` | ✅ rewrite re-emits line 1 from in-memory `data["metadata"]` |
| Log evidence | `compressor_…101124.jsonl` line 4; `orchestrator_…083426.jsonl` line 1, line 200 | caption present in #1, `""` in #2; `/compress 0.5` |
