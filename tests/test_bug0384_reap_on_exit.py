"""BUG_0384: a blocking shell_cmd must not leave orphans on normal completion.

start_new_session=True makes the shell wrapper its own session/group leader; once it
exits, any grandchild it detached (``sleep &`` on POSIX, ``start /B`` on Windows) keeps
running — on Windows the kernel never reparents, so the grandchild still reports the dead
wrapper as its parent. The completion path now reaps that tree via the existing
tree-killer in dead-root-quiet mode (expect_root_exited=True).

These are REAL-PROCESS tests: they spawn OS process trees and probe them externally, so
they are xdist-hostile and carry @pytest.mark.serial (excluded from the default -n auto
run). Run them single-process with:
    python -m pytest tests/test_bug0384_reap_on_exit.py -m serial -p no:cacheprovider -o addopts="-n 0"
Children are always reaped in a finally block. The async side of the same change is
pinned by the mock-only tests in test_async_shell_kill.py (TestKillProcessTreeExpectRootExited).
"""

import os
import subprocess
import time

import pytest

from agent_cascade.operation_manager import OperationManager
from agent_cascade.operation_manager.shell import ShellMixin


# ────────────────────────────────────────────────────────────────────────────────
# Cross-platform liveness probe (non-destructive)
# ────────────────────────────────────────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    """True if `pid` is a live process. A POSIX zombie (state Z) is treated as DEAD —
    it is no longer running, only awaiting init's reap, which is the expected transient
    after SIGKILL and is not a live orphan."""
    if os.name == 'posix':
        try:
            with open(f'/proc/{pid}/stat') as f:
                # comm may contain spaces; the state field is after the last ')'.
                stat = f.read()
            state = stat.rsplit(')', 1)[1].split()[0]
        except FileNotFoundError:
            return False
        except Exception:
            return False
        return state in {'R', 'S', 'D', 'T'}  # Z/X/gone → not a live orphan
    else:
        try:
            res = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            return False
        out = res.stdout.strip()
        return (not out) or (str(pid) in out and 'no tasks' not in out.lower())


def _wait_for_dead(pid: int, timeout: float = 5.0) -> bool:
    """Poll until `pid` is no longer alive, or timeout. Returns True if it died."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


# ────────────────────────────────────────────────────────────────────────────────
# Real blocking execute_shell_command (the shared sync/auto/safe path)
# ────────────────────────────────────────────────────────────────────────────────
class _NoStopPool:
    """Minimal pool stand-in: never stopped, stable generation, no agent_pool coupling."""
    stopped = False
    _run_generation = 1

    def register_foreground_process(self, *a, **k):
        pass

    def unregister_foreground_process(self, *a, **k):
        pass


def _make_real_om(monkeypatch):
    """OperationManager wired for a REAL subprocess.Popen run.

    The interactive approval gate is short-circuited by forcing the safety check to
    report 'safe' (exactly as the sync-stop tests do), which lets a completed command
    execute without the real approval queue. Crucially, subprocess.Popen and the
    tree-reaper (_terminate_process_tree_windows) are left untouched — reaping on
    completion is the behavior under test.
    """
    om = OperationManager.__new__(OperationManager)
    om.base_dir = os.getcwd()
    om.agent_pool = _NoStopPool()
    monkeypatch.setattr(
        ShellMixin, 'evaluate_shell_command_safety', lambda self, *a, **k: (True, []))
    # Path resolution validates cwd against the (fake) pool's allow-list; pin it to a
    # real, valid dir so the real Popen runs. It is not the behavior under test.
    monkeypatch.setattr(
        'agent_cascade.utils.tool_path_resolver.resolve_tool_path',
        lambda *a, **k: os.getcwd())
    return om


def _read_pid_file(path: str) -> int:
    """Read the captured grandchild PID from a temp file written by the test command."""
    for _ in range(50):  # brief poll: file is written right as the command runs
        try:
            with open(path) as f:
                val = f.read().strip()
            if val.isdigit():
                return int(val)
        except FileNotFoundError:
            pass
        time.sleep(0.1)
    return -1


@pytest.mark.serial
def test_blocking_completion_reaps_backgrounded_orphan(tmp_path, monkeypatch):
    """A blocking command that backgrounds a child must not leave it running when
    the command returns. Captures the grandchild PID and asserts it is reaped."""
    pid_file = str(tmp_path / 'orphan.pid')
    if os.name == 'posix':
        # Wrapper exits immediately; `$!` is the backgrounded sleep's PID, and it
        # stays in the wrapper's process group (pgid == wrapper pid by start_new_session).
        command = f"sleep 300 & echo $! > '{pid_file}'; echo done"
    else:
        # Launch a long-running child detached via `start /B`, so the blocking wrapper
        # returns while the child keeps running. The child is a child of the cmd.exe
        # wrapper (PPID == wrapper PID); Windows never reparents it once the wrapper
        # exits, so the completion sweep (anchored on the wrapper PID) still finds it.
        # The payload lives in a .ps1 to keep quoting trivial. The brief `timeout` gives
        # the child time to start and record its PID while still a live descendant, so
        # the reap runs against an already-spawned process (no create-vs-sweep race).
        script = tmp_path / 'orphan.ps1'
        script.write_text(
            "$PID | Out-File -Encoding ascii '" + pid_file + "'; Start-Sleep -Seconds 300\n")
        command = (
            f'start /B powershell -NoProfile -ExecutionPolicy Bypass -File "{script}"'
            ' & timeout /t 3 /nobreak >nul'
        )

    om = _make_real_om(monkeypatch)
    result = om.execute_shell_command(
        command=command, justification='reap-on-exit test', agent_name='serial', timeout=30)

    child_pid = _read_pid_file(pid_file)
    try:
        if child_pid < 0:
            pytest.skip(f'could not capture backgrounded child PID (result={result[:200]!r})')
        # The command completed (not timed out), so the completion reap has already run.
        assert 'timed out' not in result and 'cancelled' not in result.lower()
        assert _wait_for_dead(child_pid), (
            f'backgrounded child {child_pid} survived blocking completion — orphan leak')
    finally:
        if child_pid > 0:
            try:
                if os.name == 'posix':
                    os.kill(child_pid, 9)
                else:
                    subprocess.run(['taskkill', '/F', '/PID', str(child_pid)],
                                   capture_output=True, timeout=5)
            except Exception:
                pass


@pytest.mark.serial
@pytest.mark.skipif(os.name != 'nt', reason='Windows P2 dead-root taskkill warning only fires on Windows')
def test_blocking_plain_command_emits_no_dead_root_warning(caplog, monkeypatch):
    """Negative control: a plain blocking command that finishes cleanly must NOT trigger
    the pass-1 taskkill WARNING on the (now dead) wrapper root. Guards against log-spam
    regression from the completion reap."""
    import logging

    # The shell mixin logs through the shared agent_cascade.log logger, so capture at
    # the root and filter by message content rather than by logger name.
    caplog.set_level(logging.WARNING)
    om = _make_real_om(monkeypatch)
    result = om.execute_shell_command(
        command='cmd /c "echo done"', justification='reap-on-exit control',
        agent_name='serial', timeout=15)
    assert 'Command completed successfully' in result or 'done' in result

    offending = [r.message for r in caplog.records
                 if r.levelno >= logging.WARNING and 'taskkill' in r.message.lower()]
    assert not offending, \
        f'expected no dead-root taskkill WARNING on clean completion, got: {offending}'
