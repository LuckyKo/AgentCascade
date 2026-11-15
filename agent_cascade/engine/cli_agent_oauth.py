"""Interactive OAuth login sessions for key-less external CLI harnesses (``call_cli_agent``).

One-shot runs are container-isolated, so a harness that authenticates with OAuth (agy) keeps its
account store in a persistent Docker volume (``cli_agent_agy_auth``). *Seeding* that volume needs
a human: the CLI prints an ``accounts.google.com`` URL, waits a fixed short window (**60 s** in
agy), and also accepts an authorization code pasted on stdin. This module drives that handshake
from the Web UI:

    mgr = get_oauth_manager()
    sid, url = mgr.start_session(harness='antigravity', volume='cli_agent_agy_auth',
                                 image='cli_agent_agy')   # URL opened in the host browser
    mgr.submit_code(sid, code_from_callback_page)        # exit 0 → token written to the volume

Design constraints (verified against agy 1.5.8):

* **No localhost callback.** The CLI polls Google's out-of-band callback and reads the code from
  stdin, so the code must be relayed by the UI.
* **The 60 s window is not extendable** (``--print-timeout`` bounds the model turn, not auth). A
  lapsed window is therefore **retryable**, not fatal: the session reports ``expired`` and a new
  one-shot container can be started against the same (still empty) volume.
* **An already-authenticated volume exits 0** without printing a URL — that is a success, not a
  failure (``start_session`` reports no URL and the session settles ``success``).

Nothing here touches AgentCascade's own credentials: the login container mounts only the auth
volume and inherits no AC secrets (no workspace mount, no ``-e`` key).
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from agent_cascade.log import logger
from agent_cascade.prompts.dna import CLI_AGENT_OAUTH_PROBE_PROMPT
from agent_cascade.settings import (
    CLI_AGENT_OAUTH_SESSION_TTL_SECONDS, CLI_AGENT_OAUTH_SUBMIT_WAIT_SECONDS,
    CLI_AGENT_OAUTH_URL_WAIT_SECONDS)

# Google's consent URL, as printed by agy. Trailing punctuation/quotes are excluded so the value
# is directly usable as a link.
_OAUTH_URL_RE = re.compile(r'https://accounts\.google\.com/[^\s\'"<>]+')

# Completed/failed sessions keep their output tail for diagnostics; a bounded deque caps memory.
_OUTPUT_TAIL_LINES = 40

# Login container labelling: the name is also the handle used by ``docker kill`` on teardown.
_LOGIN_CONTAINER_PREFIX = 'cli_agent_login_'

# Session states. ``expired`` is RETRYABLE (agy's own 60 s window lapsed); the UI offers retry.
STATE_STARTING = 'starting'
STATE_WAITING = 'waiting'
STATE_SUCCESS = 'success'
STATE_EXPIRED = 'expired'
STATE_FAILED = 'failed'
STATE_CANCELLED = 'cancelled'
_TERMINAL_STATES = frozenset({STATE_SUCCESS, STATE_EXPIRED, STATE_FAILED, STATE_CANCELLED})

_LOCK = threading.Lock()
_SESSIONS: dict[str, 'OAuthSession'] = {}


@dataclass
class OAuthSession:
    """One interactive login attempt against one auth volume."""

    session_id: str
    harness: str
    volume: str
    container_name: str
    proc: subprocess.Popen
    created_at: float
    auth_url: Optional[str] = None
    state: str = STATE_STARTING
    error: Optional[str] = None
    code_submitted: bool = False
    url_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    tail: deque = field(default_factory=lambda: deque(maxlen=_OUTPUT_TAIL_LINES))

    def tail_text(self) -> str:
        return '\n'.join(self.tail)


class CliAgentOAuthManager:
    """Owns the live login sessions (thin: one container + one reader thread per session)."""

    def __init__(self, *, url_wait: Optional[float] = None, submit_wait: Optional[float] = None,
                 session_ttl: Optional[float] = None, open_browser: bool = True):
        self._url_wait = float(CLI_AGENT_OAUTH_URL_WAIT_SECONDS if url_wait is None else url_wait)
        self._submit_wait = float(
            CLI_AGENT_OAUTH_SUBMIT_WAIT_SECONDS if submit_wait is None else submit_wait)
        self._session_ttl = float(
            CLI_AGENT_OAUTH_SESSION_TTL_SECONDS if session_ttl is None else session_ttl)
        self._open_browser = open_browser

    # ── public API ───────────────────────────────────────────────────────────
    def start_session(self, *, harness: str, volume: str, image: str,
                      target: Optional[str] = None) -> tuple[str, Optional[str]]:
        """Spawn the login container and return ``(session_id, auth_url|None)``.

        Blocks up to ``url_wait`` seconds for the CLI to print its authorization URL. A None URL
        is not necessarily an error: the session may already be ``success`` (the volume held a
        valid sign-in) or terminal ``failed``. Call :meth:`session_state` to disambiguate.
        """
        self._reap()
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        adapter = get_adapter(harness)
        target = target or getattr(adapter, 'auth_volume_target', None)
        if not target:
            raise ValueError(f"harness '{harness}' declares no auth_volume_target")
        harness_cmd = adapter.build_launch_cmd(
            task=CLI_AGENT_OAUTH_PROBE_PROMPT, workspace='/workspace', posture='acceptEdits')
        session_id = uuid.uuid4().hex[:12]
        container_name = f'{_LOGIN_CONTAINER_PREFIX}{session_id}'
        cmd = [
            'docker', 'run', '-i', '--rm',
            '--name', container_name,
            '--cap-drop=ALL',
            '--security-opt=no-new-privileges',
            '-v', f'{volume}:{target}',
            image, *harness_cmd,
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding='utf-8', errors='replace')
        except Exception as e:  # noqa: BLE001 — docker missing / spawn failure → report, not crash
            logger.warning('cli_agent_oauth spawn failed: %s', e)
            raise RuntimeError(f'could not start the login container: {e}') from e

        session = OAuthSession(session_id=session_id, harness=harness, volume=volume,
                               container_name=container_name, proc=proc,
                               created_at=time.time())
        with _LOCK:
            _SESSIONS[session_id] = session
        threading.Thread(target=self._reader, args=(session,),
                         name=f'cli-agent-oauth-{session_id}', daemon=True).start()
        session.url_event.wait(self._url_wait)
        return session_id, session.auth_url

    def submit_code(self, session_id: str, code: str) -> bool:
        """Relay the pasted authorization code. True on a clean token exchange (exit 0)."""
        self._reap()
        session = self._get(session_id)
        if session is None:
            return False
        if session.state in _TERMINAL_STATES:
            return session.state == STATE_SUCCESS
        if not (code or '').strip():
            return False
        session.code_submitted = True
        try:
            session.proc.stdin.write(code.strip() + '\n')
            session.proc.stdin.flush()
        except Exception as e:  # noqa: BLE001 — broken pipe = the 60 s window already closed
            logger.debug('cli_agent_oauth stdin write failed: %s', e)
            with _LOCK:
                if session.state not in _TERMINAL_STATES:
                    session.state = STATE_EXPIRED
                    session.error = 'the login window closed before the code arrived'
            return False
        session.done_event.wait(self._submit_wait)
        if session.state not in _TERMINAL_STATES:
            # Exchange still running after the wait: tear it down rather than orphan a container.
            self._kill(session)
            with _LOCK:
                session.state = STATE_FAILED
                session.error = 'timed out waiting for the token exchange'
                session.done_event.set()
        if session.state == STATE_SUCCESS:
            from agent_cascade.engine.cli_agent_runner import _invalidate_agy_auth_cache
            _invalidate_agy_auth_cache(session.volume)
            return True
        return False

    def cancel_session(self, session_id: str) -> None:
        """Kill the login container (UI Cancel) and mark the session cancelled."""
        session = self._get(session_id)
        if session is None or session.state in _TERMINAL_STATES:
            return
        self._kill(session)
        with _LOCK:
            session.state = STATE_CANCELLED
            session.done_event.set()

    def session_state(self, session_id: str) -> Optional[dict]:
        """Snapshot of one session (None when unknown/expired). Never raises."""
        session = self._get(session_id)
        if session is None:
            return None
        return {
            'session_id': session.session_id,
            'harness': session.harness,
            'state': session.state,
            'auth_url': session.auth_url,
            'error': session.error,
            'retryable': session.state == STATE_EXPIRED,
        }

    # ── internals ────────────────────────────────────────────────────────────
    def _get(self, session_id: str) -> Optional[OAuthSession]:
        with _LOCK:
            return _SESSIONS.get(session_id)

    def _reader(self, session: OAuthSession) -> None:
        """Stream the login container's output: capture the URL, keep a diagnostic tail."""
        try:
            for line in session.proc.stdout or ():
                session.tail.append(line.rstrip())
                if session.auth_url is None:
                    m = _OAUTH_URL_RE.search(line)
                    if m:
                        session.auth_url = m.group(0)
                        with _LOCK:
                            if session.state == STATE_STARTING:
                                session.state = STATE_WAITING
                        session.url_event.set()
                        self._open(session.auth_url)
        except Exception as e:  # noqa: BLE001 — reader must never take the manager down
            logger.debug('cli_agent_oauth reader error: %s', e)
        finally:
            try:
                session.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001 — leave the process to the reaper
                pass
            with _LOCK:
                if session.state not in _TERMINAL_STATES:
                    self._settle(session)
            session.url_event.set()
            session.done_event.set()

    @staticmethod
    def _settle(session: OAuthSession) -> None:
        """Derive the terminal state from the exit code (caller holds ``_LOCK``)."""
        rc = session.proc.returncode
        if session.code_submitted:
            if rc == 0:
                session.state = STATE_SUCCESS
            else:
                session.state = STATE_FAILED
                session.error = session.error or session.tail_text()[-500:] or f'exit {rc}'
        elif rc == 0:
            # No URL was needed: the volume already held a valid sign-in.
            session.state = STATE_SUCCESS
        else:
            # agy's own auth window lapsed (or the CLI errored) before a code arrived: retryable.
            session.state = STATE_EXPIRED
            session.error = session.error or 'the login window expired before a code was submitted'

    def _open(self, url: str) -> None:
        if not self._open_browser:
            return
        try:
            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001 — headless host: the URL is still shown in the UI
            logger.debug('cli_agent_oauth could not open a browser: %s', e)

    @staticmethod
    def _kill(session: OAuthSession) -> None:
        """Best-effort teardown: docker kill the named container, then the local process."""
        try:
            subprocess.run(['docker', 'kill', session.container_name], capture_output=True,
                           text=True, timeout=10, encoding='utf-8', errors='replace')
        except Exception as e:  # noqa: BLE001 — teardown is best-effort
            logger.debug('cli_agent_oauth docker kill failed: %s', e)
        try:
            if session.proc.poll() is None:
                session.proc.kill()
        except Exception as e:  # noqa: BLE001
            logger.debug('cli_agent_oauth process kill failed: %s', e)

    def _reap(self) -> None:
        """Drop sessions past their TTL; kill any container still running (bounded memory)."""
        now = time.time()
        with _LOCK:
            expired = [s for s in _SESSIONS.values()
                       if (now - s.created_at) >= self._session_ttl]
        for session in expired:
            self._kill(session)
            with _LOCK:
                _SESSIONS.pop(session.session_id, None)


_MANAGER: Optional[CliAgentOAuthManager] = None
_MANAGER_LOCK = threading.Lock()


def get_oauth_manager() -> CliAgentOAuthManager:
    """Process-wide singleton login manager."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = CliAgentOAuthManager()
        return _MANAGER


def reset_oauth_manager() -> None:
    """Test hook: drop every session and the singleton (never called by production code)."""
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = None
    with _LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        CliAgentOAuthManager._kill(session)
