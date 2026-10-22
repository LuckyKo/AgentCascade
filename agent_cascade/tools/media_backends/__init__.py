"""
Backend registry for ``media_gen``.

The registry is an EXPLICIT Python dict of backend instances keyed by id — NOT
filesystem scanning. Backends are added by importing them here and registering
them; new backends (video, TTS, music) slot in by adding one line.

Backend selection is EXPLICIT: the caller must name a backend (``comfyui`` /
``sdcpp`` / ``kobold_tts``); there is no AUTO magic and no config-ownership
reverse index. ``media_type`` is DERIVED, never caller-supplied: single-type
backends take their only type, and comfyui infers its type from the loaded
workflow in ``preflight`` (YuE2 nodes → audio, ``SaveVideo`` → video, else image).
"""

from .base import MEDIA_TYPES, MediaBackend  # noqa: F401 (re-export)

# ── Explicit registry (NOT filesystem scanning) ──────────────────────────────
# Import the backend classes and instantiate them. New backends are added by
# importing their class and adding it to _BACKEND_CLASSES.
from .sdcpp import SdcppBackend
from .comfyui import ComfyUIBackend
# TTS (step 6) / music (step 7) / video (step 5) slot in here later.
from .kobold_tts import KoboldTTSBackend  # STUB backend: capabilities + config ownership only
from .chatterbox import ChatterboxBackend  # one-shot CLI TTS (MP3 to stdout)
from .voice_to_text import VoiceToTextBackend  # one-shot CLI ASR (audio → text, output_kind='text')

_BACKEND_CLASSES = (SdcppBackend, ComfyUIBackend, KoboldTTSBackend, ChatterboxBackend,
                    VoiceToTextBackend)

BACKENDS: dict = {}  # id -> backend instance


def register_backend(backend_cls) -> None:
    """Instantiate and register a backend class under its declared ``id``."""
    instance = backend_cls()
    if not instance.id:
        raise ValueError("backend class must declare a non-empty 'id'")
    if instance.id in BACKENDS:
        raise ValueError(f"backend id '{instance.id}' already registered")
    BACKENDS[instance.id] = instance


def get_backend(backend_id: str):
    """Return the registered backend instance by id, or None."""
    return BACKENDS.get(backend_id)


def resolve_backend(req):
    """Resolve a concrete backend for ``req`` and set ``req.media_type``.

    Backend selection is EXPLICIT — the caller must name a backend; there is no
    AUTO magic. Raises ``RuntimeError`` on any resolution failure (the caller's
    pre-VRAM ``except`` turns that into an ``ERROR:`` ContentItem with zero VRAM
    touched).

    ``media_type`` is DERIVED, never a tool parameter:
      - single-type backends (sdcpp=image, kobold_tts=audio) take their only type.
      - comfyui (image/video/audio) infers its type from the loaded workflow in
        ``preflight`` (YuE2 nodes → audio, ``SaveVideo`` → video, else image), so
        we leave it unset here and let ``preflight`` set it.
    """
    if not req.backend_id:
        raise RuntimeError(
            "no backend specified. Pass backend='" + "'|'".join(manifest_ids()) + "' "
            "(or call with prompt='__help' to list what each backend produces)."
        )
    backend = get_backend(req.backend_id)
    if not backend:
        raise RuntimeError(
            f"unknown backend '{req.backend_id}'. Available: {', '.join(BACKENDS)}"
        )
    # Defensive: the tool never sets req.media_type, but direct MediaRequest callers
    # may. If one is present, it must be a type this backend can actually produce.
    if req.media_type and req.media_type not in backend.media_types:
        raise RuntimeError(f"backend '{req.backend_id}' cannot produce '{req.media_type}'")
    if not req.media_type and len(backend.media_types) == 1:
        req.media_type = backend.media_types[0]
    # Multi-type backends (comfyui) derive their type from the workflow in preflight.
    return backend


# Populate the registry at import time.
for _cls in _BACKEND_CLASSES:
    register_backend(_cls)

# ── Data-driven presentation manifest — compose AFTER the registry loop ───────
# (Composing at the BACKENDS declaration would evaluate over an empty dict →
# empty tool enum / empty UI tabs; see the W1 insertion-point trap.)
from .manifest import build_manifest

BACKEND_MANIFEST: dict = {bid: build_manifest(b) for bid, b in BACKENDS.items()}


def manifest_ids() -> list:
    """Backend ids in registry order — the tool schema enum and REST validation set."""
    return list(BACKEND_MANIFEST)
