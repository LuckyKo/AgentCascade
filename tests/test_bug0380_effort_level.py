"""Effort slider (low|medium|high|ultra) enforcement tests (BUG_0380).

Covers the four cost-control gates a notch drives, plus the pure helpers, the
registry wiring, the WS round-trip, and the CRITICAL invariant that the
auto-skill reflection extension cannot silently defeat a capped notch.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.agent_instance import PoolSettings
from agent_cascade.settings import (
    DEFAULT_MAX_TURNS,
    EFFORT_LEVEL_DEFAULT,
    EFFORT_NESTED_CAPS,
    EFFORT_TURN_CAPS,
    SOFT_TURN_LIMIT_ABSOLUTE_MAX,
    normalize_effort_level,
    effort_budgeting_forced_off,
    effective_turn_cap,
    effective_soft_limit_cap,
)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_default_notch_is_ultra():
    # Zero-surprise upgrade: the shipped default imposes no cap.
    assert EFFORT_LEVEL_DEFAULT == 'ultra'


@pytest.mark.parametrize('raw,expected', [
    ('low', 'low'),
    ('Medium', 'medium'),
    (' HIGH ', 'high'),
    ('ULTRA', 'ultra'),
])
def test_normalize_accepts_known_notches(raw, expected):
    assert normalize_effort_level(raw) == expected


@pytest.mark.parametrize('bad', [None, '', '   ', 'bogus', 123, False])
def test_normalize_falls_back_to_default(bad):
    # PM override: unknown/invalid values fall back to EFFORT_LEVEL_DEFAULT ('ultra'),
    # NOT a hardcoded 'medium' — an unrecognized value must never silently down-cap.
    assert normalize_effort_level(bad) == EFFORT_LEVEL_DEFAULT


def test_effective_turn_cap_table():
    # ultra: no notch cap, passes the base through (defer to Max Turns per Run).
    assert effective_turn_cap(None, 'ultra') == DEFAULT_MAX_TURNS
    assert effective_turn_cap(120, 'ultra') == 120
    # low/med/high clamp a request above the notch DOWN to the notch.
    assert effective_turn_cap(None, 'low') == EFFORT_TURN_CAPS['low']    # 30
    assert effective_turn_cap(500, 'low') == EFFORT_TURN_CAPS['low']      # 30
    # a request already below the notch is honored (clamp never inflates to the notch).
    assert effective_turn_cap(80, 'high') == 80
    assert effective_turn_cap(10, 'low') == 10
    # For a non-ultra notch the effective floor is the notch itself (>= 30): a falsy request
    # falls back to DEFAULT_MAX_TURNS, then min() clamps down to the notch.
    assert effective_turn_cap(0, 'low') == EFFORT_TURN_CAPS['low']        # 30


@pytest.mark.parametrize('level,expected', [
    ('low', True), ('medium', True), ('high', True),
    ('ultra', False), ('bogus', False),  # unknown -> ultra -> not forced off
])
def test_effort_budgeting_forced_off(level, expected):
    assert effort_budgeting_forced_off(level) is expected


def test_effective_soft_limit_cap():
    # ultra keeps the existing soft ceiling (>= initial, else SOFT abs max).
    assert effective_soft_limit_cap(50, 'ultra') == max(50, SOFT_TURN_LIMIT_ABSOLUTE_MAX)
    # low/med/high bound the soft path to the notch, so soft_turn_limit cannot
    # silently exceed the effort ceiling.
    assert effective_soft_limit_cap(50, 'high') == EFFORT_TURN_CAPS['high']   # 100
    assert effective_soft_limit_cap(500, 'low') == EFFORT_TURN_CAPS['low']    # 30


# ── Registry + NON_LLM guard ──────────────────────────────────────────────────

def test_registry_validation_and_spec_present():
    from agent_cascade.settings_registry import SETTINGS_REGISTRY, validate_registry
    validate_registry()               # raises AssertionError on inconsistency (incl. key count)
    assert 'effort_level' in SETTINGS_REGISTRY


def test_effort_level_is_non_llm_key():
    from agent_cascade.constants import NON_LLM_KEYS
    assert 'effort_level' in NON_LLM_KEYS


# ── WS round-trip through the delta pipeline ──────────────────────────────────

class _MockRouter:
    def __init__(self):
        self.default_llm_cfg = {}
        self.llm_updates = []

    def update_default_llm_cfg(self, cfg):
        self.default_llm_cfg.update(cfg)
        self.llm_updates.append(dict(cfg))


class _MockPool:
    def __init__(self, tmp_path):
        import threading
        from agent_cascade.runtime_state import state as rs
        self.settings = PoolSettings()
        self.instances = {}
        self.templates = {}
        self.telemetry = None
        self.stopped = False
        self.llm_cfg = {}
        self.api_router = _MockRouter()
        self._ui_disabled_tools = {}
        self._ui_disabled_tools_lock = threading.Lock()
        self.operation_manager = None
        self._enable_async_shell_console_window = False
        self.auto_security = False
        self._pool_settings_path = tmp_path / 'pool_settings.json'
        self._settings_save_lock = threading.Lock()
        self.save_calls = 0
        self._rs = rs

    def _save_pool_settings(self):
        self.save_calls += 1
        self._rs.bump_settings_revision()

    def set_auto_security(self, enabled):
        self.auto_security = bool(enabled)


def test_effort_level_ws_round_trip(tmp_path):
    from agent_cascade.settings_delta import apply_settings_changes

    pool = _MockPool(tmp_path)
    applied, rejected, changes_out, _rev = asyncio.run(
        apply_settings_changes(pool, [], {'effort_level': 'high'}))

    assert rejected == {}
    assert 'effort_level' in applied
    assert changes_out['effort_level'] == 'high'
    # The registered handler stored the normalized notch on the live pool.
    assert pool.settings.effort_level == 'high'


# ── Run-loop helpers (entry clamp + auto-skill suppression gate) ──────────────

class _StubLoop:
    """Minimal stand-in exercising RunLoopMixin's effort helpers in isolation."""
    def __init__(self, level):
        from agent_cascade.engine.run_loop import RunLoopMixin
        self._level = level
        self._mixin = RunLoopMixin
        self._pool_setting = level

    def _get_pool_setting(self, key, default=None):
        return self._level if key == 'effort_level' else default

    def _effort_level(self):
        from agent_cascade.engine.run_loop import RunLoopMixin
        return RunLoopMixin._effort_level(self)

    def _effort_max_turns(self, requested):
        from agent_cascade.engine.run_loop import RunLoopMixin
        return RunLoopMixin._effort_max_turns(self, requested)

    def _effort_allows_auto_skill(self):
        from agent_cascade.engine.run_loop import RunLoopMixin
        return RunLoopMixin._effort_allows_auto_skill(self)


