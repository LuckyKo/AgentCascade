"""Tests for the Voice-to-Text (ASR) backend — audio.cpp one-shot CLI.

Mirrors test_media_gen_tts.py (all external I/O mocked: no real binary, no GPU, no
ffmpeg). Covers the NEW TEXT-output contract:

  * preflight resolves binary + model GGUF + reference_file (the resolve_tool_path
    SECURITY seam) BEFORE any VRAM touch, and builds the one-shot ASR argv
    (``--task asr --family qwen3_asr --model <gguf> --backend cuda --audio <wav>
    --text-out <txt>``). It never trusts the exit code.
  * SUCCESS is judged by the --text-out file being NON-EMPTY: rc=1 + non-empty →
    success; rc=0 + empty/missing → error (the CLI exits 1 even on success).
  * child logs go to a real FILE, never a PIPE.
  * input normalisation to 16 kHz mono WAV via ffmpeg; a clear preflight error when
    ffmpeg is missing and the input is not already 16 kHz mono WAV.
  * call() returns a TEXT ContentItem and SKIPS save_media_to_media; the VRAM
    save/unload/restore seam is intact (preflight strictly before VRAM).
  * the prompt gate is relaxed ONLY for text-output backends; media backends still
    require a prompt.
"""

import os
import subprocess
import wave
from types import SimpleNamespace

from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.tools.media_gen import MediaGen, _invalidate_media_gen_config
from agent_cascade.tools.media_backends.base import MediaRequest
import agent_cascade.tools.media_backends.voice_to_text as _vmod
from agent_cascade.tools.media_backends.voice_to_text import VoiceToTextBackend

_V = 'agent_cascade.tools.media_backends.voice_to_text'


@pytest.fixture(autouse=True)
def _bust_config_cache():
    _invalidate_media_gen_config()
    yield
    _invalidate_media_gen_config()


# ── config / request helpers ─────────────────────────────────────────────────

def _asr_cfg(**over):
    cfg = {
        'sdcpp': {'binary': r'C:\bin\sd-cli.exe', 'media_types': {'image': []}},
        'voice_to_text': {
            'engine': 'audio_cpp',
            'binary': r'C:\bin\audiocpp_cli.exe',
            'models_dir': r'C:\models\audio',
            'family': 'qwen3_asr',
            'model': 'Qwen3-ASR-1.7B-GGUF/qwen3-asr-1.7b-q8_0.gguf',
            'timeout': 300,
            'release_model_around_call': True,
        },
    }
    cfg.update(over)
    return cfg


def _req(**over):
    # ASR has NO prompt: prompt='' is the normal request shape for this backend.
    req = MediaRequest(
        media_type='', backend_id='', config_name=None,
        prompt='', negative_prompt='', config=_asr_cfg(), agent_pool=None,
    )
    for k, v in over.items():
        setattr(req, k, v)
    return req


def _write_16k_mono_wav(path):
    """A structurally-valid 16 kHz, single-channel, 16-bit PCM WAV (no conversion needed)."""
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b'\x00\x00' * 100)


def _write_non_16k_wav(path):
    """A valid WAV at the WRONG rate/channels → must trigger conversion."""
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b'\x00\x00\x00\x00' * 100)


def _fake_instance():
    inst = MagicMock()
    inst._last_endpoint_config = {
        'state_save_enabled': True,
        'api_base': 'http://localhost:1234',
        'model': 'test-model',
    }
    return inst


@pytest.fixture
def asr_files(tmp_path):
    """Create a fake binary + model GGUF + a ready 16k mono WAV; return paths + config."""
    binary = tmp_path / 'audiocpp_cli.exe'
    binary.write_text('fake binary')
    model = tmp_path / 'Qwen3-ASR-1.7B-GGUF' / 'qwen3-asr-1.7b-q8_0.gguf'
    model.parent.mkdir()
    model.write_text('fake model')
    audio = tmp_path / 'in.wav'
    _write_16k_mono_wav(audio)
    cfg = _asr_cfg()
    cfg['voice_to_text']['binary'] = str(binary)
    cfg['voice_to_text']['models_dir'] = str(tmp_path)
    return SimpleNamespace(cfg=cfg, binary=str(binary), model=str(model),
                           audio=str(audio), tmp=tmp_path)


# ── preflight: one-shot ASR launch contract (argv) ──────────────────────────

