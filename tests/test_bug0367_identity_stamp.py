"""Identity stamp at creation: the first logged system message of a fresh root
session must read "You are {instance_name}." on disk, not the soul name.

Covers four cases:
(a) Fresh root log faithful — create_main_agent_instance writes a stamped line 2.
(b) Legacy log untouched — restore path never rewrites an existing log's bytes.
(c) Sub-agent path — initialize_conversation stamps via build_system_message.
(d) Idempotency — double-stamp is a no-op; unstamped soul head gets exactly one stamp.
"""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.api_integration_pkg.runner import create_main_agent_instance
from agent_cascade.lifecycle_manager import (
    AgentLifecycleManager,
    _stamp_identity_to_instance_name,
)
from agent_cascade.llm.schema import SYSTEM, Message
from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger


def _make_instance(name: str, agent_class: str = 'orchestrator') -> AgentInstance:
    """Minimal AgentInstance for pool mocking."""
    return AgentInstance(
        instance_name=name,
        agent_class=agent_class,
        conversation=[],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=-1,
    )


def _make_pool_mock(instance: AgentInstance, log_dir: str, existing_history: bool = False) -> MagicMock:
    """Build a MagicMock pool sufficient for create_main_agent_instance."""
    pool = MagicMock()
    pool.create_instance.return_value = instance
    # Create a real logger in the temp dir so JSONL is written to disk.
    log_inst = AgentInstanceLogger(
        agent_class='orchestrator',
        instance_name=instance.instance_name,
        log_dir=log_dir,
        base_metadata={'supervisor': 'User'},
    )
    if existing_history:
        # Simulate a restored session: pre-populate history so the logging loop is skipped.
        log_inst.data['history'].append({'role': 'system', 'content': 'You are Orchestrator.\n'})
    pool.get_logger.return_value = log_inst
    return pool


class TestFreshRootLogFaithful:
    """(a) A fresh root session's JSONL line 2 must read 'You are {instance_name}.'"""

    def test_line2_reads_instance_name(self, tmp_path):
        log_dir = str(tmp_path / 'logs')
        os.makedirs(log_dir, exist_ok=True)
        instance = _make_instance('Maine')
        pool = _make_pool_mock(instance, log_dir)

        soul_text = 'You are Orchestrator.\n\n# Role\nYou coordinate agents.'
        create_main_agent_instance(pool, 'Maine', soul_text)

        # Find the JSONL file the logger wrote.
        log_files = list(Path(log_dir).glob('*.jsonl'))
        assert len(log_files) == 1, f"Expected 1 JSONL file, got {len(log_files)}"
        lines = log_files[0].read_text().splitlines()
        # Line 1 is metadata, line 2 is the system message.
        sys_line = json.loads(lines[1])
        assert sys_line['content'].startswith('You are Maine.\n'), (
            f"Line 2 content starts with: {sys_line['content'][:60]!r}"
        )
        # Must NOT contain the soul name as identity.
        assert 'You are Orchestrator.' not in sys_line['content']


class TestLegacyLogUntouched:
    """(b) Restoring an existing log must not rewrite its bytes (no retro-patch)."""

    def test_restore_path_preserves_bytes(self, tmp_path):
        log_dir = str(tmp_path / 'logs')
        os.makedirs(log_dir, exist_ok=True)

        # Write a legacy JSONL with the old soul-name identity.
        log_file = Path(log_dir) / 'orchestrator_Maine_20260101_000000.jsonl'
        legacy_content = (
            json.dumps({'metadata': {'instance_name': 'Maine', 'agent_class': 'orchestrator'}}) + '\n'
            + json.dumps({'role': 'system', 'content': 'You are Orchestrator.\n# Role\nOld soul.', 'timestamp': '1.0'}) + '\n'
        )
        log_file.write_text(legacy_content)
        hash_before = hashlib.sha256(legacy_content.encode()).hexdigest()

        # Simulate restore: conversation is pre-loaded from the log.
        loaded_sys_msg = Message(role=SYSTEM, content='You are Orchestrator.\n# Role\nOld soul.')
        instance = _make_instance('Maine')
        pool = _make_pool_mock(instance, log_dir, existing_history=True)
        # Point the logger at the existing file.
        pool.get_logger.return_value.log_path = str(log_file)

        create_main_agent_instance(pool, 'Maine', '', conversation=[loaded_sys_msg])

        # File must be byte-for-byte identical.
        hash_after = hashlib.sha256(log_file.read_text().encode()).hexdigest()
        assert hash_before == hash_after, 'Legacy log was modified by restore path'


