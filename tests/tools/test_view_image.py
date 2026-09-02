"""Unit tests for the view_image tool (agent_cascade/tools/custom/file_ops.py::ViewImage).

Focus: the redundant re-captioning fix (Option A).

view_image returns ``[ContentItem(image=...), ContentItem(text=caption)]``. Before the fix
the image item's ``caption`` field was left empty, so the router's return-path guard
(``_has_uncaptioned_images``) fired a SECOND vision caption LLM call on an image that had
already been described. The fix attaches the descriptive line as the image item's caption
so the guard skips it — while keeping the separate text item (text-only agents still get
the description) and NOT stripping image pixels (vision path still sends them).

File placement: existing view_image coverage lives in ``tests/test_media_storage.py``
(TestViewImageCropRegion), which sits at the tests/ root rather than tests/tools/. This new
file groups the captioning regression with the other tool-level tests under tests/tools/,
mirroring tests/tools/test_image_gen.py. It reuses the same fixture pattern (patched
_resolve_path + real PIL image) so it stays hermetic.

All external I/O is mocked (save_image_to_media / base64 encoding). No network calls.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from agent_cascade.llm.schema import ContentItem, Message, FUNCTION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_image_200x150(tmp_path):
    """Create a 200x150 test image and return its path as a string."""
    img = Image.new("RGB", (200, 150), color="green")
    for x in range(100, 200):
        for y in range(75, 150):
            img.putpixel((x, y), (255, 0, 0))
    p = tmp_path / "view_test_200x150.png"
    img.save(str(p), format="PNG")
    return str(p)


@pytest.fixture
def view_image_tool(test_image_200x150):
    """ViewImage tool with _resolve_path patched to allow temp paths."""
    from agent_cascade.tools.custom.file_ops import ViewImage
    tool = ViewImage()

    def _mock_resolve(path, mode="ro"):
        p = Path(path)
        if p.exists():
            return p
        raise ValueError(f"Image not found: {path}")

    tool._resolve_path = _mock_resolve
    return tool


def _guard_flags(items):
    """Wrap a tool result list in a FUNCTION message and ask the router's return-path guard
    whether any image still needs captioning (the exact check that gates the 2nd vision call)."""
    from agent_cascade.api_router_pkg.router import APIRouter
    fn_msg = Message(role=FUNCTION, name="view_image", content=list(items))
    return APIRouter._has_uncaptioned_images([fn_msg])


# ---------------------------------------------------------------------------
# Media-path branch (save_image_to_media succeeds)
# ---------------------------------------------------------------------------

class TestViewImageMediaPathCaption:
    def test_media_path_attaches_caption_to_image_item(self, view_image_tool, test_image_200x150, tmp_path):
        """The descriptive line is attached as the image item's caption AND kept as a text
        item — so both vision (pixels) and text-only (description) agents are served."""
        from agent_cascade.tools.custom.file_ops import save_image_to_media

        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = str(tmp_path / "media_result.png")
            result = view_image_tool.call(json.dumps({'path': test_image_200x150}))

        assert isinstance(result, list)
        assert len(result) == 2
        # Image item carries the caption metadata.
        assert result[0].image == str(tmp_path / "media_result.png")
        assert result[0].caption is not None
        assert "Viewing image" in result[0].caption
        assert "200x150" in result[0].caption
        # The separate descriptive text item is preserved (not deleted).
        assert isinstance(result[1], ContentItem)
        assert result[1].text == result[0].caption

    def test_media_path_guard_skips_recaptioning(self, view_image_tool, test_image_200x150, tmp_path):
        """Regression: after the fix, _has_uncaptioned_images returns False for a media-path
        view_image result — no second vision caption call on the return path."""
        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = str(tmp_path / "media_result.png")
            result = view_image_tool.call(json.dumps({'path': test_image_200x150}))

        assert _guard_flags(result) is False

    def test_caption_survives_dump_round_trip(self, view_image_tool, test_image_200x150, tmp_path):
        """The caption must survive model_dump (exclude_none) and a JSON round-trip so it is
        not silently stripped at persistence time — otherwise the guard would re-fire after
        a session restore."""
        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = str(tmp_path / "media_result.png")
            result = view_image_tool.call(json.dumps({'path': test_image_200x150}))

        fn_msg = Message(role=FUNCTION, name="view_image", content=list(result))
        dumped = json.loads(fn_msg.model_dump_json())
        # Re-parse the persisted form (dict items) and confirm the guard still skips it.
        restored = Message(**dumped)
        assert _guard_flags(restored.content) is False


# ---------------------------------------------------------------------------
# Base64 fallback branch (save_image_to_media raises MediaStorageError)
# ---------------------------------------------------------------------------

class TestViewImageBase64FallbackCaption:
    def test_base64_fallback_attaches_caption(self, view_image_tool, test_image_200x150):
        """When media storage fails, the base64 fallback path ALSO attaches the caption to
        the image item — otherwise the guard would re-caption on that branch too."""
        from agent_cascade.utils.media_utils import MediaStorageError

        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media',
                   side_effect=MediaStorageError("disk full")), \
             patch('agent_cascade.tools.custom.file_ops.encode_image_as_base64',
                   return_value="data:image/png;base64,AAAA") as mock_b64:
            result = view_image_tool.call(json.dumps({'path': test_image_200x150}))

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].image == "data:image/png;base64,AAAA"
        assert mock_b64.called
        # Caption attached on the fallback branch as well.
        assert result[0].caption is not None
        assert "Viewing image" in result[0].caption
        assert result[1].text == result[0].caption

    def test_base64_fallback_guard_skips_recaptioning(self, view_image_tool, test_image_200x150):
        """Regression: the base64 fallback result also passes the guard (no re-caption)."""
        from agent_cascade.utils.media_utils import MediaStorageError

        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media',
                   side_effect=MediaStorageError("disk full")), \
             patch('agent_cascade.tools.custom.file_ops.encode_image_as_base64',
                   return_value="data:image/png;base64,AAAA"):
            result = view_image_tool.call(json.dumps({'path': test_image_200x150}))

        assert _guard_flags(result) is False


# ---------------------------------------------------------------------------
# Crop region branch (caption includes crop info; still captioned)
# ---------------------------------------------------------------------------

class TestViewImageCropRegionCaption:
    def test_crop_region_result_still_captioned(self, view_image_tool, test_image_200x150, tmp_path):
        """A cropped view carries the crop region in its caption and still passes the guard."""
        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = str(tmp_path / "crop_result.png")
            result = view_image_tool.call(json.dumps({
                'path': test_image_200x150,
                'crop_region': "10,20,100,80",
            }))

        assert isinstance(result, list) and len(result) == 2
        caption = result[0].caption
        assert "200x150" in caption
        assert "cropped region x=10,y=20,w=100,h=80" in caption
        assert _guard_flags(result) is False
