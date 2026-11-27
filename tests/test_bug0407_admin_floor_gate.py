"""BUG_0407 (re-scoped) — Admin read-only floor enforced independently of the override carrier.

What the prior test missed
--------------------------
``test_bug0407_afk_admin_tool_set.py`` PRE-SEEDED ``_generate_cfg_override`` WITH the floor and
proved "an override that contains the floor is honored". It did not exercise the live reuse state
where the carrier is rebuilt/nulled WITHOUT the floor by ``run_loop`` (pops disabled_tools on a
final turn) or ``state_builder._apply_ui_config`` (re-bakes the override over all instances). In
that state the one-shot merge at ``system_agent.py:420-426`` is lost before the gate reads it.

The fix (the load-bearing behavior under test)
----------------------------------------------
Class-scoped read-only floors for the oversight classes (Admin, Reviewer), re-applied from the
already-resolved agent_type/class so enforcement no longer depends on the carrier surviving:

1. Offered-schema gate  : ``engine/helpers._get_active_functions_from_template`` (after the UI union).
2. Use-time denial gate : ``engine/tool_execution._resolve_tool_policy`` (after the resolver call).
3. Child-init merge     : ``engine/system_agent._initialize_child_run`` (Layer 1 of the resolver).

SEC-03 also blocks ``call_agent`` in the Admin floor (an oversight agent must not spawn
permissive children) and adds the Reviewer floor
(``REVIEWER_CLASS_READ_ONLY_DISABLED_TOOLS`` = Admin floor + ``__all_mcp_tools__``).

Hermetic: no live LLM, no AgentPool boot, no network, no disk IO. The doubles reproduce exactly the
input surface each real gate reads; the floor is asserted against the REAL gate output, not a stub.
"""

from __future__ import annotations

import pytest

from agent_cascade.constants import (
    ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS,
    REVIEWER_CLASS_READ_ONLY_DISABLED_TOOLS,
)
from agent_cascade.engine.helpers import _get_active_functions_from_template
from agent_cascade.engine.tool_execution import ToolExecMixin
from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent

# Control read-only tools the Admin must keep — NOT in the floor, must stay offered.
_KEEP = ('read_file', 'web_search')
# A harmless UI-disabled tool used only to make the override look realistic; NOT in the floor and
# not in the offered func_map, so it neither affects the floor assertion nor the control assertion.
_UI_DISABLED = 'grep'
_FLOOR = sorted(ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS)


class _Tool:
    """Minimal function_map entry: helpers reads ``func_map[name].function``."""

    def __init__(self, name: str):
        self.name = name
        self.function = {'name': name, 'description': f"tool {name}", 'parameters': {}}


class _LLM:
    def __init__(self, generate_cfg=None):
        self.generate_cfg = generate_cfg if generate_cfg is not None else {}


class _Template:
    """Template double exposing the input surface the gates read. agent_type='' forces the
    gates to fall back to ``instance.agent_class`` (the live AFK-Admin reuse path)."""

    def __init__(self, name, agent_type, func_map, generate_cfg=None):
        self.name = name
        self.agent_type = agent_type
        self.function_map = func_map
        self.llm = _LLM(generate_cfg)


class _Instance:
    """Instance double: instance_name, agent_class, _generate_cfg_override, _exclusive flag."""

    def __init__(self, instance_name, agent_class, cfg_override=None, exclusive=False):
        self.instance_name = instance_name
        self.agent_class = agent_class
        self._generate_cfg_override = cfg_override
        self._exclusive_disabled_tools = exclusive


class _Pool:
    """Pool double mirroring config_persist.AgentPool.get_ui_disabled_tools_for_agent — it
    resolves a live deny-list through the REAL centralized resolver."""

    def __init__(self):
        self._ui_disabled_tools = {}

    def set_ui_disabled_tools(self, disabled_tools_dict=None):
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


class _PolicyHarness(ToolExecMixin):
    """Minimal host for the real ``_resolve_tool_policy`` (the use-time denial gate)."""

    def __init__(self, pool):
        self.pool = pool


