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

"""Scoped tests for the deferred-tool (MCP) discovery BM25 ranker + JSON result envelope.

Constants are imported from the module under test (single source of truth; no magic values
per AGENTS.md §Test discipline)."""

import json

from agent_cascade.tools.search_tools.tool_search import (
    DISCOVERED_KEY,
    TOOL_SEARCH_ENVELOPE_KEY,
    ToolSearch,
    _tokenize,
    rank_deferred_tools,
)


def test_tokenize_splits_identifier_boundaries():
    # AC MCP names embed the server inline (server-tool); the tokenizer must split on '-'/'_'.
    tokens = _tokenize('srv-read_file')
    assert {'srv', 'read', 'file'} <= set(tokens)


def test_name_match_beats_repeated_text_mentions():
    # A query term in the NAME (weighted _NAME_WEIGHT times IDF) must outrank one mentioned
    # many times in the description only.
    docs = [
        {'name': 'srv-look', 'text': 'search search search search search search window'},
        {'name': 'srv-search', 'text': 'nothing related here at all'},
    ]
    ranked = rank_deferred_tools('search', docs, limit=5)
    assert ranked[0] == 'srv-search'


def test_exact_name_query_ranks_first():
    docs = [
        {'name': 'other-search', 'text': 'search search'},
        {'name': 'srv-search', 'text': 'a search tool'},
    ]
    # Whole-name exact query -> 'srv-search' first despite the other doc mentioning the term.
    ranked = rank_deferred_tools('srv-search', docs, limit=5)
    assert ranked[0] == 'srv-search'


def test_local_name_after_dash_bonus_applied():
    # The tool whose full local name (after the last '-') is fully covered by the query wins.
    docs = [
        {'name': 'srv-do-thing', 'text': 'performs the thing'},
        {'name': 'misc-thing', 'text': 'does a thing'},
    ]
    ranked = rank_deferred_tools('do thing', docs, limit=5)
    assert ranked[0] == 'srv-do-thing'


def test_empty_query_returns_empty():
    assert rank_deferred_tools('', [{'name': 'a', 'text': 'b'}], limit=5) == []


def test_no_matching_docs_returns_empty():
    assert rank_deferred_tools('zzzz', [{'name': 'a', 'text': 'b'}], limit=5) == []


def test_limit_is_honored():
    docs = [{'name': f'x-tool{i}', 'text': 'tool'} for i in range(10)]
    limit = 3
    ranked = rank_deferred_tools('tool', docs, limit=limit)
    assert len(ranked) == limit


def test_envelope_is_trailing_parseable_json():
    loaded = ['srv-a', 'srv-b']
    result = ToolSearch()._format_result(loaded, 'human-readable preamble line')
    # The last line must be standalone-valid JSON carrying the documented envelope shape.
    last_line = result.splitlines()[-1]
    parsed = json.loads(last_line)
    assert parsed[TOOL_SEARCH_ENVELOPE_KEY][DISCOVERED_KEY] == loaded


def test_call_without_context_returns_empty_envelope():
    # No agent_obj.pool / instance resolvable -> degrade to an empty envelope, never raise.
    result = ToolSearch().call({'query': 'anything'})
    parsed = json.loads(result.splitlines()[-1])
    assert parsed[TOOL_SEARCH_ENVELOPE_KEY][DISCOVERED_KEY] == []


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__]))
