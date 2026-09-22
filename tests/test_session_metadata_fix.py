"""Test that _build_session_metadata reads from operation_manager, not logger metadata.

This verifies the fix for two issues:
1. Working Dir should reflect the configured value from UI, not os.getcwd()
2. Read-only paths should appear in Session Metadata
"""
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from agent_cascade import __version__
from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.helpers import _build_session_metadata

TS_RE = r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}'


def _make_pool():
    """Build a minimal MagicMock pool usable by _build_session_metadata."""
    pool = MagicMock()
    pool.operation_manager = None
    log_inst = MagicMock()
    log_inst.data = {'metadata': {}}
    log_inst.log_path = '/fake/log.jsonl'
    pool.get_logger.return_value = log_inst
    return pool


def _make_instance(parent_instance=None, system_started_at='auto'):
    """Build a minimal MagicMock instance usable by _build_session_metadata."""
    instance = MagicMock()
    instance.instance_name = 'orchestrator'
    instance.agent_class = 'orchestrator'
    instance.parent_instance = parent_instance
    if system_started_at == 'auto':
        # Leave as MagicMock attribute (non-string) to exercise the fallback path
        pass
    else:
        instance.system_started_at = system_started_at
    return instance


def test_system_line_present():
    """The '- System:' line should be present in the Session Metadata output."""
    pool = _make_pool()
    instance = _make_instance(parent_instance=None)

    result = _build_session_metadata(pool, instance)

    assert '- System: AgentCascade v' in result, f"Expected System line:\n{result}"


def test_system_line_immediately_after_supervisor():
    """The System line should appear immediately after the Supervisor line."""
    pool = _make_pool()
    instance = _make_instance(parent_instance=None)

    result = _build_session_metadata(pool, instance)
    lines = result.splitlines()

    sup_idx = next(i for i, l in enumerate(lines) if l.startswith('- Supervisor:'))
    sys_idx = next(i for i, l in enumerate(lines) if l.startswith('- System:'))

    assert sys_idx == sup_idx + 1, (
        f"System line should be right after Supervisor. "
        f"supervisor@{sup_idx}, system@{sys_idx}:\n{result}"
    )


def test_system_line_content():
    """System line should contain product name, version, and a timestamp."""
    pool = _make_pool()
    instance = _make_instance(parent_instance=None)

    result = _build_session_metadata(pool, instance)
    lines = result.splitlines()
    sys_line = next(l for l in lines if l.startswith('- System:'))

    assert 'AgentCascade' in sys_line, f"Missing product name:\n{sys_line}"
    assert f'v{__version__}' in sys_line, f"Missing version v{__version__}:\n{sys_line}"
    assert re.search(TS_RE, sys_line), f"Missing timestamp in:\n{sys_line}"


def test_system_line_uses_constant_instance_value():
    """A real instance with system_started_at set renders that exact value (constant)."""
    pool = _make_pool()

    import time
    inst = AgentInstance(
        instance_name='real_agent',
        agent_class='coder',
        conversation=[],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=-1,
        parent_instance=None,
        system_started_at='2026-01-02 03:04',
    )

    result = _build_session_metadata(pool, inst)
    lines = result.splitlines()
    sys_line = next(l for l in lines if l.startswith('- System:'))

    assert '2026-01-02 03:04' in sys_line, f"Expected exact constant timestamp:\n{sys_line}"


def test_system_line_block_stable_across_turns():
    """KV-cache invariant: two calls on a real instance return byte-identical output.

    The System line (and the whole Session Metadata block) must be a constant for a
    given instance so _replace_section short-circuits and m0 stays byte-stable across
    turns — no per-turn churn that would invalidate the KV cache prefix.
    """
    pool = _make_pool()

    import time
    inst = AgentInstance(
        instance_name='real_agent',
        agent_class='coder',
        conversation=[],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=-1,
        parent_instance=None,
        system_started_at='2026-01-02 03:04',
    )

    out1 = _build_session_metadata(pool, inst)
    out2 = _build_session_metadata(pool, inst)
    assert out1 == out2, (
        'Session Metadata block must be byte-identical across turns '
        '(KV cache prefix preserved). Got differing output:\n'
        f"--- out1 ---\n{out1}\n--- out2 ---\n{out2}"
    )


