"""BUG_0407 regression — AFK-Admin tool set vs the user's UI disabled-tools config.

Premise under test
------------------
"Does the AFK-Admin dispatch run Admin with the CLASS-DEFAULT tool set, or does it
honor the user's UI disabled-tools config?"

What is actually load-bearing (per bug0407-dig-report @ HEAD 018bf27e):
the *offered-tool schema* for the AFK Admin is built every turn by
``engine.helpers._get_active_functions_from_template(template, instance, pool)``
(reached from ``engine.llm_call`` via ``pool=self.pool``). Inside it, the gate at
``helpers.py:235-238`` unions ``pool.get_ui_disabled_tools_for_agent(agent_name, agent_type)``
into the disabled set for any instance NOT carrying ``_exclusive_disabled_tools``
(only advisor/security set that flag — Admin does not).

So the question "UI config honored vs class-default fallback" reduces, at the
enforcement layer, to: does ``_get_active_functions_from_template`` exclude a tool
the user disabled for the Admin class, for a *non-exclusive* Admin instance?

Design notes
------------
- Hermetic: no live LLM, no AgentPool boot, no network, no disk IO.
- The pool double replicates ``config_persist.AgentPool.get_ui_disabled_tools_for_agent``
  *exactly* — it delegates to the real ``utils.disabled_tools.resolve_disabled_tools_for_agent``
  over a live ``_ui_disabled_tools`` deny-list. That keeps the production single-source-of-truth
  invariant (``utils/disabled_tools.py:86-88``): the assertion goes through the real resolver
  chain against ``pool._ui_disabled_tools``, never against a telemetry ``tools_list``.
- The template/instance doubles exercise the same input surface the real path feeds the gate:
  ``template.name`` / ``template.agent_type`` / ``template.llm.generate_cfg`` /
  ``template.function_map`` and ``instance._generate_cfg_override`` /
  ``instance._exclusive_disabled_tools``.
- The Admin read-only floor (``constants.ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS``) is applied to
  the instance override exactly as ``_initialize_child_run`` does (``system_agent.py:420-426``),
  so the test also proves the floor is honored alongside the UI map.

Tool selection: ``grep`` is a read tool the Admin normally keeps — it is NOT in the read-only
floor, so disabling it via the UI is what proves the UI map (not the floor) took effect.
``shell_cmd`` is in the floor and is used to prove the floor is applied. ``read_file`` and
``web_search`` are control tools that must stay offered.
"""

from __future__ import annotations

import pytest

from agent_cascade.constants import ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS
from agent_cascade.engine.helpers import _get_active_functions_from_template
from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent


# Tools used as fixtures in this test. We control the registry so the use-time
# registry filter in resolve_disabled_tools_for_agent (disabled_tools.py:150-157)
# does not silently drop our test tool names.
_UI_DISABLED = 'grep'          # Admin keeps this by default; NOT in the read-only floor.
_FLOOR_TOOL = 'shell_cmd'      # In ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS.
_KEEP = ('read_file', 'web_search')


class _Tool:
    """Minimal function_map entry: helpers reads ``func_map[name].function``."""

    def __init__(self, name: str):
        self.name = name
        self.function = {'name': name, 'description': f"tool {name}", 'parameters': {}}


class _LLM:
    def __init__(self, generate_cfg=None):
        self.generate_cfg = generate_cfg if generate_cfg is not None else {}


class _Template:
    """Template double exposing the input surface _get_active_functions_from_template reads."""

    def __init__(self, name, agent_type, func_map, generate_cfg=None):
        self.name = name
        self.agent_type = agent_type
        self.function_map = func_map
        self.llm = _LLM(generate_cfg)


class _Instance:
    """Instance double: instance_name, _generate_cfg_override, _exclusive_disabled_tools."""

    def __init__(self, instance_name, cfg_override=None, exclusive=False):
        self.instance_name = instance_name
        self._generate_cfg_override = cfg_override
        self._exclusive_disabled_tools = exclusive


class _Pool:
    """Pool double that mirrors config_persist.AgentPool.get_ui_disabled_tools_for_agent.

    The body below is a faithful copy of config_persist.py:730-750: it snapshots the live
    ``_ui_disabled_tools`` deny-list and resolves it through the *real* centralized resolver.
    This keeps the single-source-of-truth path intact rather than stubbing the result.
    """

    def __init__(self):
        self._ui_disabled_tools = {}

    def set_ui_disabled_tools(self, disabled_tools_dict=None):
        # Mirrors config_persist.set_ui_disabled_tools write shape (config_persist.py:695-696).
        self._ui_disabled_tools = dict(disabled_tools_dict) if disabled_tools_dict else {}

    def get_ui_disabled_tools_for_agent(self, agent_name, agent_type=''):
        dt = dict(self._ui_disabled_tools)
        if not dt:
            return set()
        return resolve_disabled_tools_for_agent(
            instance_override={'disabled_tools': dt},
            template_cfg=None,
            agent_name=agent_name,
            agent_type=agent_type,
        )


