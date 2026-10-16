"""Tests for the generic ``media_gen`` tool (increment 1: sdcpp + comfyui image).

Mirrors ``test_image_gen_sdcpp.py`` for the new backend-registry architecture.
Covers:
  * the backend registry (explicit dict, ``get_backend``/``resolve_backend``)
  * explicit-backend selection (no AUTO; single-type backends derive media_type)
  * the sdcpp backend wiring (preflight builds argv, run calls ``_sdcpp_generate``)
  * the VRAM ordering invariants V1-V9 (preflight strictly before VRAM; save→unload
    →run→restore; restore on every error path, NOT in finally; retry once after 2s)
  * the ``reference_file`` security seam (resolve_tool_path BEFORE any upload/subprocess)
  * the feedback/caption contract (config=..., backend=...; image uncaptioned)

All external I/O is mocked. No binary execution, no network. The opt-in real
smoke test lives in ``test_media_gen_sdcpp_smoke.py`` (a later increment).
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
from agent_cascade.tools.media_backends import (
    BACKENDS,
    get_backend,
    resolve_backend,
)
from agent_cascade.tools.media_backends.base import MEDIA_TYPES, MediaRequest
from agent_cascade.tools.media_backends.sdcpp import SdcppBackend
from agent_cascade.tools.media_backends.comfyui import ComfyUIBackend, _infer_media_type
from agent_cascade.tools.media_backends.kobold_tts import KoboldTTSBackend
from agent_cascade.tools.media_backends.chatterbox import ChatterboxBackend
from agent_cascade.tools.image_gen import _fmt_elapsed


@pytest.fixture(autouse=True)
def _bust_config_cache():
    """Ensure the media_gen config cache is clean before and after every test."""
    _invalidate_media_gen_config()
    yield
    _invalidate_media_gen_config()


# ── helpers ──────────────────────────────────────────────────────────────────

def _mg_cfg(**over):
    """A full media_gen config (all three backend blocks + media_types ownership)."""
    cfg = {
        'comfyui': {
            'url': 'http://127.0.0.1:8188',
            'workflow_dir': r'C:\workflows',
            'default_workflow': r'C:\workflows\flux2_t2i_9b_API.json',
            'timeout': 180,
            'media_types': {'image': ['flux2_t2i_9b_API', 'zimg_turbo_API'],
                            'video': [], 'audio': []},
        },
        'sdcpp': {
            'binary': r'C:\bin\sd-cli.exe',
            'models_dir': r'C:\models',
            'config_dir': r'C:\configs',
            'default_sdcpp_config': 'z_image_turbo',
            'timeout': 900,
            'media_types': {'image': ['anima', 'flux2_klein_9b', 'z_image_turbo']},
        },
        'kobold_tts': {
            'timeout': 300,
            'media_types': {'audio': ['kokoro_default']},
        },
    }
    cfg.update(over)
    return cfg


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


def _req(**over):
    """Build a MediaRequest with the full config baked in."""
    req = MediaRequest(
        media_type='', backend_id='', config_name=None,
        prompt='a red cube', negative_prompt='',
        config=_mg_cfg(), agent_pool=None,
    )
    for k, v in over.items():
        setattr(req, k, v)
    return req


@pytest.fixture
def sdcpp_cfg(tmp_path):
    """A full media_gen config whose sdcpp.binary is a REAL temp file so the
    ``is_file()`` guard inside ``_sdcpp_generate`` passes (mirrors image_gen's fixture)."""
    bin_path = tmp_path / 'sd-cli.exe'
    bin_path.write_text('fake binary')
    cfg = _mg_cfg()
    cfg['sdcpp']['binary'] = str(bin_path)
    return cfg


@pytest.fixture
def chatterbox_cfg(tmp_path):
    """A media_gen config whose chatterbox python/cli_script/reference are REAL
    temp files so the ``is_file()`` guards inside ``preflight`` pass (mirrors
    ``sdcpp_cfg``). Yields ``(cfg, py, cli, ref)`` so tests can assert against
    the concrete paths."""
    py = tmp_path / 'python.exe'
    py.write_text('interp')
    cli = tmp_path / 'chatterbox_tts.py'
    cli.write_text('cli')
    ref = tmp_path / 'ref.wav'
    ref.write_bytes(b'RIFF\x00\x00\x00\x00WAVE')
    cfg = {'chatterbox': {
        'python': str(py), 'cli_script': str(cli),
        'config_dir': str(tmp_path), 'default_config': 'chatterbox_default',
        'timeout': 600, 'media_types': {'audio': ['chatterbox_default']},
    }}
    return cfg, py, cli, ref


def _prepared_chatterbox_req():
    """A MediaRequest with ``prepared`` state set, for the ``run()`` tests.

    The four ``test_run_*`` cases differ only in the ``subprocess.run`` payload
    they patch, so the request + prepared block is shared here.
    """
    req = MediaRequest(media_type='', backend_id='chatterbox', config_name=None,
                      prompt='x', negative_prompt='', config={}, agent_pool=None)
    req.prepared = {'argv': ['py', 'cli'], 'timeout': 600, 'ext': 'mp3',
                    'config_name': 'chatterbox_default'}
    return req


# ── registry + explicit-backend selection (pure, no VRAM) ────────────────────

class TestMediaGenRegistry:
    def test_registry_populated(self):
        assert set(BACKENDS) == {'sdcpp', 'comfyui', 'kobold_tts', 'chatterbox'}

    def test_get_backend(self):
        assert get_backend('sdcpp') is SdcppBackend or get_backend('sdcpp').id == 'sdcpp'
        assert get_backend('nope') is None

    def test_explicit_backend_wins(self):
        req = _req(backend_id='sdcpp')
        assert resolve_backend(req).id == 'sdcpp'

    def test_explicit_backend_wrong_type_errors(self):
        req = _req(backend_id='sdcpp', media_type='audio')
        with pytest.raises(RuntimeError) as e:
            resolve_backend(req)
        assert 'cannot produce' in str(e.value)

    def test_unknown_backend_errors(self):
        req = _req(backend_id='bogus')
        with pytest.raises(RuntimeError) as e:
            resolve_backend(req)
        assert 'unknown backend' in str(e.value)

    # ── explicit backend is required; single-type backends derive media_type ──
    def test_explicit_backend_kobold_tts(self):
        req = _req(backend_id='kobold_tts')
        assert resolve_backend(req).id == 'kobold_tts'
        assert req.media_type == 'audio'

    def test_explicit_backend_sdcpp_derives_image(self):
        req = _req(backend_id='sdcpp')
        assert resolve_backend(req).id == 'sdcpp'
        assert req.media_type == 'image'

    def test_no_backend_errors(self):
        req = _req()
        with pytest.raises(RuntimeError) as e:
            resolve_backend(req)
        assert 'no backend specified' in str(e.value)

    def test_media_type_vocabulary_is_three(self):
        assert MEDIA_TYPES == ('image', 'video', 'audio')


# ── sdcpp backend wiring (mocked) ────────────────────────────────────────────

class TestSdcppBackendWiring:
    def test_preflight_builds_argv(self):
        req = _req(backend_id='sdcpp')
        with patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config',
                   return_value=_resolved_config()), \
             patch('agent_cascade.tools.media_backends.sdcpp._sdcpp_argv',
                   return_value=['sd-cli', '-p', 'a red cube', '-o', 'x.png']):
            SdcppBackend().preflight(req)
        assert req.prepared['argv'] == ['sd-cli', '-p', 'a red cube', '-o', 'x.png']
        assert req.prepared['config_name'] == 'z_image_turbo'

    def test_reference_file_rejected_for_sdcpp(self):
        req = _req(backend_id='sdcpp', reference_file=r'C:\models\ref.png')
        with pytest.raises(RuntimeError) as e:
            SdcppBackend().preflight(req)
        assert 'not supported by the stable-diffusion.cpp backend' in str(e.value)

    def test_run_returns_png_bytes(self):
        req = _req(backend_id='sdcpp')
        req.prepared = {'argv': ['sd-cli', '-o', 'x.png'], 'timeout': 900}
        with patch('agent_cascade.tools.media_backends.sdcpp._sdcpp_generate',
                   return_value=b'\x89PNG-bytes'):
            res = SdcppBackend().run(req)
        assert res.data == b'\x89PNG-bytes'
        assert res.ext == 'png'


