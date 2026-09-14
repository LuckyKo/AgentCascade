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

import os
import tempfile

from agent_cascade.tools import SimpleDocParser
from agent_cascade.tools.simple_doc_parser import parse_html_bs, get_plain_doc


def test_simple_doc_parser():
    tool = SimpleDocParser()
    res = tool.call({'url': 'https://qianwen-res.oss-cn-beijing.aliyuncs.com/QWEN_TECHNICAL_REPORT.pdf'})
    print(res)


def _write_temp_html(html: str) -> str:
    """Write HTML to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix='.html')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(html)
    return path


def test_parse_html_bs_main_content_stripping():
    """<main> content kept; <nav>, role=banner header stripped."""
    html = ('<html><head><title>Test Title</title></head><body>'
            '<nav>NAV-NOISE</nav>'
            '<header role=banner>BANNER-NOISE</header>'
            '<main>MAIN-CONTENT</main>'
            '<footer>FOOT-METADATA</footer>'
            '</body></html>')
    path = _write_temp_html(html)
    try:
        result = parse_html_bs(path)
        text = result[0]['content'][0]['text']
        assert 'MAIN-CONTENT' in text
        assert 'NAV-NOISE' not in text
        assert 'BANNER-NOISE' not in text
        # When <main> exists, only main content is extracted (footer is outside)
        assert 'FOOT-METADATA' not in text
    finally:
        os.unlink(path)


def test_parse_html_bs_fallback_to_body():
    """Page without <main>: falls back to body; nav still stripped."""
    html = ('<html><head><title>Fallback Page</title></head><body>'
            '<nav>NAV-NOISE</nav>'
            '<p>PARA-ONE</p>'
            '<p>PARA-TWO</p>'
            '</body></html>')
    path = _write_temp_html(html)
    try:
        result = parse_html_bs(path)
        text = ' '.join(item['text'] for item in result[0]['content'])
        assert 'PARA-ONE' in text
        assert 'PARA-TWO' in text
        assert 'NAV-NOISE' not in text
    finally:
        os.unlink(path)


def test_strip_boilerplate_nested_role_elements_no_crash():
    """Regression: nested role-bearing elements must not crash _strip_boilerplate.

    The docs.python.org page triggered AttributeError: 'NoneType' object has no attribute
    'get' because decomposing a parent during find_all iteration left its children with
    attrs=None, and a later .get('role') on them crashed. This is the exact repro shape.
    """
    html = ('<html><head><title>Nested Roles</title></head><body>'
            '<div role="navigation">'
            '  <span role="banner">NAV-INNER</span>'
            '  <a href="#">link</a>'
            '</div>'
            '<p>REAL-CONTENT</p>'
            '</body></html>')
    path = _write_temp_html(html)
    try:
        result = parse_html_bs(path)  # must NOT raise
        text = ' '.join(item['text'] for item in result[0]['content'])
        assert 'REAL-CONTENT' in text
        assert 'NAV-INNER' not in text  # nested role element stripped, no crash
    finally:
        os.unlink(path)


def test_strip_boilerplate_sibling_role_elements():
    """Multiple sibling role-bearing elements where the first is stripped — second still processed."""
    html = ('<html><head><title>Sibling Roles</title></head><body>'
            '<div role="banner">FIRST-BANNER</div>'
            '<div role="navigation">SECOND-NAV</div>'
            '<p>KEEP-ME</p>'
            '</body></html>')
    path = _write_temp_html(html)
    try:
        result = parse_html_bs(path)  # must NOT raise
        text = ' '.join(item['text'] for item in result[0]['content'])
        assert 'KEEP-ME' in text
        assert 'FIRST-BANNER' not in text
        assert 'SECOND-NAV' not in text
    finally:
        os.unlink(path)


def test_collapse_math_nested_no_crash():
    """Nested <math> elements must not crash _collapse_math (two-phase snapshot)."""
    html = ('<html><head><title>Nested Math</title></head><body>'
            '<p>x is '
            '<math alttext="{\\displaystyle a}"><mi>a</mi>'
            '  <math alttext="{\\displaystyle b}"><mi>b</mi></math>'
            '</math> here.</p>'
            '</body></html>')
    path = _write_temp_html(html)
    try:
        result = parse_html_bs(path, extract_image=False)  # must NOT raise
        text = ' '.join(item.get('text', '') for item in result[0]['content'])
        assert 'here' in text
    finally:
        os.unlink(path)


def test_parse_html_bs_header_with_nav_fallback():
    """Header containing a <nav> is stripped entirely (including non-nav content).

    This is the case that exposed the ordering bug: if navs are decomposed
    before the header check, find('nav') returns None and the header leaks.
    """
    html = ('<html><head><title>Header Nav Page</title></head><body>'
            '<header><nav>NAV INSIDE HEADER</nav><h1>Header Content</h1></header>'
            '<p>BODY-PARA-ONE</p>'
            '<p>BODY-PARA-TWO</p>'
            '</body></html>')
    path = _write_temp_html(html)
    try:
        result = parse_html_bs(path)
        text = ' '.join(item['text'] for item in result[0]['content'])
        # Body paragraphs should remain
        assert 'BODY-PARA-ONE' in text
        assert 'BODY-PARA-TWO' in text
        # Header (containing a nav child) must be stripped entirely
        assert 'NAV INSIDE HEADER' not in text
        assert 'Header Content' not in text
    finally:
        os.unlink(path)


def test_get_plain_doc_title_prepended():
    """get_plain_doc prepends 'Title: <title>' exactly once."""
    doc = [{'page_num': 1, 'content': [{'text': 'Hello'}], 'title': 'Test Title'}]
    output = get_plain_doc(doc)
    assert output.startswith('Title: Test Title')


def test_parse_html_bs_js_spa_note_appended():
    """Short JS shell with a SPA marker (id="root") gets the JS-rendering note."""
    html = ('<html><head><title>SPA Shell</title></head><body>'
            "<div id=\"root\"></div>"
            "<script src=\"/bundle.js\"></script>"
            '</body></html>')
    path = _write_temp_html(html)
    try:
        result = parse_html_bs(path)
        # Return shape stays intact.
        assert result[0]['page_num'] == 1
        assert result[0]['title'] == 'SPA Shell'
        texts = [item['text'] for item in result[0]['content']]
        joined = '\n'.join(texts)
        assert 'JavaScript rendering' in joined
        # The note is its own content item (last), not merged into body text.
        assert texts[-1].startswith('[Note: this page appears to require JavaScript')
    finally:
        os.unlink(path)


def test_parse_html_bs_no_note_on_server_rendered():
    """Server-rendered page with real content + scripts but NO SPA marker: no note."""
    paragraphs = ''.join(f"<p>Real paragraph {i} with enough words to fill the page.</p>"
                         for i in range(10))
    html = ('<html><head><title>Normal Page</title></head><body>'
            '<nav>NAV-NOISE</nav>'
            f"{paragraphs}"
            "<script src=\"/analytics.js\"></script>"
            '</body></html>')
    path = _write_temp_html(html)
    try:
        result = parse_html_bs(path)
        texts = [item['text'] for item in result[0]['content']]
        joined = '\n'.join(texts)
        # Enough real text + no SPA marker => the note must NOT appear.
        assert 'JavaScript rendering' not in joined
        assert all(not t.startswith('[Note:') for t in texts)
    finally:
        os.unlink(path)


if __name__ == '__main__':
    test_simple_doc_parser()
