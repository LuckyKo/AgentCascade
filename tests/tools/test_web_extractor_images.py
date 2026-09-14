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
stripping, per-page image dedup (same resolved URL collapses to one entry), text-only regression, and extract_image=False. The
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
    html = ('<html><head><title>Order Page</title></head><body>'
            '<p>PART-A before image.</p>'
            '<img src="https://example.com/abs.png" alt="Fig A">'
            '<p>PART-B after image.</p>'
            '</body></html>')
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


def _text_entries(result):
    """Return the list of text values (in order) from a parse_html_bs result."""
    return [item['text'] for item in result[0]['content'] if 'text' in item]


def test_wrapped_p_single_entry_both_paths():
    """A single <p> whose SOURCE has line-wraps (\n) must stay ONE entry.

    Regression: source line-wraps were split into separate entries (one per source
    line) because the output was split on \n. Must hold for BOTH extract_image values.
    """
    html = ('<html><head><title>T</title></head><body>'
            '<p>WRAPPED-para starts here and continues '
            '\nacross a second source line and then '
            '\na third source line to finish.</p>'
            '</body></html>')
    for ei in (True, False):
        result = _parse(html, extract_image=ei, base_url='http://x/p.html')
        texts = _text_entries(result)
        assert len(texts) == 1, f"extract_image={ei}: expected 1 entry, got {texts!r}"
        joined = texts[0]
        for frag in ('WRAPPED-para', 'second source line', 'third source line'):
            assert frag in joined


def test_div_two_p_separate_entries_both_paths():
    """<div><p>a</p><p>b</p></div> must be TWO entries (not merged) in both paths.

    Regression: the table path (extract_image=False) previously merged nested blocks
    into one entry ('ab' with no separator).
    """
    html = ('<html><head><title>T</title></head><body>'
            '<div><p>ALPHA-para-one</p><p>BETA-para-two</p></div>'
            '</body></html>')
    for ei in (True, False):
        result = _parse(html, extract_image=ei, base_url='http://x/p.html')
        texts = _text_entries(result)
        assert any('ALPHA-para-one' in t for t in texts), f"extract_image={ei}: {texts!r}"
        assert any('BETA-para-two' in t for t in texts), f"extract_image={ei}: {texts!r}"
        # Not merged into a single entry.
        assert not any(('ALPHA-para-one' in t and 'BETA-para-two' in t) for t in texts), \
            f"extract_image={ei}: paragraphs merged: {texts!r}"


def test_pre_block_preserves_newlines_both_paths():
    """<pre> must be ONE entry with internal newlines PRESERVED (code block)."""
    html = ('<html><head><title>T</title></head><body>'
            '<pre>line-one\nline-two\nline-three</pre>'
            '</body></html>')
    for ei in (True, False):
        result = _parse(html, extract_image=ei, base_url='http://x/p.html')
        texts = _text_entries(result)
        assert len(texts) == 1, f"extract_image={ei}: expected 1 entry, got {texts!r}"
        entry = texts[0]
        # Newlines preserved (not collapsed to spaces).
        assert 'line-one\nline-two\nline-three' in entry, f"extract_image={ei}: {entry!r}"


def test_br_inside_p_single_entry_both_paths():
    """<p>a<br>b</p> stays ONE entry with the two parts separated (not merged, not split).

    <br> is a soft line break; internal newlines collapse to spaces on flush, so the two
    parts appear in one entry separated by whitespace — NOT two entries, and NOT run
    together with no separator.
    """
    html = ('<html><head><title>T</title></head><body>'
            '<p>FIRSTline<br>SECONDline</p>'
            '</body></html>')
    for ei in (True, False):
        result = _parse(html, extract_image=ei, base_url='http://x/p.html')
        texts = _text_entries(result)
        assert len(texts) == 1, f"extract_image={ei}: expected 1 entry, got {texts!r}"
        entry = texts[0]
        # Both parts present and separated by whitespace (a space from <br>), not fused.
        assert 'FIRSTline' in entry and 'SECONDline' in entry
        assert 'FIRSTlineSECONDline' not in entry  # not run together with no separator


