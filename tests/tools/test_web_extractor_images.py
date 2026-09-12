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


def test_simple_doc_parser_call_with_image_entry_no_crash():
    """Regression: the full SimpleDocParser.call() path must not crash on image entries.

    The token-counting loop in call() iterates every content entry; image entries carry no
    'text'/'table' key, so it must not pass None to count_tokens. This exercises the exact
    path that parse_html_bs-level unit tests do NOT cover (they bypass call()).
    """
    from agent_cascade.tools.simple_doc_parser import SimpleDocParser
    html = ("<html><head><title>Full Path Page</title></head><body>"
            "<p>Before figure.</p>"
            '<img src="https://example.com/fig.png" alt="Fig">'
            "<p>After figure.</p>"
            "</body></html>")
    path = _write_temp_html(html)
    try:
        parser = SimpleDocParser(cfg={'extract_image': True})
        res = parser.call({'url': path})  # full call() path, incl. token counting + caching
        # Should not raise. The plain-doc output should contain the image markdown inline.
        assert '![Fig](https://example.com/fig.png)' in res
    finally:
        os.unlink(path)


# --- MathML collapse tests -------------------------------------------------

def _math_html(inner, alttext=None):
    """Build a <math> element with nested MathML leaves (the splaying case)."""
    attrs = f' alttext="{alttext}"' if alttext is not None else ''
    return (f'<html><head><title>M</title></head><body><p>cost is '
            f'<math xmlns="http://www.w3.org/1998/Math/MathML"{attrs}>'
            f'{inner}</math> in the worst case.</p></body></html>')


def test_mathml_collapsed_to_single_line():
    """A nested <math> with alttext becomes ONE text entry (the LaTeX), not splayed leaves."""
    inner = ('<mi>O</mi><mo stretchy="false">(</mo><mi>log</mi>'
             '<mo>\u2061</mo><mi>n</mi><mo stretchy="false">)</mo>')
    html = _math_html(inner, alttext='{\\displaystyle O(\\log n)}')
    result = _parse(html, extract_image=False)  # get_text path
    texts = [item.get('text', '') for item in result[0]['content']]
    joined = ' '.join(texts)
    # The full LaTeX expression appears intact on a single line...
    assert '{\\displaystyle O(\\log n)}' in joined
    # ...and the splayed individual leaves do NOT appear as separate entries.
    assert not any(t.strip() == 'O' for t in texts)
    assert not any(t.strip() == 'log' for t in texts)


def test_mathml_fallback_to_annotation_when_no_alttext():
    """Math without alttext but with an <annotation encoding=x-tex> uses the annotation."""
    inner = ('<semantics><mi>x</mi>'
             '<annotation encoding="application/x-tex">{\\displaystyle x^2}</annotation></semantics>')
    html = _math_html(inner, alttext=None)  # no alttext attr
    result = _parse(html, extract_image=False)
    joined = ' '.join(item.get('text', '') for item in result[0]['content'])
    assert '{\\displaystyle x^2}' in joined


def test_mathml_no_regression_without_math():
    """A page with no math is unaffected by the collapse."""
    html = "<html><head><title>Plain</title></head><body><p>No math here, just text.</p></body></html>"
    result = _parse(html, extract_image=False)
    joined = ' '.join(item.get('text', '') for item in result[0]['content'])
    assert 'No math here, just text.' in joined


def test_mathml_and_image_together_in_order():
    """Math collapses AND an image stays inline, both in reading order."""
    html = ("<html><head><title>Both</title></head><body>"
            "<p>cost is <math alttext='{\\displaystyle O(1)}'><mi>O</mi><mo>(</mo><mi>1</mi><mo>)</mo></math> here.</p>"
            '<img src="https://example.com/x.png" alt="X">'
            "<p>done.</p></body></html>")
    result = _parse(html, extract_image=True, base_url='http://example.com/p.html')
    seq = [(k, v) for item in result[0]['content'] for k, v in item.items()]
    kinds = [k for k, _ in seq]
    assert 'image' in kinds
    # math collapsed inline before the image; image before "done."
    text_before_img = ' '.join(v for k, v in seq[:kinds.index('image')])
    assert '{\\displaystyle O(1)}' in text_before_img
    assert '![X](https://example.com/x.png)' in [v for k, v in seq if k == 'image']


# --- Error surfacing tests -------------------------------------------------

def test_web_extractor_404_returns_clean_message():
    """A 404 fetch failure returns a clean, actionable message — no traceback/internal paths."""
    from unittest.mock import patch
    tool = WebExtractor(cfg={'work_dir': ''})
    # Simulate the download raising the same ValueError save_url_to_local_work_dir produces.
    fake_err = ValueError('Can not download this file. Please check your network or the '
                          'file link. (Error: 404 Client Error: Not Found for url: '
                          'https://example.com/missing)')
    with patch.object(tool, '_describe_fetch_error', wraps=tool._describe_fetch_error), \
         patch('agent_cascade.tools.web_extractor.SimpleDocParser') as mock_parser:
        mock_parser.return_value.call.side_effect = fake_err
        result = tool.call({'url': 'https://example.com/missing'})
    assert 'Failed to fetch https://example.com/missing' in result
    assert '404' in result
    # No raw traceback or internal file paths leak.
    assert 'Traceback' not in result
    assert 'simple_doc_parser.py' not in result
    assert 'utils.py' not in result


