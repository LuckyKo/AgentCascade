"""
Media storage utilities for AgentCascade.

Handles saving images (and future media types) to the <workspace>/logs/media/ directory,
providing path-based references instead of inline base64 data URLs.
"""

import base64
import io
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from PIL import Image

from agent_cascade.instance_id import make_instance_dir
from agent_cascade.settings import DEFAULT_WORKSPACE


class MediaStorageError(Exception):
    """Raised when media storage operations fail."""
    pass


def _get_media_root() -> Path:
    """Return the root media directory under the workspace logs.

    The media root is <workspace>/logs/media/ (or <workspace>/logs_<instance>/media/)
    depending on whether AGENT_CASCADE_INSTANCE_ID is set. This places media
    alongside agent logs for easy session cleanup and instance isolation.
    """
    base_logs = str(Path(DEFAULT_WORKSPACE) / "logs")
    instance_logs = make_instance_dir(base_logs)
    return Path(instance_logs) / "media"


def get_images_dir() -> str:
    """Return the absolute path to the images subdirectory, creating it if needed.

    Returns:
        Absolute path with forward slashes.

    Raises:
        MediaStorageError: If directory creation fails.
    """
    try:
        images_dir = _get_media_root() / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        return os.path.abspath(str(images_dir)).replace("\\", "/")
    except OSError as e:
        raise MediaStorageError(f"Failed to create images directory: {e}") from e


def _generate_media_filename(prefix: str, extension: str) -> str:
    """Generate a unique filename with timestamp and random hash.

    Format: <prefix>_<YYYYMMDD>_<HHMMSS>_<short_hash>.<ext>

    Args:
        prefix: Short type identifier (e.g., 'img', 'aud', 'vid').
        extension: File extension without leading dot (e.g., 'jpg', 'mp3').

    Returns:
        Filename string.
    """
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    # 8-char hex hash from random bytes for uniqueness under high concurrency
    short_hash = secrets.token_hex(4)
    return f"{prefix}_{timestamp}_{short_hash}.{extension}"


def save_image_to_media(
    image_source: Union[str, Path, bytes, io.BytesIO],
    source_name: Optional[str] = None,
    max_short_side: int = 1080,
    quality: float = 0.85,
    max_file_size_mb: float = 10.0,
) -> str:
    """Save an image to the media directory and return its absolute path.

    Opens the image from a file path, bytes, or BytesIO buffer. Resizes if the
    shorter dimension exceeds max_short_side, converts to RGB (handling transparency),
    and saves as JPEG. Checks final file size against max_file_size_mb.

    Args:
        image_source: File path (str/Path), raw bytes, or BytesIO containing image data.
        source_name: Optional hint for logging; not used in filename for safety.
        max_short_side: Resize if shorter dimension exceeds this value. Uses LANCZOS.
        quality: JPEG quality 0-1.
        max_file_size_mb: Maximum allowed file size after encoding.

    Returns:
        Absolute path to saved .jpg file with forward slashes.

    Raises:
        MediaStorageError: If any step fails (open, resize, encode, write, size check).
    """
    images_dir = get_images_dir()
    filename = _generate_media_filename("img", "jpg")
    dest_path = Path(images_dir) / filename

    try:
        # Open image from source
        if isinstance(image_source, (str, Path)):
            image = Image.open(image_source)
        elif isinstance(image_source, bytes):
            image = Image.open(io.BytesIO(image_source))
        elif isinstance(image_source, io.BytesIO):
            image = Image.open(image_source)
        else:
            raise MediaStorageError(f"Unsupported image_source type: {type(image_source)}")

        # Ensure pixels are loaded before any transforms
        image.load()

        width, height = image.size

        # Resize if shorter dimension exceeds max_short_side
        if max_short_side > 0 and min(width, height) > max_short_side:
            if width <= height:
                new_width = max_short_side
                new_height = int((max_short_side / width) * height)
            else:
                new_height = max_short_side
                new_width = int((max_short_side / height) * width)

            image = image.resize((new_width, new_height), resample=Image.Resampling.LANCZOS)

        # Convert to RGB for JPEG compatibility (handles RGBA, LA, P modes with transparency)
        if image.mode in ("RGBA", "LA", "P"):
            # Composite onto white background to preserve visible content of transparent areas
            rgb_image = Image.new("RGB", image.size, (255, 255, 255))
            if image.mode == "P":
                image = image.convert("RGBA")
            rgb_image.paste(image, mask=image.split()[-1] if image.mode in ("RGBA", "LA") else None)
            image = rgb_image
        elif image.mode != "RGB":
            image = image.convert("RGB")

        # Encode to JPEG in memory first (to check size before writing)
        output_buffer = io.BytesIO()
        image.save(output_buffer, format="JPEG", quality=int(quality * 100))
        encoded_data = output_buffer.getvalue()

        # Check file size limit
        file_size_mb = len(encoded_data) / (1024 * 1024)
        if file_size_mb > max_file_size_mb:
            raise MediaStorageError(
                f"Encoded image too large ({file_size_mb:.1f} MB > {max_file_size_mb:.1f} MB limit)"
            )

        # Write to disk
        dest_path.write_bytes(encoded_data)

        return os.path.abspath(str(dest_path)).replace("\\", "/")

    except MediaStorageError:
        raise
    except Exception as e:
        # Clean up partial file if it exists
        if dest_path.exists():
            try:
                dest_path.unlink()
            except OSError:
                pass
        source_hint = source_name or str(image_source) if isinstance(image_source, (str, Path)) else "<binary>"
        raise MediaStorageError(f"Failed to save image from {source_hint}: {e}") from e


