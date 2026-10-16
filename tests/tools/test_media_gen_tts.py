"""Tests for the Kobold TTS backend (media_gen increment 2, HTTP-server rewrite).

The OLD tests encoded an outdated one-shot-stdout contract (``-p`` argv, stdout
WAV capture, non-zero exit). Those are stale-contract assertions, NOT production
bugs — they are rewritten to the new server+HTTP contract, not deleted.

Covers the new contract:
  * preflight resolves binary + GGUF model (+ companion) to absolute and checks
    existence BEFORE any VRAM touch (invariant I1 / V1); allocates a port; builds
    the launch-contract argv ``--ttsmodel <m> [--ttswavtokenizer <c>] --ttsgpu
    --port <p>`` (NO ``-p``); stashes a single timeout budget split into
    ready + generation.
  * reference_file is REJECTED for kokoro/outetts (voice cloning is Qwen3TTS-only),
    but the resolve_tool_path security seam still runs first (out-of-roots → reject).
  * run() owns spawn→ready→POST→teardown; readiness requires ``tts == true``;
    audio comes from ``POST /v1/audio/speech``; response validated as WAV.
  * error paths: not-ready (TimeoutError), ConnectError/HTTP non-200/non-WAV
    (RuntimeError), generation timeout (RuntimeError).
  * VRAM slotting: preflight strictly before VRAM; teardown on every path; restore
    on every error path.

All external I/O is mocked (subprocess.Popen + httpx patched). No real binary,
no real GPU, no real server.
"""

import json
import os
import subprocess
import itertools
import httpx
from pathlib import Path
from types import SimpleNamespace

from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.tools.media_gen import (
    MediaGen,
    _invalidate_media_gen_config,
)
from agent_cascade.tools.media_backends import resolve_backend
from agent_cascade.tools.media_backends.base import MediaRequest
import agent_cascade.tools.media_backends.kobold_tts as _kmod
from agent_cascade.tools.media_backends.kobold_tts import (
    KoboldTTSBackend,
    _allocate_port,
)


@pytest.fixture(autouse=True)
def _bust_config_cache():
    _invalidate_media_gen_config()
    yield
    _invalidate_media_gen_config()


@pytest.fixture(autouse=True)
def _no_real_tree_kill():
    """Teardown issues a Windows process-tree kill (taskkill /T /F) and then polls for the
    whole tree to exit (_tree_alive → real tasklist). Patch both module-wide so no test ever
    spawns a real taskkill/tasklist against a mocked pid, and teardown returns instantly.
    Yields a namespace: .kill = _kill_process_tree mock, .alive = _tree_alive mock."""
    with patch(f'{_K}._kill_process_tree') as m_kill, \
         patch(f'{_K}._tree_alive', return_value=False) as m_alive:
        yield SimpleNamespace(kill=m_kill, alive=m_alive)


# ── helpers ──────────────────────────────────────────────────────────────────

def _tts_cfg(**over):
    cfg = {
        'comfyui': {'url': 'http://127.0.0.1:8188', 'media_types': {'image': []}},
        'sdcpp': {'binary': r'C:\bin\sd-cli.exe', 'media_types': {'image': []}},
        'kobold_tts': {
            'binary': r'C:\bin\koboldcpp.exe',
            'models_dir': r'C:\models',
            'config_dir': r'C:\configs\tts',
            'default_config': 'kokoro_default',
            'timeout': 200,
            'ready_timeout': 120,
            'port_range': [5100, 5199],
            'media_types': {'audio': ['kokoro_default']},
        },
    }
    cfg.update(over)
    return cfg


def _req(**over):
    req = MediaRequest(
        media_type='', backend_id='', config_name=None,
        prompt='Hello there, this is a test.', negative_prompt='',
        config=_tts_cfg(), agent_pool=None,
    )
    for k, v in over.items():
        setattr(req, k, v)
    return req


