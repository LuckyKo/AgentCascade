# Compression Tool-Call Pair Fix — Implementation Plan

## Problem
The compression cut-off point is calculated as `int(len(active_set) * fraction)`, then blindly slices the message list. This can split ASSISTANT(tool_call) → FUNCTION(result) pairs, breaking OpenAI's API rule that assistant messages with tool calls must be immediately followed by their function responses.

## Solution

### Part 1: Fix `compute_discard_count()` in helpers.py
- After calculating the base discard count, scan forward from position `discard`
- If an ASSISTANT message at the cut point has a `function_call`, increment discard to include its FUNCTION response(s)
- Handle both legacy mode (`function_call` attribute) and native mode (`tool_calls` extra field via `extra['tool_index']`)
- Loop until the cut boundary is clean (no orphaned pairs)

### Part 2: Defensive guard in core.py
- Before slicing at line 272/291, verify the cut point doesn't land on a FUNCTION message without its ASSISTANT partner
- Log a warning if adjustment was needed

### Part 3: Unit tests
- Test normal compression (no tool calls)
- Test single tool-call pair at boundary
- Test multiple consecutive tool-call chains at boundary
- Test edge cases (all messages are tool pairs, empty set)