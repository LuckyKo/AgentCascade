import glob as _glob
import json
import logging
from pathlib import Path
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA

logger = logging.getLogger(__name__)

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
                'default': 1000,
                'description': TOOL_METADATA['read_logs']['parameters']['max_chars_per_message']
            },
            'range': {
                'type': 'string',
                'description': TOOL_METADATA['read_logs']['parameters']['range']
            },
            'mode': {
                'type': 'string',
                'enum': ['trim_tools', 'trim_all', 'none'],
                'default': 'trim_tools',
                'description': TOOL_METADATA['read_logs']['parameters']['mode']
            },
            'format': {
                'type': 'string',
                'enum': ['raw', 'simple'],
                'default': 'simple',
                'description': TOOL_METADATA['read_logs']['parameters']['format']
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

    @staticmethod
    def _is_metadata(entry):
        """Check if an entry is a metadata line."""
        return isinstance(entry, dict) and "metadata" in entry

    @staticmethod
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

    @staticmethod
    def _truncate_strings(obj, limit):
        """Walk *obj* (dict / list / str) and truncate any long strings.

        Mutates dict/list objects in place for efficiency; returns the (possibly modified) object.
        For strings, returns a new truncated string without mutating the original.
        Safe here because parsed_lines entries are disposable after this pass.
        """
        if isinstance(obj, str):
            return ReadLogs._truncate_middle(obj, limit)
        stack = [obj]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for k, v in current.items():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
                    elif isinstance(v, str) and len(v) > limit:
                        current[k] = ReadLogs._truncate_middle(v, limit)
            elif isinstance(current, list):
                for i, v in enumerate(current):
                    if isinstance(v, (dict, list)):
                        stack.append(v)
                    elif isinstance(v, str) and len(v) > limit:
                        current[i] = ReadLogs._truncate_middle(v, limit)
        return obj

    @staticmethod
    def truncate_item(item, mode, max_chars):
        """Apply mode-specific truncation to one parsed log entry."""
        if mode == 'none':
            return item
        if mode == 'trim_all':
            # Delegate entirely to _truncate_strings for all types
            return ReadLogs._truncate_strings(item, max_chars)
        # mode == 'trim_tools' — only truncate tool-related fields
        if isinstance(item, dict):
            fc = item.get("function_call")
            if isinstance(fc, dict):
                fc["arguments"] = ReadLogs._truncate_middle(fc.get("arguments", ""), max_chars)
            elif isinstance(fc, list):
                for call in fc:
                    if isinstance(call, dict) and "arguments" in call:
                        call["arguments"] = ReadLogs._truncate_middle(
                            call["arguments"], max_chars
                        )
            # Handle modern tool_calls format: tool_calls[].function.arguments
            tc = item.get("tool_calls")
            if isinstance(tc, list):
                for call in tc:
                    if isinstance(call, dict):
                        fn = call.get("function")
                        if isinstance(fn, dict) and "arguments" in fn:
                            fn["arguments"] = ReadLogs._truncate_middle(
                                fn["arguments"], max_chars
                            )
            # Deep-truncate anything in the extra field (nested tool calls, etc.)
            if "extra" in item:
                item["extra"] = ReadLogs._truncate_strings(item["extra"], max_chars)
        return item

    @staticmethod
    def _format_simple_entry(entry, entry_num, mode='trim_tools', max_chars=1000):
        """Format one truncated log entry as a readable summary line.

        Returns (header_line, content_line_or_none).
        """
        if isinstance(entry, str):
            # Non-JSON line or raw string — respect truncation settings
            c = entry
            if mode != 'none' and len(c) > max_chars:
                c = ReadLogs._truncate_middle(c, max_chars)
            return f"⟨L {entry_num}⟩ [RAW] {c}", None

        if not isinstance(entry, dict):
            # Non-dict type — respect truncation settings
            c = str(entry)
            if mode != 'none' and len(c) > max_chars:
                c = ReadLogs._truncate_middle(c, max_chars)
            return f"⟨L {entry_num}⟩ [{type(entry).__name__}] {c}", None

        # If it doesn't have a recognizable role, treat as raw JSON
        role = entry.get("role", "").lower()
        if not role:
            json_str = json.dumps(entry, ensure_ascii=False)
            truncated = ReadLogs._truncate_middle(json_str, max_chars)
            return f"⟨L {entry_num}⟩ [RAW] {truncated}", None

        timestamp = entry.get("timestamp", "")
        time_str = ""
        if timestamp:
            # Extract HH:MM:SS portion if present
            t = str(timestamp)
            for sep in ("T", " "):
                if sep in t:
                    time_part = t.split(sep)[1][:8]
                    if len(time_part) == 8:
                        time_str = time_part
                        break
            if not time_str and ":" in t[:9]:
                time_str = t[:8]

        # Build role label
        if role == "user":
            role_label = "USER"
        elif role == "assistant":
            role_label = "ASSISTANT"
        elif role == "function" or role == "tool":
            name = entry.get("name") or entry.get("function_id", "?")
            role_label = f"TOOL({name})"
        elif role == "system":
            role_label = "SYSTEM"
        else:
            role_label = f"{role.upper()}"

        # Build tool info for assistant entries with calls
        tool_info = ""
        if role == "assistant":
            fc = entry.get("function_call")
            tc = entry.get("tool_calls")
            calls = []
            if isinstance(fc, dict):
                fn_name = fc.get("name", "?")
                args = fc.get("arguments", "")
                if len(args) > 60:
                    args = args[:57] + "..."
                calls.append(f"{fn_name}({args})")
            if isinstance(tc, list):
                for call in tc:
                    if isinstance(call, dict):
                        fn = call.get("function", {})
                        fn_name = fn.get("name", "?") if isinstance(fn, dict) else "?"
                        args = fn.get("arguments", "") if isinstance(fn, dict) else ""
                        if len(args) > 60:
                            args = args[:57] + "..."
                        calls.append(f"{fn_name}({args})")
            if calls:
                tool_info = " → " + ", ".join(calls)

        # Build header
        header_parts = [f"⟨L {entry_num}⟩"]
        if time_str:
            header_parts.append(time_str)
        header_parts.append(role_label + tool_info)
        header_line = " ".join(header_parts)

        # Content preview
        content = None
        if role in ("user", "assistant", "system"):
            content = entry.get("content") or entry.get("reasoning_content")
        elif role in ("function", "tool"):
            content = entry.get("content")
        else:
            # Fallback: try content field for unknown roles
            content = entry.get("content")

        if content:
            c = str(content)
            # Respect truncation mode for content preview:
            # - trim_tools/trim_all: apply max_chars limit (content was already truncated by truncate_item if needed)
            # - none: no additional truncation beyond what truncate_item did
            if mode != 'none' and len(c) > max_chars:
                c = ReadLogs._truncate_middle(c, max_chars)
            content_line = f"    {c}"
        else:
            content_line = None

        return header_line, content_line

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        log_file = params['log_file']
        max_chars = params.get('max_chars_per_message', 1000)
        if max_chars <= 0:
            return "Error: max_chars_per_message must be a positive integer."
        range_str = params.get('range', None)

        # Parse and validate display mode
        mode = params.get('mode', 'trim_tools')
        valid_modes = ('trim_tools', 'trim_all', 'none')
        if mode not in valid_modes:
            return f"Error: Invalid mode '{mode}'. Must be one of: {', '.join(valid_modes)}."

        # Parse and validate output format
        fmt = params.get('format', 'simple')
        valid_formats = ('raw', 'simple')
        if fmt not in valid_formats:
            return f"Error: Invalid format '{fmt}'. Must be one of: {', '.join(valid_formats)}."

        # Validate log_file input
        if not log_file or not isinstance(log_file, str) or not log_file.strip():
            return "Error: log_file must be a non-empty string."

        log_file = log_file.strip()

        # Auto-resolve bare filenames in the instance-specific log directory
        file_path = None
        if (
            "/" not in log_file
            and "\\" not in log_file
            and not log_file.startswith("./")
            and ".." not in log_file  # prevent path traversal within log dir
            and self.agent_pool
            and hasattr(self.agent_pool, "_logger")
            and hasattr(self.agent_pool._logger, "log_dir")
        ):
            try:
                log_dir = Path(self.agent_pool._logger.log_dir)
                if not log_dir.is_dir():
                    pass  # fall through to resolve_tool_path
                else:
                    # Only * and ? are treated as wildcards. [ ] are kept literal — this is a deliberate
                    # usability choice to avoid confusing filenames like file[1].log being misinterpreted.
                    has_wildcards = "*" in log_file or "?" in log_file
                    if has_wildcards:
                        pattern = str(log_dir / log_file)
                        matches = _glob.glob(pattern)
                        if len(matches) == 1:
                            file_path = Path(matches[0])
                        elif len(matches) > 1:
                            candidates = "\n".join(f"  - {m}" for m in sorted(matches))
                            return (
                                f"Error: Multiple log files match '{log_file}' "
                                f"in {log_dir}. Please specify a more specific name or full path.\n\n"
                                f"Candidates:\n{candidates}"
                            )
                    else:
                        target = log_dir / log_file
                        if target.is_file():
                            file_path = target
            except (OSError, PermissionError):
                pass  # fall through to resolve_tool_path
            except Exception as e:
                logger.exception(f"Unexpected error during log auto-resolution: {e}")
                return f"Error resolving log file path: {e}"

        # Fall back to standard resolution if not auto-resolved
        if file_path is None:
            from agent_cascade.utils.tool_path_resolver import resolve_tool_path
            try:
                file_path = resolve_tool_path(log_file, mode="ro", agent_pool=self.agent_pool)
            except ValueError as e:
                return f"Error: {str(e)}"

        if not file_path.exists() or not file_path.is_file():
            return f"Error: Log file '{log_file}' not found."

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            logger.exception(f"Error reading log file '{log_file}'")
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

        # --- Pagination / slicing ---
        # Split into metadata lines and regular log entries, tracking original positions
        other_lines = []
        for idx, l in enumerate(parsed_lines):
            if not ReadLogs._is_metadata(l):
                other_lines.append((idx + 1, l))

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

        truncated_lines = []
        for pos, item in selected:
            truncated_lines.append((pos, ReadLogs.truncate_item(item, mode, max_chars)))

        # --- Output formatting based on 'format' parameter ---
        if fmt == 'raw':
            # Original behavior: numbered JSON lines (using original entry positions)
            result = []
            for pos, item in truncated_lines:
                if isinstance(item, str):
                    line_text = item
                else:
                    line_text = json.dumps(item, ensure_ascii=False)
                num_label = "meta" if pos == 0 else pos
                result.append(f"{num_label}: {line_text}")
            return "\n".join(result)

        # simple mode: human-readable summary (using original entry positions)
        result = []
        for pos, item in truncated_lines:
            header_line, content_line = ReadLogs._format_simple_entry(item, pos, mode, max_chars)
            result.append(header_line)
            if content_line is not None:
                result.append(content_line)
        return "\n".join(result)