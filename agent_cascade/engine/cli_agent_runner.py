"""One-shot runner for external CLI coding agents (``call_cli_agent``).

Harness-AGNOSTIC. Given a resolved harness + task, this spawns the external CLI exactly
once as a synchronous, one-shot process **inside a Docker container**, blocks, captures
stdout/stderr + exit code, persists a raw log for ``read_logs``, and returns a
:class:`CliAgentResult`. It owns: the concurrency cap, credential stripping, the hard
wall-clock timeout, and container/process-tree teardown. It has NO slot/KV awareness and
does NOT wrap output in trust delimiters — the universal wrap site
(``engine/tool_execution.py`` ``_UNTRUSTED_TOOLS``) is the single source of truth.

Containment reuses the ``code_interpreter`` ``docker run`` recipe as a template (same
caps / limits / path-normalization / scoped mounts) but launches the harness as a
one-shot ``docker run --rm ... <image> <harness-argv>`` and captures stdout+exit via
``subprocess.run(timeout=...)`` — NOT via the per-kernel-id Jupyter kernel client.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent_cascade.engine.cli_agent_adapters import (
    HarnessNotFoundError, RuntimeAdapter, get_adapter)
from agent_cascade.log import logger

# Repo-root logs/ — same directory the agent JSONL logs use, so read_logs finds the file.
_LOGS_DIR = Path(__file__).resolve().parents[2] / 'logs'

# Concurrency cap: a module-level semaphore is sized from pool.settings.max_cli_agent_children.
# Re-sizing a live Semaphore is impossible, so we lazily build it under _SEM_LOCK and rebuild
# only when the configured cap actually changes (rare: settings are read once at startup).
# _SEM_LOCK guards (semaphore, size) so two threads cannot create two semaphores and silently
# defeat serialization. Holding the cap is ONLY steps 3–10 (never the approval/advisor gates,
# which run before acquisition in the handler); the `with` below guarantees release on every
# exit path — success, timeout, preflight failure, or spawn error.
_SEM_LOCK = threading.Lock()
_CLI_SEMAPHORE: Optional[threading.Semaphore] = None
_CLI_SEMAPHORE_SIZE: Optional[int] = None


@dataclass(frozen=True)
class CliAgentResult:
    """Outcome of one one-shot external-CLI run (returned unwrapped; the wrap site trusts it)."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    log_path: str
    requested_posture: str  # posture the caller requested (adapters may resolve to different argv)


@dataclass
class PreparedCliAgentRun:
    """Post-gate spawn body for one ``call_cli_agent`` run, shared by the synchronous
    :func:`run_cli_agent` and the async :class:`CliAgentTracker` background path.

    ``prepare_cli_agent_run`` runs the per-harness ENABLE gate, auth-env injection, model
    resolution, and extra-RO-mount logic ONCE; both callers then consume this record so
    that logic is never duplicated. When ``early_result`` is non-None the run was decided
    WITHOUT spawning (today only the harness-disabled exit-126 case) — the caller returns
    it directly and must not read the spawn fields. When it is None, ``docker_cmd`` /
    ``container_name`` / ``clean_env`` are ready to launch and ``cap`` is the concurrency
    cap the caller must hold around the actual ``Popen``/``subprocess.run``.
    """

    early_result: Optional[CliAgentResult]
    docker_cmd: list[str]
    container_name: str
    clean_env: dict
    cap: int
    harness: str
    caller_name: str
    requested_posture: str


def _get_semaphore(cap: int) -> threading.Semaphore:
    """Return the shared concurrency semaphore, sized to ``cap`` (rebuilt only on change)."""
    global _CLI_SEMAPHORE, _CLI_SEMAPHORE_SIZE
    with _SEM_LOCK:
        if _CLI_SEMAPHORE is None or _CLI_SEMAPHORE_SIZE != cap:
            _CLI_SEMAPHORE = threading.Semaphore(cap)
            _CLI_SEMAPHORE_SIZE = cap
        return _CLI_SEMAPHORE


