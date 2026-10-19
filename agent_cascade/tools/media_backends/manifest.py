"""Presentation + agent-help metadata for the ``media_gen`` backend registry.

LEAF module: imports only the standard library, never anything from
``agent_cascade`` — so ``base.py`` may reference these types and the backend
classes may declare a ``manifest`` without any import cycle.

Single source of truth: identity/capability fields (``id``, ``media_types``,
``server_backed``, ``requires_vram``) live on the backend CLASS and are copied
by :func:`build_manifest`; the manifest adds only presentation fields (UI
labels/fields), agent-help metadata (``__help`` sections), and behaviour
defaults (VRAM dance). The registry (``media_backends/__init__``) composes
``BACKEND_MANIFEST`` after the registry-population loop; consumers (the tool's
schema enum, the REST validation set, the UI's tabs/fields, ``__help``) read
the composed dict — adding a backend = adding a class + its ``manifest``.
"""

from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class FieldSpec:
    """One config field inside a backend block, as the UI renders it."""
    key: str                    # config key written inside the backend block
    label: str                  # UI label text
    kind: str                   # 'text' | 'path' | 'url' | 'number' | 'checkbox'
    required: bool = False
    placeholder: str = ''
    min: int | None = None
    max: int | None = None
    step: int | None = None


@dataclass(frozen=True)
class BackendManifest:
    """Presentation + agent-help metadata a backend class attaches to itself."""
    label: str                          # human name for tabs / __help headings
    note: str                           # one-line capability warning
    connection_fields: tuple = ()       # rendered by default
    advanced_fields: tuple = ()         # rendered inside a collapsed <details>
    availability: str = 'none'          # 'config_dir' | 'workflow_dir' | 'media_types' | 'none'
    agent_args: tuple = ()              # tool args this backend honours, for __help
    overrides_keys: tuple = ()          # voice/config keys passable via overrides={...}
    release_model_supported: bool = True
    defaults: dict = field(default_factory=dict)  # UI defaults; never written unless changed


def build_manifest(backend) -> dict:
    """Merge the backend CLASS identity with its presentation manifest.

    Identity/capability fields come from the class (single source), never
    re-declared in the manifest. Raises on a backend without a ``manifest``
    (the T11 consistency guard: a registered backend must document itself).
    """
    m = backend.manifest
    return {
        'id': backend.id,
        'media_types': list(backend.media_types),
        'server_backed': backend.server_backed,
        'requires_vram': backend.requires_vram,
        'release_model_supported': m.release_model_supported,
        'label': m.label,
        'note': m.note,
        'connection_fields': [asdict(f) for f in m.connection_fields],
        'advanced_fields': [asdict(f) for f in m.advanced_fields],
        'availability': m.availability,
        'agent_args': list(m.agent_args),
        'overrides_keys': list(m.overrides_keys),
        'defaults': dict(m.defaults),
    }