class _TemplatePool:
    def __init__(self, template):
        self._template = template

    def get_template(self, agent_class):  # noqa: ARG002 — mirrors pool.get_template(agent_class)
        return self._template


@pytest.fixture
def known_registry(monkeypatch):
    """Make our test tool names 'known' so the use-time registry filter keeps them."""
    known = set(_KEEP) | {_UI_DISABLED} | set(_FLOOR)
    monkeypatch.setattr('agent_cascade.tools.base.TOOL_REGISTRY', dict.fromkeys(known))
    monkeypatch.setattr('agent_cascade.constants.RUNTIME_REGISTERED_TOOLS', frozenset())


def _admin_func_map():
    # func_map MUST contain the floor tools, otherwise "absent from offered" would be trivially true.
    return {n: _Tool(n) for n in (*_FLOOR, *_KEEP)}


# Reviewer floor minus the __all_mcp_tools__ sentinel (a sentinel, not a real tool name, so it
# cannot be a func_map key; it blocks MCP tools by expansion inside order_offered_tool_names).
_REVIEWER_FLOOR_NAMED = {n for n in REVIEWER_CLASS_READ_ONLY_DISABLED_TOOLS if n != '__all_mcp_tools__'}


def _reviewer_func_map():
    # Same non-vacuous rule as _admin_func_map: the named floor tools must be present.
    return {n: _Tool(n) for n in (*sorted(_REVIEWER_FLOOR_NAMED), *_KEEP)}


def _offered_names(template, instance, pool):
    funcs = _get_active_functions_from_template(template, instance, pool)
    return {f['name'] for f in funcs}


# ── Offered-schema gate ──────────────────────────────────────────────────────────────────────

def test_admin_floor_offered_independent_of_override(known_registry):
    """(a) Warm reused AFK-Admin whose override LACKS the floor still has every floor tool
    removed from the OFFERED schema — the new gate re-adds the floor, not the carrier."""
    pool = _Pool()
    pool.set_ui_disabled_tools({'Admin': [_UI_DISABLED]})  # realistic UI config, no floor
    template = _Template(name='Admin', agent_type='', func_map=_admin_func_map())
    # Template lacks agent_type -> gate falls back to instance.agent_class='Admin' (helpers:214-217).
    instance = _Instance('AFK_Admin', agent_class='admin',
                         cfg_override={'disabled_tools': [_UI_DISABLED]}, exclusive=False)

    offered = _offered_names(template, instance, pool)

    for name in _FLOOR:
        assert name not in offered, f"{name!r} (read-only floor) was still offered to AFK-Admin"
    assert set(_KEEP) <= offered, 'control read-only tools were wrongly disabled'


def test_admin_floor_survives_override_rebuild(known_registry):
    """(b) Simulate the carrier being wiped (run_loop.py:249 pops disabled_tools; or
    _apply_ui_config re-bakes without the floor) -> the gate STILL excludes every floor tool.
    Proves enforcement is independent of the carrier surviving.

    NOTE: ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS ⊆ DEFAULT_NEW_AGENT_DISABLED_TOOLS, so once the
    explicit config is gone the read-only safe baseline also blocks the floor. This test therefore
    shows the floor HOLDS after a wipe (via either mechanism), not that THIS gate alone does it.
    The gate-only load-bearing proof is test_floor_gate_is_load_bearing."""
    template = _Template(name='Admin', agent_type='', func_map=_admin_func_map())
    instance = _Instance('AFK_Admin', agent_class='admin',
                         cfg_override={'disabled_tools': [_UI_DISABLED]}, exclusive=False)

    # The exact carrier-erasing mutation the live gap exhibits: drop disabled_tools entirely.
    instance._generate_cfg_override.pop('disabled_tools', None)
    assert 'disabled_tools' not in instance._generate_cfg_override

    offered = _offered_names(template, instance, None)  # pool=None isolates the floor from the UI union

    for name in _FLOOR:
        assert name not in offered, f"floor {name!r} reappeared after carrier rebuild"
    # No control-tools-present assertion here: with no explicit config the resolver's read-only
    # safe baseline legitimately engages and may drop control tools — unrelated to the floor,
    # which is the only thing this scenario asserts.


