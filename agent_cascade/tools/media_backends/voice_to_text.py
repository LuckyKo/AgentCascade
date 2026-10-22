"""
Voice-to-Text (ASR) backend for ``media_gen`` — audio.cpp one-shot CLI.

Transcribes an audio file to TEXT via the audio.cpp one-shot CLI::

    audiocpp_cli.exe --task asr --family qwen3_asr --model <gguf> --backend cuda \\
        --audio <16k.wav> --text-out <transcript.txt>

This is a ONE-SHOT process: the CLI loads the model, transcribes, writes
``--text-out``, and exits. There is no server, port, readiness loop, or process-tree
teardown — the simplest possible fit for ``media_gen`` (the whole lifecycle is a single
``subprocess.run``). The koboldcpp/Whisper server path is intentionally OUT OF SCOPE here.

This is the first TEXT-output backend: its manifest declares ``output_kind='text'`` and it
declares an EMPTY ``media_types`` (it produces no image/video/audio). ``media_gen.call()``
branches on ``output_kind`` to skip ``save_media_to_media`` and return the transcript inline.
``MEDIA_TYPES`` stays exactly image/video/audio (base.py invariant) — text is an output
*KIND*, not a media type.

Success is judged by the ``--text-out`` file being NON-EMPTY, NEVER the exit code: the
audio.cpp CLI writes TIMING/TRACE logs to stderr and has been observed to exit non-zero even
on success (same discipline as the TTS "rc=1 after kill is normal" rule in the TTS backends).
The child's stdout/stderr go to a real FILE, never a PIPE (the loader logs are large; PIPE
would fill the ~64 KB buffer and deadlock).

Input normalisation: audio.cpp wants 16 kHz mono PCM WAV. A resolved input that is NOT already
that is converted to it via ``ffmpeg`` into a temp file before being passed as ``--audio``;
if ``ffmpeg`` is unavailable and the input is not already ready, ``preflight`` fails clearly
(before any VRAM work).
"""

import os
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from .base import (
    MediaBackend, MediaRequest, MediaResult, resolve_media_timeout,
    _project_root, AUDIO_EXTS,
)
from .manifest import BackendManifest, FieldSpec

from agent_cascade.utils.tool_path_resolver import resolve_tool_path

# Only the audio.cpp one-shot engine is implemented (the single-id + engine-selector design
# from the plan; the koboldcpp/Whisper server engine is out of scope). ``engine`` is read from
# the config block so a future second engine is rejected explicitly rather than silently
# running audio.cpp — it is a live selector, never a dead config field.
_SUPPORTED_ENGINES = ('audio_cpp',)

# audio.cpp wants exactly 16 kHz, single-channel, 16-bit PCM WAV.
_TARGET_SR = 16000


def _is_16k_mono_wav(path: Path) -> bool:
    """True only if ``path`` is already a 16 kHz, single-channel, 16-bit PCM WAV.

    Uses the stdlib ``wave`` module, which opens PCM WAV only — so a non-WAV path, a
    non-PCM WAV, or a WAV at any other rate / channel count / bit depth returns False
    (→ convert via ffmpeg). Being strict here is safe: conversion is idempotent, so we
    only skip it for a genuinely-ready file.
    """
    try:
        with wave.open(str(path), 'rb') as w:
            return (w.getnchannels() == 1
                    and w.getframerate() == _TARGET_SR
                    and w.getsampwidth() == 2)
    except Exception:
        return False


