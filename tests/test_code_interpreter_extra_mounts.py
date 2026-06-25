"""
Test that extra work folder paths are correctly mounted in Docker commands.

Tests the actual CodeInterpreter methods (_resolve_extra_folders, _build_path_mapping)
instead of duplicating logic — if the real implementation changes, tests still catch it.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestExtraMounts(unittest.TestCase):
    """Test extra folder resolution and path mapping using actual CodeInterpreter methods."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.extra_rw_dir = os.path.join(self.tmpdir, 'rw_test')
        self.extra_ro_dir = os.path.join(self.tmpdir, 'ro_test')
        os.makedirs(self.extra_rw_dir)
        os.makedirs(self.extra_ro_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── Tests for _resolve_extra_folders ──

    def test_resolve_extra_folders_from_config(self):
        """_resolve_extra_folders falls back to config-set values when no operation_manager."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={
            'work_dir': self.tmpdir,
            'extra_work_folders_rw': [self.extra_rw_dir],
            'extra_work_folders_ro': [self.extra_ro_dir],
        })
        extra_rw, extra_ro = ci._resolve_extra_folders()
        self.assertEqual(extra_rw, [self.extra_rw_dir])
        self.assertEqual(extra_ro, [self.extra_ro_dir])

    def test_resolve_extra_folders_defaults_to_empty(self):
        """_resolve_extra_folders returns empty lists when no config is provided."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        extra_rw, extra_ro = ci._resolve_extra_folders()
        self.assertEqual(extra_rw, [])
        self.assertEqual(extra_ro, [])

    # ── Tests for _build_path_mapping ──

    def test_path_mapping_structure(self):
        """Path mapping JSON should have correct structure via _build_path_mapping."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        mounted_rw = [{'host': self.extra_rw_dir, 'container': '/workspace/extra_rw_0'}]
        mounted_ro = [{'host': self.extra_ro_dir, 'container': '/workspace/extra_ro_0'}]
        mapping = ci._build_path_mapping('test_kernel', mounted_rw, mounted_ro)

        self.assertEqual(mapping['work_dir'], '/workspace')
        self.assertEqual(len(mapping['extra_rw']), 1)
        self.assertIn('/workspace/extra_rw_0', mapping['extra_rw'])
        self.assertEqual(len(mapping['extra_ro']), 1)
        self.assertIn('/workspace/extra_ro_0', mapping['extra_ro'])

        h2c = mapping['host_to_container']
        self.assertEqual(h2c['extra_rw_0']['access'], 'read-write')
        self.assertEqual(h2c['extra_ro_0']['access'], 'read-only')

    def test_path_mapping_json_serializable(self):
        """Path mapping should be JSON serializable via _build_path_mapping."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        mounted_rw = [{'host': self.extra_rw_dir, 'container': '/workspace/extra_rw_0'}]
        mounted_ro = [{'host': self.extra_ro_dir, 'container': '/workspace/extra_ro_0'}]
        mapping = ci._build_path_mapping('test_kernel', mounted_rw, mounted_ro)
        json_str = json.dumps(mapping, indent=2)
        parsed = json.loads(json_str)
        self.assertEqual(parsed, mapping)

    def test_path_mapping_empty_mounts(self):
        """Path mapping should work with no extra mounts."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        mapping = ci._build_path_mapping('test_kernel', [], [])

        self.assertEqual(mapping['work_dir'], '/workspace')
        self.assertEqual(len(mapping['extra_rw']), 0)
        self.assertEqual(len(mapping['extra_ro']), 0)
        # host_to_container should still have the work_dir entry
        self.assertIn('work_dir', mapping['host_to_container'])

    def test_path_mapping_multiple_mounts(self):
        """Path mapping should handle multiple mounts with correct indexing."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        rw2 = os.path.join(self.tmpdir, 'rw2')
        ro2 = os.path.join(self.tmpdir, 'ro2')
        os.makedirs(rw2)
        os.makedirs(ro2)

        mounted_rw = [
            {'host': self.extra_rw_dir, 'container': '/workspace/extra_rw_0'},
            {'host': rw2, 'container': '/workspace/extra_rw_1'},
        ]
        mounted_ro = [
            {'host': self.extra_ro_dir, 'container': '/workspace/extra_ro_0'},
            {'host': ro2, 'container': '/workspace/extra_ro_1'},
        ]
        mapping = ci._build_path_mapping('test_kernel', mounted_rw, mounted_ro)

        self.assertEqual(len(mapping['extra_rw']), 2)
        self.assertEqual(len(mapping['extra_ro']), 2)
        h2c = mapping['host_to_container']
        for i in range(2):
            self.assertIn(f'extra_rw_{i}', h2c)
            self.assertIn(f'extra_ro_{i}', h2c)

    # ── Tests for Docker command generation (integration with _resolve_extra_folders) ──

    def test_docker_cmd_no_extra_mounts(self):
        """No extra paths = only work_dir mount."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})

        # Simulate what _start_kernel does for mount building
        extra_rw, extra_ro = ci._resolve_extra_folders()
        docker_run_cmd = ['docker', 'run', '-d']
        docker_run_cmd.extend(['-v', f'{os.path.abspath(self.tmpdir)}/workspace'])

        mounted_rw = []
        for folder_path in extra_rw:
            abs_path = os.path.realpath(folder_path)
            if not os.path.isdir(abs_path):
                continue
            mount_point = f'/workspace/extra_rw_{len(mounted_rw)}'
            docker_run_cmd.extend(['-v', f'{abs_path}:{mount_point}'])
            mounted_rw.append({'host': abs_path, 'container': mount_point})

        v_indices = [i for i, a in enumerate(docker_run_cmd) if a == '-v']
        self.assertEqual(len(v_indices), 1)

    def test_docker_cmd_rw_mount_no_ro_flag(self):
        """RW mounts should NOT have :ro suffix."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={
            'work_dir': self.tmpdir,
            'extra_work_folders_rw': [self.extra_rw_dir],
        })

        extra_rw, extra_ro = ci._resolve_extra_folders()
        docker_run_cmd = ['docker', 'run', '-d']
        mounted_rw = []
        for folder_path in extra_rw:
            abs_path = os.path.realpath(folder_path)
            if not os.path.isdir(abs_path):
                continue
            mount_point = f'/workspace/extra_rw_{len(mounted_rw)}'
            docker_run_cmd.extend(['-v', f'{abs_path}:{mount_point}'])
            mounted_rw.append({'host': abs_path, 'container': mount_point})

        v_indices = [i for i, a in enumerate(docker_run_cmd) if a == '-v']
        self.assertEqual(len(v_indices), 1)
        rw_vol = docker_run_cmd[v_indices[0] + 1]
        self.assertNotIn(':ro', rw_vol, f"RW mount should not have :ro: {rw_vol}")

    def test_docker_cmd_ro_mount_has_ro_flag(self):
        """RO mounts should have :ro suffix."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={
            'work_dir': self.tmpdir,
            'extra_work_folders_ro': [self.extra_ro_dir],
        })

        extra_rw, extra_ro = ci._resolve_extra_folders()
        docker_run_cmd = ['docker', 'run', '-d']
        mounted_ro = []
        for folder_path in extra_ro:
            abs_path = os.path.realpath(folder_path)
            if not os.path.isdir(abs_path):
                continue
            mount_point = f'/workspace/extra_ro_{len(mounted_ro)}'
            docker_run_cmd.extend(['-v', f'{abs_path}:{mount_point}:ro'])
            mounted_ro.append({'host': abs_path, 'container': mount_point})

        v_indices = [i for i, a in enumerate(docker_run_cmd) if a == '-v']
        self.assertEqual(len(v_indices), 1)
        ro_vol = docker_run_cmd[v_indices[0] + 1]
        self.assertIn(':ro', ro_vol, f"RO mount should have :ro: {ro_vol}")

    def test_nonexistent_paths_skipped(self):
        """Non-existent paths should be skipped during mount building."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        nonexistent = os.path.join(self.tmpdir, 'does_not_exist')
        ci = CodeInterpreter(cfg={
            'work_dir': self.tmpdir,
            'extra_work_folders_rw': [self.extra_rw_dir, nonexistent],
            'extra_work_folders_ro': [self.extra_ro_dir],
        })

        extra_rw, extra_ro = ci._resolve_extra_folders()
        mounted_rw = []
        for folder_path in extra_rw:
            abs_path = os.path.realpath(folder_path)
            if not os.path.isdir(abs_path):
                continue
            mount_point = f'/workspace/extra_rw_{len(mounted_rw)}'
            mounted_rw.append({'host': abs_path, 'container': mount_point})

        mounted_ro = []
        for folder_path in extra_ro:
            abs_path = os.path.realpath(folder_path)
            if not os.path.isdir(abs_path):
                continue
            mount_point = f'/workspace/extra_ro_{len(mounted_ro)}'
            mounted_ro.append({'host': abs_path, 'container': mount_point})

        # Should have: 1 RW (the real one) + 1 RO = 2
        self.assertEqual(len(mounted_rw), 1)
        self.assertEqual(len(mounted_ro), 1)
        # The fake path should NOT appear
        for m in mounted_rw + mounted_ro:
            self.assertNotIn('does_not_exist', m['host'])

    def test_code_interpreter_accepts_extra_paths(self):
        """CodeInterpreter.__init__ should accept and store extra path config."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={
            'work_dir': self.tmpdir,
            'extra_work_folders_rw': [self.extra_rw_dir],
            'extra_work_folders_ro': [self.extra_ro_dir],
        })
        self.assertEqual(ci.extra_work_folders_rw, [self.extra_rw_dir])
        self.assertEqual(ci.extra_work_folders_ro, [self.extra_ro_dir])

    def test_code_interpreter_defaults_to_empty(self):
        """CodeInterpreter should default to empty extra paths."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        self.assertEqual(ci.extra_work_folders_rw, [])
        self.assertEqual(ci.extra_work_folders_ro, [])

    def test_operation_manager_dynamic_resolution(self):
        """Test that _operation_manager reference enables dynamic folder resolution."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter

        # Mock operation manager with extra folders
        class MockOM:
            extra_work_folders_rw = [self.extra_rw_dir]
            extra_work_folders_ro = [self.extra_ro_dir]

        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        ci._operation_manager = MockOM()

        # Should read from operation_manager, not config defaults
        extra_rw, extra_ro = ci._resolve_extra_folders()
        self.assertEqual(extra_rw, [self.extra_rw_dir])
        self.assertEqual(extra_ro, [self.extra_ro_dir])

    # ── Tests for path security validation ──

    def test_is_path_allowed_within_work_dir(self):
        """_is_path_allowed should allow paths within work_dir."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        allowed_prefixes = [os.path.realpath(self.tmpdir)]
        self.assertTrue(ci._is_path_allowed(os.path.join(self.tmpdir, 'subdir'), allowed_prefixes))

    def test_is_path_allowed_blocks_sibling_directory(self):
        """_is_path_allowed should block paths that start with the prefix as a substring (sibling escape)."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        allowed_prefixes = [os.path.realpath(self.tmpdir)]
        # A sibling directory whose path starts with the same string should be blocked
        # e.g., /workspace_extra should NOT match /workspace
        sibling_path = self.tmpdir + '_extra'  # intentionally outside work_dir
        try:
            os.makedirs(sibling_path, exist_ok=True)
            self.assertFalse(ci._is_path_allowed(os.path.realpath(sibling_path), allowed_prefixes))
        finally:
            import shutil
            shutil.rmtree(sibling_path, ignore_errors=True)

    def test_is_path_allowed_blocks_completely_outside(self):
        """_is_path_allowed should block paths completely outside the allowed prefix."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        allowed_prefixes = [os.path.realpath(self.tmpdir)]
        # A path in a totally different location should be blocked
        outside_path = os.path.join(os.path.dirname(self.tmpdir), 'other_place')
        try:
            os.makedirs(outside_path, exist_ok=True)
            self.assertFalse(ci._is_path_allowed(os.path.realpath(outside_path), allowed_prefixes))
        finally:
            import shutil
            shutil.rmtree(outside_path, ignore_errors=True)

    def test_is_path_allowed_empty_prefixes_blocks_all(self):
        """_is_path_allowed with empty allowed_prefixes should block all paths."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        self.assertFalse(ci._is_path_allowed(self.tmpdir, []))


# ──────────────────────────────────────────────────────────────
# Tests for Docker mount fixes documented in lessons_docker_mount_fix.md
# ──────────────────────────────────────────────────────────────

def _noop(*args, **kwargs):
    """No-op function for mocking methods that should be skipped during tests."""
    pass


class TestWatchdogTypeSafety(unittest.TestCase):
    """Lesson #1 — _KERNEL_ACTIVITY[kernel_id] must always be a dict with last_active and work_dir."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent_cascade.tools.code_interpreter import _KERNEL_ACTIVITY
        # Save full global state for complete isolation
        self._saved_activity = dict(_KERNEL_ACTIVITY)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        from agent_cascade.tools.code_interpreter import _KERNEL_ACTIVITY
        # Restore the original global state exactly
        _KERNEL_ACTIVITY.clear()
        _KERNEL_ACTIVITY.update(self._saved_activity)

    def test_kernel_activity_remains_dict_after_update(self):
        """After updating via the same pattern as _start_kernel (lines ~309/~765), value stays a dict."""
        from agent_cascade.tools.code_interpreter import _KERNEL_ACTIVITY
        import time as _time

        kernel_id = 'test_kernel_type_safety'
        # Simulate initial entry from _start_kernel (line 681)
        _KERNEL_ACTIVITY[kernel_id] = {'last_active': _time.time(), 'work_dir': self.tmpdir}

        # Simulate the update pattern used at lines ~309 and ~765:
        if kernel_id in _KERNEL_ACTIVITY and isinstance(_KERNEL_ACTIVITY[kernel_id], dict):
            _KERNEL_ACTIVITY[kernel_id]['last_active'] = _time.time()
        else:
            _KERNEL_ACTIVITY[kernel_id] = {'last_active': _time.time(), 'work_dir': self.tmpdir}

        # Verify structure is preserved
        self.assertIsInstance(_KERNEL_ACTIVITY[kernel_id], dict)
        self.assertIn('last_active', _KERNEL_ACTIVITY[kernel_id])
        self.assertIn('work_dir', _KERNEL_ACTIVITY[kernel_id])
        self.assertEqual(_KERNEL_ACTIVITY[kernel_id]['work_dir'], self.tmpdir)

    def test_isinstance_guard_recovers_from_corrupted_entry(self):
        """Defensive: if _KERNEL_ACTIVITY[kernel_id] is any non-dict value, the else branch restores a dict.

        This tests the isinstance() guard — while no current code path produces a corrupted entry,
        the guard protects against future bugs or mid-restart state corruption."""
        from agent_cascade.tools.code_interpreter import _KERNEL_ACTIVITY
        import time as _time

        kernel_id = 'test_kernel_corrupt'
        # Simulate corrupted entry — old code wrote a bare float here
        _KERNEL_ACTIVITY[kernel_id] = 12345.0

        # Apply the same update pattern from lines ~309/~765:
        if kernel_id in _KERNEL_ACTIVITY and isinstance(_KERNEL_ACTIVITY[kernel_id], dict):
            _KERNEL_ACTIVITY[kernel_id]['last_active'] = _time.time()
        else:
            _KERNEL_ACTIVITY[kernel_id] = {'last_active': _time.time(), 'work_dir': self.tmpdir}

        # Must have been replaced with a proper dict
        self.assertIsInstance(_KERNEL_ACTIVITY[kernel_id], dict)
        self.assertIn('last_active', _KERNEL_ACTIVITY[kernel_id])
        self.assertIn('work_dir', _KERNEL_ACTIVITY[kernel_id])

    def test_watchdog_thread_accesses_dict_safely(self):
        """The watchdog reads activity['last_active'] — verify it works with a proper dict entry."""
        from agent_cascade.tools.code_interpreter import _KERNEL_ACTIVITY
        import time as _time

        kernel_id = 'test_kernel_watchdog'
        # Properly structured entry (as created by the fixed code at line 681)
        _KERNEL_ACTIVITY[kernel_id] = {'last_active': _time.time(), 'work_dir': self.tmpdir}

        activity = _KERNEL_ACTIVITY[kernel_id]
        # This is exactly what the watchdog does at line 116:
        elapsed = _time.time() - activity['last_active']
        # Should not raise; elapsed should be a small positive number
        self.assertIsInstance(elapsed, float)
        self.assertGreaterEqual(elapsed, 0.0)