def _wav_bytes():
    """A minimal but structurally-valid WAV (RIFF/WAVE header + a few bytes)."""
    return b'RIFF\x2e\x00\x00\x00WAVEfmt \x10\x00\x00\x00' + b'\x00' * 16


def _prepared(tmp_path, *, port=5100, total=200, ready_timeout=120,
              engine='kokoro', voice='af_bella', argv=None):
    """A prepared dict matching what preflight stashes, for run() unit tests."""
    log_path = str(tmp_path / 'kobold.log')
    if argv is None:
        argv = ['koboldcpp.exe', '--ttsmodel', 'm.gguf', '--ttsgpu', '--port', str(port)]
    return {
        'argv': argv,
        'log_path': log_path,
        'port': port,
        'port_range': [5100, 5199],
        'total': total,
        'ready_timeout': ready_timeout,
        'engine': engine,
        'voice': voice,
    }


def _get_tts_true():
    """httpx.get mock: version endpoint reports the TTS capability ready."""
    g = MagicMock()
    g.status_code = 200
    g.json.return_value = {'tts': True}
    return g


def _get_tts_false():
    """httpx.get mock: server up but TTS capability NOT attached (bare 200)."""
    g = MagicMock()
    g.status_code = 200
    g.json.return_value = {'tts': False}
    return g


def _resp(status, content):
    """httpx.post response mock with a status code and a body."""
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.text = content.decode('utf-8', 'replace') if isinstance(content, bytes) else str(content)
    return r


def _fake_instance():
    inst = MagicMock()
    inst._last_endpoint_config = {
        'state_save_enabled': True,
        'api_base': 'http://localhost:1234',
        'model': 'test-model',
    }
    return inst


_K = 'agent_cascade.tools.media_backends.kobold_tts'

# Capture the REAL implementations at import time (before any fixture mocks them) so low-level
# tests can restore them and exercise the actual command-building / parsing code.
_REAL_KILL_PROCESS_TREE = _kmod._kill_process_tree
_REAL_TREE_ALIVE = _kmod._tree_alive


@pytest.fixture
def tts_files(tmp_path):
    binary = tmp_path / 'koboldcpp.exe'
    binary.write_text('fake binary')
    model = tmp_path / 'kokoro-js.onnx.gguf'
    model.write_text('fake model')
    companion = tmp_path / 'wavtokenizer.gguf'
    companion.write_text('fake companion')
    tts_dir = tmp_path / 'tts'
    tts_dir.mkdir()
    voice = {
        'engine': 'kokoro',
        'model': str(model),
        'companion_model': str(companion),
        'voice': 'af_bella',
    }
    (tts_dir / 'kokoro_default.json').write_text(json.dumps(voice))
    cfg = _tts_cfg()
    cfg['kobold_tts']['binary'] = str(binary)
    cfg['kobold_tts']['models_dir'] = str(tmp_path)
    cfg['kobold_tts']['config_dir'] = str(tts_dir)
    return cfg


# ── preflight: launch contract (argv) ────────────────────────────────────────