_REPO_ROOT = str(_LOGS_DIR.parent)  # repo root (contains logs/ and Dockerfile.cli_agent)
_DOCKERFILE = _LOGS_DIR.parent / 'Dockerfile.cli_agent'

# Image auto-build memo: a set of tags known present (skip re-inspecting/re-building).
# _BUILD_LOCK serializes concurrent first-miss builds so two threads never `docker build` the
# same tag at once. A tag whose build FAILS is NOT added — the next call retries (fail-open).
_BUILD_LOCK = threading.Lock()
_BUILD_OK: set[str] = set()

# Short-TTL cache so repeated status calls don't hammer `docker image inspect`.
# value is (ts, present: bool | None). None means docker was unavailable (indeterminate).
_QUOTA_TTL_SECONDS: float = 120.0
_IMAGE_PRESENT_CACHE: dict[str, tuple[float, Optional[bool]]] = {}


def _image_present(tag: str) -> Optional[bool]:
    """True if ``docker image inspect <tag>`` succeeds, False if the tag is missing,
    None if docker itself is unavailable/unreachable (indeterminate). Never raises."""
    try:
        r = subprocess.run(['docker', 'image', 'inspect', tag],
                           capture_output=True, text=True, timeout=15,
                           encoding='utf-8', errors='replace')
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001 — docker not installed / timeout → indeterminate
        logger.debug('call_cli_agent image inspect failed for %s: %s', tag, e)
        return None


def _image_present_cached(tag: str) -> Optional[bool]:
    """TTL-cached wrapper around :func:`_image_present` (status calls only)."""
    now = datetime.now(timezone.utc).timestamp()
    cached = _IMAGE_PRESENT_CACHE.get(tag)
    if cached and (now - cached[0]) < _QUOTA_TTL_SECONDS:
        return cached[1]
    present = _image_present(tag)
    _IMAGE_PRESENT_CACHE[tag] = (now, present)
    return present


def _ensure_image(tag: str) -> None:
    """Best-effort auto-build of a missing image once (behind ``_BUILD_LOCK``), memoized.

    A confirmed-present or already-built tag returns immediately. A missing tag triggers a
    single ``docker build``; failure logs a warning and returns (the caller still spawns,
    and the existing spawn-error path surfaces a clean result — never crash the turn)."""
    with _BUILD_LOCK:
        if tag in _BUILD_OK:
            return
        if _image_present(tag) is True:
            _BUILD_OK.add(tag)
            return
        try:
            r = subprocess.run(
                ['docker', 'build', '-t', tag, '-f', str(_DOCKERFILE), _REPO_ROOT],
                capture_output=True, text=True, timeout=1800,
                encoding='utf-8', errors='replace')
            if r.returncode == 0:
                _BUILD_OK.add(tag)
                logger.info('call_cli_agent built image %s', tag)
            else:
                logger.warning('call_cli_agent image build failed for %s (rc=%s): %s',
                               tag, r.returncode, (r.stderr or '')[-500:])
        except Exception as e:  # noqa: BLE001 — fail-open; caller's spawn path handles it
            logger.warning('call_cli_agent image build error for %s: %s', tag, e)


def _cli_agent_image(harness: Optional[str] = None, pool: Optional[Any] = None) -> str:
    """Docker image for the one-shot container (per-harness config → shared default → auto-build).

    Resolution order (escape-hatch precedence preserved byte-for-byte for ops who set it today):
      1. ``M6_CLI_AGENT_DOCKER_IMAGE`` env override.
      2. ``pool.settings.cli_agent_images[harness]`` per-harness override.
      3. shared default ``cli_agent_default_image(harness)`` → ``cli_agent:all``.
    When a harness/pool is supplied, a missing image is auto-built once (see :func:`_ensure_image`).
    With no harness/pool (legacy callers) it falls back to the ``code_interpreter`` content-hashed
    image tag exactly as before, so any existing caller stays byte-safe.
    """
    override = os.getenv('M6_CLI_AGENT_DOCKER_IMAGE')
    if override:
        return override
    if harness is not None and pool is not None:
        from agent_cascade.settings import cli_agent_default_image
        images = getattr(getattr(pool, 'settings', None), 'cli_agent_images', None) or {}
        tag = images.get(harness) or cli_agent_default_image(harness)
        _ensure_image(tag)
        return tag
    from agent_cascade.tools.code_interpreter import _compute_docker_image_tag
    return _compute_docker_image_tag()