def save_image_from_data_uri(data_uri: str) -> str:
    """Decode a base64 data URI and save the image to media storage.

    Args:
        data_uri: A data URI string like 'data:image/png;base64,iVBORw0KGgo...'.

    Returns:
        Absolute path to saved .jpg file with forward slashes.

    Raises:
        MediaStorageError: If the data URI is invalid or saving fails.
    """
    # Validate data URI format
    if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
        raise MediaStorageError("Invalid data URI: does not start with 'data:'")

    # Extract base64 portion (everything after the first comma)
    match = re.match(r"data:[^;]+;base64,(.+)", data_uri, re.IGNORECASE)
    if not match:
        raise MediaStorageError("Invalid data URI format: expected 'data:<type>;base64,<data>'")

    try:
        image_bytes = base64.b64decode(match.group(1))
    except Exception as e:
        raise MediaStorageError(f"Failed to decode base64 from data URI: {e}") from e

    return save_image_to_media(image_source=image_bytes, source_name="data_uri")


def cleanup_old_media(max_age_days: int = 30) -> dict:
    """Remove media files older than max_age_days across all media subdirectories.

    Args:
        max_age_days: Delete files modified more than this many days ago.

    Returns:
        Dict with keys 'files_removed' (int), 'bytes_freed' (int), 'errors' (list of str).
    """
    result = {"files_removed": 0, "bytes_freed": 0, "errors": []}

    media_root = _get_media_root()
    if not media_root.exists():
        return result

    cutoff_time = time.time() - (max_age_days * 24 * 60 * 60)

    for dirpath, dirnames, filenames in os.walk(media_root):
        # Skip hidden directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for filename in filenames:
            filepath = Path(dirpath) / filename
            try:
                stat = filepath.stat()
                if stat.st_mtime < cutoff_time:
                    file_size = stat.st_size
                    filepath.unlink()
                    result["files_removed"] += 1
                    result["bytes_freed"] += file_size
            except OSError as e:
                result["errors"].append(f"Failed to remove {filepath}: {e}")

    return result


def _cleanup_worker(max_age_days: int, interval_hours: float):
    """Background thread function that periodically runs cleanup_old_media."""
    interval_seconds = interval_hours * 3600
    while True:
        time.sleep(interval_seconds)
        try:
            result = cleanup_old_media(max_age_days=max_age_days)
            if result["files_removed"] > 0:
                from agent_cascade.log import logger
                logger.info(
                    f"Media cleanup: removed {result['files_removed']} files, "
                    f"freed {result['bytes_freed'] / (1024*1024):.1f} MB"
                )
            if result["errors"]:
                from agent_cascade.log import logger
                for err in result["errors"]:
                    logger.warning(f"Media cleanup error: {err}")
        except Exception as e:
            from agent_cascade.log import logger
            logger.error(f"Media cleanup failed: {e}")


def start_media_cleanup_scheduler(max_age_days: int = 30, interval_hours: float = 6) -> threading.Thread:
    """Start a background daemon thread that periodically cleans up old media files.

    Args:
        max_age_days: Delete files older than this many days.
        interval_hours: How often to run cleanup.

    Returns:
        The started Thread object (daemon=True).

    Raises:
        MediaStorageError: If the thread cannot be started.
    """
    try:
        thread = threading.Thread(
            target=_cleanup_worker,
            args=(max_age_days, interval_hours),
            name="media-cleanup-scheduler",
            daemon=True,
        )
        thread.start()
        return thread
    except Exception as e:
        raise MediaStorageError(f"Failed to start media cleanup scheduler: {e}") from e