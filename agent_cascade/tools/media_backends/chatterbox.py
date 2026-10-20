"""
Chatterbox TTS backend for ``media_gen`` — one-shot CLI, MP3 to stdout.

Shape mirrors the kobold_tts one-shot binary backend: ``preflight()`` resolves
the interpreter + CLI script paths to absolute and checks they exist BEFORE any
VRAM touch (invariant I1),
resolves the per-voice config JSON and the ``reference_file`` voice-cloning
reference (auto-supplied from ``voices_dir``/``default_voice`` when the agent
omits it; always validated via ``resolve_tool_path``, the SECURITY seam), then builds
the argv AND the child-process contract — ``env`` (``HF_HOME``/``HF_HUB_CACHE``
point at ``<repo>/models``, see below) and ``cwd`` = the repo dir. ``run()``
executes it with ``subprocess.run(argv, shell=False, capture_output=True,
env=..., cwd=...)`` and reads the audio from **stdout** — the one-shot CLI
emits raw audio bytes on stdout and all logs on stderr (confirmed empirically),
so we capture ``result.stdout``. mp3/opus have no single universal magic header,
so we validate only that the payload is non-empty and of a reasonable size; for
'wav' we additionally require the RIFF/WAVE magic (mirrors kobold_tts) so a
corrupt/truncated WAV is not returned as success.

Confirmed argv (verified externally): ``<python> <chatterbox_tts.py> --text=<text>
--reference=<path>`` plus optional ``--device``, ``--output-format``,
``--temperature``, ``--exaggeration``, ``--cfg-weight``, ``--seed``,
``--target-sample-rate`` — all in single-token ``--flag=value`` form so a value
starting with '-' is not misparsed as an option. Exit 0 on success; non-zero
otherwise (stderr carries the reason). Generation is SLOW (model cold-start), so
the default timeout is >=600 s.

Child-process contract (see ``preflight``): the CLI must run with ``cwd`` = the
repo dir and ``HF_HOME``/``HF_HUB_CACHE`` = ``<repo>/models`` — the same env the
repo's own launcher exports. Without them ``huggingface_hub`` resolves the model
snapshot to the default user cache (where no snapshot exists) while ``engine.py``
forces offline mode, so the load dies with ``LocalEntryNotFoundError``; and the
repo's CWD-relative ``config.yaml`` is never found (its missing-file branch dumps
defaults into our workspace and silently turns device selection into 'auto').
``env`` therefore REPLACES the environment via ``{**os.environ, ...}`` — a bare
dict would drop ``PATH`` and break the pydub→FFmpeg mp3 encode.

TTS produces *audio* (one of the three canonical media types), not a 'tts' type.
"""

import os
import subprocess
from pathlib import Path

from .base import (
    MediaBackend, MediaRequest, MediaResult, resolve_media_timeout,
    _project_root, resolve_tts_config_json, is_wav_bytes,
    resolve_block_dir, list_voice_samples,
)
from .manifest import BackendManifest, FieldSpec

from agent_cascade.utils.tool_path_resolver import resolve_tool_path

# Voice-config keys (snake_case, as they appear in the voice JSON) → CLI flag.
# Presence (not truthiness) gates the flag: seed=0 and cfg_weight=0.0 are
# legitimate values that MUST be passed.
_FLAG_KEYS = {
    'device': '--device',
    'temperature': '--temperature',
    'exaggeration': '--exaggeration',
    'cfg_weight': '--cfg-weight',
    'seed': '--seed',
    'target_sample_rate': '--target-sample-rate',
    'output_format': '--output-format',
}

# Accepted output containers; the returned ext mirrors whichever is requested.
_OUTPUT_FORMATS = ('mp3', 'wav', 'opus')

# A valid MP3 frame is hundreds of bytes; this tiny floor only guards against a
# stray stderr leak masquerading as stdout, it is NOT a format/magic check.
_MIN_AUDIO_BYTES = 128


def _resolve_tts_config(config: dict, name: str | None) -> dict:
    """Load a per-voice Chatterbox config JSON (``config_dir/<name>.json``).

    Thin wrapper over the shared :func:`base.resolve_tts_config_json` for the
    ``chatterbox`` config block; see that function for the resolution rules and
    error contract.
    """
    return resolve_tts_config_json(config, 'chatterbox', 'Chatterbox', name)


