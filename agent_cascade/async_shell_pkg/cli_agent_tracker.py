"""CliAgentTracker — background (async) execution manager for ``call_cli_agent``.

Subclasses :class:`AsyncShellTracker` to reuse its battle-tested lifecycle machinery
(placeholder-swap spawn, drain threads, poll/timeout/kill, completed-run store, atexit
safety net, ``has_active_tasks``/``completion_delivered`` wake guarantees, idle relaunch).
The ONLY behavioral differences from shell are:

  * The process is a one-shot ``docker run --rm ...`` container (argv list, ``shell=False``),
    launched with the runner's cleaned ``clean_env`` — NOT a ``cmd.exe``/bash shell, so none
    of the shell-specific env wrapping, UTF-8 console config, viewer windows, or stdin pipes
    apply.
  * Teardown leads with the proven ``docker kill <container_name>`` (the same path the
    synchronous runner uses), then falls back to killing the docker-client process.
  * Notification is COMPLETION-ONLY (``heartbeat_interval`` is forced to ``-1``), never a
    periodic heartbeat.
  * Every delivery route is trust-wrapped: ``call_cli_agent`` is in
    ``tool_execution._UNTRUSTED_TOOLS`` and its output originates from an untrusted external
    CLI, but async completions are enqueued as raw USER-role messages that never pass through
    the FUNCTION-role wrap site in ``tool_execution``, so the tracker wraps them directly.
    (Control-command ``__status``/``__kill`` results are RETURNED to the dispatcher and wrapped
    there, so the tracker must NOT wrap them — doing so would nest trust delimiters.)
  * The container-concurrency semaphore (cap = ``pool.settings.max_cli_agent_children``) is
    acquired inside the spawn path and held until the container exits, so ``launch()`` returning
    early in async mode does not defeat the cap. It is released exactly once in the
    tracking-thread's finally via the ``_record_completed_run`` override.
"""

import subprocess
import threading
import time
import uuid

from agent_cascade.async_shell_pkg.constants import SPAWN_PUBLISH_TIMEOUT
from agent_cascade.async_shell_pkg.task import _elapsed_for_task
from agent_cascade.async_shell_pkg.tracker import AsyncShellTracker, _PendingSpawn
from agent_cascade.shell_utils import drain_pipe_lines
from agent_cascade.tool_utils import truncate_with_spillover
from agent_cascade.log import logger