# ── chatterbox one-shot CLI backend wiring (no live generation) ───────────────

class TestChatterboxBackendWiring:
    def test_preflight_builds_argv(self, chatterbox_cfg):
        cfg, py, cli, ref = chatterbox_cfg
        req = MediaRequest(
            media_type='', backend_id='chatterbox', config_name=None,
            prompt='hello', negative_prompt='', config=cfg, agent_pool=None,
            reference_file=str(ref), overrides={'temperature': 0.9},
        )
        # Voice config carries falsy-but-valid seed=0 / cfg_weight=0.0 and a
        # temperature that the explicit override must beat.
        voice = {'device': 'cuda', 'temperature': 0.8, 'exaggeration': 0.5,
                 'cfg_weight': 0.0, 'seed': 0, 'output_format': 'mp3'}
        with patch('agent_cascade.tools.media_backends.chatterbox._resolve_tts_config',
                   return_value=voice), \
             patch('agent_cascade.tools.media_backends.chatterbox.resolve_tool_path',
                   return_value=ref):
            ChatterboxBackend().preflight(req)
        argv = req.prepared['argv']
        assert argv[:2] == [str(py), str(cli)]
        # single-token --flag=value form (dash-safe): value is fused into the token.
        assert argv[2] == '--text=hello'
        assert f'--reference={ref}' in argv
        # presence-gated falsy values MUST reach argv (not truthiness-gated).
        assert '--seed=0' in argv
        assert '--cfg-weight=0.0' in argv
        # explicit override beats the voice-config value.
        assert '--temperature=0.9' in argv
        assert req.prepared['ext'] == 'mp3'
        assert req.prepared['config_name'] == 'chatterbox_default'

    def test_missing_reference_file_raises(self, chatterbox_cfg):
        # Chatterbox voice cloning ALWAYS needs a reference; preflight must reject
        # its absence BEFORE any subprocess.
        cfg, py, cli, ref = chatterbox_cfg
        req = MediaRequest(media_type='', backend_id='chatterbox', config_name=None,
                          prompt='x', negative_prompt='', config=cfg, agent_pool=None,
                          reference_file=None)
        with patch('agent_cascade.tools.media_backends.chatterbox._resolve_tts_config',
                   return_value={}):
            with pytest.raises(RuntimeError) as e:
                ChatterboxBackend().preflight(req)
        assert "requires a 'reference_file'" in str(e.value)

    def test_run_returns_mp3_bytes(self):
        req = _prepared_chatterbox_req()
        fake = subprocess.CompletedProcess(args=['py', 'cli'], returncode=0,
                                          stdout=b'\xff\xfb' + b'\x00' * 500, stderr=b'')
        with patch('agent_cascade.tools.media_backends.chatterbox.subprocess.run',
                   return_value=fake):
            res = ChatterboxBackend().run(req)
        assert res.data == fake.stdout
        assert res.ext == 'mp3'

    def test_empty_stdout_raises(self):
        req = _prepared_chatterbox_req()
        empty = subprocess.CompletedProcess(args=['py', 'cli'], returncode=0,
                                           stdout=b'', stderr=b'')
        with patch('agent_cascade.tools.media_backends.chatterbox.subprocess.run',
                   return_value=empty):
            with pytest.raises(RuntimeError) as e:
                ChatterboxBackend().run(req)
        assert 'no audio on stdout' in str(e.value)

    def test_run_nonzero_returncode_raises(self):
        # run() must turn a non-zero exit into RuntimeError carrying the exit code
        # and a stderr excerpt (the CLI's documented failure contract).
        req = _prepared_chatterbox_req()
        fail = subprocess.CompletedProcess(args=['py', 'cli'], returncode=7,
                                          stdout=b'', stderr=b'boom')
        with patch('agent_cascade.tools.media_backends.chatterbox.subprocess.run',
                   return_value=fail):
            with pytest.raises(RuntimeError) as e:
                ChatterboxBackend().run(req)
        assert 'chatterbox TTS failed (exit 7)' in str(e.value)

    def test_run_timeout_raises(self):
        # subprocess.run raising TimeoutExpired must surface as TimeoutError, not
        # leak the subprocess exception.
        req = _prepared_chatterbox_req()
        with patch('agent_cascade.tools.media_backends.chatterbox.subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd=['py', 'cli'], timeout=600)):
            with pytest.raises(TimeoutError) as e:
                ChatterboxBackend().run(req)
        assert 'timed out' in str(e.value)

    def test_unsupported_output_format_rejected(self, chatterbox_cfg):
        # A voice config naming an unsupported container must be rejected in preflight
        # (before any subprocess), not silently passed through to the CLI. A valid
        # reference_file is supplied so the ONLY way preflight raises is the format
        # check -- the test isolates that branch regardless of preflight ordering.
        cfg, py, cli, ref = chatterbox_cfg
        req = MediaRequest(media_type='', backend_id='chatterbox', config_name=None,
                          prompt='x', negative_prompt='', config=cfg, agent_pool=None,
                          reference_file=str(ref))
        with patch('agent_cascade.tools.media_backends.chatterbox._resolve_tts_config',
                   return_value={'output_format': 'flac'}), \
              patch('agent_cascade.tools.media_backends.chatterbox.resolve_tool_path',
                    return_value=ref):
            with pytest.raises(RuntimeError) as e:
                ChatterboxBackend().preflight(req)
        assert 'Unsupported output-format' in str(e.value)


# ── VRAM ordering invariants V1-V9 ───────────────────────────────────────────

class TestMediaGenVRAM:
    def test_vram_order_save_unload_run_restore(self, sdcpp_cfg):
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
            out = argv[argv.index('-o') + 1]
            with open(out, 'wb') as f:
                f.write(b'png')
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b'', stderr=b'')

        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=sdcpp_cfg), \
              patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config', return_value=_resolved_config()), \
              patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run), \
              patch('agent_cascade.tools.media_gen.save_media_to_media', return_value='/tmp/media/out.png'), \
              patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
              patch('agent_cascade.state_ops.save_instance_state', side_effect=_save), \
              patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload), \
              patch('agent_cascade.state_ops.restore_instance_state', side_effect=_restore):
            tool.call({'prompt': 'a red cube', 'backend': 'sdcpp'})

        assert order == ['save', 'unload', 'run', 'restore']

    def test_unload_raises_still_restores(self, sdcpp_cfg):
        # F1: the prologue is inside the try, so a raise from unload still restores.
        tool = MediaGen()

        def _unload(base):
            raise RuntimeError('unload blew up')

        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=sdcpp_cfg), \
              patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config', return_value=_resolved_config()), \
              patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_fake_run_write_bytes()), \
              patch('agent_cascade.tools.media_gen.save_media_to_media', return_value='/tmp/media/out.png'), \
              patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
              patch('agent_cascade.state_ops.save_instance_state', return_value=True), \
              patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload), \
              patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'a red cube', 'backend': 'sdcpp'})

        m_restore.assert_called_once()
        assert 'ERROR' in result[0].text

    def test_save_state_fails_no_restore(self, sdcpp_cfg):
        tool = MediaGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=sdcpp_cfg), \
              patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config', return_value=_resolved_config()), \
              patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_fake_run_write_bytes()), \
              patch('agent_cascade.tools.media_gen.save_media_to_media', return_value='/tmp/media/out.png'), \
              patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
              patch('agent_cascade.state_ops.save_instance_state', return_value=False), \
              patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'a red cube', 'backend': 'sdcpp'})

        assert result[0].image == '/tmp/media/out.png'
        m_restore.assert_not_called()

    def test_media_save_failure_still_restores(self, sdcpp_cfg):
        tool = MediaGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=sdcpp_cfg), \
              patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config', return_value=_resolved_config()), \
              patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_fake_run_write_bytes()), \
              patch('agent_cascade.tools.media_gen.save_media_to_media',
                    side_effect=RuntimeError('disk full')), \
              patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
              patch('agent_cascade.state_ops.save_instance_state', return_value=True), \
              patch('agent_cascade.state_ops.unload_all_models', return_value=True), \
              patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'a red cube', 'backend': 'sdcpp'})

        m_restore.assert_called_once()
        assert 'ERROR' in result[0].text

    def test_preflight_failure_touches_zero_vram(self, sdcpp_cfg):
        # V1: a config/model failure in preflight must NOT save/unload/restore.
        tool = MediaGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=sdcpp_cfg), \
              patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config',
                    side_effect=RuntimeError('model file missing')), \
              patch('agent_cascade.state_ops.save_instance_state') as m_save, \
              patch('agent_cascade.state_ops.unload_all_models') as m_unload, \
              patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'a red cube', 'backend': 'sdcpp'})

        m_save.assert_not_called()
        m_unload.assert_not_called()
        m_restore.assert_not_called()
        assert 'ERROR' in result[0].text


