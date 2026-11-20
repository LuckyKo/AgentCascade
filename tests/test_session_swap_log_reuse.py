"""Regression tests for BUG_0365 — session swap reuses the original log file.

A same-identity swap (load_session_from_log with the same instance name) must
reuse the original JSONL file in-place: no new timestamped copy is minted, the
logger's log_path stays the same, and the file's message content is preserved.

A clone/legacy import (load under a DIFFERENT instance name) must still copy
the file, leaving the original untouched.

The byte-equal short-circuit in rewrite_log_with_history skips the atomic write
when the on-disk file already matches the serialized content, preserving mtime
and the KV-cache stable prefix.

Test-isolation rule: no sys.modules rebinding — plain package imports + mock
pools only (see .agent_lessons/test-sys-modules-pollution-breaks-late-import-patch.md).
"""
import hashlib
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_cascade.pool.session_io import SessionIOMixin


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_log_file(tmp_path, instance_name='Maine', agent_class='orchestrator'):
    """Create a JSONL log file with a metadata header + system + user messages."""
    log_name = f'{agent_class}_{instance_name}_test.jsonl'
    log_file = tmp_path / log_name
    lines = [
        {'metadata': {
            'agent_class': agent_class,
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
    """A mock pool sufficient to drive the REAL load_session_from_log end-to-end.

    The real AgentInstanceLogger is used (not a fake) so the test can inspect
    the actual file system behavior.
    """
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


def _sha256(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def _jsonl_files_in(dir_path, prefix='orchestrator_Maine'):
    """Return the set of .jsonl filenames in dir_path matching the prefix."""
    return {
        f for f in os.listdir(dir_path)
        if f.startswith(prefix) and f.endswith('.jsonl')
    }


# ── Tests ──────────────────────────────────────────────────────────────────


class TestSameIdentitySwapReusesLog:
    """A same-identity swap must reuse the original file path."""

    def test_log_path_unchanged(self, tmp_path):
        """The logger's log_path must equal the original file after a same-name swap."""
        log_file = _make_log_file(tmp_path, instance_name='Maine')
        pool = _make_pool(tmp_path)

        status = pool.load_session_from_log(
            log_file, target_instance='Maine', clear_sub_agents_before_load=False)
        assert not str(status).startswith('Error'), f"session load failed: {status}"

        # The logger installed in the pool registry must point at the original file.
        loggers = pool._logger._loggers
        assert loggers, 'no logger was installed'
        log_inst = list(loggers.values())[0]
        assert log_inst.log_path == log_file, (
            f"logger log_path changed from {log_file} to {log_inst.log_path}")

    def test_no_new_timestamped_file(self, tmp_path):
        """No new timestamped .jsonl file must appear after a same-name swap."""
        log_file = _make_log_file(tmp_path, instance_name='Maine')
        before = _jsonl_files_in(str(tmp_path))
        pool = _make_pool(tmp_path)

        status = pool.load_session_from_log(
            log_file, target_instance='Maine', clear_sub_agents_before_load=False)
        assert not str(status).startswith('Error'), f"session load failed: {status}"

        after = _jsonl_files_in(str(tmp_path))
        new_files = after - before
        assert not new_files, (
            f"same-identity swap minted a new log file: {new_files}; "
            f"expected in-place reuse of {os.path.basename(log_file)}")

    def test_message_content_preserved(self, tmp_path):
        """Non-system message role+content must be unchanged by the swap.

        The system message gains the Session Metadata block on restore (expected),
        and _format_message adds timestamp fields, so only role+content are compared.
        """
        log_file = _make_log_file(tmp_path, instance_name='Maine')

        def _extract_non_system(path):
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            return [
                (json.loads(l).get('role'), json.loads(l).get('content'))
                for l in lines[1:]
                if l.strip() and json.loads(l).get('role') in ('user', 'assistant')
            ]

        original_msgs = _extract_non_system(log_file)

        pool = _make_pool(tmp_path)
        status = pool.load_session_from_log(
            log_file, target_instance='Maine', clear_sub_agents_before_load=False)
        assert not str(status).startswith('Error'), f"session load failed: {status}"

        new_msgs = _extract_non_system(log_file)
        assert new_msgs == original_msgs, (
            'non-system message role+content changed after a same-identity swap')


class TestCloneImportCopiesLog:
    """Loading a log under a DIFFERENT instance name must copy the file."""

    def test_copy_created_and_original_untouched(self, tmp_path):
        """A clone import creates a copy; the original file is untouched."""
        log_file = _make_log_file(tmp_path, instance_name='Maine')
        original_bytes = open(log_file, 'rb').read()
        pool = _make_pool(tmp_path)

        status = pool.load_session_from_log(
            log_file, target_instance='Clone', clear_sub_agents_before_load=False)
        assert not str(status).startswith('Error'), f"session load failed: {status}"

        # The original file must be byte-identical.
        assert open(log_file, 'rb').read() == original_bytes, (
            'clone import modified the original log file')

        # A new file was created in the temp dir.
        all_files = _jsonl_files_in(str(tmp_path), prefix='')
        new_files = {f for f in all_files if f != os.path.basename(log_file)}
        assert new_files, (
            f"clone import did not create a copy; files in dir: {sorted(all_files)}")


class TestNoOpRewritePreservesMtime:
    """The byte-equal short-circuit in rewrite_log_with_history skips the
    atomic write when the on-disk file already matches, preserving mtime."""

    def test_identical_rewrite_skips_write(self, tmp_path, monkeypatch):
        """The byte-equal short-circuit skips the atomic write when the on-disk
        file already matches the serialized content."""
        from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger
        from agent_cascade.llm.schema import Message, SYSTEM, USER

        log_dir = str(tmp_path)
        log_path = str(tmp_path / 'test_noop.jsonl')
        log_inst = AgentInstanceLogger(
            agent_class='orchestrator',
            instance_name='TestInst',
            log_dir=log_dir,
            log_path=log_path,
        )

        msgs = [
            Message(role=SYSTEM, content='You are TestInst.'),
            Message(role=USER, content='Hello'),
        ]
        # First write: creates the file.
        assert log_inst.rewrite_log_with_history(msgs, caller='test_first')

        # Use the formatted dicts from data['history'] (which have stable
        # timestamps from the first write) for the second write, ensuring
        # byte-identical content.
        formatted = list(log_inst.data['history'])

        # Track whether _atomic_write_lines is called on the second write.
        write_calls = []
        original_atomic_write = log_inst._atomic_write_lines

        def tracking_atomic_write(lines):
            write_calls.append(len(lines))
            return original_atomic_write(lines)

        monkeypatch.setattr(log_inst, '_atomic_write_lines', tracking_atomic_write)

        # Second write with the same formatted content: the short-circuit should skip.
        assert log_inst.rewrite_log_with_history(formatted, caller='test_second')

        assert not write_calls, (
            '_atomic_write_lines was called on the second identical rewrite — '
            'the byte-equal short-circuit did not fire')

    def test_different_content_still_writes(self, tmp_path, monkeypatch):
        """When the content actually changes, the write proceeds (short-circuit does not fire)."""
        from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger
        from agent_cascade.llm.schema import Message, SYSTEM, USER

        log_dir = str(tmp_path)
        log_path = str(tmp_path / 'test_noop2.jsonl')
        log_inst = AgentInstanceLogger(
            agent_class='orchestrator',
            instance_name='TestInst',
            log_dir=log_dir,
            log_path=log_path,
        )

        msgs_v1 = [
            Message(role=SYSTEM, content='You are TestInst.'),
            Message(role=USER, content='v1'),
        ]
        assert log_inst.rewrite_log_with_history(msgs_v1, caller='test_v1')

        # Track whether _atomic_write_lines is called on the changed-content rewrite.
        write_calls = []
        original_atomic_write = log_inst._atomic_write_lines

        def tracking_atomic_write(lines):
            write_calls.append(len(lines))
            return original_atomic_write(lines)

        monkeypatch.setattr(log_inst, '_atomic_write_lines', tracking_atomic_write)

        msgs_v2 = [
            Message(role=SYSTEM, content='You are TestInst.'),
            Message(role=USER, content='v2-changed'),
        ]
        assert log_inst.rewrite_log_with_history(msgs_v2, caller='test_v2')

        assert write_calls, (
            '_atomic_write_lines was not called when content changed — '
            'the byte-equal short-circuit fired incorrectly')