def test_wrapped_p_with_inline_single_entry_both_paths():
    """Wrapped <p> WITH inline tags stays ONE entry (combines newline + inline fixes)."""
    html = ('<html><head><title>T</title></head><body>'
            '<p>This module provides a portable way of using '
            '\noperating system dependent functionality.  If you want to '
            'read a file see <a href="#open"><code>open()</code></a>, if you want '
            '\nto manipulate paths, see the os.path module.</p>'
            '</body></html>')
    for ei in (True, False):
        result = _parse(html, extract_image=ei, base_url='http://x/p.html')
        texts = _text_entries(result)
        assert len(texts) == 1, f"extract_image={ei}: expected 1 entry, got {texts!r}"
        joined = texts[0]
        for frag in ('portable way', 'open()', 'os.path module'):
            assert frag in joined


def test_parse_html_bs_inline_tags_do_not_fragment_paragraph():
    """A single <p> with many inline <a>/<code>/<span> must stay ONE text entry.

    Regression: the old _walk flushed after EVERY element (incl. inline tags), so a
    paragraph dense with code refs fragmented into many tiny entries like ', if'.
    """
    html = ('<html><head><title>Doc</title></head><body>'
            '<p>This module provides a portable way of using operating system dependent '
            'functionality.  If you just want to read or write a file see '
            '<a href="#open"><code>open()</code></a>, if you want to manipulate paths, '
            'see the <a href="#os.path"><code>os.path</code></a> module, and if you want '
            'to read all the lines see the <a href="#fileinput"><code>fileinput</code></a> '
            'module.</p>'
            '</body></html>')
    result = _parse(html, base_url='http://example.com/page.html')
    texts = _text_entries(result)
    # The whole paragraph must be a single entry (not split at each inline tag).
    assert len(texts) == 1
    joined = texts[0]
    for fragment in ('portable way', 'open()', 'os.path', 'fileinput', 'module.'):
        assert fragment in joined


def test_parse_html_bs_deeply_nested_inline_single_entry():
    """Deeply nested inline tags (<a><span><code>) still yield one entry."""
    html = ('<html><head><title>Nested</title></head><body>'
            "<p>before <a href='#'><span><code>x()</code></span></a> after</p>"
            '</body></html>')
    result = _parse(html, base_url='http://example.com/p.html')
    texts = _text_entries(result)
    assert len(texts) == 1
    assert 'before' in texts[0] and 'x()' in texts[0] and 'after' in texts[0]


def test_parse_html_bs_nested_block_in_block_stays_separate():
    """<div><p>a</p><p>b</p></div> -> two entries, not one merged."""
    html = ('<html><head><title>Blocks</title></head><body>'
            '<div><p>ALPHA-para-one</p><p>BETA-para-two</p></div>'
            '</body></html>')
    result = _parse(html, base_url='http://example.com/p.html')
    texts = _text_entries(result)
    assert any('ALPHA-para-one' in t for t in texts)
    assert any('BETA-para-two' in t for t in texts)
    # They must be separate entries (not merged into one).
    assert not any(('ALPHA-para-one' in t and 'BETA-para-two' in t) for t in texts)


def test_parse_html_bs_toplevel_inline_element_not_lost():
    """A top-level inline element (<span> directly under body) must not lose its text.

    Regression guard for the content-loss safeguard: _walk no longer flushes inline
    elements, so the top-level loop must flush any leftover buffer itself.
    """
    html = ('<html><head><title>TopInline</title></head><body>'
            '<span>TOPLEVEL-INLINE-VISIBLE</span>'
            '</body></html>')
    result = _parse(html, base_url='http://example.com/p.html')
    texts = _text_entries(result)
    assert any('TOPLEVEL-INLINE-VISIBLE' in t for t in texts)


