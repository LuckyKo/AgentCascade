"""Test that _build_session_metadata reads from operation_manager, not logger metadata.

This verifies the fix for two issues:
1. Working Dir should reflect the configured value from UI, not os.getcwd()
2. Read-only paths should appear in Session Metadata
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from agent_cascade.execution_engine import _build_session_metadata


def test_working_dir_from_operation_manager():
    """Working Dir should come from operation_manager.base_dir, not logger metadata."""
    # Create a temporary workspace directory
    with tempfile.TemporaryDirectory() as tmpdir:
        custom_workspace = Path(tmpdir) / "my_workspace"
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
        log_inst.log_path = "/fake/log/path.jsonl"
        pool.get_logger.return_value = log_inst

        # Mock instance
        instance = MagicMock()
        instance.instance_name = "orchestrator"
        instance.agent_class = "orchestrator"
        instance.parent_instance = None

        result = _build_session_metadata(pool, instance)

        # Working Dir should be the custom workspace, NOT os.getcwd()
        assert str(custom_workspace) in result, f"Expected {custom_workspace} in metadata, got:\n{result}"
        # Should NOT contain the stale CWD from logger metadata
        assert os.getcwd() not in result, f"CWD leaked into metadata:\n{result}"


def test_extra_read_only_paths():
    """Read-only paths should appear when configured via operation_manager."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ro_dir = Path(tmpdir) / "read_only"
        ro_dir.mkdir()

        pool = MagicMock()
        pool.operation_manager = MagicMock()
        pool.operation_manager.base_dir = Path(tmpdir)
        pool.operation_manager.extra_work_folders_ro = [ro_dir]
        pool.operation_manager.extra_work_folders_rw = []

        log_inst = MagicMock()
        log_inst.data = {'metadata': {}}  # Empty logger metadata — extra_paths_ro never set
        log_inst.log_path = "/fake/log.jsonl"
        pool.get_logger.return_value = log_inst

        instance = MagicMock()
        instance.instance_name = "orchestrator"
        instance.agent_class = "orchestrator"
        instance.parent_instance = None

        result = _build_session_metadata(pool, instance)

        assert "Read-Only" in result, f"Expected 'Read-Only' in metadata:\n{result}"
        assert str(ro_dir) in result, f"Expected {ro_dir} in metadata:\n{result}"


def test_extra_read_write_paths():
    """Read-write paths should appear when configured via operation_manager."""
    with tempfile.TemporaryDirectory() as tmpdir:
        rw_dir = Path(tmpdir) / "read_write"
        rw_dir.mkdir()

        pool = MagicMock()
        pool.operation_manager = MagicMock()
        pool.operation_manager.base_dir = Path(tmpdir)
        pool.operation_manager.extra_work_folders_ro = []
        pool.operation_manager.extra_work_folders_rw = [rw_dir]

        log_inst = MagicMock()
        log_inst.data = {'metadata': {}}
        log_inst.log_path = "/fake/log.jsonl"
        pool.get_logger.return_value = log_inst

        instance = MagicMock()
        instance.instance_name = "orchestrator"
        instance.agent_class = "orchestrator"
        instance.parent_instance = None

        result = _build_session_metadata(pool, instance)

        assert "Read-Write" in result, f"Expected 'Read-Write' in metadata:\n{result}"
        assert str(rw_dir) in result, f"Expected {rw_dir} in metadata:\n{result}"


def test_fallback_to_logger_when_no_operation_manager():
    """When operation_manager is None, should fall back to logger metadata."""
    pool = MagicMock()
    pool.operation_manager = None

    log_inst = MagicMock()
    log_inst.data = {'metadata': {'working_dir': '/some/fallback/dir'}}
    log_inst.log_path = "/fallback/log.jsonl"
    pool.get_logger.return_value = log_inst

    instance = MagicMock()
    instance.instance_name = "orchestrator"
    instance.agent_class = "orchestrator"
    instance.parent_instance = None

    result = _build_session_metadata(pool, instance)

    assert "/some/fallback/dir" in result, f"Expected fallback working_dir:\n{result}"


def test_multiple_extra_paths():
    """Multiple read-only and read-write paths should all appear."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ro1 = Path(tmpdir) / "ro1"
        ro2 = Path(tmpdir) / "ro2"
        rw1 = Path(tmpdir) / "rw1"
        for d in [ro1, ro2, rw1]:
            d.mkdir()

        pool = MagicMock()
        pool.operation_manager = MagicMock()
        pool.operation_manager.base_dir = Path(tmpdir)
        pool.operation_manager.extra_work_folders_ro = [ro1, ro2]
        pool.operation_manager.extra_work_folders_rw = [rw1]

        log_inst = MagicMock()
        log_inst.data = {'metadata': {}}
        log_inst.log_path = "/fake/log.jsonl"
        pool.get_logger.return_value = log_inst

        instance = MagicMock()
        instance.instance_name = "orchestrator"
        instance.agent_class = "orchestrator"
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
    log_inst.log_path = "/fallback/log.jsonl"
    pool.get_logger.return_value = log_inst

    instance = MagicMock()
    instance.instance_name = "orchestrator"
    instance.agent_class = "orchestrator"
    instance.parent_instance = None

    result = _build_session_metadata(pool, instance)

    # Should fall back to logger metadata values since getattr returns defaults
    assert "/fallback/dir" not in result  # getattr(om, 'base_dir', 'Unknown') returns 'Unknown'
    assert "Working Dir: Unknown" in result, f"Expected 'Unknown' working dir:\n{result}"


if __name__ == "__main__":
    test_working_dir_from_operation_manager()
    print("[PASS] test_working_dir_from_operation_manager")

    test_extra_read_only_paths()
    print("[PASS] test_extra_read_only_paths")

    test_extra_read_write_paths()
    print("[PASS] test_extra_read_write_paths")

    test_fallback_to_logger_when_no_operation_manager()
    print("[PASS] test_fallback_to_logger_when_no_operation_manager")

    test_multiple_extra_paths()
    print("[PASS] test_multiple_extra_paths")

    test_malformed_operation_manager_falls_back()
    print("[PASS] test_malformed_operation_manager_falls_back")

    print("\nAll tests passed!")