class TestTtsPreflight:
    def test_argv_launch_contract_with_companion(self, tts_files):
        req = _req(config_name='kokoro_default', backend_id='kobold_tts')
        req.config = tts_files
        with patch(f'{_K}._is_port_free', return_value=True):
            KoboldTTSBackend().preflight(req)
        argv = req.prepared['argv']
        # Order: binary --ttsmodel <model> --ttswavtokenizer <companion> --ttsgpu --port <p>
        assert argv[0] == tts_files['kobold_tts']['binary']
        assert argv[1] == '--ttsmodel'
        assert argv[3] == '--ttswavtokenizer'
        assert '--ttsgpu' in argv
        assert '--port' in argv
        # The removed one-shot flag must be gone.
        assert '-p' not in argv
        # Single GENERATION budget = the configured timeout (NOT inflated by teardown headroom —
        # that lives only in the separate DMS wall-clock bound). ready=min(120,200)=120.
        assert req.prepared['total'] == 200
        assert req.prepared['ready_timeout'] == 120

    def test_argv_without_companion(self, tts_files):
        # A voice config with no companion_model omits --ttswavtokenizer.
        req = _req(config_name='kokoro_default', backend_id='kobold_tts')
        req.config = tts_files
        model = str(Path(tts_files['kobold_tts']['models_dir']) / 'kokoro-js.onnx.gguf')
        with patch(f'{_K}._is_port_free', return_value=True), \
             patch(f'{_K}._resolve_tts_config',
                   return_value={'engine': 'kokoro', 'model': model}):
            KoboldTTSBackend().preflight(req)
        argv = req.prepared['argv']
        assert '--ttswavtokenizer' not in argv
        assert '--ttsmodel' in argv and '--ttsgpu' in argv
        assert '--port' in argv
        assert '-p' not in argv

    def test_missing_binary_errors(self):
        req = _req(backend_id='kobold_tts')
        req.config = {'kobold_tts': {'models_dir': r'C:\models',
                                     'config_dir': r'C:\c', 'default_config': 'x',
                                     'media_types': {'audio': ['x']}}}
        with pytest.raises(RuntimeError) as e:
            KoboldTTSBackend().preflight(req)
        assert 'No koboldcpp binary configured' in str(e.value)

    def test_missing_model_errors(self, tts_files):
        # Point the model GGUF at a non-existent file.
        cfg = {k: dict(v) for k, v in tts_files.items()}
        cfg['kobold_tts']['models_dir'] = r'C:\empty'
        req = _req(config_name='kokoro_default', backend_id='kobold_tts')
        req.config = cfg
        with patch(f'{_K}._resolve_tts_config', return_value={'model': 'missing.gguf'}):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().preflight(req)
        assert 'TTS model GGUF not found' in str(e.value)

    def test_reference_file_rejected_for_kokoro_outetts(self, tts_files):
        # reference_file is REJECTED for kokoro/outetts (voice cloning is Qwen3TTS-only),
        # but the resolve_tool_path security seam still runs FIRST.
        req = _req(config_name='kokoro_default', backend_id='kobold_tts',
                   reference_file=r'C:\voices\ref.wav')
        req.config = tts_files
        req.agent_pool = MagicMock()
        resolved = MagicMock()
        resolved.is_file.return_value = True
        model = str(Path(tts_files['kobold_tts']['models_dir']) / 'kokoro-js.onnx.gguf')
        with patch(f'{_K}.resolve_tool_path', return_value=resolved) as m_resolve, \
             patch(f'{_K}._resolve_tts_config',
                   return_value={'engine': 'kokoro', 'model': model}):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().preflight(req)
        m_resolve.assert_called_once()
        _, kwargs = m_resolve.call_args
        assert kwargs.get('mode') == 'ro'
        assert 'agent_pool' in kwargs
        assert 'voice cloning is Qwen3TTS-only' in str(e.value)
        assert 'kokoro' in str(e.value)
        # --ttsdir is never emitted for these engines.
        assert '--ttsdir' not in (req.prepared or {}).get('argv', [])

    def test_out_of_roots_reference_file_rejected(self, tts_files):
        req = _req(config_name='kokoro_default', backend_id='kobold_tts',
                   reference_file=r'C:\outside\escape.wav')
        req.config = tts_files
        req.agent_pool = MagicMock()
        model = str(Path(tts_files['kobold_tts']['models_dir']) / 'kokoro-js.onnx.gguf')
        with patch(f'{_K}.resolve_tool_path',
                   side_effect=ValueError('outside allowed roots')), \
             patch(f'{_K}._resolve_tts_config',
                   return_value={'engine': 'kokoro', 'model': model}):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().preflight(req)
        assert 'Invalid reference_file path' in str(e.value)

    def test_port_allocation_avoids_default_and_other_backends(self):
        # Chosen port is deterministic given a mocked "occupied" set, inside the
        # range, and never kobold's default 5001 or ComfyUI's 8188.
        occupied = {5100, 5101, 5102}
        with patch(f'{_K}._is_port_free', side_effect=lambda p: p not in occupied):
            port = _allocate_port([5100, 5199])
        assert port == 5103
        assert port not in {5001, 8188}
        assert 5100 <= port <= 5199
        # Reserved ports are skipped even when they fall inside the range.
        with patch(f'{_K}._is_port_free', side_effect=lambda p: p != 5000):
            got = _allocate_port([5000, 5005])
        assert got == 5002  # 5000 occupied, 5001 reserved-skipped → 5002

    def test_reserved_ports_cover_inhouse_services(self):
        # The ephemeral TTS server must never collide with in-house services.
        # Assert the well-known ports are explicitly reserved (defence-in-depth on
        # top of the live _is_port_free probe), so a future port-range change that
        # overlaps them is caught here rather than at runtime.
        reserved = _kmod.RESERVED_PORTS
        for p in (5001, 8188, 8765, 9124, 12345):
            assert p in reserved, f"port {p} should be reserved"
        # llama-autoloader model-instance band (base_port 9001) is reserved.
        for p in range(9001, 9021):
            assert p in reserved, f"loader port {p} should be reserved"