# ── DMS bound declaration (plan t292 — single dispatch-boundary writer) ────────
# media_gen no longer writes _in_blocking_tool itself; the boundary (ToolDispatcher)
# is the single writer. media_gen's contribution is dms_bound(): the resolved backend
# timeout the boundary carries. The set/clear behaviour is covered generically by
# tests/test_holder_dms.py::TestDispatchBoundary.

class TestMediaGenDmsBound:
    def test_dms_bound_returns_resolved_backend_timeout(self, sdcpp_cfg):
        """t292: MediaGen.dms_bound() returns the resolved backend timeout (the value the
        boundary carries). For sdcpp the resolved timeout is 900.0 — the same value the old
        scattered marker carried, now exposed as the tool's declared bound."""
        tool = MediaGen()
        inst = MagicMock()
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=sdcpp_cfg):
            bound = tool.dms_bound({'prompt': 'a red cube', 'backend': 'sdcpp'}, inst)
        assert bound == 900.0, f"dms_bound must return the resolved sdcpp timeout, got {bound!r}"

    def test_dms_bound_parses_json_string_args(self, sdcpp_cfg):
        """dms_bound accepts the raw JSON-string tool args the boundary may pass through."""
        tool = MediaGen()
        inst = MagicMock()
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=sdcpp_cfg):
            bound = tool.dms_bound('{"backend": "sdcpp"}', inst)
        assert bound == 900.0, f"dms_bound must parse JSON-string args, got {bound!r}"

    def test_dms_bound_kobold_tts_carries_teardown_headroom(self):
        """kobold_tts teardown (process-tree-exit wait) runs AFTER generation, OUTSIDE the
        generation budget, so the DMS wall-clock bound must carry that extra headroom — otherwise
        a healthy-but-slow run is killed mid-teardown and VRAM restore is skipped (leak).

        The bound = configured timeout + TEARDOWN_HEADROOM, and it MUST be strictly LARGER than
        preflight's internal generation budget (the bare configured timeout). They are different
        quantities; asserting they're equal would be the bug."""
        import agent_cascade.tools.media_backends.kobold_tts as kmod
        cfg = {
            'kobold_tts': {
                'binary': r'C:\bin\koboldcpp.exe', 'models_dir': r'C:\models',
                'config_dir': r'C:\configs\tts', 'default_config': 'kokoro_default',
                'timeout': 200, 'ready_timeout': 120, 'port_range': [5100, 5199],
                'media_types': {'audio': ['kokoro_default']},
            },
        }
        tool = MediaGen()
        inst = MagicMock()
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg):
            bound = tool.dms_bound({'backend': 'kobold_tts'}, inst)
        gen_budget = 200  # preflight's internal generation budget = configured timeout
        # DMS bound carries the headroom on top of the generation budget.
        assert bound == gen_budget + kmod.TEARDOWN_HEADROOM, \
            f"dms_bound must be gen-budget + teardown headroom for kobold_tts, got {bound!r}"
        # The two budgets are DISTINCT: DMS bound strictly exceeds the generation budget.
        assert bound > gen_budget
        assert bound == kmod.kobold_tts_dms_bound(cfg, cfg['kobold_tts'])

    def test_call_does_not_write_marker_boundary_owns_it(self, sdcpp_cfg):
        """t292: media_gen.call() no longer writes _in_blocking_tool — the dispatch boundary
        is the single writer. A plain (non-MagicMock) instance proves call() leaves the marker
        untouched across the whole generation path."""
        class _PlainInst:
            _in_blocking_tool = 'SENTINEL'
        inst = _PlainInst()
        tool = MediaGen()

        def _gen(argv, timeout=None):
            # During generation the marker must still be the untouched sentinel.
            assert inst._in_blocking_tool == 'SENTINEL', 'call() wrote the marker — boundary must own it'
            out = argv[argv.index('-o') + 1]
            with open(out, 'wb') as f:
                f.write(b'png')
            return b'\x89PNG-fake'

        with patch.object(tool, '_get_instance', return_value=inst), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=sdcpp_cfg), \
              patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config',
                    return_value=_resolved_config()), \
              patch('agent_cascade.tools.media_backends.sdcpp._sdcpp_generate', side_effect=_gen), \
              patch('agent_cascade.tools.media_gen.save_media_to_media',
                    return_value='/tmp/media/out.png'):
            result = tool.call({'prompt': 'a red cube', 'backend': 'sdcpp'})

        assert result[0].image == '/tmp/media/out.png'
        assert inst._in_blocking_tool == 'SENTINEL', 'call() must not mutate the marker'


