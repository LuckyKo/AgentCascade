"""Supervisor that lets AC spawn/own the Telegram bridge as a child process.

Phase 3 of the AC Telegram bridge integration. The bridge (``python -m
agent_cascade.telegram_bridge``) is a standalone long-polling process; this
supervisor gives AC control over its lifecycle behind the UI toggle
(``PoolSettings.telegram_bridge_enabled``):

- ``start()`` spawns the child with a controlled, non-secret env and logs its
  stdout/stderr to ``<workspace>/logs/telegram_bridge.log``.
- ``stop()`` runs the Windows shutdown sequence: ``terminate()`` -> wait up to
  N seconds -> ``kill()`` if still alive. (On Windows ``terminate()`` is a hard
  kill, not a graceful SIGTERM — see plan §5/E7.)
- A daemon watcher thread polls the child and applies an **exit-code-aware**
  restart policy so a misconfigured bridge can never crash-loop:

    exit 0  -> clean stop (or master switch off)      -> NO restart
    exit 2  -> config problem (missing token / empty  -> PERMANENT: log + surface,
               ALLOWED_USERS)                           NO restart until re-toggled
    exit 3  -> AC client open failed (transient, e.g. -> bounded restart with
               spawned before uvicorn was ready)        exponential backoff + cap
    other   -> crash                                    -> same bounded restart

The bot token is NEVER passed via env or argv: the child reads it itself from
``config/secrets.json`` because its CWD is the repo root (matching the bridge's
existing "secret never a CLI arg / env" convention).

All state is guarded by a lock: the watcher thread and event-loop callers
(config handler, startup/shutdown hooks) both touch it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from agent_cascade.log import logger


# Bridge exit codes (see telegram_bridge/__main__.py::main):
_BRIDGE_EXIT_CLEAN = 0      # clean stop / master switch off -> never restart
_BRIDGE_EXIT_CONFIG = 2     # config problem (missing token / empty ALLOWED_USERS) -> permanent
_BRIDGE_EXIT_AC_OPEN = 3    # AC client open failed (transient) -> bounded restart

# Bounded-restart policy. The backoff is exponential with a ceiling; after
# MAX_RESTART_ATTEMPTS consecutive failed starts (no healthy run in between) the
# supervisor gives up and requires the user to toggle off->on to retry. This is
# what prevents an infinite restart loop when, e.g., the token is missing or the
# AC port is wrong.
DEFAULT_BACKOFF_BASE = 1.0      # seconds; delay for attempt n = base * 2**(n-1)
DEFAULT_BACKOFF_CAP = 30.0      # seconds; ceiling for any single backoff delay
DEFAULT_MAX_RESTART_ATTEMPTS = 5
DEFAULT_HEALTHY_WINDOW_SEC = 60.0   # child alive this long resets the restart debt
DEFAULT_STOP_WAIT_SEC = 5.0         # terminate() -> wait -> kill() window
DEFAULT_WATCH_INTERVAL_SEC = 1.0    # watcher poll cadence


class TelegramBridgeSupervisor:
    """Spawn/own/watch the Telegram bridge child process on behalf of AC.

    Attached to ``agent_pool`` as ``agent_pool.telegram_supervisor`` by
    ``create_app()`` (single source of truth — every launcher gets it). The AC
    base URL can be given explicitly, or left empty and resolved lazily at
    spawn time from ``agent_pool.server_info`` (the ACTUAL bound port, set by
    the launcher right before ``server.run()``) when attached before uvicorn
    binds. Also takes a workspace dir for the log file.

    Base URL resolution precedence (see ``_resolve_base_url``):
      1. ``ac_base_url`` if non-empty (trailing slash stripped) — used as-is.
      2. Otherwise ``agent_pool.server_info`` (host ignored; child is always
         local, so the host is forced to 127.0.0.1 and only the port is taken).
      3. Otherwise the ``AGENT_CASCADE_PORT`` env var.
      4. Otherwise the default ``http://127.0.0.1:8765``.
    """

    def __init__(self,
                 ac_base_url: str = '',
                 agent_pool=None,
                 project_root: Optional[Path] = None,
                 workspace_dir: Optional[str] = None,
                 allowed_users: str = '',
                 target_agent: str = 'Maine',
                 backoff_base: float = DEFAULT_BACKOFF_BASE,
                 backoff_cap: float = DEFAULT_BACKOFF_CAP,
                 max_restart_attempts: int = DEFAULT_MAX_RESTART_ATTEMPTS,
                 healthy_window_sec: float = DEFAULT_HEALTHY_WINDOW_SEC,
                 stop_wait_sec: float = DEFAULT_STOP_WAIT_SEC,
                 watch_interval_sec: float = DEFAULT_WATCH_INTERVAL_SEC):
        self.ac_base_url = (ac_base_url or '').rstrip('/')
        # Lazily-resolved base URL source. When the supervisor is attached from create_app()
        # (before uvicorn binds), ac_base_url is empty and we resolve the ACTUAL bound port
        # from agent_pool.server_info at spawn time (set by the launcher right before server.run()).
        self._agent_pool = agent_pool
        # CWD for the child so ``config.secrets_loader`` resolves to config/secrets.json.
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent.parent
        # Log file lives under <workspace>/logs/telegram_bridge.log (consistent with AC's logs).
        ws = Path(workspace_dir) if workspace_dir else self.project_root / 'AgentWorkspace'
        self._log_dir = ws / 'logs'
        self._log_path = self._log_dir / 'telegram_bridge.log'
        self.allowed_users = allowed_users or ''
        self.target_agent = target_agent or 'Maine'

        # Bounded-restart knobs (exposed so tests can shrink them).
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.max_restart_attempts = int(max_restart_attempts)
        self.healthy_window_sec = float(healthy_window_sec)
        self.stop_wait_sec = float(stop_wait_sec)
        self.watch_interval_sec = float(watch_interval_sec)

        # Runtime state (all accessed under self._lock).
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._enabled = False          # toggle intent (drives restart decisions)
        self._stopping = False         # True while an explicit stop() is in flight
        self._last_exit_code: Optional[int] = None
        self._error: str = ''
        self._restart_attempts = 0     # consecutive failed starts since last healthy run
        self._proc_started_at: Optional[float] = None
        self._watcher: Optional[threading.Thread] = None

    # ── Public API ────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        """True if a live child process is currently managed."""
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        """Snapshot of supervisor state (for status reporting / debugging)."""
        with self._lock:
            return {
                'enabled': self._enabled,
                'running': self.is_running(),
                'last_exit_code': self._last_exit_code,
                'error': self._error,
                'restart_attempts': self._restart_attempts,
            }

    def set_enabled(self, enabled: bool) -> None:
        """Idempotent start/stop entry point driven by the UI toggle.

        Because the UI sends a full settings snapshot on every save, this fires
        on every save — not only on flips. So it is a strict no-op when already
        in the requested state (no double-spawn on repeated "on"; safe no-op on
        "off" when nothing is running).
        """
        enabled = bool(enabled)
        with self._lock:
            if enabled and not self._enabled:
                self._enabled = True
                self._error = ''
                self._restart_attempts = 0   # fresh user intent -> clear any restart debt
                self._stopping = False       # clear any stale stop flag from a prior stop()
                self._spawn()
            elif not enabled and self._enabled:
                self._enabled = False
                self._stopping = True
                self._stop_child()

    def start(self) -> None:
        """Start the bridge (idempotent — no double-spawn if already running).

        If already enabled AND running, this is a clean no-op (it does NOT reset the
        restart-debt counters), so repeated calls from the config handler can't mask
        a backoff in progress. A genuine re-enable after a stop goes through
        set_enabled(), which handles the state transition explicitly.
        """
        with self._lock:
            if self._enabled and self._proc is not None and self._proc.poll() is None:
                return  # already running — no double-spawn, no counter reset
            self._enabled = True
            self._error = ''
            self._restart_attempts = 0
            self._spawn()

    def stop(self) -> None:
        """Stop the bridge (idempotent — safe no-op when nothing is running)."""
        with self._lock:
            self._enabled = False
            self._stopping = True
            self._stop_child()

    # ── Spawn / stop internals (caller holds self._lock) ──────────────────

    # Minimal set of host env vars the child legitimately needs to launch a Python
    # interpreter. Deliberately NOT the full os.environ — inheriting it would leak
    # any secrets present in the parent (AWS creds, DB URLs, API keys) into the
    # bridge process, violating the "pass only non-secret env" requirement.
    _ENV_ESSENTIALS = (
        'PATH', 'PATHEXT',          # locate the interpreter + .py resolution
        'SYSTEMROOT', 'WINDIR',     # Windows runtime
        'PYTHONIOENCODING', 'PYTHONUTF8',  # consistent text I/O
        'TMPDIR', 'TEMP', 'TMP',    # temp dir for subprocess internals
    )

    def _resolve_base_url(self) -> str:
        """Return the base URL to hand the child, preferring an explicit ac_base_url.

        When constructed without one (create_app attach path), resolve from
        agent_pool.server_info (set by the launcher before server.run()); fall back to
        AGENT_CASCADE_PORT env, then default 8765. Mirrors system_info.py resolution.
        """
        if self.ac_base_url:
            return self.ac_base_url
        # The bridge child is ALWAYS local to AC, so the client URL must always use
        # 127.0.0.1 — never the bind host from server_info (a launcher may bind to
        # 0.0.0.0 for LAN access; 0.0.0.0 is a bind address, not a valid connect target).
        si = getattr(self._agent_pool, 'server_info', None) if self._agent_pool else None
        port = 8765
        if isinstance(si, (tuple, list)) and len(si) == 2 and si[0] and si[1]:
            try:
                port = int(si[1])
            except (ValueError, TypeError):
                port = 8765
        else:
            env_port = os.getenv('AGENT_CASCADE_PORT')
            try:
                port = int(env_port) if env_port is not None else 8765
            except (ValueError, TypeError):
                port = 8765
        return f'http://127.0.0.1:{port}'

    def _build_env(self) -> dict:
        """Build a MINIMAL child env: safe runtime essentials + non-secret bridge vars.

        The bot token is deliberately NOT included — the child reads it itself from
        config/secrets.json (CWD = repo root). We do NOT inherit the full os.environ;
        we copy only a small allowlist of harmless runtime variables so no parent
        secrets leak into the bridge process.
        """
        env = {k: v for k, v in os.environ.items() if k in self._ENV_ESSENTIALS}
        # Authoritative bridge vars (stale host values are never carried over because
        # we built `env` from scratch).
        env['TG_BRIDGE_ENABLED'] = 'true'   # MUST be set or the child exits 0 doing nothing
        if self.allowed_users:
            env['ALLOWED_USERS'] = self.allowed_users
        env['AC_BASE_URL'] = self._resolve_base_url()
        if self.target_agent:
            env['TG_TARGET_AGENT'] = self.target_agent
        return env

    def _open_log(self):
        """Open (append) the bridge log file, creating its directory if needed."""
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            return open(self._log_path, 'a', encoding='utf-8')
        except Exception as e:  # pragma: no cover - disk/path edge cases
            logger.warning('[TelegramBridge] Cannot open log %s (%s); using DEVNULL', self._log_path, e)
            return subprocess.DEVNULL

    def _spawn(self) -> None:
        """Spawn the child process. Caller must hold self._lock."""
        if self._proc is not None and self._proc.poll() is None:
            logger.debug('[TelegramBridge] start() no-op: already running (pid=%s)', self._proc.pid)
            return

        log_file = self._open_log()
        try:
            # Log file is a real object -> pass it to Popen. On the DEVNULL fallback
            # path Popen accepts DEVNULL directly for stdout/stderr.
            kwargs = {}
            if log_file is not subprocess.DEVNULL:
                kwargs['stdout'] = log_file
                kwargs['stderr'] = subprocess.STDOUT

            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            self._proc = subprocess.Popen(
                [sys.executable, '-m', 'agent_cascade.telegram_bridge'],
                cwd=str(self.project_root),
                env=self._build_env(),
                creationflags=creationflags,
                **kwargs,
            )
            self._proc_started_at = time.monotonic()
            self._last_exit_code = None
            logger.info('[TelegramBridge] Spawned bridge pid=%s base_url=%s log=%s',
                        self._proc.pid, self._resolve_base_url(), self._log_path)
        except Exception as e:
            self._proc = None
            self._error = f'Failed to spawn Telegram bridge: {e}'
            logger.error('[TelegramBridge] %s', self._error)
        finally:
            if log_file is not subprocess.DEVNULL:
                try:
                    log_file.close()
                except Exception:
                    pass

        # (Re)start the watcher thread if it isn't already running.
        if self._watcher is None or not self._watcher.is_alive():
            self._watcher = threading.Thread(target=self._watch_loop, name='tg-bridge-watcher', daemon=True)
            self._watcher.start()

    def _stop_child(self) -> None:
        """Run the shutdown sequence on the current child. Caller holds self._lock."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            # Nothing alive to stop — clear state and return (idempotent no-op).
            self._proc = None
            return

        pid = proc.pid
        try:
            proc.terminate()   # on Windows this is a hard kill (TerminateProcess)
        except Exception as e:
            logger.warning('[TelegramBridge] terminate(pid=%s) failed: %s', pid, e)
        try:
            proc.wait(timeout=self.stop_wait_sec)
        except subprocess.TimeoutExpired:
            logger.warning('[TelegramBridge] pid=%s still alive after %.1fs; killing', pid, self.stop_wait_sec)
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception as e:
                logger.warning('[TelegramBridge] kill(pid=%s) failed: %s', pid, e)
        # Reap the exit code defensively: after a kill path returncode may be None
        # (process not yet reaped), so prefer poll() and fall back to None.
        try:
            exit_code = proc.poll() if proc.poll() is not None else getattr(proc, 'returncode', None)
        except Exception:
            exit_code = None
        self._last_exit_code = exit_code
        self._proc = None
        logger.info('[TelegramBridge] Stopped bridge pid=%s (exit=%s)', pid, exit_code)

    # ── Watcher / restart policy ──────────────────────────────────────────

    def _watch_loop(self) -> None:
        """Daemon thread: poll the child and apply the exit-code-aware restart policy.

        Handles the case where the child is *already dead* on the first iteration
        (e.g. it crashed between spawn and the watcher's first poll, or a test fake
        reports an immediate exit). In that case we skip the wait() call and go
        straight to the death-handling path.
        """
        while True:
            with self._lock:
                proc = self._proc
                if proc is None:
                    return  # stopped / never started
                already_dead = proc.poll() is not None

            if already_dead:
                # Child died before we could wait on it — process the death directly.
                rc = proc.returncode if proc.returncode is not None else -1
                self._handle_child_exit(proc, rc)
                return

            try:
                rc = proc.wait(timeout=self.watch_interval_sec)
            except subprocess.TimeoutExpired:
                # Still alive — clear restart debt if it has been healthy long enough.
                with self._lock:
                    self._reset_restart_debt_if_healthy()
                continue
            except Exception as e:  # pragma: no cover - unexpected wait errors
                logger.warning('[TelegramBridge] watcher wait error: %s', e)
                continue

            self._handle_child_exit(proc, rc)
            return  # _handle_child_exit either returns (no restart) or respawns+re-loops

    def _handle_child_exit(self, proc, rc) -> None:
        """Apply the exit-code-aware restart policy for a dead child.

        May call ``self._spawn()`` to restart; in that case the caller (_watch_loop)
        must continue its loop. Returns silently when no restart is warranted.
        """
        with self._lock:
            if self._proc is not proc:
                # We were stopped/replaced while waiting — ignore this death.
                return
            self._last_exit_code = rc
            self._proc = None
            logger.info('[TelegramBridge] Bridge exited with code %s', rc)

            if not self._enabled or self._stopping:
                # Explicitly disabled/stopped -> do not restart.
                self._stopping = False
                return

            if rc == _BRIDGE_EXIT_CLEAN:
                logger.info('[TelegramBridge] Clean exit (0); not restarting.')
                return

            if rc == _BRIDGE_EXIT_CONFIG:
                self._error = (
                    'Telegram bridge exited with a config problem (exit 2): missing bot token '
                    "or empty ALLOWED_USERS. Set 'telegram_bot_token' in config/secrets.json and "
                    'ALLOWED_USERS, then re-enable the toggle to retry.'
                )
                logger.error('[TelegramBridge] %s', self._error)
                return

            # rc == 3 (transient AC-open failure) or any other crash code.
            if self._restart_attempts >= self.max_restart_attempts:
                self._error = (
                    f'Telegram bridge failed to start {self._restart_attempts} times in a row '
                    f'(last exit code {rc}); giving up to avoid a restart loop. '
                    'Check AC reachability / logs, then re-enable the toggle to retry.'
                )
                logger.error('[TelegramBridge] %s', self._error)
                return

            self._restart_attempts += 1
            delay = min(self.backoff_cap, self.backoff_base * (2 ** (self._restart_attempts - 1)))
            attempt = self._restart_attempts

        # Sleep OUTSIDE the lock so we never block event-loop callers.
        if delay > 0:
            time.sleep(delay)

        with self._lock:
            if not self._enabled or self._stopping or self.is_running():
                return
            logger.info('[TelegramBridge] Restarting bridge (attempt %d/%d, exit=%s, backoff=%.1fs)',
                        attempt, self.max_restart_attempts, rc, delay)
            # Clear the old watcher reference so _spawn() starts a fresh one.
            # The current watcher thread is about to return anyway.
            self._watcher = None
            self._spawn()

    def _reset_restart_debt_if_healthy(self) -> None:
        """Reset the consecutive-failure counter once the child has been alive long enough.

        Called from the watcher between polls; keeps a long-lived bridge from
        accumulating restart debt across its lifetime. Caller holds self._lock.
        """
        if (self._restart_attempts > 0 and self._proc_started_at is not None
                and (time.monotonic() - self._proc_started_at) >= self.healthy_window_sec):
            self._restart_attempts = 0
