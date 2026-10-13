"""
Kobold TTS backend for ``media_gen`` — one-shot GGUF via ``koboldcpp.exe``.

Shape mirrors the sdcpp local-binary backend (``server_backed=False``,
``requires_vram=True``): ``preflight()`` resolves the binary + GGUF model path(s)
to absolute and checks they exist BEFORE any VRAM touch (invariant I1), then
builds the argv. ``run()`` executes it with ``subprocess.run(argv, shell=False,
capture_output=True)`` and reads the WAV from **stdout** — the one-shot ``-p``
mode has no WAV output-file flag (confirmed empirically, plan §6.2/§6.3 +
memory ``koboldcpp-oneshot-tts-probe``), so we capture ``result.stdout`` and
validate it is a real WAV (non-empty + RIFF/WAVE magic) before returning.

Confirmed argv (plan §6.2): ``koboldcpp.exe --ttsmodel <gguf>
[--ttswavtokenizer <gguf>] --ttsgpu -p "<text>"``. Generation is SLOW (a short
sentence did not finish in 180 s on first run), so the default timeout is >=600 s.

TTS produces *audio* (one of the three canonical media types), not a 'tts' type.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from .base import MediaBackend, MediaRequest, MediaResult, resolve_media_timeout

from agent_cascade.utils.tool_path_resolver import resolve_tool_path

# RIFF/WAVE header magic (bytes 0-3 'RIFF', bytes 8-11 'WAVE').
_RIFF_MAGIC = b'RIFF'
_WAVE_MAGIC = b'WAVE'


def _resolve_tts_config(config: dict, name: str | None) -> dict:
    """Load a per-voice TTS config JSON (``config_dir/<name>.json``).

    Mirrors ``_resolve_sdcpp_config``: resolves the config stem to a file under
    the ``kobold_tts.config_dir`` directory (falling back to ``default_config``
    when no name is given), reads it, and returns the parsed dict. Raises
    ``RuntimeError`` if no config is configured or the file is missing/malformed.
    """
    block = (config or {}).get('kobold_tts') or {}
    cfg_dir = block.get('config_dir')
    if not cfg_dir:
        raise RuntimeError(
            'No TTS config directory configured. Set kobold_tts.config_dir in '
            'media_gen.json (config/media_gen/tts).'
        )
    cfg_name = name or block.get('default_config')
    if not cfg_name:
        raise RuntimeError(
            'No TTS voice config specified and no default configured. Pass config='
            "='<name>' or set kobold_tts.default_config in media_gen.json."
        )
    # config_dir may be project-root-relative or absolute; join then resolve.
    cfg_path = Path(cfg_dir)
    if not cfg_path.is_absolute():
        # Project root is three levels up from this module (media_backends →
        # media_backends pkg → tools → agent_cascade → root). Match media_gen.py.
        root = Path(__file__).resolve().parents[4]
        cfg_path = root / cfg_path
    cfg_path = cfg_path / f"{cfg_name}.json"
    if not cfg_path.is_file():
        raise RuntimeError(f"TTS config file not found: {cfg_path}")
    try:
        import json
        with open(cfg_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"Invalid TTS config '{cfg_path}': {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"TTS config '{cfg_path}' must be a JSON object")
    return data


class KoboldTTSBackend(MediaBackend):
    id = 'kobold_tts'
    media_types = ('audio',)
    server_backed = False  # local one-shot binary, NOT an HTTP server (plan §6.2)
    requires_vram = True

    def describe(self) -> str:
        return 'Kobold TTS (koboldcpp one-shot GGUF)'

    def is_available(self, req: MediaRequest) -> bool:
        # Capability gate (cheap, non-IO): the block declares it can produce audio.
        # Existence of the binary/model is enforced in preflight (I1), not here.
        block = (req.config or {}).get('kobold_tts')
        if not isinstance(block, dict):
            return False
        mt = block.get('media_types')
        return isinstance(mt, dict) and bool(mt.get('audio'))

    def preflight(self, req: MediaRequest) -> None:
        block = (req.config or {}).get('kobold_tts') or {}

        # ── Resolve the binary to an absolute path and check it exists (I1). ──
        binary = block.get('binary')
        if not binary or not isinstance(binary, str):
            raise RuntimeError(
                'No koboldcpp binary configured. Set kobold_tts.binary in '
                'media_gen.json (e.g. N:/work/stuff/Beta/koboldcpp.exe).'
            )
        binary_path = Path(binary)
        if not binary_path.is_absolute():
            root = Path(__file__).resolve().parents[4]
            binary_path = root / binary_path
        if not binary_path.is_file():
            raise RuntimeError(f"koboldcpp binary not found: {binary_path}")

        # ── Resolve the voice config + GGUF model paths (before any VRAM). ──
        tts_cfg = _resolve_tts_config(req.config, req.config_name)
        model_name = tts_cfg.get('model')
        if not model_name or not isinstance(model_name, str):
            raise RuntimeError("TTS config is missing a 'model' GGUF path.")
        models_dir = block.get('models_dir') or ''
        model_path = Path(models_dir) / model_name
        if not model_path.is_absolute():
            root = Path(__file__).resolve().parents[4]
            model_path = root / model_path
        if not model_path.is_file():
            raise RuntimeError(f"TTS model GGUF not found: {model_path}")

        companion_name = tts_cfg.get('companion_model')
        companion_path = None
        if companion_name and isinstance(companion_name, str):
            companion_path = Path(models_dir) / companion_name
            if not companion_path.is_absolute():
                root = Path(__file__).resolve().parents[4]
                companion_path = root / companion_path
            if not companion_path.is_file():
                raise RuntimeError(f"TTS companion GGUF not found: {companion_path}")

        # ── reference_file → voice-cloning reference audio (SECURITY seam). ──
        # Resolved via resolve_tool_path BEFORE any subprocess (audit defect 5).
        ref_dir = None
        if req.reference_file:
            if not isinstance(req.reference_file, str) or not req.reference_file.strip():
                raise RuntimeError("'reference_file' must be a non-empty string path.")
            try:
                ref_path = resolve_tool_path(req.reference_file.strip(), mode='ro',
                                             agent_pool=req.agent_pool)
            except ValueError as e:
                raise RuntimeError(f"Invalid reference_file path: {e}") from e
            if not ref_path.is_file():
                raise RuntimeError(f"reference_file file not found: {req.reference_file}")
            # koboldcpp voice-cloning takes a directory (--ttsdir).
            ref_dir = str(ref_path.parent)

        # ── Timeout: unified resolver (kobold_tts.timeout > top-level timeout > 900). ──
        # One UI value (config['timeout']) is respected identically across all backends
        # (see base.resolve_media_timeout); the old clamp-up-to-600 is gone so a short
        # UI value is honoured uniformly.
        timeout = resolve_media_timeout(req.config, block)

        # ── Build argv (pure; never shell=True). ──
        argv = [str(binary_path), '--ttsmodel', str(model_path)]
        if companion_path:
            argv += ['--ttswavtokenizer', str(companion_path)]
        argv += ['--ttsgpu']
        if ref_dir:
            argv += ['--ttsdir', ref_dir]
        argv += ['-p', req.prompt]

        req.prepared = {
            'argv': argv,
            'timeout': timeout,
            'config_name': req.config_name or block.get('default_config', ''),
        }

    def run(self, req: MediaRequest) -> MediaResult:
        argv = req.prepared['argv']
        timeout = req.prepared['timeout']
        try:
            result = subprocess.run(argv, shell=False, capture_output=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"TTS generation timed out after {timeout}s")
        if result.returncode != 0:
            raise RuntimeError(
                f"koboldcpp TTS failed (exit {result.returncode}): "
                f"{(result.stderr or b'').decode('utf-8', 'replace')[:300]}"
            )
        wav = result.stdout
        if not _is_wav_bytes(wav):
            raise RuntimeError(
                'koboldcpp produced no WAV on stdout (empty or non-WAV output).'
            )
        return MediaResult(data=wav, ext='wav', meta={})


def _is_wav_bytes(data: bytes) -> bool:
    """True if ``data`` looks like a real WAV (non-empty + RIFF/WAVE magic)."""
    return (isinstance(data, (bytes, bytearray)) and len(data) >= 12
            and bytes(data[:4]) == _RIFF_MAGIC and bytes(data[8:12]) == _WAVE_MAGIC)
