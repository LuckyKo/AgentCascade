"""Regression tests for todo.md:155 — root/orchestrator Session Metadata persistence.

Bug: the root (Maine) agent's ``## Session Metadata`` block was never persisted to its
JSONL log in any session (fresh or restored), while every sub-agent class had it 100%.

Root cause: the persisting writer (``lifecycle_manager._inject_metadata_into_message``)
is only reached on the sub-agent path. The root is created by
``api_integration_pkg.runner.create_main_agent_instance``, which built and logged its
system message without ever injecting the block. On restart, ``load_session_from_log``
faithfully re-persists the blockless system message, so the loss propagates forever.

Fix: inject the metadata into ``conversation[0]`` inside ``create_main_agent_instance``
BEFORE the initial messages are logged, covering both the fresh and restore branches.

These tests drive the REAL injection path (real AgentInstance + real
``_inject_metadata_into_message`` / ``_build_session_metadata``) with a mock pool whose
logger captures what would be written to disk. They FAIL without the fix.

Test-isolation rule: no ``sys.modules`` rebinding of any package module — plain package
imports + mock pools only (see .agent_lessons/test-sys-modules-pollution-breaks-late-import-patch.md).
"""
import os
import sys
from unittest.mock import MagicMock

# Ensure the project root is importable regardless of how pytest is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_cascade.agent_instance import AgentInstance  # noqa: E402
from agent_cascade.llm.schema import Message, SYSTEM  # noqa: E402


# ── Mock pool that captures logged messages ────────────────────────────────────


class _CapturingLogger:
    """Minimal logger stand-in mirroring the real AgentInstanceLogger surface used by
    create_main_agent_instance: ``data['history']``, ``load_history_from_file()``,
    ``log_message(msg)`` and ``log_path``."""

    def __init__(self):
        self.data = {'history': [], 'metadata': {}}
        self.log_path = '/tmp/root_meta_test.jsonl'
        # Messages captured in the order they would be written to disk.
        self.logged_messages = []

    def load_history_from_file(self):
        # No-op: models a fresh session with no existing history on disk, so the
        # initial-message logging branch runs (log_inst.data['history'] stays empty).
        pass

    def log_message(self, message):
        self.logged_messages.append(message)


def _make_pool():
    """A mock pool sufficient to drive create_main_agent_instance + real metadata build.

    ``operation_manager`` carries REAL string values (not MagicMock reprs) so the
    generated metadata is deterministic and free of memory-address noise.
    """
    pool = MagicMock()
    pool.instances = {}
    pool.instance_state = {}

    om = MagicMock()
    om.base_dir = '/tmp/root_meta_ws'
    om.extra_work_folders_ro = []
    om.extra_work_folders_rw = []
    pool.operation_manager = om

    def _resolve_instance_name(name, exclude=None):
        return name

    pool._resolve_instance_name.side_effect = _resolve_instance_name

    # Real AgentInstance, mirroring test_phase5_polish.py's MockAgentPool.create_instance.
    def create_instance(instance_name, agent_class, parent_instance, max_turns, conversation):
        inst = AgentInstance(
            instance_name=instance_name,
            agent_class=agent_class,
            conversation=list(conversation),
            max_turns=max_turns,
            parent_instance=parent_instance,
            created_at=0.0,
            last_activity=0.0,
            compression_summary=None,
            latest_marker_index=-1,
        )
        pool.instances[instance_name] = inst
        return inst

    pool.create_instance.side_effect = create_instance

    # One capturing logger; get_logger returns it regardless of base_metadata.
    cap = _CapturingLogger()
    pool.get_logger.return_value = cap
    pool._captured_logger = cap

    # settings.tail_sync_check_enabled drives the (non-critical) tail-sync block; disable to keep
    # the test focused on metadata persistence.
    pool.settings.tail_sync_check_enabled = False

    return pool