def test_parse_html_bs_image_inside_inline_element_ordering():
    """<p>before <a><img></a> after</p> -> text-before, image, text-after in order."""
    html = ('<html><head><title>ImgInline</title></head><body>'
            '<p>BEFORE-TEXT <a href="#"><img src="x.png" alt="inlink"></a> AFTER-TEXT</p>'
            '</body></html>')
    result = _parse(html, base_url='http://example.com/p.html')
    seq = [(k, v) for item in result[0]['content'] for k, v in item.items()]
    kinds = [k for k, _ in seq]
    assert 'image' in kinds
    img_idx = kinds.index('image')
    text_before = ' '.join(v for k, v in seq[:img_idx])
    text_after = ' '.join(v for k, v in seq[img_idx + 1:])
    assert 'BEFORE-TEXT' in text_before
    assert 'AFTER-TEXT' in text_after


def test_parse_html_bs_unknown_custom_tag_treated_inline():
    """An unknown/custom tag wrapping a paragraph is treated as inline (no flush)."""
    html = ('<html><head><title>Custom</title></head><body>'
            '<customwrap><p>CUSTOM-WRAP-para</p></customwrap>'
            '</body></html>')
    result = _parse(html, base_url='http://example.com/p.html')
    texts = _text_entries(result)
    assert any('CUSTOM-WRAP-para' in t for t in texts)


def test_parse_html_bs_relative_src_resolved_against_base_url():
    """src='img/x.png' on a page from http://example.com/a/b.html -> /a/img/x.png."""
    html = ('<html><head><title>Rel Page</title></head><body>'
            '<img src="img/x.png" alt="rel">'
            '</body></html>')
    result = _parse(html, base_url='http://example.com/a/b.html')
    images = [v for item in result[0]['content'] for v in item.values() if 'image' in item]
    assert images == ['![rel](http://example.com/a/img/x.png)']


def test_parse_html_bs_relative_src_resolved_against_base_href_tag():
    """With no base_url (e.g. a local file), a relative src resolves against <base href>."""
    html = ('<html><head>'
            '<base href="http://cdn.example.com/assets/">'
            '<title>BaseHref Page</title></head><body>'
            '<img src="pic/y.png" alt="via-base">'
            '</body></html>')
    # base_url=None -> _resolve_image_src falls back to the <base href> in the doc.
    result = _parse(html, base_url=None)
    images = [v for item in result[0]['content'] for v in item.values() if 'image' in item]
    assert images == ['![via-base](http://cdn.example.com/assets/pic/y.png)']


def test_parse_html_bs_data_uri_skipped():
    """<img src='data:...'> is not included."""
    html = ('<html><head><title>Data Page</title></head><body>'
            '<p>TEXT-ONLY-VISIBLE</p>'
            '<img src="data:image/png;base64,iVBORw0KGgoAAAANS" alt="inline">'
            '</body></html>')
    result = _parse(html, base_url='http://example.com/p.html')
    content = result[0]['content']
    assert all('image' not in item for item in content)
    assert any('TEXT-ONLY-VISIBLE' in item.get('text', '') for item in content)


def test_parse_html_bs_missing_src_skipped():
    """<img> without src (and with empty src) is skipped without error."""
    html = ('<html><head><title>NoSrc Page</title></head><body>'
            '<p>REAL-TEXT</p>'
            '<img alt="no-src">'
            '<img src="" alt="empty-src">'
            '</body></html>')
    result = _parse(html, base_url='http://example.com/p.html')
    content = result[0]['content']
    assert all('image' not in item for item in content)
    assert any('REAL-TEXT' in item.get('text', '') for item in content)


def test_parse_html_bs_boilerplate_images_stripped():
    """Images inside <nav> or a stripped banner header do NOT appear; content-root ones DO."""
    html = ('<html><head><title>Banner Page</title></head><body>'
            '<header role="banner"><img src="logo.png" alt="logo"></header>'
            '<nav><img src="nav-icon.png" alt="nav-icon"></nav>'
            '<main><p>MAIN-TEXT</p>'
            '<img src="content.png" alt="content-img">'
            '</main>'
            '</body></html>')
    result = _parse(html, base_url='http://example.com/p.html')
    content = result[0]['content']
    images = [v for item in content for v in item.values() if 'image' in item]
    assert images == ['![content-img](http://example.com/content.png)']
    joined = '\n'.join(str(v) for item in content for v in item.values())
    assert 'logo' not in joined
    assert 'nav-icon' not in joined