class TestSubAgentPath:
    """(c) initialize_conversation stamps via build_system_message; no corruption."""

    def test_sub_agent_line2_reads_instance_name(self, tmp_path):
        log_dir = str(tmp_path / 'logs')
        os.makedirs(log_dir, exist_ok=True)

        pool = MagicMock()
        template = MagicMock()
        template.base_system_message = 'You are Researcher.\n\n# Role\nYou investigate.'
        pool.get_template.return_value = template
        pool.operation_manager = MagicMock()
        pool.operation_manager.base_dir = str(tmp_path)
        pool.operation_manager.extra_work_folders_ro = []
        pool.operation_manager.extra_work_folders_rw = []

        instance = AgentInstance(
            instance_name='researcher_x',
            agent_class='researcher',
            conversation=[],
            created_at=time.monotonic(),
            last_activity=time.monotonic(),
            latest_marker_index=-1,
        )
        log_inst = AgentInstanceLogger(
            agent_class='researcher',
            instance_name='researcher_x',
            log_dir=log_dir,
        )
        pool.get_logger.return_value = log_inst

        manager = AgentLifecycleManager(pool)
        sys_msg = manager.build_system_message('researcher', 'researcher_x')
        # build_system_message already stamped it.
        assert sys_msg.content.startswith('You are researcher_x.')

        # initialize_conversation should not corrupt the stamp.
        task_msg = Message(role='user', content='Do the thing')
        manager.initialize_conversation(
            instance=instance,
            sys_msg=sys_msg,
            task_msg=task_msg,
            is_reuse=False,
            instance_name='researcher_x',
            agent_class='researcher',
        )

        # Verify the logged line 2 still reads the instance name.
        log_files = list(Path(log_dir).glob('*.jsonl'))
        assert len(log_files) >= 1
        lines = log_files[0].read_text().splitlines()
        # Line 1 is metadata; line 2 is the system message.
        sys_line = json.loads(lines[1])
        assert sys_line['content'].startswith('You are researcher_x.')
        # No double-stamp or corruption.
        assert sys_line['content'].count('You are ') == 1


class TestIdempotency:
    """(d) The helper is idempotent; no double-stamp; unstamped soul gets one stamp."""

    def test_already_stamped_is_noop(self):
        msg = Message(role=SYSTEM, content='You are Maine.\n\n# Role\nYou coordinate.')
        original = msg.content
        _stamp_identity_to_instance_name(msg, 'Maine')
        assert msg.content == original
        _stamp_identity_to_instance_name(msg, 'Maine')
        assert msg.content == original
        assert msg.content.count('You are ') == 1

    def test_hyphenated_name_noop(self):
        msg = Message(role=SYSTEM, content='You are bug0367-research.\n\n# Role\nYou investigate.')
        original = msg.content
        _stamp_identity_to_instance_name(msg, 'bug0367-research')
        # Hyphenated names do not match r"You are\s+\w+\." so the content is unchanged.
        assert msg.content == original
        _stamp_identity_to_instance_name(msg, 'bug0367-research')
        assert msg.content == original

    def test_unstamped_soul_gets_one_stamp(self):
        msg = Message(role=SYSTEM, content='You are Orchestrator.\n\n# Role\nYou coordinate agents.')
        _stamp_identity_to_instance_name(msg, 'Maine')
        assert msg.content.startswith('You are Maine.')
        assert 'Orchestrator' not in msg.content
        assert msg.content.count('You are ') == 1

    def test_non_string_content_untouched(self):
        # Pydantic Message coerces content=None to "" (str field), so test the
        # empty-string guard path: the helper must leave it unchanged.
        msg = Message(role=SYSTEM, content='')
        _stamp_identity_to_instance_name(msg, 'Maine')
        assert msg.content == ''