def _build_docker_cmd(*, container_name: str, workspace: str, harness_cmd: list[str],
                      image: str, env_vars: Optional[dict[str, str]] = None,
                      extra_ro_mounts: Optional[list[dict]] = None) -> list[str]:
    """Build the one-shot ``docker run --rm`` argv, reusing the code_interpreter containment recipe.

    Same caps/limits/path-normalization as ``code_interpreter`` (imported lazily to keep this
    module light and avoid a heavy import-time dependency), scoped to the target workspace RW.
    NO ``-d`` (no detached Jupyter kernel), NO forwarded ports, NO ``--name`` reuse — this is a
    foreground one-shot; ``--rm`` auto-cleans on exit.

    Auth keys are injected as ``-e <VAR>=<value>`` flags (NEVER a ``secrets.json`` bind-mount)
    so the container sees only the one native key it needs. ``extra_ro_mounts`` are mounted
    ``:ro`` when the pool's ``cli_agent_paths_ro`` toggle is on. With both empty the argv is
    byte-identical to the pre-auto-setup recipe (default OFF = no behavior change).
    """
    # Lazy import: same source of truth for limits + path normalization, no copy-paste drift.
    from agent_cascade.tools.code_interpreter import (
        CONTAINER_CPU_LIMIT, CONTAINER_MEMORY_LIMIT, CONTAINER_PID_LIMIT, _normalize_path_for_docker)
    cmd = [
        'docker', 'run',
        '--rm',
        '--name', container_name,
        '--cap-drop=ALL',
        '--security-opt=no-new-privileges',
        '--memory=' + CONTAINER_MEMORY_LIMIT,
        '--cpus=' + str(CONTAINER_CPU_LIMIT),
        '--pids-limit=' + str(CONTAINER_PID_LIMIT),
        '-v', f'{_normalize_path_for_docker(os.path.realpath(workspace))}:/workspace',
        '-w', '/workspace',
    ]
    for _k, _v in (env_vars or {}).items():
        cmd += ['-e', f'{_k}={_v}']
    for _m in (extra_ro_mounts or []):
        cmd += ['-v', f"{_m['host']}:{_m['container']}:ro"]
    cmd += [image, *harness_cmd]
    return cmd


def _kill_container(container_name: str) -> None:
    """Best-effort ``docker kill`` to tear down the container and its whole process tree.

    ``--rm`` handles removal; kill handles termination. Never raises — teardown must not mask
    the original timeout result.
    """
    try:
        subprocess.run(['docker', 'kill', container_name],
                       capture_output=True, text=True, timeout=15,
                       encoding='utf-8', errors='replace')
    except Exception as e:  # noqa: BLE001 — teardown is best-effort
        logger.debug('call_cli_agent container kill failed for %s: %s', container_name, e)


def _persist_log(*, caller_name: str, harness: str, exit_code: int, timed_out: bool,
                 stdout: str, stderr: str, requested_posture: str) -> str:
    """Write stdout/stderr/exit to ``logs/call_cli_agent_{caller}_{uuid}.jsonl`` for read_logs."""
    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _LOGS_DIR / f'call_cli_agent_{caller_name}_{uuid.uuid4().hex[:12]}.jsonl'
        record = {
            'harness': harness, 'caller': caller_name, 'exit_code': exit_code,
            'timed_out': timed_out, 'requested_posture': requested_posture,
            'ts': datetime.now(timezone.utc).isoformat(),
            'stdout': stdout, 'stderr': stderr,
        }
        log_path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        return str(log_path)
    except Exception as e:  # noqa: BLE001 — log failure must not fail the call
        logger.warning('call_cli_agent log persist failed: %s', e)
        return '<persist-failed>'