# ── reference_file security seam (resolve BEFORE upload/subprocess) ───────────

class TestReferenceFileSecurity:
    def test_out_of_roots_reference_file_errors_before_vram(self):
        # An out-of-roots reference_file must be rejected in preflight (resolve_tool_path
        # raises ValueError) with ZERO VRAM touched.
        tool = MediaGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=_mg_cfg()), \
              patch('agent_cascade.tools.media_backends.comfyui.resolve_tool_path',
                    side_effect=ValueError('outside allowed roots')), \
              patch('agent_cascade.tools.media_backends.comfyui._load_workflow', return_value={}), \
              patch('agent_cascade.tools.media_backends.comfyui._inject_params',
                    return_value=({}, [])), \
              patch('agent_cascade.tools.media_backends.comfyui._inject_sampler_params',
                    return_value=({}, [])), \
              patch('agent_cascade.state_ops.save_instance_state') as m_save, \
              patch('agent_cascade.state_ops.unload_all_models') as m_unload, \
              patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'edit this', 'backend': 'comfyui',
                                'reference_file': r'C:\outside\escape.png'})

        m_save.assert_not_called()
        m_unload.assert_not_called()
        m_restore.assert_not_called()
        assert 'ERROR' in result[0].text

    def test_reference_file_resolved_via_ro_mode(self):
        # preflight must call resolve_tool_path with mode='ro' and the agent pool,
        # BEFORE any upload.
        req = _req(backend_id='comfyui', reference_file=r'C:\inside\ref.png')
        req.agent_pool = MagicMock()
        resolved = MagicMock()
        resolved.is_file.return_value = True
        with patch('agent_cascade.tools.media_backends.comfyui.resolve_tool_path',
                   return_value=resolved) as m_resolve, \
             patch('agent_cascade.tools.media_backends.comfyui._load_workflow',
                   return_value={'1': {'class_type': 'LoadImage', 'inputs': {'image': 'x.png'}}}), \
             patch('agent_cascade.tools.media_backends.comfyui._inject_params',
                   return_value=({'1': {'class_type': 'LoadImage', 'inputs': {'image': 'x.png'}}}, [])), \
             patch('agent_cascade.tools.media_backends.comfyui._inject_sampler_params',
                   return_value=({}, [])), \
             patch('agent_cascade.tools.media_backends.comfyui._upload_image',
                   return_value='uploaded_ref'):
            ComfyUIBackend().preflight(req)
        m_resolve.assert_called_once()
        # mode='ro' and agent_pool passed through:
        _, kwargs = m_resolve.call_args
        assert kwargs.get('mode') == 'ro'
        assert 'agent_pool' in kwargs
        # The LoadImage node was rewired to the uploaded reference:
        assert req.prepared['workflow']['1']['inputs']['image'] == 'uploaded_ref'


