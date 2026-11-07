"""
BUG_0265 regression: the fixed warm-reuse Security instances must never be auto-dismissed.

Root cause (``.bug_tracker/BUG_0265_prompt_cache_misses_from_instance_cold_start_and_rehydration.md``):
the Security warm-reuse feature keeps one long-lived instance (``Security_guard`` /
``Security_skilladvisor``) warm for KV/prompt-prefix reuse, but ``IdleManager._is_idle`` had no
exemption for those fixed names. Any idle gap > ``system_agent_idle_timeout_seconds`` auto-dismissed
the warm instance, so the next security check re-seeded a cold one (new logger session, cold prompt
prefix -> cache MISS). The fix exempts the two ``SECURITY_REUSE_*_NAME`` constants in ``_is_idle``.

These tests pin the two halves of the fix:
- the reserved warm-reuse names are NEVER idle (no auto-dismissal), even far beyond the timeout;
- every OTHER instance can still be reclaimed (idle reclamation is not globally disabled).

Mirrors the mock-pool shape of
``test_bug1_bug6_bug4_8_slot_deadlock.py::test_idle_checker_skips_preserved_sleeping_agent``.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.llm.schema import Message
from agent_cascade.pool.idle_manager import IdleManager
from agent_cascade.settings import (
    SECURITY_REUSE_APPROVAL_NAME,
    SECURITY_REUSE_SKILL_ADVISOR_NAME,
)


def _idle_eligible(name: str, agent_class: str = 'security') -> AgentInstance:
    """An instance idle-eligible in EVERY respect except its name: parent set (so not the
    orchestrator), IDLE, last_activity far past any timeout. This lets the tests isolate the
    reuse-name exemption as the only variable that changes the outcome."""
    return AgentInstance(
        instance_name=name,
        agent_class=agent_class,
        conversation=[Message(role='system', content='sys')],
        created_at=time.monotonic(),
        last_activity=time.monotonic() - 10_000.0,  # idle >> system/idle timeouts
        latest_marker_index=0,
        parent_instance='Main',
    )


def _manager(*instances: AgentInstance) -> IdleManager:
    """IdleManager wired to a MagicMock pool: not on the active stack, not halted, generous
    timeout settings. Built via __new__ (same as the existing _is_idle tests) to skip __init__."""
    pool = MagicMock()
    pool.instances = {inst.instance_name: inst for inst in instances}
    pool._execution.active_stack = []
    pool.is_instance_halted.return_value = False
    pool.settings = MagicMock(idle_timeout_seconds=60.0, system_agent_idle_timeout_seconds=300.0)
    manager = IdleManager.__new__(IdleManager)
    manager.pool = pool
    return manager


class TestWarmReuseNamesNeverIdle:
    """The reserved warm-reuse names are exempt from auto-dismissal (BUG_0265)."""

    @pytest.mark.parametrize('reuse_name', [
        SECURITY_REUSE_APPROVAL_NAME,
        SECURITY_REUSE_SKILL_ADVISOR_NAME,
    ])
    def test_warm_reuse_name_never_idle_beyond_timeout(self, reuse_name):
        inst = _idle_eligible(reuse_name)
        assert inst.state == AgentState.IDLE  # otherwise-eligible for dismissal
        # Would be idle-True absent the exemption: idle >> timeout, IDLE, not halted/on-stack.
        assert _manager(inst)._is_idle(reuse_name) is False


class TestNonReuseStillReclaimable:
    """The exemption must not disable idle reclamation for any other instance."""

    def test_non_reuse_instance_can_still_be_idle(self):
        inst = _idle_eligible('Compressor_3', agent_class='compressor')
        assert inst.state == AgentState.IDLE
        # System-agent timeout path; the name is not in the reuse exemption set.
        with patch('agent_cascade.pool.idle_manager.IdleManager._is_system_agent', return_value=True):
            assert _manager(inst)._is_idle('Compressor_3') is True
