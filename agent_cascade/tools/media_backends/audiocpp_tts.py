"""
audio.cpp TTS backend for ``media_gen`` — one-shot CLI, zero-shot voice cloning.

Synthesises speech (WAV) via the audio.cpp one-shot CLI::

    audiocpp_cli.exe --task tts --family <family> --model <gguf> --backend cuda \
        [--device N] --text <text> [--voice-ref <ref.wav>] [--reference-text <transcript>] \
        --out <out.wav>

This is a ONE-SHOT process (like the ``voice_to_text`` ASR backend): the CLI loads
the model, synthesises, writes ``--out`` (a WAV), and exits. There is no server,
port, readiness loop, or process-tree teardown — the whole lifecycle is a single
``subprocess.run``.

Voice cloning: pass ``reference_file`` (a reference voice clip) and it is forwarded
as ``--voice-ref``. When the agent omits it, a reference is auto-supplied from
``voices_dir``/``default_voice`` in the config block — the same Chatterbox voices
folder the ``chatterbox`` backend uses. ``reference_file`` is always validated via
``resolve_tool_path`` (the SECURITY seam). Plain TTS (no clone) needs no reference.

Some families (OmniVoice) REQUIRE a ``--reference-text`` (the transcript of the
reference audio) whenever a reference is supplied. It is taken from the
``reference_text`` config key, overridable per-call via ``overrides['reference_text']``.
When a reference is present for a family that needs it and none is provided,
``preflight`` fails with a clear, actionable error BEFORE any VRAM work.

The child's stdout/stderr go to a real FILE (never a PIPE — the loader logs are
large), and success is judged by the ``--out`` WAV being non-empty with the RIFF/WAVE
magic (NOT the exit code, which the CLI can leave non-zero even on success — the same
discipline as the other one-shot backends).
"""

import os
import subprocess
import tempfile
from pathlib import Path

from .base import (
    MediaBackend, MediaRequest, MediaResult, resolve_media_timeout,
    _project_root, resolve_block_dir, list_voice_samples, is_wav_bytes,
)
from .manifest import BackendManifest, FieldSpec

from agent_cascade.utils.tool_path_resolver import resolve_tool_path

# Config/override key → CLI flag. Presence (not truthiness) gates the flag: seed=0 and
# guidance_scale=0.0 are legitimate values that MUST be passed. The audio.cpp CLI only
# accepts the two-token ``--flag value`` form (it rejects ``--flag=value``), so these are
# emitted as separate argv elements.
_FLAG_KEYS = {
    'language': '--language',
    'instruct': '--instruct',
    'seed': '--seed',
    'num_inference_steps': '--num-inference-steps',
    'guidance_scale': '--guidance-scale',
    'speaking_rate': '--speaking-rate',
    'pitch_shift': '--pitch-shift',
    'energy_scale': '--energy-scale',
    'emotion': '--emotion',
    'voice_id': '--voice-id',
    'style_tag': '--style-tag',
    'duration_seconds': '--duration-seconds',
}

# Families whose zero-shot clone REQUIRES --reference-text when a reference is present.
_CLONE_NEEDS_REFERENCE_TEXT = frozenset({'omnivoice'})

# A valid WAV is at least a few KB; this floor only guards against a stray stderr leak
# masquerading as the output file (the RIFF/WAVE magic check in run() is the real gate).
_MIN_AUDIO_BYTES = 128


