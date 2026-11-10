"""Tests for the view_media tool (image/audio/video + crop/slice/extract).

Generated with ffmpeg fixtures so the slice/frame/audio-track paths run for real.
Media-storage writes are replaced with recording fakes so no test touches the
real media store; reuse-don't-duplicate is asserted by proving no save is made for
a plain (or already-in-store) source.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import agent_cascade.tools.custom.file_ops as file_ops
from agent_cascade.tools.custom.file_ops import ViewImage, ViewMedia


FFMPEG = shutil.which('ffmpeg')


def _ffmpeg(*args):
    return subprocess.run(
        ['ffmpeg', '-y', *args],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode


@pytest.fixture(scope='module')
def fixtures(tmp_path_factory):
    if FFMPEG is None:
        pytest.skip('ffmpeg not on PATH')
    d = tmp_path_factory.mktemp('view_media_fixtures')
    wav = d / 'clip.wav'
    mp4 = d / 'clip.mp4'
    png = d / 'still.png'
    assert _ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
                   '-c:a', 'pcm_s16le', '-ar', '44100', '-ac', '1', str(wav)) == 0
    assert _ffmpeg('-f', 'lavfi', '-i', 'color=c=blue:s=160x120:d=2',
                   '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
                   '-shortest', '-c:v', 'mpeg4', '-c:a', 'aac', str(mp4)) == 0
    assert _ffmpeg('-f', 'lavfi', '-i', 'color=c=red:s=16x16:d=1',
                   '-frames:v', '1', str(png)) == 0
    assert wav.stat().st_size > 0 and mp4.stat().st_size > 0 and png.stat().st_size > 0
    return {'wav': str(wav), 'mp4': str(mp4), 'png': str(png)}


def _make_tool():
    """Build a ViewMedia whose path resolution is a dict lookup over fixture paths."""
    tool = ViewMedia()
    tool._resolve_path = lambda p: Path(p)
    return tool


class _MediaStore:
    """Recording fake for save_media_to_media / save_image_to_media."""

    def __init__(self):
        self.records = []   # (media_type, ext, size)
        self.image_saved = 0

    def media(self, source=None, media_type=None, ext=None, **kw):
        if isinstance(source, (bytes, bytearray)):
            size = len(source)
        else:
            size = os.path.getsize(source)
        self.records.append((media_type, ext, size))
        return 'SAVE_%s_%s' % (media_type, ext)

    def image(self, image_source=None, **kw):
        self.records.append(('image', 'png', os.path.getsize(image_source)))
        return 'SAVE_image_png'


@pytest.fixture
def tool_store(monkeypatch):
    """Patches both savers + is_under_media_root (default False) and returns store."""
    store = _MediaStore()
    monkeypatch.setattr(file_ops, 'save_media_to_media', store.media)
    monkeypatch.setattr(file_ops, 'save_image_to_media', store.image)
    monkeypatch.setattr(file_ops, 'is_under_media_root', lambda p: False)
    return store


# --------------------------------------------------------------------- #
#  Dispatch per modality                                                 #
# --------------------------------------------------------------------- #

def test_plain_audio_dispatch(fixtures, tool_store):
    tool = _make_tool()
    res = tool.call(json.dumps({'path': fixtures['wav']}))
    assert isinstance(res, list) and len(res) == 2
    assert res[0].audio and os.path.splitext(res[0].audio)[1] == '.wav'
    assert res[0].caption is None
    assert res[0].video is None and res[0].image is None
    assert 'Viewing audio' in res[1].text
    # reuse-don't-duplicate: plain view saves nothing
    assert tool_store.records == []


def test_plain_video_dispatch(fixtures, tool_store):
    tool = _make_tool()
    res = tool.call(json.dumps({'path': fixtures['mp4']}))
    assert res[0].video and os.path.splitext(res[0].video)[1] == '.mp4'
    assert res[0].caption is None
    assert 'Viewing video' in res[1].text
    assert tool_store.records == []


def test_image_delegates_to_view_image(monkeypatch):
    seen = {}

    class _FakeViewImage:
        def __init__(self, agent_pool=None):
            pass

        def call(self, params):
            seen['params'] = json.loads(params)
            return ['DELEGATED_IMAGE']

    monkeypatch.setattr(file_ops, 'ViewImage', _FakeViewImage)
    tool = ViewMedia()
    res = tool.call(json.dumps({'path': 'a.png', 'crop_region': '0,0,10,10'}))
    assert res == ['DELEGATED_IMAGE']
    assert seen['params']['path'] == 'a.png'
    assert seen['params']['crop_region'] == '0,0,10,10'


# --------------------------------------------------------------------- #
#  ffmpeg operations                                                     #
# --------------------------------------------------------------------- #

def test_time_range_slice_audio(fixtures, tool_store):
    tool = _make_tool()
    res = tool.call(json.dumps({'path': fixtures['wav'], 'time_range': '0,1'}))
    assert res[0].audio == 'SAVE_audio_wav'
    assert res[0].caption is None
    mt, ext, size = tool_store.records[0]
    assert (mt, ext) == ('audio', 'wav') and size > 0
    # Prove ffmpeg actually truncated: a 0-1s slice of the 2s source must be smaller
    # on disk than the original WAV. Without this, size > 0 alone can't distinguish
    # a real slice from a full re-encode of the whole clip.
    assert size < os.path.getsize(fixtures['wav'])
    assert 'slice' in res[1].text and 'Saved to:' in res[1].text


def test_time_range_slice_video(fixtures, tool_store):
    tool = _make_tool()
    res = tool.call(json.dumps({'path': fixtures['mp4'], 'time_range': '0,1'}))
    assert res[0].video == 'SAVE_video_mp4'
    mt, ext, size = tool_store.records[0]
    assert (mt, ext) == ('video', 'mp4') and size > 0


def test_frame_at_extracts_image(fixtures, tool_store):
    tool = _make_tool()
    res = tool.call(json.dumps({'path': fixtures['mp4'], 'frame_at': 0.5}))
    assert res[0].image == 'SAVE_image_png'
    assert res[0].caption is None
    assert res[0].audio is None and res[0].video is None
    mt, ext, size = tool_store.records[0]
    assert (mt, ext) == ('image', 'png') and size > 0
    assert 'frame at' in res[1].text


def test_audio_track_extracts_wav(fixtures, tool_store):
    tool = _make_tool()
    res = tool.call(json.dumps({'path': fixtures['mp4'], 'audio_track': True}))
    assert res[0].audio == 'SAVE_audio_wav'
    mt, ext, size = tool_store.records[0]
    assert (mt, ext) == ('audio', 'wav') and size > 0
    assert 'audio track' in res[1].text


# --------------------------------------------------------------------- #
#  Validation / rejection paths                                          #
# --------------------------------------------------------------------- #

def test_mutual_exclusion_time_range_extract(fixtures):
    tool = _make_tool()
    res = tool.call(json.dumps({'path': fixtures['mp4'], 'time_range': '0,1', 'frame_at': 0.5}))
    assert isinstance(res, str) and res.startswith('ERROR') and 'cannot be combined' in res


def test_mutual_exclusion_frame_at_and_track(fixtures):
    tool = _make_tool()
    res = tool.call(json.dumps({'path': fixtures['mp4'], 'frame_at': 0.5, 'audio_track': True}))
    assert isinstance(res, str) and res.startswith('ERROR') and 'mutually exclusive' in res


def test_crop_region_on_audio_rejected():
    tool = _make_tool()
    res = tool.call(json.dumps({'path': 'a.wav', 'crop_region': '0,0,5,5'}))
    assert isinstance(res, str) and 'crop_region applies only to images' in res


def test_extract_on_audio_rejected():
    tool = _make_tool()
    res = tool.call(json.dumps({'path': 'a.wav', 'frame_at': 1.0}))
    assert isinstance(res, str) and 'video-only' in res


def test_invalid_time_range():
    tool = _make_tool()
    res = tool.call(json.dumps({'path': 'a.wav', 'time_range': '5,2'}))
    assert isinstance(res, str) and 'Invalid time_range' in res


def test_unknown_extension_error():
    tool = _make_tool()
    res = tool.call(json.dumps({'path': 'notes.txt'}))
    assert isinstance(res, str) and 'Unsupported media type' in res


def test_ffmpeg_missing_handling(tmp_path, monkeypatch):
    monkeypatch.setattr(file_ops.shutil, 'which', lambda name: None)
    # write a real file so we reach the ffmpeg check rather than the not-found check
    wav = tmp_path / 'x.wav'
    wav.write_bytes(b'RIFFxxxxfake')
    tool = _make_tool()
    res = tool.call(json.dumps({'path': str(wav), 'time_range': '0,1'}))
    assert isinstance(res, str) and res.startswith('ERROR') and 'ffmpeg' in res


def test_source_file_not_found():
    tool = _make_tool()
    res = tool.call(json.dumps({'path': 'missing_audio.wav'}))
    assert isinstance(res, str) and res.startswith('ERROR') and 'not found' in res


# --------------------------------------------------------------------- #
#  Reuse-don't-duplicate rule                                            #
# --------------------------------------------------------------------- #

def test_audio_under_media_root_not_re_saved(fixtures, monkeypatch):
    """A source already in the media store is reused verbatim: no save, flagged."""
    saved = {'n': 0}

    def media(*a, **kw):
        saved['n'] += 1
        return 'SHOULD_NOT_HAPPEN'

    monkeypatch.setattr(file_ops, 'save_media_to_media', media)
    monkeypatch.setattr(file_ops, 'is_under_media_root', lambda p: True)
    tool = _make_tool()
    res = tool.call(json.dumps({'path': fixtures['wav']}))
    assert saved['n'] == 0
    assert 'already in media store' in res[1].text
    assert 'no copy saved' in res[1].text


def test_view_image_under_media_root_not_re_saved(fixtures, monkeypatch):
    """ViewImage plain-reuse branch must skip save_image_to_media for a media-root source."""
    saved = {'n': 0}

    def image(*a, **kw):
        saved['n'] += 1
        return 'SHOULD_NOT_HAPPEN'

    monkeypatch.setattr(file_ops, 'save_image_to_media', image)
    monkeypatch.setattr(file_ops, 'is_under_media_root', lambda p: True)
    tool = ViewImage()
    tool._resolve_path = lambda p: Path(p)
    res = tool.call(json.dumps({'path': fixtures['png']}))
    assert saved['n'] == 0
    assert isinstance(res, list) and len(res) == 2
    assert res[0].caption is None
    assert 'already in media store' in res[1].text
    assert 'no copy saved' in res[1].text
