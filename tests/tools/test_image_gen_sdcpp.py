"""Tests for the stable-diffusion.cpp (sd-cli) backend of the image_gen tool.

Covers:
  * ``_sdcpp_argv`` — pure argv builder (exact-list assertions, no binary needed)
  * ``_resolve_sdcpp_config`` / ``_list_sdcpp_configs`` — model config-file selection
    + model-file resolution (per-model JSON files in ``sdcpp.config_dir``)
  * ``_sdcpp_generate`` / ``_handle_sdcpp`` — subprocess runner error contract,
    VRAM ordering, error paths, temp-file cleanup (all mocked)

All external I/O is mocked. No binary execution, no network. The opt-in real
smoke test lives in ``test_image_gen_sdcpp_smoke.py``.
"""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.tools.image_gen import (
    ImageGen,
    _SDCPP_TYPE,
    _invalidate_image_gen_config,
    _list_sdcpp_configs,
    _resolve_sdcpp_config,
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
        'config_dir': r'C:\configs',
        'default_sdcpp_config': 'z_image_turbo',
    }
    sdcpp.update(sdcpp_overrides)
    return {'type': _SDCPP_TYPE, 'timeout': 300, _SDCPP_TYPE: sdcpp}


def _model_cfg(**over):
    """A resolved-style model config (model-file paths already absolute)."""
    p = {
        'diffusion_model': r'C:\models\ckpt.safetensors',
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
            'config_dir': r'C:\configs',
            'default_sdcpp_config': 'z_image_turbo',
            'timeout': 900,
        },
    }


