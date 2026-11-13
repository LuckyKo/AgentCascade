"""Regression tests: system notifications must keep multimodal content well-typed.

Context: the 90% context warning (``_inject_compression_warning`` →
``_append_system_notification``) used to append a RAW DICT
``{'type': 'text', 'text': ...}`` to the last message's content. That is only legal
while ``content`` is a ``str``; when the last message is multimodal, ``content`` is a
``List[ContentItem]`` and every LLM adapter iterates it calling
``item.get_type_and_value()`` — so the dict aborted the whole LLM call with
``AttributeError: 'dict' object has no attribute 'get_type_and_value'`` on every retry
(and again inside fallback compression), producing a post-compression stall loop.

These tests pin:
1. ``_append_system_notification`` appends a ``ContentItem`` (never a dict) to list
   content, and de-duplicates against ContentItem-borne notifications.
2. The result survives the real serialization funnel
   (``format_as_multimodal_message`` / ``format_as_text_message``).
3. ``coerce_content_item`` is a safety net for any legacy dict that still reaches a
   content list: it coerces instead of raising, so one bad item cannot kill a turn.
"""

from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.base import _format_as_text_messages
from agent_cascade.llm.schema import USER, ContentItem, Message
from agent_cascade.utils.utils import (coerce_content_item, extract_text_from_message, format_as_multimodal_message)

WARNING = ('[SYSTEM WARNING: Context window at 92.1% capacity (110506/120000 tokens). '
           'Consider saving your progress notes before forced compression kicks in.]')
GUARD = '[SYSTEM WARNING: Context'


def _engine() -> ExecutionEngine:
    """Bare engine: the notification helpers only use each other (no pool/handlers)."""
    return ExecutionEngine.__new__(ExecutionEngine)


def _multimodal_message() -> Message:
    """A user turn carrying an attached image — the shape that triggered the crash."""
    return Message(
        role=USER,
        content=[ContentItem(text='ok, now some UX cleanup'),
                 ContentItem(image='N:/work/WD/AgentWorkspace/logs/media/images/img_1.jpg')])


class TestAppendSystemNotification:

    def test_appends_content_item_not_raw_dict(self):
        msgs = [_multimodal_message()]

        _engine()._append_system_notification(msgs, GUARD, WARNING)

        content = msgs[-1].content
        assert all(isinstance(item, ContentItem) for item in content), (
            f'non-ContentItem items in content: {[type(i).__name__ for i in content]}')
        assert content[-1].text == WARNING

    def test_result_is_serializable_by_every_adapter_funnel(self):
        msgs = [_multimodal_message()]
        _engine()._append_system_notification(msgs, GUARD, WARNING)

        reformatted = format_as_multimodal_message(
            msgs[-1], add_upload_info=True, add_multimodel_upload_info=True, add_audio_upload_info=True)
        assert all(isinstance(item, ContentItem) for item in reformatted.content)
        assert WARNING in extract_text_from_message(msgs[-1], add_upload_info=False)
        assert '[Image]' in extract_text_from_message(msgs[-1], add_upload_info=False)

    def test_duplicate_guard_recognizes_content_item_notification(self):
        msgs = [_multimodal_message()]

        _engine()._append_system_notification(msgs, GUARD, WARNING)
        _engine()._append_system_notification(msgs, GUARD, WARNING)

        texts = [item.text for item in msgs[-1].content if isinstance(item, ContentItem)]
        assert texts.count(WARNING) == 1, texts

    def test_string_content_path_unchanged(self):
        msgs = [Message(role=USER, content='plain text turn')]

        _engine()._append_system_notification(msgs, GUARD, WARNING)

        assert isinstance(msgs[-1].content, str)
        assert msgs[-1].content == f'plain text turn\n\n{WARNING}'


class TestInjectCompressionWarning:

    def test_warning_on_multimodal_last_message_does_not_poison_content(self):
        msgs = [_multimodal_message()]

        _engine()._inject_compression_warning(msgs, usage_pct=92.1,
                                              current_tokens=110506, max_tokens=120000)

        content = msgs[-1].content
        assert isinstance(content, list)
        assert all(isinstance(item, ContentItem) for item in content)
        assert any(GUARD in (item.text or '') for item in content)

        # The real pre-processing funnel every adapter runs before calling
        # item.get_type_and_value() must accept the message.
        assert format_as_multimodal_message(
            msgs[-1], add_upload_info=False, add_multimodel_upload_info=False,
            add_audio_upload_info=False) is not None


class TestLegacyDictSafetyNet:
    """A dict can still reach a content list by in-place mutation; coercion keeps the turn alive."""

    def test_coerce_content_item_handles_content_item(self):
        item = ContentItem(text='already fine')
        assert coerce_content_item(item) is item

    def test_coerce_content_item_handles_legacy_text_dict(self):
        coerced = coerce_content_item({'type': 'text', 'text': 'legacy'})
        assert isinstance(coerced, ContentItem)
        assert coerced.text == 'legacy'

    def test_coerce_content_item_degrades_unknown_dict_to_text(self):
        coerced = coerce_content_item({'weird': 'shape'})
        assert isinstance(coerced, ContentItem)
        assert 'weird' in coerced.text

    def test_multimodal_funnel_tolerates_legacy_dict(self):
        msg = _multimodal_message()
        msg.content.append({'type': 'text', 'text': WARNING})  # bypasses pydantic validation

        reformatted = format_as_multimodal_message(
            msg, add_upload_info=False, add_multimodel_upload_info=False, add_audio_upload_info=False)

        assert all(isinstance(item, ContentItem) for item in reformatted.content)
        assert WARNING in extract_text_from_message(msg, add_upload_info=False)

    def test_text_only_path_tolerates_legacy_text_dict(self):
        msg = Message(role=USER, content=[ContentItem(text='hello')])
        msg.content.append({'type': 'text', 'text': 'legacy tail'})  # bypasses validation

        [out] = _format_as_text_messages([msg])

        assert 'hello' in out.content
        assert 'legacy tail' in out.content
