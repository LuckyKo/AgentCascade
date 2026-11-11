"""Tests for the view_media tool (image/audio/video + crop/slice/extract).

Generated with ffmpeg fixtures so the slice/frame/audio-track paths run for real.
Media-storage writes are replaced with recording fakes so no test touches the
real media store; reuse-don't-duplicate is asserted by proving no save is made for
a plain (or already-in-store) source.
"""

import json
import logging
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


# --------------------------------------------------------------------- #
#  ffprobe metadata (BUG_0283)                                          #
# --------------------------------------------------------------------- #

_VIDEO_PROBE = {
    'streams': [
        {'index': 0, 'codec_name': 'h264', 'width': 1920, 'height': 1080,
         'avg_frame_rate': '30/1'},
        {'index': 1, 'codec_name': 'aac', 'sample_rate': '48000', 'channels': 2},
    ],
    'format': {'duration': '83.4'},
}

_AUDIO_PROBE = {
    'streams': [
        {'index': 0, 'codec_name': 'flac', 'sample_rate': '44100', 'channels': 2},
    ],
    'format': {'duration': '225'},
}


def _fake_run(payload, calls):
    """Return a subprocess.run replacement that records argv/kwargs and yields
    ``payload`` as JSON on stdout."""
    def _run(args, **kw):
        calls.append((args, kw))
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')
    return _run


def _wire_ffprobe(monkeypatch, payload):
    """Patch ffprobe lookup + subprocess.run; return the calls recorder."""
    calls = []
    monkeypatch.setattr(file_ops.shutil, 'which',
                        lambda name: 'C:/fake/ffprobe.exe' if name == 'ffprobe' else None)
    monkeypatch.setattr(file_ops.subprocess, 'run', _fake_run(payload, calls))
    return calls


def test_probe_video_formats_expected_string(monkeypatch):
    calls = _wire_ffprobe(monkeypatch, _VIDEO_PROBE)
    tool = ViewMedia()
    assert tool._probe_media_metadata('movie.mp4', 'video') == '(1920x1080, 30fps, 00:01:23, h264)'
    # Best-effort subprocess contract: argv list, no shell, capture/text/timeout set,
    # and the requested input path is passed through verbatim.
    args, kw = calls[0]
    assert args[0] == 'C:/fake/ffprobe.exe'
    assert 'movie.mp4' in args
    assert '-of' in args and 'json' in args
    assert not kw.get('shell')
    assert kw.get('capture_output') is True and kw.get('text') is True
    assert kw.get('timeout') == 5


def test_probe_audio_formats_expected_string(monkeypatch):
    _wire_ffprobe(monkeypatch, _AUDIO_PROBE)
    tool = ViewMedia()
    assert tool._probe_media_metadata('song.flac', 'audio') == '(44.1kHz, stereo, 03:45, flac)'


def test_probe_returns_empty_when_ffprobe_missing(monkeypatch):
    monkeypatch.setattr(file_ops.shutil, 'which', lambda name: None)
    # subprocess.run must never be reached without ffprobe; blow up if it is.
    def _boom(*a, **kw):
        raise AssertionError('subprocess.run called without ffprobe')
    monkeypatch.setattr(file_ops.subprocess, 'run', _boom)
    tool = ViewMedia()
    assert tool._probe_media_metadata('movie.mp4', 'video') == ''


def test_probe_swallows_subprocess_failure(monkeypatch):
    monkeypatch.setattr(file_ops.shutil, 'which', lambda name: 'C:/fake/ffprobe.exe')
    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(cmd='ffprobe', timeout=5)
    monkeypatch.setattr(file_ops.subprocess, 'run', _raise)
    tool = ViewMedia()
    assert tool._probe_media_metadata('movie.mp4', 'video') == ''  # never raises


def test_probe_swallows_malformed_json(monkeypatch):
    monkeypatch.setattr(file_ops.shutil, 'which', lambda name: 'C:/fake/ffprobe.exe')
    def _bad_json(args, **kw):
        return subprocess.CompletedProcess(args, 0, 'not json at all', '')
    monkeypatch.setattr(file_ops.subprocess, 'run', _bad_json)
    tool = ViewMedia()
    assert tool._probe_media_metadata('movie.mp4', 'video') == ''


def test_probe_nonzero_rc_logs_media_probe_warning(monkeypatch, caplog):
    """returncode != 0 path: returns '' and emits one [MEDIA_PROBE] WARNING.

    file_ops uses a module-level `logging.getLogger(__name__)` (NOT the shared
    agent_cascade.log logger), so caplog must target that logger's real name —
    read from file_ops.logger.name rather than guessed.
    """
    monkeypatch.setattr(file_ops.shutil, 'which', lambda name: 'C:/fake/ffprobe.exe')
    def _rc1(args, **kw):
        return subprocess.CompletedProcess(args, 1, '', 'No such file or directory')
    monkeypatch.setattr(file_ops.subprocess, 'run', _rc1)
    tool = ViewMedia()
    with caplog.at_level(logging.WARNING, logger=file_ops.logger.name):
        assert tool._probe_media_metadata('movie.mp4', 'video') == ''
    warnings = [r for r in caplog.records
                if r.levelname == 'WARNING' and '[MEDIA_PROBE]' in r.getMessage()]
    assert len(warnings) == 1
    assert 'rc=1' in warnings[0].getMessage()


