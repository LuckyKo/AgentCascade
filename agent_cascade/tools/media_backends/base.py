"""
Backend interface for the generic ``media_gen`` tool.

``media_gen`` is a multi-media generation tool (image / video / audio) built on
the ``image_gen`` tool as its prototype. This module defines the *contract* every
generation backend implements:

- ``MediaRequest``  — a normalised, backend-agnostic request built from tool params.
- ``MediaResult``   — a normalised result (raw bytes + extension + metadata).
- ``MediaBackend``  — the ABC a backend subclasses (preflight / run / describe).

Design notes (see plans/t167_media_gen_PLAN.md §2.2):

- ``preflight(req)`` runs STRICTLY BEFORE any VRAM work. It resolves config,
  model/workflow files, and the ``reference_file`` security seam (via
  ``resolve_tool_path``), and builds the argv/workflow. It raises on any failure
  so the caller never saves/unloads model state for a request that cannot run.
- ``run(req)`` does the actual generation (subprocess / HTTP) and returns a
  ``MediaResult``. It uses whatever the backend stashed in ``req.prepared``.

Backend selection is explicit (the agent names the backend per-call); there is no
AUTO resolver, so availability is validated inside each backend's ``preflight``
(fast-fail, pre-VRAM) rather than by a separate candidate-filtering step.

The canonical media-type vocabulary is exactly three types. TTS and music are
*audio* (a backend/config distinction, not an output type), so both return
``ContentItem(audio=...)``. Do NOT introduce 'music'/'tts' as media types.
"""

from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import json

# Canonical media-type vocabulary. Exactly three types.
MEDIA_TYPES = ('image', 'video', 'audio')

# ── Unified media-gen timeout ─────────────────────────────────────────────────
# The single source of truth for a render's timeout is the top-level UI value
# (config['timeout']). Every backend resolves it identically through
# resolve_media_timeout() so ONE UI setting is respected uniformly across all
# modes (image / video / audio / TTS). This shared default replaces the old
# per-backend magic numbers (600 / 900 / 180) and the kobold_tts clamp-up.
# 900s is the largest previous per-backend default — a safe floor so slow
# TTS/image renders still work when the user has not set an explicit value.
DEFAULT_MEDIA_TIMEOUT = 900.0


def resolve_media_timeout(config: Optional[dict], block: Optional[dict] = None) -> float:
    """Resolve the effective media-gen timeout (seconds), identically for every backend.

    Precedence (documented, applied in every backend):
      1. per-backend ``block['timeout']`` — a user-settable config key
         (e.g. ``comfyui.timeout`` / ``sdcpp.timeout`` / ``kobold_tts.timeout``).
      2. top-level ``config['timeout']`` — the single UI value (source of truth).
      3. ``DEFAULT_MEDIA_TIMEOUT`` — ONE shared fallback (900s), so slow TTS/image
         renders still work when the user has not set an explicit value.

    A value that fails ``int()`` coercion (None, non-numeric) falls to the shared
    default. Note the precedence is via ``dict.get``'s default argument: a *missing*
    key falls through to the next level, but a key present with a ``None`` value does
    NOT — it resolves to the shared default (e.g. ``block={'timeout': None}`` → 900,
    skipping ``config['timeout']``). No clamping is applied — the user's value is
    respected as-is (the old kobold_tts clamp-up-to-600 is gone, so a short UI value
    is honoured uniformly across all backends).
    """
    block = block or {}
    cfg = config or {}
    candidate = block.get('timeout', cfg.get('timeout', DEFAULT_MEDIA_TIMEOUT))
    try:
        return float(int(candidate))
    except (TypeError, ValueError):
        return float(DEFAULT_MEDIA_TIMEOUT)


def resolve_release_model_around_call(config: Optional[dict], backend_id: str) -> bool:
    """Per-backend VRAM opt-out. Block key > manifest default (True) > True.

    Precedence mirrors :func:`resolve_media_timeout`: the per-backend block key
    ``config[backend_id]['release_model_around_call']`` wins; absent falls to the
    manifest default (every backend's manifest defaults this to True — the
    fallback here is that same True, kept literal so ``base.py`` stays a leaf
    with no import edge to the composed ``BACKEND_MANIFEST``). Explicit bools are
    honoured; a non-bool value is parsed only for obvious strings ('false'/'true',
    case-insensitive) — anything unparseable falls back to the default, so a
    malformed config never silently disables the dance.
    """
    block = (config or {}).get(backend_id) or {}
    if 'release_model_around_call' not in block:
        return True
    value = block['release_model_around_call']
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('true', '1', 'yes', 'on'):
            return True
        if text in ('false', '0', 'no', 'off'):
            return False
    return True


# ── Shared TTS-backend plumbing (single source of truth) ──────────────────────
# The one-shot TTS backends (chatterbox / kobold_tts) share this plumbing. It
# lives here (the leaf contract module, stdlib-only) so neither backend
# re-implements it. See [[media-gen-kobold-tts-config-clean]].

def _project_root() -> Path:
    """AgentCascade project root (4 levels up from media_backends/).
    media_backends -> tools -> agent_cascade -> AgentCascade."""
    return Path(__file__).resolve().parents[3]


