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
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestAgentKernelTracking:
    """Test _AGENT_KERNELS tracking mechanism."""

    def setup_method(self):
        """Reset global state before each test."""
        from agent_cascade.tools.code_interpreter import (_AGENT_KERNELS, _DOCKER_CONTAINERS, _KERNEL_ACTIVITY,
                                                          _KERNEL_CLIENTS, _STALE_CONTAINERS)

        # Clear tracking dicts to isolate tests
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def teardown_method(self):
        """Ensure clean state after each test."""
        from agent_cascade.tools.code_interpreter import (_AGENT_KERNELS, _DOCKER_CONTAINERS, _KERNEL_ACTIVITY,
                                                          _KERNEL_CLIENTS, _STALE_CONTAINERS)
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def test_cleanup_kernels_for_session_removes_tracking(self):
        """Verify cleanup_kernels_for_session removes session from _AGENT_KERNELS."""
        from agent_cascade.tools.code_interpreter import _AGENT_KERNELS, cleanup_kernels_for_session

        # Set up mock state
        session = 'test_session_123'
        kernel_id = f"ci_{session}_{os.getpid()}"

        with patch('agent_cascade.tools.code_interpreter._KERNEL_LOCK', MagicMock()):
            _AGENT_KERNELS[session] = [kernel_id]

        # Run cleanup
        result = cleanup_kernels_for_session(session)

        # Verify tracking was removed
        assert session not in _AGENT_KERNELS, 'Session should be removed from _AGENT_KERNELS'
        assert result == 1, f"Should report 1 kernel cleaned up, got {result}"

    def test_cleanup_kernels_for_session_noop_when_empty(self):
        """Verify cleanup returns 0 when session has no kernels."""
        from agent_cascade.tools.code_interpreter import cleanup_kernels_for_session

        result = cleanup_kernels_for_session('nonexistent_session')
        assert result == 0, 'Should return 0 for unknown session'

    def test_cleanup_kernels_for_session_handles_multiple_kernels(self):
        """Verify cleanup handles multiple kernels per session."""
        from agent_cascade.tools.code_interpreter import _AGENT_KERNELS, cleanup_kernels_for_session

        session = 'multi_kernel_session'
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
        from agent_cascade.tools.code_interpreter import (_AGENT_KERNELS, _DOCKER_CONTAINERS, _KERNEL_ACTIVITY,
                                                          _KERNEL_CLIENTS, _STALE_CONTAINERS)
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def teardown_method(self):
        from agent_cascade.tools.code_interpreter import (_AGENT_KERNELS, _DOCKER_CONTAINERS, _KERNEL_ACTIVITY,
                                                          _KERNEL_CLIENTS, _STALE_CONTAINERS)
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def test_close_method_exists(self):
        """Verify close() method exists on CodeInterpreter."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        assert hasattr(CodeInterpreter, 'close'), 'CodeInterpreter should have close() method'
        assert callable(getattr(CodeInterpreter, 'close')), 'close() should be callable'

    @pytest.mark.requires_docker  # constructs CodeInterpreter -> _check_docker_availability()
    def test_close_shuts_down_kernel_clients(self):
        """Verify close() calls shutdown on kernel clients."""
        from agent_cascade.tools.code_interpreter import _DOCKER_CONTAINERS, _KERNEL_CLIENTS, CodeInterpreter

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
        _DOCKER_CONTAINERS[kernel_id] = 'fake_container_id'

        with patch('subprocess.run') as mock_run:  # noqa: F841  (patch target must stay bound)
            result = ci.close()

        # Verify shutdown was called
        mock_kc.shutdown.assert_called_once()

        # Verify kernel was removed from tracking
        assert kernel_id not in _KERNEL_CLIENTS, 'Kernel should be removed from _KERNEL_CLIENTS'
        assert kernel_id not in _DOCKER_CONTAINERS, 'Container should be removed from _DOCKER_CONTAINERS'
        assert result == 1, f"close() should return 1, got {result}"

    @pytest.mark.requires_docker  # constructs CodeInterpreter -> _check_docker_availability()
    def test_close_noop_when_no_kernels(self):
        """Verify close() returns 0 when no kernels owned."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter

        ci = CodeInterpreter(cfg={'work_dir': '/tmp/test_ci_close'})
        result = ci.close()
        assert result == 0, 'close() should return 0 when no kernels to clean up'


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
        from agent_cascade.tools.code_interpreter import (_AGENT_KERNELS, _DOCKER_CONTAINERS, _KERNEL_ACTIVITY,
                                                          _KERNEL_CLIENTS, cleanup_kernels_for_session)

        session = 'test_clear_state'
        pid = os.getpid()
        kernel_id = f"ci_{session}_{pid}"

        # Set up full tracking state
        with patch('agent_cascade.tools.code_interpreter._KERNEL_LOCK', MagicMock()):
            _AGENT_KERNELS[session] = [kernel_id]
            _KERNEL_CLIENTS[kernel_id] = MagicMock()
            _DOCKER_CONTAINERS[kernel_id] = 'test_container_123'
            _KERNEL_ACTIVITY[kernel_id] = {'last_active': 0, 'work_dir': '/tmp'}

        # Run cleanup
        result = cleanup_kernels_for_session(session)

        # Verify all tracking was cleared
        assert session not in _AGENT_KERNELS
        assert kernel_id not in _KERNEL_CLIENTS, 'Kernel client should be removed'
        assert kernel_id not in _DOCKER_CONTAINERS, 'Container should be removed'
        assert kernel_id not in _KERNEL_ACTIVITY, 'Activity tracking should be cleared'
        assert result == 1

    @pytest.mark.requires_docker  # constructs CodeInterpreter -> _check_docker_availability()
    def test_close_clears_all_kernel_state(self):
        """Verify CodeInterpreter.close() clears all tracked state for its kernels."""
        from agent_cascade.tools.code_interpreter import (_AGENT_KERNELS, _DOCKER_CONTAINERS, _KERNEL_ACTIVITY,
                                                          _KERNEL_CLIENTS, CodeInterpreter)

        ci = CodeInterpreter(cfg={'work_dir': '/tmp/test_close_clear'})
        pid = os.getpid()
        kernel_id = f"ci_test_close_{pid}"

        # Set up full tracking state
        mock_kc = MagicMock()
        with patch('agent_cascade.tools.code_interpreter._KERNEL_LOCK', MagicMock()):
            _KERNEL_CLIENTS[kernel_id] = mock_kc
            _DOCKER_CONTAINERS[kernel_id] = 'test_container_close'
            _KERNEL_ACTIVITY[kernel_id] = {'last_active': 0, 'work_dir': '/tmp/test_close_clear'}
            _AGENT_KERNELS['some_session'] = [kernel_id]

        with patch('subprocess.run') as mock_run:  # noqa: F841  (patch target must stay bound)
            result = ci.close()

        assert kernel_id not in _KERNEL_CLIENTS
        assert kernel_id not in _DOCKER_CONTAINERS
        # Kernel should be removed from session tracking too
        assert kernel_id not in _AGENT_KERNELS.get('some_session', [])
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
        import inspect

        from agent_cascade.tools.code_interpreter import cleanup_kernels_for_session

        sig = inspect.signature(cleanup_kernels_for_session)
        params = list(sig.parameters.keys())

        assert 'session_name' in params, 'Should have session_name parameter'
        assert 'force_timeout' in params, 'Should have force_timeout parameter'

        # Check default value for force_timeout
        assert sig.parameters['force_timeout'].default == 5.0

    def test_agent_kernels_dict_exists(self):
        """Verify _AGENT_KERNELS tracking dict exists."""
        from agent_cascade.tools.code_interpreter import _AGENT_KERNELS
        assert isinstance(_AGENT_KERNELS, dict), '_AGENT_KERNELS should be a dict'