def format_cli_result_message(harness: str, result: CliAgentResult) -> str:
    """Render a CliAgentResult into the handler's user-facing result string. Shared by the
    synchronous handler and the async early-completion path so both present an identical shape
    (the universal _UNTRUSTED_TOOLS wrap site still wraps the returned string)."""
    header = (f"call_cli_agent harness={harness} "
              f"exit={result.exit_code} posture={result.requested_posture} "
              f"timed_out={result.timed_out} full_output={result.log_path}")
    return f"{header}\n\n=== STDOUT ===\n{result.stdout}\n\n=== STDERR ===\n{result.stderr}"


def prepare_cli_agent_run(*, adapter: RuntimeAdapter = None, harness: str = None, task: str,
                          workspace: str, permission_posture: str, pool,
                          caller_name: str, model: Optional[str] = None) -> PreparedCliAgentRun:
    """Resolve the adapter, run the per-harness ENABLE/auth/model/RO gates, and build the
    one-shot ``docker run`` argv — WITHOUT spawning.

    Shared by :func:`run_cli_agent` (synchronous) and the async ``CliAgentTracker`` so the ENABLE
    gate, auth-key injection, model resolution, and extra-RO-mount logic are written exactly once.
    Returns a :class:`PreparedCliAgentRun`; when its ``early_result`` is non-None (today only the
    harness-disabled exit-126 case) the caller returns it directly and must not read the spawn
    fields. The concurrency semaphore is deliberately NOT acquired here: the caller holds it only
    around the actual spawn, so a disabled harness never occupies a slot, and in async mode the
    slot is held across the whole background run rather than released the instant launch returns.
    """
    if adapter is None:
        # Defensive direct-runner path: caller supplied only a harness name. The handler path
        # passes an already-preflighted adapter, so the normal path resolves + probes once.
        if harness is None:
            raise HarnessNotFoundError('run_cli_agent requires an adapter or a harness name.')
        adapter = get_adapter(harness)
        if not adapter.preflight():
            raise HarnessNotFoundError(
                f"Harness CLI for '{harness}' not installed (presence probe failed).")
    harness = harness or adapter.harness

    # (A) Per-harness ENABLE gate (D6: every harness defaults OFF until explicitly enabled in
    # pool_settings). When off, return exit 126 "disabled" WITHOUT spawning a container. Placed
    # before any semaphore so a disabled harness never occupies a slot.
    _settings = getattr(pool, 'settings', None)
    _enabled_map = (getattr(_settings, 'cli_agent_enabled', None) or {}) if _settings is not None else {}
    if not _enabled_map.get(harness, False):
        msg = f"harness '{harness}' is disabled (enable it in the Web UI: CLI Agents section)."
        log_path = _persist_log(caller_name=caller_name, harness=harness, exit_code=126,
                                timed_out=False, stdout='', stderr=msg,
                                requested_posture=permission_posture)
        return PreparedCliAgentRun(
            early_result=CliAgentResult(stdout='', stderr=msg, exit_code=126, timed_out=False,
                                        log_path=log_path, requested_posture=permission_posture),
            docker_cmd=[], container_name='', clean_env={}, cap=2,
            harness=harness, caller_name=caller_name, requested_posture=permission_posture)

    # (B) Auth key: inject the single native env var the harness reads, via docker -e (never a
    # secrets.json bind-mount). Skipped for key-less harnesses (adapter.auth_env_var is None).
    _env_vars: dict[str, str] = {}
    _auth_env_var = getattr(adapter, 'auth_env_var', None)
    if _auth_env_var:
        from config.secrets_loader import get_secret
        _key = get_secret(f'cli_{harness}_key')
        if _key:
            _env_vars[_auth_env_var] = _key

    # (C) Model: call-level argument wins, else the per-harness default map. Dict-guarded so a
    # MagicMock settings (test doubles) never leaks an auto-attribute into the adapter argv.
    _model = model
    if not _model:
        _models = getattr(_settings, 'cli_agent_models', None)
        _model = _models.get(harness) if isinstance(_models, dict) else None

    # (D) Inherited extra-paths read-only mounts, gated by cli_agent_paths_ro (default OFF).
    # Only real dirs are mounted; RO only — the external CLI is untrusted. `is True` so a
    # MagicMock settings does not spuriously enter the mount-building path.
    _extra_ro: list[dict] = []
    if getattr(_settings, 'cli_agent_paths_ro', False) is True:
        from agent_cascade.tools.code_interpreter import EXTRA_RO_MOUNT_PREFIX, _normalize_path_for_docker
        _om = getattr(pool, 'operation_manager', None)
        _ro_roots = getattr(_om, 'extra_work_folders_ro', None)
        if isinstance(_ro_roots, (list, tuple)):
            for _i, _p in enumerate(sorted(str(x) for x in _ro_roots)):
                _real = os.path.realpath(_p)
                if os.path.isdir(_real):
                    _extra_ro.append({'host': _normalize_path_for_docker(_real),
                                      'container': f'{EXTRA_RO_MOUNT_PREFIX}{_i}'})

    cap = getattr(pool.settings, 'max_cli_agent_children', 2)
    container_name = f'cli_agent_{uuid.uuid4().hex[:12]}'
    harness_cmd = adapter.build_launch_cmd(task=task, workspace=workspace,
                                           posture=permission_posture, model=_model)
    image = _cli_agent_image(harness, pool)
    docker_cmd = _build_docker_cmd(container_name=container_name, workspace=workspace,
                                   harness_cmd=harness_cmd, image=image,
                                   env_vars=_env_vars, extra_ro_mounts=_extra_ro)
    clean_env = adapter.child_env(os.environ.copy())
    return PreparedCliAgentRun(early_result=None, docker_cmd=docker_cmd,
                               container_name=container_name, clean_env=clean_env, cap=cap,
                               harness=harness, caller_name=caller_name,
                               requested_posture=permission_posture)