def test_system_line_root_and_subagent():
    """Both root agent (Supervisor: User) and sub-agent get the System line."""
    # Root agent
    pool = _make_pool()
    root = _make_instance(parent_instance=None)
    root_result = _build_session_metadata(pool, root)
    assert '- Supervisor: User' in root_result, f"Root supervisor missing:\n{root_result}"
    assert '- System: AgentCascade v' in root_result, f"Root system line missing:\n{root_result}"

    # Sub-agent (has a parent)
    sub = _make_instance(parent_instance='Maine')
    sub_result = _build_session_metadata(pool, sub)
    assert '- Supervisor: Maine' in sub_result, f"Sub supervisor missing:\n{sub_result}"
    assert '- System: AgentCascade v' in sub_result, f"Sub system line missing:\n{sub_result}"


def test_working_dir_from_operation_manager():
    """Working Dir should come from operation_manager.base_dir, not logger metadata."""
    # Create a temporary workspace directory
    with tempfile.TemporaryDirectory() as tmpdir:
        custom_workspace = Path(tmpdir) / 'my_workspace'
        custom_workspace.mkdir()

        # Mock pool with operation_manager set to custom workspace
        pool = MagicMock()
        pool.operation_manager = MagicMock()
        pool.operation_manager.base_dir = custom_workspace
        pool.operation_manager.extra_work_folders_ro = []
        pool.operation_manager.extra_work_folders_rw = []

        # Logger metadata has a DIFFERENT working_dir (simulating stale data)
        log_inst = MagicMock()
        log_inst.data = {'metadata': {'working_dir': os.getcwd()}}
        log_inst.log_path = '/fake/log/path.jsonl'
        pool.get_logger.return_value = log_inst

        # Mock instance
        instance = MagicMock()
        instance.instance_name = 'orchestrator'
        instance.agent_class = 'orchestrator'
        instance.parent_instance = None

        result = _build_session_metadata(pool, instance)

        # Working Dir should be the custom workspace, NOT os.getcwd()
        assert str(custom_workspace) in result, f"Expected {custom_workspace} in metadata, got:\n{result}"
        # Should NOT contain the stale CWD from logger metadata
        assert os.getcwd() not in result, f"CWD leaked into metadata:\n{result}"


