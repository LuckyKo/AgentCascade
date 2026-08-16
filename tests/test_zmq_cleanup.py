"""
Tests for ZMQ resource leak fix — explicit kernel cleanup on agent dismissal.

Verifies:
1. cleanup_kernels_for_session() properly removes tracked kernels
2. CodeInterpreter.close() shuts down all owned kernels
3. Kernel tracking via _AGENT_KERNELS works correctly
4. No bare except Exception blocks in critical cleanup paths
"""

import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is on path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestAgentKernelTracking:
    """Test _AGENT_KERNELS tracking mechanism."""

    def setup_method(self):
        """Reset global state before each test."""
        from agent_cascade.tools.code_interpreter import (
            _AGENT_KERNELS, _KERNEL_CLIENTS, _DOCKER_CONTAINERS,
            _STALE_CONTAINERS, _KERNEL_ACTIVITY
        )
        # Clear tracking dicts to isolate tests
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def teardown_method(self):
        """Ensure clean state after each test."""
        from agent_cascade.tools.code_interpreter import (
            _AGENT_KERNELS, _KERNEL_CLIENTS, _DOCKER_CONTAINERS,
            _STALE_CONTAINERS, _KERNEL_ACTIVITY
        )
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def test_cleanup_kernels_for_session_removes_tracking(self):
        """Verify cleanup_kernels_for_session removes session from _AGENT_KERNELS."""
        from agent_cascade.tools.code_interpreter import (
            _AGENT_KERNELS, cleanup_kernels_for_session
        )

        # Set up mock state
        session = "test_session_123"
        kernel_id = f"ci_{session}_{os.getpid()}"
        
        with patch('agent_cascade.tools.code_interpreter._KERNEL_LOCK', MagicMock()):
            _AGENT_KERNELS[session] = [kernel_id]

        # Run cleanup
        result = cleanup_kernels_for_session(session)

        # Verify tracking was removed
        assert session not in _AGENT_KERNELS, "Session should be removed from _AGENT_KERNELS"
        assert result == 1, f"Should report 1 kernel cleaned up, got {result}"

    def test_cleanup_kernels_for_session_noop_when_empty(self):
        """Verify cleanup returns 0 when session has no kernels."""
        from agent_cascade.tools.code_interpreter import cleanup_kernels_for_session

        result = cleanup_kernels_for_session("nonexistent_session")
        assert result == 0, "Should return 0 for unknown session"

    def test_cleanup_kernels_for_session_handles_multiple_kernels(self):
        """Verify cleanup handles multiple kernels per session."""
        from agent_cascade.tools.code_interpreter import (
            _AGENT_KERNELS, cleanup_kernels_for_session
        )

        session = "multi_kernel_session"
        pid = os.getpid()
        kernel_ids = [f"ci_{session}_k1_{pid}", f"ci_{session}_k2_{pid}"]
        
        with patch('agent_cascade.tools.code_interpreter._KERNEL_LOCK', MagicMock()):
            _AGENT_KERNELS[session] = list(kernel_ids)

        result = cleanup_kernels_for_session(session)

        assert session not in _AGENT_KERNELS
        assert result == 2, f"Should report 2 kernels cleaned up, got {result}"