def _offered_names(template, instance, pool):
    """Return the tool NAMES offered to the LLM by the real enforcement gate."""
    funcs = _get_active_functions_from_template(template, instance, pool)
    return {f['name'] for f in funcs}


@pytest.fixture
def known_registry(monkeypatch):
    """Make our test tool names 'known' so the use-time registry filter keeps them."""
    known = set(_KEEP) | {_UI_DISABLED, _FLOOR_TOOL}
    # resolve_disabled_tools_for_agent reads TOOL_REGISTRY (module-level) at call time.
    monkeypatch.setattr('agent_cascade.tools.base.TOOL_REGISTRY', dict.fromkeys(known))
    monkeypatch.setattr('agent_cascade.constants.RUNTIME_REGISTERED_TOOLS', frozenset())


def _admin_template():
    func_map = {n: _Tool(n) for n in (_UI_DISABLED, _FLOOR_TOOL, *_KEEP)}
    # agent_type mirrors how the Admin template is keyed; UI dict uses the 'Admin' class name.
    return _Template(name='Admin', agent_type='admin', func_map=func_map)


def _baked_admin_instance():
    """An Admin instance AFTER _initialize_child_run baked the read-only floor
    into _generate_cfg_override (system_agent.py:420-426), and NOT marked exclusive."""
    floor = sorted(ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS)
    return _Instance('admin_afk', cfg_override={'disabled_tools': list(floor)}, exclusive=False)


def test_afk_admin_ui_map_is_honored(known_registry):
    """The AFK-Admin offered schema EXCLUDES a UI-disabled Admin tool and applies the floor."""
    pool = _Pool()
    pool.set_ui_disabled_tools({'Admin': [_UI_DISABLED]})
    # Sanity: the real resolver chain actually resolves the deny-list to our tool for Admin.
    assert _UI_DISABLED in pool.get_ui_disabled_tools_for_agent('Admin', 'admin')

    offered = _offered_names(_admin_template(), _baked_admin_instance(), pool)

    assert _UI_DISABLED not in offered, 'UI-disabled Admin tool was still offered — class-default fallback'
    assert _FLOOR_TOOL not in offered, 'read-only floor tool was still offered'
    assert set(_KEEP) <= offered, 'control read-only tools were wrongly disabled'


def test_mutation_gate_is_load_bearing(known_registry):
    """Mutation-verify: neutralize the runtime gate at helpers.py:235 and the
    UI-disabled tool REAPPEARS, proving that gate (not the bake) is what applies the map."""
    pool = _Pool()
    pool.set_ui_disabled_tools({'Admin': [_UI_DISABLED]})
    template = _admin_template()
    instance = _baked_admin_instance()

    # Baseline: gate active -> tool excluded.
    assert _UI_DISABLED not in _offered_names(template, instance, pool)

    # Mutation 1: pass pool=None -> the `if pool is not None` gate (helpers.py:235) is skipped.
    assert _UI_DISABLED in _offered_names(template, instance, None), \
        'gate not load-bearing: disabling the pool union should re-offer the tool'

    # Mutation 2: carry _exclusive_disabled_tools=True -> the gate's second condition (helpers.py:236)
    # skips the UI union, exactly the advisor/security path. The tool must reappear.
    exclusive = _Instance('admin_afk', cfg_override={'disabled_tools': sorted(ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS)},
                          exclusive=True)
    assert _UI_DISABLED in _offered_names(template, exclusive, pool), \
        'gate not load-bearing: exclusive instances must bypass the UI union'


def test_control_non_admin_unaffected(known_registry):
    """The UI map is applied per-agent-type, not globally. A non-Admin child whose class
    is NOT keyed in the deny-list keeps the same tool."""
    pool = _Pool()
    pool.set_ui_disabled_tools({'Admin': [_UI_DISABLED]})  # keyed to Admin only.

    coder_template = _Template(name='Coder', agent_type='coder',
                               func_map={n: _Tool(n) for n in (_UI_DISABLED, _FLOOR_TOOL, *_KEEP)})
    # A call_agent (non-AFK) child with no explicit config override and not exclusive.
    coder = _Instance('coder_child', cfg_override=None, exclusive=False)

    offered = _offered_names(coder_template, coder, pool)
    # The Admin UI map must NOT leak onto the Coder: grep stays offered for the Coder.
    assert _UI_DISABLED in offered, 'Admin UI deny-list leaked onto a non-Admin child'