# ── run(): HTTP POST + WAV validation + lifecycle ────────────────────────────

class TestTtsRun:
    def test_run_posts_and_returns_wav(self, tmp_path, _no_real_tree_kill):
        req = _req(backend_id='kobold_tts')
        req.prepared = _prepared(tmp_path)
        proc = MagicMock()
        with patch(f'{_K}.subprocess.Popen', return_value=proc), \
             patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
             patch(f'{_K}.httpx.post', return_value=_resp(200, _wav_bytes())):
            res = KoboldTTSBackend().run(req)
        assert res.data == _wav_bytes()
        assert res.ext == 'wav'
        assert res.meta['port'] == 5100
        assert res.meta['engine'] == 'kokoro'
        assert res.meta['voice'] == 'af_bella'
        proc.wait.assert_called_once()  # teardown waits for real exit (communicate is a no-op wait w/ stdout→file)
        if os.name == 'nt':
            _no_real_tree_kill.kill.assert_called_once()  # Windows: tree-kill is the primary kill
            proc.kill.assert_not_called()
        else:
            proc.kill.assert_called_once()

    def test_run_rejects_non_wav_response(self, tmp_path):
        req = _req(backend_id='kobold_tts')
        req.prepared = _prepared(tmp_path)
        with patch(f'{_K}.subprocess.Popen', return_value=MagicMock()), \
             patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
             patch(f'{_K}.httpx.post', return_value=_resp(200, b'not a wav')):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().run(req)
        assert 'no WAV in the response' in str(e.value)

    def test_run_rejects_empty_response(self, tmp_path):
        req = _req(backend_id='kobold_tts')
        req.prepared = _prepared(tmp_path)
        with patch(f'{_K}.subprocess.Popen', return_value=MagicMock()), \
             patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
             patch(f'{_K}.httpx.post', return_value=_resp(200, b'')):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().run(req)
        assert 'no WAV in the response' in str(e.value)

    def test_run_rejects_non_200(self, tmp_path):
        req = _req(backend_id='kobold_tts')
        req.prepared = _prepared(tmp_path)
        with patch(f'{_K}.subprocess.Popen', return_value=MagicMock()), \
             patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
             patch(f'{_K}.httpx.post', return_value=_resp(500, b'server error')):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().run(req)
        assert 'HTTP 500' in str(e.value)

    def test_run_not_ready_raises_timeout(self, tmp_path, _no_real_tree_kill):
        # Readiness never true → TimeoutError, AND teardown still runs.
        req = _req(backend_id='kobold_tts')
        req.prepared = _prepared(tmp_path)
        proc = MagicMock()
        with patch(f'{_K}.subprocess.Popen', return_value=proc), \
             patch(f'{_K}.httpx.get', return_value=_get_tts_false()), \
             patch(f'{_K}.time.monotonic', side_effect=itertools.count(0, 1000)):
            with pytest.raises(TimeoutError):
                KoboldTTSBackend().run(req)
        proc.wait.assert_called_once()  # teardown waits for real exit
        if os.name == 'nt':
            _no_real_tree_kill.kill.assert_called_once()
            proc.kill.assert_not_called()
        else:
            proc.kill.assert_called_once()

    def test_run_generation_timeout(self, tmp_path, _no_real_tree_kill):
        # httpx request timeout → RuntimeError (mirrors comfyui httpx style),
        # teardown still runs.
        req = _req(backend_id='kobold_tts')
        req.prepared = _prepared(tmp_path)
        proc = MagicMock()
        with patch(f'{_K}.subprocess.Popen', return_value=proc), \
             patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
             patch(f'{_K}.httpx.post', side_effect=httpx.TimeoutException('too slow')):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().run(req)
        assert 'timed out' in str(e.value)
        proc.wait.assert_called_once()  # teardown waits for real exit
        if os.name == 'nt':
            _no_real_tree_kill.kill.assert_called_once()
            proc.kill.assert_not_called()
        else:
            proc.kill.assert_called_once()

    def test_voice_null_omits_voice_key(self, tmp_path):
        # outetts config (voice: null) → request body has NO voice key.
        req = _req(backend_id='kobold_tts')
        req.prepared = _prepared(tmp_path, engine='outetts', voice=None)
        posted = {}

        def _post(*a, **kw):
            posted.update(kw)
            return _resp(200, _wav_bytes())

        with patch(f'{_K}.subprocess.Popen', return_value=MagicMock()), \
             patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
             patch(f'{_K}.httpx.post', side_effect=_post):
            res = KoboldTTSBackend().run(req)
        assert 'voice' not in posted['json']
        assert res.meta['voice'] is None

    def test_ready_probe_requires_tts_true(self, tmp_path):
        # A bare 200 (tts:false) is NOT readiness: the code keeps polling and
        # NEVER POSTs. Guards the "bare 200 is not ready" trap.
        req = _req(backend_id='kobold_tts')
        req.prepared = _prepared(tmp_path)
        post = MagicMock()
        with patch(f'{_K}.subprocess.Popen', return_value=MagicMock()), \
             patch(f'{_K}.httpx.get', return_value=_get_tts_false()), \
             patch(f'{_K}.httpx.post', return_value=_resp(200, _wav_bytes())) as m_post, \
             patch(f'{_K}.time.monotonic', side_effect=itertools.count(0, 1000)):
            with pytest.raises(TimeoutError):
                KoboldTTSBackend().run(req)
        m_post.assert_not_called()

    def test_total_time_respects_single_budget(self, tmp_path):
        # Readiness burn consumes the budget; the POST timeout is the remainder,
        # NOT a fresh full timeout.
        req = _req(backend_id='kobold_tts')
        req.prepared = _prepared(tmp_path, total=200, ready_timeout=120)
        posted = {}

        def _post(*a, **kw):
            posted.update(kw)
            return _resp(200, _wav_bytes())

        # monotonic call order: start=0, ready-deadline-base=0, post-check=150.
        with patch(f'{_K}.subprocess.Popen', return_value=MagicMock()), \
             patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
             patch(f'{_K}.httpx.post', side_effect=_post), \
             patch(f'{_K}.time.monotonic', side_effect=[0, 0, 150, 150, 150]):
            KoboldTTSBackend().run(req)
        assert posted['timeout'] == 50  # 200 - 150 elapsed, not 200

    def test_child_logs_go_to_file_not_pipe(self, tmp_path):
        # Popen's stdout must be a real file handle, never subprocess.PIPE
        # (PIPE fills the ~64 KB buffer and deadlocks the loader).
        req = _req(backend_id='kobold_tts')
        req.prepared = _prepared(tmp_path)
        with patch(f'{_K}.subprocess.Popen', return_value=MagicMock()) as m_popen, \
             patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
             patch(f'{_K}.httpx.post', return_value=_resp(200, _wav_bytes())):
            KoboldTTSBackend().run(req)
        stdout = m_popen.call_args.kwargs['stdout']
        assert stdout is not subprocess.PIPE
        assert hasattr(stdout, 'name')
        assert Path(stdout.name) == Path(req.prepared['log_path'])

    @pytest.mark.parametrize(
        'case', ['not_ready', 'connect_error', 'http_500', 'non_wav', 'unexpected'])
    def test_teardown_on_every_failure_path(self, tmp_path, case, _no_real_tree_kill):
        # #1 invariant: kill + wait run exactly once on EVERY failure path. On Windows
        # the process-tree kill (taskkill /T /F) must ALSO fire — koboldcpp re-execs,
        # so a plain proc.kill() orphans the GPU-holding child.
        req = _req(backend_id='kobold_tts')
        req.prepared = _prepared(tmp_path)
        proc = MagicMock()
        if case == 'not_ready':
            with patch(f'{_K}.subprocess.Popen', return_value=proc), \
                 patch(f'{_K}.httpx.get', return_value=_get_tts_false()), \
                 patch(f'{_K}.time.monotonic', side_effect=itertools.count(0, 1000)):
                with pytest.raises(TimeoutError):
                    KoboldTTSBackend().run(req)
        elif case == 'connect_error':
            with patch(f'{_K}.subprocess.Popen', return_value=proc), \
                 patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
                 patch(f'{_K}.httpx.post', side_effect=httpx.ConnectError('refused')):
                with pytest.raises(RuntimeError):
                    KoboldTTSBackend().run(req)
        elif case == 'http_500':
            with patch(f'{_K}.subprocess.Popen', return_value=proc), \
                 patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
                 patch(f'{_K}.httpx.post', return_value=_resp(500, b'boom')):
                with pytest.raises(RuntimeError):
                    KoboldTTSBackend().run(req)
        elif case == 'non_wav':
            with patch(f'{_K}.subprocess.Popen', return_value=proc), \
                 patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
                 patch(f'{_K}.httpx.post', return_value=_resp(200, b'nope')):
                with pytest.raises(RuntimeError):
                    KoboldTTSBackend().run(req)
        else:  # unexpected
            with patch(f'{_K}.subprocess.Popen', return_value=proc), \
                 patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
                 patch(f'{_K}.httpx.post', side_effect=ValueError('boom')):
                with pytest.raises(ValueError):
                    KoboldTTSBackend().run(req)
        proc.wait.assert_called_once()  # teardown waits for real exit on every failure path
        if os.name == 'nt':
            # Windows: taskkill /T /F is the SOLE primary kill (proc.kill() would kill
            # the tree root before taskkill can walk it, orphaning the GPU-holding child).
            _no_real_tree_kill.kill.assert_called_once()
            proc.kill.assert_not_called()
        else:
            proc.kill.assert_called_once()