def run_cli_agent(*, adapter: RuntimeAdapter = None, harness: str = None, task: str,
                  workspace: str, permission_posture: str, timeout: int, pool,
                  caller_name: str, model: Optional[str] = None) -> CliAgentResult:
    """Spawn ONE external CLI as a one-shot Docker container, block, capture stdout+exit.

    Raises HarnessNotFoundError if ``harness`` is unregistered or its preflight (presence
    probe) fails. Always strips AC credential env vars from the child env before spawn.
    Always kills the container (whole process tree) on wall-clock timeout. The concurrency
    slot is released on every exit path via the ``with`` below.

    Args:
        adapter: already-resolved, already-preflighted RuntimeAdapter — the normal handler path
            passes this so the runner does not re-resolve or re-probe (probe happens exactly once).
        harness: optional registry key. Used only by the defensive direct path (when ``adapter``
            is None) to resolve + preflight, and for log attribution; defaults to ``adapter.harness``.
        task: prompt handed to the CLI as a single positional token (no shell → no injection).
        workspace: already-validated host path to run in (mounted RW at /workspace).
        permission_posture: already-authorized posture ('acceptEdits' in Phase 0).
        timeout: hard wall-clock seconds.
        pool: AgentPool (for settings; also the defensive-path preflight when adapter is None).
        caller_name: calling agent instance name (log attribution).
    """
    prepared = prepare_cli_agent_run(
        adapter=adapter, harness=harness, task=task, workspace=workspace,
        permission_posture=permission_posture, pool=pool, caller_name=caller_name, model=model)
    if prepared.early_result is not None:
        return prepared.early_result

    sem = _get_semaphore(prepared.cap)
    with sem:  # release guaranteed on every path from here to return
        container_name = prepared.container_name
        docker_cmd = prepared.docker_cmd
        clean_env = prepared.clean_env

        timed_out = False
        stdout = stderr = ''
        exit_code = -1
        try:
            proc = subprocess.run(docker_cmd, env=clean_env, capture_output=True, text=True,
                                  timeout=timeout, encoding='utf-8', errors='replace')
            stdout = proc.stdout or ''
            stderr = proc.stderr or ''
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as e:
            timed_out = True
            stdout = (e.stdout or b'').decode('utf-8', 'replace') if isinstance(e.stdout, bytes) else (e.stdout or '')
            stderr = (e.stderr or b'').decode('utf-8', 'replace') if isinstance(e.stderr, bytes) else (e.stderr or '')
            _kill_container(container_name)
            exit_code = 124  # conventional timeout exit code
        except OSError as e:
            # docker binary missing / spawn failure — surface as a clean error result.
            stderr = f'spawn failed: {e}'
            _kill_container(container_name)
            exit_code = 127

        log_path = _persist_log(caller_name=caller_name, harness=harness, exit_code=exit_code,
                                timed_out=timed_out, stdout=stdout, stderr=stderr,
                                requested_posture=permission_posture)
        return CliAgentResult(stdout=stdout, stderr=stderr, exit_code=exit_code,
                              timed_out=timed_out, log_path=log_path,
                              requested_posture=permission_posture)


