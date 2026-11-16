"""BUG_0329 Increment C — the MCP inputSchema cleaner must NOT be lossy.

Covers ``agent_cascade.tools.mcp_manager.MCPManager._build_tools_for_client``, the
loop that turns a server-provided MCP ``inputSchema`` into a registered tool. The
pre-fix cleaner dropped ``$defs``/``definitions``/``additionalProperties``/the top-level
``description``, mutated the server's dict in place, and never validated the schema.
After the fix:

  (a) a schema with ``$defs`` + ``additionalProperties`` + ``description`` registers
      with its FULL schema intact,
  (b) the server-provided dict is never mutated (deepcopy before any edit),
  (c) an invalid schema is warned + skipped, not registered,
  (d) a ``None`` description (tool-level or schema top-level) is coerced, never leaks.

No real MCP session: the client is a SimpleNamespace and only ``.tools``/``.resources``
are touched. Run serially (pytest.ini pins xdist):
    python -m pytest tests/test_mcp_schema_cleaner.py -q -n 0
"""

import asyncio
import copy
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.absolute()
import sys  # noqa: E402
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.tools.mcp_manager import MCPManager  # noqa: E402


def _make_mgr() -> MCPManager:
    """A bare MCPManager bypassing the singleton __new__/__init__.

    _build_tools_for_client only needs _registered_tool_names (via create_tool_class).
    """
    mgr = object.__new__(MCPManager)
    mgr._registered_tool_names: set = set()
    return mgr


def _build(tool_defs):
    """Run the cleaner over a list of {'name','description','inputSchema'} and return
    (registered_tool_objects, inputSchema_refs, the mgr_that_ran_it).
    """
    tools = [
        SimpleNamespace(name=d['name'], description=d.get('description'),
                        inputSchema=d['inputSchema'])
        for d in tool_defs
    ]
    client = SimpleNamespace(tools=tools, resources=[])  # falsy resources -> skip that branch
    mgr = _make_mgr()
    registered = asyncio.run(mgr._build_tools_for_client(client, 'srv', 'cid1'))
    return registered, tools, mgr


FULL_SCHEMA = {
    'type': 'object',
    'properties': {'x': {'$ref': '#/$defs/s'}},
    'required': [],
    'additionalProperties': False,
    '$defs': {'s': {'type': 'string'}},
    'description': 'top-level schema description',
}


class TestFullSchemaPreserved:
    def test_defs_and_additionalproperties_and_description_survive(self):
        registered, _, _ = _build([{'name': 'query', 'description': 'd', 'inputSchema': FULL_SCHEMA}])
        assert len(registered) == 1
        params = registered[0].parameters
        assert params == copy.deepcopy(FULL_SCHEMA), (
            f'cleaner must register the schema verbatim, got {params}'
        )
        # Explicit checks (not just dict-eq) so a future narrowing fails loudly.
        assert '$defs' in params
        assert params['additionalProperties'] is False
        assert params['description'] == 'top-level schema description'
        assert params['properties']['x'] == {'$ref': '#/$defs/s'}

    def test_required_initialized_when_absent(self):
        schema = {'type': 'object', 'properties': {'a': {'type': 'string'}}}
        registered, _, _ = _build([{'name': 't', 'description': 'd', 'inputSchema': schema}])
        assert registered[0].parameters['required'] == []


class TestNoInPlaceMutation:
    def test_server_dict_untouched_and_not_aliased(self):
        input_schema = copy.deepcopy(FULL_SCHEMA)
        # Force the mutation paths: omit 'required', set a null description.
        input_schema.pop('required')
        input_schema['description'] = None
        original = copy.deepcopy(input_schema)

        registered, tools, _ = _build([{'name': 't', 'description': 'd', 'inputSchema': input_schema}])
        # The cleaner coerced None -> '' on its COPY, so inputSchema keeps None untouched.
        assert tools[0].inputSchema == original, 'server-provided inputSchema was mutated'
        # Registered parameters must be a distinct object, never an alias of the source.
        assert registered[0].parameters is not tools[0].inputSchema
        registered[0].parameters['required'].append('SENTINEL')
        assert 'required' not in tools[0].inputSchema, 'mutating registered schema leaked into server dict'


class TestInvalidSchemaSkipped:
    def test_invalid_keyword_schema_is_not_registered(self):
        # A property whose 'type' is not a valid JSON-Schema primitive -> SchemaError.
        bad = {'type': 'object', 'properties': {'a': {'type': 'not-a-valid-type'}}, 'required': []}
        registered, _, mgr = _build([{'name': 'bad', 'description': 'd', 'inputSchema': bad}])
        assert registered == []
        assert mgr._registered_tool_names == set(), (
            f'skipped tool must not be tracked for security filtering, got {mgr._registered_tool_names}'
        )

    def test_non_object_type_is_skipped(self):
        bad = {'type': 'string', 'properties': {}, 'required': []}
        registered, _, mgr = _build([{'name': 't', 'description': 'd', 'inputSchema': bad}])
        assert registered == []
        assert mgr._registered_tool_names == set()


class TestDescriptionCoercion:
    def test_none_tool_description_becomes_empty_string(self):
        registered, _, _ = _build([{'name': 't', 'description': None, 'inputSchema': FULL_SCHEMA}])
        assert registered[0].description == ''

    def test_null_top_level_schema_description_coerced_not_dropped(self):
        schema = copy.deepcopy(FULL_SCHEMA)
        schema['description'] = None
        registered, _, _ = _build([{'name': 't', 'description': 'd', 'inputSchema': schema}])
        # Must still register (the None was coerced, not left to invalidate the schema).
        assert len(registered) == 1
        assert registered[0].parameters['description'] == ''


class TestOneBadToolDoesNotSinkTheServer:
    def test_good_and_bad_mixed(self):
        bad = {'type': 'object', 'properties': {'a': {'type': 'not-a-valid-type'}}, 'required': []}
        registered, _, mgr = _build([
            {'name': 'good', 'description': 'g', 'inputSchema': FULL_SCHEMA},
            {'name': 'bad', 'description': 'b', 'inputSchema': bad},
        ])
        names = [t.name for t in registered]
        assert names == ['srv-good'], f'only the valid tool should register, got {names}'
        # The bad tool must be absent from the live security-filtering set too,
        # proving it was never tracked (not merely omitted from the returned list).
        assert mgr._registered_tool_names == {'srv-good'}, (
            f'invalid tool leaked into _registered_tool_names, got {mgr._registered_tool_names}'
        )
