---
name: investigate-streaming-gaps
description: Systematic method for diagnosing why agent turn updates are not appearing in WebUI during execution, including locating streaming infrastructure and identifying missing broadcast calls.
source: auto-generated
version: "1.0.0"
triggers:
  - "streaming gap"
  - "UI not updating"
  - "agent turns invisible"
  - "webui black box"
  - "auto-skill not visible"
  - "investigate streaming"
  - "debug UI updates"
generated_by: researcher
generated_from_task: "Investigate why the auto-skill generation process is not visible in the UI during execution in N:\\work\\WD\\AgentCascade."
---

## Goal

Enable researchers to systematically diagnose why agent turn updates are not appearing in WebUI during execution by identifying missing stream update calls and understanding the streaming pipeline.

## Procedure

### Step 1 — Locate Core Streaming Infrastructure Files

Identify key files that handle streaming:
- `auto_skill_helpers.py` (if auto-skill is involved)
- `execution_engine.py` (sub-agent execution path)
- `run_agent_unified.py` (main agent execution path)
- `api_integration.py` (contains `broadcast_stream_update` helper)
- `stream_publisher.py` (WebSocket push infrastructure)

**Tool usage:** Use `grep` to search for streaming-related patterns like `stream_publisher`, `_put_stream_update`, `broadcast_stream_update`, and `yield.*turn_output`.

### Step 2 — Identify Streaming Calls in Normal Execution Paths

Find where stream updates are normally triggered:

1. In `execution_engine.py`, locate the loop that iterates over `self.run(inst)` calls and call `broadcast_stream_update` within it (typically around line 4285-4310).

2. In `run_agent_unified.py`, find the loop iterating over `run_agent_in_pool_with_recovery` and call `broadcast_stream_update` (around line 191-203).

**Key pattern:** Normal execution paths have a loop that processes `turn_output_raw` and explicitly calls a streaming helper.

### Step 3 — Examine Problematic Code Path

Examine the code path where streaming is missing:

1. In `auto_skill_helpers.py`, find the `run_auto_skill_proposal` function's generator loop (lines 68-87).

2. Look for calls to:
   - `broadcast_stream_update`
   - `_put_stream_update`
   - `stream_publisher.push_*`

3. If none exist, this is your streaming gap.

### Step 4 — Compare Sub-Agent vs Main-Agent Paths

Determine which execution path is affected:

- **Sub-agent path**: Call to `run_auto_skill_proposal` from `execution_engine.py` (around line 4358).
- **Main-agent path**: Call to `run_auto_skill_proposal` from `run_agent_unified.py` (around line 230).

Both paths often exhibit the same gap if they both consume a generator without forwarding stream updates.

### Step 5 — Analyze Generator Consumption Patterns

Check how the engine's generator is consumed:

```python
# Pattern that DOES stream:
for turn_output_raw in self.run(inst):
    # unpack and call broadcast_stream_update
```

```python
# Pattern that DOES NOT stream:
for turn_output_raw in gen:
    # just process, no streaming calls
```

### Step 6 — Document Exact File Paths and Line Numbers

Provide precise locations for the issue:

- **File 1**: `agent_cascade/auto_skill_helpers.py` - lines X-Y (generator loop)
- **File 2**: `agent_cascade/execution_engine.py` - line Z (sub-agent call site)
- **File 3**: `agent_cascade/run_agent_unified.py` - line W (main-agent call site)

### Step 7 — Provide Evidence-Based Recommendations

Suggest concrete fixes:

1. **Add streaming callback** to `run_auto_skill_proposal`:
   ```python
   def run_auto_skill_proposal(..., stream_callback=None):
       for turn_output_raw in gen:
           if stream_callback:
               stream_callback(turn_output, is_streaming)
   ```

2. **Pass streaming callbacks** from callers:
   - In `execution_engine.py`: Use `broadcast_stream_update` wrapper
   - In `run_agent_unified.py`: Same pattern

3. **Alternative**: Restructure to have caller iterate with streaming (more conservative).

## Tips

- Always compare actual code against the known working streaming pattern in your codebase.
- Check both sub-agent and main-agent execution paths—both may be affected.
- Use `grep` to find all calls to `broadcast_stream_update` as reference points.
- Look for generator consumption patterns (`for ... in gen:`) as common places where streaming is omitted.
- Consider if auto-skill should stream every turn or batch updates (performance vs visibility trade-off).
- Verify that any fix doesn't interfere with existing throttling logic in `broadcast_stream_update`.

## Quality Checklist

- [x] Name is unique (`investigate-streaming-gaps`)
- [x] Description is specific and ≥20 characters
- [x] Triggers cover multiple ways the skill will be matched
- [x] Body has concrete, actionable steps with code examples
- [x] Total file size ≤ 15 KB (approximately 3KB)
- [x] Skill is reusable for similar debugging tasks

## Evidence Quality

This investigation used primary source analysis of Python code in the AgentCascade workspace. All findings are based on direct file inspection and code pattern matching, with high confidence due to the clear absence of streaming calls in the auto-skill generator loop.

---

**Created by:** Researcher Agent (Maine)  
**Date:** 2026-07-28  
**Task Context:** UI streaming visibility investigation for auto-skill generation feature.