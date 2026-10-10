"""
Backend registry for ``media_gen``.

The registry is an EXPLICIT Python dict of backend instances keyed by id — NOT
filesystem scanning. Backends are added by importing them here and registering
them; new backends (video, TTS, music) slot in by adding one line.

The AUTO resolver (:func:`resolve_backend`) uses the **config-ownership reverse
index** (config filename stem → owning backend) as the PRIMARY discriminator
(plan §2.6 / audit defect 2). It does NOT fall back to dict-order ``live[0]``
when a config name is given — that ordering is arbitrary and would silently
mis-route a request. ``live[0]`` is used only when the request is genuinely
ambiguous (no config name and multiple live backends).
"""

from .base import MEDIA_TYPES, MediaBackend  # noqa: F401 (re-export)

# ── Explicit registry (NOT filesystem scanning) ──────────────────────────────
# Import the backend classes and instantiate them. New backends are added by
# importing their class and adding it to _BACKEND_CLASSES.
from .sdcpp import SdcppBackend
from .comfyui import ComfyUIBackend
# TTS (step 6) / music (step 7) / video (step 5) slot in here later.
from .kobold_tts import KoboldTTSBackend  # STUB backend: capabilities + config ownership only

_BACKEND_CLASSES = (SdcppBackend, ComfyUIBackend, KoboldTTSBackend)

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


def _build_config_owner(config: dict) -> dict:
    """Reverse index: config filename stem -> list of ``(backend_id, media_type)``.

    Reads each backend block's ``media_types`` lists (e.g.
    ``config['comfyui']['media_types']['image'] = ['flux2_t2i_9b_API', ...]``)
    and maps every declared config stem to the (backend, media-type) pair that
    owns it. This is the PRIMARY discriminator for AUTO resolution (audit defect 2):
    a config stem resolves to its owning backend *before* any type-based candidate
    list is consulted, so ``AUTO + config='kokoro_default'`` resolves to ``kobold_tts``
    regardless of registry ordering.
    """
    owner: dict = {}
    for backend_id, block in (config or {}).items():
        if not isinstance(block, dict):
            continue
        media_types = block.get('media_types')
        if not isinstance(media_types, dict):
            continue
        for mtype, names in media_types.items():
            if not isinstance(names, list):
                continue
            for name in names:
                if isinstance(name, str):
                    owner.setdefault(name, []).append((backend_id, mtype))
    return owner


def resolve_backend(req):
    """Resolve a concrete backend for ``req`` and set ``req.media_type``.

    Raises ``RuntimeError`` on any resolution failure (the caller's pre-VRAM
    ``except`` turns that into an ``ERROR:`` ContentItem with zero VRAM touched).

    Resolution order (plan §2.6):
      1. explicit ``backend_id`` (not 'AUTO') -> that backend.
      2. AUTO with a ``config`` -> resolve by config ownership (PRIMARY).
      3. AUTO without a config -> candidates by the declared media type.
      4. availability filter; >1 live is ambiguous (ask for explicit backend/config).
    """
    if req.backend_id and req.backend_id != 'AUTO':
        backend = get_backend(req.backend_id)
        if not backend:
            raise RuntimeError(
                f"unknown backend '{req.backend_id}'. Available: {', '.join(BACKENDS)}"
            )
        if req.media_type and req.media_type not in backend.media_types:
            raise RuntimeError(f"backend '{req.backend_id}' cannot produce '{req.media_type}'")
        if not req.media_type:
            req.media_type = backend.media_types[0]
        return backend

    owner_index = _build_config_owner(req.config)

    if req.config_name:
        owners = owner_index.get(req.config_name)
        if not owners:
            raise RuntimeError(
                f"config '{req.config_name}' is not declared by any backend in media_gen.json"
            )
        # Narrow by an explicit media-type hint if one was also given.
        if req.media_type:
            narrowed = [(b, mt) for (b, mt) in owners if mt == req.media_type]
            if narrowed:
                owners = narrowed
        if not req.media_type:
            req.media_type = owners[0][1]
        # Only keep backends that can actually produce the resolved media type.
        # Without this, a config stem owned by several backends under different
        # types could resolve to a backend that cannot serve req.media_type.
        candidates = [get_backend(b) for (b, _mt) in owners
                      if get_backend(b) and req.media_type in get_backend(b).media_types]
    else:
        declared = req.media_type
        if not declared:
            raise RuntimeError(
                'backend is AUTO but the media type could not be determined. '
                "Pass config='<name>' or media_type='<type>'."
            )
        candidates = [b for b in BACKENDS.values() if declared in b.media_types]

    live = [c for c in candidates if c and c.is_available(req)]
    if not live:
        raise RuntimeError(
            f"no backend available for '{req.media_type}'. "
            f"Tried: {', '.join(c.id for c in candidates if c) or 'none'}"
        )
    if len(live) > 1:
        raise RuntimeError(
            f"ambiguous: multiple backends available for '{req.media_type}'. "
            f"Specify backend= or config=. Tried: {', '.join(c.id for c in live)}"
        )
    return live[0]


# Populate the registry at import time.
for _cls in _BACKEND_CLASSES:
    register_backend(_cls)