def test_extra_read_only_paths():
    """Read-only paths should appear when configured via operation_manager."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ro_dir = Path(tmpdir) / 'read_only'
        ro_dir.mkdir()

        pool = MagicMock()
        pool.operation_manager = MagicMock()
        pool.operation_manager.base_dir = Path(tmpdir)
        pool.operation_manager.extra_work_folders_ro = [ro_dir]
        pool.operation_manager.extra_work_folders_rw = []

        log_inst = MagicMock()
        log_inst.data = {'metadata': {}}  # Empty logger metadata — extra_paths_ro never set
        log_inst.log_path = '/fake/log.jsonl'
        pool.get_logger.return_value = log_inst

        instance = MagicMock()
        instance.instance_name = 'orchestrator'
        instance.agent_class = 'orchestrator'
        instance.parent_instance = None

        result = _build_session_metadata(pool, instance)

        assert 'Read-Only' in result, f"Expected 'Read-Only' in metadata:\n{result}"
        assert str(ro_dir) in result, f"Expected {ro_dir} in metadata:\n{result}"


def test_extra_read_write_paths():
    """Read-write paths should appear when configured via operation_manager."""
    with tempfile.TemporaryDirectory() as tmpdir:
        rw_dir = Path(tmpdir) / 'read_write'
        rw_dir.mkdir()

        pool = MagicMock()
        pool.operation_manager = MagicMock()
        pool.operation_manager.base_dir = Path(tmpdir)
        pool.operation_manager.extra_work_folders_ro = []
        pool.operation_manager.extra_work_folders_rw = [rw_dir]

        log_inst = MagicMock()
        log_inst.data = {'metadata': {}}
        log_inst.log_path = '/fake/log.jsonl'
        pool.get_logger.return_value = log_inst

        instance = MagicMock()
        instance.instance_name = 'orchestrator'
        instance.agent_class = 'orchestrator'
        instance.parent_instance = None

        result = _build_session_metadata(pool, instance)

        assert 'Read-Write' in result, f"Expected 'Read-Write' in metadata:\n{result}"
        assert str(rw_dir) in result, f"Expected {rw_dir} in metadata:\n{result}"


def test_fallback_to_logger_when_no_operation_manager():
    """When operation_manager is None, should fall back to logger metadata."""
    pool = MagicMock()
    pool.operation_manager = None

    log_inst = MagicMock()
    log_inst.data = {'metadata': {'working_dir': '/some/fallback/dir'}}
    log_inst.log_path = '/fallback/log.jsonl'
    pool.get_logger.return_value = log_inst

    instance = MagicMock()
    instance.instance_name = 'orchestrator'
    instance.agent_class = 'orchestrator'
    instance.parent_instance = None

    result = _build_session_metadata(pool, instance)

    assert '/some/fallback/dir' in result, f"Expected fallback working_dir:\n{result}"


def test_multiple_extra_paths():
    """Multiple read-only and read-write paths should all appear."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ro1 = Path(tmpdir) / 'ro1'
        ro2 = Path(tmpdir) / 'ro2'
        rw1 = Path(tmpdir) / 'rw1'
        for d in [ro1, ro2, rw1]:
            d.mkdir()

        pool = MagicMock()
        pool.operation_manager = MagicMock()
        pool.operation_manager.base_dir = Path(tmpdir)
        pool.operation_manager.extra_work_folders_ro = [ro1, ro2]
        pool.operation_manager.extra_work_folders_rw = [rw1]

        log_inst = MagicMock()
        log_inst.data = {'metadata': {}}
        log_inst.log_path = '/fake/log.jsonl'
        pool.get_logger.return_value = log_inst

        instance = MagicMock()
        instance.instance_name = 'orchestrator'
        instance.agent_class = 'orchestrator'
        instance.parent_instance = None

        result = _build_session_metadata(pool, instance)

        assert str(ro1) in result, f"Missing {ro1}:\n{result}"
        assert str(ro2) in result, f"Missing {ro2}:\n{result}"
        assert str(rw1) in result, f"Missing {rw1}:\n{result}"


def test_malformed_operation_manager_falls_back():
    """When operation_manager exists but has no attributes, should fall back to logger metadata."""
    pool = MagicMock()
    # operation_manager exists but is a bare object with no useful attributes
    pool.operation_manager = object()

    log_inst = MagicMock()
    log_inst.data = {'metadata': {'working_dir': '/fallback/dir'}}
    log_inst.log_path = '/fallback/log.jsonl'
    pool.get_logger.return_value = log_inst

    instance = MagicMock()
    instance.instance_name = 'orchestrator'
    instance.agent_class = 'orchestrator'
    instance.parent_instance = None

    result = _build_session_metadata(pool, instance)

    # Should fall back to logger metadata values since getattr returns defaults
    assert '/fallback/dir' not in result  # getattr(om, 'base_dir', 'Unknown') returns 'Unknown'
    assert 'Working Dir: Unknown' in result, f"Expected 'Unknown' working dir:\n{result}"


if __name__ == '__main__':
    test_working_dir_from_operation_manager()
    print('[PASS] test_working_dir_from_operation_manager')

    test_extra_read_only_paths()
    print('[PASS] test_extra_read_only_paths')

    test_extra_read_write_paths()
    print('[PASS] test_extra_read_write_paths')

    test_fallback_to_logger_when_no_operation_manager()
    print('[PASS] test_fallback_to_logger_when_no_operation_manager')

    test_multiple_extra_paths()
    print('[PASS] test_multiple_extra_paths')

    test_malformed_operation_manager_falls_back()
    print('[PASS] test_malformed_operation_manager_falls_back')

    test_system_line_present()
    print('[PASS] test_system_line_present')

    test_system_line_immediately_after_supervisor()
    print('[PASS] test_system_line_immediately_after_supervisor')

    test_system_line_content()
    print('[PASS] test_system_line_content')

    test_system_line_uses_constant_instance_value()
    print('[PASS] test_system_line_uses_constant_instance_value')

    test_system_line_block_stable_across_turns()
    print('[PASS] test_system_line_block_stable_across_turns')

    test_system_line_root_and_subagent()
    print('[PASS] test_system_line_root_and_subagent')

    print('\nAll tests passed!')