# ── tree-exit wait (Windows slow CUDA teardown) ───────────────────────────────

class TestTreeExitWait:
    def test_wait_tree_gone_polls_until_dead(self):
        # Tree lingers for a few polls, then exits → _wait_tree_gone returns without error.
        alive_seq = [True, True, False]
        with patch(f'{_K}._tree_alive', side_effect=alive_seq) as m, \
             patch(f'{_K}.time.sleep') as m_sleep:
            _kmod._wait_tree_gone('koboldcpp.exe')
        assert m.call_count == 3          # polled alive twice (True), then once (False) to confirm
        m_sleep.assert_called()           # it slept between polls

    def test_wait_tree_gone_bounded_when_stuck(self):
        # Tree NEVER dies → must give up at the bound, not hang. The deadline is checked
        # BEFORE each probe; advance a fake clock past TREE_EXIT_TIMEOUT so it exits via the
        # deadline branch without ever needing to poll.
        with patch(f'{_K}._tree_alive', return_value=True) as m_alive, \
             patch(f'{_K}.time.monotonic', side_effect=itertools.count(0, _kmod.TREE_EXIT_TIMEOUT + 1)), \
             patch(f'{_K}.time.sleep'):
            _kmod._wait_tree_gone('koboldcpp.exe')  # returns (logs warning), does not hang

    def test_wait_tree_gone_noop_when_already_dead(self):
        with patch(f'{_K}._tree_alive', return_value=False) as m, \
             patch(f'{_K}.time.sleep') as m_sleep:
            _kmod._wait_tree_gone('koboldcpp.exe')
        m.assert_called_once()
        m_sleep.assert_not_called()

    def test_tree_alive_assumes_alive_on_probe_error(self, monkeypatch):
        # FAIL-SAFE DIRECTION: a flaky/errored tasklist must NOT be read as "gone" — that would
        # let media_gen restore VRAM against a busy GPU and re-introduce the leak. On
        # OSError/TimeoutExpired, _tree_alive returns True (keep polling). Restore the REAL
        # _tree_alive (autouse fixture mocks it) to exercise its fail-safe.
        monkeypatch.setattr(_kmod, '_tree_alive', _REAL_TREE_ALIVE)
        with patch(f'{_K}.subprocess.run', side_effect=OSError('tasklist blew up')):
            assert _kmod._tree_alive('koboldcpp.exe') is True
        with patch(f'{_K}.subprocess.run', side_effect=subprocess.TimeoutExpired('tasklist', 10)):
            assert _kmod._tree_alive('koboldcpp.exe') is True

    def test_tree_alive_parses_tasklist_output(self, monkeypatch):
        # Alive: CSV row contains the image name. Absent: tasklist prints "No tasks..." only.
        monkeypatch.setattr(_kmod, '_tree_alive', _REAL_TREE_ALIVE)
        with patch(f'{_K}.subprocess.run', return_value=MagicMock(
                stdout=b'"koboldcpp.exe","7508","Console","1","2,000,000 K"\n')):
            assert _kmod._tree_alive('koboldcpp.exe') is True
        with patch(f'{_K}.subprocess.run', return_value=MagicMock(
                stdout=b'INFO: No tasks are running which match the specified criteria.\n')):
            assert _kmod._tree_alive('koboldcpp.exe') is False


