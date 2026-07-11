import json
from pathlib import Path
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA

@register_tool('read_logs', allow_overwrite=True)
class ReadLogs(BaseTool):
    """Read agent log files with middle-point truncation."""

    name = 'read_logs'
    description = TOOL_METADATA['read_logs']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'log_file': {
                'type': 'string',
                'description': TOOL_METADATA['read_logs']['parameters']['log_file']
            },
            'max_chars_per_message': {
                'type': 'integer',
                'description': TOOL_METADATA['read_logs']['parameters']['max_chars_per_message']
            },
            'range': {
                'type': 'string',
                'description': TOOL_METADATA['read_logs']['parameters']['range']
            }
        },
        'required': ['log_file'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')

    @staticmethod
    def _parse_range(range_str: str, total_entries: int) -> tuple:
        """Parse a range string into 0-based Python slice indices.

        Format matches edit_file / re_indent style (1-indexed, inclusive):
            '3:7'   -> entries 3 through 7
            '5:'    -> entries 5 through end
            ':20'   -> first 20 entries
            '5'     -> single entry at position 5
            '-1'    -> last entry (single index)
            '5:-1'  -> entries 5 through second-to-last (-1 as an end bound is exclusive-like)

        Returns (start_idx, end_idx) as a half-open slice [start, end).
        """
        if total_entries == 0:
            return 0, 0  # Guard against empty logs
        range_str = range_str.strip()
        if not range_str:
            return 0, total_entries  # Empty means "all"

        if ':' in range_str:
            parts = range_str.split(':')
            if len(parts) != 2:
                raise ValueError(f"Range must have exactly one ':'. Got '{range_str}'")

            start_part, end_part = parts[0].strip(), parts[1].strip()

            # Parse start (empty means from the beginning, i.e., entry 1)
            if start_part == '':
                start = 1
            else:
                start = int(start_part)
                if start < 0:
                    start = total_entries + 1 + start  # -1 = last entry

            # Parse end (empty means to the end of the log)
            if end_part == '':
                end = total_entries
            else:
                end = int(end_part)
                if end < 0:
                    end = total_entries + end  # -1 = one before last

            # Clamp to valid bounds [1, total_entries]
            start = max(1, min(start, total_entries))
            end = max(1, min(end, total_entries))

            if start > end:
                raise ValueError(
                    f"Range start ({start}) must be <= end ({end}) "
                    f"(total entries: {total_entries})"
                )

            # Convert 1-based inclusive to 0-based half-open slice
            return start - 1, end

        else:
            # Single number = read just that one entry
            idx = int(range_str)
            if idx < 0:
                idx = total_entries + 1 + idx
            idx = max(1, min(idx, total_entries))
            return idx - 1, idx

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        log_file = params['log_file']
        max_chars = params.get('max_chars_per_message', 1000)
        if max_chars <= 0:
            return "Error: max_chars_per_message must be a positive integer."
        range_str = params.get('range', None)

        # Resolve file path via agent_pool or fallback
        if self.agent_pool and hasattr(self.agent_pool, 'operation_manager') and self.agent_pool.operation_manager:
            try:
                file_path = self.agent_pool.operation_manager._resolve_path(log_file, mode="ro")
            except ValueError as e:
                return f"Error: {str(e)}"
        else:
            # Fallback if no agent_pool (same pattern as read_file)
            from agent_cascade.settings import DEFAULT_WORKSPACE
            base_dir = Path(DEFAULT_WORKSPACE)
            if Path(log_file).is_absolute():
                file_path = Path(log_file).resolve()
            else:
                file_path = (base_dir / log_file).resolve()
            if not str(file_path).startswith(str(base_dir.resolve())):
                return f"Path '{log_file}' is outside the allowed directory"

        if not file_path.exists() or not file_path.is_file():
            return f"Error: Log file '{log_file}' not found."

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return f"Error reading file: {e}"

        # --- Parse the file content ---
        # Support three formats: JSON array, single JSON object, and JSONL (one JSON per line)
        parsed_lines = []
        stripped = content.strip()

        if stripped.startswith('['):
            # Case 1: JSON array — try to parse as a whole
            try:
                arr = json.loads(stripped)
                if isinstance(arr, list):
                    parsed_lines = [item for item in arr if item is not None]
                else:
                    parsed_lines = [{"raw": str(arr)}]
            except json.JSONDecodeError:
                pass  # Fall through to JSONL parsing below

        if not parsed_lines and stripped.startswith('{'):
            # Case 2: Single JSON object (could also be the start of a malformed array)
            try:
                obj = json.loads(stripped)
                parsed_lines = [obj]
            except json.JSONDecodeError:
                pass

        if not parsed_lines:
            # Case 3: JSONL — one JSON object per line (original behavior)
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed_lines.append(json.loads(line))
                except json.JSONDecodeError:
                    # Non-JSON lines kept as plain strings; truncation handled in the pass below
                    parsed_lines.append(line)

        # --- Helper: check if an entry is a metadata line (works on dicts and strings) ---
        def _is_metadata(entry):
            return isinstance(entry, dict) and "metadata" in entry

        # --- Pagination / slicing ---
        # Split into metadata lines (always included) and regular log entries (sliced)
        metadata_lines = [l for l in parsed_lines if _is_metadata(l)]
        other_lines = [l for l in parsed_lines if not _is_metadata(l)]

        try:
            if range_str is not None:
                # Unified range parameter (1-indexed, inclusive like re_indent / edit_file)
                start_idx, end_idx = self._parse_range(range_str, len(other_lines))
                selected = other_lines[start_idx:end_idx]
            else:
                # Default fallback — last 20 non-metadata entries
                selected = other_lines[-20:]
        except (ValueError, IndexError) as e:
            return f"Error parsing range '{range_str}': {e}"

        parsed_lines = metadata_lines + selected

        # --- Helper: truncate a string from the middle ---
        def _truncate_middle(s, limit):
            """Keep the first and last halves of *s*, replacing the middle. Always stays within *limit* chars."""
            s = str(s) if not isinstance(s, str) else s
            if len(s) > limit:
                msg = f" ... [TRUNCATED: {len(s) - limit} chars removed] ..."
                # Reserve space for the truncation message itself
                remaining = limit - len(msg)
                if remaining < 2:
                    remaining = 2  # At least 1 char each side
                half = remaining // 2
                return s[:half] + msg + s[-(remaining - half):]
            return s

        # --- Helper: recursively truncate string values in nested structures (iterative) ---
        def _truncate_strings(obj, limit):
            """Walk *obj* (dict / list / str) and truncate any long strings."""
            if isinstance(obj, str):
                return _truncate_middle(obj, limit)
            stack = [obj]
            while stack:
                current = stack.pop()
                if isinstance(current, dict):
                    for k, v in current.items():
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                        elif isinstance(v, str) and len(v) > limit:
                            current[k] = _truncate_middle(v, limit)
                elif isinstance(current, list):
                    for i, v in enumerate(current):
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                        elif isinstance(v, str) and len(v) > limit:
                            current[i] = _truncate_middle(v, limit)
            return obj

        # --- Truncation pass ---
        truncated_lines = []
        for item in parsed_lines:
            if isinstance(item, dict):
                # Fast path: truncate well-known fields directly (avoids deep walk overhead)
                if "content" in item:
                    item["content"] = _truncate_middle(item["content"], max_chars)
                if "reasoning_content" in item and item["reasoning_content"]:
                    item["reasoning_content"] = _truncate_middle(
                        item["reasoning_content"], max_chars
                    )
                # Handle function_call: single dict OR list of call dicts
                fc = item.get("function_call")
                if isinstance(fc, dict):
                    fc["arguments"] = _truncate_middle(fc.get("arguments", ""), max_chars)
                elif isinstance(fc, list):
                    for call in fc:
                        if isinstance(call, dict) and "arguments" in call:
                            call["arguments"] = _truncate_middle(
                                call["arguments"], max_chars
                            )
                # Deep-truncate anything in the extra field (nested tool calls, etc.)
                if "extra" in item:
                    item["extra"] = _truncate_strings(item["extra"], max_chars)

                truncated_lines.append(item)
            elif isinstance(item, str):
                truncated_lines.append(_truncate_middle(item, max_chars))
            else:
                # Handle non-dict / non-string entries (arrays, numbers, etc.)
                truncated_lines.append(_truncate_strings(item, max_chars))

        # Serialize back to JSON string (one line per entry) with line number prefixes.
        # Format matches read_file style: "{line_num}: {content}"
        result = []
        for i, item in enumerate(truncated_lines):
            if isinstance(item, str):
                line_text = item
            else:
                line_text = json.dumps(item, ensure_ascii=False)
            result.append(f"{i + 1}: {line_text}")

        return "\n".join(result)