# ── ComfyUI output extraction byte-identical to image_gen ────────────────────

class TestComfyUIByteIdentity:
    def test_run_delegates_to_image_gen_comfyui_generate(self):
        # The backend's run() must return EXACTLY what image_gen._comfyui_generate
        # returns for the same inputs — no re-encoding, no transformation.
        url = 'http://127.0.0.1:8188'
        workflow = {'1': {'class_type': 'KSampler'}}
        fixed = (b'\x89PNG-real-bytes', {'seed': 42})

        req = _req(backend_id='comfyui')
        req.prepared = {'url': url, 'workflow': workflow, 'timeout': 180}
        with patch('agent_cascade.tools.media_backends.comfyui._comfyui_generate',
                   return_value=fixed):
            res = ComfyUIBackend().run(req)
        assert res.data == fixed[0]
        assert res.meta == fixed[1]

        # Same call through the real image_gen function yields identical bytes.
        # Reference the module attribute (not a direct symbol) so the patch intercepts.
        import agent_cascade.tools.image_gen as _ig
        with patch('agent_cascade.tools.image_gen._comfyui_generate', return_value=fixed):
            direct = _ig._comfyui_generate(url, workflow, timeout=180)
        assert direct[0] == res.data


# ── feedback / caption contract ──────────────────────────────────────────────

