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

"""Scoped tests for the deferral-aware offered set (Seam A), its cache-stability invariant,
the ReAct Seam D selection, and the deferral-aware UI paths.

Deferred membership is MCPManager.is_mcp_tool, so the fake MCP tool names are registered in
``MCPManager._registered_tool_names`` (save/restore) to drive the real code path."""

from types import SimpleNamespace

import pytest

from agent_cascade.engine.helpers import (
    order_offered_tool_functions,
    order_offered_tool_names,
)
from agent_cascade.prompts.dna import AVAILABLE_TOOLS, TOOL_METADATA
from agent_cascade.tools.mcp_manager import MCPManager


class _FakeTool:
    """Minimal stand-in exposing the `.function` schema dict the assembler/UI read."""

    def __init__(self, name, description='a tool'):
        self.name = name
        self.function = {
            'name': name,
            'description': description,
            'parameters': {'type': 'object', 'properties': {}},
        }


class _registered_mcp:
    """Context manager that registers fake names as MCP tools, restoring on exit.

    ``MCPManager._registered_tool_names`` is process-global class state; save/restore keeps
    tests hermetic and leak-free."""

    def __init__(self, *names):
        self.names = names

    def __enter__(self):
        MCPManager._registered_tool_names.update(self.names)
        return self

    def __exit__(self, *exc):
        for name in self.names:
            MCPManager._registered_tool_names.discard(name)
        return False


def _func_map(names):
    return {n: _FakeTool(n) for n in names}


def test_core_sorted_and_mcp_excluded():
    core = ['zeta', 'alpha', 'beta']
    mcp = ['srv-read', 'srv-write']
    with _registered_mcp(*mcp):
        offered = order_offered_tool_names(_func_map(core + mcp), set())
    # Deferred MCP tools omitted (the token win); core is sorted (stable prefix).
    assert offered == sorted(core)


def test_instance_none_is_core_only():
    # Class/template UI view: no instance -> no discovered tail, even if a discovered name
    # exists in the map.
    with _registered_mcp('srv-read'):
        offered = order_offered_tool_names(_func_map(['core_x', 'srv-read']), set(), instance=None)
    assert offered == ['core_x']


def test_discovered_tail_in_insertion_order():
    # Discovered deferred tools are appended AFTER sorted core, in the instance's append
    # order (NEVER re-sorted) — the append-only tail.
    inst = SimpleNamespace(_discovered_mcp_tools=['srv-zeta', 'srv-alpha'], instance_name='i')
    with _registered_mcp('srv-zeta', 'srv-alpha'):
        offered = order_offered_tool_names(
            _func_map(['core_a', 'srv-zeta', 'srv-alpha']), set(), instance=inst)
    assert offered == ['core_a', 'srv-zeta', 'srv-alpha']


def test_discovered_deduped():
    # Already in the discovered tail, and core sort, are never duplicated in the output.
    inst = SimpleNamespace(_discovered_mcp_tools=['srv-a', 'srv-a'], instance_name='i')
    with _registered_mcp('srv-a'):
        offered = order_offered_tool_names(_func_map(['core_a', 'srv-a']), set(), instance=inst)
    assert offered == ['core_a', 'srv-a']


def test_hard_disable_never_overridden_by_discovery():
    # A per-tool hard disable (already folded into `disabled`) drops even a discovered tool.
    inst = SimpleNamespace(_discovered_mcp_tools=['srv-a'], instance_name='i')
    with _registered_mcp('srv-a'):
        offered = order_offered_tool_names(_func_map(['core_a', 'srv-a']), {'srv-a'}, instance=inst)
    assert offered == ['core_a']


def test_all_mcp_sentinel_blocks_discovered_via_fold():
    # MAJOR-3: the __all_mcp_tools__ sentinel is expanded INSIDE order_offered_tool_names, so
    # passing ONLY the sentinel (NOT the individual tool name) still blocks the discovered MCP
    # tail. Non-vacuous: without the sentinel the identical call DOES unlock srv-a.
    sentinel = '__all_mcp_tools__'
    inst = SimpleNamespace(_discovered_mcp_tools=['srv-a'], instance_name='i')
    fmap = _func_map(['core_a', 'srv-a'])
    with _registered_mcp('srv-a'):
        baseline = order_offered_tool_names(fmap, set(), instance=inst)
        with_sentinel = order_offered_tool_names(fmap, {sentinel}, instance=inst)
    assert 'srv-a' in baseline          # the discovery mechanism genuinely unlocks it...
    assert with_sentinel == ['core_a']  # ...and the sentinel (only) blocks it.


def test_disconnected_tool_pruned():
    # A discovered tool whose server disconnected (name gone from func_map) is not offered.
    inst = SimpleNamespace(_discovered_mcp_tools=['srv-a'], instance_name='i')
    with _registered_mcp('srv-a'):
        offered = order_offered_tool_names(_func_map(['core_a']), set(), instance=inst)
    assert offered == ['core_a']


def test_offered_set_is_deterministic_across_calls():
    # Cache-stability: identical func_map + disabled -> byte-identical order, twice.
    fmap = _func_map(['delta', 'core', 'charlie', 'bravo'])
    with _registered_mcp('delta'):
        first = order_offered_tool_names(fmap, set())
        second = order_offered_tool_names(fmap, set())
    assert first == second


def test_order_offered_tool_functions_matches_names():
    fmap = _func_map(['b', 'a'])
    schemas = order_offered_tool_functions(fmap, set())
    assert [s['name'] for s in schemas] == order_offered_tool_names(fmap, set())


def test_tool_search_is_registered_core_tool():
    # Registration contract: fixed always-on core tool (default-on, not in the disable baseline).
    assert 'tool_search' in AVAILABLE_TOOLS
    assert TOOL_METADATA['tool_search']['description']


def test_tool_search_description_does_not_enumerate_mcp_tools():
    # Real cache invariant: the FIXED description must never embed MCP server/tool name tokens,
    # otherwise the prompt prefix shifts as servers connect/disconnect. Registering a fake
    # server-tool must leave the constant description unaffected.
    with _registered_mcp('acme-frobnicate'):
        desc = TOOL_METADATA['tool_search']['description'].lower()
        assert 'acme' not in desc
        assert 'frobnicate' not in desc


def test_ui_universe_excludes_mcp_includes_core():
    # The state_builder / api_server toggle universe is `order_offered_tool_names(func_map,
    # set())` — core (incl. tool_search) stays toggleable, deferred MCP does not leak.
    with _registered_mcp('srv-secret'):
        universe = order_offered_tool_names(
            _func_map(['tool_search', 'core_a', 'srv-secret']), set())
    assert 'tool_search' in universe
    assert 'core_a' in universe
    assert 'srv-secret' not in universe


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__]))