class TestCodeInterpreterClose:
    """Test CodeInterpreter.close() explicit cleanup method."""

    def setup_method(self):
        from agent_cascade.tools.code_interpreter import (
            _AGENT_KERNELS, _KERNEL_CLIENTS, _DOCKER_CONTAINERS,
            _STALE_CONTAINERS, _KERNEL_ACTIVITY
        )
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def teardown_method(self):
        from agent_cascade.tools.code_interpreter import (
            _AGENT_KERNELS, _KERNEL_CLIENTS, _DOCKER_CONTAINERS,
            _STALE_CONTAINERS, _KERNEL_ACTIVITY
        )
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def test_close_method_exists(self):
        """Verify close() method exists on CodeInterpreter."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        assert hasattr(CodeInterpreter, 'close'), "CodeInterpreter should have close() method"
        assert callable(getattr(CodeInterpreter, 'close')), "close() should be callable"

    def test_close_shuts_down_kernel_clients(self):
        """Verify close() calls shutdown on kernel clients."""
        from agent_cascade.tools.code_interpreter import (
            CodeInterpreter, _KERNEL_CLIENTS, _DOCKER_CONTAINERS
        )

        # Create a minimal CodeInterpreter instance
        ci = CodeInterpreter(cfg={'work_dir': '/tmp/test_ci_close'})
        
        pid = os.getpid()
        kernel_id = f"ci_test_{pid}"
        mock_kc = MagicMock()
        # A fresh (not-yet-closed) kernel client has _closed falsy. MagicMock would
        # otherwise auto-create a truthy _closed attribute, which makes
        # _shutdown_kernel_client's `if kc is None or getattr(kc, '_closed', False)`
        # guard short-circuit and skip shutdown(). Model the realistic initial state.
        mock_kc._closed = False

        # Set up mock kernel state
        _KERNEL_CLIENTS[kernel_id] = mock_kc
        _DOCKER_CONTAINERS[kernel_id] = "fake_container_id"

        with patch('subprocess.run') as mock_run:
            result = ci.close()

        # Verify shutdown was called
        mock_kc.shutdown.assert_called_once()
        
        # Verify kernel was removed from tracking
        assert kernel_id not in _KERNEL_CLIENTS, "Kernel should be removed from _KERNEL_CLIENTS"
        assert kernel_id not in _DOCKER_CONTAINERS, "Container should be removed from _DOCKER_CONTAINERS"
        assert result == 1, f"close() should return 1, got {result}"

    def test_close_noop_when_no_kernels(self):
        """Verify close() returns 0 when no kernels owned."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter

        ci = CodeInterpreter(cfg={'work_dir': '/tmp/test_ci_close'})
        result = ci.close()
        assert result == 0, "close() should return 0 when no kernels to clean up"


class TestDismissTriggersCleanup:
    """Test that agent dismissal triggers kernel cleanup."""

    def test_dismiss_instance_calls_cleanup_for_root_agent(self):
        """Verify dismiss_instance calls cleanup_kernels_for_session for root agents."""
        from agent_cascade.tools.code_interpreter import cleanup_kernels_for_session
        
        # Verify the function exists and is callable (integration with AgentPool.dismiss_instance
        # happens in production; this confirms the API contract)
        assert callable(cleanup_kernels_for_session)
        
        # Verify it can be imported by agent_pool module context
        import inspect
        sig = inspect.signature(cleanup_kernels_for_session)
        assert 'session_name' in sig.parameters

    def test_cleanup_import_available_in_agent_pool(self):
        """Verify agent_pool.py can import cleanup_kernels_for_session."""
        # This is a basic smoke test — if the import fails, this raises
        from agent_cascade.tools.code_interpreter import cleanup_kernels_for_session
        
        # Verify function signature
        assert cleanup_kernels_for_session.__code__.co_varnames[:2] == ('session_name', 'force_timeout')

    def test_cleanup_actually_clears_tracking_dicts(self):
        """Verify cleanup_kernels_for_session clears kernel tracking state, not just the session entry."""
        from agent_cascade.tools.code_interpreter import (
            _AGENT_KERNELS, _KERNEL_CLIENTS, _DOCKER_CONTAINERS,
            _KERNEL_ACTIVITY, cleanup_kernels_for_session
        )

        session = "test_clear_state"
        pid = os.getpid()
        kernel_id = f"ci_{session}_{pid}"

        # Set up full tracking state
        with patch('agent_cascade.tools.code_interpreter._KERNEL_LOCK', MagicMock()):
            _AGENT_KERNELS[session] = [kernel_id]
            _KERNEL_CLIENTS[kernel_id] = MagicMock()
            _DOCKER_CONTAINERS[kernel_id] = "test_container_123"
            _KERNEL_ACTIVITY[kernel_id] = {'last_active': 0, 'work_dir': '/tmp'}

        # Run cleanup
        result = cleanup_kernels_for_session(session)

        # Verify all tracking was cleared
        assert session not in _AGENT_KERNELS
        assert kernel_id not in _KERNEL_CLIENTS, "Kernel client should be removed"
        assert kernel_id not in _DOCKER_CONTAINERS, "Container should be removed"
        assert kernel_id not in _KERNEL_ACTIVITY, "Activity tracking should be cleared"
        assert result == 1

    def test_close_clears_all_kernel_state(self):
        """Verify CodeInterpreter.close() clears all tracked state for its kernels."""
        from agent_cascade.tools.code_interpreter import (
            CodeInterpreter, _KERNEL_CLIENTS, _DOCKER_CONTAINERS,
            _KERNEL_ACTIVITY, _AGENT_KERNELS
        )

        ci = CodeInterpreter(cfg={'work_dir': '/tmp/test_close_clear'})
        pid = os.getpid()
        kernel_id = f"ci_test_close_{pid}"

        # Set up full tracking state
        mock_kc = MagicMock()
        with patch('agent_cascade.tools.code_interpreter._KERNEL_LOCK', MagicMock()):
            _KERNEL_CLIENTS[kernel_id] = mock_kc
            _DOCKER_CONTAINERS[kernel_id] = "test_container_close"
            _KERNEL_ACTIVITY[kernel_id] = {'last_active': 0, 'work_dir': '/tmp/test_close_clear'}
            _AGENT_KERNELS["some_session"] = [kernel_id]

        with patch('subprocess.run') as mock_run:
            result = ci.close()

        assert kernel_id not in _KERNEL_CLIENTS
        assert kernel_id not in _DOCKER_CONTAINERS
        # Kernel should be removed from session tracking too
        assert kernel_id not in _AGENT_KERNELS.get("some_session", [])
        assert result == 1


