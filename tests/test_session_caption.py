"""Tests for the "session caption" feature (compression-derived, shown in UI).

Covers the four spec areas:

1. Compression parse/strip — ``_parse_compression_output`` must split the raw
   compressor output into a clean summary body (never containing the marker or
   caption) and an optional one-line caption, while staying fully backward
   compatible with old output that has no ``CAPTION:`` line. These are the
   critical tests: the parser must not corrupt summaries or break the
   end-marker validation / retry path.

2. Metadata — ``AgentInstanceLogger.set_caption`` first-meaningful-wins semantics
   (a second call never clobbers an existing non-empty caption) and that the
   default metadata dict carries a well-defined ``caption`` field.

3. API — ``/api/sessions`` caption helpers: real caption from line-1 metadata,
   truncated first-USER-message fallback, "" when neither exists, truncation cap,
   and early stop (does not read the whole file).

All tests are self-contained — no LLM or running server required.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.prompts.dna import COMPRESSION_END_MARKER
from agent_cascade.compression.agent_invoker import _parse_compression_output


# ────────────────────────────────────────────────────────────────────────────
# 1. Compression parse/strip (the delicate part)
# ────────────────────────────────────────────────────────────────────────────

class TestParseCompressionOutput:
    """_parse_compression_output() must split summary + caption safely."""

    def test_caption_present(self):
        """A well-formed CAPTION line after the marker is parsed out cleanly."""
        raw = f"Did X, then Y.{COMPRESSION_END_MARKER} CAPTION: Debugging the parser"
        summary, caption = _parse_compression_output(raw)
        assert summary == "Did X, then Y."
        assert caption == "Debugging the parser"

    def test_caption_absent_backward_compat(self):
        """Old output with no CAPTION still validates and yields caption=''."""
        raw = f"Some summary text.{COMPRESSION_END_MARKER}"
        summary, caption = _parse_compression_output(raw)
        assert summary == "Some summary text."
        assert caption == ""

    def test_caption_absent_trailing_whitespace(self):
        """Trailing whitespace after the marker (no caption) is tolerated."""
        raw = f"Summary here.{COMPRESSION_END_MARKER}   \n"
        summary, caption = _parse_compression_output(raw)
        assert summary == "Summary here."
        assert caption == ""

    def test_empty_summary_with_marker_raises(self):
        """Nothing before the marker → 'empty summary' (retryable) error."""
        with pytest.raises(RuntimeError) as exc:
            _parse_compression_output(f"{COMPRESSION_END_MARKER} CAPTION: Something")
        assert "empty summary" in str(exc.value).lower()

    def test_missing_marker_raises(self):
        """No end marker at all → 'missing end marker' (retryable) error."""
        with pytest.raises(RuntimeError) as exc:
            _parse_compression_output("just some text without a marker")
        assert "missing end marker" in str(exc.value).lower()

    def test_malformed_trailing_text(self):
        """Stray non-CAPTION text after the marker → warn + caption='', never leaked."""
        raw = f"Body text.{COMPRESSION_END_MARKER} some stray trailing note"
        summary, caption = _parse_compression_output(raw)
        assert summary == "Body text."
        assert caption == ""

    def test_caption_containing_marker(self):
        """Edge: a marker string appearing in the tail must NOT leak into the summary
        body. When the model emits a spurious second marker (e.g. inside a caption), the
        parser falls back to the FIRST marker as the boundary so the returned body is
        guaranteed marker-free — the critical invariant for model-context safety."""
        raw = f"Real body.{COMPRESSION_END_MARKER} CAPTION: saw {COMPRESSION_END_MARKER} again"
        summary, caption = _parse_compression_output(raw)
        # The hard invariant: no end marker may survive in the returned summary body.
        assert COMPRESSION_END_MARKER not in summary
        # The caption text must never leak into the summary body either.
        assert "saw" not in summary
        # With the first-marker fallback, the real body is recovered and the (malformed)
        # tail after it yields no usable caption.
        assert summary == "Real body."

    def test_multiline_caption_rejected(self):
        """A multi-line tail is malformed: caption='' and the second line never
        leaks into the summary body."""
        raw = f"Body.{COMPRESSION_END_MARKER} CAPTION: first line\nsecond line leaked?"
        summary, caption = _parse_compression_output(raw)
        assert summary == "Body."
        assert caption == ""
        # The stray second line must NOT appear in the summary body.
        assert "second line" not in summary

    def test_summary_body_never_contains_marker_or_caption(self):
        """Hard invariant: the returned summary body is clean for model context."""
        raw = f"Clean summary.{COMPRESSION_END_MARKER} CAPTION: A session caption"
        summary, caption = _parse_compression_output(raw)
        assert COMPRESSION_END_MARKER not in summary
        assert "CAPTION:" not in summary
        assert "A session caption" not in summary


# ────────────────────────────────────────────────────────────────────────────
# 2. Metadata: set_caption first-wins semantics
# ────────────────────────────────────────────────────────────────────────────

class TestLoggerSetCaption:
    """AgentInstanceLogger.set_caption() must be first-meaningful-wins."""

    def _make_logger(self, tmp_path):
        from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger
        return AgentInstanceLogger(
            agent_class="coder",
            instance_name="w1",
            log_dir=str(tmp_path),
        )

    def test_default_metadata_has_caption_field(self, tmp_path):
        """The default metadata dict carries a well-defined (empty) caption field."""
        lg = self._make_logger(tmp_path)
        assert "caption" in lg.data["metadata"]
        assert lg.data["metadata"]["caption"] == ""

    def test_set_caption_sets_when_empty(self, tmp_path):
        lg = self._make_logger(tmp_path)
        lg.set_caption("First caption")
        assert lg.data["metadata"]["caption"] == "First caption"

    def test_set_caption_does_not_clobber(self, tmp_path):
        """A second call must not overwrite an existing non-empty caption."""
        lg = self._make_logger(tmp_path)
        lg.set_caption("First caption")
        lg.set_caption("Second caption")
        assert lg.data["metadata"]["caption"] == "First caption"

    def test_set_caption_empty_does_not_clear(self, tmp_path):
        """Passing an empty caption must not clear an existing one."""
        lg = self._make_logger(tmp_path)
        lg.set_caption("Real caption")
        lg.set_caption("")
        assert lg.data["metadata"]["caption"] == "Real caption"

    def test_set_caption_empty_on_empty_leaves_empty(self, tmp_path):
        """Empty on an empty field stays empty (no spurious value written)."""
        lg = self._make_logger(tmp_path)
        lg.set_caption("")
        assert lg.data["metadata"]["caption"] == ""


# ────────────────────────────────────────────────────────────────────────────
# 3. API caption helpers (line-1 metadata + first-user fallback + truncation)
# ────────────────────────────────────────────────────────────────────────────

class TestApiSessionCaption:
    """/api/sessions caption helpers: real caption, user-message fallback, cap."""

    def _write_jsonl(self, tmp_path, lines):
        p = tmp_path / "coder_w1_20260829_000000.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return p

    def test_real_caption_from_metadata(self, tmp_path):
        from agent_cascade.api_server import _read_session_caption
        meta = {"metadata": {"caption": "Building the caption feature"}}
        path = self._write_jsonl(tmp_path, [json.dumps(meta)])
        assert _read_session_caption(path) == "Building the caption feature"

    def test_fallback_to_first_user_message(self, tmp_path):
        from agent_cascade.api_server import _read_session_caption
        meta = {"metadata": {"caption": ""}}
        user_msg = {"role": "user", "content": "Please implement the parser"}
        path = self._write_jsonl(tmp_path, [json.dumps(meta), json.dumps(user_msg)])
        assert _read_session_caption(path) == "Please implement the parser"

    def test_empty_when_nothing(self, tmp_path):
        from agent_cascade.api_server import _read_session_caption
        meta = {"metadata": {}}  # no caption field at all
        path = self._write_jsonl(tmp_path, [json.dumps(meta)])
        assert _read_session_caption(path) == ""

    def test_truncation_caps_long_user_message(self, tmp_path):
        from agent_cascade.api_server import _read_session_caption, _SESSION_CAPTION_MAX_LEN
        meta = {"metadata": {"caption": ""}}
        long_text = "word " * 100  # far longer than the cap
        user_msg = {"role": "user", "content": long_text.strip()}
        path = self._write_jsonl(tmp_path, [json.dumps(meta), json.dumps(user_msg)])
        result = _read_session_caption(path)
        assert len(result) <= _SESSION_CAPTION_MAX_LEN + 1  # +1 for the ellipsis
        assert result.endswith("…")

    def test_stops_reading_early(self, tmp_path):
        """The first USER message is found near the top; the helper must not parse
        the whole file. We verify by wrapping open() and counting readline() calls."""
        from agent_cascade.api_server import _read_session_caption
        meta = {"metadata": {"caption": ""}}
        lines = [json.dumps(meta), json.dumps({"role": "user", "content": "early user msg"})]
        # Append many more lines that we should NOT need to read.
        for i in range(500):
            lines.append(json.dumps({"role": "assistant", "content": f"filler {i}"}))
        path = self._write_jsonl(tmp_path, lines)

        read_lines = []
        real_open = open

        def spy_open(*args, **kwargs):
            handle = real_open(*args, **kwargs)
            return _LineSpy(handle, read_lines)

        with patch("builtins.open", side_effect=spy_open):
            result = _read_session_caption(path)

        assert result == "early user msg"
        # We only needed the metadata line + the first user message (2 lines).
        assert len(read_lines) <= 3, f"Read too many lines: {len(read_lines)}"

    def test_multimodal_user_content(self, tmp_path):
        """A list-content (multimodal) user message is handled via extract_text_from_message.

        Uses the real ContentItem serialization format ({'text': ...}, no 'type' key) so
        Message(**dict) round-trips cleanly — matching how logs are actually written.
        """
        from agent_cascade.api_server import _read_session_caption
        meta = {"metadata": {"caption": ""}}
        user_msg = {"role": "user", "content": [{"text": "hello multimodal"}]}
        path = self._write_jsonl(tmp_path, [json.dumps(meta), json.dumps(user_msg)])
        result = _read_session_caption(path)
        assert "hello multimodal" in result


class _LineSpy:
    """Wraps a file handle and records each readline() for the early-stop test."""

    def __init__(self, handle, recorded):
        self._handle = handle
        self._recorded = recorded

    def readline(self, *args, **kwargs):
        line = self._handle.readline(*args, **kwargs)
        self._recorded.append(line)
        return line

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()
        return False


# ────────────────────────────────────────────────────────────────────────────
# 4. Integration: compress_context threads the caption into the marker body check
# ────────────────────────────────────────────────────────────────────────────

class TestCompressContextCaptionIntegration:
    """End-to-end (mocked compressor): caption must NOT leak into the marker body."""

    def test_caption_not_in_marker_body(self, tmp_path):
        from agent_cascade.compression.core import compress_context
        from tests.conftest import MockAgentPool
        from agent_cascade.llm.schema import SYSTEM, USER

        # Build a small history with enough active content to compress.
        history = [Message_obj(SYSTEM, "You are a test agent")]
        for i in range(10):
            history.append(Message_obj(USER, f"user {i}"))
            history.append(Message_obj("assistant", f"assistant {i}"))

        pool = MockAgentPool(history)

        # The mock instance has no agent_class; give it one so core.py can fetch the logger.
        pool.instances["TestAgent"].agent_class = "coder"

        # Wire a REAL AgentInstanceLogger into the pool so set_caption() actually runs
        # against real metadata (not a MagicMock) and we can assert on it directly.
        from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger
        real_logger = AgentInstanceLogger(
            agent_class="coder", instance_name="TestAgent", log_dir=str(tmp_path),
        )

        # MockAgentPool is a minimal simulation — add the two methods core.py's caption
        # block needs (get_instance + get_logger) so we exercise the real code path.
        pool.get_instance = lambda name: pool.instances.get(name)
        pool.get_logger = lambda name, agent_class, base_metadata=None: real_logger

        # Fake compressor agent for token-budget estimation.
        mock_comp_agent = MagicMock()
        mock_comp_agent.llm.generate_cfg = {"max_input_tokens": 128000}
        original_get_agent = pool.get_agent
        def patched_get_agent(name):
            if name == "Compressor":
                return mock_comp_agent
            return original_get_agent(name)
        pool.get_agent = patched_get_agent

        # Patch the compressor to return a summary WITH a caption.
        with patch("agent_cascade.compression.core.invoke_compression_agent") as mock_invoke:
            mock_invoke.return_value = ("Clean summary body", "My session caption")

            result = compress_context(
                agent_pool=pool,
                target_agent_name="TestAgent",
                fraction=0.5,
                mode="auto",
                force=True,
            )

            assert result.success is True
            # The marker body must contain the clean summary but NOT the caption/marker.
            marker_content = result.marker_message.content
            assert "Clean summary body" in marker_content
            assert "My session caption" not in marker_content
            assert COMPRESSION_END_MARKER not in marker_content
            # The parsed caption was threaded into the real logger's metadata (first-wins).
            assert real_logger.data["metadata"]["caption"] == "My session caption"


# ────────────────────────────────────────────────────────────────────────────
# 5. /compress command uses single-executor path (caption fix)
# ────────────────────────────────────────────────────────────────────────────

class TestCompressCommandCaptionPath:
    """Verify handle_compress_command calls compress_context with dry_run=False
    and no precomputed_summary, so the caption is parsed and saved in one pass."""

    def _make_handler(self):
        """Build a CompressionHandler with mocked pool/engine for unit testing."""
        from agent_cascade.compression.handler import CompressionHandler
        handler = CompressionHandler(MagicMock())
        # Mock engine with required methods
        engine = MagicMock()
        engine._telemetry.return_value = None  # skip telemetry
        handler.set_engine(engine)
        return handler, engine

    def test_compress_command_calls_tool_without_dry_run(self):
        """The new single-pass path must NOT pass dry_run=True or precomputed_summary."""
        from agent_cascade.llm.schema import Message, USER

        handler, engine = self._make_handler()

        # Mock the pool/template/tool chain
        mock_tool = MagicMock()
        mock_tool.call.return_value = "Compressed 50% of history successfully."
        template = MagicMock()
        template.function_map = {'compress_context': mock_tool}
        handler.pool.get_template.return_value = template

        # Mock instance
        instance = MagicMock()
        instance.instance_name = "test-agent"
        instance.agent_class = "coder"
        instance._allocated_max_input_tokens = 128000
        instance.parent_instance = None

        # detect_and_parse_compress_command returns a fraction
        handler.detect_and_parse_compress_command = MagicMock(return_value=0.5)

        # Mock pool.get_conversation to return a small valid list
        handler.pool.get_conversation.return_value = [Message(role=USER, content="x")]

        # Run the command
        result = handler.handle_compress_command(instance, [], [], response=None)

        assert result is True

        # THE KEY ASSERTION: compress_tool.call was invoked WITHOUT dry_run and
        # WITHOUT precomputed_summary — this means core.py reaches set_caption().
        call_kwargs = mock_tool.call.call_args.kwargs
        assert 'dry_run' not in call_kwargs, (
            "handle_compress_command must NOT pass dry_run=True; "
            "the caption would be skipped by the core.py guard"
        )
        assert 'precomputed_summary' not in call_kwargs, (
            "handle_compress_command must NOT pass precomputed_summary; "
            "the LLM must run so caption is parsed and saved"
        )

    def test_compress_command_tool_unavailable_returns_true(self):
        """When compress_context tool is missing, the command is still 'handled'."""
        handler, engine = self._make_handler()

        # Template without compress_context in function_map
        template = MagicMock()
        template.function_map = {}
        handler.pool.get_template.return_value = template

        instance = MagicMock()
        instance.instance_name = "test-agent"
        instance.agent_class = "coder"

        handler.detect_and_parse_compress_command = MagicMock(return_value=0.5)

        result = handler.handle_compress_command(instance, [], [], response=None)
        assert result is True  # command was handled (with error notification)

    def test_compress_command_failure_notification(self):
        """When the tool returns a failure string, a notification is injected."""
        from agent_cascade.llm.schema import Message, USER

        handler, engine = self._make_handler()

        mock_tool = MagicMock()
        mock_tool.call.return_value = "Compression failed: not enough messages"
        template = MagicMock()
        template.function_map = {'compress_context': mock_tool}
        handler.pool.get_template.return_value = template

        instance = MagicMock()
        instance.instance_name = "test-agent"
        instance.agent_class = "coder"

        handler.detect_and_parse_compress_command = MagicMock(return_value=0.5)

        # Patch the notification method to verify it's called with the right text
        mock_inject = MagicMock()
        handler._inject_compression_notification = mock_inject

        result = handler.handle_compress_command(instance, [], [], response=None)
        assert result is True  # command handled (failure notified)

        # _inject_compression_notification should have been called with failure text
        mock_inject.assert_called_once()
        call_args = mock_inject.call_args
        assert "Compression failed" in call_args[0][1]


def Message_obj(role, content):
    """Local factory (avoids importing Message at module top to keep imports light)."""
    from agent_cascade.llm.schema import Message
    return Message(role=role, content=content)