class AudiocppTTSBackend(MediaBackend):
    id = 'audiocpp_tts'
    media_types = ('audio',)

    manifest = BackendManifest(
        label='audio.cpp TTS',
        note='audio.cpp one-shot CLI — TTS + zero-shot voice cloning (WAV). Local subprocess.',
        connection_fields=(
            FieldSpec(key='binary', label='audio.cpp CLI', kind='path', required=True,
                      placeholder='.../audiocpp_cli.exe'),
            FieldSpec(key='models_dir', label='TTS Models', kind='path',
                      placeholder='N:/work/stuff/Beta/audio',
                      help='Directory containing the TTS model package (GGUF).'),
            FieldSpec(key='family', label='TTS Family', kind='text', required=True,
                      placeholder='omnivoice',
                      help='audio.cpp model family (e.g. omnivoice, ming3_tts).'),
            FieldSpec(key='model', label='TTS Model', kind='text', required=True,
                      placeholder='omnivoice-q8_0.gguf',
                      help='Model path RELATIVE to models_dir (the GGUF file).'),
        ),
        advanced_fields=(
            FieldSpec(key='device', label='GPU Device', kind='number',
                      placeholder='0', min=0, max=8, step=1,
                      help='CUDA device index for the TTS model.'),
            FieldSpec(key='voices_dir', label='Voice Samples', kind='path',
                      placeholder='N:/work/WD/Chatterbox-TTS-Server/voices',
                      help='Directory of reference audio files for voice cloning. The default '
                           'voice is auto-selected from here when the agent omits reference_file.'),
            FieldSpec(key='default_voice', label='Default Reference Audio', kind='text',
                      placeholder='af_Bella.wav',
                      help='Filename in the Voice Samples dir used when no reference_file is '
                           'passed. Leave empty for plain TTS (no cloning).'),
            FieldSpec(key='reference_text', label='Reference Transcript', kind='text',
                      help='Transcript of the default reference voice. Required for families '
                           'like OmniVoice when cloning. Also settable per-call via '
                           "overrides={'reference_text': '<transcript>'}."),
            FieldSpec(key='timeout', label='Timeout (s)', kind='number',
                      min=30, max=3600, step=30),
            FieldSpec(key='release_model_around_call',
                      label='Free VRAM around the call', kind='checkbox'),
        ),
        availability='none',
        agent_args=('config', 'reference_file', 'overrides'),
        overrides_keys=('device', 'language', 'instruct', 'seed', 'num_inference_steps',
                        'guidance_scale', 'speaking_rate', 'pitch_shift', 'energy_scale',
                        'emotion', 'voice_id', 'style_tag', 'duration_seconds',
                        'reference_text'),
        usage_hints=(('audio', 'prompt="<text to speak>", '
                              'reference_file="<voice-clip.wav>"'),),
        defaults={'release_model_around_call': True},
    )

    def describe(self) -> str:
        return 'audio.cpp TTS (one-shot CLI, zero-shot voice cloning, WAV)'

    def preflight(self, req: MediaRequest) -> None:
        block = (req.config or {}).get('audiocpp_tts') or {}

        # ── Resolve the binary to an absolute path and check it exists (I1). ──
        binary = block.get('binary')
        if not binary or not isinstance(binary, str):
            raise RuntimeError(
                'No audio.cpp CLI configured. Set audiocpp_tts.binary in media_gen.json '
                '(e.g. N:/work/stuff/Beta/llama-autoloader/backends/'
                'audiocpp-v0.9.4-cuda13.3/audiocpp_cli.exe).'
            )
        binary_path = Path(binary)
        if not binary_path.is_absolute():
            binary_path = _project_root() / binary_path
        if not binary_path.is_file():
            raise RuntimeError(f"audio.cpp CLI not found: {binary_path}")

        # ── Resolve the model family + GGUF under models_dir; clear error if missing. ──
        family = block.get('family')
        if not family or not isinstance(family, str):
            raise RuntimeError(
                'audiocpp_tts.family is required (the audio.cpp TTS model family, '
                "e.g. 'omnivoice' or 'ming3_tts')."
            )
        model_name = block.get('model')
        if not model_name or not isinstance(model_name, str):
            raise RuntimeError(
                'audiocpp_tts.model is required (the TTS model path relative to models_dir).'
            )
        models_dir = block.get('models_dir') or ''
        model_path = Path(models_dir) / model_name
        if not model_path.is_absolute():
            model_path = _project_root() / model_path
        if not model_path.is_file():
            raise RuntimeError(f"TTS model not found: {model_path}")

        # ── GPU device (int, default 0); a per-call override wins. ──
        device = self._as_int(block.get('device', 0), 0)
        if isinstance(req.overrides, dict) and 'device' in req.overrides:
            device = self._as_int(req.overrides['device'], device)

        # ── reference_file → voice-cloning reference (SECURITY seam). Falls back to
        #     voices_dir/default_voice, mirroring the chatterbox backend. A reference is
        #     OPTIONAL for plain TTS (no cloning). ──
        ref_str = req.reference_file if isinstance(req.reference_file, str) else ''
        auto_supplied_from = ''
        if not ref_str.strip():
            vd = resolve_block_dir(block, 'voices_dir')
            default_voice = block.get('default_voice')
            if vd and isinstance(default_voice, str) and default_voice.strip():
                ref_str = str(vd / default_voice.strip())
                auto_supplied_from = (f"default_voice '{default_voice.strip()}' in "
                                      f"voices_dir '{vd}'")
        ref_path = None
        if ref_str.strip():
            ref_str = ref_str.strip()
            try:
                ref_path = resolve_tool_path(ref_str, mode='ro', agent_pool=req.agent_pool)
            except ValueError as e:
                if auto_supplied_from:
                    raise RuntimeError(
                        f"audiocpp_tts {auto_supplied_from} is not an allowed path: {e}. "
                        'Add the voices directory to your allowed paths, or pass '
                        "reference_file='<path>' explicitly."
                    ) from e
                raise RuntimeError(f"Invalid reference_file path: {e}") from e
            if not ref_path.is_file():
                if auto_supplied_from:
                    available = list_voice_samples(ref_path.parent, cap=10)
                    hint = f" Available: {', '.join(available)}" if available else ''
                    raise RuntimeError(
                        f"audiocpp_tts {auto_supplied_from} was not found at {ref_path}."
                        f"{hint} Check the filename or pass reference_file='<path>' "
                        'explicitly.'
                    )
                raise RuntimeError(f"reference_file file not found: {ref_str}")

        # ── reference_text: config key < per-call override. Required for some families
        #     (e.g. OmniVoice) whenever a reference is supplied. ──
        reference_text = block.get('reference_text')
        if isinstance(req.overrides, dict) and 'reference_text' in req.overrides:
            reference_text = req.overrides['reference_text']
        reference_text = (reference_text
                          if isinstance(reference_text, str) else '')
        if ref_path is not None and family in _CLONE_NEEDS_REFERENCE_TEXT \
                and not reference_text.strip():
            raise RuntimeError(
                f"audiocpp_tts family '{family}' requires a --reference-text (the transcript "
                'of the reference audio) when cloning. Set audiocpp_tts.reference_text in '
                "media_gen.json, or pass overrides={'reference_text': '<transcript>'}."
            )

        # ── Merge defaults: config block < explicit overrides (presence-gated). ──
        merged: dict = {}
        for key in _FLAG_KEYS:
            if key in block:
                merged[key] = block[key]
        if isinstance(req.overrides, dict):
            for key in _FLAG_KEYS:
                if key in req.overrides:
                    merged[key] = req.overrides[key]

        # ── Timeout: unified resolver (audiocpp_tts.timeout > top-level > 900). ──
        timeout = resolve_media_timeout(req.config, block)

        # ── Output WAV: a real FILE in the system temp dir (written by the CLI, read by
        #     run). Unlinked by media_gen's finally via prepared['out_path']. ──
        fd, wav_out_path = tempfile.mkstemp(prefix='audiocpp_tts_out-', suffix='.wav')
        os.close(fd)

        # ── Build argv (pure; never shell=True). One-shot TTS launch contract. The audio.cpp
        #     CLI only accepts the two-token ``--flag value`` form (it rejects ``--flag=value``),
        #     so every flag and its value are separate argv elements. ──
        argv = [str(binary_path), '--task', 'tts', '--family', family,
                '--model', str(model_path), '--backend', 'cuda',
                '--device', str(device),
                '--text', req.prompt,
                '--out', wav_out_path]
        if ref_path is not None:
            argv += ['--voice-ref', str(ref_path)]
        if reference_text.strip():
            argv += ['--reference-text', reference_text]
        for key, flag in _FLAG_KEYS.items():
            if key in merged:
                argv += [flag, str(merged[key])]

        req.prepared = {
            'argv': argv,
            'wav_out_path': wav_out_path,
            'out_path': wav_out_path,          # media_gen's finally unlinks this on every path.
            'timeout': timeout,
            'family': family,
            'model': str(model_path),
            'device': device,
            'reference': ref_path.name if ref_path is not None else '',
            'config_name': req.config_name or family,
        }

    @staticmethod
    def _as_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def run(self, req: MediaRequest) -> MediaResult:
        prepared = req.prepared
        argv = prepared['argv']
        timeout = prepared['timeout']
        wav_out = prepared['wav_out_path']

        # Child logs go to a real FILE, never PIPE (the CLI's loader logs are large; a PIPE
        # would fill the ~64 KB buffer and deadlock).
        fd, log_path = tempfile.mkstemp(prefix='audiocpp_tts-', suffix='.log')
        os.close(fd)
        try:
            with open(log_path, 'ab') as lf:
                try:
                    proc = subprocess.run(argv, stdout=lf, stderr=subprocess.STDOUT,
                                          timeout=timeout)
                except subprocess.TimeoutExpired:
                    raise TimeoutError(
                        f"audiocpp_tts generation timed out after {timeout}s (log: {log_path})"
                    )
                rc = proc.returncode

            # Success = the --out WAV is non-empty with the RIFF/WAVE magic. NEVER the exit
            # code (the CLI may exit non-zero even on success).
            if not os.path.exists(wav_out) or os.path.getsize(wav_out) < _MIN_AUDIO_BYTES:
                raise RuntimeError(
                    f"audiocpp_tts produced no audio: the --out WAV was empty or missing "
                    f"(exit {rc}) (log: {log_path})"
                )
            with open(wav_out, 'rb') as f:
                audio = f.read()
            if not is_wav_bytes(audio):
                raise RuntimeError(
                    f"audiocpp_tts output is not a valid WAV (missing RIFF/WAVE magic; "
                    f"exit {rc}) (log: {log_path})"
                )
            return MediaResult(data=audio, ext='wav', meta={
                'model': prepared.get('model', ''),
                'family': prepared.get('family', ''),
                'device': prepared.get('device', 0),
                'reference': prepared.get('reference', ''),
                'bytes': len(audio),
            })
        finally:
            try:
                os.remove(log_path)
            except OSError:
                pass
