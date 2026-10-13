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
from typing import Optional

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
    server_url: Optional[str] = None     # override comfyui.url / endpoint base
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
            'server_url': self.server_url,
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
    server_backed: bool = False  # True if it talks to an external server (ComfyUI/TTS)
    requires_vram: bool = True   # participates in the VRAM save/unload/restore dance

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
