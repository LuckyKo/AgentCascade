# Recon: parseable "caption" for compression output + log metadata + UI

Read-only investigation of AgentCascade to ground a spec for adding a one-line
**caption** to the compression marker, the log metadata line (line 1), and the UI
agent/session display. All citations are `file:line` with real excerpts.

**Key constants (single source of truth):**
- `COMPRESSION_MARKER = "--- CONTEXT COMPRESSED"` — `agent_cascade/prompts/dna.py:88`
- `COMPRESSION_END_MARKER = "--- END SUMMARY ---"` — `agent_cascade/settings.py:129`

---

## 1. Compression marker format

**Two distinct markers exist — do not confuse them:**

| Constant | Value | Defined at |
|---|---|---|
| `COMPRESSION_MARKER` (START) | `--- CONTEXT COMPRESSED` | `agent_cascade/prompts/dna.py:88` |
| `COMPRESSION_END_MARKER` (END) | `--- END SUMMARY ---` | `agent_cascade/settings.py:129` |

**The final marker text is assembled by `COMPRESSION_BASELINE_TEMPLATE`** —
`agent_cascade/prompts/dna.py:103-108`:

```python
COMPRESSION_BASELINE_TEMPLATE = (
    COMPRESSION_MARKER + " ({header}) ---\n"      # → "--- CONTEXT COMPRESSED (50% of history summarized) ---\n"
    "<context_summary>\n"
    "{summary}\n"
    "</context_summary>"
)
```

So the full marker message content is **multi-line**:

```
--- CONTEXT COMPRESSED (50% of history summarized) ---
<context_summary>
<LLM summary text>
</context_summary>
```

- **START marker:** line 1 is `--- CONTEXT COMPRESSED (<header>) ---`. No separate literal START constant is emitted beyond the `COMPRESSION_MARKER` prefix.
- **END marker:** the template **does NOT currently emit `COMPRESSION_END_MARKER`** (`--- END SUMMARY ---`). The end marker is only in the *prompt* (told to the compressor LLM to append) and is stripped on the way back (see §2/§3). The template terminates at `</context_summary>` (dna.py:107).
- **Where text after the END marker would land:** appended after `</context_summary>` (last line of the template, dna.py:107). A caption line would be added as a new trailing line in this template.

**Function that builds it:** `build_marker_message()` — `agent_cascade/compression/helpers.py:381-399`:

```python
def build_marker_message(summary_text, fraction):
    pct = int(fraction * 100)
    header = f"{pct}% of history summarized"
    content = COMPRESSION_BASELINE_TEMPLATE.format(header=header, summary=summary_text)
    return Message(role=USER, content=str(content))
```

There is a parallel `build_consolidation_marker_message()` at `helpers.py:402-421`
(L2 consolidation, same template, different header).

**Storage:** the marker is stored as the `content` field of a `USER`-role `Message`,
which is serialized as a JSON object into the JSONL `history` array (each message is
one JSONL line; metadata is line 1). It is inserted into the pool at
`agent_cascade/compression/core.py:610` (`new_history = history[:active_start_idx] + [marker_message] + history[insert_pos:]`).

**Consumer-side detection** (relevant for any parser): a message is recognized as a
marker if `role == USER` and content `startswith(COMPRESSION_MARKER)` and contains
`<context_summary>` — `agent_cascade/compression/helpers.py:23-28`.

---

## 2. Compressor prompt (where to add the caption instruction)

The prompt sent to the compressor LLM is `COMPRESSION_PROMPT` —
`agent_cascade/prompts/dna.py:90-101`:

```python
COMPRESSION_PROMPT = (
    "Summarize the following conversation history.\n"
    "Focus strictly on key decisions, important facts, established context, and the current state of tasks.\n"
    "CRITICAL RULES:\n"
    "1. Output ONLY the summary. Do not include introductory or concluding remarks (e.g. 'Here is a summary').\n"
    "2. Do not include meta-commentary or thinking process.\n"
    "3. Remain concise but comprehensive enough so that future turns can proceed without the original messages.\n"
    "4. Retain a compacted initial request and any follow ups from user in the summary.\n"
    "5. Existing summary is just for reference, focus on summarizing the events after that.\n\n"
    "--- START HISTORY ---\n{history_text}\n--- END HISTORY ---\n\n"
    f"Present summary below and always terminate it with last line `{COMPRESSION_END_MARKER}` to indicate the end of the summary. It will NOT be validated without this marker."
)
```

- The instruction line to extend is the **last line** (dna.py:100):
  `Present summary below and always terminate it with last line \`--- END SUMMARY ---\` ...`
- A caption instruction ("also emit a one-line caption after `--- END SUMMARY ---`") would be appended to this template, e.g. right after dna.py:100.
- A second, analogous prompt `CONSOLIDATION_PROMPT` exists at `dna.py:110-126`
  (same end-marker instruction at dna.py:125) — L2 consolidation.