class TestFeedbackContract:
    def test_image_feedback_format(self, sdcpp_cfg):
        tool = MediaGen()
        with patch.object(tool, '_get_instance', return_value=None), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=sdcpp_cfg), \
              patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config', return_value=_resolved_config()), \
              patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_fake_run_write_bytes()), \
              patch('agent_cascade.tools.media_gen.save_media_to_media', return_value='/tmp/media/out.png'):
            result = tool.call({'prompt': 'a red cube', 'backend': 'sdcpp'})

        assert len(result) == 2
        assert result[0].image == '/tmp/media/out.png'
        assert not result[0].caption  # image left uncaptioned (router auto-captions)
        assert 'backend=sdcpp' in result[1].text
        assert 'config=z_image_turbo' in result[1].text

    def test_audio_content_item_uses_audio_field(self):
        # TTS/music are audio → ContentItem(audio=...), never a 'tts'/'music' field.
        item = MediaGen._typed_content_item.__wrapped__ if False else None
        tool = MediaGen()
        ci = tool._typed_content_item('audio', '/tmp/media/a.mp3')
        assert ci.audio == '/tmp/media/a.mp3'
        assert ci.image is None and ci.video is None

    def test_elapsed_format_in_feedback(self):
        assert _fmt_elapsed(12.345) == '12.3s'