class TestAsrPreflight:
    def test_argv_launch_contract(self, asr_files):
        req = _req(backend_id='voice_to_text', reference_file=asr_files.audio)
        req.config = asr_files.cfg
        req.agent_pool = MagicMock()
        with patch(f'{_V}.resolve_tool_path', return_value=_vmod.Path(asr_files.audio)):
            VoiceToTextBackend().preflight(req)
        argv = req.prepared['argv']
        # Exact one-shot ASR launch contract (the proven bench_audiocpp.py form).
        assert argv[0] == asr_files.binary
        assert argv[1:3] == ['--task', 'asr']
        assert argv[3:5] == ['--family', 'qwen3_asr']
        assert argv[5:7] == ['--model', asr_files.model]
        assert argv[7:9] == ['--backend', 'cuda']
        # 16k mono WAV input → NO conversion: --audio is the resolved source, not a temp.
        assert argv[9:11] == ['--audio', asr_files.audio]
        assert argv[11] == '--text-out'
        assert argv[12].endswith('.txt')
        assert 'temp_audio' not in req.prepared  # no temp allocated pre-VRAM (run() owns it)
        assert req.prepared['needs_conversion'] is False
        assert req.prepared['out_path'] == req.prepared['text_out_path']
        # Timeout read from the block (300), NOT the 900 default — proves the read path.
        assert req.prepared['timeout'] == 300

    def test_missing_binary_errors(self):
        req = _req(backend_id='voice_to_text', reference_file=r'C:\in.wav')
        req.config = {'voice_to_text': {'models_dir': r'C:\m', 'family': 'qwen3_asr',
                                        'model': 'x.gguf'}}
        with pytest.raises(RuntimeError) as e:
            VoiceToTextBackend().preflight(req)
        assert 'No audio.cpp CLI configured' in str(e.value)

    def test_unsupported_engine_errors(self, asr_files):
        cfg = {k: dict(v) for k, v in asr_files.cfg.items()}
        cfg['voice_to_text']['engine'] = 'whisper_server'
        req = _req(backend_id='voice_to_text', reference_file=asr_files.audio)
        req.config = cfg
        with pytest.raises(RuntimeError) as e:
            VoiceToTextBackend().preflight(req)
        assert 'Unsupported voice_to_text engine' in str(e.value)

    def test_missing_model_errors(self, asr_files):
        cfg = {k: dict(v) for k, v in asr_files.cfg.items()}
        cfg['voice_to_text']['models_dir'] = r'C:\empty'
        req = _req(backend_id='voice_to_text', reference_file=asr_files.audio)
        req.config = cfg
        req.agent_pool = MagicMock()
        with patch(f'{_V}.resolve_tool_path', return_value=_vmod.Path(asr_files.audio)):
            with pytest.raises(RuntimeError) as e:
                VoiceToTextBackend().preflight(req)
        assert 'ASR model not found' in str(e.value)

    def test_out_of_roots_reference_file_rejected(self, asr_files):
        req = _req(backend_id='voice_to_text', reference_file=r'C:\outside\escape.wav')
        req.config = asr_files.cfg
        req.agent_pool = MagicMock()
        with patch(f'{_V}.resolve_tool_path',
                   side_effect=ValueError('outside allowed roots')):
            with pytest.raises(RuntimeError) as e:
                VoiceToTextBackend().preflight(req)
        assert 'Invalid reference_file path' in str(e.value)

    def test_missing_reference_file_errors(self, asr_files):
        req = _req(backend_id='voice_to_text')  # no reference_file at all
        req.config = asr_files.cfg
        with pytest.raises(RuntimeError) as e:
            VoiceToTextBackend().preflight(req)
        assert "requires 'reference_file'" in str(e.value)

    def test_non_audio_ext_rejected(self, asr_files):
        not_audio = _vmod.Path(asr_files.tmp) / 'notes.txt'
        not_audio.write_text('hi')
        req = _req(backend_id='voice_to_text', reference_file=str(not_audio))
        req.config = asr_files.cfg
        req.agent_pool = MagicMock()
        with patch(f'{_V}.resolve_tool_path', return_value=not_audio):
            with pytest.raises(RuntimeError) as e:
                VoiceToTextBackend().preflight(req)
        assert 'not a recognised audio file' in str(e.value)

    def test_non_16k_input_is_flagged_for_conversion(self, asr_files):
        # A wrong-rate WAV + ffmpeg present → preflight DECIDES a conversion is needed and
        # records ffmpeg, but allocates NO temp (run() owns that). argv --audio still points
        # at the source; run() swaps in the temp only when it actually runs.
        bad = _vmod.Path(asr_files.tmp) / 'bad.wav'
        _write_non_16k_wav(bad)
        req = _req(backend_id='voice_to_text', reference_file=str(bad))
        req.config = asr_files.cfg
        req.agent_pool = MagicMock()
        with patch(f'{_V}.resolve_tool_path', return_value=bad), \
             patch(f'{_V}.shutil.which', return_value='C:/ffmpeg/ffmpeg.exe'):
            VoiceToTextBackend().preflight(req)
        assert req.prepared['needs_conversion'] is True
        assert req.prepared['ffmpeg'] == 'C:/ffmpeg/ffmpeg.exe'
        assert 'temp_audio' not in req.prepared  # no temp allocated pre-VRAM
        argv = req.prepared['argv']
        assert argv[argv.index('--audio') + 1] == str(bad)  # source; run() rewrites if it runs

    def test_missing_ffmpeg_errors_for_non_16k(self, asr_files):
        bad = _vmod.Path(asr_files.tmp) / 'bad.wav'
        _write_non_16k_wav(bad)
        req = _req(backend_id='voice_to_text', reference_file=str(bad))
        req.config = asr_files.cfg
        req.agent_pool = MagicMock()
        with patch(f'{_V}.resolve_tool_path', return_value=bad), \
             patch(f'{_V}.shutil.which', return_value=None):
            with pytest.raises(RuntimeError) as e:
                VoiceToTextBackend().preflight(req)
        assert 'ffmpeg' in str(e.value)
        assert '16 kHz' in str(e.value)


