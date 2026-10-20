"""
Kobold TTS backend for ``media_gen`` — koboldcpp v1.123 HTTP TTS server.

koboldcpp v1.123 serves TTS over HTTP (``POST /v1/audio/speech``); the old
one-shot ``-p`` stdout mode is gone. The rewrite splits into two independent
contracts (see the spec) and they are reported separately:

* **Launch contract** (starting the engine): ``--ttsmodel <m> [--ttswavtokenizer
  <c>] --ttsgpu --port <p>`` — one added flag (``--port``), one removed (``-p``),
  background instead of blocking. A green launch audit says nothing about the
  feature contract.
* **Feature contract** (how audio is obtained): stdout-WAV capture is replaced by
  an HTTP POST to ``/v1/audio/speech``.

The #1 correctness risk is the server lifecycle: v1.123 keeps a GPU-holding
process alive, so ``run()`` owns the whole spawn→ready→POST→teardown lifecycle in a
context manager (see ``_kobold_tts_server``) whose ``finally`` kills+waits the child on
**every** path — including why logs go to a file, not a pipe.

Readiness is a capability flag, not "the port answers": poll
``GET /api/extra/version`` until ``tts == true`` (a bare 200 with ``--nomodel``
reports ``tts:false``). We spawn and kill a private server per call — it is
not a user-managed persistent service.

TTS produces *audio* (one of the three canonical media types), not a 'tts' type.
"""

import os
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import logging

from .base import (
    MediaBackend, MediaRequest, MediaResult, resolve_media_timeout,
    _project_root, resolve_tts_config_json, is_wav_bytes,
)
from .manifest import BackendManifest, FieldSpec

from agent_cascade.utils.tool_path_resolver import resolve_tool_path

logger = logging.getLogger(__name__)

# ── Server-lifecycle constants (spec §1/§2/§6) ───────────────────────────────
DEFAULT_READY_TIMEOUT = 120          # seconds; covers OuteTTS GPU cold-start (~90 s)
# Default allocation window. Sits between koboldcpp's own default (5001) and the
# llama-autoloader model band (9001+), so it is clear of every in-house service below.
DEFAULT_PORT_RANGE = (5100, 5199)

# Ports that must never be handed to an ephemeral TTS server, even if momentarily free.
# Defence-in-depth on top of _is_port_free() (which refuses any port actually bound):
# this guards against the probe-then-use race and documents which services we coexist with.
#   5001      koboldcpp default listen port
#   8188      ComfyUI
#   8765      AgentCascade GUI (Gradio)
#   9001..9020 llama-autoloader model instances (base_port 9001, one per loaded model)
#   9124      llama-autoloader control/launcher port
#   12345     AgentCascade API server (default --port)
RESERVED_PORTS = frozenset({5001, 8188, 8765, 9124, 12345}) | set(range(9001, 9021))
TEARDOWN_WAIT = 10                  # bounded wait after kill(); a stuck child must not hang the call
# The re-exec'd GPU-holding child releases its CUDA context slowly AFTER the kill — longer
# than TEARDOWN_WAIT. Poll for actual process-tree exit up to this bound so media_gen does
# not restore VRAM while the GPU is still busy.
TREE_EXIT_TIMEOUT = 25              # total bound for waiting on the whole tree to exit
TREE_POLL_INTERVAL = 0.5            # seconds between "is the tree gone?" polls
# Teardown runs in the finally AFTER generation, outside the generation budget; the holder-DMS
# bound must carry this headroom (see kobold_tts_dms_bound). preflight's generation budget
# deliberately excludes it.
TEARDOWN_HEADROOM = TREE_EXIT_TIMEOUT
READY_POLL_INTERVAL = 2.0           # seconds between readiness probes
READY_POLL_TIMEOUT = 5.0            # per-probe HTTP timeout (a hung connection must not eat the whole budget)


def _resolve_tts_config(config: dict, name: str | None) -> dict:
    """Load a per-voice TTS config JSON (``config_dir/<name>.json``).

    Thin wrapper over the shared :func:`base.resolve_tts_config_json` for the
    ``kobold_tts`` config block; see that function for the resolution rules and
    error contract.
    """
    return resolve_tts_config_json(config, 'kobold_tts', 'TTS', name)