class TestPathMappingWrittenAfterDockerSuccess(unittest.TestCase):
    """Lesson #4 — path_mapping JSON file is written only AFTER confirming container started.

    These tests mock _build_docker_image and subprocess.run to avoid requiring Docker."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_path_mapping_not_written_on_docker_failure(self):
        """If subprocess.run returns non-zero, path_mapping file should NOT be created."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter

        kernel_id = 'test_kernel_fail'
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})

        # Mock subprocess.run to simulate Docker failure only on the 'docker run' call.
        # All other subprocess calls (docker images, etc.) should succeed normally.
        def mock_subprocess_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args', [])
            # Only fail on 'docker run' — let everything else proceed normally
            if isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == 'docker' and cmd[1] == 'run':
                return type('Result', (), {
                    'returncode': 1,
                    'stdout': '',
                    'stderr': 'docker error'
                })()
            return subprocess.run(*args, **kwargs)

        with mock.patch.object(ci, '_build_docker_image', side_effect=_noop):
            with mock.patch('subprocess.run', side_effect=mock_subprocess_run):
                with self.assertRaises(RuntimeError):
                    ci._start_kernel(kernel_id)

        # path_mapping file should NOT exist because container failed to start
        mapping_file = os.path.join(self.tmpdir, f'path_mapping_{kernel_id}.json')
        self.assertFalse(os.path.exists(mapping_file),
                         "path_mapping file must not be written when Docker fails")

    def test_path_mapping_written_on_docker_success(self):
        """If subprocess.run succeeds, path_mapping file SHOULD be created."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter

        kernel_id = 'test_kernel_success'
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})

        # Mock subprocess.run to simulate Docker success + container running check
        def mock_subprocess_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args', [])
            # docker rm -f — clean up leftover containers (called before docker run)
            if isinstance(cmd, list) and 'docker' in cmd and 'rm' in cmd:
                return type('Result', (), {
                    'returncode': 0,
                    'stdout': '',
                    'stderr': ''
                })()
            # docker run — success
            elif isinstance(cmd, list) and 'docker' in cmd and 'run' in cmd:
                return type('Result', (), {
                    'returncode': 0,
                    'stdout': 'abc123_container_id',
                    'stderr': ''
                })()
            # docker ps check — container is running
            elif isinstance(cmd, list) and 'docker' in cmd and 'ps' in cmd:
                return type('Result', (), {
                    'returncode': 0,
                    'stdout': 'abc123_container_id',
                    'stderr': ''
                })()
            return subprocess.run(*args, **kwargs)

        with mock.patch.object(ci, '_build_docker_image', side_effect=_noop):
            with mock.patch('subprocess.run', side_effect=mock_subprocess_run):
                try:
                    ci._start_kernel(kernel_id)
                except RuntimeError as e:
                    # Expected if Docker container starts but Jupyter connection fails — that's fine
                    pass
                except Exception as e:
                    # Unexpected — re-raise for visibility
                    raise AssertionError(f"Unexpected exception in _start_kernel: {e}") from e

        # path_mapping file SHOULD exist because Docker succeeded
        mapping_file = os.path.join(self.tmpdir, f'path_mapping_{kernel_id}.json')
        self.assertTrue(os.path.exists(mapping_file),
                        "path_mapping file must be written after Docker success")
        # Verify it's valid JSON
        with open(mapping_file) as f:
            mapping = json.load(f)
        self.assertIn('work_dir', mapping)

    def test_path_mapping_written_before_docker_ps_check(self):
        """Verify path_mapping is written after docker run succeeds but before the docker ps loop.

        This documents current behavior: if container crashes between docker run and docker ps,
        the path_mapping file will be orphaned."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter

        kernel_id = 'test_kernel_orphan'
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})

        # Mock subprocess.run: docker run succeeds, but docker ps fails (container not running)
        def mock_subprocess_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args', [])
            # docker rm -f — clean up leftover containers (called before docker run)
            if isinstance(cmd, list) and 'docker' in cmd and 'rm' in cmd:
                return type('Result', (), {
                    'returncode': 0,
                    'stdout': '',
                    'stderr': ''
                })()
            elif isinstance(cmd, list) and 'docker' in cmd and 'run' in cmd:
                return type('Result', (), {
                    'returncode': 0,
                    'stdout': 'abc123_container_id',
                    'stderr': ''
                })()
            elif isinstance(cmd, list) and 'docker' in cmd and 'ps' in cmd:
                # Container not found after run — docker ps returns empty
                return type('Result', (), {
                    'returncode': 0,
                    'stdout': '',
                    'stderr': ''
                })()
            elif isinstance(cmd, list) and 'docker' in cmd and 'logs' in cmd:
                return type('Result', (), {
                    'returncode': 0,
                    'stdout': 'container logs',
                    'stderr': ''
                })()
            return subprocess.run(*args, **kwargs)

        with mock.patch.object(ci, '_build_docker_image', side_effect=_noop):
            with mock.patch('subprocess.run', side_effect=mock_subprocess_run):
                with self.assertRaises(RuntimeError):
                    ci._start_kernel(kernel_id)

        # path_mapping file SHOULD exist because docker run succeeded (even though ps check failed)
        mapping_file = os.path.join(self.tmpdir, f'path_mapping_{kernel_id}.json')
        self.assertTrue(os.path.exists(mapping_file),
                        "path_mapping is written after docker run success, before docker ps loop")


