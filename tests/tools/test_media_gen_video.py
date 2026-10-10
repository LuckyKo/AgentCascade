"""Tests for the ComfyUI VIDEO path (media_gen increment 2, build step 5).

Covers the video-specific logic WITHOUT a live GPU run:
  * preflight reuses the IMAGE injection (the WAN i2v workflow has CLIPTextEncode
    nodes + direct-int width/height on WanImageToVideo, so ``_inject_params``
    works unchanged) and records the disk-save output contract (output_dir from
    CONFIG, save-node filename_prefix).
  * reference_file → LoadImage first-frame wiring (i2v).
  * output extraction: the newest matching file under ``output_dir/<subfolder>``
    matching the save-node prefix, extension detected at runtime.
  * full submit→poll→extract path with a mocked httpx transport + tmp files.
  * VRAM slotting (preflight before VRAM; restore on error).

The LIVE WAN integration run is gated on AGENT_CASCADE_RUN_LOCAL_TESTS=1 and is
otherwise SKIPPED — a real generation is a separate verification, not part of the
mocked unit suite.
"""

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

import httpx

from agent_cascade.tools.media_gen import (
    MediaGen,
    _invalidate_media_gen_config,
)
from agent_cascade.tools.media_backends import resolve_backend
from agent_cascade.tools.media_backends.base import MediaRequest
from agent_cascade.tools.media_backends.comfyui import ComfyUIBackend
from agent_cascade.tools.media_backends.comfyui import (
    _find_newest_output,
    _find_save_node_prefix,
    _split_prefix,
    _comfyui_generate_to_file,
)


@pytest.fixture(autouse=True)
def _bust_config_cache():
    _invalidate_media_gen_config()
    yield
    _invalidate_media_gen_config()


# ── helpers ──────────────────────────────────────────────────────────────────

def _video_cfg(**over):
    cfg = {
        'comfyui': {
            'url': 'http://127.0.0.1:8188',
            'workflow_dir': r'C:\workflows',
            'default_workflow': r'C:\workflows\video_wan2_2_14B_i2v.json',
            'output_dir': r'C:\comfyui_out',
            'timeout': 600,
            'media_types': {'image': [], 'video': ['video_wan2_2_14B_i2v'],
                            'audio': []},
        },
        'sdcpp': {'binary': r'C:\bin\sd-cli.exe', 'media_types': {'image': []}},
    }
    cfg.update(over)
    return cfg


