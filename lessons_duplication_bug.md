# Lessons Learned - AgentCascade Unified Duplication Bug Fix (2026-05-29)

## Root Cause

When user sends a message, the server:
1. Broadcasts `{'type': 'state', **build_state(generating=True)}` — includes messages already committed to pool's conversation
2. Then sends `stream_update` with `response_messages` from streaming thread

If some response messages were committed between step 1 and step 2, they appear TWICE — once in `state.data.messages` and again in `stream_update.response_messages`. The frontend was conditionally truncating only when `historyCount <= rootData.messages.length`, which was FALSE when state already included more messages, so no truncation happened and responseMsgs were appended on top → DUPLICATION.

## Fixes Applied (in web_ui/app.js)

### Fix 1: Always truncate to historyCount before pushing (lines ~923-945)
- Changed from conditional `if (historyCount <= rootData.messages.length)` to unconditional truncation
- Added guard against `historyCount > rootData.messages.length` — resets array instead of extending with undefined slots
- Added initialization fallback when rootData exists but has no messages yet

### Fix 2: Removed shadowed `wasGenerating` variable (line ~1073)
- Inner `const wasGenerating = state.generating;` always evaluated to true since state.generating was set at line 1021 before the check
- This made the throttle condition useless — updateControls fired on every stream_update tick instead of once

### Fix 3: Avoid double renderSubAgents call (lines ~1095-1117)
- When stackChanged triggers switchMainTab, switchMainTab internally calls renderSubAgents
- Now skip the first renderSubAgents call when willSwitchTab is true to avoid redundant DOM work

### Fix 4: Root-only streaming throttle improvement (lines ~1086-1092)
- During root-only streaming, render throttle was 750ms causing noticeable stutter (~1.3 FPS)
- Changed to 300ms for root-only streaming (~3.3 FPS)
- Sub-agent active: 150ms (~6.7 FPS). Idle: 750ms (~1.3 FPS)

### Fix 5: Sub-agent merge warning (lines ~979-981)
- Added console.warn when startIdx < 0, indicating server inconsistency in sub-agent data

## Key Architectural Understanding

- `historyCount` from the server is the authoritative boundary of committed messages
- `response_messages` are always NEW messages beyond that boundary
- The `state/done` handler replaces messages entirely (full state), while `stream_update` merges incrementally
- Sub-agent merge uses a different pattern: calculates `startIdx = hCount - sa.messages.length` to determine where to splice
- Root agent messages flow through subAgents just like other agents after unification

## Testing Notes

The `now` variable issue from the previous investigation (missing declaration at line ~1057) has already been fixed — it's properly declared at line 1052.