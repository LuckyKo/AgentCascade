"""Tests for the stable-diffusion.cpp (sd-cli) backend of the image_gen tool.

Covers:
  * ``_sdcpp_argv`` — pure argv builder (exact-list assertions, no binary needed)
  * ``_resolve_sdcpp_preset`` — preset selection + model-file resolution
  * ``_sdcpp_generate`` / ``_handle_sdcpp`` — subprocess runner error contract,
    VRAM ordering, error paths, temp-file cleanup (all mocked)

All external I/O is mocked. No binary execution, no network. The opt-in real
smoke test lives in ``test_image_gen_sdcpp_smoke.py``.
"""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.tools.image_gen import (
    ImageGen,
    _SDCPP_TYPE,
    _invalidate_image_gen_config,
    _resolve_sdcpp_preset,
    _sdcpp_argv,
)


@pytest.fixture(autouse=True)
def _bust_config_cache():
    """Ensure the image_gen config cache is clean before and after every test."""
    _invalidate_image_gen_config()
    yield
    _invalidate_image_gen_config()


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(**sdcpp_overrides):
    """Build a minimal config with an sdcpp block for argv-builder tests."""
    sdcpp = {
        'binary': r'C:\bin\sd-cli.exe',
        'models_dir': r'C:\models',
        'default_model': 'sdxl',
        'presets': {},
    }
    sdcpp.update(sdcpp_overrides)
    return {'type': _SDCPP_TYPE, 'timeout': 300, _SDCPP_TYPE: sdcpp}


def _preset(**over):
    """A resolved-style preset (model-file paths already absolute)."""
    p = {
        'model_arg': 'model',
        'model': r'C:\models\ckpt.safetensors',
        'vae': r'C:\models\vae.safetensors',
        'clip_l': '', 'clip_g': '', 't5xxl': '', 'llm': '',
        'sampler': 'euler_a',
        'steps': 20,
        'cfg_scale': 7.0,
        'guidance': None,
        'width': 512, 'height': 512,
    }
    p.update(over)
    return p


@pytest.fixture
def sdcpp_cfg(tmp_path):
    """A full sdcpp config with a REAL temp binary so the is_file() check passes."""
    bin_path = tmp_path / 'sd-cli.exe'
    bin_path.write_text('fake binary')
    return {
        'type': _SDCPP_TYPE,
        'timeout': 300,
        _SDCPP_TYPE: {
            'binary': str(bin_path),
            'models_dir': r'C:\models',
            'default_model': 'sdxl',
            'timeout': 900,
            'presets': {'sdxl': {'model_arg': 'model', 'model': 'c.safetensors',
                                 'vae': 'v.safetensors'}},
        },
    }


def _resolved_preset():
    return {
        'model_arg': 'model', 'model': r'C:\models\c.safetensors',
        'vae': r'C:\models\v.safetensors', 'steps': 20, 'cfg_scale': 7.0,
        'width': 512, 'height': 512, 'sampler': 'euler_a', 'guidance': None,
    }


def _fake_instance():
    inst = MagicMock()
    inst._last_endpoint_config = {
        'state_save_enabled': True,
        'api_base': 'http://localhost:1234',
        'model': 'test-model',
    }
    return inst


def _fake_run_write_bytes(data=b'\x89PNG-fake-bytes'):
    """A subprocess.run stand-in that writes ``data`` to the -o path, rc=0."""
    def _run(argv, **kwargs):
        out = argv[argv.index('-o') + 1]
        with open(out, 'wb') as f:
            f.write(data)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b'', stderr=b'')
    return _run


# ── _sdcpp_argv (pure) ───────────────────────────────────────────────────────

