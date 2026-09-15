---
name: investigate-streaming-gaps
description: Systematic method for diagnosing why agent/turn updates are not appearing in a live UI during execution — locate the streaming infrastructure, find where stream updates are normally broadcast, and identify code paths that consume a generator without forwarding updates.
source: auto-generated
version: "1.0.0"
triggers:
  - "streaming gap"
  - "UI not updating"
  - "agent turns invisible"
  - "webui black box"
  - "investigate streaming"
  - "debug UI updates"
---

## Goal

Diagnose why live turn/agent updates are not appearing in the UI by finding missing stream-broadcast calls and understanding the streaming pipeline. (Generic methodology — adapt file/function names to your codebase.)

## Procedure

1. **Locate core streaming infrastructure.** Identify the files that handle streaming: the sub-agent execution path, the main-agent execution path, the module with the `broadcast_stream_update`-style helper, and the WebSocket/publisher push layer. Grep for patterns like `stream_publisher`, `_put_stream_update`, `broadcast_stream_update`, `yield.*turn_output`.
2. **Find where stream updates ARE triggered in working paths.** In each normal execution path there is a loop over the turn-output generator that explicitly calls the broadcast helper. Note those call sites (file + line) as reference points.
3. **Examine the problematic path.** Find the function whose generator loop does NOT call any of `broadcast_stream_update` / `_put_stream_update` / `publisher.push_*`. If none exist, that's your gap.
4. **Compare sub-agent vs main-agent paths.** Determine which execution path is affected; both often exhibit the same gap if both consume a generator without forwarding updates. Check every caller of the suspect function.
5. **Analyze generator-consumption patterns.** The streaming pattern is `for turn_output in gen: ... broadcast(...)`. The non-streaming pattern is `for turn_output in gen:` with only processing and no broadcast call. Grep all consumers of the suspect generator.
6. **Document exact locations.** File + line for the gap (generator loop) and each caller site (sub-agent, main-agent).
7. **Recommend a fix.** Add an optional `stream_callback` param to the function and call it per turn; pass the broadcast helper from each caller. Alternative (more conservative): restructure so the caller iterates the generator with streaming.

## Tips

Always compare actual code against the known-working streaming pattern in your codebase. Check both sub-agent and main-agent paths — both may be affected. Grep all calls to the broadcast helper as reference points. Look for `for ... in gen:` loops as common places where streaming is omitted. Consider whether the path should stream every turn or batch updates (performance vs visibility trade-off). Verify any fix doesn't interfere with existing throttling logic in the broadcast helper.