class VoiceToTextBackend(MediaBackend):
    id = 'voice_to_text'
    # TEXT-output backend: produces NO media type. Exempt from the non-empty media_types
    # manifest test via output_kind='text' (see media_backends/manifest.py).
    media_types = ()

    manifest = BackendManifest(
        label='Voice to Text (ASR)',
        note='audio → text via audio.cpp one-shot CLI',
        connection_fields=(
            FieldSpec(key='binary', label='audio.cpp CLI', kind='path', required=True,
                      placeholder='.../audiocpp_cli.exe'),
            FieldSpec(key='models_dir', label='ASR Models', kind='path',
                      placeholder='N:/work/stuff/Beta/audio',
                      help='Directory containing the ASR model package (GGUF).'),
            FieldSpec(key='family', label='ASR Family', kind='text', required=True,
                      placeholder='qwen3_asr',
                      help='audio.cpp model family for the ASR task (e.g. qwen3_asr).'),
            FieldSpec(key='model', label='ASR Model', kind='text', required=True,
                      placeholder='Qwen3-ASR-1.7B-GGUF/qwen3-asr-1.7b-q8_0.gguf',
                      help='Model path RELATIVE to models_dir (the GGUF file or model dir).'),
        ),
        advanced_fields=(
            FieldSpec(key='timeout', label='Timeout (s)', kind='number',
                      min=30, max=3600, step=30),
            FieldSpec(key='release_model_around_call',
                      label='Free VRAM around the call', kind='checkbox'),
        ),
        availability='none',
        output_kind='text',
        agent_args=('reference_file',),
        defaults={'release_model_around_call': True},
        teardown_headroom=0,
    )

    def describe(self) -> str:
        return 'Voice to Text (ASR, audio.cpp one-shot CLI)'

    def preflight(self, req: MediaRequest) -> None:
        block = (req.config or {}).get('voice_to_text') or {}

        # ── Engine selector (single id, engine branch). Only audio.cpp is implemented. ──
        engine = block.get('engine') or 'audio_cpp'
        if engine not in _SUPPORTED_ENGINES:
            raise RuntimeError(
                f"Unsupported voice_to_text engine '{engine}'. "
                f"Implemented: {', '.join(_SUPPORTED_ENGINES)}."
            )

        # ── Resolve the binary to an absolute path and check it exists (I1). ──
        binary = block.get('binary')
        if not binary or not isinstance(binary, str):
            raise RuntimeError(
                'No audio.cpp CLI configured. Set voice_to_text.binary in '
                'media_gen.json (e.g. N:/work/WD/audio.cpp/build/.../audiocpp_cli.exe).'
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
                'voice_to_text.family is required (the audio.cpp ASR model family, '
                "e.g. 'qwen3_asr')."
            )
        model_name = block.get('model')
        if not model_name or not isinstance(model_name, str):
            raise RuntimeError(
                'voice_to_text.model is required (the ASR model path relative to models_dir).'
            )
        models_dir = block.get('models_dir') or ''
        model_path = Path(models_dir) / model_name
        if not model_path.is_absolute():
            model_path = _project_root() / model_path
        if not model_path.is_file():
            raise RuntimeError(f"ASR model not found: {model_path}")

        # ── reference_file → the input audio (SECURITY seam). The ONLY required input. ──
        # Resolved via resolve_tool_path BEFORE any subprocess; an out-of-roots path raises
        # and the request never reaches VRAM.
        if not isinstance(req.reference_file, str) or not req.reference_file.strip():
            raise RuntimeError(
                "voice_to_text requires 'reference_file' (the audio file to transcribe). "
                "Pass reference_file='<path-to-audio>'."
            )
        try:
            ref_path = resolve_tool_path(req.reference_file.strip(), mode='ro',
                                         agent_pool=req.agent_pool)
        except ValueError as e:
            raise RuntimeError(f"Invalid reference_file path: {e}") from e
        if not ref_path.is_file():
            raise RuntimeError(f"reference_file file not found: {req.reference_file}")
        if ref_path.suffix.lower() not in AUDIO_EXTS:
            raise RuntimeError(
                f"reference_file '{ref_path.name}' is not a recognised audio file. "
                f"Supported: {', '.join(sorted(AUDIO_EXTS))}."
            )

        # ── Input normalisation: audio.cpp wants 16 kHz mono PCM WAV. DECIDE here (pre-VRAM)
        #    whether a conversion is needed and validate ffmpeg is available; the converted-input
        #    temp is ALLOCATED in run(), NOT here, so a failure between preflight and run leaks
        #    nothing (media_gen's finally only owns out_path). ──
        needs_conversion = not _is_16k_mono_wav(ref_path)
        ffmpeg = None
        if needs_conversion:
            ffmpeg = shutil.which('ffmpeg')
            if not ffmpeg:
                raise RuntimeError(
                    f"Input audio '{ref_path.name}' is not 16 kHz mono WAV and ffmpeg is "
                    'not available to convert it. Provide a 16 kHz mono WAV reference_file, '
                    'or install ffmpeg on PATH.'
                )

        # ── Timeout: unified resolver (voice_to_text.timeout > top-level timeout > 900). ──
        timeout = resolve_media_timeout(req.config, block)

        # ── Transcript output file: a real FILE in the system temp dir (read by run, never
        #    parsed from stdout). Unlinked by media_gen's finally via prepared['out_path']. ──
        fd, text_out_path = tempfile.mkstemp(prefix='asr_out-', suffix='.txt')
        os.close(fd)

        # ── Build argv (pure; never shell=True). One-shot ASR launch contract. ──
        # --model takes the resolved GGUF path directly (the proven bench_audiocpp.py form).
        # --audio targets the RESOLVED SOURCE (always an existing file); run() swaps in the
        # converted temp only when needs_conversion, so preflight never references a temp it
        # might never create.
        argv = [str(binary_path), '--task', 'asr', '--family', family,
                '--model', str(model_path), '--backend', 'cuda',
                '--audio', str(ref_path), '--text-out', text_out_path]

        req.prepared = {
            'argv': argv,
            'text_out_path': text_out_path,
            'out_path': text_out_path,        # media_gen's finally unlinks this on every path.
            'needs_conversion': needs_conversion,  # run() allocates + cleans the temp itself.
            'ffmpeg': ffmpeg,                 # set only when a conversion is needed.
            'audio_src': str(ref_path),       # ffmpeg reads this; ASR gets the converted temp.
            'timeout': timeout,
            'model': str(model_path),
            'family': family,
            'config_name': req.config_name or '',
        }

    def run(self, req: MediaRequest) -> MediaResult:
        prepared = req.prepared
        argv = list(prepared['argv'])
        timeout = prepared['timeout']
        text_out = prepared['text_out_path']
        audio_src = prepared.get('audio_src')
        ffmpeg = prepared.get('ffmpeg')

        start = time.monotonic()
        # Child logs go to a real FILE, never PIPE (the CLI's TIMING/TRACE stderr is large).
        fd, log_path = tempfile.mkstemp(prefix='asr-', suffix='.log')
        os.close(fd)
        # The converted-input temp is allocated HERE (not in preflight) so it is cleaned on
        # EVERY path by the finally below — a failure before run() can never orphan it.
        temp_audio = None
        try:
            if prepared.get('needs_conversion'):
                fd, temp_audio = tempfile.mkstemp(prefix='asr_in-', suffix='.wav')
                os.close(fd)
            with open(log_path, 'ab') as lf:
                # Input normalisation (only when preflight flagged a conversion): ffmpeg runs
                # INSIDE the generation budget; the ASR command then gets the remainder.
                if temp_audio:
                    remaining = timeout - (time.monotonic() - start)
                    if remaining <= 0:
                        raise TimeoutError(
                            f"voice_to_text generation budget ({timeout}s) exhausted before "
                            f"input conversion (log: {log_path})"
                        )
                    conv = [ffmpeg, '-y', '-i', audio_src,
                            '-ar', str(_TARGET_SR), '-ac', '1', '-f', 'wav', temp_audio]
                    try:
                        conv_res = subprocess.run(conv, stdout=lf, stderr=subprocess.STDOUT,
                                                 timeout=remaining)
                    except subprocess.TimeoutExpired:
                        raise TimeoutError(
                            f"voice_to_text input conversion timed out after {timeout}s "
                            f"(log: {log_path})"
                        )
                    # A failed conversion is NOT a transcription failure — report it distinctly
                    # so a corrupt/unsupported input is not misdiagnosed as "no transcript".
                    if conv_res.returncode != 0:
                        raise RuntimeError(
                            f"voice_to_text input conversion failed (ffmpeg exit "
                            f"{conv_res.returncode}) (log: {log_path})"
                        )
                    # Point the ASR command at the converted temp (preflight's argv targeted
                    # the source, which always exists; only run() knows the temp).
                    argv[argv.index('--audio') + 1] = temp_audio

                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    raise TimeoutError(
                        f"voice_to_text generation budget ({timeout}s) exhausted before "
                        f"transcription (log: {log_path})"
                    )
                try:
                    proc = subprocess.run(argv, stdout=lf, stderr=subprocess.STDOUT,
                                          timeout=remaining)
                except subprocess.TimeoutExpired:
                    raise TimeoutError(
                        f"voice_to_text ASR timed out after {timeout}s (log: {log_path})"
                    )
                rc = proc.returncode

            # Success = the --text-out file is NON-EMPTY. NEVER the exit code (the CLI may
            # exit non-zero even on success). A missing or empty transcript is a failure.
            transcript = ''
            if os.path.exists(text_out):
                with open(text_out, 'r', encoding='utf-8', errors='replace') as f:
                    transcript = f.read().strip()
            if not transcript:
                raise RuntimeError(
                    f"voice_to_text produced no transcript: the --text-out file was empty "
                    f"or missing (exit {rc}) (log: {log_path})"
                )
            return MediaResult(text=transcript, meta={
                'model': prepared.get('model', ''),
                'family': prepared.get('family', ''),
                'chars': len(transcript),
            })
        finally:
            # Clean the converted-input temp and the log on EVERY path. The transcript file
            # (out_path) is cleaned by media_gen's finally, not here.
            if temp_audio:
                try:
                    os.remove(temp_audio)
                except OSError:
                    pass
            try:
                os.remove(log_path)
            except OSError:
                pass
