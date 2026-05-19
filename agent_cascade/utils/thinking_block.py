import re
from typing import Any

# Pre-compiled regexes for performance
# These are shared across multiple modules to avoid redundancy and maintenance issues.

# Matches both <think>...</think> and <thought>...</thought> blocks
_THINK_BLOCK_RE = re.compile(r'\s*<(think|thought)>.*?</\1>', re.IGNORECASE | re.DOTALL)

# Matches unclosed <think> or <thought> blocks at the end of a string
_THINK_BLOCK_UNCLOSED_RE = re.compile(r'\s*<(think|thought)>[^<]*$', re.IGNORECASE | re.DOTALL)

# Matches [THINK]...[/THINK] or [THOUGHT]...[/THOUGHT] blocks
_THINK_BLOCK_BRACKET_RE = re.compile(r'\s*\[(THINK|THOUGHT)\].*?\[/\1\]', re.IGNORECASE | re.DOTALL)

# Matches the reasoning content at the start of a message
_THINK_SEARCH_RE = re.compile(r'^\s*<(think|thought)>([\s\S]*?)(</\1>|$)', re.IGNORECASE)

# Matches [THINK]...[/THINK] or [THOUGHT]...[/THOUGHT] blocks at the start of a message
_BRACKET_SEARCH_RE = re.compile(r'^\s*\[(THINK|THOUGHT)\]([\s\S]*?)(\[/\1\]|$)', re.IGNORECASE)

# Matches Gemma-style thinking blocks at the start of a message
_GEMMA_THOUGHT_RE = re.compile(r"^\s*<\|channel>thought\n?([\s\S]*?)\n?<channel\|>", re.IGNORECASE)

# Matches [TOOL RESPONSE TRUNCATED...] blocks
_TOOL_TRUNCATED_RE = re.compile(r'\[TOOL RESPONSE TRUNCATED.*?\]', re.DOTALL)

# Matches <context_summary>...</context_summary> blocks
_CONTEXT_SUMMARY_RE = re.compile(r"<context_summary>[\s\n]*(.*?)[\s\n]*</context_summary>", re.DOTALL)

# Matches markdown bolding (** or __)
_MARKDOWN_BOLD_RE = re.compile(r'(\*\*|__)')

# Matches common justification prefixes like "Reason:", "Verdict:", etc.
_JUSTIFICATION_PREFIX_RE = re.compile(r'^(Reason|Justification|Verdict|Tips)[:\s-]*', re.IGNORECASE)

# Matches markdown images with base64 data
_IMAGE_DATA_RE = re.compile(r'!\[([^\]]*)\]\((data:image/[^;]+;base64,[a-zA-Z0-9+/=]+)\)')

# Matches markdown code blocks
_MARKDOWN_CODE_RE = re.compile(r'```(?:[a-zA-Z0-9]*\n)?(.*?)```', re.DOTALL)

# Matches triple-quoted strings in JSON-like text
_TRIPLE_QUOTE_RE = re.compile(r'(":\s*)"""(.*?)"""(?=[,}\s])')

# Matches double-quoted strings in JSON
_JSON_STRING_RE = re.compile(r'(:\s*)"((?:[^"\\]|\\.)*?)"(?=\s*[,}\]])', re.DOTALL)

# Matches Chinese characters
CHINESE_CHAR_RE = re.compile(r'[\u4e00-\u9fff]')


def strip_thinking_blocks(data: Any) -> Any:
    """
    Recursively remove <think> and [THINK] blocks from string values.
    """
    if isinstance(data, str):
        # Optimization: only run regex if we see potential tags
        lower_data = data.lower()
        has_tags = False
        if '<think' in lower_data or '<thought' in lower_data:
            data = _THINK_SEARCH_RE.sub('', data)
            has_tags = True
        
        if '[think' in lower_data or '[thought' in lower_data:
            data = _BRACKET_SEARCH_RE.sub('', data)
            has_tags = True
            
        if '<|channel>thought' in lower_data:
            data = _GEMMA_THOUGHT_RE.sub('', data)
            has_tags = True
            
        if has_tags:
            return data.strip()
        return data
    elif isinstance(data, dict):
        return {k: strip_thinking_blocks(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [strip_thinking_blocks(i) for i in data]
    return data
