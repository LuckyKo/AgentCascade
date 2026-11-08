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

import pytest

from agent_cascade.tools.mcp_manager import MCPManager
from agent_cascade.tools.search_tools.tool_search import (
    _CATALOG_DESC_LEN,
    _CATALOG_MAX,
    CATALOG_KEY,
    COUNT_KEY,
    DISCOVERED_KEY,
    TOOL_SEARCH_ENVELOPE_KEY,
    TRUNCATED_KEY,
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


# ── Catalog-mode helpers ──────────────────────────────────────────────────────────────
# Fakes stand in for the dispatch context only; the REAL ToolSearch instance runs its
# call()/_catalog/_format_result so catalog logic is genuinely exercised (not stubbed).


class _FakeTool:
    def __init__(self, name, description):
        self.name = name
        self.function = {
            'name': name,
            'description': description,
            'parameters': {'type': 'object', 'properties': {}},
        }


class _FakeTemplate:
    def __init__(self, function_map):
        self.function_map = function_map


class _FakePool:
    def __init__(self, instance, template):
        self._instance = instance
        self._template = template

    def get_instance(self, name):
        return self._instance if name == self._instance.instance_name else None

    def get_template(self, agent_class):
        return self._template


class _FakeAgentObj:
    """Stands in for the engine: tool_dispatcher passes agent_obj=engine (which has .pool)."""
    def __init__(self, pool):
        self.pool = pool


def _make_instance(name='inst'):
    import time
    from agent_cascade.agent_instance import AgentInstance
    now = time.monotonic()
    return AgentInstance(
        instance_name=name, agent_class='coder', conversation=[],
        created_at=now, last_activity=now, latest_marker_index=0,
    )


def _make_context(tools):
    """Wire a resolvable (agent_obj, instance_name, instance) from {name: description}.

    Returns the instance too so callers can assert ``_discovered_mcp_tools`` is unchanged
    in catalog mode (no unlock)."""
    func_map = {name: _FakeTool(name, desc) for name, desc in tools.items()}
    inst = _make_instance()
    agent_obj = _FakeAgentObj(_FakePool(inst, _FakeTemplate(func_map)))
    return agent_obj, inst.instance_name, inst


def _envelope_inner(result):
    """The trailing JSON line, unwrapped to the tool_search envelope body."""
    return json.loads(result.splitlines()[-1])[TOOL_SEARCH_ENVELOPE_KEY]


def _message(result):
    """Everything except the trailing JSON envelope line."""
    return '\n'.join(result.splitlines()[:-1])


@pytest.fixture
def registered_mcp():
    """Add names to MCPManager._registered_tool_names, restoring the original contents on
    teardown. _registered_tool_names is a CLASS-level set — leaking names would poison the
    MCP predicate for every other test module (cross-test leak)."""
    original = set(MCPManager._registered_tool_names)

    def register(*names):
        MCPManager._registered_tool_names.update(names)
        return names

    yield register
    MCPManager._registered_tool_names.clear()
    MCPManager._registered_tool_names.update(original)


def test_catalog_lists_all_mcp_tools_and_unlocks_nothing(registered_mcp):
    registered_mcp('alpha-read', 'alpha-write', 'beta-search')
    # core-calc is NOT registered -> is_mcp_tool False -> must not appear in the catalog.
    tools = {
        'alpha-read': 'Read a file from disk.',
        'alpha-write': 'Write a file to disk.',
        'beta-search': 'Search the codebase.',
        'core-calc': 'not an mcp tool',
    }
    agent_obj, inst_name, inst = _make_context(tools)
    # Pre-seed one already-discovered tool: the catalog lists ALL MCP tools (discovered or
    # not) and must leave this list untouched (no unlock).
    inst._discovered_mcp_tools = ['beta-search']
    before = list(inst._discovered_mcp_tools)

    result = ToolSearch().call({'query': ''}, agent_obj=agent_obj, agent_instance_name=inst_name)
    inner = _envelope_inner(result)

    assert inner[DISCOVERED_KEY] == []
    assert inner[CATALOG_KEY] == ['alpha-read', 'alpha-write', 'beta-search']
    assert inner[COUNT_KEY] == 3
    assert inner[TRUNCATED_KEY] is False
    # No unlock happened in catalog mode.
    assert inst._discovered_mcp_tools == before


def test_catalog_message_groups_by_server(registered_mcp):
    registered_mcp('alpha-read', 'alpha-write', 'beta-search')
    tools = {
        'alpha-read': 'Read a file from disk.',
        'alpha-write': 'Write a file to disk.',
        'beta-search': 'Search the codebase.',
    }
    agent_obj, inst_name, _ = _make_context(tools)
    # All-whitespace query also triggers catalog mode.
    result = ToolSearch().call({'query': '   '}, agent_obj=agent_obj, agent_instance_name=inst_name)
    body = _message(result)

    assert 'Available MCP tools (3 across 2 server(s)):' in body
    assert '  alpha:' in body
    assert '  beta:' in body
    assert '    - alpha-read: Read a file from disk.' in body
    assert '    - alpha-write: Write a file to disk.' in body
    assert '    - beta-search: Search the codebase.' in body


def test_catalog_description_handling_multiline_truncated_empty(registered_mcp):
    registered_mcp('srv-multi', 'srv-long', 'srv-empty')
    long_desc = 'x' * (_CATALOG_DESC_LEN + 30)
    tools = {
        'srv-multi': 'first line only\nsecond line must not appear',
        'srv-long': long_desc,
        'srv-empty': '',
    }
    agent_obj, inst_name, _ = _make_context(tools)
    result = ToolSearch().call({'query': ''}, agent_obj=agent_obj, agent_instance_name=inst_name)
    body = _message(result)

    # (a) Multi-line description -> only the stripped first line appears.
    assert '    - srv-multi: first line only' in body
    assert 'second line must not appear' not in body
    # (b) Description longer than _CATALOG_DESC_LEN -> exactly that many chars + ellipsis.
    assert f'    - srv-long: {long_desc[:_CATALOG_DESC_LEN]}…' in body
    assert long_desc not in body
    # (c) Empty description -> the ': desc' suffix is omitted (exact line, no colon).
    assert '    - srv-empty' in body.splitlines()
    assert '    - srv-empty:' not in body


def test_catalog_groups_preserve_insertion_order(registered_mcp):
    # Names chosen so alphabetical order differs from insertion order, proving the grouping
    # pins function_map insertion order (first-seen server order + within-server order), not
    # a re-sort.
    order = ['zeta-b', 'alpha-x', 'zeta-a', 'alpha-y']
    registered_mcp(*order)
    tools = {n: 'a tool' for n in order}
    agent_obj, inst_name, _ = _make_context(tools)
    result = ToolSearch().call({'query': ''}, agent_obj=agent_obj, agent_instance_name=inst_name)
    lines = _message(result).splitlines()

    def index_of(prefix):
        return next(i for i, line in enumerate(lines) if line.startswith(prefix))

    # Server headers in first-seen order: zeta before alpha (alphabetical would flip this).
    assert index_of('  zeta:') < index_of('  alpha:')
    # Within zeta: b before a (alphabetical would flip this).
    assert index_of('    - zeta-b') < index_of('    - zeta-a')
    # Within alpha: x before y.
    assert index_of('    - alpha-x') < index_of('    - alpha-y')


def test_catalog_no_dash_name_uses_whole_name_as_server(registered_mcp):
    # Spec edge case: a tool name with no '-' separator -> server is the whole name.
    registered_mcp('standalone')
    tools = {'standalone': 'a tool with no server prefix'}
    agent_obj, inst_name, _ = _make_context(tools)
    result = ToolSearch().call({'query': ''}, agent_obj=agent_obj, agent_instance_name=inst_name)
    inner = _envelope_inner(result)
    body = _message(result)

    assert inner[CATALOG_KEY] == ['standalone']
    assert '  standalone:' in body
    assert '    - standalone: a tool with no server prefix' in body


def test_catalog_truncates_at_cap(registered_mcp):
    total = _CATALOG_MAX + 5
    names = [f'alpha-tool{i}' for i in range(total)]
    registered_mcp(*names)
    tools = {n: 'a generic tool' for n in names}
    agent_obj, inst_name, _ = _make_context(tools)

    result = ToolSearch().call({'query': ''}, agent_obj=agent_obj, agent_instance_name=inst_name)
    inner = _envelope_inner(result)

    assert inner[TRUNCATED_KEY] is True
    assert len(inner[CATALOG_KEY]) == _CATALOG_MAX
    assert inner[COUNT_KEY] == total
    assert f'[+{total - _CATALOG_MAX} more not shown' in _message(result)


def test_catalog_with_no_mcp_tools_reports_empty_not_missing(registered_mcp):
    # func_map has only a core tool -> zero MCP tools. Catalog was REQUESTED (empty query),
    # so the keys ARE present with empty values — distinct from search mode, which omits them.
    tools = {'core-calc': 'not an mcp tool'}
    agent_obj, inst_name, _ = _make_context(tools)

    result = ToolSearch().call({'query': ''}, agent_obj=agent_obj, agent_instance_name=inst_name)
    inner = _envelope_inner(result)

    assert inner[CATALOG_KEY] == []
    assert inner[COUNT_KEY] == 0
    assert inner[TRUNCATED_KEY] is False
    assert 'No MCP tools are currently enabled.' in _message(result)


def test_search_mode_still_unlocks_and_omits_catalog_keys(registered_mcp):
    registered_mcp('srv-readfile')
    tools = {'srv-readfile': 'read a file from disk'}
    agent_obj, inst_name, inst = _make_context(tools)

    result = ToolSearch().call({'query': 'read file', 'limit': 1},
                               agent_obj=agent_obj, agent_instance_name=inst_name)
    inner = _envelope_inner(result)

    assert inner[DISCOVERED_KEY] == ['srv-readfile']
    assert inst._discovered_mcp_tools == ['srv-readfile']  # discovered grew (unlock happened)
    # Search mode never requests a catalog -> the catalog keys are absent (the None-vs-[]
    # discriminator: here requested=False, in the no-mcp test requested=True with empty lists).
    assert CATALOG_KEY not in inner
    assert COUNT_KEY not in inner
    assert TRUNCATED_KEY not in inner


def test_envelope_always_carries_loaded_key(registered_mcp):
    registered_mcp('srv-readfile')
    tools = {'srv-readfile': 'read a file from disk'}

    search_obj, search_name, _ = _make_context(tools)
    search_inner = _envelope_inner(ToolSearch().call(
        {'query': 'read', 'limit': 1}, agent_obj=search_obj, agent_instance_name=search_name))
    catalog_obj, catalog_name, _ = _make_context(tools)
    catalog_inner = _envelope_inner(ToolSearch().call(
        {'query': ''}, agent_obj=catalog_obj, agent_instance_name=catalog_name))

    assert DISCOVERED_KEY in search_inner
    assert DISCOVERED_KEY in catalog_inner
    assert catalog_inner[DISCOVERED_KEY] == []


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__]))
