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

"""RED->GREEN regression for CRITICAL-1: ``_discovered_mcp_tools`` must be a declared field
on the slots=True AgentInstance.

Pre-fix it was an undeclared attribute, so the two direct write sites raised AttributeError:
tool_search's unlock append (feature dead) and session_io's reload re-derivation (which
re-raises even inside its defensive except, crashing every load_session_from_log). These tests
drive a REAL AgentInstance, so they fail pre-fix and pass post-fix."""

import json
import time

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.pool.session_io import _rederive_discovered_mcp_tools
from agent_cascade.tools.mcp_manager import MCPManager
from agent_cascade.tools.search_tools.tool_search import (
    DISCOVERED_KEY,
    TOOL_SEARCH_ENVELOPE_KEY,
    ToolSearch,
)


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
    now = time.monotonic()
    return AgentInstance(
        instance_name=name, agent_class='coder', conversation=[],
        created_at=now, last_activity=now, latest_marker_index=0,
    )


def test_real_agentinstance_has_discovered_field_default_empty():
    # The field must exist and default to an empty list (slots=True -> undeclared = AttributeError).
    inst = _make_instance()
    assert inst._discovered_mcp_tools == []


def test_tool_search_unlock_writes_real_agentinstance_field():
    mcp_name = 'srv-readfile'
    MCPManager._registered_tool_names.add(mcp_name)
    try:
        inst = _make_instance()
        template = _FakeTemplate({mcp_name: _FakeTool(mcp_name, 'read a file from disk')})
        agent_obj = _FakeAgentObj(_FakePool(inst, template))

        result = ToolSearch().call(
            {'query': 'read file', 'limit': 1},
            agent_obj=agent_obj, agent_instance_name=inst.instance_name,
        )
        parsed = json.loads(result.splitlines()[-1])
        assert parsed[TOOL_SEARCH_ENVELOPE_KEY][DISCOVERED_KEY] == [mcp_name]
        # RED pre-fix: assignment to the undeclared slot raises AttributeError.
        assert inst._discovered_mcp_tools == [mcp_name]
    finally:
        MCPManager._registered_tool_names.discard(mcp_name)


def test_session_reload_rederives_into_real_agentinstance():
    # Reproduces session_io.py's reload assignment (the load_session_from_log crash site)
    # against a real instance — no pool/model spin-up needed to exercise the field bug.
    mcp_name = 'srv-writefile'
    MCPManager._registered_tool_names.add(mcp_name)
    try:
        inst = _make_instance()
        envelope = json.dumps({TOOL_SEARCH_ENVELOPE_KEY: {DISCOVERED_KEY: [mcp_name]}})
        messages = [{
            'role': 'function', 'name': TOOL_SEARCH_ENVELOPE_KEY,
            'content': 'human text line\n' + envelope,
        }]
        # Exact assignment done at session_io.py reload; crashes pre-fix (and the defensive
        # except branch re-raises, so the crash is not actually caught).
        inst._discovered_mcp_tools = _rederive_discovered_mcp_tools(messages)
        assert inst._discovered_mcp_tools == [mcp_name]
    finally:
        MCPManager._registered_tool_names.discard(mcp_name)


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__]))