def test_floor_gate_is_load_bearing(known_registry):
    """(c) Prove the NEW union — not the resolver — is what enforces. The resolver alone (the
    pre-fix ``disabled`` computed by helpers before my added line) does NOT remove floor tools
    from an override that lacks them, so without the new gate those tools WOULD be offered."""
    template = _Template(name='Admin', agent_type='', func_map=_admin_func_map())
    resolver_disabled = resolve_disabled_tools_for_agent(
        instance_override={'disabled_tools': [_UI_DISABLED]},
        template_cfg=template.llm.generate_cfg,
        agent_name=template.name,
        agent_type='admin',
    )
    for name in _FLOOR:
        assert name not in resolver_disabled, (
            'resolver unexpectedly blocks the floor — the mutation check below is meaningless')
    # i.e. resolver-only disabled set would leave every floor tool OFFERED -> new gate is required.

    # ...and the real gate (which adds the floor union on top) blocks them:
    instance = _Instance('AFK_Admin', agent_class='admin',
                         cfg_override={'disabled_tools': [_UI_DISABLED]}, exclusive=False)
    offered = _offered_names(template, instance, None)
    for name in _FLOOR:
        assert name not in offered


def test_non_admin_floor_not_widened(known_registry):
    """Control: the additive floor is class-scoped to 'admin' only — a coder keeps the floor
    tools offered (not weakened/forced off), confirming no other agent is affected."""
    coder_template = _Template(name='Coder', agent_type='coder', func_map=_admin_func_map())
    coder = _Instance('coder_child', agent_class='coder',
                      cfg_override={'disabled_tools': [_UI_DISABLED]}, exclusive=False)

    offered = _offered_names(coder_template, coder, None)
    assert 'shell_cmd' in offered, 'floor must NOT be applied to non-admin classes'


# ── Use-time denial gate ───────────────────────────────────────────────────────────────────────

def test_use_time_denial_for_admin(known_registry):
    """The use-time gate denies every floor tool for an admin whose override LACKS the floor."""
    template = _Template(name='Admin', agent_type='', func_map=_admin_func_map())
    harness = _PolicyHarness(_TemplatePool(template))
    instance = _Instance('AFK_Admin', agent_class='admin',
                         cfg_override={'disabled_tools': [_UI_DISABLED]}, exclusive=False)

    _, disabled_tools, _ = harness._resolve_tool_policy(instance)
    for name in _FLOOR:
        assert name in disabled_tools, f"{name!r} not denied at use-time for admin"


def test_use_time_denial_survives_override_rebuild(known_registry):
    """Use-time enforcement is independent of the carrier: even with an empty override
    (carrier fully wiped), the floor tools remain denied.

    NOTE: ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS ⊆ DEFAULT_NEW_AGENT_DISABLED_TOOLS, so once the
    explicit config is gone the read-only safe baseline also blocks the floor (mirrors
    test_admin_floor_survives_override_rebuild). This test shows the floor HOLDS after a wipe;
    the gate-only load-bearing proof is test_use_time_denial_for_admin (override present)."""
    template = _Template(name='Admin', agent_type='', func_map=_admin_func_map())
    harness = _PolicyHarness(_TemplatePool(template))
    instance = _Instance('AFK_Admin', agent_class='admin', cfg_override=None, exclusive=False)

    _, disabled_tools, _ = harness._resolve_tool_policy(instance)
    for name in _FLOOR:
        assert name in disabled_tools, 'floor tool not denied after carrier wipe'


def test_use_time_control_non_admin(known_registry):
    """Control: a non-admin does not get the floor applied at use-time."""
    coder_template = _Template(name='Coder', agent_type='coder', func_map=_admin_func_map())
    harness = _PolicyHarness(_TemplatePool(coder_template))
    coder = _Instance('coder_child', agent_class='coder',
                      cfg_override={'disabled_tools': [_UI_DISABLED]}, exclusive=False)

    _, disabled_tools, _ = harness._resolve_tool_policy(coder)
    assert 'shell_cmd' not in disabled_tools, 'floor must not deny tools for non-admin classes'


# ── SEC-03: Reviewer floor + Admin call_agent addition ───────────────────────────────────────