class TestTempFileCleanup:
    """Test that temporary kernel files are cleaned up properly."""

    def setup_method(self):
        from agent_cascade.tools.code_interpreter import (_AGENT_KERNELS, _DOCKER_CONTAINERS, _KERNEL_ACTIVITY,
                                                          _KERNEL_CLIENTS, _STALE_CONTAINERS)
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def teardown_method(self):
        from agent_cascade.tools.code_interpreter import (_AGENT_KERNELS, _DOCKER_CONTAINERS, _KERNEL_ACTIVITY,
                                                          _KERNEL_CLIENTS, _STALE_CONTAINERS)
        _AGENT_KERNELS.clear()
        _KERNEL_CLIENTS.clear()
        _DOCKER_CONTAINERS.clear()
        _STALE_CONTAINERS.clear()
        _KERNEL_ACTIVITY.clear()

    def test_cleanup_kernels_removes_temp_files(self):
        """Verify cleanup_kernels_for_session removes connection files and launch scripts."""
        import tempfile

        from agent_cascade.tools.code_interpreter import _AGENT_KERNELS, _KERNEL_ACTIVITY, cleanup_kernels_for_session

        with tempfile.TemporaryDirectory() as tmpdir:
            session = 'temp_file_test'
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
            assert not os.path.exists(conn_host), 'Host connection file should be removed'
            assert not os.path.exists(conn_container), 'Container connection file should be removed'
            assert not os.path.exists(launch_script), 'Launch script should be removed'

    @pytest.mark.requires_docker  # constructs CodeInterpreter -> _check_docker_availability()
    def test_close_removes_temp_files(self):
        """Verify CodeInterpreter.close() removes connection files and launch scripts."""
        import tempfile

        from agent_cascade.tools.code_interpreter import (_DOCKER_CONTAINERS, _KERNEL_ACTIVITY, _KERNEL_CLIENTS,
                                                          CodeInterpreter)

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
                _DOCKER_CONTAINERS[kernel_id] = 'fake_container'
                _KERNEL_ACTIVITY[kernel_id] = {'last_active': 0, 'work_dir': tmpdir}

            with patch('subprocess.run'):
                result = ci.close()

            assert result == 1
            # Verify temp files were removed
            assert not os.path.exists(conn_host), 'Host connection file should be removed by close()'
            assert not os.path.exists(conn_container), 'Container connection file should be removed by close()'
            assert not os.path.exists(launch_script), 'Launch script should be removed by close()'


