"""Tests for the Kobold TTS backend (media_gen increment 2, build step 6).

Covers:
  * preflight resolves binary + GGUF model (+ companion) to absolute and checks
    existence BEFORE any VRAM touch (invariant I1 / V1).
  * the CONFIRMED argv golden (plan §6.2): ``binary --ttsmodel <gguf>
    [--ttswavtokenizer <gguf>] --ttsgpu -p "<text>"``.
  * reference_file → voice-cloning via resolve_tool_path(mode='ro') BEFORE
    subprocess (security seam).
  * run() captures the WAV from STDOUT and validates RIFF/WAVE magic.
  * error paths: missing binary/model, non-WAV stdout, non-zero exit.
  * VRAM slotting: preflight strictly before VRAM; restore on every error path.

All external I/O is mocked (subprocess.run patched to return fake WAV bytes).
No real binary is executed.
"""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.tools.media_gen import (
    MediaGen,
    _invalidate_media_gen_config,
)
from agent_cascade.tools.media_backends import resolve_backend
from agent_cascade.tools.media_backends.base import MediaRequest
from agent_cascade.tools.media_backends.kobold_tts import KoboldTTSBackend


@pytest.fixture(autouse=True)
def _bust_config_cache():
    _invalidate_media_gen_config()
    yield
    _invalidate_media_gen_config()


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
            'timeout': 600,
            'media_types': {'audio': ['kokoro_default']},
        },
    }
    cfg.update(over)
    return cfg


def _req(**over):
    req = MediaRequest(
        media_type='', backend_id='AUTO', config_name=None,
        prompt='Hello there, this is a test.', negative_prompt='',
        config=_tts_cfg(), agent_pool=None,
    )
    for k, v in over.items():
        setattr(req, k, v)
    return req


def _wav_bytes():
    """A minimal but structurally-valid WAV (RIFF/WAVE header + a few bytes)."""
    return b'RIFF\x2e\x00\x00\x00WAVEfmt \x10\x00\x00\x00' + b'\x00' * 16


class _TtsFixture:
    """Real tmp files so the preflight is_file() guards pass."""


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
        'engine': 'koboldcpp',
        'model': str(model),
        'companion_model': str(companion),
        'voice': 'af_bella',
        'speed': 1.0,
    }
    (tts_dir / 'kokoro_default.json').write_text(json.dumps(voice))
    cfg = _tts_cfg()
    cfg['kobold_tts']['binary'] = str(binary)
    cfg['kobold_tts']['models_dir'] = str(tmp_path)
    cfg['kobold_tts']['config_dir'] = str(tts_dir)
    return cfg


# ── preflight: argv golden ───────────────────────────────────────────────────

class TestTtsPreflight:
    def test_argv_golden_with_companion(self, tts_files):
        req = _req(config_name='kokoro_default', backend_id='kobold_tts')
        req.config = tts_files
        KoboldTTSBackend().preflight(req)
        argv = req.prepared['argv']
        # Order: binary --ttsmodel <model> --ttswavtokenizer <companion> --ttsgpu -p <text>
        assert argv[0] == tts_files['kobold_tts']['binary']
        assert argv[1] == '--ttsmodel'
        assert argv[3] == '--ttswavtokenizer'
        assert '--ttsgpu' in argv
        assert argv[-2:] == ['-p', 'Hello there, this is a test.']
        assert req.prepared['timeout'] >= 600

    def test_argv_without_companion(self, tts_files):
        # A voice config with no companion_model omits --ttswavtokenizer.
        req = _req(config_name='kokoro_default', backend_id='kobold_tts')
        req.config = tts_files
        with patch('agent_cascade.tools.media_backends.kobold_tts._resolve_tts_config',
                   return_value={'engine': 'koboldcpp', 'model': str(
                       __import__('pathlib').Path(tts_files['kobold_tts']['models_dir'])
                       / 'kokoro-js.onnx.gguf')}):
            KoboldTTSBackend().preflight(req)
        argv = req.prepared['argv']
        assert '--ttswavtokenizer' not in argv
        assert '--ttsmodel' in argv and '--ttsgpu' in argv
        assert argv[-2:] == ['-p', 'Hello there, this is a test.']

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
        with patch('agent_cascade.tools.media_backends.kobold_tts._resolve_tts_config',
                   return_value={'model': 'missing.gguf'}):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().preflight(req)
        assert 'TTS model GGUF not found' in str(e.value)

    def test_reference_file_resolved_ro_before_subprocess(self, tts_files):
        req = _req(config_name='kokoro_default', backend_id='kobold_tts',
                   reference_file=r'C:\voices\ref.wav')
        req.config = tts_files
        req.agent_pool = MagicMock()
        resolved = MagicMock()
        resolved.is_file.return_value = True
        resolved.parent = MagicMock()
        with patch('agent_cascade.tools.media_backends.kobold_tts.resolve_tool_path',
                   return_value=resolved) as m_resolve, \
             patch('agent_cascade.tools.media_backends.kobold_tts._resolve_tts_config',
                   return_value={'model': str(
                       __import__('pathlib').Path(tts_files['kobold_tts']['models_dir'])
                       / 'kokoro-js.onnx.gguf')}):
            KoboldTTSBackend().preflight(req)
        m_resolve.assert_called_once()
        # mode='ro' and agent_pool passed through:
        _, kwargs = m_resolve.call_args
        assert kwargs.get('mode') == 'ro'
        assert 'agent_pool' in kwargs
        # The reference dir was wired as --ttsdir.
        assert '--ttsdir' in req.prepared['argv']

    def test_out_of_roots_reference_file_rejected(self, tts_files):
        req = _req(config_name='kokoro_default', backend_id='kobold_tts',
                   reference_file=r'C:\outside\escape.wav')
        req.config = tts_files
        req.agent_pool = MagicMock()
        with patch('agent_cascade.tools.media_backends.kobold_tts.resolve_tool_path',
                   side_effect=ValueError('outside allowed roots')), \
             patch('agent_cascade.tools.media_backends.kobold_tts._resolve_tts_config',
                   return_value={'model': str(
                       __import__('pathlib').Path(tts_files['kobold_tts']['models_dir'])
                       / 'kokoro-js.onnx.gguf')}):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().preflight(req)
        assert 'Invalid reference_file path' in str(e.value)


