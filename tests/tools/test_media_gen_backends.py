"""Tests for the media_gen backend *contract* layer and generic media saving.

Covers:
  * ``MediaRequest`` / ``MediaResult`` dataclass defaults and ``as_dict``
  * ``MediaBackend`` ABC (abstract methods raise NotImplementedError)
  * ``save_media_to_media`` — the image path must be BYTE-IDENTICAL to the existing
    ``save_image_to_media`` (the plan's whole safety case for not regressing images).

The image byte-identity proof has two halves: (a) a git-diff showing image_gen.py
unchanged (done in the build report), and (b) this deep-equal runtime test — the
bytes written by ``save_media_to_media(..., 'image')`` are identical to those written
by ``save_image_to_media`` for the same source.
"""

import io
import os
from pathlib import Path

import pytest

from agent_cascade.utils.media_utils import (
    save_image_to_media,
    save_media_to_media,
    MediaStorageError,
    get_images_dir,
    _get_type_dir,
)
from agent_cascade.tools.media_backends.base import (
    MEDIA_TYPES,
    MediaRequest,
    MediaResult,
    MediaBackend,
)


# ── MediaRequest / MediaResult defaults ──────────────────────────────────────

class TestMediaRequestDefaults:
    def test_defaults(self):
        req = MediaRequest()
        assert req.backend_id == ''
        assert req.media_type == ''
        assert req.reference_file is None
        assert req.media_size is None
        assert req.prepared is None

    def test_as_dict_shape(self):
        req = MediaRequest(prompt='hi', media_type='image')
        d = req.as_dict()
        assert d['prompt'] == 'hi'
        assert d['media_type'] == 'image'
        assert set(d) == {
            'media_type', 'backend_id', 'config_name', 'prompt', 'negative_prompt',
            'reference_file', 'media_size', 'overrides',
        }

    def test_media_size_is_a_dict(self):
        # P1 finding: dict-valued media_size is supported (jsonschema.validate).
        req = MediaRequest(media_size={'width': 512, 'height': 512})
        assert req.media_size == {'width': 512, 'height': 512}


class TestMediaResultDefaults:
    def test_defaults(self):
        res = MediaResult()
        assert res.data == b''
        assert res.ext == ''
        assert res.meta is None
        assert res.elapsed == 0.0


class TestMediaBackendABC:
    def test_abstract_methods_raise(self):
        class Bare(MediaBackend):
            id = 'bare'
            media_types = ('image',)

        b = Bare()
        with pytest.raises(NotImplementedError):
            b.preflight(MediaRequest())
        with pytest.raises(NotImplementedError):
            b.run(MediaRequest())

    def test_media_types_vocabulary(self):
        assert MEDIA_TYPES == ('image', 'video', 'audio')


# ── save_media_to_media: image path byte-identical to save_image_to_media ────

def _make_test_png_bytes():
    """A small deterministic PNG (so PIL re-encodes identically both times)."""
    from PIL import Image
    img = Image.new('RGB', (32, 32), color=(123, 45, 67))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


class TestSaveMediaByteIdentical:
    def test_image_path_delegates_to_save_image(self):
        # The image branch is a verbatim delegation → identical output bytes.
        src = _make_test_png_bytes()

        p1 = save_image_to_media(src)
        p2 = save_media_to_media(src, 'image')

        assert Path(p1).read_bytes() == Path(p2).read_bytes()
        # Both land in the images dir with the img_*.jpg naming.
        assert Path(p1).parent == Path(p2).parent == Path(get_images_dir())
        assert Path(p1).name.startswith('img_') and Path(p1).name.endswith('.jpg')
        assert Path(p2).name.startswith('img_') and Path(p2).name.endswith('.jpg')

    def test_audio_writes_raw_bytes_to_audio_dir(self):
        data = b'FAKE-MP3-BYTES'
        p = save_media_to_media(data, 'audio')
        assert Path(p).read_bytes() == data
        assert Path(p).parent == Path(_get_type_dir('audio'))
        assert Path(p).name.startswith('aud_') and Path(p).name.endswith('.mp3')

    def test_music_shares_audio_dir(self):
        # 'music' is audio output → audio dir, aud_ prefix (not a new media type).
        data = b'FAKE-MUSIC-BYTES'
        p = save_media_to_media(data, 'music')
        assert Path(p).read_bytes() == data
        assert Path(p).parent == Path(_get_type_dir('audio'))
        assert Path(p).name.startswith('aud_')

    def test_video_writes_raw_bytes_to_video_dir(self):
        data = b'FAKE-MP4-BYTES'
        p = save_media_to_media(data, 'video')
        assert Path(p).read_bytes() == data
        assert Path(p).parent == Path(_get_type_dir('video'))
        assert Path(p).name.startswith('vid_') and Path(p).name.endswith('.mp4')

    def test_unknown_media_type_raises(self):
        with pytest.raises(MediaStorageError) as e:
            save_media_to_media(b'x', 'hologram')
        assert 'Unsupported media_type' in str(e.value)

    def test_get_images_dir_still_works(self):
        # The public get_images_dir is a thin wrapper over _get_type_dir('images').
        assert get_images_dir() == _get_type_dir('images')
