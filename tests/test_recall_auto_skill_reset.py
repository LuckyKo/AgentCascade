"""Regression test for todo 139: stale auto-skill snapshot on recall.

When an agent instance is reused (recall), its _auto_skill_task_output and
_auto_skill_proposed fields must be cleared so that:
1. The new task's answer isn't shadowed by the old pre-reflection snapshot
   in extract_instance_output().
2. Skill reflection can fire again on the new run (one-shot is per-run, not
   per-object-lifetime).

Run: pytest tests/test_recall_auto_skill_reset.py -v
"""

import threading
from unittest.mock import MagicMock

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.compression.helpers import extract_instance_output
from agent_cascade.lifecycle_manager import AgentLifecycleManager


def _make_pool_mock():
    """Minimal pool mock with the attributes find_or_create_instance touches."""
    pool = MagicMock()
    pool._resolve_instance_name.side_effect = lambda name: name
    pool.instances = {}
    pool._children_lock = threading.Lock()
    pool._update_child_relationship = MagicMock()
    return pool


def _make_idle_instance(name='test-agent'):
    """Create an AgentInstance in IDLE state with stale auto-skill state."""
    inst = AgentInstance(
        instance_name=name,
        agent_class='coder',
        conversation=[{'role': 'user', 'content': 'old task'}],
        created_at=0.0,
        last_activity=0.0,
        latest_marker_index=-1,
    )
    inst.state = AgentState.IDLE
    # Simulate stale state from a previous run that went through extended turns:
    inst._auto_skill_task_output = 'OLD ANSWER from previous task'
    inst._auto_skill_proposed = True
    return inst


def _find_or_create(pool, name):
    """Call find_or_create_instance on a fresh manager backed by the given pool."""
    lm = AgentLifecycleManager.__new__(AgentLifecycleManager)
    lm.pool = pool
    return lm.find_or_create_instance(
        agent_class='coder',
        instance_name=name,
        caller='root',
        nest_depth=1,
    )


class TestRecallAutoSkillReset:
    """Verify auto-skill state is cleared on instance reuse."""

    def test_auto_skill_state_cleared_on_reuse(self):
        """After reuse, both stale auto-skill fields must be reset.

        _auto_skill_task_output (the pre-reflection snapshot) shadows the new
        task's answer in extract_instance_output(); _auto_skill_proposed is the
        one-shot flag that would otherwise permanently disable skill reflection.
        """
        inst = _make_idle_instance()
        pool = _make_pool_mock()
        pool.instances[inst.instance_name] = inst

        result_inst, is_reuse, _ = _find_or_create(pool, 'test-agent')

        assert is_reuse is True
        assert result_inst is inst  # same object reused
        assert result_inst._auto_skill_task_output is None, (
            f"Stale _auto_skill_task_output not cleared on reuse: "
            f"{result_inst._auto_skill_task_output!r}"
        )
        assert result_inst._auto_skill_proposed is False, (
            '_auto_skill_proposed not reset on reuse — skill reflection '
            'would be permanently disabled for this instance'
        )

    def test_extract_output_falls_back_to_messages_after_reuse(self):
        """After reuse clears the snapshot, extract_instance_output returns
        messages[-1] instead of the stale pre-reflection snapshot."""
        inst = _make_idle_instance()
        # Add a new assistant response (simulating the new task's answer):
        inst.conversation.append({'role': 'assistant', 'content': 'NEW ANSWER'})

        pool = _make_pool_mock()
        pool.instances[inst.instance_name] = inst

        result_inst, is_reuse, _ = _find_or_create(pool, 'test-agent')

        assert is_reuse is True
        # Now extract_instance_output should return the NEW answer, not the old snapshot:
        output = extract_instance_output(
            list(result_inst.conversation),
            result_inst.instance_name,
            instance=result_inst,
        )
        assert 'NEW ANSWER' in output, (
            f"extract_instance_output returned stale content after reuse: {output!r}"
        )
        assert 'OLD ANSWER' not in output

    def test_fresh_instance_unaffected(self):
        """A brand-new instance (not reused) should have default auto-skill state."""
        pool = _make_pool_mock()
        # No existing instance → find_or_create creates a new one
        result_inst, is_reuse, _ = _find_or_create(pool, 'fresh-agent')

        assert is_reuse is False
        assert result_inst._auto_skill_task_output is None
        assert result_inst._auto_skill_proposed is False