def _is_port_free(port: int) -> bool:
    """Probe-bind on 127.0.0.1; True if the port is free (then close immediately).

    This is a **probe-then-use race**: another process can grab the port between
    the probe and the launch. Accepted (single-user host); the chosen port is
    re-checked by the readiness loop, which fails fast if the bind never happens.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _allocate_port(port_range) -> int:
    """First free port in ``port_range``, skipping RESERVED_PORTS.

    RESERVED_PORTS covers every in-house service we coexist with (koboldcpp default,
    ComfyUI, AgentCascade API/GUI, llama-autoloader control + model band) so an
    ephemeral TTS server never collides with them. Deterministic (iterates ascending)
    so golden tests are stable. Raises if the whole range is occupied.
    """
    lo, hi = port_range
    for p in range(lo, hi + 1):
        if p in RESERVED_PORTS:
            continue
        if _is_port_free(p):
            return p
    raise RuntimeError(
        f"No free port available in kobold_tts range {lo}..{hi}."
    )


def kobold_tts_dms_bound(config, block) -> int:
    """Holder-DMS wall-clock bound for a kobold_tts call (the value media_gen.dms_bound returns).

    = generation timeout + TEARDOWN_HEADROOM. Teardown (the process-tree-exit wait) runs in the
    finally AFTER generation, outside the generation budget; the DMS bound must carry that extra
    headroom or a healthy-but-slow run is killed mid-teardown and VRAM restore is skipped (leak).

    This is deliberately LARGER than preflight's internal generation budget, which stays at the
    bare configured timeout so generation is never silently extended past what the user set.
    """
    return resolve_media_timeout(config, block) + TEARDOWN_HEADROOM


def _wait_until_ready(base_url: str, port: int, ready_timeout: float,
                     log_path: str) -> None:
    """Poll ``GET /api/extra/version`` until ``tts == true``; else raise TimeoutError.

    A bare 200 is NOT readiness: with ``--nomodel`` the endpoint returns 200 with
    ``tts:false`` while the server is up. We require the capability we are about
    to use. Distinguish (both mean "keep waiting", logged differently):
      * connection refused → port not bound yet (model still loading).
      * 200 with ``tts:false`` → server up, TTS model not attached yet.
    Never probe ``/v1/audio/speech`` here — that would issue a real generation.
    """
    deadline = time.monotonic() + ready_timeout
    while True:
        try:
            r = httpx.get(f"{base_url}/api/extra/version", timeout=READY_POLL_TIMEOUT)
            if r.status_code == 200:
                try:
                    payload = r.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and payload.get('tts') is True:
                    return
                logger.debug(
                    'koboldcpp TTS: server up on port %s but tts capability not attached yet',
                    port,
                )
            else:
                logger.debug(
                    'koboldcpp TTS: version endpoint returned HTTP %s on port %s',
                    r.status_code, port,
                )
        except (httpx.ConnectError, httpx.RequestError):
            logger.debug('koboldcpp TTS: port %s not bound yet (connection refused)', port)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"koboldcpp TTS server did not become ready on port {port} "
                f"within {ready_timeout}s (log: {log_path})"
            )
        time.sleep(READY_POLL_INTERVAL)


def _kill_process_tree(pid: int) -> None:
    """Windows: force-kill the whole process tree rooted at ``pid`` (taskkill /T /F).

    koboldcpp re-execs, so the GPU-holding server is a child of our Popen handle; a
    plain proc.kill() only removes the top parent and orphans the child. Bounded so a
    hung taskkill can't stall teardown; failures are swallowed (the final wait below
    still reports a genuinely stuck tree).
    """
    try:
        subprocess.run(
            ['taskkill', '/T', '/F', '/PID', str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=TEARDOWN_WAIT,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _tree_alive(image_name: str) -> bool:
    """Windows: True if ANY process with ``image_name`` is still running.

    Used to wait for the re-exec'd koboldcpp child tree to actually exit: taskkill /T sends
    the kill, but the GPU-holding child releases its CUDA context slowly, so proc.wait() on
    the (instantly-dead) parent returns far too early.

    Fail-safe direction: on a probe ERROR we return True ("assume still alive"). Assuming gone
    on a flaky tasklist would let _wait_tree_gone exit early and media_gen restore VRAM against
    a still-busy GPU — re-introducing the leak this exists to prevent. Returning True only
    costs one extra bounded poll; it can never hang because _wait_tree_gone is deadline-bounded.

    Note: matches by image-name substring, so an unrelated user koboldcpp on another port would
    also match. Acceptable here — this path only WAITS (never kills), so the worst case is
    waiting up to TREE_EXIT_TIMEOUT for someone else's server to exit.
    """
    try:
        out = subprocess.run(
            ['tasklist', '/FI', f"IMAGENAME eq {image_name}", '/FO', 'CSV', '/NH'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=TEARDOWN_WAIT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # can't tell → assume still alive; keep polling (bounded) rather than leak
    return image_name.lower() in (out.stdout or b'').decode('utf-8', 'replace').lower()


def _wait_tree_gone(image_name: str) -> None:
    """Windows: poll until no ``image_name`` process remains, bounded by TREE_EXIT_TIMEOUT.

    Covers koboldcpp's slow CUDA teardown so the caller can safely restore VRAM afterwards.
    If the tree is still alive at the bound we log and leave it — never mask the real error
    with an unbounded hang. The deadline is checked BEFORE each probe so the bound is honest
    (worst case ≈ TREE_EXIT_TIMEOUT + one final probe, not +TEARDOWN_WAIT).
    """
    deadline = time.monotonic() + TREE_EXIT_TIMEOUT
    while True:
        if time.monotonic() >= deadline:
            logger.warning(
                'koboldcpp TTS tree still alive after %ss teardown wait; leaving it '
                '(GPU may stay busy briefly).', TREE_EXIT_TIMEOUT,
            )
            return
        if not _tree_alive(image_name):
            return
        time.sleep(TREE_POLL_INTERVAL)


def _platform() -> str:
    """Current platform name ('nt' on Windows). Indirected so tests can force a branch without
    mutating the global os module (which would race under pytest-xdist)."""
    return os.name


def _terminate(proc) -> None:
    """Kill the server's whole process tree and WAIT for it to actually exit.

    NEVER inspect the exit code (kill() → rc 1 on Windows). Why each step is shaped this way:

      * **Re-exec + kill-ordering race**: koboldcpp spawns itself — Python's ``Popen``
        handle is the TOP parent; the real server (holding GPU + port) is its CHILD. Calling
        ``proc.kill()`` first kills that top parent, so by the time ``taskkill /T`` runs the
        root is already gone and the tree-kill no-ops — orphaning the child. On Windows we
        therefore do NOT call proc.kill() at all: ``taskkill /T /F`` is the SOLE primary kill,
        run while the root is still alive so it walks the whole tree. We spawn with
        CREATE_NEW_PROCESS_GROUP so that tree-kill is well-defined.
      * **Blocking wait**: ``communicate()`` does not block for exit when stdout is a file
        (ours is), so use ``wait()``, which actually blocks until the top process terminates.
      * **Bounded**: every wait is bounded so a stuck child can't hang the call; a genuinely
        stuck tree is logged and left (must not mask the real error).
    """
    is_windows = _platform() == 'nt'
    if is_windows:
        # Windows: tree-kill FIRST (and only) — must run while the root parent is alive.
        _kill_process_tree(proc.pid)
    else:
        # POSIX: proc.kill() signals the group/process; no taskkill equivalent needed.
        try:
            proc.kill()
        except OSError:
            logger.debug('koboldcpp TTS child already terminated, skipping kill')

    # Real blocking wait for the top process to exit (communicate() would be a no-op).
    try:
        proc.wait(timeout=TEARDOWN_WAIT)
    except subprocess.TimeoutExpired:
        logger.warning(
            'koboldcpp TTS child still alive after teardown; leaving it (port may stay busy).'
        )

    # Windows: the re-exec'd GPU-holding child releases its CUDA context slowly even though
    # taskkill /T reached it and the parent is already gone. Wait for the WHOLE tree to
    # actually exit before returning, so media_gen restores VRAM against a free GPU.
    if is_windows:
        _wait_tree_gone('koboldcpp.exe')


@contextmanager
def _kobold_tts_server(argv, log_path, port, ready_timeout):
    """Context manager: spawn → wait-ready → yield base_url; ALWAYS tears down.

    Child stdout/stderr go to a real FILE (``log_path``), never ``subprocess.PIPE`` — PIPE
    fills the ~64 KB OS buffer and the loader blocks, so the server never binds. Teardown
    (kill + bounded wait) runs on success AND every failure/timeout path via the ``finally``,
    and completes before ``media_gen`` restores VRAM. The log file is unlinked in the same
    ``finally`` (one cleanup point, every path).
    """
    base_url = f"http://127.0.0.1:{port}"
    proc = None
    # CREATE_NEW_PROCESS_GROUP puts the server in its own process group so teardown
    # can kill the whole tree (taskkill /T) if koboldcpp spawned worker children.
    creationflags = 0
    if _platform() == 'nt':
        creationflags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    try:
        with open(log_path, 'wb', buffering=0) as lf:
            proc = subprocess.Popen(
                argv, stdout=lf, stderr=subprocess.STDOUT, creationflags=creationflags
            )
            _wait_until_ready(base_url, port, ready_timeout, log_path)
            yield base_url
    finally:
        if proc is not None:
            # Teardown must NEVER mask the real error from the with-body, and a teardown
            # failure must not skip the log unlink. _terminate is defensive internally, but
            # guard it here too so any unexpected raise is logged, not propagated.
            try:
                _terminate(proc)
            except Exception as e:  # noqa: BLE001 - teardown must not mask the original error
                logger.warning('koboldcpp TTS teardown raised (suppressed): %s', e)
        try:
            os.unlink(log_path)
        except OSError:
            pass


class KoboldTTSBackend(MediaBackend):
    id = 'kobold_tts'
    media_types = ('audio',)

    manifest = BackendManifest(
        label='Kobold TTS',
        note='Kobold TTS one-shot GGUF — speech. SLOW (can take minutes).',
        connection_fields=(
            FieldSpec(key='binary', label='Binary', kind='path', required=True,
                      placeholder='C:\\...\\koboldcpp.exe'),
            FieldSpec(key='models_dir', label='Models Directory', kind='path'),
            FieldSpec(key='config_dir', label='Voice Configs', kind='path',
                      placeholder='<AgentCascade_root>/config/media_gen/tts',
                      help='Directory of JSON voice config files. Each .json defines a named '
                           'voice with a preset name (e.g. "af_bella") and generation parameters. '
                           'Create .json files here to add new voices.'),
        ),
        advanced_fields=(
            FieldSpec(key='default_config', label='Default Voice', kind='text',
                      placeholder='kokoro_default'),
            FieldSpec(key='timeout', label='Timeout (s)', kind='number',
                      min=60, max=3600, step=30),
            FieldSpec(key='release_model_around_call',
                      label='Free VRAM around the call', kind='checkbox'),
        ),
        availability='config_dir',
        agent_args=('config',),
        overrides_keys=(),
        usage_hints=(('audio', 'prompt="<text to speak>"'),),
        teardown_headroom=TEARDOWN_HEADROOM,
        defaults={'release_model_around_call': True},
    )

    def describe(self) -> str:
        return 'Kobold TTS (koboldcpp HTTP server)'

    def preflight(self, req: MediaRequest) -> None:
        block = (req.config or {}).get('kobold_tts') or {}

        # ── Resolve the binary to an absolute path and check it exists (I1). ──
        binary = block.get('binary')
        if not binary or not isinstance(binary, str):
            raise RuntimeError(
                'No koboldcpp binary configured. Set kobold_tts.binary in '
                'media_gen.json (e.g. N:/work/stuff/Beta/koboldcpp.exe).'
            )
        binary_path = Path(binary)
        if not binary_path.is_absolute():
            binary_path = _project_root() / binary_path
        if not binary_path.is_file():
            raise RuntimeError(f"koboldcpp binary not found: {binary_path}")

        # ── Resolve the voice config + GGUF model paths (before any VRAM). ──
        tts_cfg = _resolve_tts_config(req.config, req.config_name)
        engine = tts_cfg.get('engine')
        model_name = tts_cfg.get('model')
        if not model_name or not isinstance(model_name, str):
            raise RuntimeError("TTS config is missing a 'model' GGUF path.")
        models_dir = block.get('models_dir') or ''
        model_path = Path(models_dir) / model_name
        if not model_path.is_absolute():
            model_path = _project_root() / model_path
        if not model_path.is_file():
            raise RuntimeError(f"TTS model GGUF not found: {model_path}")

        companion_name = tts_cfg.get('companion_model')
        companion_path = None
        if companion_name and isinstance(companion_name, str):
            companion_path = Path(models_dir) / companion_name
            if not companion_path.is_absolute():
                companion_path = _project_root() / companion_path
            if not companion_path.is_file():
                raise RuntimeError(f"TTS companion GGUF not found: {companion_path}")

        # ── reference_file → voice-cloning reference audio (SECURITY seam). ──
        # Resolved via resolve_tool_path BEFORE any subprocess (audit defect 5):
        # an out-of-roots path raises and the request never reaches VRAM.
        if req.reference_file:
            if not isinstance(req.reference_file, str) or not req.reference_file.strip():
                raise RuntimeError("'reference_file' must be a non-empty string path.")
            try:
                ref_path = resolve_tool_path(req.reference_file.strip(), mode='ro',
                                             agent_pool=req.agent_pool)
            except ValueError as e:
                raise RuntimeError(f"Invalid reference_file path: {e}") from e
            if not ref_path.is_file():
                raise RuntimeError(f"reference_file file not found: {req.reference_file}")
            # --ttsdir (voice cloning) is Qwen3TTS-only; kokoro/outetts never use it.
            # Rejecting here removes a silently-ignored flag (spec §5, decided).
            raise RuntimeError(
                f"'reference_file' (voice cloning) is not supported for engine "
                f"'{engine}'; voice cloning is Qwen3TTS-only."
            )

        # ── Timeout: the GENERATION budget = the configured timeout (never silently extended).
        #    The holder-DMS wall-clock bound is separate and larger — see kobold_tts_dms_bound. ──
        total = resolve_media_timeout(req.config, block)
        ready_timeout = min(block.get('ready_timeout', DEFAULT_READY_TIMEOUT), total)

        # ── Port allocation: deterministic, collision-free (never 5001/8188). ──
        port_range = block.get('port_range') or DEFAULT_PORT_RANGE
        port = _allocate_port(port_range)

        # ── Build argv (pure; never shell=True). Launch contract: no -p, add --port. ──
        argv = [str(binary_path), '--ttsmodel', str(model_path)]
        if companion_path:
            argv += ['--ttswavtokenizer', str(companion_path)]
        argv += ['--ttsgpu']
        argv += ['--port', str(port)]

        # ── Child log file: a real FILE in the system temp dir, never PIPE. ──
        # Created here (path stashed in prepared); unlinked in the context manager's
        # finally (one cleanup point, every path). System temp is swept regardless.
        fd, log_path = tempfile.mkstemp(prefix='kobold_tts-', suffix='.log')
        os.close(fd)

        req.prepared = {
            'argv': argv,
            'log_path': log_path,
            'port': port,
            'port_range': port_range,
            'total': total,
            'ready_timeout': ready_timeout,
            'engine': engine,
            'voice': tts_cfg.get('voice'),
            'config_name': req.config_name or block.get('default_config', ''),
        }

    def run(self, req: MediaRequest) -> MediaResult:
        prepared = req.prepared
        argv = prepared['argv']
        log_path = prepared['log_path']
        port = prepared['port']
        total = prepared['total']
        ready_timeout = prepared['ready_timeout']
        engine = prepared['engine']
        voice = prepared['voice']

        start = time.monotonic()
        # The context manager owns spawn→ready→teardown; teardown runs on every path.
        with _kobold_tts_server(argv, log_path, port, ready_timeout) as base_url:
            # Single budget: the POST gets the remainder, not a fresh full timeout.
            remaining = total - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError(
                    f"koboldcpp TTS generation budget ({total}s) exhausted before the request "
                    f"(log: {log_path})"
                )
            body = {'input': req.prompt, 'model': engine, 'response_format': 'wav'}
            if voice:
                body['voice'] = voice
            try:
                resp = httpx.post(f"{base_url}/v1/audio/speech", json=body,
                                  timeout=remaining)
            except httpx.ConnectError as e:
                raise RuntimeError(
                    f"koboldcpp TTS server not reachable at {base_url}. Is it running? "
                    f"(log: {log_path})"
                ) from e
            except httpx.TimeoutException as e:
                raise RuntimeError(
                    f"koboldcpp TTS request timed out at {base_url} (log: {log_path})"
                ) from e
            if resp.status_code != 200:
                raise RuntimeError(
                    f"koboldcpp TTS failed (HTTP {resp.status_code}): {resp.text[:300]}"
                )
            wav = resp.content
            if not is_wav_bytes(wav):
                raise RuntimeError(
                    'koboldcpp returned no WAV in the response (empty or non-WAV body).'
                )
            return MediaResult(data=wav, ext='wav',
                               meta={'port': port, 'engine': engine, 'voice': voice})