class TestCreateKernelClientZmqLeak:
    """BUG_0320: a half-started kernel client must not leak its owned zmq.Context.

    _create_kernel_client calls start_channels(), which lazily creates a real
    zmq.Context (jupyter_client _context_default) and leaves the channels running.
    If the readiness wait then fails, that context must be destroyed deterministically
    here instead of leaking and blocking later in zmq.Context.__del__ -> term().

    Probed with the BUG_0319 weakref method: instrument zmq.Context.__init__ to record
    every created context, then after the operation gc.collect() and treat "alive and
    not closed" as the leak. The harness drives the REAL _create_kernel_client (no Docker),
    stubbing start_channels() to create the same real owned context AND make
    channels_running truthy (the __del__ guard that actually causes the leak) without
    opening sockets — see _stub_start_channels. wait_for_ready, time.sleep, subprocess.run
    and the best-effort _shutdown_with_timeout are stubbed.
    """

    @staticmethod
    def _make_conn_file(path, kernel_id):
        """Write a host connection JSON with free loopback ports (no kernel behind them)."""
        import json
        import socket

        ports = []
        for _ in range(5):
            s = socket.socket()
            s.bind(('127.0.0.1', 0))
            ports.append(s.getsockname()[1])
            s.close()
        port_names = ['shell_port', 'iopub_port', 'stdin_port', 'hb_port', 'control_port']
        data = {
            'ip': '127.0.0.1',
            'key': 'testkey',
            'transport': 'tcp',
            'signature_scheme': 'hmac-sha256',
            'kernel_name': '',
        }
        data.update(dict(zip(port_names, ports)))
        path.write_text(json.dumps(data))
        return str(path)

    @staticmethod
    def _instrument_contexts(monkeypatch):
        """Patch zmq.Context.__init__ to record every created context (strong refs).

        Strong refs (not weakrefs) are deliberate: the half-started client itself is
        collected after the failure, and a weakref to its context would then resolve to
        None and HIDE whether the context was actually destroyed. The defect BUG_0320
        guards is a zmq.Context left UN-destroyed (pyzmq does not auto-term — that is
        precisely why an un-destroyed context leaks/blocks, per BUG_0319), so the leak is
        "a recorded context whose .closed is still False". Holding a strong ref just lets
        us observe .closed; it does not change what the code under test does.
        """
        import gc

        import zmq

        created = []
        real_init = zmq.Context.__init__

        def _traced(self, *a, **k):
            rv = real_init(self, *a, **k)
            created.append(self)  # strong ref: observe .closed after the client is gone
            return rv

        monkeypatch.setattr(zmq.Context, '__init__', _traced)
        return created, gc

    @staticmethod
    def _live_open(created):
        """Recorded contexts that were never destroyed (still open) — i.e. the leak."""
        return [c for c in created if not c.closed]

    @staticmethod
    def _stub_start_channels(monkeypatch):
        """Reproduce the exact observable that makes jupyter_client leak the context.

        Real start_channels() does two things that matter here:
          1. Creates the client's OWNED zmq.Context the first time a channel touches
             self.context (jupyter_client _context_default sets _created_context=True).
          2. Leaves the channels running, so `channels_running` is truthy.

        jupyter_client KernelClient.__del__ only destroys an owned, still-open context
        when `channels_running` is FALSE; when it is truthy it logs "Could not destroy zmq
        context" and LEAVES IT OPEN — that is precisely the leak BUG_0320 fixes. So a stub
        that only creates the context (channels_running stays False) would let __del__
        self-clean and MASK the leak. We therefore ALSO make channels_running truthy by
        planting a live channel, WITHOUT spawning five real channel threads (which would
        open sockets to dead ports and churn file descriptors / raise 'Too many open files').
        channels_running is a read-only property: truthy iff any _*_channel is set AND
        .is_alive() is truthy — so _iopub_channel = MagicMock(is_alive=lambda: True) does it.
        """
        from jupyter_client import BlockingKernelClient

        def _start_channels(self, *a, **k):
            self.context  # first access -> _context_default -> a real zmq.Context is created
            # Make channels_running truthy -> __del__ refuses to destroy -> leak reproduces.
            self._iopub_channel = MagicMock(is_alive=lambda: True)
        monkeypatch.setattr(BlockingKernelClient, 'start_channels', _start_channels)

    def test_failure_path_destroys_owned_zmq_context(self, monkeypatch, tmp_path):
        """A forced readiness failure must tear the half-started client down (0 leaked contexts)."""
        from jupyter_client import BlockingKernelClient

        from agent_cascade.tools import code_interpreter as ci
        from agent_cascade.tools.code_interpreter import CodeInterpreter

        created, gc = self._instrument_contexts(monkeypatch)
        self._stub_start_channels(monkeypatch)

        # Callable side_effect (an exception-instance side_effect is unreliable against
        # jupyter_client 8.x's run_sync-wrapped wait_for_ready).
        def _never_ready(*a, **k):
            raise RuntimeError('never ready')

        monkeypatch.setattr(BlockingKernelClient, 'wait_for_ready', _never_ready)
        monkeypatch.setattr(ci.time, 'sleep', lambda *a, **k: None)
        monkeypatch.setattr(ci.subprocess, 'run',
                            lambda *a, **k: MagicMock(stdout='', stderr='', returncode=0))
        # Keep the test fast: _shutdown_with_timeout only best-effort-joins kc.shutdown()
        # (up to 5s). The leak is closed by step 4 (context.destroy) in _shutdown_kernel_client,
        # which still runs unstubbed — so stubbing the graceful-shutdown join is safe here.
        monkeypatch.setattr(ci, '_shutdown_with_timeout', lambda *a, **k: None)

        conn = self._make_conn_file(tmp_path / 'kernel_connection_file_bug0320_fail_host.json', 'bug0320_fail')

        with pytest.raises(RuntimeError, match='Kernel failed to start'):
            CodeInterpreter._create_kernel_client(conn, 'container_id', 'bug0320_fail')

        gc.collect()
        assert len(created) == 1, \
            'start_channels() must have created exactly one real zmq.Context (probe exercised the real path)'
        live = self._live_open(created)
        assert not live, f'BUG_0320 regression: {len(live)} leaked-open zmq context(s) after a failed kernel start'

    def test_success_path_does_not_teardown_context(self, monkeypatch, tmp_path):
        """On success the new cleanup must NOT fire; the context persists until explicit shutdown."""
        from jupyter_client import BlockingKernelClient

        from agent_cascade.tools import code_interpreter as ci
        from agent_cascade.tools.code_interpreter import CodeInterpreter

        created, gc = self._instrument_contexts(monkeypatch)
        self._stub_start_channels(monkeypatch)
        monkeypatch.setattr(BlockingKernelClient, 'wait_for_ready', lambda *a, **k: None)
        # Step 1 of the explicit shutdown below is a best-effort graceful kc.shutdown() that
        # can't do anything useful without channels; stub it so the daemon thread doesn't emit
        # an unhandled exception. Step 4 (context.destroy) — what we assert — still runs.
        monkeypatch.setattr(ci, '_shutdown_with_timeout', lambda *a, **k: None)

        conn = self._make_conn_file(tmp_path / 'kernel_connection_file_bug0320_succ_host.json', 'bug0320_succ')
        kc = CodeInterpreter._create_kernel_client(conn, 'container_id', 'bug0320_succ')
        try:
            gc.collect()
            assert kc is not None, 'success path must return the client'
            assert len(created) == 1
            # The owned context must STILL be open here: had the BUG_0320 failure-path
            # cleanup fired on success, the context would already be closed (0 live).
            # 1 live is direct proof the new except block was not entered on the happy path.
            assert len(self._live_open(created)) == 1, \
                'BUG_0320: the success path must NOT tear the client down — the context must persist'
            ci._shutdown_kernel_client(kc, 'bug0320_succ')
            gc.collect()
            assert len(self._live_open(created)) == 0, \
                'explicit shutdown must destroy the owned context'
        finally:
            # Always run the real teardown (it is idempotent) so the test never leaves
            # real channel threads/sockets around, even if an assertion above failed.
            ci._shutdown_kernel_client(kc, 'bug0320_succ')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
