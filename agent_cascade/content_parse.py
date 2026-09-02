"""Content parsing utilities for multimodal messages and system message extraction."""

from agent_cascade.utils.thinking_block import _IMAGE_DATA_RE as _IMAGE_DATA_PATTERN
from agent_cascade.utils.media_utils import save_image_from_data_uri, MediaStorageError

def _extract_system_message(agent) -> str:
    """Extract system message content from an agent.
    
    Priority: base_system_message > system_message > llm.cfg['system'].
    Returns '' (empty string) if no system message found — consistent with
    how downstream callers check truthiness via `if sys_content:`.
    Never returns None.
    """
    if hasattr(agent, 'base_system_message') and agent.base_system_message:
        return str(agent.base_system_message)
    if hasattr(agent, 'system_message') and agent.system_message:
        return str(agent.system_message)
    if hasattr(agent, 'llm') and hasattr(agent.llm, 'cfg'):
        cfg = agent.llm.cfg
        val = cfg.get('system', '') or cfg.get('system_message', '')
        if val:
            return val
    return ''


def _parse_multimodal_content(text):
    """
    Parse markdown images ![alt](data:...) and return a list of content items.
    If no images are found, returns the original text.

    Saves base64 data URIs to media storage as paths; falls back to inline
    base64 if media storage fails.
    """
    from agent_cascade.log import logger

    parts = []
    last_end = 0
    for match in _IMAGE_DATA_PATTERN.finditer(text):
        start, end = match.span()
        if start > last_end:
            parts.append({'text': text[last_end:start]})
        alt, url = match.groups()
        try:
            media_path = save_image_from_data_uri(url)
            parts.append({'image': media_path})  # Path instead of base64
            parts.append({'text': f"Saved to: {media_path}"})
        except MediaStorageError as e:
            # Fallback to inline base64 if media storage fails
            logger.warning(f"Media storage failed for user image, keeping inline base64: {e}")
            parts.append({'image': url})
        last_end = end
    
    if last_end < len(text):
        parts.append({'text': text[last_end:]})

    if not parts:
        return text
    if len(parts) == 1 and 'text' in parts[0]:
        return parts[0]['text']
    return parts
