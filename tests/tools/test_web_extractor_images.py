# Copyright 2023 The Qwen team, Alibaba Group. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for inline image extraction in parse_html_bs / web_extractor.

Item 1+1b (core extraction) is covered here end-to-end at the parse_html_bs level:
reading order, relative->absolute resolution, data:/empty src skipping, boilerplate
stripping, no dedup, text-only regression, and extract_image=False. The
web_extractor-level default (extract_images on by default) is tested via the tool's
parameter schema; its call() behavior is identical to SimpleDocParser with
cfg['extract_image'] set, which is what parse_html_bs receives.

All tests use small inline HTML written to temp files — no network.
"""

import os
import tempfile

from agent_cascade.tools.simple_doc_parser import parse_html_bs
from agent_cascade.tools.web_extractor import WebExtractor


def _write_temp_html(html: str) -> str:
    """Write HTML to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix='.html')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(html)
    return path


def _parse(html: str, extract_image: bool = True, base_url: str = None):
    """Write html to a temp file and parse it; cleans up the temp file."""
    path = _write_temp_html(html)
    try:
        return parse_html_bs(path, extract_image=extract_image, base_url=base_url)
    finally:
        os.unlink(path)


def test_parse_html_bs_images_in_reading_order():
    """Images land between the surrounding text blocks in reading order."""
    html = ("<html><head><title>Order Page</title></head><body>"
            "<p>PART-A before image.</p>"
            '<img src="https://example.com/abs.png" alt="Fig A">'
            "<p>PART-B after image.</p>"
            "</body></html>")
    result = _parse(html, base_url='http://example.com/page.html')
    items = result[0]['content']
    # Flatten to a sequence of (kind, value) in output order.
    seq = [(k, v) for item in items for k, v in item.items()]
    kinds = [k for k, _ in seq]
    assert 'image' in kinds
    img_idx = kinds.index('image')
    text_before = ' '.join(v for k, v in seq[:img_idx])
    text_after = ' '.join(v for k, v in seq[img_idx + 1:])
    assert 'PART-A' in text_before
    assert 'PART-B' in text_after
    # The image entry is markdown with alt text and the absolute URL.
    img_value = [v for k, v in seq if k == 'image'][0]
    assert img_value == '![Fig A](https://example.com/abs.png)'


def test_parse_html_bs_relative_src_resolved_against_base_url():
    """src='img/x.png' on a page from http://example.com/a/b.html -> /a/img/x.png."""
    html = ("<html><head><title>Rel Page</title></head><body>"
            '<img src="img/x.png" alt="rel">'
            "</body></html>")
    result = _parse(html, base_url='http://example.com/a/b.html')
    images = [v for item in result[0]['content'] for v in item.values() if 'image' in item]
    assert images == ['![rel](http://example.com/a/img/x.png)']


def test_parse_html_bs_relative_src_resolved_against_base_href_tag():
    """With no base_url (e.g. a local file), a relative src resolves against <base href>."""
    html = ("<html><head>"
            '<base href="http://cdn.example.com/assets/">'
            "<title>BaseHref Page</title></head><body>"
            '<img src="pic/y.png" alt="via-base">'
            "</body></html>")
    # base_url=None -> _resolve_image_src falls back to the <base href> in the doc.
    result = _parse(html, base_url=None)
    images = [v for item in result[0]['content'] for v in item.values() if 'image' in item]
    assert images == ['![via-base](http://cdn.example.com/assets/pic/y.png)']


def test_parse_html_bs_data_uri_skipped():
    """<img src='data:...'> is not included."""
    html = ("<html><head><title>Data Page</title></head><body>"
            '<p>TEXT-ONLY-VISIBLE</p>'
            '<img src="data:image/png;base64,iVBORw0KGgoAAAANS" alt="inline">'
            "</body></html>")
    result = _parse(html, base_url='http://example.com/p.html')
    content = result[0]['content']
    assert all('image' not in item for item in content)
    assert any('TEXT-ONLY-VISIBLE' in item.get('text', '') for item in content)


def test_parse_html_bs_missing_src_skipped():
    """<img> without src (and with empty src) is skipped without error."""
    html = ("<html><head><title>NoSrc Page</title></head><body>"
            "<p>REAL-TEXT</p>"
            '<img alt="no-src">'
            '<img src="" alt="empty-src">'
            "</body></html>")
    result = _parse(html, base_url='http://example.com/p.html')
    content = result[0]['content']
    assert all('image' not in item for item in content)
    assert any('REAL-TEXT' in item.get('text', '') for item in content)


def test_parse_html_bs_boilerplate_images_stripped():
    """Images inside <nav> or a stripped banner header do NOT appear; content-root ones DO."""
    html = ("<html><head><title>Banner Page</title></head><body>"
            '<header role="banner"><img src="logo.png" alt="logo"></header>'
            '<nav><img src="nav-icon.png" alt="nav-icon"></nav>'
            '<main><p>MAIN-TEXT</p>'
            '<img src="content.png" alt="content-img">'
            '</main>'
            "</body></html>")
    result = _parse(html, base_url='http://example.com/p.html')
    content = result[0]['content']
    images = [v for item in content for v in item.values() if 'image' in item]
    assert images == ['![content-img](http://example.com/content.png)']
    joined = '\n'.join(str(v) for item in content for v in item.values())
    assert 'logo' not in joined
    assert 'nav-icon' not in joined


def test_parse_html_bs_duplicate_images_kept():
    """Two identical <img> at different positions both appear (no dedup)."""
    html = ("<html><head><title>Dup Page</title></head><body>"
            '<p>FIRST-POS</p>'
            '<img src="x.png" alt="dup">'
            '<p>MIDDLE</p>'
            '<img src="x.png" alt="dup">'
            "</body></html>")
    result = _parse(html, base_url='http://example.com/p.html')
    content = result[0]['content']
    images = [v for item in content for v in item.values() if 'image' in item]
    assert images == ['![dup](http://example.com/x.png)', '![dup](http://example.com/x.png)']


def test_parse_html_bs_no_images_text_only_regression():
    """Text-only page with extract_image=True: no image entries, text unchanged."""
    html = ("<html><head><title>Plain Page</title></head><body>"
            "<p>PARA-ONE.</p>"
            "<p>PARA-TWO.</p>"
            "</body></html>")
    result = _parse(html, extract_image=True, base_url='http://example.com/p.html')
    content = result[0]['content']
    assert all('image' not in item for item in content)
    texts = [item['text'] for item in content]
    joined = '\n'.join(texts)
    assert 'PARA-ONE.' in joined
    assert 'PARA-TWO.' in joined


def test_parse_html_bs_extract_image_false_omits_images():
    """extract_image=False: images omitted, text still present."""
    html = ("<html><head><title>Off Page</title></head><body>"
            '<p>TEXT-ONLY-VISIBLE</p>'
            '<img src="https://example.com/hidden.png" alt="hidden">'
            "</body></html>")
    result = _parse(html, extract_image=False, base_url='http://example.com/p.html')
    content = result[0]['content']
    assert all('image' not in item for item in content)
    joined = '\n'.join(item.get('text', '') for item in content)
    assert 'TEXT-ONLY-VISIBLE' in joined
    assert 'hidden.png' not in joined


def test_web_extractor_extract_images_param_defaults_true():
    """web_extractor exposes extract_images (bool, default true) in its parameter schema."""
    tool = WebExtractor()
    props = tool.parameters['properties']
    assert 'extract_images' in props
    assert props['extract_images']['type'] == 'boolean'
    assert props['extract_images']['default'] is True
    # url remains the only required param.
    assert tool.parameters['required'] == ['url']
