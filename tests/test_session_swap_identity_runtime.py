"""Verification tests for BUG_0364 — identity is a runtime concern; log is preserved.

The JSONL log is a historical record: the system line keeps the soul-derived
name (e.g. "You are Orchestrator.") and is never patched. The runtime identity
is rewritten in-memory by turn_setup on every turn. After a swap, the first
turn self-heals the live prompt to "You are {instance_name}." while the log
keeps its original line. That divergence is accepted by design.

These tests verify:
  1. The live (in-memory) system prompt self-heals to the instance name after
     a swap + one turn-setup pass.
  2. The log file retains the original soul name (no identity patch).

Test-isolation rule: no sys.modules rebinding — plain package imports + mock
pools only.
"""
import json
import os
import threading
from unittest.mock import MagicMock

import pytest

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.llm.schema import Message, SYSTEM, USER
from agent_cascade.pool.session_io import SessionIOMixin


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_log_file(tmp_path, instance_name='Maine'):
    """Create a JSONL log whose system line is 'You are Orchestrator.'."""
    log_name = f'orchestrator_{instance_name}_identity.jsonl'
    log_file = tmp_path / log_name
    lines = [
        {'metadata': {
            'agent_class': 'orchestrator',
            'instance_name': instance_name,
            'current_log_path': str(log_file),
        }},
        {'role': 'system', 'content': 'You are Orchestrator. Technical lead.'},
        {'role': 'user', 'content': 'Do the thing.'},
        {'role': 'assistant', 'content': 'Done.'},
    ]
    with open(log_file, 'w', encoding='utf-8') as f:
        for m in lines:
            f.write(json.dumps(m) + '\n')
    return str(log_file)


def _make_pool(tmp_path):
    """A mock pool sufficient to drive the REAL load_session_from_log."""
    class MockPool(SessionIOMixin):
        def __init__(self):
            self.instances = {}
            self.instance_state = {}
            self.instance_summaries = {}
            self.children = {}
            self.terminated_instances = set()
            self._halted_instances = set()
            self._compression_halted = set()
            self._instances_version = 0
            self._children_lock = threading.Lock()
            self._execution = MagicMock()
            self._execution._state_lock = threading.RLock()
            self._logger = MagicMock()
            self._logger.log_dir = str(tmp_path)
            self._logger.workspace_dir = None
            self._logger._loggers = {}
            om = MagicMock()
            om.base_dir = str(tmp_path)
            om.extra_work_folders_ro = []
            om.extra_work_folders_rw = []
            self.operation_manager = om

        def _resolve_instance_name(self, name, exclude=None):
            return name

        def _dismiss_all_instances(self, exclude=None):
            pass

    return MockPool()


def _run_turn_setup(pool, instance):
    """Run the identity-rewrite portion of _setup_turn on the instance.

    Uses TurnSetupMixin._refresh_system_prompt directly (the method that
    performs the in-memory identity rewrite) with a minimal engine double.
    """
    from agent_cascade.engine.turn_setup import TurnSetupMixin

    class _Engine(TurnSetupMixin):
        def __init__(self, pool_ref):
            self.pool = pool_ref

    engine = _Engine(pool)
    conv = list(instance.conversation)
    # template=None → no template-derived system message injection;
    # the identity rewrite (step 1) still runs on the existing system message.
    engine._refresh_system_prompt(instance, instance.instance_name, conv, None)
    # Sync the (possibly rewritten) conversation back onto the instance.
    instance.conversation = conv
    return conv


# ── Tests ──────────────────────────────────────────────────────────────────


class TestIdentitySelfHealAfterSwap:

    def test_live_prompt_self_heals_to_instance_name(self, tmp_path):
        """After a swap + one turn-setup pass, the in-memory system prompt
        starts with 'You are {instance_name}.' (not the soul name)."""
        log_file = _make_log_file(tmp_path, instance_name='Maine')
        pool = _make_pool(tmp_path)

        status = pool.load_session_from_log(
            log_file, target_instance='Maine', clear_sub_agents_before_load=False)
        assert not str(status).startswith('Error'), f"session load failed: {status}"

        inst = pool.instances['Maine']
        assert inst is not None, 'instance not installed by load_session_from_log'

        # Before turn setup, the in-memory system prompt has the soul name.
        sys_msg = inst.conversation[0]
        assert 'You are Orchestrator.' in sys_msg.content, (
            f"expected soul name in pre-setup system prompt, got: {sys_msg.content[:80]!r}")

        # Run the identity rewrite.
        conv = _run_turn_setup(pool, inst)

        # After the rewrite, the live system prompt has the instance name.
        assert conv[0].content.startswith('You are Maine.'), (
            f"live system prompt should start with 'You are Maine.', "
            f"got: {conv[0].content[:80]!r}")

    def test_log_file_retains_soul_name(self, tmp_path):
        """The log file's system line must still say 'You are Orchestrator.'
        after the swap + turn-setup — no identity patch is written to disk."""
        log_file = _make_log_file(tmp_path, instance_name='Maine')
        pool = _make_pool(tmp_path)

        status = pool.load_session_from_log(
            log_file, target_instance='Maine', clear_sub_agents_before_load=False)
        assert not str(status).startswith('Error'), f"session load failed: {status}"

        inst = pool.instances['Maine']
        _run_turn_setup(pool, inst)

        # Read the log file back and verify the system line is unchanged.
        with open(log_file, 'r', encoding='utf-8') as f:
            log_lines = f.readlines()
        # Find the system message line (first non-metadata line with role=system).
        sys_lines = [
            json.loads(l) for l in log_lines[1:]
            if l.strip() and json.loads(l).get('role') == 'system'
        ]
        assert sys_lines, 'no system message found in log file'
        assert 'You are Orchestrator.' in sys_lines[0]['content'], (
            'log file system line was patched to the instance name — '
            'the log must retain the original soul name')

    def test_no_new_file_created_on_same_identity_swap(self, tmp_path):
        """A same-identity swap must not mint a new timestamped log file."""
        log_file = _make_log_file(tmp_path, instance_name='Maine')
        before = {
            f for f in os.listdir(str(tmp_path))
            if f.startswith('orchestrator_Maine') and f.endswith('.jsonl')
        }
        pool = _make_pool(tmp_path)

        status = pool.load_session_from_log(
            log_file, target_instance='Maine', clear_sub_agents_before_load=False)
        assert not str(status).startswith('Error'), f"session load failed: {status}"

        after = {
            f for f in os.listdir(str(tmp_path))
            if f.startswith('orchestrator_Maine') and f.endswith('.jsonl')
        }
        new_files = after - before
        assert not new_files, (
            f"same-identity swap created new log file(s): {new_files}")