class TestSdcppArgv:
    def test_model_arg_selects_flag(self):
        cfg = _cfg()
        argv = _sdcpp_argv(cfg, _preset(model_arg='model'), prompt='x', output_path='/out.png')
        assert '-m' in argv and '--diffusion-model' not in argv
        argv2 = _sdcpp_argv(cfg, _preset(model_arg='diffusion_model'), prompt='x', output_path='/out.png')
        assert '--diffusion-model' in argv2 and '-m' not in argv2

    def test_te_flags_omitted_when_empty(self):
        argv = _sdcpp_argv(_cfg(), _preset(), prompt='x', output_path='/out.png')
        for flag in ('--clip_l', '--clip_g', '--t5xxl', '--llm'):
            assert flag not in argv

    def test_all_te_flags_emitted_in_order(self):
        preset = _preset(clip_l='cl', clip_g='cg', t5xxl='t5', llm='llm', vae='vae')
        argv = _sdcpp_argv(_cfg(), preset, prompt='x', output_path='/out.png')
        idx = [argv.index(f) for f in ('--clip_l', '--clip_g', '--t5xxl', '--llm', '--vae')]
        assert idx == sorted(idx)

    def test_seed_none_is_explicit_random(self):
        # F3: seed=None -> -s -1 (explicit random), NOT omitted.
        argv = _sdcpp_argv(_cfg(), _preset(), prompt='x', output_path='/out.png')
        assert argv[argv.index('-s') + 1] == '-1'

    def test_seed_negative_is_random(self):
        argv = _sdcpp_argv(_cfg(), _preset(), prompt='x', seed=-1, output_path='/out.png')
        assert argv[argv.index('-s') + 1] == '-1'

    def test_seed_explicit(self):
        argv = _sdcpp_argv(_cfg(), _preset(), prompt='x', seed=42, output_path='/out.png')
        assert argv[argv.index('-s') + 1] == '42'

    def test_seed_non_numeric_is_random(self):
        argv = _sdcpp_argv(_cfg(), _preset(), prompt='x', seed='garbage', output_path='/out.png')
        assert argv[argv.index('-s') + 1] == '-1'

    def test_param_overrides_preset(self):
        argv = _sdcpp_argv(_cfg(), _preset(steps=20), prompt='x', steps=4, output_path='/out.png')
        assert argv[argv.index('--steps') + 1] == '4'

    def test_preset_default_used(self):
        argv = _sdcpp_argv(_cfg(), _preset(steps=20), prompt='x', output_path='/out.png')
        assert argv[argv.index('--steps') + 1] == '20'

    def test_final_fallback(self):
        preset = _preset()
        for k in ('steps', 'cfg_scale', 'width', 'height'):
            del preset[k]
        argv = _sdcpp_argv(_cfg(), preset, prompt='x', output_path='/out.png')
        assert argv[argv.index('--steps') + 1] == '20'
        assert argv[argv.index('--cfg-scale') + 1] == '7'
        assert argv[argv.index('-W') + 1] == '512'
        assert argv[argv.index('-H') + 1] == '512'

    def test_guidance_omitted_when_none(self):
        argv = _sdcpp_argv(_cfg(), _preset(guidance=None), prompt='x', output_path='/out.png')
        assert '--guidance' not in argv

    def test_guidance_emitted_when_set(self):
        argv = _sdcpp_argv(_cfg(), _preset(guidance=3.5), prompt='x', output_path='/out.png')
        assert argv[argv.index('--guidance') + 1] == '3.5'

    def test_cfg_scale_g_formatting(self):
        argv = _sdcpp_argv(_cfg(), _preset(cfg_scale=7.0), prompt='x', output_path='/out.png')
        assert argv[argv.index('--cfg-scale') + 1] == '7'

    def test_prompt_is_single_argv_element(self):
        # Regression for the cmd /c discovery: a prompt with spaces AND commas
        # must appear as exactly one element, unmodified.
        prompt = 'a red cube, on a white background'
        argv = _sdcpp_argv(_cfg(), _preset(), prompt=prompt, output_path='/out.png')
        assert argv.count(prompt) == 1
        assert argv[argv.index('-p') + 1] == prompt

    def test_prompt_starting_with_dash_rejected(self):
        # F8
        with pytest.raises(ValueError):
            _sdcpp_argv(_cfg(), _preset(), prompt='--foo', output_path='/out.png')

    def test_prompt_leading_space_then_dash_rejected(self):
        # F8 (first NON-space char is '-')
        with pytest.raises(ValueError):
            _sdcpp_argv(_cfg(), _preset(), prompt='  --foo', output_path='/out.png')

    def test_paths_passed_through_verbatim(self):
        preset = _preset(model=r'C:\models\sub\ckpt.safetensors')
        argv = _sdcpp_argv(_cfg(), preset, prompt='x', output_path='/out.png')
        assert r'C:\models\sub\ckpt.safetensors' in argv

    def test_extra_args_appended_verbatim(self):
        cfg = _cfg(extra_args=['--fa', '--split-mode', 'l'])
        argv = _sdcpp_argv(cfg, _preset(), prompt='x', output_path='/out.png')
        assert argv[-3:] == ['--fa', '--split-mode', 'l']

    def test_offload_and_backend_flags(self):
        cfg = _cfg(backend='diffusion=CUDA0', offload_to_cpu=True)
        argv = _sdcpp_argv(cfg, _preset(), prompt='x', output_path='/out.png')
        assert 'diffusion=CUDA0' in argv and '--offload-to-cpu' in argv
        argv2 = _sdcpp_argv(_cfg(), _preset(), prompt='x', output_path='/out.png')
        assert '--offload-to-cpu' not in argv2 and 'diffusion=CUDA0' not in argv2

    def test_output_path_always_present(self):
        argv = _sdcpp_argv(_cfg(), _preset(), prompt='x', output_path='/out/abs.png')
        assert argv[argv.index('-o') + 1] == '/out/abs.png'