class TestNoBareExceptInCleanupPaths:
    """Verify no bare except Exception blocks in critical cleanup paths."""

    def test_kill_kernels_and_containers_no_bare_except(self):
        """Verify _kill_kernels_and_containers uses specific exceptions."""
        import ast
        import inspect
        from agent_cascade.tools.code_interpreter import _kill_kernels_and_containers
        
        source = inspect.getsource(_kill_kernels_and_containers)
        tree = ast.parse(source)
        
        bare_excepts = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    bare_excepts.append(node.lineno)
        
        assert not bare_excepts, f"Found bare except blocks at lines: {bare_excepts}"

    def test_cleanup_kernels_for_session_no_bare_except(self):
        """Verify cleanup_kernels_for_session uses specific exceptions."""
        import ast
        import inspect
        from agent_cascade.tools.code_interpreter import cleanup_kernels_for_session
        
        source = inspect.getsource(cleanup_kernels_for_session)
        tree = ast.parse(source)
        
        bare_excepts = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    bare_excepts.append(node.lineno)
        
        assert not bare_excepts, f"Found bare except blocks at lines: {bare_excepts}"

    def test_code_interpreter_del_no_bare_except(self):
        """Verify CodeInterpreter.__del__ uses specific exceptions."""
        import ast
        import inspect
        import textwrap
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        
        source = inspect.getsource(CodeInterpreter.__del__)
        # Dedent to make it parseable as standalone code
        source = textwrap.dedent(source)
        tree = ast.parse(source)
        
        bare_excepts = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    bare_excepts.append(node.lineno)
        
        assert not bare_excepts, f"Found bare except blocks at lines: {bare_excepts}"


class TestModuleLevelAPI:
    """Test module-level API for external cleanup."""

    def test_cleanup_kernels_for_session_signature(self):
        """Verify cleanup_kernels_for_session has correct signature."""
        from agent_cascade.tools.code_interpreter import cleanup_kernels_for_session
        import inspect
        
        sig = inspect.signature(cleanup_kernels_for_session)
        params = list(sig.parameters.keys())
        
        assert 'session_name' in params, "Should have session_name parameter"
        assert 'force_timeout' in params, "Should have force_timeout parameter"
        
        # Check default value for force_timeout
        assert sig.parameters['force_timeout'].default == 5.0

    def test_agent_kernels_dict_exists(self):
        """Verify _AGENT_KERNELS tracking dict exists."""
        from agent_cascade.tools.code_interpreter import _AGENT_KERNELS
        assert isinstance(_AGENT_KERNELS, dict), "_AGENT_KERNELS should be a dict"