def test_admin_use_time_floor_includes_call_agent(known_registry):
    """SEC-03 explicit pin: the Admin read-only floor now denies ``call_agent`` at use-time —
    an oversight agent must not spawn children that inherit a permissive toolset."""
    template = _Template(name='Admin', agent_type='', func_map=_admin_func_map())
    harness = _PolicyHarness(_TemplatePool(template))
    instance = _Instance('AFK_Admin', agent_class='admin',
                         cfg_override={'disabled_tools': [_UI_DISABLED]}, exclusive=False)

    _, disabled_tools, _ = harness._resolve_tool_policy(instance)
    assert 'call_agent' in disabled_tools, 'Admin floor must deny call_agent (SEC-03)'
    assert set(_FLOOR) <= disabled_tools, 'Admin floor tools not all denied at use-time'


def test_use_time_denial_for_reviewer(known_registry):
    """The use-time gate denies the FULL Reviewer floor (Admin floor + __all_mcp_tools__) for a
    reviewer whose override LACKS the floor. The floor union is applied AFTER the resolver's
    use-time registry filter, so even the __all_mcp_tools__ sentinel lands in disabled_tools."""
    template = _Template(name='Reviewer', agent_type='', func_map=_reviewer_func_map())
    harness = _PolicyHarness(_TemplatePool(template))
    instance = _Instance('reviewer_child', agent_class='reviewer',
                         cfg_override={'disabled_tools': [_UI_DISABLED]}, exclusive=False)

    _, disabled_tools, _ = harness._resolve_tool_policy(instance)
    for name in sorted(REVIEWER_CLASS_READ_ONLY_DISABLED_TOOLS):
        assert name in disabled_tools, f"{name!r} (Reviewer floor) not denied at use-time"


def test_use_time_control_non_floor_classes(known_registry):
    """Control: neither floor is applied to non-floor classes — they keep shell/write/call_agent
    and are NOT forced to disable __all_mcp_tools__."""
    _NOT_FORCED = ('shell_cmd', 'write_file', 'call_agent', '__all_mcp_tools__')
    for cls in ('coder', 'generalist'):
        template = _Template(name=cls.title(), agent_type=cls, func_map=_reviewer_func_map())
        harness = _PolicyHarness(_TemplatePool(template))
        inst = _Instance(f'{cls}_child', agent_class=cls,
                         cfg_override={'disabled_tools': [_UI_DISABLED]}, exclusive=False)

        _, disabled_tools, _ = harness._resolve_tool_policy(inst)
        for name in _NOT_FORCED:
            assert name not in disabled_tools, f"{name!r} floor-forced onto non-floor class {cls!r}"


def test_reviewer_floor_offered_independent_of_override(known_registry):
    """The Reviewer floor strips every named floor tool from the OFFERED schema even when the
    override lacks it — mirroring test_admin_floor_offered_independent_of_override."""
    template = _Template(name='Reviewer', agent_type='reviewer', func_map=_reviewer_func_map())
    instance = _Instance('reviewer_child', agent_class='reviewer',
                         cfg_override={'disabled_tools': [_UI_DISABLED]}, exclusive=False)

    offered = _offered_names(template, instance, None)  # pool=None isolates the floor from the UI union
    for name in _REVIEWER_FLOOR_NAMED:
        assert name not in offered, f"{name!r} (Reviewer floor) was still offered to a reviewer"
    assert set(_KEEP) <= offered, 'control read-only tools were wrongly disabled'


def test_reviewer_floor_not_widened_to_non_reviewer(known_registry):
    """Control: the additive Reviewer floor is class-scoped — a coder keeps the floor tools
    offered (not weakened), confirming no other class is affected."""
    coder_template = _Template(name='Coder', agent_type='coder', func_map=_reviewer_func_map())
    coder = _Instance('coder_child', agent_class='coder',
                      cfg_override={'disabled_tools': [_UI_DISABLED]}, exclusive=False)

    offered = _offered_names(coder_template, coder, None)
    for name in ('shell_cmd', 'call_agent'):
        assert name in offered, f"Reviewer floor must NOT apply to non-reviewer class (got {name!r} denied)"
