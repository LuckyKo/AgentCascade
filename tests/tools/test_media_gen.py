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
        'chatterbox': {
            'python': r'C:\py\python.exe',
            'cli_script': r'C:\cli\chatterbox_tts.py',
            'config_dir': r'C:\tts',
            'default_config': 'chatterbox_default',
            'timeout': 600,
            'media_types': {'audio': ['chatterbox_default']},
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


# The shared fixture must mirror what ``preflight`` guarantees — including the
# child-process contract keys (see the ``test_run_without_contract_keys_*`` case
# for the .get()→None inherit path that older/hand-built payloads take).
_CHATTERBOX_REPO = 'C:/fake/Chatterbox-TTS-Server'
_CHATTERBOX_HF_HOME = _CHATTERBOX_REPO + '/models'


def _prepared_chatterbox_req():
    """A MediaRequest with ``prepared`` state set, for the ``run()`` tests.

    The four ``test_run_*`` cases differ only in the ``subprocess.run`` payload
    they patch, so the request + prepared block is shared here. The block mirrors
    what ``preflight`` guarantees — argv/timeout/ext/config_name PLUS the child-process
    contract (``env``/``cwd``).
    """
    req = MediaRequest(media_type='', backend_id='chatterbox', config_name=None,
                      prompt='x', negative_prompt='', config={}, agent_pool=None)
    req.prepared = {'argv': ['py', 'cli'], 'timeout': 600, 'ext': 'mp3',
                    'config_name': 'chatterbox_default',
                    'cwd': _CHATTERBOX_REPO,
                    'env': {**os.environ, 'HF_HOME': _CHATTERBOX_HF_HOME,
                            'HF_HUB_CACHE': _CHATTERBOX_HF_HOME,
                            'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                            'HF_HUB_DISABLE_TELEMETRY': '1'}}
    return req


# ── registry + explicit-backend selection (pure, no VRAM) ────────────────────

class TestMediaGenRegistry:
    def test_registry_populated(self):
        assert set(BACKENDS) == {'sdcpp', 'comfyui', 'kobold_tts', 'chatterbox'}

    def test_manifest_matches_registry(self):
        """T12: composed manifest == registry, same order (guards the empty-manifest trap)."""
        from agent_cascade.tools.media_backends import BACKEND_MANIFEST, manifest_ids
        assert manifest_ids() == list(BACKENDS)
        assert set(BACKEND_MANIFEST) == set(BACKENDS)

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

    def test_server_url_capability_is_removed(self):
        """Capability removal: an agent must not be able to retarget a media backend's
        server per call (unvalidated SSRF + local-file exfiltration via ComfyUI upload).
        The drift guard pins key-set *equality*, so re-adding the key on BOTH sides
        satisfies it — the absence is pinned here.
        The live config key `comfyui.url` (FieldSpec) is a different thing and must stay."""
        from agent_cascade.prompts.dna import TOOL_METADATA
        from agent_cascade.tools.media_backends import BACKEND_MANIFEST
        assert 'server_url' not in MediaGen.parameters['properties']
        assert 'server_url' not in TOOL_METADATA['media_gen']['parameters']
        for m in BACKEND_MANIFEST.values():
            assert 'server_url' not in m['agent_args'], \
                f"server_url found in {m['label']} agent_args"


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
        # Child-process contract (the HF-cache fix): env/cwd are stashed alongside
        # argv so run() can hand them to subprocess.run. Default cache root is the
        # CLI script's parent dir + 'models' (mirrors the repo's own launcher).
        assert req.prepared['cwd'] == str(cli.parent)
        env = req.prepared['env']
        models = str(cli.parent / 'models')
        assert env['HF_HOME'] == models
        assert env['HF_HUB_CACHE'] == models
        assert env['HF_HUB_OFFLINE'] == '1'
        assert env['TRANSFORMERS_OFFLINE'] == '1'
        assert env['HF_HUB_DISABLE_TELEMETRY'] == '1'
        # env= REPLACES the child environment, so every host var we do not
        # deliberately override must survive intact (PATH is what pydub→FFmpeg
        # needs for the mp3 encode).
        overridden = {'HF_HOME', 'HF_HUB_CACHE', 'HF_HUB_OFFLINE',
                      'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY'}
        for k, v in os.environ.items():
            if k not in overridden:
                assert env[k] == v
        assert 'PATH' in env

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

    def test_default_voice_autosupplies_reference_file(self, chatterbox_cfg):
        # No reference_file passed, but voices_dir + default_voice are configured:
        # preflight must auto-supply the reference by joining voices_dir/default_voice
        # and hand THAT path to the SECURITY seam. The stub ignores its argument, so
        # assert_called_with proves the load-bearing join (not the stub's return value).
        from pathlib import Path
        cfg, py, cli, ref = chatterbox_cfg
        cfg['chatterbox']['voices_dir'] = str(ref.parent)
        cfg['chatterbox']['default_voice'] = ref.name
        req = MediaRequest(media_type='', backend_id='chatterbox', config_name=None,
                          prompt='x', negative_prompt='', config=cfg, agent_pool=None,
                          reference_file=None)
        with patch('agent_cascade.tools.media_backends.chatterbox._resolve_tts_config',
                   return_value={}), \
              patch('agent_cascade.tools.media_backends.chatterbox.resolve_tool_path',
                    return_value=ref) as rtp:
            ChatterboxBackend().preflight(req)
        # Assert the exact joined path was passed to resolve_tool_path (the
        # absolute-voices_dir branch), not the stub's return value.
        expected = str(Path(cfg['chatterbox']['voices_dir']) / cfg['chatterbox']['default_voice'])
        rtp.assert_called_once_with(expected, mode='ro', agent_pool=None)

    def test_default_voice_relative_voices_dir_resolves_project_root(self, chatterbox_cfg):
        # A RELATIVE voices_dir must resolve against the project root, not the CWD.
        # The stub ignores its argument, so assert_called_with proves the exact
        # string handed to the SECURITY seam (the load-bearing join, not the return).
        from agent_cascade.tools.media_backends.chatterbox import _project_root
        cfg, py, cli, ref = chatterbox_cfg
        cfg['chatterbox']['voices_dir'] = 'some/relative/voices'
        cfg['chatterbox']['default_voice'] = 'speaker_a.wav'
        expected = str(_project_root() / 'some/relative/voices' / 'speaker_a.wav')
        req = MediaRequest(media_type='', backend_id='chatterbox', config_name=None,
                          prompt='x', negative_prompt='', config=cfg, agent_pool=None,
                          reference_file=None)
        with patch('agent_cascade.tools.media_backends.chatterbox._resolve_tts_config',
                   return_value={}), \
              patch('agent_cascade.tools.media_backends.chatterbox.resolve_tool_path',
                    return_value=ref) as rtp:
            ChatterboxBackend().preflight(req)
        rtp.assert_called_once_with(expected, mode='ro', agent_pool=None)

    def test_auto_supplied_voice_outside_allowed_paths_names_config_keys(self, chatterbox_cfg):
        # The auto-supplied reference must clear the SECURITY seam. When voices_dir
        # (the fixture's tmp_path) is outside the allowed dirs, the error must name
        # the config keys — NOT reference_file, which the agent never passed.
        # resolve_tool_path is NOT patched: this drives the real agent_pool=None
        # fallback, which rejects any path outside DEFAULT_WORKSPACE.
        cfg, py, cli, ref = chatterbox_cfg
        cfg['chatterbox']['voices_dir'] = str(ref.parent)
        cfg['chatterbox']['default_voice'] = ref.name
        req = MediaRequest(media_type='', backend_id='chatterbox', config_name=None,
                          prompt='x', negative_prompt='', config=cfg, agent_pool=None,
                          reference_file=None)
        with patch('agent_cascade.tools.media_backends.chatterbox._resolve_tts_config',
                   return_value={}):
            with pytest.raises(RuntimeError) as e:
                ChatterboxBackend().preflight(req)
        msg = str(e.value)
        assert 'not an allowed path' in msg
        assert 'default_voice' in msg and 'voices_dir' in msg

    def test_no_reference_file_no_voices_dir_raises(self, chatterbox_cfg):
        # No reference_file AND no voices_dir configured: the error must name both
        # reference_file and the voices_dir/default_voice escape hatch.
        cfg, py, cli, ref = chatterbox_cfg
        req = MediaRequest(media_type='', backend_id='chatterbox', config_name=None,
                          prompt='x', negative_prompt='', config=cfg, agent_pool=None,
                          reference_file=None)
        with patch('agent_cascade.tools.media_backends.chatterbox._resolve_tts_config',
                   return_value={}):
            with pytest.raises(RuntimeError) as e:
                ChatterboxBackend().preflight(req)
        assert "requires a 'reference_file'" in str(e.value)
        assert 'voices_dir' in str(e.value)

    def test_no_reference_file_voices_dir_no_default_raises(self, chatterbox_cfg):
        # voices_dir set but no default_voice: the error must name default_voice and
        # list the audio files actually available in the directory.
        cfg, py, cli, ref = chatterbox_cfg
        cfg['chatterbox']['voices_dir'] = str(ref.parent)
        req = MediaRequest(media_type='', backend_id='chatterbox', config_name=None,
                          prompt='x', negative_prompt='', config=cfg, agent_pool=None,
                          reference_file=None)
        with patch('agent_cascade.tools.media_backends.chatterbox._resolve_tts_config',
                   return_value={}):
            with pytest.raises(RuntimeError) as e:
                ChatterboxBackend().preflight(req)
        assert 'default_voice' in str(e.value)
        assert 'ref.wav' in str(e.value)

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

    def test_run_forwards_env_and_cwd(self):
        # The contract must actually reach subprocess.run, not just sit in
        # ``prepared``: a kwargs-capturing fake proves what run() hands over.
        # Value checks live in the preflight test — here we only prove forwarding.
        req = _prepared_chatterbox_req()
        fake = subprocess.CompletedProcess(args=['py', 'cli'], returncode=0,
                                          stdout=b'\xff\xfb' + b'\x00' * 500,
                                          stderr=b'')
        with patch('agent_cascade.tools.media_backends.chatterbox.subprocess.run',
                   side_effect=lambda *a, **kw: fake) as run_mock:
            res = ChatterboxBackend().run(req)
        assert res.data == fake.stdout
        args, kwargs = run_mock.call_args
        assert args[0] == req.prepared['argv']    # argv stays positional
        assert kwargs['cwd'] == _CHATTERBOX_REPO
        assert kwargs['env'] is req.prepared['env']  # the same dict, unmodified

    def test_run_without_contract_keys_still_inherits(self):
        # The .get() fallback: a hand-built/older prepared payload that omits the
        # contract keys must still run — subprocess.run receives None for both,
        # which is its documented "inherit from the parent" behaviour.
        req = _prepared_chatterbox_req()
        req.prepared.pop('env')
        req.prepared.pop('cwd')
        fake = subprocess.CompletedProcess(args=['py', 'cli'], returncode=0,
                                          stdout=b'\xff\xfb' + b'\x00' * 500,
                                          stderr=b'')
        with patch('agent_cascade.tools.media_backends.chatterbox.subprocess.run',
                   side_effect=lambda *a, **kw: fake) as run_mock:
            res = ChatterboxBackend().run(req)
        assert res.data == fake.stdout
        _, kwargs = run_mock.call_args
        assert kwargs['env'] is None and kwargs['cwd'] is None

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


# ── release_model_around_call opt-out (W2 / T5-T7) ────────────────────────────

class TestReleaseModelFlag:
    """Per-backend VRAM opt-out. Absent + manifest default True == today's dance
    (pinned by TestMediaGenVRAM.test_vram_order); explicit false => zero calls."""

    def test_flag_false_zero_vram_calls(self, sdcpp_cfg):
        cfg = {k: dict(v) if isinstance(v, dict) else v for k, v in sdcpp_cfg.items()}
        cfg['sdcpp'] = dict(cfg['sdcpp'])
        cfg['sdcpp']['release_model_around_call'] = False
        tool = MediaGen()
        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg), \
              patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config', return_value=_resolved_config()), \
              patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_fake_run_write_bytes()), \
              patch('agent_cascade.tools.media_gen.save_media_to_media', return_value='/tmp/media/out.png'), \
              patch('agent_cascade.state_ops.save_instance_state') as m_save, \
              patch('agent_cascade.state_ops.unload_all_models') as m_unload, \
              patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'prompt': 'a red cube', 'backend': 'sdcpp'})
        m_save.assert_not_called()
        m_unload.assert_not_called()
        m_restore.assert_not_called()
        assert result[0].image == '/tmp/media/out.png'

    def test_flag_true_explicit_full_dance(self, sdcpp_cfg):
        cfg = {k: dict(v) if isinstance(v, dict) else v for k, v in sdcpp_cfg.items()}
        cfg['sdcpp'] = dict(cfg['sdcpp'])
        cfg['sdcpp']['release_model_around_call'] = True
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
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg), \
              patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config', return_value=_resolved_config()), \
              patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run), \
              patch('agent_cascade.tools.media_gen.save_media_to_media', return_value='/tmp/media/out.png'), \
              patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
              patch('agent_cascade.state_ops.save_instance_state', side_effect=_save), \
              patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload), \
              patch('agent_cascade.state_ops.restore_instance_state', side_effect=_restore):
            result = tool.call({'prompt': 'a red cube', 'backend': 'sdcpp'})
        assert order == ['save', 'unload', 'run', 'restore']
        assert result[0].image == '/tmp/media/out.png'

    def test_flag_is_per_block_not_global(self, sdcpp_cfg):
        """A neighbour block's false must not disable the caller's dance (T7)."""
        cfg = {k: dict(v) if isinstance(v, dict) else v for k, v in sdcpp_cfg.items()}
        cfg['chatterbox'] = dict(cfg['chatterbox'])
        cfg['chatterbox']['release_model_around_call'] = False
        tool = MediaGen()
        order = []

        def _save2(inst):
            order.append('save'); return True

        def _unload2(base):
            order.append('unload'); return True

        def _restore2(inst, held_endpoint_cfg=None):
            order.append('restore'); return True

        def _run2(argv, **kwargs):
            order.append('run')
            out = argv[argv.index('-o') + 1]
            with open(out, 'wb') as f:
                f.write(b'png')
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b'', stderr=b'')

        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg), \
              patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config', return_value=_resolved_config()), \
              patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run2), \
              patch('agent_cascade.tools.media_gen.save_media_to_media', return_value='/tmp/media/out.png'), \
              patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
              patch('agent_cascade.state_ops.save_instance_state', side_effect=_save2), \
              patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload2), \
              patch('agent_cascade.state_ops.restore_instance_state', side_effect=_restore2):
            result = tool.call({'prompt': 'a red cube', 'backend': 'sdcpp'})
        assert order == ['save', 'unload', 'run', 'restore']
        assert result[0].image == '/tmp/media/out.png'

    def test_pool_present_restore_uses_held_slot_helper(self, sdcpp_cfg):
        """F4: production path — with a live pool the restore MUST route through
        restore_held_slot_state(pool, instance, name, label=name), NOT the
        pool-None fallback. The held endpoint is resolved LIVE from the pool
        router (the carried 'held' dict is the SAVED endpoint and may be stale);
        the stable instance-name label survives a system agent consuming
        _state_label inside the window. Patching the helper (not HTTP) proves
        routing + arguments; the helper's own request behaviour is state_ops'
        contract, tested there."""
        tool = MediaGen(agent_pool=MagicMock(name='agent_pool'))
        inst = _fake_instance()
        inst.instance_name = 'caller_agent'
        order = []

        def _save(inst_):
            order.append('save'); return True

        def _unload(base):
            order.append('unload'); return True

        def _restore_held(pool, inst_, inst_name, label=None):
            order.append('restore')
            # The real helper resolves the held endpoint from pool.api_router;
            # assert it received the pool and the stable label, not a stale cfg.
            assert label == 'caller_agent' and inst_name == 'caller_agent'
            return True

        def _run(argv, **kwargs):
            order.append('run')
            out = argv[argv.index('-o') + 1]
            with open(out, 'wb') as f:
                f.write(b'png')
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b'', stderr=b'')

        held_restore = MagicMock(name='restore_held_slot_state')
        held_restore.side_effect = _restore_held
        with patch.object(tool, '_get_instance', return_value=inst), \
              patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=sdcpp_cfg), \
              patch('agent_cascade.tools.media_backends.sdcpp._resolve_sdcpp_config', return_value=_resolved_config()), \
              patch('agent_cascade.tools.image_gen.subprocess.run', side_effect=_run), \
              patch('agent_cascade.tools.media_gen.save_media_to_media', return_value='/tmp/media/out.png'), \
              patch('agent_cascade.state_ops.save_instance_state', side_effect=_save), \
              patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload), \
              patch('agent_cascade.state_ops.restore_held_slot_state', side_effect=held_restore), \
              patch('agent_cascade.state_ops.restore_instance_state') as fallback_restore:
            result = tool.call({'prompt': 'a red cube', 'backend': 'sdcpp'})

        assert order == ['save', 'unload', 'run', 'restore']
        assert result[0].image == '/tmp/media/out.png'
        held_restore.assert_called_once()
        args, kwargs = held_restore.call_args
        assert args[0] is not None                      # the pool is threaded through
        assert args[2] == 'caller_agent'
        assert kwargs.get('label') == 'caller_agent'    # stable label, not _state_label
        fallback_restore.assert_not_called()            # production path, not the fallback


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

    def test_audio_feedback_includes_voice_when_reference_present(self):
        # The auto-supplied/explicit voice identity (prepared['reference']) must
        # surface in the audio feedback line (the "missing voice feedback" fix).
        tool = MediaGen()
        text = tool._build_feedback(
            'audio', '/tmp/media/a.mp3', 'chatterbox',
            {'config_name': 'chatterbox_default', 'reference': 'af_Bella.wav'},
            1.23,
        )
        assert 'Generated audio:' in text
        assert 'backend=chatterbox' in text
        assert 'voice=af_Bella.wav' in text
        assert 'config=chatterbox_default' in text

    def test_audio_feedback_omits_voice_when_no_reference(self):
        # A backend without a 'reference' key (e.g. kobold_tts) must NOT emit a
        # 'voice=' fragment, and a None prepared must not raise (the guard).
        tool = MediaGen()
        text = tool._build_feedback(
            'audio', '/tmp/media/a.wav', 'kobold_tts',
            {'config_name': 'kokoro_default'}, 1.0,
        )
        assert 'voice=' not in text
        assert 'backend=kobold_tts' in text
        none_text = tool._build_feedback('audio', '/x/a.wav', 'b', None, 1.0)
        assert 'Generated audio:' in none_text


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

    def test_help_lists_backend_configs_with_json_descriptions(self, tmp_path):
        """T8: __help scans the chatterbox config_dir and lists voices with their JSON descriptions."""
        d = tmp_path / 'voices'
        d.mkdir()
        (d / 'voice_one.json').write_text(json.dumps({'description': 'Test voice one'}), encoding='utf-8')
        cfg = {'chatterbox': {'config_dir': str(d), 'media_types': {'audio': ['voice_one']}}}
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg):
            result = MediaGen().call({'prompt': '__help'})
        text = result[0].text
        assert 'backend: chatterbox' in text
        assert 'voice_one' in text
        assert 'Test voice one' in text

    def test_help_lists_voice_samples(self, tmp_path):
        """__help scans chatterbox.voices_dir and lists the reference audio files."""
        d = tmp_path / 'samples'
        d.mkdir()
        for name in ('alpha.wav', 'beta.mp3'):
            (d / name).write_bytes(b'RIFF\x00\x00\x00\x00WAVE')
        cfg = {'chatterbox': {'config_dir': '', 'voices_dir': str(d), 'media_types': {}}}
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg):
            result = MediaGen().call({'prompt': '__help'})
        text = result[0].text
        assert f'voice samples (2, chatterbox.voices_dir = {d}):' in text
        assert 'alpha.wav' in text
        assert 'beta.mp3' in text

    def test_help_voice_samples_count_is_honest_and_overflows(self, tmp_path):
        """Header count is the TRUE total (not a scan cap); only _HELP_MAX_STEMS (40)
        are listed and the remainder is summarized. Guards the uncapped list_voice_samples."""
        d = tmp_path / 'samples'
        d.mkdir()
        for i in range(41):
            (d / f'voice_{i:02d}.wav').write_bytes(b'RIFF\x00\x00\x00\x00WAVE')
        cfg = {'chatterbox': {'config_dir': '', 'voices_dir': str(d), 'media_types': {}}}
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg):
            result = MediaGen().call({'prompt': '__help'})
        text = result[0].text
        assert f'voice samples (41, chatterbox.voices_dir = {d}):' in text
        assert text.count('    - voice_') == 40
        assert '… (+1 more)' in text

    def test_help_ownership_filter_hides_claimed_foreign_stems(self, tmp_path):
        """T9: shared config_dir — stems claimed by kobold_tts must NOT appear in the
        chatterbox section (the ownership filter)."""
        d = tmp_path / 'voices'
        d.mkdir()
        for stem in ('kokoro_default', 'outetts_default'):
            (d / f'{stem}.json').write_text('{}', encoding='utf-8')
        cfg = {'kobold_tts': {'config_dir': str(d),
                              'media_types': {'audio': ['kokoro_default', 'outetts_default']}},
               'chatterbox': {'config_dir': str(d), 'media_types': {}}}
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg):
            result = MediaGen().call({'prompt': '__help'})
        text = result[0].text
        chunk = next(p for p in text.split('== backend: ') if p.startswith('chatterbox'))
        assert 'kokoro_default' not in chunk
        assert 'outetts_default' not in chunk

    def test_help_availability_list_is_capped(self, tmp_path):
        """T10: 45 on-disk stems -> at most _HELP_MAX_STEMS (40) listed + overflow line."""
        d = tmp_path / 'voices'
        d.mkdir()
        for i in range(45):
            (d / f'voice_{i:02d}.json').write_text('{}', encoding='utf-8')
        cfg = {'chatterbox': {'config_dir': str(d), 'media_types': {}}}
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg):
            result = MediaGen().call({'prompt': '__help'})
        text = result[0].text
        assert text.count('    - voice_') == 40
        assert '… (+' in text  # overflow marker present


# ── list_voice_samples helper (shared by preflight hint + __help) ─────────────

def test_list_voice_samples_uncapped_default_and_cap(tmp_path):
    from agent_cascade.tools.media_backends.base import list_voice_samples
    d = tmp_path / 'v'
    d.mkdir()
    for i in range(45):
        (d / f'voice_{i:02d}.wav').write_bytes(b'RIFF\x00\x00\x00\x00WAVE')
    (d / 'notes.txt').write_text('ignore me')          # non-audio excluded
    assert len(list_voice_samples(d)) == 45            # uncapped default
    assert len(list_voice_samples(d, cap=10)) == 10    # cap bounds the list
    assert list_voice_samples(d)[0] == 'voice_00.wav'  # sorted
    assert list_voice_samples(tmp_path / 'nope') == [] # missing dir -> []