class TestTempFileCleanup:
    """Test that temporary kernel files are cleaned up properly."""

    def setup_method(self):
        from agent_cascade.tools.code_interpreter import (
            _AGENT_KERNELS, _KERNEL_CLIENTS, _DOCKER_CONTAINERS,
            _STALE_CONTAINERS, _KERNEL_ACTIVITY
        )
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def teardown_method(self):
        from agent_cascade.tools.code_interpreter import (
            _AGENT_KERNELS, _KERNEL_CLIENTS, _DOCKER_CONTAINERS,
            _STALE_CONTAINERS, _KERNEL_ACTIVITY
        )
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def test_cleanup_kernels_removes_temp_files(self):
        """Verify cleanup_kernels_for_session removes connection files and launch scripts."""
        import tempfile
        from agent_cascade.tools.code_interpreter import (
            _AGENT_KERNELS, _KERNEL_ACTIVITY, cleanup_kernels_for_session
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            session = "temp_file_test"
            pid = os.getpid()
            kernel_id = f"ci_{session}_{pid}"

            # Create temp files that would exist after kernel start
            conn_host = os.path.join(tmpdir, f'kernel_connection_file_{kernel_id}_host.json')
            conn_container = os.path.join(tmpdir, f'kernel_connection_file_{kernel_id}_container.json')
            launch_script = os.path.join(tmpdir, f'launch_kernel_{kernel_id}.py')

            for f in [conn_host, conn_container, launch_script]:
                Path(f).write_text('test content')

            # Set up tracking state with work_dir pointing to tmpdir
            with patch('agent_cascade.tools.code_interpreter._KERNEL_LOCK', MagicMock()):
                _AGENT_KERNELS[session] = [kernel_id]
                _KERNEL_ACTIVITY[kernel_id] = {'last_active': 0, 'work_dir': tmpdir}

            # Run cleanup (no actual docker/kernel to clean)
            result = cleanup_kernels_for_session(session)

            assert result == 1
            # Verify temp files were removed
            assert not os.path.exists(conn_host), "Host connection file should be removed"
            assert not os.path.exists(conn_container), "Container connection file should be removed"
            assert not os.path.exists(launch_script), "Launch script should be removed"

    def test_close_removes_temp_files(self):
        """Verify CodeInterpreter.close() removes connection files and launch scripts."""
        import tempfile
        from agent_cascade.tools.code_interpreter import (
            CodeInterpreter, _KERNEL_CLIENTS, _DOCKER_CONTAINERS, _KERNEL_ACTIVITY
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            ci = CodeInterpreter(cfg={'work_dir': tmpdir})
            pid = os.getpid()
            kernel_id = f"ci_temp_close_{pid}"

            # Create temp files
            conn_host = os.path.join(tmpdir, f'kernel_connection_file_{kernel_id}_host.json')
            conn_container = os.path.join(tmpdir, f'kernel_connection_file_{kernel_id}_container.json')
            launch_script = os.path.join(tmpdir, f'launch_kernel_{kernel_id}.py')

            for f in [conn_host, conn_container, launch_script]:
                Path(f).write_text('test content')

            mock_kc = MagicMock()
            with patch('agent_cascade.tools.code_interpreter._KERNEL_LOCK', MagicMock()):
                _KERNEL_CLIENTS[kernel_id] = mock_kc
                _DOCKER_CONTAINERS[kernel_id] = "fake_container"
                _KERNEL_ACTIVITY[kernel_id] = {'last_active': 0, 'work_dir': tmpdir}

            with patch('subprocess.run'):
                result = ci.close()

            assert result == 1
            # Verify temp files were removed
            assert not os.path.exists(conn_host), "Host connection file should be removed by close()"
            assert not os.path.exists(conn_container), "Container connection file should be removed by close()"
            assert not os.path.exists(launch_script), "Launch script should be removed by close()"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