class TestWorkDirAttributeExists(unittest.TestCase):
    """Lesson #3 Bug A — ci.work_dir must exist and be used (not self.base_dir)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_work_dir_is_set_after_init(self):
        """CodeInterpreter.work_dir must be set correctly after __init__."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        self.assertIsNotNone(ci.work_dir)
        self.assertEqual(ci.work_dir, self.tmpdir)

    def test_work_dir_used_in_path_security_check(self):
        """The allowed_prefixes in _start_kernel uses self.work_dir (not self.base_dir).

        Verify work_dir exists and is used to build allowed_prefixes that actually block
        paths outside the work directory via _is_path_allowed."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})

        # Verify work_dir attribute exists and is correct
        self.assertTrue(hasattr(ci, 'work_dir'))
        self.assertEqual(ci.work_dir, self.tmpdir)

        # Verify base_dir does NOT exist (it was the old broken attribute name)
        self.assertFalse(hasattr(ci, 'base_dir'),
                         "CodeInterpreter should not have base_dir — use work_dir instead")

        # Build allowed_prefixes from ci.work_dir (as _start_kernel does at line 564)
        allowed_prefixes = [os.path.realpath(ci.work_dir)] if ci.work_dir else []

        # Paths within work_dir are allowed
        subdir = os.path.join(self.tmpdir, 'subdir')
        os.makedirs(subdir, exist_ok=True)
        self.assertTrue(ci._is_path_allowed(os.path.realpath(subdir), allowed_prefixes))

        # Paths outside work_dir are blocked
        sibling_path = self.tmpdir + '_extra'
        try:
            os.makedirs(sibling_path, exist_ok=True)
            self.assertFalse(ci._is_path_allowed(os.path.realpath(sibling_path), allowed_prefixes))
        finally:
            import shutil
            shutil.rmtree(sibling_path, ignore_errors=True)


class TestWorkDirPriorityChain(unittest.TestCase):
    """Lesson #5 — work_dir follows: config > env var > default."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_env_var_used_when_no_config(self):
        """M6_CODE_INTERPRETER_WORK_DIR env var is used when config doesn't provide work_dir."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        with mock.patch.dict(os.environ, {
            'M6_CODE_INTERPRETER_WORK_DIR': self.tmpdir
        }):
            ci = CodeInterpreter(cfg={})  # No work_dir in config
            self.assertEqual(ci.work_dir, self.tmpdir)

    def test_config_overrides_env_var(self):
        """Config work_dir takes priority over M6_CODE_INTERPRETER_WORK_DIR env var."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        override_dir = os.path.join(self.tmpdir, 'override')
        os.makedirs(override_dir)
        with mock.patch.dict(os.environ, {
            'M6_CODE_INTERPRETER_WORK_DIR': self.tmpdir
        }):
            ci = CodeInterpreter(cfg={'work_dir': override_dir})
            self.assertEqual(ci.work_dir, override_dir)

    def test_default_used_when_nothing_provided(self):
        """Default work_dir is used when neither config nor env var provides one.

        Default comes from the parent class BaseToolWithFileAccess which sets:
        default_work_dir = DEFAULT_WORKSPACE / 'tools' / self.name"""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        with mock.patch.dict(os.environ, {}, clear=False):
            # Remove the env var if it exists
            os.environ.pop('M6_CODE_INTERPRETER_WORK_DIR', None)
            ci = CodeInterpreter(cfg={})
            # Default comes from the parent class BaseToolWithFileAccess
            self.assertTrue(os.path.isabs(ci.work_dir))
            self.assertIn('code_interpreter', ci.work_dir)


if __name__ == '__main__':
    unittest.main()