def test_probe_skips_missing_fields_gracefully(monkeypatch):
    # No fps, no codec, no dims on the video stream -> only what ffprobe reported shows.
    payload = {
        'streams': [{'codec_name': 'mpeg2video', 'width': 720, 'height': 480}],
        'format': {'duration': '700'},
    }
    _wire_ffprobe(monkeypatch, payload)
    tool = ViewMedia()
    # duration >= 1h for video -> HH:MM:SS; fps skipped (absent), codec present.
    assert tool._probe_media_metadata('old.mpeg', 'video') == '(720x480, 00:11:40, mpeg2video)'


def test_probe_unknown_modality_returns_empty(monkeypatch):
    """An unknown modality (e.g. 'image') must not enter the audio branch."""
    _wire_ffprobe(monkeypatch, {'streams': [{'sample_rate': 44100, 'channels': 2}], 'format': {'duration': '5'}})
    tool = ViewMedia()
    result = tool._probe_media_metadata('photo.png', 'image')
    assert result == ''


# --------------------------------------------------------------------- #
#  Caption-site probes must target the DERIVED output, not the source    #
#  (BUG_0283 issues #1/#2). These mock the whole ffmpeg+ffprobe flow at  #
#  the subprocess level and assert on ContentItem.text.                  #
# --------------------------------------------------------------------- #

# Slice output carries the slice's own (short) duration; the full source carries
# its (long) duration. Resolution/fps are identical (-c copy slice), so DURATION
# is the only discriminator -> a caption with the source duration fails the test.
_SLICE_OUT_PROBE = {
    'streams': [{'codec_name': 'h264', 'width': 1920, 'height': 1080, 'avg_frame_rate': '30/1'}],
    'format': {'duration': '1.0'},
}
_SLICE_SRC_PROBE = {
    'streams': [{'codec_name': 'h264', 'width': 1920, 'height': 1080, 'avg_frame_rate': '30/1'}],
    'format': {'duration': '83.4'},
}
# Audio-track output is a 16 kHz mono WAV; the source video carries WxH/fps/codec.
# Modality and content both differ, so the caption can't accidentally match.
_TRACK_OUT_PROBE = {
    'streams': [{'codec_name': 'pcm_s16le', 'sample_rate': '16000', 'channels': 1}],
    'format': {'duration': '2.0'},
}
_TRACK_SRC_PROBE = _VIDEO_PROBE  # source video metadata (must NOT appear in an audio-track caption)


def _wire_ffmpeg_and_ffprobe(monkeypatch, out_payload, src_payload):
    """Single subprocess.run replacement for BOTH the ffmpeg run and the ffprobe
    caption-site probe.

    The ffmpeg call writes a non-empty artifact at its output path (so the
    ``out.exists() and st_size > 0`` guards pass) and records that path. The ffprobe
    call then returns ``out_payload`` only if it is probing THAT recorded path,
    otherwise ``src_payload``. This is the whole point of the test: if production
    probes the wrong target (the source), it gets ``src_payload`` and the caption
    assertions fail -- no vacuous pass from a path-agnostic mock.
    """
    state = {'out': None}

    def _run(args, **kw):
        exe = str(args[0])
        target = str(args[-1])
        if 'ffprobe' in exe:
            payload = out_payload if target == state['out'] else src_payload
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')
        # ffmpeg: stand up a real, non-empty output file at argv's final path.
        Path(target).write_bytes(b'\x00' * 1024)
        state['out'] = target
        return subprocess.CompletedProcess(args, 0, b'', b'')

    monkeypatch.setattr(file_ops.shutil, 'which',
                        lambda name: {'ffmpeg': 'C:/fake/ffmpeg.exe',
                                      'ffprobe': 'C:/fake/ffprobe.exe'}.get(name))
    monkeypatch.setattr(file_ops.subprocess, 'run', _run)


def test_slice_caption_reports_slice_duration_not_source(tmp_path, tool_store, monkeypatch):
    """Issue #2: the time_range caption must show the SLICE's duration, not the
    full source's. Resolution/fps are shared (``-c copy``) so only duration differs.
    """
    src = tmp_path / 'full.mp4'
    src.write_bytes(b'fake-video-source')  # exists() check; content irrelevant, ffmpeg is mocked
    _wire_ffmpeg_and_ffprobe(monkeypatch, _SLICE_OUT_PROBE, _SLICE_SRC_PROBE)
    tool = _make_tool()
    res = tool.call(json.dumps({'path': str(src), 'time_range': '0,1'}))
    text = res[1].text
    assert 'slice' in text and 'Saved to:' in text
    assert '00:00:01' in text, f'expected slice duration 00:00:01 in caption: {text}'
    assert '00:01:23' not in text, f'source duration leaked into slice caption: {text}'


def test_audio_track_caption_reports_audio_not_video_metadata(tmp_path, tool_store, monkeypatch):
    """Issue #1: the audio_track caption must describe the derived 16 kHz mono WAV
    (kHz + channels), NOT the source video (resolution/fps/codec).
    """
    src = tmp_path / 'video.mp4'
    src.write_bytes(b'fake-video-source')
    _wire_ffmpeg_and_ffprobe(monkeypatch, _TRACK_OUT_PROBE, _TRACK_SRC_PROBE)
    tool = _make_tool()
    res = tool.call(json.dumps({'path': str(src), 'audio_track': True}))
    text = res[1].text
    assert 'audio track' in text and 'Saved to:' in text
    assert '16kHz' in text and 'mono' in text, f'expected audio metadata (16kHz/mono) in caption: {text}'
    assert 'x1080' not in text and 'fps' not in text, f'video metadata leaked into audio-track caption: {text}'