def test_web_extractor_describe_fetch_error_variants():
    """_describe_fetch_error maps common failure shapes to short clean reasons."""
    desc = WebExtractor._describe_fetch_error
    # HTTP status codes
    assert '404' in desc(ValueError('... (Error: 404 Client Error: Not Found ...)'))
    assert '403' in desc(ValueError('... (Error: 403 Client Error: Forbidden ...)'))
    # Connection-level failures (no status code)
    assert 'timed out' in desc(ValueError('... (Error: HTTPSConnectionPool timed out)'))
    assert 'DNS' in desc(ValueError('... (Error: Failed to resolve name for host)'.replace('resolve', 'name resolution')))
    # Fallback: first line, no traceback
    fb = desc(RuntimeError('some odd failure\nat line 2\nat line 3'))
    assert fb == 'some odd failure'


def test_web_extractor_success_path_unaffected():
    """On success, web_extractor returns the parsed content unchanged (error handling is a no-op)."""
    from unittest.mock import patch
    tool = WebExtractor(cfg={'work_dir': ''})
    with patch('agent_cascade.tools.web_extractor.SimpleDocParser') as mock_parser:
        mock_parser.return_value.call.return_value = 'PARSED-CONTENT-OK'
        result = tool.call({'url': 'https://example.com/ok'})
    assert result == 'PARSED-CONTENT-OK'


# --- Table-aware text extraction tests (Issue 2) ---------------------------

def test_table_infobox_rows_compact():
    """A 2-column label/value table becomes one 'Label: Value' line per row, not splayed cells."""
    html = ("<html><head><title>Infobox</title></head><body>"
            "<table class='infobox'>"
            "<tr><th>Kingdom:</th><td>Animalia</td></tr>"
            "<tr><th>Phylum:</th><td>Chordata</td></tr>"
            "<tr><th>Class:</th><td>Mammalia</td></tr>"
            "</table>"
            "<p>Prose paragraph.</p>"
            "</body></html>")
    result = _parse(html, extract_image=False)  # table-aware path
    texts = [item.get('text', '') for item in result[0]['content']]
    joined = ' '.join(texts)
    # Each row is one compact entry.
    assert 'Kingdom: Animalia' in joined
    assert 'Phylum: Chordata' in joined
    assert 'Class: Mammalia' in joined
    # Not splayed: no standalone 'Animalia' or 'Kingdom:' entries.
    assert not any(t.strip() == 'Animalia' for t in texts)
    assert not any(t.strip() == 'Kingdom:' for t in texts)
    # Prose still present.
    assert 'Prose paragraph.' in joined


def test_table_wider_data_rows_pipe_joined():
    """A 3+ column data table is pipe-joined, not mangled into label:value pairs."""
    html = ("<html><head><title>Data</title></head><body>"
            "<table>"
            "<tr><th>Name</th><th>Age</th><th>City</th></tr>"
            "<tr><td>Alice</td><td>30</td><td>Paris</td></tr>"
            "</table>"
            "</body></html>")
    result = _parse(html, extract_image=False)
    joined = ' '.join(item.get('text', '') for item in result[0]['content'])
    # Header row and data row are pipe-joined single lines.
    assert 'Name | Age | City' in joined
    assert 'Alice | 30 | Paris' in joined


def test_table_td_label_with_trailing_colon():
    """Wikipedia taxonomy rows use <td>Label:</td><td>Value</td> (no <th>) — must be label:value."""
    html = ("<html><head><title>Taxonomy</title></head><body>"
            "<table class='infobox'>"
            "<tr><td>Kingdom:</td><td>Animalia</td></tr>"
            "<tr><td>Phylum:</td><td>Chordata</td></tr>"
            "</table>"
            "</body></html>")
    result = _parse(html, extract_image=False)
    joined = ' '.join(item.get('text', '') for item in result[0]['content'])
    # Colon-ending <td> label -> 'Label: Value' (no doubled pipe separator).
    assert 'Kingdom: Animalia' in joined
    assert 'Phylum: Chordata' in joined
    assert 'Kingdom: | Animalia' not in joined


def test_table_no_regression_prose_only():
    """A page with no tables is unaffected by the table-aware extraction."""
    html = ("<html><head><title>Plain</title></head><body>"
            "<p>First paragraph.</p>"
            "<p>Second paragraph.</p>"
            "</body></html>")
    result = _parse(html, extract_image=False)
    joined = ' '.join(item.get('text', '') for item in result[0]['content'])
    assert 'First paragraph.' in joined
    assert 'Second paragraph.' in joined


def test_table_with_image_in_table_preserved_on_image_path():
    """The image path still extracts an image that lives inside a table (no DOM mutation)."""
    html = ("<html><head><title>Table+Img</title></head><body>"
            "<table><tr><td>Label</td><td>"
            '<img src="https://example.com/in-table.png" alt="InTable">'
            "</td></tr></table>"
            "</body></html>")
    result = _parse(html, extract_image=True, base_url='http://example.com/p.html')
    imgs = [item.get('image', '') for item in result[0]['content'] if 'image' in item]
    assert '![InTable](https://example.com/in-table.png)' in imgs