def _resolved_config():
    return {
        'diffusion_model': r'C:\models\c.safetensors',
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
    def test_always_diffusion_model_flag(self):
        # The redesign always emits --diffusion-model (the -m toggle is dropped).
        argv = _sdcpp_argv(_cfg(), _model_cfg(), prompt='x', output_path='/out.png')
        assert '--diffusion-model' in argv and '-m' not in argv
        assert argv[argv.index('--diffusion-model') + 1] == r'C:\models\ckpt.safetensors'

    def test_te_flags_omitted_when_empty(self):
        argv = _sdcpp_argv(_cfg(), _model_cfg(), prompt='x', output_path='/out.png')
        for flag in ('--clip_l', '--clip_g', '--t5xxl', '--llm'):
            assert flag not in argv

    def test_all_te_flags_emitted_in_order(self):
        model_cfg = _model_cfg(clip_l='cl', clip_g='cg', t5xxl='t5', llm='llm', vae='vae')
        argv = _sdcpp_argv(_cfg(), model_cfg, prompt='x', output_path='/out.png')
        idx = [argv.index(f) for f in ('--clip_l', '--clip_g', '--t5xxl', '--llm', '--vae')]
        assert idx == sorted(idx)

    def test_seed_none_is_explicit_random(self):
        # F3: seed=None -> -s -1 (explicit random), NOT omitted.
        argv = _sdcpp_argv(_cfg(), _model_cfg(), prompt='x', output_path='/out.png')
        assert argv[argv.index('-s') + 1] == '-1'

    def test_seed_negative_is_random(self):
        argv = _sdcpp_argv(_cfg(), _model_cfg(), prompt='x', seed=-1, output_path='/out.png')
        assert argv[argv.index('-s') + 1] == '-1'

    def test_seed_explicit(self):
        argv = _sdcpp_argv(_cfg(), _model_cfg(), prompt='x', seed=42, output_path='/out.png')
        assert argv[argv.index('-s') + 1] == '42'

    def test_seed_non_numeric_is_random(self):
        argv = _sdcpp_argv(_cfg(), _model_cfg(), prompt='x', seed='garbage', output_path='/out.png')
        assert argv[argv.index('-s') + 1] == '-1'

    def test_param_overrides_config(self):
        argv = _sdcpp_argv(_cfg(), _model_cfg(steps=20), prompt='x', steps=4, output_path='/out.png')
        assert argv[argv.index('--steps') + 1] == '4'

    def test_config_default_used(self):
        argv = _sdcpp_argv(_cfg(), _model_cfg(steps=20), prompt='x', output_path='/out.png')
        assert argv[argv.index('--steps') + 1] == '20'

    def test_final_fallback(self):
        model_cfg = _model_cfg()
        for k in ('steps', 'cfg_scale', 'width', 'height'):
            del model_cfg[k]
        argv = _sdcpp_argv(_cfg(), model_cfg, prompt='x', output_path='/out.png')
        assert argv[argv.index('--steps') + 1] == '20'
        assert argv[argv.index('--cfg-scale') + 1] == '7'
        assert argv[argv.index('-W') + 1] == '512'
        assert argv[argv.index('-H') + 1] == '512'

    def test_guidance_omitted_when_none(self):
        argv = _sdcpp_argv(_cfg(), _model_cfg(guidance=None), prompt='x', output_path='/out.png')
        assert '--guidance' not in argv

    def test_guidance_emitted_when_set(self):
        argv = _sdcpp_argv(_cfg(), _model_cfg(guidance=3.5), prompt='x', output_path='/out.png')
        assert argv[argv.index('--guidance') + 1] == '3.5'

    def test_cfg_scale_g_formatting(self):
        argv = _sdcpp_argv(_cfg(), _model_cfg(cfg_scale=7.0), prompt='x', output_path='/out.png')
        assert argv[argv.index('--cfg-scale') + 1] == '7'

    def test_sampler_from_config(self):
        # The sampler is config-file-only (the tool param was dropped).
        argv = _sdcpp_argv(_cfg(), _model_cfg(sampler='euler'), prompt='x', output_path='/out.png')
        assert argv[argv.index('--sampling-method') + 1] == 'euler'

    def test_prompt_is_single_argv_element(self):
        # Regression for the cmd /c discovery: a prompt with spaces AND commas
        # must appear as exactly one element, unmodified.
        prompt = 'a red cube, on a white background'
        argv = _sdcpp_argv(_cfg(), _model_cfg(), prompt=prompt, output_path='/out.png')
        assert argv.count(prompt) == 1
        assert argv[argv.index('-p') + 1] == prompt

    def test_prompt_starting_with_dash_rejected(self):
        # F8
        with pytest.raises(ValueError):
            _sdcpp_argv(_cfg(), _model_cfg(), prompt='--foo', output_path='/out.png')

    def test_prompt_leading_space_then_dash_rejected(self):
        # F8 (first NON-space char is '-')
        with pytest.raises(ValueError):
            _sdcpp_argv(_cfg(), _model_cfg(), prompt='  --foo', output_path='/out.png')

    def test_paths_passed_through_verbatim(self):
        model_cfg = _model_cfg(diffusion_model=r'C:\models\sub\ckpt.safetensors')
        argv = _sdcpp_argv(_cfg(), model_cfg, prompt='x', output_path='/out.png')
        assert r'C:\models\sub\ckpt.safetensors' in argv

    def test_extra_args_appended_verbatim(self):
        cfg = _cfg(extra_args=['--fa', '--split-mode', 'l'])
        argv = _sdcpp_argv(cfg, _model_cfg(), prompt='x', output_path='/out.png')
        assert argv[-3:] == ['--fa', '--split-mode', 'l']

    def test_offload_and_backend_flags(self):
        cfg = _cfg(backend='diffusion=CUDA0', offload_to_cpu=True)
        argv = _sdcpp_argv(cfg, _model_cfg(), prompt='x', output_path='/out.png')
        assert 'diffusion=CUDA0' in argv and '--offload-to-cpu' in argv
        argv2 = _sdcpp_argv(_cfg(), _model_cfg(), prompt='x', output_path='/out.png')
        assert '--offload-to-cpu' not in argv2 and 'diffusion=CUDA0' not in argv2

    def test_output_path_always_present(self):
        argv = _sdcpp_argv(_cfg(), _model_cfg(), prompt='x', output_path='/out/abs.png')
        assert argv[argv.index('-o') + 1] == '/out/abs.png'


# ── _list_sdcpp_configs ──────────────────────────────────────────────────────

class TestListSdcppConfigs:
    def test_empty_dir_returns_empty(self, tmp_path):
        d = tmp_path / 'cfgs'
        d.mkdir()
        assert _list_sdcpp_configs(str(d)) == []

    def test_missing_dir_returns_empty(self, tmp_path):
        assert _list_sdcpp_configs(str(tmp_path / 'nope')) == []

    def test_lists_json_stems_sorted(self, tmp_path):
        d = tmp_path / 'cfgs'
        d.mkdir()
        (d / 'z_image_turbo.json').write_text('{}')
        (d / 'anima.json').write_text('{}')
        (d / 'notes.txt').write_text('ignore me')
        (d / 'flux2_klein_9b.json.example').write_text('{}')  # non-.json, hidden
        configs = _list_sdcpp_configs(str(d))
        names = [c['name'] for c in configs]
        assert names == ['anima', 'z_image_turbo']  # sorted, .txt and .example excluded
        assert all(c['path'].endswith('.json') for c in configs)


# ── _resolve_sdcpp_config ────────────────────────────────────────────────────

class TestSdcppConfigResolution:
    def _cfg_with_files(self, tmp_path, present, config_name='z_image_turbo',
                        config=None, global_overrides=None):
        """Create a config_dir with one config file and a models_dir with `present`."""
        if config is None:
            config = {'diffusion_model': 'c.safetensors', 'vae': 'v.safetensors'}
        models = tmp_path / 'models'
        models.mkdir(parents=True, exist_ok=True)
        for name in present:
            p = models / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('x')
        cfg_dir = tmp_path / 'sdcpp_configs'
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / f'{config_name}.json').write_text(json.dumps(config))
        sdcpp = {
            'binary': 'sd-cli',
            'models_dir': str(models),
            'config_dir': str(cfg_dir),
            'default_sdcpp_config': config_name,
        }
        if global_overrides:
            sdcpp.update(global_overrides)
        return {'type': _SDCPP_TYPE, _SDCPP_TYPE: sdcpp}

    def test_default_config_used(self, tmp_path):
        cfg = self._cfg_with_files(tmp_path, ['c.safetensors', 'v.safetensors'])
        model_cfg = _resolve_sdcpp_config(cfg, None)
        assert model_cfg['diffusion_model'].endswith('c.safetensors')
        assert model_cfg['vae'].endswith('v.safetensors')

    def test_explicit_name_used(self, tmp_path):
        cfg = self._cfg_with_files(tmp_path, ['c.safetensors', 'v.safetensors'])
        model_cfg = _resolve_sdcpp_config(cfg, 'z_image_turbo')
        assert model_cfg['diffusion_model'].endswith('c.safetensors')

    def test_missing_config_raises_with_names(self, tmp_path):
        cfg = self._cfg_with_files(tmp_path, ['c.safetensors', 'v.safetensors'])
        with pytest.raises(RuntimeError) as exc:
            _resolve_sdcpp_config(cfg, 'nope')
        assert 'nope' in str(exc.value)
        assert 'z_image_turbo' in str(exc.value)  # available names listed

    def test_missing_model_file_raises_naming_all_missing(self, tmp_path):
        # Only the checkpoint present; the VAE is missing -> named in the message.
        cfg = self._cfg_with_files(tmp_path, ['c.safetensors'])
        with pytest.raises(RuntimeError) as exc:
            _resolve_sdcpp_config(cfg, 'z_image_turbo')
        msg = str(exc.value)
        assert 'not found' in msg
        assert 'v.safetensors' in msg

    def test_model_key_absent_raises(self, tmp_path):
        cfg = self._cfg_with_files(tmp_path, ['v.safetensors'],
                                   config={'vae': 'v.safetensors'})
        with pytest.raises(RuntimeError) as exc:
            _resolve_sdcpp_config(cfg, 'z_image_turbo')
        assert "no 'diffusion_model'" in str(exc.value)

    def test_no_configs_raises(self, tmp_path):
        cfg_dir = tmp_path / 'empty_configs'
        cfg_dir.mkdir()
        cfg = {'type': _SDCPP_TYPE, _SDCPP_TYPE: {
            'binary': 'sd-cli', 'config_dir': str(cfg_dir),
            'default_sdcpp_config': '',
        }}
        with pytest.raises(RuntimeError) as exc:
            _resolve_sdcpp_config(cfg, None)
        assert 'none' in str(exc.value)

    def test_full_path_fallback(self, tmp_path):
        # Q1: a full .json path is used directly, bypassing the dir scan.
        cfg = self._cfg_with_files(tmp_path, ['c.safetensors', 'v.safetensors'])
        full_path = str(tmp_path / 'sdcpp_configs' / 'z_image_turbo.json')
        model_cfg = _resolve_sdcpp_config(cfg, full_path)
        assert model_cfg['diffusion_model'].endswith('c.safetensors')

    def test_binary_models_dir_inheritance(self, tmp_path):
        # Inheritable values fall through to the global sdcpp block.
        cfg = self._cfg_with_files(tmp_path, ['c.safetensors', 'v.safetensors'])
        model_cfg = _resolve_sdcpp_config(cfg, 'z_image_turbo')
        assert model_cfg['binary'] == 'sd-cli'  # inherited from global
        assert model_cfg['models_dir'].endswith('models')

    def test_per_file_models_dir_override(self, tmp_path):
        # A config file that sets its own models_dir wins over the global.
        models2 = tmp_path / 'models2'
        models2.mkdir()
        (models2 / 'c.safetensors').write_text('x')
        (models2 / 'v.safetensors').write_text('x')
        cfg = self._cfg_with_files(
            tmp_path, ['c.safetensors', 'v.safetensors'],
            config={'diffusion_model': 'c.safetensors', 'vae': 'v.safetensors',
                    'models_dir': str(models2)},
        )
        model_cfg = _resolve_sdcpp_config(cfg, 'z_image_turbo')
        assert model_cfg['models_dir'] == str(models2)
        assert model_cfg['diffusion_model'].endswith('c.safetensors')


# ── _handle_sdcpp (mocked) ───────────────────────────────────────────────────

class TestSdcppHandleMocked:
    def test_success_returns_uncaptioned_image(self, sdcpp_cfg):
        tool = ImageGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
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
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
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
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
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
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
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
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
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
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
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
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
              patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run), \
              patch('agent_cascade.tools.image_gen.save_image_to_media', return_value='/tmp/media/out.png'):
            result = tool.call({'prompt': 'a red cube'})

        assert 'ERROR' in result[0].text
        assert 'no image' in result[0].text

    def test_save_state_fails_no_restore(self, sdcpp_cfg):
        tool = ImageGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=sdcpp_cfg), \
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
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
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
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
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
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
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
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
              patch('agent_cascade.tools.image_gen._resolve_sdcpp_config', return_value=_resolved_config()), \
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