# ── _resolve_sdcpp_preset ────────────────────────────────────────────────────

class TestSdcppConfigResolution:
    def _cfg_with_files(self, tmp_path, present):
        models = tmp_path / 'models'
        for name in present:
            p = models / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('x')
        sdcpp = {
            'binary': 'sd-cli',
            'models_dir': str(models),
            'default_model': 'sdxl',
            'presets': {'sdxl': {'model_arg': 'model', 'model': 'ckpt.safetensors',
                                 'vae': 'vae.safetensors'}},
        }
        return {'type': _SDCPP_TYPE, _SDCPP_TYPE: sdcpp}

    def test_default_model_used(self, tmp_path):
        cfg = self._cfg_with_files(tmp_path, ['ckpt.safetensors', 'vae.safetensors'])
        preset = _resolve_sdcpp_preset(cfg, None)
        assert preset['model'].endswith('ckpt.safetensors')
        assert preset['vae'].endswith('vae.safetensors')

    def test_missing_preset_raises_with_names(self, tmp_path):
        cfg = self._cfg_with_files(tmp_path, ['ckpt.safetensors', 'vae.safetensors'])
        with pytest.raises(RuntimeError) as exc:
            _resolve_sdcpp_preset(cfg, 'nope')
        assert 'nope' in str(exc.value)
        assert 'sdxl' in str(exc.value)  # available names listed

    def test_missing_model_file_raises_naming_all_missing(self, tmp_path):
        # Only the checkpoint present; the VAE is missing -> named in the message.
        cfg = self._cfg_with_files(tmp_path, ['ckpt.safetensors'])
        with pytest.raises(RuntimeError) as exc:
            _resolve_sdcpp_preset(cfg, 'sdxl')
        msg = str(exc.value)
        assert 'not found' in msg
        assert 'vae.safetensors' in msg

    def test_model_key_absent_raises(self, tmp_path):
        cfg = self._cfg_with_files(tmp_path, ['vae.safetensors'])
        cfg[_SDCPP_TYPE]['presets']['sdxl'] = {'model_arg': 'model', 'vae': 'vae.safetensors'}
        with pytest.raises(RuntimeError) as exc:
            _resolve_sdcpp_preset(cfg, 'sdxl')
        assert "no 'model'" in str(exc.value)

    def test_no_presets_raises(self):
        cfg = {'type': _SDCPP_TYPE, _SDCPP_TYPE: {'binary': 'sd-cli'}}
        with pytest.raises(RuntimeError) as exc:
            _resolve_sdcpp_preset(cfg, None)
        assert 'presets' in str(exc.value)


# ── _handle_sdcpp (mocked) ───────────────────────────────────────────────────

