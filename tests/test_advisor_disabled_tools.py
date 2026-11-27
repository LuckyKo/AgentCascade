"""System-launched Security advisors are decoupled from the UI disabled_tools map.

The Security guard (``security_handler.py``) and the Skill Advisor (``advisor_runner.py``) are
spawned by the system, not by a user, so their tool set must come from the fixed
``settings.ADVISOR_AGENT_DISABLED_TOOLS`` set rather than whatever the user happens to have
toggled off for the 'Security' entry in the UI. A UI edit must neither widen nor narrow what the
guard can see, while the UI set keeps governing user-/agent-called sub-agents.

Two gates enforce tool policy, and only one of them can inject the UI map:

1. ``engine.helpers._get_active_functions_from_template`` (the OFFERED-TOOL schema) unions the
   *live* pool config after resolution — that union is gated on
   ``AgentInstance._exclusive_disabled_tools``.
2. ``engine.tool_execution._resolve_tool_policy`` (the use-time auto-deny) resolves from the
   instance override only and never reads the UI map, so it needs no flag at all. Asserted here as
   the negative control that a divergent UI entry can never reach the enforcer.

``Agent._get_active_functions`` / ``_get_disabled_tool_names`` are display/legacy views (the UI
tool list, system_info, the fncall path); they run on the shared ``Agent`` TEMPLATE, never on the
advisor ``AgentInstance`` that carries the flag, so they are deliberately not gated.

Real ``AgentInstance`` objects are used on purpose: the class is ``@dataclass(slots=True)``, so a
SimpleNamespace double would hide an undeclared-field AttributeError on the production path.
"""

import dataclasses
import importlib.util
import logging
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.advisor_runner import run_lightweight_advisor
from agent_cascade.agent import Agent
from agent_cascade.agent_instance import AgentInstance
from agent_cascade.constants import RUNTIME_REGISTERED_TOOLS
from agent_cascade.engine.helpers import _get_active_functions_from_template
from agent_cascade.engine.tool_execution import ToolExecMixin
from agent_cascade.settings import ADVISOR_AGENT_DISABLED_TOOLS
from agent_cascade.tools.base import TOOL_REGISTRY

AGENT_NAME = 'Security'
AGENT_TYPE = 'Security'

# A tool the advisor set deliberately frees, plus one arbitrary core tool: both stand in for
# "the user disabled this in the UI". They must be registered, otherwise the resolver's
# use-time registry filter would drop them and the test would pass vacuously.
UI_ONLY_DISABLED = ('shell_cmd', 'grep')


def _known_tools() -> set:
    """Names the resolver's use-time registry filter considers real (same source of truth)."""
    return set(TOOL_REGISTRY.keys()) | RUNTIME_REGISTERED_TOOLS


@pytest.fixture(autouse=True)
def _require_ui_only_tools_registered():
    unregistered = [t for t in UI_ONLY_DISABLED if t not in _known_tools()]
    assert not unregistered, (
        f'Test fixture broken: {unregistered} are not in TOOL_REGISTRY, so the resolver would '
        f'silently drop them and the assertions below would pass vacuously.'
    )


def _advisor_disabled() -> set:
    """The fixed set as the resolver can actually observe it (registry-filtered)."""
    return set(ADVISOR_AGENT_DISABLED_TOOLS) & _known_tools()


def _new_instance(instance_name: str = 'Security_test') -> AgentInstance:
    """A bare real AgentInstance — fields carry their declared defaults."""
    now = time.time()
    return AgentInstance(
        instance_name=instance_name,
        agent_class=AGENT_TYPE,
        conversation=[],
        created_at=now,
        last_activity=now,
        latest_marker_index=0,
    )


def _make_instance(exclusive: bool, fixed_set: bool) -> AgentInstance:
    """A real AgentInstance, configured the way the two advisor call sites configure one."""
    instance = _new_instance()
    # Mirrors security_handler.py / advisor_runner.py: the cfg the instance carries is
    # template generate_cfg merged with llm_safe_cfg, whose disabled_tools is the fixed set.
    instance._generate_cfg_override = (
        {'disabled_tools': sorted(ADVISOR_AGENT_DISABLED_TOOLS)} if fixed_set else {}
    )
    instance._exclusive_disabled_tools = exclusive
    return instance


class _StubAgent(Agent):
    """Minimal Agent template: enough attributes for the two resolution helpers."""

    def __init__(self, template_cfg, function_map):
        # Deliberately not calling Agent.__init__ — it would build a chat model and register
        # real tools; only name / agent_type / llm.generate_cfg / function_map are read here.
        self.name = AGENT_NAME
        self.agent_type = AGENT_TYPE
        self.llm = type('Llm', (), {'generate_cfg': template_cfg})()
        self.function_map = function_map

    def _run(self, messages, lang='en', **kwargs):
        yield []