# ── run(): success-by-file, never exit code; logs to file ───────────────────

def _prepared(tmp_path, *, needs_conversion=False, ffmpeg=None, audio_src=None,
              model='C:/m/q.gguf', timeout=300):
    """A prepared dict matching what preflight stashes, for run() unit tests.

    Mirrors preflight's CURRENT contract: it records needs_conversion + audio_src and does
    NOT allocate the converted temp (run() does, and cleans it). argv --audio targets the
    source (always an existing file); run() swaps in the temp only when needs_conversion.
    """
    import tempfile
    fd, text_out = tempfile.mkstemp(prefix='asr_out-', suffix='.txt')
    os.close(fd)
    src = audio_src or str(tmp_path / 'in.wav')
    argv = ['audiocpp_cli.exe', '--task', 'asr', '--family', 'qwen3_asr',
            '--model', model, '--backend', 'cuda',
            '--audio', src, '--text-out', text_out]
    return {
        'argv': argv,
        'text_out_path': text_out,
        'out_path': text_out,
        'needs_conversion': needs_conversion,
        'ffmpeg': ffmpeg,
        'audio_src': src,
        'timeout': timeout,
        'model': model,
        'family': 'qwen3_asr',
    }


class TestAsrRun:
    def test_success_ignores_exit_code(self, tmp_path):
        # rc=1 (the CLI's normal success exit) + a non-empty --text-out file → SUCCESS.
        req = _req(backend_id='voice_to_text')
        req.prepared = _prepared(tmp_path)
        def _run(argv, *a, **kw):
            i = argv.index('--text-out')
            with open(argv[i + 1], 'w', encoding='utf-8') as f:
                f.write('  hello there transcript  ')
            return MagicMock(returncode=1)
        with patch(f'{_V}.subprocess.run', side_effect=_run):
            res = VoiceToTextBackend().run(req)
        assert res.text == 'hello there transcript'  # stripped
        assert res.meta['chars'] == len('hello there transcript')
        assert res.meta['family'] == 'qwen3_asr'

    def test_empty_transcript_errors_despite_rc_zero(self, tmp_path):
        # rc=0 but the --text-out file is EMPTY → FAILURE (exit code is never trusted).
        req = _req(backend_id='voice_to_text')
        req.prepared = _prepared(tmp_path)
        def _run(argv, *a, **kw):
            i = argv.index('--text-out')
            open(argv[i + 1], 'w').close()  # create but leave empty
            return MagicMock(returncode=0)
        with patch(f'{_V}.subprocess.run', side_effect=_run):
            with pytest.raises(RuntimeError) as e:
                VoiceToTextBackend().run(req)
        assert 'no transcript' in str(e.value)

    def test_missing_transcript_file_errors(self, tmp_path):
        # The --text-out file was NEVER CREATED → the defensive `os.path.exists(...)` False
        # branch is exercised (distinct from the EMPTY-file branch tested separately).
        req = _req(backend_id='voice_to_text')
        req.prepared = _prepared(tmp_path)
        os.remove(req.prepared['text_out_path'])  # _prepared pre-creates it; remove to hit the missing branch
        with patch(f'{_V}.subprocess.run', return_value=MagicMock(returncode=0)):
            with pytest.raises(RuntimeError) as e:
                VoiceToTextBackend().run(req)
        assert 'no transcript' in str(e.value)

    def test_child_logs_go_to_file_not_pipe(self, tmp_path):
        req = _req(backend_id='voice_to_text')
        req.prepared = _prepared(tmp_path)
        def _run(argv, *a, **kw):
            i = argv.index('--text-out')
            with open(argv[i + 1], 'w', encoding='utf-8') as f:
                f.write('ok')
            return MagicMock(returncode=1)
        with patch(f'{_V}.subprocess.run', side_effect=_run) as m_run:
            VoiceToTextBackend().run(req)
        stdout = m_run.call_args.kwargs['stdout']
        assert stdout is not subprocess.PIPE
        assert hasattr(stdout, 'name')
        assert m_run.call_args.kwargs['stderr'] == subprocess.STDOUT

    def test_conversion_invokes_ffmpeg_then_asr(self, tmp_path):
        # needs_conversion → run() allocates a temp, converts via ffmpeg (-ar 16000), THEN
        # runs ASR against that temp. The ASR --audio MUST equal the ffmpeg output target.
        req = _req(backend_id='voice_to_text')
        req.prepared = _prepared(tmp_path, needs_conversion=True,
                                 ffmpeg='C:/ffmpeg/ffmpeg.exe', audio_src=str(tmp_path / 'src.mp3'))
        calls = []
        def _run(argv, *a, **kw):
            calls.append(list(argv))
            if '--text-out' in argv:
                i = argv.index('--text-out')
                with open(argv[i + 1], 'w', encoding='utf-8') as f:
                    f.write('converted ok')
                return MagicMock(returncode=1)  # ASR: normal success exit
            return MagicMock(returncode=0)      # ffmpeg conversion: success
        with patch(f'{_V}.subprocess.run', side_effect=_run):
            res = VoiceToTextBackend().run(req)
        assert len(calls) == 2
        assert calls[0][0] == 'C:/ffmpeg/ffmpeg.exe'
        assert calls[0][calls[0].index('-ar') + 1] == '16000'
        # The ASR input is exactly the file ffmpeg was told to write — the run-allocated temp.
        assert calls[1][calls[1].index('--audio') + 1] == calls[0][-1]
        assert res.text == 'converted ok'

    def test_conversion_failure_reports_distinct_error(self, tmp_path):
        # ffmpeg present but FAILS (corrupt input) → a DISTINCT conversion error, and the
        # ASR command is never attempted (not misdiagnosed as "no transcript").
        req = _req(backend_id='voice_to_text')
        req.prepared = _prepared(tmp_path, needs_conversion=True,
                                 ffmpeg='C:/ffmpeg/ffmpeg.exe', audio_src=str(tmp_path / 'src.mp3'))
        calls = []
        def _run(argv, *a, **kw):
            calls.append(list(argv))
            return MagicMock(returncode=1)  # ffmpeg conversion fails
        with patch(f'{_V}.subprocess.run', side_effect=_run):
            with pytest.raises(RuntimeError) as e:
                VoiceToTextBackend().run(req)
        assert 'input conversion failed' in str(e.value)
        assert len(calls) == 1  # ASR never attempted
        assert '--text-out' not in calls[0]
        assert not os.path.exists(calls[0][-1])  # run-allocated temp cleaned on this failure path

    def test_temp_audio_cleaned_on_failure(self, tmp_path):
        # The run-allocated conversion temp must be removed even when the run fails.
        req = _req(backend_id='voice_to_text')
        req.prepared = _prepared(tmp_path, needs_conversion=True,
                                 ffmpeg='C:/ffmpeg/ffmpeg.exe', audio_src=str(tmp_path / 'src.mp3'))
        calls = []
        def _run(argv, *a, **kw):
            calls.append(list(argv))
            return MagicMock(returncode=0)  # ffmpeg ok; ASR produces no transcript file
        with patch(f'{_V}.subprocess.run', side_effect=_run):
            with pytest.raises(RuntimeError):
                VoiceToTextBackend().run(req)
        assert not os.path.exists(calls[0][-1])  # temp cleaned on the no-transcript path


