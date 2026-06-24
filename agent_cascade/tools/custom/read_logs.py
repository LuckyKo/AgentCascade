import os
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
            'last_n_messages': {
                'type': 'integer',
                'description': TOOL_METADATA['read_logs']['parameters']['last_n_messages']
            },
            'start_index': {
                'type': 'integer',
                'description': TOOL_METADATA['read_logs']['parameters']['start_index']
            },
            'nr_of_entries': {
                'type': 'integer',
                'description': TOOL_METADATA['read_logs']['parameters']['nr_of_entries']
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

    def call(self, params: str, **kwargs) -> str:
        from agent_cascade.utils.utils import json_loads
        try:
            if isinstance(params, str):
                p = json_loads(params)
                params = json.dumps(p)
        except Exception:
            pass

        params = self._verify_json_format_args(params)
        log_file = params['log_file']
        max_chars = params.get('max_chars_per_message', 1000)
        last_n = params.get('last_n_messages', None)
        start_index = params.get('start_index', None)
        nr_of_entries = params.get('nr_of_entries', 20)

        if not self.agent_pool:
            return "Error: agent_pool not available."

        base_dir = self.agent_pool.operation_manager.base_dir
        file_path = base_dir / log_file

        if not file_path.exists() or not file_path.is_file():
            return f"Error: Log file '{log_file}' not found."

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            return f"Error reading file: {e}"

        parsed_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                parsed_lines.append(json.loads(line))
            except json.JSONDecodeError:
                # If a line is somehow invalid JSON, just append it as a raw string event
                parsed_lines.append({"event": "UNPARSABLE_LINE", "raw": line})

        if start_index is not None or 'nr_of_entries' in params:
            metadata_lines = [l for l in parsed_lines if "metadata" in l]
            other_lines = [l for l in parsed_lines if "metadata" not in l]
            
            start = start_index if start_index is not None else 0
            if nr_of_entries == -1:
                end = len(other_lines)
            else:
                end = start + nr_of_entries
                
            parsed_lines = metadata_lines + other_lines[start:end]
        elif last_n is not None and last_n > 0:
            # We always keep metadata (first line usually) and then the last N
            metadata_lines = [l for l in parsed_lines if "metadata" in l]
            other_lines = [l for l in parsed_lines if "metadata" not in l]
            parsed_lines = metadata_lines + other_lines[-last_n:]
        else:
            # Default fallback if no pagination parameters are provided
            metadata_lines = [l for l in parsed_lines if "metadata" in l]
            other_lines = [l for l in parsed_lines if "metadata" not in l]
            parsed_lines = metadata_lines + other_lines[-20:]

        # Truncate content in the middle
        for item in parsed_lines:
            if "content" in item and isinstance(item["content"], str):
                content = item["content"]
                if len(content) > max_chars:
                    half = max_chars // 2
                    item["content"] = content[:half] + f"\n\n... [TRUNCATED: {len(content) - max_chars} chars removed] ...\n\n" + content[-half:]
            # Also truncate function call args if they are huge
            if "function_call" in item and isinstance(item["function_call"], dict):
                args = item["function_call"].get("arguments", "")
                if isinstance(args, str) and len(args) > max_chars:
                    half = max_chars // 2
                    item["function_call"]["arguments"] = args[:half] + f"\n\n... [TRUNCATED: {len(args) - max_chars} chars removed] ...\n\n" + args[-half:]
        
        # Serialize back to pretty JSON string
        result = []
        for item in parsed_lines:
            result.append(json.dumps(item, ensure_ascii=False))
            
        return "\n".join(result)
