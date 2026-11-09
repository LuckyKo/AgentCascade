"""Regression tests for BUG_0276: the `item.file` flatten gap.

Same bug class as the v0.14.31 audio/video flatten fix (commit 81a71114):
a ContentItem carrying only `file` (or, for dialogue retrieval, `video`) is
handled inconsistently across three sites — it either RAISES or is SILENTLY
DROPPED, while image/audio/video are handled:

1. compression/handler.py flatten loop (non-vision stringify): no `elif item.file:`
   -> a file-only ContentItem is dropped from the flattened markdown.
2. llm/function_calling.py::validate_num_fncall_results: `if item.file: raise`
   -> a file item in a tool-call result CRASHES the validation.
3. agents/dialogue_retrieval_agent.py content-preservation predicate: `video`
   omitted -> a video item is dropped when the long-dialogue agent rebuilds content.

Fix: a `file` item becomes a `[File: ...]` text marker (consistent with the
existing `[Audio file: ...]` / `[Video file: ...]` markers), never raises and is
never silently dropped; `video` is handled everywhere `audio` is.

These tests assert the marker string / non-raise / branch presence directly so
that removing any of the three fixes re-introduces the failure (mutation-sensitive).
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_cascade.llm.schema import ASSISTANT, FUNCTION, ContentItem, FunctionCall, Message


class TestFileContentItemFlatten:
    """compression/handler.py: a file ContentItem must flatten to a [File: ...] marker."""

    def test_non_vision_agent_preserves_file_marker(self):
        """Regression: a `file`-only ContentItem must NOT be silently dropped by the
        non-vision text-flatten path in compression/handler.py. The v0.14.31 fix added
        audio/video branches; `file` was never added, so a file-only item matched
        `isinstance(item, ContentItem)` but no `elif`, and vanished from the flattened
        string (memory after compression). The fix emits a `[File: ...]` marker.
        """
        from agent_cascade.compression.handler import CompressionHandler

        mock_instance = MagicMock()
        mock_instance._pending_notifications = []
        mock_instance._cache_notifications = []
        mock_instance._tool_warnings = []

        file_path = 'file:///N:/work/WD/AgentWorkspace/report.pdf'
        tool_result = [
            ContentItem(file=file_path),
            ContentItem(text='Attached document: report.pdf'),
        ]

        handler = CompressionHandler(MagicMock())  # pool is required by __init__
        llm_cfg = {
            'model_service_type': 'oai',
            'max_input_tokens': 8192,
        }  # NOT vision-capable -> exercises the flatten loop

        result = handler._assemble_tool_result(
            instance=mock_instance,
            raw_tool_result=tool_result,
            char_limit=10000,
            instance_name='Maine',
            tool_name='read_file',
            base_dir=Path('.'),
            llm_cfg=llm_cfg,
        )

        assert isinstance(result, str), \
            f"Expected flattened markdown string for non-vision agent, got {type(result).__name__}"
        assert f"[File: {file_path}]" in result, \
            "File ContentItem was dropped during flatten — expected '[File: ...]' marker"
        assert 'Attached document: report.pdf' in result, \
            'Text ContentItem should still be preserved alongside the file marker'

    def test_assemble_source_has_file_branch(self):
        """Source-pinned mutation guard for the file flatten fix.

        Reads the actual source of CompressionHandler._assemble_tool_result and asserts
        the file branch and its exact marker string are present. If someone removes the
        `elif item.file:` line (re-introducing the silent-drop bug), this test fails on
        its own — independent of the functional case. Marker text is pinned to match the
        audio/video marker format.
        """
        import inspect

        from agent_cascade.compression.handler import CompressionHandler

        src = inspect.getsource(CompressionHandler._assemble_tool_result)
        assert 'elif item.file:' in src, \
            'file branch removed from the flatten loop (silent-drop regression)'
        assert 'f"[File: {item.file}]"' in src, \
            'file marker must match the [Audio file: ...] / [Video file: ...] format'


class TestToolCallFileItemValidation:
    """llm/function_calling.py: a file item in a tool-call result must not raise."""

    def test_validate_num_fncall_results_accepts_file_item(self):
        """A FUNCTION message whose content is a file-only ContentItem must NOT raise.

        Pre-fix this raised `ValueError('Tool call results with content type="file" are
        not supported.')`. Post-fix the file ContentItem is converted in-place to a text
        ContentItem carrying the `[File: ...]` marker, so the content becomes valid text
        downstream. The existing image / non-multimodal `raise` guard is left untouched.
        """
        from agent_cascade.llm.function_calling import validate_num_fncall_results

        file_path = 'file:///N:/work/WD/AgentWorkspace/output.csv'
        messages = [
            Message(role=ASSISTANT, name=None, content='',
                    function_call=FunctionCall(name='read_file', arguments='{}')),
            Message(role=FUNCTION, name='read_file', content=[ContentItem(file=file_path)]),
        ]

        # Must NOT raise, regardless of multimodal support (file becomes text).
        validate_num_fncall_results(messages, support_multimodal_input=False)

        content = messages[-1].content
        assert isinstance(content, list)
        assert len(content) == 1
        converted = content[0]
        assert converted.file is None, 'file field should have been cleared by the in-place conversion'
        assert converted.text == f'[File: {file_path}]', \
            'file item should be converted to a [File: ...] text marker'

    def test_validate_source_keeps_image_guard(self):
        """Mutation guard: the fix must NOT remove the existing image non-multimodal raise.

        The `if item.image and (not support_multimodal_input): raise ...` guard is
        intentionally preserved. Assert the image guard (and its message) is still in the
        source, and the file `raise` is gone (replaced by the conversion).
        """
        import inspect

        from agent_cascade.llm.function_calling import validate_num_fncall_results

        src = inspect.getsource(validate_num_fncall_results)
        assert 'if item.image and (not support_multimodal_input):' in src, \
            'image non-multimodal guard must stay'
        assert 'content type="file" are not supported' not in src, \
            'the file ValueError raise must be replaced by the [File: ...] conversion'
        assert 'if item.file:' in src, 'file item must still be handled (converted to text)'


class TestVideoPreservedInDialogueRetrieval:
    """agents/dialogue_retrieval_agent.py: a video item must be kept, like audio/image/file."""

    def test_content_predicate_includes_video(self):
        """Source-pinned guard for the dialogue-retrieval content predicate.

        `_run` rebuilds `new_content` and re-appends media items from the last user
        message via `if item.file or item.image or item.audio:` — `item.video` was
        omitted, so a video item was silently dropped. This test asserts the predicate
        includes `video` so the drop cannot reappear. Mirrors the source-pin pattern
        used for the audio/video flatten branches.
        """
        import inspect

        from agent_cascade.agents.dialogue_retrieval_agent import DialogueRetrievalAgent

        src = inspect.getsource(DialogueRetrievalAgent._run)
        assert 'if item.file or item.image or item.audio or item.video:' in src, \
            'video dropped from the dialogue-retrieval content predicate — expected `or item.video`'