# ── VRAM slotting + text result shaping (the new backend must obey V1-V9) ───

class TestAsrVRAM:
    def _run_call(self, tool, cfg, *, transcript='the transcript text'):
        order = []

        def _save(inst):
            order.append('save'); return True

        def _unload(base):
            order.append('unload'); return True

        def _restore(inst, held_endpoint_cfg=None):
            order.append('restore'); return True

        def _subprocess_run(argv, *a, **kw):
            order.append('run')
            i = argv.index('--text-out')
            with open(argv[i + 1], 'w', encoding='utf-8') as f:
                f.write(transcript)
            return MagicMock(returncode=1)

        with patch.object(tool, '_get_instance', return_value=_fake_instance()), \
             patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg), \
             patch(f'{_V}.subprocess.run', side_effect=_subprocess_run), \
             patch('agent_cascade.tools.media_gen.save_media_to_media') as m_save_media, \
             patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
             patch('agent_cascade.state_ops.save_instance_state', side_effect=_save), \
             patch('agent_cascade.state_ops.unload_all_models', side_effect=_unload), \
             patch('agent_cascade.state_ops.restore_instance_state', side_effect=_restore):
            result = tool.call({'backend': 'voice_to_text', 'reference_file': '/in.wav'})
        return order, result, m_save_media

    def test_order_and_text_result_skips_media_save(self, asr_files):
        tool = MediaGen()
        # reference_file must resolve through the security seam → patch it to a real file.
        with patch(f'{_V}.resolve_tool_path', return_value=_vmod.Path(asr_files.audio)):
            order, result, m_save_media = self._run_call(tool, asr_files.cfg)
        assert order == ['save', 'unload', 'run', 'restore']
        m_save_media.assert_not_called()  # text output → NO media save
        assert result[0].text == 'the transcript text'
        assert 'Transcribed:' in result[1].text
        assert 'backend=voice_to_text' in result[1].text

    def test_preflight_failure_touches_zero_vram(self, asr_files):
        # Missing model → preflight raises; zero VRAM touched (V1).
        cfg = {k: dict(v) for k, v in asr_files.cfg.items()}
        cfg['voice_to_text']['models_dir'] = r'C:\empty'
        tool = MediaGen()
        with patch(f'{_V}.resolve_tool_path', return_value=_vmod.Path(asr_files.audio)), \
             patch.object(tool, '_get_instance', return_value=_fake_instance()), \
             patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=cfg), \
             patch('agent_cascade.state_ops.save_instance_state') as m_save, \
             patch('agent_cascade.state_ops.unload_all_models') as m_unload, \
             patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'backend': 'voice_to_text', 'reference_file': '/in.wav'})
        m_save.assert_not_called()
        m_unload.assert_not_called()
        m_restore.assert_not_called()
        assert 'ERROR' in result[0].text

    def test_generation_failure_still_restores(self, asr_files):
        # Empty transcript → run() raises; restore still runs; ERROR surfaced.
        tool = MediaGen()
        def _empty_run(argv, *a, **kw):
            i = argv.index('--text-out')
            open(argv[i + 1], 'w').close()
            return MagicMock(returncode=0)
        with patch(f'{_V}.resolve_tool_path', return_value=_vmod.Path(asr_files.audio)), \
             patch.object(tool, '_get_instance', return_value=_fake_instance()), \
             patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=asr_files.cfg), \
             patch(f'{_V}.subprocess.run', side_effect=_empty_run), \
             patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
             patch('agent_cascade.state_ops.save_instance_state', return_value=True), \
             patch('agent_cascade.state_ops.unload_all_models', return_value=True), \
             patch('agent_cascade.state_ops.restore_instance_state') as m_restore:
            result = tool.call({'backend': 'voice_to_text', 'reference_file': '/in.wav'})
        m_restore.assert_called_once()
        assert 'ERROR' in result[0].text


# ── prompt gate: relaxed ONLY for text-output backends ──────────────────────

class TestPromptGate:
    def test_text_backend_needs_no_prompt(self, asr_files):
        # No prompt → must reach preflight (NOT the prompt-required error).
        tool = MediaGen()
        with patch.object(VoiceToTextBackend, 'preflight',
                          side_effect=RuntimeError('PREFLIGHT-REACHED')), \
             patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=asr_files.cfg):
            result = tool.call({'backend': 'voice_to_text', 'reference_file': '/in.wav'})
        assert 'PREFLIGHT-REACHED' in result[0].text
        assert 'prompt' not in result[0].text

    def test_media_backend_still_requires_prompt(self, asr_files):
        # A media backend with no prompt → the prompt-required error is preserved.
        tool = MediaGen()
        with patch('agent_cascade.tools.media_gen._get_media_gen_config', return_value=asr_files.cfg):
            result = tool.call({'backend': 'sdcpp'})
        assert "'prompt' is required" in result[0].text
