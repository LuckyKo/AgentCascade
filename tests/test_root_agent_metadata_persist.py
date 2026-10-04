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


# ── Restore path: load_session_from_log ────────────────────────────────────────
# The root is NOT re-created via create_main_agent_instance on restart; it is restored
# directly inside pool.load_session_from_log. That path must ALSO inject the block and
# sync it back into the DICT list `cleaned` (which rewrite_log_with_history persists),
# otherwise the missing block propagates to disk forever.


def _make_blockless_restore_log(tmp_path):
    """Build a JSONL log whose system message has NO Session Metadata block."""
    import json as _json

    lines = [
        {'role': 'system', 'content': 'You are Maine. Technical lead.'},
        {'role': 'user', 'content': 'Do the thing.'},
        {'role': 'assistant', 'content': 'Done.'},
    ]
    log_file = tmp_path / 'orchestrator_Maine_test.jsonl'
    with open(log_file, 'w', encoding='utf-8') as f:
        for m in lines:
            f.write(_json.dumps(m) + '\n')
    return str(log_file)


def _make_restore_pool():
    """A mock pool sufficient to drive the REAL load_session_from_log end-to-end.

    ``self._logger`` is a MagicMock (its ``log_dir``/``_lock``/``_loggers`` are used by step 8),
    and AgentInstanceLogger is patched so its rewrite_log_with_history records the DICT list it
    receives — that list is exactly what would be written to disk, i.e. the revert-proof target.
    """
    import threading
    from agent_cascade.pool.session_io import SessionIOMixin

    # Subclass the mixin so the real _parse_json_input / _extract_last_session helpers are
    # available; only load_session_from_log is what we drive end-to-end.
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
            # _execution._state_lock is acquired around the instance swap; a plain RLock suffices.
            self._execution = MagicMock()
            self._execution._state_lock = threading.RLock()

            self._logger = MagicMock()
            self._logger.log_dir = '/tmp/root_meta_restore'
            self._logger.workspace_dir = None
            # {key: logger} registry; load_session_from_log pops/closes then re-inserts.
            self._logger._loggers = {}

            om = MagicMock()
            om.base_dir = '/tmp/root_meta_ws'
            om.extra_work_folders_ro = []
            om.extra_work_folders_rw = []
            self.operation_manager = om

        def _resolve_instance_name(self, name, exclude=None):
            return name

        def _dismiss_all_instances(self, exclude=None):
            # No-op: the pool starts empty for this test.
            pass

    return MockPool()


def _run_restore(pool, log_file):
    """Bind the real SessionIOMixin.load_session_from_log onto the mock pool and run it."""
    from agent_cascade.pool.session_io import SessionIOMixin

    load = SessionIOMixin.load_session_from_log.__get__(pool, type(pool))
    return load(log_input=log_file, target_instance='Maine', clear_sub_agents_before_load=False)


class TestRootMetadataRestorePath:

    def test_restore_in_memory_and_persisted_gain_metadata(self, tmp_path):
        """Core revert-proof contract for the RESTORE path.

        A blockless system message is loaded via the REAL load_session_from_log. After it runs,
        BOTH (a) the in-memory conversation[0] AND (b) the dict list handed to
        rewrite_log_with_history (i.e. what hits disk) must contain the block. Without the fix,
        neither does — the blockless system message is faithfully propagated to the new log.
        """
        from unittest.mock import patch

        pool = _make_restore_pool()
        captured = {}

        class _CapturingAIL:
            def __init__(self, *args, **kwargs):
                self.log_path = '/tmp/root_meta_restore/restored.jsonl'
                self.data = {'history': [], 'metadata': {}}

            @staticmethod
            def copy_session_file(source_path, log_dir, agent_class, instance_name):
                return '/tmp/root_meta_restore/restored.jsonl'

            def rewrite_log_with_history(self, new_history, allow_shrink=False, caller='unknown'):
                captured['history'] = list(new_history)
                return True

        # AgentInstanceLogger is imported locally inside load_session_from_log, so patch it at its
        # home module (the import resolves to the mock there). This does NOT rebind any package
        # module in sys.modules — it only swaps one attribute on an already-imported module.
        with patch('agent_cascade.logger.agent_instance_logger.AgentInstanceLogger', _CapturingAIL):
            result = _run_restore(pool, _make_blockless_restore_log(tmp_path))

        assert 'Loaded' in result, f"unexpected load result: {result!r}"

        # (a) In-memory conversation[0] gained the block.
        inst = pool.instances['Maine']
        assert isinstance(inst.conversation[0], Message)
        assert '## Session Metadata' in inst.conversation[0].content, (
            "restored root's in-memory system message is missing the Session Metadata block")

        # (b) The re-persisted log (the dict list rewrite_log_with_history received) has it too.
        rewritten = captured.get('history')
        assert rewritten is not None, 'load_session_from_log did not call rewrite_log_with_history'
        sys_dicts = [d for d in rewritten if isinstance(d, dict) and d.get('role') == 'system']
        assert len(sys_dicts) >= 1, 'rewritten history must contain the system message'
        assert '## Session Metadata' in sys_dicts[0].get('content', ''), (
            "restored root's RE-PERSISTED system message is missing the Session Metadata block — "
            'the injected content was not synced back into the dict list before rewrite')

    def test_restore_is_idempotent_when_block_present(self, tmp_path):
        """If the loaded log already carries the block, it is not duplicated."""
        from unittest.mock import patch
        import json as _json

        pool = _make_restore_pool()

        class _CapturingAIL:
            def __init__(self, *args, **kwargs):
                self.log_path = '/tmp/root_meta_restore/restored.jsonl'
                self.data = {'history': [], 'metadata': {}}

            @staticmethod
            def copy_session_file(source_path, log_dir, agent_class, instance_name):
                return '/tmp/root_meta_restore/restored.jsonl'

            def rewrite_log_with_history(self, new_history, allow_shrink=False, caller='unknown'):
                return True

        lines = [
            {'role': 'system',
             'content': 'You are Maine.\n## Session Metadata\n- Supervisor: User\n- System: x'},
            {'role': 'user', 'content': 'hi'},
        ]
        log_file = tmp_path / 'orchestrator_Maine_idem.jsonl'
        with open(log_file, 'w', encoding='utf-8') as f:
            for m in lines:
                f.write(_json.dumps(m) + '\n')

        with patch('agent_cascade.logger.agent_instance_logger.AgentInstanceLogger', _CapturingAIL):
            _run_restore(pool, str(log_file))

        inst = pool.instances['Maine']
        assert inst.conversation[0].content.count('## Session Metadata') == 1