# ── Windows teardown: forced + real argv pinned (platform-portable) ───────────

class TestWindowsTeardownForced:
    """Force the Windows branch via the module-local _platform() helper so the Windows teardown
    path is exercised on ANY runner (deterministic under pytest-xdist), and pin the REAL
    taskkill/tasklist argvs so a regression dropping /T or /F can't pass green."""

    def _force_platform(self, name):
        # Patch the module-local _platform() helper (NOT the global os module) so the branch is
        # deterministic on any runner without a cross-worker race under pytest-xdist.
        return patch.object(_kmod, '_platform', return_value=name)

    def test_windows_terminate_does_not_call_proc_kill(self):
        # The ordering-race guard: on Windows taskkill /T /F is the SOLE primary kill;
        # proc.kill() must NOT be called (it would kill the tree root before taskkill walks it).
        proc = MagicMock(pid=4242)
        with self._force_platform('nt'), \
             patch(f'{_K}._kill_process_tree') as m_kill, \
             patch(f'{_K}._wait_tree_gone'):
            _kmod._terminate(proc)
        m_kill.assert_called_once_with(4242)
        proc.kill.assert_not_called()
        proc.wait.assert_called_once()

    def test_kill_process_tree_pins_taskkill_argv(self, monkeypatch):
        # Pin the exact command: taskkill /T /F /PID <pid>. Dropping /T (tree) or /F (force)
        # would silently regress to a partial kill — this catches it. The autouse fixture mocks
        # _kill_process_tree module-wide, so restore the REAL one here to exercise its argv.
        monkeypatch.setattr(_kmod, '_kill_process_tree', _REAL_KILL_PROCESS_TREE)
        with patch(f'{_K}.subprocess.run') as m_run:
            _kmod._kill_process_tree(1234)
        args, kwargs = m_run.call_args
        assert args[0] == ['taskkill', '/T', '/F', '/PID', '1234']
        assert kwargs.get('timeout') is not None  # bounded

    def test_kill_process_tree_swallows_taskkill_error(self, monkeypatch):
        # A failed taskkill must not propagate (teardown must not mask the real error).
        monkeypatch.setattr(_kmod, '_kill_process_tree', _REAL_KILL_PROCESS_TREE)
        with patch(f'{_K}.subprocess.run', side_effect=OSError('no such process')):
            _kmod._kill_process_tree(999)  # must not raise

    def test_posix_terminate_uses_proc_kill_not_taskkill(self):
        # Counterpart: on POSIX the kill is proc.kill(); taskkill must never be invoked.
        proc = MagicMock(pid=1)
        with self._force_platform('posix'), \
             patch(f'{_K}._kill_process_tree') as m_kill, \
             patch(f'{_K}._wait_tree_gone'):
            _kmod._terminate(proc)
        proc.kill.assert_called_once()
        m_kill.assert_not_called()


