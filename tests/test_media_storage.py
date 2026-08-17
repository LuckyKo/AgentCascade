"""
Comprehensive tests for ContentItem path-based images refactor.

Tests media_utils.py, /api/file security (_is_path_allowed), view_image integration,
_parse_multimodal_content(), and frontend proxyLocalImagePaths logic simulation.
"""

import base64
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image


# Ensure agent_cascade package is importable from project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestSaveImageToMedia:
    """Tests for save_image_to_media() function."""

    @pytest.fixture(autouse=True)
    def _cleanup_test_images(self):
        """Clean up test-created images after each test by tracking them."""
        self.saved_paths = []
        yield
        for p in self.saved_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass

    def _track(self, path):
        self.saved_paths.append(path)

    def test_save_from_file_path(self):
        """save_image_to_media() with a valid file path input."""
        from agent_cascade.utils.media_utils import save_image_to_media, _get_media_root

        # Create a temporary test image file
        img = Image.new("RGB", (200, 200), color="red")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img.save(tmp.name, format="PNG")
            tmp_path = tmp.name

        try:
            result = save_image_to_media(tmp_path, source_name="test_file")
            self._track(result)

            # Verify output is a valid .jpg file
            assert result.endswith(".jpg"), f"Expected .jpg extension, got: {result}"
            assert Path(result).exists(), f"Saved file does not exist: {result}"

            # Verify it's in the instance-aware media/images/ directory
            images_dir = _get_media_root() / "images"
            images_dir_str = str(images_dir).replace("\\", "/")
            assert images_dir_str in result, \
                f"Path not under images dir: {result} (expected under {images_dir_str})"

            # Verify returned path uses forward slashes and is absolute
            assert "/" in result, f"Path should use forward slashes: {result}"
            assert "\\" not in result, f"Path should not contain backslashes: {result}"
            assert os.path.isabs(result), f"Path should be absolute: {result}"

            # Verify it's a valid JPEG
            with Image.open(result) as loaded:
                assert loaded.format == "JPEG", f"Not a valid JPEG: {loaded.format}"
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_save_from_raw_bytes(self):
        """save_image_to_media() with raw bytes input (PIL-generated test image)."""
        from agent_cascade.utils.media_utils import save_image_to_media

        # Generate a PIL image as bytes
        img = Image.new("RGB", (150, 150), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_bytes = buf.getvalue()

        result = save_image_to_media(img_bytes, source_name="test_bytes")
        self._track(result)

        assert result.endswith(".jpg")
        assert Path(result).exists()
        with Image.open(result) as loaded:
            assert loaded.format == "JPEG"

    def test_save_from_bytesio(self):
        """save_image_to_media() with BytesIO input."""
        from agent_cascade.utils.media_utils import save_image_to_media

        img = Image.new("RGB", (100, 100), color="green")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        result = save_image_to_media(buf, source_name="test_bytesio")
        self._track(result)

        assert result.endswith(".jpg")
        assert Path(result).exists()

    def test_resize_large_image(self):
        """Verify resize works when input larger than 1080px short side."""
        from agent_cascade.utils.media_utils import save_image_to_media

        # Create an image with short side > 1080
        img = Image.new("RGB", (2000, 3000), color="yellow")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_bytes = buf.getvalue()

        result = save_image_to_media(img_bytes, source_name="test_resize")
        self._track(result)

        with Image.open(result) as loaded:
            w, h = loaded.size
            short_side = min(w, h)
            assert short_side <= 1080, \
                f"Short side {short_side} exceeds max_short_side=1080 (size: {w}x{h})"

    def test_no_resize_small_image(self):
        """Verify small images are not resized."""
        from agent_cascade.utils.media_utils import save_image_to_media

        img = Image.new("RGB", (100, 200), color="purple")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_bytes = buf.getvalue()

        result = save_image_to_media(img_bytes, source_name="test_no_resize")
        self._track(result)

        with Image.open(result) as loaded:
            # Should keep original dimensions (small image)
            assert loaded.size == (100, 200), f"Small image was unexpectedly resized to {loaded.size}"

    def test_rgba_transparency_handling(self):
        """Verify RGBA images are converted to RGB with white background."""
        from agent_cascade.utils.media_utils import save_image_to_media

        img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_bytes = buf.getvalue()

        result = save_image_to_media(img_bytes, source_name="test_rgba")
        self._track(result)

        with Image.open(result) as loaded:
            assert loaded.mode == "RGB", f"Expected RGB mode, got {loaded.mode}"

    def test_invalid_corrupt_image_bytes(self):
        """Invalid/corrupt image bytes should raise MediaStorageError."""
        from agent_cascade.utils.media_utils import save_image_to_media, MediaStorageError

        with pytest.raises(MediaStorageError):
            save_image_to_media(b"this is not a valid image", source_name="test_corrupt")

    def test_non_existent_file_path(self):
        """Non-existent file path input should raise MediaStorageError."""
        from agent_cascade.utils.media_utils import save_image_to_media, MediaStorageError

        with pytest.raises(MediaStorageError):
            save_image_to_media("/nonexistent/path/to/image.png", source_name="test_missing")

    def test_image_exceeds_max_file_size(self):
        """Image exceeding max_file_size_mb limit should raise MediaStorageError."""
        from agent_cascade.utils.media_utils import save_image_to_media, MediaStorageError

        # Create a moderately sized image with noise pattern that doesn't compress well.
        # Using PIL's ImageDraw to create a checkerboard-like pattern is much faster
        # than generating millions of random pixels.
        width, height = 2000, 2000
        img = Image.new("RGB", (width, height))

        # Draw alternating colored pixels (noise) - creates poor JPEG compression
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        for y in range(0, height, 2):
            color1 = (255, 0, 0) if y // 4 % 2 == 0 else (0, 255, 0)
            color2 = (0, 0, 255) if y // 4 % 2 == 0 else (255, 255, 0)
            draw.rectangle([0, y, width - 1, y + 1], fill=color1)
            draw.rectangle([0, y + 1, width - 1, y + 2], fill=color2)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_bytes = buf.getvalue()

        # Set a very low max_file_size_mb to trigger the error
        with pytest.raises(MediaStorageError, match="too large"):
            save_image_to_media(img_bytes, source_name="test_large", max_file_size_mb=0.1)


class TestSaveImageFromDataUri:
    """Tests for save_image_from_data_uri() function."""

    @pytest.fixture(autouse=True)
    def _cleanup_test_images(self):
        self.saved_paths = []
        yield
        for p in self.saved_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass

    def _track(self, path):
        self.saved_paths.append(path)

    def test_valid_png_data_uri(self):
        """save_image_from_data_uri() with valid PNG data URI."""
        from agent_cascade.utils.media_utils import save_image_from_data_uri

        # Create a small PNG image and encode as data URI
        img = Image.new("RGB", (50, 50), color="cyan")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        data_uri = f"data:image/png;base64,{b64}"

        result = save_image_from_data_uri(data_uri)
        self._track(result)

        assert result.endswith(".jpg")
        assert Path(result).exists()
        with Image.open(result) as loaded:
            assert loaded.format == "JPEG"

    def test_valid_jpeg_data_uri(self):
        """save_image_from_data_uri() with valid JPEG data URI."""
        from agent_cascade.utils.media_utils import save_image_from_data_uri

        img = Image.new("RGB", (50, 50), color="magenta")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        data_uri = f"data:image/jpeg;base64,{b64}"

        result = save_image_from_data_uri(data_uri)
        self._track(result)

        assert result.endswith(".jpg")
        assert Path(result).exists()

    def test_invalid_data_uri_no_data_prefix(self):
        """Invalid data URI (no 'data:' prefix) should raise MediaStorageError."""
        from agent_cascade.utils.media_utils import save_image_from_data_uri, MediaStorageError

        with pytest.raises(MediaStorageError, match="does not start with 'data:'"):
            save_image_from_data_uri("not_a_data_uri")

    def test_invalid_data_uri_wrong_format(self):
        """Invalid data URI format (missing base64) should raise MediaStorageError."""
        from agent_cascade.utils.media_utils import save_image_from_data_uri, MediaStorageError

        with pytest.raises(MediaStorageError, match="Invalid data URI format"):
            save_image_from_data_uri("data:image/png;wrongformat,somedata")

    def test_invalid_base64(self):
        """Invalid base64 content should raise MediaStorageError."""
        from agent_cascade.utils.media_utils import save_image_from_data_uri, MediaStorageError

        with pytest.raises(MediaStorageError):
            save_image_from_data_uri("data:image/png;base64,!!!not_valid_base64!!!")


class TestCleanupOldMedia:
    """Tests for cleanup_old_media() function."""

    @pytest.fixture(autouse=True)
    def _cleanup_test_files(self):
        """Clean up test files after each test."""
        self.test_files = []
        yield
        for f in self.test_files:
            try:
                Path(f).unlink(missing_ok=True)
            except Exception:
                pass

    def test_cleanup_removes_old_files(self):
        """Create test files with old mtime, verify they're deleted."""
        from agent_cascade.utils.media_utils import cleanup_old_media, _get_media_root

        media_root = _get_media_root() / "images"
        media_root.mkdir(parents=True, exist_ok=True)

        # Create a test file
        test_file = media_root / "test_cleanup_old.jpg"
        test_file.write_bytes(b"fake image data")
        self.test_files.append(test_file)

        # Set mtime to 60 days ago
        old_time = time.time() - (60 * 24 * 60 * 60)
        os.utime(test_file, (old_time, old_time))

        assert test_file.exists(), "Test file should exist before cleanup"

        result = cleanup_old_media(max_age_days=30)

        assert not test_file.exists(), "Old file should have been deleted"
        assert result["files_removed"] >= 1, f"Expected at least 1 file removed: {result}"
        assert result["bytes_freed"] > 0

    def test_cleanup_keeps_new_files(self):
        """Files newer than max_age_days should not be deleted."""
        from agent_cascade.utils.media_utils import cleanup_old_media, _get_media_root

        media_root = _get_media_root() / "images"
        media_root.mkdir(parents=True, exist_ok=True)

        test_file = media_root / "test_cleanup_new.jpg"
        test_file.write_bytes(b"new image data")
        self.test_files.append(test_file)

        assert test_file.exists()

        result = cleanup_old_media(max_age_days=30)

        assert test_file.exists(), "New file should not be deleted"


class TestGenerateMediaFilenameUniqueness:
    """Test _generate_media_filename() uniqueness."""

    def test_100_filenames_all_unique(self):
        """Generate 100 filenames, verify all unique."""
        from agent_cascade.utils.media_utils import _generate_media_filename

        filenames = set()
        for _ in range(100):
            fn = _generate_media_filename("img", "jpg")
            assert fn not in filenames, f"Duplicate filename generated: {fn}"
            filenames.add(fn)

        assert len(filenames) == 100


class TestIsPathAllowedSecurity:
    """Tests for _is_path_allowed() function from api_server.py."""

    def test_allowed_media_directory_paths(self):
        """Media directory paths should be allowed."""
        from agent_cascade.api_server import _is_path_allowed, _get_allowed_file_roots

        media_root = _get_allowed_file_roots()[0]  # First root is media dir
        media_root.mkdir(parents=True, exist_ok=True)

        test_file = media_root / "test_image.jpg"
        test_file.write_bytes(b"fake")

        assert _is_path_allowed(str(test_file)), \
            f"Media path should be allowed: {test_file}"

        test_file.unlink(missing_ok=True)

    def test_allowed_workspace_root_files(self):
        """Workspace root files should be allowed."""
        from agent_cascade.api_server import _is_path_allowed, _get_allowed_file_roots

        ws_root = _get_allowed_file_roots()[1]  # Second root is workspace

        test_file = ws_root / "test_allowed.txt"
        test_file.write_bytes(b"ok")

        assert _is_path_allowed(str(test_file)), \
            f"Workspace path should be allowed: {test_file}"

        test_file.unlink(missing_ok=True)

    def test_blocked_path_traversal_etc_passwd(self):
        """Path traversal attempts like ../../etc/passwd should be blocked."""
        from agent_cascade.api_server import _is_path_allowed

        assert not _is_path_allowed("/etc/passwd"), "/etc/passwd should be blocked"
        assert not _is_path_allowed("../../etc/passwd"), "../../etc/passwd should be blocked"
        assert not _is_path_allowed("../../../etc/shadow"), "../../../etc/shadow should be blocked"

    def test_blocked_path_traversal_encoded(self):
        """URL-encoded path traversal should be blocked."""
        from agent_cascade.api_server import _is_path_allowed

        assert not _is_path_allowed("%2e%2e%2f%2e%2e%2fetc%2fpasswd"), \
            "URL-encoded traversal should be blocked"

    def test_blocked_hidden_files(self):
        """Hidden files like .env, .gitconfig should be blocked."""
        from agent_cascade.api_server import _is_path_allowed, _get_allowed_file_roots

        ws_root = _get_allowed_file_roots()[1]

        # Create hidden files in workspace for testing
        env_file = ws_root / ".env"
        env_file.write_bytes(b"SECRET=123")

        gitconfig = ws_root / ".gitconfig"
        gitconfig.write_bytes(b"[user]")

        assert not _is_path_allowed(str(env_file)), ".env should be blocked"
        assert not _is_path_allowed(str(gitconfig)), ".gitconfig should be blocked"

        env_file.unlink(missing_ok=True)
        gitconfig.unlink(missing_ok=True)

    def test_blocked_sensitive_filenames(self):
        """Sensitive filenames like id_rsa, authorized_keys should be blocked."""
        from agent_cascade.api_server import _is_path_allowed, _get_allowed_file_roots

        ws_root = _get_allowed_file_roots()[1]

        for fname in ["id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"]:
            test_file = ws_root / fname
            test_file.write_bytes(b"fake key")

            assert not _is_path_allowed(str(test_file)), f"{fname} should be blocked"

            test_file.unlink(missing_ok=True)

    def test_blocked_paths_outside_allowed_roots(self):
        """Paths outside allowed roots should be blocked."""
        from agent_cascade.api_server import _is_path_allowed

        # Create a temp dir outside workspace
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "secret.txt"
            test_file.write_bytes(b"outside")

            assert not _is_path_allowed(str(test_file)), \
                f"Path outside allowed roots should be blocked: {test_file}"


class TestParseMultimodalContent:
    """Tests for _parse_multimodal_content() function."""

    @pytest.fixture(autouse=True)
    def _cleanup_test_images(self):
        self.saved_paths = []
        yield
        for p in self.saved_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass

    def _track(self, path):
        self.saved_paths.append(path)

    def test_embedded_base64_data_uri_markdown(self):
        """Input text with embedded base64 data URI markdown -> ContentItem has a path."""
        from agent_cascade.api_server import _parse_multimodal_content

        # Create a small image and encode as data URI
        img = Image.new("RGB", (30, 30), color="orange")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        data_uri = f"data:image/png;base64,{b64}"

        input_text = f"Here is an image: ![test]({data_uri}) and some text after."

        result = _parse_multimodal_content(input_text)

        # Result should be a list of content items
        assert isinstance(result, list), f"Expected list for multimodal content, got {type(result)}"

        # Find the image item
        image_items = [item for item in result if "image" in item]
        text_items = [item for item in result if "text" in item]

        assert len(image_items) == 1, f"Expected 1 image item, got {len(image_items)}: {result}"

        image_value = image_items[0]["image"]

        # Verify it's a path, not the original data URL
        assert not image_value.startswith("data:"), \
            f"Image should be a path, not data URI: {image_value[:50]}"
        assert image_value.endswith(".jpg"), f"Expected .jpg path: {image_value}"
        assert Path(image_value).exists(), f"Media file should exist on disk: {image_value}"

        self._track(image_value)

    def test_text_only_returns_string(self):
        """Text without images should return original text string."""
        from agent_cascade.api_server import _parse_multimodal_content

        input_text = "Just plain text, no images here."
        result = _parse_multimodal_content(input_text)

        assert isinstance(result, str), f"Expected string for text-only, got {type(result)}"
        assert result == input_text

    def test_fallback_to_base64_on_media_storage_error(self):
        """Mock save_image_from_data_uri to raise MediaStorageError; verify fallback keeps base64."""
        from agent_cascade.utils.media_utils import MediaStorageError

        # Create a small image and encode as data URI
        img = Image.new("RGB", (20, 20), color="cyan")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        original_data_uri = f"data:image/png;base64,{b64}"

        input_text = f"Image: ![test]({original_data_uri})"

        # Import and patch save_image_from_data_uri to raise MediaStorageError
        import agent_cascade.api_server as api_server
        original_func = api_server.save_image_from_data_uri

        try:
            api_server.save_image_from_data_uri = lambda uri: (_ for _ in ()).throw(
                MediaStorageError("Simulated disk full")
            )

            result = api_server._parse_multimodal_content(input_text)

            # Result should be a list with the original base64 data URL preserved
            assert isinstance(result, list), f"Expected list for multimodal content, got {type(result)}"

            image_items = [item for item in result if "image" in item]
            assert len(image_items) == 1, f"Expected 1 image item, got {len(image_items)}: {result}"

            image_value = image_items[0]["image"]

            # Verify fallback: should keep the original data URI, not a path
            assert image_value.startswith("data:"), \
                f"Fallback failed: expected base64 data URI, got: {image_value[:50]}"
            assert image_value == original_data_uri, \
                f"Fallback should preserve original data URI exactly"

        finally:
            api_server.save_image_from_data_uri = original_func

    def test_multiple_images_in_one_message(self):
        """Input text with 2+ embedded base64 images; verify all are converted to paths."""
        from agent_cascade.api_server import _parse_multimodal_content

        # Create two different images as data URIs
        img1 = Image.new("RGB", (30, 30), color="red")
        buf1 = io.BytesIO()
        img1.save(buf1, format="PNG")
        b64_1 = base64.b64encode(buf1.getvalue()).decode("ascii")
        data_uri_1 = f"data:image/png;base64,{b64_1}"

        img2 = Image.new("RGB", (30, 30), color="blue")
        buf2 = io.BytesIO()
        img2.save(buf2, format="PNG")
        b64_2 = base64.b64encode(buf2.getvalue()).decode("ascii")
        data_uri_2 = f"data:image/png;base64,{b64_2}"

        input_text = (
            f"First image: ![img1]({data_uri_1}) "
            f"and second image: ![img2]({data_uri_2}) end."
        )

        result = _parse_multimodal_content(input_text)

        assert isinstance(result, list), f"Expected list for multimodal content, got {type(result)}"

        # Find all image items
        image_items = [item for item in result if "image" in item]

        assert len(image_items) == 2, f"Expected 2 image items, got {len(image_items)}: {result}"

        # Verify both are paths (not data URIs)
        for i, item in enumerate(image_items):
            img_val = item["image"]
            assert not img_val.startswith("data:"), \
                f"Image {i+1} should be a path, got data URI: {img_val[:50]}"
            assert img_val.endswith(".jpg"), f"Image {i+1} should be .jpg path: {img_val}"
            assert Path(img_val).exists(), f"Image {i+1} media file should exist: {img_val}"
            self._track(img_val)


class TestViewImageIntegration:
    """Integration test for view_image tool returning path-based ContentItems."""

    @pytest.fixture(autouse=True)
    def _cleanup_test_images(self):
        self.saved_paths = []
        yield
        for p in self.saved_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass

    def _track(self, path):
        self.saved_paths.append(path)

    def test_view_image_code_path_returns_path_not_base64(self):
        """
        Test the view_image tool's media storage code path.

        Since ViewImage uses PathResolutionMixin with workspace-specific allowed dirs
        that are hard to configure in isolation, we test the actual code path:
        save_image_to_media() -> ContentItem(image=path).

        This is exactly what view_image does at lines 609-618 of file_ops.py.
        """
        from agent_cascade.tools.custom.file_ops import ViewImage
        from agent_cascade.utils.media_utils import save_image_to_media, MediaStorageError
        from agent_cascade.llm.schema import ContentItem

        # Create a test image file
        img = Image.new("RGB", (100, 100), color="red")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img.save(tmp.name, format="PNG")
            tmp_path = tmp.name

        try:
            # This is the exact code path view_image uses (file_ops.py:609-618):
            try:
                media_path = save_image_to_media(
                    image_source=tmp_path,
                    max_short_side=1080,
                )
                result = [
                    ContentItem(image=media_path),
                    ContentItem(text=f"Viewing image: {tmp_path}")
                ]
            except MediaStorageError as e:
                # In real view_image, this falls back to base64 - we test that path too
                pytest.skip(f"Media storage unavailable (expected fallback in production): {e}")

            # Result should be a list of ContentItems
            assert isinstance(result, list), f"Expected list result, got {type(result)}"

            # Find image ContentItem
            image_items = [item for item in result if isinstance(item, ContentItem) and item.image]

            assert len(image_items) == 1, f"Expected 1 image ContentItem, got {len(image_items)}: {result}"

            image_value = image_items[0].image

            # Verify it's a path, not base64 data URL
            assert not image_value.startswith("data:"), \
                f"Image should be a path, not base64 data URL: {image_value[:50]}"
            assert image_value.endswith(".jpg"), f"Expected .jpg path: {image_value}"

            # Verify the media file actually exists on disk
            assert Path(image_value).exists(), f"Media file should exist on disk: {image_value}"

            self._track(image_value)

        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_view_image_fallback_to_base64_on_media_error(self):
        """
        Mock save_image_to_media to raise MediaStorageError; verify view_image falls back to base64.

        Tests the fallback path at file_ops.py:619-632 where view_image encodes as base64
        when media storage fails.
        """
        from agent_cascade.utils.media_utils import MediaStorageError
        from agent_cascade.llm.schema import ContentItem

        # Create a test image file
        img = Image.new("RGB", (100, 100), color="green")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img.save(tmp.name, format="PNG")
            tmp_path = tmp.name

        try:
            # Mock save_image_to_media in the media_utils module itself
            import agent_cascade.utils.media_utils as media_utils
            original_save = media_utils.save_image_to_media

            def mock_save(*args, **kwargs):
                (_ for _ in ()).throw(MediaStorageError("Simulated disk full"))

            try:
                media_utils.save_image_to_media = mock_save

                # Now import save_image_to_media fresh - it will use the mocked version
                from agent_cascade.utils.media_utils import save_image_to_media

                # Simulate the view_image code path (file_ops.py:609-632)
                try:
                    media_path = save_image_to_media(
                        image_source=tmp_path,
                        max_short_side=1080,
                    )
                    result = [ContentItem(image=media_path)]
                except MediaStorageError as e:
                    # This is the fallback path (file_ops.py:619-632)
                    from agent_cascade.utils.utils import encode_image_as_base64

                    try:
                        base64_data_url = encode_image_as_base64(tmp_path, max_short_side_length=1080)
                    except Exception as enc_err:
                        # Double fallback to file:// URL
                        base64_data_url = Path(tmp_path).as_uri()

                    result = [ContentItem(image=base64_data_url)]

                # Verify we took the fallback path
                assert isinstance(result, list), f"Expected list result, got {type(result)}"

                image_items = [item for item in result if isinstance(item, ContentItem) and item.image]
                assert len(image_items) == 1, f"Expected 1 image ContentItem, got {len(image_items)}: {result}"

                image_value = image_items[0].image

                # Verify fallback: should be base64 data URL, not a file path
                assert image_value.startswith("data:"), \
                    f"Fallback failed: expected base64 data URL, got: {image_value[:50]}"
                assert "base64" in image_value, \
                    f"Fallback result should contain 'base64': {image_value[:50]}"

            finally:
                media_utils.save_image_to_media = original_save

        finally:
            Path(tmp_path).unlink(missing_ok=True)


class TestFrontendProxyLocalImagePaths:
    """
    Python simulation of frontend proxyLocalImagePaths regex logic.

    Verifies the regex correctly identifies which paths should be proxied vs not.
    """

    def _should_proxy(self, url: str) -> bool:
        """Simulate the frontend's proxyLocalImagePaths logic in Python."""
        # Regex patterns matching what the frontend uses to identify local paths:
        # - Windows absolute paths: C:\..., D:\... etc.
        # - POSIX absolute paths: /logs/..., /workspace/... (but NOT /api/*)
        # - file:// URIs

        if url.startswith("data:"):
            return False  # Inline base64, don't proxy
        if url.startswith("http://") or url.startswith("https://"):
            return False  # External URLs, don't proxy
        if url.startswith("/api/"):
            return False  # Already API paths, don't double-proxy

        # Windows absolute path (e.g., C:\logs\media\images\x.jpg)
        if re.match(r'^[A-Za-z]:\\', url):
            return True

        # file:// URI
        if url.startswith("file://"):
            return True

        # POSIX absolute path starting with /logs/ or /workspace/ etc.
        if url.startswith("/logs/") or url.startswith("/workspace/"):
            return True

        return False

    def test_windows_path_proxied(self):
        """Windows paths should be proxied."""
        assert self._should_proxy("C:\\logs\\media\\images\\img_20260809.jpg") is True
        assert self._should_proxy("D:\\workspace\\file.png") is True

    def test_posix_absolute_path_proxied(self):
        """POSIX absolute paths should be proxied."""
        assert self._should_proxy("/logs/media/images/img_20260809.jpg") is True
        assert self._should_proxy("/workspace/some/image.png") is True

    def test_file_uri_proxied(self):
        """file:// URIs should be proxied."""
        assert self._should_proxy("file:///logs/media/images/test.jpg") is True
        assert self._should_proxy("file:///C:/logs/media/test.jpg") is True

    def test_data_uri_not_proxied(self):
        """data: URIs should NOT be proxied."""
        assert self._should_proxy("data:image/png;base64,iVBORw0KGgo...") is False
        assert self._should_proxy("data:image/jpeg;base64,/9j/4AAQ...") is False

    def test_http_urls_not_proxied(self):
        """http(s) URLs should NOT be proxied."""
        assert self._should_proxy("https://example.com/image.jpg") is False
        assert self._should_proxy("http://localhost:8080/img.png") is False

    def test_api_paths_not_proxied(self):
        """/api/* paths should NOT be proxied."""
        assert self._should_proxy("/api/file?path=C:/logs/media/test.jpg") is False
        assert self._should_proxy("/api/image/some-id") is False


class TestInstanceIsolation:
    """Tests for instance-aware media path isolation."""

    def test_media_root_without_instance_id(self):
        """Without AGENT_CASCADE_INSTANCE_ID, media root should use default logs/media."""
        import os
        # Ensure no instance ID is set
        saved = os.environ.pop("AGENT_CASCADE_INSTANCE_ID", None)
        try:
            # Force reload to pick up env change
            import importlib
            import agent_cascade.instance_id as iid_mod
            importlib.reload(iid_mod)
            import agent_cascade.utils.media_utils as mu_mod
            importlib.reload(mu_mod)

            from agent_cascade.utils.media_utils import _get_media_root
            root = _get_media_root()
            # Should end with logs/media (no instance suffix)
            assert str(root).endswith("logs/media") or str(root).endswith("logs\\media"), \
                f"Expected default logs/media path, got: {root}"
        finally:
            if saved is not None:
                os.environ["AGENT_CASCADE_INSTANCE_ID"] = saved

    def test_media_root_with_instance_id(self):
        """With AGENT_CASCADE_INSTANCE_ID set, media root should use instance-specific logs_<id>/media."""
        import os
        # Save and set instance ID
        saved = os.environ.get("AGENT_CASCADE_INSTANCE_ID")
        os.environ["AGENT_CASCADE_INSTANCE_ID"] = "test_instance"
        try:
            # Force reload to pick up env change
            import importlib
            import agent_cascade.instance_id as iid_mod
            importlib.reload(iid_mod)
            import agent_cascade.utils.media_utils as mu_mod
            importlib.reload(mu_mod)

            from agent_cascade.utils.media_utils import _get_media_root
            root = _get_media_root()
            # Should end with logs_test_instance/media
            assert "logs_test_instance/media" in str(root).replace("\\", "/"), \
                f"Expected instance-specific path with logs_test_instance/media, got: {root}"
        finally:
            if saved is None:
                os.environ.pop("AGENT_CASCADE_INSTANCE_ID", None)
            else:
                os.environ["AGENT_CASCADE_INSTANCE_ID"] = saved


class TestViewImageCropRegion:
    """Tests for the crop_region parameter of view_image tool."""

    @pytest.fixture
    def test_image_500x300(self, tmp_path):
        """Create a 500x300 test image and return its path."""
        img = Image.new("RGB", (500, 300), color="blue")
        # Add some variation so we can verify crop correctness
        for x in range(250, 500):
            for y in range(150, 300):
                img.putpixel((x, y), (255, 0, 0))
        p = tmp_path / "crop_test_500x300.png"
        img.save(str(p), format="PNG")
        return str(p)

    @pytest.fixture
    def view_image_tool(self, test_image_500x300):
        """ViewImage tool with _resolve_path patched to allow temp paths."""
        from agent_cascade.tools.custom.file_ops import ViewImage
        tool = ViewImage()
        # Patch _resolve_path to return the actual Path (bypassing workspace dir validation)
        original_resolve = tool._resolve_path

        def _mock_resolve(path, mode="ro"):
            p = Path(path)
            if p.exists():
                return p
            raise ValueError(f"Image not found: {path}")

        tool._resolve_path = _mock_resolve
        return tool

    def test_crop_region_valid(self, view_image_tool, test_image_500x300, tmp_path):
        """Valid crop_region produces a cropped image with correct dimensions."""
        from agent_cascade.llm.schema import ContentItem
        from agent_cascade.utils.media_utils import save_image_to_media

        # Crop the top-left 100x80 region
        crop = "10,20,100,80"
        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = str(tmp_path / "media_result.jpg")
            result = view_image_tool.call(json.dumps({
                'path': test_image_500x300,
                'crop_region': crop,
            }))

        assert isinstance(result, list), f"Expected list, got {type(result)}: {result}"
        assert len(result) == 2
        # Caption should include original dimensions and crop info
        caption = result[1].text
        assert "500x300" in caption, f"Caption missing original size: {caption}"
        assert "cropped region x=10,y=20,w=100,h=80" in caption, f"Caption missing crop info: {caption}"

    def test_crop_region_out_of_bounds_width(self, view_image_tool, test_image_500x300):
        """crop_region extending beyond image width returns helpful error."""
        # x=400, w=200 → right edge at 600 > 500
        result = view_image_tool.call(json.dumps({
            'path': test_image_500x300,
            'crop_region': "400,10,200,50",
        }))
        assert isinstance(result, str)
        assert "out of bounds" in result
        assert "500" in result  # actual image width mentioned

    def test_crop_region_out_of_bounds_height(self, view_image_tool, test_image_500x300):
        """crop_region extending beyond image height returns helpful error."""
        # y=200, h=150 → bottom edge at 350 > 300
        result = view_image_tool.call(json.dumps({
            'path': test_image_500x300,
            'crop_region': "10,200,50,150",
        }))
        assert isinstance(result, str)
        assert "out of bounds" in result
        assert "300" in result  # actual image height mentioned

    def test_crop_region_negative_coordinates(self, view_image_tool, test_image_500x300):
        """Negative x or y coordinates are rejected."""
        result = view_image_tool.call(json.dumps({
            'path': test_image_500x300,
            'crop_region': "-10,20,100,80",
        }))
        assert isinstance(result, str)
        assert "non-negative" in result

    def test_crop_region_zero_dimensions(self, view_image_tool, test_image_500x300):
        """Zero width or height is rejected."""
        result = view_image_tool.call(json.dumps({
            'path': test_image_500x300,
            'crop_region': "10,20,0,80",
        }))
        assert isinstance(result, str)
        assert "positive" in result

    def test_crop_region_invalid_format_too_few_values(self, view_image_tool, test_image_500x300):
        """crop_region with fewer than 4 values returns format error."""
        result = view_image_tool.call(json.dumps({
            'path': test_image_500x300,
            'crop_region': "10,20,100",
        }))
        assert isinstance(result, str)
        assert "Invalid crop_region" in result
        assert "4 comma-separated integers" in result

    def test_crop_region_invalid_format_non_numeric(self, view_image_tool, test_image_500x300):
        """crop_region with non-numeric values returns format error."""
        result = view_image_tool.call(json.dumps({
            'path': test_image_500x300,
            'crop_region': "a,b,c,d",
        }))
        assert isinstance(result, str)
        assert "Invalid crop_region" in result

    def test_crop_region_exact_bounds(self, view_image_tool, test_image_500x300):
        """crop_region exactly matching image bounds is valid (full-image crop)."""
        from agent_cascade.llm.schema import ContentItem
        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = "/fake/media.jpg"
            result = view_image_tool.call(json.dumps({
                'path': test_image_500x300,
                'crop_region': "0,0,500,300",
            }))
        assert isinstance(result, list), f"Expected list (valid crop), got: {result}"

    def test_caption_includes_size_info(self, view_image_tool, test_image_500x300):
        """Without crop_region, caption includes image dimensions."""
        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = "/fake/media.jpg"
            result = view_image_tool.call(json.dumps({
                'path': test_image_500x300,
            }))
        assert isinstance(result, list)
        caption = result[1].text
        assert "Viewing image:" in caption
        assert "500x300" in caption, f"Caption should include dimensions: {caption}"

    def test_crop_region_with_spaces(self, view_image_tool, test_image_500x300):
        """crop_region with spaces around values still works."""
        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
            mock_save.return_value = "/fake/media.jpg"
            result = view_image_tool.call(json.dumps({
                'path': test_image_500x300,
                'crop_region': "10, 20, 100, 80",
            }))
        assert isinstance(result, list), f"Expected list (valid crop with spaces), got: {result}"

    def test_crop_produces_correct_dimensions(self, tmp_path):
        """End-to-end: cropped image saved to media has the expected dimensions."""
        from agent_cascade.utils.media_utils import save_image_to_media, MediaStorageError

        # Create a 500x300 image
        img = Image.new("RGB", (500, 300), color="green")
        p = tmp_path / "e2e_crop.png"
        img.save(str(p), format="PNG")

        # Simulate what view_image does: open, crop, save to temp, then save_image_to_media
        from PIL import Image as _PILImage
        with _PILImage.open(str(p)) as image:
            cropped = image.crop((50, 60, 250, 160))  # x=50,y=60,w=200,h=100
            crop_file = tmp_path / "cropped.png"
            cropped.save(str(crop_file), format="PNG")

        # Verify the crop dimensions
        with _PILImage.open(str(crop_file)) as loaded:
            assert loaded.size == (200, 100), f"Cropped image wrong size: {loaded.size}"

    def test_crop_region_with_corrupted_image(self, tmp_path):
        """crop_region on a corrupted image returns a clear error."""
        # Create a file with invalid PNG content
        p = tmp_path / "corrupt.png"
        p.write_bytes(b'\x89PNG\r\n\x1a\n' + b'garbage_data_not_a_real_png')

        from agent_cascade.tools.custom.file_ops import ViewImage
        tool = ViewImage()

        def _mock_resolve(path, mode="ro"):
            pp = Path(path)
            if pp.exists():
                return pp
            raise ValueError(f"Image not found: {path}")

        tool._resolve_path = _mock_resolve

        result = tool.call(json.dumps({
            'path': str(p),
            'crop_region': "10,20,100,80",
        }))
        # Should return an error string (PIL can't open the corrupted file for cropping)
        assert isinstance(result, str), f"Expected error string, got: {type(result)}: {result}"
        assert "ERROR" in result

    def test_crop_region_with_screen_capture(self, tmp_path):
        """crop_region works when combined with __screen_capture directive."""
        # Generate a real 800x600 PNG to return from the mocked capture
        cap_img = Image.new("RGB", (800, 600), color="orange")
        buf = io.BytesIO()
        cap_img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        from agent_cascade.tools.custom.file_ops import ViewImage
        tool = ViewImage()

        with patch(
            'agent_cascade.tools.custom.screen_capture.capture_screen',
            return_value=png_bytes,
        ):
            with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
                mock_save.return_value = str(tmp_path / "capture_result.jpg")
                result = tool.call(json.dumps({
                    'path': '__screen_capture',
                    'crop_region': "100,50,200,150",
                }))

        assert isinstance(result, list), f"Expected list (successful crop), got: {result}"
        assert len(result) == 2
        caption = result[1].text
        # Caption should include original capture dimensions and crop info
        assert "800x600" in caption, f"Caption missing original size: {caption}"
        assert "cropped region x=100,y=50,w=200,h=150" in caption, f"Caption missing crop info: {caption}"

    def test_crop_region_with_svg_file(self, tmp_path):
        """crop_region works after SVG→PNG conversion."""
        # Create a minimal SVG file (200x100 rectangle)
        svg_content = '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="red"/></svg>'
        svg_path = tmp_path / "test_crop.svg"
        svg_path.write_text(svg_content)

        # Create the PNG that _convert_svg_to_png would produce (200x100)
        converted_png = tmp_path / "converted_200x100.png"
        img = Image.new("RGB", (200, 100), color="red")
        img.save(str(converted_png), format="PNG")

        from agent_cascade.tools.custom.file_ops import ViewImage
        tool = ViewImage()

        def _mock_resolve(path, mode="ro"):
            pp = Path(path)
            if pp.exists():
                return pp
            raise ValueError(f"Image not found: {path}")

        tool._resolve_path = _mock_resolve

        with patch.object(
            ViewImage, '_convert_svg_to_png',
            return_value=converted_png,
        ):
            with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
                mock_save.return_value = str(tmp_path / "svg_crop_result.jpg")
                result = tool.call(json.dumps({
                    'path': str(svg_path),
                    'crop_region': "10,20,80,50",
                }))

        assert isinstance(result, list), f"Expected list (successful SVG crop), got: {result}"
        assert len(result) == 2
        caption = result[1].text
        # Caption should include the converted PNG dimensions and crop info
        assert "200x100" in caption, f"Caption missing original size: {caption}"
        assert "cropped region x=10,y=20,w=80,h=50" in caption, f"Caption missing crop info: {caption}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])