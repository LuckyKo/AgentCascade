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

"""Scoped tests for session re-derivation of the discovered-MCP tail (F2 envelope parse)
and the ReAct Seam D offered-set selection. Constants imported from the code under test."""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.agents.react_chat import ReActChat
from agent_cascade.engine.helpers import _get_active_functions_from_template
from agent_cascade.llm.schema import FUNCTION, ROLE, Message
from agent_cascade.pool.session_io import _rederive_discovered_mcp_tools
from agent_cascade.tools.mcp_manager import MCPManager
from agent_cascade.tools.search_tools.tool_search import (
    DISCOVERED_KEY,
    TOOL_SEARCH_ENVELOPE_KEY,
)


class _FakeTool:
    def __init__(self, name, description='a tool'):
        self.name = name
        self.function = {
            'name': name,
            'description': description,
            'parameters': {'type': 'object', 'properties': {}},
        }


class _registered_mcp:
    def __init__(self, *names):
        self.names = names

    def __enter__(self):
        MCPManager._registered_tool_names.update(self.names)
        return self

    def __exit__(self, *exc):
        for name in self.names:
            MCPManager._registered_tool_names.discard(name)
        return False


def _envelope(loaded):
    return json.dumps({TOOL_SEARCH_ENVELOPE_KEY: {DISCOVERED_KEY: loaded}})


def _tool_result(loaded):
    """A tool_search result as it lands in a session log: prose line + trailing envelope."""
    return 'human-readable result text\n' + _envelope(loaded)


def _func_result_message(loaded, name=TOOL_SEARCH_ENVELOPE_KEY):
    return {ROLE: FUNCTION, 'name': name, 'content': _tool_result(loaded)}


def test_rederive_collects_in_order_deduped():
    messages = [
        _func_result_message(['srv-a', 'srv-b']),
        {ROLE: 'user', 'content': 'a user turn'},
        _func_result_message(['srv-b', 'srv-c']),
    ]
    assert _rederive_discovered_mcp_tools(messages) == ['srv-a', 'srv-b', 'srv-c']


def test_rederive_ignores_prose_only_results():
    # No parseable JSON envelope line -> contributes nothing (never parses free text).
    messages = [{ROLE: FUNCTION, 'name': TOOL_SEARCH_ENVELOPE_KEY, 'content': 'plain prose, no json'}]
    assert _rederive_discovered_mcp_tools(messages) == []


def test_rederive_ignores_other_function_results():
    messages = [{ROLE: FUNCTION, 'name': 'other_tool', 'content': _tool_result(['srv-a'])}]
    assert _rederive_discovered_mcp_tools(messages) == []


def test_rederive_handles_empty_history():
    assert _rederive_discovered_mcp_tools([]) == []


def _make_react(names):
    # ReActChat.__init__ pulls in llm/memory wiring; build the instance without __init__ and
    # attach only what _prepend_react_prompt / the resolver read.
    react = object.__new__(ReActChat)
    react.function_map = {n: _FakeTool(n) for n in names}
    react.llm = MagicMock()
    react.llm.generate_cfg = {}
    react.name = 'reacttest'
    react.agent_type = 'reacttest'
    return react


def _prompt_content(react, instance, pool=None):
    return react._prepend_react_prompt(
        [Message(role='user', content='hello')], lang='en', instance=instance, pool=pool)[0].content


def test_react_defers_mcp_until_discovered():
    mcp_name = 'srv-dotheheavything'
    core_name = 'coretool'
    # Seam D changed only WHICH names _prepend_react_prompt selects (the deferral-aware
    # resolver) — not the disabled_tools default policy, which is tested by its own module.
    # Stub the resolver to an empty disable set so this test asserts only selection +
    # discovered-tail append, decoupled from the defense-in-depth default table.
    with _registered_mcp(mcp_name), patch(
            'agent_cascade.utils.disabled_tools.resolve_disabled_tools_for_agent',
            return_value=set()):
        react = _make_react([core_name, mcp_name])
        # No instance -> undiscovered deferred MCP is omitted from the ReAct prompt.
        content_off = _prompt_content(react, instance=None)
        assert mcp_name not in content_off
        assert core_name in content_off
        # After discovery, the tool is selected by the resolver and appears in the prompt.
        instance = SimpleNamespace(
            _discovered_mcp_tools=[mcp_name], _generate_cfg_override=None,
            instance_name='r', agent_class='reacttest')
        content_on = _prompt_content(react, instance=instance)
        assert mcp_name in content_on
        assert core_name in content_on


def test_react_mcp_deferral_is_inert_core_only():
    # MINOR-4: ReAct's tool dispatch passes agent_obj=self (no .pool), so tool_search can never
    # unlock a deferred tool — the discovered tail stays empty forever and ReAct is core-only
    # under MCP deferral. A real AgentInstance (slots field) starts empty: the only reachable
    # state. Assert the resolver-based offered set excludes registered MCP tools in that state.
    mcp_name = 'srv-inertthing'
    core_name = 'coretool'
    now = time.monotonic()
    inst = AgentInstance(
        instance_name='r', agent_class='reacttest', conversation=[],
        created_at=now, last_activity=now, latest_marker_index=0,
    )
    with _registered_mcp(mcp_name):
        assert inst._discovered_mcp_tools == []  # empty -> never unlocked
        react = _make_react([core_name, mcp_name])
        names = [f['name'] for f in _get_active_functions_from_template(react, inst, None)]
        assert mcp_name not in names
        assert core_name in names


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__]))