class _StubPool:
    """Pool double exposing every UI read shape the production code uses.

    ``get_template`` backs the use-time gate (``_resolve_tool_policy`` resolves the disabled set
    through the template); ``get_ui_disabled_tools_for_agent`` / ``_ui_disabled_tools`` back the
    live-union gate and the display resolvers."""

    def __init__(self, ui_entry, template=None):
        self._ui_disabled_tools = {AGENT_NAME: list(ui_entry)}
        self._template = template

    def get_template(self, agent_class):
        return self._template

    def get_ui_disabled_tools_for_agent(self, agent_name, agent_type=''):
        return list(self._ui_disabled_tools.get(agent_name, []))


class _StubEngine:
    """Only ``.pool`` is read by ``ToolExecMixin._resolve_tool_policy``."""

    def __init__(self, pool):
        self.pool = pool


def _make_func_map(names):
    """function_map shape the engine expects: tool objects exposing ``.function``."""
    return {name: SimpleNamespace(function={'name': name, 'description': '', 'parameters': {}})
            for name in names}


def _run_advisor_and_capture(agent_class: str, template, instance_name: str, **kwargs) -> AgentInstance:
    """Drive ``run_lightweight_advisor`` through a fake engine (no real LLM) and hand back the
    instance its config block produced.

    Mirrors the harness in tests/test_skill_advisor.py, but the fresh-spawn branch returns a REAL
    ``AgentInstance`` on purpose: with a MagicMock, an unset flag reads back as an auto-created
    child mock instead of the declared ``False`` default, so the negative control would be blind.
    """
    captured = {}

    def _create_system_agent(**kw):
        inst = _new_instance(kw['instance_name'])
        captured['inst'] = inst
        return inst

    fake_engine = MagicMock()
    fake_engine._create_system_agent.side_effect = _create_system_agent
    fake_engine.run.side_effect = lambda inst: iter([['turn1']])
    fake_engine._telemetry.return_value = None

    pool = MagicMock()
    pool.stopped = False
    pool._telemetry.return_value = None
    pool.get_template.return_value = template

    import agent_cascade.compression.helpers as ch_mod
    import agent_cascade.execution_engine as ee_mod
    with patch.object(ee_mod, 'ExecutionEngine', return_value=fake_engine), \
            patch.object(ch_mod, 'extract_instance_output', return_value='OUTPUT'):
        run_lightweight_advisor(pool, agent_class, instance_name, 'task', 'caller', **kwargs)
    return captured['inst']


# ── The flag itself ─────────────────────────────────────────────────────────────────

def test_exclusive_flag_is_a_declared_field():
    """slots=True means an undeclared dynamic write raises AttributeError — catch it here.

    Also pins the default: only the Security advisor call sites opt an instance in."""
    fresh = _new_instance()
    assert '_exclusive_disabled_tools' in {f.name for f in dataclasses.fields(AgentInstance)}, (
        'AgentInstance is @dataclass(slots=True): an undeclared attribute would raise '
        'AttributeError on the first advisor spawn.'
    )
    assert fresh._exclusive_disabled_tools is False, (
        'a fresh instance must default to non-exclusive so ordinary agents keep live UI updates'
    )
    assert _make_instance(exclusive=True, fixed_set=True)._exclusive_disabled_tools is True


def test_guard_floor_disables_untrusted_content_tools():
    """SEC-07: a trusted Security reviewer must not fetch untrusted web content or view
    media — web_search / web_extractor / view_media are in the fixed floor, and each is a
    registered tool (so the resolver's use-time registry filter does not drop it vacuously)."""
    for tool in ('web_search', 'web_extractor', 'view_media'):
        assert tool in ADVISOR_AGENT_DISABLED_TOOLS, (
            f'{tool!r} must be in the Security guard floor so the guard cannot fetch '
            f'untrusted web content / view media'
        )
        assert tool in _known_tools(), (
            f'{tool!r} is in the floor but not a registered tool, so the resolver would '
            f'drop it and the guard would keep it enabled'
        )


# ── Gate 1: the offered-tool schema (engine/helpers) ─────────────────────────────────

def test_exclusive_schema_path_skips_live_pool_union():
    """The offered-tool schema (engine path) must not be widened by live UI config."""
    func_map = _make_func_map(sorted(ADVISOR_AGENT_DISABLED_TOOLS) + list(UI_ONLY_DISABLED))
    template = _StubAgent({}, func_map)
    pool = _StubPool(UI_ONLY_DISABLED)
    instance = _make_instance(exclusive=True, fixed_set=True)

    offered = {f['name'] for f in _get_active_functions_from_template(template, instance, pool=pool)}

    # The advisor's own fixed disables stay off; the UI-only names stay available.
    assert set(func_map) - offered == _advisor_disabled()
    for tool in UI_ONLY_DISABLED:
        assert tool in offered, f'{tool!r} was disabled by the UI map despite the exclusive flag'


def test_non_exclusive_schema_path_still_applies_live_pool_union():
    """Regression guard: an ordinary Security sub-agent keeps live UI propagation."""
    func_map = _make_func_map(sorted(ADVISOR_AGENT_DISABLED_TOOLS) + list(UI_ONLY_DISABLED))
    template = _StubAgent({}, func_map)
    pool = _StubPool(UI_ONLY_DISABLED)
    instance = _make_instance(exclusive=False, fixed_set=True)

    offered = {f['name'] for f in _get_active_functions_from_template(template, instance, pool=pool)}

    for tool in UI_ONLY_DISABLED:
        assert tool not in offered, f'{tool!r} must stay disabled for a non-exclusive instance'


