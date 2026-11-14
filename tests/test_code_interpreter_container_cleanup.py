"""
BUG_0321 regression tests — `_start_kernel` must not orphan a Docker container when
kernel readiness fails.

Root cause (live tree @ 8292e5a5): `_start_kernel` runs `docker run -d` and binds the
live container id BEFORE calling `_create_kernel_client`. If the kernel never becomes
ready, that helper raises (after BUG_0320 tore down its zmq context) *before*
`_start_kernel` returns, so `call()`'s tuple-unpack aborts, its local `container_id`
stays None, and the container — never registered in `_DOCKER_CONTAINERS` (that only
happens in `call()` on success) — is invisible to every later teardown path -> orphaned
live container.

The fix wraps the `_create_kernel_client` call in `_start_kernel` with a `try/except`
that stops + force-removes the container (reusing the file's existing startup-failure
`docker stop` + `docker rm -f` idiom) and re-raises the original error.

These tests drive the REAL `_start_kernel` body (from `docker run` through the cleanup)
without Docker: the CodeInterpreter instance is built with `object.__new__` to bypass
`__init__`'s unconditional `_check_docker_availability()` gate, the heavy pre-`docker run`
helpers are stubbed, and `subprocess.run` is a recorder that returns a fake container id
only for the `docker run` argv. No Docker daemon and no `requires_docker` marker are
needed.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

# Ensure project root is on path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

FAKE_CID = 'fake-cid'


def _docker(argv):
    """Return the argv list of a recorded subprocess.run call (defensive copy)."""
    return list(argv)


class TestStartKernelContainerCleanup:
    """BUG_0321: the started container must be torn down on a failed kernel start."""

    def teardown_method(self):
        """Clear shared process-global tracking dicts so tests leave no residue
        (mirrors the setup/teardown convention in test_zmq_cleanup.py)."""
        from agent_cascade.tools.code_interpreter import (_DOCKER_CONTAINERS, _KERNEL_ACTIVITY,
                                                          _KERNEL_CLIENTS, _STALE_CONTAINERS)
        _DOCKER_CONTAINERS.clear()
        _KERNEL_CLIENTS.clear()
        _KERNEL_ACTIVITY.clear()
        _STALE_CONTAINERS.clear()

    @staticmethod
    def _make_ci(monkeypatch, tmp_path, calls):
        """Build a CodeInterpreter with __init__ bypassed and _start_kernel's
        pre-`docker run` dependencies stubbed, so the real method body runs hermetically.

        `calls` is a list that records every subprocess.run argv in order. The recorder
        returns a container id (FAKE_CID) only for the `docker run -d` argv so the test
        can distinguish the new post-failure cleanup from the pre-run `docker rm -f
        <container_name>` hygiene that _start_kernel also performs.
        """
        from agent_cascade.tools import code_interpreter as ci_mod
        from agent_cascade.tools.code_interpreter import CodeInterpreter

        # Clear shared tracking dicts so the assertions are deterministic.
        ci_mod._DOCKER_CONTAINERS.clear()
        ci_mod._KERNEL_CLIENTS.clear()
        ci_mod._KERNEL_ACTIVITY.clear()
        ci_mod._STALE_CONTAINERS.clear()

        # Bypass __init__ (which unconditionally runs _check_docker_availability).
        ci = object.__new__(CodeInterpreter)
        ci.work_dir = str(tmp_path)
        ci.container_work_dir = '/workspace'
        ci.docker_image_name = 'fake_image'

        # Stub the heavy pre-`docker run` helpers _start_kernel calls.
        ci._build_docker_image = lambda: None
        ci._get_free_ports = lambda n: list(range(20000, 20000 + n))
        ci._resolve_extra_folders = lambda: ([], [])
        ci._process_extra_folders = lambda *a, **k: []
        ci._build_path_mapping = lambda *a, **k: {
            'host_to_container': {},
            'container_to_host': {},
        }

        # Module-level side effects we don't want to run for real.
        monkeypatch.setattr(ci_mod.time, 'sleep', lambda *a, **k: None)
        monkeypatch.setattr(ci_mod.shutil, 'copy', lambda *a, **k: None)
        monkeypatch.setattr(ci_mod, 'set_active_mappings', lambda *a, **k: None)

        # subprocess.run recorder: only `docker run -d` yields the container id.
        def _fake_run(argv, *a, **k):
            calls.append(_docker(argv))
            if len(argv) >= 3 and argv[0] == 'docker' and argv[1] == 'run':
                return MagicMock(returncode=0, stdout=FAKE_CID + '\n', stderr='')
            return MagicMock(returncode=0, stdout='', stderr='')

        monkeypatch.setattr(ci_mod.subprocess, 'run', _fake_run)
        return ci

    @staticmethod
    def _docker_cmds_for(calls, cid, *verb):
        """Return recorded docker subcommands (e.g. ['stop']) run against `cid`."""
        return [
            a for a in calls
            if len(a) > len(verb) and a[0] == 'docker' and a[1:1 + len(verb)] == list(verb) and cid in a
        ]

    def test_failed_kernel_start_tears_down_container(self, monkeypatch, tmp_path):
        """A forced _create_kernel_client failure must stop + rm -f the started container
        and re-raise the original error; the container must not be left in the tracking map."""
        kernel_id = 'bug0321_fail'
        calls = []
        ci = self._make_ci(monkeypatch, tmp_path, calls)

        def _boom(*a, **k):
            raise RuntimeError('Kernel failed to start: boom')

        ci._create_kernel_client = _boom

        # The original startup error must propagate unchanged (not masked by cleanup).
        with pytest.raises(RuntimeError, match='Kernel failed to start: boom'):
            ci._start_kernel(kernel_id)

        # The new cleanup fired: docker stop + docker rm -f against the live container id.
        stops = self._docker_cmds_for(calls, FAKE_CID, 'stop')
        rms = self._docker_cmds_for(calls, FAKE_CID, 'rm', '-f')
        assert stops, f'BUG_0321: container {FAKE_CID} was not stopped on a failed kernel start (calls={calls})'
        assert rms, f'BUG_0321: container {FAKE_CID} was not force-removed on a failed kernel start (calls={calls})'

        # Success-only registration invariant intact: the container was never registered.
        from agent_cascade.tools.code_interpreter import _DOCKER_CONTAINERS
        assert kernel_id not in _DOCKER_CONTAINERS, \
            'Failed-start container must not be registered in _DOCKER_CONTAINERS'

    def test_successful_kernel_start_does_not_teardown_container(self, monkeypatch, tmp_path):
        """On success the cleanup must NOT fire (container kept), then normal
        _cleanup_single_kernel removes it — proving the except branch is dead on success
        and the normal teardown path still works."""
        kernel_id = 'bug0321_succ'
        calls = []
        ci = self._make_ci(monkeypatch, tmp_path, calls)

        dummy_kc = MagicMock()
        ci._create_kernel_client = lambda *a, **k: dummy_kc

        kc, cid = ci._start_kernel(kernel_id)

        # Success path returns the client and the live container id.
        assert kc is dummy_kc, 'success path must return the kernel client'
        assert cid == FAKE_CID, f'success path must return the started container id, got {cid!r}'

        # The failure-path cleanup must NOT have fired: no docker stop/rm against FAKE_CID.
        # (The only FAKE_CID-bearing call legitimately made on success is none at all;
        #  the pre-run hygiene uses container_name, not FAKE_CID.)
        stop_rm = [a for a in calls
                   if a[0] == 'docker' and a[1] in ('stop', 'rm') and FAKE_CID in a]
        assert not stop_rm, \
            f'BUG_0321: cleanup must not fire on a successful start, but saw {stop_rm}'

        # Activity tracking was set (success-only watchdog registration unchanged).
        from agent_cascade.tools.code_interpreter import _DOCKER_CONTAINERS, _KERNEL_ACTIVITY, _cleanup_single_kernel
        assert kernel_id in _KERNEL_ACTIVITY, 'success path must still register watchdog activity'

        # Normal teardown still removes the container: simulate close()'s path.
        _DOCKER_CONTAINERS[kernel_id] = cid
        calls.clear()
        _cleanup_single_kernel(kernel_id, ci.work_dir)
        assert self._docker_cmds_for(calls, FAKE_CID, 'stop'), \
            'normal _cleanup_single_kernel must stop the container'
        assert kernel_id not in _DOCKER_CONTAINERS, \
            'normal _cleanup_single_kernel must pop the container from tracking'