def _system_messages_logged(pool):
    """Return the logged messages whose role is 'system' (as dicts or Message objects)."""
    out = []
    for m in pool._captured_logger.logged_messages:
        role = getattr(m, 'role', None) if not isinstance(m, dict) else m.get('role')
        if role == SYSTEM or role == 'system':
            out.append(m)
    return out


def _content_of(msg):
    if isinstance(msg, dict):
        return msg.get('content', '')
    return getattr(msg, 'content', '')


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestRootMetadataPersistence:

    def test_fresh_root_system_message_persisted_with_metadata(self):
        """Core contract: on a fresh pool the LOGGED system message contains the block.

        This mirrors what sub-agents already satisfy. Fails without the fix because the
        root path previously logged the raw, blockless system message.
        """
        from agent_cascade.api_integration_pkg.runner import create_main_agent_instance

        pool = _make_pool()
        create_main_agent_instance(
            pool=pool,
            instance_name='Maine',
            system_message_content='You are Maine. Technical lead.',
        )

        sys_msgs = _system_messages_logged(pool)
        assert len(sys_msgs) >= 1, f"expected a logged system message, got {len(sys_msgs)}"
        content = _content_of(sys_msgs[0])
        assert '## Session Metadata' in content, (
            "root's persisted system message is missing the Session Metadata block")
        # Root agent's supervisor is the User (parent_instance is None).
        assert '- Supervisor: User' in content, (
            "root's persisted metadata must list the User as supervisor")

    def test_fresh_root_in_memory_conversation_has_metadata(self):
        """The in-memory conversation[0] also carries the block (not just the log)."""
        from agent_cascade.api_integration_pkg.runner import create_main_agent_instance

        pool = _make_pool()
        inst = create_main_agent_instance(
            pool=pool,
            instance_name='Maine',
            system_message_content='You are Maine. Technical lead.',
        )

        assert isinstance(inst.conversation[0], Message)
        assert '## Session Metadata' in inst.conversation[0].content
        assert '- Supervisor: User' in inst.conversation[0].content

    def test_restore_round_trip_gains_metadata(self):
        """Revert-proof for the restore branch.

        A restored session passes a pre-existing blockless system message as conversation[0].
        The fix must inject the block before it is (re-)persisted, so a reload no longer
        propagates the missing block forward.
        """
        from agent_cascade.api_integration_pkg.runner import create_main_agent_instance

        pool = _make_pool()
        # Simulate what load_session_from_log hands back: a system message with NO metadata.
        restored_sys = Message(role=SYSTEM, content='You are Maine. Technical lead.')
        assert '## Session Metadata' not in restored_sys.content

        inst = create_main_agent_instance(
            pool=pool,
            instance_name='Maine',
            system_message_content='You are Maine. Technical lead.',  # unused on restore path
            conversation=[restored_sys],
        )

        # In-memory: the blockless message gained the block.
        assert '## Session Metadata' in inst.conversation[0].content
        assert '- Supervisor: User' in inst.conversation[0].content

        # Persisted: the logged system message carries the block (the revert-proof assertion).
        sys_msgs = _system_messages_logged(pool)
        assert len(sys_msgs) >= 1, 'restore path must still log the initial system message'
        content = _content_of(sys_msgs[0])
        assert '## Session Metadata' in content, (
            "restored root's persisted system message is missing the Session Metadata block")

    def test_injection_is_idempotent(self):
        """If the system message already has the block, it is not duplicated."""
        from agent_cascade.api_integration_pkg.runner import create_main_agent_instance

        pool = _make_pool()
        already = Message(role=SYSTEM, content=(
            'You are Maine.\n## Session Metadata\n- Supervisor: User\n- System: x'))

        inst = create_main_agent_instance(
            pool=pool,
            instance_name='Maine',
            system_message_content='unused',
            conversation=[already],
        )

        assert inst.conversation[0].content.count('## Session Metadata') == 1