def test_parse_html_bs_duplicate_images_deduped():
    """Two <img> with the same resolved src collapse to ONE entry (per-page dedup).

    Regression for commit 427ca0b: responsive/lazy-load pages emit the same image
    multiple times; parse_html_bs must emit it once. Distinct URLs are unaffected.
    """
    html = ('<html><head><title>Dup Page</title></head><body>'
            '<p>FIRST-POS</p>'
            '<img src="x.png" alt="dup">'
            '<p>MIDDLE</p>'
            '<img src="x.png" alt="dup">'
            '<img src="y.png" alt="other">'
            '</body></html>')
    result = _parse(html, base_url='http://example.com/p.html')
    content = result[0]['content']
    images = [v for item in content for v in item.values() if 'image' in item]
    # The duplicated x.png appears once; the distinct y.png is preserved. Order follows
    # document order: x (first seen) then y.
    assert images == ['![dup](http://example.com/x.png)', '![other](http://example.com/y.png)']


def test_parse_html_bs_no_images_text_only_regression():
    """Text-only page with extract_image=True: no image entries, text unchanged."""
    html = ('<html><head><title>Plain Page</title></head><body>'
            '<p>PARA-ONE.</p>'
            '<p>PARA-TWO.</p>'
            '</body></html>')
    result = _parse(html, extract_image=True, base_url='http://example.com/p.html')
    content = result[0]['content']
    assert all('image' not in item for item in content)
    texts = [item['text'] for item in content]
    joined = '\n'.join(texts)
    assert 'PARA-ONE.' in joined
    assert 'PARA-TWO.' in joined


def test_parse_html_bs_extract_image_false_omits_images():
    """extract_image=False: images omitted, text still present."""
    html = ('<html><head><title>Off Page</title></head><body>'
            '<p>TEXT-ONLY-VISIBLE</p>'
            '<img src="https://example.com/hidden.png" alt="hidden">'
            '</body></html>')
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
    html = ('<html><head><title>Full Path Page</title></head><body>'
            '<p>Before figure.</p>'
            '<img src="https://example.com/fig.png" alt="Fig">'
            '<p>After figure.</p>'
            '</body></html>')
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
    html = '<html><head><title>Plain</title></head><body><p>No math here, just text.</p></body></html>'
    result = _parse(html, extract_image=False)
    joined = ' '.join(item.get('text', '') for item in result[0]['content'])
    assert 'No math here, just text.' in joined


def test_mathml_and_image_together_in_order():
    """Math collapses AND an image stays inline, both in reading order."""
    html = ('<html><head><title>Both</title></head><body>'
            "<p>cost is <math alttext='{\\displaystyle O(1)}'><mi>O</mi><mo>(</mo><mi>1</mi><mo>)</mo></math> here.</p>"
            '<img src="https://example.com/x.png" alt="X">'
            '<p>done.</p></body></html>')
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
    html = ('<html><head><title>Infobox</title></head><body>'
            "<table class='infobox'>"
            '<tr><th>Kingdom:</th><td>Animalia</td></tr>'
            '<tr><th>Phylum:</th><td>Chordata</td></tr>'
            '<tr><th>Class:</th><td>Mammalia</td></tr>'
            '</table>'
            '<p>Prose paragraph.</p>'
            '</body></html>')
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
    html = ('<html><head><title>Data</title></head><body>'
            '<table>'
            '<tr><th>Name</th><th>Age</th><th>City</th></tr>'
            '<tr><td>Alice</td><td>30</td><td>Paris</td></tr>'
            '</table>'
            '</body></html>')
    result = _parse(html, extract_image=False)
    joined = ' '.join(item.get('text', '') for item in result[0]['content'])
    # Header row and data row are pipe-joined single lines.
    assert 'Name | Age | City' in joined
    assert 'Alice | 30 | Paris' in joined


