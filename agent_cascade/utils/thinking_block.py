import re
from typing import Any

from agent_cascade.utils.media_utils import _MEDIA_EXTS_BY_MODALITY

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
_GEMMA_THOUGHT_RE = re.compile(r'^\s*<\|channel>thought\n?([\s\S]*?)(?:\n?<channel\|>|$)', re.IGNORECASE)

# Matches [TOOL RESPONSE TRUNCATED...] blocks
_TOOL_TRUNCATED_RE = re.compile(r'\[TOOL RESPONSE TRUNCATED.*?\]', re.DOTALL)

# Matches <context_summary>...</context_summary> blocks
_CONTEXT_SUMMARY_RE = re.compile(r'<context_summary>[\s\n]*(.*?)[\s\n]*</context_summary>', re.DOTALL)

# Matches markdown bolding (** or __)
_MARKDOWN_BOLD_RE = re.compile(r'(\*\*|__)')

# Matches common justification prefixes like "Reason:", "Verdict:", etc.
_JUSTIFICATION_PREFIX_RE = re.compile(r'^(Reason|Justification|Verdict|Tips)[:\s-]*', re.IGNORECASE)

# Matches markdown images with base64 data
_IMAGE_DATA_RE = re.compile(r'!\[([^\]]*)\]\((data:image/[^;]+;base64,[a-zA-Z0-9+/=]+)\)')

# Matches markdown MEDIA links: ![alt](url) where url is an image/audio/video
# base64 data-URI, or a local file/drive path whose extension is a known
# audio/video type. Used by the user-input pipeline (content_parse) to turn a
# user-inserted media reference into an audio/video/image ContentItem.
# Kept SEPARATE from _IMAGE_DATA_RE on purpose: _IMAGE_DATA_RE is image-only
# and is reused by token estimation (utils/utils.py IMAGE_REGEX), which must
# NOT start matching audio/video. The image data-URI branch below is
# functionally equivalent to _IMAGE_DATA_RE for valid image data-URIs
# (only the MIME-subtype char class differs), so existing image parsing is unchanged.
# Extension alternation for _MEDIA_DATA_RE, derived from the single source of truth
# in media_utils._MEDIA_EXTS_BY_MODALITY so all media-ext vocabularies stay in
# lock-step. Sorted (frozenset/dict iteration order is not guaranteed) for a
# deterministic, reproducible compiled pattern.
_MEDIA_EXT_ALT = '|'.join(sorted(ext for exts in _MEDIA_EXTS_BY_MODALITY.values() for ext in exts))

_MEDIA_DATA_RE = re.compile(
    r'!\[([^\]]*)\]\('
    r'('
        r'data:(?:image|audio|video)/[^;)]+;base64,[A-Za-z0-9+/=]+'
        r'|'
        r'(?:file://|[A-Za-z]:[\\/]|\\\\|/)[^\s)]+\.'
        r'(?:' + _MEDIA_EXT_ALT + r')'
    r')\)',
    re.IGNORECASE,  # Local paths carry mixed-case extensions (N:\Music\Track.WAV).
)

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

    Only strips leading/trailing whitespace if thinking blocks were actually removed,
    preserving original whitespace for strings that don't contain thinking tags.
    """
    if not isinstance(data, str):
        return data

    # Optimization: only run regex if we see potential tags
    lower_data = data.lower()

    any_changed = False
    changed = True
    while changed:
        changed = False
        if '<think' in lower_data or '<thought' in lower_data:
            new_data = _THINK_SEARCH_RE.sub('', data, count=1)
            if new_data != data:
                data = new_data
                lower_data = data.lower()
                changed = True
                any_changed = True

        if not changed and ('[think' in lower_data or '[thought' in lower_data):
            new_data = _BRACKET_SEARCH_RE.sub('', data, count=1)
            if new_data != data:
                data = new_data
                lower_data = data.lower()
                changed = True
                any_changed = True

        if not changed and '<|channel>thought' in lower_data:
            new_data = _GEMMA_THOUGHT_RE.sub('', data, count=1)
            if new_data != data:
                data = new_data
                lower_data = data.lower()
                changed = True
                any_changed = True

    # Only strip whitespace if we actually removed thinking blocks.
    # This preserves original whitespace for strings without thinking tags,
    # which is critical for preserving indentation in code content like edit_file's new_content.
    return data.strip() if any_changed else data
