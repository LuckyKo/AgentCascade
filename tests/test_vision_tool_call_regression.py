"""Regression test for tool_message_image_embedding_must_not_be_flattened.md.

Direct integration tests that verify the image embedding fix without requiring
the full AC runtime (worker threads, WebSocket handlers, etc).

Tests:
1. _assemble_tool_result preserves ContentItem lists for vision agents (qwenvl_oai)
2. qwenvl_oai.convert_messages_to_dicts converts file:// URIs to base64
3. Full flow: tool result -> Message -> LLM call with base64 images
"""

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestVisionToolCallAssembleResult:
    """Test _assemble_tool_result preserves ContentItem lists for vision agents."""
    
    def test_vision_agent_preserves_contentitem_list(self):
        """For qwenvl_oai agents, view_image's ContentItem list should NOT be stringified."""
        from agent_cascade.llm.schema import ContentItem
        
        # Create a mock instance (AgentInstance-like) with proper empty notification queues
        mock_instance = MagicMock()
        mock_instance._pending_notifications = []
        mock_instance._cache_notifications = []
        mock_instance._tool_warnings = []
        
        # Create tool result exactly like view_image returns
        file_url = "file:///N:/work/WD/AgentWorkspace/no_thx.jpg"
        tool_result = [
            ContentItem(image=file_url),
            ContentItem(text="Viewing image: no_thx.jpg")
        ]
        
        # Create compression handler with vision-capable llm_cfg
        from agent_cascade.compression.handler import CompressionHandler
        
        handler = CompressionHandler(MagicMock())  # pool is required by __init__
        
        # Call _assemble_tool_result with qwenvl_oai config (vision-capable)
        llm_cfg = {
            "model_service_type": "qwenvl_oai",
            "max_input_tokens": 8192,
        }
        
        result = handler._assemble_tool_result(
            instance=mock_instance,
            raw_tool_result=tool_result,
            char_limit=10000,
            instance_name="Maine",
            tool_name="view_image",
            base_dir=Path("."),
            llm_cfg=llm_cfg,
        )
        
        # The result should be a ContentItem list, NOT a string
        assert isinstance(result, list), \
            f"Expected ContentItem list for vision agent, got {type(result).__name__}: {str(result)[:200]}"
        
        assert len(result) == 2, f"Expected 2 ContentItems, got {len(result)}"
        assert result[0].image == file_url, "First item should be image ContentItem"
        assert result[1].text == "Viewing image: no_thx.jpg", "Second item should be text ContentItem"
    
    def test_non_vision_agent_stringifies_contentitem_list(self):
        """For non-vision agents (oai), view_image's ContentItem list SHOULD be stringified to markdown."""
        from agent_cascade.llm.schema import ContentItem
        
        mock_instance = MagicMock()
        mock_instance._pending_notifications = []
        mock_instance._cache_notifications = []
        mock_instance._tool_warnings = []
        
        file_url = "file:///N:/work/WD/AgentWorkspace/no_thx.jpg"
        tool_result = [
            ContentItem(image=file_url),
            ContentItem(text="Viewing image: no_thx.jpg")
        ]
        
        from agent_cascade.compression.handler import CompressionHandler
        
        handler = CompressionHandler(MagicMock())  # pool is required by __init__
        
        # Call with plain oai config (NOT vision-capable)
        llm_cfg = {
            "model_service_type": "oai",
            "max_input_tokens": 8192,
        }
        
        result = handler._assemble_tool_result(
            instance=mock_instance,
            raw_tool_result=tool_result,
            char_limit=10000,
            instance_name="Maine",
            tool_name="view_image",
            base_dir=Path("."),
            llm_cfg=llm_cfg,
        )
        
        # For non-vision agents, result should be a markdown string
        assert isinstance(result, str), \
            f"Expected markdown string for non-vision agent, got {type(result).__name__}"
        
        # Should contain markdown image link with file:// URI (since we can't encode images)
        assert "![" in result or "file://" in result, \
            "Non-vision agent result should contain markdown image link"