def cli_agent_status(pool) -> dict[str, dict]:
    """Per-harness readiness snapshot for the Web UI + ``system_info`` render. Never raises.

    Shape: ``{harness: {enabled, key_configured, image_present, quota_available, model}}``.
      * ``key_configured`` is ``None`` for key-less harnesses (``adapter.auth_env_var`` is None,
        e.g. antigravity's cached OAuth) and ``bool`` otherwise.
      * ``image_present`` is a TTL-cached ``docker image inspect`` result (``None`` when docker
        itself is unavailable — indeterminate, not a hard "missing").
      * ``quota_available`` is always ``'unknown'`` in Phase 0; an adapter MAY add a
        ``probe_quota(self) -> str`` ('ok'|'exhausted'|'unknown') and any exception maps to 'unknown'.
    """
    from agent_cascade.engine.cli_agent_adapters import get_adapter, registered_harnesses
    from agent_cascade.settings import cli_agent_default_image
    from config.secrets_loader import get_secret
    out: dict[str, dict] = {}
    _settings = getattr(pool, 'settings', None)
    enabled_map = getattr(_settings, 'cli_agent_enabled', None) or {}
    images_map = getattr(_settings, 'cli_agent_images', None) or {}
    models_map = getattr(_settings, 'cli_agent_models', None) or {}
    for h in registered_harnesses():
        try:
            adapter = get_adapter(h)
            _auth = getattr(adapter, 'auth_env_var', None)
            key_configured = None if _auth is None else bool(get_secret(f'cli_{h}_key'))
            image_present = _image_present_cached(images_map.get(h) or cli_agent_default_image(h))
            _probe = getattr(adapter, 'probe_quota', None)
            if _probe is None:
                quota_available = 'unknown'
            else:
                try:
                    quota_available = _probe()
                except Exception:  # noqa: BLE001 — a failing probe is 'unknown', never fatal
                    quota_available = 'unknown'
            out[h] = {
                'enabled': bool(enabled_map.get(h, False)),
                'key_configured': key_configured,
                'image_present': image_present,
                'quota_available': quota_available,
                'model': models_map.get(h),
            }
        except Exception as e:  # noqa: BLE001 — status must never crash the caller
            logger.debug('cli_agent_status failed for %s: %s', h, e)
            out[h] = {'enabled': False, 'key_configured': None, 'image_present': None,
                      'quota_available': 'unknown', 'model': None}
    return out