class ChatterboxBackend(MediaBackend):
    id = 'chatterbox'
    media_types = ('audio',)

    manifest = BackendManifest(
        label='Chatterbox TTS',
        note='Chatterbox-TTS-Server one-shot CLI — speech (MP3). Local subprocess.',
        connection_fields=(
            FieldSpec(key='python', label='Python Interpreter', kind='path', required=True,
                      placeholder='.../chatterbox_env/Scripts/python.exe'),
            FieldSpec(key='cli_script', label='CLI Script', kind='path', required=True,
                      placeholder='.../chatterbox_tts.py'),
            FieldSpec(key='config_dir', label='Voice Configs', kind='path',
                      placeholder='<AgentCascade_root>/config/media_gen/tts',
                      help='Directory of JSON voice config files. Each .json defines generation '
                           'parameters (temperature, exaggeration, etc.) for a named voice preset. '
                           'Create .json files here to add new presets.'),
            FieldSpec(key='voices_dir', label='Voice Samples', kind='path',
                      placeholder='<path>/voices',
                      help='Directory of reference audio files (WAV/MP3/OGG/FLAC) for voice '
                           'cloning. The default voice is auto-selected from here when the agent '
                           'does not pass reference_file.'),
        ),
        advanced_fields=(
            FieldSpec(key='default_config', label='Default Voice', kind='text',
                      placeholder='chatterbox_default'),
            FieldSpec(key='default_voice', label='Default Reference Audio', kind='text',
                      placeholder='speaker_a.wav',
                      help='Filename in Voice Samples dir used when no reference_file is passed. '
                           'Leave empty to require an explicit reference_file.'),
            FieldSpec(key='timeout', label='Timeout (s)', kind='number',
                      min=60, max=3600, step=30),
            FieldSpec(key='release_model_around_call',
                      label='Free VRAM around the call', kind='checkbox'),
        ),
        availability='config_dir',
        agent_args=('config', 'reference_file', 'overrides'),
        overrides_keys=('device', 'temperature', 'exaggeration', 'cfg_weight',
                        'seed', 'target_sample_rate', 'output_format'),
        usage_hints=(('audio', 'prompt="<text to speak>", '
                              'reference_file="<voice-clip.wav>"'),),
        defaults={'release_model_around_call': True},
    )

    def describe(self) -> str:
        return 'Chatterbox TTS (one-shot CLI, MP3)'

    def preflight(self, req: MediaRequest) -> None:
        block = (req.config or {}).get('chatterbox') or {}

        # ── Resolve the interpreter to an absolute path and check it exists (I1). ──
        python = block.get('python')
        if not python or not isinstance(python, str):
            raise RuntimeError(
                'No Chatterbox interpreter configured. Set chatterbox.python in '
                'media_gen.json (e.g. N:/work/WD/Chatterbox-TTS-Server/'
                'chatterbox_env/Scripts/python.exe).'
            )
        python_path = Path(python)
        if not python_path.is_absolute():
            python_path = _project_root() / python_path
        if not python_path.is_file():
            raise RuntimeError(f"Chatterbox interpreter not found: {python_path}")

        # ── Resolve the CLI script to an absolute path and check it exists (I1). ──
        cli_script = block.get('cli_script')
        if not cli_script or not isinstance(cli_script, str):
            raise RuntimeError(
                'No Chatterbox CLI script configured. Set chatterbox.cli_script in '
                'media_gen.json (e.g. N:/work/WD/Chatterbox-TTS-Server/'
                'chatterbox_tts.py).'
            )
        cli_path = Path(cli_script)
        if not cli_path.is_absolute():
            cli_path = _project_root() / cli_path
        if not cli_path.is_file():
            raise RuntimeError(f"Chatterbox CLI script not found: {cli_path}")

        # ── Child-process contract: env + cwd for the one-shot child. The WHY
        #     (HF cache root, offline mode, the CWD-relative config.yaml) is in
        #     the module docstring's "Child-process contract" paragraph. ──
        repo_dir = cli_path.parent
        # ``hf_home`` is an OPTIONAL, deliberately UNDECLARED override key (same
        # precedent as kobold_tts's ready_timeout / port_range). A relative value
        # resolves against repo_dir, NOT _project_root(): this cache belongs to
        # the repo (and cwd is the repo), unlike the block's other path keys.
        hf_home_override = block.get('hf_home')
        if isinstance(hf_home_override, str) and hf_home_override.strip():
            hf_home = Path(hf_home_override.strip())
            if not hf_home.is_absolute():
                hf_home = repo_dir / hf_home
        else:
            hf_home = repo_dir / 'models'
        env = {
            **os.environ,  # env= REPLACES the child env: keep PATH (pydub→FFmpeg mp3)
            'HF_HOME': str(hf_home),
            'HF_HUB_CACHE': str(hf_home),
            'HF_HUB_OFFLINE': '1',
            'TRANSFORMERS_OFFLINE': '1',
            'HF_HUB_DISABLE_TELEMETRY': '1',
        }

        # ── Resolve the voice config (before any VRAM). ──
        tts_cfg = _resolve_tts_config(req.config, req.config_name)

        # ── Merge defaults: CLI defaults < voice-config values < explicit overrides. ──
        # The CLI's own defaults are implicit (we simply omit those flags), so the
        # merged dict only ever carries keys the voice config or overrides set.
        merged: dict = {}
        for key in _FLAG_KEYS:
            if key in tts_cfg:
                merged[key] = tts_cfg[key]
        if isinstance(req.overrides, dict):
            for key in _FLAG_KEYS:
                if key in req.overrides:
                    merged[key] = req.overrides[key]

        # ── Output container → returned ext (default mp3). ──
        out_fmt = merged.get('output_format', 'mp3')
        if out_fmt not in _OUTPUT_FORMATS:
            raise RuntimeError(
                f"Unsupported output-format '{out_fmt}'. "
                f"Use one of {', '.join(_OUTPUT_FORMATS)}."
            )
        ext = out_fmt

        # ── reference_file → voice-cloning reference audio (SECURITY seam). ──
        # Voice cloning always needs a reference. If the agent did not pass one,
        # fall back to voices_dir/default_voice from the config block.
        ref_str = req.reference_file if isinstance(req.reference_file, str) else ''
        # A reference auto-supplied from config (not passed by the agent) must report
        # a seam rejection in terms of the config keys, not reference_file.
        auto_supplied_from = ''
        if not ref_str.strip():
            vd = resolve_block_dir(block, 'voices_dir')
            default_voice = block.get('default_voice')
            if vd and isinstance(default_voice, str) and default_voice.strip():
                ref_str = str(vd / default_voice.strip())
                auto_supplied_from = (f"default_voice '{default_voice.strip()}' in "
                                      f"voices_dir '{vd}'")
            elif vd:
                # voices_dir configured but no default_voice: list what's available.
                available = list_voice_samples(vd, cap=10)
                hint = f" Available: {', '.join(available)}" if available else ''
                raise RuntimeError(
                    "Chatterbox requires a 'reference_file' (voice-cloning reference "
                    f"audio). Pass reference_file='<path>' or set chatterbox.default_voice.{hint}"
                )
            else:
                raise RuntimeError(
                    "Chatterbox requires a 'reference_file' (voice-cloning reference "
                    "audio). Pass reference_file='<path-to-audio>' or configure "
                    'chatterbox.voices_dir + chatterbox.default_voice.'
                )
        ref_str = ref_str.strip()
        try:
            ref_path = resolve_tool_path(ref_str, mode='ro', agent_pool=req.agent_pool)
        except ValueError as e:
            if auto_supplied_from:
                raise RuntimeError(
                    f"Chatterbox {auto_supplied_from} is not an allowed path: {e}. "
                    'Add the voices directory to your allowed paths, or pass '
                    "reference_file='<path>' explicitly."
                ) from e
            raise RuntimeError(f"Invalid reference_file path: {e}") from e
        if not ref_path.is_file():
            if auto_supplied_from:
                available = list_voice_samples(ref_path.parent, cap=10)
                hint = f" Available: {', '.join(available)}" if available else ''
                raise RuntimeError(
                    f"Chatterbox {auto_supplied_from} was not found at {ref_path}."
                    f"{hint} Check the filename or pass reference_file='<path>' "
                    'explicitly.'
                )
            raise RuntimeError(f"reference_file file not found: {ref_str}")

        # ── Timeout: unified resolver (chatterbox.timeout > top-level timeout > 900). ──
        timeout = resolve_media_timeout(req.config, block)

        # ── Build argv (pure; never shell=True). ──
        # Single-token --flag=value form: in the two-token form a value starting
        # with '-' (TTS text is arbitrary user input) would be misparsed as an
        # option by argparse. The CLI accepts both forms, so we emit the safe one.
        argv = [str(python_path), str(cli_path),
                f'--text={req.prompt}', f'--reference={ref_path}']
        for key, flag in _FLAG_KEYS.items():
            if key in merged:
                argv += [f'{flag}={merged[key]}']

        req.prepared = {
            'argv': argv,
            'timeout': timeout,
            'ext': ext,
            'config_name': req.config_name or block.get('default_config', ''),
            'env': env,
            'cwd': str(repo_dir),
            'reference': ref_path.name,  # voice identity for feedback
        }

    def run(self, req: MediaRequest) -> MediaResult:
        argv = req.prepared['argv']
        timeout = req.prepared['timeout']
        try:
            # .get(): env/cwd are optional in ``prepared`` (hand-built test fixtures
            # and any older prepared payload may omit them; None ⇒ inherit as before).
            result = subprocess.run(argv, shell=False, capture_output=True,
                                    timeout=timeout,
                                    env=req.prepared.get('env'),
                                    cwd=req.prepared.get('cwd'))
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"Chatterbox TTS generation timed out after {timeout}s")
        if result.returncode != 0:
            raise RuntimeError(
                f"chatterbox TTS failed (exit {result.returncode}): "
                f"{(result.stderr or b'').decode('utf-8', 'replace')[:300]}"
            )
        audio = result.stdout
        if not _is_audio_bytes(audio, req.prepared['ext']):
            raise RuntimeError(
                'chatterbox produced no audio on stdout (empty or too small).'
            )
        return MediaResult(data=audio, ext=req.prepared['ext'], meta={})


def _is_audio_bytes(data: bytes, ext: str) -> bool:
    """True if ``data`` looks like real audio for the requested container.

    For 'wav' we additionally require the RIFF/WAVE magic so a corrupt/truncated
    WAV is not returned as success. mp3/opus have no single universal magic header,
    so we keep only the non-empty + size floor guard.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < _MIN_AUDIO_BYTES:
        return False
    if ext == 'wav':
        return is_wav_bytes(data)
    return True
