"""Tests for the ComfyUI MUSIC path (YuE2) — media_gen increment 2, build step 7.

The critical contract (plan §5.5): the agent's music request maps to TWO fields —
``style`` (instrumentation/mood) and ``lyrics`` (with [verse] tags) — surfaced as
separate override keys, NOT a single prompt. The YuE2 workflow has NO CLIPTextEncode
nodes, so ``_inject_params`` would hard-raise; instead we apply the three ordered
changes HERE (image_gen is frozen):
  1. prompt placement is SKIPPED,
  2. a multi-node writer writes style/lyrics to EVERY YuE2 text node (22 + 23),
  3. seed injection STILL runs (KSampler node 8 + the per-node seeds).

Music output is *audio* (one of the three canonical media types), extracted from
disk like video.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.tools.media_gen import (
    MediaGen,
    _invalidate_media_gen_config,
)
from agent_cascade.tools.media_backends import resolve_backend
from agent_cascade.tools.media_backends.base import MediaRequest
from agent_cascade.tools.media_backends.comfyui import (
    ComfyUIBackend,
    _inject_music_params,
    _find_save_node_prefix,
    _find_newest_output,
)


@pytest.fixture(autouse=True)
def _bust_config_cache():
    _invalidate_media_gen_config()
    yield
    _invalidate_media_gen_config()


# ── helpers ──────────────────────────────────────────────────────────────────

def _music_cfg(**over):
    cfg = {
        'comfyui': {
            'url': 'http://127.0.0.1:8188',
            'workflow_dir': r'C:\workflows',
            'default_workflow': r'C:\workflows\yue2_full.json',
            'output_dir': r'C:\comfyui_out',
            'timeout': 600,
            'media_types': {'image': [], 'video': [], 'audio': ['yue2_full']},
        },
        'sdcpp': {'binary': r'C:\bin\sd-cli.exe', 'media_types': {'image': []}},
    }
    cfg.update(over)
    return cfg


def _req(**over):
    req = MediaRequest(
        media_type='', backend_id='AUTO', config_name=None,
        prompt='violin with girl singing jpop anime', negative_prompt='',
        config=_music_cfg(), agent_pool=None,
    )
    for k, v in over.items():
        setattr(req, k, v)
    return req


def _fake_instance():
    inst = MagicMock()
    inst._last_endpoint_config = {
        'state_save_enabled': True,
        'api_base': 'http://localhost:1234',
        'model': 'test-model',
    }
    return inst


# A faithful fragment of yue2_full.json (the nodes the injection touches).
_YUE2_WF = {
    '8': {'inputs': {'seed': 7, 'steps': 32, 'cfg': 1, 'sampler_name': 'dpm_2',
                     'scheduler': 'sgm_uniform', 'denoise': 1,
                     'model': ['15', 0], 'positive': ['22', 0],
                     'negative': ['22', 0], 'latent_image': ['5', 0]},
          'class_type': 'KSampler'},
    '10': {'inputs': {'filename_prefix': 'audio/YuE2', 'format': 'flac',
                      'audio': ['17', 0]}, 'class_type': 'SaveAudioAdvanced'},
    '22': {'inputs': {'style': 'OLD STYLE', 'lyrics': 'OLD LYRICS',
                      'abc': ['14', 0], 'seed': 17, 'mode': 'full',
                      'max_duration': 360, 'temperature': 1, 'top_p': 0.95,
                      'top_k': 100, 'repetition_penalty': 1.2, 'cfg_scale': 1,
                      'clip': ['15', 1]}, 'class_type': 'YuE2GenerateMusic'},
    '23': {'inputs': {'style': 'OLD STYLE', 'lyrics': 'OLD LYRICS',
                      'seed': 10, 'mode': 'full', 'max_abc_tokens': 8192,
                      'temperature': 0.7, 'top_p': 0.9, 'top_k': 30,
                      'repetition_penalty': 1.005, 'penalty_window': 100,
                      'clip': ['15', 1]}, 'class_type': 'YuE2GenerateABC'},
}


# ── the two-field injection (the core contract) ──────────────────────────────

class TestMusicInjection:
    def test_inject_writes_style_and_lyrics_to_both_nodes(self):
        wf = json.loads(json.dumps(_YUE2_WF))
        style = 'lo-fi synthwave, warm pads'
        lyrics = '[verse]\nsunny day\nsunny day'
        wf, report = _inject_music_params(wf, style=style, lyrics=lyrics, seed=42)
        # BOTH YuE2 text nodes updated (22 and 23).
        assert wf['22']['inputs']['style'] == style
        assert wf['22']['inputs']['lyrics'] == lyrics
        assert wf['23']['inputs']['style'] == style
        assert wf['23']['inputs']['lyrics'] == lyrics
        # Seed STILL injected into KSampler node 8 AND the per-node seeds.
        assert wf['8']['inputs']['seed'] == 42
        assert wf['22']['inputs']['seed'] == 42
        assert wf['23']['inputs']['seed'] == 42
        # Report names every touched field.
        assert any('22.style' in r for r in report)
        assert any('23.lyrics' in r for r in report)
        assert any('8.seed' in r for r in report)

    def test_inject_skips_prompt_placement(self):
        # No CLIPTextEncode nodes exist; _inject_music_params never raises and
        # never looks for a prompt slot.
        wf = json.loads(json.dumps(_YUE2_WF))
        wf, report = _inject_music_params(wf, style='s', lyrics='l', seed=None)
        assert not any('CLIP' in r for r in report)
        # seed=None → no seed fields touched.
        assert wf['8']['inputs']['seed'] == 7

    def test_inject_only_touches_declared_fields(self):
        # A node without a 'style' key is not given one.
        wf = json.loads(json.dumps(_YUE2_WF))
        wf['23']['inputs'].pop('style')
        wf, report = _inject_music_params(wf, style='s', lyrics='l', seed=None)
        assert 'style' not in wf['23']['inputs']
        assert wf['22']['inputs']['style'] == 's'

    def test_find_save_node_prefix_audio(self):
        assert _find_save_node_prefix(_YUE2_WF) == 'audio/YuE2'


# ── preflight: music uses the music path, not _inject_params ─────────────────

class TestMusicPreflight:
    def test_preflight_uses_music_injection(self):
        req = _req(config_name=r'C:\workflows\yue2_full.json', backend_id='comfyui',
                   media_type='audio',
                   overrides={'style': 'dark synth', 'lyrics': '[verse]\nla la',
                              'seed': 99})
        wf = json.loads(json.dumps(_YUE2_WF))
        with patch('agent_cascade.tools.media_backends.comfyui._load_workflow',
                   return_value=wf), \
             patch('agent_cascade.tools.media_backends.comfyui._inject_music_params',
                   wraps=_inject_music_params) as m_music:
            ComfyUIBackend().preflight(req)
        # The music path was taken (NOT _inject_params).
        m_music.assert_called_once()
        assert req.prepared['workflow']['22']['inputs']['style'] == 'dark synth'
        assert req.prepared['workflow']['22']['inputs']['lyrics'] == '[verse]\nla la'
        assert req.prepared['workflow']['8']['inputs']['seed'] == 99
        assert req.prepared['fetch'] == 'server_file'
        assert req.prepared['filename_prefix'] == 'audio/YuE2'

    def test_preflight_prompt_doubles_as_style(self):
        # No explicit style override → prompt is used as the style.
        req = _req(config_name=r'C:\workflows\yue2_full.json', backend_id='comfyui',
                   media_type='audio', overrides={'lyrics': '[verse]\nhi'})
        wf = json.loads(json.dumps(_YUE2_WF))
        with patch('agent_cascade.tools.media_backends.comfyui._load_workflow',
                   return_value=wf):
            ComfyUIBackend().preflight(req)
        assert req.prepared['workflow']['22']['inputs']['style'] == req.prompt

    def test_music_rejects_reference_file(self):
        # Music has no LoadImage / voice-cloning seam; reference_file is ignored
        # (not wired), and must not crash.
        req = _req(config_name=r'C:\workflows\yue2_full.json', backend_id='comfyui',
                   media_type='audio', reference_file=r'C:\x\ref.wav')
        wf = json.loads(json.dumps(_YUE2_WF))
        with patch('agent_cascade.tools.media_backends.comfyui._load_workflow',
                   return_value=wf):
            ComfyUIBackend().preflight(req)
        # No LoadImage wiring attempted (music branch skips it).
        assert '97' not in req.prepared['workflow']


# ── output extraction (audio, runtime ext) ───────────────────────────────────

class TestMusicExtraction:
    def test_find_newest_audio(self, tmp_path):
        out = tmp_path / 'audio'
        out.mkdir()
        f = out / 'YuE2_0001_.flac'
        f.write_bytes(b'flac-bytes')
        found = _find_newest_output(str(tmp_path), 'audio/YuE2',
                                   submit_time=0.0, preexisting=set())
        assert found == (b'flac-bytes', 'flac')


# ── VRAM slotting for the music path ─────────────────────────────────────────

class TestMusicVRAM:
    def test_order_save_unload_run_restore(self, tmp_path):
        tool = MediaGen()
        order = []

        def _save(inst):
            order.append('save'); return True

        def _unload(base):
            order.append('unload'); return True

        def _restore(inst, held_endpoint_cfg=None):
            order.append('restore'); return True

        def _gen(url, workflow, output_dir, timeout, client=None):
            order.append('run')
            return b'flac', 'flac', {}

        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=_music_cfg()), \
              patch('agent_cascade.tools.media_backends.comfyui._load_workflow',
                    return_value=json.loads(json.dumps(_YUE2_WF))), \
              patch('agent_cascade.tools.media_backends.comfyui._inject_music_params',
                    wraps=_inject_music_params), \
              patch('agent_cascade.tools.media_backends.comfyui._comfyui_generate_to_file',
                    side_effect=_gen), \
              patch('agent_cascade.tools.media_gen.save_media_to_media', return_value='/tmp/media/m.flac'), \
              patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
              patch('agent_cascade.state_ops.save_instance_state', side_effect=_save), \
              patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload), \
              patch('agent_cascade.state_ops.restore_instance_state', side_effect=_restore):
            result = tool.call({'prompt': 'synth', 'backend': 'comfyui',
                                'config': r'C:\workflows\yue2_full.json',
                                'media_type': 'audio',
                                'overrides': {'style': 'synth', 'lyrics': '[verse]\nla'}})

        assert order == ['save', 'unload', 'run', 'restore']
        assert result[0].audio == '/tmp/media/m.flac'
        assert 'backend=comfyui' in result[1].text
