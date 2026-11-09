"""Phase-2 omni-multimodal input-pipeline tests (M5 / Seams 2 & 4).

Covers ``content_parse._parse_multimodal_content`` carrying user-inserted media
markdown (``![alt](url)``) through to ``audio`` / ``video`` ``ContentItem``-shaped
dicts so they reach the ``qwenvl_oai`` adapter:

  * ``data:audio/*`` / ``data:video/*`` data-URIs -> ``{'audio'|'video': saved_path}``
    + a ``[NOTE: Saved as …]`` text part (image path unchanged as a regression guard);
  * local ``.mp3`` / ``.mp4`` paths -> ``{'audio'|'video': path}`` emitted as-is;
  * mixed text + media interstitial ordering is preserved;
  * no media match -> the raw text string is returned unchanged;
  * ``save_audio_from_data_uri`` / ``save_video_from_data_uri`` honor the MIME subtype
    as the saved file extension.

Real (un-mocked) media-save helpers are used; every file written is removed in
teardown so no temp artifacts are left in the media store.
"""

import os

import pytest

from agent_cascade.content_parse import _media_modality, _parse_multimodal_content


@pytest.fixture
def saved_paths():
    """Collect any saved media path; delete them after each test."""
    seen = []

    def _track(items):
        for it in items:
            for key in ('image', 'audio', 'video'):
                v = it.get(key)
                if isinstance(v, str) and not v.startswith('data:'):
                    seen.append(v)
        return items

    yield _track
    for p in seen:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


class TestModalityClassification:
    def test_data_uri_subtypes(self):
        assert _media_modality('data:audio/wav;base64,AAA') == 'audio'
        assert _media_modality('data:video/mp4;base64,BBB') == 'video'
        assert _media_modality('data:image/png;base64,CCC') == 'image'

    def test_local_path_extensions(self):
        assert _media_modality('N:\\music\\track.mp3') == 'audio'
        assert _media_modality('N:/video/clip.mp4') == 'video'
        assert _media_modality('file:///tmp/x.webm') == 'video'

    def test_non_media_returns_none(self):
        assert _media_modality('notmedia.txt') is None
        assert _media_modality('foo.png') is None  # extension alone (not a data-URI) is not matched


class TestParseMultimodalContent:

    def test_audio_data_uri_emits_audio_item(self, saved_paths):
        url = 'data:audio/wav;base64,AAAA'
        parts = saved_paths(_parse_multimodal_content(f'start ![a]({url}) end'))
        # text | audio | note | text
        assert parts[0] == {'text': 'start '}
        audio = parts[1]
        assert 'audio' in audio
        assert not audio['audio'].startswith('data:')  # saved to a path, not inline
        assert audio['audio'].endswith('.wav')  # MIME subtype honored as extension
        assert parts[2] == {'text': f"[NOTE: Saved as {audio['audio']}]"}
        assert parts[3] == {'text': ' end'}

    def test_video_data_uri_emits_video_item(self, saved_paths):
        url = 'data:video/mp4;base64,BBBB'
        parts = saved_paths(_parse_multimodal_content(f'![v]({url})'))
        video = parts[0]
        assert 'video' in video
        assert not video['video'].startswith('data:')
        assert video['video'].endswith('.mp4')
        assert parts[1] == {'text': f"[NOTE: Saved as {video['video']}]"}

    def test_image_data_uri_still_works(self, saved_paths):
        """Regression: image handling is unchanged by the audio/video extension.

        Uses a genuine 1x1 PNG so the real save path runs (fake base64 would
        correctly hit the MediaStorageError inline-fallback branch, not this one).
        """
        url = ('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC'
               '0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=')
        parts = saved_paths(_parse_multimodal_content(f'![i]({url})'))
        image = parts[0]
        assert 'image' in image
        assert not image['image'].startswith('data:')
        assert parts[1] == {'text': f"[NOTE: Saved as {image['image']}]"}

    def test_local_audio_path_emitted_as_is(self):
        # No data-URI, no save, no NOTE — the path is already a reference.
        parts = _parse_multimodal_content('![s](N:\\music\\track.mp3)')
        assert parts == [{'audio': 'N:\\music\\track.mp3'}]

    def test_local_video_path_emitted_as_is(self):
        parts = _parse_multimodal_content('![c](N:/video/clip.mp4)')
        assert parts == [{'video': 'N:/video/clip.mp4'}]

    def test_mixed_ordering_preserved(self, saved_paths):
        au = 'data:audio/wav;base64,AAAA'
        vi = 'data:video/mp4;base64,BBBB'
        text = f'before ![a]({au}) middle ![v]({vi}) after'
        parts = saved_paths(_parse_multimodal_content(text))
        assert parts[0] == {'text': 'before '}
        assert 'audio' in parts[1]
        assert parts[2] == {'text': f"[NOTE: Saved as {parts[1]['audio']}]"}
        assert parts[3] == {'text': ' middle '}
        assert 'video' in parts[4]
        assert parts[5] == {'text': f"[NOTE: Saved as {parts[4]['video']}]"}
        assert parts[6] == {'text': ' after'}

    def test_no_media_returns_raw_text_unchanged(self):
        raw = 'just some text with a [link](http://x/y) and ![notmedia](foo.txt)'
        assert _parse_multimodal_content(raw) == raw


class TestMediaSaveHelpers:

    def test_save_audio_honors_mime_subtype(self):
        from agent_cascade.utils.media_utils import save_audio_from_data_uri
        path = save_audio_from_data_uri('data:audio/mp3;base64,AAAA')
        try:
            assert path.endswith('.mp3')
            assert os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_save_video_honors_mime_subtype(self):
        from agent_cascade.utils.media_utils import save_video_from_data_uri
        path = save_video_from_data_uri('data:video/webm;base64,BBBB')
        try:
            assert path.endswith('.webm')
            assert os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.remove(path)
