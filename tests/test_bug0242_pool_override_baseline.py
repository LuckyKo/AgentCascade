# ============================================================================
# AgentCascade — BUG_0242 Regression Tests
# ============================================================================
# BUG_0242: Sub-agents (coder/researcher) spawned while the caller's
# _generate_cfg_override is None/unwarmed got the read-only baseline
# DEFAULT_NEW_AGENT_DISABLED_TOOLS attached to their override, because the
# resolver's Layer-4 gate saw has_explicit_config=False. The live pool
# per-agent entry was only unioned AFTER resolution (post-hoc, can only ADD
# disables), so it could never re-enable a baseline tool.
#
# Fix under test (agent_cascade/lifecycle_manager.py, propagate_settings):
# the live pool entry for the child's class is read BEFORE the resolver call
# and passed through instance_override, forcing has_explicit_config=True so
# the baseline is skipped. Soul/discovered agents WITHOUT a pool entry must
# still receive the baseline (intended read-only-by-default behavior).
# ============================================================================

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.constants import DEFAULT_NEW_AGENT_DISABLED_TOOLS
from agent_cascade.lifecycle_manager import AgentLifecycleManager
from agent_cascade.llm.schema import SYSTEM, USER, Message
from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent

# Representative members of DEFAULT_NEW_AGENT_DISABLED_TOOLS (real registered
# tools) — used to detect baseline attachment at the enforcement gates.
BASELINE_SAMPLE = frozenset(('shell_cmd', 'write_file', 'edit_file', 'code_interpreter', 'delete_file'))


def _make_template(name, agent_type, generate_cfg=None):
    """Minimal template double: .name, .agent_type, .llm.generate_cfg."""
    return SimpleNamespace(
        name=name,
        agent_type=agent_type,
        llm=SimpleNamespace(generate_cfg=generate_cfg),
    )


def _make_caller(override=None):
    """Caller instance double with only the attributes propagate_settings reads."""
    return SimpleNamespace(
        agent_class='caller_cls',
        _generate_cfg_override=override,
        max_turns=50,
    )


def _make_child(instance_name='coder1'):
    return AgentInstance(
        instance_name=instance_name,
        agent_class='child_cls',
        conversation=[],
        max_turns=None,
        parent_instance='main',
        created_at=0.0,
        last_activity=0.0,
        compression_summary=None,
        latest_marker_index=-1,
    )


def _make_pool(caller_inst, ui_disabled=None, ui_accessor=None):
    """Pool double covering every attribute propagate_settings/initialize_conversation touch.

    ui_disabled: dict keyed by agent name/type -> iterable of disabled tool names,
        mimicking pool.config_persist.get_ui_disabled_tools_for_agent semantics
        (returns a SET of disabled tool names; empty set when no entry exists).
    ui_accessor: raw callable override for defensive tests.
    """
    pool = MagicMock()
    pool.api_router = MagicMock()
    pool.settings = MagicMock(enable_agent_budgeting=False, tail_sync_check_enabled=False)
    pool._state_lock = threading.RLock()
    pool.get_instance = lambda name: caller_inst if name == 'main' else None

    def get_template(agent_class):
        return {
            'caller_cls': CALLER_TMPL,
            'child_cls': CHILD_CODER_TMPL,
            'soul_cls': SOUL_TMPL,
        }.get(agent_class)

    pool.get_template = get_template

    if ui_accessor is not None:
        pool.get_ui_disabled_tools_for_agent = ui_accessor
    else:
        ui = ui_disabled or {}

        def ui_disabled_for(agent_name='', agent_type=''):
            out = set()
            for key in (agent_name, agent_type):
                if key:
                    out |= set(ui.get(key, ()))
            return out

        pool.get_ui_disabled_tools_for_agent = ui_disabled_for
    return pool


CALLER_TMPL = _make_template('Main', 'orchestrator', generate_cfg=None)
CHILD_CODER_TMPL = _make_template('Coder', 'coder', generate_cfg=None)
SOUL_TMPL = _make_template('Mystic', 'mystic', generate_cfg=None)


def _propagate(pool, child, agent_class='child_cls'):
    mgr = AgentLifecycleManager(pool)
    mgr.propagate_settings(child, 'main', agent_class)
    return child


def _resolved_at_enforcement(child, child_tmpl, agent_name='Coder', agent_type='coder'):
    """Re-solve disabled tools exactly as both enforcement gates do
    (engine/helpers._get_active_functions_from_template and
    engine/tool_execution._resolve_tool_policy): from the child's override."""
    return resolve_disabled_tools_for_agent(
        instance_override=child._generate_cfg_override,
        template_cfg=child_tmpl.llm.generate_cfg,
        agent_name=agent_name,
        agent_type=agent_type,
    )


# ---------------------------------------------------------------------------
# (a) Built-in class WITH a live pool entry, caller override None:
#     pool entry wins, baseline NOT attached.
# ---------------------------------------------------------------------------
def test_none_override_caller_pool_entry_skips_baseline():
    caller = _make_caller(override=None)
    pool = _make_pool(caller, ui_disabled={'Coder': ['forget_last']})
    child = _propagate(pool, _make_child())

    assert child._generate_cfg_override is not None
    cfg_disabled = set(child._generate_cfg_override.get('disabled_tools', ()))
    # Pool entry applied...
    assert 'forget_last' in cfg_disabled
    # ...and the read-only baseline was NOT attached.
    assert not (cfg_disabled & BASELINE_SAMPLE)
    # Both enforcement gates resolve from the same override → no truncation.
    resolved = _resolved_at_enforcement(child, CHILD_CODER_TMPL)
    assert not (resolved & BASELINE_SAMPLE)


