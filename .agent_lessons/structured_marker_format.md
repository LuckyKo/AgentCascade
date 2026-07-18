# Structured Marker Format Feature

## Date: 2026-06-16

## Overview

Added structured marker format output for compression/session load operations. After compression or session load, the history structure is annotated with markers showing:

```
[SYSTEM][USER_MSG_0][SUMMARY_1][SUMMARY_2]...[rest of tail messages]
```

## Implementation Details

### Changes Made

#### 1. agent_pool.py - Added `output_structured_marker_format()` method

**Location:** After `find_last_marker()` method (around line 615)

**Purpose:** Outputs a structured representation of the history showing:
- `[SYSTEM]` - the system message at index 0
- `[USER_MSG_0]` - the first user message after the system message (the anchor)
- `[SUMMARY_N]` - each compression marker numbered sequentially
- Tail messages are shown as regular messages

**Key Features:**
- Identifies all compression markers using COMPRESSION_MARKER prefix
- Numbers summaries sequentially starting from 1
- Preserves system message and first user message identification
- Outputs formatted string for logging/debugging

#### 2. agent_pool.py - Modified `load_session_from_log()` method

**Location:** Line ~1043, after history cleanup and before return statement

**Change:** Added call to `output_structured_marker_format()` to display structure after session load

```python
# Output structured marker format for debugging/visibility
structure_output = self.output_structured_marker_format(instance_name, cleaned_messages)
if structure_output:
    logger.info(f"[SESSION LOAD STRUCTURE] {instance_name}:\n{structure_output}")
```

#### 3. agent_cascade/compression/core.py - Modified `apply_compression()` function

**Location:** After successful compression application (around line 180)

**Change:** Added call to output structured format after compression:

```python
# Output structured marker format for visibility
if hasattr(agent_pool, 'output_structured_marker_format'):
    history = agent_pool.get_conversation(target_agent_name)
    structure_output = agent_pool.output_structured_marker_format(target_agent_name, history)
    if structure_output:
        logger.info(f"[COMPRESSION STRUCTURE] {target_agent_name}:\n{structure_output}")
```

## Output Format Example

When compression is applied or session is loaded, the log will show:

```
[COMPRESSION STRUCTURE] agent_instance_name:
History Structure (15 messages):
  [0] [SYSTEM] - You are a helpful assistant.
  [1] [USER_MSG_0] - Initial user request...
  [2] [SUMMARY_1] --- CONTEXT COMPRESSED (50% of history summarized) ---
  [3] Assistant response after compression...
  [4] User follow-up...
  ...
```

## Benefits

1. **Debugging**: Easy to see the structure of compressed history
2. **Verification**: Confirms system message and anchor are preserved
3. **Visibility**: Shows how many compressions occurred (numbered summaries)
4. **Session Load**: Immediately shows restored session structure

## Testing Recommendations

1. Test with fresh agent (no compression) - should show only [SYSTEM] and [USER_MSG_0]
2. Test after single compression - should show [SUMMARY_1]
3. Test after multiple compressions - should show [SUMMARY_1], [SUMMARY_2], etc.
4. Test session load from log file - should output structure on load
5. Verify numbering is sequential and accurate

## Related Files

- `agent_pool.py` - Main implementation
- `agent_cascade/compression/core.py` - Compression trigger point
- `agent_cascade/prompts/dna.py` - COMPRESSION_MARKER definition