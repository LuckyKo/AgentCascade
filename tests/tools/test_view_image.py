"""Unit tests for the view_image tool (agent_cascade/tools/custom/file_ops.py::ViewImage).

Focus: the image item is deliberately left UNCAPTIONED so the router's return-path guard
(``_has_uncaptioned_images``) triggers a GENUINE vision/LLM caption via ``caption_images()``.

view_image returns ``[ContentItem(image=...), ContentItem(text=caption)]`` where:
  * the image item carries NO ``caption`` (``caption=None``) — this is intentional so the
    guard fires a real vision caption rather than reusing a pre-filled descriptive line;
  * the separate text item carries the descriptive line ("Viewing image: <path> (WxH) ...")
    for text-only agents. It does NOT count as an image caption, so it cannot suppress
    real captioning.

This is the POST-REVERT behavior. An earlier fix (5089a51) pre-filled the image item's
``caption`` with the descriptive line to make the guard skip a second vision call; that was
reverted in 9440a15 because it suppressed genuine captioning. These tests pin the current
intended contract so a regression back to pre-filling (or to dropping the text item) fails.

File placement: existing view_image coverage lives in ``tests/test_media_storage.py``
(TestViewImageCropRegion), which sits at the tests/ root rather than tests/tools/. This new
file groups the captioning behavior with the other tool-level tests under tests/tools/,
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

class TestViewImageMediaPathUncaptioned:
    def test_media_path_leaves_image_uncaptioned(self, view_image_tool, test_image_200x150, tmp_path):
        """The image item is left UNCAPTIONED (caption=None) by design so the return-path
        guard triggers a genuine vision caption. The descriptive line lives in the separate
        text item for text-only agents."""
        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = str(tmp_path / "media_result.png")
            result = view_image_tool.call(json.dumps({'path': test_image_200x150}))

        assert isinstance(result, list)
        assert len(result) == 2
        # Image item points at the saved media path.
        assert result[0].image == str(tmp_path / "media_result.png")
        # Image item is deliberately NOT pre-filled with a caption (post-revert behavior).
        assert result[0].caption is None
        # The separate descriptive text item carries the "Viewing image ... (WxH)" line.
        assert isinstance(result[1], ContentItem)
        assert result[1].text is not None
        assert "Viewing image" in result[1].text
        assert "200x150" in result[1].text

    def test_media_path_guard_fires_for_genuine_caption(self, view_image_tool, test_image_200x150, tmp_path):
        """The return-path guard MUST report the image as uncaptioned so the router generates
        a genuine vision/LLM caption. A pre-filled caption (the reverted behavior) would make
        this False and suppress real captioning — that is the regression we must not reintroduce."""
        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = str(tmp_path / "media_result.png")
            result = view_image_tool.call(json.dumps({'path': test_image_200x150}))

        assert _guard_flags(result) is True

    def test_uncaptioned_state_survives_dump_round_trip(self, view_image_tool, test_image_200x150, tmp_path):
        """The uncaptioned image item must survive model_dump (exclude_none) and a JSON
        round-trip so the guard STILL fires after a session restore — i.e. caption=None is not
        accidentally dropped into a pre-filled state by serialization."""
        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = str(tmp_path / "media_result.png")
            result = view_image_tool.call(json.dumps({'path': test_image_200x150}))

        fn_msg = Message(role=FUNCTION, name="view_image", content=list(result))
        dumped = json.loads(fn_msg.model_dump_json())
        # The image item must NOT carry a caption key after serialization (exclude_none
        # drops the None), so it is unambiguously uncaptioned on restore.
        assert not dumped["content"][0].get("caption"), (
            "image item unexpectedly carried a caption through serialization; "
            "this would suppress genuine vision captioning on session restore"
        )
        # Re-parse the persisted form (dict items) and confirm the guard still fires.
        restored = Message(**dumped)
        assert _guard_flags(restored.content) is True


# ---------------------------------------------------------------------------
# Base64 fallback branch (save_image_to_media raises MediaStorageError)
# ---------------------------------------------------------------------------

class TestViewImageBase64FallbackUncaptioned:
    def test_base64_fallback_leaves_image_uncaptioned(self, view_image_tool, test_image_200x150):
        """When media storage fails, the base64 fallback path ALSO leaves the image item
        uncaptioned (caption=None) — so the guard fires a genuine vision caption on that
        branch too. The descriptive line stays in the separate text item."""
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
        # Image item left uncaptioned on the fallback branch as well (post-revert behavior).
        assert result[0].caption is None
        # Descriptive line carried by the separate text item.
        assert isinstance(result[1], ContentItem)
        assert "Viewing image" in result[1].text

    def test_base64_fallback_guard_fires_for_genuine_caption(self, view_image_tool, test_image_200x150):
        """The base64 fallback result must ALSO be reported as uncaptioned by the guard so a
        genuine vision caption is generated (not suppressed by a pre-filled caption)."""
        from agent_cascade.utils.media_utils import MediaStorageError

        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media',
                    side_effect=MediaStorageError("disk full")), \
              patch('agent_cascade.tools.custom.file_ops.encode_image_as_base64',
                    return_value="data:image/png;base64,AAAA"):
            result = view_image_tool.call(json.dumps({'path': test_image_200x150}))

        assert _guard_flags(result) is True


# ---------------------------------------------------------------------------
# Crop region branch (caption includes crop info; still captioned)
# ---------------------------------------------------------------------------

class TestViewImageCropRegionUncaptioned:
    def test_crop_region_result_still_uncaptioned(self, view_image_tool, test_image_200x150, tmp_path):
        """A cropped view carries the crop region in its descriptive text item AND is left
        uncaptioned (caption=None) so the guard still fires a genuine vision caption."""
        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = str(tmp_path / "crop_result.png")
            result = view_image_tool.call(json.dumps({
                'path': test_image_200x150,
                'crop_region': "10,20,100,80",
            }))

        assert isinstance(result, list) and len(result) == 2
        # Image item left uncaptioned (post-revert behavior).
        assert result[0].caption is None
        # Descriptive text item carries the size AND the crop region.
        text = result[1].text
        assert "200x150" in text
        assert "cropped region x=10,y=20,w=100,h=80" in text
        # Guard still fires for genuine captioning.
        assert _guard_flags(result) is True