class TestVisionToolCallMessageConversion:
    """Test qwenvl_oai.convert_messages_to_dicts converts images to base64."""
    
    @pytest.fixture
    def test_image_path(self, tmp_path):
        """Create a minimal valid JPEG for testing."""
        # Minimal 1x1 white pixel JPEG
        jpeg_bytes = bytes([
            0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
            0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
            0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
            0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
            0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
            0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
            0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
            0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
            0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
            0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
            0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
            0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
            0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
            0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
            0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
            0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
            0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
            0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
            0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
            0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
            0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
            0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
            0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
            0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
            0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
            0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
            0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD5, 0xDB, 0x20, 0xA8, 0xF9, 0xFF, 0xD9
        ])
        
        img_path = tmp_path / "test_image.jpg"
        img_path.write_bytes(jpeg_bytes)
        return img_path
    
    def test_qwenvl_converts_file_uri_to_base64(self, test_image_path):
        """qwenvl_oai.convert_messages_to_dicts should convert file:// URIs to base64 data URIs."""
        from agent_cascade.llm.schema import Message, ContentItem
        from agent_cascade.llm.qwenvl_oai import QwenVLChatAtOAI
        
        # Create a function message with ContentItem list (like view_image result)
        file_url = test_image_path.as_uri()  # file:///path/to/test_image.jpg
        
        messages = [
            Message(role="user", content="Please look at this image"),
            Message(role="assistant", content="I'll use view_image to see it"),
            Message(
                role="function",
                name="view_image",
                content=[
                    ContentItem(image=file_url),
                    ContentItem(text=f"Viewing image: {test_image_path.name}")
                ]
            )
        ]
        
        # Create a QwenVLChatAtOai instance (doesn't need real API config for this test)
        llm = QwenVLChatAtOAI(cfg={"model": "test-model", "api_base": "http://localhost:9999/v1"})
        
        # Convert messages to dicts (this is called before the actual API call)
        converted = llm.convert_messages_to_dicts(messages)
        
        # Find the function message in converted output.
        # After _conv_agent_cascade_messages_to_oai(), FUNCTION role becomes "tool" with no "name".
        # Look for tool messages that contain view_image-related content (image_url items or Viewing image text).
        func_msg = None
        for msg in converted:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                has_view_image_content = False
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "image_url":
                                has_view_image_content = True
                                break
                            text = item.get("text", "")
                            if isinstance(text, str) and "Viewing image" in text:
                                has_view_image_content = True
                                break
                if has_view_image_content:
                    func_msg = msg
                    break
        
        assert func_msg is not None, "Function message not found in converted messages"
        
        content = func_msg.get("content")
        assert isinstance(content, list), \
            f"Function message content should be a list, got {type(content).__name__}"
        
        # Find the image_url item
        image_item = None
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image_url":
                image_item = item
                break
        
        assert image_item is not None, \
            f"No image_url item found in function message content. Content: {content}"
        
        url = image_item.get("image_url", {}).get("url", "")
        
        # Verify it's a base64 data URI (not a file:// URI)
        assert url.startswith("data:image"), \
            f"Expected base64 data URI, got: {url[:100]}"
        
        # Extract and verify the base64 part
        b64_data = url.split(",", 1)[1]
        assert len(b64_data) > 50, "Base64 data too short — image may not have been read correctly"
        
        # Verify it's valid base64 by decoding
        decoded = base64.b64decode(b64_data)
        assert decoded.startswith(b'\xFF\xD8'), "Decoded data doesn't start with JPEG magic bytes"


