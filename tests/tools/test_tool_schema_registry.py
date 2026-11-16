"""BUG_0329 (Increment A) — registry-wide tool-schema validity invariant.

Every advertised (statically-registered) tool must expose a well-formed OpenAI
JSON-Schema ``parameters`` block: valid JSON Schema, ``required`` is a subset of
``properties``, and ``type == 'object'``. Importing the tool modules is enough to
trigger the ``@register_tool`` side-effects, so this turns schema validity into a
build-time check rather than a runtime surprise (BaseTool.__init__ deliberately
skips validation, so before this test a malformed schema could reach the API).

The MCP-generated tools are built dynamically at connect time and are not present
in ``TOOL_REGISTRY`` at import, so they are out of scope here; MCP schema cleaning
is covered by Increment C.
"""

import importlib
import pkgutil

import pytest

import agent_cascade.tools as _tools_pkg
from agent_cascade.tools._agent_instance_proxy import _AgentInstanceFunctionProxy
from agent_cascade.tools.base import TOOL_REGISTRY, is_tool_schema


def _populate_registry() -> None:
    """Import every module under agent_cascade.tools so @register_tool runs.

    Optional-dependency import failures (a tool needs an uninstalled package) are
    skipped; they do not affect the tools that DO import cleanly, which are exactly
    the advertised set this invariant must cover.
    """
    package_path = _tools_pkg.__path__
    for module_info in pkgutil.walk_packages(package_path, prefix=_tools_pkg.__name__ + '.'):
        if module_info.name.endswith('.__init__'):
            continue
        try:
            importlib.import_module(module_info.name)
        except Exception:  # noqa: BLE001 - optional deps / env-gated imports
            continue


_populate_registry()

# Core advertised (statically-registered) tools that MUST be present; guards
# against a silent import failure that would otherwise make the sweep vacuously
# pass. Proxy tools (call_agent/dismiss_agent) are NOT in TOOL_REGISTRY and are
# covered separately by test_proxy_tool_schema_is_valid.
_EXPECTED_CORE = {
    'read_file', 'write_file', 'edit_file', 'delete_file', 'copy_file', 're_indent',
    'list_dir', 'grep', 'read_logs', 'send_message', 'storage', 'retrieval',
    'scan_skills', 'compress_context', 'tool_search',
}


def _test_params():
    """(name, cls) for every registered tool whose class-level parameters is a dict."""
    out = []
    for name, cls in sorted(TOOL_REGISTRY.items()):
        params = getattr(cls, 'parameters', None)
        if isinstance(params, dict):
            out.append((name, cls))
    return out


def test_expected_core_tools_registered():
    missing = _EXPECTED_CORE - set(TOOL_REGISTRY)
    assert not missing, f'expected advertised tools not registered: {sorted(missing)}'


@pytest.mark.parametrize('tool_name, tool_cls', _test_params())
def test_registered_tool_schema_is_valid(tool_name, tool_cls):
    """Every advertised registered tool's parameters pass is_tool_schema()."""
    function = {
        'name': tool_name,
        'description': getattr(tool_cls, 'description', ''),
        'parameters': tool_cls.parameters,
    }
    assert is_tool_schema(function), (
        f'{tool_name}: parameters is not a valid tool schema')
    params = tool_cls.parameters
    assert params['type'] == 'object'
    assert set(params['required']).issubset(params['properties'])


@pytest.mark.parametrize('tool_name', ['call_agent', 'dismiss_agent'])
def test_proxy_tool_schema_is_valid(tool_name):
    """Proxy tools build their schema from dna.py; must also be valid."""
    proxy = _AgentInstanceFunctionProxy(tool_name)
    assert is_tool_schema(proxy.function), (
        f'{tool_name}: proxy parameters is not a valid tool schema')
