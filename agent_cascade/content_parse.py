"""Content parsing utilities for multimodal messages and system message extraction."""

from agent_cascade.utils.media_utils import (_MEDIA_EXTS_BY_MODALITY, MediaStorageError, save_audio_from_data_uri,
                                             save_image_from_data_uri, save_video_from_data_uri)
from agent_cascade.utils.thinking_block import _MEDIA_DATA_RE as _MEDIA_DATA_PATTERN


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


def _rewrite_goal_command(text):
    """Rewrite a leading ``/goal <text>`` into the supervisor-task prefix (todo.md:160).

    The compression system preserves only USER messages that start with
    ``Context: This is a message from`` (see ``is_supervisor_task_message`` in
    agent_cascade/compression/helpers.py) when building the
    ``<last_supervisor_task>`` block of a compression marker. Rewriting the
    user's ``/goal`` command to that prefix — BEFORE any other processing —
    makes the new goal stick through compression automatically, with no
    further logic needed.

    Rules:
      - Only an EXACT leading ``/goal`` word is rewritten (case-insensitive);
        ``/goals``, ``/goalx``, or ``/goal`` mid-message are left untouched.
      - The rest of the line becomes the task text; leading/trailing whitespace
        around it is stripped, internal spacing preserved.
      - Non-matching input is returned unchanged (byte-identical).

    Uses ``_TASK_PREFIX`` from compression/helpers.py so the prefix this rewrite
    produces and the prefix the compression system detects can never drift apart.
    """
    if not isinstance(text, str):
        return text
    t = text.lstrip()
    low = t.lower()
    goal_word = '/goal'
    if not (low == goal_word or low.startswith(goal_word + ' ')):
        return text
    rest = t[len(goal_word):].strip()
    # Local import: compression/helpers.py is a heavier module and this keeps the
    # content_parse -> compression dependency lazy (house style for cross-module refs).
    from agent_cascade.compression.helpers import _TASK_PREFIX
    return f'{_TASK_PREFIX} User:\n{rest}'


_AUDIO_EXTS = tuple('.' + ext for ext in _MEDIA_EXTS_BY_MODALITY['audio'])
_VIDEO_EXTS = tuple('.' + ext for ext in _MEDIA_EXTS_BY_MODALITY['video'])


def _media_modality(url):
	"""Classify a markdown media URL into 'image'/'audio'/'video', or None.

	Recognizes data:image/*, data:audio/*, data:video/* data-URIs and local
	file/drive paths (by audio/video extension). Returns None for anything that
	does not match, so the caller can degrade to a text path-reference rather
	than crash.
	"""
	low = url.lower()
	if low.startswith('data:image/'):
		return 'image'
	if low.startswith('data:audio/'):
		return 'audio'
	if low.startswith('data:video/'):
		return 'video'
	if low.startswith('file://'):
		low = low[len('file://'):]
	if low.endswith(_AUDIO_EXTS):
		return 'audio'
	if low.endswith(_VIDEO_EXTS):
		return 'video'
	return None


def _parse_multimodal_content(text):
	"""
	Parse markdown media ![alt](url) and return a list of content items.

	Supports image/audio/video base64 data-URIs and local file/drive paths for
	audio/video. If no media is found, returns the original text unchanged.

	Data URIs are saved to media storage as paths (emitting a '[NOTE: Saved as
	…]' text part); falls back to inline base64 if media storage fails. A local
	path is emitted as-is (already a reference). An unrecognised/unsupported
	media type degrades to a literal text path-reference (never crashes).
	"""
	from agent_cascade.log import logger

	# todo.md:160 — rewrite /goal into the supervisor-task prefix so the new
	# goal is preserved verbatim in compression markers (logic already exists
	# in compression/helpers.py). Must run before any other content processing.
	text = _rewrite_goal_command(text)

	parts = []
	last_end = 0
	for match in _MEDIA_DATA_PATTERN.finditer(text):
		start, end = match.span()
		if start > last_end:
			parts.append({'text': text[last_end:start]})
		url = match.group(2)
		modality = _media_modality(url)
		if modality is None:
			# Defensive: _MEDIA_DATA_PATTERN only matches image/audio/video, so
			# this is unreachable while the regex and this helper stay in sync.
			# Degrade to a literal text path-reference rather than crash on drift.
			parts.append({'text': match.group(0)})
			last_end = end
			continue
		if url.startswith('data:'):
			try:
				if modality == 'image':
					media_path = save_image_from_data_uri(url)
				elif modality == 'audio':
					media_path = save_audio_from_data_uri(url)
				else:
					media_path = save_video_from_data_uri(url)
				parts.append({modality: media_path})  # Path instead of base64
				parts.append({'text': f"[NOTE: Saved as {media_path}]"})
			except MediaStorageError as e:
				# Fallback to inline base64 if media storage fails
				logger.warning(f"Media storage failed for user {modality}, keeping inline base64: {e}")
				parts.append({modality: url})
		else:
			# Local file/drive path: already a reference, nothing to save.
			parts.append({modality: url})
		last_end = end

	if last_end < len(text):
		parts.append({'text': text[last_end:]})

	if not parts:
		return text
	if len(parts) == 1 and 'text' in parts[0]:
		return parts[0]['text']
	return parts