class TestVisionToolCallFullFlow:
    """Integration test simulating the full flow from tool result to LLM call."""
    
    @pytest.fixture
    def test_image_path(self, tmp_path):
        """Create a minimal valid JPEG for testing."""
        jpeg_bytes = bytes([
            0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
            0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
            0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
            0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
            0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
            0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
            0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
            0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
            0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
            0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
            0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
            0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
            0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
            0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
            0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
            0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
            0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
            0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
            0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
            0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
            0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
            0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
            0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
            0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
            0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
            0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
            0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD5, 0xDB, 0x20, 0xA8, 0xF9, 0xFF, 0xD9
        ])
        
        img_path = tmp_path / "no_thx.jpg"
        img_path.write_bytes(jpeg_bytes)
        return img_path
    
    def test_full_flow_vision_agent(self, test_image_path):
        """Full flow: view_image result -> _assemble_tool_result -> Message -> convert_messages_to_dicts.
        
        This simulates exactly what happens in execution_engine.py when a vision agent
        receives a tool call result with images.
        """
        from agent_cascade.llm.schema import Message, ContentItem
        from agent_cascade.compression.handler import CompressionHandler
        from agent_cascade.llm.qwenvl_oai import QwenVLChatAtOAI
        
        # Step 1: view_image returns ContentItem list
        file_url = test_image_path.as_uri()
        tool_result = [
            ContentItem(image=file_url),
            ContentItem(text=f"Viewing image: {test_image_path.name}")
        ]
        
        # Step 2: _assemble_tool_result processes it for a vision agent
        handler = CompressionHandler(MagicMock())  # pool is required by __init__
        
        mock_instance = MagicMock()
        mock_instance._pending_notifications = []
        mock_instance._cache_notifications = []
        mock_instance._tool_warnings = []
        
        llm_cfg = {"model_service_type": "qwenvl_oai"}
        
        assembled = handler._assemble_tool_result(
            instance=mock_instance,
            raw_tool_result=tool_result,
            char_limit=10000,
            instance_name="Maine",
            tool_name="view_image",
            base_dir=Path("."),
            llm_cfg=llm_cfg,
        )
        
        # Should be preserved as ContentItem list
        assert isinstance(assembled, list), \
            f"Step 2 FAILED: ContentItem list was stringified (BUG). Got {type(assembled).__name__}"
        
        # Step 3: execution_engine.py creates a function Message with this content
        fn_msg = Message(
            role="function",
            name="view_image",
            content=assembled,
        )
        
        # Verify Message stored ContentItem instances
        assert isinstance(fn_msg.content, list), "Message.content should be a list"
        assert any(hasattr(item, 'image') for item in fn_msg.content), \
            "ContentItem instances lost — got plain dicts instead"
        
        # Step 4: Build conversation with the function message
        messages = [
            Message(role="user", content="Please look at this image"),
            Message(role="assistant", content="I'll check it"),
            fn_msg,
        ]
        
        # Step 5: qwenvl_oai converts to dicts for API call — images become base64
        llm = QwenVLChatAtOAI(cfg={"model": "test-model", "api_base": "http://localhost:9999/v1"})
        converted = llm.convert_messages_to_dicts(messages)
        
        # Verify function message in converted output has base64 image.
        # After _conv_agent_cascade_messages_to_oai(), FUNCTION role becomes "tool" with no "name".
        func_msg = None
        for msg in converted:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                has_view_image_content = False
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "image_url":
                                has_view_image_content = True
                                break
                            text = item.get("text", "")
                            if isinstance(text, str) and "Viewing image" in text:
                                has_view_image_content = True
                                break
                if has_view_image_content:
                    func_msg = msg
                    break
        
        assert func_msg is not None, "Function message lost during conversion"
        
        content = func_msg.get("content")
        assert isinstance(content, list), f"Content should be list, got {type(content).__name__}"
        
        # Find image_url item with base64 data
        has_base64_image = False
        has_file_uri_bug = False
        has_markdown_bug = False
        
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "image_url":
                    url = item.get("image_url", {}).get("url", "")
                    if url.startswith("data:image"):
                        has_base64_image = True
                    elif url.startswith("file://"):
                        has_file_uri_bug = True
                
                if item.get("type") == "text":
                    text = item.get("text", "")
                    if "![" in text and ".jpg)" in text:
                        has_markdown_bug = True
        
        # Core assertion: base64 image must be present
        assert has_base64_image, \
            f"FULL FLOW FAILED: No base64 image in function message.\nContent: {content}"
        
        # Negative assertions: these indicate the bug is present
        assert not has_file_uri_bug, \
            "BUG DETECTED: file:// URI leaked into converted message (images not encoded)"
        
        assert not has_markdown_bug, \
            "BUG DETECTED: Markdown image link found (ContentItem list was stringified)"