**How the prompt is assembled and sent:** `agent_cascade/compression/agent_invoker.py:467`
(`summary_prompt = COMPRESSION_PROMPT.format(history_text=history_text)`), passed as the
`task=` of the Compressor system agent at `agent_invoker.py:484-489`.

---

## 3. Where compression output is parsed/consumed (caption-parsing hook)

After the compressor returns, the raw assistant text is extracted and the
`COMPRESSION_END_MARKER` is validated then **stripped** in
`agent_cascade/compression/agent_invoker.py:345-379` (inside `invoke_compression_agent`):

```python
345: # Extract the summary from the last assistant message
346: summary = ""
347: if final_msgs:
348:     for msg_obj in reversed(final_msgs):
...
352:         content = extract_text_from_message(msg_obj, add_upload_info=False)
353:         summary = strip_thinking_blocks(content)
354:         break
...
364: # Validate we got a usable summary
365: if not summary.strip():
366:     raise RuntimeError(f"{timeout_label} Agent returned an empty summary")
368: # Validate compression marker — ensures compressor didn't hallucinate or continue agentic task
369: if not summary.strip().endswith(COMPRESSION_END_MARKER):
370:     raise RuntimeError(
371:         f"{timeout_label} output missing end marker '{COMPRESSION_END_MARKER}' — "
372:         f"compressor may have hallucinated or continued the task"
373:     )
375: # Strip the marker from the returned summary (validated above)
376: summary = summary.strip()
377: summary = summary[:-len(COMPRESSION_END_MARKER)].strip()
379: return summary.strip()
```

**This is the caption-parsing hook.** Today the code requires the end marker as the
*final* line and strips everything up to it. To capture a caption, the block at
`agent_invoker.py:369-377` is where you'd (a) accept an optional one-line caption after
the end marker and (b) return it as a second value (currently returns only `summary`).

Downstream, the returned summary is wrapped into the marker by
`core.py:555` (`marker_message = build_marker_message(generated_summary, fraction)`),
called from `invoke_compression_agent` at `core.py:533-538`.

**Note:** the end-marker is also used to gate validation on retries
(`COMPRESSION_MAX_RETRIES`, `agent_cascade/settings.py:127-128`).

---

## 4. Metadata line (line 1) of the log

`agent_cascade/logger/agent_instance_logger.py`.

**The metadata dict is built in `__init__`** at `agent_instance_logger.py:78-89`:

```python
self.data = {
    "metadata": {
        "agent_class": self.agent_class,      # normalized lowercase
        "instance_name": instance_name,
        "start_timestamp": self.start_time.isoformat(),
        "last_update": self.start_time.isoformat(),
        "current_log_path": self.log_path,
        "working_dir": os.getcwd(),
        "supervisor": "System",
    },
    "history": []
}
```

**How line 1 is written** — `_initial_save()` at `agent_instance_logger.py:256-296`,
which appends the metadata as the first JSONL line:

```python
295: self._append_line({"metadata": self.data["metadata"]})
296: self._initialized = True
```

**Update/rewrite behavior:**
- **Write-once at init** for the on-disk line: `_initial_save()` guards with
  `self._initialized` (agent_instance_logger.py:269) and *skips* if the file already
  has a metadata first line (agent_instance_logger.py:279-293). There is **no public
  method to rewrite just the metadata line in place** — it is not re-emitted per
  message.
- `update_supervisor()` (agent_instance_logger.py:108-115) updates `data["metadata"]["supervisor"]`
  **in-memory only** — the on-disk line 1 keeps the original value (docstring explicitly
  says "The on-disk JSONL first line retains the original value").
- `update_timestamp()` (agent_instance_logger.py:298-300) updates `data["metadata"]["last_update"]`
  in memory; it is persisted only when a **full file rewrite** happens.
- Full rewrites that re-emit the metadata header + all messages exist in:
  - `rewrite_log_with_history()` → `agent_instance_logger.py:553`
    (`lines = [json.dumps({"metadata": self.data["metadata"]}, ...)]`)
  - `_sync_marker_single_write()` → `agent_instance_logger.py:665`
  - `_consolidate_markers_in_jsonl()` → `agent_instance_logger.py:776`

**Implication for a `"caption"` field:** a new field can be added to the dict at
`agent_instance_logger.py:78-89`, but to persist it to disk you must (a) set it in
`self.data["metadata"]` and (b) trigger a rewrite path (e.g. `_sync_marker_single_write`,
which already runs during compression marker sync) — or add a dedicated
metadata-rewrite method mirroring `update_supervisor`. There is currently no
lightweight "rewrite only line 1" helper.

---

## 5. UI: agent-name display + log-metadata reading

`web_ui/app.js` (single 272 KB file).

**Session list (where a caption would render under the name):**
- `fetchSessions()` — `app.js:928-942` calls `GET /api/sessions` and stores `sessions`.
- `renderSessions()` — `app.js:945-978` builds each item. The agent name is rendered at
  `app.js:961-962`:

```js
958: sessionsList.innerHTML = filtered.map(s => `
959:   <div class="session-item" data-path="${escapeHtml(s.path.replace(/\\/g, '/'))}">
960:     <div class="session-item-header">
961:       <span class="session-item-name">${escapeHtml(s.name)}</span>
962:       <span class="session-item-agent">${escapeHtml(s.agent)}</span>
963:     </div>
964:     <div class="session-item-meta">
965:       <span>${formatDate(s.mtime * 1000)}</span>
966:       <span>${formatSize(s.size)}</span>
967:     </div>
968:   </div>
969: `).join('');
```
  A caption line would slot in here (e.g. a new `<span class="session-item-caption">`
  after `session-item-agent`, or a second row under the name).

**Does the UI already read the JSONL metadata line? NO.**
- The only "metadata" references in `app.js` are for the *live agent pool* state sync
  (`app.js:2011`, `app.js:2031` — meta fields like `active`, `agent_class`, `is_halted`),
  unrelated to log-file metadata.
- The backend `/api/sessions` endpoint (`agent_cascade/api_server.py:893-931`) builds
  each session entry **from the filename only** (`parts = p.stem.split('_')`,
  `api_server.py:907-914`), NOT by reading line 1:

```python
917: sessions.append({
918:     "path": str(p),
919:     "name": instance_name,
920:     "agent": agent_class,
921:     "timestamp": timestamp,
922:     "size": p.stat().st_size,
923:     "mtime": p.stat().st_mtime
924: })
```
  **No field is read from the JSONL metadata line anywhere in the UI or this endpoint.**
  A caption surfaced to the UI must therefore be added to this endpoint's response
  (and read from line 1) or passed through the WS `agents` payload.

**Existing settings/metadata edit round-trip pattern to mirror:**
- `saveSettings()` — `app.js:1096-...` (persists to `localStorage` under
  `agent-cascade-settings`) and `loadSettings()` — `app.js:1196-...`, debounced auto-save
  at `app.js:1476-1479`, called at `app.js:1625`.
- This is a **client-side localStorage round-trip**, not tied to the log metadata line.
  It is the natural pattern to copy for a human-editable caption: read into a field,
  persist on change, apply on load. Note it does NOT write into the JSONL — if the caption
  must be durable across machines / loadable from the log, a server round-trip (new
  `/api/sessions/<id>` PUT or a WS `update_session_meta` message) would be needed in
  addition to / instead of localStorage.

**Agent-name tab display** (live agent tabs, separate from the session list):
`renderAgentTabs` area — icon/label logic at `app.js:3950` and `app.js:4457` / `app.js:4487`
(`agentData?.agent_class === 'orchestrator' ? '💬' : '🤖'`). A caption per live agent tab
would render alongside the tab label here.

---

## 6. How / when compression is triggered (caption frequency)

Compression is **per-agent-instance** and driven by **token-usage thresholds** checked
**before every LLM call** (plus post-tool async drains and a manual `/compress`).

- Trigger check: `_check_and_trigger_compression()` —
  `agent_cascade/engine/compression_exec.py:54`, invoked on the pre-LLM path at
  `agent_cascade/engine/llm_call.py:139`.
- Fire condition: `if usage_pct > force_threshold` — `compression_exec.py:159`,
  where `force_threshold = self.pool.settings.compression_force_threshold`
  (`compression_exec.py:93`).
- Default threshold: `COMPRESSION_FORCE_THRESHOLD = 96.0` (96% of context used) —
  `agent_cascade/settings.py:103-104`; warning threshold 90% (`settings.py:105-106`);
  default discard fraction 70% (`settings.py:119-120`).

**Implication for caption frequency:** a caption is emitted **once per compression
event** (each time a session crosses the ~96% usage bar and is compressed, or on manual
`/compress`), not per message. In a long-running session that compresses repeatedly,
you get one caption per compression cycle; the *latest* one is the natural one to show
in the UI/metadata. L2 consolidation events (`CONSOLIDATION_PROMPT`, dna.py:110-126)
also produce a marker and would get a caption the same way.

---

## Cross-cutting notes for the spec

1. **End marker is currently stripped, not persisted.** The template (dna.py:103-108)
   does not emit `--- END SUMMARY ---`; it is only required by the prompt (dna.py:100)
   and removed in `agent_invoker.py:377`. A caption "after the end marker" therefore
   must be captured in `agent_invoker.py:369-377` before the marker strip, then threaded
   through `core.py:555` → `build_marker_message` (helpers.py:381) into the template.
2. **Metadata line is write-once** (agent_instance_logger.py:295) and only re-emitted on
   full rewrites (553/665/776). Adding a `"caption"` field needs a set + a rewrite trigger.
3. **UI does not read line 1 today** — `/api/sessions` (api_server.py:893-931) is
   filename-derived. Surfacing the caption requires a backend change (read line 1 or
   carry it in the WS `agents` payload) plus a `renderSessions` (app.js:945-978) change.
4. **No code was modified** — this report is read-only.