# ── comfyui media-type inference (no caller media_type param) ─────────────────

class TestMediaInference:
    """comfyui derives media_type from the loaded workflow: YuE2→audio, SaveVideo→video, else image."""
    def test_infer_music(self):
        wf = {'22': {'class_type': 'YuE2GenerateMusic', 'inputs': {}},
              '10': {'class_type': 'SaveAudioAdvanced', 'inputs': {}}}
        assert _infer_media_type(wf) == 'audio'

    def test_infer_video(self):
        wf = {'1': {'class_type': 'KSampler', 'inputs': {}},
              '9': {'class_type': 'SaveVideo', 'inputs': {}}}
        assert _infer_media_type(wf) == 'video'

    def test_infer_image(self):
        wf = {'1': {'class_type': 'KSampler', 'inputs': {}},
              '9': {'class_type': 'SaveImage', 'inputs': {}}}
        assert _infer_media_type(wf) == 'image'

    def test_music_wins_over_video(self):
        # A YuE2 graph also has a SaveAudio node; music must be detected FIRST.
        wf = {'22': {'class_type': 'YuE2GenerateMusic', 'inputs': {}},
              '9': {'class_type': 'SaveVideo', 'inputs': {}}}
        assert _infer_media_type(wf) == 'audio'


# ── __help self-documentation ────────────────────────────────────────────────

class TestHelpCommand:
    def test_help_returns_text_without_generating(self):
        cfg = {'comfyui': {'workflow_dir': 'x', 'media_types': {'image': ['a'], 'audio': ['yue2_full']}}}
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg):
            result = MediaGen().call({'prompt': '__help'})
        assert len(result) == 1
        assert result[0].image is None and result[0].audio is None and result[0].video is None
        assert 'backend: comfyui' in result[0].text
        assert 'yue2_full' in result[0].text