def resolve_tts_config_json(config: dict, block_key: str, label: str,
                            name: str | None) -> dict:
    """Load ``config_dir/<name>.json`` for a TTS backend block.

    Shared by the chatterbox and kobold_tts backends. ``block_key`` selects the
    config block (``'chatterbox'`` / ``'kobold_tts'``); ``label`` is the human-
    readable word used in error strings (``'Chatterbox'`` / ``'TTS'``). Falls
    back to the block's ``default_config`` when ``name`` is None. Raises
    ``RuntimeError`` (carrying ``label``) if unconfigured/missing/malformed.
    """
    block = (config or {}).get(block_key) or {}
    cfg_path = resolve_block_dir(block, 'config_dir')
    if cfg_path is None:
        raise RuntimeError(
            f'No {label} config directory configured. Set {block_key}.config_dir '
            f'in media_gen.json (config/media_gen/tts).'
        )
    cfg_name = name or block.get('default_config')
    if not cfg_name:
        raise RuntimeError(
            f"No {label} voice config specified and no default configured. Pass "
            f"config='<name>' or set {block_key}.default_config in media_gen.json."
        )
    cfg_path = cfg_path / f"{cfg_name}.json"
    if not cfg_path.is_file():
        raise RuntimeError(f"{label} config file not found: {cfg_path}")
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"Invalid {label} config '{cfg_path}': {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} config '{cfg_path}' must be a JSON object")
    return data


# ── Voice-sample discovery (voices_dir / default_voice feature) ───────────────
#: Audio file extensions recognised for voice samples.
AUDIO_EXTS = {'.wav', '.mp3', '.ogg', '.flac'}


def resolve_block_dir(block: dict, key: str) -> Path | None:
    """Resolve a directory key from a config block against the project root.
    Returns None if the key is missing/empty/not a string."""
    val = block.get(key)
    if not isinstance(val, str) or not val.strip():
        return None
    p = Path(val)
    if not p.is_absolute():
        p = _project_root() / p
    return p


def list_voice_samples(directory: Path, cap: int | None = None) -> list[str]:
    """Sorted audio filenames in a directory. Never raises.

    ``cap`` bounds the returned list (None = uncapped). Callers that print a
    listing pass their own display cap; the uncapped result gives an honest total.
    """
    try:
        names = sorted(
            f.name for f in directory.iterdir()
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS
        )
    except OSError:
        return []
    return names[:cap]


# RIFF/WAVE header magic (bytes 0-3 'RIFF', bytes 8-11 'WAVE'). Only meaningful
# for the 'wav' container; mp3/opus have no universal magic.
RIFF_MAGIC = b'RIFF'
WAVE_MAGIC = b'WAVE'


def is_wav_bytes(data: bytes) -> bool:
    """True if ``data`` looks like a real WAV (non-empty + RIFF/WAVE magic)."""
    return (isinstance(data, (bytes, bytearray)) and len(data) >= 12
            and bytes(data[:4]) == RIFF_MAGIC and bytes(data[8:12]) == WAVE_MAGIC)


@dataclass
class MediaRequest:
    """A normalised, backend-agnostic generation request.

    Built once from tool params and passed through ``preflight`` → ``run``.
    ``preflight`` populates ``prepared`` with backend-specific resolved state
    (model config, argv, workflow, temp output path, …) that ``run`` consumes.
    """

    # ── Identity / selection ────────────────────────────────────────────────
    media_type: str = ''                 # one of MEDIA_TYPES (derived, not caller-supplied)
    backend_id: str = ''                 # explicit backend id (required; no AUTO)
    config_name: Optional[str] = None    # config/workflow/model filename stem

    # ── Content ─────────────────────────────────────────────────────────────
    prompt: str = ''
    negative_prompt: str = ''

    # ── Per-call overrides (D7: reference_file + media_size are core) ──────
    reference_file: Optional[str] = None  # SECURITY seam: resolved via resolve_tool_path
    media_size: Optional[dict] = None    # {'width': int, 'height': int, ...}
    overrides: Optional[dict] = None     # free-form backend-specific overrides

    # ── Resolved / populated at call time ───────────────────────────────────
    config: dict = None                  # the full media_gen config dict (read by backends)
    extra: dict = None                   # escape hatch for backend-specific inputs
    prepared: dict = None                # backend-populated after preflight()
    agent_pool: Optional[object] = None  # agent pool, for resolve_tool_path (reference_file)

    def as_dict(self) -> dict:
        """Return a plain dict of the request (for logging / debugging)."""
        return {
            'media_type': self.media_type,
            'backend_id': self.backend_id,
            'config_name': self.config_name,
            'prompt': self.prompt,
            'negative_prompt': self.negative_prompt,
            'reference_file': self.reference_file,
            'media_size': self.media_size,
            'overrides': self.overrides,
        }


@dataclass
class MediaResult:
    """A normalised generation result: raw bytes + extension + metadata."""
    data: bytes = b''
    ext: str = ''
    meta: dict = None
    elapsed: float = 0.0


class MediaBackend(ABC):
    """Base class every ``media_gen`` backend implements.

    A backend declares its identity and capabilities as class attributes, then
    implements the four methods below. Backends are stateless; all per-call state
    lives on the :class:`MediaRequest` (see ``prepared``).
    """

    id: str = ''                 # unique backend id (registry key)
    media_types: tuple = ()      # subset of MEDIA_TYPES this backend can serve
    manifest = None              # optional BackendManifest presentation entry (.manifest module);
                                 # REQUIRED for registration — build_manifest raises without it.

    def preflight(self, req: MediaRequest) -> None:
        """Validate + resolve everything BEFORE any VRAM work.

        Must resolve the ``reference_file`` security seam via ``resolve_tool_path``
        before any upload/subprocess. Raises ``RuntimeError``/``ValueError``/``OSError``
        on any failure; the caller turns that into an ERROR result without touching
        VRAM. On success, stash resolved state in ``req.prepared``.
        """
        raise NotImplementedError

    def run(self, req: MediaRequest) -> MediaResult:
        """Execute the generation using ``req.prepared``; return a MediaResult."""
        raise NotImplementedError

    def describe(self) -> str:
        """Human-readable one-line description for error listings."""
        return self.id