def test_run_loop_entry_clamp():
    for level in ('low', 'medium', 'high'):
        assert _StubLoop(level)._effort_max_turns(9999) == EFFORT_TURN_CAPS[level]
    # ultra: passthrough of the instance's own bound.
    assert _StubLoop('ultra')._effort_max_turns(9999) == 9999
    assert _StubLoop('ultra')._effort_max_turns(None) == DEFAULT_MAX_TURNS


def test_auto_skill_suppression_gate():
    # CRITICAL: low/med/high suppress the reflection extension, so a run cannot be
    # silently extended past the notch. ultra preserves existing behavior.
    for level in ('low', 'medium', 'high'):
        assert _StubLoop(level)._effort_allows_auto_skill() is False
    assert _StubLoop('ultra')._effort_allows_auto_skill() is True
    # Combined invariant: for a capped notch the ceiling holds AND no extension is
    # granted, so the auto-skill path cannot defeat the cap.
    stub = _StubLoop('low')
    assert stub._effort_max_turns(9999) == EFFORT_TURN_CAPS['low']
    assert stub._effort_allows_auto_skill() is False


# ── Nested-depth gate ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('level,server_cap,child_depth,exceeded', [
    ('low', 10, 1, False),   # notch 1 -> child depth 1 allowed
    ('low', 10, 2, True),    # notch 1 -> child depth 2 rejected
    ('ultra', 10, 10, False),  # notch None -> defer to server cap
    ('ultra', 10, 11, True),
    ('high', 2, 3, True),    # min(server=2, notch=3)=2 -> depth 3 rejected
    ('high', 10, 3, False),  # min(server=10, notch=3)=3 -> depth 3 allowed
])
def test_nesting_depth_notch_cap(level, server_cap, child_depth, exceeded):
    from agent_cascade.tool_dispatcher import ToolDispatcher
    disp = ToolDispatcher.__new__(ToolDispatcher)
    disp.pool = SimpleNamespace(settings=SimpleNamespace(
        max_nesting_depth=server_cap, effort_level=level))
    instance = SimpleNamespace(instance_name='caller')
    result = disp._check_nesting_depth(instance, child_depth)
    assert (result is not None) is exceeded