def _req(**over):
    req = MediaRequest(
        media_type='', backend_id='AUTO', config_name=None,
        prompt='the camera orbits around the face', negative_prompt='',
        config=_video_cfg(), agent_pool=None,
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


# A minimal API-format WAN workflow fragment for injection tests.
_WAN_WF = {
    '97': {'inputs': {'image': 'Anima_00001_.png'}, 'class_type': 'LoadImage'},
    '108': {'inputs': {'filename_prefix': 'video/Wan2.2_i2v', 'format': 'auto',
                       'video': ['129_170', 0]}, 'class_type': 'SaveVideo'},
    '129_93': {'inputs': {'text': 'orig', 'clip': ['129_84', 0]},
               'class_type': 'CLIPTextEncode', '_meta': {'title': 'CLIP Text Encode (Positive Prompt)'}},
    '129_89': {'inputs': {'text': 'orig neg', 'clip': ['129_84', 0]},
               'class_type': 'CLIPTextEncode', '_meta': {'title': 'CLIP Text Encode (Negative Prompt)'}},
    '129_98': {'inputs': {'width': 640, 'height': 640, 'length': ['129_163', 1],
                          'batch_size': 1, 'positive': ['129_93', 0],
                          'negative': ['129_89', 0], 'start_image': ['97', 0]},
               'class_type': 'WanImageToVideo'},
}


# ── extraction logic (pure, tmp files) ───────────────────────────────────────

class TestVideoExtraction:
    def test_split_prefix(self):
        assert _split_prefix('video/Wan2.2_i2v') == ('video', 'Wan2.2_i2v')
        assert _split_prefix('audio/YuE2') == ('audio', 'YuE2')
        assert _split_prefix('plain') == ('', 'plain')

    def test_find_save_node_prefix(self):
        assert _find_save_node_prefix(_WAN_WF) == 'video/Wan2.2_i2v'

    def test_find_newest_output_picks_new_file(self, tmp_path):
        out = tmp_path / 'video'
        out.mkdir()
        old = out / 'Wan2.2_i2v_0001_.mp4'
        old.write_bytes(b'old')
        new = out / 'Wan2.2_i2v_0002_.mp4'
        new.write_bytes(b'new-video-bytes')
        # 'old' existed before the run; 'new' is the fresh output.
        found = _find_newest_output(str(tmp_path), 'video/Wan2.2_i2v',
                                   submit_time=0.0, preexisting={old.name})
        assert found is not None
        data, ext = found
        assert data == b'new-video-bytes'
        assert ext == 'mp4'  # detected at runtime, never assumed

    def test_find_newest_output_runtime_ext(self, tmp_path):
        # A different extension is detected, not hardcoded.
        out = tmp_path / 'video'
        out.mkdir()
        f = out / 'Wan2.2_i2v_0001_.webm'
        f.write_bytes(b'webm-bytes')
        found = _find_newest_output(str(tmp_path), 'video/Wan2.2_i2v',
                                   submit_time=0.0, preexisting=set())
        assert found == (b'webm-bytes', 'webm')

    def test_find_newest_output_none_when_dir_missing(self, tmp_path):
        assert _find_newest_output(str(tmp_path / 'nope'), 'video/X',
                                  submit_time=0.0, preexisting=set()) is None

    def test_find_newest_output_none_when_no_match(self, tmp_path):
        out = tmp_path / 'video'
        out.mkdir()
        (out / 'other.mp4').write_bytes(b'x')
        assert _find_newest_output(str(tmp_path), 'video/Wan2.2_i2v',
                                  submit_time=0.0, preexisting=set()) is None


# ── full submit→poll→extract with mocked httpx ───────────────────────────────

class TestVideoGenerateToFile:
    def test_generate_to_file_reads_written_file(self, tmp_path):
        out = tmp_path / 'video'
        out.mkdir()
        produced = out / 'Wan2.2_i2v_0001_.mp4'
        produced.write_bytes(b'generated-video')

        def handler(request: httpx.Request):
            if request.url.path == '/prompt':
                return httpx.Response(200, json={'prompt_id': 'pid-1'})
            if request.url.path.startswith('/history/'):
                return httpx.Response(200, json={
                    'pid-1': {'status': {'completed': True}, 'outputs': {}}})
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        data, ext, meta = _comfyui_generate_to_file(
            'http://127.0.0.1:8188', _WAN_WF, str(tmp_path), timeout=10,
            client=client,
        )
        assert data == b'generated-video'
        assert ext == 'mp4'

    def test_generate_to_file_no_output_raises(self, tmp_path):
        # Completed but no file written → clear error.
        def handler(request: httpx.Request):
            if request.url.path == '/prompt':
                return httpx.Response(200, json={'prompt_id': 'pid-1'})
            if request.url.path.startswith('/history/'):
                return httpx.Response(200, json={
                    'pid-1': {'status': {'completed': True}, 'outputs': {}}})
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(RuntimeError) as e:
            _comfyui_generate_to_file(
                'http://127.0.0.1:8188', _WAN_WF, str(tmp_path / 'empty'),
                timeout=10, client=client)
        assert 'no output file was found' in str(e.value)

    def test_generate_to_file_no_save_node_raises(self):
        wf = {'1': {'inputs': {}, 'class_type': 'KSampler'}}
        with pytest.raises(RuntimeError) as e:
            _comfyui_generate_to_file('http://x', wf, '/tmp', timeout=10,
                                      client=httpx.Client())
        assert 'No disk-save node' in str(e.value)


# ── preflight: video injection + output contract ─────────────────────────────

class TestVideoPreflight:
    def test_preflight_records_output_contract(self):
        req = _req(config_name=r'C:\workflows\video_wan2_2_14B_i2v.json',
                   backend_id='comfyui', media_type='video')
        with patch('agent_cascade.tools.media_backends.comfyui._load_workflow',
                   return_value=json.loads(json.dumps(_WAN_WF))), \
             patch('agent_cascade.tools.media_backends.comfyui._inject_params',
                   return_value=(_WAN_WF, [])), \
             patch('agent_cascade.tools.media_backends.comfyui._inject_sampler_params',
                   return_value=({}, [])):
            ComfyUIBackend().preflight(req)
        assert req.prepared['fetch'] == 'server_file'
        assert req.prepared['output_dir'] == r'C:\comfyui_out'
        assert req.prepared['filename_prefix'] == 'video/Wan2.2_i2v'

    def test_preflight_requires_output_dir(self):
        req = _req(config_name=r'C:\workflows\video_wan2_2_14B_i2v.json',
                   backend_id='comfyui', media_type='video')
        req.config = {**_video_cfg(), 'comfyui': {**_video_cfg()['comfyui'],
                                                  'output_dir': ''}}
        with patch('agent_cascade.tools.media_backends.comfyui._load_workflow',
                   return_value=json.loads(json.dumps(_WAN_WF))), \
             patch('agent_cascade.tools.media_backends.comfyui._inject_params',
                   return_value=(_WAN_WF, [])), \
             patch('agent_cascade.tools.media_backends.comfyui._inject_sampler_params',
                   return_value=({}, [])):
            with pytest.raises(RuntimeError) as e:
                ComfyUIBackend().preflight(req)
        assert 'No comfyui.output_dir configured' in str(e.value)

    def test_reference_file_wires_loadimage(self):
        req = _req(config_name=r'C:\workflows\video_wan2_2_14B_i2v.json',
                   backend_id='comfyui', media_type='video', reference_file=r'C:\inside\first.png')
        req.agent_pool = MagicMock()
        resolved = MagicMock()
        resolved.is_file.return_value = True
        wf = json.loads(json.dumps(_WAN_WF))
        with patch('agent_cascade.tools.media_backends.comfyui.resolve_tool_path',
                   return_value=resolved), \
             patch('agent_cascade.tools.media_backends.comfyui._load_workflow',
                   return_value=wf), \
             patch('agent_cascade.tools.media_backends.comfyui._inject_params',
                   return_value=(wf, [])), \
             patch('agent_cascade.tools.media_backends.comfyui._inject_sampler_params',
                   return_value=({}, [])), \
             patch('agent_cascade.tools.media_backends.comfyui._upload_image',
                   return_value='uploaded_first_frame'):
            ComfyUIBackend().preflight(req)
        assert req.prepared['workflow']['97']['inputs']['image'] == 'uploaded_first_frame'


# ── VRAM slotting for the video path ─────────────────────────────────────────

class TestVideoVRAM:
    def test_order_save_unload_run_restore(self, tmp_path):
        tool = MediaGen()
        order = []
        out = tmp_path / 'video'
        out.mkdir()
        (out / 'Wan2.2_i2v_0001_.mp4').write_bytes(b'vid')

        def _save(inst):
            order.append('save'); return True

        def _unload(base):
            order.append('unload'); return True

        def _restore(inst, held_endpoint_cfg=None):
            order.append('restore'); return True

        def _gen(url, workflow, output_dir, timeout, client=None):
            order.append('run')
            return b'vid', 'mp4', {}

        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=_video_cfg()), \
              patch('agent_cascade.tools.media_backends.comfyui._load_workflow',
                    return_value=json.loads(json.dumps(_WAN_WF))), \
              patch('agent_cascade.tools.media_backends.comfyui._inject_params',
                    return_value=(_WAN_WF, [])), \
              patch('agent_cascade.tools.media_backends.comfyui._inject_sampler_params',
                    return_value=({}, [])), \
              patch('agent_cascade.tools.media_backends.comfyui._comfyui_generate_to_file',
                    side_effect=_gen), \
              patch('agent_cascade.tools.media_gen.save_media_to_media', return_value='/tmp/media/v.mp4'), \
              patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
              patch('agent_cascade.state_ops.save_instance_state', side_effect=_save), \
              patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload), \
              patch('agent_cascade.state_ops.restore_instance_state', side_effect=_restore):
            result = tool.call({'prompt': 'orbit the face', 'backend': 'comfyui',
                                'config': r'C:\workflows\video_wan2_2_14B_i2v.json',
                                'media_type': 'video'})

        assert order == ['save', 'unload', 'run', 'restore']
        assert result[0].video == '/tmp/media/v.mp4'
        assert 'backend=comfyui' in result[1].text


# ── LIVE WAN integration (deferred; gated) ───────────────────────────────────

@pytest.mark.skipif(os.environ.get('AGENT_CASCADE_RUN_LOCAL_TESTS') != '1',
                    reason='Live WAN i2v run requires a real ComfyUI GPU server')
def test_live_wan_integration():
    """Opt-in real run. Skipped unless AGENT_CASCADE_RUN_LOCAL_TESTS=1."""
    cfg = _video_cfg()
    req = _req(config_name=r'C:\workflows\video_wan2_2_14B_i2v.json',
               backend_id='comfyui')
    backend = resolve_backend(req)
    backend.preflight(req)
    res = backend.run(req)
    assert res.data and res.ext in ('mp4', 'webm', 'mkv')