def test_table_td_label_with_trailing_colon():
    """Wikipedia taxonomy rows use <td>Label:</td><td>Value</td> (no <th>) — must be label:value."""
    html = ('<html><head><title>Taxonomy</title></head><body>'
            "<table class='infobox'>"
            '<tr><td>Kingdom:</td><td>Animalia</td></tr>'
            '<tr><td>Phylum:</td><td>Chordata</td></tr>'
            '</table>'
            '</body></html>')
    result = _parse(html, extract_image=False)
    joined = ' '.join(item.get('text', '') for item in result[0]['content'])
    # Colon-ending <td> label -> 'Label: Value' (no doubled pipe separator).
    assert 'Kingdom: Animalia' in joined
    assert 'Phylum: Chordata' in joined
    assert 'Kingdom: | Animalia' not in joined


def test_table_no_regression_prose_only():
    """A page with no tables is unaffected by the table-aware extraction."""
    html = ('<html><head><title>Plain</title></head><body>'
            '<p>First paragraph.</p>'
            '<p>Second paragraph.</p>'
            '</body></html>')
    result = _parse(html, extract_image=False)
    joined = ' '.join(item.get('text', '') for item in result[0]['content'])
    assert 'First paragraph.' in joined
    assert 'Second paragraph.' in joined


def test_table_with_image_in_table_preserved_on_image_path():
    """The image path still extracts an image that lives inside a table (no DOM mutation)."""
    html = ('<html><head><title>Table+Img</title></head><body>'
            '<table><tr><td>Label</td><td>'
            '<img src="https://example.com/in-table.png" alt="InTable">'
            '</td></tr></table>'
            '</body></html>')
    result = _parse(html, extract_image=True, base_url='http://example.com/p.html')
    imgs = [item.get('image', '') for item in result[0]['content'] if 'image' in item]
    assert '![InTable](https://example.com/in-table.png)' in imgs


# ---------------------------------------------------------------------------
# Admonition label merging (Note / See also / Warning -> one entry with body)
# ---------------------------------------------------------------------------

def test_admonition_note_label_merged_with_body():
    """A Sphinx admonition label ('Note') merges into its body as 'Note: <body>'."""
    html = ('<html><head><title>Adm</title></head><body>'
            '<p>Intro paragraph.</p>'
            '<div class="admonition note">'
            '<p class="admonition-title">Note</p>'
            '<p>All functions raise OSError on invalid input.</p>'
            '</div>'
            '<p>Trailing paragraph.</p>'
            '</body></html>')
    result = _parse(html, extract_image=True)
    texts = [item.get('text', '') for item in result[0]['content']]
    # The label must NOT be a standalone entry.
    assert 'Note' not in texts, f"standalone 'Note' entry found: {texts!r}"
    # The merged entry carries the label prefix + body in ONE entry.
    assert any(t.strip().startswith('Note:') and 'OSError' in t for t in texts), \
        f"no merged 'Note: ...' entry: {texts!r}"


def test_admonition_see_also_and_warning_merged():
    """'See also' and 'Warning' labels also merge into their following body."""
    html = ('<html><head><title>Adm2</title></head><body>'
            '<div class="admonition seealso">'
            '<p class="admonition-title">See also</p>'
            '<p>The os.reload_environ() function.</p>'
            '</div>'
            '<div class="admonition warning">'
            '<p class="admonition-title">Warning</p>'
            '<p>This function is not thread-safe.</p>'
            '</div>'
            '</body></html>')
    result = _parse(html, extract_image=True)
    texts = [item.get('text', '') for item in result[0]['content']]
    assert any(t.strip().startswith('See also:') and 'reload_environ' in t for t in texts), \
        f"no merged 'See also: ...' entry: {texts!r}"
    assert any(t.strip().startswith('Warning:') and 'thread-safe' in t for t in texts), \
        f"no merged 'Warning: ...' entry: {texts!r}"


