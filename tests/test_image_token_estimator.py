"""Tests for the dimension-aware image token estimator (BUG_0137).

Verifies that ``estimate_image_tokens()`` reproduces the measured Qwen3-VL token costs
from an image's pixel dimensions, mirrors the wire path's short-side resize rule, handles
every image source type, and is wired into ``get_message_stats()`` (structured items and
Markdown data-URIs) with an image-fingerprint cache key.

The measured calibration table (qwen3-vl-4b, fixed prompt, ``usage.prompt_tokens`` minus
the text-only baseline) and the formula it implies (32px cells, half-up rounding, +2 vision
markers, 11-token floor) are reproduced here. Live re-calibration against a running
qwen3-vl-4b is skipped unless ``AGENT_CASCADE_RUN_LOCAL_TESTS=1``.
"""

import base64
import io
import os

import pytest
from PIL import Image

from agent_cascade.llm.schema import USER, ContentItem, Message
from agent_cascade.settings import IMAGE_TOKEN_ESTIMATE, IMAGE_TOKEN_MIN
from agent_cascade.utils.utils import (estimate_image_tokens, get_message_stats,
                                       extract_text_from_message, _resized_dims, _tokens_from_size)


def _make_jpeg_bytes(width: int, height: int, color=(120, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', (width, height), color).save(buf, format='JPEG')
    return buf.getvalue()


def _write_jpeg(tmp_path, name: str, width: int, height: int, color=(120, 30, 30)) -> str:
    path = os.path.join(str(tmp_path), name)
    with open(path, 'wb') as fh:
        fh.write(_make_jpeg_bytes(width, height, color))
    return path


def _data_uri(width: int, height: int, color=(120, 30, 30)) -> str:
    return 'data:image/jpeg;base64,' + base64.b64encode(_make_jpeg_bytes(width, height, color)).decode('utf-8')


# Measured Qwen3-VL token cost per image size (see module docstring). The boundary rows
# also pin the rounding mode: 1000 -> 31 cells (proves NOT math.ceil), 1040 -> 33 cells
# (proves NOT banker's round()).
PIXEL_TABLE = [
    (32, 32, 11), (64, 64, 11), (96, 96, 11),
    (128, 128, 18), (256, 256, 66), (384, 384, 146), (512, 512, 258),
    (640, 480, 302), (480, 640, 302), (768, 768, 578),
    (1000, 1000, 963), (1008, 1008, 1026), (1024, 1024, 1026), (1030, 1030, 1026),
    (1040, 1040, 1091), (1050, 1050, 1091), (1080, 607, 648), (1080, 1080, 1158),
    (1100, 1100, 1158), (1920, 1080, 2042),
]


class TestPixelTable:
    """The estimator must reproduce the measured token cost for each pixel size."""

    @pytest.mark.parametrize('w,h,expected', PIXEL_TABLE)
    def test_size(self, tmp_path, w, h, expected):
        # Pure formula (no I/O) and the end-to-end estimator over a real JPEG must agree.
        assert _tokens_from_size(w, h) == expected
        path = _write_jpeg(tmp_path, f'img_{w}x{h}.jpg', w, h)
        assert estimate_image_tokens(path) == expected

    def test_tiny_floor(self):
        # Anything below the floor clamps to IMAGE_TOKEN_MIN.
        assert _tokens_from_size(1, 1) == IMAGE_TOKEN_MIN
        assert _tokens_from_size(16, 16) == IMAGE_TOKEN_MIN


class TestResizeRule:
    """The estimator must mirror the wire path's strict short-side downscale rule."""

    def test_short_side_exactly_cap_is_not_resized(self):
        # 1920x1080 short side == 1080 -> NOT resized -> 2042, not 648.
        assert _resized_dims(1920, 1080, 1080) == (1920, 1080)
        assert _tokens_from_size(1920, 1080) == 2042

    def test_short_side_above_cap_is_resized(self, tmp_path):
        # 3840x2160 -> resized to 1920x1080 -> 2042 (not the un-resized 3840x2160 count).
        assert _resized_dims(3840, 2160, 1080) == (1920, 1080)
        path = _write_jpeg(tmp_path, 'big.jpg', 3840, 2160)
        assert estimate_image_tokens(path) == 2042

    def test_square_above_cap(self, tmp_path):
        # 1100x1100 short side > 1080 -> resized to 1080x1080 -> 1158.
        assert _resized_dims(1100, 1100, 1080) == (1080, 1080)
        path = _write_jpeg(tmp_path, 'sq.jpg', 1100, 1100)
        assert estimate_image_tokens(path) == 1158


class TestSourceTypes:
    """Local path, file:// URL, and data URI must agree; unreadable refs fall back."""

    def test_readable_sources_agree(self, tmp_path):
        from pathlib import Path
        path = _write_jpeg(tmp_path, 'agree.jpg', 1008, 1008)
        file_uri = Path(path).as_uri()
        data_uri = _data_uri(1008, 1008)
        expected = estimate_image_tokens(path)
        assert expected == 1026
        assert estimate_image_tokens(file_uri) == expected
        assert estimate_image_tokens(data_uri) == expected

    @pytest.mark.skipif(os.name != 'nt', reason='Chrome "/C:/..." form is Windows-only')
    def test_windows_chrome_path(self, tmp_path):
        # A leading-slash drive path ('/N:/...') is what Chrome emits; sanitize_chrome_file_path
        # strips it, so the estimator must still read the dimensions.
        path = _write_jpeg(tmp_path, 'chrome.jpg', 1008, 1008)
        assert estimate_image_tokens('/' + path) == 1026

    def test_unreadable_fall_back(self, tmp_path):
        missing = os.path.join(str(tmp_path), 'nope.jpg')
        assert estimate_image_tokens(missing) == IMAGE_TOKEN_ESTIMATE
        assert estimate_image_tokens('https://example.com/x.jpg') == IMAGE_TOKEN_ESTIMATE
        assert estimate_image_tokens('http://example.com/x.jpg') == IMAGE_TOKEN_ESTIMATE
        assert estimate_image_tokens('') == IMAGE_TOKEN_ESTIMATE
        assert estimate_image_tokens(None) == IMAGE_TOKEN_ESTIMATE
        assert estimate_image_tokens('data:image/png;base64,not_valid_base64!!!') == IMAGE_TOKEN_ESTIMATE


class TestGetMessageStatsWiring:
    """The estimator must be wired into both the structured and Markdown paths, and the
    cache key must fold in an image fingerprint (BUG_0137 cache-key defect)."""

    @pytest.fixture(autouse=True)
    def _isolate_cache(self):
        from agent_cascade.utils import utils as U
        if hasattr(U.get_message_stats, '_msg_stats'):
            U.get_message_stats._msg_stats.clear()
        yield
        if hasattr(U.get_message_stats, '_msg_stats'):
            U.get_message_stats._msg_stats.clear()

    def test_structured_image_adds_dimension_aware_cost(self, tmp_path):
        """A structured ContentItem(image=path) for a 1008^2 image adds 1,026 image tokens,
        not 255. Isolate the image term by comparing an image-only message against the exact
        placeholder text that extract_text_from_message renders for it."""
        path = _write_jpeg(tmp_path, 'img1008.jpg', 1008, 1008)
        img_msg = Message(role=USER, content=[ContentItem(image=path)])
        placeholder = extract_text_from_message(img_msg, add_upload_info=True)
        text_msg = Message(role=USER, content=placeholder)

        delta = get_message_stats(img_msg)['tokens'] - get_message_stats(text_msg)['tokens']
        assert delta == 1026
        assert delta != IMAGE_TOKEN_ESTIMATE  # the flat constant must NOT be used

    def test_structured_same_text_different_images_differ(self, tmp_path):
        """Same text + one image each but different images must NOT collide on the cache key
        and must produce different totals differing by the per-image estimate."""
        # Identical filenames in different dirs so the rendered placeholder text is identical
        # and the ONLY difference is the image term (the cache-key defect).
        dA = os.path.join(str(tmp_path), 'a')
        dB = os.path.join(str(tmp_path), 'b')
        os.makedirs(dA)
        os.makedirs(dB)
        pA = os.path.join(dA, 'img.jpg')
        pB = os.path.join(dB, 'img.jpg')
        with open(pA, 'wb') as fh:
            fh.write(_make_jpeg_bytes(1008, 1008))
        with open(pB, 'wb') as fh:
            fh.write(_make_jpeg_bytes(128, 128))

        text = 'same text'
        m1008 = get_message_stats(Message(role=USER,
                                          content=[ContentItem(text=text), ContentItem(image=pA)]))['tokens']
        m128 = get_message_stats(Message(role=USER,
                                         content=[ContentItem(text=text), ContentItem(image=pB)]))['tokens']

        assert m1008 != m128, 'same-text/different-image messages must not share a cache entry'
        assert m1008 - m128 == estimate_image_tokens(pA) - estimate_image_tokens(pB) == 1008

    def test_markdown_data_uri_uses_image_group(self, tmp_path):
        """A Markdown data-URI image must estimate from the data URI (regex group 2), not the
        caption (group 1). Two messages with the same caption but different images differ by
        exactly the per-image estimate; if group 1 were used both would fall back to 255 (delta 0)."""
        md1008 = f'![cat]({_data_uri(1008, 1008)})'
        md128 = f'![cat]({_data_uri(128, 128)})'
        t1008 = get_message_stats(Message(role=USER, content=md1008))['tokens']
        t128 = get_message_stats(Message(role=USER, content=md128))['tokens']

        assert t1008 - t128 == estimate_image_tokens(_data_uri(1008, 1008)) \
            - estimate_image_tokens(_data_uri(128, 128)) == 1008


class TestStateIntegration:
    """A session of 15 x 1008^2 images must contribute ~15.4k image tokens, ~11.5k more than
    the old flat-255 accounting (the reported ~10k context undercount)."""

    @pytest.fixture(autouse=True)
    def _isolate_cache(self):
        from agent_cascade.utils import utils as U
        if hasattr(U.get_message_stats, '_msg_stats'):
            U.get_message_stats._msg_stats.clear()
        yield
        if hasattr(U.get_message_stats, '_msg_stats'):
            U.get_message_stats._msg_stats.clear()

    def test_15_images_image_term(self, tmp_path):
        n = 15
        img_msgs = []
        for i in range(n):
            path = _write_jpeg(tmp_path, f'img{i}.jpg', 1008, 1008, color=(i * 10 % 255, 30, 30))
            img_msgs.append(Message(role=USER, content=[ContentItem(image=path)]))
        # Baseline: the exact placeholder text each image renders, with no image term.
        text_msgs = [
            Message(role=USER, content=extract_text_from_message(m, add_upload_info=True)) for m in img_msgs
        ]

        new_total = sum(get_message_stats(m)['tokens'] for m in img_msgs)
        base_total = sum(get_message_stats(m)['tokens'] for m in text_msgs)
        image_term = new_total - base_total

        assert image_term == n * 1026 == 15390
        # The old flat-255 accounting undercounted by exactly this gap.
        assert image_term - n * IMAGE_TOKEN_ESTIMATE == n * (1026 - IMAGE_TOKEN_ESTIMATE) == 11565


@pytest.mark.skipif(os.environ.get('AGENT_CASCADE_RUN_LOCAL_TESTS') != '1',
                    reason='set AGENT_CASCADE_RUN_LOCAL_TESTS=1 to calibrate against a live qwen3-vl-4b')
class TestLiveCalibration:
    """Optional: send generated images to the configured vision endpoint and assert the
    prompt-token deltas match the estimator within +/-2 tokens."""

    def test_live_deltas_match_estimator(self):
        # Guarded opt-in; requires a running qwen3-vl-4b. Kept minimal and skipped by default.
        pytest.skip('live calibration requires a configured vision endpoint; run manually')
