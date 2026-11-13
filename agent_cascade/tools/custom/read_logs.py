import glob as _glob
import json
import logging
import re
from pathlib import Path

from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)

# Entries shown when neither `range` nor `by_line` picks a window (the tail is the
# interesting part of an agent log). Also the match window when `filter` is set.
DEFAULT_TAIL_ENTRIES = 20
# LLM-supplied flags sometimes arrive as strings despite the boolean schema.
_TRUE_STRINGS = frozenset({'1', 'true', 'yes', 'on'})
_FALSE_STRINGS = frozenset({'0', 'false', 'no', 'off', ''})

@register_tool('read_logs', allow_overwrite=True)
class ReadLogs(BaseTool):
    """Read agent log files with middle-point truncation, optionally filter-matched."""

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
            },
            'by_line': {
                'type': 'boolean',
                'default': False,
                'description': TOOL_METADATA['read_logs']['parameters']['by_line']
            },
            'filter': {
                'type': 'string',
                'description': TOOL_METADATA['read_logs']['parameters']['filter']
            },
            'filter_regex': {
                'type': 'boolean',
                'default': False,
                'description': TOOL_METADATA['read_logs']['parameters']['filter_regex']
            },
            'filter_case_sensitive': {
                'type': 'boolean',
                'default': False,
                'description': TOOL_METADATA['read_logs']['parameters']['filter_case_sensitive']
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
            '5:-1'  -> entries 5 through last

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
                    end = total_entries + 1 + end  # -1 = last entry (same convention as start)

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
    def _parse_file_line_range(range_str: str, total_lines: int) -> tuple:
        """Parse a range as 1-based inclusive *physical file lines*.

        Used by ``by_line=true``. Returns (start_line, end_line) inclusive,
        clamped to [1, total_lines]. Semantics match ``_parse_range``
        (open ends, negative indices count from the end).
        """
        start_idx, end_idx = ReadLogs._parse_range(range_str, total_lines)
        # _parse_range returns a 0-based half-open slice; convert back to the
        # 1-based inclusive bounds used for physical-line filtering.
        return start_idx + 1, end_idx

    @staticmethod
    def _as_bool(value, default=False) -> bool:
        """Coerce an LLM-supplied flag to bool (the schema says boolean, clients send strings)."""
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
        return default

    @staticmethod
    def _entry_match_text(item) -> str:
        """Return the text a ``filter`` is matched against for one log entry.

        Unparsed lines match as-is; parsed entries match against their full JSON
        serialization (never the truncated preview), so a filter can find text that
        ``max_chars_per_message`` would otherwise hide. ``ensure_ascii=False`` keeps
        non-ASCII log text literally matchable.
        """
        if isinstance(item, str):
            return item
        try:
            return json.dumps(item, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(item)

    @classmethod
    def _build_filter_matcher(cls, pattern: str, use_regex=False, case_sensitive=False):
        """Compile ``filter`` into a predicate over entries.

        Raises ValueError with an actionable message for an invalid regex.
        """
        if use_regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                regex = re.compile(pattern, flags)
            except re.error as e:
                raise ValueError(
                    f"invalid filter regex {pattern!r}: {e}. Fix the pattern, or set "
                    'filter_regex=false to match it as literal text.'
                ) from e
            return lambda item: regex.search(cls._entry_match_text(item)) is not None
        if case_sensitive:
            return lambda item: pattern in cls._entry_match_text(item)
        needle = pattern.casefold()
        return lambda item: needle in cls._entry_match_text(item).casefold()

    @staticmethod
    def _humanize_size(file_path: Path) -> str:
        """Return a humanized file size string (B/KB/MB/GB/TB), matching read_file's style.

        Reuses FileOpsMixin._format_size so the two tools can't drift on thresholds.
        Returns "?" if the stat call fails.
        """
        try:
            from agent_cascade.operation_manager.file_operations import FileOpsMixin

            # Non-Path objects (e.g. test mocks) raise here and fall through to "?".
            size_bytes = file_path.stat().st_size
            return FileOpsMixin._format_size(size_bytes)
        except Exception:
            return '?'

    @staticmethod
    def _is_metadata(entry):
        """Check if an entry is a metadata line."""
        return isinstance(entry, dict) and 'metadata' in entry

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
        # mode == 'trim_tools' — truncate ONLY tool OUTPUTS, keep tool calls intact.
        # Tool calls (assistant entries with function_call / tool_calls arguments) are the
        # request and must be preserved so the agent can see exactly what was called.
        # Only the results (content on role="function"/"tool" entries) are trimmed.
        if isinstance(item, dict):
            role = str(item.get('role', '')).lower()
            if role in ('function', 'tool'):
                # Tool output: truncate the result content
                if 'content' in item and isinstance(item['content'], str):
                    item['content'] = ReadLogs._truncate_middle(item['content'], max_chars)
                # Deep-truncate extra on tool-output entries (may carry nested results)
                if 'extra' in item:
                    item['extra'] = ReadLogs._truncate_strings(item['extra'], max_chars)
            # NOTE: function_call.arguments / tool_calls[].function.arguments are intentionally
            # left intact here — they are assistant tool *calls*, not outputs.
        return item

    @staticmethod
    def _format_simple_entry(entry, entry_num, mode='trim_tools', max_chars=1000, src_line=None):
        """Format one truncated log entry as a readable summary line.

        ``entry_num`` is read_logs' own 1-based entry index (the coordinate used
        by the ``range`` parameter); ``src_line`` is the physical file line when
        it is known (BUG_0195). Both are shown so citations cannot silently
        drift between the two coordinate systems.

        Returns (header_line, content_line_or_none).
        """
        label = (f"⟨E {entry_num} | L {src_line}⟩" if src_line is not None
                 else f"⟨E {entry_num}⟩")

        if isinstance(entry, str):
            # Non-JSON line or raw string — respect truncation settings
            c = entry
            if mode != 'none' and len(c) > max_chars:
                c = ReadLogs._truncate_middle(c, max_chars)
            return f"{label} [RAW] {c}", None

        if not isinstance(entry, dict):
            # Non-dict type — respect truncation settings
            c = str(entry)
            if mode != 'none' and len(c) > max_chars:
                c = ReadLogs._truncate_middle(c, max_chars)
            return f"{label} [{type(entry).__name__}] {c}", None

        # If it doesn't have a recognizable role, treat as raw JSON
        role = entry.get('role', '').lower()
        if not role:
            json_str = json.dumps(entry, ensure_ascii=False)
            if mode != 'none' and len(json_str) > max_chars:
                json_str = ReadLogs._truncate_middle(json_str, max_chars)
            return f"{label} [RAW] {json_str}", None

        timestamp = entry.get('timestamp', '')
        time_str = ''
        if timestamp:
            # Extract HH:MM:SS portion if present
            t = str(timestamp)
            for sep in ('T', ' '):
                if sep in t:
                    time_part = t.split(sep)[1][:8]
                    if len(time_part) == 8:
                        time_str = time_part
                        break
            if not time_str and ':' in t[:9]:
                time_str = t[:8]

        # Build role label
        if role == 'user':
            role_label = 'USER'
        elif role == 'assistant':
            role_label = 'ASSISTANT'
        elif role == 'function' or role == 'tool':
            name = entry.get('name') or entry.get('function_id', '?')
            role_label = f"TOOL({name})"
        elif role == 'system':
            role_label = 'SYSTEM'
        else:
            role_label = f"{role.upper()}"

        # Build tool info for assistant entries with calls
        tool_info = ''
        if role == 'assistant':
            fc = entry.get('function_call')
            tc = entry.get('tool_calls')
            calls = []
            if isinstance(fc, dict):
                fn_name = fc.get('name', '?')
                args = fc.get('arguments', '')
                if len(args) > 60:
                    args = args[:57] + '...'
                calls.append(f"{fn_name}({args})")
            if isinstance(tc, list):
                for call in tc:
                    if isinstance(call, dict):
                        fn = call.get('function', {})
                        fn_name = fn.get('name', '?') if isinstance(fn, dict) else '?'
                        args = fn.get('arguments', '') if isinstance(fn, dict) else ''
                        if len(args) > 60:
                            args = args[:57] + '...'
                        calls.append(f"{fn_name}({args})")
            if calls:
                tool_info = ' → ' + ', '.join(calls)

        # Build header
        header_parts = [label]
        if time_str:
            header_parts.append(time_str)
        header_parts.append(role_label + tool_info)
        header_line = ' '.join(header_parts)

        # Content preview — handle both content and reasoning_content for assistant entries
        content = None
        reasoning = None
        if role in ('user', 'system'):
            content = entry.get('content')
        elif role == 'assistant':
            content = entry.get('content')
            reasoning = entry.get('reasoning_content')
        elif role in ('function', 'tool'):
            content = entry.get('content')
        else:
            # Fallback: try content field for unknown roles
            content = entry.get('content')

        result_lines = [header_line]

        if reasoning:
            result_lines.append(f"    (reasoning)")
            result_lines.append(f"    {str(reasoning)}")
            result_lines.append(f"    (reasoning_end)")

        if content:
            c = str(content)
            # Content preview is always capped at max_chars in non-'none' modes. This is the
            # display-time safety net for simple format. Note that data-level truncation in
            # truncate_item only touches tool OUTPUTS (role=function/tool) under trim_tools;
            # assistant content/reasoning are NOT pre-truncated there, so this cap matters.
            if mode != 'none' and len(c) > max_chars:
                c = ReadLogs._truncate_middle(c, max_chars)
            result_lines.append(f"    {c}")

        return header_line, '\n'.join(result_lines[1:]) if len(result_lines) > 1 else None

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        log_file = params['log_file']
        max_chars = params.get('max_chars_per_message', 1000)
        if max_chars <= 0:
            return 'Error: max_chars_per_message must be a positive integer.'
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
            return 'Error: log_file must be a non-empty string.'

        log_file = log_file.strip()

        # Auto-resolve bare filenames in the instance-specific log directory
        file_path = None
        if (
            '/' not in log_file
            and '\\' not in log_file
            and not log_file.startswith('./')
            and '..' not in log_file  # prevent path traversal within log dir
            and self.agent_pool
            and hasattr(self.agent_pool, '_logger')
            and hasattr(self.agent_pool._logger, 'log_dir')
        ):
            try:
                log_dir = Path(self.agent_pool._logger.log_dir)
                if not log_dir.is_dir():
                    pass  # fall through to resolve_tool_path
                else:
                    # Only * and ? are treated as wildcards. [ ] are kept literal — this is a deliberate
                    # usability choice to avoid confusing filenames like file[1].log being misinterpreted.
                    has_wildcards = '*' in log_file or '?' in log_file
                    if has_wildcards:
                        pattern = str(log_dir / log_file)
                        matches = _glob.glob(pattern)
                        if len(matches) == 1:
                            file_path = Path(matches[0])
                        elif len(matches) > 1:
                            candidates = '\n'.join(f"  - {m}" for m in sorted(matches))
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
                file_path = resolve_tool_path(log_file, mode='ro', agent_pool=self.agent_pool)
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
        # Support three formats: JSON array, single JSON object, and JSONL (one JSON per line).
        # Each entry is stored as (physical file line or None, parsed item).
        parsed_entries = []
        stripped = content.strip()

        if stripped.startswith('['):
            # Case 1: JSON array — try to parse as a whole. Individual array
            # elements are not tied to physical lines here, so src_line is None.
            try:
                arr = json.loads(stripped)
                if isinstance(arr, list):
                    parsed_entries = [(None, item) for item in arr if item is not None]
                else:
                    parsed_entries = [(None, {'raw': str(arr)})]
            except json.JSONDecodeError:
                pass  # Fall through to JSONL parsing below

        if not parsed_entries and stripped.startswith('{'):
            # Case 2: Single JSON object (could also be the start of a malformed array)
            try:
                obj = json.loads(stripped)
                parsed_entries = [(1, obj)]
            except json.JSONDecodeError:
                pass

        if not parsed_entries:
            # Case 3: JSONL — one JSON object per line (original behavior).
            # BUG_0195: record the physical 1-based file line for each entry so
            # the output can report both coordinates (entry index != file line
            # whenever blank or multi-line records exist).
            for line_no, line in enumerate(content.splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed_entries.append((line_no, json.loads(line)))
                except json.JSONDecodeError:
                    # Non-JSON lines kept as plain strings; truncation handled in the pass below
                    parsed_entries.append((line_no, line))

        # --- Pagination / slicing ---
        # Keep all lines including metadata, tracking original positions.
        # Each tuple is (1-based entry index, physical file line or None, item).
        all_lines = []
        for idx, (src_line, l) in enumerate(parsed_entries):
            all_lines.append((idx + 1, src_line, l))

        total = len(all_lines)
        file_size_str = self._humanize_size(file_path)

        # --- Empty log (0 entries): mirror read_file's empty-file style ---
        if total == 0:
            return f"OK: Read {file_path} entries 0/0 ({fmt}, {file_size_str})"

        by_line = self._as_bool(params.get('by_line', False))

        # --- Optional entry filter ---
        # Matching runs over the WHOLE entry (untruncated), so it can find text that
        # `max_chars_per_message` would cut out of the simple preview. With a filter
        # active, `range` counts matches instead of all entries, and the header says so.
        filter_pattern = params.get('filter')
        if filter_pattern == '':
            filter_pattern = None
        matcher = None
        filter_label = ''
        if filter_pattern is not None:
            use_regex = self._as_bool(params.get('filter_regex', False))
            try:
                matcher = self._build_filter_matcher(
                    filter_pattern, use_regex,
                    self._as_bool(params.get('filter_case_sensitive', False)))
            except ValueError as e:
                return f"Error: {e}"
            filter_label = f'/{filter_pattern}/' if use_regex else f"'{filter_pattern}'"

        match_total = None  # number of entries matching the filter (None = no filter)
        match_first = match_last = None
        line_start = line_end = None
        try:
            if by_line:
                # BUG_0195: interpret `range` as physical file lines. Requires a
                # line-resolved source (JSONL / mixed / single object), not a
                # whole-file JSON array.
                if range_str is None:
                    return ("Error: by_line=true requires an explicit 'range' "
                            "(e.g. '181:259') expressed in physical file lines.")
                if not any(src_line is not None for _, src_line, _ in all_lines):
                    return ('Error: by_line=true is not supported for this file — '
                            'its entries are not resolvable to physical file lines '
                            "(JSON array). Use the entry-based 'range' instead.")
                total_file_lines = len(content.splitlines())
                line_start, line_end = self._parse_file_line_range(range_str, total_file_lines)
                selected = [t for t in all_lines
                            if t[1] is not None and line_start <= t[1] <= line_end]
                if matcher is not None:
                    # The line window is the scope; the filter narrows it further.
                    selected = [t for t in selected if matcher(t[2])]
                    match_total = len(selected)
                    match_first, match_last = 1, match_total
                if not selected:
                    if matcher is not None:
                        return (f"OK: Read {file_path} matches 0 in file lines "
                                f"{line_start}-{line_end} ({fmt}, {file_size_str}; "
                                f"filter {filter_label})")
                    return (f"Error: No log entries on file lines "
                            f"{line_start}-{line_end} in {file_path}.")
            elif matcher is not None:
                matches = [t for t in all_lines if matcher(t[2])]
                match_total = len(matches)
                if not matches:
                    # Recoverable empty result: the caller can widen the filter or
                    # drop it to read the entries themselves.
                    return (f"OK: Read {file_path} matches 0 of {total} entries "
                            f"({fmt}, {file_size_str}; filter {filter_label})")
                if range_str is not None:
                    start_idx, end_idx = self._parse_range(range_str, match_total)
                else:
                    # Default window = tail of the MATCHES (mirrors the no-filter default).
                    start_idx = max(0, match_total - DEFAULT_TAIL_ENTRIES)
                    end_idx = match_total
                selected = matches[start_idx:end_idx]
                match_first, match_last = start_idx + 1, end_idx
            elif range_str is not None:
                # Unified range parameter (1-indexed inclusive like re_indent / edit_file)
                start_idx, end_idx = self._parse_range(range_str, total)
                selected = all_lines[start_idx:end_idx]
            else:
                # Default fallback — last DEFAULT_TAIL_ENTRIES entries
                selected = all_lines[-DEFAULT_TAIL_ENTRIES:]
        except (ValueError, IndexError) as e:
            return f"Error parsing range '{range_str}': {e}"

        truncated_lines = []
        for entry_num, src_line, item in selected:
            truncated_lines.append(
                (entry_num, src_line, ReadLogs.truncate_item(item, mode, max_chars)))

        # --- Build the operation-status header (first line of output) ---
        # BUG_0195: the window is in ENTRY coordinates (the coordinate `range`
        # accepts). Physical file lines are appended when every selected entry
        # has one, so a reader can translate citations without guessing.
        # With a filter the window is in MATCH coordinates instead, reported against
        # the entry total so the omitted non-matching entries stay recoverable.
        first = truncated_lines[0][0]
        last = truncated_lines[-1][0]
        src_lines = [src for _, src, _ in truncated_lines if src is not None]

        if match_total is None:
            header = f"OK: Read {file_path} entries {first}-{last}/{total} ({fmt}, {file_size_str}"
            cite_file_lines = True
        else:
            # by_line already names the line window, so don't repeat it below.
            scope = (f"in file lines {line_start}-{line_end}" if by_line
                     else f"of {total} entries")
            header = (f"OK: Read {file_path} matches {match_first}-{match_last}/{match_total} "
                      f"{scope} ({fmt}, {file_size_str}; filter {filter_label}")
            cite_file_lines = not by_line
        if cite_file_lines and len(src_lines) == len(truncated_lines):
            header += f"; file lines {src_lines[0]}-{src_lines[-1]}"
        header += ')'

        # --- Output formatting based on 'format' parameter ---
        if fmt == 'raw':
            # Numbered JSON lines. BUG_0195: prefix with the physical file line
            # (L...) when known, otherwise the entry index (E...) — never a bare
            # number that could be mistaken for the other coordinate system.
            result = []
            for entry_num, src_line, item in truncated_lines:
                if isinstance(item, str):
                    line_text = item
                else:
                    line_text = json.dumps(item, ensure_ascii=False)
                num_label = f"L{src_line}" if src_line is not None else f"E{entry_num}"
                result.append(f"{num_label}: {line_text}")
            return f"{header}\n```\n" + '\n'.join(result) + '\n```'

        # simple mode: human-readable summary (entry index + physical file line)
        result = []
        for entry_num, src_line, item in truncated_lines:
            header_line, content_line = ReadLogs._format_simple_entry(
                item, entry_num, mode, max_chars, src_line=src_line)
            result.append(header_line)
            if content_line is not None:
                result.append(content_line)
        return f"{header}\n```\n" + '\n'.join(result) + '\n```'