class CliAgentTracker(AsyncShellTracker):
    """Background runner for external CLI one-shot containers (see module docstring)."""

    def __init__(self, pool=None):
        super().__init__(pool=pool)
        # Launch context side map keyed by an opaque run_id (stored as task.command). Holds the
        # prebuilt argv / container name / env / attribution fields that the overrides need but
        # that do not belong on the shared AsyncShellTask dataclass. Written once in launch()
        # (before the tracking thread can read it) and pruned in _record_completed_run.
        self._launch_ctx: dict = {}

    # ────────────────────────────────────────────────────────────────────────
    def _ctx(self, task) -> dict:
        """Return the launch-context dict for a task, or {} if unavailable."""
        if task is None:
            return {}
        return self._launch_ctx.get(task.command, {})

    # ────────────────────────────────────────────────────────────────────────
    def launch(self, *, agent_name: str, docker_cmd, container_name: str, clean_env: dict,
               timeout: int, caller_name: str, harness: str, requested_posture: str,
               cap: int):
        """Register a background container run and briefly poll for early completion.

        Mirrors :meth:`AsyncShellTracker.launch` but with the CLI-specific launch context
        instead of a shell command string, and completion-only notification (heartbeat is
        forced to -1). The per-agent concurrency cap from ``MAX_ASYNC_SHELL_PER_AGENT`` is
        still enforced by the inherited method (and the container cap by the semaphore held in
        :meth:`_spawn_process`).

        Returns:
            ``(tool_id, pid, completed_early, return_code, log_path, early_output)`` —
            ``log_path`` is the persisted-result path (set only when the run finished within the
            launch poll window; otherwise it is persisted later on completion).
        """
        run_id = uuid.uuid4().hex
        ctx = {
            'docker_cmd': docker_cmd, 'container_name': container_name, 'clean_env': clean_env,
            'timeout': timeout, 'caller_name': caller_name, 'harness': harness,
            'requested_posture': requested_posture, 'cap': cap,
        }
        # Publish before super() starts the tracking thread, which reads it in _spawn_process.
        with self._lock:
            self._launch_ctx[run_id] = ctx

        try:
            tool_id, pid, early_output, completed_early, return_code = super().launch(
                agent_name=agent_name, command=run_id,
                heartbeat_interval=-1.0,  # completion-only — the whole point of this tracker
                timeout=timeout, cwd=None, console_window=False,
            )
        except Exception:
            # launch() raised (e.g. per-agent cap reached before any thread started) — do not
            # leak the side-map entry.
            with self._lock:
                self._launch_ctx.pop(run_id, None)
            raise

        log_path = ''
        if completed_early:
            # The container finished inside the launch poll window, so the tracking thread
            # skipped its completion build (and therefore its persist). Persist here so the
            # inline result still has a readable full_output log, same as the sync runner.
            stdout = '\n'.join(l for l in (early_output or []) if l is not None)
            log_path = self._persist(caller_name=caller_name, harness=harness,
                                     exit_code=return_code if return_code is not None else 0,
                                     timed_out=False, stdout=stdout, stderr='',
                                     requested_posture=requested_posture)
        return tool_id, pid, completed_early, return_code, log_path, early_output

    # ────────────────────────────────────────────────────────────────────────
    def _persist(self, *, caller_name: str, harness: str, exit_code: int, timed_out: bool,
                 stdout: str, stderr: str, requested_posture: str) -> str:
        """Delegate to the runner's log writer (lazy import to keep the layering clean)."""
        from agent_cascade.engine.cli_agent_runner import _persist_log
        return _persist_log(caller_name=caller_name, harness=harness, exit_code=exit_code,
                            timed_out=timed_out, stdout=stdout, stderr=stderr,
                            requested_posture=requested_posture)

    # ────────────────────────────────────────────────────────────────────────
    def _wrap(self, body: str) -> str:
        """Trust-wrap a completion/error body (lazy import; no-op when the flag is off)."""
        from agent_cascade.engine.tool_execution import _wrap_untrusted
        return _wrap_untrusted('call_cli_agent', body)

    # ────────────────────────────────────────────────────────────────────────
    def _spawn_process(self, agent_name: str, tool_id: int, command: str, cwd=None):
        """Spawn the one-shot ``docker run`` container (no shell) off the launch thread.

        Acquires the container-concurrency semaphore HERE (on the tracking thread, so a
        saturated cap serializes background runs without stalling ``launch()``), publishes a
        ``_PendingSpawn`` placeholder so launch()'s early check does not spin, swaps the real
        ``Popen`` in under ``task._lock``, then starts the shared pipe-drain threads. The
        semaphore is handed to the task and released in :meth:`_record_completed_run`.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            raise RuntimeError(f"Task {agent_name}/{tool_id} vanished before spawn")
        ctx = self._ctx(task)

        from agent_cascade.engine.cli_agent_runner import _get_semaphore
        sem = _get_semaphore(ctx.get('cap', 2))
        sem.acquire()  # blocking is correct here: tracking thread, off launch()'s critical path

        # Publish placeholder immediately; the real Popen runs off-thread and is swapped in.
        with task._lock:
            task.pid = 0
            task.process = _PendingSpawn()

        spawn_result = {}

        def _do_spawn():
            try:
                p = subprocess.Popen(
                    ctx['docker_cmd'],
                    env=ctx['clean_env'],
                    shell=False,               # argv list — no shell, no injection surface
                    stdin=subprocess.DEVNULL,  # one-shot; no interactive input
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    start_new_session=True,
                )
                spawn_result['proc'] = p
            except Exception as e:  # surfaced below after cleanup
                spawn_result['error'] = e

        spawn_thread = threading.Thread(
            target=_do_spawn, daemon=True, name=f'cli_agent_spawn_{tool_id}')
        spawn_thread.start()

        deadline = time.time() + SPAWN_PUBLISH_TIMEOUT
        while 'proc' not in spawn_result and 'error' not in spawn_result:
            if time.time() >= deadline:
                with task._lock:
                    if isinstance(task.process, _PendingSpawn):
                        task.process = None
                spawn_thread.join(timeout=5)
                orphan = spawn_result.get('proc')
                if orphan is not None:
                    try:
                        self._kill_process_tree(orphan, agent_name, tool_id)
                    except Exception as kill_err:
                        logger.warning(f"[CliAgent] Failed to kill orphaned spawn for "
                                       f"{agent_name} tool_id={tool_id}: {kill_err}")
                sem.release()  # never handed to the task; release now or the slot leaks
                raise RuntimeError(f"Container spawn did not complete within "
                                   f"{SPAWN_PUBLISH_TIMEOUT:.0f}s for {agent_name} tool_id={tool_id}")
            time.sleep(0.05)

        if 'error' in spawn_result:
            with task._lock:
                if isinstance(task.process, _PendingSpawn):
                    task.process = None
            spawn_thread.join(timeout=5)
            orphan = spawn_result.get('proc')
            if orphan is not None:
                try:
                    self._kill_process_tree(orphan, agent_name, tool_id)
                except Exception as kill_err:
                    logger.warning(f"[CliAgent] Failed to kill orphaned spawn for "
                                   f"{agent_name} tool_id={tool_id}: {kill_err}")
            sem.release()  # spawn failed — release now; _record_completed_run sees no _cli_sem
            raise spawn_result['error']

        proc = spawn_result['proc']

        # Swap the real Popen in under lock and hand the semaphore to the task for release
        # exactly once in _record_completed_run (same tracking thread → no lock needed here).
        with task._lock:
            task.pid = proc.pid
            task.process = proc
            task._cli_sem = sem

        logger.debug(f"[CliAgent] Launched tool_id={tool_id} for {agent_name}, PID={proc.pid}")

        # Shared pipe-drain threads (identical machinery to the shell tracker).
        stdout_lock = threading.Lock()
        stderr_lock = threading.Lock()
        t_out = threading.Thread(target=drain_pipe_lines,
                                 args=(proc.stdout, task.stdout_lines, stdout_lock),
                                 daemon=True, name=f'cli_agent_stdout_{tool_id}')
        t_err = threading.Thread(target=drain_pipe_lines,
                                 args=(proc.stderr, task.stderr_lines, stderr_lock),
                                 daemon=True, name=f'cli_agent_stderr_{tool_id}')
        t_out.start()
        t_err.start()
        with task._lock:
            task._drain_t_out = t_out
            task._drain_t_err = t_err
            task._stdout_lock = stdout_lock
            task._stderr_lock = stderr_lock

        return task

    # ────────────────────────────────────────────────────────────────────────
    def _kill_process_tree(self, proc, agent_name: str, tool_id: int) -> None:
        """Tear down the container. Leads with the proven ``docker kill`` then kills the client.

        ``docker kill`` terminates the whole container process tree from the daemon side (the
        authoritative teardown, same as the sync runner); killing the local docker-client only
        severs the client. Both are best-effort — teardown must not mask the real result.
        """
        ctx = self._ctx(self._get_task(agent_name, tool_id))
        container_name = ctx.get('container_name')
        if container_name:
            from agent_cascade.engine.cli_agent_runner import _kill_container
            _kill_container(container_name)  # never raises
        if proc is not None and not isinstance(proc, _PendingSpawn) and proc.poll() is None:
            try:
                proc.kill()
            except Exception as e:  # noqa: BLE001 — best-effort backstop
                logger.debug(f"[CliAgent] client-process kill failed for {agent_name} "
                             f"tool_id={tool_id}: {e}")

    # ────────────────────────────────────────────────────────────────────────
    def _build_completion_header(self, tool_id: int, task, timed_out: bool = False,
                                 error: str = None) -> str:
        """CLI-labeled completion header. Timeout is reported with the conventional exit 124
        (matching the synchronous runner) regardless of the raw container exit code."""
        elapsed = _elapsed_for_task(task)
        if timed_out and not error:
            timeout_val = task.timeout if task else '?'
            return (f"⟨call_cli_agent completed⟩ Tool ID: {tool_id}\n"
                    f"Timed out after {timeout_val}s ({elapsed:.1f}s total, exit=124). "
                    f"Container terminated.")
        if error:
            return (f"⟨call_cli_agent completed⟩ Tool ID: {tool_id}\n"
                    f"Error: {error} ({elapsed:.1f}s elapsed).")
        rc = task.return_code if task and task.return_code is not None else 0
        if rc == -1:
            status = 'killed externally (via __kill)'
        elif rc == 0:
            status = 'success'
        else:
            status = f"exit code {rc}"
        return (f"⟨call_cli_agent completed⟩ Tool ID: {tool_id}\n"
                f"Completed in {elapsed:.1f} s ({status}).")

    # ────────────────────────────────────────────────────────────────────────
    def _build_completion_message(self, agent_name: str, tool_id: int, task,
                                  timed_out: bool = False) -> str:
        """Merged completion message (header + remaining output + full_output footer), trust-wrapped.

        Full stdout/stderr is persisted for ``read_logs`` before the message body is truncated
        for display. Timeout is flagged on the task so ``_record_completed_run`` normalizes the
        recorded exit code to the conventional 124.
        """
        # Flag timeout for the completed-run normalization, then persist FULL output (raw lines,
        # read before the display truncation consumes/advances the position).
        if task is not None and timed_out:
            task._cli_timed_out = True
        log_path = ''
        if task is not None:
            with task._lock:
                stdout_full = '\n'.join(task.stdout_lines)
                stderr_full = '\n'.join(task.stderr_lines)
            ctx = self._ctx(task)
            log_path = self._persist(
                caller_name=ctx.get('caller_name', 'unknown'), harness=ctx.get('harness', 'unknown'),
                exit_code=124 if timed_out else (task.return_code if task.return_code is not None else 0),
                timed_out=timed_out, stdout=stdout_full, stderr=stderr_full,
                requested_posture=ctx.get('requested_posture', 'acceptEdits'))

        header = self._build_completion_header(tool_id, task, timed_out=timed_out)
        remaining_text = self._get_remaining_output_text(agent_name, tool_id)
        msg = header if not remaining_text else f"{header}\n\nOutput:\n{remaining_text}"
        msg = f"{msg}\n\nfull_output={log_path}"
        return self._wrap(msg)

    # ────────────────────────────────────────────────────────────────────────
    def _send_completion_message(self, agent_name: str, tool_id: int, timed_out: bool = False,
                                 error: str = None) -> None:
        """Standalone error-route completion (the normal/timeout paths use the merged build in
        _track_task). Trust-wrapped like every other async delivery; persists the error so a
        later ``read_logs`` still shows why the run failed."""
        task = self._get_task(agent_name, tool_id)
        header = self._build_completion_header(tool_id, task, timed_out=timed_out, error=error)
        ctx = self._ctx(task)
        log_path = self._persist(
            caller_name=ctx.get('caller_name', 'unknown'), harness=ctx.get('harness', 'unknown'),
            exit_code=-1, timed_out=timed_out, stdout='', stderr=error or '',
            requested_posture=ctx.get('requested_posture', 'acceptEdits'))
        body = header if not log_path or log_path == '<persist-failed>' else f"{header}\n\nfull_output={log_path}"
        self._deliver_completion(agent_name, self._wrap(body))

    # ────────────────────────────────────────────────────────────────────────
    def _record_completed_run(self, agent_name: str, tool_id: int, task) -> None:
        """Release the container-concurrency semaphore exactly once and prune the launch context.

        The semaphore is held across the whole background run (acquired in ``_spawn_process``).
        This is the single, guaranteed post-exit release point: the inherited ``_track_task``
        calls it from the tracking thread's finally on EVERY exit path (normal, timeout, error,
        killed). Releasing here — not at ``launch()`` return — is what keeps the container cap
        honest in async mode.
        """
        # Normalize a timed-out run's recorded exit code to the conventional 124 BEFORE
        # super() snapshots the completed-run store, so late __status reflects the convention.
        if task is not None and getattr(task, '_cli_timed_out', False):
            with task._lock:
                task.return_code = 124

        super()._record_completed_run(agent_name, tool_id, task)

        if task is not None:
            sem = getattr(task, '_cli_sem', None)
            if sem is not None:
                try:
                    sem.release()
                except ValueError:
                    logger.debug(f"[CliAgent] semaphore over-release guard for {agent_name} "
                                 f"tool_id={tool_id}")
                task._cli_sem = None  # prevents any double-release on reentry
            run_id = getattr(task, 'command', None)
            if run_id is not None:
                with self._lock:
                    self._launch_ctx.pop(run_id, None)

    # ────────────────────────────────────────────────────────────────────────
    def get_status(self, agent_name: str, tool_id: int):
        """Manual status check (CLI-labeled, no heartbeat line, no raw command echo).

        Returned to the dispatcher, so it is trust-wrapped there — NOT here. Falls back to the
        completed-run store when the live task has already been reaped.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            run = self._get_completed_run(agent_name, tool_id)
            if run is not None:
                rc = run.get('return_code')
                elapsed = run.get('elapsed', 0.0) or 0.0
                output = run.get('output')
                msg = (f"⟨call_cli_agent status⟩ Tool ID: {tool_id}\n"
                       f"Process already completed (exit code {rc}, {elapsed:.0f}s).")
                if output:
                    msg += f"\n\nOutput:\n{output}"
                return msg
            return (f"No running call_cli_agent run found for agent '{agent_name}' with tool_id "
                    f"{tool_id} — TERMINAL: this run has completed or crashed and its record was "
                    f"cleaned up. Do NOT poll again; the result is unobtainable from here (any "
                    f"completion message was already queued). Continue without it.")

        with task._lock:
            pid = task.pid
            completed = task.completed
            return_code = task.return_code
            elapsed = time.time() - task.start_time  # inline: already hold the non-reentrant lock
            combined = list(task.stdout_lines) + list(task.stderr_lines)
            consumed_lines = combined[task.last_heartbeat_sent_pos:]
            task.last_heartbeat_sent_pos = len(combined)

        if completed:
            rc = return_code if return_code is not None else '?'
            status_label = f"completed (exit code {rc}, {elapsed:.0f}s)"
        else:
            status_label = f"running ({elapsed:.0f}s elapsed)"

        msg = (f"⟨call_cli_agent status⟩ Tool ID: {tool_id}\n"
               f"Status: {status_label}\n"
               f"PID: {pid}\n")

        if consumed_lines:
            output_text = self._format_output_text(consumed_lines)
            char_limit = self._get_shell_char_limit()
            if char_limit > 0:
                try:
                    base_dir = self._pool.operation_manager.base_dir if self._pool and hasattr(
                        self._pool, 'operation_manager') else None
                    if base_dir:
                        output_text = truncate_with_spillover(
                            output_text, char_limit, instance_name=agent_name,
                            tool_name='call_cli_agent_async', base_dir=base_dir, operation_mode='mid')
                except Exception as e:
                    logger.debug(f"[CliAgent] truncate_with_spillover failed in get_status for "
                                 f"{agent_name}: {e}")
            msg += f"\nOutput ({len(consumed_lines)} lines):\n{output_text}"
        else:
            msg += '\nNo new output since last status check.'
        return msg