# ── VRAM slotting (the new backend must obey V1-V9) ──────────────────────────

class TestTtsVRAM:
    def test_order_save_unload_run_restore(self, tts_files):
        tool = MediaGen()
        order = []

        def _save(inst):
            order.append('save'); return True

        def _unload(base):
            order.append('unload'); return True

        def _restore(inst, held_endpoint_cfg=None):
            order.append('restore'); return True

        def _spawn(*args, **kwargs):
            # Marks the moment run() spawns the server (the "run" step).
            order.append('run')
            return MagicMock()

        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=tts_files), \
              patch(f'{_K}._is_port_free', return_value=True), \
              patch(f'{_K}.subprocess.Popen', side_effect=_spawn), \
              patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
              patch(f'{_K}.httpx.post', return_value=_resp(200, _wav_bytes())), \
              patch('agent_cascade.tools.media_gen.save_media_to_media', return_value='/tmp/media/a.wav'), \
              patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
              patch('agent_cascade.state_ops.save_instance_state', side_effect=_save), \
              patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload), \
              patch('agent_cascade.state_ops.restore_instance_state', side_effect=_restore):
            result = tool.call({'prompt': 'hi', 'backend': 'kobold_tts',
                                'config': 'kokoro_default'})

        assert order == ['save', 'unload', 'run', 'restore']
        assert result[0].audio == '/tmp/media/a.wav'
        assert 'backend=kobold_tts' in result[1].text

    def test_preflight_failure_touches_zero_vram(self, tts_files):
        # preflight must NOT start the server: a config failure touches zero VRAM.
        tool = MediaGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=tts_files), \
              patch(f'{_K}._resolve_tts_config', side_effect=RuntimeError('no voice config')), \
              patch('agent_cascade.state_ops.save_instance_state') as m_save, \
              patch('agent_cascade.state_ops.unload_all_models') as m_unload, \
              patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'hi', 'backend': 'kobold_tts',
                                'config': 'kokoro_default'})

        m_save.assert_not_called()
        m_unload.assert_not_called()
        m_restore.assert_not_called()
        assert 'ERROR' in result[0].text

    def test_generation_failure_still_restores(self, tts_files):
        # Failure now comes from the HTTP path; restore must still run.
        tool = MediaGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=tts_files), \
              patch(f'{_K}._is_port_free', return_value=True), \
              patch(f'{_K}.subprocess.Popen', return_value=MagicMock()), \
              patch(f'{_K}.httpx.get', return_value=_get_tts_true()), \
              patch(f'{_K}.httpx.post', return_value=_resp(200, b'not wav')), \
              patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
              patch('agent_cascade.state_ops.save_instance_state', return_value=True), \
              patch('agent_cascade.state_ops.unload_all_models', return_value=True), \
              patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'hi', 'backend': 'kobold_tts',
                                'config': 'kokoro_default'})

        m_restore.assert_called_once()
        assert 'ERROR' in result[0].text
