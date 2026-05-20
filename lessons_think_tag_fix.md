# Lesson: Fix for Aggressive Think-Tag Stripping

## Problem
The framework used regex patterns `_THINK_BLOCK_RE` and `_THINK_BLOCK_BRACKET_RE` to strip reasoning/thinking blocks from agent responses. These patterns lacked start-of-string anchors (`^`), causing them to strip tags and their content from *anywhere* in the text. This led to data corruption and crashes when tag-like patterns appeared inside tool arguments (e.g., code being written to a file or tasks being sent to sub-agents).

## Solution
1. **Anchored Regexes**: Added anchored versions of the regexes in `agent_cascade/utils/thinking_block.py`:
   - `_THINK_BLOCK_ANCHORED_RE = re.compile(r'^\s*<' + 'think|thought' + r'>.*?</\1>', ...)`
   - `_THINK_BLOCK_BRACKET_ANCHORED_RE = re.compile(r'^\s*\[(THINK|THOUGHT)\].*?\[/\1\]', ...)`
2. **Context-Aware Stripping**: Updated `api_server.py` to use these anchored versions during the text cleaning phase for verdict extraction. This ensures that only blocks intended as "thinking" (at the very beginning of the message) are removed.

## Guidance for Future Changes
- **Use Anchors**: When stripping meta-content (like thinking blocks or context summaries), always use patterns anchored to the start of the string (`^`) to avoid accidental modification of embedded data.
- **Indirect Tag References**: Avoid using literal tag strings (e.g., `<` + `think` + `>`) in tool calls or print statements to prevent triggering system-level filters or accidental stripping by the very logic being modified. Use string concatenation or variables.
- **Verify Calling Context**: Before applying a global "clean" or "strip" function, verify if the data is a full message or a partial fragment/argument where tags might be legitimate content.