class TestSdcppHandleMocked:
    def test_success_returns_uncaptioned_image(self, sdcpp_cfg):
        tool = ImageGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_fake_run_write_bytes()), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'), \
             patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
             patch('agent_cascade.state_ops.save_instance_state', return_value=True), \
             patch('agent_cascade.state_ops.unload_all_models', return_value=True), \
             patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'a red cube'})

        assert len(result) == 2
        assert result[0].image == '/tmp/media/out.png'
        assert not result[0].caption
        assert 'backend=sdcpp' in result[1].text
        m_restore.assert_called_once()

    def test_vram_order_save_unload_run_restore(self, sdcpp_cfg):
        tool = ImageGen()
        order = []

        def _save(inst):
            order.append('save'); return True

        def _unload(base):
            order.append('unload'); return True

        def _restore(inst, held_endpoint_cfg=None):
            order.append('restore'); return True

        def _run(argv, **kwargs):
            order.append('run')
            out = argv[argv.index('-o') + 1]
            with open(out, 'wb') as f:
                f.write(b'png')
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b'', stderr=b'')

        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'), \
             patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
             patch('agent_cascade.state_ops.save_instance_state', side_effect=_save), \
             patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload), \
             patch('agent_cascade.state_ops.restore_instance_state', side_effect=_restore):
            tool.call({'prompt': 'a red cube'})

        assert order == ['save', 'unload', 'run', 'restore']

    def test_unload_raises_still_restores(self, sdcpp_cfg):
        # F1: the prologue is inside the try, so a raise from unload_all_models
        # must still trigger the restore (no dangling KV).
        tool = ImageGen()

        def _unload(base):
            raise RuntimeError('unload blew up')

        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'), \
             patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
             patch('agent_cascade.state_ops.save_instance_state', return_value=True), \
             patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload), \
             patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'a red cube'})

        m_restore.assert_called_once()
        assert 'ERROR' in result[0].text

    def test_nonzero_rc_raises_runtime_with_stderr_tail(self, sdcpp_cfg):
        tool = ImageGen()

        def _run(argv, **kwargs):
            return subprocess.CompletedProcess(args=argv, returncode=3, stdout=b'',
                                               stderr=b'boom: some fatal detail')

        with patch.object(tool, '_get_instance', return_value=None), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'):
            result = tool.call({'prompt': 'a red cube'})

        assert 'ERROR' in result[0].text
        assert 'exited with code 3' in result[0].text
        assert 'some fatal detail' in result[0].text

    def test_assert_abort_code_is_reported(self, sdcpp_cfg):
        tool = ImageGen()

        def _run(argv, **kwargs):
            return subprocess.CompletedProcess(args=argv, returncode=3221226505, stdout=b'',
                                               stderr=b'GGML_ASSERT(ggml_can_repeat) failed')

        with patch.object(tool, '_get_instance', return_value=None), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'):
            result = tool.call({'prompt': 'a red cube'})

        assert '3221226505' in result[0].text

    def test_timeout_raises_timeouterror_and_restores(self, sdcpp_cfg):
        tool = ImageGen()

        def _run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'), \
             patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
             patch('agent_cascade.state_ops.save_instance_state', return_value=True), \
             patch('agent_cascade.state_ops.unload_all_models', return_value=True), \
             patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'a red cube'})

        assert 'ERROR' in result[0].text
        assert 'timed out' in result[0].text
        m_restore.assert_called_once()

    def test_zero_rc_no_output_file_raises(self, sdcpp_cfg):
        tool = ImageGen()

        def _run(argv, **kwargs):
            # rc=0 but writes nothing to the -o path
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b'', stderr=b'')

        with patch.object(tool, '_get_instance', return_value=None), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'):
            result = tool.call({'prompt': 'a red cube'})

        assert 'ERROR' in result[0].text
        assert 'no image' in result[0].text

    def test_save_state_fails_no_restore(self, sdcpp_cfg):
        tool = ImageGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_fake_run_write_bytes()), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'), \
             patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
             patch('agent_cascade.state_ops.save_instance_state', return_value=False), \
             patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'a red cube'})

        assert result[0].image == '/tmp/media/out.png'
        m_restore.assert_not_called()

    def test_media_save_failure_still_restores(self, sdcpp_cfg):
        tool = ImageGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_fake_run_write_bytes()), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', side_effect=OSError('disk full')), \
             patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
             patch('agent_cascade.state_ops.save_instance_state', return_value=True), \
             patch('agent_cascade.state_ops.unload_all_models', return_value=True), \
             patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'a red cube'})

        m_restore.assert_called_once()
        assert 'ERROR' in result[0].text

    def test_temp_file_cleaned_on_success(self, sdcpp_cfg):
        # F4: patch mkstemp to record the path, assert it is unlinked after the call.
        tool = ImageGen()
        created = {}
        real_mkstemp = __import__('tempfile').mkstemp

        def _mkstemp(*a, **k):
            fd, path = real_mkstemp(*a, **k)
            created['path'] = path
            return fd, path

        with patch.object(tool, '_get_instance', return_value=None), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_fake_run_write_bytes()), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'), \
             patch('agent_cascade.tools.image_gen.tempfile.mkstemp', side_effect=_mkstemp):
            result = tool.call({'prompt': 'a red cube'})

        assert result[0].image == '/tmp/media/out.png'
        assert 'path' in created
        assert not os.path.exists(created['path'])

    def test_temp_file_cleaned_on_failure(self, sdcpp_cfg):
        # F4: temp file is unlinked even when generation fails (non-zero rc).
        tool = ImageGen()
        created = {}
        real_mkstemp = __import__('tempfile').mkstemp

        def _mkstemp(*a, **k):
            fd, path = real_mkstemp(*a, **k)
            created['path'] = path
            return fd, path

        def _run(argv, **kwargs):
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout=b'', stderr=b'fail')

        with patch.object(tool, '_get_instance', return_value=None), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'), \
             patch('agent_cascade.tools.image_gen.tempfile.mkstemp', side_effect=_mkstemp):
            result = tool.call({'prompt': 'a red cube'})

        assert 'ERROR' in result[0].text
        assert 'path' in created
        assert not os.path.exists(created['path'])

    def test_prompt_starting_with_dash_returns_error(self, sdcpp_cfg):
        # F8 end-to-end: a dash-prefixed prompt is rejected cleanly, not a crash.
        tool = ImageGen()
        with patch.object(tool, '_get_instance', return_value=None), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._resolve_sdcpp_preset', return_value=_resolved_preset()), \
             patch('agent_cascade.tools.image_gen.subprocess.run') as m_run:
            result = tool.call({'prompt': '--evil'})

        assert 'ERROR' in result[0].text
        m_run.assert_not_called()

    def test_input_image_rejected_for_sdcpp(self, sdcpp_cfg):
        tool = ImageGen()
        with patch.object(tool, '_get_instance', return_value=None), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen.subprocess.run') as m_run:
            result = tool.call({'prompt': 'a red cube', 'input_image': 'C:\\ref.png'})

        assert 'ERROR' in result[0].text
        assert 'input_image' in result[0].text
        m_run.assert_not_called()

    def test_missing_sdcpp_block_returns_error(self):
        cfg = {'type': _SDCPP_TYPE, 'timeout': 300}  # no sdcpp block
        tool = ImageGen()
        with patch.object(tool, '_get_instance', return_value=None), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=cfg), \
             patch('agent_cascade.tools.image_gen.subprocess.run') as m_run:
            result = tool.call({'prompt': 'a red cube'})

        assert 'ERROR' in result[0].text
        assert 'sdcpp' in result[0].text
        m_run.assert_not_called()

    def test_comfyui_path_untouched_by_type_default(self):
        # No 'type' key -> ComfyUI path; _sdcpp_generate never called.
        tool = ImageGen()
        with patch.object(tool, '_get_instance', return_value=None), \
             patch('agent_cascade.tools.image_gen._get_image_gen_config',
                   return_value={'url': 'http://comfyui:8188', 'timeout': 60,
                                 'default_workflow': '/wf/test.json'}), \
             patch('agent_cascade.tools.image_gen._load_workflow',
                   return_value={'1': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}}}), \
             patch('agent_cascade.tools.image_gen._inject_params',
                   side_effect=lambda wf, **kw: (wf, ['prompt → 1'])), \
             patch('agent_cascade.tools.image_gen._comfyui_generate',
                   return_value=(b'fake_png', {'seed': 1})), \
             patch('agent_cascade.tools.image_gen._sdcpp_generate') as m_sdcpp, \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'):
            result = tool.call({'prompt': 'a red cube'})

        assert result[0].image == '/tmp/media/out.png'
        m_sdcpp.assert_not_called()

    def test_svg_path_unaffected(self, sdcpp_cfg):
        # An SVG prompt still routes to the SVG path, never touching sdcpp.
        tool = ImageGen()
        svg = '<svg width="10" height="10"><rect width="10" height="10"/></svg>'
        with patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
             patch('agent_cascade.tools.image_gen._render_svg_to_png_bytes', return_value=b'png'), \
             patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/svg.png'), \
             patch('agent_cascade.tools.image_gen._sdcpp_generate') as m_sdcpp:
            result = tool.call({'prompt': svg})

        assert result[0].image == '/tmp/media/svg.png'
        m_sdcpp.assert_not_called()