# ---------------------------------------------------------------------------
# (b) Soul/discovered agent WITHOUT a pool entry: baseline still applies.
# ---------------------------------------------------------------------------
def test_soul_agent_without_pool_entry_keeps_baseline():
    caller = _make_caller(override=None)
    pool = _make_pool(caller, ui_disabled={})
    child = _make_child('mystic1')
    _propagate(pool, child, agent_class='soul_cls')

    assert child._generate_cfg_override is not None
    resolved = _resolved_at_enforcement(child, SOUL_TMPL, agent_name='Mystic', agent_type='mystic')
    # Intended behavior: read-only-by-default for non-exempt types with no config.
    assert BASELINE_SAMPLE <= resolved


# ---------------------------------------------------------------------------
# (c1) Caller with explicit flat disables, no pool entry: disables inherited.
#      Note the resolver treats the caller's explicit config as explicit config
#      for the child resolution too (has_explicit_config=True), so the baseline
#      is intentionally NOT attached — pre-existing semantics, unchanged by the
#      BUG_0242 fix (this path receives the identical instance_override before
#      and after the fix).
# ---------------------------------------------------------------------------
def test_caller_flat_disables_inherited_without_pool_entry():
    caller = _make_caller(override={'disabled_tools': ['web_search']})
    pool = _make_pool(caller, ui_disabled={})
    child = _propagate(pool, _make_child())

    cfg_disabled = set(child._generate_cfg_override.get('disabled_tools', ()))
    assert 'web_search' in cfg_disabled  # caller explicit disable inherited
    assert cfg_disabled == {'web_search'}  # explicit config present → baseline skipped by design


# ---------------------------------------------------------------------------
# (c2) Caller per-agent dict + live pool entry for the same child: both
#      disables kept, baseline skipped.
# ---------------------------------------------------------------------------
def test_caller_dict_disables_and_pool_entry_merge_without_baseline():
    caller = _make_caller(override={'disabled_tools': {'Coder': ['propose_skill']}})
    pool = _make_pool(caller, ui_disabled={'Coder': ['forget_last']})
    child = _propagate(pool, _make_child())

    cfg_disabled = set(child._generate_cfg_override.get('disabled_tools', ()))
    assert 'propose_skill' in cfg_disabled  # caller's explicit child entry preserved
    assert 'forget_last' in cfg_disabled    # live pool entry applied
    assert not (cfg_disabled & BASELINE_SAMPLE)  # baseline skipped


# ---------------------------------------------------------------------------
# (d) Reuse path: initialize_conversation() nulls the override (poison point
#     ~lifecycle_manager.py:445), then propagate_settings must still produce a
#     non-baseline override when a live pool entry exists.
# ---------------------------------------------------------------------------
def test_reuse_path_nulls_override_then_pool_entry_skips_baseline():
    caller = _make_caller(override=None)
    pool = _make_pool(caller, ui_disabled={'Coder': ['forget_last']})
    child = _make_child('coder1')
    child.conversation = [Message(role=SYSTEM, content='You are Coder v1.')]
    child._generate_cfg_override = {'disabled_tools': ['stale_tool_from_previous_run']}

    mgr = AgentLifecycleManager(pool)
    mgr.initialize_conversation(
        child,
        Message(role=SYSTEM, content='You are Coder v2.'),
        Message(role=USER, content='New task.'),
        True,
        'coder1',
        'child_cls',
    )
    # Document the poison: reuse nulls the override, so without the fix the
    # subsequent propagation would resolve from None → baseline attaches.
    assert child._generate_cfg_override is None

    mgr.propagate_settings(child, 'main', 'child_cls')
    cfg_disabled = set(child._generate_cfg_override.get('disabled_tools', ()))
    assert 'forget_last' in cfg_disabled
    assert 'stale_tool_from_previous_run' not in cfg_disabled  # stale config dropped by reuse
    assert not (cfg_disabled & BASELINE_SAMPLE)
    resolved = _resolved_at_enforcement(child, CHILD_CODER_TMPL)
    assert not (resolved & BASELINE_SAMPLE)


# ---------------------------------------------------------------------------
# Defensive: a non-collection pool accessor result (e.g. bare MagicMock from a
# test double, or None) must fall back to the old caller-override behavior —
# for a None-override caller that means the baseline still applies.
# ---------------------------------------------------------------------------
def test_non_collection_pool_result_falls_back_to_baseline():
    caller = _make_caller(override=None)
    pool = _make_pool(caller, ui_accessor=lambda agent_name='', agent_type='': MagicMock())
    child = _propagate(pool, _make_child())

    resolved = _resolved_at_enforcement(child, CHILD_CODER_TMPL)
    assert 'shell_cmd' in resolved  # baseline preserved (no usable pool entry)