# ── Gate 2: use-time auto-deny (engine/tool_execution) ───────────────────────────────

def test_use_time_gate_resolves_only_the_override_for_an_exclusive_instance():
    """The use-time denial set for an exclusive advisor is exactly its fixed set.

    The enforcer never reads the UI map, so a divergent UI entry can never reach it — which is why
    the offered-schema union is the only place the exclusive flag is load-bearing."""
    template = _StubAgent({}, {})
    pool = _StubPool(sorted(_advisor_disabled() | set(UI_ONLY_DISABLED)), template=template)
    instance = _make_instance(exclusive=True, fixed_set=True)

    _, disabled, _ = ToolExecMixin._resolve_tool_policy(_StubEngine(pool), instance)

    assert disabled == _advisor_disabled(), (
        'use-time set drifted from the fixed advisor set: '
        f'extra={sorted(disabled - _advisor_disabled())} '
        f'missing={sorted(_advisor_disabled() - disabled)}'
    )
    for tool in UI_ONLY_DISABLED:
        assert tool not in disabled, f'UI-only disabled tool {tool!r} leaked into the advisor set'


# ── Display-only view (agent.py): NOT an enforcement path ────────────────────────────

def test_display_view_still_honors_the_ui_entry():
    """An ordinary Security agent's UI entry keeps resolving in the display view.

    The view is built per TEMPLATE (never per advisor instance), so it intentionally ignores the
    exclusive flag — it is not the enforcement path (see engine/helpers' live-pool gate)."""
    agent = _StubAgent({}, {})
    pool = _StubPool(['grep'])

    disabled = agent._get_disabled_tool_names(pool=pool)

    assert 'grep' in disabled, 'UI per-agent entry must keep governing user/agent-called sub-agents'


# ── Production wiring: the advisor call sites actually set flag + fixed set ──────────

def test_advisor_runner_wires_fixed_set_and_flag_for_security_class():
    """The skill-advisor call site (``agent_class='Security'``) sets the fixed set + the flag.

    The template carries a divergent UI-derived disabled_tools list, so passing proves the fixed
    set won rather than the merge having been skipped."""
    template = _StubAgent({'disabled_tools': list(UI_ONLY_DISABLED)}, {})
    inst = _run_advisor_and_capture('Security', template, instance_name='Security_op_wiring1')

    assert inst._exclusive_disabled_tools is True
    assert inst._generate_cfg_override['disabled_tools'] == sorted(ADVISOR_AGENT_DISABLED_TOOLS)


def test_advisor_runner_leaves_non_security_class_on_its_ui_baseline():
    """A non-Security advisor (the AFK 'Admin' reviewer) keeps the prior UI/template behavior.

    It must NOT get the Security fixed set nor opt out of the UI map: that would swap its
    read-only baseline for the Security set and hand it shell_cmd / web_search / MCP tools.
    Negative control for the Security-class gate."""
    template = _StubAgent({'disabled_tools': ['template_baseline']}, {})
    inst = _run_advisor_and_capture(
        'Admin', template,
        instance_name='Admin_afk_wiring1',
        max_turns=5,
        generate_cfg={'disabled_tools': ['ui_tool'], 'temperature': 0.2})

    assert inst._exclusive_disabled_tools is False, 'a non-Security advisor must not opt out of the live UI map'
    assert inst._generate_cfg_override['disabled_tools'] == ['ui_tool'], \
        'the caller UI set must stay authoritative for a non-Security advisor'
    assert inst._generate_cfg_override['disabled_tools'] != sorted(ADVISOR_AGENT_DISABLED_TOOLS)


# ── Env-override floor (settings.py) ─────────────────────────────────────────────────

def test_empty_advisor_disabled_set_warns_loudly(monkeypatch, caplog):
    """``AGENT_CASCADE_ADVISOR_DISABLED_TOOLS=''`` is a deliberate escape hatch — warn, don't refuse.

    Loads a throwaway copy of settings.py in an isolated namespace (house pattern, see
    tests/test_sanity_probe.py): importlib.reload would mutate the module every worker shares."""
    monkeypatch.setenv('AGENT_CASCADE_ADVISOR_DISABLED_TOOLS', '')
    settings_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 'agent_cascade', 'settings.py')
    spec = importlib.util.spec_from_file_location('_settings_advisor_toolset_copy', settings_path)
    mod = importlib.util.module_from_spec(spec)
    with caplog.at_level(logging.WARNING):
        spec.loader.exec_module(mod)

    assert mod.ADVISOR_AGENT_DISABLED_TOOLS == frozenset()
    lines = [r.getMessage() for r in caplog.records if '[ADVISOR-TOOLSET]' in r.getMessage()]
    assert lines, 'an empty advisor disabled set must warn loudly that the reviewer runs unguarded'
    assert 'UNGUARDED' in lines[-1]
