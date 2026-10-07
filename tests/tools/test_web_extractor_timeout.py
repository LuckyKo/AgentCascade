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

"""Tests for the web_extractor / SimpleDocParser parse-timeout guard.

The CPU-bound parse step (pdfminer/pdfplumber etc.) is bounded by a wall-clock budget
enforced in a shared worker pool (simple_doc_parser._PARSE_EXECUTOR). On timeout the
parse is abandoned and a clean DocParserError containing "timed out" is raised; at the
web_extractor level that surfaces as a one-line "Failed to fetch ..." string.

All tests use dummy files (extension-based type detection) with the real parsers
mocked — no network, no real PDF parsing. Budgets are patched on the settings MODULE
(caller reads them at call time, so attribute monkeypatching takes effect).

Run serially (orphaned worker threads make xdist flaky here):
    python -m pytest tests/tools/test_web_extractor_timeout.py -o addopts="" -q
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade import settings
from agent_cascade.tools.simple_doc_parser import DocParserError, SimpleDocParser
from agent_cascade.tools.storage import KeyNotExistsError
from agent_cascade.tools.web_extractor import WebExtractor
from agent_cascade.utils.utils import hash_sha256


def _make_pdf(tmp_path, name='big.pdf') -> str:
    """Create a dummy .pdf file (content irrelevant — the real parser is mocked)."""
    p = tmp_path / name
    p.write_bytes(b'%PDF-1.4 dummy bytes for parse-timeout test')
    return str(p)


def _make_parser(tmp_path) -> SimpleDocParser:
    """Parser with an isolated Storage cache per test."""
    return SimpleDocParser(cfg={'work_dir': str(tmp_path), 'path': str(tmp_path / 'db')})


def _slow_pdf(path, extract_image=False):
    time.sleep(8)  # Simulates a multi-minute pathological parse (no real hang)
    return [{'page_num': 1, 'content': [{'text': 'late'}]}]


def _fast_pdf(path, extract_image=False):
    return [{'page_num': 1, 'content': [{'text': 'hello world'}]}]


def test_pdf_parse_timeout_returns_clean_error_within_budget(tmp_path, monkeypatch):
    """CORE: a slow PDF parse times out at the budget with a clean DocParserError."""
    monkeypatch.setattr('agent_cascade.settings.WEB_EXTRACTOR_PARSE_TIMEOUT_BY_TYPE', {'pdf': 2.0})
    monkeypatch.setattr('agent_cascade.tools.simple_doc_parser.parse_pdf', _slow_pdf)
    parser = _make_parser(tmp_path)
    pdf = _make_pdf(tmp_path)

    start = time.time()
    with pytest.raises(DocParserError) as ei:
        parser.call({'url': pdf})
    elapsed = time.time() - start

    assert elapsed < 4.0, f'Expected timeout at ~2s budget, took {elapsed:.1f}s'
    assert 'timed out' in str(ei.value).lower()


def test_fast_parse_unaffected(tmp_path, monkeypatch):
    """A fast parse returns its content normally (no timeout, cache path intact)."""
    monkeypatch.setattr('agent_cascade.tools.simple_doc_parser.parse_pdf', _fast_pdf)
    parser = _make_parser(tmp_path)
    pdf = _make_pdf(tmp_path)

    result = parser.call({'url': pdf})

    assert 'hello world' in result
    # Normal flow reached the cache put: a second call now hits the cache.
    assert parser.db.get(f'{hash_sha256(os.path.normpath(pdf))}_ori') is not None


def test_non_pdf_type_gets_its_own_budget(tmp_path, monkeypatch):
    """The per-type budget dict is consulted: html uses its own (1s) budget, not the default."""
    monkeypatch.setattr('agent_cascade.settings.WEB_EXTRACTOR_PARSE_TIMEOUT_BY_TYPE', {'html': 1.0})

    def _slow_html(path, extract_image=False, base_url=None):
        time.sleep(5)
        return []

    monkeypatch.setattr('agent_cascade.tools.simple_doc_parser.parse_html_bs', _slow_html)
    parser = _make_parser(tmp_path)
    html = tmp_path / 'page.html'
    html.write_text('<html><body><p>dummy</p></body></html>')

    start = time.time()
    with pytest.raises(DocParserError) as ei:
        parser.call({'url': str(html)})
    elapsed = time.time() - start

    # Times out at the 1s html budget (a 5s sleep would NOT time out at the 30s default).
    assert elapsed < 3.0, f'Expected timeout at ~1s html budget, took {elapsed:.1f}s'
    assert 'timed out' in str(ei.value).lower()


def test_orphaned_thread_still_returns_promptly_and_pool_stays_usable(tmp_path, monkeypatch):
    """After a timeout (orphan keeps running), a fast parse on another worker returns promptly."""
    monkeypatch.setattr('agent_cascade.settings.WEB_EXTRACTOR_PARSE_TIMEOUT_BY_TYPE', {'pdf': 2.0})
    monkeypatch.setattr('agent_cascade.tools.simple_doc_parser.parse_pdf', _slow_pdf)
    parser = _make_parser(tmp_path)
    slow_pdf = _make_pdf(tmp_path, 'slow.pdf')

    with pytest.raises(DocParserError):
        parser.call({'url': slow_pdf})  # Times out; orphan thread keeps sleeping

    # Swap in the fast parser: must run on a spare worker and return promptly.
    monkeypatch.setattr('agent_cascade.tools.simple_doc_parser.parse_pdf', _fast_pdf)
    fast_pdf = _make_pdf(tmp_path, 'fast.pdf')
    start = time.time()
    result = parser.call({'url': fast_pdf})
    elapsed = time.time() - start

    assert 'hello world' in result
    assert elapsed < 2.0, f'Spare worker should be available; fast parse took {elapsed:.1f}s'


def test_timeout_does_not_write_cache(tmp_path, monkeypatch):
    """On timeout the db.put at the end of call() never runs — no cache entry is written."""
    monkeypatch.setattr('agent_cascade.settings.WEB_EXTRACTOR_PARSE_TIMEOUT_BY_TYPE', {'pdf': 2.0})
    monkeypatch.setattr('agent_cascade.tools.simple_doc_parser.parse_pdf', _slow_pdf)
    parser = _make_parser(tmp_path)
    pdf = _make_pdf(tmp_path)

    with pytest.raises(DocParserError):
        parser.call({'url': pdf})

    with pytest.raises(KeyNotExistsError):
        parser.db.get(f'{hash_sha256(os.path.normpath(pdf))}_ori')


def test_web_extractor_timeout_returns_clean_string():
    """End-to-end: a parse-timeout DocParserError becomes a clean one-line tool result."""
    with patch('agent_cascade.tools.web_extractor.SimpleDocParser') as mock_cls:
        mock_cls.return_value.call.side_effect = DocParserError(
            code='TimeoutError', message='Document parsing timed out after 120s (file type: pdf)')
        result = WebExtractor(cfg={'work_dir': ''}).call({'url': 'https://x/big.pdf'})

    assert 'Failed to fetch https://x/big.pdf' in result
    assert 'Traceback' not in result
    assert 'simple_doc_parser.py' not in result
    assert 'document parsing timed out' in result


def test_describe_fetch_error_maps_parse_timeout():
    """_describe_fetch_error maps the parse-timeout error to the friendly reason."""
    err = DocParserError(code='TimeoutError',
                         message='Document parsing timed out after 120s (file type: pdf)')
    desc = WebExtractor._describe_fetch_error(err)
    assert desc == 'document parsing timed out (file is too large or too complex to parse in time)'