# ── run(): stdout WAV + RIFF validation ──────────────────────────────────────

class TestTtsRun:
    def test_run_captures_stdout_wav(self):
        req = _req(backend_id='kobold_tts')
        req.prepared = {'argv': ['koboldcpp.exe', '-p', 'hi'], 'timeout': 600}
        with patch('agent_cascade.tools.media_backends.kobold_tts.subprocess.run',
                   return_value=subprocess.CompletedProcess(
                       args=[], returncode=0, stdout=_wav_bytes(), stderr=b'')):
            res = KoboldTTSBackend().run(req)
        assert res.data == _wav_bytes()
        assert res.ext == 'wav'

    def test_run_rejects_non_wav_stdout(self):
        req = _req(backend_id='kobold_tts')
        req.prepared = {'argv': ['koboldcpp.exe', '-p', 'hi'], 'timeout': 600}
        with patch('agent_cascade.tools.media_backends.kobold_tts.subprocess.run',
                   return_value=subprocess.CompletedProcess(
                       args=[], returncode=0, stdout=b'not a wav', stderr=b'')):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().run(req)
        assert 'no WAV on stdout' in str(e.value)

    def test_run_rejects_empty_stdout(self):
        req = _req(backend_id='kobold_tts')
        req.prepared = {'argv': ['koboldcpp.exe', '-p', 'hi'], 'timeout': 600}
        with patch('agent_cascade.tools.media_backends.kobold_tts.subprocess.run',
                   return_value=subprocess.CompletedProcess(
                       args=[], returncode=0, stdout=b'', stderr=b'')):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().run(req)
        assert 'no WAV on stdout' in str(e.value)

    def test_run_nonzero_exit(self):
        req = _req(backend_id='kobold_tts')
        req.prepared = {'argv': ['koboldcpp.exe', '-p', 'hi'], 'timeout': 600}
        with patch('agent_cascade.tools.media_backends.kobold_tts.subprocess.run',
                   return_value=subprocess.CompletedProcess(
                       args=[], returncode=2, stdout=b'', stderr=b'boom')):
            with pytest.raises(RuntimeError) as e:
                KoboldTTSBackend().run(req)
        assert 'exit 2' in str(e.value)

    def test_run_timeout_raises_timeouterror(self):
        req = _req(backend_id='kobold_tts')
        req.prepared = {'argv': ['koboldcpp.exe', '-p', 'hi'], 'timeout': 600}
        with patch('agent_cascade.tools.media_backends.kobold_tts.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('koboldcpp.exe', 600)):
            with pytest.raises(TimeoutError):
                KoboldTTSBackend().run(req)


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

        def _run(argv, **kwargs):
            order.append('run')
            return subprocess.CompletedProcess(args=argv, returncode=0,
                                              stdout=_wav_bytes(), stderr=b'')

        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=tts_files), \
              patch('agent_cascade.tools.media_backends.kobold_tts.subprocess.run', side_effect=_run), \
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
        tool = MediaGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=tts_files), \
              patch('agent_cascade.tools.media_backends.kobold_tts._resolve_tts_config',
                    side_effect=RuntimeError('no voice config')), \
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
        tool = MediaGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=tts_files), \
              patch('agent_cascade.tools.media_backends.kobold_tts.subprocess.run',
                    return_value=subprocess.CompletedProcess(
                        args=[], returncode=0, stdout=b'not wav', stderr=b'')), \
              patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
              patch('agent_cascade.state_ops.save_instance_state', return_value=True), \
              patch('agent_cascade.state_ops.unload_all_models', return_value=True), \
              patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'hi', 'backend': 'kobold_tts',
                                'config': 'kokoro_default'})

        m_restore.assert_called_once()
        assert 'ERROR' in result[0].text


def _fake_instance():
    inst = MagicMock()
    inst._last_endpoint_config = {
        'state_save_enabled': True,
        'api_base': 'http://localhost:1234',
        'model': 'test-model',
    }
    return inst
