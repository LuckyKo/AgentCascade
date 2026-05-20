import re
from typing import Any

# Pre-compiled regexes for performance
# These are shared across multiple modules to avoid redundancy and maintenance issues.

# Matches only CLOSED <think>...</think> and <thought>...</thought> blocks at the start
_THINK_BLOCK_RE = re.compile(r'^\s*<(' + 'think|thought' + r')>([\s\S]*?)</\1>', re.IGNORECASE)

# Matches unclosed <think> or <thought> blocks that go UNTIL THE END of a string
# (Used for streaming or incomplete responses)
_THINK_BLOCK_UNCLOSED_RE = re.compile(r'^\s*<(' + 'think|thought' + r')>[\s\S]*$', re.IGNORECASE)

# Matches only CLOSED [THINK]...[/THINK] or [THOUGHT]...[/THOUGHT] blocks at the start
_THINK_BLOCK_BRACKET_RE = re.compile(r'^\s*\[(' + 'THINK|THOUGHT' + r')\]([\s\S]*?)\[/\1\]', re.IGNORECASE)

# Matches unclosed [THINK] or [THOUGHT] blocks that go UNTIL THE END of a string
_THINK_BLOCK_BRACKET_UNCLOSED_RE = re.compile(r'^\s*\[(' + 'THINK|THOUGHT' + r')\][\s\S]*$', re.IGNORECASE)

# Internal aliases for stripping logic
_THINK_SEARCH_RE = _THINK_BLOCK_RE
_BRACKET_SEARCH_RE = _THINK_BLOCK_BRACKET_RE

# Matches Gemma-style thinking blocks at the start of a message
_GEMMA_THOUGHT_RE = re.compile(r"^\s*<\|channel>thought\n?([\s\S]*?)(?:\n?<channel\|>|$)", re.IGNORECASE)

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
    Iteratively remove <think> and [THINK] blocks from the START of a string.
    Note: This function is non-recursive. To clean structured content (like history),
    call it on the individual string fields.
    """
    if not isinstance(data, str):
        return data
        
    # Optimization: only run regex if we see potential tags
    lower_data = data.lower()
    
    changed = True
    while changed:
        changed = False
        if '<think' in lower_data or '<thought' in lower_data:
            new_data = _THINK_SEARCH_RE.sub('', data, count=1)
            if new_data != data:
                data = new_data
                lower_data = data.lower()
                changed = True
        
        if not changed and ('[think' in lower_data or '[thought' in lower_data):
            new_data = _BRACKET_SEARCH_RE.sub('', data, count=1)
            if new_data != data:
                data = new_data
                lower_data = data.lower()
                changed = True
                
        if not changed and '<|channel>thought' in lower_data:
            new_data = _GEMMA_THOUGHT_RE.sub('', data, count=1)
            if new_data != data:
                data = new_data
                lower_data = data.lower()
                changed = True
        
    return data.strip()