def test_admonition_label_does_not_leak_across_blocks():
    """A label only prefixes the NEXT text entry; it never leaks into a later block."""
    html = ('<html><head><title>Adm3</title></head><body>'
            '<div class="admonition note">'
            '<p class="admonition-title">Note</p>'
            '<p>First body.</p>'
            '</div>'
            '<p>Unrelated paragraph that must NOT be prefixed.</p>'
            '</body></html>')
    result = _parse(html, extract_image=True)
    texts = [item.get('text', '') for item in result[0]['content']]
    # The unrelated paragraph must not carry the 'Note:' prefix.
    assert any(t.strip() == 'Unrelated paragraph that must NOT be prefixed.' for t in texts), \
        f"unrelated paragraph was altered: {texts!r}"
    # And the label applied to exactly one entry.
    prefixed = [t for t in texts if t.strip().startswith('Note:')]
    assert len(prefixed) == 1, f"label leaked into multiple entries: {texts!r}"


def test_admonition_label_not_leaked_when_body_is_image():
    """If a non-text boundary (image) follows the label, the label is dropped, not leaked."""
    html = ('<html><head><title>Adm4</title></head><body>'
            '<div class="admonition note">'
            '<p class="admonition-title">Note</p>'
            '<img src="https://example.com/x.png" alt="X">'
            '</div>'
            '<p>Later text must not be prefixed.</p>'
            '</body></html>')
    result = _parse(html, extract_image=True)
    texts = [item.get('text', '') for item in result[0]['content']]
    # No entry should carry a stray 'Note:' prefix (the image consumed the boundary).
    assert not any(t.strip().startswith('Note:') for t in texts), \
        f"label leaked past an image: {texts!r}"


def test_admonition_label_not_leaked_when_body_empty():
    """An admonition with NO text body must not leak its label to a following paragraph."""
    html = ('<html><head><title>Adm5</title></head><body>'
            '<div class="admonition note">'
            '<p class="admonition-title">Note</p>'
            '</div>'
            '<p>Next paragraph must not be prefixed.</p>'
            '</body></html>')
    result = _parse(html, extract_image=True)
    texts = [item.get('text', '') for item in result[0]['content']]
    assert any(t.strip() == 'Next paragraph must not be prefixed.' for t in texts), \
        f"label leaked into following paragraph: {texts!r}"
    assert not any(t.strip().startswith('Note:') for t in texts), \
        f"stray 'Note:' prefix found: {texts!r}"


def test_admonition_label_not_leaked_across_nested_siblings():
    """A label inside a nested empty admonition must not leak to a sibling block's text."""
    html = ('<html><head><title>Adm6</title></head><body>'
            '<div>'
              '<div class="admonition note"><p class="admonition-title">Note</p></div>'
              '<p>Sibling text must not be prefixed.</p>'
            '</div>'
            '</body></html>')
    result = _parse(html, extract_image=True)
    texts = [item.get('text', '') for item in result[0]['content']]
    assert any(t.strip() == 'Sibling text must not be prefixed.' for t in texts), \
        f"label leaked across nested siblings: {texts!r}"


def test_admonition_sphinx_sibling_blocks_merge():
    """Real Sphinx structure: title and body are SEPARATE sibling <p> blocks in one div.

    The label must still merge into the body even though they're not inline — the body's
    own block-end flush consumes the stashed label before the enclosing div resets it.
    """
    html = ('<html><head><title>Adm7</title></head><body>'
            '<p>Intro.</p>'
            '<div class="admonition note">'
            '<p class="admonition-title">Note</p>'
            '<p>The body of the note lives in its own paragraph.</p>'
            '</div>'
            '<p>Trailing.</p>'
            '</body></html>')
    result = _parse(html, extract_image=True)
    texts = [item.get('text', '') for item in result[0]['content']]
    assert any(t.strip().startswith('Note:') and 'body of the note' in t for t in texts), \
        f"sibling-block merge failed: {texts!r}"
    assert any(t.strip() == 'Trailing.' for t in texts), \
        f"trailing paragraph altered: {texts!r}"
